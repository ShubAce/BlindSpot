# BlindSpot API: contract notes for the frontend

The final, exact shape of every endpoint as served on 2026-10-31 (IST) by `services/api` on live
data, with a trimmed real sample, units, null cases and refresh cadence. This file is what the
frontend builds from. Where it differs from `docs/architecture.md` section 5, this file wins.

Measured on the dev machine with uvicorn on port 8000. "p50" is the median of 10 calls (warm,
in-process cache) and "cold" is the first call. "Wire" is the gzip size.

## Conventions (all endpoints)

- **Envelope.** JSON bodies are flat: the payload keys sit at the top level, next to
  `generated_at` (UTC ISO with `Z`) and `sources: SourceRef[]`. There is no `data` wrapper.
  `SourceRef = {key, label, url|null, fetched_at|null, fetch_id|null}`. Keys in source URLs
  are masked as `***`. Some SourceRef URLs are about 2 KB long (CAMS): truncate them in the UI.
- **Errors** are JSON with one shape: `{detail, status, path, generated_at, sources: []}`.
  - 400: bad bbox or bad dates.
  - 404: unknown id or city. The body lists the valid values when there is a short list.
  - 422: validation. `detail` is a list of pydantic errors.
  - 429: agent rate limit, with `Retry-After`.
  - 502: upstream failed. 503: upstream paused, with `Retry-After`.
  - 503 with `missing_module`: a library this deployment lacks (imagery rendering on serverless).
  - 503 `database unavailable`.
- **Times.** Every timestamp is UTC ISO-8601 with `Z`. Fields ending `_ist` are IST: either
  `HH:MM` strings or ISO strings with `+05:30`. Daily series use IST days (`day_basis: "IST"`).
- **GeoJSON** coordinates are `[lon, lat]`. `bbox=minLon,minLat,maxLon,maxLat`. The default is
  the AOI `73.8,27.6,77.7,32.6`.
- **India clipping.** Fire counts are clipped to the Survey of India polygons of Punjab, Haryana,
  Delhi and Chandigarh (`clip: "india"`), never to the AOI box, which also covers Pakistan's
  Punjab. Without boundaries, counts are withheld with a `note`; they never fall back to the box.
- **Honest emptiness.** Missing inputs give empty arrays or nulls plus a `reason` or `note`. No
  number is ever invented. Show the reason.
- **Wording rules** (SYNTHESIS, build-time updates):
  - Say "candidate unseen burns", never "unseen burns".
  - GK-2A is "an activity index in pixel-hours", not a fire count.
  - Trajectories are a two-model ensemble with a cone. Never show one path as truth.
  - Show CAMS PM2.5 as "model", never in place of measured PM2.5.
- **Caching.** Responses carry `Cache-Control: public, max-age=N`. The server also keeps hot
  responses in memory for 30-300 s (`blindspot/api/cache.py`), so polling faster than the TTL
  in the table below gains nothing. A cached body keeps the `generated_at` of the moment it was
  built.
- **Freshness.** `GET /api/sources` gives per-source status (`ok|stale|error|never`) with
  `last_success_at`. Use it for the "data as of" badges.

## Latency and size (live, 2026-10-31 05:03 IST)

| Endpoint | p50 ms | cold ms | raw bytes | wire bytes | max-age s |
| --- | --- | --- | --- | --- | --- |
| GET /api/health | 2.3 | 2.9 | 152 | 152 | - |
| GET /api/sources | 5.0 | 6.2 | 12,509 | 3,434 | 15 |
| GET /api/stats/headline | 1.5 | 265 | 10,857 | 3,885 | 60 |
| GET /api/fires | 2.5 | 16.9 | 80,946 | 6,431 | 30 |
| GET /api/fires/daily | 1.5 | 53.6 | 8,450 | 1,653 | 30 |
| GET /api/fires/hours | 1.3 | 33.6 | 4,318 | 1,303 | 30 |
| GET /api/fires/official | 1.2 | 12.8 | 8,114 | 1,434 | 30 |
| GET /api/orbits/tle | 10.0 | 11.3 | 9,905 | 2,469 | 300 |
| GET /api/orbits/passes | 12.7 | 44.3 | 15,361 | 2,805 | 30 |
| GET /api/orbits/clock | 11.7 | 185 | 4,829 | 1,171 | 600 |
| GET /api/orbits/validation | 2.9 | 87.7 | 4,258 | 1,155 | 600 |
| GET /api/scars?limit=500&min_confidence=0.5 | 13.1 | 77.5 | 557,510 | 93,901 | 60 |
| GET /api/scars/summary | 1.3 | 91.3 | 10,388 | 3,071 | 60 |
| GET /api/scars/{id} | 20.9 | 23.3 | 8,460 | 2,895 | 60 |
| GET /api/risk?limit=500 | 11.4 | 850 | 956,324 | 79,541 | 60 |
| GET /api/air/latest | 2.6 | 58.4 | 128,533 | 13,683 | 60 |
| GET /api/air/series?city=Delhi | 1.2 | 12.9 | 8,955 | 1,907 | 120 |
| GET /api/air/stubble-share | 1.1 | 5.2 | 6,926 | 1,828 | 600 |
| GET /api/met/now?city=Delhi | 1.1 | 59.2 | 1,624 | 739 | 120 |
| GET /api/met/wind-field | 1.2 | 7.2 | 4,769 | 1,662 | 120 |
| GET /api/smoke/forward | 31.1 | 436 | 12,444,189 | 2,480,364 | 60 |
| GET /api/smoke/back?city=Delhi | 6.3 | 52.6 | 1,364,849 | 265,296 | 60 |
| GET /api/smoke/receptors | 1.3 | 8.8 | 16,142 | 2,892 | 60 |
| GET /api/smoke/validation | 6.9 | 17.3 | 430,653 | 60,586 | 300 |
| GET /api/imagery/featured | 1.2 | 2.0 | 7,162 | 1,335 | 300 |
| GET /api/imagery/render (disk-cached JPEG) | 1.6 | 2.6 | 274,812 | (jpeg) | immutable |
| GET /api/imagery/scenes | 4.3 | 5.4 | 6,688 | 949 | 300 |
| GET /api/imagery/tilejson/{kind}/{item} | 3.0 | 1,325 | 706 | 706 | 3600 |
| GET /api/imagery/tiles/... (first render about 6 s, then disk) | 1.4 | 3.0 | 97,506 | (png) | immutable |
| GET /api/geo/boundaries | 57.7 | 358 | 1,370,856 | 380,985 | 3600 |
| GET /api/geo/districts | 5.1 | 6.8 | 8,202 | 1,517 | 3600 |
| GET /api/geo/cities | 5.1 | 6.9 | 7,576 | 1,954 | 3600 |
| GET /api/geo/lookup | 8.5 | 14.4 | 1,641 | 733 | 3600 |
| GET /api/geo/places?q= | 10.0 | 12.1 | 895 | 895 | 3600 |
| GET /api/geo/tiles/{z}/{x}/{y}.pbf | 3.7 | 39.7 | 36,139 | 20,786 | 3600 |
| GET /api/geostationary/diurnal | 1.7 | 2.6 | 44,207 | 7,240 | 60 |
| GET /api/geostationary/latest | 1.1 | 5.9 | 753 | 753 | 60 |
| GET /api/bulletin/latest | 1.1 | 5.9 | 5,203 | 1,895 | 300 |
| GET /api/agent/examples | 12.7 | 12.8 | 12,593 | 3,381 | 30 |
| GET /api/agent/runs/{id} | 4.9 | 5.2 | 25,258 | 5,675 | 60 |
| GET /api/agent/status | 36.9 | 38.2 | 1,168 | 442 | 10 |
| POST /api/agent/ask (SSE) | 146 replay | 8,425 live | 25 frames | | - |

Load the heavy payloads lazily: `/api/smoke/forward` is 2.5 MB gzip, `/api/geo/boundaries`
0.38 MB, and `/api/scars` with no filters returns 5,000 features (4.75 MB raw). For the map, pass
`bbox` and/or `min_confidence` and `limit`.

---

## GET /api/stats/headline (new: the landing and `/live` header)

No parameters. Six blocks, each self-contained. A block whose inputs are missing, or whose
computation failed, is `{available: false, reason, ...}` while the others still render.
Cached for 60 s.

```json
{
 "generated_at": "2026-10-30T23:32:57.921050Z",
 "as_of": "2026-10-30T23:32:57.659402Z",
 "fires_24h": {
  "available": true, "count": 232,
  "by_satellite": {"Suomi NPP": 62, "NOAA-20": 127, "NOAA-21": 43, "Aqua": 0, "Terra": 0},
  "by_state": {"Punjab": 232},
  "window_ist": {"from": "2026-10-30T05:02+05:30", "to": "2026-10-31T05:02+05:30"},
  "window": {"from_utc": "2026-10-29T23:32:57.659402Z", "to_utc": "2026-10-30T23:32:57.659402Z"},
  "latest_acq_utc": "2026-10-30T20:14:00Z", "static_excluded": 1, "clip": "india",
  "region": "Punjab, Haryana, Delhi, Chandigarh (Survey of India polygons)",
  "note": "Every VIIRS/MODIS detection counts once per satellite: ..."
 },
 "season": {
  "since": "2026-09-15", "until": "2026-10-31", "day_basis": "IST",
  "firms_detections": 4782, "firms_by_state": {"Punjab": 4285, "Haryana": 469, "Delhi": 28},
  "firms_complete_days": 43, "firms_days": 47, "static_excluded": 469,
  "official_events": 1043, "official_by_state": {"Punjab": 1019, "Haryana": 23, "Delhi": 1},
  "official_source": "CREAMS, ICAR-IARI (rice residue layer)",
  "official_last_layer": "2026-10-30", "official_layers_missing": ["2026-09-17", "2026-10-31"],
  "note": "FIRMS counts every satellite detection (several per fire); CREAMS publishes one event per fire ..."
 },
 "unseen_burns": {
  "available": true, "label": "candidate unseen burns",
  "clusters": 67854, "area_ha": 104476.3, "unseen_clusters": 59465, "unseen_area_ha": 86303.0,
  "checked_area_ha": 104476.3, "unseen_share": 0.8261, "districts": 38,
  "first_pass": "2026-10-02T05:40:36.219000Z", "last_pass": "2026-10-30T05:40:52.298000Z",
  "model_version": "s2burn-rules-2",
  "precision": 0.87, "precision_ci95": [0.679, 0.955], "precision_n": 23,
  "note": "Scars from Sentinel-2 passes processed so far ...; treat the share as an upper bound ..."
 },
 "delhi": {
  "city": "Delhi", "available": true,
  "pm25": null, "unit": "µg/m³", "observed_at": null, "provider": "openaq", "n_stations": 0,
  "stale": true,
  "last": {"value": 66.0, "observed_at": "2026-10-30T21:30:00Z", "n_stations": 39, "age_minutes": 122, "stale": true},
  "pm25_24h_mean": 38.8,
  "aqi": null,
  "basis": "median PM2.5 of the city's stations that reported within the freshness window (OpenAQ republishing CPCB/DPCC monitors)",
  "smoke_trace": {
   "run_at": "2026-10-30T23:27:41.234977Z", "verdict_hint": "mixed",
   "verdict_reason": "the model runs disagree, or the air passed only a little burning",
   "share_over_burn_belt": {"h24": 0.5527, "h48": 0.2814, "h72": 0.1898},
   "fire_exposure_index": 0.332, "fire_exposure_range": [0.08, 0.332]
  }
 },
 "passes": {
  "available": true,
  "site": {"lat": 30.8421, "lon": 75.4159, "name": "Punjab (centroid)", "basis": "centroid of the Punjab state polygon in the boundary table"},
  "last": {"satellite": "Aqua", "instrument": "MODIS", "at_utc": "2026-10-30T23:23:29Z", "start_utc": "2026-10-30T23:20:51Z", "end_utc": "2026-10-30T23:26:07Z", "min_ground_distance_km": 414.1},
  "next": {"satellite": "Terra", "instrument": "MODIS", "at_utc": "2026-10-31T04:09:05Z", "start_utc": "2026-10-31T04:06:25Z", "end_utc": "2026-10-31T04:11:44Z", "min_ground_distance_km": 396.0},
  "watching": [], "blind_for_minutes": 273, "instruments": ["VIIRS", "MODIS"],
  "note": "Fire-detecting sensors only (VIIRS, MODIS). ..."
 },
 "gk2a_evening": {
  "available": true,
  "unit": "GK-2A fire pixels (~12 km2 each) per 10-minute scan inside Punjab and Haryana ...: an activity index, not a number of fires",
  "days": 3, "since": "2026-10-27T23:32:57.885551Z", "scans": 423, "peak_ist": "16:00-17:00",
  "pixel_hours": 712.7, "pixel_hours_outside_polar_windows": 541.2, "share_outside_polar_windows": 0.7594,
  "unobserved_span_ist": {"start": "16:30", "end": "20:41", "minutes": 251, "pixel_hours": 476.0},
  "validation": {"viirs_hit_rate": 0.0229, "viirs_false_alarm_rate": 0.8154, "viirs_n": 829,
   "modis_aqua_day": {"passes": 2, "hit_rate": 0.3614, "n": 166, "false_alarm_rate": 0.069, "hit_rate_strong": 0.8421, "n_strong": 19, "...": "..."}},
  "note": "An activity index in GK-2A pixel-hours, not a fire count: ..."
 },
 "sources": [{"key": "firms", "label": "NASA FIRMS active fires (VIIRS 375 m, MODIS 1 km; NRT)", "url": "https://firms.modaps.eosdis.nasa.gov/api/area/csv/***/MODIS_NRT/...", "fetched_at": "2026-10-30T22:53:03.239924Z", "fetch_id": 4276}, "... firms, creams, stac_e84, openaq, datagov_aqi, cams, celestrak, gk2a"]
}
```

Notes for the UI:

- `fires_24h.count` uses the same rule as `GET /api/fires` (clipped, static sites excluded), so
  the two always agree. The live agent answer quoted the same 232.
- `season`: show `firms_detections` beside `official_events`. They are not the same unit:
  FIRMS counts detections per satellite, CREAMS counts de-duplicated events. Say so (`note`).
  `official_layers_missing` lists IST days CREAMS has not published (today until about noon,
  and 17 Sep, which was never published).
- `unseen_burns`: always show `precision` with `precision_ci95` and `precision_n` next to
  `unseen_share`.
  - `unseen_share` is null when no scar has been checked against FIRMS yet.
  - `first_pass` / `last_pass` are the Sentinel-2 passes the totals cover. They grow as the
    6-hourly sentinel job processes more tiles.
- `delhi`:
  - `pm25` is measured PM2.5 now: the median of stations that reported within 120 minutes.
    It is **null when every station is stale**, which happens for hours at night because OpenAQ
    republishes CPCB data 2-4 h late. Then show `last.value` with `last.age_minutes` and a
    "stale" badge.
  - Never substitute `aqi` (a 24-hour NAQI index from CPCB) or the CAMS model for it.
  - `aqi` is null when the data.gov.in feed is not keyed or not fresh.
  - `smoke_trace` is null until the smoke job has a Delhi back run.
- `passes`:
  - `last` and `next` count the fire sensors only (VIIRS, MODIS).
  - `blind_for_minutes` is the time until the next fire-sensor look starts: 0 while one is
    watching, null when none falls inside 36 h.
  - `watching` is a list (empty most of the time).
  - The pass times are checked against FIRMS acquisition times: median error 0.6 min (see
    `/api/orbits/validation`).
- `gk2a_evening` is null-safe: `available: false` with `reason` before any scan is stored.
  `share_outside_polar_windows` is null when no FIRMS detections fall in the period.
- Cadence: the inputs change every 30 min (fires, air, GK-2A), every 3 h (met, smoke) and every
  6 h (scars). Polling every 60 s is plenty.

---

## Fires: `/api/fires*`

### GET /api/fires

Params:

- `hours` (1..720, default 24).
- `bbox`.
- `min_confidence` (`low|nominal|high`).
- `region` (`all|punjab|haryana|delhi|chandigarh`, lower case; `IN-PB` gives 422).
- `clip` (`india|none`).
- `end` (ISO UTC).
- `limit` (default 20,000; at most 50,000).

```json
{"hours": 24, "count": 232,
 "by_satellite": {"Suomi NPP": 62, "NOAA-20": 127, "NOAA-21": 43, "Aqua": 0, "Terra": 0},
 "window": {"from_utc": "2026-10-29T23:32:57.938300Z", "to_utc": "2026-10-30T23:32:57.938300Z"},
 "latest_acq_utc": "2026-10-30T20:14:00Z", "region": "all", "clip": "india",
 "min_confidence": null, "truncated": false, "note": null,
 "features": {"type": "FeatureCollection", "features": [
  {"type": "Feature", "id": 21104, "geometry": {"type": "Point", "coordinates": [75.507, 31.32]},
   "properties": {"id": 21104, "satellite": "Suomi NPP", "instrument": "VIIRS", "source": "VIIRS_SNPP_NRT",
    "acq_utc": "2026-10-30T20:14:00Z", "confidence": "nominal", "frp_mw": 1.1, "daynight": "N",
    "state": "Punjab", "district": "Jalandhar", "scan_km": 0.4, "track_km": 0.6}}]},
 "generated_at": "...", "sources": ["..."]}
```

- `confidence` is `low|nominal|high`: VIIRS classes, and MODIS mapped from 0-100.
- `frp_mw` is in MW. `scan_km` / `track_km` are the pixel size in km.
- Features are ordered newest first. `truncated` is true when `count` exceeds `limit`.
- Cadence: `firms_nrt` runs every 30 min. The NRT latency is about 3 h after overpass.

### GET /api/fires/daily

Params: `since`, `until` (IST dates; default 15 Sep to today), `region`, `include_static`.

```json
{"region": "all", "since": "2026-09-15", "until": "2026-10-31", "day_basis": "IST",
 "series": [{"date": "2026-09-15", "count": 16, "frp_sum": 68.92,
   "by_satellite": {"Suomi NPP": 4, "NOAA-20": 4, "NOAA-21": 8, "Aqua": 0, "Terra": 0}, "complete": false}],
 "totals": {"count": 4782, "frp_sum": 26848.11}, "complete_days": 43,
 "static_excluded": 469, "static_rule": "Detections within 400 m of a spot where FIRMS saw heat on 3 or more different nights ...",
 "include_static": false, "note": null}
```

- `complete: false` marks a day that is still partial (today) or not yet re-fetched through the
  area API. Draw it as partial.

### GET /api/fires/hours

Params: `days` (1..90, default 7), `region`, `end`.

```json
{"days": 7, "region": "all", "window": {"from_utc": "...", "to_utc": "..."}, "total": 2165,
 "bins": [{"hour_ist": 0, "count": 0}],
 "by_satellite": {"Suomi NPP": [0, "... 24 counts"], "NOAA-20": [], "NOAA-21": [], "Aqua": [], "Terra": []},
 "daytime_share": 0.9838,
 "observed_windows_ist": [{"start": "01:06", "end": "02:59", "count": 34, "share": 0.0157,
   "satellites": ["NOAA-20", "NOAA-21", "Suomi NPP"], "daynight": "N", "minor": false}],
 "unobserved_span_ist": {"start": "16:34", "end": "01:06", "hours": 8.53, "detections": 1, "share": 0.0005,
   "minor_windows": [{"start": "20:56", "end": "20:56", "count": 1, "satellites": ["Terra"]}],
   "after_daytime": true, "rule": "from the end of the last daytime window to the next window"},
 "gaps_ist": [{"start": "02:59", "end": "12:35", "hours": 9.6, "...": "..."}],
 "static_excluded": 77, "static_rule": "...", "include_static": false,
 "method": {"window_gap_minutes": 30, "min_window_share": 0.01, "basis": "..."}, "note": null}
```

The shaded "blind span" on the 24-hour chart is `unobserved_span_ist` (here 16:34 to 01:06 IST,
8.5 h). Pair it with `/api/geostationary/diurnal`, which shows the activity inside that span.

### GET /api/fires/official (addition: CREAMS official counts)

Params: `since`, `until`, `region`.

```json
{"layer": "rice", "region": "all", "since": "2026-09-15", "until": "2026-10-31", "day_basis": "IST",
 "series": [{"date": "2026-09-15", "count": 2, "by_satellite": {"Suomi NPP": 2}, "layer_events": 5, "published": true, "final": true}],
 "totals": {"count": 1043, "by_state": {"Punjab": 1019, "Haryana": 23, "Delhi": 1}, "by_satellite": {"Suomi NPP": 692, "Aqua": 351}},
 "by_district": [{"district": "Tarn Taran", "state": "Punjab", "count": 390}],
 "layer_totals": {"events": 2696, "layers_fetched": 45, "layers_missing": ["2026-09-17", "2026-10-31"],
   "note": "All events in the CREAMS layers, every state CREAMS monitors."}, "note": null}
```

- `totals.count` is the AOI number. `layer_totals.events` covers every state and is not an AOI
  number.
- `published: false` means CREAMS has not published that day yet (usually today).
- Cadence: `creams_daily` runs hourly.

---

## Orbits: `/api/orbits/*`

- **GET /api/orbits/tle** returns `{satellites: [{key, name, norad_id, instrument, swath_km,
  half_swath_km, line1, line2, epoch, epoch_age_hours, fetched_at, object_name, omm: {...},
  last_fire_data_utc}], swath_sources}`.
  - 8 satellites: snpp, noaa20, noaa21, aqua, terra, s2a, s2b, s2c.
  - `line1`/`line2` feed satellite.js `twoline2satrec`; `omm` feeds `json2satrec`.
  - Cadence: every 12 h. Skipped when CelesTrak was asked less than 2 h ago.
- **GET /api/orbits/passes**: `lat`, `lon` (both or neither; the default is the Punjab
  centroid), `hours_back` (0..72, default 24), `hours_ahead` (0..168, default 48),
  `instruments=VIIRS,MODIS,MSI`.

```json
{"site": {"lat": 30.8421, "lon": 75.4159, "name": "Punjab (centroid)", "basis": "...", "default": true},
 "window": {"from_utc": "2026-10-29T23:33:00Z", "to_utc": "2026-11-01T23:33:00Z"},
 "passes": [{"satellite": "Terra", "key": "terra", "instrument": "MODIS", "start_utc": "2026-10-30T03:27:53Z",
   "peak_utc": "2026-10-30T03:30:25Z", "end_utc": "2026-10-30T03:32:57Z", "peak_elevation_deg": 49.2,
   "min_ground_distance_km": 519.5, "off_nadir_deg": 36.1, "direction": "descending", "daynight": "D"}],
 "current": {"watching": [], "next": {"satellite": "Terra", "instrument": "MODIS", "at_utc": "2026-10-31T04:09:05Z", "start_utc": "...", "end_utc": "...", "min_ground_distance_km": 396.0},
   "last": {"satellite": "Aqua", "...": "..."}, "next_any": {"...": "..."}, "blind_for_minutes": 273},
 "satellites": [{"satellite": "Suomi NPP", "key": "snpp", "instrument": "VIIRS", "half_swath_km": 1520.0, "tle_epoch": "...", "tle_age_hours": 9.1, "last_fire_data_utc": "2026-10-30T20:19:00Z"}],
 "visibility_rule": {"VIIRS": {"half_swath_km": 1520.0, "source": "..."}, "MODIS": {"half_swath_km": 1165.0}, "MSI": {"half_swath_km": 148.0}},
 "notes": ["Sentinel-2 passes are predictions: ..."]}
```

There is no `hours` parameter. A pass counts when the site lies inside the instrument's swath
(`min_ground_distance_km <= half_swath_km`).

- **GET /api/orbits/clock** (`lat`, `lon`, `days=16`): when each satellite usually looks at the
  site, in IST, for the "look clock" graphic.
- **GET /api/orbits/validation** (`days=3`, `region`): `{overall: {n, mean_min, median_min,
  p95_abs_min, within_3_min_share, ...}, satellites: [{satellite, error_minutes, ...}], method,
  note}`. The live result is a median error of 0.6 min and a p95 |error| of 2.1 min.

---

## Burn scars: `/api/scars*` (Sentinel-2)

- **GET /api/scars**: `since`, `until`, `district`, `unseen` (bool), `bbox`, `limit` (default
  5000), `min_confidence` (0-1).
  - Returns `{totals: {clusters, area_ha, unseen_clusters, unseen_area_ha, checked_clusters},
    returned, truncated, window: {first_pass, last_pass}, method: {model_version, summary, label,
    audit, unseen_rule}, features: FeatureCollection<Polygon>}`.
  - Feature properties are `{id, area_ha, burn_after, burn_before, district, subdistrict,
    village, village_km, centroid, dnbr, nbr_post, confidence, unseen, fire_matches,
    nearest_fire_m, scene_pre, scene_post, label, state, gap_days, hazy, res_m, mgrs_tile,
    collection}`.
  - `label` is "candidate unseen burn", "candidate burn, FIRMS-confirmed", or "candidate burn
    (no fire-record verdict yet)" when `unseen` is null. Use it verbatim.
- **GET /api/scars/summary** (`since`; default 1 Oct):

```json
{"season": {"since": "2026-10-01", "clusters": 67854, "area_ha": 104476.28, "unseen_area_ha": 86303.0, "unseen_share": 0.826,
   "fire_detections": 4319, "fire_detections_scope": "FIRMS detections in the districts listed in by_district (addition)",
   "checked_area_ha": 104476.28, "last_pass": "2026-10-30T05:40:52.298000Z"},
 "by_district": [{"district": "Amritsar", "state": "Punjab", "area_ha": 14318.36, "clusters": 9269, "unseen_area_ha": 10715.28,
   "unseen_share": 0.748, "fire_detections": 658, "scope": ["hotspots", "punjab"], "res_m": 20}],
 "coverage": {"processed": 142, "skipped": 32, "failed": 0, "catalogued": 251, "latest_processed_pass": "2026-10-30T05:40:52.298000Z", "tiles": ["43RCP", "... 12 tiles"]},
 "method": {"model_version": "s2burn-rules-2", "summary": "...", "label": "candidate unseen burn",
   "audit": {"candidate_unseen": {"precision": 0.87, "precision_ci95": [0.679, 0.955], "audited": 23, "...": "..."},
             "firms_confirmed": {"precision": 0.833, "...": "..."}, "recall_vs_firms": {"recall_same_window": 0.353, "...": "..."}, "current": true},
   "unseen_rule": "unseen = no FIRMS detection within max(1 km, pixel half-diagonal + 200 m) ..."}}
```

  `res_m` is 20 in the hotspot districts and 40 elsewhere. Coverage grows every 6 h.
- **GET /api/scars/{id}** returns the case file:
  - `case` holds the properties plus `geometry`.
  - `imagery: {bbox, pre: {date, datetime, item_id, collection, cloud_pct, truecolor_url,
    swir_url, nbr_url}, post: {...}}`.
  - `fires_nearby: [{id, source, satellite, acq_utc, lat, lon, confidence, frp_mw, distance_m,
    match_radius_m, in_window, explains_scar}]`.
  - `weather_at_burn: {available, series: [{t, wind_10m_kmh, wind_from_deg, blh_m, temp_2m_c,
    precip_mm}]}`.
  - An unknown id returns 404 "no such scar".
  - The imagery URLs are absolute (`PUBLIC_BASE_URL`).

## Burn risk: GET /api/risk

Params: `district`, `limit` (default 500), `min_score`, `bbox`.

```json
{"as_of": "2026-10-30T23:13:02.192298Z",
 "totals": {"clusters": 172130, "area_ha": 333050.0, "scored": 172130, "scored_area_ha": 333050.0},
 "features": {"type": "FeatureCollection", "features": ["{id, area_ha, harvested_after, harvested_before, last_seen_unburnt, days_since_harvest, risk_score, risk_factors, district, village, village_inferred, centroid, mgrs_tile, state}"]},
 "ranking": [{"village": "Chamar Tibbi, Moranwali, Model Town ( R", "district": "Sangrur", "clusters": 147, "area_ha": 498.04,
   "risk_score": 86.2, "max_risk_score": 90.8, "risk_weighted_ha": 429.54, "centroid": [75.753, 30.21], "village_inferred": true}],
 "unnamed": {"clusters": 0, "area_ha": 0.0, "why": "..."},
 "ranking_order": "risk_weighted_ha = ...",
 "method": {"model": "risk-v2", "formula": "risk = 100 x dryness x (0.4 timing + 0.4 neighbourhood + 0.2 history)", "weights": {"timing": 0.4, "neighbourhood": 0.4, "history": 0.2}, "factors": {}, "note": "A transparent ranking, not a probability ..."},
 "backtest": {"headline": {"tile": "43REP", "auc": 0.687, "burnt": 182, "unburnt": 991, "base_rate": 0.155, "precision_at_k": {}, "baselines": {}, "villages": {}},
   "all_pairs": {"pairs": 148, "auc_weighted_mean": 0.556}, "timing_tested": false},
 "reason": null}
```

- `risk_score` runs from 0 to 100. It is a ranking, not a probability.
- `risk_factors.neighbourhood.candidate_unseen_burns_7d` was renamed from `unseen_burns_7d`.
- Village names come from census 2011 and are cut at 40 characters upstream (see the ranking
  sample).
- Present the backtest honestly: the latest pass pair has AUC 0.687, but the weighted mean over
  all 148 pairs (13 tiles) is **0.556**.
- Cadence: every 3 h. The run now takes about 13 min for 172k fields.

---

## Air: `/api/air/*`

- **GET /api/air/latest** (`at`, `bbox`) returns `{as_of, fresh_minutes: 120, stations: [...],
  receptors: [...], counts: {stations, fresh, stale, with_pm25, model_points, cities}, reason?}`.
  - `stations[]` entries are `{id ("openaq:6964" | "cpcb:<name>" | "cams:<city>"), name, city,
    state, lat, lon, pm25, pm10, observed_at, provider, is_model, pm25_observed_at,
    pm10_observed_at, age_minutes, stale, aqi?}`.
  - CPCB-feed-only stations have `pm25`/`pm10` null and carry
    `aqi: {value, category, prominent, subindex, ...}`.
  - `receptors[]` entries are `{city, state, pm25_now, pm25_24h_mean, observed_at, provider,
    n_stations, pm10_now, n_stations_pm10, n_stations_total, n_stale, pm25_24h_hours,
    pm25_last: {value, observed_at, n_stations, age_minutes, stale}, last_observed_at, lat, lon,
    aqi, model: {pm25, pm10, valid_at, provider: "cams"}}`.
  - `pm25_now` is null when every station is stale. At 05:03 IST all 134 stations were stale
    (the OpenAQ delay). Show `pm25_last` with its age.
- **GET /api/air/series** (`city`, required; `hours=72`, `ahead_hours=0`, `at`) returns
  `{city, hours, unit: "µg/m³", as_of, series: [{t, pm25, n_stations}],
  model_series: [{t, pm25}], provider: "openaq", model_provider: "cams", hours_with_data,
  max_stations, model_forecast?}`.
  - Series `t` marks the end of an IST clock hour, so it falls on `:30Z`. Model points fall on
    the hour (UTC).
  - A gap is `pm25: null, n_stations: 0`.
  - An unknown city returns 404 with the list of cities. Cities are case-insensitive and
    aliased (Gurgaon -> Gurugram).
- **GET /api/air/stubble-share** (IITM DSS) returns `{run_date, stale, first_seen_at,
  last_seen_at, today: {date, stubble_pct}, daily: [...], outlook: [{date, stubble_pct,
  fire_input, sources}], unit, method_note, attribution}`. `stale` is true from midnight IST
  until DSS updates (about 10:20 IST). The outlook values are climatology-driven; label them as
  "outlook".
- Cadence: `air` every 30 min; `air_dss` at 10:20, 14:20 and 20:20 IST.

## Weather: `/api/met/*`

- **GET /api/met/now** (`city` or `lat`+`lon`, `t`) returns `{city, district, state, t, lat,
  lon, basis, wind_10m: {speed_kmh, from_deg, u_ms, v_ms}, wind_925: {...},
  transport_wind: {...}, blh_m, temp_2m_c, rain_mm_24h, ventilation_m2_s,
  inversion: {present, t925_minus_t2m_c, model, basis}, ensemble: {gfs_seamless: {...}}, model,
  run_at, upstream_runs, attribution}`.
  - Delhi live: wind 5.8 km/h from 308°, BLH 77 m, 18.6 °C, 10.1 mm of rain in 24 h.
  - `inversion` is an object, not a boolean.
  - `rain_mm_24h` is null unless all 24 hours are present.
- **GET /api/met/wind-field** (`t=now`, `level=925hPa|10m|bl`, `model=best_match|gfs_seamless`)
  returns `{t, level, grid: {lon0: 70, lat0: 26, dlon: 0.5, dlat: 0.5, nx: 21, ny: 17},
  u: [357], v: [357], units, order, model, run_at, available, requested_t, attribution}`.
  The grid is row-major from the SW corner: `index = j*nx + i`.
- Cadence: `met` every 3 h. It skips when the upstream model runs are unchanged.

## Smoke: `/api/smoke/*` (two-model ensemble)

- **GET /api/smoke/forward** returns `{run_at, t0, t_end, hours, step_minutes, members,
  method, notes, inputs: {fires, scars, scars_kind: "candidate_unseen_burns", fire_tonnes,
  scar_tonnes, fires_by_state, release_window, particles_per_source}, trails: [{id,
  path: [[lon, lat]], times: [unix_s], weight, origin: {type, id, district}, model}],
  receptors: [{city, lat, lon, state, district, series: [{t, smoke_index}], peak: {t, value},
  arrival_t, by_state, unattributed_share}], run_id}`.
  - 2000 trails, 12.4 MB raw and 2.5 MB gzip: load it only when the smoke layer is shown.
  - Colour or filter trails by `model` (`best_match` | `gfs_seamless`).
  - Fires in Pakistan are modelled and labelled "outside India".
- **GET /api/smoke/back** (`city=Delhi`, `hours=72`) returns `{run_at, city, t0, hours,
  trails: [200], summary: {share_over_burn_belt: {h24, h48, h72}, fire_exposure_index,
  fire_exposure_range: [lo, hi], top_districts, top_source_districts, blh_m_now,
  wind_from_deg_now, wind_speed_kmh_now, exposure_from_scars, exposure_unattributed_share,
  left_grid_share, cone (GeoJSON Polygon), members: [{model, centerline, ...}],
  verdict_reason, wind_basis}, verdict_hint: "stubble_likely"|"mixed"|"stubble_unlikely",
  receptor, notes, run_id}`.
  - Draw `summary.cone` and both `members[].centerline`. Show `fire_exposure_range` as the
    uncertainty.
  - An unknown city returns 404 with the list of valid cities.
  - Delhi live: "mixed", FEI 0.332 (0.08-0.332), belt share h24 55%.
- **GET /api/smoke/receptors** returns `{receptors: [{city, lat, lon, district, state, run_at,
  verdict_hint, verdict_reason, share_over_burn_belt, fire_exposure_index,
  fire_exposure_range, blh_m_now, wind_from_deg_now}]}` for 24 cities. These are the map
  markers.
- **GET /api/smoke/validation** returns the modelled smoke index against observed PM2.5 per
  city, with `r` and `n_eff`. Delhi now: r 0.65 at lag 0, 0.85 at a 4 h lag. n_eff is small:
  show it as indicative.
- Cadence: `smoke` runs after each `met`; `smoke_validate` every 6 h.

## Imagery: `/api/imagery/*`

- **GET /api/imagery/featured** returns:
  - `aoi: {name, bbox, center, size_km, district, state, chosen_by, chosen_value,
    chosen_unit, chosen_count, chosen_since}`.
  - `frames: [{date, datetime, item_id, collection, platform, cloud_pct, tile_cloud_pct,
    hazy, blue_median, truecolor_url, swir_url, width: 1600, height: 1000}]`, four frames,
    oldest first.
  - `passes_considered`, `passes_clear`, `dropped`, `reason`, `built_at`, `stale`.
  - Live window: "Near Raipur Peerbakhshwala, Kapurthala district", 824 ha of scars since
    10 Oct, frames on 13-28 Oct.
- **GET /api/imagery/render** (`collection`, `item`, `bbox`, `kind=truecolor|swir|nbr`, `w`,
  `h`, `format`) returns image/jpeg (or png for nbr).
  - Cached on disk and immutable.
  - Errors are JSON: 400 for a bbox outside the AOI, 422 for a bad collection or size, 404 for a
    missing item.
  - On a serverless deployment without rasterio/pillow, it returns 503 with `missing_module`
    unless the picture is already on disk.
- **GET /api/imagery/scenes** (`lon`, `lat`, required; `days=30`, `limit=10`) returns
  `{scenes: [{date, datetime, item_id, collection, mgrs_tile, platform, tile_cloud_pct,
  nodata_pct, status, note, usable, tiles: {truecolor, swir, nbr}, tilejson: {...}}]}`.
- **GET /api/imagery/tilejson/{kind}/{item}** returns TileJSON 3.0.0. The first call costs
  about 1.3 s upstream.
- **GET /api/imagery/tiles/{kind}/{item}/{z}/{x}/{y}** returns image/png and is immutable. The
  first render of a tile takes about 6 s; after that it is served from disk.

## Geography: `/api/geo/*`

- **GET /api/geo/boundaries** (`level=state|district|subdistrict`, `state`, `simplify=0.001`,
  `bbox`) returns `{level, state, bbox, simplify_deg, count, notes, attribution,
  features: FeatureCollection<MultiPolygon, {id, level, name, state, code, area_km2, aliases?,
  district?, iso3166_2?, derived?, source}>}`.
  - 96 AOI districts, 0.38 MB gzip.
  - Draw India's outline from `level=state` (Survey of India), never from the basemap.
  - Show `notes` (the Hansi-derived and Delhi caveats).
- **GET /api/geo/districts** (`state`) returns `{count, districts: [{id, name, state, code,
  area_km2, aliases, in_lgd_today, derived}]}`.
- **GET /api/geo/cities** returns `{count, rule, reason, cities: [{id, name, state, district,
  lat, lon, population, population_source, geonames_id, wikidata, ..., tier}]}` for 22
  receptor cities.
- **GET /api/geo/lookup** (`lat`, `lon`, `max_km`) returns `{point, state, district,
  subdistrict, nearest_place, nearest_city}`. Every field is null outside India.
- **GET /api/geo/places** (`q`, `kind`, `state`, `limit`) returns `{q, count, places: [{id,
  kind, name, subdistrict, district, state, lat, lon, population, source}]}`.
- **GET /api/geo/tiles/{z}/{x}/{y}.pbf** returns MVT with the layers `states`, `districts`
  (z5 and up) and `subdistricts` (z8 and up). An empty tile returns 204.

## Geostationary (GK-2A): `/api/geostationary/*`

- **GET /api/geostationary/diurnal** (`days` 1..7, default 3) returns `{days, since, unit,
  region: ["Punjab", "Haryana"], scans, scans_failed, slots: [{t, count, count_pb, count_hr,
  clear_frac}], hourly_ist: [{hour, mean_count, scans}] x24, polar_windows_ist: [{start, end,
  start_min, end_min, satellites}], polar_passes, peak_ist, peak_mean_count, pixel_hours,
  pixel_hours_outside_polar_windows, share_outside_polar_windows, unobserved_gaps_ist:
  [{start, end, minutes, pixel_hours}], interpretation: [str], validation: {reference,
  hit_rate, false_alarm_rate, n, n_geo, passes, groups: {viirs_all, viirs_day, viirs_night,
  modis_aqua_day, modis_terra, all}, period, algorithm, method}}`.
  - Use `unit` and `interpretation` as the copy.
  - `mean_count` is null for hours without scans.
- **GET /api/geostationary/latest** (`min_confidence=1|2`, `slot=<ISO>`) returns
  `{scan: {slot_utc, t, age_min, count, count_pb, count_hr, count_high, frp_sum_mw,
  clear_frac, sza_deg, daylight, algorithm}, unit, fires: FeatureCollection<Polygon, {lon,
  lat, state, confidence: "nominal"|"high", frp_mw, excess_k, bt38_k, bt11_k, area_km2,
  absolute_test}>}`.
  - At night the newest scan usually has 0 features. Use `slot=2026-10-29T10:40:00Z` (49
    pixels) for the evening-peak demo.
  - An unknown slot returns 404.
- Cadence: every 30 min. The backfill runs daily at 04:20 IST.

## Agent and bulletin

- **POST /api/agent/ask** `{question, lang?: "en"|"hi"|"pa"}` returns `text/event-stream`.
  - Each frame is `event: <type>` plus `data: <json>` (the event name equals `data.type`).
  - A live run takes 8.4 s, 25 frames. The same question within 10 min replays from the
    stored run in about 140 ms, with `run.cached: true` and `done.replayed: true`.
  - Bad body: 422. Per-IP limit: 429 with `Retry-After`.

```
run          {"type":"run","run_id":"97c49cde-...","model":"gemini-3.5-flash-lite","lang":"en"}
thought      {"type":"thought","text":"**Analyzing Punjab Fires** ..."}
tool_call    {"type":"tool_call","id":"call_1047264","name":"get_fire_activity","args":{"hours":24,"region":"punjab"}}
map          {"type":"map","action":"show_layer","payload":{"layer":"fires","visible":true}}
tool_result  {"type":"tool_result","id":"call_1047264","name":"get_fire_activity","ok":true,"summary":"232 fire detections in the last 24 h (Punjab); most in Tarn Taran (54)","sources":[{"key":"firms","label":"...","url":"...","fetched_at":"...","fetch_id":4276,"marker":9}]}
answer_delta {"type":"answer_delta","text":"Satellites recorded 232 active fire detections ..."}
answer       {"type":"answer","text":"... 232 ... [9] ... 09:39 IST on 31 Oct by the Terra satellite [1] ...","citations":[{"marker":9,"source":{...}}]}
verification {"type":"verification","checked":10,"verified":10,"unverified":[],"passed":true,"uncited":[],"bad_markers":[],"revised":false,"first_pass_unverified":[]}
done         {"type":"done","run_id":"97c49cde-...","duration_ms":8228,"ok":true,"models":["gemini-3.5-flash-lite"]}
```

  - Citation markers are the run's source numbers (`[9]`, `[1]`), not 1..n in reading order.
    Map them through `answer.citations[].marker`.
  - The `map` payload is `{layer, visible, params?}`. Layers seen live: `air_stations`, `fires`,
    `smoke_back`.
- **GET /api/agent/runs/{id}** returns `{run: {id, kind, question, lang, model, started_at,
  finished_at, duration_ms, ok, answer, citations, verification, tools_used, error, events,
  tools, usage}}`. An unknown id returns 404.
- **GET /api/agent/examples** (`limit` 1..20, `lang`) returns `{examples: [run without
  events]}`. It lists only verified runs that cite sources.
- **GET /api/agent/status** returns `{key_configured, quota_day_started_at, reserve_share,
  models: [{model, used_today, daily_limit, new_questions_stop_at, cooling_down_until, ...}],
  per_ip_limits}`.
- **GET /api/bulletin/latest** (`lang=en|hi|pa`; anything else gives 422) returns `{date, lang,
  model, run_id, title, summary, sections: [{heading, body}], citations: [{marker, source}],
  verification: {passed, checked, verified, unverified, uncited, bad_markers,
  removed_sentences, script_ok, foreign_script_words, first_pass_unverified}, data_as_of,
  available_langs}`.
  - The date is the IST day. Before 07:30 IST it is the previous day's bulletin.
  - **Hide or warn on a language with `verification.script_ok: false`.** On 2026-10-31 the
    Punjabi bulletin is flagged: the model mixed in Bengali and Cyrillic letters. The hourly
    retries (07:30-12:30 IST) try again.
  - Hindi uses Devanagari digits; Punjabi uses 0-9.

## Core

- **GET /api/health** returns `{ok, time, db, version, scheduler}`.
- **GET /api/sources** returns `{sources: [{key, label, kind, status, last_success_at,
  last_attempt_at, last_error, records_last, latency_ms_last, cadence_minutes, docs_url}]}`.
