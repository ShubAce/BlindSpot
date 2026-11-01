'use client';

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { isLangCode, langEnglish, type LangCode } from '@/lib/langs';
import { plainText } from '@/lib/text';

/**
 * Voice, as progressive enhancement: speak a question into the investigator's box (the Web Speech
 * API's SpeechRecognition) and listen to an answer or the bulletin (speechSynthesis), in English,
 * Hindi or Punjabi. Where a browser has neither, nothing renders and nothing breaks.
 *
 * The investigator console and the bulletin both use it (hence components/voice, not either page's
 * folder), so the read-aloud control looks and behaves the same on both pages.
 *
 *   const dict = useDictation((text) => setDraft(text));   // never submits: the reader presses Ask
 *   dict.start(draft);  dict.stop();  dict.switchLang('hi', draft);
 *
 *   const r = useReadAloud('answer-<run>', text, 'hi');      // r.start(), r.pause(), r.resume(), r.stop()
 */

/** The site's three languages (lib/langs.ts): voice adds only their speech tags. */
export type VoiceLang = LangCode;

/** BCP 47 tags for recognition and voices: the Indian variant of each language. */
export const SPEECH_TAGS: Readonly<Record<VoiceLang, string>> = { en: 'en-IN', hi: 'hi-IN', pa: 'pa-IN' };

/** The language a text is written in, by its script: Gurmukhi is Punjabi, Devanagari Hindi, else English. */
export function scriptLang(text: string): VoiceLang {
  let deva = 0;
  let guru = 0;
  let latin = 0;
  for (const ch of text) {
    const c = ch.codePointAt(0) ?? 0;
    if (c >= 0x0a00 && c <= 0x0a7f) guru++;
    else if (c >= 0x0900 && c <= 0x097f) deva++;
    else if ((c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a)) latin++;
  }
  if (guru > deva && guru >= latin / 4) return 'pa';
  if (deva > guru && deva >= latin / 4) return 'hi';
  return 'en';
}

const noop = () => () => undefined;

/** A browser capability, false on the server and during hydration, so the markup matches and the
 *  control appears only once the browser has said it can. */
function useCapability(check: () => boolean): boolean {
  return useSyncExternalStore(noop, check, () => false);
}

// ---------------------------------------------------------------------------------------------
// listening: SpeechRecognition (Chrome, Edge and Safari name it webkitSpeechRecognition)

type RecognitionResultList = ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onstart: (() => void) | null;
  onresult: ((e: { results: RecognitionResultList }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
};
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}
const hasRecognition = () => recognitionCtor() != null;

const LANG_KEY = 'blindspot:voice-lang';

/** The reader's last choice, else the page's language, else the browser's first, else English. */
function startingLang(): VoiceLang {
  try {
    const kept = window.localStorage.getItem(LANG_KEY);
    if (isLangCode(kept)) return kept;
  } catch {
    // storage blocked: the page and the browser decide
  }
  const page = document.documentElement.lang.slice(0, 2).toLowerCase();
  if (page === 'hi' || page === 'pa') return page;
  const first = (navigator.languages?.[0] ?? navigator.language ?? '').slice(0, 2).toLowerCase();
  return first === 'hi' || first === 'pa' ? first : 'en';
}

function keepLang(lang: VoiceLang) {
  try {
    window.localStorage.setItem(LANG_KEY, lang);
  } catch {
    // the choice lasts for this page
  }
}

/** What went wrong, in the reader's words, with what to do instead. */
function hearingError(code: string, lang: VoiceLang): string {
  switch (code) {
    case 'no-speech':
      return 'No speech was heard. Press the microphone and speak, or type the question.';
    case 'audio-capture':
      return 'No microphone was found on this device. Type the question instead.';
    case 'not-allowed':
      return 'The microphone is blocked for this site. Allow it in the browser’s site settings, or type the question.';
    // the browser's own speech service is off or missing (Safari with Siri and Dictation off, a
    // managed browser): the site's microphone permission has nothing to do with it
    case 'service-not-allowed':
      return 'Speech recognition is turned off or unavailable in this browser. Type the question instead.';
    case 'network':
      return 'Speech recognition in this browser needs a connection. Type the question instead.';
    case 'language-not-supported':
      return `This browser cannot recognise ${langEnglish(lang)} speech. Choose another language, or type the question.`;
    default:
      return 'Listening stopped before a question was heard. Try again, or type the question.';
  }
}

const joinText = (base: string, said: string) => [base.trim(), said.trim()].filter(Boolean).join(' ');

export type Dictation = {
  /** The browser can listen (false on the server and before hydration). */
  supported: boolean;
  listening: boolean;
  /** The language it listens in: the last one used, or where it will start. */
  lang: VoiceLang | null;
  error: string | null;
  /** Listen, adding what is said after `base` (the words already in the box). */
  start: (base: string) => void;
  /** Stop listening and keep what was heard. */
  stop: () => void;
  /** Stop listening and take no more words (the question was sent, or the reader is typing). */
  abort: () => void;
  /** Listen in another language from now on; restarts, keeping `base`, when listening. */
  switchLang: (lang: VoiceLang, base: string) => void;
  clearError: () => void;
};

/**
 * Dictation into a text box. `onText` gets the box's whole new text as words arrive (interim results
 * included, so the reader sees them); it never submits. One utterance at a time: recognition stops
 * by itself after a pause, or with stop().
 */
export function useDictation(onText: (text: string) => void): Dictation {
  const supported = useCapability(hasRecognition);
  const [listening, setListening] = useState(false);
  const [lang, setLang] = useState<VoiceLang | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const sink = useRef(onText);
  useEffect(() => {
    sink.current = onText;
  });

  const listen = useCallback((base: string, chosen: VoiceLang) => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const prev = rec.current;
    rec.current = null;
    prev?.abort();
    const r = new Ctor();
    r.lang = SPEECH_TAGS[chosen];
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    let heard = false;
    let failed = false;
    r.onstart = () => {
      if (rec.current === r) setListening(true);
    };
    r.onresult = (e) => {
      if (rec.current !== r) return;
      let said = '';
      for (let i = 0; i < e.results.length; i++) said += e.results[i][0]?.transcript ?? '';
      if (said.trim()) heard = true;
      sink.current(joinText(base, said));
    };
    r.onerror = (e) => {
      if (rec.current !== r || e.error === 'aborted') return;
      failed = true;
      setError(hearingError(e.error, chosen));
    };
    r.onend = () => {
      if (rec.current !== r) return;
      rec.current = null;
      setListening(false);
      if (!heard && !failed) setError(hearingError('no-speech', chosen));
    };
    rec.current = r;
    setLang(chosen);
    setError(null);
    try {
      r.start();
      setListening(true);
    } catch {
      rec.current = null;
      setListening(false);
      setError(hearingError('', chosen));
    }
  }, []);

  const start = useCallback((base: string) => listen(base, lang ?? startingLang()), [lang, listen]);
  // with no recogniser live (it was ended under the console) there is nothing to stop: the state
  // still says listening, so it is cleared here, or the pressed microphone could never be released
  const stop = useCallback(() => {
    if (rec.current) rec.current.stop();
    else setListening(false);
  }, []);
  const abort = useCallback(() => {
    const r = rec.current;
    rec.current = null;
    r?.abort();
    setListening(false);
  }, []);
  const switchLang = useCallback(
    (next: VoiceLang, base: string) => {
      keepLang(next);
      setLang(next);
      if (rec.current) listen(base, next);
    },
    [listen],
  );
  const clearError = useCallback(() => setError(null), []);

  // Leaving the page (or the console) ends the microphone at once. Next can keep the page mounted but
  // hidden (React's <Activity>), which runs this cleanup and keeps the state: the late `onend` of the
  // aborted recogniser is ignored (it is no longer rec.current), so `listening` is settled here.
  useEffect(
    () => () => {
      const r = rec.current;
      rec.current = null;
      r?.abort();
      setListening(false);
    },
    [],
  );

  return { supported, listening, lang, error, start, stop, abort, switchLang, clearError };
}

// ---------------------------------------------------------------------------------------------
// reading aloud: speechSynthesis, one reader at a time on the page

const synth = (): SpeechSynthesis | null =>
  typeof window !== 'undefined' && 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance === 'function' ? window.speechSynthesis : null;
const hasSynthesis = () => synth() != null;

type VoiceList = { list: readonly SpeechSynthesisVoice[]; ready: boolean };
const NO_VOICES: VoiceList = { list: [], ready: false };
let voices: VoiceList = NO_VOICES;
const voiceListeners = new Set<() => void>();
let voicesStarted = false;

function setVoices(next: VoiceList) {
  voices = next;
  for (const l of voiceListeners) l();
}

/** Voices arrive late in Chrome (voiceschanged) and at once elsewhere; after 2 s the list is what it is. */
function startVoices() {
  const s = synth();
  if (!s || voicesStarted) return;
  voicesStarted = true;
  const read = () => {
    const list = s.getVoices();
    if (list.length || voices.list.length) setVoices({ list, ready: list.length > 0 || voices.ready });
  };
  read();
  s.addEventListener?.('voiceschanged', read);
  window.setTimeout(() => setVoices({ list: s.getVoices(), ready: true }), 2_000);
  // leaving the page (closing it, or the back-forward cache keeping it) ends any reading
  window.addEventListener('pagehide', () => stopAll());
}

function subscribeVoices(cb: () => void) {
  voiceListeners.add(cb);
  startVoices();
  return () => {
    voiceListeners.delete(cb);
  };
}

const norm = (tag: string) => tag.replace(/_/g, '-').toLowerCase();

/**
 * The best voice for a language: its Indian variant first (en-IN, hi-IN, pa-IN), then any voice of
 * the language (for English, British before American, closer to Indian English), the higher quality
 * ones (Google, "Natural", "Neural") before the rest. Null when the device has none: a Punjabi text
 * is never read by a Hindi or English voice.
 */
export function pickVoice(list: readonly SpeechSynthesisVoice[], lang: VoiceLang): SpeechSynthesisVoice | null {
  const want = norm(SPEECH_TAGS[lang]);
  const score = (v: SpeechSynthesisVoice): number => {
    const tag = norm(v.lang);
    const base = tag.split('-')[0];
    if (base !== lang) return -1;
    let s = 0;
    if (tag === want || (tag.startsWith(`${lang}-`) && tag.endsWith('-in'))) s += 100;
    else if (lang === 'en' && tag === 'en-gb') s += 40;
    else if (lang === 'en' && tag === 'en-us') s += 30;
    else s += 10;
    if (/google|natural|neural|online/i.test(v.name)) s += 5;
    if (v.localService) s += 1;
    return s;
  };
  let best: SpeechSynthesisVoice | null = null;
  let top = -1;
  for (const v of list) {
    const s = score(v);
    if (s > top) {
      top = s;
      best = v;
    }
  }
  return top >= 0 ? best : null;
}

export type ReadStatus = 'idle' | 'speaking' | 'paused';
type Reader = { owner: string | null; status: ReadStatus; index: number; total: number; error: string | null; errorOwner: string | null };
const IDLE: Reader = { owner: null, status: 'idle', index: 0, total: 0, error: null, errorOwner: null };

let reader: Reader = IDLE;
const readerListeners = new Set<() => void>();
let chunks: string[] = [];
let voice: SpeechSynthesisVoice | null = null;
let tag = 'en-IN';
/** Bumped on every start, pause and stop, so the events of a cancelled queue are ignored. */
let generation = 0;
/** Utterances still queued: held here, or some browsers collect them and never fire their end. */
let queue: SpeechSynthesisUtterance[] = [];

function setReader(patch: Partial<Reader>) {
  reader = { ...reader, ...patch };
  for (const l of readerListeners) l();
}
function subscribeReader(cb: () => void) {
  readerListeners.add(cb);
  return () => {
    readerListeners.delete(cb);
  };
}

/** Sentences, each short enough that no browser cuts it off (Chrome stops a long utterance after about 15 s). */
export function sentences(text: string): string[] {
  const out: string[] = [];
  const flat = text.replace(/\s+/g, ' ').trim();
  let start = 0;
  for (let i = 0; i < flat.length; i++) {
    // a sentence ends at . ! ? or the danda (।, ॥) followed by a space; "4.5" and "U.S." mid-word do not
    if ('.!?।॥'.includes(flat[i]) && flat[i + 1] === ' ') {
      out.push(flat.slice(start, i + 1));
      start = i + 2;
    }
  }
  if (start < flat.length) out.push(flat.slice(start));
  const short: string[] = [];
  for (let s of out) {
    while (s.length > 220) {
      const comma = Math.max(s.lastIndexOf(', ', 200), s.lastIndexOf('; ', 200));
      const space = s.lastIndexOf(' ', 200);
      const at = comma > 80 ? comma + 1 : space > 80 ? space : 200;
      short.push(s.slice(0, at).trim());
      s = s.slice(at).trim();
    }
    if (s.trim()) short.push(s.trim());
  }
  return short;
}

/** Whether the device is still speaking or holds a queue (read before cancelling it). */
const busy = () => {
  const s = synth();
  return Boolean(s && (s.speaking || s.pending));
};

/**
 * Queue the sentences from `i` on. `wasBusy`: something was speaking a moment ago. Then the cancel
 * gets a beat before the new queue (Chrome can drop an utterance queued in the same moment as a
 * cancel); otherwise the queue starts at once, inside the click, as iOS Safari requires.
 */
function speakFrom(i: number, wasBusy: boolean) {
  const s = synth();
  if (!s) return;
  const run = ++generation;
  if (wasBusy) s.cancel();
  queue = [];
  setReader({ status: 'speaking', index: i });
  const enqueue = () => {
    if (run !== generation) return;
    for (let k = i; k < chunks.length; k++) {
      const u = new SpeechSynthesisUtterance(chunks[k]);
      if (voice) u.voice = voice;
      u.lang = voice?.lang ?? tag;
      u.onstart = () => {
        if (run === generation) setReader({ index: k });
      };
      u.onend = () => {
        if (run === generation && k === chunks.length - 1) {
          queue = [];
          setReader({ owner: null, status: 'idle', index: 0, total: 0 });
        }
      };
      u.onerror = (e) => {
        // our own cancel (pause, stop, another reader) is not a failure
        if (run !== generation || e.error === 'interrupted' || e.error === 'canceled') return;
        generation++;
        s.cancel();
        queue = [];
        setReader({ owner: null, status: 'idle', error: 'This device could not read it aloud.', errorOwner: reader.owner });
      };
      queue.push(u);
      s.speak(u);
    }
    // a queue left paused by an earlier pause() would never start
    if (s.paused) s.resume();
  };
  if (wasBusy) window.setTimeout(enqueue, 80);
  else enqueue();
}

function stopAll() {
  const s = synth();
  generation++;
  queue = [];
  s?.cancel();
  if (reader.owner || reader.status !== 'idle') setReader({ owner: null, status: 'idle', index: 0, total: 0 });
}

export type ReadAloud = {
  /** The browser can speak (false on the server and before hydration). */
  supported: boolean;
  /** The voice list has loaded (or 2 s have passed): `voice` null now means the device has none. */
  ready: boolean;
  voice: SpeechSynthesisVoice | null;
  status: ReadStatus;
  /** The sentence being read (0-based) and how many there are. */
  index: number;
  total: number;
  error: string | null;
  start: () => void;
  pause: () => void;
  resume: () => void;
  stop: () => void;
};

/**
 * Read `text` aloud in `lang` with the device's best voice for it. `id` names this reader: starting
 * one stops any other on the page, and a reader stops when its text or language changes or when its
 * component goes (a navigation, the console closing). Pause ends the sentence being read and resume
 * starts it again, which works the same in every browser (speechSynthesis.pause does not on Android).
 */
export function useReadAloud(id: string, text: string, lang: VoiceLang): ReadAloud {
  const supported = useCapability(hasSynthesis);
  const list = useSyncExternalStore(subscribeVoices, () => voices, () => NO_VOICES);
  const state = useSyncExternalStore(subscribeReader, () => reader, () => IDLE);
  const best = useMemo(() => pickVoice(list.list, lang), [list, lang]);
  const parts = useMemo(() => sentences(text), [text]);

  useEffect(
    () => () => {
      if (reader.owner === id) stopAll();
    },
    [id, text, lang],
  );

  const mine = state.owner === id;
  return {
    supported,
    ready: list.ready,
    voice: best,
    status: mine ? state.status : 'idle',
    index: mine ? state.index : 0,
    total: mine ? state.total : parts.length,
    error: state.errorOwner === id ? state.error : null,
    start: () => {
      if (!parts.length) return;
      const wasBusy = busy();
      stopAll();
      chunks = parts;
      voice = best;
      tag = SPEECH_TAGS[lang];
      setReader({ owner: id, status: 'speaking', index: 0, total: parts.length, error: null, errorOwner: null });
      speakFrom(0, wasBusy);
    },
    pause: () => {
      if (reader.owner !== id) return;
      generation++;
      queue = [];
      synth()?.cancel();
      setReader({ status: 'paused' });
    },
    resume: () => {
      if (reader.owner === id) speakFrom(reader.index, busy());
    },
    stop: () => {
      if (reader.owner === id) stopAll();
    },
  };
}

// ---------------------------------------------------------------------------------------------
// text as it should sound

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

/**
 * Text for the ear: citation markers and bold marks out, each bullet its own sentence, and in
 * English the notation a voice would spell out ("2026-11-01", "µg/m³", "95% CI 68–96%") in words.
 * Only the notation changes, never a figure.
 */
export function speakable(text: string, lang: VoiceLang): string {
  // markers out before a line's end is checked, so "… fires. [2]" does not gain a second full stop
  let out = text
    .split(/\n+/)
    .map((l) => plainText(l).replace(/^[-•*]\s+/, ''))
    .filter(Boolean)
    .map((l) => (/[.!?।॥:]$/.test(l) ? l : `${l}.`))
    .join(' ');
  if (lang === 'en') {
    out = out
      .replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, (all, y: string, m: string, d: string) => (MONTHS[Number(m) - 1] ? `${Number(d)} ${MONTHS[Number(m) - 1]} ${y}` : all))
      .replace(/µg\/m³|μg\/m³|ug\/m3/g, 'micrograms per cubic metre')
      .replace(/(\d)\s?km\/h\b/g, '$1 kilometres an hour')
      .replace(/(\d)\s?ha\b/g, '$1 hectares')
      .replace(/\b95% CI\b/g, '95% confidence interval')
      // "67.9%–95.5%" as well as "0.5–0.65"
      .replace(/(\d%?)\s?[–—]\s?(\d)/g, '$1 to $2');
  }
  return out;
}
