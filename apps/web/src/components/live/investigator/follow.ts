'use client';

import { useEffect, useRef, type RefObject } from 'react';

import type { Investigation } from './model';

/**
 * Keeps the newest step in view while a run unfolds (unless the reader has scrolled up), then brings
 * the answer's heading to the top once the answer starts, and lets go. Only the console's own scroll
 * box moves (on wide screens); below 1100px the console is in the page flow and nothing scrolls.
 */
export function useFollow(box: RefObject<HTMLElement | null>, inv: Investigation | null, answerId: string) {
  const stick = useRef(true);
  const answered = useRef<string | null>(null);
  const runKey = inv?.key ?? null;
  const hasAnswer = Boolean(inv && (inv.draft || inv.answer));
  const steps = inv?.steps.length ?? 0;

  // a new run starts at the top, following
  useEffect(() => {
    stick.current = true;
    answered.current = null;
    const el = box.current;
    el?.scrollTo({ top: 0 });
  }, [runKey, box]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 72;
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [box, runKey]);

  useEffect(() => {
    const el = box.current;
    if (!el || !inv || el.scrollHeight <= el.clientHeight) return;
    if (hasAnswer) {
      if (answered.current === inv.key) return;
      answered.current = inv.key;
      if (!stick.current) return;
      const head = document.getElementById(answerId);
      if (head) el.scrollTo({ top: el.scrollTop + head.getBoundingClientRect().top - el.getBoundingClientRect().top - 8 });
      stick.current = false;
      return;
    }
    if (stick.current && inv.status === 'running') el.scrollTo({ top: el.scrollHeight });
  }, [box, inv, hasAnswer, steps, answerId]);
}
