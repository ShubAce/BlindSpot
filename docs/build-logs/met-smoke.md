# met-smoke build log (resume)
## Done
- Read COMMON + module task + SYNTHESIS build-time updates. Files all present.
## Next
- run tests, run met + smoke jobs, check ensemble + burn_cluster input, endpoints, grep hardcoded data.
- tests 19/19 pass; met job ran (runs 5,6: best_match+gfs_seamless, 0.5deg 21x17 grid), smoke chained (forward 115 + 22 back runs), smoke_validate 138.
- burn_cluster: 2780 rows but unseen IS NULL for all (sentinel match_fires not run yet) -> live runs have 0 scars. Dry run (rolled-back tx) proved the path: 1451 candidate scars -> Amritsar/Jalandhar/Ludhiana exposure from scars, Delhi 0.
- Added test_load_scars_reads_only_candidate_unseen_burn_clusters; load_scars note now counts not-yet-matched scars.
- 20/20 tests pass; smoke rerun (forward 139) now reads 1391 candidate unseen scars live (sentinel matched meanwhile: 2473 unseen / 54 seen).
- Delhi 2026-10-31 IST: verdict mixed, FEI 0.264 (range 0.112-0.264), belt share h24 .45/h48 .23/h72 .16, scars 0, wind from 301 deg 6.5 km/h, BLH 73 m.
## Next
- none for this module; open issue: 98% unseen ratio is for sentinel module to audit.
