# Sentinel-2 burn scars and crop state: feasibility (BlindSpot)

I verified everything below with real calls on **2026-10-30, 15:45–16:40 UTC**, from Windows 11 on a residential connection in India. Items that were not verified are marked **UNVERIFIED**. The scripts that produced these numbers are in the ephemeral session scratch dir `…/scratchpad/sentinel2/` (`s01…s06*.py`, `snippet_burns.py`) and are not in the repo.

## 0. TL;DR

- **Both keyless catalogues work.** Use Earth Search `sentinel-2-c1-l2a` for analytics: 4.2 h sensing→STAC latency (PC still lacked the Oct-30 pass at 16:24 UTC), scale/offset metadata, no signing. Use Planetary Computer (PC) for XYZ tiles, ESA WorldCover and Sentinel-1 RTC.
- **PC serves corrupted pixels for every 2026-10-28 S2 item over Punjab** (16/16 items, R005 and R048). Reflectance is noise (DN 0–255, correlation ≈ 0 with Earth Search) while SCL is intact; the other 49 PC items from Oct 18–28 are byte-identical to Earth Search. The PC tiler and its "latest clear" mosaic render black there (still broken at 16:36 UTC). **Every scene needs a plausibility gate** (§7).
- **Work at 20 m.** Punjab plots are 0.9 ha median / 1.4 ha mean (Walker et al. 2022) ≈ 22 px at 20 m but 5.6 px at 40 m. Keep 40/80 m overviews for quick-looks and state maps.
- **dNBR alone detects harvest, not burning.** Over Oct 18→28, harvested fields >2 km from any fire had median dNBR **0.40**; VIIRS fire neighbourhoods had **0.10**.
- **Best detector: bi-temporal NIR+SWIR darkening** between clear passes ≤5 days apart (ΔB8A>0.07, ΔB11>0.07, ΔB12>0.03) **plus a post-date char state** (CEEW 2025: BAIS2 ≥ 0.78 & TBI ≤ 0.34). Footprint-level TPR 0.66 at FPR 0.35 (an upper bound, since negatives likely contain unseen burns); best single features reach AUC ≈ 0.70.
- **Burnt vs harvested-unburnt separates only weakly in real data.** A naive FIRMS-bag LightGBM scored spatial-CV AUC 0.59, worse than single features; do not ship it (§3.6).
- **Candidate "unseen burns".** In a 40×40 km hotspot (43REQ) the rule found 2,773 clusters ≥0.52 ha (4,382 ha) appearing between the Oct 23 and Oct 28 passes; **87%** (84% of area) have no VIIRS detection within 750 m in that interval. Tillage is a known confound, so present them as candidates with an evidence viewer.
- **Smoke haze is the main data risk.** The Oct 23 pass had blue ×1.9 and NDVI 0.31→0.20, yet SCL flagged ~1% cloud and Sen2Cor AOT was 0.38. B11/B12 were nearly unaffected, so lead with SWIR features.
- **Volume and time.** Punjab+Haryana spans 23 MGRS tiles (19 material). A full 20 m refresh is ≈4.6 GB for 10 assets or ≈1.6 GB for B8A/B11/B12/SCL; at 40 m, 1.2 GB / 0.4 GB. On this link a 20 m refresh takes ~13 min, network-bound; rule CPU is 1–2 min on 4 cores.
- **PC tiler verified** for true colour, SWIR, NBR expressions with a colormap, bbox crops and mosaics. Expressions must remove the +1000 DN offset.
- **S1 RTC is available** (S1D, every 3–5 days, mixed geometry). It helps with crop state (VH AUC 0.77 standing vs harvested), not with burnt vs harvested (AUC ≤ 0.57).

## 1. STAC catalogues

| | Planetary Computer | Earth Search v1 (Element84) |
|---|---|---|
| STAC root | `https://planetarycomputer.microsoft.com/api/stac/v1` | `https://earth-search.aws.element84.com/v1` |
| S2 collections | `sentinel-2-l2a` | `sentinel-2-l2a` (legacy), `sentinel-2-c1-l2a` (Collection-1, use this) |
| Auth | keyless; hrefs must be signed (`planetary_computer.sign_inplace`) | none; public `https://…s3.us-west-2.amazonaws.com` COGs |
| Open + point search, Oct 1–30 | 1.26 s + 1.83 s (14 items) | 1.24–1.37 s + 2.1–2.5 s (15 items) |
| AOI polygon search (PB+HR, Oct 18–30) | — | 114 items in 13.5 s (paginated) |
| Latency, Oct-30 pass (sensed 05:40 UTC) | absent at 16:24 UTC (>10.7 h) | `created` 09:50 UTC (**4.2 h**) |
| Hosting | Azure West Europe | AWS us-west-2 |

**SAS tokens.**

- Request: `GET https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a`, no key needed.
- Response: `{"msft:expiry":"2026-10-30T17:11:49Z","token":"st=…&se=…&sp=rl&sv=2025-07-05&sr=c&…&sig=…"}`.
- Lifetime: issued 16:26 UTC, so about 45 min. The SDK caches and renews the token, but long jobs must re-sign hrefs.
- Rate limits are not published. An optional `Ocp-Apim-Subscription-Key` / `PC_SDK_SUBSCRIPTION_KEY` reportedly only raises the token rate (community sources, **UNVERIFIED**).
- I sent about 300 COG and tile requests across both catalogues and saw no throttling.

**Items over Sangrur (75.84E, 30.25N), 2026-10-01..30.** All are tile **43REP**, baseline **05.13**, EPSG:32643; cloud is `eo:cloud_cover` %.

- R105 is a swath-edge orbit: about 71% of the tile is nodata, and it covers only 62% of a 10 km Sangrur box.
- Three satellites (S2A, S2B, S2C) are active, so each orbit repeats every 5 days.

| date | sat | orbit | cloud | note | date | sat | orbit | cloud | note |
|---|---|---|---|---|---|---|---|---|---|
| 10-02 | S2A | R105 | 0.00 | edge | 10-18 | S2B | R005 | 0.00 | **clear** |
| 10-03 | S2C | R005 | 0.00 | clear | 10-20 | S2C | R105 | 0.01 | edge |
| 10-05 | S2B | R105 | 0.01 | edge | 10-22 | S2A | R105 | 0.01 | edge |
| 10-08 | S2B | R005 | 41.3 | | 10-23 | S2C | R005 | 45.1 | smoke haze |
| 10-10 | S2C | R105 | 44.8 | edge | 10-25 | S2B | R105 | 0.00 | edge |
| 10-13 | S2C | R005 | 24.3 | | 10-28 | S2B | R005 | 0.00 | **PC corrupt** |
| 10-15 | S2B / S2A | R105 / R005 | 34.1 / 28.5 | | 10-30 | S2C | R105 | 25.2 | Earth Search only |

**Naming differences**

| concept | PC `sentinel-2-l2a` | E84 `sentinel-2-l2a` / `sentinel-2-c1-l2a` |
|---|---|---|
| Item id | `S2B_MSIL2A_20261028T053929_R005_T43REP_20261028T092239` | `S2B_43REP_20261028_0_L2A` / `S2B_T43REP_20261028T054808_L2A` |
| Band assets | `B01…B12, B8A, SCL, AOT, WVP, visual` | `coastal, blue, green, red, rededge1-3, nir, nir08, nir09, swir16, swir22, scl, aot, wvp, visual` |
| Extras | `tilejson`, `rendered_preview` | legacy: `*-jp2` assets on requester-pays `s3://sentinel-s2-l2a`. c1: `cloud`/`snow` probability COGs, `storage:requester_pays=false` |
| MGRS tile | `s2:mgrs_tile="43REP"` | `grid:code="MGRS-43REP"`, `mgrs:*` |
| Relative orbit | `sat:relative_orbit=5` | absent; parse `_R005_` from `s2:product_uri` |
| `datetime` | datatake start (05:38:29) | tile sensing time (05:50:35) |
| Scale/offset | **no `raster:bands`** | `raster:bands: scale 0.0001, offset -0.1` (legacy also has `earthsearch:boa_offset_applied=true`) |
| Tile filter | `query={"s2:mgrs_tile":{"eq":"43REQ"}}` | `query={"grid:code":{"eq":"MGRS-43REQ"}}` |

**Gotchas**

- **DN offset.** Baseline 05.13 DNs include **+1000**, so reflectance = (DN−1000)/10⁴ and DN 0 is nodata.
- **PC and E84-c1 DNs are identical** on good dates. I did not compare pixels for the legacy E84 collection.
- **odc-stac dtype.** It returns float32 for PC, because there is no `raster:bands`, and uint16 for E84.
- **Overviews.** PC reflectance overviews are **cubic** and its SCL overviews are nearest (`OVERVIEW_RESAMPLING` tag). E84 has no tag, but its SCL overviews contained only valid class values (2/4/5/7).
- **Speed from India.** E84 (us-west-2) is about 2× slower than PC: 41 s vs 21 s single-threaded for the same window.

## 2. Window reads (rasterio / odc-stac)

- **Box:** 10.24 × 10.24 km around Sangrur, `EPSG:32643 [575660, 3341620, 585900, 3351860]`, snapped to 160 m so the 20/40/80 m grids nest.
- **Assets:** 10 per date (B02 B03 B04 B06 B07 B08 B8A B11 B12 SCL) × 2 dates (Oct 18, Oct 28) = 20 COGs.
- **Lower resolutions:** `ds.read(…, out_shape=…)` makes GDAL use the 2×/4×/8× COG overviews. Bytes are from `CPL_VSIL_SHOW_NETWORK_STATS`.

| source | res | threads | wall | MB | | source | res | threads | wall | MB |
|---|---|---|---|---|---|---|---|---|---|---|
| PC | 20 m | 1 | 21.0 s | 29.4 | | E84-c1 | 20 m | 1 | 41.2 s | 29.1 |
| PC | 20 m | 8 | **5.2 s** | 29.4 | | E84-c1 | 20 m | 8 | 8.0 s | 29.1 |
| PC | 40 m | 8 | 4.0 s | 14.8 | | E84-c1 | 40 m | 8 | 10.4 s | 11.3 |
| PC | 80 m | 8 | 4.1 s | 12.2 | | E84-c1 | 80 m | 8 | 5.8 s | 8.5 |

- **odc-stac** (6 bands × 2 dates, `pool=8`):
  - E84-c1: 8.7 / 3.5 / 3.3 s at 20 / 40 / 80 m.
  - PC (`patch_url=pc.sign`): 5.2 / 2.3 / 2.2 s.
  - It snaps the box to 257 px at 40 m; pass a `geobox` for exact grids.
- **Small windows:** at 40/80 m, latency rather than bytes dominates, because whole 512² blocks and the headers are fetched anyway.
- **40×40 km hotspot window:** 20 assets from E84-c1 in 29.3 s. ESA WorldCover for the same window (PC `esa-worldcover`, item `ESA_WorldCover_10m_2021_v200_N30E075`, asset `map`, mode-reprojected to 20 m) took 3.5 s.
- **Full tile, one band** (`GDAL_NUM_THREADS=4`):

  | read | time |
  |---|---|
  | E84-c1 B12 at 20 m | 22.0 s |
  | E84-c1 B04 at 20 m (overview) | 21.3 s |
  | PC B12 at 20 m | 32.4 s |
  | PC B12 at 40 m | 13.6 s |
  | PC B04 at 20 m (overview) | 35.4 s |
  | PC SCL | 3.0 s |

  This link sustains about 1.7–2.1 MB/s per stream, or about 5.7 MB/s (PC) and 3.6 MB/s (E84) aggregated over 8 streams.

## 3. Indices, masks, FIRMS cross-check and separability

### 3.1 Formulas

Every index uses reflectance ρ = (DN−1000)/10⁴.

| index | formula |
|---|---|
| NDVI | (B08−B04)/(B08+B04) |
| NBR | (B8A−B12)/(B8A+B12) |
| NBR2 ≈ NDTI | (B11−B12)/(B11+B12) |
| dX | pre − post (MIRBI and BAIS2 deltas are taken as post − pre) |
| BAIS2 (Filipponi 2018) | (1−√(B06·B07·B8A/B04))·((B12−B8A)/√(B12+B8A)+1) |
| MIRBI | 10·B12 − 9.8·B11 + 2 |
| VIS (visible albedo) | 0.443·B02 + 0.317·B03 + 0.240·B04 |
| TBI (tasselled-cap brightness) | 0.3510·B02 + 0.3813·B03 + 0.3437·B04 + 0.7196·B08 + 0.2396·B11 + 0.1949·B12 |

- **VIS coefficients:** Liang et al. 2002 (RSE 84:25), the ETM+ total-visible equation, applied to the S2 equivalent bands.
- **TBI:** the version used by Deshpande et al. 2022 and CEEW 2025.

**Masks**

- Keep SCL ∈ {2,4,5,6,7} on both dates. Class 2 is kept on purpose: no pixel near a fire was classed 2 or 3, and at fire pixels on the post date SCL was 5 = 89.6% and 4 = 10.1%.
- Restrict to cropland, WorldCover 2021 class 40, which is 88% of the hotspot window.

### 3.2 Real sample

- **Location.** The Sangrur box had only 1 VIIRS detection in 7 days, so I moved to the densest Indian cell: 43REQ, `EPSG:32643 [500000, 3450000, 540000, 3490000]`, centre 31.36N 75.21E, 4.0 Mpx at 20 m.
- **Passes.** E84-c1 R005 on Oct 18 (clear), Oct 23 (hazy) and Oct 28 (clear).
- **FIRMS.** I used the keyless 7-day VIIRS CSVs from J1, SNPP and J2, covering Oct 23–30.
  - Inside the PB+HR polygon there were 2,017 detections, 95% of them daytime (07–09 UTC = 12:30–14:30 IST).
  - In the window, **121 detections fell between the Oct 23 and Oct 28 overpasses** and 107 came after the Oct 28 overpass.
- **Natural control.** Fires detected *after* the post image mark fields that were harvested but **still unburnt on the image date**. I call these LATER.

### 3.3 Post-image values (Oct 28) and changes (Oct 18→28)

Medians, cropland pixels only.

| group | n px | NBR | NBR2 | BAIS2 | MIRBI | VIS | B12 | dNBR | dNDVI |
|---|---|---|---|---|---|---|---|---|---|
| ≤200 m of a VIIRS fire, Oct 23–28 | 29,099 | 0.023 [−.09,.13] | 0.104 | 0.724 | 1.633 | 0.090 | 0.198 | 0.098 | 0.058 |
| ≤200 m of a fire after Oct 28 (still unburnt) | 16,310 | 0.044 | 0.133 | 0.683 | 1.437 | 0.099 | 0.224 | 0.088 | 0.057 |
| Standing crop (NDVI>0.6), >2 km from fires | 147,515 | 0.438 | 0.235 | 0.303 | 1.349 | 0.053 | 0.112 | 0.006 | 0.031 |
| Harvested Oct 18–28 (dNDVI>0.25), >2 km from fires | 191,432 | −0.005 | 0.097 | 0.723 | 1.565 | 0.100 | 0.244 | **0.399** | 0.352 |

- **Pixel level:** fire vs LATER pixels give Kaufman–Remer M ≤ 0.20 and AUC 0.50–0.62 for every index. A 200 m disc is 12.5 ha, or 6–12 fields, so the burnt field is diluted.
- **Burnt neighbourhoods are darker in SWIR:** B11 0.232 vs 0.273, B12 0.198 vs 0.224, MIRBI 1.63 vs 1.44.
- **Harvest alone produces a larger dNBR than burning.** Generic dNBR severity classes (low severity from ≈0.1, Key & Benson; not re-verified today) are therefore unusable here.

### 3.4 Footprint-level separability

- **Unit:** P80 of each feature within a 200 m VIIRS disc, cropland pixels only.
- **Positives:** 116 discs with a fire between the two passes.
- **Negatives:** 60 discs with a fire after Oct 28, each more than 1 km from any earlier fire.

| pair | ΔB8A | ΔNBR2 | ΔB12 | ΔB11 | MIRBI_post | NBR_post | ΔMIRBI | dNBR | dNDVI |
|---|---|---|---|---|---|---|---|---|---|
| Oct 23→28 (5 d), AUC | **0.716** | 0.702 | 0.696 | 0.695 | 0.691 | 0.684 | 0.679 | 0.670 | 0.617 |
| Oct 18→28 (10 d), AUC | 0.707 | 0.686 | 0.625 | 0.665 | 0.693 | 0.686 | 0.707 | **0.580** | 0.483 |

- **Youden cut-offs (5-day pair):** ΔB8A ≥ 0.073, ΔNBR2 ≥ 0.078, ΔB11 ≥ 0.072, ΔB12 ≥ 0.028, MIRBI_post ≥ 1.86.
- **A longer gap hurts dNBR**, because harvest contaminates it. This matches Walker et al. 2022: the burn signal lasts only days, and tillage mimics the NIR/SWIR drop.
- **Visual check:** before/after chips of the highest-FRP fires show black or dark-brown fields appearing inside the VIIRS circles.

### 3.5 Rules (Oct 23→28)

A bag (disc) counts as "detected" if ≥0.52 ha of it is flagged.

| rule | crop flagged | bag TPR | bag FPR* | bag AUC |
|---|---|---|---|---|
| Darkening: ΔB8A>0.07 & ΔB11>0.07 & ΔB12>0.03, 2×2 opening, clusters ≥13 px | 4.9% | 0.69 | 0.47 | 0.699 |
| **Darkening + char state on post (BAIS2≥0.78 & TBI≤0.34)** | 3.2% | **0.66** | **0.35** | 0.699 |
| CEEW T3 alone, single date (Oct 28) | 21.1% | 0.87 | 0.77 | 0.701 |
| Grid-search best darkening, ΔB8A>0.09 & ΔB11>0.03 & ΔB12>0.03 (pixel counts, no cluster filter) | 5.1% | 0.72 | 0.45 | 0.684 |

\* The negative discs sit inside a hotspot, so part of this "FPR" is real unseen burns or tillage; treat it as an upper bound.

A single-date char rule flags char of any age plus dark soil. Use it **only as a state constraint on a bi-temporal change**.

### 3.6 Literature thresholds, and a weak-supervision attempt that failed

- **CEEW 2025**, *Monitoring Crop Residue Burning Better* (Tables 2 and 7): **BAIS2 ≥ 0.78 & TBI ≤ 0.34**.
  - Validated in Hoshiarpur/Jalandhar on 29 Oct 2021: pixel accuracy 0.75, TPR 0.59, FPR 0.006; it caught 262 of 287 burnt fields and falsely flagged 13 of 95 unburnt ones.
  - CEEW puts B8 inside the BAIS2 square root; using B8A gave near-identical results here.
- **Deshpande et al. 2022** (MethodsX 9:101741): BAIS2 + TBI with 5th–95th percentile cut-offs, which separate better than MIRBI or NBR.
- **NRSC/ISRO:** operational MIRBI on 10 m, 5-day Sentinel-2 for Punjab/Haryana (2022–23). The threshold is not public, and the PDFs now return 404, so this is cited from a search snippet.
- **Walker et al. 2022** (arXiv 2209.10148): 441 burnt and 240 unburnt plots in Punjab.
  - The char index and NIR work best within days of the burn, where M > 1.5.
  - Random-forest plot accuracy was 0.82 (0.81 with Sentinel-2 only).
  - Tilled-but-unburnt plots show almost the same NIR/SWIR change.
- **Naive weak supervision, tested here — reject.**
  - Setup: LightGBM with 36 features (post bands and indices plus deltas vs Oct 23 and Oct 18), 300 trees, 31 leaves. Fire discs labelled 1; LATER discs and green crop labelled 0; one multiple-instance refinement round keeping the top 30% of positive pixels.
  - Spatial GroupKFold over 10 km blocks gave **bag AUC 0.597 (round 0) and 0.587 (round 1)**.
  - The model flags ≈14% of valid cropland (19,347 ha), including river sand bars. Label noise dominates.

### 3.7 Recommended weak-supervision scheme for the LightGBM pixel model (UNVERIFIED end-to-end)

1. **Positives.** Pixels of §3.5 row-2 clusters whose interval contains a VIIRS or MODIS detection ≤375 m away with confidence ≠ low. These give field-shaped labels instead of 12.5 ha discs.
2. **Negatives.**
   - Harvested-residue pixels on the post date that did **not** darken and lie ≤375 m from a fire detected *after* the post image ("ready to burn"). These are also the labels for the "burns next" model.
   - Standing crop (NDVI > 0.6).
   - Non-crop pixels from WorldCover.
   - Leave pixels that darkened without char unlabelled.
3. **Training.** Positive-unlabelled learning (Elkan–Noto calibration, or a 0.3 weight on unlabelled pixels used as negatives). Add the §3.4 features, days since the last clear pass, and a haze flag.
4. **Acceptance.** The spatial-block-CV FIRMS-bag AUC must beat 0.72. Also audit about 200 random "unseen" clusters in the evidence viewer (§5) and report their precision.
5. **Season-long labels.** With a `FIRMS_MAP_KEY`, extend labels to Oct 1–Nov 30 (§7).

### 3.8 Smoke haze (observed in the hotspot window)

| metric | Oct 18 | Oct 23 | Oct 28 |
|---|---|---|---|
| B02 median | 0.063 | **0.117** | 0.073 |
| NDVI median | 0.305 | 0.203 | 0.249 |
| B12 median | 0.189 | 0.197 | 0.210 |
| Sen2Cor AOT | 0.26 | 0.38 | 0.19 |

- On Oct 23 the tile's `eo:cloud_cover` was 22.4%, yet SCL marked only about 1.3% of the window as cloud or cirrus. Sen2Cor AOT is near-constant per scene, so it is a weak haze flag.
- **Rule:** flag a scene as hazy if the cropland B02 median rises by more than 0.03 over the previous clear pass. Then use SWIR-only features (ΔB11, ΔB12, ΔNBR2, MIRBI) and lower the confidence.

## 4. Volume and compute: all Punjab + Haryana

**AOI.** geoBoundaries `https://www.geoboundaries.org/api/current/gbOpen/IND/ADM1/` returns the GeoJSON URL. Select by `shapeISO` IN-PB / IN-HR, not by name ("Haryāna" has a macron). Area 50,321 + 44,182 = **94,503 km²**.

**MGRS tiles.** 23 intersect the AOI; 19 have ≥250 km² inside it: 43RDN RDP RDQ REL REM REN REP REQ RFL RFM RFN RFP RFQ RGL RGM RGN RGP SDR SER. Slivers: 43RCP, RGQ, SES, SFR.

**Passes, Oct 18–30.**

| orbit | dates | tiles |
|---|---|---|
| R005 | Oct 18, 23, 28 | 16 |
| R105 | Oct 20, 22, 25, 30 | 15 |
| R048 | Oct 21, 26 | 2 |

At tile level (not checked per pixel), R005 + R105 together reach all 23 tiles about every 5 days, and the central RE*/RF* strip is seen by both orbits, every 2–3 days.

**MB per tile-band** (exact, from the COG headers; 43REQ, 2026-10-18):

| asset | PC 10 m | PC 20 m | PC 40 m | PC 80 m | E84-c1 20 m | E84-c1 40 m |
|---|---|---|---|---|---|---|
| 10 m band (B02/B04) | 207–214 | 52–54 (overview) | 13.3–13.6 | 3.3 | 43–46 (overview) | 11.1–11.7 |
| 20 m band (B06/B8A/B11/B12) | — | 54–55 | 13.7–13.8 | 3.5 | 46–47 | 11.8–12.0 |
| SCL | — | 2.8 | 1.0 | 0.3 | 2.9 | 1.0 |

PC files are DEFLATE without a predictor in 512² blocks. E84 uses predictor 2 and 1024² blocks for the 10 m bands.

**Per full-AOI refresh** (derived: ≈4.5 KB/km²/band at 20 m and ≈1.15 at 40 m, plus 20% for tile overlap; a single pass covers an estimated 0.5–0.7× of this):

| asset set | 20 m volume | 20 m time at ~6 MB/s | 40 m volume | 40 m time |
|---|---|---|---|---|
| Minimal: B8A, B11, B12, SCL | 1.6 GB | ≈4.5 min | 0.4 GB | ≈1.1 min |
| Detection: 9 bands + SCL | 4.6 GB | ≈13 min (≈21 min from E84) | 1.2 GB | ≈3.3 min |

- An in-region VM brings either set under about 1 min.
- **CPU, 4 cores, ≈200 M cropland px at 20 m:**

  | step | rate | time |
  |---|---|---|
  | 36-feature build | 1.2 Mpx/s per core | ≈45 s |
  | Rules + labelling + vectorising | — | ≈1–2 min |
  | LightGBM, 300 trees, 4 threads | 0.19 Mpx/s | ≈17 min (≈4.4 min at 40 m) |

  Run LightGBM on candidate pixels only (5–20%) to get it under about 3 min. Process in 2048² blocks, about 170 MB per date.

**Incremental pipeline**

1. **Poll** E84-c1 every 30 min between 06 and 14 UTC, using `intersects=AOI` and `datetime≥last_seen`. Skip items with `s2:nodata_pixel_percentage>95` or `eo:cloud_cover>70`. Record each (tile, date) in PostGIS.
2. **Read** B02 B03 B04 B06 B07 B08 B8A B11 B12 SCL at 20 m, only for blocks that contain WorldCover-40 pixels. Cache the 20 m cropland mask per tile.
3. **Validate.** The B12 median over SCL-valid pixels must be in (0.02, 0.6) and the correlation with the previous clear pass > 0.3; on failure switch catalogue (this catches PC's 10-28 data). Set the haze flag (§3.8).
4. **Per-pixel state**, cropland only, Zarr/npz at ~12 B/px (≈2.4 GB): `last_clear_doy`, last B8A/B11/B12/NDVI/TBI (float16), `state` ∈ {standing, harvested, burnt, tilled, green-again}, `harvest_doy`, `burn_doy`, `burn_conf`. Transitions:
   - standing → harvested: NDVI drops >0.25 to <0.35 and B11 rises.
   - harvested → burnt: the §3.5 darkening+char rule fires between clear observations ≤6 days apart.
   - harvested → tilled: darkening without char. burnt → tilled: the char fades.
5. **Events.** Label 8-connected new-burn pixels after a 2×2 opening; keep clusters ≥13 px (0.52 ha; sample median 1.2 ha, p90 3.6 ha). Polygonise (`rasterio.features.shapes`), simplify at 10 m, and store in PostGIS with interval, area, mean deltas, BAIS2, TBI and confidence. A cluster is `seen` if a VIIRS/MODIS detection lies ≤750 m away within the interval, else **`unseen`**.
6. **Next-burn candidates**: harvested-residue fields, with features days since harvest, neighbour burn rate within 2 km over 5 days, district and wind/rain forecast. Train on LATER-type labels (§3.7, step 2).

## 5. PC data API tiles (verified)

- Base: `https://planetarycomputer.microsoft.com/api/data/v1`; routes are from its `/openapi.json`.
- Every response below was `200 image/png`. Item tiles took 0.5–1.4 s and were 70–165 KB at 256².
- Tested at Sangrur, z13, x5821/y3373, with item `S2B_MSIL2A_20261018T053829_R005_T43REP_20261018T092057`.

```
# (a) true colour (PC's own "Natural color" render)
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&assets=B04&assets=B03&assets=B02&nodata=0&color_formula=Gamma RGB 3.2 Saturation 0.8 Sigmoidal RGB 25 0.35
# (b) SWIR false colour
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&assets=B12&assets=B8A&assets=B04&nodata=0&color_formula=Gamma RGB 3.7 Saturation 1.5 Sigmoidal RGB 15 0.35
# (c) NBR + colormap, offset-corrected (PC's default NDVI render ignores the +1000 DN offset and looks washed out)
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&expression=(B8A-B12)/(B8A%2BB12-2000)&asset_as_band=true&nodata=0&rescale=-0.5,0.7&colormap_name=rdylgn
# TileJSON (same query string), 0.8 s:  /item/WebMercatorQuad/tilejson.json?collection=…&item=…&expression=…
# Field evidence chip, lon/lat bbox, 2.1 s:  /item/bbox/75.08,31.31,75.12,31.35/512x512.png?collection=…&item=…&assets=B12&assets=B8A&assets=B04&nodata=0&…
```

- The same requests for the 10-28 item return noise or black. That is a data fault, not a URL fault.
- Encode `+` as `%2B`.
- dNBR needs two items, so no single tile request can produce it. Show pre and post side by side or with a swipe, and serve our own burn polygons as GeoJSON or MVT.

**Mosaic ("latest clear" across Punjab)**

`POST /mosaic/register`, body:

```
{"collections":["sentinel-2-l2a"],"filter-lang":"cql2-json","filter":{"op":"and","args":[
 {"op":"s_intersects","args":[{"property":"geometry"},{AOI polygon}]},
 {"op":"anyinteracts","args":[{"property":"datetime"},{"interval":["2026-10-15T00:00:00Z","2026-10-27T23:59:59Z"]}]},
 {"op":"<=","args":[{"property":"eo:cloud_cover"},10]}]},"sortby":[{"field":"datetime","direction":"desc"}]}
```

- The response took 0.24–0.55 s: `{"id":"pc1.1793463471.2889764adaee4f188a47344207a03d11","links":[…/info, …/tilejson.json, …/WMTSCapabilities.xml]}`.
- Tiles: `GET /mosaic/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&assets=B04&assets=B03&assets=B02&nodata=0&color_formula=…`. This works at z9 and z12 in 0.85–2.5 s, and the NBR expression works too.
- TileJSON: `/mosaic/{id}/WebMercatorQuad/tilejson.json?collection=…`.

**Mosaic gotchas**

- With the interval running to Oct 30, tiles came back **black**, because the corrupt 10-28 items sort first. Exclude dates that fail validation.
- `/mosaic/{id}/point/{lon},{lat}` returned HTTP 500.
- `/mosaic/{id}/tiles/…/assets` returned 422 ("must contain a collection parameter") even with `collection` set.
- The OpenAPI shows `pixel_selection`, `items_limit` and `time_limit` (default 5 s) parameters.
- A mosaic of explicit item ids via a CQL2 `in` filter is **UNVERIFIED**.

**Other layers verified** (same item-tile route):

- S1 RTC: `collection=sentinel-1-rtc&item={id}&assets=vv&rescale=0,0.5&colormap_name=gray` (1.8 s).
- WorldCover: `collection=esa-worldcover&item=ESA_WorldCover_10m_2021_v200_N30E075&assets=map&colormap_name=esa-worldcover` (0.9 s).

## 6. Sentinel-1 RTC (PC `sentinel-1-rtc`)

**Availability (verified)**

- Over Sangrur, Oct 1–30 2026, there are 8 items. The six most recent are all **S1D**: Oct 12 D34, 16 A100, 19 D136, 23 A27, 24 D34, 28 A100.
- Over the 43REQ window there are 9 items, all S1D: Oct 4/16/28 A100, Oct 11/23 A27, and Oct 12/24 D34 (two frames each).
- The same geometry repeats every 12 days; some acquisition arrives every 3–5 days.
- The newest RTC item anywhere was S1C 2026-10-30 05:58 UTC, already ingested by about 16 UTC.

**Assets and access**

- `vv` and `vh`: float32 linear γ⁰, files named `…/measurement/iw-vv.rtc.tiff`, EPSG:32643, 10 m, overviews 2–64, nodata −32768.
- **The extension is `.tiff`.** Set `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff"`, or GDAL fails with "not recognized as a supported dataset name".
- Licence: CC-BY-4.0 (Catalyst, Microsoft). The `sentinel-1-grd` collection is `proprietary` (Copernicus terms).
- Reading the 40 km window at 20 m took 11.5–15.4 s per polarisation.
- Check the frame footprint: the frame over Sangrur (T130310) did not cover 43REQ, so the next frame (T130339) was needed.

**Test** (A100, Oct 16 → Oct 28, P20/P50/P80 per 200 m disc)

- ΔVV, ΔVH and Δ(VH−VV) separate fire discs (n=159) from LATER discs (n=31) at only **AUC 0.50–0.57** (e.g. ΔVH P50 −0.81 vs −0.88 dB).
- Crop state on Oct 28 is the useful part: **VH standing −15.05 dB vs harvested −17.98 dB, pixel AUC 0.77**; VV AUC 0.64; VH−VV AUC 0.65.

**Literature.** I found no study that detects post-harvest residue burning with C-band SAR. Wildfire studies show VH drops over burnt forest, and Belenguer-Plomer et al. 2019 (RSE) combine VV/VH change with hotspots. Harvest detection from VH peaks or coherence is established.

**Verdict:** use S1 only as the cloud/haze fallback for the standing → harvested transition, never as evidence of burning.

## 7. Recommended approach and tested code

**Sources**

- **Analytics:** E84 `sentinel-2-c1-l2a`, with PC as a validated fallback.
- **Cropland mask:** WorldCover 2021 from PC.
- **Evidence viewer:** the PC tiler.
- **FIRMS (keyless, verified):** `https://firms.modaps.eosdis.nasa.gov/data/active_fire/{noaa-20-viirs-c2|suomi-npp-viirs-c2|noaa-21-viirs-c2|modis-c6.1}/csv/{J1_VIIRS_C2|SUOMI_VIIRS_C2|J2_VIIRS_C2|MODIS_C6_1}_South_Asia_7d.csv`.
  - Each took 1.9–2.9 s and was 0.15–1.24 MB. Last-Modified was 15:42 UTC, and range requests return 206.
  - Header: `latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight`.
  - Sample row: `28.96295,79.78813,330.52,0.55,0.68,2026-10-23,0659,N20,nominal,2.0NRT,297.77,6.05,D`.
- **FIRMS with `FIRMS_MAP_KEY`:** unlocks season history via `/api/area/csv/{MAP_KEY}/{VIIRS_NOAA20_NRT|…}/{w,s,e,n}/{1..10}[/{YYYY-MM-DD}]`. An invalid key returns `HTTP 400 "Invalid MAP_KEY."`; a valid key is **UNVERIFIED**.

**Tested snippet.** I ran it verbatim today (26.5 s, including FIRMS):

- Box `[500000,3460000,510240,3470240]`, pre = `S2C_T43REQ_20261023T054822_L2A`, post = `S2B_T43REQ_20261028T054808_L2A`.
- Output: `n_clusters=327, area_ha=555.7, unseen=263`.
- No WorldCover mask was applied in this run; add one in production.

```python
import os, io, json, numpy as np, pandas as pd, httpx, rasterio, shapely
from concurrent.futures import ThreadPoolExecutor
from rasterio.windows import from_bounds
from rasterio.features import shapes
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import shape, mapping
from pyproj import Transformer
from pystac_client import Client
os.environ.update({"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
                   "GDAL_HTTP_MULTIPLEX": "YES", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif"})
E84 = {"B02": "blue", "B03": "green", "B04": "red", "B06": "rededge2", "B07": "rededge3", "B08": "nir",
       "B8A": "nir08", "B11": "swir16", "B12": "swir22", "SCL": "scl"}
VALID_SCL = [2, 4, 5, 6, 7]
FIRMS = ["noaa-20-viirs-c2/csv/J1_VIIRS_C2_South_Asia_7d.csv", "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_South_Asia_7d.csv",
         "noaa-21-viirs-c2/csv/J2_VIIRS_C2_South_Asia_7d.csv"]

def read_item(item, bounds, res=20, workers=8):
    def one(b):
        with rasterio.open(item.assets[E84[b]].href) as ds:
            w = from_bounds(*bounds, transform=ds.transform)
            n = (int(round((bounds[3] - bounds[1]) / res)), int(round((bounds[2] - bounds[0]) / res)))
            return b, ds.read(1, window=w, out_shape=n)
    with ThreadPoolExecutor(workers) as ex:
        raw = dict(ex.map(one, E84))
    refl = {b: np.where(a == 0, np.nan, (a.astype(np.float32) - 1000) / 1e4) for b, a in raw.items() if b != "SCL"}
    valid = np.isin(raw["SCL"], VALID_SCL)
    med = np.nanmedian(refl["B12"][valid]) if valid.any() else np.nan
    if not (0.02 < med < 0.6):  # plausibility gate: PC 2026-10-28 noise gives negative reflectance
        raise ValueError(f"{item.id}: implausible B12 median {med}")
    return refl, valid

def detect(pre_item, post_item, bounds, crs="EPSG:32643", res=20, min_px=13):
    a, va = read_item(pre_item, bounds, res); b, vb = read_item(post_item, bounds, res)
    bais2 = (1 - np.sqrt(b["B06"] * b["B07"] * b["B8A"] / b["B04"])) * ((b["B12"] - b["B8A"]) / np.sqrt(b["B12"] + b["B8A"]) + 1)
    tbi = 0.3510*b["B02"] + 0.3813*b["B03"] + 0.3437*b["B04"] + 0.7196*b["B08"] + 0.2396*b["B11"] + 0.1949*b["B12"]
    burn = (va & vb & (a["B8A"] - b["B8A"] > 0.07) & (a["B11"] - b["B11"] > 0.07) & (a["B12"] - b["B12"] > 0.03)
            & (bais2 >= 0.78) & (tbi <= 0.34))            # production: & worldcover_class == 40
    burn = ndimage.binary_opening(burn, np.ones((2, 2)))
    lab, n = ndimage.label(burn)
    sizes = ndimage.sum(burn, lab, range(1, n + 1))
    lab[~np.isin(lab, np.flatnonzero(sizes >= min_px) + 1)] = 0
    tf = rasterio.transform.from_origin(bounds[0], bounds[3], res, res)
    tr = Transformer.from_crs(crs, 4326, always_xy=True)
    to_ll = lambda c: np.column_stack(tr.transform(c[:, 0], c[:, 1]))
    out = []
    for geom, v in shapes(lab.astype(np.int32), mask=lab > 0, transform=tf):
        g = shape(geom).simplify(res / 2)
        out.append({"id": int(v), "area_ha": round(g.area / 1e4, 2), "geom_utm": g, "geometry": mapping(shapely.transform(g, to_ll))})
    return out

def firms_7d():
    d = pd.concat([pd.read_csv(io.StringIO(httpx.get("https://firms.modaps.eosdis.nasa.gov/data/active_fire/" + u,
                   timeout=60).raise_for_status().text)) for u in FIRMS], ignore_index=True)
    d["t"] = pd.to_datetime(d.acq_date + " " + d.acq_time.astype(str).str.zfill(4), format="%Y-%m-%d %H%M", utc=True)
    return d

cat = Client.open("https://earth-search.aws.element84.com/v1")
pre, post = sorted(cat.search(collections=["sentinel-2-c1-l2a"],
                   ids=["S2C_T43REQ_20261023T054822_L2A", "S2B_T43REQ_20261028T054808_L2A"]).items(), key=lambda i: i.datetime)
feats = detect(pre, post, (500000.0, 3460000.0, 510240.0, 3470240.0))
f = firms_7d(); f = f[(f.t > pre.datetime) & (f.t <= post.datetime)]
tree = cKDTree(np.c_[Transformer.from_crs(4326, 32643, always_xy=True).transform(f.longitude.values, f.latitude.values)])
for ft in feats:
    c = ft.pop("geom_utm").centroid
    ft["nearest_viirs_m"] = float(tree.query([c.x, c.y])[0]); ft["seen_by_firms"] = ft["nearest_viirs_m"] <= 750
```

**For PC**, use the `B04`-style keys and `Client.open(…, modifier=planetary_computer.sign_inplace)`. Always keep the plausibility gate. You can also cross-check a 64² window against the other catalogue: identical DNs mean the scene is good.

## 8. Licensing and attribution

| source | licence | attribution |
|---|---|---|
| Sentinel-2 and Sentinel-1 (both catalogues) | Copernicus Sentinel Data Terms (STAC `proprietary`; free, full and open) | "Contains modified Copernicus Sentinel data 2026" |
| PC `sentinel-1-rtc` | CC-BY-4.0 (Catalyst, Microsoft) | credit Microsoft Planetary Computer |
| `esa-worldcover` | CC-BY-4.0 | "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium" |
| NASA FIRMS | open | "We acknowledge the use of data from NASA FIRMS (https://earthdata.nasa.gov/firms), part of NASA ESDIS" |
| geoBoundaries IND ADM1 | CC BY 2.5 IN (DataMeet / Election Commission of India) | as given in the API record |

The licence values were read today from the STAC collection metadata and the geoBoundaries API. The quoted attribution sentences are the providers' standard wording and were not re-checked today. Every value shown in the app is computed live from these sources. **Do not cache PC scenes that fail validation.**

## 9. Open items / not verified

- Season-long FIRMS history with a valid `MAP_KEY`.
- Whether PC repairs its 2026-10-28 assets. Re-run the PC-vs-E84 identity check (`s02b`).
- The rule-seeded PU LightGBM (§3.7).
- How specific the "unseen" candidates are, checked against ground truth or very-high-resolution imagery (the tillage confound).
- Mosaics built from explicit item ids.
- Key & Benson dNBR class breaks were not re-verified, and they are not used.
