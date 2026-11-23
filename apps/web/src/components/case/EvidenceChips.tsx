'use client';

import { useEffect, useId, useMemo, useRef, useState } from 'react';

import { dateIST, timeIST } from '@/lib/format';

import { platformName, type CaseState } from './audit';
import { pathFor, VIEW, type BBox, type OtherMark } from './picture-marks';
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
 * Every other scar the API stores inside the same square is outlined too, thinner and under the
 * case's own (picture-marks.ts): yellow those first seen burnt between the case's two passes, cyan
 * those that had burnt by the first, so the dark fields the model mapped are outlined and not only
 * this one. The larger are drawn first, whatever their kind, so a small field on a large one stays
 * on top. Each is a link to its own case file, for a pointer or a finger: inside the overlay,
 * hidden from screen readers, so out of the tab order too (a hundred links in each of four
 * pictures would bury the page's own); the sentence under the pictures gives their numbers.
 *
 * The marks on the pictures take the --photo-* colours (app-screen.css), the same on both themes:
 * a photograph does not change with the page. The key below draws them the same way.
 */

const STROKE: Record<CaseState, string> = { unseen: 'var(--photo-unseen)', alerted: 'var(--photo-seen)', unchecked: 'var(--photo-seen)' };
type Others = { casing: string; marks: OtherMark[]; same: boolean; earlier: boolean };

function OtherScars({ others }: { others: Others }) {
  return (
    <>
      {/* one dark casing under all of them, so a thin line holds on pale ground too */}
      <path d={others.casing} className="chip__casing" />
      <g className="chip__others">
        {others.marks.map((m) => (
          <a key={m.id} href={`/case/${m.id}`} tabIndex={-1} className={`is-${m.rel}`}>
            <title>{m.title}</title>
            <path d={m.d} />
          </a>
        ))}
      </g>
    </>
  );
}

function Chip({
  src,
  alt,
  label,
  outline,
  showOutline,
  dots,
  state,
  others,
}: {
  src: string | null;
  alt: string;
  label: string;
  outline: string;
  showOutline: boolean;
  dots: { x: number; y: number; explains: boolean; key: number }[];
  state: CaseState;
  others: Others | null;
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
            {showOutline && others ? <OtherScars others={others} /> : null}
            {showOutline && outline ? (
              <>
                {/* the case's own outline stays the boldest: a dark casing under it */}
                <path d={outline} className="chip__casing chip__casing--case" strokeDasharray={state === 'unchecked' ? '14 10' : undefined} />
                <path
                  d={outline}
                  fill="none"
                  stroke={STROKE[state]}
                  strokeWidth={state === 'unchecked' ? 3 : 4}
                  strokeDasharray={state === 'unchecked' ? '14 10' : undefined}
                  vectorEffect="non-scaling-stroke"
                  strokeLinejoin="round"
                />
              </>
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
  otherMarks = [],
  othersNote = null,
  othersLabels = { same: 'Other scars, same days', earlier: 'Other scars, burnt earlier' },
}: {
  bbox: BBox | null;
  pre: CaseImageryPass | null;
  post: CaseImageryPass | null;
  geometry: CaseProps['geometry'];
  fires: CaseFire[];
  state: CaseState;
  place: string;
  /** The other scars inside the square, as CaseScreen drew them (picture-marks.ts otherMarks). */
  otherMarks?: OtherMark[];
  /** The sentence under the pictures that counts them (picture-marks.ts othersSentence). */
  othersNote?: string | null;
  /** The key's words for the two kinds, with the case's dates (picture-marks.ts othersLabels). */
  othersLabels?: { same: string; earlier: string };
}) {
  const [showOutline, setShowOutline] = useState(true);
  const toggleId = useId();
  const outline = bbox ? pathFor(geometry, bbox) : '';
  const n = bbox ? otherMarks.length : 0;
  const others = useMemo<Others | null>(
    () =>
      bbox && otherMarks.length
        ? {
            casing: otherMarks.map((m) => m.d).join(''),
            marks: otherMarks,
            same: otherMarks.some((m) => m.rel === 'same'),
            earlier: otherMarks.some((m) => m.rel === 'earlier'),
          }
        : null,
    [bbox, otherMarks],
  );
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
    p ? `Sentinel-2 ${kind} picture of the fields ${place}, ${dateIST(p.datetime, { year: true })}, ${when} the burn${showOutline ? `, with the scar${n ? ` and ${n} other${n === 1 ? '' : 's'}` : ''} outlined` : ''}.` : '';
  const caseW = n ? (state === 'unchecked' ? 3 : 4) : 2;
  const keyLine = (stroke: string) => (
    <svg className="evidence__swatch" width="22" height="12" aria-hidden="true">
      <rect x="1" y="1" width="20" height="10" fill="none" stroke={stroke} strokeWidth="1.5" />
    </svg>
  );

  return (
    <div className="evidence">
      <div className="evidence__grid" role="group" aria-label="Sentinel-2 pictures before and after the burn">
        <span className="evidence__col">{passLabel(pre, 'Before')}</span>
        <span className="evidence__col">{passLabel(post, 'After')}</span>
        <Chip src={pre?.truecolor_url ?? null} alt={altFor(pre, 'true-colour', 'before')} label="True colour" outline={outline} showOutline={showOutline} dots={dots} state={state} others={others} />
        <Chip src={post?.truecolor_url ?? null} alt={altFor(post, 'true-colour', 'after')} label="True colour" outline={outline} showOutline={showOutline} dots={dots} state={state} others={others} />
        <Chip src={pre?.swir_url ?? null} alt={altFor(pre, 'short-wave infrared', 'before')} label="Short-wave infrared" outline={outline} showOutline={showOutline} dots={dots} state={state} others={others} />
        <Chip src={post?.swir_url ?? null} alt={altFor(post, 'short-wave infrared', 'after')} label="Short-wave infrared" outline={outline} showOutline={showOutline} dots={dots} state={state} others={others} />
      </div>
      {othersNote ? <p className="evidence__others">{othersNote}</p> : null}
      <div className="evidence__foot">
        {/* a control, so not on paper: the pictures print with the outline as it is set on screen */}
        <label htmlFor={toggleId} className="switch" data-print="hide">
          <input id={toggleId} type="checkbox" checked={showOutline} onChange={(e) => setShowOutline(e.target.checked)} />
          <span>{n ? 'Outline the scars' : 'Outline the scar'}</span>
        </label>
        {/* one item per mark, each wrapping as a whole */}
        <span className="evidence__key">
          <span className="evidence__keyitem">
            {/* as wide as on the pictures beside the others' thin lines (4 or 3 against 1.5) */}
            <svg className="evidence__swatch" width="22" height="12" aria-hidden="true">
              <rect x={caseW / 2} y={caseW / 2} width={22 - caseW} height={12 - caseW} fill="none" stroke={STROKE[state]} strokeWidth={caseW} strokeDasharray={state === 'unchecked' ? '4 3' : undefined} />
            </svg>
            {n ? 'This scar' : 'The scar as mapped'}
          </span>
          {others?.same ? (
            <span className="evidence__keyitem">
              {keyLine('var(--photo-same)')}
              {othersLabels.same}
            </span>
          ) : null}
          {others?.earlier ? (
            <span className="evidence__keyitem">
              {keyLine('var(--photo-earlier)')}
              {othersLabels.earlier}
            </span>
          ) : null}
          {dots.length ? (
            <span className="evidence__keyitem">
              <svg className="evidence__swatch" width="12" height="12" aria-hidden="true">
                <circle cx="6" cy="6" r="4.5" fill="var(--photo-fire)" />
              </svg>
              Fire alert inside this square
            </span>
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
