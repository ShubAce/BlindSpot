'use client';

import { useEffect, useRef, type CSSProperties, type ElementType, type ReactNode } from 'react';

import { s } from './styles';

/**
 * The entrance for a section's parts (references/motion.md): children marked `data-r` (with
 * `style={{ '--i': n }}` for their order) rise 24px and fade in, staggered, once, when the block
 * enters the viewport.
 *
 * Content is never hidden without script: the block is only marked hidden after mount, and only if it
 * is still below the fold then. Under reduced motion the CSS drops the travel and the fade.
 */
export function Reveal({ as: Tag = 'div', className, style, children, id, 'aria-labelledby': labelledBy }: {
  as?: ElementType;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
  id?: string;
  'aria-labelledby'?: string;
}) {
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const r = el.getBoundingClientRect();
    if (r.top < window.innerHeight * 0.92) return; // already in view: leave it shown
    el.dataset.reveal = 'hidden';
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            el.dataset.reveal = 'shown';
            io.disconnect();
          }
        }
      },
      { rootMargin: '0px 0px -12% 0px', threshold: 0.01 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <Tag ref={ref} className={className ? `${s.reveal} ${className}` : s.reveal} style={style} id={id} aria-labelledby={labelledBy}>
      {children}
    </Tag>
  );
}

