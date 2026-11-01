'use client';

import { number } from '@/lib/format';
import { langEnglish } from '@/lib/langs';

import { useReadAloud, type VoiceLang } from './voice';
import './voice.css';

/**
 * "Listen": reads a text aloud with the device's own voice for its language, with pause and stop.
 * Renders nothing where the browser cannot speak; where it can but has no voice for the language
 * (Punjabi on most laptops), it says so instead of reading Punjabi with a Hindi or English voice.
 * The first button keeps its place as it turns from Listen to Pause to Resume, so keyboard focus
 * stays on it.
 *
 *   <Listen id={`answer-${runId}`} text={speakable(answer, 'en')} lang="en" what="this answer" />
 */
export function Listen({
  id,
  text,
  lang,
  what,
  size = 'small',
  className,
}: {
  /** Names this reader on the page; starting another reader stops this one. */
  id: string;
  /** What to read, already in words for the ear (see speakable()). */
  text: string;
  lang: VoiceLang;
  /** What is read, for the buttons' names: "this answer", "the Hindi bulletin". */
  what: string;
  /** 'small' sits among 28px controls (the investigator), 'regular' among 36px ones (the bulletin). */
  size?: 'small' | 'regular';
  className?: string;
}) {
  const r = useReadAloud(id, text, lang);
  if (!r.supported || !text.trim()) return null;
  const cls = `vo${className ? ` ${className}` : ''}`;
  // English falls back to the browser's default voice; Hindi and Punjabi are never read by another language's voice
  if (r.ready && !r.voice && lang !== 'en') {
    return (
      <p className={`${cls} vo-none`} data-size={size} role="note">
        This device has no {langEnglish(lang)} voice to read it aloud.
      </p>
    );
  }
  const reading = r.status !== 'idle';
  const main =
    r.status === 'speaking'
      ? { label: 'Pause', name: `Pause reading ${what}`, icon: <PauseIcon />, act: r.pause }
      : r.status === 'paused'
        ? { label: 'Resume', name: `Resume reading ${what}`, icon: <PlayIcon />, act: r.resume }
        : { label: 'Listen', name: `Listen to ${what}`, icon: <SpeakerIcon />, act: r.start };
  return (
    <div className={cls} data-size={size} data-status={r.status} role="group" aria-label={`Read ${what} aloud`}>
      <button type="button" className="vo-button" onClick={main.act} aria-label={main.name} title={r.voice ? `Read by ${r.voice.name}` : undefined}>
        {main.icon}
        <span aria-hidden="true">{main.label}</span>
      </button>
      {reading ? (
        <button type="button" className="vo-button" onClick={r.stop} aria-label={`Stop reading ${what}`}>
          <StopIcon />
          <span aria-hidden="true">Stop</span>
        </button>
      ) : null}
      {reading && r.total > 1 ? (
        <span className="vo-progress">
          {r.status === 'paused' ? 'Paused at ' : ''}
          {`${number(r.index + 1)} of ${number(r.total)}`}
        </span>
      ) : null}
      {!reading && r.error ? (
        <span className="vo-progress" role="status">
          {r.error}
        </span>
      ) : null}
    </div>
  );
}

const icon = { width: 14, height: 14, viewBox: '0 0 14 14', 'aria-hidden': true } as const;

function SpeakerIcon() {
  return (
    <svg {...icon}>
      <path d="M2 5.2h2.2L7.4 2.6v8.8L4.2 8.8H2z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M9.6 4.8a3 3 0 010 4.4M11.2 3.2a5.2 5.2 0 010 7.6" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}
function PauseIcon() {
  return (
    <svg {...icon}>
      <path d="M4.5 3v8M9.5 3v8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
function PlayIcon() {
  return (
    <svg {...icon}>
      <path d="M4 2.8v8.4L11 7z" fill="currentColor" />
    </svg>
  );
}
function StopIcon() {
  return (
    <svg {...icon}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1" fill="currentColor" />
    </svg>
  );
}
