/**
 * Self-check of the other scars drawn on a case file's pictures (picture-marks.ts): the short paths
 * land on the outlines, the drawing order and the sentence follow the API's numbers.
 *
 *   node src/components/case/picture-marks.check.ts                       # fixture shapes
 *   node src/components/case/picture-marks.check.ts --case case_1234.json  # also a saved GET /api/scars/{id}
 *
 * Node 22.18+ and 24 run TypeScript directly. Exits 1 when a check fails. Nothing here ships in the
 * app.
 */

type Marks = typeof import('./picture-marks');
type CaseFile = import('./types').CaseFile;
type OtherScars = import('./types').OtherScars;

async function loadMarks(): Promise<Marks> {
  // Let Node resolve the app's '@/' alias and extensionless relative imports to their .ts files.
  const nodeModule = (await import('node:module')) as unknown as {
    registerHooks?: (hooks: { resolve: (spec: string, ctx: unknown, next: (spec: string, ctx: unknown) => unknown) => unknown }) => void;
  };
  const src = new URL('../../', import.meta.url);
  nodeModule.registerHooks?.({
    resolve(spec, ctx, next) {
      if (spec.startsWith('@/')) return next(new URL(`${spec.slice(2)}.ts`, src).href, ctx);
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
  return (await import(new URL('./picture-marks.ts', import.meta.url).href)) as Marks;
}

let failed = 0;
function check(what: string, ok: boolean, detail = ''): void {
  if (!ok) failed++;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}${detail ? `: ${detail}` : ''}`);
}

/** The corners a short path draws, back in absolute units. */
function corners(d: string): [number, number][][] {
  const rings: [number, number][][] = [];
  let ring: [number, number][] = [];
  let x = 0;
  let y = 0;
  for (const [, cmd, args] of d.matchAll(/([MhvlzZ])([^MhvlzZ]*)/g)) {
    const n = args.trim() ? args.trim().split(/[\s,]+|(?=-)/).filter(Boolean).map(Number) : [];
    if (cmd === 'M') [x, y] = n;
    else if (cmd === 'h') x += n[0];
    else if (cmd === 'v') y += n[0];
    else if (cmd === 'l') [x, y] = [x + n[0], y + n[1]];
    if (cmd === 'z' || cmd === 'Z') {
      rings.push(ring);
      ring = [];
    } else ring.push([x, y]);
  }
  return rings;
}

const T0 = '2026-11-17T05:50:52Z';
const T1 = '2026-11-22T05:50:50Z';
const square = (x: number, y: number, s: number) => [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]];

async function main(): Promise<number> {
  const m = await loadMarks();
  const box: [number, number, number, number] = [75, 29, 75.01, 29.01];

  // a pixel-traced outline: axis-aligned runs become h and v, relative to the last corner
  check('square path', m.compactPath({ type: 'Polygon', coordinates: square(75.001, 29.001, 0.002) }, box) === 'M100 900h200v-200h-200z');
  // any outline: every corner within half a unit of where the full-precision path puts it
  const ring = Array.from({ length: 50 }, (_, i) => [75.005 + 0.003 * Math.cos(i / 8), 29.005 + 0.002 * Math.sin(i / 8)]);
  ring.push(ring[0]);
  const back = corners(m.compactPath({ type: 'Polygon', coordinates: [ring] }, box))[0];
  const exact = ring.slice(0, -1).map(([lon, lat]) => [((lon - 75) / 0.01) * m.VIEW, ((29.01 - lat) / 0.01) * m.VIEW]);
  check('curved path keeps every corner', back.length === exact.length, `${back.length} of ${exact.length}`);
  check('curved path within half a unit', back.every(([x, y], i) => Math.abs(x - exact[i][0]) <= 0.5 && Math.abs(y - exact[i][1]) <= 0.5));
  const multi = m.compactPath({ type: 'MultiPolygon', coordinates: [square(75.001, 29.001, 0.001), square(75.005, 29.005, 0.001)] }, box);
  check('multipolygon: one closed ring each', corners(multi).length === 2 && (multi.match(/z/g) ?? []).length === 2, multi);

  const other = (id: number, relation: 'same' | 'earlier' | 'later', area_ha: number, after = T0, before = T1) => ({
    id, relation, burn_after: after, burn_before: before, unseen: true, confidence: 0.9, area_ha,
    outline: { type: 'Polygon' as const, coordinates: square(75.002, 29.002, 0.001) },
  });
  const o: OtherScars = {
    min_confidence: 0.5,
    totals: { same: { scars: 2, area_ha: 3.5 }, earlier: { scars: 2, area_ha: 12.4 }, later: { scars: 1, area_ha: 2 } },
    total: 5, returned: 5, truncated: false,
    scars: [other(1, 'same', 0.72), other(2, 'same', 2.78), other(3, 'earlier', 1.4, '2026-11-12T05:50:48Z', T0), other(4, 'earlier', 11), other(5, 'later', 2)],
  };
  const marks = m.otherMarks(o, box);
  check('earlier drawn first, then same; larger first; later never', marks.map((k) => k.id).join() === '4,3,2,1', marks.map((k) => k.id).join());
  check('title names the case, area and window', marks[3].title === 'Case 1: 0.72 ha, burnt 17–22 Nov', marks[3].title);
  check('older API answer: nothing drawn, no sentence', m.otherMarks(undefined, box).length === 0 && m.othersSentence(undefined, 0, { after: T0, before: T1 }) === null);

  const passes = { after: T0, before: T1 };
  const sentence = (totals: OtherScars['totals'], drawn: number) => m.othersSentence({ min_confidence: 0.5, totals }, drawn, passes);
  const labels = m.othersLabels(passes);
  check('key words carry the case dates', labels.same === 'Other scars, burnt 17–22 Nov' && labels.earlier === 'Other scars, burnt by 17 Nov', JSON.stringify(labels));
  const nov = (d: number) => `${d} Nov`;
  const ha = (v: string) => `${v} ha`;
  const tail = 'all with confidence 0.5 or more, as on the live map.';
  const cases: [string, string | null, string][] = [
    ['both kinds', sentence({ same: { scars: 54, area_ha: 383.68 }, earlier: { scars: 72, area_ha: 303.84 }, later: { scars: 0, area_ha: 0 } }, 126),
      `Also mapped in this picture: 54 other burn scars first seen burnt between ${nov(17)} and ${nov(22)} (${ha('384')}) and 72 that had burnt by ${nov(17)} (${ha('304')}), ${tail}`],
    ['one', sentence({ same: { scars: 1, area_ha: 0.72 } }, 1),
      `Also mapped in this picture: 1 other burn scar first seen burnt between ${nov(17)} and ${nov(22)} (${ha('0.72')}), with confidence 0.5 or more, as on the live map.`],
    ['earlier only', sentence({ same: { scars: 0, area_ha: 0 }, earlier: { scars: 12, area_ha: 49.76 } }, 12),
      `Also mapped in this picture: 12 other burn scars that had burnt by ${nov(17)} (${ha('49.8')}), ${tail}`],
    ['none', sentence({ same: { scars: 0, area_ha: 0 }, earlier: { scars: 0, area_ha: 0 }, later: { scars: 0, area_ha: 0 } }, 0),
      'No other burn scar with confidence 0.5 or more is mapped in this picture.'],
    ['cut and later', sentence({ same: { scars: 500, area_ha: 2000 }, earlier: { scars: 260, area_ha: 900 }, later: { scars: 3, area_ha: 5 } }, 300),
      `Also mapped in this picture: 500 other burn scars first seen burnt between ${nov(17)} and ${nov(22)} (${ha('2,000')}) and 260 that had burnt by ${nov(17)} (${ha('900')}), ${tail} Only the 300 largest are outlined. 3 more, first seen burnt after ${nov(22)}, are not drawn.`],
    ['one later', sentence({ same: { scars: 2, area_ha: 3 }, later: { scars: 1, area_ha: 1 } }, 2),
      `Also mapped in this picture: 2 other burn scars first seen burnt between ${nov(17)} and ${nov(22)} (${ha('3.00')}), ${tail} 1 more, first seen burnt after ${nov(22)}, is not drawn.`],
  ];
  for (const [what, got, want] of cases) check(`sentence, ${what}`, got === want, got ?? 'null');

  const i = process.argv.indexOf('--case');
  if (i > 0 && process.argv[i + 1]) {
    const { readFile } = await import('node:fs/promises');
    const data = JSON.parse(await readFile(process.argv[i + 1], 'utf8')) as CaseFile;
    const bbox = data.imagery?.bbox ?? null;
    const os = data.other_scars;
    const drawn = m.otherMarks(os, bbox);
    const t = os?.totals ?? {};
    const want = (t.same?.scars ?? 0) + (t.earlier?.scars ?? 0);
    check(`case ${data.case.id}: every same and earlier scar drawn`, os?.truncated ? drawn.length < want : drawn.length === want, `${drawn.length} of ${want}`);
    // a scar that reaches in from outside the picture starts off it: a negative first corner
    check(`case ${data.case.id}: no path empty`, drawn.every((k) => /^M-?\d+ -?\d+[hvlz]/.test(k.d) && k.d.endsWith('z')));
    const chars = drawn.reduce((s, k) => s + k.d.length, 0);
    const vertices = (os?.scars ?? []).reduce((s, k) => s + (k.outline ? m.ringsOf(k.outline).reduce((a, r) => a + r.length, 0) : 0), 0);
    console.log(`     ${drawn.length} paths, ${chars} characters for ${vertices} corners; ${m.othersSentence(os, drawn.length, { after: data.case.burn_after, before: data.case.burn_before })}`);
  }
  return failed ? 1 : 0;
}

main().then((code) => process.exit(code));
