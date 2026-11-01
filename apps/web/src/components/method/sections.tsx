import type { ReactNode } from 'react';

import { auditPhrase } from '@/components/case/audit';
import type { StateBoundaries } from '@/components/case/types';
import { loadFailed } from '@/components/pwa/LoadFailed';
import { PATHS } from '@/lib/api';
import { serverGet, type ApiResult } from '@/lib/api-server';
import { dateIST, dateTimeIST, metric, MISSING, number, percent } from '@/lib/format';

import { Architecture } from './Architecture';
import { AucStrip, AuditForest, DiurnalChart, PassErrorChart, SmokeDots, type ForestRow } from './charts';
import { FrameImage } from './FrameImage';
import { Relative } from './Relative';
import type { Featured, GeoDiurnal, GeoGroup, Headline, OrbitValidation, RiskMethod, ScarsSummary, SmokeCityRow, SmokeValidation, SourceRow, SourcesList } from './types';

/**
 * The method page's sections. Each is an async Server Component that reads its own endpoints (cached
 * 30 s by serverGet) inside its own <Suspense>, so a slow or failing endpoint holds back only its
 * section and says so in one sentence. Every figure here is read from the API at request time; the
 * words around them describe the method as the API and docs/architecture.md state it.
 */

/** Paths this page reads that lib/api.ts does not list yet (kept here, so the shared file is untouched). */
const P = { orbitsValidation: '/api/orbits/validation', smokeValidation: '/api/smoke/validation' } as const;

/** API prose for people: drops pointers meant for developers ("see /api/...", "See `backtest` ..."). */
const prose = (t: string | null | undefined): string =>
  (t ?? '')
    .replace(/\s*\(see \/api[^)]*\)/g, '')
    .replace(/\s*See `[^`]+`[^.]*\./g, '')
    // a code identifier ("dropped by BurnRule.keeps") means nothing to a reader; the sentence stands without it
    .replace(/\s+by\s+[A-Z]\w*\.\w+(?:\(\))?/g, '')
    .trim();

const headline = () => serverGet<Headline>(PATHS.headline);
const scarsSummary = () => serverGet<ScarsSummary>(PATHS.scarsSummary);

// data-load-failed: the offline worker never lets a page with this note replace a good copy it saved
export function Failed({ what, r }: { what: string; r: ApiResult<unknown> }) {
  if (r.ok) return null;
  return (
    <p className="m-failed" role="status" data-load-failed="">
      {`Couldn't load ${what}. ${r.error.message} `}
      <a href="" className="m-link">
        Retry
      </a>
    </p>
  );
}

/** A large figure with its unit and one line saying what it is. */
export function Figure({ value, unit, children }: { value: string; unit?: string; children: ReactNode }) {
  return (
    <div className="m-figure">
      <p className="m-figure__value nums">
        {value}
        {unit ? <span className="m-figure__unit">{unit}</span> : null}
      </p>
      <p className="m-figure__label">{children}</p>
    </div>
  );
}

/** API prose set as a sentence: a capital first letter and real comparison signs ("<=6 days" as "≤6 days"). */
const sentence = (t: string) => {
  const s = t.replace(/<=/g, '≤').replace(/>=/g, '≥');
  return s.charAt(0).toUpperCase() + s.slice(1);
};

export function Quote({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="m-quote">
      <p className="m-quote__label">{label}</p>
      <p className="m-quote__text">{typeof children === 'string' ? sentence(children) : children}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// hero picture: the same fields on four passes

/**
 * The API stores each featured frame at 1600 x 1000, the size of the landing's full-width hero; here
 * the frames sit in quarter- and half-width cells, so the browser is offered 400 and 800 px renders
 * of the same frame too (the same URL with a smaller w and h) and picks by `sizes`. 800 px across the
 * 6.4 km window is already finer than Sentinel-2's 10 m pixels, so a 2x screen stays sharp. The
 * stored frame stays in the set as its largest member and as `src`, the fallback FrameImage turns
 * to if a smaller render fails. The API draws a smaller frame on a grid-snapped box (it moves an edge
 * by at most 1/64 of the side), the same box for both colours of a pass, so the crossfade stays put.
 */
const FRAME_WIDTHS = [400, 800];

function frameSrcSet(url: string, w: number, h: number): string | undefined {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return undefined;
  }
  if (!u.pathname.endsWith('/api/imagery/render') || !u.searchParams.has('w') || !u.searchParams.has('h') || !(w > 0 && h > 0)) return undefined;
  const sized = FRAME_WIDTHS.filter((x) => x < w).map((x) => {
    const v = new URL(u);
    v.searchParams.set('w', String(x));
    v.searchParams.set('h', String(Math.round((x * h) / w)));
    // URLSearchParams writes the bbox's commas as %2C: a bare comma would split a srcset entry
    return `${v.toString()} ${x}w`;
  });
  return sized.length ? [...sized, `${url.replace(/,/g, '%2C')} ${w}w`].join(', ') : undefined;
}

// the cells' widths: four across (two on a phone) in the hero, two across (one on a phone) below
const HERO_SIZES = '(max-width: 720px) calc(50vw - 19px), calc(25vw - 20px)';
const PAIR_SIZES = '(max-width: 640px) calc(100vw - 32px), calc(50vw - 35px)';

export async function HeroFrames() {
  const r = await serverGet<Featured>(PATHS.imageryFeatured);
  if (!r.ok) return <Failed what="the Sentinel-2 pictures" r={r} />;
  const f = r.data.frames ?? [];
  if (!f.length) return <p className="m-failed">The API has no clear Sentinel-2 passes to show yet{r.data.reason ? `: ${r.data.reason}` : '.'}</p>;
  const a = r.data.aoi;
  const place = a?.name ? (a.name.toLowerCase().startsWith('near') ? `${a.name.charAt(0).toLowerCase()}${a.name.slice(1)}` : `near ${a.name}`) : 'in this square';
  return (
    <figure className="m-frames">
      <div className="m-frames__row">
        {f.map((fr, i) => (
          <div key={fr.item_id} className="m-frames__cell">
            <FrameImage
              src={fr.truecolor_url}
              srcSet={frameSrcSet(fr.truecolor_url, fr.width, fr.height)}
              sizes={HERO_SIZES}
              alt={`Sentinel-2 picture of the fields ${place}, ${dateIST(fr.datetime, { year: true })}, in true colour.`}
              width={fr.width}
              height={fr.height}
              loading="eager"
              fetchPriority={i < 2 ? 'high' : 'auto'}
            />
            {/* the signature move: the same pass fades slowly into short-wave infrared and back */}
            <FrameImage
              className="m-frames__swir"
              src={fr.swir_url}
              srcSet={frameSrcSet(fr.swir_url, fr.width, fr.height)}
              sizes={HERO_SIZES}
              alt=""
              width={fr.width}
              height={fr.height}
              loading="lazy"
              style={{ animationDelay: `${i * 1.6}s` }}
            />
            <span className="m-frames__date nums">{`${dateIST(fr.datetime, { weekday: true })}${fr.platform ? `, ${fr.platform.replace(/^S2/, 'Sentinel-2')}` : ''}`}</span>
          </div>
        ))}
      </div>
      <figcaption className="m-caption">
        {/* "about": a frame smaller than the stored one is drawn on a grid-snapped box (see FRAME_WIDTHS) */}
        {`The same ${a?.size_km ? `patch of fields, about ${number(a.size_km[0], { decimals: 1 })} by ${number(a.size_km[1], { decimals: 1 })} km,` : 'fields'} ${place}, on ${f.length} clear Sentinel-2 passes from ${dateIST(f[0].datetime)} to ${dateIST(f[f.length - 1].datetime, { year: true })}, in true colour fading into short-wave infrared, where burnt ground shows dark.`}
        {a?.chosen_value != null && a.chosen_unit ? ` The API chose this square for its ${number(a.chosen_value)} ${a.chosen_unit} of burn scars since ${dateIST(a.chosen_since)}.` : ''}
      </figcaption>
    </figure>
  );
}

// ---------------------------------------------------------------------------------------------
// the blind hours: GK-2A against the polar windows

const GROUPS: { key: keyof NonNullable<GeoDiurnal['validation']>['groups']; label: string }[] = [
  { key: 'viirs_day', label: 'VIIRS, day passes' },
  { key: 'viirs_night', label: 'VIIRS, night passes' },
  { key: 'modis_aqua_day', label: 'MODIS Aqua, afternoon' },
  { key: 'modis_terra', label: 'MODIS Terra' },
  { key: 'all', label: 'All polar passes' },
];

export async function BlindHours() {
  const r = await serverGet<GeoDiurnal>(PATHS.geostationaryDiurnal);
  if (!r.ok) return <Failed what="the geostationary record" r={r} />;
  const d = r.data;
  if (d.available === false || !d.hourly_ist?.length) return <p className="m-failed">No geostationary scans are stored yet{d.reason ? `: ${d.reason}` : '.'}</p>;
  const biggest = [...(d.unobserved_gaps_ist ?? [])].sort((a, b) => b.pixel_hours - a.pixel_hours)[0];
  const g = d.validation?.groups ?? {};
  const cell = (v: number | null | undefined, of?: number) => (v == null ? <span className="missing">—</span> : <>{percent(v)}{of != null ? <span className="m-of"> of {number(of)}</span> : null}</>);
  return (
    <>
      <div className="m-figures">
        <Figure value={percent(d.share_outside_polar_windows)}>
          of GK-2A fire activity ({number(d.pixel_hours_outside_polar_windows, { decimals: 0 })} of {number(d.pixel_hours, { decimals: 0 })} pixel-hours) fell outside the times a polar fire satellite was overhead.
        </Figure>
        <Figure value={d.peak_ist ?? '—'} unit=" IST">
          was the busiest hour, with {number(d.peak_mean_count, { decimals: 1 })} fire pixels in a mean scan.
        </Figure>
        {biggest ? (
          <Figure value={number(biggest.pixel_hours, { decimals: 0 })} unit=" pixel-hours">
            fell between {biggest.start} and {biggest.end} IST, {Math.floor(biggest.minutes / 60)} h {biggest.minutes % 60} min in which no polar satellite reported a fire.
          </Figure>
        ) : null}
      </div>
      <div className="m-chart">
        <p className="m-chart__title">GK-2A fire pixels in a mean 10-minute scan, by hour of the day</p>
        <DiurnalChart data={d} />
        <p className="m-caption">
          {d.unit}. Mean over the last {number(d.days)} days ({number(d.scans)} scans since {dateTimeIST(d.since, { suffix: true })}). Shaded: the windows when a VIIRS or MODIS satellite passed, {d.polar_windows_ist.map((w) => `${w.start}–${w.end}`).join(', ')}.
        </p>
      </div>
      {d.validation ? (
        <div className="m-table-wrap">
          <table className="m-table m-table--stack">
            <caption>
              GK-2A checked against the polar satellites (VIIRS and MODIS) from FIRMS, pass by pass{d.validation.period ? `, ${dateIST(d.validation.period[0])} to ${dateIST(d.validation.period[1])}` : ''}
            </caption>
            <thead>
              <tr>
                <th scope="col">Reference</th>
                <th scope="col" className="num">Passes</th>
                <th scope="col" className="num">Polar fire pixels GK-2A also flagged</th>
                <th scope="col" className="num">Strong fires flagged</th>
                <th scope="col" className="num">GK-2A pixels with no polar fire</th>
              </tr>
            </thead>
            <tbody>
              {GROUPS.map(({ key, label }) => {
                const row: GeoGroup | undefined = g[key];
                if (!row) return null;
                return (
                  <tr key={key}>
                    <th scope="row">{label}</th>
                    <td className="num" data-label="Passes">{number(row.passes)}</td>
                    <td className="num" data-label="Polar fire pixels GK-2A also flagged">{cell(row.hit_rate, row.n)}</td>
                    <td className="num" data-label="Strong fires flagged">{cell(row.hit_rate_strong, row.n_strong)}</td>
                    <td className="num" data-label="GK-2A pixels with no polar fire">{cell(row.false_alarm_rate, row.n_geo)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {d.interpretation?.length ? (
        <ul className="m-points m-points--two">
          {d.interpretation.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      ) : null}
      {d.validation?.method ? <p className="m-small">{`Method${d.validation.algorithm ? ` (${d.validation.algorithm})` : ''}: ${d.validation.method}`}</p> : null}
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// pass times against FIRMS

export async function PassTimes() {
  const r = await serverGet<OrbitValidation>(P.orbitsValidation);
  if (!r.ok) return <Failed what="the pass-time check" r={r} />;
  const d = r.data;
  const o = d.overall;
  if (!o || !d.satellites?.length) return <p className="m-failed">No detections to check pass times against in this window{d.note ? `: ${d.note}` : '.'}</p>;
  return (
    <>
      <div className="m-cols m-cols--figures">
      <div className="m-figures m-figures--stack">
        <Figure value={number(o.median_min, { decimals: 1, signed: true })} unit=" min">
          median difference between the predicted pass and the time FIRMS stamped on {number(o.n)} detections.
        </Figure>
        <Figure value={`±${number(o.p95_abs_min, { decimals: 1 })}`} unit=" min">
          holds 95% of them. {percent(o.within_3_min_share)} fall within three minutes.
        </Figure>
      </div>
      <Quote label="How the API checks it">{d.method}</Quote>
      </div>
      <div className="m-chart">
        <p className="m-chart__title">Predicted pass time minus FIRMS acquisition time, by satellite</p>
        <PassErrorChart data={d} />
        <p className="m-caption">
          Gold dot: the median. Bar: every detection, from the earliest to the latest difference. {number(d.days)} days of detections, {dateIST(d.window.from_utc)} to {dateIST(d.window.to_utc, { year: true })}.
        </p>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// the burn-scar rule

export async function ScarRule() {
  const [r, f] = await Promise.all([scarsSummary(), serverGet<Featured>(PATHS.imageryFeatured)]);
  if (!r.ok) return <Failed what="the burn-scar method" r={r} />;
  const m = r.data.method;
  const c = r.data.coverage;
  const frames = f.ok ? f.data.frames : [];
  const pair = frames.length >= 2 ? [frames[0], frames[frames.length - 1]] : [];
  return (
    <>
      {pair.length ? (
        <figure className="m-pair">
          {pair.map((fr) => (
            <div key={fr.item_id} className="m-pair__cell">
              <FrameImage
                src={fr.swir_url}
                srcSet={frameSrcSet(fr.swir_url, fr.width, fr.height)}
                sizes={PAIR_SIZES}
                alt={`Sentinel-2 short-wave infrared picture of the same fields, ${dateIST(fr.datetime, { year: true })}.`}
                width={fr.width}
                height={fr.height}
                loading="lazy"
              />
              <span className="m-frames__date nums">{`Short-wave infrared, ${dateIST(fr.datetime, { weekday: true })}`}</span>
            </div>
          ))}
        </figure>
      ) : null}
      <div className="m-cols">
        <div>
          {m?.summary ? <Quote label={`The detection rule${m.model_version ? `, ${m.model_version}` : ''}`}>{m.summary}</Quote> : null}
          {m?.unseen_rule ? <Quote label="When a scar counts as unseen">{m.unseen_rule}</Quote> : null}
        </div>
        <div>
          <p className="m-body">
            Every scar is in one of three states. A FIRMS fire alert explains it, and a satellite saw it burn. No alert explains it, and it is a candidate unseen burn. Or it has not been checked against the alerts yet, and it is called neither.
          </p>
          {c ? (
            <dl className="m-facts">
              <div>
                <dt>Sentinel-2 passes processed</dt>
                <dd className="nums">
                  {number(c.processed)} of {number(c.catalogued)} catalogued
                </dd>
              </div>
              <div>
                <dt>Skipped (cloud, haze, no data) and failed</dt>
                <dd className="nums">
                  {number(c.skipped)} and {number(c.failed)}
                </dd>
              </div>
              <div>
                <dt>Tiles covered</dt>
                <dd className="nums">{number(c.tiles.length)}</dd>
              </div>
              <div>
                <dt>Latest pass processed</dt>
                <dd className="nums">{c.latest_processed_pass ? dateTimeIST(c.latest_processed_pass, { suffix: true }) : '—'}</dd>
              </div>
            </dl>
          ) : null}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// accuracy of the scars

export async function Accuracy() {
  const [h, s] = await Promise.all([headline(), scarsSummary()]);
  if (!h.ok && !s.ok) return <Failed what="the audit" r={h} />;
  const u = h.ok ? h.data.unseen_burns : undefined;
  const audit = s.ok ? s.data.method?.audit : undefined;
  const rows: ForestRow[] = [];
  if (u?.precision != null) rows.push({ label: 'Candidate unseen burns that were burns', value: u.precision, ci: u.precision_ci95 ?? null, n: `n = ${u.precision_n ?? '—'}`, lead: true });
  const fc = audit?.firms_confirmed;
  if (fc?.precision != null) rows.push({ label: 'Alert-confirmed scars that were burns', value: fc.precision, ci: fc.precision_ci95 ?? null, n: `n = ${fc.audited ?? '—'}` });
  const rc = audit?.recall_vs_firms;
  if (rc?.recall_same_window != null) rows.push({ label: 'VIIRS fires with a scar, same window', value: rc.recall_same_window, ci: rc.recall_same_window_ci95 ?? null, n: `n = ${rc.viirs_detections ?? '—'}` });
  const phrase = auditPhrase(u);
  return (
    <>
      {u?.available && u.unseen_share != null ? (
        <div className="m-figures">
          <Figure value={percent(u.unseen_share)}>
            of the burnt area mapped since {dateIST(u.first_pass)} ({metric('area_ha', u.unseen_area_ha).value} of {metric('area_ha', u.area_ha).value} ha) is candidate unseen burns{phrase ? `, ${phrase}` : ''}.
          </Figure>
          <Figure value={number(u.unseen_clusters ?? null)}>
            candidate unseen burns in {number(u.districts ?? null)} districts, mapped by rule {u.model_version ?? '—'} up to the pass of {dateIST(u.last_pass, { year: true })}{phrase ? `; ${phrase}` : ''}.
          </Figure>
        </div>
      ) : (
        <p className="m-failed" {...loadFailed(!h.ok)}>{u?.reason ?? (h.ok ? 'The API states no share of candidate unseen burns yet.' : `Couldn't load the headline: ${h.ok ? '' : h.error.message}`)}</p>
      )}
      {rows.length ? (
        <div className="m-chart">
          <p className="m-chart__title">Audited by eye, with 95% intervals</p>
          <AuditForest rows={rows} />
        </div>
      ) : null}
      <div className="m-cols">
        <div>
          {audit?.method ? <Quote label={`How the audit was done${audit.date ? `, ${dateIST(`${audit.date}T12:00:00+05:30`, { year: true })}` : ''}`}>{audit.method}</Quote> : null}
          {rc?.note ? <Quote label="The recall sample">{rc.note}</Quote> : null}
        </div>
        <div>
          {u?.note ? <Quote label="How to read the share">{prose(u.note)}</Quote> : null}
          {audit?.lookalikes_removed ? <Quote label="Look-alikes already removed">{prose(audit.lookalikes_removed)}</Quote> : null}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// smoke model against PM2.5

export async function Smoke() {
  const [r, h] = await Promise.all([serverGet<SmokeValidation>(P.smokeValidation, undefined, 15_000), headline()]);
  if (!r.ok) return <Failed what="the smoke check" r={r} />;
  const d = r.data;
  const phrase = h.ok ? auditPhrase(h.data.unseen_burns) : null;
  const rows: SmokeCityRow[] = (d.cities ?? [])
    .map((c) => ({
      city: c.city,
      model: c.smoke_per_km_mixing_depth?.lag0?.r ?? null,
      modelLag: c.smoke_per_km_mixing_depth?.lag0?.lag_h ?? null,
      cams: c.cams_baseline?.lag0?.r ?? null,
      ventilation: c.ventilation_only?.lag0?.r ?? null,
      nEff: c.smoke_per_km_mixing_depth?.lag0?.n_eff ?? null,
      hours: c.hours_observed ?? null,
    }))
    .filter((c) => c.model != null || c.cams != null)
    .sort((a, b) => (b.model ?? -2) - (a.model ?? -2));
  return (
    <>
      <div className="m-figures">
        <Figure value={number(d.median_r_smoke_per_km_mixing_depth ?? null, { decimals: 2 })}>median correlation of BlindSpot&apos;s smoke model with measured PM2.5 across cities.</Figure>
        <Figure value={number(d.median_r_cams ?? null, { decimals: 2 })}>for the CAMS model, the baseline to beat.</Figure>
        <Figure value={number(d.median_r_ventilation_only ?? null, { decimals: 2 })}>for ventilation alone: stagnant air with no smoke at all.</Figure>
      </div>
      <div className="m-cols m-cols--figures">
        {d.headline ? <p className="m-statement">Delhi: {d.headline}.</p> : <span />}
        <Quote label="How the API checks it">{d.method}</Quote>
      </div>
      {rows.length ? (
        <div className="m-chart">
          <p className="m-chart__title">Correlation r with measured PM2.5, hour by hour, at no lead</p>
          <ul className="m-legend" aria-label="Legend">
            <li>
              <svg width="14" height="14" aria-hidden="true">
                <circle cx="7" cy="7" r="6" fill="var(--accent-mark)" />
              </svg>
              BlindSpot smoke model (smoke per km of mixing depth)
            </li>
            <li>
              <svg width="14" height="14" aria-hidden="true">
                <rect x="3" y="3" width="8" height="8" transform="rotate(45 7 7)" fill="var(--ink)" />
              </svg>
              CAMS model
            </li>
            <li>
              <svg width="14" height="14" aria-hidden="true">
                <circle cx="7" cy="7" r="5" fill="none" stroke="var(--muted)" strokeWidth="1.5" />
              </svg>
              Ventilation alone
            </li>
          </ul>
          <SmokeDots rows={rows} />
          <p className="m-caption">
            {d.period ? `Hindcast from ${dateTimeIST(d.period.from, { suffix: true })} to ${dateTimeIST(d.period.to, { suffix: true })}` : `Run of ${dateTimeIST(d.run_at, { suffix: true })}`}
            {d.inputs ? `, from ${number(d.inputs.fires ?? null)} fire detections and ${number(d.inputs.scars ?? null)} candidate unseen burns${phrase ? ` (${phrase})` : ''}` : ''}. &ldquo;n eff&rdquo; is the number of independent hours behind each r, as the API counts them: hourly values that follow each other are not independent.
          </p>
        </div>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// the risk ranking

const BASELINE_WORDS: Record<string, string> = {
  random: 'A random order',
  fires_7d_only: 'Fire alerts nearby in the last 7 days, alone',
  neighbourhood_only: 'Burning nearby, alone',
  burned_ha_7d_only: 'Scar hectares nearby in the last 7 days, alone',
  history_only: "Last season's fires nearby, alone",
};

/** The same rankings inside a sentence: "ranking by burning nearby alone scored …". */
const BASELINE_IN_A_SENTENCE: Record<string, string> = {
  random: 'a random order',
  fires_7d_only: 'ranking by fire alerts nearby in the last 7 days alone',
  neighbourhood_only: 'ranking by burning nearby alone',
  burned_ha_7d_only: 'ranking by scar hectares nearby in the last 7 days alone',
  history_only: "ranking by last season's fires nearby alone",
};

export async function Risk() {
  const r = await serverGet<RiskMethod>(PATHS.risk, { limit: 1 }, 15_000);
  if (!r.ok) return <Failed what="the risk method" r={r} />;
  const d = r.data;
  const m = d.method;
  const hb = d.backtest?.headline;
  const all = d.backtest?.all_pairs;
  return (
    <>
      {hb || all ? (
        <div className="m-figures">
          {all?.auc_weighted_mean != null ? (
            <Figure value={number(all.auc_weighted_mean, { decimals: 3 })}>
              area under the curve over all {number(all.pairs)} pass pairs, weighted by size{all.auc_ci95?.length === 2 ? ` (95% CI ${number(all.auc_ci95[0], { decimals: 3 })}–${number(all.auc_ci95[1], { decimals: 3 })})` : ''}. 0.5 is a random order; 1 would rank every burnt field first.
            </Figure>
          ) : null}
          {hb ? (
            <Figure value={number(hb.auc, { decimals: 3 })}>
              on the latest pair, tile {hb.tile}, {dateIST(hb.t0)} to {dateIST(hb.t1)}: {number(hb.burnt)} fields burnt and {number(hb.unburnt)} did not ({percent(hb.base_rate, 1)}).
            </Figure>
          ) : null}
        </div>
      ) : null}
      {m ? (
        <div className="m-cols">
          <div>
            <p className="m-formula">{m.formula}</p>
            <p className="m-small">{`Model ${m.model}, scoring ${number(d.totals?.scored ?? d.totals?.clusters ?? null)} harvested fields as of ${dateTimeIST(d.as_of, { suffix: true })}.`}</p>
            <p className="m-body">{prose(m.note)}</p>
          </div>
          <dl className="m-facts m-facts--wide">
            {Object.entries(m.factors).map(([k, v]) => (
              <div key={k}>
                <dt>
                  {k.charAt(0).toUpperCase() + k.slice(1)}
                  {m.weights[k] != null ? <span className="m-of nums">{` weight ${number(m.weights[k], { decimals: 1 })}`}</span> : null}
                </dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
      {all?.auc_by_pair?.length ? (
        <div className="m-chart">
          <p className="m-chart__title">How well the ranking ordered the next pass, for every pass pair</p>
          {/* a legend, not labels in the plot: on a phone the dots of a month crowd any label set among them */}
          <ul className="m-legend" aria-label="Legend">
            {all.auc_weighted_mean != null ? (
              <li>
                <svg width="22" height="14" aria-hidden="true">
                  <line x1="1" x2="21" y1="7" y2="7" stroke="var(--accent)" strokeWidth="2" />
                </svg>
                {`Weighted mean of all pairs, ${number(all.auc_weighted_mean, { decimals: 3 })}`}
              </li>
            ) : null}
            <li>
              <svg width="22" height="14" aria-hidden="true">
                <line x1="1" x2="21" y1="7" y2="7" stroke="var(--axis-ink)" strokeWidth="1.5" />
              </svg>
              0.5, a random ranking
            </li>
            {hb ? (
              <li>
                <svg width="14" height="14" aria-hidden="true">
                  <circle cx="7" cy="7" r="5.5" fill="var(--accent-mark)" stroke="var(--ink)" strokeWidth="1.5" />
                </svg>
                {`The latest pair: ${hb.tile}, ${dateIST(hb.t0)} to ${dateIST(hb.t1)}`}
              </li>
            ) : null}
          </ul>
          <AucStrip pairs={all.auc_by_pair} mean={all.auc_weighted_mean} lead={hb ? { tile: hb.tile, t0: hb.t0, t1: hb.t1, burnt: hb.burnt, unburnt: hb.unburnt } : null} />
          <p className="m-caption">Each dot is one pair of Sentinel-2 passes over one tile, placed at the later pass. One pass covers many tiles, so a date can hold many dots.</p>
        </div>
      ) : null}
      <div className="m-cols m-cols--table">
      {hb?.baselines ? (
        <div className="m-table-wrap">
          <table className="m-table m-table--fit">
            <caption>The latest pair against simpler rankings</caption>
            <thead>
              <tr>
                <th scope="col">Ranking</th>
                <th scope="col" className="num">Area under the curve</th>
              </tr>
            </thead>
            <tbody>
              <tr className="is-lead">
                <th scope="row">{`The risk score (${m?.model ?? 'current model'})`}</th>
                <td className="num">{number(hb.auc, { decimals: 3 })}</td>
              </tr>
              {Object.entries(hb.baselines)
                .sort((a, b) => b[1].auc - a[1].auc)
                .map(([k, v]) => (
                  <tr key={k}>
                    <th scope="row">{BASELINE_WORDS[k] ?? k.replace(/_/g, ' ')}</th>
                    <td className="num">{number(v.auc, { decimals: 3 })}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <div>
        <p className="m-statement">
          Both scores come from this season&apos;s own <span className="m-nobreak">Sentinel-2</span> passes.
        </p>
        <p className="m-body">
          {/* the pair the second figure, the gold dot and the table all describe; never "the pair shown first", which is the all-pairs mean */}
          {hb ? `The table's pair, tile ${hb.tile} from ${dateIST(hb.t0)} to ${dateIST(hb.t1)}, is ${hb.why_this_pair ?? 'the latest pass pair'}.` : ''}
          {(() => {
            // Said plainly when a simpler ranking beat the full score on this pair.
            const best = hb?.baselines ? Object.entries(hb.baselines).sort((a, b) => b[1].auc - a[1].auc)[0] : undefined;
            return hb && best && best[1].auc > hb.auc
              ? ` On it, ${BASELINE_IN_A_SENTENCE[best[0]] ?? `ranking by ${best[0].replace(/_only$/, '').replace(/_/g, ' ')} alone`} scored ${number(best[1].auc, { decimals: 3 })}, above the full score's ${number(hb.auc, { decimals: 3 })}.`
              : '';
          })()}
        </p>
      </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// limitations, in the API's own words where it states them

export async function Limits() {
  const [h, s, sm, rk, geo] = await Promise.all([
    headline(),
    scarsSummary(),
    serverGet<SmokeValidation>(P.smokeValidation, undefined, 15_000),
    serverGet<RiskMethod>(PATHS.risk, { limit: 1 }, 15_000),
    serverGet<GeoDiurnal>(PATHS.geostationaryDiurnal),
  ]);
  const items: { title: string; body: string }[] = [];
  const a = s.ok ? s.data.method?.audit : undefined;
  if (a?.known_lookalikes) items.push({ title: 'Some scars are not burns.', body: `${a.known_lookalikes.charAt(0).toUpperCase()}${a.known_lookalikes.slice(1)}.` });
  const rc = a?.recall_vs_firms;
  if (rc?.recall_same_window != null)
    items.push({
      title: 'The scars miss fires too.',
      body: `Of ${number(rc.viirs_detections ?? null)} VIIRS fire detections in the audited square, ${number(rc.scar_same_window ?? null)} had a scar between the same two passes (${percent(rc.recall_same_window)}${rc.recall_same_window_ci95 ? `, 95% CI ${percent(rc.recall_same_window_ci95[0])}–${percent(rc.recall_same_window_ci95[1])}` : ''}) and ${number(rc.scar_any_window ?? null)} in any window. Small fields, cloud and passes far apart all hide scars.`,
    });
  const u = h.ok ? h.data.unseen_burns : undefined;
  if (u?.note) {
    const phrase = auditPhrase(u);
    items.push({ title: 'The unseen share is an upper bound.', body: `${prose(u.note)}${phrase && u.unseen_share != null ? ` Today: ${percent(u.unseen_share)} of the burnt area, ${phrase}.` : ''}` });
  }
  if (geo.ok && geo.data.interpretation?.[1]) items.push({ title: 'GK-2A is an activity index, not a fire count.', body: geo.data.interpretation[1] });
  const na = sm.ok ? sm.data.method?.match(/Correlation is not attribution.*$/)?.[0] : null;
  if (sm.ok && sm.data.headline) items.push({ title: 'The smoke check is thin.', body: `In Delhi, ${sm.data.headline}.${na ? ` ${na}` : ''}` });
  if (rk.ok && rk.data.method?.note) items.push({ title: 'Risk is a ranking.', body: prose(rk.data.method.note) });
  if (!items.length)
    return (
      // marked when a read failed, so this render never replaces a good offline copy of the page
      <p className="m-failed" {...loadFailed(!h.ok || !s.ok || !sm.ok || !rk.ok || !geo.ok)}>
        The limitations are stated by the API with each figure, and the API is not answering just now.
      </p>
    );
  return (
    <ul className="m-limits">
      {items.map((it) => (
        <li key={it.title}>
          <p className="m-limits__title">{it.title}</p>
          <p className="m-limits__body">{it.body}</p>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------------------------
// data sources

const KIND_WORDS: Record<string, string> = {
  fires: 'Active-fire detections from polar satellites',
  fires_official: 'Official residue-burning events',
  fires_geo: 'Geostationary scans every 10 minutes',
  orbits: 'Orbital elements for the pass clock',
  air: 'Measured air quality at ground stations',
  air_model: 'Modelled air quality (a model, not a measurement)',
  air_attribution: "Modelled stubble share of Delhi's PM2.5",
  weather: 'Winds and mixing height for the smoke model',
  imagery: 'Sentinel-2 scenes and pictures',
  landcover: 'Cropland mask',
  boundaries: 'State, district and subdistrict outlines',
  places: 'Village, town and city names',
  llm: 'The investigator and the bulletin',
};

/** A licence the API states: in the source's own label, or in the boundaries attribution. */
function licenceFor(key: string, label: string, attribution: string | null): string | null {
  const inLabel = label.match(/\b(CC BY[\w .-]*\d(?:\.\d)?|CC0|ODbL)\b/);
  if (inLabel) return inLabel[1];
  if (!attribution) return null;
  const word: Record<string, RegExp> = {
    osm: /OpenStreetMap[^()]*\(([^)]+)\)/,
    geonames: /GeoNames[^()]*\(([^)]+)\)/,
    wikidata: /Wikidata[^()]*\(([^)]+)\)/,
    lgd: /ramSeraph[^()]*\(([^)]+)\)/,
    boundaries: /ramSeraph[^()]*\(([^)]+)\)/,
  };
  return word[key] ? (attribution.match(word[key])?.[1] ?? null) : null;
}

function cadence(min: number | null): string {
  if (min == null) return 'On demand';
  if (min < 60) return `Every ${min} min`;
  if (min === 1440) return 'Daily';
  if (min === 10080) return 'Weekly';
  return min % 60 === 0 ? `Every ${min / 60} h` : `Every ${min} min`;
}

/** One row per upstream: the API registers some upstreams under two keys (the Sentinel-2 COGs on
 *  AWS, as `s2_cog` and `s2_cog_e84`, share one address), which read as a duplicate. Rows with the
 *  same docs address merge into the one with the newest good data, which keeps its own status, label
 *  and last good time; a cadence missing there comes from its twin. */
function onePerUpstream(rows: SourceRow[]): SourceRow[] {
  const groups = new Map<string, SourceRow[]>();
  for (const r of rows) {
    const k = r.docs_url ?? `key:${r.key}`;
    groups.set(k, [...(groups.get(k) ?? []), r]);
  }
  return [...groups.values()].map((g) => {
    if (g.length === 1) return g[0];
    const t = (r: SourceRow) => (r.last_success_at ? Date.parse(r.last_success_at) : -Infinity);
    const best = g.reduce((a, b) => (t(b) > t(a) ? b : a));
    return { ...best, cadence_minutes: best.cadence_minutes ?? g.find((r) => r.cadence_minutes != null)?.cadence_minutes ?? null };
  });
}

export async function Sources() {
  const [r, b] = await Promise.all([serverGet<SourcesList>(PATHS.sources), serverGet<StateBoundaries>(PATHS.geoBoundaries, { level: 'state', simplify: 0.02 }, 12_000)]);
  if (!r.ok) return <Failed what="the source list" r={r} />;
  const attribution = b.ok ? (b.data.attribution ?? null) : null;
  const rows = onePerUpstream(r.data.sources.filter((s) => s.kind !== 'unregistered'));
  const ok = rows.filter((s) => s.status === 'ok').length;
  return (
    <>
      <p className="m-statement">
        {number(ok)} of {number(rows.length)} sources answered on their last attempt, checked <Relative iso={r.data.generated_at} />.
      </p>
      <div className="m-table-wrap">
        <table className="m-table m-table--sources">
          <caption>Every upstream the API reads, with its status now</caption>
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">What it gives</th>
              <th scope="col">Refreshed</th>
              <th scope="col">Licence</th>
              <th scope="col">Last good data</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const lic = licenceFor(s.key, s.label, attribution);
              return (
                <tr key={s.key}>
                  <th scope="row">
                    {s.docs_url ? (
                      <a className="m-link" href={s.docs_url} target="_blank" rel="noreferrer noopener">
                        {s.label}
                      </a>
                    ) : (
                      s.label
                    )}
                  </th>
                  <td>{KIND_WORDS[s.kind] ?? s.kind.replace(/_/g, ' ')}</td>
                  <td className="nowrap">{cadence(s.cadence_minutes)}</td>
                  {/* a licence the API does not state is not guessed: the cell links to the source instead */}
                  <td>
                    {lic ??
                      (s.docs_url ? (
                        <a className="m-link" href={s.docs_url} target="_blank" rel="noreferrer noopener" aria-label={`${s.label}: its terms, on the source's site (opens a new tab)`}>
                          Source&apos;s terms
                        </a>
                      ) : (
                        <span className="missing">{MISSING}</span>
                      ))}
                  </td>
                  <td className="nowrap">
                    <span className={`m-status m-status--${s.status}`} aria-hidden="true" />
                    {s.status === 'ok' ? '' : `${s.status}, `}
                    {s.last_success_at ? <Relative iso={s.last_success_at} /> : 'never'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {attribution ? <p className="m-small">{attribution}</p> : null}
    </>
  );
}

export async function ArchitectureSection() {
  const r = await serverGet<SourcesList>(PATHS.sources);
  return <Architecture sources={r.ok ? r.data.sources : null} />;
}
