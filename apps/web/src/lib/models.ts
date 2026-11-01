/**
 * Model ids as the API sends them, in a reader's words. Safe on the server and in the browser.
 *
 *   modelName('gemini-3.6-flash')          // "Gemini 3.6 Flash"
 *   modelName('gemini-3.5-flash-lite')     // "Gemini 3.5 Flash-Lite"
 *   weatherModelName('best_match')         // "Open-Meteo best-match model"
 */

/** A Gemini model id ("gemini-3.6-flash") as Google writes the name ("Gemini 3.6 Flash"); other ids unchanged. */
export function modelName(id: string | null | undefined): string {
  if (!id) return 'Gemini';
  const m = /^gemini-([\d.]+)-(.+)$/i.exec(id);
  if (!m) return id;
  const rest = m[2]
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join('-');
  return `Gemini ${m[1]} ${rest}`;
}

/** An Open-Meteo weather model id ("best_match", "gfs_seamless") as words, naming where it comes from. */
export function weatherModelName(id: string | null | undefined): string {
  if (!id) return 'Open-Meteo model';
  if (id === 'best_match') return 'Open-Meteo best-match model';
  // the forecast centres' acronyms stay capitals: "gfs_seamless" reads "GFS seamless"
  const words = id.replace(/_/g, ' ').replace(/^(gfs|icon|ecmwf|gem|jma|ukmo|cma)\b/i, (s) => s.toUpperCase());
  return `Open-Meteo ${words} model`;
}
