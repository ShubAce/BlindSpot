'use client';

import { useEffect, useRef, useState, type CSSProperties } from 'react';

/**
 * One Sentinel-2 frame on the method page, at the size it is shown. HeroFrames hands it a srcSet of
 * the API's renders (400 and 800 px wide, plus the stored 1600 px frame) and `sizes`, so the browser
 * fetches the smallest that stays sharp in its cell: about a tenth of the bytes on a phone.
 *
 * Only the 1600 x 1000 frame is stored by the API; a smaller one is drawn on its first request and
 * then served by the CDN. If that render fails (the API could not draw it, or refused a client past
 * its limit of new pictures), the frame falls back to the stored picture in `src`; if that fails too,
 * the frame keeps its box with nothing in it rather than show a broken image.
 */

type State = 'sized' | 'stored' | 'failed';

const same = (a: string, b: string) => {
  try {
    return new URL(a).href === new URL(b).href;
  } catch {
    return a === b;
  }
};

export function FrameImage({
  src,
  srcSet,
  sizes,
  width,
  height,
  alt,
  className,
  loading,
  fetchPriority,
  style,
}: {
  src: string;
  srcSet?: string;
  sizes?: string;
  width: number;
  height: number;
  alt: string;
  className?: string;
  loading: 'eager' | 'lazy';
  fetchPriority?: 'high' | 'low' | 'auto';
  style?: CSSProperties;
}) {
  const ref = useRef<HTMLImageElement | null>(null);
  const [state, setState] = useState<State>('sized');
  const sized = state === 'sized' && !!srcSet;

  // One failure can be reported twice (the check on mount, then React replaying an error that fired
  // before hydration), so act only on a picture that has failed now, and only on the URL this state
  // loads: a late report of the small render's failure must not also condemn the stored picture.
  const failed = (img: HTMLImageElement) => {
    if (!img.complete || img.naturalWidth > 0) return;
    const url = img.currentSrc || img.src;
    setState((s) => (s === 'sized' ? (srcSet ? 'stored' : 'failed') : s === 'stored' && same(url, src) ? 'failed' : s));
  };

  // An error before hydration fires no React event, so look once on mount. A lazy picture not yet
  // fetched is not `complete`, so it is left alone.
  useEffect(() => {
    if (ref.current) failed(ref.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, []);

  return (
    // eslint-disable-next-line @next/next/no-img-element -- rendered by the BlindSpot API (absolute URLs, immutable); the sizes are the API's own
    <img
      ref={ref}
      className={className}
      // sizes and srcSet before src, so a browser never starts on the large src first
      sizes={sized ? sizes : undefined}
      srcSet={sized ? srcSet : undefined}
      src={src}
      width={width}
      height={height}
      alt={alt}
      aria-hidden={alt === '' ? true : undefined}
      loading={loading}
      fetchPriority={fetchPriority}
      decoding="async"
      style={state === 'failed' ? { ...style, visibility: 'hidden' } : style}
      onError={(e) => failed(e.currentTarget)}
    />
  );
}
