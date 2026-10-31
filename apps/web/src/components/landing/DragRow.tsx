'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';

import { s } from './styles';

/**
 * The drag row's frame (direction: "Drag row of moments"): a horizontally scrolling, snapping list,
 * back and forward buttons that move it by one column, and a 6px bar whose ink segment shows how far
 * along the row is. The row scrolls by touch, trackpad, keyboard (it is focusable) and the buttons.
 */
export function DragRow({ title, titleId, aside, children }: { title: string; titleId: string; aside?: ReactNode; children: ReactNode }) {
  const ref = useRef<HTMLOListElement | null>(null);
  const [pos, setPos] = useState({ start: 0, size: 1, atStart: true, atEnd: false });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const max = el.scrollWidth - el.clientWidth;
      const size = el.scrollWidth > 0 ? el.clientWidth / el.scrollWidth : 1;
      const start = max > 0 ? (el.scrollLeft / max) * (1 - size) : 0;
      setPos({ start, size, atStart: el.scrollLeft <= 2, atEnd: el.scrollLeft >= max - 2 });
    };
    update();
    el.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    return () => {
      el.removeEventListener('scroll', update);
      window.removeEventListener('resize', update);
    };
  }, []);

  const step = (dir: 1 | -1) => {
    const el = ref.current;
    const item = el?.querySelector('li');
    if (!el || !item) return;
    const gap = parseFloat(getComputedStyle(el).columnGap || '0') || 0;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollBy({ left: dir * (item.getBoundingClientRect().width + gap), behavior: reduce ? 'auto' : 'smooth' });
  };

  return (
    <>
      <div className={s.dayHead}>
        <h2 id={titleId} className={s.dayTitle} data-r style={{ ['--i' as string]: 0 }}>
          {title}
        </h2>
        {aside ? (
          <p className={s.dayAside}>
            {aside}
          </p>
        ) : null}
        <div className={s.dayButtons}>
          <button type="button" className={s.square} onClick={() => step(-1)} disabled={pos.atStart} aria-label="Previous moment">
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
              <path d="M15 5 8 12l7 7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="square" />
            </svg>
          </button>
          <button type="button" className={s.square} onClick={() => step(1)} disabled={pos.atEnd} aria-label="Next moment">
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
              <path d="m9 5 7 7-7 7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="square" />
            </svg>
          </button>
        </div>
      </div>
      <div className={s.dayBar} aria-hidden="true">
        <span className={s.dayBarInk} style={{ left: `${pos.start * 100}%`, width: `${pos.size * 100}%` }} />
      </div>
      <ol ref={ref} className={s.dayRow} tabIndex={0} aria-labelledby={titleId}>
        {children}
      </ol>
    </>
  );
}
