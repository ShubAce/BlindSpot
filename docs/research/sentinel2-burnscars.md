# Sentinel-2 burn scars and crop state: feasibility (BlindSpot)

I verified everything below with real calls on **2026-10-30, 15:45–16:40 UTC**, from Windows 11 on a residential connection in India.
Items that were not verified are marked **UNVERIFIED**. The scratch scripts that produced these numbers are in the session scratch dir `.../scratchpad/sentinel2/` (`s01`–`s06`, `snippet_burns.py`). That directory is ephemeral and is not part of the repo.

## 0. TL;DR

- **Both keyless STAC catalogues work.** Use **Earth Search `sentinel-2-c1-l2a` for analytics**, for three reasons:
  - Its latency is 4.2 h from sensing to STAC. On PC, the 2026-10-30 items were still missing at 16:24 UTC.
  - It carries `scale/offset` metadata.
  - It needs no URL signing.

  Use **Planetary Computer (PC) for XYZ tiles, ESA WorldCover and Sentinel-1 RTC**.
- **PC is serving corrupted pixels for every 2026-10-28 Sentinel-2 item over Punjab** (16/16 items, orbits R005 and R048).
  - Reflectance bands hold noise (DN 0–255, correlation with Earth Search ≈ 0). SCL is intact.
  - All 49 other PC items from Oct 18–28 matched Earth Search byte-for-byte.
  - The PC tiler and the "latest clear" mosaic therefore render black for this region. As of 16:36 UTC the problem was not fixed.
  - **A data-plausibility check on every scene is therefore mandatory** (§7).
- **20 m is the working resolution.** Punjab plots are 0.9 ha median and 1.4 ha mean (Walker et al. 2022), which is ≈ 22 px at 20 m but only 5.6 px at 40 m. Use 40 m or 80 m overviews only for quick-looks and state maps.
- **dNBR on its own detects harvest, not burning.** Harvested fields with no FIRMS fire within 2 km had a median dNBR of **0.40** (Oct 18→28). VIIRS fire neighbourhoods had **0.10**.
- **Bi-temporal NIR+SWIR darkening plus a post-date "char" state worked best.** The darkening is measured between consecutive clear passes ≤5 days apart. The char state uses CEEW's BAIS2 ≥ 0.78 & TBI ≤ 0.34.
  - At footprint level it scored TPR 0.66 against FPR 0.35.
  - The FPR is an upper bound, because the negative footprints probably contain unseen burns.
  - The best single features reach AUC ≈ 0.70 (ΔB8A 0.716, ΔNBR2 0.702).
- **Burnt and harvested-unburnt pixels are only weakly separable** in this real sample. A naive FIRMS-bag LightGBM model reached spatial-CV AUC 0.59, which is worse than the single features, so **do not ship it as-is** (§3.6).
- **Candidate unseen burns.** In a 40×40 km hotspot (tile 43REQ), the rule flagged **2,773 clusters ≥0.52 ha (4,382 ha)** that appeared between the Oct 23 and Oct 28 passes.
  - **87%** of those clusters (84% of the area) have no VIIRS detection within 750 m in that interval.
  - Tillage is a known confound (Walker et al. 2022), so present these as *candidates* with an evidence viewer.
- **Smoke haze is the main data risk in this season.** The Oct 23 pass was visibly hazy: blue reflectance went ×1.9 and NDVI fell 0.31→0.20.
  - SCL flagged only about 1% cloud, and Sen2Cor AOT was only 0.38.
  - B11/B12 were almost unaffected, so SWIR features are the robust ones.
- **Volume per full refresh of Punjab + Haryana** (23 MGRS tiles, 19 material) is about **4.6 GB at 20 m for 10 assets**, or 1.6 GB for the minimal B8A/B11/B12/SCL set. At 40 m it is 1.2 GB (0.4 GB minimal). On this link that takes about 13 min at 20 m, and the job is network-bound. The CPU cost on 4 cores is about 1–2 min for rules.
- **The PC tiler works for the evidence viewer.** I verified true colour, SWIR, NBR expressions with colormaps, bbox crops and search-based mosaics. Expressions must remove the +1000 DN offset. The mosaic needs a date filter while the Oct 28 problem persists.
- **Sentinel-1 RTC is available on PC.** In October 2026 the satellite is S1D, every 3–5 days with mixed geometry.
  - It helps with crop state: standing vs harvested gives VH AUC 0.77.
  - It is useless for burnt vs harvested-unburnt (AUC ≤ 0.57).

## 1. STAC catalogues

| | Planetary Computer | Earth Search v1 (Element84) |
|---|---|---|
| STAC root | `https://planetarycomputer.microsoft.com/api/stac/v1` | `https://earth-search.aws.element84.com/v1` |
| S2 collections | `sentinel-2-l2a` | `sentinel-2-l2a` (legacy) and `sentinel-2-c1-l2a` (Collection-1, recommended) |
| Auth | Keyless. Asset hrefs must be signed with `planetary_computer.sign_inplace` or `GET /api/sas/v1/token/{collection}`. | None. Plain public `https://…s3.us-west-2.amazonaws.com` COGs. |
| Open + point search, Oct 1–30 | 1.26 s + 1.83 s (14 items) | 1.24–1.37 s + 2.1–2.5 s (15 items) |
| AOI polygon search (PB+HR, Oct 18–30) | n/a | 114 items in 13.5 s (paginated) |
| Latency (Oct 30 pass, sensed 05:40 UTC) | not ingested at 16:24 UTC (>10.7 h) | `created` 09:50 UTC (**4.2 h**) |
| Data region | Azure West Europe | AWS us-west-2 |

**SAS token** (`GET https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a`, keyless):

```
{"msft:expiry":"2026-10-30T17:11:49Z","token":"st=…&se=2026-10-30T17%3A11%3A49Z&sp=rl&sv=2025-07-05&sr=c&…&sig=…"}
```

- The token was issued at 16:26 UTC, so it is valid for about 45 min. `planetary_computer` caches and renews it automatically, but long jobs must re-sign hrefs.
- The rate limit is undocumented. Community sources say an optional `Ocp-Apim-Subscription-Key` / `PC_SDK_SUBSCRIPTION_KEY` only raises the *token* rate limit, and personal keys may no longer be issued (**UNVERIFIED**).
- In my session, ~300 COG and tile requests went through with no throttling. Earth Search has no published limit, and I hit none.

**Items over Sangrur (75.84E, 30.25N), 2026-10-01..30.** All are tile **43REP**, processing baseline **05.13**, EPSG:32643. Cloud is `eo:cloud_cover` in %. R105 is a swath-edge orbit with about 71% nodata in the tile, and it covers only 62% of a 10 km Sangrur box.

| date | sat | orbit | cloud | note | date | sat | orbit | cloud | note |
|---|---|---|---|---|---|---|---|---|---|
| 10-02 | S2A | R105 | 0.00 | edge | 10-18 | S2B | R005 | 0.00 | **clear** |
| 10-03 | S2C | R005 | 0.00 | clear | 10-20 | S2C | R105 | 0.01 | edge |
| 10-05 | S2B | R105 | 0.01 | edge | 10-22 | S2A | R105 | 0.01 | edge |
| 10-08 | S2B | R005 | 41.3 | | 10-23 | S2C | R005 | 45.1 | smoke haze |
| 10-10 | S2C | R105 | 44.8 | edge | 10-25 | S2B | R105 | 0.00 | edge |
| 10-13 | S2C | R005 | 24.3 | | 10-28 | S2B | R005 | 0.00 | **PC corrupt** |
| 10-15 | S2B+S2A | R105+R005 | 34.1 / 28.5 | | 10-30 | S2C | R105 | 25.2 | Earth Search only |

Three satellites are active: S2A, S2B and S2C. The same orbit repeats every 5 days.

**Naming differences**

| concept | PC `sentinel-2-l2a` | E84 `sentinel-2-l2a` / `-c1-l2a` |
|---|---|---|
| Item id | `S2B_MSIL2A_20261028T053929_R005_T43REP_20261028T092239` | `S2B_43REP_20261028_0_L2A` / `S2B_T43REP_20261028T054808_L2A` |
| Band assets | `B01…B12,B8A,SCL,AOT,WVP,visual` | `coastal,blue,green,red,rededge1-3,nir,nir08,nir09,swir16,swir22,scl,aot,wvp,visual` |
| Extra assets | `tilejson`, `rendered_preview` (tiler links) | l2a: `*-jp2` assets on **requester-pays** `s3://sentinel-s2-l2a`. c1: `cloud`/`snow` probabilities as COGs, `preview`, `storage:requester_pays=false`. |
| MGRS tile property | `s2:mgrs_tile="43REP"` | `grid:code="MGRS-43REP"` (plus `mgrs:*`) |
| Relative orbit | `sat:relative_orbit=5` | absent; parse `_R005_` from `s2:product_uri` |
| `datetime` | datatake start (05:38:29) | tile sensing time (05:50:35) |
| Scale/offset | **no `raster:bands`** | `raster:bands: scale 0.0001, offset -0.1` (l2a also has `earthsearch:boa_offset_applied=true`) |
| Tile filter | `query={"s2:mgrs_tile":{"eq":"43REQ"}}` | `query={"grid:code":{"eq":"MGRS-43REQ"}}` |

**Gotchas**

- Baseline 05.13 L2A DNs include **+1000**. Reflectance is `(DN−1000)/10000`, and DN 0 means nodata.
  - PC and E84-c1 DNs are identical on good dates. I did not compare pixels for E84 legacy l2a.
  - odc-stac returns **float32 for PC** (no `raster:bands`) and uint16 for E84.
- E84 `sentinel-2-l2a` is slower from India: 41 s vs 21 s single-threaded for the same window (Table 2).
- PC COG overviews of reflectance bands are **cubic**; SCL overviews are nearest. E84 does not tag the overview resampling, but the SCL overviews held only valid classes.

## 2. Window reads (rasterio / odc-stac)

The box is 10.24 km × 10.24 km around Sangrur: `EPSG:32643 [575660, 3341620, 585900, 3351860]`. It is snapped to a 160 m grid so that the 20, 40 and 80 m grids nest. Each run reads 10 assets (B02 B03 B04 B06 B07 B08 B8A B11 B12 SCL) × 2 dates (Oct 18 and Oct 28) = 20 COGs. 10 m bands were read through `out_shape`, so GDAL picks the 2×/4×/8× overview. Bytes are from `CPL_VSIL_SHOW_NETWORK_STATS`.

| source | res | threads | wall | MB downloaded | source | res | threads | wall | MB |
|---|---|---|---|---|---|---|---|---|---|
| PC | 20 m | 1 | 21.0 s | 29.4 | E84-c1 | 20 m | 1 | 41.2 s | 29.1 |
| PC | 20 m | 8 | **5.2 s** | 29.4 | E84-c1 | 20 m | 8 | 8.0 s | 29.1 |
| PC | 40 m | 8 | 4.0 s | 14.8 | E84-c1 | 40 m | 8 | 10.4 s | 11.3 |
| PC | 80 m | 8 | 4.1 s | 12.2 | E84-c1 | 80 m | 8 | 5.8 s | 8.5 |

- **odc-stac** loaded 6 bands × 2 dates with `pool=8`:
  - E84-c1: 8.7 / 3.5 / 3.3 s at 20 / 40 / 80 m.
  - PC (`patch_url=pc.sign`): 5.2 / 2.3 / 2.2 s.
- Below 20 m, latency rather than bytes dominates small windows: whole 512² blocks plus headers are fetched.
- A 40×40 km window (E84-c1, 20 assets) took 29.3 s. ESA WorldCover for it took 3.5 s (PC `esa-worldcover`, item `ESA_WorldCover_10m_2021_v200_N30E075`, asset `map`, mode-reprojected to the 20 m UTM grid).
- Full-tile reads of a single band (`GDAL_NUM_THREADS=4`):

  | read | time |
  |---|---|
  | E84-c1 B12 at 20 m | 22.0 s |
  | E84-c1 B04 at 20 m (overview) | 21.3 s |
  | PC B12 at 20 m | 32.4 s |
  | PC B12 at 40 m | 13.6 s |
  | PC B04 at 20 m (overview) | 35.4 s |
  | PC SCL | 3.0 s |

  This link sustains about 2–3 MB/s per stream and about 6 MB/s aggregate to PC or 3.6 MB/s to E84.
- odc-stac snaps to 257×257 px at 40 m for this box. Pass an explicit `geobox` if exact alignment matters.

## 3. Indices, masking, FIRMS cross-check and separability

### 3.1 Formulas

All formulas use reflectance ρ = (DN−1000)/10⁴.

| index | formula |
|---|---|
| NDVI | (B08−B04)/(B08+B04) |
| NBR | (B8A−B12)/(B8A+B12) |
| NBR2 (≈ NDTI residue index) | (B11−B12)/(B11+B12) |
| dX | pre − post (MIRBI and BAIS2 are signed post − pre) |
| BAIS2 (Filipponi 2018, B8A form) | (1−√(B06·B07·B8A/B04))·((B12−B8A)/√(B12+B8A)+1) |
| MIRBI | 10·B12 − 9.8·B11 + 2 |
| Visible albedo (Liang et al. 2002, ETM+ visible, mapped to S2 B02/B03/B04) | 0.443·B02 + 0.317·B03 + 0.240·B04 |
| TBI (Tasseled-cap brightness, as used by Deshpande 2022 and CEEW 2025) | 0.3510·B02 + 0.3813·B03 + 0.3437·B04 + 0.7196·B08 + 0.2396·B11 + 0.1949·B12 |

**Masks**

- Keep SCL ∈ {2, 4, 5, 6, 7} on both dates. Class 2 (dark area) is kept on purpose: no FIRMS-adjacent pixel was classed 2 or 3. Post-date SCL at fire pixels was 5 = 89.6% and 4 = 10.1%.
- Restrict to cropland with WorldCover 2021 class 40, which covered 88% of the hotspot window.

### 3.2 Real sample

- **Sangrur box**: only 1 VIIRS detection in 7 days, so I moved the analysis to the densest Indian-Punjab cell.
- **Hotspot window**: tile 43REQ, `EPSG:32643 [500000, 3450000, 540000, 3490000]`, centre 31.36N 75.21E, 4.0 Mpx at 20 m.
- **S2 passes** (E84-c1, R005): Oct 18 (clear), Oct 23 (hazy) and Oct 28 (clear).
- **FIRMS**: keyless 7-day VIIRS CSVs from NOAA-20 (J1), SNPP and NOAA-21 (J2), covering Oct 23–30.
  - Inside the PB+HR polygon: 2,017 detections, 95% daytime (07–09 UTC = 12:30–14:30 IST).
  - In the window: **121 detections between the Oct 23 and Oct 28 overpasses** and 107 after the Oct 28 overpass.
- **Natural control**: fires detected *after* the post image mark fields that were harvested but **still unburnt** on the image date ("LATER" group).

### 3.3 Post-image values (Oct 28) and changes (Oct 18→28)

Medians, IQR in brackets.

| group (cropland, valid) | n px | NBR | NBR2 | BAIS2 | MIRBI | VIS | B12 | dNBR | dNDVI |
|---|---|---|---|---|---|---|---|---|---|
| ≤200 m of VIIRS fire Oct 23–28 | 29,099 | 0.023 [−.09,.13] | 0.104 | 0.724 | 1.633 | 0.090 | 0.198 | 0.098 | 0.058 |
| ≤200 m of fire *after* Oct 28 (unburnt) | 16,310 | 0.044 | 0.133 | 0.683 | 1.437 | 0.099 | 0.224 | 0.088 | 0.057 |
| standing crop (NDVI>0.6), >2 km from fires | 147,515 | 0.438 | 0.235 | 0.303 | 1.349 | 0.053 | 0.112 | 0.006 | 0.031 |
| harvested Oct 18–28 (dNDVI>0.25), >2 km from fires | 191,432 | −0.005 | 0.097 | 0.723 | 1.565 | 0.100 | 0.244 | **0.399** | 0.352 |

What the table shows:

- Pixel-level separation is negligible. Comparing all pixels within 200 m of a fire with the LATER group gives Kaufman–Remer M ≤ 0.20 and AUC 0.50–0.62 for every index. A 200 m disc is 12.5 ha and covers 6–12 fields, so the burnt field is diluted.
- Burnt neighbourhoods are darker in SWIR: B11 0.232 vs 0.273, B12 0.198 vs 0.224, MIRBI 1.63 vs 1.44.
- **Harvest alone produces a larger dNBR than burning**, so the generic USGS dNBR classes (low severity from ≈0.1; not re-verified today) cannot be used on crop residue.

### 3.4 Footprint-level separability

Unit: P80 of each feature over the cropland pixels of a 200 m VIIRS disc. Positives: 116 discs with a fire between the overpasses. Negatives: 60 discs with a fire after Oct 28 that are >1 km from any earlier fire.

| pair | ΔB8A | ΔNBR2 | ΔB12 | ΔB11 | MIRBI_post | NBR_post | ΔMIRBI | dNBR | dNDVI |
|---|---|---|---|---|---|---|---|---|---|
| Oct 23→28 (5 d) AUC | **0.716** | 0.702 | 0.696 | 0.695 | 0.691 | 0.684 | 0.679 | 0.670 | 0.617 |
| Oct 18→28 (10 d) AUC | 0.707 | 0.686 | 0.625 | 0.665 | 0.693 | 0.686 | 0.707 | **0.580** | 0.483 |

- Youden-optimal cuts on the 5-day pair: ΔB8A ≥ 0.073, ΔNBR2 ≥ 0.078, ΔB11 ≥ 0.072, ΔB12 ≥ 0.028, MIRBI_post ≥ 1.86.
- The longer gap degrades dNBR because harvest contaminates it. This is consistent with Walker et al. 2022, who found the burn signal persists only days and that tillage mimics the NIR/SWIR drop.
- Visual check: before/after chips of the top-FRP fires show distinct black or dark-brown fields appearing inside the VIIRS circles.

### 3.5 Rules tested

Bag counts as "detected" if ≥0.52 ha of the disc is flagged.

| rule | crop flagged | bag TPR | bag FPR* | bag AUC |
|---|---|---|---|---|
| Darkening Oct 23→28: ΔB8A>0.07 & ΔB11>0.07 & ΔB12>0.03, 2×2 opening, ≥13 px | 4.9% | 0.69 | 0.47 | 0.699 |
| **Darkening + CEEW char state (BAIS2≥0.78 & TBI≤0.34 on post)** | 3.2% | **0.66** | **0.35** | 0.699 |
| CEEW T3 single-date only (Oct 28) | 21.1% | 0.87 | 0.77 | 0.701 |
| Grid-search best darkening (ΔB8A>0.09, ΔB11>0.03, ΔB12>0.03) | 5.1% | 0.72 | 0.45 | 0.684 |

\* The negative discs sit inside a hotspot, so part of the FPR is real unseen burns or tillage. Treat it as an upper bound.

A single-date char rule flags any char of any age plus dark soil, which is why it hits 21% of cropland. **Use it only as a state constraint on a bi-temporal change.**

### 3.6 Literature thresholds and validation

- **CEEW 2025** ("Monitoring Crop Residue Burning Better", §Data & methodology, Tables 2 and 7):
  - Thresholds: **BAIS2 ≥ 0.78 & TBI ≤ 0.34**.
  - Validated in Hoshiarpur/Jalandhar on 29 Oct 2021: pixel accuracy 0.75, TPR 0.59, FPR 0.006. It flagged 262/287 burnt fields and 13/95 unburnt fields.
  - CEEW uses B8 inside the BAIS2 square root. With B8A the result was identical in our sample.
- **Deshpande et al. 2022** (MethodsX 9:101741): BAIS2 + TBI with 5th–95th percentile cuts, which separated better than MIRBI and NBR.
- **NRSC/ISRO**: operational MIRBI on 10 m, 5-day Sentinel-2 for Punjab/Haryana in 2022–23. The threshold is not public, and the NRSC PDF links now return 404, so this is cited from a search snippet.
- **Walker et al. 2022** (arXiv 2209.10148):
  - Ground truth of 441 burnt and 240 unburnt plots in Punjab, 2019.
  - The char index and NIR change are best right after a burn. M > 1.5 is reached only within days.
  - RF plot accuracy 0.82 (Sentinel-only 0.81). Tilled-unburnt plots show almost the same NIR/SWIR change.
- **Naive weak supervision (tested)**:
  - LightGBM on 36 features (post bands and indices, deltas vs Oct 23 and Oct 18), 300 trees, 31 leaves.
  - Labels: all pixels in fire discs = 1, LATER discs and green crop = 0, with one multiple-instance refinement round (keep the top 30% of positive pixels).
  - Spatial GroupKFold over 10 km blocks gave **bag AUC 0.597 (round 0) and 0.587 (round 1)**, below the single features.
  - The resulting map flagged 12% of cropland, including river sand bars. Label noise dominates, so **reject this design**.

### 3.7 Recommended weak-supervision scheme for the LightGBM pixel classifier

This scheme is **UNVERIFIED** end-to-end.

1. **Positives (precision first).** Take pixels of rule clusters from §3.5 (row 2) whose observation interval contains a VIIRS or MODIS detection ≤375 m away with confidence ≠ low. This yields field-shaped labels instead of 12.5 ha discs.
2. **Negatives.**
   - (a) Pixels in a "harvested residue" state on the post date that **did not darken** and sit ≤375 m from a fire detected *after* the post image. These are ready to burn but not yet burnt, and they also serve as labels for the "burns next" model.
   - (b) Standing crop (NDVI > 0.6).
   - (c) Water, built-up and other non-crop pixels from WorldCover.
   - Leave tilled-without-char pixels unlabeled.
3. **Training.** Use positive-unlabeled learning: Elkan–Noto calibration, or a weight of 0.3 on unlabeled-as-negative. Include features from §3.4 plus days since the last clear observation and a haze flag.
4. **Evaluation.**
   - Spatial block CV, reported as FIRMS-bag AUC (positive vs LATER discs), must beat 0.72.
   - Manually audit ~200 random "unseen" clusters in the before/after viewer (§5) and report their precision.
5. With a `FIRMS_MAP_KEY`, extend the labels to the whole season (Oct 1–Nov 30) instead of the last 7 days (§7).

### 3.8 Smoke haze (observed)

On Oct 23 in the hotspot window:

| measure | Oct 18 | Oct 23 | Oct 28 |
|---|---|---|---|
| `eo:cloud_cover` (tile) | — | 22.4% | — |
| SCL cloud + cirrus (window) | — | ≈1.3% | — |
| B02 median | 0.063 | **0.117** | 0.073 |
| NDVI median | 0.305 | 0.203 | 0.249 |
| B12 median | 0.189 | 0.197 | 0.210 |
| Sen2Cor AOT | 0.26 | 0.38 | 0.19 |

Sen2Cor AOT is near-constant per scene, so it is a weak flag. Flag a scene as hazy when the cropland B02 median rises by >0.03 over the previous clear pass. On hazy scenes use SWIR-only features (ΔB11, ΔB12, ΔNBR2, MIRBI) and lower the confidence.

## 4. Volume and compute: all Punjab + Haryana

**AOI.** Source: geoBoundaries `gbOpen/IND/ADM1`, `shapeISO` IN-PB and IN-HR. Endpoint: `https://www.geoboundaries.org/api/current/gbOpen/IND/ADM1/`, which returns the GeoJSON URLs. Use `shapeISO`, because the name is "Haryāna" with a diacritic. Area: PB 50,321 km² + HR 44,182 km² = **94,503 km²**.

**MGRS tiles.** 23 tiles intersect the AOI, and 19 have ≥250 km² of overlap: 43RDN RDP RDQ REL REM REN REP REQ RFL RFM RFN RFP RFQ RGL RGM RGN RGP SDR SER. The slivers are 43RCP, RGQ, SES and SFR.

Passes from Oct 18–30:

| orbit | dates | tiles |
|---|---|---|
| R005 | Oct 18, 23, 28 | 16 |
| R105 | Oct 20, 22, 25, 30 | 15 |
| R048 | Oct 21, 26 | 2 |

The AOI is fully covered every ≤5 days. The central strip (RE*/RF*) is seen by two orbits, which gives a 2–3 day revisit.

**MB per tile-band.** Exact compressed bytes per overview level from the COG headers, tile 43REQ, 2026-10-18:

| asset | PC 10 m | PC 20 m | PC 40 m | PC 80 m | E84-c1 20 m | E84-c1 40 m |
|---|---|---|---|---|---|---|
| 10 m band (B02/B04) | 207–214 | 52–54 (overview) | 13.3–13.6 | 3.3 | 43–46 (overview) | 11.1–11.7 |
| 20 m band (B06/B8A/B11/B12) | — | 54–55 | 13.7–13.8 | 3.5 | 46–47 | 11.8–12.0 |
| SCL | — | 2.8 | 1.0 | 0.3 | 2.9 | 1.0 |

PC files are DEFLATE without a predictor, 512² blocks. E84 uses predictor 2, with 1024² blocks on 10 m bands.

**Per full AOI refresh.** These figures are derived from the measurements above (≈4.5 KB/km²/band at 20 m, ≈1.15 KB at 40 m, plus 20% for tile overlap). A single pass covers roughly 0.5–0.7× of this.

| asset set | 20 m volume | 20 m time on this link (~6 MB/s) | 40 m volume | 40 m time |
|---|---|---|---|---|
| Minimal: B8A, B11, B12, SCL | 1.6 GB | ≈4.5 min | 0.4 GB | ≈1.1 min |
| Detection set: 9 bands + SCL | 4.6 GB | ≈13 min (≈21 min from E84) | 1.2 GB | ≈3.3 min |

On an in-region cloud VM this drops below about 1 min.

**CPU (4 cores).** Cropland is about 200 M px at 20 m (about 50 M at 40 m).

| step | measured rate | full AOI at 20 m |
|---|---|---|
| Feature build (36 features, numpy) | 1.2 Mpx/s per core | ≈45 s |
| Rules, labelling, vectorising | — | ≈1–2 min |
| LightGBM (300 trees, 4 threads) | 0.19 Mpx/s | ≈17 min (≈4.4 min at 40 m) |

To cut the LightGBM cost, run inference only on candidate pixels (harvested or darkened, 5–20%), which takes ≤3 min, or use fewer trees. Process in 2048² blocks (about 170 MB per date for 10 float32 bands).

**Incremental pipeline (recommended)**

1. **Poll.** Every 30 min between 06 and 14 UTC, run an E84-c1 STAC search with `intersects=AOI` and `datetime≥last_seen`. Skip items with `s2:nodata_pixel_percentage>95` or `eo:cloud_cover>70`. Process each (tile, date) once and record it in PostGIS.
2. **Read.** For blocks that contain WorldCover-40 pixels, read B02 B03 B04 B06 B07 B08 B8A B11 B12 SCL at 20 m. Cache the cropland mask per tile at 20 m once.
3. **Validate.**
   - The B12 median of SCL-valid pixels must lie in (0.02, 0.6).
   - The correlation with the previous clear pass of the same tile must be > 0.3.
   - If the scene fails, fall back to the other catalogue. That check is what catches PC 2026-10-28.
   - Set a haze flag as in §3.8.
4. **Per-pixel state.** Store per tile at 20 m, cropland only, as Zarr/npz (about 12 B/px, about 2.4 GB for the AOI):
   - `last_clear_doy`, last B8A/B11/B12/NDVI/TBI as float16
   - `state` ∈ {standing, harvested-residue, burnt, dark-soil/tilled, green-again}
   - `harvest_doy`, `burn_doy`, `burn_conf`

   Transitions:
   - standing→harvested when NDVI drops >0.25 to <0.35 and B11 rises.
   - harvested→burnt when the §3.5 darkening + char rule fires between consecutive clear observations ≤6 days apart.
   - harvested→tilled when there is darkening without char.
   - burnt→tilled when char fades.
5. **Events.** Label 8-connected new-burnt pixels after a 2×2 opening, keep clusters ≥13 px (0.52 ha), run `rasterio.features.shapes`, simplify by 10 m, and store in PostGIS with:
   - observation interval (prev_clear, this_pass)
   - area, mean ΔB8A/ΔB11/ΔB12, BAIS2, TBI, confidence
   - FIRMS match: any VIIRS/MODIS detection ≤750 m inside the interval ⇒ `seen`, otherwise **`unseen`**

   In the sample, detected clusters had a median of 1.2 ha and a p90 of 3.6 ha.
6. **Next-burn candidates.** Every harvested-residue pixel or field, with features: days since harvest, burn rate of neighbours within 2 km over the last 5 days, district, wind and rain forecast. Train on LATER-type transitions (§3.7, item 2a).

## 5. PC data API tiles (verified)

The base is `https://planetarycomputer.microsoft.com/api/data/v1`; routes were read from `/openapi.json`. All responses below were `200 image/png`, item tiles took 0.5–1.4 s and were 70–165 KB at 256².

```
# (a) true colour (PC's own "Natural color" render)
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&assets=B04&assets=B03&assets=B02&nodata=0&color_formula=Gamma RGB 3.2 Saturation 0.8 Sigmoidal RGB 25 0.35
# (b) SWIR false colour
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&assets=B12&assets=B8A&assets=B04&nodata=0&color_formula=Gamma RGB 3.7 Saturation 1.5 Sigmoidal RGB 15 0.35
# (c) NBR with colormap: remove the +1000 DN offset (PC's default NDVI render does NOT and is washed out)
/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&item={ITEM}&expression=(B8A-B12)/(B8A%2BB12-2000)&asset_as_band=true&nodata=0&rescale=-0.5,0.7&colormap_name=rdylgn
# TileJSON for MapLibre (same query string):     /item/WebMercatorQuad/tilejson.json?collection=…&item=…&expression=…
# Evidence chip for one field (bbox in lon/lat):  /item/bbox/75.08,31.31,75.12,31.35/512x512.png?collection=…&item=…&assets=B12&assets=B8A&assets=B04&nodata=0&…   (2.1 s)
```

- Tested on Sangrur z13 tile 5821/3373 with item `S2B_MSIL2A_20261018T053829_R005_T43REP_20261018T092057`. All renders were correct.
- The same request for the 10-28 item returns noise or black. That is a data problem, not a URL problem.
- In URLs, encode `+` as `%2B`. Expressions use asset names with `asset_as_band=true`.
- dNBR needs two items, so the tiler cannot produce it in one request. Show pre and post side by side or with a swipe, and serve our own burn polygons as GeoJSON or MVT.

**Mosaic (latest clear imagery across Punjab).** First register the search:

`POST /mosaic/register` with body:

```
{"collections":["sentinel-2-l2a"],"filter-lang":"cql2-json","filter":{"op":"and","args":[
 {"op":"s_intersects","args":[{"property":"geometry"},{AOI polygon}]},
 {"op":"anyinteracts","args":[{"property":"datetime"},{"interval":["2026-10-15T00:00:00Z","2026-10-27T23:59:59Z"]}]},
 {"op":"<=","args":[{"property":"eo:cloud_cover"},10]}]},"sortby":[{"field":"datetime","direction":"desc"}]}
```

The response took 0.24–0.55 s:

```
{"id":"pc1.1793463471.2889764adaee4f188a47344207a03d11","links":[…/mosaic/{id}/info, …/tilejson.json, …/WMTSCapabilities.xml]}
```

Then request tiles:

`GET /mosaic/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?collection=sentinel-2-l2a&assets=B04&assets=B03&assets=B02&nodata=0&color_formula=…`

These took 0.85–2.5 s at z9 and z12, and the NBR expression also works. The tilejson is at `/mosaic/{id}/WebMercatorQuad/tilejson.json?collection=…`.

Mosaic gotchas:

- An interval that included Oct 28 rendered **black** tiles at z9 and z12, because the corrupt Oct 28 items sort first.
- `/mosaic/{id}/point/{lon},{lat}` returned HTTP 500.
- `/mosaic/{id}/tiles/…/assets` returned 422 ("must contain a collection parameter") even with `collection=`.
- Default pixel selection is "first", and the PgSTAC `time_limit` is 5 s.
- Recommendation: keep Oct 28 (or any date that fails validation) out of the interval, or select explicit item IDs with an `ids` / `in` filter (**UNVERIFIED**).

Other tile layers verified:

- S1 RTC: `…?collection=sentinel-1-rtc&item={id}&assets=vv&rescale=0,0.5&colormap_name=gray`, 1.8 s.
- WorldCover: `…?collection=esa-worldcover&item=ESA_WorldCover_10m_2021_v200_N30E075&assets=map&colormap_name=esa-worldcover`, 0.9 s.

## 6. Sentinel-1 RTC (PC `sentinel-1-rtc`)

Availability, verified:

- 8 items in Oct 2026 over Sangrur, **all S1D**: Oct 4/16/28 ascending orbit 100, Oct 11/23 ascending 27, Oct 12/24 descending 34, Oct 19 descending 136. The same geometry repeats every 12 days, and any geometry is available every 3–5 days.
- The latest item anywhere was S1C 2026-10-30 05:58 UTC, already ingested in the afternoon.
- Assets: `vv` and `vh` as float32 linear γ⁰, `…/measurement/iw-vv.rtc.tiff`, EPSG:32643, 10 m, overviews 2–64, nodata −32768, 512² DEFLATE.
- **`.tiff` extension**: set `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff"`, otherwise GDAL fails with "not recognized as a supported dataset name".
- Licence `CC-BY-4.0` (providers Catalyst and Microsoft). The GRD collection is `proprietary` (Copernicus terms).
- Reading the 40 km window at about 20 m took 11.5–15.4 s per polarisation.
- Check footprints: the frame that covers Sangrur did not cover the 43REQ window. Use the adjacent frame, the T130339 one.

Test: Oct 16 → Oct 28, same geometry (ascending 100), footprint P20/P50/P80 of ΔVV, ΔVH, Δ(VH−VV), VH_post and VV_post.

- Fire discs (n=159) vs LATER discs (n=31): **AUC 0.50–0.57**. For example, ΔVH P50 was −0.81 dB vs −0.88 dB.
- Crop state on the Oct 28 pass:
  - **VH: standing −15.05 dB vs harvested −17.98 dB, pixel AUC 0.77.**
  - VV: AUC 0.64.
  - VH−VV: AUC 0.65.
- Literature:
  - No study found that detects post-harvest residue burning with C-band SAR.
  - Wildfire studies report VH decreases over burnt *forest*, and Belenguer-Plomer et al. 2019 (RSE) use VV/VH change together with hotspots.
  - Harvest-date detection from VH peaks or coherence is established.
- **Verdict:** use S1 only as a cloud and haze fallback for the standing→harvested transition. It cannot confirm or deny burning.

## 7. Recommended approach and tested code

**Data roles**

- Earth Search `sentinel-2-c1-l2a` is the analytics source, with PC `sentinel-2-l2a` as a validated fallback.
- WorldCover 2021 from PC is the cropland mask.
- FIRMS supplies labels and the seen/unseen join:
  - Keyless 7-day CSVs today: `https://firms.modaps.eosdis.nasa.gov/data/active_fire/{noaa-20-viirs-c2|suomi-npp-viirs-c2|noaa-21-viirs-c2|modis-c6.1}/csv/{J1_VIIRS_C2|SUOMI_VIIRS_C2|J2_VIIRS_C2|MODIS_C6_1}_South_Asia_7d.csv`.
  - Each took 1.9–2.9 s and was 0.15–1.24 MB. Last-Modified was 15:42 UTC, and range requests work (206).
  - Columns: `latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight`.
  - Sample row: `28.96295,79.78813,330.52,0.55,0.68,2026-10-23,0659,N20,nominal,2.0NRT,297.77,6.05,D`.
- `FIRMS_MAP_KEY` unlocks `/api/area/csv/{MAP_KEY}/{VIIRS_NOAA20_NRT|…}/{w,s,e,n}/{1..10}[/{YYYY-MM-DD}]` for season-long history. With an invalid key it returns `HTTP 400 "Invalid MAP_KEY."`; a valid key is **UNVERIFIED**.
- The PC tiler serves the evidence viewer.

**Tested snippet.** I ran it today on box `[500000,3460000,510240,3470240]` with pre `S2C_T43REQ_20261023T054822_L2A` and post `S2B_T43REQ_20261028T054808_L2A`.

- Wall time was 24.5 s, including the FIRMS download.
- Output: `n_clusters=327, area_ha=555.7, unseen=263`. This run had no WorldCover mask, so add one in production.

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

**For PC instead of E84:**

- Use band keys (`B04`, …) and open the client with `modifier=planetary_computer.sign_inplace`.
- Always run the plausibility gate. Optionally cross-check a 64² window against the other catalogue; identical DNs mean the scene is good.

## 8. Licensing and attribution

| source | licence / terms | attribution |
|---|---|---|
| Sentinel-2/-1 (both catalogues) | Copernicus Sentinel Data Terms; STAC `license: proprietary` (free, full, open) | "Contains modified Copernicus Sentinel data 2026" |
| PC `sentinel-1-rtc` | CC-BY-4.0 (Catalyst, Microsoft) | credit Microsoft Planetary Computer |
| `esa-worldcover` | CC-BY-4.0 | "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium" |
| NASA FIRMS | open | "We acknowledge the use of data from NASA FIRMS (https://earthdata.nasa.gov/firms), part of NASA ESDIS" |
| geoBoundaries IND ADM1 | CC BY 2.5 IN (DataMeet / Election Commission of India) | per API record |
| CEEW 2025 thresholds | cited publication | — |
| Walker et al. 2022 | arXiv | cite |

Every value shown in the app is derived live from these sources, so no static data is involved. **Do not cache the Oct 28 PC scenes.**

## 9. Open items / not verified

- A valid FIRMS `MAP_KEY` (season-long labels).
- Whether PC repairs the 2026-10-28 assets. Re-test with `s02b`-style PC-vs-E84 identity checks.
- The rule-seeded PU LightGBM from §3.7.
- Specificity of the "unseen" candidates against ground truth or very-high-resolution imagery, including the tillage confound.
- Whether a `ids`-filtered mosaic works.
- Exact dNBR severity breaks (Key & Benson 2006) were not re-verified, and they are not used.
