# Geostationary fire detection over Punjab (the 3-7 PM IST blind spot)

BlindSpot research note. Verified 2026-10-30; times are UTC unless marked IST (UTC+5:30).
**VERIFIED** means a real HTTP call was made today. **UNVERIFIED** means it was not callable without a key or account (the reason is given).
Test sites: Sangrur 30.25N 75.84E, Delhi 28.61N 77.21E. The scratch evidence (scripts, cached arrays) is in the agent scratchpad, not in the repo.

## 1. Verdict

1. **Feasible today, no key needed:** GK-2A AMI Level-1B full-disk NetCDF on AWS Open Data (`noaa-gk2a-pds`), from KMA's satellite at 128.2E. It scans every 10 min and is live: the 2026-10-30 15:20 slot was published at 15:43Z. Punjab falls inside one 1375x1375 HDF5 chunk, so an HTTP Range read fetches about 1 MB per channel per scan (3-4% of the file).
   - We ran our own MWIR contextual test on **2026-10-29** over the Punjab polygon:

     | Hour (IST) | 09:00-15:59 | 16:00-16:59 | 17:00-17:59 | 18:00-18:59 |
     |---|---|---|---|---|
     | Unique fire pixels per hour | 13-92 | **404** | **494** | 14 |

   - The 10-min peak was 325 pixels at 17:00 IST.
   - Evening detections are spatially consistent with the burning areas: 87-99% lie within 8 km of VIIRS day detections, against 64% for random Punjab pixels.
   - This is the recommended pipeline (section 4), estimated at ~10-14 h.
2. **Best official product (free account):** EUMETSAT LSA SAF **MSG-IODC FRP-PIXEL**, from Meteosat-9 at 45.5E.
   - Lists of fire pixels with FRP every 15 min. Archive runs 2022-01 to now, 96 files/day, 94-150 KB each.
   - The listing needs no key; a download returns 401 without an account (both VERIFIED).
   - LSA SAF lists it as "Demonstration" status. This is the source behind FIRMS's "Meteosat 9 (IODC)" map layer.
3. **ISRO:** INSAT-3DS `3SIMG_L2P_FIR` (at :00/:30) and INSAT-3DR `3RIMG_L2P_FIR` (at :15/:45) exist for 2026-10-29.
   - MOSDAC search needs no key and returns a bbox per granule. The preview JPGs also need no key, and the 3DS ones print a point COUNT.
   - The KML data needs an approved MOSDAC login.
   - The 3DS full-disk count on 29 Oct went 110 (11:30 IST), then 0 (15:00-16:00), then 10 (17:00), then 0 (18:30).
4. **Peer-reviewed history (no key):** Jethva's GK2A hourly dataset for Punjab and Haryana, 2019-2025, on Zenodo under CC-BY-4.0. The share of conf>=2 detections at or after 16:00 IST rose from 51.9% (2021) to 95.1% (2024) and 96.2% (2025).
5. **FIRMS:** its geostationary layers are map-only and provisional. The API, WMS and WFS serve only MODIS, VIIRS and Landsat (VERIFIED). The FIRMS regional VIIRS CSVs need no key and are ideal for validation.
6. **Not viable for a hackathon:** EUMETSAT FIR through the Data Store (not there), MTG-I1 at 0 deg (Punjab is at view zenith 86.5 deg), Himawari-9 (VZA 77 deg, 8.4x5.7 km pixels), and FY-4B/FY-2H (needs an NSMC form and real-name verification).

## 2. Evidence to cite

- **ISRO SAC study.** N. Singh, R. Pradhan, B. P. Shukla et al., "Evidence of shift in stubble burning timing over northwest India from geostationary satellite observations", *Current Science*, 25 Nov 2025.
  - Uses MSG SEVIRI at 15-min cadence.
  - Peak fire time moved from ~13:30 IST (2020) to ~17:00 IST (2024).
  - Trend 2012-2024: VIIRS -3,743 fires/yr against SEVIRI +43/yr.
  - Paper not fetched; numbers come from Tribune, ThePrint and Deccan Herald coverage.
- **iFOREST *Stubble Burning Status Report 2025*** (Dec 2025).
  - Uses 15-min SEVIRI from Meteosat-8 and -9.
  - More than 90% of large Punjab fires in 2024 and 2025 started after 15:00 IST, against ~3% in 2021.
  - Sentinel-2 burnt area in Punjab fell from 31,447 km2 (2022) to ~20,000 km2 (2025), about -36%. Active-fire counts fell more than 90% since 2021.
  - Haryana burnt area went from 11,633 km2 (2019) to 8,812 km2 (2025).
  - Report PDF not fetched; numbers come from Tribune and PTI coverage.
- **Jethva, H.**, "Timing the Flames", *Remote Sens.* 2026, 18(10), 1506, doi:10.3390/rs18101506. Dataset doi:10.5281/zenodo.20084790 (VERIFIED, section 8).
- **Our FIRMS numbers** (VERIFIED, keyless CSV, Punjab polygon):
  - VIIRS day passes on 29 Oct: S-NPP 08:07-08:09Z 166 detections, NOAA-20 08:28Z 123, NOAA-21 07:31Z 56.
  - Night passes (01:25-02:42 IST) on 28-30 Oct: 1-5 detections each.

## 3. Option matrix (geometry computed with pyproj geos at Sangrur; cadence and status from today's calls)

| Source | Sub-lon | VZA / pixel over Punjab | Cadence | Fire product? | Access | Verified today |
|---|---|---|---|---|---|---|
| **GK-2A AMI L1B (AWS)** | 128.2E | 66 deg / ~4.5x3.6 km (17 km2) | 10 min | No (L1B only), so we detect ourselves | Keyless S3 | **Yes**: list, range read, calibration, detection |
| **LSA SAF FRP-PIXEL IODC** | 45.5E | 48 deg / ~3.9x4.1 km (16 km2) | 15 min | **Yes** (FRP lists) | Free LSA SAF account, HTTP Basic auth | Listing yes; download 401 |
| EUMETSAT HRSEVIRI-IODC L1.5 | 45.5E | same | 15 min | No (radiances) | Free EUMETSAT key, OAuth2 | Search and metadata yes; download needs token |
| EUMETView WMS `msg_iodc:fire` (EUMETSAT FIR) | 45.5E | same | 15 min, from 2020-08 | Image only (red = active, yellow = potential) | Keyless WMS | Yes, but sparse and insensitive |
| INSAT-3DS `3SIMG_L2P_FIR` | 82E | 36 deg / ~4.1x5.1 km | 30 min | Yes (KML points) | MOSDAC account; search and preview keyless | Search and preview yes |
| INSAT-3DR `3RIMG_L2P_FIR` | 74E | 35 deg / ~4.1x5.0 km | 30 min (offset 15) | Yes (KML), noisy | same | Search yes |
| Himawari-9 AHI (AWS) | 140.7E | 77 deg / ~8.4x5.7 km | 10 min | No fire product on AWS | Keyless S3 | Listing only |
| FY-4B AGRI / FY-2H VISSR | 105E / 79E | 47 deg / 5x5.5 km; 35 deg / 5x6 km | 15 / 30 min (nominal, unverified) | FY-4A FHS (docs) | NSMC form to iusc_nsmc@cma.cn, real-name verification | No (portal reachable only) |
| MTG-I1 FCI AFM | 0E | 86.5 deg / ~34x20 km | 10 min | Yes (EO:EUM:DAT:0682) | EUMETSAT | Not useful: Punjab is at the limb |

## 4. GK-2A on AWS (recommended live pipeline)

### 4.1 Endpoints (all VERIFIED, no auth)

- **List one hour:** `GET https://noaa-gk2a-pds.s3.amazonaws.com/?list-type=2&prefix=AMI/L1B/FD/{YYYYMM}/{DD}/{HH}/gk2a_ami_le1b_{ch}`
  - Took 1.0-1.5 s.
  - Each hour folder holds 96 objects: 16 channels x 6 scans.
- **File:** `https://noaa-gk2a-pds.s3.amazonaws.com/AMI/L1B/FD/{YYYYMM}/{DD}/{HH}/gk2a_ami_le1b_{ch}_fd{res}ge_{YYYYMMDDHHMM}.nc`
  - `ch` is one of sw038, ir087, ir096, ir105, ir112, ir123, ir133, wv063, wv069, wv073 or nr013/nr016 (all at res `020`); vi004/vi005/vi008 (`010`); vi006 (`005`).
  - Full-file sizes: sw038 28.5 MB, ir112 34.9 MB.
  - A full GET took 10-48 s (0.7-2.8 MB/s from India).
- **Latency:** published 21-23 min after slot start (15:20 slot -> LastModified 15:43:20Z). The :50 slot often lands 37-39 min late.
- **Gaps:** the 06:00Z slot returned 404 on both 29 and 30 Oct. Always list before you fetch.
- **Notifications:** SNS `arn:aws:sns:us-east-1:709902155096:NewGK2AObject` (Lambda/SQS only). For the hackathon, poll the listing every 10 min instead.
- **Trimmed sample** of the 2026-10-29 hour-11 listing:
  ```
  AMI/L1B/FD/202610/29/11/gk2a_ami_le1b_sw038_fd020ge_202610291130.nc  LastModified 2026-10-29T11:53:02Z  Size 28485330
  AMI/L1B/FD/202610/29/11/gk2a_ami_le1b_ir112_fd020ge_202610291130.nc  LastModified 2026-10-29T11:53:40Z  Size 34925058
  ```

### 4.2 File facts (VERIFIED on `..._sw038_fd020ge_202610291130.nc`)

- **Pixel array:** `image_pixel_values` is uint16, 5500x5500, **chunks (1375,1375), gzip**.
  - The Punjab window `[1150:1375, 640:900]` sits entirely in chunk (0,0): 716,559 B for sw038 and 882,262 B for ir112.
  - Haryana and Delhi spill into chunk (1375,0), about 2.3-2.7 MB.
- **Range reads:** h5py over a Range-request file object read **1,048,576 B per channel** (4 GETs of 256 KiB) in 2.0-3.9 s. A full scan step (two channels) is ~1.84-2.1 MB.
- **Quality bits:** the top 2 bits are the quality flag (`0:good_pixel, 1:conditionally_usable_pixel, 2:out_of_scan_area_pixel, 3:error_pixel`). There are 14 valid bits (`number_of_valid_bits_per_pixel`).
- **Calibration attributes** (global attrs, sw038 at 11:30):
  - `DN_to_Radiance_Gain=-0.00108297`, `DN_to_Radiance_Offset=17.69998741`
  - `Teff_to_Tbb_c0/c1/c2 = -0.44784394 / 1.00065568 / -6.3382409e-08`
  - Plus `Plank_constant_h`, `light_speed`, `Boltzmann_constant_k`.
- **Navigation attributes:** `projection_type=GEOS`, `sub_longitude=2.2375121` rad (128.2 deg), `cfac=20425338.903`, `lfac=-20425338.903`, `coff=loff=2750.5`, `nominal_satellite_height=42164000`.
  - These give 56 urad per pixel: projected pixel 2004.0083 m, half-extent 5,511,022.902 m, row 0 = north.
- **Our navigation matches satpy 0.60 (`ami_l1b` reader) exactly:**
  - Sangrur -> row 1292, col 748 -> 75.8445E 30.2423N
  - Delhi -> row 1358, col 746 -> 77.2325E 28.6075N
  - The Punjab polygon covers rows 1199-1323 and cols 700-798 (4,136 px). The column axis is foreshortened near the limb.
- **Our brightness temperatures match satpy `calib_mode="FILE"` exactly** (BT3.8 293.626 K and BT11.2 272.067 K at the Sangrur pixel). satpy's default PYSPECTRAL mode reads BT3.8 0.26 K warmer.
- Central wavelengths are 3.83 um and 11.23 um (satpy `ami_l1b.yaml`).

### 4.3 Measured diurnal cycle, 2026-10-29 (Punjab polygon from geoBoundaries IN-PB, 4,136 GK2A pixels)

Unique fire pixels in each IST hour (union of the 10-min scans):

| Hour IST | 09 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 (to 18:30) |
|---|---|---|---|---|---|---|---|---|---|---|
| Fire pixels | 29 | 55 | 13 | 60 | 88 | 73 | 92 | **404** | **494** | 14 |

- Hours 09-11 had only 1-2 scans each (30-min steps), so they are not directly comparable.
- **10-min series:** 16:10 86, 16:30 156, 16:50 307, **17:00 325**, 17:10 278, 17:30 146, 17:40 52, 18:00 11, 18:30 0. Computed sunset in Sangrur is ~17:42 IST, with sun elevation 7.8 deg at 17:00. The INSAT-3DS official count shows the same shape (section 7).
- **2026-10-30** (30-min steps): a hazy, cloudy day. Median BT11 was ~288 K and median BTD 11-15 K, so the daytime test fired nothing from 09:00 to 16:30. It found 216 at 17:00, 113 at 17:30 and 22 at 18:00.

### 4.4 Validation against VIIRS (FIRMS keyless CSV)

- **Near-simultaneous comparison at 13:00-14:00 IST** (S-NPP 08:07Z, NOAA-20 08:28Z, NOAA-21 07:31Z):
  - GK2A precision 0.09-0.25 and recall 0.01-0.07, with +/-1 pixel tolerance.
  - So midday GEO detections are mostly false alarms. Their centroid is 75.4E 32.0N, along the Gurdaspur foothill cloud edges.
  - Midday stubble fires are probably too small to show in a ~17 km2 pixel (an inference).
- **Evening spatial plausibility** (fraction of GK2A detections within 8 km of any VIIRS day detection on 28-30 Oct; 64% would be expected by chance):
  - 29 Oct, 15:50-17:50 IST: 87-99%. Centroids were 74.8-75.2E 30.9-31.3N, around Ferozepur, Moga and Tarn Taran.
  - 30 Oct: 17:00 68% (solar contamination), 17:30 97%, 18:00 100%.
- **How to describe it:** an *evening fire-activity index*, strongest from 16:30 IST to after sunset. It is not a fire count and is not comparable 1:1 with VIIRS.

### 4.5 Tested code (exact file run today; `python gk2a_fire.py 202610291130` printed `17:00 IST fire_px=325 read=5.2s`)

```python
"""GK2A AMI L1B (AWS NODD, keyless) -> Punjab fire pixels. Tested 2026-10-30. deps: httpx h5py numpy scipy pyproj shapely"""
import io, datetime as dt
import numpy as np, httpx, h5py, pyproj
from scipy.ndimage import generic_filter, percentile_filter

S3 = "https://noaa-gk2a-pds.s3.amazonaws.com"
KEY = "AMI/L1B/FD/{t:%Y%m}/{t:%d}/{t:%H}/gk2a_ami_le1b_{ch}_fd020ge_{t:%Y%m%d%H%M}.nc"
ROWS, COLS = slice(1150, 1375), slice(640, 900)     # 2-km FD grid window; Punjab lies in HDF5 chunk (0,0)
WL_UM = {"sw038": 3.83, "ir112": 11.23}              # central wavelengths (same as satpy ami_l1b.yaml)
GEOS = pyproj.CRS("+proj=geos +lon_0=128.2 +h=35785863 +a=6378137 +rf=298.257024882273 +units=m")
PX, EXT = 2004.0083, 5511022.902                    # m per pixel, half-extent (from cfac/coff attrs)

class RangeFile(io.RawIOBase):                       # seekable file over HTTP Range, 256 KiB block cache
    def __init__(self, url, client, block=1 << 18):
        self.url, self.c, self.b, self.pos, self.cache = url, client, block, 0, {}
        r = client.head(url); r.raise_for_status(); self.size = int(r.headers["content-length"])
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = [off, self.pos + off, self.size + off][whence]; return self.pos
    def readinto(self, buf):
        n = min(len(buf), self.size - self.pos); out = bytearray()
        while len(out) < n:
            i, o = divmod(self.pos + len(out), self.b)
            if i not in self.cache:
                s = i * self.b; r = self.c.get(self.url, headers={"Range": f"bytes={s}-{min(s + self.b, self.size) - 1}"})
                r.raise_for_status(); self.cache[i] = r.content
            out += self.cache[i][o:o + n - len(out)]
        buf[:n] = out; self.pos += n; return n

def read_bt(t: dt.datetime, ch: str, client: httpx.Client) -> np.ndarray:
    with h5py.File(RangeFile(f"{S3}/{KEY.format(t=t, ch=ch)}", client), "r") as h:
        d, a = h["image_pixel_values"], dict(h.attrs)
        dn = d[ROWS, COLS].astype(np.int64); bits = int(d.attrs["number_of_valid_bits_per_pixel"][0])
    qf, dn = dn >> 14, dn & ((1 << bits) - 1)       # top 2 bits = quality flag (0 good)
    rad = a["DN_to_Radiance_Gain"][0] * dn + a["DN_to_Radiance_Offset"][0]   # mW m-2 sr-1 (cm-1)-1
    hh, c, k, wn = a["Plank_constant_h"][0], a["light_speed"][0], a["Boltzmann_constant_k"][0], 1e6 / WL_UM[ch]
    with np.errstate(invalid="ignore", divide="ignore"):
        te = (hh * c / k) * wn / np.log(2 * hh * c * c * wn ** 3 / (rad * 1e-5) + 1)
    bt = a["Teff_to_Tbb_c0"][0] + a["Teff_to_Tbb_c1"][0] * te + a["Teff_to_Tbb_c2"][0] * te ** 2
    return np.where((qf == 0) & (rad > 0), bt, np.nan)

def lonlat():
    tr = pyproj.Transformer.from_crs(GEOS, "EPSG:4326", always_xy=True)
    cc, rr = np.meshgrid(np.arange(COLS.start, COLS.stop), np.arange(ROWS.start, ROWS.stop))
    return tr.transform(-EXT + (cc + 0.5) * PX, EXT - (rr + 0.5) * PX)        # row 0 = north

def _med(x):
    x = x[np.isfinite(x)]; return np.median(x) if x.size >= 8 else np.nan
def _mad(x):
    x = x[np.isfinite(x)]; return 1.4826 * np.median(np.abs(x - np.median(x))) if x.size >= 8 else np.nan

def detect(bt38, bt11):
    """Prototype contextual MWIR test. Reliable from ~16:30 IST (low sun); daytime needs a VIS cloud mask."""
    btd = bt38 - bt11
    p90 = percentile_filter(np.nan_to_num(bt11, nan=150.0), 90, size=15)
    clear = np.isfinite(btd) & (bt11 > 283.0) & (bt11 > p90 - 4.0)
    cand = clear & (btd > 4.0)
    bg = np.where(clear & ~cand, btd, np.nan)
    m = generic_filter(bg, _med, size=15, mode="constant", cval=np.nan)
    s = generic_filter(bg, _mad, size=15, mode="constant", cval=np.nan)
    fire = cand & (btd > m + np.maximum(3.0 * s, 3.0))
    return fire, np.where(fire, btd - m, np.nan)     # mask, BTD excess (K)

if __name__ == "__main__":
    import sys, time
    from shapely import contains_xy
    from shapely.geometry import shape
    t = dt.datetime.strptime(sys.argv[1], "%Y%m%d%H%M")
    with httpx.Client(timeout=60) as cl:
        t0 = time.time(); b38, b11 = read_bt(t, "sw038", cl), read_bt(t, "ir112", cl); dl = time.time() - t0
        gb = cl.get("https://www.geoboundaries.org/api/current/gbOpen/IND/ADM1/").json()["simplifiedGeometryGeoJSON"]
        feats = cl.get(gb, follow_redirects=True).json()["features"]
    punjab = shape(next(f for f in feats if f["properties"]["shapeISO"] == "IN-PB")["geometry"])
    lon, lat = lonlat(); inside = contains_xy(punjab, np.nan_to_num(lon), np.nan_to_num(lat))
    fire, exc = detect(b38, b11); f = fire & inside
    print(f"{t:%Y-%m-%d %H:%M}Z = {t + dt.timedelta(hours=5, minutes=30):%H:%M} IST  fire_px={int(f.sum())}  read={dl:.1f}s")
    for y, x, e in list(zip(lat[f], lon[f], exc[f]))[:3]: print(f"   {y:.3f}N {x:.3f}E excess={e:.1f}K")
```

Hourly aggregation (this exact excerpt was tested; it returned `{16: 307, 17: 415}` for the 11:20/11:30/11:40Z scans):
```python
IST = dt.timedelta(hours=5, minutes=30); hours = {}
for t in scans_utc:                                   # e.g. every 10 min, 03:30-13:30Z; skip 404 slots
    fire, _ = detect(read_bt(t, "sw038", cl), read_bt(t, "ir112", cl))
    hours.setdefault((t + IST).hour, []).append(fire & inside)
counts = {h: int(np.any(v, axis=0).sum()) for h, v in sorted(hours.items())}
```
Run it with `uv run --with h5py python gk2a_fire.py ...`. h5py is not in the API env, so add it with `uv add h5py` when productionising. satpy is not needed; it was used only for cross-checking and drags in pyspectral.

### 4.6 Pipeline and effort

1. A worker polls the S3 listing every 10 min. No auth.
2. Range-read sw038 and ir112 (later also nr016/vi006 for a daytime cloud mask), calibrate, and run `detect`.
3. Clip to the state polygons and write rows to PostGIS: `geo_fire_px(sensor, scan_utc, geom Point, btd_excess, bt38, bt11, clear_frac, src_url)`.
4. API: `/geo-fires/diurnal?date=&state=PB` returns `[{hour_ist, pixels, scans, clear_frac}]`; `/geo-fires/pixels?from=&to=` returns GeoJSON (~4 km footprints for a deck.gl GridCell or Polygon layer).

Effort:

| Task | Hours |
|---|---|
| Ingest worker (code above exists) | 3-4 |
| Daytime VIS/NIR cloud test, plus a static hot-spot mask (refineries and power plants) from September scans | 3-4 |
| Tuning against keyless FIRMS VIIRS at 13:30 IST | 2 |
| Backfill from 1 Oct, 12:00-18:30 IST at 10-min steps (~1,200 steps, ~2.2 GB, ~1.5 h wall at ~4.6 s/step sequential) | 1-2 |
| UI chart | 2 |
| **Total** | **~10-14** |

### 4.7 Gotchas

- **Daytime false alarms.** In daylight the 3.8 um band includes reflected sunlight, so reflective clouds and haze raise BTD (30 Oct: median BTD 11-15 K). Treat detections before ~16:30 IST as low confidence until a VIS/NIR cloud test is added; the evening window is the clean one.
- **Northern edge.** Pixels at about 32N near the Himalayan foothills (Gurdaspur, Pathankot) are cloud-edge prone.
- **Pixel size.** One ~17 km2 pixel can hold many fields. Report fire pixels or pixel-hours, never "fires".
- **Scan timing.** Punjab is scanned about 2 min after slot start (the north-to-south FD scan starts at the slot time).
- **Missing slots** return HTTP 404 (06:00Z on 29 and 30 Oct). Wrap reads in try/except.
- **Sweep axis.** Use the default `sweep=y` geos. The row/column centre convention is `(i+0.5)*PX` from the edge, as in the code.

## 5. LSA SAF MSG-IODC FRP-PIXEL (official EUMETSAT SAF product)

- **Listing (VERIFIED, keyless, h5ai HTML, 0.8-2.7 s):** `https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MSG-IODC/FRP-PIXEL/HDF5/{YYYY}/{MM}/{DD}/`
  - Each day holds 96 ListProduct and 96 QualityProduct files. 2024-11-05 had 95; 2025-10-25 had 96.
  - Years present: 2022/01 through 2026/10/30.
  - Parse `<a href=".../HDF5_LSASAF_...">` together with `<td class="fb-s">` for sizes.
- **File (UNVERIFIED download):** `https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MSG-IODC/FRP-PIXEL/HDF5/{YYYY}/{MM}/{DD}/HDF5_LSASAF_MSG-IODC_FRP-PIXEL-ListProduct_IODC-Disk_{YYYYMMDDHHMM}`
  - No file extension.
  - Without credentials the request returns `HTTP 401` with an HTML login page (VERIFIED).
  - Auth is HTTP Basic, per the server Readme (`wget --user=XXX --password=XXX ...`).
  - WebDAV and THREDDS are also offered; `thredds.lsasvcs.ipma.pt` returns 401 without credentials (VERIFIED).
- **Register:** https://mokey.lsasvcs.ipma.pt/auth/signup (HTTP 200). The account is free. Approval time is unknown, so register now.
- **Trimmed sample listing, 2026-10-29:** `..._202610290000` 94 KB (LastModified 00:31) · `..._202610291130` 135 KB (12:30) · `..._202610291300` 150 KB (13:30). Files land in hourly batches about 30-80 min after the slot (at 16:21Z the newest file was the 15:00 slot).
- **Volume:** ~9-14 MB/day for the whole IODC disk, so a whole season (61 days) is under 1 GB.
- **Contents (UNVERIFIED for IODC; from the CAMS GOES/Himawari FRP PUM v3.0, Table 2, which says the fields are "the same as … Meteosat SEVIRI FRP-Pixel" at LSA SAF):**
  - One row per fire pixel. Real value = stored int16 / scale factor (also stored as the `SCALING_FACTOR` attribute per the LSA SAF POF).
  - `FRP` (MW, /10), `FRP_UNCERTAINTY` (/100), `LATITUDE` and `LONGITUDE` (deg, /100), `FIRE_CONFIDENCE` (0-1, /100), `BT_MIR` and `BT_TIR` (K, /10), `PIXEL_SIZE` (km2, /100), `PIXEL_VZA` (/100), `ACQTIME` (HHMM UTC), `ABS_LINE`, `ABS_PIXEL`.
  - Decode sketch (UNVERIFIED): `v = h[name][()] / h[name].attrs.get("SCALING_FACTOR", 1)`.
- **Pipeline:** list the day -> GET 4 files/hour with Basic auth -> h5py -> filter the Punjab polygon -> hourly counts and FRP sums.
  - Effort is ~3-4 h once the account works. This is the cleanest "official" diurnal series and gives FRP, so energy per hour can be summed.
  - It uses the same sensor and cadence as the ISRO SAC and iFOREST analyses (SEVIRI at 15 min). Press coverage does not say which product they used.
- **Caveats:** the LSA SAF fire-products page lists MSG-IODC FRP-Pixel as **Demonstration** (2017-NRT; this server path starts in 2022). It underdetects low-FRP fires (Wooster et al. 2015, ACP 15:13217).

## 6. EUMETSAT Data Store and EUMETView (Meteosat-9 IODC, SEVIRI)

- **Collections (VERIFIED, keyless):** `GET https://api.eumetsat.int/data/browse/collections?format=json` returned 191 collections in 6.7 s (first call).
  - IODC entries: `EO:EUM:DAT:MSG:HRSEVIRI-IODC` (340,820 products; date `2017-02-01/`; platforms MSG1 = Met-8 with 186,210 and MSG2 = Met-9 with 154,610), `EO:EUM:DAT:MSG:CLM-IODC`, `EO:EUM:DAT:MSG:CTH-IODC`.
  - **There is no FIR or FRP IODC collection.** `EO:EUM:DAT:MSG:FIR-IODC` exists only in EUMETSAT's vocabulary (EUMETCast/GRIB).
  - MTG Active Fire Monitoring is `EO:EUM:DAT:0682` (netCDF) and `0801` (CAP), 0-degree disc only.
- **Search (VERIFIED, keyless, 1.2-2.0 s):**
  ```
  GET https://api.eumetsat.int/data/search-products/1.0.0/os?pi=EO:EUM:DAT:MSG:HRSEVIRI-IODC&dtstart=2026-10-29T06:00:00Z&dtend=2026-10-29T14:00:00Z&c=100&sort=start,time,1&format=json
  ```
  - That query returned `totalResults: 32` (15-min cadence).
  - Sample feature: `id=MSG2-SEVI-MSG15-0100-NA-20261029112739.512000000Z-NA`, `date 2026-10-29T11:15:09.665Z/11:27:39.512Z`, `productInformation.size 178655` (zip size; KB units inferred), `extraInformation.md5`, `links.data[0].href=https://api.eumetsat.int/data/download/1.0.0/collections/EO%3AEUM%3ADAT%3AMSG%3AHRSEVIRI-IODC/products/{id}`, and a `sip-entries` `.../entry?name={id}.nat`.
  - OSDD limits: `c` < 501; `dtend - dtstart` <= `P101DT16H`.
  - A whole day (2026-10-28) returned 96 products. At 16:25Z the newest slot was 16:00Z (scan end 16:12:39Z).
- **Metadata (VERIFIED, keyless):** `.../products/{id}/metadata?format=json` gives `processingDate 11:27:56Z` (17 s after scan end), `timeliness NOMINAL`, `format MSGNative`, size 264,824 (.nat, KB inferred). Rights are `NRTLicense`, `GeneralLicense`.
- **Download (needs auth):**
  - No `Authorization` header: `404 "No matching resource found for given API Request"` (VERIFIED).
  - Bogus bearer token: `401 code 900901 Invalid Credentials` (VERIFIED).
  - `POST https://api.eumetsat.int/token` with `grant_type=client_credentials` but no Basic credentials: `401 {"error":"invalid_client"}` (VERIFIED).
  - Get a key and secret at https://api.eumetsat.int/api-key/ (HTTP 200). Register through https://user.eumetsat.int/; note `eoportal.eumetsat.int` gave DNS NXDOMAIN from this network today.
  - Library: `eumdac` 3.1.1 installs fine (UNVERIFIED download):
    ```python
    tok = eumdac.AccessToken((KEY, SECRET)); coll = eumdac.DataStore(tok).get_collection("EO:EUM:DAT:MSG:HRSEVIRI-IODC")
    for p in coll.search(dtstart=start, dtend=end): ...  # p.open() streams the zip
    ```
  - Decode with satpy `seviri_l1b_native` using IR_039 and IR_108 (UNVERIFIED).
  - Volume is ~175 MB zip per 15-min slot, so a full disk is ~17 GB/day. It is impractical without the Data Tailor ROI crop: `/epcs/1.0.0/*` returns 404 without a token, so Data Tailor is UNVERIFIED.
- **EUMETView WMS (VERIFIED, keyless):** `https://view.eumetsat.int/geoserver/wms`, layer `msg_iodc:fire`, title "Active Fire Monitoring (GRIB) - MSG - Indian Ocean".
  - `time` dimension `2020-08-01T00:00Z/now/PT15M`; default was 2026-10-30T16:00Z at 16:25Z.
  - Legend: red = active fire, yellow = potential fire.
  - GetMap works in EPSG:4326 and **EPSG:3857** (256 px tile, 1.8 s), so MapLibre can use it directly:
    `...&request=GetMap&layers=msg_iodc:fire&styles=&crs=EPSG:3857&bbox={bbox-epsg-3857}&width=256&height=256&format=image/png&transparent=true&time=2026-10-29T11:45:00Z`
  - Reality check, 29 Oct over all of India: only 8 coloured pixels at 16:30 IST and 45 at 17:15 IST. Yellow pixels appeared at 31.05-31.12N 74.85-74.88E, the same Ferozepur/Zira cluster that GK2A and INSAT flagged.
  - The PNG is anti-aliased (varying alpha). GetFeatureInfo returned `RED_BAND/GREEN_BAND/BLUE_BAND = 0` even on a coloured pixel. One GetMap returned HTTP 500. **Use it for visuals only, not counts.**
- **Ops note:** NOAA OSPO notices show short Met-9 IODC outages, e.g. 2026-01-21 01:00-01:45Z (ground segment) and a 27 h safe mode on 2026-07-15/16. No announced IODC end date was found.

## 7. ISRO INSAT-3DS / INSAT-3DR via MOSDAC

- **Search (VERIFIED, keyless, 0.3-1.0 s; the endpoint used by the official `mdapi.py`):**
  `GET https://mosdac.gov.in/apios/datasets.json?datasetId=3SIMG_L2P_FIR&startTime=2026-10-29&endTime=2026-10-29`
  - Returns `totalResults` 44 and `itemsPerPage` up to 100 (page with `startIndex`).
  - Trimmed entry: `{"identifier":"3SIMG_29OCT2026_1230_L2P_FIR_V01R00.kml","id":"18716308","dcDate":"2026-10-29T12:30:00Z/2026-10-29T13:00:00Z","boundbox":[{"west":"74.895","south":"31.251","east":"74.895","north":"31.251"}]}`
  - A granule with no fire carries the sentinel bbox `80.0,-10.0,80.0,-10.0`.
  - **Bug:** `dcDate` uses a 12-hour clock (the 1300 granule shows `01:00Z`). Parse the time from `identifier`.
  - 29 Oct evening bboxes sat over Punjab and Rajasthan: 16:30 IST `74.377-74.962E 30.706-31.162N`; 17:00 `74.557-76.72E 25.433-31.204N`.
- **Related datasets (VERIFIED via the same search):** `3RIMG_L2P_FIR` (41 granules, 4 MB, at :15/:45; bboxes span 14-127E, so noisy), `3SIMG_L2P_SMK` (21 granules), `3SIMG_L1B_STD` (47 granules, 19,500 MB in total), `3RIMG_L1B_STD` (41 granules, 17,778 MB).
  - The L1B bboxes confirm the sub-satellite points: 3DS at 82.0E (0.84-163.15) and 3DR at 74.0E (-7.15-155.15).
- **Preview JPG (VERIFIED, keyless, ~600 KB):**
  - 3DS: `https://mosdac.gov.in/look/3S_IMG/preview/{YYYY}/{DDMON}/3SIMG_{DDMONYYYY}_{HHMM}_L2P_FIR_V01R00.jpg`. The header prints `COUNT:N POINTS`.
  - 3DR: the same pattern with `3R_IMG`/`3RIMG` at :15/:45. No count is printed.
  - The directory listing returns 403.
- **3DS full-disk counts read from the 29 Oct previews (IST):** 11:30 110 · 12:30 70 · 13:00 38 · 13:30 3 · 14:00 7 · 14:30 2 · 15:00-16:00 0 · 16:30 5 · **17:00 10** (red cluster over Punjab) · 17:30 6 · 18:00 1 · 18:30 0.
  - This is a conservative fixed-threshold product, but it independently shows the evening Punjab peak.
- **Data download (UNVERIFIED, needs an approved account at https://mosdac.gov.in/signup/):**
  - `POST https://mosdac.gov.in/download_api/gettoken` with JSON `{"username","password"}`, which returns `access_token`/`refresh_token`.
  - Then `GET https://mosdac.gov.in/download_api/download?id={id}` with `Authorization: Bearer`.
  - Limits: 5,000 files/day per user. HTTP 429 with `type` `minute_limit` (back off 20 s) or `daily_limit`. Three bad logins lock the account for 1 h.
  - KML parsing gives points every 30 min (3DS) and 15-min sampling combined with 3DR. Effort ~3 h after approval.

## 8. Jethva GK2A hourly dataset (Zenodo, history 2019-2025)

- **Record (VERIFIED, 0.75 s):** `GET https://zenodo.org/api/records/20084790`. Licence `cc-by-4.0`, published 2026-05-12, 16 files.
  - Files: `GK2-AMI_FireHotspotsData_{YYYY}_10-11_{Punjab|Haryana}_India_v1.txt` (0.38-4.46 MB), `README.pdf`, and a JPEG map.
  - Download: `https://zenodo.org/api/records/20084790/files/{key}/content` (2.1 MB in 4.6 s).
- **Format:** 11 header lines, then whitespace-separated columns `year month day hour minute lon lat btd_ref bt38 bt112 conf`.
  - Hours are **IST**, 12-18 only (12:00-18:00 IST window), with minute always 00.
  - `conf` is 1-3. The README advises using 2-3 for quantitative work.
  - Sample row: `2025 10 01 13 00  74.3756  30.1837   7.3787 304.3759 293.6568  1`
  - Read with `pd.read_csv(path, sep=r"\s+", skiprows=11, names=cols)`.
- **Computed today, Punjab, conf>=2:**

  | Year | Detections | Share >=15h | Share >=16h | Peak hour (IST) |
  |---|---|---|---|---|
  | 2021 | 39,361 | 77.7% | 51.9% | 16 |
  | 2024 | 13,444 | 99.0% | 95.1% | 17 |
  | 2025 | 19,392 | 98.7% | 96.2% | 17 |

- **Usage:** this is the strongest "diurnal shift" chart available (published, peer-reviewed, real data). Fetch it at runtime from the Zenodo API and do not commit the files. Cite the DOI.

## 9. NASA FIRMS

- **Geostationary layers:** GOES FDC; LSA SAF / IPMA FRP-PIXEL for Meteosat-11 and **Meteosat-9 IODC**; Himawari.
  - Provisional, filtered to high confidence, ~30 min latency, visible in the FIRMS web map "Advanced Mode" only (Earthdata FIRMS blog, 2024-06, updated 2025-07).
  - **Not in the API:** the `/api/area` sources are LANDSAT_NRT, MODIS_NRT/SP and VIIRS_{SNPP,NOAA20,NOAA21}_{NRT,SP}.
  - WMS/WFS layers are also polar only (FIRMS mapserver docs).
  - `api/data_availability/csv/DEMO_KEY/ALL` returned `401 Invalid MAP_KEY.` (VERIFIED). A free MAP_KEY unlocks the area API (5,000 transactions per 10 min) but still no GEO data.
- **Keyless VIIRS regional CSV (VERIFIED, 2.1-2.3 s):** `https://firms.modaps.eosdis.nasa.gov/data/active_fire/{suomi-npp-viirs-c2|noaa-20-viirs-c2|noaa-21-viirs-c2}/csv/{SUOMI_VIIRS_C2|J1_VIIRS_C2|J2_VIIRS_C2}_South_Asia_{24h|48h|7d}.csv`
  - Header: `latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight`.
  - Use it to validate GEO detections and to show "polar sees ~150 at 13:30 IST and 1-5 at night".

## 10. Other geostationary satellites

- **Himawari-9 (140.7E):** `s3://noaa-himawari9` is keyless and holds `AHI-L1b-FLDK/`, `AHI-L2-FLDK-Clouds/`, `-ISatSS/` and `-Winds/`, but no Fire prefix (VERIFIED). Punjab is at VZA 77 deg with ~8.4x5.7 km pixels. Skip it.
- **FY-4B (105E since 2024-03-05) and FY-2H (79E):** per the NSMC notice of 2024-01-30, FY-4A moved to 86.5E as a backup with its data service stopped; a 2025 CMA deck instead places FY-4A at 123.5E, non-operational. The NSMC portal answers (`satellite.nsmc.org.cn/DataPortal/en/support/faq.html` returned 200). Downloads need a "FengYun Satellite Data Requirement Form" emailed to iusc_nsmc@cma.cn, plus real-name verification and quotas. FY-4A has a documented FHS fire product, but we found no confirmed FY-4B fire product. **Not feasible in a hackathon (UNVERIFIED).**
- **MTG-I1 (0 deg):** Punjab is at VZA 86.5 deg with ~34x20 km pixels. Useless here.

## 11. If the GEO pipeline slips: strongest alternative evidence

1. Use the Jethva Zenodo hourly series (section 8) for the shift chart: share at or after 16:00 IST rose from 52% (2021) to 96% (2025). Pair it with the ISRO SAC (peak ~13:30 to ~17:00 IST) and iFOREST (>90% of large fires after 15:00) citations.
2. Compare Sentinel-2 burn scars against FIRMS VIIRS: count Sentinel-2 burnt fields with no VIIRS detection within the dNBR window. This is the core BlindSpot "unseen burn" metric.
   - iFOREST: burnt area fell only ~36% from 2022 to 2025 while active-fire counts fell >90%.
   - FIRMS night passes see only 1-5 Punjab fires, against 56-166 by day.
3. Live official visuals: the INSAT-3DS FIR preview (count in the header) and the EUMETView `msg_iodc:fire` WMS time animation. Both are keyless.

## 12. Licensing and attribution

| Source | Terms | Attribution |
|---|---|---|
| GK2A on AWS NODD | "open to the public and can be used as desired" | Cite "KMA GK-2A Satellite Data, accessed {date}, https://registry.opendata.aws/noaa-gk2a-pds". Do not imply NOAA endorsement. |
| EUMETSAT Data Store / EUMETView | `NRTLicense`/`GeneralLicense` (collection rights) | Credit "contains EUMETSAT Meteosat-9 data". |
| LSA SAF | Registered free use | Cite the LSA SAF FRP product and Wooster et al. 2015. |
| MOSDAC | ISRO data policy, registered users | Credit "MOSDAC, SAC-ISRO". |
| Zenodo dataset | CC-BY-4.0 | Cite doi:10.5281/zenodo.20084790 and Jethva 2026. |
| geoBoundaries IND ADM1 | CC BY 2.5 IN (DataMeet / ECI) | Credit the source. |

## 13. Not verified / open questions

- LSA SAF IODC file contents and names (needs an account; it is unclear whether the files are bz2-compressed).
- EUMETSAT downloads and Data Tailor ROI quotas (need a key).
- MOSDAC KML schema (needs an approved account).
- FY-4B data access.
- The ISRO and iFOREST documents themselves (cited via press coverage).
- The GK2A detector is a prototype. Its thresholds (BTD>4 K, excess > max(3 sigma, 3 K), BT11>283 K) are untuned. It has no VIS cloud test and no static-source mask. It was validated on two days only.
- The post-17:40 IST collapse in detections (seen by both GK2A and INSAT-3DS on 29 Oct) needs multi-day confirmation against Jethva's 2025 18:00 share (12.4%).
