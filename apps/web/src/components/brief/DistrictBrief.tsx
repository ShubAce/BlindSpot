import Link from 'next/link';
import type { ReactNode } from 'react';

import { Kpi } from '@/components/case/parts';
import { liveAtHref } from '@/components/live/location-link';
import { dateTimeIST, metricText, MISSING, NBSP, number } from '@/lib/format';

import { CsvButtons } from './CsvDownload';
import { BarCell, BriefScope, LongerWindows, NotesCard, RowLine, SourcesFoot } from './parts';
import { PrintButton } from './PrintButton';
import { auditSentence, count, csvHref, districtHref, ha, haText, indexHref, passDate, plural, rankingSentence, ROW_LIMIT, strengthLabel, windowWords } from './read';
import type { BriefVillage, BriefWatchRow, DistrictBriefResponse } from './types';

/**
 * /brief/[district]: one district's field brief, to read on screen, print for a field visit or
 * download as a CSV in English, Hindi or Punjabi.
 *
 * Its question: which villages should a team visit, and which harvested fields should it watch? Two
 * kinds of evidence, each with its own qualification beside it, never merged: the villages where
 * Sentinel-2 mapped candidate unseen burns (upper bounds, with the audit), and the watchlist of
 * harvested fields ordered by the burn-risk run (ranks, never probabilities, with the backtest). The
 * watchlist's rows outside the run's top tenth sit in their own group and say so.
 */

export function BackToIndex({ days }: { days: number }) {
  return (
    <p className="br-eyebrow">
      <Link href={indexHref(days)} className="br-eyebrow__link">
        <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14" data-print="hide">
          <path d="M8.5 3.5L5 7l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span className="br-print-only">BlindSpot </span>
        Field brief
      </Link>
    </p>
  );
}

export function DistrictBrief({ data, days, watchAll = false }: { data: DistrictBriefResponse; days: number; watchAll?: boolean }) {
  const w = data.window;
  const t = data.totals;
  const district = data.district;
  // ?watch=all reads every row of both lists; the villages stay at the first ROW_LIMIT, as their CSV has the rest
  const villages = (data.villages ?? []).slice(0, ROW_LIMIT);
  const watch = data.watchlist ?? [];
  const risk = data.risk_run;

  const burnt =
    w && t.unseen_clusters > 0
      ? `${count(t.unseen_clusters)} candidate unseen ${plural(t.unseen_clusters, 'burn')} covering ${metricText('area_ha', t.unseen_area_ha)} in ${count(t.villages)} ${plural(t.villages, 'village')}, in ${windowWords(w)}.`
      : w
        ? `No candidate unseen burns in ${windowWords(w)}.`
        : `No burn scar is mapped yet${data.reason ? `: ${data.reason}` : '.'}`;
  const watched =
    t.top_tenth_fields > 0
      ? ` ${count(t.top_tenth_fields)} harvested ${plural(t.top_tenth_fields, 'field')} in ${count(t.top_tenth_villages)} ${plural(t.top_tenth_villages, 'village')} rank in the top tenth for the next burn.`
      : t.harvested_fields > 0
        ? ` None of its ${count(t.harvested_fields)} harvested fields ranks in the top tenth for the next burn.`
        : '';

  return (
    <>
      <div className="title-row br-title">
        <div className="title-row__text">
          <BackToIndex days={days} />
          <h1>
            {district}
            {data.state ? <span className="br-h1-state">{`${NBSP}${data.state}`}</span> : null}
          </h1>
          <p className="finding">
            {burnt}
            {watched}
          </p>
          <p className="finding-audit br-audit">{auditSentence(data.candidate_unseen_audit)}</p>
          {/* the finding's top-tenth count (and the KPI below) is a rank: its backtest stands beside it, as on the index */}
          {watched ? <p className="finding-audit br-audit">{rankingSentence(data.ranking_reliability)}</p> : null}
        </div>
        <BriefScope data={data} days={days} hrefFor={(d) => districtHref(district, d, watchAll)} />
      </div>

      <div className="kpis" role="group" aria-label={`Totals for ${district}`}>
        <Kpi label="Candidate unseen burns" value={count(t.unseen_clusters)} sub="upper bound" />
        {/* a real zero prints as "0", not "0.0" */}
        <Kpi label="Their area" value={ha(t.unseen_area_ha, t.unseen_area_ha >= 100 || !t.unseen_area_ha ? 0 : 1)} unit="ha" sub="upper bound" />
        <Kpi label="Villages with them" value={count(t.villages)} sub={t.newest_pass ? `newest pass ${passDate(t.newest_pass)}` : 'none in the window'} />
        <Kpi label="Harvested fields" value={count(t.harvested_fields)} sub={`straw still lying, ${haText(t.harvested_area_ha, 0)}`} />
        <Kpi label="In the top tenth" value={count(t.top_tenth_fields)} unit={plural(t.top_tenth_fields, 'field')} sub={`in ${count(t.top_tenth_villages)} ${plural(t.top_tenth_villages, 'village')}`} />
      </div>

      <VillagesCard data={data} villages={villages} days={days} />
      <WatchlistCard data={data} watch={watch} days={days} watchAll={watchAll} />
      {/* the API's reading notes follow the lists: the finding above already carries the audit and
          the ranking's backtest, so the villages need not wait a phone screen behind them */}
      <NotesCard notes={data.notes} />

      {/* after the lists and their notes: the page leads with the figures and the villages, the
          part a field team reads on a phone (the CSV sits on the villages' own card) */}
      <div className="br-actions br-actions--end" data-print="hide">
        <PrintButton />
        <span className="br-actions__links">
          {/* the map opens on the district's villages and watched fields (plain /live when none has
              coordinates); the investigator opens with a question about it typed in, never sent */}
          <Link className="br-textlink" href={liveAtHref([...(data.villages ?? []), ...(data.watchlist ?? [])])}>
            Open on the live map
          </Link>
          <Link className="br-textlink" href={`/live?draft=${encodeURIComponent(`What should field teams in ${district} check first this week?`)}`}>
            Ask the investigator
          </Link>
        </span>
      </div>

      <SourcesFoot data={data} window={w} risk={risk} />
      <p className="br-print-only br-printed">
        {`Printed from the BlindSpot field brief for ${district}, built ${dateTimeIST(data.generated_at, { year: true, suffix: true })}. Every village of the window, with its map link, is in the CSV: ${csvHref(data.links?.csv, district, days, 'en')}`}
      </p>
    </>
  );
}

// ---------------------------------------------------------------------------------------------

function Card({ id, title, sub, aside, className, children }: { id: string; title: string; sub?: ReactNode; aside?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <section className={`card br-card${className ? ` ${className}` : ''}`} aria-labelledby={`${id}-title`}>
      <header className="br-card__head">
        <div className="br-card__titles">
          <h2 id={`${id}-title`} className="card__title">
            {title}
          </h2>
          {sub ? <p className="br-card__sub">{sub}</p> : null}
        </div>
        {aside ? <div className="card__aside br-card__aside">{aside}</div> : null}
      </header>
      {children}
    </section>
  );
}

const villageName = (v: string | null) =>
  v ?? (
    <span className="br-unnamed">
      Unnamed <span className="br-unnamed__why">no village within 3 km</span>
    </span>
  );

/** Google Maps on screen; on paper the coordinates, which a phone can still find. */
function MapCell({ url, lat, lon, place }: { url: string | null; lat: number | null; lon: number | null; place: string }) {
  const coords = lat != null && lon != null ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : null;
  return (
    <td className="br-c-act br-c-map">
      {url ? (
        <a className="br-rowlink" href={url} target="_blank" rel="noreferrer noopener" title={coords ? `Open ${coords} in Google Maps` : 'Open in Google Maps'} data-print="hide">
          Map
          <svg aria-hidden="true" width="11" height="11" viewBox="0 0 12 12" className="br-ext">
            <path d="M4.5 2.5h5v5M9.5 2.5L3 9" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="sr-only">{`: ${place} in Google Maps, opens a new tab`}</span>
        </a>
      ) : (
        <span className="missing" data-print="hide">{MISSING}</span>
      )}
      {coords ? <span className="br-print-only nums">{coords}</span> : null}
    </td>
  );
}

function VillagesCard({ data, villages, days }: { data: DistrictBriefResponse; villages: BriefVillage[]; days: number }) {
  const w = data.window;
  const total = data.villages_total ?? villages.length;
  const maxHa = villages.reduce((m, v) => Math.max(m, v.unseen_area_ha || 0), 0);
  return (
    <Card
      id="br-villages"
      title="Villages with candidate unseen burns"
      sub="Largest candidate unseen area first. A village’s map link and case file open its largest burn."
      aside={total > villages.length ? `The ${count(villages.length)} largest of ${count(total)}; the CSV has all ${count(total)}` : `${count(total)} ${plural(total, 'village')}`}
    >
      {/* the CSV lists the villages with candidate unseen burns: with none, it would be a header only */}
      {total > 0 ? (
        <div className="br-actions br-actions--csv" data-print="hide">
          <CsvButtons csvPath={data.links?.csv} district={data.district} days={days} window={w} />
        </div>
      ) : null}
      {villages.length ? (
        <div className="br-tablebox">
          <table className="br-table br-table--villages">
            <caption className="sr-only">{`Villages of ${data.district} with candidate unseen burns${w ? `, ${windowWords(w)}` : ''}`}</caption>
            <thead>
              <tr>
                <th scope="col" className="br-num br-c-rank">
                  <span aria-hidden="true">#</span>
                  {/* the row's place in the list, not the burn-risk rank the watchlist shows */}
                  <span className="sr-only">Position</span>
                </th>
                <th scope="col" className="br-c-name">
                  Village
                </th>
                <th scope="col">Sub-district</th>
                <th scope="col" className="br-num">
                  Burns
                </th>
                <th scope="col" className="br-num">
                  Hectares
                </th>
                <td className="br-c-bar" aria-hidden="true" />
                <th scope="col" className="br-num">
                  Newest pass
                </th>
                <th scope="col" className="br-c-act">
                  Location
                </th>
                <th scope="col" className="br-c-act">
                  Largest burn
                </th>
              </tr>
            </thead>
            <tbody>
              {villages.map((v, i) => {
                const caseId = v.case_ids?.[0] ?? null;
                const place = v.village ?? `an unnamed place in ${v.subdistrict ?? data.district}`;
                return (
                  <tr key={`${v.village ?? '-'}|${v.subdistrict ?? '-'}|${i}`}>
                    <td className="br-num br-c-rank">{i + 1}</td>
                    <th scope="row" className="br-c-name">
                      {villageName(v.village)}
                    </th>
                    <td className="br-c-lead">
                      <span className="br-cell">{v.subdistrict ?? <span className="missing">{MISSING}</span>}</span>
                      <RowLine parts={[v.subdistrict, `${count(v.unseen_clusters)} ${plural(v.unseen_clusters, 'burn')}`, `newest pass ${passDate(v.newest_pass)}`]} />
                    </td>
                    <td className="br-num br-c-meta">
                      {count(v.unseen_clusters)}
                      <span className="br-ph">{` ${plural(v.unseen_clusters, 'burn')}`}</span>
                    </td>
                    <td className="br-num br-c-key">
                      {ha(v.unseen_area_ha, 1)}
                      <span className="br-ph">{`${NBSP}ha`}</span>
                    </td>
                    <BarCell value={v.unseen_area_ha} max={maxHa} />
                    <td className="br-num br-c-meta">
                      <span className="br-ph">newest pass </span>
                      {passDate(v.newest_pass)}
                    </td>
                    <MapCell url={v.maps_url} lat={v.lat} lon={v.lon} place={place} />
                    <td className="br-c-act">
                      {caseId != null ? (
                        <Link className="br-rowlink" href={`/case/${caseId}`} prefetch={false}>
                          {`Case ${caseId}`}
                        </Link>
                      ) : (
                        <span className="missing">{MISSING}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="br-empty">
          {w ? `No candidate unseen burns in ${data.district} in ${windowWords(w)}.` : `No burn scar is mapped yet${data.reason ? `: ${data.reason}` : '.'}`}
          {w ? <LongerWindows days={days} hrefFor={(d) => districtHref(data.district, d)} /> : null}
        </p>
      )}
    </Card>
  );
}

function WatchlistCard({ data, watch, days, watchAll }: { data: DistrictBriefResponse; watch: BriefWatchRow[]; days: number; watchAll: boolean }) {
  const risk = data.risk_run;
  const rel = data.ranking_reliability;
  const top = watch.filter((r) => r.in_top_tenth);
  const rest = watch.filter((r) => !r.in_top_tenth);
  const total = data.watchlist_total ?? watch.length;
  const inTop = data.watchlist_in_top_tenth ?? top.length;
  const pill = strengthLabel(rel);
  const ranked = risk?.fields_ranked != null ? number(risk.fields_ranked) : null;
  const runAt = risk?.as_of ? dateTimeIST(risk.as_of, { suffix: true }) : null;
  // Worded from the data: a district whose fields all rank below the run's top tenth (19 of the 44 of
  // the week to 30 Oct) must not be told its list is "ranked highest for the next burn".
  const title =
    inTop > 0
      ? 'Watchlist: harvested fields, the burn-risk run’s top tenth first'
      : total > 0
        ? 'Watchlist: harvested fields by burn-risk rank (none in the top tenth)'
        : 'Watchlist: harvested fields by burn-risk rank';
  // No CSV carries the watchlist, so the page itself offers every row (?watch=all), and the way back.
  const cut = total > watch.length;
  const toggle = watchAll ? (watch.length > ROW_LIMIT ? `Show the first ${ROW_LIMIT}` : null) : cut ? `Show all ${count(total)}` : null;

  const row = (r: BriefWatchRow, rank: number) => {
    const place = r.village ?? `an unnamed place in ${r.subdistrict ?? data.district}`;
    return (
      <tr key={`${r.village ?? '-'}|${r.subdistrict ?? '-'}|${rank}`} className={r.in_top_tenth ? undefined : 'is-below'}>
        <td className="br-num br-c-rank">{rank}</td>
        <th scope="row" className="br-c-name">
          {villageName(r.village)}
        </th>
        <td className="br-c-lead">
          <span className="br-cell">{r.subdistrict ?? <span className="missing">{MISSING}</span>}</span>
          <RowLine
            parts={[
              r.subdistrict,
              `${count(r.fields)} ${plural(r.fields, 'field')}`,
              `${ha(r.area_ha, 1)}${NBSP}ha`,
              r.best_rank != null ? `best rank ${count(r.best_rank)}` : null,
            ]}
          />
        </td>
        <td className="br-num br-c-meta">
          {count(r.fields)}
          <span className="br-ph">{` ${plural(r.fields, 'field')}`}</span>
        </td>
        <td className="br-num br-c-meta">
          {ha(r.area_ha, 1)}
          <span className="br-ph">{`${NBSP}ha`}</span>
        </td>
        <td className="br-num br-c-key">
          {r.in_top_tenth ? (
            <>
              {count(r.top_tenth_fields)}
              <span className="br-ph">{' in the top tenth'}</span>
            </>
          ) : (
            <>
              <span className="br-dt">0</span>
              <span className="br-ph">not in the top tenth</span>
            </>
          )}
        </td>
        <td className="br-num br-c-meta">
          <span className="br-ph">best rank </span>
          {r.best_rank != null ? count(r.best_rank) : <span className="missing">{MISSING}</span>}
        </td>
        <MapCell url={r.maps_url} lat={r.lat} lon={r.lon} place={place} />
      </tr>
    );
  };

  return (
    <Card
      id="br-watch"
      className="br-card--watch"
      title={title}
      sub={`Harvested fields whose straw was still lying on the latest clear pass, grouped by village and ordered by the burn-risk run${runAt ? ` of ${runAt}` : ''}.`}
      aside={
        <>
          <span>{cut ? `The first ${count(watch.length)} of ${count(total)} villages` : `${watchAll && total > ROW_LIMIT ? 'All ' : ''}${count(total)} ${plural(total, 'village')}`}</span>
          {toggle ? (
            <Link className="br-textlink br-asidelink" href={`${districtHref(data.district, days, !watchAll)}#br-watch-title`} prefetch={false} data-print="hide">
              {toggle}
            </Link>
          ) : null}
        </>
      }
    >
      <div className="br-reliability">
        {pill ? <span className="br-pill br-pill--warning">{pill}</span> : null}
        <p>{rel?.reading || rankingSentence(rel)}</p>
      </div>

      {watch.length ? (
        <div className="br-tablebox">
          <table className="br-table br-table--watch">
            <caption className="sr-only">{`Watchlist of ${data.district}: villages with harvested fields, ordered by the burn-risk ranking`}</caption>
            <thead>
              <tr>
                <th scope="col" className="br-num br-c-rank">
                  <span aria-hidden="true">#</span>
                  {/* "Rank" here would read beside "Best rank" as if it were the burn-risk rank */}
                  <span className="sr-only">Position</span>
                </th>
                <th scope="col" className="br-c-name">
                  Village
                </th>
                <th scope="col">Sub-district</th>
                <th scope="col" className="br-num">
                  Harvested fields
                </th>
                <th scope="col" className="br-num">
                  Hectares
                </th>
                <th scope="col" className="br-num">
                  Fields in the top tenth
                </th>
                <th scope="col" className="br-num">
                  Best rank
                  {ranked ? <span className="br-th-sub">{`of ${ranked}`}</span> : null}
                </th>
                <th scope="col" className="br-c-act">
                  Location
                </th>
              </tr>
            </thead>
            {top.length ? (
              <tbody>
                <tr className="br-grouprow">
                  <th colSpan={8} scope="rowgroup">
                    {`In the top tenth: ${count(inTop)} ${plural(inTop, 'village has', 'villages have')} fields among the run’s highest-ranked tenth`}
                  </th>
                </tr>
                {top.map((r, i) => row(r, i + 1))}
              </tbody>
            ) : null}
            {rest.length ? (
              <tbody className="br-below">
                <tr className="br-grouprow">
                  <th colSpan={8} scope="rowgroup">
                    {top.length
                      ? 'Not in the top tenth: listed by their best-ranked field only, not high risk'
                      : `No field in ${data.district} is in the top tenth: these villages are listed by their best-ranked field only, not high risk`}
                  </th>
                </tr>
                {rest.map((r, i) => row(r, top.length + i + 1))}
              </tbody>
            ) : null}
          </table>
        </div>
      ) : (
        <p className="br-empty">{`No harvested field with straw still lying is mapped in ${data.district} on the latest clear pass.`}</p>
      )}

      <p className="card__note">
        {`Best rank is the rank of the village’s best field among the ${ranked ?? 'ranked'} fields of the run (1 = the highest). The brief has no scores and no probabilities.`}
      </p>
    </Card>
  );
}
