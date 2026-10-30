# Meteorology, smoke trajectories and model air quality

Research for BlindSpot. Every number below comes from a live call made on **2026-10-30 between 15:45 and 16:25 UTC** (21:15 to 21:55 IST) from the dev machine. Anything not tested is marked **UNVERIFIED**. The prototype scripts live in the session scratch dir `C:\Users\shubh\AppData\Local\Temp\claude\f--forge\375401be-2a9f-45a8-acfb-c0a0fdbd6c12\scratchpad\meteo\scripts\` (`fetch_grid.py`, `lagrangian.py`, `run_proto.py`, `test_integrator.py`, `test_doc_snippet.py`). That folder is temporary, so section 3 repeats the core code.

## TL;DR
- **Winds and BLH come from the Open-Meteo Forecast API (no key needed).** One **POST** returns the whole 0.5° grid over 26-34N, 70-80E: 357 points x 7 variables x 144 h in **2.46 s** (2.87 MB of JSON; gzip cut a smaller 17-point test 7x, 60.6 to 8.3 KB). The cap is **1000 locations per request**. GET fails with HTTP 414 once the URL passes ~8.2 KB, so **always use POST**.
- **Use `wind_u_component_<lvl>` and `wind_v_component_<lvl>`.** They work for 10m and for every hPa level. They are not listed in the docs but were verified today. They avoid the wrap-around bug you get when you interpolate wind directions.
- **Boundary-layer height (BLH) has patchy model support.** `best_match` provides it (its values match ECMWF IFS 9 km exactly), and so does `gfs_seamless` (the GFS HPBL field). It is **all null** in `ecmwf_ifs025`, `icon_seamless`, `gfs025`, AIFS, JMA, UKMO, GEM, CMA and Meteo-France. ECMWF IFS 0.25° and AIFS have **no 950 hPa level**.
- **Backtests:** the ERA5 archive has **no pressure levels**, and the API gives no error (the values come back null). The historical-forecast API has pressure levels from 2021 or earlier, but **BLH only from 2024-09-01**. The 2024 and 2025 burning seasons can therefore be replayed in full.
- **The prototype** is numpy only: RK2 integration, bilinear-in-space and linear-in-time interpolation, a BL-weighted wind, and a random walk. 500 particles x 72 h takes **0.06 s**, and 11,520 particles x 88 h takes 0.60 s.
  - **Today's result:** the air over Delhi at 21:30 IST came from **western UP and Uttarakhand** (north-east flow after a western disturbance), **not from Punjab** (Punjab plus Haryana made up 4% of particle-hours).
  - **Forecast:** winds turn north-westerly. Smoke from fires around Sangrur reaches Delhi around **16:30-18:30 IST on 31 Oct**. The smoke index at Delhi from all Punjab fires peaks on **1 Nov around 17:30 IST**.
- **The biggest sources of uncertainty are the wind level and the model, not the grid spacing.** Shift of the particle-cloud centroid at -24 h:

  | change | shift |
  |---|---|
  | 0.5° to 1° grid | 12 km |
  | 10 m wind instead of the BL-mean | 40 km |
  | 925 hPa wind instead of the BL-mean | 221 km |
  | GFS instead of `best_match` | 155 km |

  So spend the API budget on a **2-model ensemble**, not on a finer grid.
- **Air quality from the Open-Meteo AQ API (CAMS global)** works for Delhi, Ludhiana and Chandigarh.
  - PM is native **0.4° (blocky)** and CO is 0.1°. The forecast runs about 5 days. CAMS runs every 12 h and appears about 9.8 h after the model start time.
  - **There is no fire-specific variable over India.** `pm10_wildfires` exists but covers Europe only, so it is null here.
  - CAMS takes its fire emissions from GFAS, which uses MODIS and VIIRS polar-orbiter detections. **CAMS therefore has the same blind spot for evening burns, which is BlindSpot's USP.**

## 1. Open-Meteo Forecast API
`GET|POST https://api.open-meteo.com/v1/forecast` - keyless; licence CC BY 4.0; the free tier is **non-commercial only**. You must show a link next to the data: `<a href="https://open-meteo.com/">Weather data by Open-Meteo.com</a>` (open-meteo.com/en/licence).

| param | we use | verified behaviour |
|---|---|---|
| `latitude`,`longitude` | comma lists | up to 1000 points. 1001 gives `400 {"error":true,"reason":"Only up to 1000 locations can be requested at once"}` |
| `hourly` | comma list | an invalid name gives 400 with a Swift type name in `reason` (e.g. for `u_component_of_wind_925hPa`) |
| `past_days`/`forecast_days` | 3/3 (3/5 costs no more) | server allows past 0-**93** (docs say 92: `"Allowed range 0 to 93. Given 120."`) and forecast 0-16 (17 gives `"Forecast days is invalid. Allowed range 0 to 16. Given 17."`) |
| `forecast_hours`/`past_hours` | optional | counted from the current hour (we got 16:00Z, 17:00Z) |
| `wind_speed_unit` | `ms` | the default is km/h, and that also applies to u/v components |
| `timeformat`,`timezone` | `unixtime`, `GMT` | `Asia/Kolkata` gives `utc_offset_seconds:19800` |
| `cell_selection` | `nearest` for grids | the default `land` can move the point. With nearest, the returned cell was at most 0.056° from the request (IFS 9 km grid) |
| `models` | `best_match` or explicit | if you list several models, each key gets a `_<model>` suffix |
| `current` | optional | 15-min block, e.g. `{"time":"2026-10-30T21:30","interval":900,"wind_speed_10m":0.73,"wind_direction_10m":16,...}` |

**Multi-location response.** You get a JSON *array* in request order. A single location returns a plain object. **The first element has no `location_id`** (it is implicitly 0), so rely on the array order. `elevation` comes from a DEM at the requested point, not from the model's terrain.
```json
[{"latitude":30.263618,"longitude":75.87822,"generationtime_ms":0.03,"utc_offset_seconds":0,"timezone":"GMT","elevation":239.0,
  "hourly_units":{"time":"unixtime","wind_u_component_925hPa":"m/s"},"hourly":{"time":[1793376000,1793379600],"wind_u_component_925hPa":[-0.56,-0.78]}},
 {"latitude":28.576448,"longitude":77.18678,"elevation":217.0,"location_id":1,"hourly":{"time":[...],"wind_u_component_925hPa":[-0.56,-0.45]}},
 {"latitude":30.75571,"longitude":76.82408,"elevation":345.0,"location_id":2,"hourly":{"...":[2.23,1.68]}}]
```
**Transport.**
- **GET:** a path plus query of 8,174 B worked; 8,274 B gave **414**, returned as nginx HTML rather than JSON. 357 points needs about 4 KB of URL, but 1000 points needs about 12 KB.
- **POST:** both a form body (same parameter names) and a JSON body (`{"latitude":[..],"longitude":[..],"hourly":[..]}`) returned 200. A 40 KB body was fine.
- **gzip:** honoured (60.6 KB shrank to 8.3 KB on the wire).

**Free-tier limits** (open-meteo.com/en/pricing and /en/terms, checked today): **600 calls/min, 5,000/h, 10,000/day, 300,000/month** (the monthly limit is "not enforced" yet). Limits apply per IP and per application.

**How calls are counted (call weight).** Each request costs the sum over its locations of `max(1, max(1, nDays/14) * nVarsModels/10)`, where nVarsModels is the number of variables times the number of models.
- This formula is reconstructed from Open-Meteo's server unit tests (`calculateQueryWeight`; mirror cited below), the pricing-page examples (15 variables over 2 weeks = 1.5 calls) and a maintainer comment ("minimum 14 days").
- **It was not checked against the live rate limiter**, and the shape of a 429 response is **UNVERIFIED** (we chose not to trigger one).
- Result: **with up to 10 variables and up to 14 days, each location costs one call**. The 357-point grid is 357 calls; asking for 2 models doubles that.

**Variables that returned values today** (`best_match`, Delhi and Sangrur):
- Wind components: `wind_u_component_{10m,950hPa,925hPa,850hPa,700hPa}` and `wind_v_component_*`. They match u=-s·sin(dir), v=-s·cos(dir) to ±0.04 m/s.
- Wind speed and direction: `wind_speed_*` and `wind_direction_*`. Direction is the direction the wind blows **from**.
- Mixing and temperature: `boundary_layer_height` (m), `temperature_{2m,80m,120m,180m,950hPa,925hPa,850hPa}`, `geopotential_height_<lvl>hPa`.
- Other: `vertical_velocity_850hPa` (m/s), `wind_speed_{80m,100m,120m,180m}`, `precipitation`, `surface_pressure`, `cape`, `convective_inhibition`, `visibility`, `sensible_heat_flux`.

**Which model supplies which variable at Delhi.** Tested in one request: 14 models, 6 variables, `past_days=1&forecast_days=16`; 2.2 s.

| model | BLH | 950 hPa | 925 hPa | 10 m | last non-null hour |
|---|---|---|---|---|---|
| `best_match` | yes | yes | yes | yes | ≥11-14 23Z (end of request) |
| `gfs_seamless` / `gfs_global` | yes (GFS HPBL) | yes | yes | yes | ≥11-14 23Z |
| `gfs025` | no | yes | yes | no | ≥11-14 23Z |
| `ecmwf_ifs025` | **no** | **no** | yes | yes | 11-14 02Z |
| `ecmwf_ifs` (9 km) | yes | no | **no** | yes | 11-14 00Z |
| `ecmwf_aifs025_single` | no | no | yes | yes | 11-14 11Z |
| `icon_seamless` / `icon_global` | no | yes | yes | yes | **11-07 00Z (7.5 d)** |
| `ukmo_seamless` / `gem_seamless` / `cma_grapes_global` / `meteofrance_seamless` | no | yes | yes | yes | 11-06 00Z / 11-09 02Z / 11-03 20Z / 11-03 12Z |
| `jma_seamless` | no | **no** | yes | yes | 11-10 05Z |

The 850 hPa level was confirmed for `best_match`, `gfs_seamless`, `icon_seamless` and `ecmwf_ifs025`. The 700 hPa level was only fetched for `best_match`.

**What `best_match` is made of** (exact-match test over 0-7 days):
- **BLH, 10 m wind and precipitation equal `ecmwf_ifs` in 100% of hours.**
- **Pressure-level variables match no single model exactly.** They are closest to ICON: for 925 hPa wind speed, r=0.93 and MAE 0.63 m/s; for T925, r=0.97. After day 7 they match GFS exactly 80% of the time.
- So the vertical profiles in `best_match` **mix models**. That is fine for advection. For **inversion and gradient diagnostics, use one model**: `icon_seamless` has T2m, 80, 120 and 180 m and all hPa levels; `gfs_seamless` lacks 180 m; `ecmwf_ifs` has no height or pressure levels.
- **Pin `models=` explicitly so runs are reproducible.**

**Height of each level.** Mean `geopotential_height` over 4 points and 6 days (spread ±30 m): 950 = 590 m, 925 = 820 m, 850 = 1545 m, 700 = 3170 m ASL. The plains sit at about 210-250 m, so the heights above ground are about 350, 580, 1300 and 2930 m. **Over the Himalaya these levels are underground, but the API still returns values.** Mask them using `Z_ASL - elevation`.

**Latency** (from India to Open-Meteo):

| request | size | latency | payload |
|---|---|---|---|
| 2 locations, 16 variables, 48 h (GET) | 2 x 16 x 48 | 0.80 s | 10.9 KB |
| **0.5° grid, 1 POST** (7 vars: u/v 10m, 925, 850 + BLH; past 3 / forecast 3) | 357 x 7 x 144 | **2.46 s** | 2.87 MB |
| same grid, 4 parallel POSTs (u/v 950, 700, T2m, T925, T850, precip) | 357 x 8 x 144 | **1.79 s wall** | 4 x 0.74 MB |
| 1° grid, `gfs_seamless`, 9 variables | 99 x 9 x 144 | 1.65 s | 0.97 MB |

**Model-run metadata** (no key; use it to refresh only when a new run lands): `https://api.open-meteo.com/data/{ecmwf_ifs025|ecmwf_ifs|dwd_icon|ncep_gfs025|ncep_gfs013}/static/meta.json` and `https://air-quality-api.open-meteo.com/data/cams_global/static/meta.json`. Each returns `last_run_initialisation_time`, `last_run_availability_time`, `update_interval_seconds`, `temporal_resolution_seconds` and `data_end_time` (unix seconds). Values seen at about 16:10Z:

| meta | latest run | available at | latency | time step | horizon |
|---|---|---|---|---|---|
| ecmwf_ifs025 | 10-30 06Z | 13:19Z | 7.3 h | 3 h | 147 h |
| ecmwf_ifs | 06Z | 12:22Z | 6.4 h | - | 145 h |
| dwd_icon | 12Z | 15:39Z | 3.6 h | 1 h | 181 h |
| ncep_gfs025 | 06Z | 12:43Z | 6.7 h | 1 h | 385 h |
| cams_global | 00Z | 09:48Z | 9.8 h | 1 h (docs: 3-hourly native) | 121 h |

## 2. Historical winds for backtests

| API | pressure-level winds | BLH | notes |
|---|---|---|---|
| `archive-api.open-meteo.com/v1/archive` | **no.** `wind_speed_925hPa` returns 200, unit `"undefined"`, 72/72 null | yes | Has 10 m and 100 m winds (including `wind_u_component_100m`). `models=era5` ends 10-24 23Z (about 6 days behind). The default `best_match` reaches today because it uses IFS. |
| `historical-forecast-api.open-meteo.com/v1/forecast` | **yes.** `gfs_seamless` and `best_match` back to at least 2021-11-01; `icon_seamless` from 2023; `ecmwf_ifs025` from 2024 (no 950 hPa) | **from 2024-09-01 01Z (`best_match`) and 2024-09-02 12Z (`gfs`)** | Same parameters as the forecast API, plus `start_date`/`end_date`. About 1.0 s per day-request. |
| `previous-runs-api.open-meteo.com/v1/forecast` | no (`wind_speed_925hPa_previous_day1` gives 400) | yes (`boundary_layer_height_previous_day1`) | Useful for checking forecast skill on surface variables only. |
| NOMADS `filter_gfs_0p25.pl` (NOAA GRIB2) | yes, any level, 0.25° | `HPBL` | No key. One forecast hour for the domain is 30 KB and took 3.7 s. Readable with rasterio (GDAL GRIB driver: 17 bands, 33x41 grid). How long NOMADS keeps old runs and its rate policy are **UNVERIFIED**. |

Working calls (all returned 200 today):
```
https://archive-api.open-meteo.com/v1/archive?latitude=28.61&longitude=77.21&start_date=2025-11-01&end_date=2025-11-03&hourly=wind_speed_10m,wind_direction_10m,wind_speed_100m,wind_direction_100m,boundary_layer_height,temperature_2m,precipitation&wind_speed_unit=ms
  -> 2025-11-01T00Z: ws10 1.58 m/s, wd10 252°, ws100 3.85, BLH 75 m
https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=28.61&longitude=77.21&start_date=2025-11-01&end_date=2025-11-01&hourly=wind_u_component_925hPa,wind_v_component_925hPa,wind_u_component_850hPa,wind_u_component_950hPa,wind_u_component_10m,boundary_layer_height&models=gfs_seamless&wind_speed_unit=ms&timezone=GMT
  -> 06Z: u925 3.35, v925 -2.12, u950 3.33, u850 3.29, u10 2.5 m/s, BLH 1925 m (best_match: 0.89/-1.56/0.97/2.28/2.05, 1880 m)
https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?dir=%2Fgfs.20261030%2F12%2Fatmos&file=gfs.t12z.pgrb2.0p25.f003&var_UGRD=on&var_VGRD=on&var_HPBL=on&var_TMP=on&lev_10_m_above_ground=on&lev_950_mb=on&lev_925_mb=on&lev_850_mb=on&lev_700_mb=on&lev_surface=on&lev_2_m_above_ground=on&subregion=&toplat=34&leftlon=70&rightlon=80&bottomlat=26
```
For the 2021-2023 seasons, take winds from historical-forecast `gfs_seamless` and BLH from the ERA5 archive.

## 3. Lagrangian particle prototype (numpy)

**Method**
- **Fields:** the two `best_match` grid fetches above (0.5°, 2026-10-27 00Z to 11-01 23Z, hourly, no NaNs).
- **BL-weighted wind:** `V_bl = (1/h)∫₀ʰ V(z)dz` with `h = max(BLH, 100 m)`. V(z) is piecewise-linear through 10 m, 950, 925, 850 and 700 hPa, with level heights `Z_ASL - elevation`. Underground levels are dropped. V is held constant below 10 m and above 700 hPa. Precomputing it for the full grid (144x17x21) takes 0.11 s.
- **Integration:** Heun RK2 with dt = 15 min. Particles leaving the domain are frozen and flagged.
- **Diffusion:** a horizontal random walk with σ = sqrt(2·K·dt), K = 1e4 m²/s.
- **Region labels:** geoBoundaries gbOpen IND ADM2 (736 districts, ODbL, built from LGD), IND ADM1 (CC BY 2.5 IN, DataMeet) and PAK ADM1/ADM2 (public domain), via `geoboundaries.org/api/current/gbOpen/{IND|PAK}/{ADM1|ADM2}/` and the `simplifiedGeometryGeoJSON` link (5-8 MB, 1-3.4 s).

Core code. This block was copied out of this doc and run by `test_doc_snippet.py`. It gave output bit-identical to the prototype (same `bl_mean_wind` and particle tracks, plus a live `fetch` call):
```python
import numpy as np, httpx
R, DEG = 6_371_000.0, 180 / np.pi
Z_ASL = {"950": 590.0, "925": 820.0, "850": 1545.0, "700": 3170.0}   # measured geopotential heights, NW India

def fetch(pts, hourly, model="best_match", past_days=3, forecast_days=3):      # pts: [(lat, lon)] <= 1000
    r = httpx.post("https://api.open-meteo.com/v1/forecast", timeout=180, data={
        "latitude": ",".join(f"{a:g}" for a, _ in pts), "longitude": ",".join(f"{b:g}" for _, b in pts),
        "hourly": ",".join(hourly), "past_days": past_days, "forecast_days": forecast_days, "models": model,
        "wind_speed_unit": "ms", "timezone": "GMT", "timeformat": "unixtime", "cell_selection": "nearest"})
    r.raise_for_status(); js = r.json()
    return js if isinstance(js, list) else [js]      # rows in request order; field (T,NY,NX) = array(...).T.reshape

def bl_mean_wind(u_levels, v_levels, z_agl, blh, floor=100.0, n_sub=24):
    h = np.maximum(blh, floor); shp = blh.shape[1:]
    Z = np.stack([np.maximum(np.broadcast_to(np.asarray(z, float), shp), 10.0) for z in z_agl])  # underground -> 10 m node
    out = []
    for comp in (u_levels, v_levels):
        U = np.stack(comp); acc = np.zeros_like(h, dtype=np.float64)
        for k in range(n_sub):                                    # midpoint rule over [0, h]
            s = h * ((k + 0.5) / n_sub)
            val = np.where(s <= Z[0], U[0], 0.0)
            for l in range(len(Z) - 1):
                z0, z1 = Z[l], Z[l + 1]
                w = (s - z0) / np.where(z1 > z0, z1 - z0, 1.0)
                val = val + np.where((s > z0) & (s <= z1), U[l] + w * (U[l + 1] - U[l]), 0.0)
            acc += val + np.where(s > Z[-1], U[-1], 0.0)
        out.append((acc / n_sub).astype(np.float32))
    return out[0], out[1]

class WindField:                                                  # u, v: (T, NY, NX); lat/lon ascending; time unix s
    def __init__(s, lat, lon, time, u, v):
        s.la0, s.dla, s.ny = float(lat[0]), float(lat[1] - lat[0]), len(lat)
        s.lo0, s.dlo, s.nx = float(lon[0]), float(lon[1] - lon[0]), len(lon)
        s.t0, s.dt, s.nt = float(time[0]), float(time[1] - time[0]), len(time); s.u, s.v = np.asarray(u, float), np.asarray(v, float)
    def inside(s, la, lo):
        return (la >= s.la0) & (la <= s.la0 + s.dla * (s.ny - 1)) & (lo >= s.lo0) & (lo <= s.lo0 + s.dlo * (s.nx - 1))
    def time_ok(s, t): return s.t0 <= t <= s.t0 + s.dt * (s.nt - 1)
    def __call__(s, la, lo, t):                                   # bilinear in space x linear in time
        fy = np.clip((la - s.la0) / s.dla, 0, s.ny - 1 - 1e-9); fx = np.clip((lo - s.lo0) / s.dlo, 0, s.nx - 1 - 1e-9)
        ft = min(max((t - s.t0) / s.dt, 0.0), s.nt - 1 - 1e-9); j, i, k = fy.astype(int), fx.astype(int), int(ft)
        wy, wx, wt = fy - j, fx - i, ft - k
        bil = lambda F: (1-wy)*(1-wx)*F[j, i] + (1-wy)*wx*F[j, i+1] + wy*(1-wx)*F[j+1, i] + wy*wx*F[j+1, i+1]
        return ((1-wt)*bil(s.u[k]) + wt*bil(s.u[k+1]), (1-wt)*bil(s.v[k]) + wt*bil(s.v[k+1]))

def _move(la, lo, u, v, dt): return la + v * dt / R * DEG, lo + u * dt / (R * np.cos(la / DEG)) * DEG

def run_particles(field, lat, lon, t_rel, t_start, duration_s, dt_s=900.0, k_h=1.0e4, seed=0, record_every_s=3600.0):
    """dt_s<0 = backward. Particles start when the clock passes t_rel. NaN in records = not released / left domain."""
    rng = np.random.default_rng(seed); la, lo, t_rel = np.array(lat, float), np.array(lon, float), np.asarray(t_rel, float)
    alive = np.ones(la.size, bool); n_steps = int(round(duration_s / abs(dt_s))); rec_every = int(round(record_every_s / abs(dt_s)))
    rec_t, rec_la, rec_lo, sig, t = [], [], [], np.sqrt(2.0 * k_h * abs(dt_s)), float(t_start)
    for n in range(n_steps + 1):
        released = (t_rel <= t + 1e-6) if dt_s > 0 else (t_rel >= t - 1e-6)
        if n % rec_every == 0:
            show = released & alive; rec_t.append(t)
            rec_la.append(np.where(show, la, np.nan)); rec_lo.append(np.where(show, lo, np.nan))
        if n == n_steps or not field.time_ok(t + dt_s): break
        act = alive & released
        if act.any():
            a, b = la[act], lo[act]
            u1, v1 = field(a, b, t); a1, b1 = _move(a, b, u1, v1, dt_s)              # Heun / RK2
            u2, v2 = field(a1, b1, t + dt_s); a2, b2 = _move(a, b, 0.5*(u1+u2), 0.5*(v1+v2), dt_s)
            if k_h > 0:                                                              # random-walk diffusion
                a2 = a2 + sig * rng.standard_normal(a2.size) / R * DEG
                b2 = b2 + sig * rng.standard_normal(b2.size) / (R * np.cos(a2 / DEG)) * DEG
            ok = field.inside(a2, b2); idx = np.flatnonzero(act)
            la[idx[ok]], lo[idx[ok]] = a2[ok], b2[ok]; alive[idx[~ok]] = False
        t += dt_s
    return {"rec_t": np.array(rec_t), "rec_lat": np.array(rec_la), "rec_lon": np.array(rec_lo), "exited": ~alive}
# usage: z_agl=[10]+[Z_ASL[k]-elev for k in ("950","925","850","700")]; ub,vb=bl_mean_wind([u10,u950,u925,u850,u700],[v...],z_agl,blh)
# res=run_particles(WindField(lat,lon,time,ub,vb), lat0, lon0, np.full(500,t_now), t_now, 72*3600, dt_s=-900)
# then label hourly positions with gpd.sjoin(points, adm2, predicate="within")
```
**Numerical checks** (`test_integrator.py`):
- A uniform 10 m/s wind for 24 h moved a particle 8.9722°, exactly as expected.
- At 72 h, changing the time step from 3600/1800/900 s to 300 s moved the endpoint by 0.7/0.2/0.0 km.
- Integrating 72 h back and then 72 h forward ended **2.0 km** from the start, over a path of about 524 km.
- 10,000 particles x 72 h took **0.51 s**.

### 3a. Backward run: where Delhi's air came from
500 particles were released over a 10 km disk at Delhi at **2026-10-30 16:00Z (21:30 IST)**.

| window | share of particle-hours by state | districts crossed (share of particles) |
|---|---|---|
| 0 to -24 h | UP 64%, Delhi 22%, Haryana 12%, Uttarakhand 2% | Meerut 84%, Muzaffarnagar 73%, Ghaziabad 72%, Baghpat 59%, North Delhi 48%, Sonipat (HR) 38%, Bijnor 37% |
| -24 to -48 h | Uttarakhand 83%, UP 15%, Himachal 2% | Tehri Garhwal 88%, Haridwar 81%, Dehradun 80%, Uttarkashi 74%, Saharanpur 45% |
| -48 to -72 h | Uttarakhand 91%, outside the domain 5%, Himachal 2% | Uttarkashi 77%, Tehri Garhwal 77%, Chamoli 64%, Rudraprayag 62%, Kinnaur (HP) 7% |

Where the particle cloud's centroid was:

| time | centroid | district | median distance from Delhi |
|---|---|---|---|
| -24 h | 29.53N 77.92E | Muzaffarnagar | 127 km |
| -48 h | 30.70N 78.69E | Tehri Garhwal | 281 km |
| -72 h | 30.62N 79.26E | Rudraprayag | 306 km |

By -72 h, 23% of particles had left through the 80E edge.

What the fetched grid shows at Delhi (28.5N, 77.0E):
- **27-28 Oct:** north-north-west winds at 1-3.5 m/s.
- **29-30 Oct:** NE to E winds with rain (western disturbance).
- **31 Oct - 1 Nov forecast:** north-westerly winds (300-330°).
- **BLH:** 0.9-1.3 km at midday on dry days (0.3-0.4 km on the rainy 29-30 Oct) and 15-55 m at night.
- **Inversion:** at 05:30 IST, T925 - T2m = +1.3 to +4 K, a surface inversion.

**Sensitivity** (same random seed). Centroid shift versus the BL-mean run at -24/-48/-72 h:

| wind used | shift (km) | left domain by 72 h | Punjab+Haryana share of particle-hours |
|---|---|---|---|
| BL-mean (baseline) | 0 / 0 / 0 | 23% | 4% |
| 10 m | 40 / 76 / 94 | 2% | 10% |
| 950 hPa | 95 / 19 / 29 | 7% | 3% |
| 925 hPa | **221** / 59 / 22 | **96%** | 4% |
| 850 hPa | **245** / 63 / 20 | **100%** | 1% |
| BL + residual layer (h = max BLH over the last 12 h) | 50 / 31 / 24 | 57% | 4% |

For 925 and 850 hPa, the -48 h and -72 h centroids are computed from the few particles still inside the domain, so those shifts understate the real difference.

Other sensitivities:
- **Diffusion K:** the spread at -24/-72 h is 3/0 km with K=0, 34/47 km with K=1e4, and 60/88 km with K=3e4.
- **Model and grid** (levels 10 m to 850 hPa only), shift versus `best_match` at 0.5°:
  - `best_match` subsampled to 1°: 12/30/29 km.
  - `gfs_seamless` at 1°: **155/140/79 km**.
- **How different the two models' winds are:** the mean difference |ΔV_bl| between `best_match` and GFS is 1.54 m/s (90th percentile 2.89). The mean |V_bl| is only 2.17 m/s (`best_match`) and 2.58 m/s (GFS). Mean BLH is 408 m versus 504 m.

### 3b. Forward 48 h from fires around Sangrur
**Fire input.** The FIRMS file `https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_South_Asia_24h.csv` (no key):
- **Download:** 200 in 2.1 s, 3,932 rows, `last-modified: 2026-10-30 15:42:19 GMT`.
- **Passes covered:** 29 Oct 08:28Z (day), 29 Oct 20:51Z (night) and 30 Oct 08:09Z (day).
- **Columns:** `latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight`.
- `acq_time` is UTC written as HHMM without leading zeros, e.g. `809`.

From the latest pass, 10 detections lay within 60 km of Sangrur (FRP sum 47.0 MW). We released 500 particles, split in proportion to FRP.

| release scenario | +6 h | +12 h | +24 h | +48 h | arrival at Delhi (within 30 km) |
|---|---|---|---|---|---|
| A: puff at the satellite hour (08Z = 13:30 IST) | Sangrur (Punjab 98%) | still Sangrur at 01:30 IST (Punjab 87%, Haryana 13%) | Kaithal (Haryana 78%) | Hapur, UP (UP 71%) | 27% of particles, first ≥1% at +27 h (31 Oct 16:30 IST) |
| B: "unseen" evening burning, 16:30-18:30 IST | Sangrur (98%) | Sangrur (Punjab 79%, Haryana 21%) | Karnal (Haryana 64%, UP 30%) | Amroha, UP (76%) | 28%, first at +26 h (31 Oct 18:30 IST) |

Share of particles that came within 30 km of other cities in scenario B: Patiala 50%, Karnal 55%, Panipat 48%, Ludhiana 14%, Chandigarh 10%, Bathinda 10%, Hisar 8%.

**Story for the demo.** At the 17:30 IST burning peak, BLH at Sangrur was **15 m** and the wind 0.2 m/s. The smoke sits in place for about 12 hours under the night-time inversion and then drifts south-east toward the NCR. FIRMS sees none of that evening release.

**All Punjab and Haryana fires feeding Delhi.** 576 NOAA-20 detections fell in Indian Punjab (574) and Haryana (2). Each got 20 particles released at its detection hour: 11,520 particles over 88 h, 0.60 s, 1% left the domain.

The **Delhi smoke index** is the sum of FRP/20 over particles within 30 km of Delhi. It is 0 until **31 Oct 15:30 IST**, then rises: 2.0 at 23:30, 13.6 on 1 Nov at 07:30, and a **peak of 34.3 at 17:30 IST on 1 Nov**. It stays at 21-26 until the data ends on 2 Nov at 04:30 IST.

**Limits of the prototype.**
- It is 2-D. Particles are assumed to be well mixed through the boundary layer.
- There is no deposition or wet scavenging (precipitation is fetched, so this is easy to add).
- Paths across the Himalaya are low-confidence, because the pressure levels there are underground and there is no terrain blocking.
- Each fire is released once, at its detection hour; there is no diurnal emission profile yet.

## 4. Open-Meteo Air Quality API (CAMS)
`GET https://air-quality-api.open-meteo.com/v1/air-quality` - no key. The multi-location format is the same as the forecast API (3 cities took 1.55 s, 46 KB).
- **Limits:** `forecast_days` 0-7. Asking for 8 gives `"Allowed range 0 to 7. Given 7."` (the message itself is buggy). `past_days` up to 93 works.
- **History:** `start_date` and `end_date` work for 2024-11-01..03 (72/72 values). 2022-08-01 came back all null.
- **Domain:** `domains=auto|cams_global|cams_europe`; India uses `cams_global`.

**Values at 2026-10-30 16:00Z** (from the `current=pm2_5,us_aqi` block and the hourly series):

| city (snapped cell) | pm2_5 µg/m³ | us_aqi | CO µg/m³ | AOD | last valid hour |
|---|---|---|---|---|---|
| Delhi (28.6, 77.2) | 65.2 | 79 | 1473 | 0.26 | 11-04 00Z (CO 02Z) |
| Ludhiana (30.9, 75.9) | 54.1 | 136 | 501 | 0.31 | 11-04 00Z |
| Chandigarh (30.7, 76.8) | 47.7 | 103 | 1615 | 0.27 | 11-04 00Z |

- **Variables that returned values over Delhi:** `pm2_5, pm10, carbon_monoxide, nitrogen_dioxide, sulphur_dioxide, ozone, aerosol_optical_depth, dust, uv_index, us_aqi(+_pm2_5 etc.), european_aqi(+sub-indices), carbon_dioxide (ppm), methane, formaldehyde, glyoxal, nitrogen_monoxide, peroxyacyl_nitrates, sea_salt_aerosol`.
- **Valid names that are null over India (Europe only):** `ammonia, pm10_wildfires, non_methane_volatile_organic_compounds, secondary_inorganic_aerosol, residential_elementary_carbon, total_elementary_carbon, pm2_5_total_organic_matter`, and pollen.
- **Resolution, from a transect at 77.2E with points every 0.05°:**
  - Returned cells snap to a 0.1° grid.
  - **pm2_5 is constant across 0.4° blocks** (51.1 up to 28.55N, 32.1 for 28.6-28.95N, 24.7 from 29.0N), which means native 0.4° with nearest-neighbour lookup.
  - **CO changes every 0.1°**, which fits the 0.1° CAMS greenhouse-gas forecast.
  - So pm2_5 cannot tell apart cities less than about 40 km apart.
- **Forecast horizon and refresh:** about 5 days (121 h). The latest run at 16Z was the 00Z run, which became available at 09:48Z; `update_interval_seconds` is 43200 (12 h).
- **AQI fields:** they lag hourly PM because they use a rolling mean. Delhi showed pm2_5 65 but us_aqi 79 after the rain. This is inferred from the values, not documented.
- **Biomass burning:** there is no dedicated tracer over India. CO, AOD, formaldehyde and glyoxal are only rough proxies. CAMS fire emissions come from GFAS. GFAS v1.4.2 has used MODIS and VIIRS since December 2025 ([ECMWF forum](https://forum.ecmwf.int/t/changes-to-cams-global-biomass-burning-emissions-based-on-fire-radiative-power-gfas-data-availability-on-ads-and-ecmwf-data-portal/14325)). Both are polar orbiters, so CAMS cannot see the ~5 PM burns either. **That makes CAMS a good baseline to beat.**
- **Attribution:** credit CAMS ("Copernicus Atmosphere Monitoring Service") and Open-Meteo (see the AQ docs page).

## 5. Recommendations
1. **Grid and domain.** Grid spacing barely matters, but leaving the domain is a real problem, so **make the domain bigger rather than the grid finer**: use **24-36N, 66-84E at 0.75°**, which is 17x25 = 425 points.
   - If you want 0.5° over the same box, it is 925 points, still under the 1000 cap.
   - Send each grid as POST chunks of ≤ 450 points, spaced at least 60 s apart, to stay under 600 calls/min. Whether a single request costing more than 600 is accepted is **UNVERIFIED**.
2. **Levels and variables.**
   - Core set, 10 variables, so each location costs 1 call: u/v at 10 m, 950, 925 and 850 hPa, plus `boundary_layer_height` and `precipitation`.
   - Add u/v at 700 hPa and `temperature_2m` and `temperature_925hPa` (14 variables, so 1.4 calls per location) only if you show the inversion layer. If you do, compute the inversion from `icon_seamless`, which has 2/80/120/180 m and hPa levels from one model.
   - Use `cell_selection=nearest`, `timeformat=unixtime`, `wind_speed_unit=ms`, and `past_days=3&forecast_days=5`; that time range costs nothing extra.
3. **Ensemble.** Fetch `best_match` and `gfs_seamless`. They are the only models tested that have both BLH and pressure-level winds for all 16 days. Run the particles on both and show the spread as the uncertainty cone, because the model difference (80-155 km) is the main error.
   - Budget: 425 points x 2 models x 4 refreshes a day = **3,400 calls/day**, about a third of the free tier, leaving room for the AQ API and backtests.
   - Refresh when `meta.json` shows a new `last_run_availability_time` (each model updates every 6 h), not on a fixed timer.
4. **Pipeline.** A backend cron fetches the grid, stores it as npz or PostGIS rasters, and precomputes V_bl.
   - Trajectories are computed on request in under 0.1 s.
   - Return `{centerline:[[lon,lat,unix]...], particles:{t:[...], lon:[[...]], lat:[[...]]}, attribution:{state/district shares}, meta:{model, run_init, fetched_at}}`.
   - The frontend draws it with deck.gl `TripsLayer` (about 100 sampled particles) and a `HeatmapLayer` or `ScreenGridLayer` of hourly particle density.
5. **Validation.** CPCB data needs a key (see section 6).
   - **Real time:** compute an hourly smoke index for each CPCB Delhi station (FRP-weighted particles within 10-30 km). Correlate it with the station's PM2.5 *anomaly* (PM2.5 minus its 7-day rolling 20th percentile) at lags of 0-12 h. Also report the partial correlation controlling for the ventilation coefficient (BLH x wind speed), since stagnation alone raises PM.
   - **Backtest on 15 Oct - 30 Nov 2024 and 2025:** use historical-forecast winds and BLH (complete from Sep 2024), FIRMS archive fires (MAP_KEY) and CPCB or OpenAQ PM2.5.
   - **Metrics:** Spearman ρ on daily values, and the hit rate for severe days (PM2.5 > 250) versus days when the smoke index is above its 90th percentile.
   - **Baselines:** CAMS `pm2_5` from the AQ API (available since at least 2024-11) and persistence.
   - **The key experiment for the pitch:** compare ρ for FIRMS-only releases with ρ for FIRMS plus Sentinel-2 "unseen" burns released at 17:00 IST. If adding the unseen burns raises the correlation, that is quantitative evidence for the BlindSpot thesis.

## 6. Unverified or blocked
- **How a 429 response looks, and whether one request costing more than 600 calls is allowed:** not provoked, to avoid using up the IP's shared quota.
- **The call-weight formula:** taken from code and docs, not measured.
- **CPCB station PM2.5:** `https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69?format=json&filters[city]=Delhi` without a key returns `400 {"error":"Authorization field missing"}`. A `DATA_GOV_IN_API_KEY` (from data.gov.in > My Account) unlocks it. That this resource ID is the real-time AQI dataset is **UNVERIFIED**.
- **OpenAQ v3:** without a key it returns `401 "A valid API key must be provided in the X-API-Key header."`. `OPENAQ_API_KEY` unlocks it.
- **FIRMS area and archive API** (for backtests): needs `FIRMS_MAP_KEY`. Not tested here; another topic covers it.
- **NOMADS:** how long it keeps runs and its rate limits. Also, does the IND ADM1 boundary follow the official Survey of India line? Check this before any public map display.

## Sources
- open-meteo.com/en/docs, /en/pricing, /en/terms, /en/licence, /en/docs/air-quality-api (read 2026-10-30)
- Multi-location announcement, "up to 1000 locations" and the weight discussion: https://openmeteo.substack.com/p/weather-data-for-multiple-locations (plus its comments)
- Server weight tests (mirror of the open-meteo repo): https://huggingface.co/spaces/soiz1/open-wether/blob/main/Tests/AppTests/OutputformatTests.swift
- GFAS adds VIIRS: https://atmosphere.copernicus.eu/drifting-away-modis-cams-global-fire-assimilation-system and the ECMWF forum post linked in section 4
- geoBoundaries API: https://www.geoboundaries.org/api/current/gbOpen/IND/ADM2/ (cite Runfola et al. 2020). NASA FIRMS / LANCE (acknowledgement required). NOAA NOMADS GFS (public domain).
