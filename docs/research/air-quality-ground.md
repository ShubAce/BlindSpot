# Ground-truth air quality (CPCB) — Delhi-NCR, Punjab, Haryana

Research date: **2026-10-30** (IST = UTC+5:30). Legend: **[V]** = verified with a real call today;
**[U]** = UNVERIFIED (reason given). All numbers quoted below are real values returned today.

## 0. TL;DR

- **Primary live source = data.gov.in resource `3b01bcb8-…`** (CPCB's official open-data feed, GODL-India). Works
  keyless-ish with the public sample key, hourly, ~550 CAAQMS stations (3836 rows). **Two traps:** (1) the values are **CPCB AQI
  sub-indices, not µg/m³** (shown below by cross-check with the CPCB bulletin); (2) the WAF **tarpits `python-httpx`/`urllib`
  User-Agents** (read timeout, 0 bytes) — always send a custom UA. [V]
- **Best validation target = IITM DSS** `https://ews.tropmet.res.in/dss/`: it publishes the **daily % contribution of
  stubble burning to Delhi PM2.5** (season-to-date series) plus a 4-day per-source/per-district forecast. No JSON
  endpoint, but the numbers are **server-rendered inline as Chart.js arrays** → one GET + regex. [V]
  DSS's stubble share is driven by **VIIRS active-fire counts** (its own disclaimer) — i.e. exactly the satellite input
  BlindSpot says misses ~5 PM burns. Pitch: "DSS is VIIRS-blind too; we correct its fire input."
- **Secondary = OpenAQ v3** (needs free key; **401 without key** [V]). CPCB is provider **id 168** [V]; gives
  concentrations (µg/m³, 15-min) + history. Keyless **S3 archive** works but lags 3–7 days [V].
- **Tertiary / daily truth = CPCB AQI Bulletin PDF** (keyless, daily 4 PM city AQI, published ~16:05 IST, years of
  archive) [V]. **Agent context = IITM/IMD daily Delhi AQ & weather bulletin text** on `ews.tropmet.res.in` [V].
- Not usable today: CPCB CCR portal (legacy JSON 404; new portal = AES-encrypted proxy + captcha), SAFAR (TCP connect
  timeout), WAQI `demo` token (always returns Shanghai). [V]
- **Coverage reality (good pitch material):** nearest CPCB station to Sangrur is **Patiala, 51.8 km**; Punjab had
  only **7** stations reporting today vs **59** in Delhi. The burn belt is a ground-truth desert.

## 1. Recommended chain, polling and caching

| Role | Source | Key | Cadence we poll | Cache / staleness rule |
|---|---|---|---|---|
| Primary (live, station) | data.gov.in CPCB real-time AQI | personal key (sample key for dev) | hourly at **HH:40 IST** (feed refreshes ~HH:33 IST) | append-only rows keyed (station, pollutant, obs_time); serve from DB; `stale` if now − obs_time > 2.5 h |
| Secondary (µg/m³ + history) | OpenAQ v3, provider 168 | `X-API-Key` (free) | on demand by agent; latest every 30 min if primary stale | 30 min for latest; history immutable |
| Backfill (keyless) | OpenAQ S3 archive | none | nightly, days D-8..D-3 | immutable files |
| Tertiary / daily truth | CPCB AQI Bulletin PDF | none | daily 16:30 IST, retry hourly while 404 | immutable once parsed |
| Validation target | IITM DSS stubble share | none | 10:00, 14:00, 20:00 IST + on demand | store every snapshot (fetched_at, run date = first forecast label); 1 h cache |
| Agent narrative | IITM/IMD Delhi bulletin text | none | daily 12:00 IST | 6 h |
| Optional | WAQI (`aqicn`) | token | only if all above fail | label "US-EPA scale", never mix with NAQI |

Rules: one in-flight request per host; ≥1 s spacing with personal keys, ≥30 s with the shared sample key; on 429
back off 30→45→60→75→90 s (max 6 tries) [V: 429s cleared after 45–135 s today]. Store `source_url`, `fetched_at`,
payload hash with every row (provenance for the no-mock-data rule). **data.gov.in keeps no history** (only the current
24 h window) and is a **24-h rolling sub-index** (smooths spikes) → start the hourly archiver on day 1, and get the
OpenAQ key on day 1 too: hourly µg/m³ (`/v3/sensors/{id}/hours`) is what the spike-investigating agent needs.
Suggested tables (PostGIS):
```sql
create table aq_obs (source text, station text, city text, state text, geom geography(Point,4326), pollutant text,
  value double precision, unit text /* 'naqi_subindex' | 'ug/m3' | 'ppb' */, window_h int /* 24|8|1 */,
  obs_time timestamptz, feed_updated timestamptz, fetched_at timestamptz default now(),
  primary key (source, station, pollutant, obs_time));
create table dss_snapshot (fetched_at timestamptz primary key, run_date date, daily_stubble_pct jsonb, forecast_pct jsonb,
  data_sha256 text /* hash the extracted arrays: raw HTML changes every hit (visitor counter) */);
create table cpcb_bulletin (bulletin_date date, city text, aqi int, category text, prominent text[],
  stations_used int, stations_total int, primary key (bulletin_date, city));
```

## 2. PRIMARY — data.gov.in "Real time Air Quality Index from various locations" (CPCB)

### 2.1 Endpoint [V]
```
GET https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69
    ?api-key=<KEY>&format=json|csv|xml&limit=<n>&offset=<n>
    &filters[state]=Delhi&filters[pollutant_id]=PM2.5      # exact, case-sensitive, AND-combined
    &sort[station]=asc                                       # deterministic paging
    &fields=station,pollutant_id,avg_value,last_update,latitude,longitude   # projection (ids, not names)
Header: User-Agent: BlindSpot/0.1 (...)   # REQUIRED - python-httpx/urllib UAs hang until timeout
```
- Sample key (public): `579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b`. **Caps `limit` at 10**: asked
  `limit=1000`, got `"limit": 10, "count": 10` [V]. A full Delhi+Punjab+Haryana pull = 665 rows = 67 calls → use it only for dev.
- Filters: exact match, **case-sensitive** (`punjab` → `total: 0`) [V]; **no OR** (`filters[state]=Punjab,Haryana`
  → `total: 0`) [V] → one call per state. Combined `state`+`pollutant_id` works (Delhi PM2.5 → `total: 59`) [V].
- `sort[station]=asc` works [V]; `fields=` works and the response `field` list shrinks accordingly [V].
- Valid `pollutant_id` values seen: `PM2.5, PM10, NO2, SO2, CO, OZONE, NH3`. State values: `Delhi`, `Punjab`,
  `Haryana`, `Uttar Pradesh` (with a space; `Uttar_Pradesh` → 0) [V]. UP-NCR comes from `Uttar Pradesh` (74 PM2.5
  stations statewide); `filters[city]=Ghaziabad` works (8 PM2.5 stations) [V] → per-city calls or bbox client-side.
- CSV uses the *names* as headers (`pollutant_min,pollutant_max,pollutant_avg`), JSON uses the *ids*
  (`min_value,max_value,avg_value`) [V]. XML wraps the same envelope [V].
- 429 body: `{"error": "Rate limit exceeded"}`; no `Retry-After`. `X-Ratelimit-Limit: -1`, `X-Ratelimit-Remaining: -1`,
  `X-Ratelimit-Reset: 1790676954` (constant) → headers are meaningless; 429s are the shared sample key's throttle (5 in
  ~50 calls today: 3 at 4 s spacing, 2 at 30 s spacing; a 5-page pull at 30 s spacing took 122.6 s with none) [V].
  `Access-Control-Allow-Origin: *` (browser-callable, but keep the key server-side).

### 2.2 Trimmed real responses [V]
`filters[state]=Delhi&filters[pollutant_id]=PM2.5&sort[station]=asc&fields=…` at 15:47Z (`field` = name→id pairs):
```json
{"title":"Real time Air Quality Index from various locations",
 "org":["Ministry of Environment, Forest and Climate Change","Central Pollution Control Board"],
 "updated_date":"2026-10-30T15:03:00Z","total":59,"count":10,"limit":"10","offset":"0",
 "field":[{"name":"station","id":"station","type":"keyword"},{"name":"pollutant_avg","id":"avg_value","type":"keyword"}, …],
 "records":[{"station":"Alipur, Delhi - DPCC","pollutant_id":"PM2.5","avg_value":"60",
             "last_update":"30-10-2026 20:00:00","latitude":"28.815329","longitude":"77.15301"},
            {"station":"Anand Vihar, Delhi - DPCC","pollutant_id":"PM2.5","avg_value":"46", …}]}
```
Full record (no `fields=`), `filters[state]=Punjab`, after the 16:03:41Z refresh:
```json
{"country":"India","state":"Punjab","city":"Jalandhar","station":"Civil Line, Jalandhar - PPCB",
 "last_update":"30-10-2026 21:00:00","latitude":"31.321907","longitude":"75.578914",
 "pollutant_id":"NH3","min_value":"4","max_value":"6","avg_value":"5"}
```
Everything is a string; missing data is the literal `"NA"` (6 of 59 Delhi PM2.5 rows today). `last_update` is
`dd-mm-YYYY HH:MM:SS` **IST**, the end of CPCB's rolling window, and is the *batch* time (identical for every row in a
state, incl. `NA` rows) — not per-station freshness. Official metadata (Nuxt state of the dataset page): frequency
**Hourly**, granularity **Hourly**; note: *"real-time data … displayed live without human intervention … may display
some errors or abnormal values."* Licence: page footer "licensed under the Government Open Data License-India" [V].

### 2.3 The values are NAQI **sub-indices**, not concentrations [V, by cross-check]
Station AQI = max sub-index over pollutants (CPCB rule, ≥3 pollutants incl. PM2.5 or PM10). Punjab single-station
cities, data.gov.in at 21:00 IST vs official CPCB bulletin at 16:00 IST (same day):

| City | data.gov.in max sub-index (pollutant) | CPCB bulletin AQI (prominent) | If it were µg/m³ the AQI would be |
|---|---|---|---|
| Amritsar | 167 (PM2.5) | 178 (PM2.5) | ~336 (Very Poor) |
| Khanna | 164 (PM2.5) | 173 (PM2.5) | ~334 |
| Ludhiana | 100 (PM10) | 99 (PM10) | — |
| Patiala | 46 (PM10) | 48 (PM10) | — |

Also CO values of 20–105 are impossible as mg/m³. → Treat `avg_value` as the pollutant sub-index (24-h window for
PM/NO2/SO2/NH3, 8-h for CO/O3); `min/max` = extremes of the hourly sub-index over the window (inferred [U]). For
µg/m³ either invert the breakpoints (§9) or use OpenAQ.

### 2.4 Cadence and latency [V]
Observed refresh: at 15:58:47Z rows said `last_update 20:00` (`updated_date 15:03:00Z`); at **16:03:33Z rows already
said 21:00 while `updated_date` was still 15:03:00Z**; at 16:04:03Z `updated_date` = 16:03:41Z. → hourly refresh at
~HH:03 UTC (≈HH:33 IST), ~33 min after the window end; data age 33–93 min. The row flip precedes the metadata stamp
by seconds, so a multi-page pull can straddle it → restart if either `updated_date` or the newest `last_update`
changes between pages (implemented in §8). Latency: 0.34–0.96 s typical (n≈35), max 3.6 s; 429s return in ~0.25–0.42 s.

### 2.5 Coverage today [V]
- All India: `total` 3836 rows. Delhi 413 rows / **59 PM2.5 stations** (DPCC 43, IITM 10, CPCB 6); Haryana 203 rows /
  **29 PM2.5 stations** (3 `NA`); Punjab 49 rows / **7 stations** (Amritsar, Bathinda, Jalandhar, Khanna, Ludhiana,
  Mandi Gobindgarh, Patiala; each has rows for all 7 pollutants, some `NA`). Rupnagar absent (bulletin: "insufficient data").
- Nearest to test points: **Sangrur → Patiala 51.8 km, Khanna 64.6 km, Mandi Gobindgarh 64.8 km, Ludhiana 72.7 km.
  Delhi (28.61, 77.21) → Talkatora Garden 2.0 km, IMD Lodhi Road 2.6 km.**

### 2.6 Personal key [U — no account/key exists; sign-up needs email/mobile OTP]
- Flow (from the R `datagovindia` docs + portal nav): register/login at https://www.data.gov.in → "My Account"/dashboard
  → API key shown there → put in `DATA_GOV_IN_API_KEY`. Same URL, just a different `api-key`.
- What it should unlock: `limit` > 10 (all 3 states in ~3 calls, all-India 3836 rows in a few pages) and no shared-key
  429s. **The exact max `limit` and per-key quota are undocumented** — code requests 1000/page and trusts `count`/`total`,
  never assumes. Verify on first use: `limit=1000` → expect `"count" > 10`.

## 3. VALIDATION TARGET — IITM Decision Support System (DSS) for Delhi [V]

**URL:** `https://ews.tropmet.res.in/dss/` (same 77 KB page as `/dss/index.php`). Only https (the http→https redirect is
broken: `Location: https://ews.tropmet.res.indss/`). **No API/JSON/CSV**: no XHR/fetch in the page or `js/main.js`, no
data-file links, Wayback CDX for `/dss/*` lists only the HTML pages. PHP renders the data inline:
```js
const data2 = {
  labels:["2026-10-03 12:00:00","2026-10-04 12:00:00","2026-10-06 12:00:00", … ,"2026-10-30 12:00:00"],
  datasets: [{ label: 'Stubble Burning', backgroundColor: "#a31430", … stack: "Base",
      data:[0.00158,0.02979,0.32413, … ,0.97542,0.98258,1.74913] }]};
```
What the main page contains (today's extract):
1. **"Daily Mean contribution from Stubble Burning to the PM2.5 mass concentration in Delhi"** — season-to-date daily
   series, unit **% of Delhi PM2.5** (y-axis " % contribution"). Today: 27 points 2026-10-03 → 10-30 (10-05 missing);
   peak 5.21 % (10-25); last 5: 10-26 4.68, 10-27 1.56, 10-28 0.98, 10-29 0.98, **10-30 1.75**.
2. **"Daily Mean of Local and Non-Local Fractional Contribution to PM2.5 in Delhi for the next four days"** — 29 series
   (8 Delhi sectors: Energy, Transport, Industries, Waste burning, Construction, Road Dust, Residential, Other;
   19 NCR districts: Karnal … Jind; `Stubble Burning`; `Others`), labels 2026-10-30 … 11-02, each day sums to
   **100.0 %** [V]. Stubble: **1.75 → 19.16 → 33.49 → 13.40 %**. Transport 17.8 %. Page note: days 2–4 use
   *climatological* fire counts/emissions → treat them as forecasts, only day 0 as the "official" value.
   The day-0 forecast value equals the history series' last point (1.749125 vs 1.74913).

Other pages (same inline pattern; my generic extractor handles all):
- `https://ews.tropmet.res.in/ob_fire_count.php` — **hourly "Fire CO%"** (fire share of Delhi CO, 663 h, 10-02 10:00 →
  10-30 00:00, newest first, max 10.13 %), daily "CO"/"PM2.5" fire contribution (PM2.5 identical to item 1), MODIS/VIIRS
  daily fire counts 2012–2026 — **stale: 2026 series stops at 2026-05-22**.
- `dss/DSSmean.php` — 20 %/40 % emission-reduction scenarios (4 days). `dss/hourly_mean.php` — **dead** (`labels:null`,
  `data:null`, images from Oct 2025).

Semantics/caveats (from page text, [V]): stubble contribution "is determined using active fire count data from the
VIIRS satellite-based instrument received on the evening of the previous day"; source contributions use the **2021
emission inventory**. DSS is seasonal (commented-out banner: "DSS has been stopped for this season. It will resume in
September 2024"); news reports past pauses for data re-checks → detect staleness (first forecast label ≠ today IST).
Methodology citation: Govardhan et al., *GMD* 17, 2617–2640 (2024), "Decision Support System version 1.0 (DSS v1.0) for
air quality management in Delhi, India". Licence: footer "Copyright © All rights reserved … IITM, Pune" → display
values with attribution + link, don't mirror the page.

Gotchas [V]: intermittent TLS failures (`schannel: failed to receive handshake` after 22 s) and one 21 s stall; normal
1.5–1.7 s. Label typo `'Stuble Burning'` on hourly_mean.php — match `/stub+le/i`. Strip `<!-- -->` blocks first (old
disclaimers live in comments). Daily publish time **[U]**: today's run was already live at 21:20 IST; a re-fetch at
22:03 IST had identical arrays (only the visitor counter changed); poll 3×/day.

## 4. SECONDARY — OpenAQ v3

- **Key required for data endpoints**: `/v3/locations…`, `/v3/providers`, `/v3/locations/tiles/…` → `401 {"message":
  "Unauthorized. A valid API key must be provided in the X-API-Key header."}` [V]; only `/openapi.json` and `/docs` are
  keyless [V]. v1/v2 → `410 Gone … Please migrate to Version 3 endpoints.` [V].
- Free key: https://explore.openaq.org/register → account page; header `X-API-Key`. Free tier **60 req/min, 2,000
  req/h**; headers `x-ratelimit-used/limit/remaining/reset`; 429 on excess; repeated abuse → ban (docs.openaq.org).
- Endpoints (from the live keyless `https://api.openaq.org/openapi.json`, version 3.0.0 [V]; responses [U] — no key):
  ```
  GET /v3/locations?coordinates=28.61,77.21&radius=25000&limit=100     # lat,lon ; radius m, max 25000
  GET /v3/locations?bbox=73.8,29.5,77.6,32.6&providers_id=168&limit=1000   # minLon,minLat,maxLon,maxLat; not with coordinates
  GET /v3/locations?iso=IN&providers_id=168&parameters_id=2&page=2
  GET /v3/locations/{id}/latest        GET /v3/locations/{id}/sensors
  GET /v3/sensors/{id}/hours?datetime_from=2026-10-27T00:00:00Z&datetime_to=…   (also /measurements, /days)
  GET /v3/parameters/2/latest?datetime_min=…   # pm25 = parameter 2 (no geo filter on this one)
  ```
- **CPCB is a provider: id 168, name "CPCB"** [V via server-rendered explore.openaq.org/locations/50 and /235]:
  50 = "Punjabi Bagh, Delhi - DPCC" (28.674045, 77.131023, datetimeLast 2026-10-30T14:30Z = 20:00 IST — same freshness as
  data.gov.in), 235 = "Anand Vihar, New Delhi - DPCC". `licenses: null` for CPCB locations (no explicit licence;
  attribute "CPCB via OpenAQ"). 8118 = "New Delhi" US Embassy (provider AirNow, US Public Domain). Punjab/Haryana
  coverage on OpenAQ **[U]** (needs key: `bbox` query above).
- **Keyless S3 archive** [V]: `https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/locationid={id}/year=2026/month=10/location-{id}-{YYYYMMDD}.csv.gz`
  (list with `?list-type=2&prefix=records/csv.gz/locationid=50/year=2026/month=10/`). Lag: loc 50 newest file 10-23,
  loc 8118 newest 10-26 (written 2026-10-29T18:30Z) → 3–7 days. Columns
  `location_id,sensors_id,location,datetime,lat,lon,parameter,units,value`; CPCB rows are **15-min µg/m³/ppb**, e.g.
  `50,12234795,"Punjabi Bagh, Delhi - DPCC-3379482","2026-10-23T00:15:00+05:30",…,"pm10","µg/m³","331.0"`; PM2.5 that day
  ranged 63–227 µg/m³. Parse with `csv` (names contain commas).

## 5. TERTIARY — CPCB daily AQI Bulletin PDF [V]
`GET https://www.cpcb.gov.in/upload/Downloads/AQI_Bulletin_{YYYYMMDD}.pdf` — keyless, 0.2–0.9 s, ~290 KB, 14 pages.
`Last-Modified` 10-30 10:35:57Z and 10-29 10:35:53Z → **published ~16:05 IST**; before that the URL returns **404**
(10-31 tested). Archive works (2025-11-05 → 200). Content: "Air Quality Index on Oct 30, 2026 @ 4 PM (Average of past
24 hours)", one row per city (256 today) + "cities absent … insufficient data" list. `pdfplumber` `extract_tables()`
gives clean rows (parser in §8). Today: **Delhi 60 Satisfactory (53/59 stations)**; yesterday **Delhi 222 Poor (58/59)** —
rain on 10-29/30 (IITM bulletin) explains the drop. Punjab/Haryana today: Amritsar 178, Khanna 173, Sonipat 125,
Ludhiana 99, Jalandhar 90, Panipat 88, Patiala 48, Bathinda 42, Jind 28. City AQI = mean of station AQIs (bulletin note).
Attribution "CPCB National AQI Bulletin"; no explicit licence text checked on cpcb.gov.in [U].

## 6. Agent context — IITM/IMD "Air Quality and Weather Bulletin for Delhi" [V]
`https://ews.tropmet.res.in/` (165 KB, 0.5 s) carries the daily bulletin as HTML text, e.g. *"The air quality over Delhi
was in the Poor category with CPCB AQI 222 at 4 PM on 29.10.2026 … Moderate rain accompanied with thunderstorm … surface
wind speeds up to 16 kmph gusting to 36 kmph from the northwest …"* plus next-days AQI category outlook. Strip tags,
slice from `1. Past Weather and Air Quality Observation`. Forecast maps are PNGs only (`data/AQI_data/forecast/pm2.5/images/d02/{n}.png`); `download.php` → 404.

## 7. Checked and rejected [V]
- **CPCB CCR / AQI_India portal**: `app.cpcbccr.com/*` → Cloudflare 308 (≈22 s) to `airquality.cpcb.gov.in`, now one
  Angular SPA (`/ccr/main-XMSR42BH.js` + 44 chunks). Legacy JSON endpoints `aqi_dashboard/aqi_all_Parameters` and
  `aqi_station_all_india` → **404**. Data now flows through `insight_proxy_api_v2_0/service.php` (GET → 500) with
  `createEncryptedRequest`/`decryptResponse` (AES, client ids/secrets in the bundle) and captcha verification → not a
  public API; do not reverse-engineer. Use data.gov.in (CPCB's sanctioned open channel) + bulletin instead.
- **SAFAR** `safar.tropmet.res.in` (103.251.186.104): TCP connect timeout on 80 and 443 (21 s). Treat as dead.
- **WAQI/aqicn**: `token=demo` returns Shanghai (idx 1437) for `geo:28.61;77.21` and `delhi` alike; `/v2/map/bounds` →
  `"Invalid key"` [V]. Unverified (no token): free token at https://aqicn.org/data-platform/token/, values on the
  **US-EPA AQI** scale re-computed from CPCB raw data, attribution required by WAQI terms [U] → last resort only, never
  mixed with NAQI.

## 8. Tested code
The python blocks in §8 and §9 were extracted **verbatim from this file** and executed: the network functions against
the live endpoints on 2026-10-30 16:27–16:29Z (`fetch_cpcb_stations("Punjab")` → 49 rows / 5 pages in 122.6 s on the
sample key; DSS 1.7 s; bulletin 256 cities), the pure functions offline on the saved real rows. Needs only `httpx`
(already a dependency) + `pdfplumber` for the bulletin.
```python
import os, re, time, datetime as dt, httpx
UA = {"User-Agent": "BlindSpot/0.1 (+hackathon research)"}   # python-* UAs are tarpitted by the WAF
DGI_URL = "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_ist = lambda s: dt.datetime.strptime(s, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)

def _get(client, params):
    for wait in (0, 30, 45, 60, 75, 90):                 # spaced backoff for shared-key 429s
        time.sleep(wait)
        r = client.get(DGI_URL, params=params)
        if r.status_code == 429:
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("data.gov.in: still 429 after 6 tries")

def fetch_cpcb_stations(state, pollutant=None, key=None):
    key = key or os.getenv("DATA_GOV_IN_API_KEY") or SAMPLE_KEY
    page = 10 if key == SAMPLE_KEY else 1000             # sample key silently caps at 10
    base = {"api-key": key, "format": "json", "limit": page, "filters[state]": state, "sort[station]": "asc"}
    if pollutant:
        base["filters[pollutant_id]"] = pollutant
    rows, offset, stamp, batch, restarts = [], 0, None, None, 0
    with httpx.Client(headers=UA, timeout=30) as c:
        while True:
            d = _get(c, {**base, "offset": offset})
            newest = max((_ist(r["last_update"]) for r in d.get("records", [])), default=None)
            # rows flip seconds BEFORE updated_date is re-stamped -> watch both, restart on change
            if stamp and restarts < 3 and (d["updated_date"] != stamp or (newest and batch and newest > batch)):
                rows, offset, stamp, batch, restarts = [], 0, None, None, restarts + 1
                continue
            stamp, batch = d["updated_date"], batch or newest
            rows += d.get("records", [])
            offset += int(d["count"]) or page
            if offset >= int(d["total"]) or not d.get("records"):
                break
            if key == SAMPLE_KEY:
                time.sleep(30)
    num = lambda v: None if v == "NA" else int(v)
    return [{"station": r["station"].strip(), "city": r["city"], "state": r["state"],
             "lat": float(r["latitude"]), "lon": float(r["longitude"]), "pollutant": r["pollutant_id"],
             "subindex_avg": num(r["avg_value"]), "subindex_min": num(r["min_value"]), "subindex_max": num(r["max_value"]),
             "obs_time_utc": _ist(r["last_update"]).astimezone(dt.timezone.utc).isoformat(), "feed_updated": stamp}
            for r in rows]

def fetch_dss_stubble():
    with httpx.Client(headers=UA, timeout=60) as c:
        for _ in range(3):                                # flaky TLS on ews.tropmet.res.in
            try:
                html = c.get("https://ews.tropmet.res.in/dss/").text
                break
            except httpx.TransportError:
                time.sleep(20)
        else:
            raise RuntimeError("DSS unreachable")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    charts = []                                           # [(labels, {series: values})]
    for m in re.finditer(r"labels\s*:\s*\[([^\]]*)\]|label\s*:\s*'([^']+)'[^{}]*?data\s*:\s*\[([^\]]*)\]", html, re.S):
        if m.group(1) is not None:
            charts.append((re.findall(r'"([^"]+)"', m.group(1)), {}))
        elif charts:
            charts[-1][1][m.group(2)] = [float(x) for x in m.group(3).split(",") if x.strip()]
    hist = next(c for c in charts if len(c[1]) == 1 and "Stubble Burning" in c[1])
    fc = next(c for c in charts if "Stubble Burning" in c[1] and "Others" in c[1])
    return {"daily_stubble_pct": dict(zip([l[:10] for l in hist[0]], hist[1]["Stubble Burning"])),
            "forecast_pct": {lab: {k: round(v[i], 3) for k, v in fc[1].items()} for i, lab in enumerate(fc[0])}}
```
Output of the verbatim run (sample key):
```
fetch_cpcb_stations(Punjab): 49 rows in 122.6s, feed=2026-10-30T16:03:41Z, obs=['2026-10-30T15:30:00+00:00']
   Golden Temple, Amritsar - PPCB {'aqi': 167, 'prominent': ['PM2.5']}
   Punjab Agricultural University, Ludhiana - PPCB {'aqi': 100, 'prominent': ['PM10']}      (… 5 more)
fetch_dss_stubble: 1.7s; n_days=27; last=('2026-10-30', 1.74913);
   forecast={'2026-10-30': 1.749, '2026-10-31': 19.162, '2026-11-01': 33.486, '2026-11-02': 13.404}
parse_bulletin: 256 cities; [{'city': 'Delhi', 'category': 'Satisfactory', 'aqi': 60, 'prominent': ['CO'],
   'stations_used': 53, 'stations_total': 59}, …]
```
Same paging logic in a scratch script, `Haryana`+`PM2.5` (3 pages): 29 rows / 29 stations in 61.7 s; the 4-day
forecast shares summed to 100.0 % on every day.
Station AQI (CPCB rule) — tested on all 49 Punjab rows (reproduces the §2.3 table):
```python
def station_aqi(rows):
    by = {}
    for r in rows:
        if r["subindex_avg"] is not None:
            by.setdefault(r["station"], {})[r["pollutant"]] = r["subindex_avg"]
    out = {}
    for s, p in by.items():
        ok = len(p) >= 3 and ("PM2.5" in p or "PM10" in p)
        aqi = max(p.values()) if ok else None
        out[s] = {"aqi": aqi, "prominent": [k for k, v in p.items() if v == aqi] if ok else []}
    return out
```
CPCB bulletin parser (tested on 2026-10-30, 10-29 and 2025-11-05 PDFs; needs `pdfplumber` — add with `uv add` when
the team agrees; I did not touch pyproject):
```python
import pdfplumber, re
def parse_bulletin(path):                       # -> [{"city","category","aqi","prominent","stations_used","stations_total"}]
    rows = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            for t in pg.extract_tables():
                for r in t:
                    if not r or not (r[0] or "").strip().isdigit():
                        continue
                    _, city, cat, aqi, pol, st = (c or "" for c in r[:6])
                    names = [p.strip() for p in pol.split("\n")[0].split(",")]   # 'PM , PM\n2.5 10'
                    subs, pols, k = (pol.split("\n")[1].split() if "\n" in pol else []), [], 0
                    for n in names:
                        if n in ("PM", "O", "NO") and k < len(subs):
                            pols.append(n + subs[k]); k += 1
                        else:
                            pols.append(n)
                    m = re.match(r"(\d+)/(\d+)", st.strip())
                    rows.append({"city": " ".join(city.split()), "category": cat.strip(),
                                 "aqi": int(aqi) if aqi.strip().isdigit() else None, "prominent": pols,
                                 "stations_used": int(m[1]) if m else None, "stations_total": int(m[2]) if m else None})
    return rows
```

## 9. Sub-index → µg/m³ (CPCB NAQI, report CUPS/82/2014-15, Table 3.11 [V: PDF at airquality.cpcb.gov.in/ccr_docs/FINAL-REPORT_AQI_.pdf])

| Category (index) | PM10 24h | PM2.5 24h | NO2 24h | O3 8h | CO 8h (mg/m³) | SO2 24h | NH3 24h |
|---|---|---|---|---|---|---|---|
| Good (0–50) | 0–50 | 0–30 | 0–40 | 0–50 | 0–1.0 | 0–40 | 0–200 |
| Satisfactory (51–100) | 51–100 | 31–60 | 41–80 | 51–100 | 1.1–2.0 | 41–80 | 201–400 |
| Moderate (101–200) | 101–250 | 61–90 | 81–180 | 101–168 | 2.1–10 | 81–380 | 401–800 |
| Poor (201–300) | 251–350 | 91–120 | 181–280 | 169–208 | 10.1–17 | 381–800 | 801–1200 |
| Very Poor (301–400) | 351–430 | 121–250 | 281–400 | 209–748 | 17.1–34 | 801–1600 | 1201–1800 |
| Severe (401–500) | 430+ | 250+ | 400+ | 748+ | 34+ | 1600+ | 1800+ |

Invert piecewise-linearly (tested: Amritsar PM2.5 index 167 → 80.1 µg/m³, Khanna 164 → 79.2, Patiala 12 → 7.2):
```python
BP = {"PM2.5": [(0, 30, 0, 50), (30, 60, 50, 100), (60, 90, 100, 200), (90, 120, 200, 300), (120, 250, 300, 400), (250, 380, 400, 500)],
      "PM10":  [(0, 50, 0, 50), (50, 100, 50, 100), (100, 250, 100, 200), (250, 350, 200, 300), (350, 430, 300, 400), (430, 510, 400, 500)]}
def subindex_to_conc(pollutant, idx):          # (C_lo, C_hi, I_lo, I_hi) segments, ug/m3, 24-h
    for c_lo, c_hi, i_lo, i_hi in BP.get(pollutant, []):
        if idx <= i_hi:
            return round(c_lo + (idx - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)
    return None
```
Severe is open-ended (380 µg/m³ for PM2.5 and 510 for PM10 at index 500 are my assumption; report index > 400 as
"≥ 250 µg/m³"). Accuracy ≈ integer rounding of the index; it is still a 24-h average, so for hourly smoke-arrival
analysis use OpenAQ concentrations.

## 10. Latency measured today
data.gov.in 0.34–0.96 s (worst 3.6 s; 20 s hang with a python UA) · DSS 1.5–1.7 s (worst 21 s stall / 22 s TLS fail) ·
AQEWS 0.5 s · CPCB bulletin 0.2–0.9 s · OpenAQ 401 0.4 s, S3 list 1.0–1.8 s, S3 file 1.5 s · explore.openaq.org 1.4–1.9 s ·
WAQI demo 0.8–1.8 s.

## 11. Still UNVERIFIED (and how to close)
1. data.gov.in personal-key max `limit` and quota — no key; first call with a key: `limit=1000`, check `count`.
2. OpenAQ v3 response shapes and Punjab/Haryana CPCB coverage — no key; run the `bbox` + `providers_id=168` query.
3. DSS daily publish time and whether past daily values get revised — single-day observation; snapshot 3×/day.
4. data.gov.in `min/max` semantics (assumed: extremes of hourly sub-index over the 24-h window).
5. WAQI behaviour with a real token.
