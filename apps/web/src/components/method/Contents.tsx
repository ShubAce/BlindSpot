'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * The method page's contents, kept at hand on a page some thirty screens long. It stays out of sight
 * (and out of the Tab order) while the hero's own contents list is on screen, then pins under the
 * site's bar: on a wide screen a slim row of every section, the one being read marked; on a tablet
 * or phone one "Contents" control naming the section being read, which opens the full list. It is a
 * strip of its own above the page, like the site's bar, and never floats over the text: each
 * section's anchor lands below it (method.css, scroll-margin-top).
 */

export type ContentsItem = { id: string; title: string; short: string };

export function MethodContents({ sections, after }: { sections: ContentsItem[]; after: string }) {
  const [shown, setShown] = useState(false);
  const [current, setCurrent] = useState<string | null>(null);
  const menu = useRef<HTMLDetailsElement>(null);
  // the sections by id: a server refresh hands over a new array of the same sections, which must not
  // re-subscribe the scroll listeners
  const ids = sections.map((s) => s.id).join(' ');

  useEffect(() => {
    let frame = 0;
    const read = () => {
      frame = 0;
      const toc = document.getElementById(after);
      setShown(toc ? toc.getBoundingClientRect().bottom < 0 : window.scrollY > 600);
      // the section being read: the last one whose top has passed a third of the way down the screen
      const line = window.innerHeight / 3;
      let now: string | null = null;
      for (const id of ids.split(' ')) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= line) now = id;
      }
      setCurrent(now);
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(read);
    };
    frame = requestAnimationFrame(read);
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [ids, after]);

  // a hidden strip closes its menu, so it never comes back open over the page
  useEffect(() => {
    if (!shown && menu.current) menu.current.open = false;
  }, [shown]);

  const close = () => {
    if (menu.current) menu.current.open = false;
  };
  const now = sections.find((s) => s.id === current) ?? null;

  return (
    <div className="m-contents" data-shown={shown ? '' : undefined} inert={!shown}>
      <nav className="m-contents__bar" aria-label="Contents">
        <span className="m-contents__label">Contents</span>
        <ul>
          {sections.map((s) => (
            <li key={s.id}>
              <a href={`#${s.id}`} aria-current={s.id === current ? 'location' : undefined} title={s.short === s.title ? undefined : s.title}>
                {s.short}
              </a>
            </li>
          ))}
        </ul>
      </nav>
      <details
        className="m-contents__menu"
        ref={menu}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && menu.current?.open) {
            close();
            menu.current.querySelector('summary')?.focus();
          }
        }}
      >
        <summary>
          <span className="m-contents__label">Contents</span>
          <span className="m-contents__now">{now ? now.title : 'How BlindSpot counts'}</span>
          <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" className="m-contents__chev">
            <path d="M3 4.5L6 7.5l3-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </summary>
        <nav aria-label="Contents">
          <ol>
            {sections.map((s) => (
              <li key={s.id}>
                <a href={`#${s.id}`} aria-current={s.id === current ? 'location' : undefined} onClick={close}>
                  {s.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>
      </details>
    </div>
  );
}
