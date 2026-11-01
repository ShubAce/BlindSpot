import type { ReactNode } from 'react';

import type { SourceRef } from '@/lib/api';
import { dateTimeIST, MISSING } from '@/lib/format';

import { shortUrl } from './audit';

/**
 * Pieces the case file shares with the field brief: a KPI tile and one input of a provenance list.
 * Server Components with no state, so either screen renders them as they are.
 */

/** A figure with its label, its unit and a subline; a missing value is an em dash, with no unit. */
export function Kpi({ label, value, unit, sub }: { label: string; value: string; unit?: string; sub?: ReactNode }) {
  return (
    <div className="kpi">
      <span className="kpi__label">{label}</span>
      <span className={`kpi__value nums${value === MISSING ? ' missing' : ''}`}>
        {value}
        {unit && value !== MISSING ? <span className="kpi__unit"> {unit}</span> : null}
      </span>
      {sub ? <span className="kpi__sub nums">{sub}</span> : null}
    </div>
  );
}

/** One input of a provenance list: its name (each screen words it), when it was fetched, and a short
 *  link to it. */
export function ProvItem({ source: s, label }: { source: SourceRef; label: string }) {
  return (
    <li className="prov__item">
      <span className="prov__label">{label}</span>
      <span className="prov__meta nums">
        {s.fetched_at ? `fetched ${dateTimeIST(s.fetched_at, { suffix: true })}` : 'fetch time not recorded'}
        {s.fetch_id != null ? ` · fetch ${s.fetch_id}` : ''}
      </span>
      {s.url ? (
        <a className="prov__url" href={s.url} rel="noreferrer noopener" target="_blank" title={s.url}>
          {shortUrl(s.url)}
        </a>
      ) : null}
    </li>
  );
}
