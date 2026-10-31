'use client';

import Image from 'next/image';
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

/**
 * A picture from the API or NASA GIBS (or a local photograph), at an explicit size so nothing shifts.
 *
 * Remote pictures are passed through untouched (`unoptimized`): the API's Sentinel-2 renders and GIBS
 * images are already the size they are shown at, and re-encoding would soften 10 m pixels. If the
 * picture does not arrive, the frame keeps its size and says so in one line, instead of a broken icon.
 */
export function Pic({ src, width, height, alt, className, style, sizes, priority = false, failText, local = false, frame }: {
  src: string;
  width: number;
  height: number;
  alt: string;
  className?: string;
  style?: CSSProperties;
  sizes?: string;
  priority?: boolean;
  /** The honest line shown in the frame if the picture fails, e.g. "NASA GIBS did not send this picture." */
  failText: string;
  /** A photograph from public/: let Next resize it. */
  local?: boolean;
  /** Crop into a box: a class that sets the box's aspect-ratio (landing.css `lp-box-*`). */
  frame?: string;
}) {
  const ref = useRef<HTMLImageElement | null>(null);
  const [failed, setFailed] = useState(false);
  // An error before hydration fires no React event: check once on mount.
  useEffect(() => {
    const img = ref.current;
    if (img && img.complete && img.naturalWidth === 0) setFailed(true);
  }, []);
  const wrap = (node: ReactNode) => (frame ? <span className={`lp-box ${frame}`}>{node}</span> : node);
  if (failed) {
    return wrap(
      <span role="img" aria-label={alt} className={className} style={{ ...style, display: 'flex', alignItems: 'flex-end', background: 'var(--surface)', aspectRatio: `${width} / ${height}` }}>
        <span style={{ padding: 12, color: 'var(--muted)', fontSize: 'var(--step-small)', lineHeight: 1.4 }}>{failText}</span>
      </span>,
    );
  }
  return wrap(
    <Image
      ref={ref}
      src={src}
      width={width}
      height={height}
      alt={alt}
      className={className}
      style={style}
      sizes={sizes}
      unoptimized={!local}
      preload={priority}
      loading={priority ? 'eager' : 'lazy'}
      decoding="async"
      onError={() => setFailed(true)}
    />,
  );
}
