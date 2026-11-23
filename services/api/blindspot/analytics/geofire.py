"""Fire pixels from GK-2A infrared scans, checked against polar satellites, and the diurnal record.

Pure functions over numpy arrays: no database, no network. jobs/geostationary.py feeds them
scans and stores what they return.

The fire test (detect_fires) follows the contextual approach of the MODIS active-fire algorithm
(Giglio et al. 2016, Remote Sensing of Environment 178:31-41) and of SEVIRI FRP-PIXEL (Roberts
and Wooster 2008, IEEE TGRS 46:1200-1218), adapted to GK-2A's ~12 km2 pixels over Punjab
(view zenith ~66 deg):

1. A fire inside a pixel raises its 3.8 um brightness temperature (BT38) far more than its 11 um
   one (BT11), because 3.8 um sits near the peak of Planck emission at flame temperatures. So the
   signal is the difference BTD = BT38 - BT11, compared with the pixel's neighbours.
2. Clouds are screened with the 11 um channel (cold, or colder than the clear land nearby).
3. A pixel is a fire when its BTD and BT38 stand far above a robust background (median and MAD of
   clear, non-candidate neighbours), its BT11 is not cloud-cooled, and its 3.8 um excess equals at
   least a fixed fire radiative power (MIR radiance method, Wooster et al. 2003, Remote Sensing of
   Environment 86:83-107). That last floor means the same fire strength at noon and at night, so
   the diurnal curve is not an artefact of thresholds that change with the sun.

What GK-2A can see (measured against VIIRS and MODIS on 28-30 Oct 2026): background noise in BTD
is ~0.2 K at night, ~0.3-0.6 K in the afternoon and 0.4-1.4 K at midday (haze and cloud raise it).
Pixels where VIIRS saw fire at midday show only ~0.5-1.7 K of excess, so most midday crop fires
are below GK-2A's noise; afternoon and evening fires show 1.5-4 K and are detectable. Counts are
therefore an activity index in pixels (~12 km2 each), never a number of fires.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from blindspot.analytics.fires_stats import STATIC_MIN_NIGHTS, STATIC_RADIUS_KM


def _ndimage() -> Any:
    """scipy.ndimage, imported on first use. Only the jobs run this module's image steps; the web
    API imports the module for its constants and summaries and must start without scipy
    (serverless deployments do not ship it)."""
    from scipy import ndimage

    return ndimage


# Stored with every slot, pixel and validation row; the API reads only the current version.
# gk2a-ctx-3: pixels flagged as persistent heat sources (STATIC_RULE below) are
# left out of fire and of every background.
ALGORITHM_VERSION = "gk2a-ctx-3"

# --- sun geometry ------------------------------------------------------------------------------------


def solar_position(
    lon: np.ndarray | float, lat: np.ndarray | float, when: datetime
) -> tuple[np.ndarray, np.ndarray]:
    """Solar zenith and azimuth (degrees, azimuth clockwise from north) at `when` (UTC).

    NOAA Global Monitoring Laboratory "General Solar Position Calculations" (fractional-year
    series for the equation of time and declination); accurate to a few tenths of a degree,
    plenty for day/night regimes.
    """
    when = when.astimezone(UTC)
    lon_a, lat_a = np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
    doy = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * np.pi / 365 * (doy - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
        - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g)
    )  # fmt: skip
    decl = (
        0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
        + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g)
    )  # fmt: skip
    true_solar_min = hours * 60 + eqtime + 4 * lon_a
    hour_angle = np.radians(true_solar_min / 4 - 180)
    phi = np.radians(lat_a)
    cos_z = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(hour_angle)
    zenith = np.degrees(np.arccos(np.clip(cos_z, -1, 1)))
    sin_z = np.sqrt(np.clip(1 - cos_z**2, 1e-12, None))
    cos_az = (np.sin(decl) - np.sin(phi) * cos_z) / (np.cos(phi) * sin_z)
    azimuth = np.degrees(np.arccos(np.clip(cos_az, -1, 1)))
    azimuth = np.where(hour_angle > 0, 360 - azimuth, azimuth)  # afternoon: sun in the west
    return zenith, azimuth


def satellite_direction(
    lon: np.ndarray, lat: np.ndarray, sub_lon_deg: float, sat_distance_m: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Unit vector (east, north, up) from each ground point to a geostationary satellite.
    A spherical Earth (radius 6371 km) is accurate enough for a direction."""
    r_earth = 6.371e6
    lo, la = np.radians(lon), np.radians(lat)
    p = r_earth * np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    sl = np.radians(sub_lon_deg)
    s = np.array([sat_distance_m * np.cos(sl), sat_distance_m * np.sin(sl), 0.0])
    v = s.reshape(3, *([1] * np.ndim(lon))) - p
    v = v / np.linalg.norm(v, axis=0)
    east = -np.sin(lo) * v[0] + np.cos(lo) * v[1]
    north = -np.sin(la) * np.cos(lo) * v[0] - np.sin(la) * np.sin(lo) * v[1] + np.cos(la) * v[2]
    up = np.cos(la) * np.cos(lo) * v[0] + np.cos(la) * np.sin(lo) * v[1] + np.sin(la) * v[2]
    return east, north, up


def sun_satellite_angle(
    sza: np.ndarray, saz: np.ndarray, sat_dir: tuple[np.ndarray, np.ndarray, np.ndarray]
) -> np.ndarray:
    """Angle (degrees) between the directions to the sun and to the satellite. Near 0 the
    satellite looks along the sunbeam (backscatter: bright 3.8 um reflection). Sun glint (the
    specular direction) never comes closer than ~42 deg over Punjab, so it needs no test."""
    z, a = np.radians(sza), np.radians(saz)
    sun = (np.sin(z) * np.sin(a), np.sin(z) * np.cos(a), np.cos(z))
    dot = sun[0] * sat_dir[0] + sun[1] * sat_dir[1] + sun[2] * sat_dir[2]
    return np.degrees(np.arccos(np.clip(dot, -1.0, 1.0)))


# --- fire radiative power (MIR radiance method) --------------------------------------------------

# Physical constants (CODATA 2018). Over 650-1350 K the 3.8 um Planck radiance is close to
# a * T^4, so a pixel's FRP is A * sigma / a * (L_pixel - L_background) whatever the fire's size
# and temperature (Wooster et al. 2003, eq. 6).
STEFAN_BOLTZMANN = 5.670374419e-8  # W m-2 K-4
PLANCK_C1 = 1.191042972e8  # 2 h c^2, W um4 m-2 sr-1
PLANCK_C2 = 1.438776877e4  # h c / k, um K
SW038_UM = 3.83  # GK-2A SW038 central wavelength (as in sources/gk2a.py)


def mir_radiance_constant(wavelength_um: float = SW038_UM) -> float:
    """Least-squares fit of Planck(wavelength, T) = a * T^4 over 650-1350 K, as Wooster et al.
    2003 did. Gives 3.12e-9 W m-2 sr-1 um-1 K-4 at 3.83 um; the same fit gives 2.94e-9 at 3.96 um
    and 3.02e-9 at 3.9 um, close to the published 3.0e-9 (MODIS) and 3.06e-9 (SEVIRI)."""
    t = np.linspace(650.0, 1350.0, 701)
    planck = PLANCK_C1 / wavelength_um**5 / np.expm1(PLANCK_C2 / (wavelength_um * t))
    return float(np.sum(planck * t**4) / np.sum(t**8))


MIR_A = mir_radiance_constant()


def planck_radiance(bt: np.ndarray, wavelength_um: float = SW038_UM) -> np.ndarray:
    """Spectral radiance (W m-2 sr-1 um-1) of a black body at brightness temperature `bt` (K)."""
    with np.errstate(over="ignore", invalid="ignore"):
        return PLANCK_C1 / wavelength_um**5 / np.expm1(PLANCK_C2 / (wavelength_um * bt))


def frp_mw(bg_bt38: np.ndarray, fire_excess_k: np.ndarray, area_km2: np.ndarray) -> np.ndarray:
    """Fire radiative power (MW) of a pixel whose 3.8 um brightness temperature exceeds the
    background `bg_bt38` by `fire_excess_k` because of fire.

    We pass the BTD excess as the fire's part of the 3.8 um excess: a sun-heated field warms both
    channels alike, so it cancels in BTD, while a fire raises 3.8 um far more than 11 um (using BTD
    drops the fire's small 11 um rise, so the estimate errs low). Top of atmosphere: not corrected
    for 3.8 um absorption along GK-2A's long slant path, so a lower bound. We use it as a detection
    floor that means the same fire strength by day and night, not as a calibrated FRP product.
    """
    excess = planck_radiance(bg_bt38 + fire_excess_k) - planck_radiance(bg_bt38)
    return area_km2 * 1e6 * STEFAN_BOLTZMANN / MIR_A * excess / 1e6


# --- the fire test ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FireTestParams:
    """Thresholds of the fire test (kelvin unless stated). These are the values the job runs;
    the validation stored with every run says how well they do."""

    # Cloud screen (thermal only; see cloud_mask for why there is no reflectance test)
    cloud_bt11_abs: float = 265.0  # colder than this at 11 um: cloud (MODIS C6 uses 265 K at 12 um)
    cloud_bt11_drop: float = 5.0  # this much below the local warm reference: cloud
    warm_ref_size: int = 15  # window (pixels) of that warm reference (90th percentile of BT11)
    # In daylight also compare with a regional warm reference (41 px ~ 180 x 150 km): inside a
    # wide cloud deck the local reference is itself cloud, and sunlit cloud mimics fire at 3.8 um.
    cloud_bt11_drop_regional_day: float = 4.0
    warm_ref_regional_size: int = 41
    cloud_buffer_px: int = 2  # daytime fire pixels this close to a cloud are dropped
    # Backscatter: GK-2A sees Punjab from the east-south-east, so on autumn mornings (~07:15-10:00
    # IST) the sun is nearly behind the satellite and sunlight reflected at 3.8 um peaks (the
    # backscatter "hot spot"). Daytime fire pixels where the sun-satellite angle is below this
    # are not trusted.
    backscatter_min_deg: float = 30.0
    # Day / night by solar zenith angle; thresholds that differ are interpolated in between
    day_sza: float = 80.0
    night_sza: float = 90.0
    # Candidates: BTD above the local median of clear pixels by this much (a cheap first pass)
    candidate_margin: float = 0.5
    # Background: clear, non-candidate neighbours, the window grown until there are enough
    bg_sizes: tuple[int, ...] = (7, 11, 15, 21)
    bg_min_valid: int = 10
    bg_min_fraction: float = 0.25
    sigma_floor: float = 0.2  # sigma (1.4826 x MAD) never counts as less than this (~2 x NEdT)
    # Contextual tests
    k_btd: float = 3.5  # BTD excess > k * sigma(BTD)
    k_bt38: float = 3.0  # BT38 excess > k * sigma(BT38)
    max_bt11_deficit_day: float = 4.0  # BT11 at most this far below its background (MODIS test 4)
    max_bt11_deficit_night: float = 2.0  # thin cloud cools 11 um; a fire never does
    min_frp_mw: float = 12.0  # the 3.8 um excess must equal at least this fire power
    # Absolute test: heat no background can explain
    abs_bt38: float = 330.0
    abs_btd: float = 20.0
    # Confidence "high": at least this fire power, or the absolute test
    high_frp_mw: float = 40.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_PARAMS = FireTestParams()


@dataclass
class FireTestResult:
    """Per-pixel output of detect_fires (all arrays have the scan window's shape)."""

    fire: np.ndarray  # bool
    confidence: np.ndarray  # int8: 0 none, 1 nominal, 2 high
    valid: np.ndarray  # bool: both channels good
    cloud: np.ndarray  # bool
    clear: np.ndarray  # bool: valid and not cloud
    btd: np.ndarray  # BT38 - BT11
    bg_btd: np.ndarray  # background median BTD (NaN where not computed)
    bg_btd_sigma: np.ndarray
    bg_bt38: np.ndarray
    bg_bt38_sigma: np.ndarray
    bg_bt11: np.ndarray
    bg_count: np.ndarray  # background pixels used
    excess: np.ndarray  # BTD - background BTD (K), NaN where not computed
    frp_mw: np.ndarray  # top-of-atmosphere FRP estimate (MW), NaN where not computed
    absolute: np.ndarray  # bool: passed the absolute test
    day_weight: np.ndarray  # 1 day ... 0 night (by solar zenith)
    # Excluded pixels (persistent heat sources) that passed every test: never fire, but kept
    # so the job can store them apart and see that a flagged source is still burning.
    excluded_fire: np.ndarray | None = None
    stats: dict[str, Any] = field(default_factory=dict)


def _day_weight(sza: np.ndarray, p: FireTestParams) -> np.ndarray:
    """1 in daylight (SZA < day_sza), 0 at night (SZA > night_sza), linear in between."""
    return np.clip((p.night_sza - sza) / (p.night_sza - p.day_sza), 0.0, 1.0)


def cloud_mask(
    bt11: np.ndarray, day_weight: np.ndarray | None = None, p: FireTestParams = DEFAULT_PARAMS
) -> np.ndarray:
    """True where a pixel is cloudy: colder than `cloud_bt11_abs` at 11 um, or more than
    `cloud_bt11_drop` below the local warm reference (90th percentile of BT11 nearby, i.e. the
    clear land around it), or, in daylight, more than `cloud_bt11_drop_regional_day` below a
    regional warm reference.

    Why no 1.6 um reflectance test: measured on 28-30 Oct 2026 scans, at GK-2A's 66 deg view of
    Punjab the (mostly ice) clouds' 1.6 um albedo, 0.20-0.30, overlaps the land's, 0.15-0.23, and
    dividing by cos(solar zenith) flags most of the region after 16:00 IST. Daytime detections that
    VIIRS did not confirm sat 5 K (median) below the regional warm reference with BTD near 19 K;
    confirmed ones sat within 2 K of it with BTD near 8 K. The thermal tests catch the clouds that
    cause false alarms.
    """
    finite = np.isfinite(bt11)
    filled = np.where(finite, bt11, np.nanmin(bt11) if finite.any() else 0.0)
    warm_ref = _ndimage().percentile_filter(filled, 90, size=p.warm_ref_size, mode="nearest")
    cloud = finite & ((bt11 < p.cloud_bt11_abs) | (bt11 < warm_ref - p.cloud_bt11_drop))
    if day_weight is not None and bool((day_weight > 0.5).any()):
        regional = _ndimage().percentile_filter(
            filled, 90, size=p.warm_ref_regional_size, mode="nearest"
        )
        day = day_weight > 0.5
        cloud |= finite & day & (bt11 < regional - p.cloud_bt11_drop_regional_day)
    return cloud


def _background(
    values: Sequence[np.ndarray],
    usable: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    size: int,
) -> tuple[list[np.ndarray], list[np.ndarray], np.ndarray]:
    """Robust background (median, sigma = 1.4826 x MAD) of each array in `values` around the
    given pixels, from `usable` neighbours in a size x size window (centre excluded)."""
    half = size // 2
    pad = [(half, half), (half, half)]
    use = np.pad(usable, pad, constant_values=False)
    use_w = sliding_window_view(use, (size, size))[rows, cols].copy()
    use_w[:, half, half] = False
    count = use_w.reshape(len(rows), -1).sum(axis=1)
    medians, sigmas = [], []
    for v in values:
        vw = sliding_window_view(np.pad(v, pad, constant_values=np.nan), (size, size))[rows, cols]
        vw = np.where(use_w, vw, np.nan).reshape(len(rows), -1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # an all-NaN row gives NaN, as wanted
            med = np.nanmedian(vw, axis=1)
            mad = np.nanmedian(np.abs(vw - med[:, None]), axis=1)
        medians.append(med)
        sigmas.append(1.4826 * mad)
    return medians, sigmas, count


def detect_fires(
    bt38: np.ndarray,
    bt11: np.ndarray,
    sza: np.ndarray,
    area_km2: np.ndarray,
    backscatter_deg: np.ndarray | None = None,
    exclude: np.ndarray | None = None,
    p: FireTestParams = DEFAULT_PARAMS,
) -> FireTestResult:
    """Run the contextual fire test on one scan window.

    `sza` is the solar zenith angle per pixel (degrees), `area_km2` each pixel's footprint area,
    `backscatter_deg` the angle between the sun and the satellite seen from each pixel, and
    `exclude` marks pixels never to call fire (persistent industrial heat sources). An excluded
    pixel is also never part of a neighbour's background (a hot plant would raise it and hide a
    fire next door); it is still tested, and `excluded_fire` says where it passed.

    A pixel is a fire when it is clear and either
      - absolute: BT38 > abs_bt38 and BTD > abs_btd, or
      - contextual: BTD excess > k_btd x sigma(BTD), BT38 excess > k_bt38 x sigma(BT38), BT11 no
        more than the allowed deficit below its background, and a 3.8 um excess worth at least
        min_frp_mw of fire power;
    and, in daylight, it is at least `cloud_buffer_px` from any cloud (sunlit cloud edges mimic
    fire at 3.8 um) and not in backscatter geometry.
    """
    shape = bt38.shape
    valid = np.isfinite(bt38) & np.isfinite(bt11)
    w = _day_weight(sza, p)
    cloud = valid & cloud_mask(bt11, w, p)
    clear = valid & ~cloud
    btd = bt38 - bt11

    absolute = clear & (bt38 > p.abs_bt38) & (btd > p.abs_btd)

    # Candidates: BTD above the local median of clear pixels. The median filter cannot skip NaNs,
    # so non-clear pixels take the scene's clear median for this first pass only.
    clear_median = float(np.nanmedian(btd[clear])) if clear.any() else np.nan
    filled = np.where(clear, btd, clear_median if np.isfinite(clear_median) else 0.0)
    local_median = _ndimage().median_filter(filled, size=p.bg_sizes[1])
    candidate = (clear & (btd - local_median > p.candidate_margin)) | absolute

    # Background of each candidate from clear, non-candidate neighbours, growing the window until
    # it holds enough of them (MODIS C6 grows 3x3 -> 21x21 the same way). Excluded pixels never
    # count as background, whether or not they are candidates now.
    usable = clear & ~candidate
    if exclude is not None:
        usable &= ~exclude
    nan = np.full(shape, np.nan)
    bg_btd, bg_btd_s, bg_38, bg_38_s, bg_11 = (nan.copy() for _ in range(5))
    bg_count = np.zeros(shape, dtype=np.int32)
    todo_r, todo_c = np.nonzero(candidate)
    for size in p.bg_sizes:
        if todo_r.size == 0:
            break
        medians, sigmas, count = _background([btd, bt38, bt11], usable, todo_r, todo_c, size)
        enough = (count >= p.bg_min_valid) & (count >= p.bg_min_fraction * (size * size - 1))
        r, c = todo_r[enough], todo_c[enough]
        bg_btd[r, c], bg_38[r, c], bg_11[r, c] = (m[enough] for m in medians)
        bg_btd_s[r, c], bg_38_s[r, c] = sigmas[0][enough], sigmas[1][enough]
        bg_count[r, c] = count[enough]
        todo_r, todo_c = todo_r[~enough], todo_c[~enough]

    excess = btd - bg_btd
    frp = frp_mw(bg_38, excess, area_km2)
    deficit = p.max_bt11_deficit_night + (p.max_bt11_deficit_day - p.max_bt11_deficit_night) * w
    with np.errstate(invalid="ignore"):
        contextual = (
            candidate
            & np.isfinite(bg_btd)
            & (excess > p.k_btd * np.maximum(bg_btd_s, p.sigma_floor))
            & (bt38 - bg_38 > p.k_bt38 * np.maximum(bg_38_s, p.sigma_floor))
            & (bt11 - bg_11 > -deficit)
            & (frp >= p.min_frp_mw)
        )
    fire = absolute | contextual
    if p.cloud_buffer_px > 0:
        size = 2 * p.cloud_buffer_px + 1
        near_cloud = _ndimage().maximum_filter(cloud.astype(np.uint8), size=size) > 0
        fire &= ~(near_cloud & (w > 0.5) & ~absolute)
    backscatter = np.zeros(shape, dtype=bool)
    if backscatter_deg is not None:
        backscatter = (sza < 90.0) & (backscatter_deg < p.backscatter_min_deg)
        fire &= ~(backscatter & ~absolute)
    excluded_fire = np.zeros(shape, dtype=bool)
    if exclude is not None:
        excluded_fire = fire & exclude
        fire &= ~exclude

    passed = fire | excluded_fire  # excluded pixels that passed keep their confidence, for audit
    with np.errstate(invalid="ignore"):
        strong = passed & (absolute | (frp >= p.high_frp_mw))
    confidence = np.where(strong, 2, np.where(passed, 1, 0)).astype(np.int8)
    high = strong & fire
    stats = {
        "valid": int(valid.sum()),
        "cloud": int(cloud.sum()),
        "clear": int(clear.sum()),
        "candidates": int(candidate.sum()),
        "backscatter": int(backscatter.sum()),
        "fires": int(fire.sum()),
        "high": int(high.sum()),
        "excluded_fires": int(excluded_fire.sum()),
        "clear_btd_median": None if np.isnan(clear_median) else round(clear_median, 3),
    }
    return FireTestResult(
        fire=fire,
        confidence=confidence,
        valid=valid,
        cloud=cloud,
        clear=clear,
        btd=btd,
        bg_btd=bg_btd,
        bg_btd_sigma=bg_btd_s,
        bg_bt38=bg_38,
        bg_bt38_sigma=bg_38_s,
        bg_bt11=bg_11,
        bg_count=bg_count,
        excess=excess,
        frp_mw=np.where(np.isfinite(bg_38), frp, np.nan),
        absolute=absolute,
        day_weight=w,
        excluded_fire=excluded_fire,
        stats=stats,
    )


# --- persistent heat sources --------------------------------------------------------------------------

# A pixel GK-2A sees burning in the deep night on several nights is a plant, kiln or flare, not a
# field: crop residue is burnt by day and in the evening. jobs/geostationary.py flags such pixels,
# and pixels FIRMS's own static rule covers, before each run, and passes them as `exclude`.
# scripts/gk2a_eval_static.py measured the rule on the 27 Oct - 22 Nov 2026 record with no
# look-ahead: it removed 0.08% of 16:00-18:30 IST detections and 25% of 23:00-05:00 IST ones.
STATIC_LOOKBACK_NIGHTS = 14
STATIC_MIN_DEEP_NIGHTS = 2
DEEP_NIGHT_IST = ("23:00", "05:00")
STATIC_RULE = (
    "GK-2A pixels that are persistent heat sources, not fields, are left out of the counts unless "
    "include_static=true: pixels whose footprint meets a spot where FIRMS saw heat on "
    f"{STATIC_MIN_NIGHTS} or more nights (within {STATIC_RADIUS_KM * 1000:.0f} m), and pixels "
    f"where GK-2A itself saw heat between {DEEP_NIGHT_IST[0]} and {DEEP_NIGHT_IST[1]} IST on "
    f"{STATIC_MIN_DEEP_NIGHTS} or more of the previous {STATIC_LOOKBACK_NIGHTS} nights. The rules "
    "aim at plants, kilns and flares; some flagged spots may be straw-bale depots, which burn crop "
    "residue but are not field burning."
)


# --- footprints ------------------------------------------------------------------------------------


def pixel_area_km2(corner_lon: np.ndarray, corner_lat: np.ndarray) -> np.ndarray:
    """Footprint area (km2) of each pixel from its corner grid (shape rows+1 x cols+1), by the
    shoelace formula in a Lambert azimuthal equal-area projection centred on the grid."""
    import pyproj

    lon0, lat0 = float(np.nanmean(corner_lon)), float(np.nanmean(corner_lat))
    laea = pyproj.Transformer.from_crs(
        "EPSG:4326", f"+proj=laea +lat_0={lat0} +lon_0={lon0} +units=m", always_xy=True
    )
    x, y = laea.transform(corner_lon, corner_lat)
    x0, y0, x1, y1 = x[:-1, :-1], y[:-1, :-1], x[:-1, 1:], y[:-1, 1:]
    x2, y2, x3, y3 = x[1:, 1:], y[1:, 1:], x[1:, :-1], y[1:, :-1]
    twice = (x0 * y1 - x1 * y0) + (x1 * y2 - x2 * y1) + (x2 * y3 - x3 * y2) + (x3 * y0 - x0 * y3)
    return np.abs(twice) / 2 / 1e6


# --- validation against polar-orbiting detections ----------------------------------------------------


@dataclass(frozen=True)
class PolarPass:
    """One look of one polar satellite at the region: its detections there, grouped in time."""

    satellite: str
    instrument: str
    start: datetime
    end: datetime
    n_detections: int
    median_scan_km: float
    frp_sum_mw: float

    @property
    def mid(self) -> datetime:
        return self.start + (self.end - self.start) / 2


def group_passes(
    acq_utc: np.ndarray,
    satellite: np.ndarray,
    instrument: np.ndarray,
    scan_km: np.ndarray,
    frp: np.ndarray,
    gap_minutes: float = 20.0,
) -> list[PolarPass]:
    """Group detections into passes: same satellite, consecutive times under `gap_minutes` apart
    (one overpass of Punjab lasts 1-3 minutes; the same satellite returns ~100 minutes later)."""
    passes: list[PolarPass] = []
    for sat in sorted(set(satellite.tolist())):
        sel = np.nonzero(satellite == sat)[0]
        order = sel[np.argsort(acq_utc[sel])]
        seconds = acq_utc[order].astype("datetime64[s]").astype("int64")
        breaks = np.nonzero(np.diff(seconds) > gap_minutes * 60)[0] + 1
        for group in np.split(order, breaks):
            t = acq_utc[group].astype("datetime64[s]").astype("int64")
            passes.append(
                PolarPass(
                    satellite=str(sat),
                    instrument=str(instrument[group[0]]),
                    start=datetime.fromtimestamp(int(t.min()), UTC),
                    end=datetime.fromtimestamp(int(t.max()), UTC),
                    n_detections=int(group.size),
                    median_scan_km=float(np.median(scan_km[group])),
                    frp_sum_mw=float(np.nansum(frp[group])),
                )
            )
    return sorted(passes, key=lambda p: p.start)


@dataclass
class PassComparison:
    """GK-2A fire pixels vs one polar pass, on the GK-2A grid, inside the analysis region."""

    ref_pixels: int  # GK-2A pixels holding at least one polar detection
    ref_hits: int  # ...that GK-2A flagged (that pixel or a neighbour)
    ref_pixels_strong: int  # ref pixels whose summed polar FRP >= strong_frp_mw
    ref_hits_strong: int
    ref_pixels_clear: int  # ref pixels GK-2A saw clear (not cloud-masked)
    geo_pixels: int  # GK-2A fire pixels
    geo_hits: int  # ...with a polar detection in the same or a neighbouring pixel
    strong_frp_mw: float

    @property
    def hit_rate(self) -> float | None:
        return self.ref_hits / self.ref_pixels if self.ref_pixels else None

    @property
    def false_alarm_rate(self) -> float | None:
        return (self.geo_pixels - self.geo_hits) / self.geo_pixels if self.geo_pixels else None


@dataclass(frozen=True)
class PassMatch:
    """Pixel masks of one GK-2A vs polar comparison, on the GK-2A window grid."""

    geo: np.ndarray  # GK-2A fire pixels inside the region
    geo_hit: np.ndarray  # ...with a polar detection in the same or a neighbouring pixel
    ref: np.ndarray  # region pixels holding at least one polar detection
    ref_hit: np.ndarray  # ...with a GK-2A fire pixel in the same or a neighbouring pixel
    strong: np.ndarray  # ref pixels whose summed polar FRP >= the strong threshold
    clear: np.ndarray

    def counts(self, where: np.ndarray | None = None) -> dict[str, int]:
        """The comparison counted only on the pixels of `where` (all when None). Matches still
        use every region pixel, so a pixel at the edge of `where` can be confirmed from outside."""
        m = np.ones(self.geo.shape, dtype=bool) if where is None else where
        return {
            "ref_pixels": int((self.ref & m).sum()),
            "ref_hits": int((self.ref_hit & m).sum()),
            "ref_pixels_strong": int((self.strong & m).sum()),
            "ref_hits_strong": int((self.strong & self.ref_hit & m).sum()),
            "ref_pixels_clear": int((self.ref & self.clear & m).sum()),
            "geo_pixels": int((self.geo & m).sum()),
            "geo_hits": int((self.geo_hit & m).sum()),
        }


def match_pass(
    geo_fire: np.ndarray,
    clear: np.ndarray,
    region: np.ndarray,
    ref_rows: np.ndarray,
    ref_cols: np.ndarray,
    ref_frp: np.ndarray,
    strong_frp_mw: float = 30.0,
    tolerance_px: int = 1,
) -> PassMatch:
    """Match a GK-2A fire mask with polar detections mapped to window (row, col) indices.

    Matching allows one pixel of slack (a 3 x 3 neighbourhood): a VIIRS point and a GK-2A pixel
    centre can sit a pixel apart through geolocation error and terrain parallax.
    """
    shape = geo_fire.shape
    inside = (ref_rows >= 0) & (ref_rows < shape[0]) & (ref_cols >= 0) & (ref_cols < shape[1])
    ref_rows, ref_cols, ref_frp = ref_rows[inside], ref_cols[inside], ref_frp[inside]
    in_region = region[ref_rows, ref_cols]
    ref_rows, ref_cols, ref_frp = ref_rows[in_region], ref_cols[in_region], ref_frp[in_region]
    ref = np.zeros(shape, dtype=bool)
    frp_sum = np.zeros(shape)
    ref[ref_rows, ref_cols] = True
    np.add.at(frp_sum, (ref_rows, ref_cols), np.nan_to_num(ref_frp))
    geo = geo_fire & region
    size = 2 * tolerance_px + 1
    geo_near = _ndimage().maximum_filter(geo.astype(np.uint8), size=size) > 0
    ref_near = _ndimage().maximum_filter(ref.astype(np.uint8), size=size) > 0
    return PassMatch(
        geo=geo,
        geo_hit=geo & ref_near,
        ref=ref,
        ref_hit=ref & geo_near,
        strong=ref & (frp_sum >= strong_frp_mw),
        clear=clear & region,
    )


def compare_pass(
    geo_fire: np.ndarray,
    clear: np.ndarray,
    region: np.ndarray,
    ref_rows: np.ndarray,
    ref_cols: np.ndarray,
    ref_frp: np.ndarray,
    strong_frp_mw: float = 30.0,
    tolerance_px: int = 1,
) -> PassComparison:
    """Compare a GK-2A fire mask with polar detections mapped to window (row, col) indices,
    over the whole region (match_pass)."""
    m = match_pass(
        geo_fire, clear, region, ref_rows, ref_cols, ref_frp, strong_frp_mw, tolerance_px
    )
    return PassComparison(**m.counts(), strong_frp_mw=strong_frp_mw)


# --- evaluation only: validation where the polar sensor "could see" ---------------------------------
# COVERED_MAX_SCAN_DEG, SCAN_ANGLE_BINS, scan_angle_counts and bootstrap_rates serve the evaluation
# scripts (scripts/gk2a_eval_coverage.py, gk2a_eval_background.py); the job and the API do not use
# them. A "covered" rate counts only region pixels the polar sensor viewed at most this far off nadir
# (scan angle at the satellite), where its pixels are small (MODIS: 1.9 km along the scan at 40 deg,
# 4.8 km at 55 deg). It failed its pre-registered bar, so the site does not publish it: the MODIS
# unconfirmed share is not flat inside 0-40 deg. Season replay, 27 Oct-22 Nov 2026 (189 passes,
# `uv run python scripts/gk2a_eval_coverage.py --scans ...`): 3.6% (27/755) at 0-20 deg, 1.3%
# (5/378) at 20-30, 10.0% (58/579) at 30-40, 37.8% at 40-45, 43.6% at 45-50, 93.0% at 50-55.
COVERED_MAX_SCAN_DEG = {"MODIS": 40.0, "VIIRS": 50.0}
SCAN_ANGLE_BINS = (0.0, 20.0, 30.0, 40.0, 45.0, 50.0, 55.0, 90.0)  # the last: the swath edge on


def scan_angle_counts(
    match: PassMatch, scan_deg: np.ndarray, bins: Sequence[float] = SCAN_ANGLE_BINS
) -> list[dict[str, Any]]:
    """The comparison's counts in bins of the polar sensor's scan angle at each pixel."""
    out = []
    with np.errstate(invalid="ignore"):
        for lo, hi in pairwise(bins):
            c = match.counts((scan_deg >= lo) & (scan_deg < hi))
            out.append({"lo": lo, "hi": hi, **{k: c[k] for k in (
                "geo_pixels", "geo_hits", "ref_pixels", "ref_hits")}})  # fmt: skip
    return out


def pooled_rates(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pool per-pass comparisons: hit rate = sum(ref_hits) / sum(ref_pixels) and false-alarm
    rate = 1 - sum(geo_hits) / sum(geo_pixels), with the counts behind them."""
    rows = list(rows)
    ref = sum(r["ref_pixels"] for r in rows)
    hits = sum(r["ref_hits"] for r in rows)
    strong = sum(r["ref_pixels_strong"] for r in rows)
    strong_hits = sum(r["ref_hits_strong"] for r in rows)
    geo = sum(r["geo_pixels"] for r in rows)
    geo_hits = sum(r["geo_hits"] for r in rows)
    return {
        "passes": len(rows),
        "hit_rate": round(hits / ref, 4) if ref else None,
        "n": ref,
        "hits": hits,
        "false_alarm_rate": round((geo - geo_hits) / geo, 4) if geo else None,
        "n_geo": geo,
        "geo_confirmed": geo_hits,
        "hit_rate_strong": round(strong_hits / strong, 4) if strong else None,
        "n_strong": strong,
        "hits_strong": strong_hits,
    }


BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20261123  # fixed, so the same rows always give the same interval


def bootstrap_rates(
    rows: Sequence[dict[str, Any]], draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED
) -> dict[str, list[float] | None]:
    """95% intervals of the pooled hit, strong-hit and false-alarm rates, resampling whole passes
    with replacement. Pixels of one pass are not independent (one swath-edge pass once gave 316 of
    355 unconfirmed pixels), so an interval over pixels (Wilson) would be far too narrow."""
    keys = {
        "hit_rate": ("ref_hits", "ref_pixels"),
        "hit_rate_strong": ("ref_hits_strong", "ref_pixels_strong"),
        "false_alarm_rate": ("geo_unconfirmed", "geo_pixels"),
    }
    out: dict[str, list[float] | None] = dict.fromkeys(keys)
    if len(rows) < 2:
        return out
    idx = np.random.default_rng(seed).integers(0, len(rows), size=(draws, len(rows)))

    def column(key: str) -> np.ndarray:
        if key == "geo_unconfirmed":
            return np.array([r["geo_pixels"] - r["geo_hits"] for r in rows], dtype=float)
        return np.array([r[key] for r in rows], dtype=float)

    for name, (num, den) in keys.items():
        n, d = column(num)[idx].sum(axis=1), column(den)[idx].sum(axis=1)
        ok = d > 0
        if ok.sum() < draws / 2:  # most resamples hold no pixel of this kind
            continue
        ratio = n[ok] / d[ok]
        out[name] = [round(float(np.percentile(ratio, 2.5)), 4),
                     round(float(np.percentile(ratio, 97.5)), 4)]  # fmt: skip
    return out


# --- diurnal record ------------------------------------------------------------------------------------

IST_OFFSET = timedelta(hours=5, minutes=30)  # India has one time zone and no daylight saving


def ist_minute_of_day(t: datetime) -> int:
    local = t.astimezone(UTC) + IST_OFFSET
    return local.hour * 60 + local.minute


def ist_minutes(t: datetime) -> float:
    """Minute of the IST day with its fraction (seconds count)."""
    local = t.astimezone(UTC) + IST_OFFSET
    return local.hour * 60 + local.minute + local.second / 60


def ist_date(t: datetime) -> date:
    """The IST calendar day of a moment, whatever time zone the datetime carries."""
    return (t.astimezone(UTC) + IST_OFFSET).date()


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping [start, end] minute-of-day intervals."""
    out: list[list[int]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def _split_day(a: float, b: float) -> list[tuple[float, float]]:
    """A minute-of-day interval that may cross midnight, as pieces inside [0, 1440]."""
    if a < 0:
        return [(a + 1440, 1440), (0, b)]
    if b > 1440:
        return [(a, 1440), (0, b - 1440)]
    return [(a, b)]


def _hhmm(minute: int) -> str:
    minute = max(0, min(1440, minute))
    return "24:00" if minute == 1440 else f"{minute // 60:02d}:{minute % 60:02d}"


def polar_windows(passes: Iterable[PolarPass], pad_minutes: float) -> list[dict[str, Any]]:
    """IST times of day at which a polar satellite looked at the region on at least one day of the
    period: each pass [start, end] padded by `pad_minutes`, then merged across passes and days."""
    pieces: list[tuple[int, int, str]] = []
    for p in passes:
        a = ist_minute_of_day(p.start) - pad_minutes
        b = ist_minute_of_day(p.end) + pad_minutes
        if b < a:  # the pass itself straddles midnight IST
            b += 1440
        for lo, hi in _split_day(a, b):
            pieces.append((int(np.floor(lo)), int(np.ceil(hi)), p.satellite))
    windows = []
    for lo, hi in merge_intervals((lo, hi) for lo, hi, _ in pieces):
        sats = sorted({s for a, b, s in pieces if a < hi and b > lo})
        windows.append(
            {"start": _hhmm(lo), "end": _hhmm(hi), "start_min": lo, "end_min": hi, "satellites": sats}
        )
    return windows


def inside_windows(minute_of_day: float, windows: Sequence[dict[str, Any]]) -> bool:
    """Whether a moment falls in a window, each window [start, end) so that a moment on a
    boundary belongs to exactly one of a window and the gap after it."""
    return any(w["start_min"] <= minute_of_day < w["end_min"] for w in windows)


SCAN_MINUTES = 10  # GK-2A full-disk repeat cycle: each scan stands for 10 minutes


def scan_overlap(
    minute_of_day: float,
    spans: Iterable[tuple[float, float]],
    scan_minutes: float = SCAN_MINUTES,
) -> float:
    """Share (0-1) of the 10 minutes a scan stands for, [t - 5, t + 5), that falls inside the
    given minute-of-day spans [start, end) (a span may run past 1440 when it wraps midnight).

    Counting a scan by its overlap, instead of by whether its moment is inside, removes the knife
    edge at window boundaries: with 15-minute pads Aqua's window used to end exactly on the
    17:02 IST peak scan, which then counted fully on one side or the other (and on both sides,
    before windows were made half-open)."""
    lo, hi = minute_of_day - scan_minutes / 2, minute_of_day + scan_minutes / 2
    total = 0.0
    for start, end in spans:
        for shift in (-1440.0, 0.0, 1440.0):
            total += max(0.0, min(hi, end + shift) - max(lo, start + shift))
    return min(1.0, total / scan_minutes)


def window_spans(windows: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
    return [(float(w["start_min"]), float(w["end_min"])) for w in windows]


def outside_shares(
    slots: Sequence[tuple[datetime, int]],
    passes: Sequence[PolarPass],
    pads: Sequence[float] = (0, 5, 10, 15),
) -> list[dict[str, Any]]:
    """Share of GK-2A fire pixels outside the polar windows, for several pads, two ways:
    `union` puts every day's windows on one IST clock (a time of day counts as seen if a polar
    satellite looked then on any day: what the diurnal headline uses), `per_day` compares each scan
    only with the passes of its own IST day."""
    total = sum(count for _, count in slots)
    by_day: dict[Any, list[PolarPass]] = {}
    for p in passes:
        by_day.setdefault(ist_date(p.start), []).append(p)
    out = []
    for pad in pads:
        union = window_spans(polar_windows(passes, pad))
        day_spans = {d: window_spans(polar_windows(dp, pad)) for d, dp in by_day.items()}
        outside_union = outside_day = 0.0
        for t, count in slots:
            m = ist_minutes(t)
            outside_union += count * (1 - scan_overlap(m, union))
            outside_day += count * (1 - scan_overlap(m, day_spans.get(ist_date(t), [])))
        out.append({
            "pad_min": pad,
            "union": round(outside_union / total, 4) if total else None,
            "per_day": round(outside_day / total, 4) if total else None,
        })  # fmt: skip
    return out


def hourly_means(slots: Sequence[tuple[datetime, int]]) -> list[dict[str, Any]]:
    """Mean fire-pixel count per scan in each IST hour (0-23), with the number of scans."""
    sums = np.zeros(24)
    scans = np.zeros(24, dtype=int)
    for t, count in slots:
        hour = ist_minute_of_day(t) // 60
        sums[hour] += count
        scans[hour] += 1
    return [
        {
            "hour": h,
            "mean_count": round(float(sums[h] / scans[h]), 2) if scans[h] else None,
            "scans": int(scans[h]),
        }
        for h in range(24)
    ]
