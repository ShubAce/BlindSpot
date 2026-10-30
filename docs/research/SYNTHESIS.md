# BlindSpot: research synthesis and build plan

Lead-engineer review of the eight research docs in `docs/research/` (3,325 lines), written 2026-10-30. I re-ran the
critical calls myself between **17:13 and 17:31 UTC (22:43–23:01 IST)** from the team's Windows dev box (residential
link, India).

Tags used below:
- **PASS**: I re-verified it with a real call today.
- **DOC**: a topic agent verified it today and I did not re-run it.
- **UNVERIFIED**: nobody could call it; the reason is given.

My scripts and logs are in the ephemeral session dir
`C:\Users\shubh\AppData\Local\Temp\claude\f--forge\375401be-2a9f-45a8-acfb-c0a0fdbd6c12\scratchpad\synthesis\{scripts,logs}`.
The smoke test in §9 is the reusable copy.

## 0. Verdict

- **The data spine is real, and keyless.** FIRMS CSVs, CREAMS WFS, CelesTrak, Earth Search, PC tiles, Open-Meteo,
  data.gov.in (sample key) and GK-2A on AWS all answered today. Only the LLM needs a key before we can build, so get the
  Gemini key first.
- **Fix three bugs before anything ships, or the demo will show wrong numbers** (fixes in §5):
  - Bounding-box counts include Pakistan's Punjab: 611 rows vs 252 in the Indian polygons.
  - The repo's shared User-Agent is tarpitted by data.gov.in (ReadTimeout after 20.4 s).
  - PC's 2026-10-28 Sentinel-2 pixels are still noise.
- **Today's live data backs the core story:**
  - CREAMS' official layers held only S-NPP and Aqua events on every day checked.
  - The last daytime look over Punjab is Aqua at 15:07–16:37 IST.
  - GK-2A sees the 16–18 IST spike. The geostationary doc validated it on 29 Oct (DOC); I reproduced the 30 Oct count,
    and that hazy day is noisy.
- **Biggest scientific risks:**
  - The precision of "unseen burn" candidates (tillage looks like burning).
  - Next-to-burn ML: the naive model failed (AUC 0.59).

  Ship unseen burns as audited candidates. Ship next-to-burn only behind an AUC gate.
- **Not researched at all: hosting.** A live URL is a submission requirement. Decide before building the Sentinel-2 job,
  because that job is network-bound (1.6–4.6 GB per full refresh).

## 1. Re-verification (my calls, 2026-10-30)

| # | Claim | Call | Result | Verdict |
|---|---|---|---|---|
| 1 | FIRMS keyless CSV | HEAD + GET `noaa-20-viirs-c2/csv/J1_VIIRS_C2_South_Asia_24h.csv` | 200; HEAD 1.13 s, GET 1.20 s; ETag `"636061a7…"`; Last-Modified 16:42:18Z (an hourly rewrite), yet the content matches the docs' 15:45Z fetch byte-for-byte in size and row count (325,549 B; 3,932 rows covering 10-29 06:42Z → 10-30 09:54Z); **no ACAO header**; `accept-ranges: bytes`; raw `acq_time` is always 4 chars (`0650`) | PASS |
| 1b | Punjab counts | that file joined to geoBoundaries IND + PAK ADM1 | IN-PB: 123 (IST 10-29) + 127 (10-30). IN-HR: 2. **PAK Punjab: 625 + 665.** The AOI bbox `73.8,27.6,77.7,32.6` holds 611 rows | FIRMS doc's 127 PASS; meteo doc's "574 Indian Punjab" **FAIL** |
| 2 | PC STAC, SAS and tiles | SAS token; search `sentinel-2-l2a` at Sangrur 10-24..31; item tile z13/5821/3373 for `S2B_MSIL2A_20261018T053829_R005_T43REP_20261018T092057`; NBR expression tile; tilejson | SAS 0.91 s (expires in about 45 min); open 0.98 s + search 1.62 s, 2 items; true-colour tile 200 PNG 165 KB in 2.4 s (real imagery, checked visually); NBR tile 0.57 s; tilejson 0.24 s; tiles send `ACAO: *` and `max-age=3600` | PASS |
| 2b | PC 10-28 corruption | 256² B12 window at 20 m, PC vs Earth Search | PC DN 1–255, median reflectance −0.087, corr −0.004. E84 DN 1356–9773, median 0.178. The PC tile renders black (mean grey 0.7) | **Still broken at 17:18Z** |
| 2c | PC ingestion lag | same search | No 10-30 item on PC at 17:18Z, more than 11.6 h after sensing | CONFIRMED |
| 3 | Earth Search c1 | search `sentinel-2-c1-l2a` | `S2C_T43REP_20261030T053555_L2A` created 09:50:52Z (4.2 h lag); `raster:bands` scale 0.0001, offset −0.1; `datetime` = tile sensing time (05:50:37.307 on 10-28, matching MTD_TL) | PASS |
| 4 | Open-Meteo multi-location | POST: 3 points, 10 vars (u/v at 10m/950/925/850 hPa, BLH, precip), `models=best_match,gfs_seamless` | 200 in 0.69 s, 32 KB. JSON array of 3: element 0 has **no `location_id`**, then 1 and 2. Keys end in `_best_match` / `_gfs_seamless`. 0 nulls in BLH or 925 hPa for both models. `dwd_icon` meta: 12Z run available at 15:38:40Z (3.6 h). CAMS AQ Delhi at 17Z: pm2_5 70.1, us_aqi 82 (1.14 s) | PASS |
| 5 | CPCB via data.gov.in | Punjab PM2.5 with a clean UA, then the repo's exact UA | Clean UA: 200 in 0.72 s, 7 stations, `updated_date` 17:04:46Z, batch 22:00 IST, ACAO `*`. **Repo UA `BlindSpot/0.1.0 (…; python-httpx/0.28.1)`: ReadTimeout after 20.4 s.** CPCB bulletin 2026-10-30: HEAD 200, Last-Modified 10:35:57Z, 291 KB | PASS (tarpit CONFIRMED) |
| 6 | CelesTrak GP | `gp.php?CATNR=43013&FORMAT=JSON`; then `CATNR=27424` (§9) | 200 in 7.98 s and 1.40 s; `ACAO: *`; epochs 10-29 18:29Z and 22:11Z. skyfield `EarthSatellite.from_omm` passes over Sangrur match the FIRMS doc to the second (31 Oct 02:03:56 IST at 363 km, 13:20:59 IST at 403 km) | PASS |
| 7 | CREAMS official layer | WFS rice layers for 30 Oct, 29 Oct, 1 Oct, 15 Sep | 0.05–0.30 s each. 30 Oct: 44 events, all S-NPP at 13h (still so at 17:25Z). 29 Oct: 223 = S-NPP 116 + AQUA 107. 1 Oct: 23 S-NPP. 15 Sep: 5 S-NPP. **None from NOAA-20, NOAA-21 or Terra** | PASS + new finding |
| 8 | GK-2A live | S3 list of hour 11; the doc's §4.5 script run verbatim on `202610301130` | 6 scans, LastModified 22–33 min after slot start. `fire_px=216`, read 5.2 s: exact match. **But only 35% of those pixels lie within 8 km of same-day NOAA-20 detections** (median 12.6 km), and 52 of 216 sit at ≥31.8N on the foothill cloud edge | Reproduced; signal DOUBTFUL on hazy days |
| 9 | Jethva header (11 vs 12 lines) | Zenodo record, one file, and a 2 KB Range read | CC-BY-4.0, 16 files. Data starts at line 12 in HR-2019 and PB-2025, so there are **11 header lines**. Range requests return 206 | Geostationary doc right |
| 10 | FIRMS `DAY_RANGE` | GET `/api/area/` | Page says `DAY_RANGE: 1 .. 5` and "5000 transactions / 10-minute interval" | FIRMS doc right |
| 11 | Gemini free models | ai.google.dev pricing page (last updated 2026-10-28), via WebFetch | Free: `gemini-3.8-flash`, `-3.6-flash`, `-3.5-flash-lite`, `-3.1-flash-lite`, `-3-flash-preview`, plus `gemini-embedding-2/-001` and `gemini-3.8-flash-tts`. Not free: `gemini-4-argon`, `gemini-3.1-pro-preview`. Search grounding not available on the free tier for 3.x | Page PASS; **model calls UNVERIFIED (no key)** |
| 12 | Web map stack | OFM dark style; indianopenmaps `states/soi/tiles.json`; EOX z10 tile; `SOI_States.parquet` | 200 in 0.61 s, ACAO `*`; 200 in 4.1 s cold, ACAO `*`; 200 in 2.6 s, ACAO echoes the Origin; 302 → 200, 19,084,932 B | PASS |

## 2. Contradictions and doubtful claims (resolved where possible)

1. **Pakistan contamination.** Meteo doc §3b says the J1 24h file had "576 NOAA-20 detections in Indian Punjab (574) and
   Haryana (2)". The same file has 250 + 2 inside the Indian polygons.
   - So the all-fires forward run (11,520 particles; Delhi smoke index peak 34.3 on 1 Nov at 17:30 IST) is
     **UNVERIFIED** and probably includes Pakistani fires. The likely cause is a name join on "Punjab" across IND and PAK
     ADM1. The Sangrur-only run (fires within 60 km) is unaffected.
   - The same trap sits in the Gemini doc's `count_firms_fires(73.8,27.6,77.6,32.6)` tool ("Punjab+Haryana"), and in
     the repo's `aoi_bbox` if anyone counts with it: that box holds 611 NOAA-20 rows vs 252 in IN-PB + IN-HR.
   - **Rule:** clip every count with `ST_Within` on Indian polygons, matching `shapeISO`, never `shapeName`.
   - PAK ADM1 contains units named "Azad Kashmir" and "Gilgit-Baltistan". Never render PAK labels.
2. **FIRMS area API `DAY_RANGE` is 1..5**, as the live page says. The S2 doc's `{1..10}` is wrong.
3. **The Jethva files have 11 header lines.** The domain doc's `skiprows=12` silently drops the first row of each file.
4. **`acq_time` is a zero-padded 4-char string in the raw CSV.** The meteo doc's "809" is a pandas-int artefact. Read it
   with `dtype=str` and `zfill(4)`.
5. **The "seen" tolerance conflicts between docs:** a fixed 750 m to the centroid (S2 doc) vs a sensor-dependent radius
   (FIRMS doc).
   - **Decision:** `ST_DWithin(burn_polygon::geography, fire_point::geography, r)` with
     `r = 500·√(scan²+track²) + 200 m`. That gives VIIRS 465–760 m. Today's MODIS rows give p50 1,014 m and p95 1,249 m,
     with about 2.8 km at the swath edge.
   - Measure distance to the polygon, not to its centroid. A big field's centroid can sit far from a detection on its edge.
6. **CREAMS' satellites are fewer than its bulletin implies.** The bulletin's satellite table lists S-NPP, Terra, Aqua and
   NOAA-20, but every daily layer I pulled holds only S-NPP and Aqua. On 29 Oct FIRMS had 123 NOAA-20 detections in IN-PB,
   and CREAMS has none of them. It is unknown whether CREAMS de-duplicates NOAA-20 against S-NPP. Pitch wording: "the
   official layer contained only S-NPP and Aqua events on the days checked."
7. **Aqua is still producing data.** The domain doc's search-level "instrument shutdown 26 Aug 2026" is contradicted:
   the FIRMS MODIS 24h file (whole South Asia box) had 448 Aqua rows at 17:28Z, and CREAMS 29 Oct had 107 Aqua events. Drive the clock's MODIS rows from
   actual FIRMS presence (age of the last `A` row), not from TLEs alone.
8. **ISRO SAC VIIRS trend.** The geostationary doc (from press) pairs "−3,743/yr" with SEVIRI's +43/yr, which mixes
   periods. The domain doc read the paper: S-NPP −3,743/yr for 2012–24 and −18,883/yr for 2020–24, vs SEVIRI +43/yr.
   Quote like-for-like periods. (DOC; not re-fetched.)
9. **iFOREST burnt-area drop.** The report itself says 35% (p.25) and 37% (p.29); 31,447 → about 20,000 km² is −36%. Say
   "about a third" and cite both figures. (DOC.)
10. **GK-2A on 30 Oct.** The count reproduces (216), but agreement with same-day NOAA-20 is only 35%. The doc's "68%" used
    all VIIRS from 28–30 Oct, where 64% would happen by chance. Hazy scans are near chance. Use a per-scan agreement score
    as a quality gate, and demo the 29 Oct curve (87–99%) plus the Jethva history.
11. **"87% of clusters unseen" (S2 doc) is a candidate rate.** Its precision is unknown (tillage confound), and that
    snippet ran without the WorldCover mask. Don't quote it until it is audited (§6, P0-4).
12. **STAC `datetime` differs by catalogue.** PC gives the datatake start (05:38:29). E84 c1 gives the tile sensing time
    (05:50:37). Both docs are right for their own catalogue.
13. **Models disagree on BLH at the burning hour.** At Delhi on 10-30 12Z (17:30 IST), `best_match` gave 140 m and
    `gfs_seamless` 1,295 m. That is a 9× spread, which strengthens the 2-model ensemble decision.
14. **Gemini 2.5 models.** The pricing page still lists them as free, but the Gemini doc reports 404s for new keys since
    2026-09-18. Keep them out of the fallback chain until `models.list` on our key shows them.
15. **CelesTrak latency** was 1.3–2.1 s in the doc and 7.98 s for me. Use timeouts of 30 s or more, and never retry a 403.

## 3. Decision table

| Need | Primary | Fallback | Verified |
|---|---|---|---|
| Live active fires | FIRMS South Asia `24h` CSV ×4. Backend HEAD every 10 min, GET when the ETag changes, proxy to the browser (no CORS) | `48h`/`7d` files on cold start; area API with MAP_KEY | PASS |
| Officially counted fires | CREAMS WFS `geokrishi_v:{D}_{month}_{YYYY}_rice`. Pull D hourly (partial) and D-1 each morning | CREAMS bulletin PDF | PASS / DOC |
| Season history (15 Sep →) | CREAMS daily layers (1 keyless call/day, 0.1–0.3 s) + FIRMS `7d` | FIRMS area API: 4 sources × 10 five-day calls (MAP_KEY) | PASS / UNVERIFIED |
| Orbits | CelesTrak GP `CATNR` + `FORMAT=JSON` for 8 satellites, cron every 6 h, Postgres cache | Last good OMM with its epoch age shown | PASS |
| Pass prediction / clock | skyfield `passes_in_swath` (VIIRS 1520 / MODIS 1165 / MSI 148 km) on the server; satellite.js 7.1.0 tracks and swaths in the browser | — | PASS / DOC |
| S2 analytics | Earth Search `sentinel-2-c1-l2a` (keyless, 4.2 h lag, scale/offset metadata) | PC `sentinel-2-l2a` behind the B12-median and identity gates | PASS |
| S2 evidence viewer | PC data-API item tiles (CORS `*`), validated items only | Our own PNG chips from E84 COG windows (rasterio + pillow) | PASS / UNVERIFIED |
| Cropland mask | ESA WorldCover 2021, class 40 (PC) | — | DOC |
| Crop state under haze | Sentinel-1 RTC VH (PC), standing → harvested only | — | DOC |
| Live diurnal timing | GK-2A L1B (AWS) + the doc's detector; ≥16:30 IST only; per-scan VIIRS agreement gate | INSAT-3DS FIR preview counts (MOSDAC, keyless); EUMETView `msg_iodc:fire` WMS as a visual | PASS / DOC |
| Historical timing shift | Zenodo Jethva GK-2A 2019–25 (CC BY 4.0), fetched at runtime | ISRO SAC 2025 / iFOREST 2025 citations | PASS |
| Winds + BLH | Open-Meteo POST grid, 24–36N 66–84E at 0.75° (425 pts), `best_match` + `gfs_seamless`, refreshed when `meta.json` changes | NOMADS GFS 0.25° GRIB filter | PASS / DOC |
| Backtest winds | Open-Meteo historical-forecast API (BLH from 2024-09-01) | ERA5 archive (no pressure levels) | DOC |
| Model AQ baseline | Open-Meteo AQ (CAMS global, 0.4° PM). It is fire-blind: GFAS uses polar orbiters | — | PASS |
| Ground AQ, live | data.gov.in CPCB feed. Values are NAQI **sub-indices**; invert for µg/m³ | CPCB AQI bulletin PDF (daily, ~16:05 IST) | PASS |
| Ground AQ, hourly µg/m³ | OpenAQ v3, provider 168 (key) | OpenAQ S3 archive (keyless, 3–7 day lag) | UNVERIFIED / DOC |
| Stubble-share benchmark | IITM DSS Chart.js scrape, 3×/day | — (SAFAR unreachable) | DOC (2 docs agree) |
| Drawn boundaries | `SOI_States.parquet` → our MVT. Strip OFM boundary layers; filter the "Azad Kashmir"/"Gilgit-Baltistan" labels | indianopenmaps SOI tiles | PASS |
| Clipping / attribution | geoBoundaries IND ADM1 (`shapeISO`) now; LGD/Bharatmaps districts by LGD code | SOI_States once `pyarrow` is added | PASS / DOC |
| Basemap / imagery | OpenFreeMap `dark` + `indiaCompliant()`; EOX `s2cloudless-2025` toggle | CARTO only with a key | PASS |
| LLM | Investigator: `gemini-3.5-flash-lite`, falling back to `3.1-flash-lite`, `3.8-flash`, `3.6-flash`. Bulletin: `3.8-flash`. Manual tool loop (Gemini doc §4) | Deterministic evidence pack, never canned text | **UNVERIFIED** (no key) |
| Hosting | **UNDECIDED.** Not researched by anyone | — | — |

## 4. Accounts and keys to create (in this order)

| # | What | Where | Env var | Unlocks | When |
|---|---|---|---|---|---|
| 1 | Gemini API key | https://aistudio.google.com/apikey; then copy the real RPM/RPD from https://aistudio.google.com/rate-limit into the router | `GEMINI_API_KEY` (leave `GOOGLE_API_KEY` unset: it takes precedence) | The whole agent and the bulletin. The free tier uses prompts for training, so send public data only | **today** |
| 2 | NASA FIRMS MAP_KEY | https://firms.modaps.eosdis.nasa.gov/api/map_key/ (emailed; 32 chars) | `FIRMS_MAP_KEY` | Area API backfill 15 Sep → today; `data_availability`; 5,000 transactions / 10 min | **today** |
| 3 | data.gov.in API key | https://data.gov.in → sign in → My Account → API key | `DATA_GOV_IN_API_KEY` | `limit` > 10 (3 calls instead of 67) and no shared-key 429s. Max limit undocumented: check that `count` > 10 on the first call | **today** |
| 4 | OpenAQ key | https://explore.openaq.org/register | `OPENAQ_API_KEY` | Hourly µg/m³ per CPCB station + history (60/min, 2,000/h per docs) | P1 |
| 5 | LSA SAF account | https://mokey.lsasvcs.ipma.pt/auth/signup | `LSASAF_USERNAME`, `LSASAF_PASSWORD` (already in `.env.example`) | Meteosat-9 IODC FRP-PIXEL: the official 15-min geostationary fire list with FRP | register now (approval time unknown); stretch |
| 6 | MOSDAC account | https://mosdac.gov.in/signup/ | `MOSDAC_USERNAME`, `MOSDAC_PASSWORD` (already in `.env.example`) | INSAT-3DS/3DR FIR KML points | optional |
| — | Skip | CARTO key, ArcGIS, EUMETSAT, Earthdata | — | OFM and EOX replace CARTO and Esri; SEVIRI L1.5 is about 175 MB per slot; LANCE text files showed no latency gain | — |
| — | Hosting | Not researched: a web host for Next.js, a container host for FastAPI plus the scheduler, Postgres **with PostGIS** | `DATABASE_URL` | The submission's "Live Deployment" URL | decide this week |

## 5. Engineering fixes before writing connectors (all from verified failures)

1. **User-Agent.** `services/api/blindspot/http.py` builds `USER_AGENT` ending in `python-httpx/<ver>`. data.gov.in
   tarpits it (verified today: ReadTimeout after 20.4 s), and WDQS returned 403 to httpx (geography doc). Use
   `BlindSpot/0.1.0 (+<repo URL>; <contact email>)` with no library token.
2. **Per-source pacing.** `fetch()` retries a 429 after 1–2 s. That is wrong for two sources:
   - data.gov.in's sample key sends no Retry-After and needs 30–90 s spacing.
   - CelesTrak must never be retried on 403/404: 50 errors in 2 h gets the IP firewalled, and repeat downloads return 403.

   Add per-source `min_interval_s`, `retry_statuses` and `max_attempts`.
3. **Dependencies** (team decision; research edited nothing): `uv add pyarrow h5py pdfplumber`.
   - `pyarrow`: GeoParquet for SOI and LGD.
   - `h5py`: GK-2A range reads (I ran it via `uv run --with h5py`).
   - `pdfplumber`: the CPCB bulletin.
4. **Config.** Widen `met_bbox` from `(70,26,80,34)` to `(66,24,84,36)`: 23–100% of particles leave the small box within
   72 h (meteo §3a). Use `aoi_bbox` only as a pre-filter, never for counts.
5. **Time normalisation at ingest.** Store everything as `timestamptz` in UTC and show IST.

   | Source | As delivered |
   |---|---|
   | FIRMS | `acq_date` + `acq_time`, UTC |
   | CREAMS rice layer | `acq_date` is a float `DDMMYYYY` with no zero pad (`1102026` = 1 Oct); `acq_time` is `HH:MM:SS` IST |
   | CREAMS `al_ind` layer | `YYYYMMDD` date + UTC `hhmm` |
   | data.gov.in | `dd-mm-YYYY HH:MM:SS`, IST, batch time |
   | Jethva | hours in IST |
   | GK-2A | filenames in UTC |
   | Open-Meteo | `timeformat=unixtime` |
6. **Scene gate for S2.** Per scene, the B12 median must lie in (0.02, 0.6). When both catalogues hold the scene, also
   run a 64-px identity check against E84 (§9). PC 2026-10-28 fails it today.
7. **No-dummy-data optics.** `apps/web/public/photos/credits.json` is a hardcoded JSON file shipped in the web app. It
   holds photo credits, not data, but the rule is zero-tolerance and literally names "hardcoded JSON files". Move the
   credits into the component, or fetch them from the Wikimedia Commons API. Likewise, derive receptor cities from a
   GeoNames population/distance query rather than a typed name list.

## 6. MVP scope, in priority order

**Demo narrative (3 min, every number computed live).**
1. **Clock.** Click a field near Sangrur. Satellites move live, and the 24-h dial shows today's looks, ending with a line
   like "last daytime look Aqua 15:51 IST, next Terra 20:37" (the FIRMS doc's §3.3 values for 31 Oct; always computed
   live).
2. **Curve.** The Jethva history (2021: 52% of detections at or after 16:00; 2025: 96%) and today's GK-2A evening index.
   The peak lands in the dial's blank sector.
3. **Three numbers.** Officially counted (CREAMS; today one 4-minute S-NPP pass) vs seen by NASA (FIRMS,
   polygon-clipped) vs burnt Sentinel-2 fields with no detection, shown with their audited precision.
4. **Field.** Open an unseen field: before/after S2 chips, and the list of in-swath passes that missed it.
5. **Smoke.** Release smoke from those fields at 17:00 IST and show its arrival at Delhi with a 2-model cone. Compare
   with IITM DSS's VIIRS-driven stubble share.
6. **Agent.** A judge asks a question. The tool calls stream, the answer is cited, and an EN/HI/PA bulletin follows.

**P0: the demo spine. Build in this order** (estimates for a 2–3 person team):
- **P0-1 Data spine (0.5–1 d).**
  - Jobs: `firms_nrt` (HEAD/ETag every 10 min; upsert on `(source, lat, lon, acq_date, acq_time)`); `creams_daily` (D
    hourly, D-1 at 09:00 IST, backfill from 15 Sep); `tle_refresh` (6 h); boundaries (geoBoundaries ADM1, LGD districts,
    SOI lines as MVT).
  - Routes: `/api/fires`, `/api/official`, `/api/orbits`, `/api/passes`, `/api/sources` (health from `source_fetch`).
  - Accept when: the IN-PB count equals a manual polygon join, and every row carries a `fetch_id`.
- **P0-2 Website shell and map (1 d).** Next.js + MapLibre (OFM dark through `indiaCompliant()`) + a deck.gl
  `MapboxOverlay` (interleaved). Layers: FIRMS by sensor, CREAMS events, an IST time slider, an "as of" badge per layer,
  and an About-data panel that reads provenance live.
- **P0-3 Blind-spot clock (1 d).**
  - The 24-h dial for any clicked point: in-swath passes for VIIRS×3, MODIS×2 and MSI×3, from the cached OMM.
  - Live satellite.js tracks and swath polygons, FIRMS detections by IST hour, sunset, and the Jethva curve.
  - Accept when: Sangrur passes match skyfield (§1 #6).
- **P0-4 Unseen burns (1.5–2 d).**
  - Scope: E84 c1 on 2–4 hotspot MGRS tiles, 43REQ plus its neighbours (Tarn Taran, Amritsar, Ferozepur lead the 2026
    CREAMS counts).
  - Pipeline: 20 m, WorldCover-40 mask, scene gate, haze flag, darkening + char rule (S2 doc §3.5) → PostGIS polygons.
  - Labels: `seen` against FIRMS using the §2.5 radius, `counted` against CREAMS, and the in-swath passes during the
    interval.
  - Evidence viewer: PC before/after tiles for validated items, plus bbox chips.
  - **Audit:** two people blind-label 100 random unseen clusters as burnt / tilled / unsure. Publish precision with a
    Wilson 95% CI on the About page. Only after that may anyone quote "% unseen".
- **P0-5 Investigator agent (1–1.5 d).**
  - The manual loop from the Gemini doc, with **PostGIS-backed tools instead of bbox CSV counts**:
    `fires(area, t0, t1, sensor)`, `official_events(...)`, `unseen_burns(...)`, `passes(lat, lon, t0, t1)`,
    `trajectory(origin|receptor, t)`, `aq_stations(city, hours)`, `dss_share(date)`, `cams(lat, lon)`,
    `geo_diurnal(date)`.
  - SSE stream; every citation is a `source_fetch` URL; per-model fallback chain.
  - Scheduled autonomous runs after each data refresh produce the "featured investigations", which also saves quota.

**P1: depth.**
- **P1-6 Smoke transport (1 d).**
  - Fields: the Open-Meteo grid (§3), BL-mean wind, the RK2 particles (meteo §3).
  - Runs: forward from unseen burns at 17:00 IST vs FIRMS-only at 13:30; backward from Delhi.
  - Display: TripsLayer + density + the 2-model cone. District shares come from polygons (re-run the all-fires case).
- **P1-7 GK-2A evening index (1 d).** The code exists. Add a static-source mask (cells hot on ≥3 nights), a VIS/NIR cloud
  test before 16:30 IST, and a per-scan agreement score.
- **P1-8 AQ truth panel (1 d).** An append-only data.gov.in archiver from day 1 (the feed keeps no history), the bulletin
  parser, DSS snapshots, a CAMS baseline, and the sub-index → µg/m³ inversion (AQ doc §9).
- **P1-9 Bulletin (0.5 d).** `gemini-3.8-flash` writes one EN/HI/PA JSON. A deterministic check confirms every number and
  URL appears in the facts; then Flash-Lite verifies.
- **P1-10 Next-to-burn v0 (1 d, gated).**
  - Start with a transparent risk score: days since harvest, neighbour burn rate within 2 km over 5 days, rain and wind
    forecast.
  - Show a live hit-rate backtest.
  - Call it "AI" only if a rule-seeded PU-LightGBM beats spatial-CV AUC 0.72 (S2 doc §3.7).

**P2: stretch.** S1 RTC crop state under haze; LSA SAF FRP-PIXEL; INSAT-3DS counts; a Gemini TTS voice bulletin
(`gemini-3.8-flash-tts` is listed as free; UNVERIFIED); season replay for off-season judging; CHC routing (no open
dataset exists today).

## 7. Top risks and mitigations

1. **Gemini quota or 429s while judges watch.** Community numbers: Flash about 20 RPD, Flash-Lite about 500 RPD; UNVERIFIED.
   - Lead with Flash-Lite, then the chain.
   - Per-IP limit of about 3/min, single-flight requests, and answers cached on the data snapshot.
   - Precomputed featured runs; keep 20% of RPD in reserve.
   - When every model is exhausted, show the evidence pack.
2. **Unseen-burn false positives.** Tillage mimics the NIR/SWIR drop, and a single-date CEEW rule flags 21% of cropland.
   - Bi-temporal + char rule only, on pairs ≤6 days apart.
   - WorldCover mask and haze flag.
   - Show the audited precision and the "candidate" label next to every number.
3. **Few live fires on judging day.** 2026 is the lowest season yet: Punjab 1,047 to 30 Oct vs 1,418 in 2025.
   - Add a "replay" mode that runs on real archived data: CREAMS daily layers, FIRMS area API, the S2 archive,
     historical-forecast winds.
   - Label it with its data date. Never fabricate.
4. **Wrong geography on stage.** Pakistan inflation, PAK labels, a non-SOI outline.
   - Polygon clipping, SOI lines, the label filter.
   - A test that compares IN-PB counts against a manual join.
5. **Trajectory uncertainty.** Model choice moves the centroid 155 km at −24 h, and BLH differs 9× at 17:30 IST.
   - Run the 2-model ensemble and draw the cone.
   - Validate against the CPCB PM2.5 anomaly at 0–12 h lags; never show a single path as truth.
6. **GK-2A false alarms** on hazy or cloud-edge scans (35% agreement on 30 Oct).
   - Evening window, cloud test, static mask, agreement gate.
   - Call it an "activity index" (pixel-hours), never a fire count.
7. **Upstream policies and outages.**
   - Known hazards: CelesTrak's firewall, WDQS at 1 req/min, Overpass at capacity, DSS TLS stalls, SAFAR down, PC corrupt
     scenes, FIRMS without CORS.
   - Pages read only PostGIS, filled by scheduled jobs. Show the last good value with its time.
   - Never call a third party on page load, except tile servers (OFM, EOX, PC).
8. **Compute and network.** A full S2 refresh is 1.6–4.6 GB (about 13 min on this link), and free hosts sleep or cap RAM.
   - Keep S2 to the hotspot tiles.
   - Run heavy jobs on the dev box or an in-region VM, and write the results to the hosted PostGIS.
9. **Licences.**
   - Open-Meteo's free tier is non-commercial with attribution.
   - EOX layers for 2017–2025 are CC BY-NC-SA.
   - IITM DSS is "All rights reserved": show values with a link, don't mirror the page.
   - CREAMS is "not for legal purposes".
   - The Commons photos are CC BY-SA and need credit.
   - Gemini's free tier trains on prompts.

   All of this is fine for a hackathon. Put every credit in the About panel.
10. **Mismatched "as of" times.** FIRMS lags 1–3 h, CPCB 33–95 min, S2 4.2 h, CAMS 9.8 h, GK-2A 22–39 min; DSS is daily.
    Show an as-of time per layer in the UI and inside every agent answer.

## 8. Open questions

1. Hosting target, and the judging date and time. The date decides whether replay mode is needed and whether the live
   17:00 IST window can be shown.
2. The real Gemini RPM/RPD for our key, the `models.list` output, and whether `mode=VALIDATED` works on each chain model.
3. The FIRMS area-API CSV columns: is there an `instrument` column, and is VIIRS confidence `l/n/h`?
4. Does CREAMS de-duplicate NOAA-20 against S-NPP, or ignore NOAA-20? (Affects pitch wording only.)
5. Unseen-burn precision after the P0-4 audit.
6. The data.gov.in personal-key maximum `limit`; OpenAQ coverage of CPCB stations in Punjab and Haryana.
7. The LSA SAF IODC file format (needs an account), and whether PC repairs its 10-28 scenes (the gate covers both cases).
8. Which polygon set to clip with. geoBoundaries ADM1 matches SOI per the geography doc. Pick one so the map and the
   agent always agree (geoBoundaries now, SOI_States once `pyarrow` lands), and state it on the About page.

## 9. Tested snippet: upstream smoke test (run verbatim at 17:28 UTC)

Use it as the `/api/sources` deep check, and run it before every demo. Each source gets one gentle call.

```python
"""BlindSpot upstream smoke test: one gentle call per critical source -> PASS/WARN/FAIL + latency (2026-10-30)."""
import io, os, sys, time, datetime as dt, httpx, numpy as np, pandas as pd, rasterio, planetary_computer as pc
from rasterio.windows import from_bounds
from pystac_client import Client
sys.stdout.reconfigure(encoding="utf-8")
os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
UA = {"User-Agent": "BlindSpot/0.1 (+https://github.com/<team>/blindspot)"}  # never put 'httpx' in it (data.gov.in tarpit)
c = httpx.Client(headers=UA, timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
NOW = dt.datetime.now(dt.UTC); WEEK = f"{(NOW - dt.timedelta(days=7)):%Y-%m-%d}/{NOW:%Y-%m-%d}"
SANGRUR = {"type": "Point", "coordinates": [75.84, 30.25]}

def match_radius_m(scan_km, track_km, slack_m=200.0):
    """FIRMS point -> burn polygon 'seen' radius: pixel half-diagonal + slack (VIIRS ~465-760 m, MODIS ~0.9-2.8 km)."""
    return 500.0 * np.hypot(scan_km, track_km) + slack_m

def check(name, fn):
    t = time.time()
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = "FAIL", f"{type(e).__name__}: {str(e)[:120]}"
    print(f"{status:<4} {name:<22} {time.time() - t:5.2f}s  {detail}")

def firms():
    r = c.get("https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_South_Asia_24h.csv"); r.raise_for_status()
    d = pd.read_csv(io.BytesIO(r.content), dtype={"acq_time": str}); rad = match_radius_m(d.scan, d.track)
    return "PASS", (f"{len(d)} rows {d.satellite.value_counts().to_dict()} CORS={r.headers.get('access-control-allow-origin')} "
                    f"match radius m p50/p95={rad.median():.0f}/{rad.quantile(.95):.0f}")

def stac_pair():
    e = sorted(Client.open("https://earth-search.aws.element84.com/v1").search(collections=["sentinel-2-c1-l2a"], intersects=SANGRUR, datetime=WEEK).items(), key=lambda i: i.datetime)
    p = sorted(Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=pc.sign_inplace).search(collections=["sentinel-2-l2a"], intersects=SANGRUR, datetime=WEEK).items(), key=lambda i: i.datetime)
    lag = lambda i: (dt.datetime.fromisoformat(i.properties["created"].replace("Z", "+00:00")) - i.datetime).total_seconds() / 3600
    msg = f"E84 {len(e)} items newest {e[-1].datetime:%m-%d} (lag {lag(e[-1]):.1f} h) | PC {len(p)} items newest {p[-1].datetime:%m-%d}"
    day = p[-1].datetime.strftime("%Y%m%d"); tile = p[-1].properties["s2:mgrs_tile"]          # plausibility/identity gate
    ee = next(i for i in e if day in i.id and tile in i.id)
    with rasterio.open(p[-1].assets["B12"].href) as a, rasterio.open(ee.assets["swir22"].href) as b:
        bb = (a.bounds.left + 50000, a.bounds.bottom + 50000, a.bounds.left + 51280, a.bounds.bottom + 51280)  # 64 px at 20 m
        x, y = a.read(1, window=from_bounds(*bb, transform=a.transform)), b.read(1, window=from_bounds(*bb, transform=b.transform))
    same = np.array_equal(x, y); med = float(np.median((x[x > 0] - 1000) / 1e4))
    return ("PASS" if same else "WARN"), msg + f" | PC {day} {tile} identical to E84: {same} (PC B12 median {med:.3f})"

def openmeteo():
    hv = [f"wind_{k}_component_{l}" for l in ("10m", "950hPa", "925hPa", "850hPa") for k in "uv"] + ["boundary_layer_height", "precipitation"]
    js = c.post("https://api.open-meteo.com/v1/forecast", data={"latitude": "30.25,28.61", "longitude": "75.84,77.21", "hourly": ",".join(hv),
                "models": "best_match,gfs_seamless", "forecast_days": 1, "wind_speed_unit": "ms", "timeformat": "unixtime", "cell_selection": "nearest"}).raise_for_status().json()
    nulls = sum(v is None for loc in js for k, s in loc["hourly"].items() if k.startswith("boundary") for v in s)
    return "PASS", f"{len(js)} locations, {len(js[0]['hourly']) - 1} series each, BLH nulls={nulls}"

def datagov():
    d = c.get("https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69", params={"api-key": os.getenv("DATA_GOV_IN_API_KEY") or "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b",
              "format": "json", "limit": 10, "filters[state]": "Delhi", "filters[pollutant_id]": "PM2.5"}).raise_for_status().json()
    return "PASS", f"Delhi PM2.5 stations total={d['total']} got={d['count']} feed updated {d['updated_date']} batch {d['records'][0]['last_update']} IST"

def celestrak():
    r = c.get("https://celestrak.org/NORAD/elements/gp.php", params={"CATNR": 27424, "FORMAT": "JSON"})
    if r.status_code != 200:
        return "FAIL", f"HTTP {r.status_code} {r.text[:80]} (stop: >50 errors/2 h gets the IP firewalled)"
    o = r.json()[0]; age = (NOW - dt.datetime.fromisoformat(o["EPOCH"]).replace(tzinfo=dt.UTC)).total_seconds() / 3600
    return "PASS", f"{o['OBJECT_NAME']} epoch {o['EPOCH'][:16]}Z age {age:.1f} h"

def creams():
    y = (NOW + dt.timedelta(hours=5.5) - dt.timedelta(days=1)).date()                       # D-1 in IST (same-day layer is partial)
    r = c.get("https://creams.iari.res.in/geokrishi/geoserver/wfs", params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
              "typeNames": f"geokrishi_v:{y.day}_{y:%B}_{y.year}_rice".lower(), "outputFormat": "application/json"}).raise_for_status().json()
    sats = pd.Series([f["properties"]["satellite"] for f in r["features"]]).value_counts().to_dict()
    return "PASS", f"{y} official events={len(r['features'])} by satellite {sats}"

for name, fn in (("FIRMS MODIS 24h", firms), ("S2 STAC E84+PC + gate", stac_pair), ("Open-Meteo POST", openmeteo),
                 ("data.gov.in CPCB", datagov), ("CelesTrak GP", celestrak), ("CREAMS WFS D-1", creams)):
    check(name, fn)
```

Output (`uv run python -I upstream_smoke.py`; I filtered out one pystac `UserWarning`):

```
PASS FIRMS MODIS 24h         1.65s  559 rows {'A': 448, 'T': 111} CORS=None match radius m p50/p95=1014/1249
WARN S2 STAC E84+PC + gate  11.32s  E84 4 items newest 10-30 (lag 4.2 h) | PC 3 items newest 10-28 | PC 20261028 43REP identical to E84: False (PC B12 median 6.466)
PASS Open-Meteo POST         0.85s  2 locations, 20 series each, BLH nulls=0
PASS data.gov.in CPCB        0.42s  Delhi PM2.5 stations total=59 got=10 feed updated 2026-10-30T17:04:46Z batch 30-10-2026 22:00:00 IST
PASS CelesTrak GP            1.40s  AQUA epoch 2026-10-29T22:11Z age 19.3 h
PASS CREAMS WFS D-1          0.30s  2026-10-29 official events=223 by satellite {'S-NPP': 116, 'AQUA': 107}
```

The WARN is the gate working. PC's newest scene (10-28) is not identical to Earth Search, and its B12 median is
implausible, so the pipeline must use E84 for that date and must not show the PC tile.

**Attribution reminder** (full strings are in the topic docs):
- "Fire data: NASA FIRMS"
- "Official counts: CREAMS, ICAR-IARI"
- "Orbits: CelesTrak"
- "Contains modified Copernicus Sentinel data 2026"
- "Weather data by Open-Meteo.com (CC BY 4.0)"
- "CAMS via Open-Meteo"
- "CPCB via data.gov.in (GODL-India)"
- "IITM DSS"
- "KMA GK-2A via AWS NODD"
- "Jethva 2026, doi:10.5281/zenodo.20084790"
- "Boundaries: Survey of India"
- "OpenFreeMap © OpenMapTiles, OpenStreetMap"
- "EOX s2cloudless (CC BY-NC-SA)"

## Build-time updates (lead engineer, 2026-10-30 late evening) — read these, they override anything above
- **Now installed** in services/api: `pyarrow` (SOI GeoParquet boundaries), `h5py` (GK-2A NetCDF4/HDF5 chunk reads), `pdfplumber` (CPCB bulletin PDF). Import them freely; still no other `uv add`.
- **User-Agent fixed** in `blindspot/http.py`: the `python-httpx/<ver>` token is gone (data.gov.in tarpitted it).
- **Clip every count to India's polygons** (match on shapeISO `IN-PB`, `IN-HR`, `IN-DL`, `IN-CH`), never a lat/lon box: the AOI bbox includes Pakistan's Punjab (~2.4x inflation).
- **FIRMS area API DAY_RANGE is 1..5**, not 10.
- **Official counts:** the fires module should also ingest the keyless CREAMS GeoKrishi daily WFS layers (decision table above) into its own table and expose `GET /api/fires/official?since=` (contract addition), so the UI can show official counts beside FIRMS and unseen burns. If the fires module cannot finish it, integration will.
- **Unseen burns are "candidate unseen burns"** in all copy and API fields until an audit precision with a confidence interval is published by the sentinel module.
- **Trajectories:** two-model ensemble (best_match + gfs_seamless) and an uncertainty cone; never one path as truth.
- **GK-2A:** present as an evening fire-activity index in pixel-hours with the validation numbers, not as a fire count.
