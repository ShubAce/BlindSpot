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
 * the stream then ends, so a console renders them in place. An error the server answered before any
 * run started (a 400, 422, 429 or 503) says so with `refused` and its `status`: the question was not
 * asked, so a console can hand the words back to the reader.
 */

import { API_BASE, PATHS, type Citation, type SourceRef } from './api';
import type { LangCode } from './langs';

export type AgentMapAction = 'fly_to' | 'show_layer' | 'highlight';

/** One earlier question and answer a follow-up was given (answers without their citation markers),
 *  as the API sends and stores it. The console reads it into its own camelCase shape (model.ts). */
export type ThreadPairWire = { run_id: string; question: string; answer: string };

export type AgentEvent =
  | {
      type: 'run';
      run_id: string;
      model: string;
      lang?: string;
      /** a replay of the same question asked in the last few minutes */
      cached?: boolean;
      /** a follow-up: the run it follows, and exactly the questions and answers the model was given */
      follow_up_of?: string | null;
      context?: ThreadPairWire[];
      /** until when this run can itself be followed up (UTC); the stored run carries it if this does not */
      follow_up_until?: string | null;
    }
  | { type: 'thought'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; name: string; ok: boolean; summary: string; sources: SourceRef[] }
  | { type: 'map'; action: AgentMapAction; payload: Record<string, unknown> }
  | { type: 'answer_delta'; text: string }
  | { type: 'answer'; text: string; citations: Citation[] }
  | { type: 'verification'; checked: number; verified: number; unverified: string[]; passed: boolean }
  | {
      type: 'error';
      message: string;
      retryable: boolean;
      /** the server refused the question before any run started (its HTTP status is `status`) */
      refused?: boolean;
      status?: number;
      /** seconds the server asked us to wait (Retry-After) */
      retryAfterS?: number;
      /** the connection failed, not the run: once a run has started it finishes on the server and is
       *  stored (GET /api/agent/runs/{id}), so its answer can still be fetched */
      transport?: boolean;
    }
  | { type: 'done'; run_id: string; duration_ms: number }
  /** An event this client does not know yet: kept, not dropped, so a console can show it. */
  | { type: 'unknown'; event: string; data: unknown };

export type AgentEventType = AgentEvent['type'];

export type AskBody = {
  question: string;
  lang?: LangCode;
  /** Ask a follow-up of this earlier run (its `run_id`); the server gives the model that question and answer as context. */
  follow_up_of?: string;
};

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

const sentence = (s: string) => (/[.!?]$/.test(s.trim()) ? s.trim() : `${s.trim()}.`);

/** Where a 422's pydantic errors point: "question", "follow_up_of" or null. */
function invalidField(detail: unknown): string | null {
  if (!Array.isArray(detail)) return null;
  for (const e of detail) {
    const loc = (e as { loc?: unknown } | null)?.loc;
    if (Array.isArray(loc) && loc.includes('follow_up_of')) return 'follow_up_of';
    if (Array.isArray(loc) && loc.includes('question')) return 'question';
  }
  return null;
}

/** A follow-up whose earlier run's window has closed: the server's refusal, and the console's own
 *  note when the window closes while a follow-up is being written. */
export const FOLLOW_UP_TOO_OLD = 'This conversation is too old to continue: ask the full question.';

/**
 * One kind sentence for a question the server refused before starting a run. The API's own `detail`
 * names a run id ("Cannot follow up 5cd6...: it is more than 24 h old ..."), so a follow-up refusal is
 * said in the reader's words instead, by what it means; a 429's detail is already a plain sentence.
 */
export function refusalMessage(status: number, detail: unknown, followUp: boolean, retryAfterS?: number): string {
  const text = typeof detail === 'string' ? detail : '';
  if (status === 400 && followUp) {
    if (/more than [\d.]+ h old/i.test(text)) return FOLLOW_UP_TOO_OLD;
    if (/without an answer/i.test(text)) return 'The earlier investigation ended without an answer, so there is nothing to follow up: ask the full question.';
    if (/no finished investigation/i.test(text)) return 'The earlier investigation could not be found, so this cannot be a follow-up: ask the full question.';
    if (/not an investigation/i.test(text)) return 'Only an investigation can be followed up: ask the full question.';
    return 'This conversation cannot be continued: ask the full question.';
  }
  if (status === 422) {
    const field = invalidField(detail);
    if (field === 'follow_up_of') return 'The earlier investigation was not recognised, so this cannot be a follow-up: ask the full question.';
    if (field === 'question') return 'Write a question of 3 to 600 characters.';
    return 'The investigator could not read this question.';
  }
  if (status === 429) {
    if (text) return sentence(text);
    return retryAfterS ? `Too many questions from this address; try again in ${Math.ceil(retryAfterS)} s.` : 'The investigator is answering other questions; try again in a minute.';
  }
  if (status === 503 && followUp) return 'The earlier answer could not be read just now. Try again in a moment, or ask the full question.';
  if (status === 503) return text ? `The investigator is unavailable right now (${text.replace(/\.$/, '')}). Try again in a minute.` : 'The investigator is unavailable right now. Try again in a minute.';
  if (status >= 500) return `The investigator could not start (${status}). Try again in a minute.`;
  if (text) return `The investigator refused the question: ${sentence(text)}`;
  return `The investigator could not start (${status}).`;
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
      const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
      yield {
        type: 'error',
        message: timedOut ? 'The investigator did not answer in time.' : offline ? 'No connection: a new question needs one.' : 'The investigator is not reachable.',
        retryable: true,
        transport: true,
      };
      return;
    }

    if (!res.ok || !res.body) {
      let detail: unknown = null;
      try {
        detail = ((await res.json()) as { detail?: unknown }).detail ?? null;
      } catch {
        detail = null; // not JSON (a proxy's page): the status says enough
      }
      const ra = Number(res.headers.get('Retry-After'));
      const retryAfterS = Number.isFinite(ra) && ra > 0 ? ra : undefined;
      // 200 with no body is not a refusal: the run may have started, so it is not handed back
      const refused = !res.ok;
      yield {
        type: 'error',
        message: refusalMessage(res.status, detail, Boolean(body.follow_up_of), retryAfterS),
        retryable: res.status === 429 || res.status >= 500,
        refused,
        status: res.status,
        retryAfterS,
      };
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
          transport: true,
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
      yield { type: 'error', message: 'The investigator stopped before finishing.', retryable: true, transport: true };
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
