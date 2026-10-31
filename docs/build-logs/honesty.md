# Honesty build log (risk ranking + bulletin/investigator wording), started 2026-10-31

Owner: analytics/risk.py, jobs/risk.py (risk part), api/routers/risk.py, models/risk.py,
agent/bulletin.py, agent/prompts.py, agent/verifier.py, agent/runner.py (answer checks only),
api/routers/bulletin.py. Ports 8510-8519. Scratch: %TEMP%/claude/f--forge/<session>/scratchpad/honesty.

- done: read architecture, contract notes, deploy-data/serve/api logs, risk + agent code.
- next: diagnose the saturation on real rows; extract the backtest dataset from the local DB (read only).
- resumed 2026-10-31 (second agent): owned files unchanged so far (git diff empty for them).
- next: diagnose saturation on real rows (local DB read-only + live API), extract backtest features.
- done: offline analysis of the 148-pair backtest dataset (scratch backtest_rows_local.csv, 361,031
  field-pairs, 12 tiles, extracted read-only from the local DB by the first agent). Tile-cluster
  bootstrap, all pairs: v2 AUCw 0.556 [0.491, 0.649], top-10% lift 1.34 [1.24, 1.51]; history alone
  0.484 [0.442, 0.545] (no skill); burnt ha alone 0.570; no variant beats v2 on held-out pairs
  (t1 > 2026-10-24, 47 pairs): dAUC CIs all include 0.
- diagnosis: saturation = timing 1.0 (inside 10 d) + noisy-OR of two exp-saturating counts (typical =
  median, so busy places reach 1.0) + history exp-saturating (50 fires vs typical 6 -> 1.0) =>
  100 x (0.4 + 0.4 + 0.2) = 100. Rounded to 2 dp, dozens tie at 100.0.
- next: pre-registered check of no-history / hyperbolic-mean variants (choose on dev pairs, report held-out).
- done: measured what the risk job pulls from the DB today (byte proxy, local DB, same data as Neon):
  state-boundary union for the history clip 17.8 MB, load_fields 18.9 MB, neighbourhood 6.9 MB,
  backtest 61.4 MB: ~105 MB per run (Neon egress, daily). Target < 10 MB.
- decision (pre-registered before held-out check of the final form): risk-v3 keeps v2's factors and
  weights (no variant beat v2 on held-out pairs, so nothing is "improved" on skill) and only changes
  the scale: each count becomes n / (n + typical) (0.5 at the typical count, never 1.0) and the two
  neighbourhood views are averaged instead of noisy-OR. Held-out dAUC vs v2 [-0.010, +0.017] (same skill).
- plan: scoring and backtest run set-based in SQL temp tables (history points COPY'd in and clipped in
  SQL; rain cells COPY'd in); score formula in SQL mirrors analytics/risk.py score_fields (tested equal);
  backtest metrics per pair in SQL (rank-sum AUC, top-10% hits, calibration bands); only per-pair
  aggregates + the headline pair's rows come back. Python: CIs (field bootstrap for headline,
  tile-cluster bootstrap for all pairs), calibration table, verdict text.
  Route: risk_rank, risk_percentile, burn_probability (+ci95) per field; method states the scale.
- next: implement analytics/risk.py v3 + SQL scorer.
- done: analytics/risk.py: risk-v3 (relative n/(n+typical), mean of the two neighbourhood views),
  score_fields(typ=...), score_sql() (same formula in SQL), BANDS/band_index, auc_ci (stratified
  field bootstrap), tile_bootstrap (tile-cluster bootstrap of AUCw, top-10% lift, band shares).
- next: jobs/risk.py SQL pipeline (rk temp table, history COPY + SQL clip, rain cells COPY, self-check
  of SQL vs score_fields on ~270 rows, store via risk_stage) and SQL backtest (bt temp table).
- done: jobs/risk.py rewritten (risk(), _load_field_table, load_history/fill_history, fill_neighbourhood,
  _fill_rain, _self_check, _score_stats, _insert_run/_apply_stage, store_scores kept for Python-scored
  lists; backtest in SQL: _load_pairs, _pair_metrics, _headline, _all_pairs, verdict). Not run yet.
- next: dry-run on the local DB through the byte proxy with rollback (scratch measure_v3.py), fix bugs.
- done: dry run of the new job on the local DB (rollback, no rain fetch): scoring part pulled 43 KB
  (was 43.6 MB with the boundary clip), 83 s; self-check 273 rows equal; score max 90.19 (1 field at
  the max), top-500 have 178 distinct scores (88.0-90.19), p50 53.7, p90 77.3. Backtest identical to
  the offline analysis: headline 43REP AUC 0.701 [0.664, 0.736]; all 148 pairs AUCw 0.560 [0.491,
  0.663] (tile bootstrap), top-10% lift 1.42 [1.24, 1.73] (21.6% vs 15.2%); report JSON 76 KB.
- done: router (risk_rank, risk_rank_of, risk_percentile, risk_band, burn_probability(+_ci95); risk_score
  2 dp; method.scale/fields; note states strength with numbers; backtest summary without windows list,
  read with `backtest - 'windows'`), models/risk.py latest_backtest_summary.
- done: tests/test_honesty_risk.py (10 tests) + updated test_imagery_risk (relative scale, mean,
  SQL timing-branch backtest). 54 passed with compaction tests.
- next: full suite, then the risk job on Neon through the byte proxy (egress + time).
- full suite: 297 passed, 1 failed (tests/test_agent.py::test_a_silent_stream_hits_our_deadline_and_falls_back:
  "model-b: daily budget used (reserve kept)", runner budget area, not mine; also fails alone).
- blocked: running the risk job on Neon (production write) was refused by the session's permission
  classifier. Not worked around. The lead must run it (command in notes). Proxy connectivity to Neon
  (read-only SELECT) worked: latest Neon run id 3, risk-v2, 2026-10-31 00:49Z.
- next: bulletin/investigator wording (prompts.py, bulletin.py check + fallback, runner answer check).
- done: read bulletin.py, prompts.py, verifier.py, runner.py investigate(); today's local bulletin (run
  55c79279) has the four defects (unseen share as fact, no precision/CI, "risk scores of 100.0",
  "232 crop-residue fires"). get_burn_scars (tools.py, not mine) drops method.audit, so the bulletin
  adds the audit from /api/scars/summary itself. Gemini status 06:56Z: flash-lite 35-40/500, 3.8-flash 9/20,
  3.6-flash 11/20.
- next: verifier.honesty (facts walk, check per sentence/unit, fallback sentences en/hi/pa), wire into
  bulletin (retry once, then deterministic fallback) and runner (revision pass, then fallback).
- done: verifier.py honesty section (CANDIDATE_WORDS/RISK_WORDS per language, honesty_facts walk,
  check_honesty per sentence/line, unseen_sentence/risk_sentence in en/hi/pa (scripts checked),
  honest_fallback keeping line breaks and bullets); prompts.py rules (investigator, bulletin,
  translation glossary), honesty_turn, revision_turn(honesty=...); bulletin.py add_unseen_audit,
  honest(), publish(hf=...) (re-check after the number scrub), English retry once, translate retry
  on honesty too, translate_stored reads facts from the stored run's tools; runner.py investigate():
  honesty issues join the one revision pass, then honest_fallback, verification.honesty.
- next: tests with recorded Gemini replies; then regenerate today's bulletin locally (+ Neon if allowed).
- done: tests/test_honesty_bulletin.py (10 tests; recorded fixture tests/fixtures/agent/bulletin_2026-10-31.json =
  today's real en/hi/pa texts + fact blocks + refs + the /api/scars/summary audit recorded from the live API).
  All pass: check flags the live bulletin for all 3 reasons; fallback in en/hi/pa passes script + number
  checks; write_bulletin asks English twice then rewrites 3 sentences; investigator: one revision, then rewrite.
- next: full suite; regenerate today's bulletin on the local dev DB (BULLETIN_FORCE=1, --no-chain).
- done: full suite 307 passed, 1 failed (test_foundation::test_fetch_records_a_redacted_source_fetch_row: live
  Open-Meteo 503 "service is overloaded"; not mine).
- done: today's bulletin regenerated on the LOCAL dev DB (BULLETIN_FORCE=1, --no-chain, 165 s, 07:10Z):
  en gemini-3.6-flash, hi gemini-3.5-flash-lite, pa gemini-3.6-flash; all verified, script_ok, honesty passed
  on the first draft (no retry, no fallback). Gemini requests: 7 (3 failed 503/504 on 3.8/3.6-flash, 4 ok).
  :8000 (older code) serves en/hi/pa 200 with the new rows. English used the village-level AUC 0.79 of the
  latest pair (local backtest is still risk-v2: no all-pairs CI/verdict); prompts tightened to use the
  all-pairs AUC and the method note's word (takes effect on the next run).
- not done: Neon bulletin (production write; the Neon risk-job run was refused by the permission
  classifier, and only 3 of the 10 allowed Gemini calls are left, too few for en+hi+pa with 503 retries).
- next: contract notes (risk + bulletin sections), final suite, report.
- done: router notes `method.scores_model` + a transitional `scale` while stored scores are older
  than risk-v3 (checked live on :8513 against the dev DB: rank 1 of 172,130, percentile 99.999,
  band "top 1%", burn_probability null because the dev backtest is still v2). Servers on 8511-8513 stopped.
- done: docs/api-contract-notes.md risk + bulletin sections updated.
- done: full suite 309 passed (93 s).
- open for the lead: run the risk job on Neon (refused here), deploy API, then regenerate the Neon
  bulletin for 2026-10-31 (BULLETIN_FORCE=1 ... run bulletin --no-chain); tools.get_burn_scars should
  pass method.audit.candidate_unseen so the investigator can quote precision (now it must omit shares).

## Repair (reviewer findings, 2026-10-31)

- next: fix check_honesty false positives (unseen cue words, exclude audit numbers, count nouns after
  risk numbers, "a-b%" CI ranges), AUC cherry-pick check, investigator audit + keep candidate sentence,
  burn_probability basis, crop-fire phrasings + hi/pa, verdict caveat; then Neon bulletin.
- done: verifier honesty check rewritten (unseen figure needs a cue word or an exact-decimal repeat; audit
  numbers excluded; count nouns after risk numbers; "a-b%" ranges; AUC check: auc_not_all_pairs /
  auc_without_interval; crop-fire phrasings + hi/pa rule restricted to FIRMS counts; fallback keeps
  district figures with "(candidate unseen burns)" + one audit sentence per unit; auc_sentence;
  firms_sentence hi/pa). runner: _with_unseen_audit adds the /api/scars/summary audit to get_burn_scars
  before the check (bulletin shares runner.unseen_audit). prompts: AUC + fire-detection glossary.
  Probes (scratchpad honesty/repair): reviewer's 7 benign sentences all pass; live en/hi/pa bulletins
  flagged and repaired with no issues left; local AUC 0.79 flagged in en/hi/pa; 96afd348 keeps its scar
  bullet + audit; d125f375 keeps Karnal/Kaithal with the label.
- next: risk: tested-order cuts in the job, burn_probability by the tested ordering, method note, verdict
  caveat + per-tile AUC; then tests.
- done: risk: analytics tested_score/tested_score_sql/tested_band; job _tested_order (cuts of the tested
  ordering per band, Spearman vs risk_score, top-1% overlap) stored as backtest.tested_order with run_id;
  all_pairs.auc_by_tile; verdict says the latest pair's CI is too narrow + where its tile ranks.
  Route: burn_probability (+ new burn_probability_band) from the tested ordering, null unless the
  backtest's tested_order is the scoring run's; risk_band = today's ordering; method note names the
  tested part, Spearman/overlap, "timing is untested". Tests: test_honesty_risk 16 passed,
  test_honesty_bulletin 18 passed (8 new).
- next: contract notes, full suite, then Neon (risk job, bulletin regeneration/recheck).
- done: bulletin.recheck_stored(day, write) + `python -m blindspot.agent.bulletin recheck DAY [--dry-run]`:
  honesty rule on stored rows from the English run's tools (+ audit), numbers re-verified, no Gemini;
  test added (19 in test_honesty_bulletin). Contract notes updated. Full suite 332 passed (before recheck).
- next: dry-run recheck on local DB (no write: repair rules forbid rewriting local rows), then Neon:
  read-only look, recheck --dry-run, then write; then the risk job on Neon.
- done: recheck dry-runs. Local (2026-10-31): en/hi/pa each 1 sentence (the village AUC 0.79 -> all-pairs
  AUC 0.56 over 148 pairs), all verified/script_ok/honesty passed; not written (repair rules: no row
  rewrites on the shared local DB). Neon dry-run: audit added; en 3, hi 2, pa 3 sentences rewritten,
  all verified, script_ok, honesty passed, 0 removed.
- blocked: the Neon write (recheck without --dry-run) was refused by the permission classifier
  ("Modify Shared Resources"). Not worked around. The risk job on Neon is the same kind of write: not tried.
- next: route check on the dev DB through TestClient (read-only), final suite, report.
- done: route on the dev DB (TestClient, read-only): method.scores_model now also for legacy rows
  (dev has no risk_run: shows risk-v2); burn_probability null (no v3 backtest/tested_order there).
- done: final full suite 333 passed (83 s). :8000 and the live API untouched (no local or Neon writes).
- open for the lead (refused here): Neon bulletin recheck, then the risk job on Neon, commands in the report.
