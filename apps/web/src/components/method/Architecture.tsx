import type { SourceRow } from './types';

/**
 * How the parts connect, drawn from docs/architecture.md sections 2 and 6: each upstream source feeds
 * one scheduled job, every job writes to PostGIS, the API reads only the database, and the website
 * and the investigator read only the API. The names and cadences are the architecture's own; the
 * small square beside each source is live, its status from /api/sources.
 *
 * Two drawings of the same diagram: left to right on wide screens, top to bottom on a phone, so the
 * labels stay at a readable size instead of scaling down with the width.
 */

type Lane = { source: string; keys: string[]; job: string; every: string };

const LANES: Lane[] = [
  // cadences: the cron slots in .github/workflows/data-jobs.yml
  { source: 'NASA FIRMS', keys: ['firms'], job: 'firms_nrt', every: 'hourly, 3-hourly at night' },
  { source: 'CelesTrak', keys: ['celestrak'], job: 'tle', every: 'daily, 16:47 IST' },
  { source: 'OpenAQ, CPCB', keys: ['openaq', 'datagov_aqi'], job: 'air', every: 'hourly, 3-hourly at night' },
  { source: 'Open-Meteo', keys: ['openmeteo'], job: 'met, then smoke', every: 'every 6 h' },
  { source: 'Sentinel-2 STAC', keys: ['stac_e84', 'stac_pc'], job: 'sentinel, then risk', every: 'daily, 16:47 IST' },
  { source: 'Gemini', keys: ['gemini'], job: 'bulletin', every: 'daily, 07:30 IST' },
];

const STATUS_FILL: Record<string, string> = { ok: 'var(--positive-mark)', stale: 'var(--warning-mark)', error: 'var(--negative-mark)', never: 'var(--muted)' };

function statusOf(lane: Lane, sources: SourceRow[] | null): SourceRow['status'] | null {
  if (!sources) return null;
  const rows = sources.filter((s) => lane.keys.includes(s.key));
  if (!rows.length) return null;
  const order: SourceRow['status'][] = ['error', 'stale', 'never', 'ok'];
  return order.find((o) => rows.some((r) => r.status === o)) ?? null;
}

const Arrow = ({ d, m }: { d: string; m: string }) => <path d={d} fill="none" stroke="var(--line)" strokeWidth={1.5} markerEnd={`url(#${m})`} />;

function Node({ x, y, w, h, title, sub, strong = false }: { x: number; y: number; w: number; h: number; title: string; sub?: string; strong?: boolean }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={2} fill={strong ? 'var(--accent)' : 'var(--surface)'} />
      <text x={x + 14} y={y + (sub ? h / 2 - 3 : h / 2 + 5)} fontSize={15} fontWeight={600} fill={strong ? 'var(--accent-ink)' : 'var(--ink)'}>
        {title}
      </text>
      {sub ? (
        <text x={x + 14} y={y + h / 2 + 15} fontSize={12} fill={strong ? 'var(--accent-ink)' : 'var(--muted)'}>
          {sub}
        </text>
      ) : null}
    </g>
  );
}

function Defs({ id }: { id: string }) {
  return (
    <defs>
      <marker id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0 L10 5 L0 10 Z" fill="var(--line)" />
      </marker>
    </defs>
  );
}

export function Architecture({ sources }: { sources: SourceRow[] | null }) {
  const rowH = 64;
  const top = 40;
  const H = top + LANES.length * rowH + 8;
  const midY = top + (LANES.length * rowH) / 2;
  const label = 'Diagram: six upstream sources feed six scheduled jobs; the jobs write to PostGIS; the FastAPI service reads only the database; the website and the Gemini investigator read only the API.';

  return (
    <figure className="arch">
      {/* wide: left to right */}
      <svg className="arch__wide" viewBox={`0 0 1200 ${H}`} role="img" aria-label={label}>
        <Defs id="arch-a-w" />
        {['Sources', 'Scheduled jobs', 'Database', 'API', 'Readers'].map((t, i) => (
          <text key={t} x={[0, 270, 600, 800, 1010][i]} y={18} fontSize={13} fill="var(--muted)">
            {t}
          </text>
        ))}
        {LANES.map((l, i) => {
          const y = top + i * rowH;
          const st = statusOf(l, sources);
          return (
            <g key={l.source}>
              <Node x={0} y={y} w={210} h={48} title={l.source} />
              {st ? <rect x={190} y={y + 18} width={10} height={10} fill={STATUS_FILL[st]}><title>{`status: ${st}`}</title></rect> : null}
              <Arrow m="arch-a-w" d={`M212 ${y + 24} H264`} />
              <Node x={270} y={y} w={250} h={48} title={l.job} sub={l.every} />
              <Arrow m="arch-a-w" d={`M522 ${y + 24} C560 ${y + 24}, 560 ${midY}, 596 ${midY}`} />
            </g>
          );
        })}
        <Node x={600} y={midY - 60} w={150} h={120} title="PostGIS" sub="one table per module" strong />
        <Arrow m="arch-a-w" d={`M752 ${midY} H796`} />
        <Node x={800} y={midY - 60} w={160} h={120} title="FastAPI /api" sub="reads the database only" />
        <Arrow m="arch-a-w" d={`M962 ${midY - 20} C985 ${midY - 20}, 985 ${midY - 90}, 1006 ${midY - 90}`} />
        <Arrow m="arch-a-w" d={`M962 ${midY + 20} C985 ${midY + 20}, 985 ${midY + 90}, 1006 ${midY + 90}`} />
        <Node x={1010} y={midY - 120} w={190} h={60} title="Website" sub="map, cases, bulletin" />
        <Node x={1010} y={midY + 60} w={190} h={60} title="Investigator" sub="Gemini, cited, checked" />
      </svg>

      {/* phone: top to bottom */}
      <svg className="arch__tall" viewBox="0 0 400 940" role="img" aria-label={label}>
        <Defs id="arch-a-t" />
        <text x={0} y={16} fontSize={13} fill="var(--muted)">
          Sources and the jobs that read them
        </text>
        {LANES.map((l, i) => {
          const y = 30 + i * 70;
          const st = statusOf(l, sources);
          return (
            <g key={l.source}>
              <Node x={0} y={y} w={180} h={56} title={l.source} />
              {st ? <rect x={162} y={y + 23} width={10} height={10} fill={STATUS_FILL[st]} /> : null}
              <Arrow m="arch-a-t" d={`M182 ${y + 28} H200`} />
              <Node x={204} y={y} w={196} h={56} title={l.job} sub={l.every} />
            </g>
          );
        })}
        <Arrow m="arch-a-t" d="M302 450 V486" />
        <Node x={60} y={490} w={280} h={80} title="PostGIS" sub="one table per module" strong />
        <Arrow m="arch-a-t" d="M200 572 V608" />
        <Node x={60} y={612} w={280} h={80} title="FastAPI /api" sub="reads the database only" />
        <Arrow m="arch-a-t" d="M150 694 C150 730, 100 730, 100 760" />
        <Arrow m="arch-a-t" d="M250 694 C250 730, 300 730, 300 760" />
        <Node x={0} y={764} w={190} h={80} title="Website" sub="map, cases, bulletin" />
        <Node x={210} y={764} w={190} h={80} title="Investigator" sub="Gemini, checked" />
        <text x={0} y={880} fontSize={12} fill="var(--muted)">
          Square beside a source: its live status.
        </text>
      </svg>
      <figcaption className="arch__key">
        <span>
          <i style={{ background: 'var(--positive-mark)' }} /> answering
        </span>
        <span>
          <i style={{ background: 'var(--warning-mark)' }} /> late
        </span>
        <span>
          <i style={{ background: 'var(--negative-mark)' }} /> failing
        </span>
        <span>The square beside each source is its status now, from the API.</span>
      </figcaption>
    </figure>
  );
}
