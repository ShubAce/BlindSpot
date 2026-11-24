/**
 * Self-check of the audit phrase (audit.ts): an audit of an earlier rule than the one that mapped the
 * scars (/api/stats/headline unseen_burns.audit_current false) is never stated as the current rule's.
 *
 *   node src/components/case/audit.check.ts
 *
 * Node 22.18+ and 24 run TypeScript directly. Exits 1 when a check fails. Nothing here ships in the
 * app.
 */

type Audit = typeof import('./audit');

async function loadAudit(): Promise<Audit> {
  // Let Node resolve the app's '@/' alias to its .ts files.
  const nodeModule = (await import('node:module')) as unknown as {
    registerHooks?: (hooks: { resolve: (spec: string, ctx: unknown, next: (spec: string, ctx: unknown) => unknown) => unknown }) => void;
  };
  const src = new URL('../../', import.meta.url);
  nodeModule.registerHooks?.({
    resolve(spec, ctx, next) {
      if (spec.startsWith('@/')) return next(new URL(`${spec.slice(2)}.ts`, src).href, ctx);
      return next(spec, ctx);
    },
  });
  return (await import(new URL('./audit.ts', import.meta.url).href)) as Audit;
}

let failed = 0;
function check(what: string, ok: boolean, detail = ''): void {
  if (!ok) failed++;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}${detail ? `: ${detail}` : ''}`);
}

async function main(): Promise<number> {
  const { auditPhrase, auditOlderText, auditIsOlder } = await loadAudit();
  // the shape /api/stats/headline serves (numbers as in the 31 Oct audit, for the check only)
  const base: NonNullable<Parameters<Audit['auditPhrase']>[0]> = { available: true, precision: 0.833, precision_ci95: [0.608, 0.942], precision_n: 18 };
  const older = { ...base, audit_date: '2026-10-31', audit_model_version: 's2burn-rules-2', audit_current: false };
  const current = { ...base, audit_date: '2026-10-31', audit_model_version: 's2burn-rules-2', audit_current: true };

  // spaces as plain spaces: dates and units are joined with no-break spaces
  const p = (auditPhrase(older, 'was') ?? '').replace(/[\u00a0\u202f]/g, ' ');
  check('an older audit is flagged', auditIsOlder(older) && !auditIsOlder(current) && !auditIsOlder(base));
  check('the phrase names the earlier rule and its day', p.includes('of the earlier rule s2burn-rules-2, not of the current rule') && p.includes('31 Oct 2026'), p);
  check('the qualification sits inside the brackets', /^audit precision was 83% \(95% CI 61%–94%, n = 18; an audit of 31 Oct 2026 of the earlier rule s2burn-rules-2, not of the current rule\)$/.test(p), p);
  check('a current audit reads as before', auditPhrase(current) === 'audit precision 83% (95% CI 61%–94%, n = 18)', auditPhrase(current) ?? 'null');
  check('a block without the flag reads as before', auditPhrase(base) === auditPhrase(current) && auditOlderText(base) === null);
  check('no audit, no phrase', auditPhrase({ available: true, precision: null }) === null);
  return failed ? 1 : 0;
}

main().then((code) => process.exit(code));
