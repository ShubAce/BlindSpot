# imagery-risk build log (resumed 2026-10-31)
## done
- 35 tests pass (tests/test_imagery_risk.py). Full suite: 195 pass, 1 fail in test_sentinel.py (missing fixture tests/fixtures/sentinel/matched_scar.json; sentinel module's).
- risk job runs for real: 30524 harvest_cluster rows scored (avg 71.9), ~30-36 s.
- FIX: precision_at_k broke ties by list order (burnt listed first) -> inflated integer-count baselines (fires_7d p@50 0.94 at AUC 0.49). Now tie-averaged expected precision; test added.
- MODEL risk-v2: v1 neighbourhood (FIRMS counts) had AUC ~0.5 (weighted mean 0.479 over 13 pass pairs). v2 adds Sentinel-2 burnt ha within 5 km/7 d via noisy-OR -> weighted AUC 0.662; headline pair 28->30 Oct AUC 0.682 (225 burnt / 991 unburnt, base 0.185, p@100 0.302); village-level AUC 0.803, p@10 villages 1.0. New baseline burned_ha_7d_only (0.684). History factor ~0.51 alone.
- risk_factors.neighbourhood.unseen_burns_7d -> candidate_unseen_burns_7d (SYNTHESIS wording); METHOD text updated.
- /api/risk empty reason distinguishes filters vs not mapped.
- featured: _choose now prefers passes <5% window cloud and spreads frames over time. Rebuilt: 15, 20, 25, 28 Oct over "Near Lasara Pohlewas, Ludhiana" (181.6 ha burnt, 117 scars). All 8 frames 200 image/jpeg, viewed: clear, fields sharp, burn scars visible in SWIR. (Before: 30 Oct 9%-cloud frame with cloud+shadow.)
- render/tiles/tilejson/scenes verified incl. validation (bbox outside AOI 400, w>2048 422, bad collection/kind 422, missing item 404 JSON).
## next
- nothing blocking. Known data issue (boundaries module): census2011 village names truncated at 40 chars (e.g. "Chamar Tibbi, Moranwali, Model Town ( R").
