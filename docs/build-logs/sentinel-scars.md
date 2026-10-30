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
