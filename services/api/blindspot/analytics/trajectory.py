"""Smoke transport: a 2-D Lagrangian particle model driven by the boundary-layer mean wind.

Plain numpy (plus scipy's KD-tree and rasterio for district labels); no database. The met job uses
`pressure_level_heights` and `bl_mean_wind` to precompute the wind the particles ride on; the smoke
job releases particles from fires and burn scars (forward) or from cities (backward) and uses the
accumulators below to turn particle positions into smoke-index series and back-trajectory summaries.

The method is the prototype validated in docs/research/meteo-and-trajectories.md section 3, kept
as validated:
- Wind: V_bl = (1/h) * integral of V(z) from 0 to h, with h = max(BLH, 100 m) and V(z)
  piecewise-linear through 10 m, 950, 925 and 850 hPa. By day, smoke from a field fire mixes
  through the boundary layer within an hour, so the layer-mean wind is what carries it (the
  research showed that using the 925 hPa wind instead moves the 24 h position by 221 km).
- Integration: Heun (RK2) with a 10 minute step; bilinear in space, linear in time. Research
  checks: 72 h back then forward returns within 2 km of the start; a 5 minute step moves the
  72 h end point by less than 0.2 km.
- Diffusion: a horizontal random walk, sigma = sqrt(2 K dt) with K = 1e4 m^2/s.
- A particle that leaves the wind grid stops there and is flagged.
Limits (stated with every result): 2-D, so particles are assumed well mixed through the boundary
layer; no deposition or rain-out; no terrain blocking over the Himalaya.

Units. Fires and burn scars are put on one scale, tonnes of crop residue (dry matter) burnt, so the
smoke index means "tonnes of burnt residue whose smoke is within 30 km of the city". See
`fire_dry_matter_t` and `scar_dry_matter_t` for the cited conversions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import numpy as np

# --- constants (each with its source) ---------------------------------------------------------------

EARTH_RADIUS_M = 6_371_000.0  # mean Earth radius (IUGG R1 = 6,371,008.8 m, rounded)
DEG = 180.0 / math.pi
KM_PER_DEG = EARTH_RADIUS_M / 1000.0 / DEG  # 111.195 km per degree of latitude
R_DRY_AIR = 287.05  # J/(kg K), gas constant of dry air (ICAO Doc 7488, ISO 2533)
G0 = 9.80665  # m/s^2, standard gravity (3rd CGPM, 1901)
LAPSE_RATE = 0.0065  # K/m, standard-atmosphere lapse rate (ICAO Doc 7488, ISO 2533)

BL_FLOOR_M = 100.0  # research section 3: h = max(BLH, 100 m); a night BLH of 15 m is real but
#                     a 15 m layer-mean would just be the 10 m wind, and smoke rises a little
K_H = 1.0e4  # m^2/s horizontal diffusivity (research section 3; spread 34/47 km at -24/-72 h)
DT_S = 600.0  # 10 minute integration step
RECORD_S = 1200.0  # positions kept every 20 minutes (the browser payload limit)
RECEPTOR_RADIUS_KM = 30.0  # "within 30 km of the city" (research section 3b)

# Fire radiative energy -> biomass burnt: (0.368 +/- 0.015) kg per MJ.
# Wooster et al. 2005, J. Geophys. Res. 110, D24311, doi:10.1029/2005JD006318, eq. 13-14.
FRE_TO_DRY_MATTER_KG_PER_MJ = 0.368
# One FIRMS detection is a snapshot of fire radiative power; a crop-residue fire lives about half
# an hour (Liu et al. 2020, Atmos. Environ. X, doi:10.31223/osf.io/ye6x7, cited in
# docs/research/domain-science-and-prior-art.md section 1), so one detection = FRP x 1800 s of energy.
FIRE_DURATION_S = 1800.0
# A burnt rice field consumes 5.5 t of dry matter per hectare. IPCC 2006 Guidelines for National
# Greenhouse Gas Inventories, Vol. 4, Ch. 2, Table 2.4 ("rice residues", post-harvest field
# burning; the table gives fuel actually consumed, MB x Cf).
RICE_RESIDUE_CONSUMED_T_PER_HA = 5.5

# When in the day a field is burnt, by IST hour: Punjab, Oct-Nov 2025, GK-2A geostationary
# detections with confidence >= 2, computed from Jethva's open dataset (Zenodo
# doi:10.5281/zenodo.20084790, CC BY 4.0) in docs/research/domain-science-and-prior-art.md
# section 2. It matches ISRO SAC's finding that peak burning moved from ~13:30 IST (2020) to
# ~17:00 IST (2024): Singh et al., Current Science 129(10):921-923 (2025). The dataset covers
# 12:00-18:00 IST only, so burning after 18:00 IST is folded into the 18:00 hour.
BURN_HOUR_SHARE_IST: dict[int, float] = {
    12: 0.002, 13: 0.006, 14: 0.005, 15: 0.025, 16: 0.227, 17: 0.611, 18: 0.124,
}
IST_OFFSET = timedelta(hours=5, minutes=30)


def fire_dry_matter_t(frp_mw: np.ndarray | float) -> np.ndarray | float:
    """Tonnes of residue burnt behind one detection: FRP (MW) x 1800 s x 0.368 kg/MJ."""
    return np.asarray(frp_mw, dtype=float) * FIRE_DURATION_S * FRE_TO_DRY_MATTER_KG_PER_MJ / 1000.0


def scar_dry_matter_t(area_ha: np.ndarray | float) -> np.ndarray | float:
    """Tonnes of residue burnt on a burn scar: area (ha) x 5.5 t/ha (IPCC rice residues)."""
    return np.asarray(area_ha, dtype=float) * RICE_RESIDUE_CONSUMED_T_PER_HA


# --- small geometry helpers -------------------------------------------------------------------------


def wind_speed_from(u: float, v: float) -> tuple[float, float]:
    """(speed m/s, direction the wind blows FROM in degrees, 0 = north, 90 = east)."""
    speed = math.hypot(u, v)
    return speed, (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0


def bilinear(grid: dict[str, Any], values: np.ndarray, lon: float, lat: float) -> float | None:
    """Bilinear interpolation of one (ny, nx) field (or a flat row-major array) at a point.

    None when the point is outside the grid or any of the four corners is missing.
    """
    nx, ny = int(grid["nx"]), int(grid["ny"])
    arr = np.asarray(values, dtype=float).reshape(ny, nx)
    fx = (lon - grid["lon0"]) / grid["dlon"]
    fy = (lat - grid["lat0"]) / grid["dlat"]
    if not (0.0 <= fx <= nx - 1 and 0.0 <= fy <= ny - 1):
        return None
    i, j = min(int(fx), nx - 2), min(int(fy), ny - 2)
    wx, wy = fx - i, fy - j
    corners = arr[j : j + 2, i : i + 2]
    if not np.all(np.isfinite(corners)):
        return None
    top = (1 - wx) * corners[1, 0] + wx * corners[1, 1]
    bottom = (1 - wx) * corners[0, 0] + wx * corners[0, 1]
    return float((1 - wy) * bottom + wy * top)


def _unit_xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Points on the unit sphere, so a KD-tree gives exact great-circle neighbourhoods."""
    la, lo = np.radians(lat), np.radians(lon)
    c = np.cos(la)
    return np.column_stack((c * np.cos(lo), c * np.sin(lo), np.sin(la)))


def _chord(radius_km: float) -> float:
    return 2.0 * math.sin(radius_km / (2.0 * EARTH_RADIUS_M / 1000.0))


# --- vertical structure -------------------------------------------------------------------------------


def pressure_level_heights(
    ps_hpa: np.ndarray, t2m_c: np.ndarray, levels_hpa: Sequence[float]
) -> list[np.ndarray]:
    """Height above ground (m) of each pressure level, from the hypsometric equation.

    z = (R_d T_mean / g) ln(p_s / p), with T_mean the mean temperature of the layer, taken as the
    2 m temperature cooling at the standard lapse rate (closed form below). Using the model's own
    surface pressure puts each level at the right height over every grid node, and a level below
    the ground (p_s < p, over the Himalaya) comes out negative. Checked against the research's
    measured geopotential heights over the plains: ~360 / 590 / 1310 m for 950 / 925 / 850 hPa.
    """
    ps = np.asarray(ps_hpa, dtype=np.float64)
    ts = np.asarray(t2m_c, dtype=np.float64) + 273.15
    out = []
    for p in levels_hpa:
        log_ratio = np.log(ps / p)
        # z = (R/g)(Ts - Gamma z/2) L  =>  z = (R Ts L / g) / (1 + R Gamma L / (2 g))
        z = (R_DRY_AIR * ts * log_ratio / G0) / (1.0 + R_DRY_AIR * LAPSE_RATE * log_ratio / (2.0 * G0))
        out.append(z.astype(np.float32))
    return out


def bl_mean_wind(
    u_levels: Sequence[np.ndarray],
    v_levels: Sequence[np.ndarray],
    z_levels: Sequence[np.ndarray | float],
    blh: np.ndarray,
    floor_m: float = BL_FLOOR_M,
) -> tuple[np.ndarray, np.ndarray]:
    """The mean wind over the boundary layer, (1/h) * integral of V(z) dz from 0 to h.

    `u_levels`/`v_levels` are the winds at increasing heights (10 m first) and `z_levels` their
    heights above ground; h = max(BLH, floor). V(z) is linear between levels, equal to the 10 m wind
    below 10 m and to the top level's wind above it. A level at or below 10 m (underground over high
    terrain) is replaced by the 10 m wind, so an underground value never enters the mean. The
    integral of a piecewise-linear profile is computed exactly, segment by segment.
    """
    h = np.maximum(np.asarray(blh, dtype=np.float64), floor_m)
    z0 = float(np.max(z_levels[0])) if np.ndim(z_levels[0]) else float(z_levels[0])
    shape = h.shape
    heights = [np.broadcast_to(np.asarray(z0, dtype=np.float64), shape)]
    for z in z_levels[1:]:
        heights.append(np.maximum(np.broadcast_to(np.asarray(z, dtype=np.float64), shape), z0))
    result = []
    for levels in (u_levels, v_levels):
        base = np.asarray(levels[0], dtype=np.float64)
        values = [base]
        for k in range(1, len(levels)):
            above = heights[k] > z0
            values.append(np.where(above, np.asarray(levels[k], dtype=np.float64), base))
        # below the first level: constant
        acc = np.minimum(h, heights[0]) * values[0]
        for k in range(len(values) - 1):
            za, zb = heights[k], heights[k + 1]
            top = np.clip(h, za, zb)  # integrate from za up to min(h, zb), nothing if h <= za
            span = top - za
            with np.errstate(invalid="ignore", divide="ignore"):
                frac = np.where(zb > za, span / (zb - za), 0.0)
            v_top = values[k] + frac * (values[k + 1] - values[k])
            acc += span * 0.5 * (values[k] + v_top)
        acc += np.maximum(h - heights[-1], 0.0) * values[-1]  # above the top level: constant
        result.append((acc / h).astype(np.float32))
    return result[0], result[1]


# --- the wind field and the integrator -------------------------------------------------------------


@dataclass
class WindField:
    """Gridded wind (T, ny, nx) on a regular lon/lat grid and an hourly (regular) time axis."""

    lon0: float
    lat0: float
    dlon: float
    dlat: float
    nx: int
    ny: int
    t0: float  # unix seconds of the first time step
    dt: float  # seconds between time steps
    nt: int
    u: np.ndarray
    v: np.ndarray

    @classmethod
    def from_arrays(
        cls, grid: dict[str, Any], times: np.ndarray, u: np.ndarray, v: np.ndarray
    ) -> WindField:
        times = np.asarray(times, dtype=np.float64)
        if times.size < 2 or not np.allclose(np.diff(times), times[1] - times[0]):
            raise ValueError("wind times must be regular and at least two")
        return cls(
            float(grid["lon0"]), float(grid["lat0"]), float(grid["dlon"]), float(grid["dlat"]),
            int(grid["nx"]), int(grid["ny"]), float(times[0]), float(times[1] - times[0]),
            int(times.size), np.ascontiguousarray(u, dtype=np.float32),
            np.ascontiguousarray(v, dtype=np.float32),
        )

    @property
    def t_end(self) -> float:
        return self.t0 + self.dt * (self.nt - 1)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            self.lon0, self.lat0,
            self.lon0 + self.dlon * (self.nx - 1), self.lat0 + self.dlat * (self.ny - 1),
        )

    def covers(self, t: float) -> bool:
        return self.t0 - 1e-6 <= t <= self.t_end + 1e-6

    def inside(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """True where a point lies on the grid (False for NaN, so bad wind data stops a particle)."""
        west, south, east, north = self.bbox
        return (lat >= south) & (lat <= north) & (lon >= west) & (lon <= east)

    def __call__(self, lat: np.ndarray, lon: np.ndarray, t: float) -> tuple[np.ndarray, np.ndarray]:
        fy = np.clip((lat - self.lat0) / self.dlat, 0.0, self.ny - 1 - 1e-9)
        fx = np.clip((lon - self.lon0) / self.dlon, 0.0, self.nx - 1 - 1e-9)
        j, i = fy.astype(np.intp), fx.astype(np.intp)
        wy, wx = fy - j, fx - i
        idx = j * self.nx + i
        ft = min(max((t - self.t0) / self.dt, 0.0), self.nt - 1 - 1e-9)
        k = int(ft)
        wt = ft - k
        nx = self.nx

        def bil(frame: np.ndarray) -> np.ndarray:
            flat = frame.ravel()
            south = (1 - wx) * flat[idx] + wx * flat[idx + 1]
            north = (1 - wx) * flat[idx + nx] + wx * flat[idx + nx + 1]
            return (1 - wy) * south + wy * north

        u = (1 - wt) * bil(self.u[k]) + wt * bil(self.u[k + 1])
        v = (1 - wt) * bil(self.v[k]) + wt * bil(self.v[k + 1])
        return u, v


def _move(lat: np.ndarray, lon: np.ndarray, u: np.ndarray, v: np.ndarray, dt: float):
    new_lat = lat + v * dt / EARTH_RADIUS_M * DEG
    new_lon = lon + u * dt / (EARTH_RADIUS_M * np.cos(np.radians(lat))) * DEG
    return new_lat, new_lon


class Observer(Protocol):
    def record(self, r: int, t: float, lat: np.ndarray, lon: np.ndarray, active: np.ndarray) -> None: ...


@dataclass
class Integration:
    record_times: np.ndarray  # unix seconds of each record
    exited: np.ndarray  # particles that left the grid
    stopped_early: str | None  # why the run ended before t_end, if it did


def n_records(t_start: float, t_end: float, record_s: float = RECORD_S) -> int:
    return int(round(abs(t_end - t_start) / record_s)) + 1


def integrate(
    wind: WindField,
    lat: np.ndarray,
    lon: np.ndarray,
    t_release: np.ndarray,
    *,
    t_start: float,
    t_end: float,
    dt_s: float = DT_S,
    k_h: float = K_H,
    record_s: float = RECORD_S,
    seed: int = 0,
    observers: Iterable[Observer] = (),
) -> Integration:
    """Move particles from t_start to t_end (backwards when t_end < t_start).

    A particle starts moving once the clock passes its release time (forward) or reaches it
    (backward); until then it waits at its release point and is not "active". Every `record_s`
    seconds each observer gets the positions and the active mask. `record_s` must be a multiple
    of `dt_s`. Deterministic for a given seed.
    """
    backward = t_end < t_start
    step = -abs(dt_s) if backward else abs(dt_s)
    every = int(round(record_s / abs(dt_s)))
    if every < 1 or abs(every * abs(dt_s) - record_s) > 1e-6:
        raise ValueError("record_s must be a whole multiple of dt_s")
    n_steps = int(round(abs(t_end - t_start) / abs(dt_s)))
    lat = np.array(lat, dtype=np.float64)
    lon = np.array(lon, dtype=np.float64)
    t_release = np.asarray(t_release, dtype=np.float64)
    alive = np.ones(lat.size, dtype=bool)
    sigma = math.sqrt(2.0 * k_h * abs(dt_s))
    rng = np.random.default_rng(seed)
    observers = list(observers)
    record_times: list[float] = []
    stopped: str | None = None
    t = float(t_start)
    for n in range(n_steps + 1):
        released = t_release >= t - 1e-6 if backward else t_release <= t + 1e-6
        if n % every == 0:
            active = released & alive
            for obs in observers:
                obs.record(len(record_times), t, lat, lon, active)
            record_times.append(t)
        if n == n_steps:
            break
        if not (wind.covers(t) and wind.covers(t + step)):
            edge = datetime.fromtimestamp(wind.t0 if backward else wind.t_end, UTC)
            stopped = f"wind data {'starts' if backward else 'ends'} at {edge:%Y-%m-%d %H:%MZ}"
            break
        idx = np.flatnonzero(released & alive)
        if idx.size:
            a, b = lat[idx], lon[idx]
            u1, v1 = wind(a, b, t)
            a1, b1 = _move(a, b, u1, v1, step)
            u2, v2 = wind(a1, b1, t + step)
            a2, b2 = _move(a, b, 0.5 * (u1 + u2), 0.5 * (v1 + v2), step)
            if k_h > 0:
                a2 = a2 + sigma * rng.standard_normal(idx.size) / EARTH_RADIUS_M * DEG
                b2 = b2 + sigma * rng.standard_normal(idx.size) / (
                    EARTH_RADIUS_M * np.cos(np.radians(a2))
                ) * DEG
            ok = wind.inside(a2, b2)
            lat[idx[ok]], lon[idx[ok]] = a2[ok], b2[ok]
            alive[idx[~ok]] = False
        t += step
    return Integration(np.array(record_times), ~alive, stopped)


# --- releases ---------------------------------------------------------------------------------------


def scar_release_hours(
    burn_after: datetime, burn_before: datetime, t_lo: float, t_hi: float
) -> tuple[np.ndarray, np.ndarray]:
    """When a burn scar's smoke is released: the IST burning hours between its two clear passes.

    The scar burnt after the clear pass `burn_after` and before the pass `burn_before` that shows
    it (Sentinel-2 passes Punjab around 11:10 IST, before the afternoon burning), on a day we do not
    know. So every burning hour in between gets the share BURN_HOUR_SHARE_IST gives it, and the
    shares are scaled to sum to 1 over the window: each day is equally likely. Returns (hour start
    in unix seconds, share of the scar's total) for the hours that also fall in [t_lo, t_hi).
    """
    start, end = burn_after.timestamp(), burn_before.timestamp()
    first_day = (burn_after + IST_OFFSET).date()
    last_day = (burn_before + IST_OFFSET).date()
    starts, shares = [], []
    day = first_day
    while day <= last_day:
        for hour, share in BURN_HOUR_SHARE_IST.items():
            ts = (datetime(day.year, day.month, day.day, hour, tzinfo=UTC) - IST_OFFSET).timestamp()
            if start <= ts and ts + 3600.0 <= end:
                starts.append(ts)
                shares.append(share)
        day += timedelta(days=1)
    if not starts:  # a window shorter than one burning hour: release it all at its middle
        starts, shares = [0.5 * (start + end) - 1800.0], [1.0]
    hours = np.array(starts, dtype=np.float64)
    weights = np.array(shares, dtype=np.float64)
    weights /= weights.sum()
    keep = (hours >= t_lo) & (hours < t_hi)
    return hours[keep], weights[keep]


# --- district labels ---------------------------------------------------------------------------------


@dataclass
class LabelGrid:
    """District (and state) of any point, from boundary polygons burnt into a fine raster.

    Label 0 means "outside every loaded boundary". Looking up a million particle positions this
    way takes milliseconds, where a point-in-polygon test per position would take minutes.
    """

    west: float
    north: float
    step: float
    nx: int
    ny: int
    ids: np.ndarray  # (ny, nx) int32, row 0 = north
    labels: list[dict[str, str | None]]  # index -> {"state", "district"}

    def lookup(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        row = np.floor((self.north - np.asarray(lat)) / self.step)
        col = np.floor((np.asarray(lon) - self.west) / self.step)
        ok = (row >= 0) & (row < self.ny) & (col >= 0) & (col < self.nx)
        out = np.zeros(np.shape(lat), dtype=np.int32)
        out[ok] = self.ids[row[ok].astype(np.intp), col[ok].astype(np.intp)]
        return out

    def label_mask(self, predicate) -> np.ndarray:
        """Boolean per label index, e.g. which labels are in the burn belt."""
        return np.array([bool(predicate(lab)) for lab in self.labels], dtype=bool)


def build_label_grid(
    states: Sequence[tuple[Any, str]],
    districts: Sequence[tuple[Any, str | None, str]],
    bbox: tuple[float, float, float, float],
    step: float = 0.01,
) -> LabelGrid:
    """Rasterise state polygons, then district polygons on top (shapely geometries, lon/lat)."""
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    west, south, east, north = bbox
    nx = int(math.ceil((east - west) / step))
    ny = int(math.ceil((north - south) / step))
    labels: list[dict[str, str | None]] = [{"state": None, "district": None}]
    shapes = []
    for geom, state in states:
        labels.append({"state": state, "district": None})
        shapes.append((geom, len(labels) - 1))
    for geom, state, district in districts:
        labels.append({"state": state, "district": district})
        shapes.append((geom, len(labels) - 1))
    ids = np.zeros((ny, nx), dtype=np.int32)
    if shapes:
        ids = rasterize(
            shapes, out_shape=(ny, nx), transform=from_origin(west, north, step, step),
            fill=0, dtype="int32",
        )
    return LabelGrid(west, north, step, nx, ny, ids, labels)


# --- forward runs: what reaches each city -------------------------------------------------------------


class ReceptorSeries:
    """Smoke index at each receptor at every record: sum of particle weights within the radius.

    Also split by source kind (FIRMS fire / candidate unseen burn scar) and, summed over the run,
    by the label (state/district) the smoke was emitted from.
    """

    def __init__(
        self,
        rec_lat: np.ndarray,
        rec_lon: np.ndarray,
        weight: np.ndarray,
        kind: np.ndarray,
        origin_label: np.ndarray,
        n_labels: int,
        n_records: int,
        radius_km: float = RECEPTOR_RADIUS_KM,
    ) -> None:
        self.rlat = np.asarray(rec_lat, dtype=np.float64)
        self.rlon = np.asarray(rec_lon, dtype=np.float64)
        self.coslat = np.cos(np.radians(self.rlat))
        self.weight = np.asarray(weight, dtype=np.float64)
        self.kind = np.asarray(kind, dtype=np.int8)
        self.label = np.asarray(origin_label, dtype=np.int32)
        self.r2 = radius_km**2
        c = self.rlat.size
        self.total = np.zeros((c, n_records))
        self.by_kind = np.zeros((c, 2, n_records))
        self.count = np.zeros((c, n_records), dtype=np.int32)
        self.by_label = np.zeros((c, max(n_labels, 1)))

    def record(self, r: int, t: float, lat: np.ndarray, lon: np.ndarray, active: np.ndarray) -> None:
        idx = np.flatnonzero(active)
        if not idx.size or r >= self.total.shape[1]:
            return
        dy = (lat[idx][None, :] - self.rlat[:, None]) * KM_PER_DEG
        dx = (lon[idx][None, :] - self.rlon[:, None]) * KM_PER_DEG * self.coslat[:, None]
        within = dx * dx + dy * dy <= self.r2
        if not within.any():
            return
        w = self.weight[idx]
        self.total[:, r] = within @ w
        kinds = self.kind[idx]
        for k in (0, 1):
            self.by_kind[:, k, r] = within @ np.where(kinds == k, w, 0.0)
        self.count[:, r] = within.sum(axis=1)
        cs, ps = np.nonzero(within)
        np.add.at(self.by_label, (cs, self.label[idx][ps]), w[ps])


class TrailRecorder:
    """Positions of a chosen subset of particles at every record, for the browser's trails."""

    def __init__(self, particle_idx: np.ndarray, n_records: int) -> None:
        self.idx = np.asarray(particle_idx, dtype=np.intp)
        self.lat = np.full((n_records, self.idx.size), np.nan, dtype=np.float64)
        self.lon = np.full((n_records, self.idx.size), np.nan, dtype=np.float64)

    def record(self, r: int, t: float, lat: np.ndarray, lon: np.ndarray, active: np.ndarray) -> None:
        if r >= self.lat.shape[0] or not self.idx.size:
            return
        on = active[self.idx]
        self.lat[r, on] = lat[self.idx][on]
        self.lon[r, on] = lon[self.idx][on]


def trails_payload(
    recorder: TrailRecorder,
    record_times: np.ndarray,
    meta: Sequence[dict[str, Any]],
    *,
    chronological: bool = True,
    decimals: int = 4,
    min_points: int = 2,
) -> list[dict[str, Any]]:
    """Recorded trails as the contract's {id, path:[[lon,lat]], times:[unix_s], ...} objects.

    Only the points where the particle was active are kept (after release, before leaving the
    grid). Back-trajectory records run backwards in time; `chronological` reorders every trail
    oldest first, so a back trail ends at the city.
    """
    times = np.asarray(record_times[: recorder.lat.shape[0]], dtype=np.float64)
    order = np.argsort(times) if chronological else np.arange(times.size)
    out = []
    for k in range(recorder.idx.size):
        la, lo = recorder.lat[order, k], recorder.lon[order, k]
        ok = np.isfinite(la) & np.isfinite(lo)
        if ok.sum() < min_points:
            continue
        path = np.column_stack((np.round(lo[ok], decimals), np.round(la[ok], decimals))).tolist()
        out.append({**meta[k], "path": path, "times": times[order][ok].astype(np.int64).tolist()})
    return out


def hourly_from_records(values: np.ndarray, record_times: np.ndarray, record_s: float = RECORD_S):
    """Centred 1-hour means of 20-minute records, at the records that fall on the hour."""
    times = np.asarray(record_times, dtype=np.float64)
    per_hour = int(round(3600.0 / record_s))
    on_hour = np.flatnonzero(np.isclose(np.mod(times, 3600.0), 0.0))
    half = per_hour // 2
    out = []
    for r in on_hour:
        lo, hi = max(0, r - half), min(values.shape[-1], r + half + 1)
        out.append(values[..., lo:hi].mean(axis=-1))
    stacked = np.stack(out, axis=-1) if out else np.zeros(values.shape[:-1] + (0,))
    return times[on_hour], stacked


def peak_and_arrival(times: np.ndarray, series: np.ndarray, arrival_fraction: float = 0.1):
    """Peak {t, value} and the first time the series reaches `arrival_fraction` of its peak."""
    if series.size == 0 or not np.isfinite(series).any() or float(np.nanmax(series)) <= 0.0:
        return None, None
    k = int(np.nanargmax(series))
    peak = float(series[k])
    first = int(np.flatnonzero(series >= arrival_fraction * peak)[0])
    return (times[k], peak), times[first]


# --- back runs: where a city's air came from ---------------------------------------------------------


@dataclass
class Sources:
    """Emission events for the back-run exposure: where, which record, how many tonnes."""

    lat: np.ndarray
    lon: np.ndarray
    record: np.ndarray  # record index (int) the emission falls in
    weight: np.ndarray  # tonnes of dry matter emitted in that record
    label: np.ndarray  # LabelGrid index of the emission point
    kind: np.ndarray  # 0 = FIRMS fire, 1 = candidate unseen burn scar

    @classmethod
    def empty(cls) -> Sources:
        z = np.zeros(0)
        zi = np.zeros(0, dtype=np.int32)
        return cls(z, z, zi, z, zi, zi.astype(np.int8))

    def __len__(self) -> int:
        return int(self.lat.size)


class BackSummary:
    """Per city (particle group): time over the burn belt, exposure to fires, districts crossed,
    and the hourly centroid and spread of the particle cloud (for the uncertainty cone).

    Exposure: by reciprocity of transport, the forward smoke index at the city equals, for each
    emission of w tonnes at point x and time t, w times the fraction of the city's back particles
    found within the receptor radius of x at time t. Summed over every fire and scar, that is the
    `fire_exposure_index`, in the same units as the forward smoke index.
    """

    WINDOWS_H = (24, 48, 72)

    def __init__(
        self,
        group: np.ndarray,
        n_groups: int,
        labels: LabelGrid | None,
        belt_mask: np.ndarray | None,
        sources: Sources,
        t_release: float,
        radius_km: float = RECEPTOR_RADIUS_KM,
        record_s: float = RECORD_S,
    ) -> None:
        self.group = np.asarray(group, dtype=np.intp)
        self.n_groups = n_groups
        self.n_per_group = np.bincount(self.group, minlength=n_groups).astype(np.float64)
        self.labels = labels
        self.belt_mask = belt_mask
        n_labels = len(labels.labels) if labels is not None else 1
        self.sources = sources
        self.t_release = t_release
        self.chord = _chord(radius_km)
        self.record_s = record_s
        w = len(self.WINDOWS_H)
        self.known = np.zeros((n_groups, w))  # particle-records inside the grid
        self.belt = np.zeros((n_groups, w))  # ... of which over the burn belt
        self.total = np.zeros((n_groups, w))  # all particle-records, incl. those that left
        self.residence = np.zeros((n_groups, n_labels))  # particle-records per label, 72 h
        self.exposure = np.zeros(n_groups)
        self.exposure_by_kind = np.zeros((n_groups, 2))
        self.exposure_by_label = np.zeros((n_groups, n_labels))
        self.hours: list[float] = []
        self.centroid: list[np.ndarray] = []  # (n_groups, 2) lon, lat per hour
        self.spread_km: list[np.ndarray] = []  # (n_groups,) 80th percentile distance per hour
        self._by_record: dict[int, np.ndarray] = {}
        if len(sources):
            for r in np.unique(sources.record):
                self._by_record[int(r)] = np.flatnonzero(sources.record == r)

    def record(self, r: int, t: float, lat: np.ndarray, lon: np.ndarray, active: np.ndarray) -> None:
        elapsed_h = abs(self.t_release - t) / 3600.0
        idx = np.flatnonzero(active)
        g = self.group[idx]
        counts = np.bincount(g, minlength=self.n_groups).astype(np.float64)
        if self.labels is not None and idx.size:
            lab = self.labels.lookup(lat[idx], lon[idx])
            np.add.at(self.residence, (g, lab), 1.0)
            belt = (
                np.bincount(g[self.belt_mask[lab]], minlength=self.n_groups)
                if self.belt_mask is not None
                else np.zeros(self.n_groups)
            )
        else:
            belt = np.zeros(self.n_groups)
        for k, window in enumerate(self.WINDOWS_H):
            if elapsed_h <= window + 1e-6 and r > 0:  # record 0 is the release itself
                self.known[:, k] += counts
                self.belt[:, k] += belt
                self.total[:, k] += self.n_per_group
        if r in self._by_record and idx.size:
            self._expose(self._by_record[r], lat[idx], lon[idx], g)
        if r % int(round(3600.0 / self.record_s)) == 0:
            self._centroids(t, lat[idx], lon[idx], g)

    def _expose(self, src_idx: np.ndarray, plat: np.ndarray, plon: np.ndarray, g: np.ndarray) -> None:
        # Imported here so the web API (met/smoke routers import this module) needs no scipy.
        from scipy.spatial import cKDTree

        tree = cKDTree(_unit_xyz(plat, plon))
        s = self.sources
        hits = tree.query_ball_point(_unit_xyz(s.lat[src_idx], s.lon[src_idx]), r=self.chord)
        for j, near in zip(src_idx, hits, strict=True):
            if not near:
                continue
            frac = np.bincount(g[near], minlength=self.n_groups) / np.maximum(self.n_per_group, 1.0)
            contrib = s.weight[j] * frac
            self.exposure += contrib
            self.exposure_by_kind[:, int(s.kind[j])] += contrib
            self.exposure_by_label[:, int(s.label[j])] += contrib

    def _centroids(self, t: float, plat: np.ndarray, plon: np.ndarray, g: np.ndarray) -> None:
        cen = np.full((self.n_groups, 2), np.nan)
        spread = np.full(self.n_groups, np.nan)
        for k in range(self.n_groups):
            m = g == k
            if m.sum() < 5:
                continue
            la, lo = plat[m], plon[m]
            clat = float(np.mean(la))
            clon = float(np.mean(lo))
            d = np.hypot((la - clat) * KM_PER_DEG, (lo - clon) * KM_PER_DEG * math.cos(math.radians(clat)))
            cen[k] = (clon, clat)
            spread[k] = float(np.percentile(d, 80))
        self.hours.append(t)
        self.centroid.append(cen)
        self.spread_km.append(spread)

    # results for one group ---------------------------------------------------------------------

    def belt_share(self, k: int) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for w, window in enumerate(self.WINDOWS_H):
            known = self.known[k, w]
            share = self.belt[k, w] / known if known > 0 and self.belt_mask is not None else None
            out[f"h{window}"] = None if share is None else round(float(share), 4)
        return out

    def left_domain_share(self, k: int) -> float | None:
        total = self.total[k, -1]
        return None if total <= 0 else round(float(1.0 - self.known[k, -1] / total), 4)

    def path(self, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Hourly (times, centroid lon/lat (H, 2), 80th percentile spread km), oldest first."""
        times = np.array(self.hours)
        cen = np.array([c[k] for c in self.centroid]) if self.centroid else np.zeros((0, 2))
        spr = np.array([s[k] for s in self.spread_km]) if self.spread_km else np.zeros(0)
        order = np.argsort(times)
        return times[order], cen[order], spr[order]


def top_shares(weights: np.ndarray, labels: list[dict[str, str | None]], key: str, n: int = 5):
    """The `n` labels with the largest share of `weights`, aggregated by district or state name.

    Shares are of the whole, label 0 ("outside every loaded boundary") included, so they never
    overstate a district just because some of the air was over unmapped land.
    """
    total = float(np.sum(weights))
    if total <= 0:
        return []
    agg: dict[tuple[str, str | None], float] = {}
    for k in range(1, weights.size):
        if weights[k] <= 0:
            continue
        lab = labels[k]
        name = lab.get(key)
        if not name:
            continue
        ident = (name, lab.get("state")) if key == "district" else (name, None)
        agg[ident] = agg.get(ident, 0.0) + float(weights[k])
    ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:n]
    if key == "district":
        return [{"district": d, "state": s, "share": round(v / total, 4)} for (d, s), v in ranked]
    return [{"state": st, "share": round(v / total, 4)} for (st, _), v in ranked]


def cone_geometry(
    paths: Sequence[tuple[np.ndarray, np.ndarray]], decimals: int = 3, tolerance_deg: float = 0.02
) -> dict[str, Any] | None:
    """GeoJSON geometry of the uncertainty cone: the union of circles (radius = 80th percentile
    spread) around every member's hourly centroid. Two members that disagree give a wide cone."""
    import shapely
    from shapely import affinity
    from shapely.geometry import Point, mapping

    circles = []
    for centroids, spread in paths:
        for (lon, lat), radius in zip(centroids, spread, strict=False):
            if not (np.isfinite(lon) and np.isfinite(lat) and np.isfinite(radius)):
                continue
            radius = max(float(radius), 2.0)
            unit = Point(lon, lat).buffer(1.0, quad_segs=6)
            circles.append(
                affinity.scale(
                    unit,
                    xfact=radius / (KM_PER_DEG * math.cos(math.radians(lat))),
                    yfact=radius / KM_PER_DEG,
                    origin=(lon, lat),
                )
            )
    if not circles:
        return None
    hull = shapely.union_all(circles).simplify(tolerance_deg)
    hull = shapely.set_precision(hull, 10.0**-decimals)
    return mapping(hull) if not hull.is_empty else None


# Verdict thresholds. They are judgement calls, stated so anyone can check a verdict by hand:
# - BELT: share of the last 48 h the city's air spent over Punjab + Haryana.
# - EXPOSURE: tonnes of burnt residue whose smoke the air picked up (fire_exposure_index). 5 t is
#   about the smoke of three typical VIIRS detections (median FRP ~5 MW -> 3.3 t each).
BELT_LIKELY = 0.25
BELT_UNLIKELY = 0.05
EXPOSURE_LIKELY_T = 5.0
EXPOSURE_UNLIKELY_T = 0.5


def verdict_hint(
    belt_h48: Sequence[float | None], exposure: Sequence[float | None]
) -> tuple[str | None, str]:
    """'stubble_likely' / 'mixed' / 'stubble_unlikely' from every ensemble member, with a reason.

    - likely: every member's air picked up smoke from fires as they burned (exposure high), and
      (when boundaries are loaded) crossed the burn belt for a good part of the last 48 h;
    - unlikely: every member's air stayed away from the burn belt, or (without boundaries) passed
      no fire as it burned;
    - mixed: anything else. That includes air that crossed the burn belt but passed no *detected*
      fire: polar satellites miss the evening burning, so "no detection" is not "no smoke".
    None when there is nothing to judge from (no fire data and no boundaries).
    """
    belts = [b for b in belt_h48 if b is not None]
    exps = [e for e in exposure if e is not None]
    if not belts and not exps:
        return None, "no fire detections or boundaries to judge from"
    belt_hi = all(b >= BELT_LIKELY for b in belts) if belts else True
    belt_lo = bool(belts) and all(b < BELT_UNLIKELY for b in belts)
    exp_hi = bool(exps) and all(e >= EXPOSURE_LIKELY_T for e in exps)
    exp_lo = (not exps) or all(e < EXPOSURE_UNLIKELY_T for e in exps)
    if exp_hi and belt_hi:
        return "stubble_likely", (
            "every model run carried the air over the burn belt and past fires as they burned"
        )
    if belt_lo and exp_lo:
        return "stubble_unlikely", "in every model run the air stayed away from the burn belt"
    if not belts and exp_lo:
        return "stubble_unlikely", "in no model run did the air pass a fire as it burned"
    if belt_hi and exp_lo:
        return "mixed", (
            "the air crossed the burn belt but passed no detected fire; satellites miss the "
            "evening burning, so this cannot rule smoke out"
        )
    return "mixed", "the model runs disagree, or the air passed only a little burning"


def round_or_none(value: float | None, digits: int = 3) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


# --- validation statistics ---------------------------------------------------------------------------


def point_series(grid: dict[str, Any], values: np.ndarray, lon: float, lat: float) -> np.ndarray:
    """Bilinear interpolation of a (T, ny, nx) field at one point, for every time step at once."""
    nx, ny = int(grid["nx"]), int(grid["ny"])
    fx = (lon - grid["lon0"]) / grid["dlon"]
    fy = (lat - grid["lat0"]) / grid["dlat"]
    if not (0.0 <= fx <= nx - 1 and 0.0 <= fy <= ny - 1):
        return np.full(values.shape[0], np.nan)
    i, j = min(int(fx), nx - 2), min(int(fy), ny - 2)
    wx, wy = fx - i, fy - j
    v = values.astype(np.float64)
    south = (1 - wx) * v[:, j, i] + wx * v[:, j, i + 1]
    north = (1 - wx) * v[:, j + 1, i] + wx * v[:, j + 1, i + 1]
    return (1 - wy) * south + wy * north


def _lag1(x: np.ndarray) -> float:
    if x.size < 3 or np.std(x) == 0:
        return 0.0
    return float(np.corrcoef(x[:-1], x[1:])[0, 1])


def _residual(y: np.ndarray, z: np.ndarray) -> np.ndarray:
    design = np.column_stack((np.ones_like(z), z))
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    return y - design @ coef


def lagged_correlations(
    model: np.ndarray, obs: np.ndarray, control: np.ndarray | None = None, max_lag: int = 12,
    min_n: int = 12,
) -> dict[str, Any]:
    """How well an hourly model series tracks an hourly observed one, model leading by 0..max_lag h.

    For each lag: Pearson r, Spearman rho, n, and (with `control`) the partial r once both series
    are regressed on the control (here log ventilation, since stagnant air alone raises PM2.5). At
    lag 0 also the effective number of independent hours (Bretherton et al. 1999,
    n_eff = n (1 - r1x r1y) / (1 + r1x r1y)), because neighbouring hours are not independent.
    """
    from scipy.stats import pearsonr, spearmanr

    by_lag = []
    for lag in range(max_lag + 1):
        x = model[: model.size - lag] if lag else model
        y = obs[lag:]
        z = None if control is None else (control[: control.size - lag] if lag else control)
        ok = np.isfinite(x) & np.isfinite(y) & (np.isfinite(z) if z is not None else True)
        n = int(ok.sum())
        if n < min_n or np.std(x[ok]) == 0 or np.std(y[ok]) == 0:
            by_lag.append({"lag_h": lag, "n": n, "r": None, "rho": None, "partial_r": None})
            continue
        entry: dict[str, Any] = {
            "lag_h": lag, "n": n,
            "r": round(float(pearsonr(x[ok], y[ok])[0]), 3),
            "rho": round(float(spearmanr(x[ok], y[ok])[0]), 3),
            "partial_r": None,
        }
        if z is not None and np.std(z[ok]) > 0:
            rx, ry = _residual(x[ok], z[ok]), _residual(y[ok], z[ok])
            if np.std(rx) > 0 and np.std(ry) > 0:
                entry["partial_r"] = round(float(np.corrcoef(rx, ry)[0, 1]), 3)
        if lag == 0:
            a, b = _lag1(x[ok]), _lag1(y[ok])
            entry["n_eff"] = round(n * (1 - a * b) / (1 + a * b), 1) if (1 + a * b) > 0 else None
        by_lag.append(entry)
    scored = [e for e in by_lag if e["r"] is not None]
    best = max(scored, key=lambda e: e["r"]) if scored else None
    return {"lag0": by_lag[0], "best_lag": best, "by_lag": by_lag}


def describe_r(r: float | None, n_eff: float | None) -> str:
    """Plain words for a correlation, honest about small samples."""
    if r is None:
        return "not enough overlapping hours"
    size = abs(r)
    words = "no" if size < 0.1 else "a weak" if size < 0.3 else "a moderate" if size < 0.6 else "a strong"
    sign = "" if size < 0.1 else (" positive" if r > 0 else " negative")
    text = f"{words}{sign} correlation (r = {r:.2f})"
    if n_eff is not None and n_eff < 20:
        text += f"; only ~{n_eff:.0f} independent hours, so not statistically robust"
    return text
