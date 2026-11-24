# sentinel-scars build log (resume session 2026-10-31)

## State found
- Files exist: sources/stac.py, models/sentinel.py, analytics/burnscar.py, analytics/harvest.py,
  jobs/sentinel.py, api/routers/scars.py, tests/test_sentinel.py.
- Last full job (2026-10-31 00:08 IST): tile 43REP hotspots, 14 passes, 2780 burn_cluster,
  30524 harvest_cluster. The run was cut off before fire matching and place labels: every
  burn_cluster had unseen = NULL and district = NULL.
- tests/fixtures/sentinel/matched_scar.json was missing (1 test failing).

## Done
- Ran match_fires + label_places on the stored rows: 2780 matched (coverage 2026-09-15..10-30),
  all districts/sub-districts labelled, 2762 with a village within 3 km.
- Re-recorded matched_scar.json from the dev DB (scar 3825). 14/14 tests passed.
- Visual audit (chips: scratchpad/build/sentinel/chips/, tool audit.py + chiplib.py):
  - Random sample a3 (seed 1, rules-1): 6 candidate unseen + 4 FIRMS-confirmed. 9/10 burnt.
    False positive: #2906, a flood-irrigated field (black in SWIR, mean post NBR +0.08).
  - Look-alike stratum (mean post NBR >= 0, 3 clusters): 3/3 false (1 flooded field,
    2 cloud shadows on the 2026-10-30 pass that SCL missed).
  - Edge stratum (post NBR in [-0.05, 0)): 1 burnt, 1 probable (hazy pre pass).
  - Fix: cluster-level filter BurnRule.keeps (mean post NBR < 0). Model s2burn-rules-2. The pixel
    rule is unchanged, so the block memory stays keyed on pixel_version s2burn-rules-1 (no season
    reprocess). The job step apply_cluster_filter migrated stored rows: 253 deleted, 2527 relabelled.
  - Hold-out random sample h1 (seed 7, rules-2): 6 unseen + 4 confirmed, 10/10 burnt.
  - Precision, random samples on rules-2: candidate unseen 11/11 (Wilson 95% 0.74-1.00);
    FIRMS-confirmed 8/8 (0.68-1.00).
  - Spot check, top 2 of the default listing (2026-10-30 pass, 25% cloud): 1 fresh burn,
    1 doubtful (thin cloud shadow, mean post NBR -0.07, confidence 1.0). Known residual risk.
  - Recall against FIRMS (recall4.py): 17 VIIRS nominal/high detections in the processed 43REP
    square between clear passes <= 6 days apart. 6/17 had a scar in the same window
    (0.35, CI 0.17-0.59) and 15/17 in any window. Of 3 rendered misses: 1 outside the tile's
    100 km core (it belongs to 43RFP, which has not been processed), 1 an industrial roof
    (correctly not a crop burn), 1 with no visible burn.
- Router: per-feature `label` ("candidate unseen burn" / "candidate burn, FIRMS-confirmed" /
  "candidate burn (no fire-record verdict yet)"), and `method.label` and `method.audit` (the
  precision and recall with Wilson CIs). In the summary, season.fire_detections now counts only
  the districts that have scars (it was all of Punjab and Haryana) and comes with fire_detections_scope.
- Tests: 17 pass (+3 new: the filter, the Wilson interval/audit, the candidate labels).

## Done (continued, 03:30-04:00 IST)
- Ran the sentinel job for real (budget 25 min, run_sentinel, about 1583 s). No errors. The hotspot
  stage covered 43RFP, 43RDQ, 43REQ, 43RDP, 43SDR, 43RFQ, 43REN, 43SER and 43RFN (43REP had
  nothing new), and the punjab stage at 40 m covered 43RDP and 43REQ. Pending for the next run:
  the rest of the punjab tiles and all of haryana. The filter dropped 11.7k look-alike clusters.
  fire_match: 51,365 clusters matched (coverage 2026-09-15..10-30). Totals: 51,365 burn clusters,
  79,078 ha, 44,670 candidate unseen, 151,565 harvest clusters.
- Cross-tile random audit (f1: 6 from 43RFP; g1: 8 unseen + 2 confirmed from the other tiles).
  - 43RFP 3/6 burnt. False: 16446, a smoke or cloud shadow; 7673, a flooded field (black in SWIR).
    Uncertain: 17724.
  - Other tiles 7/8 burnt. Uncertain: 48980 (hazy pre pass). Confirmed: 1/2, and 24423 is
    uncertain (its pre pass is thick haze).
  - Candidate unseen across all tiles: 21/25 = 0.84 (Wilson 0.65-0.94). FIRMS-confirmed: 9/10
    (0.60-0.98). AUDIT in burnscar.py was updated.
  - Feature check: the false positives have d_b11 >= d_b8a and a large d_b12 with a small dNBR,
    but no single threshold separates them from the true positives with hazy pre passes, so no
    further rule change (it would overfit 30 samples).
- The local PostGIS stopped at 03:59:47 IST: Docker Desktop quit and the container's fast shutdown
  exited with code 0. Restart policy is "no". I restarted Docker Desktop and ran `docker compose -f
  infra/docker-compose.yml up -d`; no data was lost.
- With f1's fire verdicts in, the final precision on random samples is: candidate unseen 20/23 =
  0.87 (Wilson 0.68-0.95); FIRMS-confirmed 10/12 = 0.83 (0.55-0.95). AUDIT was updated.
- Tests: 17 pass. TestClient: /api/scars 200 (137 ms with limit=3; the default limit of 5000
  gives 4.75 MB uncompressed and truncated=true), /api/scars/summary 200, /api/scars/{id} 200, unknown id 404.

## Next
- Next job runs: punjab 40 m for the remaining tiles, then haryana.
- To do if time allows: penalise confidence for partly cloudy post passes (shadows).

## s2burn-rules-3 (haze deferral): tried, not shipped (2026-11-23)
- Idea: on a pass hazy by both the blue-rise flag and a dark-target estimate, a pixel that darkened
  in SWIR without burning keeps its view from before the haze, and the next pass within 6 days
  judges it against that view.
- Pre-registered bar: new-only area >= 10% of old in 2 of 3 replay windows, and new-only precision
  >= max(0.6, old precision - 0.10) in a blind audit.
- Replays from 1 Oct on cached Earth Search reads at 20 m (scripts/burn_replay.py, added in d4c84b2
  and removed by its revert 77a9f09): new-only area was 13.5% (Fazilka 43RDP), 39.6% (Barnala
  43REP) and 30.1% (Tarn Taran 43REQ) of old. Area half met.
- Blind audit of 100 chips from those replays (scripts/burn_chips.py, also added in d4c84b2 and
  removed by 77a9f09; seed 20261123; chips show the passes and the outline only), judged by a
  vision model, three votes per chip, majority verdict.
  Burnt / not burnt / unsure: old 17 / 9 / 14, new-only 8 / 17 / 15, loose-only 3 / 16 / 1.
  New-only precision 8/25 = 0.32 (Wilson 0.17-0.52) against max(0.6, 17/26 - 0.10) = 0.60, and
  23/40 = 0.575 even with every unsure chip counted as burnt. Precision half failed: reverted.
- Examples from the review: a 0.6 ha sliver beside a burnt field; a 0.52 ha patch on a village edge
  with no char. With a 10-day window the kept view can be up to 12 days old (6 + 6), which plausibly
  lets tillage in.
- These are model judgements on replay strata, not comparable with the hand audit in AUDIT. The old
  stratum's 17/26 (14 of 40 unsure) is a reason for a fresh random audit of stored rules-2 clusters.
- Code and scripts: commit d4c84b2, reverted by 77a9f09 (branch models/burnscars).

## s2burn-rules-4h (haze correction): evaluated and blind-audited 2026-11-24
- Why: on smoke-hazy passes burns plain to the eye were left unmarked (case 4723594, Ludhiana,
  43REQ 17 -> 22 Nov: 14 of 24 hotspot blocks hazy). Haze adds path radiance, mostly to the
  visible bands (little to SWIR): a burnt field looks brighter (TBI up) and less charred (BAIS2
  down), so it fails the char test. Its NIR darkening hardly changes: the fitted B8A offset was
  <= 0.009 in 31 of the 32 hazy block-passes below (0.022 on W1's 22 Nov pass).
- Rule: on a pass haze_check calls hazy, every band is corrected to the block's last clear pass
  before any test (burnscar.normalise_haze): per ~5 km cell the median change of unchanged ground
  (SWIR-2, SWIR-1 and NIR stable against the clear reference), clipped at 0 and non-increasing
  with wavelength; cells with < 200 unchanged pixels take the median of fitted cells; < 4 fitted
  cells or an offset > 0.15 leaves the pass raw; a cell with blue haze > 0.08 is left raw. The
  memory keeps the corrected view and all nine bands of the last clear pass (state 11 -> 21 MB per
  block). Clear passes are untouched (they are the reference). Also: a burn seen after the NDVI
  reference (on a hazy pass) is no longer called a harvest on the next clear pass.
- Development (two cached blocks outside every evaluation window, 43RDP r0_c0 and 43REP
  r4096_c1024): a first version took the most common change as haze and chained each correction on
  the previous one; during the harvest the most common change is harvest (NIR down, SWIR up), and
  over 9 consecutive hazy passes it drifted until it erased 99% of 43REP's burn pixels. Theil-Sen
  gains on unchanged pixels ranged 0.54-1.79 between passes, so the rule fits offsets only.
  W4's window touches r0_c0 for its top ~380 m (about 9% of the window).
- Pre-registered bar: scripts/burn_haze_eval.py docstring, committed (91c3f28) before the first
  evaluation run. Command: uv run python scripts/burn_haze_eval.py replay <out> (then summary, chips).
- Six windows, season from 1 Oct through the 22 Nov pass, production resolution, ha:

  | window | res | rules-2 | rules-4h | added | share | removed |
  |---|---|---|---|---|---|---|
  | W1 case 4723594 Ludhiana 43REQ (3 x 3 km) | 20 m | 92.36 | 163.64 | 71.28 | 77.2% | 0.00 |
  | W2 case 4936479 Ratta Theh 43REN | 40 m | 721.44 | 737.60 | 16.32 | 2.3% | 0.16 |
  | W3 case 4939467 Kalwan 43REN | 40 m | 239.36 | 241.28 | 2.88 | 1.2% | 0.96 |
  | W4 Fazilka 43RDP | 20 m | 1467.28 | 1567.00 | 108.12 | 7.4% | 8.40 |
  | W5 Barnala 43REP | 20 m | 2613.68 | 3234.44 | 642.72 | 24.6% | 21.96 |
  | W6 Tarn Taran 43REQ | 20 m | 1610.72 | 1836.32 | 280.16 | 17.4% | 54.56 |

  Bar (a) met in 3 of 6 windows (W1, W5, W6). 32 hazy block-passes: 28 corrected with a non-zero
  offset in at least one cell; 4 left raw because every fitted cell was too hazy (W1 8 Oct and
  23 Oct, W3 12 Nov, W4 2 Nov). So W1 has 4 hazy block-passes but only 2 corrected (2 and 22 Nov).
  22 Nov 43REQ offsets (gain 1): block r4096_c2048 B02 0.043,
  B03 0.041, B04 0.030, B06 0.029, B07 0.025, B08 0.022, B8A 0.022, B11 0.003, B12 0 (16/16 cells);
  block r0_c0 B02 0.037 ... B8A 0.009.
- "Visibly charred" ground of 17 -> 22 Nov, a spectral proxy (raw reflectance, post BAIS2 >= 0.70,
  TBI <= 0.40, dB12 > 0.02, dB8A > 0.04) outside every stored scar: W1 95.04 ha, of which rules-4h
  outlines 67.76 ha (rules-2 replay 0); W2 116.24 ha and W3 56.32 ha, of which rules-4h adds nothing
  (their 22 Nov pass was clear: those misses are 40 m mixed pixels, not haze). The proxy is not a
  judgement by eye: in the blind audit none of the 4 W1 added chips was judged burnt (2 not
  burnt, 2 unsure).
- FIRMS proxy (not a bar): a detection within 375 m in the pass window for 70/597 added pieces
  (11.7%) and 266/2396 rules-2 clusters (11.1%).
- Runtime: against main's rules-2 code on 5 cached blocks, +0.028-0.082 s per block-pass and
  +0.46-0.62 s per block-memory save; with production's 4188 block-passes and 227 blocks (run
  2065, 54.6 min) the run grows to about 62.6 min (77.7 min with 25% more passes); bar (d) met.
- Blind audit (independent judges, 24 Nov; chips 30 added, 20 baseline, 10 positive and 10
  negative controls, seed 20261124, each on the cluster's own passes in the site's colours;
  'unsure' counts as not burnt):

  | stratum | burnt | not burnt | unsure | strict share | decided share |
  |---|---|---|---|---|---|
  | added by rules-4h | 19 | 6 | 5 | 19/30 = 0.63 | 19/25 = 0.76 |
  | baseline (rules-2 clusters) | 14 | 4 | 2 | 14/20 = 0.70 | 14/18 = 0.78 |
  | positive controls (FIRMS-confirmed) | 6 | 0 | 4 | 6/10 | 6/6 |
  | negative controls (unchanged cropland) | 0 | 10 | 0 | 0/10 | 0/10 |

  Bar (b): 0.63 >= max(0.60, 0.70 - 0.10) = 0.60, met with one chip to spare (17/30 would
  fail). Bar (c): 6/6 decided positives burnt and 10/10 negatives not burnt, met on decided chips as registered
  (over all positives 6/10). Nearly every added chip has a hazy post pass. Precision of the added area (0.63 strict) is lower than the 31 Oct audit of rules-2
  candidate unseen burns (15/18 = 0.83): the site therefore states that audit as one of the earlier
  rule (headline audit_current, components/case/audit.ts auditOlderText) until rules-4h has its
  own audit of candidate unseen burns.
- Removed area (rules-2 minus rules-4h; W6 54.56 ha, W5 21.96, W4 8.40) was not audited. The
  verification found real burns in it: W6's largest piece is a 6.2 ha field, jet black on the
  clear 13 Oct pass after the hazy 8 Oct pass. Rules-4h still flags 151 of its 155 px, but the
  de-hazed 8 Oct memory trims edge pixels, the part's mean post NBR moves from -0.002 to +0.002
  and the look-alike filter (mean post NBR < 0) drops the whole field; pieces of 2.0 and 1.6 ha
  (23 -> 28 Oct) also look burnt. Of the rules-2 clusters no rules-4h cluster touches, most were
  flagged on the same pass but fell under the minimum size (W6 58 of 63, 36.1 ha; W5 19 of 22) or
  to the NBR filter (W6 1, W5 2). Not fixed here: the look-alike filter drops a whole part whose
  mean NBR sits at 0.
- Case links (scripts/burn_haze_eval.py links <out>): of 30,043 rules-2 scars in the six windows'
  blocks, 26,078 keep their id and 3,627 are forwarded to a scar of the same pass; 338 (1.1%;
  W6 143 of 3,113) had no successor. _FORWARD_SQL now also forwards to a scar of another pass
  within rearm_days (20) covering >= 50% of the old outline (a burn moved to an earlier, hazy
  pass): 66 more links work (W6 62), 272 (0.9%) answer 404, of which 245 have no rules-4h scar
  overlapping them within 20 days.
- Transition: the API serves the rows of RULE.version and of s2burn-rules-2 (SERVED_VERSIONS)
  until the sentinel job has rewritten every tile (one transaction per tile, so nothing is
  counted twice), so the map never goes blank, provided the API is deployed before the first
  sentinel run of the new rule (an older API serves only s2burn-rules-2, so every rewritten tile
  would vanish from it until the deploy). The headline lists the versions counted (model_versions).
## s2burn-rules-4 (seed and grow): tried, not shipped (2026-11-24)
- Idea: pixels that pass every strict rules-2 test (after the 2x2 opening at 20 m) are seeds. A
  pixel that passes looser tests for the same pass pair (dB8A > 0.04, dB11 > 0.04, dB12 > 0.02,
  BAIS2 >= 0.70, brightness <= 0.40, post NBR < 0; same gap, re-arm, cropland and clear-sky rules)
  joins a burn when it is 4-connected to a seed through such pixels. The 2x2 opening runs again on
  the grown burn at 20 m, and the look-alike filter judges a cluster's seed pixels.
- Pre-registered bar (in the eval script's docstring, committed before the first evaluation
  replay); all four were required: (a) added area >= 10% of rules-2 in 3 of 6 windows; (b) blind
  audit 'added' precision >= max(0.6, rules-2 precision - 0.10); (c) the audit is valid: >= 70% of
  positive controls judged burnt and >= 70% of negative controls not burnt (decided chips); (d) the
  daily sentinel run stays within SENTINEL_BUDGET_MIN = 100 min.
- Development window only (Kaithal 43RFP, 40 m): without the per-pixel NBR test, grown pixels
  pushed seed clusters' mean NBR over 0 and the filter dropped their seeds (1482 px vs 129 under
  rules-2; 67 ha of rules-2 area lost). With the NBR test and the seed-based filter: 2.9 ha lost.
- Replays (scripts/burn_grow_eval.py) of season masks from 1 Oct to the 22 Nov pass, inner
  windows, production stage and resolution, on cached Earth Search reads:
  window                                   res   rules-2 ha  rules-4 ha  added ha (share)  removed ha
  W1 case 4723594 Agwar Gujjran 43REQ      20 m      92.9       184.2      91.2 (98.2%)       0.0
  W2 case 4936479 Ratta Theh 43REN         40 m     721.4       896.0     174.6 (24.2%)       0.0
  W3 case 4939467 Kalwan 43REN             40 m     239.4       336.8      97.4 (40.7%)       0.0
  W4 Fazilka 43RDP                         20 m    1477.4      1679.6     202.8 (13.7%)       0.6
  W5 Barnala 43REP                         20 m    2637.0      3808.2    1176.5 (44.6%)       5.3
  W6 Tarn Taran 43REQ                      20 m    1621.5      2550.2     945.4 (58.3%)      16.8
  Bar (a): met in 6 of 6.
- The recall gain was real. The case windows' visibly charred ground of the 17 -> 22 Nov pair
  outside every stored scar (20 m: BAIS2 >= 0.70, TBI <= 0.40, dB12 > 0.02, dB8A > 0.04, clear on
  both passes): W1 96.0 ha, of which rules-2 replayed outlines 0.0 and rules-4 72.9 (76%); W2
  116.2 ha, 3.6 vs 52.4 (45%); W3 56.3 ha, 1.4 vs 29.5 (52%).
- Runtime: apply_pass on a full 1024 px block (43REP r4096_c1024, 13 passes, 3 repeats) took
  0.026 s more per block-pass with growth (mean; 0.031 s median); an independent re-measurement
  gave 0.028 s (0.038 s median). Job run 2065 (23 Nov, 54.6 min) read 4188 block-passes, so,
  doubled for a slower runner, about 58-60 of the 100 min. Bar (d): met.
- Blind audit of 70 chips (scripts/burn_grow_chips.py, seed 20261123; true colour and SWIR on the
  outline's own pre and post passes), judged blind by a vision model, three votes per chip,
  majority verdict. Strata: 30 added pieces, 20 rules-2 replay clusters, 10 positive controls
  (stored scars with a FIRMS detection within about 1 km) and 10 negative controls (real cluster
  shapes moved onto calm, clear cropland).
  Burnt / not burnt / unsure: added 10 / 10 / 10, rules-2 15 / 3 / 2, positive 5 / 4 / 1,
  negative 0 / 10 / 0.
  Bar (b): added 10/20 = 0.50 (Wilson 0.30-0.70) against max(0.6, 15/18 - 0.10) = 0.733. Failed,
  even at the top of the interval (0.33 with unsure counted as not burnt). Bar (c): positive
  controls 5/9 = 0.56 (0.27-0.81), below 0.70. Failed, so the audit is not valid either. Negative
  controls: 0 of 10 burnt. Not shipped: reverted by ed08024.
- Why it failed (chip review and the replays):
  - Growth leaks into neighbouring straw fields. chip_065, a 2.0 ha piece of a 67.5 ha Barnala
    cluster (2 -> 7 Nov), stays straw-coloured in true colour and SWIR, while the fields around
    it turn black; chip_016 (Fazilka) looks the same. When either pass is hazy, the loose tests
    (NBR < 0 included) do not separate dry straw from char.
  - Growth also rescued seed clusters below the minimum size (13 px at 20 m). 10 of the 30 added
    chips were wholly new clusters, judged 3 / 5 / 2. By size (exploratory, not pre-registered):
    added pieces under 1 ha 1 / 7 / 4, pieces of 1 ha or more 9 / 3 / 6.
  - What the website shows was not in the bar. The scar list keeps confidence >= 0.5
    (scars.WEBSITE_LIST), and the job scores confidence on cluster means that include grown
    pixels, against the strict cut-offs, so grown clusters lose confidence. Recomputed from the
    replay clusters (burnscar.confidence on each cluster's means, size, haze flag and gap), the site
    would lose rules-2 area it shows today: W1 3.0, W2 6.9, W3 7.2, W4 14.7, W5 144.3 (5.9% of
    the 2448.2 ha shown) and W6 38.8 ha. Its gain was smaller than the gain over all clusters
    (W5 +614.0 ha against +1176.5 ha).
  - Growth merges seed clusters (pooling their seed NBR means) and sets burn_day on grown pixels,
    which blocks re-arming for rearm_days. W5 has 7 rules-2 clusters (4.4 ha) and W6 18 (12.1 ha)
    with no rules-4 cluster on the same pass. A later real burn on grown ground could be dated to
    the grown pass; not measured.
- The positive-control definition was weak. A FIRMS detection within about 1 km says little about
  the scar itself in fire-dense districts. 4 of the 5 failed positives are under 1 ha, and two
  (chip_005, chip_064) have hazy pre passes and look like genuine rules-2 false positives. A future
  audit should pre-register stronger positives: a FIRMS pixel inside or within 200 m of the
  outline, at least 2 ha, clear passes.
- For a next candidate (not tried): score area at confidence >= 0.5, as the website shows it;
  keep the minimum cluster size on the seeds before growth; do not grow when either pass is hazy.
- These are model judgements on replay strata, not comparable with the hand audit in AUDIT (the
  rules-2 stratum's 15/18 only happens to equal it).
- Code and scripts: commits 3e41bf6 (rule), 68c52ee and db380b6 (scripts/burn_grow_eval.py,
  scripts/burn_grow_chips.py), 1ea35a2 and d908ab3 (docs), all reverted by ed08024 (branch
  w3/grow). Of 1ea35a2, only the README label of the hand-audit row (rule s2burn-rules-2, 31 Oct
  2026) was kept (d04ff5d).
