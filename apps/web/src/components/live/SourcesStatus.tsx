'use client';

import { useEffect, useId, useMemo, useRef, useState } from 'react';

import { ErrorNote, Skeleton } from '@/components/ui/states';
import type { SourceStatus } from '@/lib/api';
import { number, relative } from '@/lib/format';
import { useNow } from '@/lib/live';

import { useSourcesLive } from './data';
import styles from './styles';

/**
 * Each upstream's freshness (/api/sources), compactly: one line in the title row ("21 of 22 sources
 * fresh", with the first problem named) that opens the full list, problems first. Every number on
 * the screen comes from one of these sources.
 */

const ORDER: Record<string, number> = { error: 0, stale: 1, never: 2, ok: 3 };

/** "NASA FIRMS active fires (VIIRS 375 m, ...)" -> "NASA FIRMS active fires". */
const shortLabel = (s: SourceStatus) => s.label.split(/\s[(:]|;\s/)[0].replace(/,\s*$/, '');

const STATUS_TEXT: Record<string, string> = { error: 'failed', stale: 'stale', never: 'never ran' };
const TONE: Record<string, string> = { error: 'negative', stale: 'warning', never: 'neutral' };

function StatusMark({ s, named = false }: { s: SourceStatus; named?: boolean }) {
  if (s.status === 'ok') return null;
  // In the title row the pill names the source ("NASA FIRMS failed"): a bare "failed" beside
  // "fresh" read as if mission control itself had failed.
  const name = named ? `${shortLabel(s).split(' ').slice(0, 2).join(' ')} ` : '';
  return (
    <span className={styles.pill} data-tone={TONE[s.status] ?? 'neutral'}>
      {name}
      {STATUS_TEXT[s.status] ?? s.status}
    </span>
  );
}

export function SourcesStatus() {
  const src = useSourcesLive();
  const now = useNow(30_000);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const id = useId();

  const rows = useMemo(
    () =>
      (src.data?.sources ?? [])
        .filter((s) => s.kind !== 'unregistered')
        .slice()
        .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || shortLabel(a).localeCompare(shortLabel(b))),
    [src.data],
  );
  const ok = rows.filter((r) => r.status === 'ok').length;
  const problem = rows.find((r) => r.status !== 'ok');

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    const onDown = (e: PointerEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onDown);
    };
  }, [open]);

  return (
    <div className={styles.sourcesBox} ref={box}>
      {src.error && !src.data ? (
        <ErrorNote what="source status" error={src.error} onRetry={() => void src.mutate()} style={{ fontSize: 12 }} />
      ) : !src.data ? (
        <Skeleton width={170} height={16} />
      ) : (
        <button type="button" className={styles.sourcesButton} aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)}>
          <span>
            Sources: <span className="nums">{number(ok)}</span> of <span className="nums">{number(rows.length)}</span> fresh
          </span>
          {problem ? <StatusMark s={problem} named /> : null}
        </button>
      )}
      {open && src.data ? (
        <div id={id} className={styles.sourcesPanel} role="region" aria-label="Sources and their freshness">
          <p className={styles.sourcesPanelHead}>
            Last good fetch of each source{src.data.generated_at && now ? `, checked ${relative(src.data.generated_at, now)}` : ''}
          </p>
          <ul className={styles.sources}>
            {rows.map((s) => (
              <li key={s.key} title={s.last_error ? `${s.label}\nLast error: ${s.last_error}` : s.label}>
                <span className={styles.sourceName}>{shortLabel(s)}</span>
                <StatusMark s={s} />
                <span className={`${styles.sourceAge} nums`}>{s.last_success_at && now ? relative(s.last_success_at, now) : <span className="missing">—</span>}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
