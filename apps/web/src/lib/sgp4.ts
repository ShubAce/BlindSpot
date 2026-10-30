/**
 * The pure-JavaScript SGP4 path of satellite.js 7, imported file by file.
 *
 * Why not `from 'satellite.js'`: the package entry also re-exports its WASM bulk propagator, whose
 * emscripten builds contain `await import("node:module")` and `new Worker(new URL(...))`. A browser
 * bundle would have to resolve those, and Turbopack reports Node built-ins in client code as
 * "Module not found". We use only the four plain modules below; none of them reaches the WASM code.
 * (The package `exports` map hides these paths from bare specifiers, so they are imported by
 * relative path into node_modules, which bundlers and TypeScript both resolve.)
 *
 * Everything else in the app imports orbital maths from lib/orbits.ts, never from here directly.
 */

export { twoline2satrec, json2satrec } from '../../node_modules/satellite.js/dist/io.js';
export { propagate } from '../../node_modules/satellite.js/dist/propagation/propagate.js';
export { gstime } from '../../node_modules/satellite.js/dist/propagation/gstime.js';
export { eciToGeodetic, degreesLat, degreesLong } from '../../node_modules/satellite.js/dist/transforms.js';
export type { SatRec } from '../../node_modules/satellite.js/dist/propagation/SatRec.js';
