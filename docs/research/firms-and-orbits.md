# FIRMS active fires + overpass prediction (the "blind-spot clock")

Researched and tested live on **2026-10-30, 15:45–17:30 UTC (21:15–23:00 IST)**. Every endpoint below was called today
from the dev box (Windows, uv env). Anything I could not call is marked **UNVERIFIED** with the reason. The scratch
scripts that produced the numbers are not in the repo and may be wiped:
`C:\Users\shubh\AppData\Local\Temp\claude\f--forge\375401be-2a9f-45a8-acfb-c0a0fdbd6c12\scratchpad\firms-orbits\{scripts,js}`.
The two snippets in section 6 are self-contained copies of them.

## TL;DR for implementers
- **Keyless live feed works**: 12 South Asia CSVs (4 sensors x 24h/48h/7d), 42 KB to 1.2 MB each, 1.6–2.6 s to download.
  Files are **rewritten every ~60 min** but the **ETag (=MD5) only changes when the content changes**. Poll with `HEAD`,
  `GET` only on ETag change. Conditional GET is ignored (always 200 + full body). **No CORS header**: proxy via FastAPI.
- `acq_date`/`acq_time` are **UTC** (FIRMS attribute docs say so; our SGP4 model agrees with the labels to within
  -2.0..+3.2 min for 7,926 detections, so an IST interpretation, off by 5.5 h, is ruled out). The labels come in
  ~2-minute steps, so treat them as **±3 min**.
- **Today (IST date 2026-10-30, files fetched 21:15 IST and re-fetched 21:45 IST)**: Punjab **227** VIIRS detections (NOAA-20 127, NOAA-21 43,
  S-NPP 57, MODIS 0); Haryana **0**. 7-day Punjab: **98.8 % daytime** detections. Across 7 days, Punjab and Haryana
  had only 1 detection (Terra, 20:56) between **16:35 and 01:19 IST**. That gap exists because almost no satellite
  passes then, not because fires stop (section 3.4).
- **Aqua and Terra are no longer at 13:30/10:30.** Live TLEs give Aqua LTAN **15:47** and Terra LTAN **20:34** (so it
  descends at ~08:34). Over Punjab, Aqua MODIS passes at **15:07–16:37 IST**, the latest daytime look before the ~17:00
  burning peak. After Aqua, the next look is Terra at night, **19:54–21:15 IST**. VIIRS (375 m) only covers
  **12:30–14:47 IST** and **01:13–03:30 IST**.
- **Orbit model validated**: the skyfield/SGP4 time of closest approach matches FIRMS `acq_time` within ±2.3 min over
  Punjab. It matches Sentinel-2 tile `SENSING_TIME` within **1–4 s**. satellite.js 7.1.0 reproduces the skyfield passes
  within 1 s and 0.1 km.
- A **MAP_KEY** (free, emailed) is needed for the area API, which we use to backfill the 2026-09-15 to today season (40 calls).
  The key is not issued yet, so the area API is **UNVERIFIED** beyond its error responses.

---
## 1. FIRMS keyless regional files

### 1.1 URLs (all HTTP 200 today)
```
https://firms.modaps.eosdis.nasa.gov/data/active_fire/{dir}/csv/{prefix}_South_Asia_{24h|48h|7d}.csv
  dir/prefix:  noaa-20-viirs-c2/J1_VIIRS_C2 | noaa-21-viirs-c2/J2_VIIRS_C2 | suomi-npp-viirs-c2/SUOMI_VIIRS_C2 | modis-c6.1/MODIS_C6_1
  also: .../{dir}/shapes/zips/{prefix}_South_Asia_24h.zip (151 KB)   .../{dir}/kml/{prefix}_South_Asia_24h.kml (3.46 MB)
  Global equivalent: {prefix}_Global_24h.csv (J1 = 7.26 MB; don't use)
File index (keyless JSON): https://firms.modaps.eosdis.nasa.gov/api/active_fire_files/all?format=json   (43 KB, 1.6 s)
Region boxes (keyless JSON): https://firms.modaps.eosdis.nasa.gov/api/coordinates/all?format=json
  -> "South Asia": {"area":"54 5.5, 102 40","filename":"South_Asia"}   (W S, E N; covers Iran..Thailand)
```
| file (fetched 15:45Z) | bytes | rows | acquisition span (UTC) | Last-Modified |
|---|---|---|---|---|
| J1 (NOAA-20) 24h / 48h / 7d | 325,549 / 432,643 / 1,243,151 | 3,932 / 5,223 / 15,012 | 10-29 06:42 → 10-30 09:54 (24h file spans 27 h) | 15:42:19Z |
| J2 (NOAA-21) 24h / 48h / 7d | 125,406 / 238,153 / 943,195 | 1,511 / 2,870 / 11,388 | 10-29 05:46 → 10-30 08:56 | 15:44:50Z |
| SUOMI (S-NPP) 24h / 48h / 7d | 155,932 / 319,570 / 1,026,483 | 1,924 / 3,953 / 12,699 | 10-29 06:22 → 10-30 09:34 | 14:49:53Z |
| MODIS C6.1 (Aqua+Terra) 24h / 48h / 7d | 42,646 / 55,489 / 151,548 | 559 / 726 / 1,991 | 10-29 02:49 → 10-30 12:49 | 14:47:20Z |

The 24h/48h/7d windows are **not** rolling from "now". The 24h file held 27 h of acquisitions, and its content
(MD5) did not change across hourly rewrites while no new passes arrived. Checked: 24h ⊂ 48h ⊂ 7d row-for-row, with 0
duplicates on `(lat, lon, acq_date, acq_time, satellite)` within a file. Always filter by `acq_date`/`acq_time`
yourself and upsert on that key.

### 1.2 Columns and real rows (trimmed)
```
VIIRS: latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
30.27566,75.60682,338.23,0.38,0.36,2026-10-30,0809,N20,nominal,2.0NRT,298.23,3.76,D     <- 30 km W of Sangrur
30.97428,76.13415,350.48,0.42,0.61,2026-10-30,0714,N21,nominal,2.0NRT,294.34,7.27,D
29.53142,74.53265,341.02,0.5,0.49,2026-10-30,0750,N,nominal,2.0NRT,295.14,6.03,D
MODIS: latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,confidence,version,bright_t31,frp,daynight
30.08173,74.96458,310.56,1.36,1.15,2026-10-29,1045,A,63,6.1NRT,295.42,10.72,D
```
- `acq_time` is `HHMM` **UTC** and must be read as a string (zero-pad it; `0809`). The attributes page says "Time of
  acquisition/overpass of the satellite (in UTC)". `satellite`: `N`=S-NPP, `N20`, `N21`, `A`=Aqua, `T`=Terra.
- `confidence`: VIIRS uses the words `low|nominal|high`. MODIS uses an integer 0–100, classed as low <30, nominal
  30–79, high ≥80 (MODIS C6/C6.1 Fire User Guide v1.0, Table 11, p.51; checked in the PDF).
- `scan`/`track` give the pixel size in km. It grows off-nadir: in the 7d files, the VIIRS max is 0.80 x 0.78 and the
  MODIS max is 4.82 x 2.0. `frp` is in MW.
  `daynight` is D or N. NRT has no `type` (static-source) field; that field only exists in the SP MCD14ML data.

### 1.3 Cadence, latency, caching (measured)
- **Rewrite cadence:** Last-Modified advanced about hourly at ~:40–:50 past the hour (MODIS 14:47:20 → 15:46:45; SUOMI
  14:49:53 → 15:49:33). The ETag stayed identical when no new passes arrived. The FAQ says "updated every 60 minutes".
- `If-None-Match` and `If-Modified-Since` both returned **200 with the full body**, so conditional GET does not work. A `HEAD` takes ~1.1 s.
- **No `Access-Control-Allow-Origin`** on the CSVs (tested with an `Origin` header), so the browser cannot fetch them.
- **Latency:** FAQ: NRT is available "within 3 hours of a satellite observation (on a best-effort basis)". RT/URT exist
  only for US/Canada. The newest NOAA-20 row at 15:42Z was 09:54Z, which is just the region's last daytime VIIRS pass.
  The orbit model gives the box coverage on 10-30 (UTC): VIIRS daytime 05:28–10:33, then nothing until 17:54 (NOAA-21),
  18:27 (S-NPP) and 18:46 (NOAA-20). Aqua 08:02–11:27; Terra night 12:48, 14:18–14:33 and 15:57–16:12. So "newest row"
  is not "latency". The poller measurement is in section 1.6.

### 1.4 Punjab / Haryana today
Polygons come from geoBoundaries `gbOpen/IND/ADM1` (DataMeet/ECI, CC BY 2.5 IN). Match on `shapeISO` (`IN-PB`,
`IN-HR`), because `shapeName` carries diacritics (`Haryāna`). Bounds: PB `73.880,29.542,76.939,32.513`, HR
`74.474,27.653,77.604,30.929`.

**IST date 2026-10-30** (a UTC-date filter gives the same except NOAA-21 = 42; its 01:25 IST row is UTC 10-29):

| state | NOAA-20 | NOAA-21 | S-NPP | MODIS | total | day/night | FRP MW mean / median / p90 / max | confidence (low/nominal/high) |
|---|---|---|---|---|---|---|---|---|
| Punjab | 127 | 43 | 57 | 0 | **227** | 226 D / 1 N | all 4.70 / 3.78 / 8.40 / 23.36, sum 1066 MW (N20 3.97/3.28/8.17/11.83; N21 6.43/5.67/10.42/23.36; SNPP 5.01/4.08/8.69/21.54) | all 2/222/3 (N20 2/125/0, N21 0/41/2, SNPP 0/56/1) |
| Haryana | 0 | 0 | 0 | 0 | **0** | – | – | – |

**7 days by IST date** (Punjab | Haryana; columns N20/N21/SNPP/MODIS; 10-23 is partial):
`10-23 15/28/41/24 | 1/3/6/3`, `10-24 57/26/51/1 | 7/7/14/0`, `10-25 83/34/16/2 | 34/10/11/0`,
`10-26 75/74/65/87 | 14/8/8/11`, `10-27 60/128/64/2 | 6/20/8/0`, `10-28 129/118/145/32 | 14/29/20/0`,
`10-29 126/57/171/172 | 7/3/8/0`, `10-30 127/43/57/0 | 0/0/0/0`.
- 7-day day/night split: Punjab **2085 D / 25 N**; Haryana 165 D / 87 N. Most of Haryana's N rows are persistent
  industrial or urban sources: `28.35N 77.23E` (12 detections on 4 nights), `28.71N 76.86E` (11 on 5), `29.12N 75.78E`
  (7 on 4). **Mask cells that recur on ≥3 nights.**
- 7-day FRP (MW) mean/median/p90/max: N20 4.59/3.55/8.96/42.25; N21 5.31/4.36/9.14/37.85; SNPP 4.30/3.53/8.12/23.18;
  MODIS 13.00/9.97/24.08/104.03. MODIS has fewer detections and higher FRP per detection because its 1 km pixels only
  catch big fires.
- **Histogram of detection time (IST hour, PB+HR, 7 d, n=2,362):** `01h:35  02h:75  03h:1  09h:11  12h:146  13h:1459
  14h:312  15h:148  16h:174  20h:1`. Every other hour has **0**.
  Per satellite: S-NPP D 12:35–14:37, N 01:19–03:18 · NOAA-20 D 12:37–14:35, N 01:38–02:59 · NOAA-21 D 12:35–14:35,
  N 01:19–02:59 · Aqua D **15:19–16:34** (322 det.) · Terra D 09:21–09:42, N 20:56.

### 1.5 Gotchas
- Multi-sensor duplicates: the same field can be seen by S-NPP, N20 and N21 20–50 min apart. Keep them as separate
  observations (useful as a persistence signal). Dedupe key: `(source, lat, lon, acq_date, acq_time)` across the 24h, 48h and 7d files.
- Swath-edge blindness: on 10-30, Aqua had Sangrur at 947 km off-track (pixel ~4–5 km) and FIRMS shows 0 Punjab
  MODIS rows. "In swath" does not mean "able to see a 1-ha fire". Weight observations by `scan*track`.
- The South Asia box includes Pakistan, Iran, Myanmar and Thailand, so always clip to state polygons.

### 1.6 Latency poll result
See the poller log summary appended at the end of this doc (section 7).

---
## 2. FIRMS area API (needs MAP_KEY) and NRT daily archive

### 2.1 Endpoints
```
GET https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{W,S,E,N|world}/{DAY_RANGE}[/{YYYY-MM-DD}]
    no DATE  -> "most recent data, from TODAY to TODAY-(DAY_RANGE-1)";  DATE -> [DATE .. DATE+DAY_RANGE-1]
    DAY_RANGE: 1..5 ("number of days to query at one time", page text today)
    SOURCE: VIIRS_NOAA20_NRT | VIIRS_NOAA21_NRT | VIIRS_SNPP_NRT | MODIS_NRT | LANDSAT_NRT (US/Canada only)
            VIIRS_NOAA20_SP | VIIRS_NOAA21_SP | VIIRS_SNPP_SP | MODIS_SP   (standard/science quality; replaces NRT after 2–5 months)
    *_NRT includes RT/URT where available (US/Canada); RT/URT rows are removed when NRT arrives or after 6 h.
GET https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{MAP_KEY}/{ALL|<SOURCE>}   -> date availability per source (CSV shape UNVERIFIED)
GET https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/?MAP_KEY={MAP_KEY}            -> key status / usage
GET https://firms.modaps.eosdis.nasa.gov/api/missing_data/ , /api/kml_fire_footprints/       (documented; not needed)
country / countries APIs: listed as "Feature currently not available" on /api/ (API v4.2.5).
```
Responses observed today with a syntactically valid fake key (`0`*32):

| call | HTTP | body |
|---|---|---|
| area | 400 `text/plain` | `Invalid MAP_KEY.` |
| data_availability | 401 | `Invalid MAP_KEY.` |
| mapkey_status | 403 `text/html` | `MAP_KEY is invalid or your have exceeded your transaction/time limit. Please try again later.` |
| country | 400 | `Invalid API call.` |

Latency ~1.1 s each. With a real key, mapkey_status is said to return JSON
`{transaction_limit, current_transactions, transaction_interval}` (field names from Microsoft's connector docs).
**UNVERIFIED (no key).**
- **MAP_KEY**: free. Enter an email at https://firms.modaps.eosdis.nasa.gov/api/map_key/ and the key arrives by email.
  The site's JS validates `^[0-9a-z]{32}$`. You can't re-register the same email (manage it at `/download`). Put it in `.env` as `FIRMS_MAP_KEY`.
- **Limits**: "5000 transactions / 10-minute interval. Larger transactions may count as multiple requests (ex.
  requesting 7 days)". There is no published formula, so assume 1 transaction per day per call and stay far below the limit.
- **Area-API CSV columns: UNVERIFIED.** Expected to match the files plus possibly an `instrument` column and VIIRS
  confidence as `l/n/h`. Parse by header name and normalise `l→low, n→nominal, h→high`.

### 2.2 Season backfill 2026-09-15 → today (live call UNVERIFIED: needs key. I dry-ran the loop offline with `httpx.MockTransport`: it produced 40 URLs, `.../5/2026-09-15` … `.../1/2026-10-30`)
46 days in 5-day chunks is 10 calls per source, so 4 NRT sources need **40 calls**. That is ~184 transactions if every
day counts as one, versus the 5000 limit. Use the PB+HR bbox `73.8,27.6,77.7,32.6` and clip to polygons afterwards. SP
normally lags NRT by 2–5 months (FIRMS archive note), so expect NRT only for this season; confirm with
`data_availability`.
```python
import io, time, httpx, pandas as pd
from datetime import date, timedelta
AREA = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{k}/{src}/73.8,27.6,77.7,32.6/{n}/{d}"
def backfill(key, start=date(2026, 9, 15), end=None):
    end, out = end or date.today(), []
    with httpx.Client(timeout=120) as c:
        for src in ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT"):
            d = start
            while d <= end:
                n = min(5, (end - d).days + 1)
                r = c.get(AREA.format(k=key, src=src, n=n, d=d.isoformat()))
                if r.status_code != 200 or r.text.startswith(("Invalid", "<")):
                    raise RuntimeError(f"{src} {d}: {r.status_code} {r.text[:120]}")   # stop, don't retry-loop
                out.append(pd.read_csv(io.StringIO(r.text), dtype={"acq_time": str}).assign(source=src))
                d += timedelta(days=n); time.sleep(1)
    return pd.concat(out).drop_duplicates(["source", "latitude", "longitude", "acq_date", "acq_time"])
```

### 2.3 LANCE NRT daily text files (alternative backfill; needs Earthdata Login)
- The directory listings are keyless (HTTP 200, ~1.9 s) and hold **61 daily files**, DOY 243–303 (2026-08-31 → 10-30):
  `https://nrt3.modaps.eosdis.nasa.gov/archive/FIRMS/{noaa-20-viirs-c2|noaa-21-viirs-c2|suomi-npp-viirs-c2|modis-c6.1}/South_Asia/`
- File names: `J1_VIIRS_C2_South_Asia_VJ114IMGTDL_NRT_2026303.txt`, `J2_..._VJ214IMGTDL_NRT_`, `SUOMI_..._VNP14IMGTDL_NRT_`,
  `MODIS_C6_1_South_Asia_MCD14DL_NRT_`. The listing HTML embeds `window.laadsArchiveFiles=[{"last-modified":"2026-10-30 15:00","name":...,"size":...}]`.
- An unauthenticated download returns **303 → `/profiles/licenses/archive/...`** (login). Use a free Earthdata token
  (urs.earthdata.nasa.gov → Generate Token) with `Authorization: Bearer <token>`. **UNVERIFIED (no token).** The FAQ
  calls these daily files "the first place data are posted". But at 16:24Z the listed DOY-303 sizes matched the 10-30
  rows already in the regional 24h CSVs (+1 byte/row, CRLF): J2 52,594 B = 626 rows, SUOMI 44,045 = 536, MODIS 5,225 =
  66, J1 171.2 KB = 2,095. So I saw no latency advantage at that check.

---
## 3. Orbits: CelesTrak GP + skyfield

### 3.1 GP endpoints and verified NORAD IDs
`GET https://celestrak.org/NORAD/elements/gp.php?CATNR={id}&FORMAT=TLE|JSON` returned 200 for all 8, 168 B TLE each,
1.3–2.1 s. The IDs were confirmed via `?NAME=...&FORMAT=JSON` on CelesTrak itself. Use CATNR, because NAME=TERRA also
returns TerraSAR-X, SkyTerra-1 and others, and NAME=SUOMI also returns SUOMI-100. **Always pass FORMAT**: the default
became CSV on 2026-05-09.

| NORAD | CelesTrak OBJECT_NAME | intl des. | TLE epoch (UTC) | mean alt km | LTAN (LMST) |
|---|---|---|---|---|---|
| 37849 | SUOMI NPP | 2011-061A | 2026-10-29 23:13 | 838.8 | 13:38 |
| 43013 | NOAA 20 (JPSS-1) | 2017-073A | 2026-10-29 18:29 | 838.7 | 13:31 |
| 54234 | NOAA 21 (JPSS-2) | 2022-150A | 2026-10-29 22:36 | 838.7 | 13:24 |
| 27424 | AQUA | 2002-022A | 2026-10-29 22:11 | 697.8 | **15:47** (drifted from 13:30) |
| 25994 | TERRA | 1999-068A | 2026-10-29 22:27 | 701.1 | **20:34** asc = 08:34 desc (from 10:30) |
| 40697 / 42063 / 60989 | SENTINEL-2A / 2B / 2C | 2015-028A / 2017-013A / 2024-157A | 10-29 23:16 / 23:06 / 15:33 | 800.9 | 22:30 asc = 10:30 desc |

Sample TLE (`CATNR=27424&FORMAT=TLE`):
```
AQUA
1 27424U 02022A   26302.92450185  .00000386  00000+0  86456-4 0  9991
2 27424  98.4470 275.0309 0000827  96.2468 286.8398 14.62255676302969
```
JSON is the OMM keyword object, e.g. `{"OBJECT_NAME":"NOAA 21 (JPSS-2)","EPOCH":"2026-10-29T22:36:18.679968","MEAN_MOTION":14.19555974,...,"NORAD_CAT_ID":54234}`.
It can go straight into `satellite.js json2satrec`; it agreed with the TLE path to 6 m.

### 3.2 Swath → visibility criterion (spherical Earth, R = 6371 km)
A cross-track scanner images the site at the **culmination (time of closest approach, TCA)**. The site is inside the
swath when the ground distance from the sub-satellite point at TCA is ≤ half-swath. Equivalently, the satellite's
elevation at the site is ≥ e_min, where `tan e = (cos λ − R/(R+h)) / sin λ`, `λ = half_swath/R`, and the off-nadir angle is `η = 90° − e − λ`.

| instrument | half-swath used | e_min at current alt (nominal alt) | off-nadir at edge | half-swath from scan angle at current alt |
|---|---|---|---|---|
| VIIRS (3040 km) | 1520 km | **20.4°** at 839 km (20.2° at 829) | 55.9° | ±56.28° → 1555 km |
| MODIS (2330 km) | 1165 km | **24.3°** Aqua 698 km / 24.4° Terra 701 km (24.5° at 705) | 55.2° | ±55° → 1151 (Aqua) / 1157 (Terra) km |
| S2 MSI (290 km) | 145 km (+3 km tolerance → **148**) | **78.5°** at 801 km | 10.2° | ±10.3° → 146 km |

Sangrur sits **145–146 km** off the S2 relative-orbit-105 track. Real tiles exist there (see section 4), so use 148 km for MSI.
Count MSI only on daylight descending passes. VIIRS and MODIS see both day and night.

### 3.3 All in-swath passes over Sangrur (30.25N 75.84E), next 48 h from 2026-10-30 16:00Z
Format: IST TCA (UTC) satellite node · cross-track km · max elevation · off-nadir.
```
Sat 31 Oct 01:44:10 (20:14:10Z) S-NPP   desc 999 km 33.4° 47.6° | 02:03:56 (20:33:56Z) NOAA-20 desc 363 km 63.4° 23.3°
          02:47:16 (21:17:16Z) NOAA-21 desc 828 km 39.4° 43.1° | 03:24:07 (21:54:07Z) S-NPP   desc 1355 km 23.7° 54.0°
          04:53:36 (23:23:36Z) Aqua    desc 471 km 52.1° 33.6° | 09:39:12 (04:09:12Z) Terra   desc 452 km 53.3° 32.6°
          13:01:25 (07:31:25Z) S-NPP   asc 1017 km 32.8° 48.0° | 13:20:59 (07:50:59Z) NOAA-20 asc  403 km 60.8° 25.5°
          14:04:26 (08:34:26Z) NOAA-21 asc  792 km 40.9° 41.9° | 14:41:34 (09:11:34Z) S-NPP   asc 1355 km 23.7° 54.1°
          15:51:09 (10:21:09Z) Aqua    asc   72 km 83.4°  6.0° | 20:37:14 (15:07:14Z) Terra   asc  136 km 77.7° 11.1°
Sun 01 Nov 01:25:18 (19:55:18Z) S-NPP   desc 1453 km 21.6° 55.3° | 01:45:07 (20:15:07Z) NOAA-20 desc 813 km 40.1° 42.6°
          02:28:34 (20:58:34Z) NOAA-21 desc 391 km 61.6° 24.9° | 03:05:29 (21:35:29Z) S-NPP   desc  924 km 35.9° 45.7°
          03:53:50 (22:23:50Z) Aqua    desc 953 km 30.1° 51.3° | 08:40:24 (03:10:24Z) Terra   desc  946 km 30.4° 51.1°
          11:10:45 (05:40:45Z) S2A     desc 145 km 78.3° 10.4° (edge; S2A acquires only sparsely, check STAC)
          12:42:48 (07:12:48Z) S-NPP   asc 1446 km 21.7° 55.3° | 13:02:18 (07:32:18Z) NOAA-20 asc  841 km 39.0° 43.4°
          13:45:37 (08:15:37Z) NOAA-21 asc  343 km 64.7° 22.2° | 14:22:42 (08:52:42Z) S-NPP   asc  902 km 36.7° 45.2°
          16:28:46 (10:58:46Z) Aqua    asc  821 km 34.8° 47.8° | 21:15:56 (15:45:56Z) Terra   asc  789 km 36.7° 46.7°
```

### 3.4 Blind-spot clock: 16-day statistics for Sangrur (2026-10-31 → 11-15)
| satellite | day window IST (passes) | night window IST (passes) |
|---|---|---|
| S-NPP / NOAA-20 / NOAA-21 (VIIRS 375 m) | 12:42–14:47 (21) / 12:37–14:36 (20) / 12:30–14:29 (20) | 01:25–03:30 / 01:20–03:18 / 01:13–03:12 |
| Aqua (MODIS 1 km) | **15:07–16:37 (16)** | 03:47–05:24 (17) |
| Terra (MODIS 1 km) | 08:37–09:57 (16) | **19:54–21:15 (16)** |
| Sentinel-2A/B/C (MSI 10–20 m) | 11:10 or 11:20, 10 passes in 16 d (11-01 S2A, 11-02 S2C, 11-04 S2B+S2A, 11-07 S2B, 11-09 S2C, 11-11 S2A, 11-12 S2C, 11-14 S2B+S2A) | none |

That is ≈5.8 daytime and ≈5.9 night FIRMS-sensor looks per day, and every daytime look falls between 08:37 and 16:37 IST.
On every day, the last daytime look is Aqua, **0.4–1.9 h before 17:00 IST**. Sunset at Sangrur on 10-30 is ~17:42
IST. The next look after that is Terra at 19:54–21:15. **A field lit after Aqua's pass (~15:10–16:40) and burnt out
before Terra (~20:00) can never appear in FIRMS.** That is the product's core claim, and it is computed live from TLEs.

---
## 4. Validation: model vs data

### 4.1 Per-detection TCA − FIRMS label (Punjab+Haryana, 10-29 and 10-30 UTC, 749 detections)
| sat | label UTC (IST) | n | error min (min..max) | median cross-track km | median `scan` |
|---|---|---|---|---|---|
| S-NPP | 10-29 08:07 (13:37) / 08:09 (13:39) / 10-30 07:50 (13:20) | 138 / 31 / 57 | +1.57..+2.23 / +0.23..+0.35 / +0.08..+0.63 | 208 / 159 / 599 | 0.41 / 0.40 / 0.42 |
| NOAA-20 | 10-29 08:28 (13:58) / 10-30 08:09 (13:39) | 125 / 127 | +0.33..+1.03 / +0.65..+1.27 | 420 / 32 | 0.50 / 0.38 |
| NOAA-21 | 10-29 07:31 (13:01) / 19:55 (01:25) / 10-30 07:14 (12:44) / 08:54 (14:24) | 56/1/21/21 | +0.90..+1.43 / +0.67 / −0.62..−0.23 / −0.53..−0.15 | 734/1110/1135/1167 | 0.51/0.45/0.47/0.49 |
| Aqua | 10-29 10:45 (16:15) | 171 | −1.72..−1.15 | 381 | 1.37 |
| Terra | 10-29 15:26 (20:56) | 1 | +1.68 | 323 | 1.26 |

- **Sangrur-only check** (predicted Sangrur TCA vs labels of detections within 50 km): S-NPP +1.82 and +0.10 min,
  NOAA-20 +0.48 and +0.72, NOAA-21 +0.90, Aqua −1.67. **Error is ≤ 2 min.**
- **Region-wide** (all 7,926 South Asia detections, 10-29/30): 99.56 % within ±150 s; overall range −120..+192 s.
  Labels step in 120/180 s increments, so FIRMS times are quantised to about 2 min. This error is label granularity,
  not orbit error.
- **Geometry check**: the predicted cross-track distance correlates with FIRMS `scan` (pixel growth off-nadir) at
  r = 0.996 for Aqua (n=171) and r = 0.968 for NOAA-20 (n=252).
- **Swath check** (South Asia, 10-29/30): the farthest detection from the predicted track was 1,525.6 km for VIIRS
  (S-NPP; NOAA-21 1,502; NOAA-20 1,509) against our 1,520 km half-swath. For MODIS it was 1,110 km (Aqua) and 1,060 km
  (Terra) against 1,151–1,165 km. Edge pixels measured `scan` ≈ 0.79 km (VIIRS) and ≈ 4.7 km (MODIS).
- Night passes over Sangrur with good geometry (e.g. NOAA-20 10-30 02:22 IST, 83 km off-track) show 0 detections within
  50 km. The fires are already out by then, which supports the "unseen" narrative.

### 4.2 Sentinel-2 (independent check)
Planetary Computer STAC returned `sentinel-2-l2a` items intersecting Sangrur for October (14 items, 2.3 s, tile 43REP).
The tile `granule-metadata` asset (MTD_TL.xml, signed via `planetary_computer`, 1.6–5.2 s each) carries `SENSING_TIME`.
Comparison as SENSING_TIME vs predicted TCA (Δ): S2B 10-18 r5 05:50:35.9 vs 05:50:34 (−2 s) · S2C 10-20 r105 05:40:40.9
vs 05:40:39 (−2 s) · S2A 10-22 r105 05:40:49.4 vs 05:40:45 (−4 s) · S2C 10-23 r5 05:50:36.3 vs 05:50:35 (−1 s) · S2B
10-25 r105 05:40:41.0 vs 05:40:39 (−2 s) · S2B 10-28 r5 05:50:37.3 vs 05:50:35 (−2 s).

The STAC `datetime` (e.g. `05:38:29Z`) is the **datatake start, 8–14 min before the tile is actually imaged**. Use
`SENSING_TIME` or our TCA for "when S2 saw it" (11:10/11:20 IST). The predicted S2A passes on 10-05 and 10-25 have no
Planetary Computer item. S2A (post-2C "extended campaign") is in orbit, but its October items over Sangrur are sparse
(10-02, 10-15, 10-22). **A predicted pass is not an acquisition, so confirm with STAC.**

---
## 5. CelesTrak policy and frontend (satellite.js)

### 5.1 CelesTrak usage policy (gp-data-formats.php and usage-policy.php, read today)
- GP data "updated once every 2 hours" and "only download data once per update". As of 2026-03-26 they "enforce the
  one-download-per-update policy for all users, starting with the Active and Starlink GROUPs". A repeat request returns
  **HTTP 403** "GP data has not updated since your last successful download...".
- HTTP errors are limited to **50 errors (301/403/404) per 2 h**; going over sends the IP to the firewall for manual
  review. M2M clients must stop on any non-200 and alert a human. The policy says a 301 signals an outdated URL, so
  always use the canonical `https://celestrak.org/NORAD/elements/gp.php`.
- NAT and shared IPs should use a caching proxy. No licence or attribution text is given; credit "orbital data: CelesTrak" anyway.
- Observed today: 15 CATNR/NAME calls in ~4 min all returned 200 (no 403 on repeats). `gp.php` sends
  **`Access-Control-Allow-Origin: *`**, but browsers should still not call it, because every visitor would count
  against the policy.
- **Our pattern:** a backend cron runs every 6 h (never faster than every 2 h). It makes 8 CATNR JSON calls spaced ~3
  s apart and stores them in Postgres (`tle(norad, epoch, omm_json, fetched_at)`). `/api/orbits` serves the cached OMM
  JSON. If CelesTrak fails, keep the last good set (TLEs are fine for days) and show their epoch age.

### 5.2 satellite.js 7.1.0 (already in `apps/web/package.json`) outline, tested in Node 24
I tested it against the same TLEs: all 24 VIIRS/MODIS passes in 48 h match skyfield to ≤1 s and ≤0.1 km. The pass
search for 5 satellites x 48 h took 55 ms.
```ts
import { json2satrec, propagate, gstime, eciToGeodetic, degreesLat, degreesLong } from 'satellite.js';
const R = 6371.0088, D = Math.PI / 180, HALF = { VIIRS: 1520, MODIS: 1165, MSI: 148 };
const satrec = json2satrec(omm);                          // omm = one object from /api/orbits (CelesTrak FORMAT=JSON)
function sub(s, d) { const pv = propagate(s, d); if (!pv) return null;   // null on SGP4 error
  const g = eciToGeodetic(pv.position, gstime(d)); return { lat: degreesLat(g.latitude), lon: degreesLong(g.longitude) }; }
// ground track: sample sub() every 20–30 s for ±50 min -> deck.gl PathLayer (split at the antimeridian)
// swath: at each sample, bearing b = bearing(sub(t), sub(t+1s)); left = dest(p, b-90°, HALF), right = dest(p, b+90°, HALF)
//        polygon = [...left, ...right.reverse()] -> deck.gl PolygonLayer (great-circle destination formula)
// next in-swath pass over a site: 30 s scan for local minima of hav(site, sub(t)) < HALF+200 km,
//        ternary-search to 0.5 s, keep if dist <= HALF  -> countdown "next VIIRS look in 3 h 12 min"
// live marker: requestAnimationFrame -> sub(satrec, new Date())  (measured 1.5 µs per propagate+eciToGeodetic, Node 24)
```
Full tested file: `scratchpad/firms-orbits/js/passes.mjs` (haversine, bearing, destination, swathPolygon, passes).
Helpers: `bearing = atan2(sinΔλ·cosφ2, cosφ1·sinφ2 − sinφ1·cosφ2·cosΔλ)`, and
`dest: φ2 = asin(sinφ1·cosδ + cosφ1·sinδ·cosθ), λ2 = λ1 + atan2(sinθ·sinδ·cosφ1, cosδ − sinφ1·sinφ2)` with `δ = km/R`.

---
## 6. Recommended approach
1. **`firms_poll` job (every 10 min).** `HEAD` the 4 South Asia `24h` CSVs. On an ETag change, `GET`, parse, clip to
   PB/HR polygons (PostGIS `ST_Within`), and upsert into `fire_detections`. Primary key is `(source, lat, lon,
   acq_date, acq_time)`. Store `t_utc` as timestamptz and derive IST only for display. On cold start, load the `7d`
   files. With a MAP_KEY, run the backfill from section 2.2 once.
2. **`tle_refresh` job (every 6 h).** Fetch CelesTrak CATNR JSON for the 8 satellites and cache them. Server-side
   skyfield (`orbits.py`) powers `/api/passes?lat&lon&hours` and the blind-spot clock. Client-side satellite.js draws
   live tracks and swaths from the same cached OMM.
3. **"Unseen burn" rule.** Take an S2 burn scar that first appears between clear S2 dates t1 and t2. Collect FIRMS
   detections within the pixel half-diagonal plus geolocation slack (VIIRS ≈ 0.5 km, MODIS ≈ 1.5–3 km, i.e. use
   `scan`/`track`) during [t1, t2]. If there are none, the scar is unseen. Also record **how many in-swath day passes
   occurred** in [t1, t2] from the orbit model, and show the IST windows when nobody was looking.
4. **Mask static sources**: a 400 m cell with detections on ≥3 nights, or on ≥5 days in a month, is masked. This mirrors
   FIRMS's own static-thermal-anomaly approach.
5. **Attribution in the UI**: "Fire data: NASA FIRMS" plus a link (the FAQ asks for "NASA FIRMS" in captions; its
   paper acknowledgment starts "We acknowledge the use of imagery from the NASA LANCE FIRMS..."). "Orbits: CelesTrak".
   "Boundaries: geoBoundaries / DataMeet (CC BY 2.5 IN)". For public display of India's outline, Survey of India
   boundaries may be required. geoBoundaries is fine for analysis.

### Tested Python snippet: swath-aware passes (core of `orbits.py`; output below is from this exact code)
```python
import math
from datetime import datetime, timedelta, timezone
import numpy as np
from skyfield.api import load, wgs84, EarthSatellite
ts, R = load.timescale(), 6371.0088          # builtin timescale -> no network
def e_min_deg(half_km, h_km):                 # min site elevation for a ground half-swath
    lam = half_km / R; return math.degrees(math.atan2(math.cos(lam) - R / (R + h_km), math.sin(lam)))
def hav_km(a1, o1, a2, o2):
    a1, a2 = np.radians(a1), np.radians(a2)
    x = np.sin((a2 - a1) / 2)**2 + np.cos(a1) * np.cos(a2) * np.sin(np.radians(o2 - o1) / 2)**2
    return 2 * R * np.arcsin(np.sqrt(x))
def passes_in_swath(l1, l2, lat, lon, start, hours, half_km):
    sat, site = EarthSatellite(l1, l2, "", ts), wgs84.latlon(lat, lon)
    h = float(wgs84.height_of(sat.at(ts.from_datetime(start))).km)
    t, ev = sat.find_events(site, ts.from_datetime(start), ts.from_datetime(start + timedelta(hours=hours)),
                            altitude_degrees=max(e_min_deg(half_km, h) - 3, 0))
    out = []
    for ti in t[ev == 1]:                                   # culmination = time of closest approach
        sp = wgs84.geographic_position_of(sat.at(ti))
        d = float(hav_km(lat, lon, sp.latitude.degrees, sp.longitude.degrees))
        if d <= half_km:
            out.append((ti.utc_datetime().astimezone(timezone(timedelta(hours=5, minutes=30))), round(d)))
    return out
# passes_in_swath(*tle_lines_for_43013, 30.25, 75.84, datetime(2026,10,30,16,tzinfo=timezone.utc), 48, 1520)
# -> 2026-10-31T02:03:56+05:30 363 | 2026-10-31T13:20:59+05:30 403 | 2026-11-01T01:45:07+05:30 813 | 2026-11-01T13:02:18+05:30 840
# MSI: also require sun elevation > 0 (MSI images only on the daylit descending pass).
```

### Tested Python snippet: keyless FIRMS ingest → Punjab/Haryana (ran in 10 s; printed N20 127 / N21 43 / SNPP 57)
```python
import io, httpx, numpy as np, pandas as pd, geopandas as gpd
BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"
FILES = {"VIIRS_NOAA20": "noaa-20-viirs-c2/csv/J1_VIIRS_C2", "VIIRS_NOAA21": "noaa-21-viirs-c2/csv/J2_VIIRS_C2",
         "VIIRS_SNPP": "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2", "MODIS": "modis-c6.1/csv/MODIS_C6_1"}
_etags = {}                                     # prod: persist in Postgres
def states(c):                                  # Punjab + Haryana polygons (geoBoundaries gbOpen IND ADM1)
    meta = c.get("https://www.geoboundaries.org/api/current/gbOpen/IND/ADM1/").json()
    g = gpd.read_file(io.BytesIO(c.get(meta["simplifiedGeometryGeoJSON"], follow_redirects=True).content))
    g = g[g.shapeISO.isin(["IN-PB", "IN-HR"])]  # not shapeName: it has diacritics ("Haryāna")
    return g.assign(state=g.shapeISO.map({"IN-PB": "Punjab", "IN-HR": "Haryana"}))[["state", "geometry"]]
def fetch(source, window, c):                   # HEAD first; GET only when the ETag (MD5) changed
    url = f"{BASE}/{FILES[source]}_South_Asia_{window}.csv"
    etag = c.head(url).headers.get("etag")
    if etag and _etags.get(url) == etag:
        return None
    r = c.get(url); r.raise_for_status(); _etags[url] = etag
    df = pd.read_csv(io.BytesIO(r.content), dtype={"acq_time": str})
    if "brightness" in df:                      # MODIS: % confidence -> class (C6.1 UG Table 11)
        p = df.confidence.astype(int)
        df = df.rename(columns={"brightness": "bright_ti4", "bright_t31": "bright_ti5"}).assign(conf_pct=p)
        df["confidence"] = np.select([p < 30, p < 80], ["low", "nominal"], "high")
    df["t_utc"] = pd.to_datetime(df.acq_date + df.acq_time.str.zfill(4), format="%Y-%m-%d%H%M", utc=True)
    return df.assign(source=source, t_ist=df.t_utc.dt.tz_convert("Asia/Kolkata"))
with httpx.Client(timeout=60) as c:
    st, frames = states(c), [f for s in FILES if (f := fetch(s, "48h", c)) is not None]
df = pd.concat(frames)
det = gpd.sjoin(gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs=4326), st, predicate="within")
print(det[det.t_ist.dt.date.astype(str) == "2026-10-30"].groupby(["state", "source"]).size())
```

---
## 7. Sources (read today)
FIRMS: `/api/` (v4.2.5 service list), `/api/area/`, `/api/map_key/`, `/api/data_availability/`, `/content/active_fire/`
(cadence and archive notes). earthdata.nasa.gov `/data/tools/firms/faq` (latency, 60-min updates, citation, static
sources) and `.../active-fire-data-attributes-modis-viirs` (UTC, codes). MODIS C6/C6.1 Fire User Guide v1.0 PDF
(confidence Table 11). CelesTrak `/NORAD/documentation/gp-data-formats.php` and `/usage-policy.php`. geoBoundaries API
`gbOpen/IND/ADM1`. Planetary Computer STAC `sentinel-2-l2a`. npm `satellite.js@7.1.0` (published 2026-07-23).
Out of scope, **UNVERIFIED**: the only sensors watching 15:00–20:00 IST every 15–30 min are geostationary (e.g.
INSAT-3DR/3DS fire products on MOSDAC). FIRMS does not serve them for India; another researcher should check them.
