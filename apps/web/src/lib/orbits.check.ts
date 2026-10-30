/**
 * Self-check: do the browser's pass predictions (lib/orbits.ts) agree with the API's
 * (/api/orbits/passes, skyfield on the server)? Both start from the same CelesTrak rows.
 *
 *   node src/lib/orbits.check.ts                      # API at NEXT_PUBLIC_API_URL or http://localhost:8000
 *   node src/lib/orbits.check.ts --api http://127.0.0.1:8000 --lat 30.25 --lon 75.84 --hours 48
 *
 * Node 22.18+ and 24 run TypeScript directly. Exits 1 when a pass is missing on either side or a time
 * of closest approach differs by more than 10 s (the research found skyfield and satellite.js agree
 * to about 1 s). Nothing here ships in the app; it only reads the live API.
 */

type OrbitsModule = typeof import('./orbits');
type TleResponse = import('./api').OrbitsTleResponse;
type PassesResponse = import('./api').PassesResponse;

function arg(name: string, fallback: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

async function loadOrbits(): Promise<OrbitsModule> {
  // Let Node resolve the app's extensionless relative imports ('./sgp4') to their .ts files.
  const nodeModule = (await import('node:module')) as unknown as {
    registerHooks?: (hooks: {
      resolve: (spec: string, ctx: unknown, next: (spec: string, ctx: unknown) => unknown) => unknown;
    }) => void;
  };
  nodeModule.registerHooks?.({
    resolve(spec, ctx, next) {
      if ((spec.startsWith('./') || spec.startsWith('../')) && !/\.[cm]?[jt]s$/.test(spec)) {
        try {
          return next(`${spec}.ts`, ctx);
        } catch {
          return next(spec, ctx);
        }
      }
      return next(spec, ctx);
    },
  });
  const url = new URL('./orbits.ts', import.meta.url).href;
  return (await import(url)) as OrbitsModule;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`${res.status} from ${url}`);
  return (await res.json()) as T;
}

const ist = (d: Date) => new Date(d.getTime() + 5.5 * 3600_000).toISOString().slice(5, 19).replace('T', ' ') + ' IST';

async function main(): Promise<number> {
  const api = arg('api', process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/+$/, '');
  const site = { lat: Number(arg('lat', '30.25')), lon: Number(arg('lon', '75.84')) };
  const hours = Number(arg('hours', '48'));
  const orbits = await loadOrbits();

  const tle = await getJson<TleResponse>(`${api}/api/orbits/tle`);
  const sats = orbits.parseTles(tle);
  console.log(`elements: ${sats.length} satellites (${sats.map((s) => `${s.name} ${s.instrument} ±${s.halfSwathKm} km`).join(', ')})`);
  if (!sats.length) {
    console.log('FAIL: /api/orbits/tle returned no usable elements');
    return 1;
  }

  const from = new Date();
  const mine = orbits.predictPasses(site, sats, from, hours);
  const server = await getJson<PassesResponse>(`${api}/api/orbits/passes?lat=${site.lat}&lon=${site.lon}&hours_back=0&hours_ahead=${hours}`);
  const theirs = server.passes.map((p) => ({ ...p, tca: new Date(p.peak_utc), start: new Date(p.start_utc), end: new Date(p.end_utc) }));

  let failures = 0;
  const used = new Set<number>();
  console.log(`\nsite ${site.lat}, ${site.lon}; ${mine.length} client passes, ${theirs.length} API passes in ${hours} h\n`);
  for (const p of mine) {
    let best = -1;
    let bestDt = Infinity;
    theirs.forEach((q, i) => {
      if (used.has(i) || q.satellite !== p.satName) return;
      const dt = Math.abs(q.tca.getTime() - p.tca.getTime());
      if (dt < bestDt) {
        best = i;
        bestDt = dt;
      }
    });
    if (best < 0 || bestDt > 10 * 60_000) {
      // Passes that start right at the window edges can legitimately fall on one side only.
      const edge = p.tca.getTime() - from.getTime() < 15 * 60_000 || from.getTime() + hours * 3600_000 - p.tca.getTime() < 15 * 60_000;
      console.log(`${edge ? 'edge' : 'MISS'}  ${p.satName.padEnd(12)} ${ist(p.tca)}  ${p.crossTrackKm.toFixed(0)} km: not in the API's list`);
      if (!edge) failures++;
      continue;
    }
    used.add(best);
    const q = theirs[best];
    const dTca = (p.tca.getTime() - q.tca.getTime()) / 1000;
    const dStart = (p.start.getTime() - q.start.getTime()) / 1000;
    const dEnd = (p.end.getTime() - q.end.getTime()) / 1000;
    const ok = Math.abs(dTca) <= 10;
    if (!ok) failures++;
    console.log(
      `${ok ? 'ok  ' : 'FAIL'}  ${p.satName.padEnd(12)} ${ist(p.tca)}  TCA ${dTca >= 0 ? '+' : ''}${dTca.toFixed(1)} s  start ${dStart.toFixed(0)} s  end ${dEnd.toFixed(0)} s  ` +
        `distance ${p.crossTrackKm.toFixed(0)} vs ${q.min_ground_distance_km.toFixed(0)} km`,
    );
  }
  theirs.forEach((q, i) => {
    if (used.has(i)) return;
    const edge = q.tca.getTime() - from.getTime() < 15 * 60_000 || from.getTime() + hours * 3600_000 - q.tca.getTime() < 15 * 60_000;
    console.log(`${edge ? 'edge' : 'MISS'}  ${q.satellite.padEnd(12)} ${ist(q.tca)}  only in the API's list`);
    if (!edge) failures++;
  });

  const live = orbits.liveStatus(site, tle, from);
  console.log(`\nlive status now: watching [${live.watching.map((w) => `${w.name} until ${ist(w.until)}`).join(', ')}]; next ${live.next ? `${live.next.name} at ${ist(live.next.at)}` : 'none'}`);
  console.log(`API current:     watching [${server.current.watching.map((w) => `${w.satellite} until ${ist(new Date(w.until_utc))}`).join(', ')}]; next ${server.current.next ? `${server.current.next.satellite} at ${ist(new Date(server.current.next.at_utc))}` : 'none'}`);
  console.log(failures ? `\nFAIL: ${failures} disagreement(s)` : '\nPASS: client and API agree');
  return failures ? 1 : 0;
}

main().then(
  (code) => process.exit(code),
  (e: unknown) => {
    console.error(`could not run the check: ${e instanceof Error ? e.message : String(e)}`);
    process.exit(2);
  },
);

export {};
