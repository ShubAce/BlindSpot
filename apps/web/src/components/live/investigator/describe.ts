/**
 * The investigator's steps in plain words. Each tool call (services/api blindspot/agent/tools.py)
 * becomes one sentence built only from the arguments the model actually sent: a missing argument is
 * left out, never filled with the tool's default. The exact call is shown beside it as code.
 */

import { dateTimeIST } from '@/lib/format';

type Args = Record<string, unknown>;

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : typeof v === 'number' && Number.isFinite(v) ? String(v) : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
const title = (s: string) => s.replace(/\b([a-z])/g, (m) => m.toUpperCase());

const REGION: Record<string, string> = { punjab: 'Punjab', haryana: 'Haryana', all: 'Punjab and Haryana' };

/** "last 24 h", "last 3 days" */
function lastHours(h: number | null): string | null {
  if (h == null) return null;
  return h % 24 === 0 && h >= 48 ? `last ${h / 24} days` : `last ${h} h`;
}

/** The agent's map layer names (tools.py MAP_LAYERS) as this screen's toggle names. */
export const LAYER_WORDS: Record<string, string> = {
  fires: 'fire detections',
  scars: 'burn scars',
  unseen_scars: 'candidate unseen burns',
  risk: 'harvested, unburnt fields',
  smoke_forward: 'the smoke forecast',
  smoke_back: 'air reaching the city',
  air_stations: 'air stations',
  wind: 'wind',
  satellite_passes: 'satellite passes',
  imagery: 'Sentinel-2 imagery',
};

const FEATURE_WORDS: Record<string, string> = {
  scar: 'burn scar',
  risk_cluster: 'risk cluster',
  fire: 'fire detection',
  station: 'air station',
  district: 'district',
  city: 'city',
  village: 'village',
};

/** One sentence for a tool call. */
export function describeCall(name: string, args: Args): string {
  const a = args ?? {};
  switch (name) {
    case 'get_fire_activity': {
      const region = str(a.region);
      const where = region ? REGION[region.toLowerCase()] ?? title(region) : null;
      return ['Checked fire detections', where ? `in ${where}` : null].filter(Boolean).join(' ') + (lastHours(num(a.hours)) ? `, ${lastHours(num(a.hours))}` : '');
    }
    case 'get_satellite_passes': {
      const place = str(a.place);
      const lon = num(a.lon);
      const lat = num(a.lat);
      const where = place ?? (lon != null && lat != null ? `${lat.toFixed(2)}° N, ${lon.toFixed(2)}° E` : null);
      const back = num(a.hours_back);
      const ahead = num(a.hours_ahead);
      const span = [back != null ? `${back} h back` : null, ahead != null ? `${ahead} h ahead` : null].filter(Boolean).join(' and ');
      return `Looked up fire-satellite passes${where ? ` over ${where}` : ''}${span ? `, ${span}` : ''}`;
    }
    case 'get_burn_scars': {
      const district = str(a.district);
      const since = str(a.since);
      const until = str(a.until);
      const what = a.unseen_only === true ? 'candidate unseen burns' : 'Sentinel-2 burn scars';
      const parts = [`Searched ${what}`, district ? `in ${title(district)}` : null, since ? `since ${since}` : null, until ? `until ${until}` : null];
      const limit = num(a.limit);
      return parts.filter(Boolean).join(' ') + (limit != null ? `, largest ${limit}` : '');
    }
    case 'get_case_file': {
      const id = str(a.scar_id);
      return id ? `Opened the case file of burn scar ${id}` : 'Opened a burn scar case file';
    }
    case 'get_air_quality': {
      const city = str(a.city);
      return `Read measured PM2.5${city ? ` in ${city}` : ''}${lastHours(num(a.hours)) ? `, ${lastHours(num(a.hours))}` : ''}`;
    }
    case 'trace_air_back': {
      const city = str(a.city);
      const h = num(a.hours);
      return `Traced ${city ? `${city}'s` : 'the'} air back${h != null ? ` ${h} h` : ''} over the burn belt`;
    }
    case 'forecast_smoke':
      return 'Ran the forward smoke forecast from recent fires';
    case 'get_weather': {
      const city = str(a.city);
      return `Read the wind and mixing height${city ? ` over ${city}` : ''}`;
    }
    case 'get_burn_risk': {
      const district = str(a.district);
      return `Ranked harvested, unburnt fields by burn risk${district ? ` in ${title(district)}` : ''}`;
    }
    case 'get_field_brief': {
      // the brief PPCB and HSPCB teams get at /brief/<district>: where to look first, and why
      const district = str(a.district);
      const days = num(a.days);
      const span = days != null ? `, ${days === 1 ? 'the newest day' : `the newest ${days} days`} of Sentinel-2 passes` : '';
      return `Field brief: villages for field teams in ${district ? title(district) : 'a district'}${span}`;
    }
    case 'show_on_map':
      return describeMap(str(a.action) ?? '', a);
    default:
      // a tool added to the API after this screen: its own name, never a guess at what it did
      return `Called ${name.replace(/_/g, ' ')}`;
  }
}

/** "Showed fire detections on the map", "Flew the map to Sangrur", "Highlighted burn scar 42626". */
export function describeMap(action: string, p: Args): string {
  const label = str(p.label) ?? str(p.place);
  if (action === 'show_layer') {
    const layer = str(p.layer);
    const city = str(p.city) ?? str((p.params as Args | undefined)?.city);
    const words = layer ? LAYER_WORDS[layer] ?? layer.replace(/_/g, ' ') : 'a layer';
    if (layer === 'smoke_back' && city) return `Showed the air reaching ${city} on the map`;
    return `Showed ${words} on the map`;
  }
  if (action === 'fly_to') return `Moved the map to ${label ?? 'the place it named'}`;
  if (action === 'highlight') {
    const kind = str(p.feature_type);
    const id = str(p.feature_id) ?? str(p.id);
    const words = kind ? FEATURE_WORDS[kind] ?? kind : 'a feature';
    if (id) return `Highlighted ${words} ${id} on the map`;
    return `Highlighted ${label ?? `the ${words}`} on the map`;
  }
  return 'Used the map';
}

/** The exact call, as code: `get_fire_activity(hours=24, region="punjab")`. */
export function callCode(name: string, args: Args): string {
  const inner = Object.entries(args ?? {})
    .map(([k, v]) => `${k}=${typeof v === 'string' ? JSON.stringify(v) : JSON.stringify(v)}`)
    .join(', ');
  return `${name}(${inner})`;
}

/**
 * A thought summary from Gemini is "**Title**\n\nBody...". The step list shows the title only,
 * with the body one click away. A thought without a title shows its first sentence.
 */
export function thoughtTitle(text: string): string {
  const m = /\*\*(.+?)\*\*/.exec(text);
  if (m) return m[1].trim();
  const first = text.replace(/\s+/g, ' ').trim().split(/(?<=[.!?])\s/)[0] ?? '';
  return first.length > 90 ? `${first.slice(0, 88)}…` : first;
}

export function thoughtBody(text: string): string {
  return text.replace(/\*\*(.+?)\*\*/, '').replace(/\*\*/g, '').trim();
}

/** Normalised question for matching a typed question to a verified example. */
export const normaliseQuestion = (q: string) => q.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayMonth = (y: string, m: string, d: string) => `${Number(d)} ${MONTHS[Number(m) - 1] ?? m}${y === String(new Date().getUTCFullYear()) ? '' : ` ${y}`}`;
const group = (n: string) => {
  const [i, f] = n.split('.');
  return Number(i).toLocaleString('en-IN') + (f ? `.${f}` : '');
};

/**
 * A tool's one-line result (written by the API for logs) in the screen's words. Only the notation
 * changes, never a value: a Python dict of hour windows `{'h24': 43.5, 'h48': 22.0}` becomes
 * "24 h 43.5, 48 h 22.0", ISO dates read "31 Oct", long bare figures get digit grouping (ids
 * after "Case", "scar" or "#" are left alone), and a range of percentages takes an en dash.
 */
export function tidySummary(text: string): string {
  return text
    .replace(/\{([^{}]*)\}/g, (all, inner: string) => {
      const pairs = [...inner.matchAll(/'h(\d+)':\s*(-?[\d.]+|None)/g)];
      if (!pairs.length) return all;
      return pairs.map(([, h, v]) => `${h} h ${v === 'None' ? '—' : v}`).join(', ');
    })
    // A UTC stamp is shown in IST; a bare or IST one keeps its clock time.
    .replace(/\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|\+00:00)?)?/g, (all, y: string, m: string, d: string, t?: string, utc?: string) => {
      if (t && utc) {
        const at = new Date(all.replace(' ', 'T').replace('+00:00', 'Z'));
        if (!Number.isNaN(at.getTime())) return dateTimeIST(at, { suffix: true });
      }
      return `${dayMonth(y, m, d)}${t ? `, ${t}` : ''}`;
    })
    .replace(/(^|[^\w#.,])(\d{5,}(?:\.\d+)?)(?![\d,])/g, (all, pre: string, n: string, offset: number, whole: string) =>
      /(case|scar|id)\s*$/i.test(whole.slice(Math.max(0, offset - 6), offset + pre.length)) ? all : `${pre}${group(n)}`,
    )
    // a range of percentages takes an en dash: "95% CI 67.9–95.5%"
    .replace(/(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)%/g, '$1–$2%');
}
