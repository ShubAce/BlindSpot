# geostationary build log

## Done (2026-10-31 ~03:35 IST)
- sources/gk2a.py: listing, HTTP Range reads via http.fetch, calibration, navigation, npz cache, FIRMS keyless reference. Added load_cached_scan(); 2 ruff fixes.
- analytics/geofire.py: contextual fire test, FRP floor, validation and diurnal helpers. Fixed the pixel-area text (~12 km2, not 17).
- models/geofires.py: geo_fire_slot, geo_fire_pixel (with footprint polygon), geo_fire_validation.
- jobs/geostationary.py: `geostationary` runs every 30 min (last 3 h of scans, then re-validates the last 24 h). `geostationary_backfill` runs daily at 04:20 IST over the last 3 days and prunes cache files older than 8 days. Counts are clipped to the SOI state polygons (boundary.props iso3166_2 IN-PB/IN-HR).
- api/routers/geostationary.py: GET /api/geostationary/diurnal?days=1..7 and GET /api/geostationary/latest?min_confidence=&slot=.
- tests/test_geostationary.py: 8 tests on real fixtures, all passing.
- Ran for real: the backfill processed 430 scans (0 failed, 405 from cache, 59 MB downloaded) and stored 4276 fire pixels and 21 validation passes. The live job also ran OK.
- Validation (gk2a-ctx-2):
  - VIIRS overall: hit rate 2.3% (n=829), false-alarm rate 82% (n_geo=65).
  - Aqua afternoon: hit rate 36% (n=166), 84% for strong fires (n=19), false-alarm rate 7% (n_geo=58).
  - Night: 0 of 27.

## Next
- Nothing is blocking. Possible work: tune the midday daylight tests; run the jobs on a schedule.
