import { Suspense } from 'react';

import { Skeleton } from '@/components/ui/states';
import { dateTimeIST, metricText, number } from '@/lib/format';
import { PHOTOS } from '@/lib/photo-credits';

import { getRisk } from './data';
import { s } from './styles';
import { Pic } from './Pic';
import { Reveal } from './Reveal';
import { firstPlace, order } from './words';

/**
 * Section 3, list (direction: "Statement, table pushed right"). The live ranking of villages whose
 * fields are harvested but not yet burnt (GET /api/risk `ranking`), with the method's own caveat and
 * its backtest stated plainly beside it.
 */

const ROWS = 12;

export function Harvested() {
  return (
    <Reveal as="section" className={s.list} aria-labelledby="list-h">
      <h2 id="list-h" className={s.listTitle} data-r style={order(0)}>
        <span className={s.listTitleFirst}>Harvested,</span> not yet burnt
      </h2>
      <div className={s.listBody}>
        <div className={s.listSide} data-r style={order(2)}>
          <figure className={s.listPhoto}>
            <Pic
              src={PHOTOS.seederTractor.src}
              width={PHOTOS.seederTractor.width}
              height={PHOTOS.seederTractor.height}
              alt="A tractor with a seeder sowing wheat into rice stubble in Punjab, seen through leaves."
              className={s.cover}
              sizes="(max-width: 760px) 100vw, 30vw"
              failText="This photograph did not load."
              frame="lp-box-portrait"
              local
            />
            <figcaption className={s.caption}>Sown without burning: a seeder in rice stubble, Punjab. Photograph: CIAT.</figcaption>
          </figure>
          <Suspense fallback={null}>
            <Caveat />
          </Suspense>
        </div>
        <div className={s.listTable} data-r style={order(1)}>
          <Suspense fallback={<TableSkeleton />}>
            <RiskTable />
          </Suspense>
          <p className={s.listNote}>
            A seeder can sow wheat into standing stubble. We rank the fields that are cut but not burnt, so the machines reach them before the match does.
          </p>
        </div>
      </div>
    </Reveal>
  );
}

async function RiskTable() {
  const r = await getRisk();
  if (!r.ok) return <p className={s.honest}>{`The ranking did not load. ${r.error.message}`}</p>;
  const rows = (r.data.ranking ?? []).slice(0, ROWS);
  if (!rows.length) return <p className={s.honest}>{r.data.reason ?? 'No harvested, unburnt fields are ranked yet.'}</p>;
  return (
    <table className={s.table}>
      <caption className={s.tableCaption}>
        {`Villages in the ranking’s order, first at the top${r.data.as_of ? `, ranked ${dateTimeIST(r.data.as_of)} IST` : ''}${r.data.totals?.clusters != null ? ` from ${number(r.data.totals.clusters)} harvested fields` : ''}.`}
      </caption>
      <thead>
        <tr>
          <th scope="col">District</th>
          <th scope="col">Village</th>
          <th scope="col" className={s.tdValue}>
            Cut, not burnt
          </th>
          <th scope="col" className={s.tdValue}>
            Fields
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={`${row.district}|${row.village}`} data-group={i > 0 && i % 4 === 0 ? 'yes' : undefined}>
            <td className={s.tdKind}>{row.district}</td>
            <td className={s.tdPlace} title={row.village}>
              {`${row.village_inferred ? 'near ' : ''}${firstPlace(row.village) ?? row.village}`}
            </td>
            <td className={s.tdValue}>{metricText('area_ha', row.area_ha)}</td>
            <td className={`${s.tdValue} ${s.tdFields}`}>{number(row.clusters)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The method's own caveat, quoted from the API (any sentence that points at a response field is
 *  left out), and how the ordering did on the burns that followed. */
async function Caveat() {
  const r = await getRisk();
  if (!r.ok) return null;
  const note = (r.data.method?.note ?? '')
    .split(/(?<=\.)\s+/)
    .filter((x) => x && !x.includes('`'))
    .join(' ')
    .trim();
  const bt = r.data.backtest;
  if (!note && bt?.headline?.auc == null && bt?.all_pairs?.auc_weighted_mean == null) return null;
  // Lead with the season-wide figure and its interval when the API has one (risk-v3 backtest); one
  // pass pair alone flatters the ranking.
  const all = bt?.all_pairs;
  const ci = all?.auc_ci95;
  const seasonLine =
    all?.auc_weighted_mean != null && ci && ci.length === 2
      ? `Tested on the burns that followed: AUC ${number(all.auc_weighted_mean, { decimals: 2 })} over ${number(all.pairs)} pairs of passes (95% CI ${number(ci[0], { decimals: 2 })}–${number(ci[1], { decimals: 2 })}; 0.5 would be chance)${bt?.headline?.auc != null ? `, ${number(bt.headline.auc, { decimals: 2 })} on the latest pair alone` : ''}.`
      : null;
  return (
    <div className={s.listMethod}>
      {note ? (
        <figure className={s.quote}>
          <blockquote>
            <p>{note}</p>
          </blockquote>
          <figcaption className={s.caption}>From the ranking’s method, as the API states it.</figcaption>
        </figure>
      ) : null}
      {seasonLine ? (
        <p className={s.listBacktest}>{seasonLine}</p>
      ) : bt?.headline?.auc != null ? (
        <p className={s.listBacktest}>
          {`Tested on the burns that followed: AUC ${number(bt.headline.auc, { decimals: 2 })} on the latest pair of passes${bt.all_pairs?.auc_weighted_mean != null ? `, ${number(bt.all_pairs.auc_weighted_mean, { decimals: 2 })} averaged over ${number(bt.all_pairs.pairs)} pairs` : ''}${bt.headline.baselines?.random?.auc != null ? `; ${number(bt.headline.baselines.random.auc, { decimals: 1 })} would be chance` : ''}.`}
        </p>
      ) : null}
    </div>
  );
}

function TableSkeleton() {
  return (
    <div aria-hidden="true" style={{ display: 'grid', gap: 26 }}>
      {Array.from({ length: ROWS }, (_, i) => (
        <Skeleton key={i} height={18} width="100%" />
      ))}
    </div>
  );
}
