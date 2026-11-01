'use client';

import { useEffect, useEffectEvent, useRef } from 'react';

import type { ChartHandle } from '@/lib/charts';

/**
 * Mount one of the imperative charts from lib/charts.ts into a div, redraw it when `key` changes,
 * and clean it up on unmount.
 *
 *   const key = JSON.stringify(series.map((s) => s.values));
 *   const ref = useChart((el) => Charts.line(el, { labels, series }), key);
 *   return <div ref={ref} />;
 *
 * `key` is any string or number that changes exactly when the chart's inputs change (the data's
 * `generated_at` is often enough). The draw function always sees the latest props. A theme change
 * needs nothing in `key`: the chart redraws itself with the new tokens (lib/charts.ts, lifecycle).
 */
export function useChart<T extends ChartHandle>(draw: (el: HTMLDivElement) => T | null, key: string | number) {
  const ref = useRef<HTMLDivElement>(null);
  const drawLatest = useEffectEvent((el: HTMLDivElement) => draw(el));
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handle = drawLatest(el);
    return () => handle?.destroy();
  }, [key]);
  return ref;
}
