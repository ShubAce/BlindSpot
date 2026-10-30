"""When satellites can see a place: swath-aware pass prediction, and a check against NASA FIRMS.

How a pass is found (plain words, so anyone can explain it):
1. Propagate the satellite from its CelesTrak TLE with SGP4 every 30 s and work out the point on
   the ground directly under it.
2. A scanning instrument sweeps a strip (the swath) centred on that ground track. A site is "in
   the swath" on a pass when its closest distance to the ground track is at most half the swath
   width: VIIRS 1520 km, MODIS 1165 km, Sentinel-2 MSI 148 km (INSTRUMENTS; the same numbers the
   map uses to draw swaths).
3. Each local minimum of the site-to-ground-track distance is refined to the second. That moment
   (peak_utc, the time of closest approach) is when a cross-track scanner actually images the
   site; start_utc/end_utc bracket the minutes when the site lies within half a swath of the
   point under the satellite.

Speed: SGP4 runs vectorised (sgp4's C++ sgp4_array), and its TEME positions are turned into
Earth-fixed ones by rotating through Greenwich mean sidereal time (skyfield's theta_GMST1982, from
Vallado et al., AIAA 2006-6753). That is how satellite.js works in the browser, and it agrees with
skyfield's full-precision path to well under a kilometre (tests/test_orbits.py checks it), while
being about 50x faster than evaluating the IAU-2000A nutation series for every sample.

Validated against reality: the predicted closest-approach times match FIRMS acquisition labels
over Punjab and Haryana to within about +/-2 minutes (firms_label_errors re-checks it on stored
detections). FIRMS labels are whole minutes in ~2 minute steps. Sentinel-2 passes are predictions
only: MSI images on sunlit descending passes, but whether a scene was acquired is for STAC to say.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sgp4.api import Satrec, jday
from skyfield.sgp4lib import theta_GMST1982
from sqlalchemy import text
from sqlalchemy.orm import Session

IST_ZONE = ZoneInfo("Asia/Kolkata")  # for the IST dial only; every stored time is UTC
EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius (Moritz, Geodetic Reference System 1980)
WGS84_A_KM = 6378.137  # WGS-84 equatorial radius (NIMA TR8350.2)
WGS84_F = 1 / 298.257223563  # WGS-84 flattening (NIMA TR8350.2)
WGS84_E2 = WGS84_F * (2 - WGS84_F)
COARSE_STEP_S = 30.0  # first sweep; the point under the satellite moves at most ~7.5 km/s
MAX_GROUND_SPEED_KM_S = 7.5  # bound for low Earth orbit, used to size search margins
MIN_GROUND_SPEED_KM_S = 6.0  # lower bound at 690-850 km, used to size the in-view window
FIRE_INSTRUMENTS = ("VIIRS", "MODIS")  # the instruments behind NASA FIRMS fire detections


@dataclass(frozen=True)
class Instrument:
    name: str
    swath_km: float  # published swath width
    half_swath_km: float  # the visibility test: site within this of the ground track
    scan_half_angle_deg: float  # published scan half-angle (explains the swath; see tests)
    sunlit_descending_only: bool  # MSI images only on sunlit descending passes
    citation: str


INSTRUMENTS: dict[str, Instrument] = {
    "VIIRS": Instrument(
        "VIIRS", 3040.0, 1520.0, 56.06, False,
        "NOAA NESDIS InPort, VIIRS instrument record 1239: 'The limits of the field of regard are "
        "at +/-56.06 degrees, covering a swath width of 3040km'",
    ),
    "MODIS": Instrument(
        "MODIS", 2330.0, 1165.0, 55.0, False,
        "NASA LAADS DAAC, MODIS specifications: swath '2330km (cross track)', field of view "
        "110 deg, orbit 705 km",
    ),
    "MSI": Instrument(
        "MSI", 290.0, 148.0, 10.3, True,
        "ESA Sentinel-2 MSI technical guide: 20.6 deg field of view, 'compound swath width of "
        "290 km'; half-swath 145 km + 3 km because Sentinel-2 tiles exist 145-146 km off-track at "
        "Sangrur (docs/research/firms-and-orbits.md 3.2, SYNTHESIS decision table)",
    ),
}  # fmt: skip

# Default site for /api/orbits/passes when no lat/lon is given: the centroid of Punjab. Taken from
# the boundary table when the boundaries module has loaded the state; otherwise this reference
# value: the centroid of the geoBoundaries gbOpen IND ADM1 polygon IN-PB (DataMeet / Election
# Commission of India, CC BY 2.5 IN), computed with shapely on 2026-10-30.
PUNJAB_CENTROID_REFERENCE = (30.8425, 75.4153)  # lat, lon


# --- geometry ------------------------------------------------------------------------------------


def half_swath_from_scan(half_angle_deg: float, altitude_km: float) -> float:
    """Ground distance from nadir to the swath edge for a scan half-angle at an altitude (sphere).

    Law of sines in the triangle Earth centre - satellite - swath edge: the Earth-centre angle is
    lam = asin((R + h) / R * sin(eta)) - eta. Used to show the published swaths are consistent
    with the published scan angles (MODIS 55 deg at 705 km gives exactly 1165 km).
    """
    eta = math.radians(half_angle_deg)
    ratio = (EARTH_RADIUS_KM + altitude_km) / EARTH_RADIUS_KM * math.sin(eta)
    if ratio >= 1.0:  # the edge ray would miss the Earth: the swath ends at the horizon
        return EARTH_RADIUS_KM * math.acos(EARTH_RADIUS_KM / (EARTH_RADIUS_KM + altitude_km))
    return EARTH_RADIUS_KM * (math.asin(ratio) - eta)


def ground_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Great-circle distance in km (haversine on the mean-radius sphere); works on numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dlon = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _geodetic(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Earth-fixed x, y, z (km) -> WGS-84 geodetic latitude and longitude in degrees."""
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - WGS84_E2))
    for _ in range(4):  # fixed-point iteration; converges to < 1 mm for orbital heights
        n = WGS84_A_KM / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - WGS84_E2 * n / (n + h)))
    return np.degrees(lat), np.degrees(np.arctan2(y, x))


def _ecef(lat_deg: float, lon_deg: float) -> np.ndarray:
    """A point on the WGS-84 ellipsoid as Earth-fixed x, y, z in km."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    return np.array(
        [n * math.cos(lat) * math.cos(lon), n * math.cos(lat) * math.sin(lon),
         n * (1 - WGS84_E2) * math.sin(lat)]
    )  # fmt: skip


def elevation_deg(site_lat: float, site_lon: float, sat_xyz: np.ndarray) -> float:
    """Angle of the satellite above the site's horizon (the ellipsoid normal is 'up')."""
    lat, lon = math.radians(site_lat), math.radians(site_lon)
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    los = sat_xyz - _ecef(site_lat, site_lon)
    return math.degrees(math.asin(float(np.dot(los, up) / np.linalg.norm(los))))


def sun_elevation_deg(lat: float, lon: float, when: datetime) -> float:
    """Solar elevation at a place and UTC time, accurate to a fraction of a degree.

    NOAA Global Monitoring Laboratory, "General Solar Position Calculations"
    (gml.noaa.gov/grad/solcalc/solareqns.PDF): fractional year, equation of time and declination
    as Fourier series, then the hour angle from true solar time. Enough to tell day from night.
    """
    when = when.astimezone(UTC)
    leap = when.year % 4 == 0 and (when.year % 100 != 0 or when.year % 400 == 0)
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / (366 if leap else 365) * (when.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )  # fmt: skip
    decl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )  # fmt: skip
    hour_angle = math.radians((hour * 60 + eqtime + 4 * lon) / 4 - 180)
    phi = math.radians(lat)
    cos_zenith = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


# --- propagation ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrbitElements:
    """What pass prediction needs from one tle row."""

    norad_id: int
    key: str
    name: str
    instrument: str
    line1: str
    line2: str
    epoch: datetime
    fetched_at: datetime | None = None
    fetch_id: int | None = None

    def satrec(self) -> Satrec:
        return _satrec(self.line1, self.line2)


@lru_cache(maxsize=64)
def _satrec(line1: str, line2: str) -> Satrec:
    return Satrec.twoline2rv(line1, line2)


@dataclass(frozen=True)
class Track:
    offset_s: np.ndarray  # seconds after t0
    lat: np.ndarray  # point under the satellite, WGS-84 geodetic degrees (NaN if SGP4 failed)
    lon: np.ndarray
    xyz: np.ndarray  # Earth-fixed position, km, shape (n, 3)


def track(satrec: Satrec, t0: datetime, offsets_s: np.ndarray) -> Track:
    """Where the satellite is at t0 + offsets: SGP4 in TEME, rotated to Earth-fixed by GMST.

    UTC stands in for UT1 (they differ by under 0.9 s, about 0.4 km of Earth rotation at most),
    exactly as satellite.js does in the browser.
    """
    t0 = t0.astimezone(UTC)
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute,
                    t0.second + t0.microsecond / 1e6)  # fmt: skip
    fr = fr0 + np.asarray(offsets_s, dtype=float) / 86400.0
    jd = np.full_like(fr, jd0)
    err, r, _v = satrec.sgp4_array(jd, fr)
    theta, _ = theta_GMST1982(jd, fr)
    c, s = np.cos(theta), np.sin(theta)
    x = c * r[:, 0] + s * r[:, 1]
    y = -s * r[:, 0] + c * r[:, 1]
    z = r[:, 2].copy()
    bad = err != 0  # SGP4 error codes (e.g. a decayed orbit): leave those samples out
    x[bad] = y[bad] = z[bad] = np.nan
    lat, lon = _geodetic(x, y, z)
    return Track(np.asarray(offsets_s, dtype=float), lat, lon, np.column_stack([x, y, z]))


def _refine_minimum(x: np.ndarray, y: np.ndarray, j: int) -> tuple[float, float]:
    """Vertex of the parabola through the samples around index j: sub-step time and value."""
    if j <= 0 or j >= len(x) - 1:
        return float(x[j]), float(y[j])
    y0, y1, y2 = float(y[j - 1]), float(y[j]), float(y[j + 1])
    denom = y0 - 2 * y1 + y2
    if not denom > 0:
        return float(x[j]), y1
    shift = 0.5 * (y0 - y2) / denom  # in samples, within (-1, 1)
    return float(x[j]) + shift * float(x[j + 1] - x[j]), y1 - 0.25 * (y0 - y2) * shift


def _crossing(t_out: float, d_out: float, t_in: float, d_in: float, level: float) -> float:
    """Linear interpolation of the time the distance crosses `level` between two samples."""
    if d_out == d_in:
        return t_in
    return t_in + (t_out - t_in) * (level - d_in) / (d_out - d_in)


@dataclass(frozen=True)
class Pass:
    satellite: str
    key: str
    instrument: str
    start_utc: datetime
    peak_utc: datetime  # closest approach: when a cross-track scanner images the site
    end_utc: datetime
    peak_elevation_deg: float
    min_ground_distance_km: float  # site to ground track at closest approach
    off_nadir_deg: float  # how far from straight down the instrument looks at the site
    direction: str  # ascending (north-bound) / descending
    daynight: str  # D when the Sun is above the site's horizon at peak, else N

    def as_dict(self) -> dict[str, Any]:
        return {
            "satellite": self.satellite,
            "key": self.key,
            "instrument": self.instrument,
            "start_utc": self.start_utc,
            "peak_utc": self.peak_utc,
            "end_utc": self.end_utc,
            "peak_elevation_deg": round(self.peak_elevation_deg, 1),
            "min_ground_distance_km": round(self.min_ground_distance_km, 1),
            "off_nadir_deg": round(self.off_nadir_deg, 1),
            "direction": self.direction,
            "daynight": self.daynight,
        }


def _round_s(t: datetime) -> datetime:
    return (t + timedelta(microseconds=500_000)).replace(microsecond=0)


def find_passes(
    orbit: OrbitElements,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    *,
    step_s: float = COARSE_STEP_S,
) -> list[Pass]:
    """Every pass of `orbit` whose swath covers (lat, lon), with closest approach in [start, end]."""
    instrument = INSTRUMENTS[orbit.instrument]
    half = instrument.half_swath_km
    satrec = orbit.satrec()
    pad = timedelta(minutes=15)  # catch a pass whose closest approach sits right at an edge
    t0 = start - pad
    offsets = np.arange(0.0, (end - start + 2 * pad).total_seconds() + step_s, step_s)
    coarse = track(satrec, t0, offsets)
    dist = ground_km(lat, lon, coarse.lat, coarse.lon)
    minima = np.flatnonzero((dist[1:-1] <= dist[:-2]) & (dist[1:-1] < dist[2:])) + 1
    margin = step_s * MAX_GROUND_SPEED_KM_S  # a coarse sample can miss the minimum by this much
    candidates = minima[dist[minima] <= half + margin]
    if candidates.size == 0:
        return []

    # Refine every candidate at 1 s in one SGP4 call, then each in-view window at 2 s in another.
    fine_rel = np.arange(-step_s, step_s + 0.5, 1.0)
    fine_t = (offsets[candidates][:, None] + fine_rel[None, :]).ravel()
    fine = track(satrec, t0, fine_t)
    fine_d = ground_km(lat, lon, fine.lat, fine.lon).reshape(len(candidates), -1)
    fine_lat = fine.lat.reshape(len(candidates), -1)
    fine_t = fine_t.reshape(len(candidates), -1)

    found: list[tuple[float, float, bool]] = []  # (tca offset s, distance km, ascending)
    for row_t, row_d, row_lat in zip(fine_t, fine_d, fine_lat, strict=True):
        j = int(np.nanargmin(row_d))
        tca_s, d_min = _refine_minimum(row_t, row_d, j)
        peak = t0 + timedelta(seconds=tca_s)
        if d_min > half or not (start <= peak <= end):
            continue
        ascending = bool(row_lat[min(j + 1, len(row_t) - 1)] > row_lat[max(j - 1, 0)])
        found.append((tca_s, d_min, ascending))
    if not found:
        return []

    reach_s = half / MIN_GROUND_SPEED_KM_S + 30.0
    win_rel = np.arange(-reach_s, reach_s + 1.0, 2.0)
    win_t = (np.array([f[0] for f in found])[:, None] + win_rel[None, :]).ravel()
    win = track(satrec, t0, win_t)
    win_d = ground_km(lat, lon, win.lat, win.lon).reshape(len(found), -1)
    win_t = win_t.reshape(len(found), -1)
    peak_xyz = track(satrec, t0, np.array([f[0] for f in found])).xyz

    passes = []
    for (tca_s, d_min, ascending), row_t, row_d, xyz in zip(found, win_t, win_d, peak_xyz, strict=True):
        peak = t0 + timedelta(seconds=tca_s)
        sun = sun_elevation_deg(lat, lon, peak)
        if instrument.sunlit_descending_only and (ascending or sun <= 0):
            continue  # MSI does not image on this pass
        k = int(np.argmin(np.abs(row_t - tca_s)))
        start_s = end_s = tca_s  # grazing the swath edge: in view for an instant
        if row_d[k] <= half:
            a = b = k
            while a > 0 and row_d[a - 1] <= half:
                a -= 1
            while b < len(row_t) - 1 and row_d[b + 1] <= half:
                b += 1
            if a > 0:
                start_s = _crossing(row_t[a - 1], row_d[a - 1], row_t[a], row_d[a], half)
            if b < len(row_t) - 1:
                end_s = _crossing(row_t[b + 1], row_d[b + 1], row_t[b], row_d[b], half)
        elevation = elevation_deg(lat, lon, xyz)
        lam_deg = math.degrees(d_min / EARTH_RADIUS_KM)
        passes.append(
            Pass(
                satellite=orbit.name,
                key=orbit.key,
                instrument=orbit.instrument,
                start_utc=_round_s(t0 + timedelta(seconds=float(min(start_s, tca_s)))),
                peak_utc=_round_s(peak),
                end_utc=_round_s(t0 + timedelta(seconds=float(max(end_s, tca_s)))),
                peak_elevation_deg=elevation,
                min_ground_distance_km=d_min,
                off_nadir_deg=max(0.0, 90.0 - elevation - lam_deg),
                direction="ascending" if ascending else "descending",
                daynight="D" if sun > 0 else "N",
            )
        )
    return passes


def passes_for_site(
    orbits: Iterable[OrbitElements], lat: float, lon: float, start: datetime, end: datetime
) -> list[Pass]:
    """All in-swath passes of all satellites over a site, in time order."""
    found = [p for orbit in orbits for p in find_passes(orbit, lat, lon, start, end)]
    return sorted(found, key=lambda p: p.peak_utc)


def _brief(p: Pass | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "satellite": p.satellite,
        "instrument": p.instrument,
        "at_utc": p.peak_utc,
        "start_utc": p.start_utc,
        "end_utc": p.end_utc,
        "min_ground_distance_km": round(p.min_ground_distance_km, 1),
    }


def current_status(passes: Sequence[Pass], now: datetime) -> dict[str, Any]:
    """Who is watching the site right now, and the next and last looks.

    `next` and `last` count the fire-detecting instruments only (VIIRS, MODIS), because
    "watching" in BlindSpot means a satellite that could report a fire; `next_any` also counts
    Sentinel-2. `blind_for_minutes` is how long until the next fire-sensor look begins (0 while
    one is watching; null when none falls inside the window).
    """
    watching = [
        {"satellite": p.satellite, "instrument": p.instrument, "until_utc": p.end_utc,
         "peak_utc": p.peak_utc}
        for p in passes if p.start_utc <= now <= p.end_utc
    ]  # fmt: skip
    fire = [p for p in passes if p.instrument in FIRE_INSTRUMENTS]
    nxt = min((p for p in fire if p.peak_utc > now), key=lambda p: p.peak_utc, default=None)
    last = max((p for p in fire if p.peak_utc <= now), key=lambda p: p.peak_utc, default=None)
    nxt_any = min((p for p in passes if p.peak_utc > now), key=lambda p: p.peak_utc, default=None)
    fire_watching = any(w["instrument"] in FIRE_INSTRUMENTS for w in watching)
    blind: int | None = 0 if fire_watching else None
    if not fire_watching and nxt is not None:
        blind = max(0, round((nxt.start_utc - now).total_seconds() / 60))
    return {
        "watching": watching,
        "next": _brief(nxt),
        "last": _brief(last),
        "next_any": _brief(nxt_any),
        "blind_for_minutes": blind,
    }


def _minute_ist(t: datetime) -> int:
    local = t.astimezone(IST_ZONE)
    return local.hour * 60 + local.minute


def look_clock(passes: Sequence[Pass]) -> dict[str, Any]:
    """The 24 h IST dial from predicted passes: each satellite's daily look windows (day and
    night), the windows in which any fire sensor (VIIRS, MODIS) looks, and the longest daily gap
    between them, which is the blind span predicted from orbits alone.

    Uses the same windowing as the data-derived /api/fires/hours, so the two can be compared.
    """
    from blindspot.analytics.fires_stats import observed_windows  # shared windowing rule

    groups: dict[tuple[str, str, str], Counter[int]] = defaultdict(Counter)
    for p in passes:
        groups[(p.satellite, p.instrument, p.daynight)][_minute_ist(p.peak_utc)] += 1
    looks = []
    for (satellite, instrument, daynight), minutes in groups.items():
        windows, _, _ = observed_windows(minutes, gap_min=90, min_share=0.0)
        for w in windows:
            looks.append({"satellite": satellite, "instrument": instrument, "daynight": daynight,
                          "start_ist": w["start"], "end_ist": w["end"], "passes": w["count"]})  # fmt: skip
    looks.sort(key=lambda w: (w["start_ist"], w["satellite"]))
    fire_minutes: Counter[int] = Counter()
    fire_sats: dict[int, set[str]] = defaultdict(set)
    fire_dn: dict[int, Counter[str]] = defaultdict(Counter)
    for p in passes:
        if p.instrument in FIRE_INSTRUMENTS:
            m = _minute_ist(p.peak_utc)
            fire_minutes[m] += 1
            fire_sats[m].add(p.satellite)
            fire_dn[m][p.daynight] += 1
    windows, gaps, span = observed_windows(fire_minutes, fire_sats, fire_dn, min_share=0.0)
    return {"looks": looks, "fire_sensor_windows_ist": windows, "gaps_ist": gaps,
            "blind_span_ist": span}  # fmt: skip


# --- database helpers (shared with the agent and headline modules) -------------------------------


def load_orbits(session: Session) -> list[OrbitElements]:
    """Every stored element set, in catalogue order (empty until the tle job has run)."""
    from blindspot.sources.celestrak import SATELLITES  # the catalogue order

    rows = session.execute(
        text(
            "SELECT norad_id, key, name, instrument, line1, line2, epoch, fetched_at, fetch_id "
            "FROM tle"
        )
    ).mappings()
    by_id = {r["norad_id"]: OrbitElements(**dict(r)) for r in rows}
    ordered = [by_id.pop(s.norad_id) for s in SATELLITES if s.norad_id in by_id]
    return ordered + list(by_id.values())


@dataclass(frozen=True)
class Site:
    lat: float
    lon: float
    name: str
    basis: str  # where the coordinates came from


def default_site(session: Session) -> Site:
    """Punjab's centroid: from the boundary table if the state is loaded, else the reference."""
    row = session.execute(
        text(
            """
            SELECT ST_Y(c) AS lat, ST_X(c) AS lon FROM (
                SELECT ST_Centroid(ST_Union(geom)) AS c FROM boundary
                WHERE level = 'state' AND lower(name) = 'punjab'
            ) s WHERE c IS NOT NULL
            """
        )
    ).first()
    if row is not None and row.lat is not None:
        return Site(round(row.lat, 4), round(row.lon, 4), "Punjab (centroid)",
                    "centroid of the Punjab state polygon in the boundary table")  # fmt: skip
    lat, lon = PUNJAB_CENTROID_REFERENCE
    return Site(lat, lon, "Punjab (centroid)",
                "reference centroid of the geoBoundaries IND ADM1 polygon IN-PB")  # fmt: skip


# --- validation against NASA FIRMS ---------------------------------------------------------------

LABEL_MATCH_SLACK_KM = 50.0  # a detection this far beyond the half-swath is "unmatched"


@dataclass(frozen=True)
class LabelCheck:
    satellite: str
    acq_utc: datetime  # FIRMS label
    predicted_utc: datetime | None  # our closest approach over the detection (None: unmatched)
    error_min: float | None  # predicted - label, minutes
    cross_track_km: float | None  # the detection's distance from the predicted ground track


def firms_label_errors(
    orbit: OrbitElements, detections: Sequence[tuple[datetime, float, float]]
) -> list[LabelCheck]:
    """For FIRMS detections (acq_utc, lat, lon) of one satellite, predict when that satellite was
    closest to each detection and compare with the FIRMS acquisition label.

    Detections sharing a label share one orbit sample (+/-20 min at 2 s), so thousands of
    detections cost a few SGP4 calls. A detection farther from the predicted track than the half
    swath plus 50 km is reported unmatched: a wrong satellite label or a stale TLE, never hidden.
    """
    half = INSTRUMENTS[orbit.instrument].half_swath_km
    satrec = orbit.satrec()
    groups: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
    for acq, lat, lon in detections:
        groups[acq].append((lat, lon))
    offsets = np.arange(-1200.0, 1200.5, 2.0)
    out: list[LabelCheck] = []
    for acq, points in groups.items():
        path = track(satrec, acq, offsets)
        lats = np.array([p[0] for p in points])[:, None]
        lons = np.array([p[1] for p in points])[:, None]
        dist = ground_km(lats, lons, path.lat[None, :], path.lon[None, :])
        for row in dist:
            j = int(np.nanargmin(row))
            t_s, d_min = _refine_minimum(offsets, row, j)
            if d_min > half + LABEL_MATCH_SLACK_KM or j in (0, len(offsets) - 1):
                out.append(LabelCheck(orbit.name, acq, None, None, None))
                continue
            out.append(LabelCheck(orbit.name, acq, acq + timedelta(seconds=t_s), t_s / 60.0, d_min))
    return out


def summarize_errors(errors_min: Sequence[float]) -> dict[str, Any] | None:
    """n, mean, median, range, 95th-percentile absolute error and the share within 3 minutes."""
    if not errors_min:
        return None
    e = np.asarray(errors_min, dtype=float)
    return {
        "n": int(e.size),
        "mean_min": round(float(e.mean()), 2),
        "median_min": round(float(np.median(e)), 2),
        "min_min": round(float(e.min()), 2),
        "max_min": round(float(e.max()), 2),
        "p95_abs_min": round(float(np.percentile(np.abs(e), 95)), 2),
        "within_3_min_share": round(float(np.mean(np.abs(e) <= 3.0)), 4),
    }
