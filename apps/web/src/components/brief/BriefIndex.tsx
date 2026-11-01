import Link from 'next/link';

import { Kpi } from '@/components/case/parts';
import { MISSING, NBSP } from '@/lib/format';
import { LANGS } from '@/lib/langs';

import { BarCell, BriefScope, LongerWindows, NotesCard, RowLine, SourcesFoot } from './parts';
import { auditSentence, count, csvFileName, csvHref, districtHref, ha, indexHref, passDate, plural, rankingSentence, windowWords } from './read';
import type { BriefDistrictRow, BriefIndexResponse, CsvLang } from './types';

/**
 * /brief: the districts of Punjab and Haryana ranked by their candidate unseen burns in a window of
 * Sentinel-2 passes, for the pollution control boards' field teams (PPCB, HSPCB) and for anyone
 * judging what BlindSpot finds.
 *
 * Its question: which district should a field team open first? The ranked table answers it (largest
 * candidate unseen area first, as the API orders it), with a bar per row so the order is seen and
 * not only read. The audit and the ranking's backtest sit directly under the totals, so no number
 * appears without its qualification. Each row opens the district's brief and its CSV.
 */

export function BriefIndex({ data, days, lang }: { data: BriefIndexResponse; days: number; lang: CsvLang }) {
  const w = data.window;
  const rows = data.districts ?? [];
  const totals = data.totals;
  const maxHa = rows.reduce((m, r) => Math.max(m, r.unseen_area_ha || 0), 0);
  // Every village belongs to one district, so these sums are exact for the listed districts.
  const villages = rows.reduce((s, r) => s + (r.villages || 0), 0);
  const topFields = rows.reduce((s, r) => s + (r.top_tenth_fields || 0), 0);
  const topVillages = rows.reduce((s, r) => s + (r.top_tenth_villages || 0), 0);
  const csvLang = LANGS.find((l) => l.code === lang) ?? LANGS[0];

  const finding = `Where field teams could go first: the villages where Sentinel-2 mapped candidate burn scars that no fire alert explains (upper bounds)${w ? `, in ${windowWords(w)}` : ''}, and the harvested fields ranked highest for the next burn.`;

  return (
    <>
      <div className="title-row br-title">
        <div className="title-row__text">
          <h1>Field brief</h1>
          <p className="finding">{finding}</p>
        </div>
        <BriefScope data={data} days={days} hrefFor={(d) => indexHref(d, lang)} />
      </div>

      {!w ? (
        <p className="br-empty">{`No burn scar is mapped yet, so there is no brief to give${data.reason ? `: ${data.reason}` : '.'}`}</p>
      ) : (
        <>
          <div className="kpis br-kpis--4" role="group" aria-label={`Totals for ${windowWords(w)}`}>
            <Kpi label="Candidate unseen burns" value={count(totals?.unseen_clusters)} sub="upper bound" />
            <Kpi label="Their area" value={ha(totals?.unseen_area_ha, 0)} unit="ha" sub="upper bound" />
            <Kpi label="Villages with them" value={count(villages)} sub={`in ${count(totals?.districts ?? rows.length)} districts`} />
            <Kpi label="Harvested fields in the top tenth" value={count(topFields)} sub={`in ${count(topVillages)} villages of these districts`} />
          </div>

          {/* the honesty of the numbers, set in the same view as them: the audit first, then the ranking */}
          <div className="br-qual">
            <p>{auditSentence(data.candidate_unseen_audit)}</p>
            <p>{rankingSentence(data.ranking_reliability)}</p>
          </div>

          <section className="card br-card" aria-labelledby="br-districts-title">
            <header className="br-card__head">
              <div className="br-card__titles">
                <h2 id="br-districts-title" className="card__title">
                  Districts, largest candidate unseen area first
                </h2>
                <p className="br-card__sub">{`${count(rows.length)} districts of Punjab and Haryana. Open one for its villages and its watchlist.`}</p>
              </div>
              <nav className="br-langs" aria-label="CSV language" data-print="hide">
                <span className="br-langs__label">CSV in</span>
                {LANGS.map((l) => (
                  <Link key={l.code} href={indexHref(days, l.code)} aria-current={l.code === lang ? 'true' : undefined} className="br-langs__opt" scroll={false}>
                    <span lang={l.code}>{l.label}</span>
                  </Link>
                ))}
              </nav>
            </header>

            {rows.length ? (
              <div className="br-tablebox">
                <table className="br-table br-table--districts">
                  <caption className="sr-only">{`Districts ranked by the area of their candidate unseen burns, ${windowWords(w)}`}</caption>
                  {/* the two group headers (scope="colgroup") name only the columns of a real column
                      group: without these, the table model gives them no cells, and both "Villages"
                      columns would be read without their group */}
                  <colgroup span={3} />
                  <colgroup span={5} />
                  <colgroup span={2} />
                  <colgroup />
                  <thead>
                    <tr className="br-groups">
                      <th rowSpan={2} scope="col" className="br-num br-c-rank">
                        <span aria-hidden="true">#</span>
                        <span className="sr-only">Position</span>
                      </th>
                      <th rowSpan={2} scope="col" className="br-c-name">
                        District
                      </th>
                      <th rowSpan={2} scope="col">
                        State
                      </th>
                      <th colSpan={5} scope="colgroup" className="br-group">
                        Candidate unseen burns <span className="br-group__note">upper bounds</span>
                      </th>
                      <th colSpan={2} scope="colgroup" className="br-group">
                        Harvested fields in the top tenth
                      </th>
                      <th rowSpan={2} scope="col" className="br-c-act" data-print="hide">
                        CSV
                      </th>
                    </tr>
                    <tr>
                      <th scope="col" className="br-num">
                        Burns
                      </th>
                      <th scope="col" className="br-num">
                        Hectares
                      </th>
                      <td className="br-c-bar" aria-hidden="true" />
                      {/* the words a screen reader without column groups would miss */}
                      <th scope="col" className="br-num">
                        Villages<span className="sr-only"> with candidate unseen burns</span>
                      </th>
                      <th scope="col" className="br-num">
                        Newest pass
                      </th>
                      <th scope="col" className="br-num">
                        Fields<span className="sr-only"> in the top tenth</span>
                      </th>
                      <th scope="col" className="br-num">
                        Villages<span className="sr-only"> with fields in the top tenth</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <DistrictRow key={r.district} r={r} rank={i + 1} maxHa={maxHa} days={days} lang={lang} langName={csvLang.english} window={w} />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="br-empty">
                {`No candidate unseen burns in ${windowWords(w)}.`}
                <LongerWindows days={days} hrefFor={(d) => indexHref(d, lang)} />
              </p>
            )}

            <p className="card__note">
              {`Burns and hectares count candidate unseen burns: Sentinel-2 burn scars that no NASA FIRMS fire alert explains, so upper bounds. The top tenth counts harvested fields whose straw was still lying on the latest clear pass and that rank in the top tenth of the burn-risk run; a district with none still has a watchlist on its page. Each CSV lists every village of the window in ${csvLang.english}, with the audit in its header.`}
            </p>
          </section>
        </>
      )}

      <NotesCard notes={data.notes} />
      <SourcesFoot data={data} window={w} risk={data.risk_run} />
    </>
  );
}

function DistrictRow({
  r,
  rank,
  maxHa,
  days,
  lang,
  langName,
  window: w,
}: {
  r: BriefDistrictRow;
  rank: number;
  maxHa: number;
  days: number;
  lang: CsvLang;
  langName: string;
  window: BriefIndexResponse['window'];
}) {
  const none = !r.top_tenth_fields;
  return (
    <tr>
      <td className="br-num br-c-rank">{rank}</td>
      <th scope="row" className="br-c-name">
        <Link href={districtHref(r.district, days)} className="br-namelink">
          {r.district}
        </Link>
      </th>
      <td className="br-c-lead">
        <span className="br-cell">{r.state ?? <span className="missing">{MISSING}</span>}</span>
        <RowLine
          parts={[
            r.state,
            `${count(r.unseen_clusters)} ${plural(r.unseen_clusters, 'burn')}`,
            `${count(r.villages)} ${plural(r.villages, 'village')}`,
            `newest pass ${passDate(r.newest_pass)}`,
          ]}
        />
      </td>
      <td className="br-num br-c-meta">
        {count(r.unseen_clusters)}
        <span className="br-ph">{` ${plural(r.unseen_clusters, 'burn')}`}</span>
      </td>
      <td className="br-num br-c-key">
        {ha(r.unseen_area_ha, 0)}
        <span className="br-ph">{`${NBSP}ha`}</span>
      </td>
      <BarCell value={r.unseen_area_ha} max={maxHa} />
      <td className="br-num br-c-meta">
        {count(r.villages)}
        <span className="br-ph">{` ${plural(r.villages, 'village')}`}</span>
      </td>
      <td className="br-num br-c-meta">
        <span className="br-ph">newest pass </span>
        {passDate(r.newest_pass)}
      </td>
      <td className={`br-num br-c-tt${none ? ' is-zero' : ''}`}>
        {none ? (
          <>
            <span className="br-dt">0</span>
            <span className="br-ph">No harvested field in the top tenth</span>
          </>
        ) : (
          <>
            <span className="br-ph">Top tenth: </span>
            {count(r.top_tenth_fields)}
            <span className="br-ph">{` ${plural(r.top_tenth_fields, 'field')}`}</span>
          </>
        )}
      </td>
      <td className={`br-num br-c-tt${none ? ' is-zero br-dt-cell' : ''}`}>
        <span className="br-ph">in </span>
        {count(r.top_tenth_villages)}
        <span className="br-ph">{` ${plural(r.top_tenth_villages, 'village')}`}</span>
      </td>
      <td className="br-c-act" data-print="hide">
        <a className="br-rowlink" href={csvHref(r.links?.csv, r.district, days, lang)} download={csvFileName(r.district, w, lang)} hrefLang={lang} type="text/csv">
          CSV
          <span className="sr-only">{` for ${r.district}, in ${langName}`}</span>
        </a>
      </td>
    </tr>
  );
}
