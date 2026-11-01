'use client';

import { useCallback } from 'react';

/**
 * A callback ref that marks a scroller with `data-more-start` and `data-more-end` while more of it
 * lies out of view on that side, so live.css can fade that edge: the cue that a row or a card
 * scrolls, which a hidden scrollbar (the layer toggles on a phone) or a short card never gives.
 * It follows scrolling, the box's size and the content changing inside it.
 *
 *   const cue = useScrollCue<HTMLDivElement>('x');
 *   <div ref={cue} className="lv-toggles">…</div>
 */
export function useScrollCue<T extends HTMLElement>(axis: 'x' | 'y'): (el: T | null) => (() => void) | undefined {
  return useCallback(
    (el: T | null) => {
      if (!el) return undefined;
      const update = () => {
        const pos = axis === 'x' ? el.scrollLeft : el.scrollTop;
        const max = axis === 'x' ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight;
        // a pixel of slack: zoomed pages round scroll positions
        el.toggleAttribute('data-more-start', pos > 1);
        el.toggleAttribute('data-more-end', max - pos > 1);
      };
      update();
      el.addEventListener('scroll', update, { passive: true });
      // the box and each child: content can grow while the box stays capped, and a child's own size
      // covers any change of text inside it
      const ro = new ResizeObserver(update);
      ro.observe(el);
      for (const c of el.children) ro.observe(c);
      // children added or removed: only those are watched or let go (a new one reports its first size)
      const mo = new MutationObserver((records) => {
        for (const r of records) {
          for (const n of r.addedNodes) if (n instanceof Element) ro.observe(n);
          for (const n of r.removedNodes) if (n instanceof Element) ro.unobserve(n);
        }
        update();
      });
      mo.observe(el, { childList: true });
      return () => {
        el.removeEventListener('scroll', update);
        ro.disconnect();
        mo.disconnect();
      };
    },
    [axis],
  );
}
