'use client';

import { useEffect, useId, useRef, useState } from 'react';

import { dateIST, timeIST } from '@/lib/format';

import { platformName, type CaseState } from './audit';
import type { CaseFire, CaseImageryPass, CaseProps } from './types';

/**
 * The evidence: the same square of ground on the clear Sentinel-2 pass before the burn and on the
 * pass that shows it, in true colour and in short-wave infrared, as rendered by the API
 * (/api/imagery/render, cached on disk). The scar's own outline is drawn over each picture from its
 * geometry, and the fire alerts the API found near it are drawn as dots, so the reader can check the
 * claim with their own eyes. The outline can be switched off to see the ground under it.
 *
 * The chips are plain <img> with their real size (512 x 512 from the API), so nothing shifts while
 * they load; a picture that fails (the renderer can be missing on a serverless deployment) is
 * replaced by one sentence in its frame.
 *
 * The marks on the pictures take the --photo-* colours (app-screen.css), the same on both themes:
 * a photograph does not change with the page. The key below draws them the same way.
 */

type BBox = [number, number, number, number];
const VIEW = 1000;

function ringsOf(g: CaseProps['geometry']): number[][][] {
  if (!g) return [];
  return g.type === 'Polygon' ? g.coordinates : g.coordinates.flat();
}

function pathFor(g: CaseProps['geometry'], bbox: BBox): string {
  const [x0, y0, x1, y1] = bbox;
  const px = (lon: number) => ((lon - x0) / (x1 - x0)) * VIEW;
  const py = (lat: number) => ((y1 - lat) / (y1 - y0)) * VIEW;
  return ringsOf(g)
    .map((ring) => ring.map((p, i) => `${i ? 'L' : 'M'}${px(p[0]).toFixed(1)} ${py(p[1]).toFixed(1)}`).join('') + 'Z')
    .join('');
}

const STROKE: Record<CaseState, string> = { unseen: 'var(--photo-unseen)', alerted: 'var(--photo-seen)', unchecked: 'var(--photo-seen)' };

function Chip({
  src,
  alt,
  label,
  outline,
  showOutline,
  dots,
  state,
}: {
  src: string | null;
  alt: string;
  label: string;
  outline: string;
  showOutline: boolean;
  dots: { x: number; y: number; explains: boolean; key: number }[];
  state: CaseState;
}) {
  const [failed, setFailed] = useState(false);
  // The chips load at once, so one can fail before hydration, when React has no handler yet: look once.
  const img = useRef<HTMLImageElement>(null);
  useEffect(() => {
    const el = img.current;
    if (el && el.complete && el.naturalWidth === 0) setFailed(true);
  }, []);
  return (
    <figure className="chip">
      <div className="chip__frame">
        {src && !failed ? (
          // eslint-disable-next-line @next/next/no-img-element -- rendered on demand by the BlindSpot API (absolute URL, immutable cache); the size is fixed so nothing shifts. The two chips are the case file's first picture (its LCP), so they load at once.
          <img ref={img} src={src} alt={alt} width={512} height={512} loading="eager" fetchPriority="high" decoding="async" onError={() => setFailed(true)} />
        ) : (
          <p className="chip__note">{src ? 'The API could not render this picture just now.' : 'The API has no picture for this pass.'}</p>
        )}
        {/* no picture, no outline: drawn over the note it would cross out its words */}
        {src && !failed ? (
          <svg className="chip__overlay" viewBox={`0 0 ${VIEW} ${VIEW}`} aria-hidden="true" preserveAspectRatio="none">
            {showOutline && outline ? (
              <path
                d={outline}
                fill="none"
                stroke={STROKE[state]}
                strokeWidth={state === 'unchecked' ? 3 : 4}
                strokeDasharray={state === 'unchecked' ? '14 10' : undefined}
                vectorEffect="non-scaling-stroke"
                strokeLinejoin="round"
              />
            ) : null}
            {dots.map((d) => (
              <circle key={d.key} cx={d.x} cy={d.y} r={14} fill="var(--photo-fire)" stroke="var(--photo-ring)" strokeWidth={4} vectorEffect="non-scaling-stroke" />
            ))}
          </svg>
        ) : null}
      </div>
      <figcaption className="chip__caption">{label}</figcaption>
    </figure>
  );
}

export function EvidenceChips({
  bbox,
  pre,
  post,
  geometry,
  fires,
  state,
  place,
}: {
  bbox: BBox | null;
  pre: CaseImageryPass | null;
  post: CaseImageryPass | null;
  geometry: CaseProps['geometry'];
  fires: CaseFire[];
  state: CaseState;
  place: string;
}) {
  const [showOutline, setShowOutline] = useState(true);
  const toggleId = useId();
  const outline = bbox ? pathFor(geometry, bbox) : '';
  const dots = bbox
    ? fires
        .filter((f) => f.lon >= bbox[0] && f.lon <= bbox[2] && f.lat >= bbox[1] && f.lat <= bbox[3])
        .map((f) => ({
          key: f.id,
          explains: f.explains_scar,
          x: ((f.lon - bbox[0]) / (bbox[2] - bbox[0])) * VIEW,
          y: ((bbox[3] - f.lat) / (bbox[3] - bbox[1])) * VIEW,
        }))
    : [];
  const passLabel = (p: CaseImageryPass | null, when: string) =>
    p ? `${when}: ${platformName(p.item_id) ?? 'Sentinel-2'}, ${dateIST(p.datetime, { weekday: true })} ${timeIST(p.datetime, { suffix: true })}` : `${when}: no pass`;
  const altFor = (p: CaseImageryPass | null, kind: string, when: string) =>
    p ? `Sentinel-2 ${kind} picture of the fields ${place}, ${dateIST(p.datetime, { year: true })}, ${when} the burn${showOutline ? ', with the scar outlined' : ''}.` : '';

  return (
    <div className="evidence">
      <div className="evidence__grid" role="group" aria-label="Sentinel-2 pictures before and after the burn">
        <span className="evidence__col">{passLabel(pre, 'Before')}</span>
        <span className="evidence__col">{passLabel(post, 'After')}</span>
        <Chip src={pre?.truecolor_url ?? null} alt={altFor(pre, 'true-colour', 'before')} label="True colour" outline={outline} showOutline={showOutline} dots={dots} state={state} />
        <Chip src={post?.truecolor_url ?? null} alt={altFor(post, 'true-colour', 'after')} label="True colour" outline={outline} showOutline={showOutline} dots={dots} state={state} />
        <Chip src={pre?.swir_url ?? null} alt={altFor(pre, 'short-wave infrared', 'before')} label="Short-wave infrared" outline={outline} showOutline={showOutline} dots={dots} state={state} />
        <Chip src={post?.swir_url ?? null} alt={altFor(post, 'short-wave infrared', 'after')} label="Short-wave infrared" outline={outline} showOutline={showOutline} dots={dots} state={state} />
      </div>
      <div className="evidence__foot">
        {/* a control, so not on paper: the pictures print with the outline as it is set on screen */}
        <label htmlFor={toggleId} className="switch" data-print="hide">
          <input id={toggleId} type="checkbox" checked={showOutline} onChange={(e) => setShowOutline(e.target.checked)} />
          <span>Outline the scar</span>
        </label>
        <span className="evidence__key">
          <svg className="evidence__swatch" width="22" height="12" aria-hidden="true">
            <rect x="1" y="1" width="20" height="10" fill="none" stroke={STROKE[state]} strokeWidth="2" strokeDasharray={state === 'unchecked' ? '4 3' : undefined} />
          </svg>
          The scar as mapped
          {dots.length ? (
            <>
              <svg className="evidence__swatch" width="12" height="12" aria-hidden="true" style={{ marginLeft: 16 }}>
                <circle cx="6" cy="6" r="4.5" fill="var(--photo-fire)" />
              </svg>
              Fire alert inside this square
            </>
          ) : null}
        </span>
        {pre?.cloud_pct != null || post?.cloud_pct != null ? (
          <span className="evidence__cloud nums">
            Cloud in the tile: before {pre?.cloud_pct == null ? '—' : `${Math.round(pre.cloud_pct)}%`}, after {post?.cloud_pct == null ? '—' : `${Math.round(post.cloud_pct)}%`}
          </span>
        ) : null}
      </div>
    </div>
  );
}
