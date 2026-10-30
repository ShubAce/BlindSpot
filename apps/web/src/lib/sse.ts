/**
 * The investigator's stream: POST /api/agent/ask returns text/event-stream.
 *
 * EventSource can only GET, so this reads the fetch body as a stream and parses Server-Sent Events
 * by hand (WHATWG rules: `event:` / `data:` / `id:` fields, blank line ends an event, `:` lines are
 * comments such as keep-alive pings, CRLF or LF). Each `data:` payload is one JSON object
 * (architecture.md section 5). The event name comes from the `event:` field, or from a `type` key in
 * the JSON when the server sends bare `data:` lines.
 *
 *   const ctrl = new AbortController();
 *   for await (const ev of streamAgent({ question: 'Why is Delhi\'s air bad today?' }, { signal: ctrl.signal })) {
 *     if (ev.type === 'answer_delta') appendText(ev.text);
 *   }
 *
 * Failures never throw out of the generator: they arrive as an `error` event (with `retryable`), and
 * the stream then ends, so a console renders them in place.
 */

import { API_BASE, PATHS, type Citation, type SourceRef } from './api';

export type AgentMapAction = 'fly_to' | 'show_layer' | 'highlight';

export type AgentEvent =
  | { type: 'run'; run_id: string; model: string }
  | { type: 'thought'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; name: string; ok: boolean; summary: string; sources: SourceRef[] }
  | { type: 'map'; action: AgentMapAction; payload: Record<string, unknown> }
  | { type: 'answer_delta'; text: string }
  | { type: 'answer'; text: string; citations: Citation[] }
  | { type: 'verification'; checked: number; verified: number; unverified: string[]; passed: boolean }
  | { type: 'error'; message: string; retryable: boolean }
  | { type: 'done'; run_id: string; duration_ms: number }
  /** An event this client does not know yet: kept, not dropped, so a console can show it. */
  | { type: 'unknown'; event: string; data: unknown };

export type AgentEventType = AgentEvent['type'];

export type AskBody = { question: string; lang?: 'en' | 'hi' | 'pa' };

export type StreamOptions = {
  signal?: AbortSignal;
  /** Give up if no byte arrives for this long (the server pings while tools run). Default 90 s. */
  idleTimeoutMs?: number;
  /** Give up if the response headers do not arrive in time. Default 20 s. */
  connectTimeoutMs?: number;
  base?: string;
};

const KNOWN = new Set<AgentEventType>([
  'run', 'thought', 'tool_call', 'tool_result', 'map', 'answer_delta', 'answer', 'verification', 'error', 'done',
]);

/** One raw SSE event, before JSON parsing. */
export type RawSseEvent = { event: string; data: string; id: string | null };

/**
 * Incremental SSE parser: feed it decoded text chunks, get complete events back. Exposed for tests
 * and for any other SSE endpoint.
 */
export class SseParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];
  private id: string | null = null;

  push(chunk: string): RawSseEvent[] {
    this.buffer += chunk;
    const out: RawSseEvent[] = [];
    // A line ends at \n, \r\n or \r. Keep a trailing \r: it may be the first half of a \r\n.
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const c = this.buffer[i];
      if (c !== '\n' && c !== '\r') continue;
      if (c === '\r' && i === this.buffer.length - 1) break;
      const line = this.buffer.slice(start, i);
      if (c === '\r' && this.buffer[i + 1] === '\n') i++;
      start = i + 1;
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  /** Flush at end of stream: an event without its final blank line still counts. */
  end(): RawSseEvent[] {
    const out: RawSseEvent[] = [];
    if (this.buffer) {
      // push() holds back a trailing \r (it may have been half of a \r\n); at the end it is a
      // line ending, not part of the value.
      const ev = this.line(this.buffer.replace(/\r$/, ''));
      if (ev) out.push(ev);
      this.buffer = '';
    }
    const last = this.dispatch();
    if (last) out.push(last);
    return out;
  }

  private line(line: string): RawSseEvent | null {
    if (line === '') return this.dispatch();
    if (line.startsWith(':')) return null; // comment / keep-alive
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
    else if (field === 'id') this.id = value;
    return null; // `retry:` and unknown fields are ignored
  }

  private dispatch(): RawSseEvent | null {
    if (!this.data.length) {
      this.event = '';
      return null;
    }
    const ev = { event: this.event || 'message', data: this.data.join('\n'), id: this.id };
    this.event = '';
    this.data = [];
    return ev;
  }
}

/** Turn a raw SSE event into a typed AgentEvent. */
export function toAgentEvent(raw: RawSseEvent): AgentEvent {
  let payload: unknown;
  try {
    payload = JSON.parse(raw.data);
  } catch {
    payload = raw.data;
  }
  const obj = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : null;
  const name = raw.event !== 'message' ? raw.event : typeof obj?.type === 'string' ? obj.type : typeof obj?.event === 'string' ? obj.event : 'message';
  if (!KNOWN.has(name as AgentEventType) || !obj) return { type: 'unknown', event: name, data: payload };
  // Some servers wrap the fields: {type, data: {...}}. Accept both.
  const fields = obj.data && typeof obj.data === 'object' && !Array.isArray(obj.data) ? (obj.data as Record<string, unknown>) : obj;
  return { ...fields, type: name } as AgentEvent;
}

/** Stream the investigator's events for one question. */
export async function* streamAgent(body: AskBody, opts: StreamOptions = {}): AsyncGenerator<AgentEvent, void, undefined> {
  const controller = new AbortController();
  const onAbort = () => controller.abort(opts.signal?.reason);
  opts.signal?.addEventListener('abort', onAbort, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  const arm = (ms: number) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, ms);
  };

  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  try {
    arm(opts.connectTimeoutMs ?? 20_000);
    let res: Response;
    try {
      res = await fetch(`${(opts.base ?? API_BASE).replace(/\/+$/, '')}${PATHS.agentAsk}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal: controller.signal,
        cache: 'no-store',
      });
    } catch {
      if (opts.signal?.aborted) return;
      yield {
        type: 'error',
        message: timedOut ? 'The investigator did not answer in time.' : 'The investigator is not reachable.',
        retryable: true,
      };
      return;
    }

    if (!res.ok || !res.body) {
      let detail = '';
      try {
        const j = (await res.json()) as { detail?: unknown };
        detail = typeof j.detail === 'string' ? j.detail : '';
      } catch {
        detail = '';
      }
      const retryable = res.status === 429 || res.status >= 500;
      const message = res.status === 429
        ? 'The investigator is answering other questions; try again in a minute.'
        : detail
          ? `The investigator refused the question: ${detail}.`
          : `The investigator could not start (${res.status}).`;
      yield { type: 'error', message, retryable };
      return;
    }

    reader = res.body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    const idle = opts.idleTimeoutMs ?? 90_000;
    arm(idle);
    let sawDone = false;
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch {
        if (opts.signal?.aborted) return;
        yield {
          type: 'error',
          message: timedOut ? 'The investigator went quiet for too long.' : 'The connection to the investigator dropped.',
          retryable: true,
        };
        return;
      }
      arm(idle);
      const raws = chunk.done ? parser.push(decoder.decode()).concat(parser.end()) : parser.push(decoder.decode(chunk.value, { stream: true }));
      for (const raw of raws) {
        const ev = toAgentEvent(raw);
        if (ev.type === 'done') sawDone = true;
        yield ev;
      }
      if (chunk.done) break;
    }
    if (!sawDone && !opts.signal?.aborted) {
      yield { type: 'error', message: 'The investigator stopped before finishing.', retryable: true };
    }
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener('abort', onAbort);
    if (reader) {
      try {
        await reader.cancel();
      } catch {
        // already closed
      }
    }
  }
}
