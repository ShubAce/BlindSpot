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
  - 400: bad bbox or bad dates; a `follow_up_of` run that cannot be followed up (see "Agent and
    bulletin").
  - 404: unknown id or city. The body lists the valid values when there is a short list.
  - 422: validation. `detail` is a list of pydantic errors.
  - 429, always with `Retry-After` and `Cache-Control: no-store`: the per-client limits on agent
    questions, live picture renders and live map tiles (see "Agent and bulletin" and "Imagery"),
    and a never-answered heavy query variant past the per-instance allowance (see "Serverless
    deployment").
  - 502: upstream failed. 503: upstream paused, with `Retry-After`.
  - 503 from `/api/stats/headline` when no block could be computed (the database is down): the
    same body, `no-store`, `Retry-After: 60` (see "Serverless deployment").
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
  built. Successful GETs also carry `s-maxage` for the CDN in front of the serverless API (see
  "Serverless deployment" at the end).
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
- Cadence: the inputs change hourly 08:17-18:17 UTC and every ~3 h at night (fires, air, GK-2A),
  every 6 h (met, smoke) and daily (scars). The CDN keeps the headline until :30 after each hourly
  run, so polling every 60 s is plenty; each block's `freshness` says how old its data is.

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
- Cadence: `firms_nrt` runs hourly 08:17-18:17 UTC and at 00:17, 03:17, 06:17, 21:17. The NRT
  latency is about 3 h after overpass.

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
- Cadence: `creams_daily` runs with `firms_nrt` (hourly in the afternoon and evening, ~3-hourly at night).

---

## Orbits: `/api/orbits/*`

- **GET /api/orbits/tle** returns `{satellites: [{key, name, norad_id, instrument, swath_km,
  half_swath_km, line1, line2, epoch, epoch_age_hours, fetched_at, object_name, omm: {...},
  last_fire_data_utc}], swath_sources}`.
  - 8 satellites: snpp, noaa20, noaa21, aqua, terra, s2a, s2b, s2c.
  - `line1`/`line2` feed satellite.js `twoline2satrec`; `omm` feeds `json2satrec`.
  - Cadence: daily at 11:17 UTC. Skipped when CelesTrak was asked less than 2 h ago.
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

  `res_m` is 20 in the hotspot districts and 40 elsewhere. Coverage grows daily (11:17 UTC).
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

- `risk_score` runs from 0 to 100. It is a relative index, not a probability (`method.scale`).
- **risk-v3 (2026-10-31, the saturation fix).** risk-v2 turned counts into 1 - exp(-n/typical) and
  combined the two neighbourhood views with a noisy-OR, so busy places hit 1.0: 500 fields scored
  99.7-100.0 and 35 tied at exactly 100.0. v3 keeps the same factors and weights but uses
  n / (n + typical) (0.5 at the typical count, never 1) and averages the two views. On the dev
  database: top score 90.19 (one field), the top 500 hold 178 distinct scores (88.0-90.19), p50
  53.7, p90 77.3. Same skill as v2 (no variant beat v2 on the 47 pass pairs after 24 Oct).
  `risk_score` now has 2 decimals.
- New per-field properties (additions): `risk_rank` (1 = highest of the run; equal scores share
  a rank), `risk_rank_of` (fields scored), `risk_percentile` (share of the run's fields with a
  lower score, %, 3 dp; the top field is 99.999, never 100), `risk_band` ("top 1%", "top 1-5%",
  "top 5-10%", "top 10-25%", "top 25-50%", "bottom 50%"; the band in today's ordering by
  `risk_score`), `burn_probability` and `burn_probability_ci95` (calibrated on the backtest: the
  share of past fields in the same band of their pass that burnt by the next clear pass, 95%
  tile-bootstrap interval) and `burn_probability_band` (addition, repair of 2026-10-31).
  **The backtest tests the ordering by neighbourhood and history only** (timing and rain cannot be
  reconstructed for past passes), so `burn_probability` is the calibration of the field's band in
  THAT ordering (`burn_probability_band`, which can differ from `risk_band`), placed with the cuts
  the risk job stores in `backtest.tested_order`. It is null until the stored backtest carries the
  `tested_order` of the run that made today's scores. When a number is needed, show the probability
  with its interval and say it is for the tested ordering, not the score.
- `method` additions: `scale`, `fields` (what each new property means) and, while the stored
  scores still come from an older model, `scores_model` (for example "risk-v2"). `method.note`
  states how strong the ranking proved, with the backtest's numbers, once a v3 backtest exists.
- `backtest` additions: `model`, `verdict` (one paragraph, every number from the report),
  `strength` ("weak" / "modest" / "moderate", decided by the numbers), `calibration` (`bands`:
  [{top_share_max, band, fields, burnt, burnt_share, burnt_share_ci95, lift}], `base_rate`,
  `horizon`, `what`), `pairs_are`. `headline` adds `n`, `auc_ci95`, `precision_ci95`, `lift_ci95`,
  `hits_at_k`, `ci_note`, `factors_used`. `all_pairs` adds `field_pairs`, `tiles`, `auc_ci95`,
  `top_10pct` {burnt_share, burnt_share_ci95, lift, lift_ci95, base_rate},
  `baselines_auc_weighted_mean`, `what`, `ci_method`; `auc_by_pair` items add `tile`. `windows`
  is still the number of pass-pair windows.
- Repair additions (2026-10-31): `all_pairs.auc_by_tile` [{tile, pairs, field_checks, auc}] (size-
  weighted AUC per tile, most checks first); `backtest.tested_order` {run_id, what, fields, cuts:
  [{band, min_tested_score}], spearman_with_risk_score, top_1pct_overlap}. `backtest.verdict` now
  says the latest pair's interval is narrower than the truth and where its tile ranks among tiles
  with over 100 checks (on the dev data 43REP was the best tile; 43REQ, 35% of the checks, scored
  0.444). `method.note` says the tested part of the ordering is weak, that timing (40% of the
  weight) is untested, and how closely today's score follows the tested ordering (dev database:
  Spearman 0.754, 49.4% of today's top 1% also in the tested top 1%; reviewer's measurement).
- Backtest, measured with risk-v3 on the dev database (148 pass pairs, 361,031 field checks, 12
  tiles): AUC 0.560 (95% CI 0.491-0.663, resampling whole tiles), so **not clearly better than
  chance**; fields in the top 10% of each pass burnt 1.42 times as often as the average field (95%
  CI 1.24-1.73; 21.6% against 15.2%). Latest pair (43REP, 1,173 fields): AUC 0.701 (0.664-0.736,
  field bootstrap, optimistic). Present it as `backtest.verdict` says: a weak ranking for choosing
  where to look first, not a forecast. Never show a raw score as certainty ("score 100").
- `risk_factors.neighbourhood.candidate_unseen_burns_7d` was renamed from `unseen_burns_7d`;
  v3 adds `risk_factors.neighbourhood.scale`, and `combined` reads "mean of events and burnt hectares".
- Village names come from census 2011 and are cut at 40 characters upstream (see the ranking
  sample).
- Cadence: daily, after the 11:17 UTC Sentinel-2 run. The job now scores and backtests inside the
  database and pulls about 0.05 MB from it (it pulled ~105 MB a run before, measured through a
  byte-counting proxy on the dev database).

## Field brief: `/api/brief*` (addition, 2026-11-01)

Where the pollution control boards' field teams (PPCB in Punjab, HSPCB in Haryana) could look first
in a district, built from the burn scars and the latest risk run (`routers/brief.py`). Two kinds of
evidence, each with its qualification in every answer: candidate unseen burns (upper bounds, with
`candidate_unseen_audit`) and the watchlist (ranks, never probabilities, with
`ranking_reliability`).

- **The window** is `days` (1..30, default 7) UTC days of Sentinel-2 passes ending at the **newest
  pass in the scar table, not today**: `window: {from, to, days, newest_pass, anchor, basis}`. The
  days are the UTC dates of the pass that shows a burn (`burn_before`), as in `/api/scars`, so
  `/api/scars?district=<d>&unseen=true&since=<from>&until=<to>` lists exactly the scars a brief
  counts. Say "the 7 days of passes up to <to>", never "the last 7 days".
- **GET /api/brief** (`days`) returns `{window, label: "candidate unseen burns", districts:
  [{district, state, unseen_clusters, unseen_area_ha, villages, newest_pass, harvested_fields,
  harvested_area_ha, top_tenth_fields, top_tenth_villages, links: {brief, csv}}],
  districts_order, totals: {districts, unseen_clusters, unseen_area_ha}, candidate_unseen_audit:
  {precision, precision_ci95, audited, method, date, current, reading}, risk_run: {run_id, model,
  as_of, fields_ranked, top_tenth}, ranking_reliability: {available, model, computed_at, strength,
  timing_tested, all_pairs: {pairs, auc_weighted_mean, auc_ci95, top_10pct}, reading}, notes:
  [str], reason}`: the districts of Punjab and Haryana with candidate unseen burns in the window,
  largest candidate unseen area first. Before any scar is mapped: `window: null`, `districts: []`
  and a `reason`.
- **GET /api/brief/{district}** (`days`; `limit` 1..2000, default 50, rows per list; the name in
  any case, punctuation ignored: "s a s nagar" finds S.A.S Nagar) returns `{district, state,
  window, label, totals: {unseen_clusters, unseen_area_ha, villages, newest_pass,
  harvested_fields, harvested_area_ha, top_tenth_fields, top_tenth_villages}, villages: [{village,
  subdistrict, unseen_clusters, unseen_area_ha, newest_pass, lat, lon, case_ids, maps_url,
  case_url}], villages_total, villages_order, watchlist: [{village, subdistrict, fields, area_ha,
  top_tenth_fields, top_tenth_area_ha, in_top_tenth, best_rank, lat, lon, field_ids, maps_url}],
  watchlist_total, watchlist_in_top_tenth, watchlist_order, candidate_unseen_audit, risk_run,
  ranking_reliability, notes, links, reason}`.
  - `totals` always cover every village (`limit` cuts only the lists) and equal the district's
    row in `GET /api/brief`.
  - A village's point (`lat`, `lon`, `maps_url`) is its largest candidate unseen burn; `case_ids`
    (up to 5, largest first) open `/case/<id>`, and `case_url` is the first. `village: null` is a
    sub-district's scars with no census village within 3 km: show it as unnamed.
  - **The watchlist lists every village with harvested-but-unburnt fields:** those with fields in
    the risk run's top tenth first (`in_top_tenth: true`, the first `watchlist_in_top_tenth`
    rows), then the others by their best-ranked field. **Never present a row with `in_top_tenth:
    false` as top tenth or high risk.** Many districts have few such villages and some none: on
    the dev database on 2026-11-01, 21 of 36 districts had fewer than ten, and Karnal none, while
    its watchlist listed 250 villages, the best ranked 18,794th of 172,130 fields. `best_rank` is
    the rank of the village's best field among `risk_run.fields_ranked` (1 = the highest); the
    brief has no scores and no probabilities.
  - An unknown district is a 404 whose `detail` names the districts with burn scars.
- **GET /api/brief/{district}/csv** (`days`; `lang` en|hi|pa, anything else 422) returns
  `text/csv; charset=utf-8` with a byte-order mark (without it Excel garbles Hindi and Punjabi):
  one row per village of the window (all of them), columns village, sub-district, district,
  candidate unseen burns, area (ha), newest pass date, lat, lon, Google Maps link, case file link.
  - The header is in the chosen language, and the count and area cells name the window and carry
    the audit, so a saved or forwarded file keeps its qualification: "candidate unseen burns,
    Sentinel-2 passes 2026-10-24 to 2026-10-30 (upper bound: in a hand audit of 23, 87% were real
    burns, 95% CI 68%-96%)".
  - The file is `blindspot-brief-<district slug>-<from>-to-<to>.csv`, and
    `Link: </api/brief/<district>?days=<days>>; rel="describedby"` points to the JSON brief, whose
    `sources` and `notes` cover every figure in the file.
  - A name that starts like a spreadsheet formula (`=`, `+`, `-`, `@`, tab, carriage return) is
    written with a leading apostrophe: village names include OpenStreetMap places, which anyone can
    edit. Numbers use the digits 0-9 in every language.
- Sources (`brief.SOURCE_KEYS`): `stac_e84`, `s2_cog`, `worldcover`, `firms`, `boundaries`,
  `openmeteo` (the risk run's rain), `census2011`, `osm`.
- Caching: the CDN keeps every 200 answer on the DAILY clock (that of `/api/scars` and
  `/api/risk`), browsers 5 min, each instance 10 min.
- Measured read-only on the dev database on 2026-11-01 (body bytes, then gzip): `/api/brief?days=7`
  15,623 / 3,700 (36 districts; 1.7-4.1 s for an instance's first call, which scans the scar
  table and ranks every harvested field); `/api/brief/Patiala` with the defaults 37,295 / 7,807; the largest
  district with `days=30&limit=2000` (Jalandhar: 832 villages, 1,122 watchlist rows) 635,322 /
  92,461; the largest CSV (Gurdaspur, Punjabi, `days=30`) 173,601 / 25,226.

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
- Cadence: `air` hourly 08:17-18:17 UTC and at 00:17, 03:17, 06:17, 21:17; `air_dss` with `met`
  at 03:17, 09:17, 15:17 and 21:17 UTC.

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
- Cadence: `met` every 6 h (03:17, 09:17, 15:17, 21:17 UTC). It skips when the upstream model
  runs are unchanged.

## Smoke: `/api/smoke/*` (two-model ensemble)

- **GET /api/smoke/forward** returns `{run_at, t0, t_end, hours, step_minutes, members,
  method, notes, inputs: {fires, scars, scars_kind: "candidate_unseen_burns", fire_tonnes,
  scar_tonnes, fires_by_state, release_window, particles_per_source}, trails: [{id,
  path: [[lon, lat]], times: [unix_s], weight, origin: {type, id, district}, model}],
  receptors: [{city, lat, lon, state, district, series: [{t, smoke_index}], peak: {t, value},
  arrival_t, by_state, unattributed_share}], run_id}`.
  - 2000 trails, 12.4 MB raw and 2.5 MB gzip: load it only when the smoke layer is shown.
  - Optional (addition): `max_trails` (1..10000) and `stride` (1..100) make it smaller; then the
    body also has `trails_selection`. See "Serverless deployment".
  - Colour or filter trails by `model` (`best_match` | `gfs_seamless`).
  - Fires in Pakistan are modelled and labelled "outside India".
- **GET /api/smoke/back** (`city=Delhi`, `hours=72`) returns `{run_at, city, t0, hours,
  trails: [200], summary: {share_over_burn_belt: {h24, h48, h72}, fire_exposure_index,
  fire_exposure_range: [lo, hi], top_districts, top_source_districts, blh_m_now,
  wind_from_deg_now, wind_speed_kmh_now, exposure_from_scars, exposure_unattributed_share,
  left_grid_share, cone (GeoJSON Polygon), members: [{model, centerline, ...}],
  verdict_reason, wind_basis}, verdict_hint: "stubble_likely"|"mixed"|"stubble_unlikely",
  receptor, notes, run_id}`.
  - Takes the same optional `max_trails` and `stride` as `/forward` (addition).
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
- Cadence: `smoke` runs after each `met`; `smoke_validate` daily at 11:17 UTC.

## Imagery: `/api/imagery/*`

- **GET /api/imagery/featured** returns:
  - `aoi: {name, bbox, center, size_km, district, state, chosen_by, chosen_value,
    chosen_unit, chosen_count, chosen_since}`.
  - `frames: [{date, datetime, item_id, collection, platform, cloud_pct, tile_cloud_pct,
    hazy, blue_median, truecolor_url, swir_url, width: 1600, height: 1000}]`, four frames,
    oldest first.
  - `passes_considered`, `passes_clear`, `dropped`, `reason`, `built_at`, `stale`.
  - Additions: `age_minutes` (minutes since `built_at`, as of `generated_at`; null if
    `built_at` is missing) and `stale_after_hours`.
  - `stale` is true once the frames are older than `stale_after_hours`. The frames are still
    served, so show them with an "older pictures" note rather than hiding them.
    - On Vercel this is 30 h. The `featured` job runs once a day on GitHub Actions, chained
      after `sentinel` at 11:40 UTC. Add the chain's 150 min timeout and 3.5 h of slack, and you
      get 30 h.
    - In a long-lived process it is 6 h. That process rebuilds the frames itself in a background
      thread.
    - A Vercel instance never starts a thread or builds frames during a request.
    - On Vercel, when no manifest is stored at all, the route returns 503 with `Retry-After: 300`.
  - Measured on a local Vercel-like instance on 2026-10-31: a manifest 412 min old gave
    `stale: false, stale_after_hours: 30` on Vercel and `stale: true, stale_after_hours: 6`
    locally.
  - Live window: "Near Raipur Peerbakhshwala, Kapurthala district", 824 ha of scars since
    10 Oct, frames on 13-28 Oct.
- **GET /api/imagery/render** (`collection`, `item`, `bbox`, `kind=truecolor|swir|nbr`, `w`,
  `h`, `format`) returns image/jpeg (or png for nbr).
  - Cached on disk and immutable. The featured frames and the case-file chips are also kept in
    the database, so every serverless instance serves them at once (see "Serverless
    deployment").
  - **Tile restriction.** The item must lie on a Sentinel-2 MGRS tile that intersects the area
    we watch (AOI bbox plus 0.25°: 48 tiles, worked out from the AOI in code rather than typed
    in). Any other item returns 400, and the detail names its tile. The check reads only the
    item id, so it makes no upstream call (a 10SEG item: 400 in about 12 ms, 0 `source_fetch`
    rows). The same rule applies to `tiles/` and `tilejson/`.
  - **Exact URLs are served as stored.** The URLs in `featured` frames and case-file chips are
    served byte for byte with the same headers as before. Checked against the committed build:
    152 of 152 stored URLs (8 frames and 144 chips) were identical, apart from the added
    `X-Imagery-Bbox` header.
  - **Snapping.** Other boxes are snapped to a grid before rendering. The grid step is the
    power of two (in degrees) just under 1/32 of the box's longer side, so no edge moves more
    than 1/64 of the side. As a result, requests a few metres apart share one picture.
    `X-Imagery-Bbox` (an addition) gives the box actually drawn.
  - **Live render limit, per client.** A picture that has to be drawn now counts against the
    client's limit: 30 a minute and 300 a day (`IMAGERY_RENDER_PER_MINUTE`/`_PER_DAY`).
    - Pictures already drawn (on disk or stored) never count.
    - On Vercel the count is shared by every instance (the `client_hit` table), so a cold start
      does not reset it.
    - Past the limit the route returns 429 with `Retry-After` (seconds) and
      `Cache-Control: no-store`. The body is the usual JSON error: `{detail, status, path,
      generated_at, sources}`, for example "Too many new pictures from this address (at most 30
      new pictures a minute); pictures already drawn are not limited. Try again in 57 s."
    - The limit is checked before any database work that only a live render needs. A 512 x 512
      box needs one query to tell whether it is a case-file chip (kept exact and stored); a client
      already past its limit is refused before that query. Such a client gets 429 for a 512 x 512
      box even when its snapped picture is on disk. The query returns only the scar whose extent
      is centred on the box (at most one row was seen across all 20,000 chip boxes in the dev
      database; before, it returned every cluster under the box, up to 50 geometries). Measured:
      the reviewer's 12-request flood of 512 px boxes now gives 200, 200, 429 x 10 with 2
      queries returning 0 rows (before: 12 queries of 50 rows, about 14 KB each).
  - Errors are JSON: 400 for a bbox outside the AOI or an item off the watched tiles, 422 for a
    bad collection or size, 404 for a missing item. An item id that upstream does not know is
    remembered as missing for 1 h on each instance, so asking for it again makes no new call.
  - On a deployment without rasterio/pillow, it returns 503 with `missing_module` unless the
    picture is already stored. The Vercel build now ships both.
- **GET /api/imagery/scenes** (`lon`, `lat`, required; `days=30`, `limit=10`) returns
  `{scenes: [{date, datetime, item_id, collection, mgrs_tile, platform, tile_cloud_pct,
  nodata_pct, status, note, usable, tiles: {truecolor, swir, nbr}, tilejson: {...}}]}`.
- **GET /api/imagery/tilejson/{kind}/{item}** returns TileJSON 3.0.0. The first call costs
  about 1.3 s upstream. Items off the watched tiles return 400 before the STAC lookup.
- **GET /api/imagery/tiles/{kind}/{item}/{z}/{x}/{y}** returns image/png and is immutable. The
  first render of a tile takes about 6 s; after that it is served from disk.
  - Items off the watched tiles return 400.
  - **Live tile limit, per client.** A tile already drawn on this instance is served at once
    and never counts. A tile that has to be drawn counts against a budget of its own, separate
    from and larger than the live render limit, because one map view loads 30-60 tiles and
    people behind one NAT share an address: 600 a minute and 6,000 a day
    (`IMAGERY_TILES_PER_MINUTE`/`_PER_DAY`). On Vercel the count is shared by every instance, as
    for renders.
    - A client past the limit is refused before the STAC lookup. The 429 has the same shape as
      for renders, for example "Too many new map tiles from this address (at most 600 new map
      tiles a minute); map tiles already drawn are not limited. Try again in 60 s."
    - Tiles are also bounded by the AOI, by the scene's footprint (404 outside it) and by the
      per-instance render cap (503 when busy).
    - Measured on two Vercel-like instances (renders 2 a minute, tiles 4 a minute): a client
      refused on `/render` (200, 200, then 429 x 18) got 4 live tiles and then 429 x 6, across
      both instances. A tile it had already drawn was still served (200).
  - Item ids must match the Sentinel-2 id patterns exactly. An id with a trailing newline used
    to pass the check and failed later with a 500 or a dropped connection; now it is a 400 with
    no upstream call, on `render`, `tiles` and `tilejson`.

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
- Cadence: hourly 08:17-18:17 UTC and at 00:17, 03:17, 06:17, 21:17. The backfill runs daily at
  06:17 UTC (11:47 IST).

## Agent and bulletin

- **POST /api/agent/ask** `{question, lang?: "en"|"hi"|"pa", follow_up_of?: <run_id>}` returns
  `text/event-stream`.
  - Each frame is `event: <type>` plus `data: <json>` (the event name equals `data.type`).
  - **Follow-ups (addition, 2026-11-01).** `follow_up_of` is the `run_id` of an earlier
    investigation's `run` event (kind `ask` or `featured`, answered, started less than 24 h ago;
    `AGENT_FOLLOW_UP_HOURS`). The model is also given that question and answer, and the pair
    before it in a longer thread: at most 2 pairs, oldest first, answers without their citation
    markers and cut at 1,600 characters, never any tool output. They are context, not evidence:
    the verifier checks the new answer only against this run's own tool results, so every
    figure is fetched again. The cache key includes the earlier run, so the same follow-up of
    the same run replays within 10 min; questions asked on their own keep their keys.
  - **Follow-up refusals,** both before the client is counted or Gemini is called, in the usual
    JSON error shape with `Cache-Control: no-store`: **400** when the run is unknown, not an
    investigation (a bulletin), ended without an answer, or is too old (`detail` says which, for
    example "Cannot follow up <id>: it is more than 24 h old and its data has moved on. Ask the
    full question again."); **503** with `Retry-After: 30` when the run could not be read (the
    database is unavailable). A `follow_up_of` that is not a run id (32-36 characters of
    `0-9a-f-`) is a 422; an empty string means a question asked on its own.
  - A follow-up's `run` event also carries `follow_up_of` and `context: [{run_id, question,
    answer}]`, exactly what the model was given.
  - A live run takes 8.4 s, 25 frames. The same question within 10 min replays from the
    stored run in about 140 ms, with `run.cached: true` and `done.replayed: true`.
  - Bad body: 422. Per-IP limit: 429 with `Retry-After`.
  - **Per-client limit:** 3 new questions a minute and 40 a day
    (`AGENT_IP_PER_MINUTE`/`AGENT_IP_PER_DAY`).
    - Only questions that start a new run count. A replay of a stored run and a join of the
      same question already running never count. Measured on 2026-10-31: 5 replays and 4
      simultaneous identical questions (1 run) counted 0 and 1.
    - Joins work within one instance only (the runs in flight are kept in process memory). On
      Vercel the same new question arriving at two instances at the same moment starts two runs,
      and each is counted for its own client. Measured: 2 instances, 2 runs; 1 instance, 1 run.
    - Replays are not limited, but each instance reads a stored run's events (about 11-20 KB)
      from Postgres at most once per 2 minutes (and never past the run's 10 min replay window).
      Measured: 8 replays alternating over two instances read the run twice, once per instance.
    - On Vercel the client is the address Vercel puts in `X-Forwarded-For`. Vercel overwrites
      that header, so a client cannot spoof it there.
    - Elsewhere the API ignores `X-Forwarded-For` and `X-Real-IP` and counts the socket peer, as
      uvicorn reports it. uvicorn itself rewrites the peer from `X-Forwarded-For` when the
      connection comes from an address it trusts: 127.0.0.1 by default, and every address with
      `--forwarded-allow-ips='*'` (as in the Dockerfile). Run off Vercel with
      `--no-proxy-headers`, or name only the real proxy's address, or a client can choose its own
      address. Measured with 5 different forged addresses from one socket: 200, 200, 200, 429, 429
      with proxy headers off; 200 x 5 with uvicorn's defaults behind 127.0.0.1.
    - A client the shared count refused is remembered by that instance until `Retry-After`
      runs out, so further requests from it are refused without a database query.
    - IPv6 addresses count per /64.
    - On Vercel the count is also kept in Postgres (`client_hit`, which stores only a salted
      HMAC of the address, never the address), so every instance shares it. Measured across two
      instances: 200, 200, 200, 429.
    - If that table cannot be read, the in-memory limit still applies.
  - **429 shape:** the usual JSON error `{detail, status: 429, path, generated_at, sources: []}`
    with `Retry-After` in seconds and `Cache-Control: no-store`. Example `detail`: "Too many
    questions from this address (at most 3 questions a minute). Try again in 51 s." Today
    `sse.ts` marks a 429 retryable and shows a fixed "try again in a minute"; it could show
    `detail` or `Retry-After` instead.

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
  - `tool_call`/`tool_result` can name an 11th tool, `get_field_brief` (addition, 2026-11-01):
    the district field brief below, for "where should PPCB/HSPCB teams go in <district>". Its
    `tool_result.summary` is "Field brief <district> (<first day> to <last day>): <n> candidate
    unseen burns, <ha> ha in <v> villages (audited precision 87%, 95% CI 67.9-95.5%); <k>
    villages with fields in the risk run's top tenth". `describe.ts` has no label for it yet (it
    falls back to "Called get field brief").
- **GET /api/agent/runs/{id}** returns `{run: {id, kind, question, lang, model, started_at,
  finished_at, duration_ms, ok, answer, citations, verification, tools_used, follow_up_of,
  context, follow_up_until, error, events, tools, usage}}`. An unknown id returns 404.
  - Additions (2026-11-01): `follow_up_of` (the run this one followed up, or null), `context`
    (the earlier questions and answers it was given, `[]` for a question asked on its own) and
    `follow_up_until` (until when this run can itself be followed up, an absolute UTC time so a
    cached copy stays true; null when it never can: a bulletin, a failed run, no answer).
- **GET /api/agent/examples** (`limit` 1..20, `lang`) returns `{examples: [run without
  events]}`. It lists only verified runs that cite sources, and never a follow-up ("and
  yesterday?" means nothing without its thread). Each example carries the same three thread
  fields.
- **GET /api/agent/stats** (addition, 2026-11-01; `days` 1..365, default 30, IST days with today
  included) counts how the guardrails did, from every stored run of the window (`agent_run`),
  never from the model's opinion of itself. Returns `{days, window: {from_utc, to_utc,
  first_day_ist, last_day_ist, day_basis: "IST"}, basis, questions: {...}, bulletins: {...},
  definitions: {...}}`. `questions` (kinds `ask` and `featured`) and `bulletins` have the same
  shape: `{kinds, runs, answered, failed, first_run_at, last_run_at, figures: {checked, matched,
  matched_share, runs, unknown}, all_matched: {runs, of, share, without_figures, unknown},
  corrected_figures: {runs, of, share, then_passed, then_passed_share, sentences_removed,
  unknown, how}, honesty: {passed, of, share, first_draft_issues, first_draft_issues_share,
  then_passed, then_passed_share, fallback_runs, fallback_sentences, unknown}, duration_ms: {p50,
  p90, n}, tool_calls: {median, n}, models: [{model, runs, share}], languages: [{lang, runs,
  share}], series: [{date, runs, answered, all_matched, of, all_matched_share}]}`.
  - A field that older rows lack (the honesty check exists since 31 Oct 2026) is **unknown, not
    false**: such runs are left out of that block's `of` and shares and counted in `unknown`.
    Show "n of m (k unknown)", never a share over all runs.
  - `sources` is the newest recorded Gemini call (`gemini`); the counts are BlindSpot's own.
  - Measured on the dev database on 2026-11-01 (read-only): 11,320 bytes raw, 2,541 gzip, 72 ms.
    CDN: HOURLY (the catch-all rule), in process 5 min.
- **GET /api/agent/status** returns `{key_configured, quota_day_started_at, reserve_share,
  models: [{model, used_today, daily_limit, new_questions_stop_at, cooling_down_until, ...}],
  per_ip_limits: {per_minute, per_day, shared_across_instances}}`. `shared_across_instances` is
  an addition: it is true when the limit is counted in Postgres (on Vercel).
- **GET /api/bulletin/latest** (`lang=en|hi|pa`; anything else gives 422) returns `{date, lang,
  model, run_id, title, summary, sections: [{heading, body}], citations: [{marker, source}],
  verification: {passed, checked, verified, unverified, uncited, bad_markers,
  removed_sentences, script_ok, foreign_script_words, first_pass_unverified}, data_as_of,
  available_langs, available, reason}`.
  - The date is the IST day. Before 07:30 IST it is the previous day's bulletin.
  - `available` and `reason` are additions. `available` is false when the text failed the
    script check (`verification.script_ok: false`). Then `reason` says why, for example "The
    Punjabi bulletin is unavailable: the translation mixes letters of another script into the
    text (...)". Otherwise `reason` is null. The text is still returned, for audit only.
  - **Hide or warn on a language with `available: false`.** The Punjabi translation is now
    asked for in Gurmukhi only (U+0A00-U+0A7F, digits 0-9), at low temperature. If it fails the
    script check, it is asked once more on the other model. A later run of the day translates
    only the missing or flagged language, from the stored English. The 2026-10-31 Punjabi
    bulletin passed after a re-run at 05:37 IST (gemini-3.6-flash, 30/30 numbers verified).
  - Both Hindi and Punjabi write every number with the digits 0-9 (the prompts require it, so the
    verifier can match them).
  - **Honesty check (2026-10-31, addition `verification.honesty`).** Every language is checked
    by its numbers (agent/verifier.py `check_honesty`): a sentence that gives the candidate unseen
    share or area must call them candidate unseen burns, and its section must give the audit
    precision with both ends of its 95% interval (taken from /api/scars/summary
    `method.audit.candidate_unseen`, never typed); raw burn-risk scores are rejected; FIRMS counts
    may not be called crop fires ("crop-residue fires", "crop fires", "stubble-burning incidents",
    and in Hindi/Punjabi a FIRMS count with a crop word and a fire/case word but no word for
    "detection"); an AUC of one pass pair, of the village ranking or of a baseline may not be given
    (only the all-pairs AUC, with its interval when there is one). Repair of 2026-10-31: an unseen
    figure is only a claim when its sentence says it is about burns no satellite saw (or repeats an
    unseen figure to its last decimal), audit numbers are never taken for unseen shares, and a
    number that counts fields or villages is not a score, so true sentences are no longer replaced.
    A rejected English draft is retried once; whatever still fails is rewritten from the tool
    outputs: the season's figure by the candidate sentence with the audit, a district's figure kept
    with "(candidate unseen burns)" and one audit sentence, a single-pair AUC by the all-pairs one,
    a score by the top-ranked village. The investigator's check adds the audit to the
    get_burn_scars output first, so its candidate figures are qualified instead of dropped. `verification.honesty` is
    `{passed, first_pass_issues, retried, after_retry_issues, fallback_sentences}`. The same rule
    runs on investigator answers (`verification.honesty` on the `verification` SSE event:
    `{passed, first_pass_issues, fallback_sentences}`). Stored rows keep their old keys.
  - The 2026-10-31 bulletin was regenerated on the dev database at 07:10 UTC with this check (all
    three languages passed on the first draft).

## Core

- **GET /api/health** returns `{ok, time, db, version, scheduler}`.
- **GET /api/sources** returns `{sources: [{key, label, kind, status, last_success_at,
  last_attempt_at, last_error, records_last, latency_ms_last, cadence_minutes, docs_url}]}`.

---

## Serverless deployment (Vercel function in front of Neon)

The same code runs as one Vercel Python function (`infra/deploy_vercel_api.py`) that reads Neon
Postgres. The jobs run elsewhere (GitHub Actions) and write to Neon. Response shapes are the ones
above. The only differences are the additions listed here.

### Caching headers

A CDN sits in front of the function. It keeps a response only when `Cache-Control` has
`s-maxage`. Every successful (200) GET gets:

`public, max-age=<route's own max-age, at most 300 s and the CDN lifetime>, s-maxage=<seconds to the group's next expiry>, stale-while-revalidate=<swr>, stale-if-error=604800`

The CDN keeps an answer until the job group that feeds it has next run, on a clock (times UTC;
`blindspot/api/cache.py`, `docs/neon-budget.md`). Every answer of a group expires at the same
moments, so `s-maxage` varies with the time of the request (never under 60 s):

| Group | Paths | Expires (UTC) | `stale-while-revalidate` |
| --- | --- | --- | --- |
| HOURLY | `/api/fires*`, `/api/stats/headline`, `/api/sources`, `/api/scars/summary`, `/api/air/latest`, `/api/air/series`, `/api/geostationary/*`, `/api/orbits/passes`, `/api/agent/examples`, `/api/agent/status`, `/api/agent/stats`, everything else under `/api/` | :30 of 08-19 and 00, 01, 03, 04, 06, 07, 21, 22 | 86400 |
| SIX_HOURLY | `/api/smoke/*` (not validation), `/api/met/*`, `/api/air/stubble-share`, `/api/orbits/validation` | :30 of 04, 10, 16, 22 (the hour after met) and 06, 11, 17, 00 (late start) | 86400 |
| DAILY | `/api/scars`, `/api/scars/{id}`, `/api/risk`, `/api/brief*` (JSON and CSV), `/api/smoke/validation`, `/api/orbits/tle`, `/api/orbits/clock`, `/api/imagery/featured`, `/api/imagery/scenes`, `/api/agent/runs/{id}` (finished runs only) | 13:30, 14:30 | 172800 |
| BULLETIN | `/api/bulletin/*` | :30 of 02, 03, 04, 06, 07 | 86400 |
| WEEKLY | `/api/geo/*`, `/api/imagery/tilejson/*` | 03:30 every day | 604800 |
| pictures | `/api/imagery/render`, `/api/imagery/tiles/*` | `public, max-age=31536000, s-maxage=31536000, immutable` | |

- Every expiry point except the bulletin's 02:30 is also an HOURLY point, so one page view
  refreshes every expired group at once (one database wake-up): at most 21 a day.
- **Never cached by the CDN:**
  - errors (any status other than 200);
  - the headline when no block could be computed (the database is down): **503** with the same
    body, `no-store` and `Retry-After: 60`, so the CDN's `stale-if-error` keeps serving the last
    good headline;
  - `/api/health`;
  - `POST /api/agent/ask` (the SSE stream);
  - an agent run that is still in progress. `GET /api/agent/runs/{id}` with `finished_at: null`
    is sent with `no-store`.
- `/api/agent/status` (the Gemini quota, the same for every visitor) is on the HOURLY clock: the
  minute poll of an open `/live` tab is answered by the CDN and never wakes the database. Its
  budget can be up to an hour old; `POST /api/agent/ask` checks the real budget before a live run.
- Every answer has `Vary: Origin` once (CORS adds it), because the CORS headers differ per origin.
- Lifetimes add up. A response can be as old as its group's last expiry point at the CDN, then
  up to `max-age` in the browser. With `stale-while-revalidate`, the first request after expiry
  gets the old copy while the CDN refreshes it; with `stale-if-error`, the CDN keeps answering
  for a week if the API or the database fails. `generated_at` always says when the body was built,
  and each headline block's `freshness` says how old its data is.
- A headline with some (not all) blocks that could not be computed is sent
  `public, max-age=30, s-maxage=60`: retried within a minute, but not by every visitor's poll.
- The in-process cache (30-300 s per warm instance) is unchanged.
- Heavy answers (smoke trails, scar lists, boundaries, smoke validation) are kept finished in the
  database (`response_body`) until their data changes, and sent byte for byte as a live
  computation would send them; only `generated_at` is fresh. A scar list stays valid while the
  rows it selects are unchanged in the columns it shows, so each season-replay day (`until=`)
  keeps its answer when newer scars arrive. `until` on or after today is the plain list.
  `max_trails` / `stride` / `hours` variants of a smoke run are derived from the stored full
  answer, never from the run's trails again.
- **429 for new heavy variants (addition).** A heavy query whose exact parameters were never
  answered before (a smoke forward variant, a scar list with `limit` over 200 other than the live
  map's list with one `until` or `district`, a whole boundary level) counts against an allowance
  of 20 per 10 minutes per server instance; past it the answer is 429 with `Retry-After` and
  `no-store`, and no database read. Parameters the website uses are stored after their first
  answer and are never refused; neither is a known query whose data changed.

### Headline freshness (addition)

Each block of `GET /api/stats/headline` (`fires_24h`, `season`, `unseen_burns`, `delhi`, `passes`,
`gk2a_evening`) gains `freshness: {job, last_success_at, last_run_at, newest_data_at,
stale_after_hours, stale, basis}`. `stale` is true when the job that feeds the block has not
succeeded within its schedule's longest gap plus an hour (4 h for fires, CREAMS, air and GK-2A;
26 h for Sentinel-2 and element sets). `newest_data_at` is the newest observation behind the figure
(the newest FIRMS acquisition for `fires_24h`, the newest Delhi reading, the newest GK-2A scan,
the last Sentinel-2 pass); null where a block has none. Show "as of" and a stale badge from it: a
rolling 24 h count from a stopped feed otherwise just shrinks.

### CORS

- Allowed origins: `http://localhost:3000`, any origin in `CORS_ORIGINS`, and any origin that
  fully matches `CORS_ORIGIN_REGEX`.
- The deploy script sets `CORS_ORIGIN_REGEX` to `^https://blindspot[a-z0-9-]*\.vercel\.app$`.
  This covers `https://blindspot.vercel.app` and its preview URLs.

### New optional parameters and fields

| Endpoint | Parameter | Effect |
| --- | --- | --- |
| GET /api/smoke/forward, GET /api/smoke/back | `max_trails` (int, 1..10000) | Keeps at most this many trails. It takes the heaviest trail of each ensemble member in turn (ties broken by id), so both models stay represented and the same run always gives the same trails. They come back in their stored order. |
| same | `stride` (int, 1..100, default 1) | Keeps points 0, n, 2n, ... of each trail's `path` and `times`, plus the last point, so a trail still ends where the model put it. |

- With either parameter, the body gains `trails_selection: {max_trails, stride, trails_total,
  trails_returned, rule}`. Without them, the body is exactly as before.
- `GET /api/bulletin/latest` gains `available` and `reason` (see "Agent and bulletin").

### Limits

Vercel caps a function response at 4.5 MB. Every default response was measured on 2026-10-31,
05:30 IST, on live data, with the new code:

| Endpoint (default parameters) | raw bytes | gzip bytes |
| --- | --- | --- |
| GET /api/smoke/forward | 12,444,189 | 2,480,371 |
| GET /api/scars (no filters: 5000 features) | 4,546,077 | 616,368 |
| GET /api/geo/boundaries | 1,370,856 | 380,987 |
| GET /api/smoke/back | 1,364,849 | 265,300 |
| GET /api/risk (risk-v3 fields; re-measured 2026-10-31 13:40 IST on the dev database; `limit=1000`, the live map's query: 2,039,862 / 155,401) | 1,041,233 | 81,349 |
| GET /api/smoke/validation | 430,653 | 60,588 |
| GET /api/imagery/render (featured frame, JPEG) | 283,975 | (jpeg) |
| GET /api/geo/tiles/6/45/26.pbf | 142,441 | 81,699 |
| GET /api/brief/{district}/csv (every village; the largest, Gurdaspur in Punjabi, `days=30`, measured 2026-11-01 on the dev database) | 173,601 | 25,226 |
| every other endpoint | < 130,000 | < 14,000 |

- Every default response is under 4 MB after gzip. The largest is 2.5 MB.
- The API gzips every body over 1 KB when the request sends `Accept-Encoding: gzip`. Browsers
  always do.
- Two default bodies are larger than 4.5 MB uncompressed: `/api/smoke/forward` and `/api/scars`
  with no filters. A client that does not accept gzip must ask for less:
  - `/api/smoke/forward?max_trails=500&stride=2` is 1.6 MB raw (335 KB gzip).
  - `/api/smoke/back?max_trails=100&stride=2` is 327 KB raw.
  - `/api/scars?limit=500&min_confidence=0.5` is 558 KB raw.
- A request runs for at most 60 s. A picture that is not stored and has to be rendered live
  takes 1-20 s.

### Pictures on serverless

- Each function instance starts with an empty `/tmp`. Pictures are therefore looked up in three
  places, in order: the instance's disk, the `rendered_asset` table, then a live render.
- The `featured` job (daily, after `sentinel`) stores three things in `rendered_asset`:
  - the featured frames (8 JPEGs);
  - the before/after chips of the 24 largest candidate unseen burns (144 pictures);
  - the `/api/imagery/featured` payload itself.
- The job deletes stored pictures that nothing references and nobody used for 7 days.
  Live: 153 assets, 7.8 MB.
- Stored API answers live in their own table (`response_body`, see "Caching headers"), so this
  pruning never removes one; the daily `prune` job deletes stored answers nobody read for 3 days
  (the next request stores them again).
- A picture served from the database has the same bytes and the same headers as a fresh render:
  `ETag`, `X-Imagery-Source`, `X-Imagery-Collection`, `X-Imagery-Item`, `X-Fetch-Id`,
  `X-Rendered-At`, `X-Imagery-Note`. 14/14 frames and chips served by an instance with an empty
  disk matched the original disk copies byte for byte, in 7-10 ms each.
- A live render through `/api/imagery/render` is written to the database only if it is a frame of
  the current featured window or a chip of a burn scar's case file. Other crops and map tiles
  stay on the instance's disk.
- Picture URLs (`/api/imagery/featured` frames, case-file chips, tilejson) always start with the
  serving deployment's own address (`PUBLIC_BASE_URL`), even when the job that stored the featured
  manifest ran on another host.
- The bundle includes pillow and rasterio, imported only when used. A picture whose Planetary
  Computer pixels fail the plausibility gate can still be drawn from Earth Search.

### Cold start

Measured on the dev machine, Python 3.12:

- Importing `blindspot.api.main` takes 1.5-1.6 s. It was 1.59 s (`-X importtime`) / 1.70 s before
  this change. The biggest parts are fastapi (0.37 s), google-genai types (0.23 s), sqlalchemy +
  geoalchemy2 (~0.35 s) and shapely + numpy (0.11 s).
- With `INIT_DB_ON_STARTUP=false`, it took 2.2 s from process start until `/api/health`
  answered.
- First requests after that: `/api/stats/headline` 590 ms (cold database connection), then
  `/api/fires` 48 ms.
