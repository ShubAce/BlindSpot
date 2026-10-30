# Review: boundaries + air (skeptical reviewer)

Run on 2026-10-30, 21:45 to 22:05 UTC, against the dev DB `blindspot` (jobs, endpoints) and
`blindspot_test` (tests and empty-database behaviour). Scratch scripts and logs are in
`%TEMP%/claude/f--forge/.../scratchpad/build/review_ba/`.

## Verdict

Both modules are solid and honest. I found and fixed one real data-quality defect in **air**:
hourly means built from 1 or 2 of their 4 readings were served as if they were complete. I also
corrected one inaccurate code comment in **boundaries**. **boundaries** had no functional defects.

## What was checked, and the result

| Check | boundaries | air |
| --- | --- | --- |
| Module tests | 17 pass | 33 pass (32 + 1 new); with smoke and foundation, 82 pass |
| One real job run | `boundaries` ok in 172.9 s (LGD 4 fetches, 3 release files 304 not-modified, Overpass 4 states, GeoNames, Wikidata) | `air` ok in 149.4 s; second run ok after the fix; `air_dss` ok in 1.9 s |
| Provenance | every call is a source_fetch row (boundaries 3, lgd 4, census2011 1, osm 4, geonames 1, wikidata 1 via track_fetch) | openaq 159 + 197 (repair), datagov_aqi 5, cams 1-3, iitm_dss 1 |
| Keys never stored | 0 source_fetch rows contain a raw key (url, error and meta scanned against all 4 configured keys) | 0; logs show `api-key=***` |
| No dummy data (grep) | clean; only cited constants (DST guideline, LGD/ISO codes, Hansi gazette 682-ARIC-03-2025/7392) | clean; NAQI breakpoints and windows (CPCB CUPS/82/2014-15 Table 3.11, checked against the published bands), IUGG Earth radius |
| Empty-DB behaviour (`blindspot_test`) | `/cities` count 0 + `reason`; `/boundaries` count 0 + note; `/lookup` all null; sources are null-valued refs | `/latest` zero counts + `reason`; `/series` 404 "cities with data: none yet"; `/stubble-share` `reason` |
| Model never mixed with observed | n/a | verified in code and live: `pm25_now`/`pm25_24h_mean` come from OpenAQ only; CAMS sits under `receptors[].model` and `is_model` stations; CPCB sub-indices appear only under `aqi`, never as µg/m³ (`station.pm25` is null for datagov-only stations) |

### Spec verifications (boundaries)
- `district_for_point(75.84, 30.25)` -> Punjab / **Sangrur** / tehsil Sangrur. `nearest_place` -> Sangrur
  (OSM town, 0.85 km). A Lahore point (74.34, 31.55) -> None (outside India, as required by the
  "clip to India's polygons" rule).
- All 5,555 FIRMS detections with no district fall outside every SOI Indian state polygon (0 inside
  India): Pakistan's Punjab is correctly left unlabelled.
- Every CREAMS `official_fire_event.district` matches a boundary name (case-insensitive): 0 mismatches.
- Receptor cities (22) and air-quality city names agree: all 17 AOI cities resolve to the same
  spelling in `air_obs` (Delhi 39 stations, Noida/Gurugram/Ghaziabad 4, Faridabad/Chandigarh 3, the
  Punjab cities 1 each). Jaipur, Agra, Jodhpur, Gwalior and Saharanpur are region-tier smoke receptors
  outside the AOI box with no station data, so `/api/air/series` returns 404 for them by design.
  Meerut has data.gov.in AQI only.

### Independent accuracy check (air)
OpenAQ hourly PM2.5, as a 24-h mean per station (at least 18 h), against CPCB's own 24-h PM2.5
sub-index from data.gov.in inverted to µg/m³: **80 station pairs, Pearson r = 0.960**, median
difference -2.8 µg/m³, 49/80 within 5 and 65/80 within 10. The small low bias is OpenAQ's lag: our
window ends about 2 h before CPCB's, during hours when Delhi's PM was rising at night.

## Defects fixed

1. **air: partial hours served as hourly means** (`blindspot/sources/openaq.py`,
   `blindspot/api/routers/air.py`). OpenAQ's `/measurements/hourly` averages whatever readings an
   hour has and reports `coverage.percentComplete`. The parser ignored it, so an hour built from a
   single 15-minute reading (25 %) was stored and served as that hour's mean. Upstream this is 12.2 %
   of hours (1,157 of 9,519 over the last 8 days, 98 stations). It is also how the newest hour
   arrives while its readings are still coming in (the live run had a mean for the hour ending
   18:30Z, but the station's newest reading was at 18:00Z).
   - `parse_hourly` now skips hours under 75 % complete (`MIN_HOURLY_PERCENT_COMPLETE`), the same
     75 % data-capture rule the module already applies to 24-h means. The job's existing re-read
     logic picks the hour up once it is complete (it re-reads from `last - 2h` as soon as a reading
     at or after `last + 1h` exists).
   - `_SERIES_SQL` and `_DAILY_MEAN_SQL` used stored 15-minute readings for **any** hour without an
     hourly mean, which would have brought back the hours just dropped. They now use 15-minute
     readings only for hours after the station's newest hourly mean (hours OpenAQ has not averaged
     yet). An older hour without a mean stays a gap.
   - Dev DB repaired: I re-read each live station's last 8 days through `OpenAQClient` (logged) and
     deleted the 1,074 stored hourly rows (of 8,010) that OpenAQ reports as under 75 % complete.
     Visible effects: Bathinda's `pm25_24h_mean` is now null (15 valid hours, under the 18 needed)
     instead of 17.5 µg/m³ from partial hours. Delhi `pm25_last` is now the 18:00Z reading, not a
     partial 18:30Z mean.
   - Tests: `test_parse_hourly_reads_ist_aligned_hours` now asserts the partial hours are dropped;
     new `test_a_dropped_partial_hour_is_not_refilled_by_a_stray_reading`.
2. **boundaries: inaccurate comment** (`blindspot/jobs/boundaries.py`, `_DISTRICT_ALIASES`). It
   claimed aliases like Gurugram <- Gurgaon and Nuh <- Mewat. The census-village file already uses
   today's names, so the live aliases are only Ferozepur <- Firozpur and Charkhi Dadri <-
   Charki Dadri. The comment now says so, so nobody claims Gurgaon/Mewat matching to judges.

## Endpoint results (TestClient, dev DB)

Geo (all have `generated_at` and real `sources` with fetch ids; `Cache-Control: max-age=3600`):

| Call | Status | Time (cold/warm) | Size | Notes |
| --- | --- | --- | --- | --- |
| `/api/geo/boundaries` (district, AOI) | 200 | 672 / 283 ms | 1,338 KB raw, **370 KB gzip** | 96 features (whole polygons touching the AOI); target was under 1.5 MB gzip |
| `?level=state` | 200 | 676 / 196 ms | 239 KB gzip | 9 |
| `?level=subdistrict` | 200 | 755 / 262 ms | 305 KB gzip | 195 |
| `?state=Punjab` / `IN-HR` / `3` | 200 | under 170 ms | about 85 KB gzip | 23 each |
| `?simplify=0` | 200 | 1.6 s | 1,720 KB gzip | opt-in full detail |
| `/api/geo/districts` | 200 | 14 ms | | 58 |
| `/api/geo/cities` | 200 | 12 ms | | 22 |
| `/api/geo/lookup` | 200 | 15-22 ms | | Sangrur / New Delhi / Hansi (derived) / Pakistan -> null |
| `/api/geo/places?q=Bhuch` | 200 | 40 ms | | Bhucho Mandi (town), Bhucho Kalan (village, pop 7,854) |
| `/api/geo/tiles/{z}/{x}/{y}.pbf` | 200 / 204 | 21-194 ms | 10-140 KB | 404 off-grid |
| Bad input | 422 level, 400 bbox, 422 lat, 422 q, 404 tile | | | |

Air:

| Call | Status | Time | Notes |
| --- | --- | --- | --- |
| `/api/air/latest` | 200 | 92 ms, 130 KB | 98 OpenAQ + 36 CPCB-feed-only stations, 50 CAMS points, 52 cities |
| `/api/air/series?city=Delhi&hours=72` | 200 | 18 ms | 72 points, 65 with data, max 39 stations; 72 model points |
| `?city=ludhiana` / `New Delhi` (aliases) | 200 | 11 ms | resolved to `Ludhiana` / `Delhi` |
| `?ahead_hours=48` | 200 | 10 ms | 48 forecast points under `model_forecast` |
| `?city=Atlantis` / `hours=0` | 404 / 422 | | 404 lists the cities with data |
| `/api/air/stubble-share` | 200 | 8 ms | run 2026-10-30, `stale=true` (it is 03:30 IST on the 31st; DSS updates at 10:20 IST) |
| `/latest?at=2026-10-30T12:00:00Z` | 200 | 54 ms | replay: Delhi 44.3 µg/m³ from 38 fresh stations |

Trimmed real `receptors[]` entry (Delhi, 21:50Z; OpenAQ upstream has nothing newer than 18:00Z,
which I confirmed by comparing OpenAQ's raw `/measurements` with our stored max):
```json
{"city":"Delhi","state":"Delhi","pm25_now":null,"pm25_24h_mean":34.9,"observed_at":null,
 "provider":"openaq","n_stations":0,"n_stations_total":39,"n_stale":39,"pm25_24h_hours":21,
 "pm25_last":{"value":72.0,"observed_at":"2026-10-30T18:00:00Z","n_stations":39,"age_minutes":236,"stale":true},
 "aqi":{"value":92,"category":"Satisfactory","n_stations":55,"observed_at":"2026-10-30T20:30:00Z","provider":"datagov_aqi"},
 "model":{"pm25":61.2,"pm10":129.9,"valid_at":"2026-10-30T21:00:00Z","provider":"cams"}}
```

## Dev DB row counts after the review
boundary 412 (40 states, 177 districts incl. derived Hansi, 195 tehsils) · place 20,790 (census
villages 19,883, OSM villages 607, OSM towns 278, cities 22) · air_obs 39,950 (openaq 7,499,
datagov_aqi 2,499, cams 29,952) · dss_snapshot 1.

The "place 32,867" in the review brief was not a real row count. Every boundaries run (5 in
job_run, before and after this review) wrote exactly 20,790 places, and the table's insert/delete
counters net to 20,790 live rows. The 32,867 was most likely a pre-vacuum `n_live_tup` estimate. No
data was lost.

## Notes for the frontend / integration
- When `pm25_now` is null (OpenAQ republishes CPCB data 2-4 h late, worst at night), show
  `pm25_last.value` with its `age_minutes` and a "stale" badge. Never substitute `model.pm25` or
  `aqi`. They are different quantities (a model, and a 24-h NAQI index).
- `stations[].id` is a string with a provider prefix (`openaq:50`, `cpcb:<name>`, `cams:<city>`).
  CPCB-feed-only stations have `pm25`/`pm10` null and carry `aqi` only.
- Contract additions beyond architecture §5. Air: `as_of`, `fresh_minutes`, `counts`, `reason`;
  stations get `pm25_observed_at`, `pm10_observed_at`, `age_minutes`, `stale` and `aqi`; receptors
  get `state`, `pm10_now`, `n_stations_pm10`, `n_stations_total`, `n_stale`, `pm25_24h_hours`,
  `pm25_last`, `last_observed_at`, `lat`, `lon`, `aqi` and `model`. Series adds `hours`, `unit`,
  `as_of`, `provider`, `model_provider`, `hours_with_data`, `max_stations`, optional
  `model_forecast` (with `ahead_hours`) and the optional `at` parameter. `GET /api/air/stubble-share`
  is new. Geo routes are all additions: `/boundaries`, `/districts`, `/cities`, `/lookup`, `/places`
  and `/tiles`.
- Series `t` is the end of an IST clock hour, so it falls at :30 UTC. Model points fall on the hour (UTC).
- `/api/geo/boundaries` features are whole polygons touching the bbox, not clipped. Draw state lines
  from `level=state` (Survey of India) or from the tiles' `states` layer, never from the basemap.

## Open issues (not fixed; outside these modules or by design)
- OpenAQ's lag means `pm25_now` is null for several hours each night. That is honest, but the
  headline should fall back to `pm25_last`, clearly aged (integration).
- data.gov.in AQI is fresher (CPCB's 20:30Z batch) but holds 24-h rolling sub-indices, not hourly
  concentrations, so it cannot fill `pm25_now`.
- Only two district aliases exist. Old names such as Gurgaon or Mewat in user questions will not
  match unless the agent normalises them.
- Ruff reports style findings across both modules (FURB167 `re.S` aliases, one BLE001 in
  `label_everything` that is deliberate, PLR0402), and `ruff format` would rewrap to 88 columns,
  while the codebase is written to about 100 and pyproject sets no `line-length`. I left both alone
  to avoid churn during integration.
- The CAMS SourceRef URL is about 2 KB (52 coordinate pairs). It is correct, but the UI should not
  print it in full.
