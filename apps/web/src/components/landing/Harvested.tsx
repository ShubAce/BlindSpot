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
        <figure className={s.listPhoto} data-r style={order(2)}>
          <Pic
            src={PHOTOS.seederTractor.src}
            width={PHOTOS.seederTractor.width}
            height={PHOTOS.seederTractor.height}
            alt="A tractor with a seeder sowing wheat into rice stubble in Punjab, seen through leaves."
            className={s.cover}
            sizes="(max-width: 760px) 60vw, 22vw"
            failText="This photograph did not load."
            frame="lp-box-portrait"
            local
          />
          <figcaption className={s.caption}>Sown without burning: a seeder in rice stubble, Punjab. Photograph: CIAT.</figcaption>
        </figure>
        <p className={s.listWord} aria-hidden="true" data-r style={order(1)}>
          Villages
        </p>
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
  const bt = r.data.backtest;
  return (
    <>
      <table className={s.table}>
        <caption className="sr-only">Villages with the most harvested, unburnt field area weighted by burn risk, highest first</caption>
        <thead className="sr-only">
          <tr>
            <th scope="col">District</th>
            <th scope="col">Village</th>
            <th scope="col">Unburnt area and risk score</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={`${row.district}|${row.village}`} data-group={i > 0 && i % 4 === 0 ? 'yes' : undefined}>
              <td className={s.tdKind}>{row.district}</td>
              <td className={s.tdPlace} title={row.village}>
                {`${row.village_inferred ? 'near ' : ''}${firstPlace(row.village) ?? row.village}`}
              </td>
              <td className={s.tdValue}>
                {metricText('area_ha', row.area_ha)}
                <span className={s.tdScore}>{` · ${number(row.clusters)} fields · score ${number(row.risk_score)}`}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={s.listMethod}>
        {r.data.as_of ? `Ranked ${dateTimeIST(r.data.as_of)} IST from ${number(r.data.totals?.clusters)} harvested fields. ` : ''}
        {r.data.method?.note ? `${r.data.method.note.replace(/\s*See `backtest`.*$/, '')} ` : ''}
        {bt?.headline?.auc != null
          ? `Tested on the burns that followed: AUC ${number(bt.headline.auc, { decimals: 2 })} on the latest pair of passes${bt.all_pairs?.auc_weighted_mean != null ? `, ${number(bt.all_pairs.auc_weighted_mean, { decimals: 2 })} averaged over ${number(bt.all_pairs.pairs)} pairs` : ''}${bt.headline.baselines?.random?.auc != null ? ` (${number(bt.headline.baselines.random.auc, { decimals: 1 })} would be chance)` : ''}.`
          : ''}
      </p>
    </>
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
