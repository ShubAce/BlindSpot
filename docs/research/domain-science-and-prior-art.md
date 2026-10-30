# Domain science, prior art and pitch-grade facts (BlindSpot)

Researched 2026-10-30 (IST). Every fact has a URL. Tags: **[V]** = I read the primary source or ran the call today;
**[S]** = press/secondary page read today; **[S, search-level]** = only a search-engine summary of that URL was seen
(page not opened - re-check before quoting); **[U]** = could not verify (reason given). Scratch scripts and raw downloads
are in the session scratchpad only (not in repo).

## 0. Pitch-grade facts (each one is traceable)

1. **Peak burning moved from ~13:30 IST (2020) to ~17:00 IST (2024)**, with VIIRS trend -18,883 counts/yr (2020-24)
   while geostationary SEVIRI counts were flat (+43/yr). ISRO SAC, *Current Science* 129(10):921-923, 25 Nov 2025,
   doi:10.18520/cs/v129/i10/921-923, https://www.currentscience.ac.in/Volumes/129/10/0921.pdf **[V]**
2. **96.2% of high-confidence GK2A fire detections in Punjab (Oct-Nov 2025, 12-18 IST window) fell at or after 16:00 IST;
   in 2020 it was 40.2%.** Peak hour moved 15:00 -> 17:00 IST. Computed today from Jethva's open dataset
   (Zenodo 10.5281/zenodo.20084790, CC BY 4.0); pixel-hour detections, not unique fires. **[V]**
3. **Same years, two stories: counts -90%, burnt area -37%.** CREAMS Punjab events 49,922 (2022) -> 5,114 (2025)
   (83,002 in 2020); iFOREST Sentinel-2 burnt area 31,447 km2 (2022) -> ~20,000 km2 (2025).
   https://iforest.global/wp-content/uploads/2025/12/StubbleBurning_StatusReport_2025_Website.pdf **[V]**
4. **The official bulletin admits the gap:** "all agricultural fires events at all time are not recorded by satellites due
   to: (a) fires taking place when satellite over-pass is not there, and (b) presence of thick cloud and smog".
   CREAMS bulletin No. 46, 30-Oct-2026, last page. **[V]**
5. **On 30 Oct 2026 all 44 officially counted fires came from one satellite in a 4-minute window** (Suomi-NPP,
   acq 13:16:03-13:20:00 IST). On 29 Oct, 107 of 223 events (99 of Punjab's 194) came from drifting Aqua at ~16:09 IST,
   so today's counts partly depend on an ageing satellite's drift. CREAMS WFS + bulletin **[V]**
6. **Ground truth: 7 satellite detections vs 169 actual fires**, 2 days, 74 km2, Sangrur (2020) - CEEW, cited in
   CEEW Issue Brief Oct 2026 (*Beyond Single Sources*), https://www.ceew.in/sites/default/files/ceew-improving-crop-residue-burning-monitoring-satellite-imagery.pdf **[V]** (original 2025 study not opened)
7. **Delhi's official source-apportionment inherits the blind spot:** IITM DSS states its stubble share "is determined using
   active fire count data from the VIIRS satellite-based instrument received on the evening of the previous day".
   https://ews.tropmet.res.in/dss/index.php **[V]**
8. **Supreme Court, 18 Nov 2024:** "The ultimate object should be to ensure that all instances of farm burning throughout the
   day can be reported" (direction to procure geostationary data). https://www.livelaw.in/amp/top-stories/get-data-from-stationary-satellites-on-farm-fires-instead-of-polar-orbiting-ones-supreme-court-directs-centre-275553 **[S]**
9. **Burning is migrating:** CREAMS 2025 season - Madhya Pradesh 17,067 and UP 7,290 events vs Punjab 5,114. **[V]**

## 1. Paddy-residue burning: season, behaviour, counts

**Timeline (Punjab)**
| Stage | Window | Source |
|---|---|---|
| DSR sowing / transplanting 2026 | DSR 15-31 May; transplant Zone 2 (Gurdaspur, Pathankot, Amritsar, Tarn Taran, Rupnagar, Mohali, Fatehgarh Sahib, Hoshiarpur) 1 Jun; Zone 3 (Faridkot, Bathinda, Ferozepur, Muktsar, Fazilka) 5 Jun; Zone 4 (Ludhiana, Malerkotla, Mansa, Moga, Patiala, Sangrur, Barnala, Kapurthala, Jalandhar, SBS Nagar) 9 Jun | https://babushahi.com/full-news.php?id=220401 **[S]** |
| Official fire-monitoring window | 15 Sep - 30 Nov ("standard protocol developed by ISRO") | PIB 01-Dec-2025 https://www.pib.gov.in/PressReleasePage.aspx?PRID=2197201 **[V]** |
| Harvest | Oct to mid-Nov; peak burning 2nd half Oct-1st half Nov | Arbain & Imasu 2025 (below); ISPRS 2019 https://isprs-archives.copernicus.org/articles/XLII-3-W6/31/2019/isprs-archives-XLII-3-W6-31-2019.pdf **[S]** |
| Harvest -> wheat sowing gap | quoted as 10-15 d (Arbain & Imasu 2025), 20-30 d (CRM guidelines 2018 https://agrimachinery.nic.in/Files/Guidelines/CRM.pdf **[V]**) | varies by source; treat as 10-30 d |
| Wheat sowing | PAU: first fortnight of Nov optimal, sow by 15 Nov; after that "1.5 quintal per acre per week" yield loss | https://www.tribuneindia.com/news/punjab/sow-wheat-by-nov-15-to-prevent-grain-shrivelling-pau-to-farmers-448039 **[S]** |

- **Groundwater law compressed the window.** Peak post-monsoon fire activity and AOD in Punjab shifted ~2 weeks later
  (2003-2016); compression coincided with +42% total burning, +55% regional AOD; drivers include the 2009 groundwater
  policy that delays transplanting. Liu et al., Environ. Res. Lett., doi:10.1088/1748-9326/abcc28 **[V abstract]**
- **Harvest-to-burn lag: [U]** no study measuring days per field was found. Proxies: burnt plots "are almost always tilled
  shortly after the burn event"; a plot "may be completely burnt within a day and resemble unburned plots within two to
  three days" (Walker et al. 2022, arXiv:2209.10148 **[V]**); CEEW: many fields are burnt and cleared within the 5-day
  Sentinel-2 revisit (CEEW 2026 brief **[V]**). Fire lifetime ~1/2 h (Liu et al. 2020, Atmos. Env. X, preprint
  doi:10.31223/osf.io/ye6x7 **[V abstract]**). BlindSpot should *measure* lag (S2 NDVI-drop date -> burn-scar date).
- **Field size:** 3,206 Punjab rice plots, mean 1.4 ha, median 0.9 ha (Walker et al. 2022 **[V]**). So one S2 20 m pixel
  = 0.04 ha (22-35 px/field); one VIIRS 375 m pixel = 14 ha (~10 fields); one GK2A ~4 km pixel ~1,600 ha (~1,100 fields).
- **Why farmers burn:** short gap before wheat; combine harvesters leave stalks "too big to be tilled back into the soil"
  (Walker 2022 **[V]**); survey of 150 households: ~41% cite the short window, 48% say burning is "quicker and more
  economical"; Super Seeder operation ~INR 2,000-2,500/acre (CEEW, via https://scroll.in/article/1011927 and CEEW blog
  **[S, search-level]**). Happy Seeder systems are 10-20% more profitable than burning (Shyamsundar et al., *Science*
  365:536, doi:10.1126/science.aaw4085 **[V abstract]**) yet burning persists -> behaviour/liquidity, not only economics.
- **Straw volume:** states planned for 27.62 Mt (2.762 crore t) of paddy stubble in 2026 (PIB 16-Jun-2026
  https://www.pib.gov.in/PressReleasePage.aspx?PRID=2273815 **[V]**). Punjab alone ~19.5-20 Mt/yr (NGT filings/CEEW **[S, search-level]**).
  CREAMS 2020: Punjab straw 25.45 Mt, 12.84 Mt burnt (50.5%) (special bulletin, below) **[V]**.

**Official fire counts, 15 Sep-30 Nov (CREAMS final bulletin 30-Nov-2025, No. 77) [V]**
| State | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|
| Punjab | 83,002 | 71,304 | 49,922 | 36,663 | 10,909 | 5,114 |
| Haryana | 4,202 | 6,987 | 3,661 | 2,303 | 1,406 | 662 |
| Uttar Pradesh | 4,631 | 4,242 | 3,017 | 3,996 | 6,142 | 7,290 |
| Madhya Pradesh | 14,148 | 8,160 | 11,737 | 12,500 | 16,360 | 17,067 |
| Rajasthan | 1,756 | 1,350 | 1,268 | 1,775 | 2,772 | 2,890 |
Sangrur (top district): 11,727 (2020) -> 695 (2025). PIB (01-Dec-2025): Punjab 2025 = -53% vs 2024, -86% vs 2023, -90% vs
2022, -93% vs 2021; Haryana 662 = -53/-71/-81/-91%. PIB attributes it to "State and District-specific Action Plans,
large-scale deployment of crop residue management machinery, and strict enforcement measures" **[V]**.

**2026 season to date (CREAMS bulletin No. 46, 15 Sep-30 Oct 2026) [V]:** total 2,739 events = Punjab 1,047, Haryana 29,
UP 345, Delhi 2, Rajasthan 614, Uttarakhand 174, MP 528. Same-period Punjab: 29,712 (2020), 10,374 (2021), 13,873 (2022),
6,284 (2023), 2,466 (2024), 1,418 (2025), **1,047 (2026)**; Haryana 742 (2024), 93 (2025), 29 (2026). Leading districts:
Tarn Taran 405, Amritsar 175, Ferozepur 105; Sangrur only 13 (Zone-4 transplanted 9 Jun, i.e. later harvest - consistent
with, not proof of, a timing effect). PPCB's 1,007 to 29 Oct (https://www.thehansindia.com/news/national/punjab-records-1007-stubble-burning-cases-tarn-taran-tops-list-with-397-incidents-1137378 **[S]**) + CREAMS' 40 on 30 Oct = 1,047, so the two agree.
Enforcement to 29 Oct: 246 FIRs, 297 red entries, Rs 17.50 lakh compensation (Rs 13.15 lakh recovered) **[S]**.
Centre's 13-Oct-2026 review: MP 277 vs 55 and Rajasthan 60 vs 20 (15 Sep-12 Oct, y/y; search-level); states asked to
"initiate ground monitoring, particularly after 3 PM" (https://www.thedailyjagran.com/india/news/delhi-air-pollution-why-center-has-directed-ground-monitoring-of-stubble-burning-after-3-pm-despite-satellite-tracking-success-10332664 ,
https://www.business-standard.com/amp/industry/agriculture/centre-asks-5-states-to-go-on-mission-mode-against-stubble-burning-126101301228_1.html **[S]**).

## 2. The satellite blind spot: evidence, court, officials

**ISRO SAC (SEVIRI) [V, full text read]** - Nimisha Singh, Rohit Pradhan, Bipasha Paul Shukla, Mehul R. Pandya (SAC-ISRO,
Ahmedabad). "Evidence of shift in stubble burning timing over northwest India from geostationary satellite observations",
*Current Science* 129(10), 921-923 (25 Nov 2025; received 25 Sep, accepted 24 Oct 2025). Data: MSG/SEVIRI IODC active-fire
product (CAP format, EUMETSAT; Meteosat-9 at 45.5E primary since Jun 2022, earlier Meteosat-8 at 41.5E), every 15 min,
15 Oct-30 Nov 2020-2024, Punjab+Haryana. Findings: 2020 fires "ignited around noon, peaked near 13:30 IST, and subsided by
15:30 IST"; gradual shift 2021-24, "peak fire activity occurring around 17:00 IST in 2024"; "diurnal window of fire activity
narrowed substantially, suggesting an informed adjustment by farmers"; VIIRS S-NPP trend -3,743/yr (2012-24) and
-18,883/yr (2020-24) vs SEVIRI +43/yr. Caveats stated: 2-5 km pixels, low saturation, weak sensitivity to faint fires.
Companion: Singh et al. 2025, "Active fire detection using Indian geostationary satellite INSAT-3DS", *Remote Sens. Earth
Syst. Sci.* 8:1014-1027, doi:10.1007/s41976-025-00233-4 (late-afternoon peak in mid-Nov 2024) **[V citation only]**.

**Hiren Jethva (NASA GSFC / Morgan State) GK2A [V]** - *Remote Sensing* 18(10):1506 (11 May 2026), "Timing the Flames:
Geostationary Satellite Detection of Diurnally Shifting Stubble Burning in Northwestern India", doi:10.3390/rs18101506
(MDPI 403s scripts; abstract via https://api.semanticscholar.org/graph/v1/paper/DOI:10.3390/rs18101506). Abstract: GK2A-AMI
shows "a clear spike in fire-related signals occurring around and after 4:00 PM local time", shift "beginning in 2022";
Aqua's orbital drift catches some late fires "but the MODIS still misses a large number of fires occurring around and after
4 pm"; AMI ~4 km over NW India. Data: Zenodo record 20084790 (CC BY 4.0, 12 May 2026): 14 ASCII files (2019-2025 x
Punjab/Haryana, Oct-Nov), columns year, month, day, hour(IST), minute, lon, lat, BTD, BT3.8, BT11.2, confidence(1-3);
12:00-18:00 IST only; README: use confidence 2-3. NASA Earth Observatory (15 Dec 2025): "Most stubble fires now happen
between 4 p.m. and 6 p.m."; 2025 season "moderate" vs recent years.
https://science.nasa.gov/earth/earth-observatory/new-timing-for-stubble-burning-in-india/ **[V]**
*Computed today (Punjab, conf>=2):* 2020 n=15,229, % by hour 12-18 = 0.5/8.2/21.4/29.7/19.3/7.9/13.0;
2025 n=19,392 = 0.2/0.6/0.5/2.5/22.7/61.1/12.4. (Counts are pixel-hours of a preliminary 4 km algorithm; do not compare
magnitudes with VIIRS counts.)

**iFOREST "Stubble Burning Status Report: 2025" (08-12-2025) [V, PDF read]** - Ishaan Kochhar, Manish Bilore, Meet Makwana.
Stack: MODIS/VIIRS counts (conf >30%, CGLS-LC100 cropland, de-duplicated), Sentinel-2 dNBR burnt area at 100 m ("possible
to do at 20 m"), SEVIRI FIRC-IODC 15-min. Findings: counts -92% Punjab / -90% Haryana vs 2021 peaks; ">90% of large farm
fires in Punjab in 2024 and 2025 occurred after 3:00 PM. In 2021, only 3%"; Haryana mostly after 3 PM since 2019;
Punjab kharif burnt area 31,447 km2 (2022) -> ~20,000 km2 (2025) (report says "37%" on p.29 and "35%" on p.25); Haryana
11,633 km2 (2019) -> 8,812 km2 (2025), fluctuating 7,000-10,000 km2; 2025 analysis used data only to 27 Nov. Asks IITM DSS to
"revise its methodology" and CREAMS to "begin monitoring and publishing burnt-area data".

**Supreme Court (MC Mehta v UoI, WP(C) 13029/1985; Justices A.S. Oka & A.G. Masih) [S via LiveLaw]**
- 18 Nov 2024: amicus Aparajita Singh: NASA satellite passes ~10:30 am / 1:30 pm; Korean geostationary satellite saw fires at
  4.20 PM. Order: arrangements for "getting data either from Korean stationery satellites or any other stationery satellites
  ... The ultimate object should be to ensure that all instances of farm burning throughout the day can be reported" (link in s.0).
- 28 Nov 2024: wants "a machinery to be in place that 24/7, we must get data of stubble burning"; on a report that officials
  advised burning after 4 pm: "if it is correct, it is very serious". ASG: ISRO testing a burnt-area protocol.
  https://livelaw.in/top-stories/supreme-court-seeks-247-monitoring-of-stubble-burning-says-state-officials-cannot-help-farmers-avoid-satellite-detection-276663
- 12 Nov 2025 (CJI B.R. Gavai, sceptical): "Do you want us to believe that all farmers will wait for the satellite to move?"
  https://scroll.in/latest/1088511/sc-to-examine-if-punjab-haryana-farmers-are-evading-satellite-surveillance-to-burn-crop-stubble

**Officials / rebuttals / responses**
- Government: decline is real and due to action plans, CRM machines, enforcement (PIB 01-Dec-2025 **[V]**).
- Ravindra Khaiwal (PGIMER): "Over 95% of fires are detected in morning passes, not evening" (ThePrint, 3 Dec 2025,
  https://theprint.in/environment/are-farm-fires-going-uncounted-isro-study-suggests-farmers-have-figured-out-a-way-to-evade-satellites/2797583/ **[S]**).
  Note: CREAMS' own event lists for 29-30 Oct 2026 contain only S-NPP 13:16-13:37 IST, Aqua 16:09-16:15 IST and 4 night
  detections at ~02:23 IST - nothing from Terra's ~09:00-10:00 IST pass **[V]**.
- CREAMS' own lead (V.K. Sehgal, Nov 2024): "we are witnessing a spike in farm fires during evening and night hours"
  (https://www.tribuneindia.com/news/punjab/to-dodge-satellite-farmers-in-punjab-resort-to-stubble-burning-during-night **[S]**).
- CAQM (Sep 2026): assessing fire *intensity* and *burnt area*, "coming up with a protocol"
  (https://theprint.in/india/caqm-assessing-intensity-of-stubble-fires-instead-of-relying-on-fire-counts-in-haryana-punjab/3034123/ **[S]**).
- ISRO modified fire algorithm to be tested in rabi 2026 (Indian Express p7, 20 Feb 2026, via
  https://superkalam.com/current-affairs/20-02-2026/isro-s-improved-fire-detection-algo-ready-for-test-in-rabi-harvest-season-pg7-fdf07665-87e4-4901-9fe5-821828be2114 **[S]**).

**Live overpass clock (computed today, skyfield + CelesTrak TLEs, epoch 29-Oct-2026; Sangrur 30.25N 75.84E;
max-elevation >=25 deg MODIS / >=20 deg VIIRS as swath proxy) [V]**
| Sat | 29-Oct-2026 (IST) | 30-Oct-2026 (IST) |
|---|---|---|
| Terra (drifted early) | 09:59, 20:57 | 09:00, 19:58 |
| Aqua (drifted late) | 05:15, **16:13** | 04:16, 15:13 (low, 30 deg) |
| Suomi-NPP | 02:21, 13:38 | 02:03, 13:20 |
| NOAA-20 | 02:41, 13:58 | 02:22, 13:39 |
| NOAA-21 | 01:44, 13:01 | 01:25, 03:05, 12:43, 14:23 (all <31 deg) |
Matches CREAMS acquisitions (29 Oct: S-NPP 13:35, Aqua 16:09; 30 Oct: S-NPP 13:16). CREAMS uses S-NPP, Terra, Aqua, NOAA-20
(bulletin satellite table; NOAA-21 not listed). Aqua end-of-mission date: **[U]** (a search summary of NASA's 2025 Aqua
status PDFs said instrument shutdown 26 Aug 2026 - not opened; Aqua data were still in CREAMS on 29 Oct 2026). When Aqua
stops, the ~16:00 IST look disappears and official counts should drop further with no change on the ground.

## 3. Burnt-area remote sensing over Punjab

**Indices (Sentinel-2 bands: B2 blue, B3 green, B4 red, B8 NIR, B8A, B11 SWIR1, B12 SWIR2; reflectance 0-1)**
| Index | Formula | Thresholds used in Punjab/NW India work | Reported skill |
|---|---|---|---|
| NBR / dNBR | (B8-B12)/(B8+B12); dNBR = pre - post | dNBR >= 0.1 ("burnt"; Key-Benson classes 0.1/0.27/0.44/0.66) - Arbain & Imasu 2025; Landsat-8 NBR with 2-sigma threshold - Chawla & Sandhu 2020 | Chawla: OA 93-96%, kappa 0.817-0.90 (GCPs, Patiala/Ludhiana 2014-18) |
| NBR2 | (B11-B12)/(B11+B12) | used as RF feature | Walker 2022 |
| MIRBI | 10*B12 - 9.8*B11 + 2 | NRSC operational kharif 2023 burnt-area monitoring (MIRBI) **[U: NRSC page 404 today]** | strongest long-lasting NIR/SWIR separability (Walker) |
| BAIS2 | (1-sqrt(B6*B7*B8A/B4))*((B12-B8A)/sqrt(B12+B8A)+1) (Filipponi 2018, doi:10.3390/ECRS-2-05177) | **BAIS2 >= 0.78 AND Tasseled-Cap Brightness <= 0.34**, crop mask (Bhuvan crop layer x ESRI LULC), >=5 burnt px/field, OSM roads removed - CEEW | OA ~75%, TPR 59% (S2, Indian conditions) |
| Char index | (B+G+R) + 15*max(abs(B-G),abs(B-R),abs(R-G)) | PlanetScope/S2 visible-only | best in first days after burn, decays fast |
| BAI (PlanetScope) | 1/((0.1-R)^2+(0.06-NIR)^2) | 35.78 <= BAI <= 160.08 (CEEW T3) | OA 82%, TPR 54%, FPR 0.23% |

- **The core confusion is burnt vs harvested-and-tilled, not burnt vs green.** Walker et al. (ground truth: 441 burned,
  240 unburned-tilled plots): NIR/SWIR change after tilling is "almost the same" as after burning; burns drop NIR/SWIR more,
  "but this difference does not last more than a few days"; NDVI/SR are "not relevant". Best RF (S2+PlanetScope): 82% OA
  (91% burn, 63% no-burn), balanced 78%. Effective S2 revisit per plot in season: 6.8 d mean, 12 d largest gap (haze/cloud).
- **CEEW fusion:** some S2 strips revisit every 2-3 days (a 9,700 km2 strip in eastern Punjab); PlanetScope + S2 found 2-5x
  more burnt parcels than S2 alone (23 Oct-7 Nov 2024, 14 villages); combined OA 75%, ~54% of burnt pixels detected.
- **Estimates disagree ~2x - never present one number without method:**
| Source | Year | Punjab burnt | Haryana burnt | Method |
|---|---|---|---|---|
| CREAMS special bulletins [V] | 2018 / 2019 / 2020 | 15,145 / 11,083 / 16,589 km2 (49.4 / 37.4 / 55.7% of RS rice area) | 2,372 (2019), 2,163 (2020) km2 | multidate moderate (2019) / high-res (2020) imagery, hierarchical decision rules + ground truth |
| IITM, Ambulkar et al. JGR-A 2025, doi:10.1029/2024JD042198 [V abstract] | 10 Oct-30 Nov 2022 | 14,700 km2 | 8,300 km2 | S2 burnt area; 51% of crop area; S2 emissions ~3.6x VIIRS-based |
| iFOREST 2025 [V] | 2022 / 2025 | 31,447 / ~20,000 km2 | 8,812 (2025) | S2 dNBR at 100 m |
| Arbain & Imasu, *Sensors* 25:5588 (2025) [V full text] | 2022-24 | >34,000 km2 in a fortnight window (>68% of state area) | - | S2 dNBR >= 0.1, 15-day composites, ESA WorldCover cropland |
CREAMS's own RS rice area was 29,775 km2 (2020), so >30,000 km2 "burnt" is implausible -> dNBR-only methods count harvest.
URLs: CREAMS https://creams.iari.res.in/Creams_website_bulletin/Fire_bulletin_2020_special/03_RiceResidue_BurntStatistics_PunHar_2019.pdf
and .../04_RiceResidue_BurnStatistics_2020.pdf; Arbain full text https://www.ebi.ac.uk/europepmc/webservices/rest/PMC12431154/fullTextXML;
Chawla & Sandhu (Heliyon 2020) https://www.ebi.ac.uk/europepmc/webservices/rest/PMC6965717/fullTextXML;
Walker https://arxiv.org/pdf/2209.10148.
- **PRSC (Punjab Remote Sensing Centre):** issues daily satellite fire alerts to PPCB/districts via its "Crop Residue Burning
  Information and Management System" (Excel/tabular daily); 2019: 48,125 alerts, 40,557 visited, 17,832 "no fire";
  Malerkotla case: 6 of 23 PRSC fields confirmed (https://www.pressreader.com/india/hindustan-times-patiala/20191115/281599537326431 ,
  https://www.tribuneindia.com/news/punjab/only-6-out-of-23-stubble-fires-confirmed-in-punjabs-malerkotla **[S, search-level]**).
  No public PRSC burnt-area product or method document found **[U]**.

**BlindSpot recipe (from the above):** (1) crop mask; (2) per-field harvest date = first clear S2 date with NDVI drop;
(3) burn = BAIS2 >= 0.78 & TCB <= 0.34 (CEEW) corroborated by dMIRBI/dNBR2 vs last clear pre-image and a char-index check,
>= 5 px/field; (4) "unseen burn" = burnt field with no CREAMS/FIRMS point within ~750 m between last-clear and first-burnt
dates; (5) report precision honestly (~75-82% OA in the literature) and show the method next to every area number.

## 4. Smoke transport to Delhi and stubble-share methods

- **Transport time:** HYSPLIT (GDAS 1 deg, 120 h forward, 500-1000 m AGL): Punjab/Haryana parcels reach Delhi's mixing layer
  "within 36-48 h"; "60% of trajectories originating over active-burn zones intersected Delhi within 36 h"; Punjab share of
  regional fire counts 90% (2020) -> 40% (2024). *Air Qual Atmos Health* 2026, doi:10.1007/s11869-026-01885-x,
  https://www.ebi.ac.uk/europepmc/webservices/rest/PMC12823738/fullTextXML **[V]**. 32-sensor CUPI-G network (2022): two Punjab
  plumes reached Delhi "with a delay of 1 to 3 days"; ground peaks at midnight/early morning; rural hourly PM2.5 often
  >1000 ug/m3 (Sci Rep, doi:10.1038/s41598-023-39471-1, https://researchmatters.in/news/new-sensor-network-tracks-how-toxic-smoke-crop-fires-travels-delhi **[S]**).
- **Boundary layer:** post-monsoon 2022 mean BLH 0.53 +/- 0.45 km (ceilometer, EGU23-5925
  https://meetingorganizer.copernicus.org/EGU23/EGU23-5925.html **[S]**); lowest seasonal mean daytime BLH ~695 m in winter
  (Raman lidar, Nakoudi et al. AMT 12:2595, 2019 **[S]**). Evening burning matters: "A shift to late evening CRB leads to very
  strong nighttime build-up of PM2.5 ... when the boundary layer is shallow"; low 2024 impact may reflect evening burning
  "not captured by existing emission inventories" (Biswal, Takigawa, Mangaraj, ... Vikas Singh, *Environ. Sci.: Atmos.* 2025,
  doi:10.1039/d5ea00052a; quoted text seen via search snippet, RSC returns 403 **[S]**).
- **IITM DSS (operational, MoES):** WRF-Chem v3.9.1, 10 km, passive tagged tracers; fire emissions = FINN daily climatology
  (2002-18) switched on where near-real-time S-NPP VIIRS counts (confidence >70%) exist for Day 1, VIIRS climatology (2011-18)
  for Days 2-5; post-monsoon 2021 seasonal mean stubble share 7.3-8%, daily max 37% (authors call it a lower bound).
  Govardhan et al., GMD 17:2617 (2024), doi:10.5194/gmd-17-2617-2024 **[V]**. Live page today: 2026 season-to-date max 5.21%
  (25 Oct), 1.75% on 30 Oct; forecast 19.16 / 33.49 / 13.40% for 31 Oct-2 Nov (climatology-driven). **[V]** 2025: peak
  22.47% on 12 Nov; November mean 7% vs 20% in 2024; 2024 peak 38% (press: https://www.business-standard.com/india-news/delhi-air-pollution-aqi-severe-winter-smog-2025-125111600064_1.html , HT via Magzter **[S, search-level]**).
- **SAFAR (IITM):** Beig et al., STOTEN 709:136126 (2020) - satellite + field incidence inventory, SAFAR CTM; biomass-burning
  share up to 58% (2018) **[S abstract]**. Reported peaks: 44% (1 Nov 2019), 42% (5 Nov 2020), 48% (7 Nov 2021)
  (https://www.outlookindia.com/national/farm-fires-contributing-to-nearly-40-of-delhi-s-pollution-study-reveals-news-234449 **[S, search-level]**).
  **https://safar.tropmet.res.in/ was unreachable today (TCP connect timeout 21 s) [U].**
- **The share is contested (show ranges, not one number):** Punjab CRB ~14% of Delhi-NCR PM2.5, Oct-Nov 2022 ("very weak
  coupling") - Mangaraj et al., npj Clim Atmos Sci 2025, doi:10.1038/s41612-025-00901-8 **[V abstract]**; vs CRB share of
  surface PM2.5 of 34% Punjab / 33% Haryana / 30% Delhi-NCR (1-15 Nov 2022 means), 55% in Delhi-NCR during one plume, with
  GFAS prior emissions "underestimated by approximately 8.6 times" - Kajino et al., ACP 25:7137 (2025),
  doi:10.5194/acp-25-7137-2025 **[V]**; NASA's Pawan Gupta: 40-70% on a given day, 20-30% seasonal, <10%
  annual (NASA EO page **[V]**). Same RIHN/Aakash network, different methods -> BlindSpot's angle: emission *timing* + burnt
  *area* are the missing inputs.

## 5. Existing tools and the honest gap

| Tool | What it does | Access (tested today) | Gap |
|---|---|---|---|
| CREAMS-IARI bulletins + GeoKrishi | Daily official fire events (CAQM protocol), district tables, PDFs | Keyless WFS + PDFs **[V]** | Polar-orbit snapshots only; no burnt area published (iFOREST asks for it) |
| PRSC / HARSAC / PPCB | State alerts -> field verification, FIRs | No public API found **[U]** | Same satellite inputs; verification lag |
| IITM DSS / EWS | Stubble % of Delhi PM2.5, 4-day forecast | Keyless HTML **[V]** | Driven by previous-evening VIIRS + climatology |
| SAFAR | Historic stubble share | Unreachable **[U]** | - |
| NASA FIRMS | MODIS/VIIRS NRT points | (other research doc) | Same overpass times |
| Jethva GK2A / SAC SEVIRI / iFOREST | Diurnal timing (research) | Zenodo dataset keyless **[V]** | 4-6 km pixels; not field-level; not operational |
| CEEW (Oct 2026 brief) | S2 + commercial PlanetScope fusion | Report only | Needs paid imagery; not live |
| Unnat Kisan / i-Khet apps | CRM machine booking; 85,000+ machines mapped (Oct 2025) | App only, no open API found (https://www.babushahi.com/view-news.php?id=211615 **[S, search-level]**) | ~15% of surveyed CHCs had heard of such apps (CEEW **[S, search-level]**) |
| Startups | Blue Sky Analytics (ESA award 2019), nurture.farm pilots (claims) | **[S]**, status unknown | Not open / not current |
**What none does (our USP, stated carefully):** a live, field-level join of (a) official fire points, (b) Sentinel-2 burn
scars, (c) geostationary diurnal timing and (d) the live overpass clock -> a list of *burnt-but-never-counted* fields; plus
next-to-burn prediction for harvested fields, time-correct smoke attribution to cities, and an agent that cites every source.
Burn-scar mapping itself is NOT new (CEEW, iFOREST, IITM, NRSC do it); the novelty is the operational fusion and attribution.

## 6. Interventions and routing datasets

- **CRM scheme** (Central Sector Scheme, since 2018-19): Rs 4,266.47 crore to Punjab, Haryana, UP, MP and ICAR; >3.54 lakh
  machines; >43,500 CHCs; 2026-27 provision Rs 544.15 crore (Rs 272.07 crore released); 2026 targets >46,000 machines, 910
  CHCs, 141 stubble supply-chain projects (PIB 16-Jun-2026 **[V]**). 2018 guidelines: 50% subsidy (individuals), 80% of
  in-situ machinery cost for CHC projects (Rs 10-75 lakh); machines: Super SMS, Happy Seeder, straw chopper/mulcher, shrub
  master, reversible MB plough, rotary slasher, zero-till drill, rotavator (https://agrimachinery.nic.in/Files/Guidelines/CRM.pdf **[V]**;
  later lists add Super/Smart Seeder, balers - see CRMGuideline2020-21.pdf **[U, not opened]**).
- **Behavioural evidence:** PES with partial up-front payment cut burning by 10 percentage points (satellite-corroborated);
  standard pay-after-verification PES "has no effect" - Jack, Jayachandran, Kala, Pande, AER: Insights 7(1):39-55 (2025),
  doi:10.1257/aeri.20230431 **[V abstract]**.
- **PUSA bio-decomposer (IARI):** evidence is weak - Delhi-govt WAPCOS audit (79 farmers, 15 villages, Delhi only, 2021):
  "ninety per cent of the farmers claimed" stubble turned to manure in 15-20 days (farmer claims, not measurements)
  (https://www.deccanherald.com/india/stubble-burning-audit-by-central-agency-shows-pusa-bio-decomposer-highly-effective-says-kejriwal-1029843.html **[S, search-level]**).
  Do not present it as proven.
- **Ex-situ:** Verbio CBG plant, Lehragaga (Sangrur): designed 33 TPD CBG from ~300 t/day straw, ~1 lakh t straw/yr
  (https://www.tribuneindia.com/news/punjab/indias-biggest-bio-energy-plant-begins-ops-tomorrow-in-lehragaga-441962 **[S, search-level]**).
  Co-firing lag: 4 Punjab TPPs co-fired 3.12 lakh MT pellets vs 11.83 lakh MT 2025-26 target (to Sep 2025); Punjab 3,284 fires
  (15 Sep-6 Nov 2025) vs 5,041 a year earlier (PIB 08-Nov-2025 https://www.pib.gov.in/PressReleasePage.aspx?PRID=2187847 **[V]**).
- **Datasets for routing (CHCs / biomass plants) - none is open+keyless today:** AIKOSH "Crop Residue Management (CRM)
  Dataset" (district-wise machinery, Farm Machinery Banks; https://aikosh.indiaai.gov.in/home/datasets/details/m_t_dataset.html):
  page 200 but API `aikosha-api.indiaai.gov.in/.../datasets/detail/m_t_dataset` -> **403**, file link
  `krishimapper.dac.gov.in/appdevapi/api/AIKosh/GetAIKosh_DownloadURL?datasetIdentifier=6&fileIdentifier=FILE03` -> **401**
  (login needed). Punjab "Target for CRM Scheme 2024-25" PDF -> **404**. OSM Overpass (power=plant + plant:source=biomass,
  Punjab) -> **502**, fallback instance timed out **[U]**. Unnat Kisan has no public API. Usable now: CAQM/PIB-named plants
  (Lehragaga CBG, Rajpura/Lehra Mohabbat/Ropar/Mansa TPPs) geocoded at runtime; retry Overpass later; register on AIKOSH.

## 7. Verified endpoints (keyless; called 2026-10-30)

| Name | URL template | Latency | Notes |
|---|---|---|---|
| CREAMS WFS daily paddy fires | `https://creams.iari.res.in/geokrishi/geoserver/wfs?service=WFS&version=2.0.0&request=GetFeature&typeNames=geokrishi_v:{D}_{month}_{YYYY}_rice&outputFormat=application/json&srsName=EPSG:4326` | 0.28-0.47 s | D no leading zero, month lowercase English; fields s_no_, district, block, satellite, latitude, longitude, acq_date (float DDMMYYYY), acq_time "HH:MM:SS" IST, day_flag, fire_power |
| CREAMS WFS all-India agri fires | same, `..._{YYYY}_al_ind` | 0.27 s | different schema: acq_date float **YYYYMMDD**, acq_time UTC int hhmm, time_ist, instrument, frp, state, district, sa_lulc250 |
| CREAMS WFS capabilities | `...wfs?service=WFS&version=2.0.0&request=GetCapabilities` | 0.57 s (880 KB) | 1,581 feature types (rice/wheat/al_ind per day, soils, GeoServer demo layers); WMS caps 15 s / 2.6 MB - avoid |
| CREAMS bulletin PDF | `https://creams.iari.res.in/Creams_website_bulletin/Fire_bulletin_{YYYY}/Fire_bulletin_{YYYY}r/{NN}.RiceResidueFireBulletin_{DD}{Mon}_{YYYY}_ICAR.pdf` | 0.46-0.89 s (2.9-3.8 MB) | NN=01 on 15 Sep; discover real links via `https://creams.iari.res.in/index.php?rest_route=/wp/v2/pages/1123&_fields=content` (1.8 s; 2018-2022 dirs differ) |
| IITM DSS | `https://ews.tropmet.res.in/dss/index.php` | 1.4-1.8 s | Chart.js arrays in HTML (`label: 'Stubble Burning'`); 2021 inventory disclaimer |
| IITM EWS fire counts | `https://ews.tropmet.res.in/ob_fire_count.php` | 0.58 s | MODIS/VIIRS yearly series + "Fire CO%" hourly arrays (not parsed yet) |
| Zenodo GK2A | `https://zenodo.org/api/records/20084790` ; `.../files/GK2-AMI_FireHotspotsData_{YYYY}_10-11_{Punjab|Haryana}_India_v1.txt/content` | 0.74 s ; 2.2-4.8 s (0.4-4.5 MB) | 12 header lines; whitespace-separated; CC BY 4.0, cite Jethva 2026 |
| CelesTrak GP | `https://celestrak.org/NORAD/elements/gp.php?CATNR={25994|27424|37849|43013|54234}&FORMAT=TLE` | 1.3-2.6 s | cache >= 6 h; feed to skyfield for the overpass clock |
| PIB release | `https://www.pib.gov.in/PressReleasePage.aspx?PRID={id}` | 0.46-0.97 s | needs browser User-Agent (WebFetch got 403) |
| Literature (agent) | `https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=title,abstract,authors,year,venue` ; `https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML` | 0.6-2.3 s | S2 returns 429 above ~1 req/s; Crossref returned 429 once; MDPI/RSC/PMC HTML block bots |

Licensing: CREAMS disclaimer - research products under CAQM protocol, not "used for any legal purpose"; attribute "CREAMS
Lab, ICAR-IARI". DSS: attribute IITM/MoES. GK2A: CC BY 4.0. CEEW brief: CC BY-NC 4.0. Current Science/iFOREST: cite, no reuse
of figures. Gotchas: same-day CREAMS layer is partial (30 Oct had only the 13:16 pass) - re-pull D-1 next morning; missing
layer -> HTTP 400 `ows:ExceptionReport` (XML, not JSON); bulletins say "six study States" but list seven; GK2A covers only
12-18 IST; DSS Day 2-4 forecasts use 2011-18 climatology so they overshoot in low-burn years.

## 8. Tested snippet (ran 2026-10-30 with `uv run python`; output below)

```python
import io, re, datetime as dt, httpx, pandas as pd
UA = {"User-Agent": "BlindSpot/0.1"}
def creams_events(day: dt.date, kind="rice") -> pd.DataFrame:
    layer = f"geokrishi_v:{day.day}_{day.strftime('%B').lower()}_{day.year}_{kind}"
    r = httpx.get("https://creams.iari.res.in/geokrishi/geoserver/wfs", headers=UA, timeout=60, params={
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
        "outputFormat": "application/json", "srsName": "EPSG:4326"})
    r.raise_for_status()                      # unpublished day -> 400 + XML exception
    df = pd.DataFrame([f["properties"] for f in r.json()["features"]])
    if not df.empty:                          # rice layers: acq_date float DDMMYYYY
        df["acq_date"] = pd.to_datetime(df.acq_date.astype("int64").astype(str).str.zfill(8), format="%d%m%Y")
        df["hour_ist"] = df.acq_time.str[:2].astype(int)
    return df
def dss_stubble_share() -> dict:
    html = httpx.get("https://ews.tropmet.res.in/dss/index.php", headers=UA, timeout=40).text
    out = {}
    for m in re.finditer(r"labels:\s*\[([^\]]*)\]\s*,\s*datasets:\s*\[(.*?)\]\s*\}\s*;", html, re.S):
        labels = [s.strip().strip("\"'")[:10] for s in m.group(1).split(",") if s.strip()]
        for ds in re.finditer(r"label:\s*'([^']+)'.*?data:\s*\[([^\]]*)\]", m.group(2), re.S):
            if ds.group(1).strip().lower() == "stubble burning":
                vals = [float(v) for v in ds.group(2).split(",") if v.strip()]
                out["forecast" if len(labels) <= 5 else "season_daily"] = list(zip(labels, vals))
    return out
def gk2a_hourly_share(year: int, state="Punjab") -> pd.Series:
    url = f"https://zenodo.org/api/records/20084790/files/GK2-AMI_FireHotspotsData_{year}_10-11_{state}_India_v1.txt/content"
    txt = httpx.get(url, headers=UA, timeout=120, follow_redirects=True).text
    df = pd.read_csv(io.StringIO(txt), sep=r"\s+", skiprows=12,
                     names=["y","m","d","hour","mi","lon","lat","btd","bt38","bt112","conf"])
    df = df[df.conf >= 2]
    return (df.groupby("hour").size() / len(df) * 100).round(1)
```
Output (this exact block, run verbatim): `CREAMS 29-Oct-2026: 223 events in 0.44s` (AQUA@16h 107, S-NPP@13h 112,
S-NPP@02h 4); 31-Oct layer -> `HTTPStatusError 400` (not yet published); `DSS 1.65s last=('2026-10-30', 1.749) forecast
19.16/33.49/13.40`; `GK2A Punjab 2020 1.85s: {12:0.5,13:8.2,14:21.4,15:29.7,16:19.3,17:7.9,18:13.0}` (2025 run: 16:22.7, 17:61.1).

## 9. Not verified / blocked (do not claim in the pitch without re-checking)
- Exact harvest->burn lag in days (no study found). Aqua end-of-mission date. NRSC MIRBI burnt-area pages (404 today) and
  whether NRSC data are public. PRSC burnt-area method. SAFAR site (unreachable). EUMETSAT SEVIRI FIR / MOSDAC INSAT-3DS fire
  product access (not tested here). Biswal et al. 2025 numbers (18/16/9% for 2022/23/24) seen only in a search snippet.
  2025 DSS peaks (22.47%, 38% in 2024) are press-reported. Survey percentages on why farmers burn are second-hand.
  CHC/biomass-plant locations: no keyless open dataset reachable today (AIKOSH 403/401, Overpass 502).
