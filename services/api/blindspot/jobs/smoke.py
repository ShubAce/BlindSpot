"""Job `smoke`: where the smoke of recent fires is going, and where each city's air came from.

Runs after every successful `met` (architecture.md section 6):
1. Wind: the newest boundary-layer mean wind of each ensemble member (best_match, gfs_seamless).
   SYNTHESIS: two models and a cone, never one path as truth.
2. Forward run: particles from every FIRMS detection of the last 24 h in the wind grid (released at
   detection time, weighted by fire radiative power) and from candidate unseen burn scars
   (Sentinel-2 scars no FIRMS detection explains; weighted by area, released across their burn
   window with the evening-peaked profile ISRO SAC found), followed until 48 h after now. A city's
   smoke index = tonnes of burnt residue whose smoke is within 30 km of it.
3. Back runs: 500 particles over each receptor city now, followed 72 h back: share of that time
   over the burn belt (Punjab + Haryana), exposure to fires as they burned, districts crossed,
   the two-model cone and a verdict hint.
4. One transaction writes the forward row and one back row per city; older rows lose their trails.

Inputs come from tables other modules fill (fire_detection, burn_cluster, boundary, place). Each is
read only if it exists; a missing input is reported in the run, never replaced by a made-up one.
"""

from __future__ import annotations

import logging
import math
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import orjson
import shapely
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import trajectory as tj
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX
from blindspot.http import FetchError
from blindspot.jobs import job
from blindspot.models.met import MetRun, latest_runs, load_level, row_at
from blindspot.models.smoke import SmokeRun
from blindspot.provenance import SourceRef, latest_refs, ref_for_fetch, utcnow
from blindspot.sources.openmeteo import geocode_city

log = logging.getLogger(__name__)

MEMBER_ORDER = ("best_match", "gfs_seamless")  # the first one present is the primary
LOOKBACK_H = 24  # fires and scar hours released in the forward run: the last 24 h
HORIZON_H = 48  # forward run continues 48 h past now
BACK_H = 72  # back runs go 72 h back
FWD_PARTICLES = 24_000  # per member; split equally between sources
PER_SOURCE_MIN, PER_SOURCE_MAX = 3, 40
BACK_PER_CITY = 500  # per member, released over the city's 30 km disk
FWD_TRAILS_PER_MEMBER = 1000  # contract: at most 2,000 trails in a browser payload
BACK_TRAILS_PER_MEMBER = 100
TRAIL_GZIP_BUDGET = 2_600_000  # bytes; the browser limit is 3 MB gzipped
MAX_RECEPTORS = 24
MAX_SCAR_SOURCES = 3000  # beyond this, scars are pooled into 0.05 deg cells
KEEP_TRAIL_RUNS = 2  # runs per (kind, city) that keep their trails
KEEP_DAYS = 14  # summaries and series are kept this long
BELT_STATES = {"punjab", "haryana"}  # the burn belt, matched on Indian state polygons only
# Receptor names from docs/research/geography-and-basemaps.md section 7 ("config, not data").
# Used only when the boundaries module has loaded no `place` of kind 'city'; their coordinates then
# come live from GeoNames through Open-Meteo's geocoding API, never from this file.
RECEPTOR_NAMES = (
    "Delhi", "Gurugram", "Noida", "Faridabad", "Ghaziabad", "Chandigarh", "Ludhiana", "Amritsar",
    "Jalandhar", "Patiala", "Bathinda", "Hisar", "Panipat", "Karnal", "Sonipat", "Rohtak",
    "Jaipur", "Lucknow",
)
_GEOCODED: dict[str, dict[str, Any] | None] = {}  # per-process cache of geocoding answers


# --- inputs ------------------------------------------------------------------------------------------


@dataclass
class Member:
    model: str
    run: MetRun
    wind: tj.WindField
    blh_now: np.ndarray | None = None  # boundary-layer height grid at the hour nearest run_at
    blh_valid_at: datetime | None = None


@dataclass
class Receptor:
    name: str
    lat: float
    lon: float
    district: str | None
    state: str | None
    source: str  # "place" (boundaries module) or "geocoded" (Open-Meteo / GeoNames)
    fetch_id: int | None = None


@dataclass
class Fires:
    """FIRMS detections in a time window (one row each)."""

    id: np.ndarray
    t: np.ndarray  # acquisition time, unix s
    lat: np.ndarray
    lon: np.ndarray
    frp: np.ndarray
    dm: np.ndarray  # tonnes of dry matter burnt (tj.fire_dry_matter_t)
    state: list[str | None]
    district: list[str | None]
    fetch_ids: list[int]
    stats: dict[str, Any] = field(default_factory=dict)

    def subset(self, mask: np.ndarray) -> Fires:
        idx = np.flatnonzero(mask)
        return Fires(
            self.id[idx], self.t[idx], self.lat[idx], self.lon[idx], self.frp[idx], self.dm[idx],
            [self.state[i] for i in idx], [self.district[i] for i in idx], self.fetch_ids, self.stats,
        )

    def __len__(self) -> int:
        return int(self.id.size)


@dataclass
class Scars:
    """Candidate unseen burn scars (pooled into cells when there are very many)."""

    id: list[int]  # burn_cluster id (the largest one in a pooled cell)
    n_clusters: list[int]
    lat: np.ndarray
    lon: np.ndarray
    area_ha: np.ndarray
    dm: np.ndarray  # tonnes of dry matter (tj.scar_dry_matter_t)
    after: list[datetime]
    before: list[datetime]
    state: list[str | None]
    district: list[str | None]
    fetch_ids: list[int]
    stats: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.id)


def _table_exists(session: Session, table: str) -> bool:
    return session.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar()


def _columns(session: Session, table: str) -> set[str]:
    rows = session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    ).scalars()
    return set(rows)


def load_members(session: Session, at: datetime) -> tuple[list[Member], list[str]]:
    """The newest wind grid of each ensemble member, as WindFields (primary first).

    When the met job stored that run in this process (smoke is chained after met), the grids come
    from its memory (jobs.met.stored_level), not back from the database: the same values, ~1.5 MB
    less database egress per member."""
    from blindspot.jobs import met as met_job

    runs = latest_runs(session, MEMBER_ORDER)
    members, notes = [], []
    for model in MEMBER_ORDER:
        run = runs.get(model)
        if run is None:
            notes.append(f"{model}: no wind grid stored yet")
            continue
        held = met_job.stored_level(run.id)
        times, arrays = held if held is not None else load_level(session, run, "bl", ("u", "v"))
        if times.size < 2:
            notes.append(f"{model}: run {run.id} has no boundary-layer wind rows")
            continue
        wind = tj.WindField.from_arrays(run.grid, times, arrays["u"], arrays["v"])
        member = Member(model, run, wind)
        surface = met_job.stored_surface_at(run.id, at) if held is not None else None
        surface = surface or row_at(session, run, "10m", at)
        if surface is not None and surface.blh is not None:
            member.blh_now = np.asarray(surface.blh, dtype=np.float64)
            member.blh_valid_at = surface.valid_at
        members.append(member)
    session.expunge_all()
    return members, notes


def load_receptors(session: Session, bbox: tuple[float, float, float, float]) -> tuple[list[Receptor], str]:
    """Receptor cities inside the wind grid: `place` rows of kind 'city' when the boundaries module
    has loaded them (AOI first, then by population), else the research list geocoded live."""
    west, south, east, north = bbox
    if _table_exists(session, "place"):
        rows = session.execute(
            text(
                """
                SELECT DISTINCT ON (lower(name)) name, lat, lon, district, state, population, fetch_id,
                       (lon BETWEEN :aw AND :ae AND lat BETWEEN :as_ AND :an) AS in_aoi
                FROM place
                WHERE kind = 'city' AND lon BETWEEN :w AND :e AND lat BETWEEN :s AND :n
                ORDER BY lower(name), population DESC NULLS LAST
                """
            ),
            {"w": west, "e": east, "s": south, "n": north, "aw": AOI_BBOX[0], "ae": AOI_BBOX[2],
             "as_": AOI_BBOX[1], "an": AOI_BBOX[3]},
        ).mappings().all()
        if rows:
            ranked = sorted(rows, key=lambda r: (not r["in_aoi"], -(r["population"] or 0)))
            out = [
                Receptor(r["name"], float(r["lat"]), float(r["lon"]), r["district"], r["state"],
                         "place", r["fetch_id"])
                for r in ranked[:MAX_RECEPTORS]
            ]
            return out, f"{len(out)} cities from the place table (kind 'city')"
    out, failed = [], []
    for name in RECEPTOR_NAMES:
        if name not in _GEOCODED:
            try:
                _GEOCODED[name] = geocode_city(name)
            except FetchError as exc:
                failed.append(f"{name}: {exc}")
                continue
        hit = _GEOCODED[name]
        if hit and west <= hit["lon"] <= east and south <= hit["lat"] <= north:
            out.append(Receptor(name, hit["lat"], hit["lon"], hit.get("district"), hit.get("state"),
                                "geocoded", hit.get("fetch_id")))
    note = f"{len(out)} cities geocoded live (no place rows of kind 'city' yet)"
    if failed:
        note += f"; geocoding failed for {len(failed)}"
    return out, note


def load_labels(
    session: Session, bbox: tuple[float, float, float, float]
) -> tuple[tj.LabelGrid | None, np.ndarray | None, str]:
    """District/state raster from the boundary table, and which labels are the burn belt."""
    if not _table_exists(session, "boundary"):
        return None, None, "boundary table missing"
    from blindspot.api.cache import fetch_binary

    params = {"w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3]}
    # Binary format: the WKB arrives as bytes, not hex text (half the database egress, ~1.2 MB a run).
    rows = fetch_binary(
        session,
        """
        SELECT level, name, state, code, ST_AsBinary(ST_SimplifyPreserveTopology(geom, 0.002)) AS wkb
        FROM boundary
        WHERE level IN ('state', 'district')
          AND geom && ST_MakeEnvelope(%(w)s, %(s)s, %(e)s, %(n)s, 4326)
        ORDER BY level DESC, id
        """,
        params,
    )
    if not rows:
        return None, None, "boundaries not loaded yet: districts and the burn belt are unlabelled"
    states, districts = [], []
    for r in rows:
        geom = shapely.from_wkb(bytes(r["wkb"]))
        if r["level"] == "state":
            states.append((geom, r["name"]))
        else:
            districts.append((geom, r["state"], r["name"]))
    grid = tj.build_label_grid(states, districts, bbox, step=0.01)
    # Indian polygons only are loaded (the boundaries module clips to India), so "Punjab" here is
    # Indian Punjab. Pakistani fires land on label 0: modelled, but never counted as Punjab.
    belt = grid.label_mask(lambda lab: (lab.get("state") or "").strip().lower() in BELT_STATES)
    note = f"{len(states)} state and {len(districts)} district polygons"
    if not belt.any():
        return grid, None, note + "; no Punjab or Haryana polygon found, so no burn-belt share"
    return grid, belt, note


def load_fires(
    session: Session, t_from: datetime, t_to: datetime, bbox: tuple[float, float, float, float]
) -> tuple[Fires | None, str]:
    """FIRMS detections in [t_from, t_to] inside the wind grid; low-confidence ones are dropped
    (FIRMS flags them for sun glint and other false alarms)."""
    if not _table_exists(session, "fire_detection"):
        return None, "fire_detection table does not exist yet"
    needed = {"id", "acq_utc", "lat", "lon", "frp_mw", "confidence"}
    missing = needed - _columns(session, "fire_detection")
    if missing:
        return None, f"fire_detection lacks columns {sorted(missing)}"
    rows = session.execute(
        text(
            """
            SELECT id, extract(epoch FROM acq_utc)::float8 AS t, lat, lon, frp_mw, confidence,
                   state, district, fetch_id
            FROM fire_detection
            WHERE acq_utc >= :t0 AND acq_utc <= :t1
              AND lon BETWEEN :w AND :e AND lat BETWEEN :s AND :n
            ORDER BY acq_utc, id
            """
        ),
        {"t0": t_from, "t1": t_to, "w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3]},
    ).mappings().all()
    keep = [r for r in rows if r["confidence"] != "low" and (r["frp_mw"] or 0) > 0]
    stats = {
        "in_window": len(rows),
        "used": len(keep),
        "low_confidence_skipped": sum(1 for r in rows if r["confidence"] == "low"),
        "no_frp_skipped": sum(1 for r in rows if r["confidence"] != "low" and not (r["frp_mw"] or 0) > 0),
    }
    fetch_ids = sorted({r["fetch_id"] for r in keep if r["fetch_id"] is not None}, reverse=True)
    frp = np.array([r["frp_mw"] for r in keep], dtype=np.float64)
    fires = Fires(
        id=np.array([r["id"] for r in keep], dtype=np.int64),
        t=np.array([r["t"] for r in keep], dtype=np.float64),
        lat=np.array([r["lat"] for r in keep], dtype=np.float64),
        lon=np.array([r["lon"] for r in keep], dtype=np.float64),
        frp=frp,
        dm=np.asarray(tj.fire_dry_matter_t(frp), dtype=np.float64),
        state=[r["state"] for r in keep],
        district=[r["district"] for r in keep],
        fetch_ids=fetch_ids,
        stats=stats,
    )
    return fires, f"{len(keep)} FIRMS detections ({stats['low_confidence_skipped']} low-confidence skipped)"


def load_scars(
    session: Session, t_from: datetime, t_to: datetime, bbox: tuple[float, float, float, float]
) -> tuple[Scars | None, str]:
    """Candidate unseen burn scars whose burn window overlaps [t_from, t_to]. Scars that FIRMS did
    see are left out on purpose: their fires are already in the FIRMS input."""
    if not _table_exists(session, "burn_cluster"):
        return None, "burn_cluster table does not exist yet"
    needed = {"id", "centroid", "area_ha", "burn_after", "burn_before", "unseen"}
    missing = needed - _columns(session, "burn_cluster")
    if missing:
        return None, f"burn_cluster lacks columns {sorted(missing)}"
    rows = session.execute(
        text(
            """
            SELECT id, ST_Y(centroid) AS lat, ST_X(centroid) AS lon, area_ha, burn_after, burn_before,
                   state, district, fetch_id
            FROM burn_cluster
            WHERE unseen IS TRUE AND burn_before > :t0 AND burn_after < :t1 AND area_ha > 0
              AND ST_X(centroid) BETWEEN :w AND :e AND ST_Y(centroid) BETWEEN :s AND :n
            ORDER BY id
            """
        ),
        {"t0": t_from, "t1": t_to, "w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3]},
    ).mappings().all()
    stats = {"candidate_unseen": len(rows), "pooled": False}
    if len(rows) > MAX_SCAR_SOURCES:  # pool by 0.05 deg cell and window, area-weighted centroid
        pools: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            key = (round(r["lat"] / 0.05), round(r["lon"] / 0.05), r["burn_after"], r["burn_before"])
            p = pools.setdefault(key, {"rows": [], "area": 0.0})
            p["rows"].append(r)
            p["area"] += r["area_ha"]
        merged = []
        for p in pools.values():
            a = np.array([r["area_ha"] for r in p["rows"]])
            big = max(p["rows"], key=lambda r: r["area_ha"])
            merged.append({**big, "lat": float(np.average([r["lat"] for r in p["rows"]], weights=a)),
                           "lon": float(np.average([r["lon"] for r in p["rows"]], weights=a)),
                           "area_ha": float(a.sum()), "n": len(p["rows"])})
        rows = merged
        stats["pooled"] = True
    area = np.array([r["area_ha"] for r in rows], dtype=np.float64)
    scars = Scars(
        id=[int(r["id"]) for r in rows],
        n_clusters=[int(r.get("n", 1)) for r in rows],
        lat=np.array([r["lat"] for r in rows], dtype=np.float64),
        lon=np.array([r["lon"] for r in rows], dtype=np.float64),
        area_ha=area,
        dm=np.asarray(tj.scar_dry_matter_t(area), dtype=np.float64),
        after=[r["burn_after"] for r in rows],
        before=[r["burn_before"] for r in rows],
        state=[r["state"] for r in rows],
        district=[r["district"] for r in rows],
        fetch_ids=sorted({r["fetch_id"] for r in rows if r.get("fetch_id") is not None}, reverse=True),
        stats=stats,
    )
    note = f"{stats['candidate_unseen']} candidate unseen burn scars"
    # Scars the sentinel module has not yet matched against FIRMS (unseen IS NULL) are left out: no
    # verdict is not the same as unseen. Say how many, so an empty scar input is not misread.
    unmatched = session.execute(
        text(
            """
            SELECT count(*) FROM burn_cluster
            WHERE unseen IS NULL AND burn_before > :t0 AND burn_after < :t1
              AND ST_X(centroid) BETWEEN :w AND :e AND ST_Y(centroid) BETWEEN :s AND :n
            """
        ),
        {"t0": t_from, "t1": t_to, "w": bbox[0], "s": bbox[1], "e": bbox[2], "n": bbox[3]},
    ).scalar() or 0
    if unmatched:
        stats["not_yet_matched"] = int(unmatched)
        note += f" ({unmatched} more scars in the window not yet matched against FIRMS, left out)"
    return scars, note


# --- forward run ----------------------------------------------------------------------------------


@dataclass
class Particles:
    lat: np.ndarray
    lon: np.ndarray
    t_rel: np.ndarray
    weight: np.ndarray  # tonnes of dry matter each particle carries
    kind: np.ndarray  # 0 = FIRMS fire, 1 = candidate unseen scar
    source: np.ndarray  # index into `sources`
    sources: list[dict[str, Any]]


def build_forward_particles(
    fires: Fires | None, scars: Scars | None, t_lo: float, t_hi: float, rng: np.random.Generator
) -> Particles:
    """Particles for every source whose emission falls in [t_lo, t_hi]: an equal number per source
    (so every plume is drawn with the same detail), each carrying its share of the tonnes."""
    plans: list[tuple[str, int, float, float, np.ndarray, np.ndarray, float, str | None, str | None]] = []
    if fires is not None:
        for i in range(len(fires)):
            if t_lo <= fires.t[i] <= t_hi:
                plans.append(("fire", int(fires.id[i]), fires.lat[i], fires.lon[i],
                              np.array([fires.t[i]]), np.array([1.0]), float(fires.dm[i]),
                              fires.state[i], fires.district[i]))
    if scars is not None:
        for j in range(len(scars)):
            hours, shares = tj.scar_release_hours(scars.after[j], scars.before[j], t_lo, t_hi)
            if hours.size and shares.sum() > 0:
                plans.append(("scar", scars.id[j], scars.lat[j], scars.lon[j], hours, shares,
                              float(scars.dm[j] * shares.sum()), scars.state[j], scars.district[j]))
    if not plans:
        z = np.zeros(0)
        return Particles(z, z, z, z, z.astype(np.int8), z.astype(np.int64), [])
    n = int(np.clip(FWD_PARTICLES // len(plans), PER_SOURCE_MIN, PER_SOURCE_MAX))
    lat, lon, t_rel, weight, kind, source, meta = [], [], [], [], [], [], []
    for k, (typ, sid, la, lo, hours, shares, dm, state, district) in enumerate(plans):
        if typ == "fire":  # burns for ~30 min after the detection
            times = hours[0] + rng.uniform(0.0, tj.FIRE_DURATION_S, n)
        else:  # a burning hour drawn by its share, then a moment within it
            times = rng.choice(hours, size=n, p=shares / shares.sum()) + rng.uniform(0.0, 3600.0, n)
        lat.append(np.full(n, la))
        lon.append(np.full(n, lo))
        t_rel.append(times)
        weight.append(np.full(n, dm / n))
        kind.append(np.full(n, 0 if typ == "fire" else 1, dtype=np.int8))
        source.append(np.full(n, k, dtype=np.int64))
        meta.append({"type": typ, "id": sid, "state": state, "district": district, "dm_t": dm})
    return Particles(
        np.concatenate(lat), np.concatenate(lon), np.concatenate(t_rel), np.concatenate(weight),
        np.concatenate(kind), np.concatenate(source), meta,
    )


def _fill_origin_names(p: Particles, labels: tj.LabelGrid | None) -> None:
    """Name a source's state/district from our polygons when its own row has none yet (the fires
    module labels rows after insert). Outside every polygon the names stay blank."""
    if labels is None:
        return
    for k, src in enumerate(p.sources):
        if src["state"] is None and src["district"] is None:
            first = int(np.flatnonzero(p.source == k)[0])
            lab = labels.labels[int(labels.lookup(p.lat[first : first + 1], p.lon[first : first + 1])[0])]
            src["state"], src["district"] = lab["state"], lab["district"]


def choose_trails(p: Particles, budget: int, rng: np.random.Generator) -> np.ndarray:
    """Which particles get a browser trail: one per source, biggest sources first, then a random
    draw (weighted by tonnes) for the rest of the budget. The order is the priority order."""
    if p.lat.size == 0:
        return np.zeros(0, dtype=np.intp)
    first = np.flatnonzero(np.r_[True, p.source[1:] != p.source[:-1]])  # first particle of a source
    dm = np.array([s["dm_t"] for s in p.sources])
    chosen = list(first[np.argsort(-dm[p.source[first]], kind="stable")][:budget])
    left = budget - len(chosen)
    if left > 0:
        rest = np.setdiff1d(np.arange(p.lat.size), chosen)
        if rest.size:
            w = p.weight[rest] / p.weight[rest].sum()
            chosen += list(rng.choice(rest, size=min(left, rest.size), replace=False, p=w))
    return np.array(chosen, dtype=np.intp)


def _labels_for(labels: tj.LabelGrid | None, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    return labels.lookup(lat, lon) if labels is not None else np.zeros(np.shape(lat), dtype=np.int32)


def _unattributed(by_label: np.ndarray) -> float | None:
    """Share emitted outside every loaded state polygon. With the Survey of India state polygons
    loaded that means outside India (across the border), modelled but not attributed to a state."""
    total = float(by_label.sum())
    return None if total <= 0 else round(float(by_label[0]) / total, 4)


def _gzip_size(obj: Any) -> tuple[int, int]:
    raw = orjson.dumps(obj)
    return len(raw), len(zlib.compress(raw, 6))


def fit_trails(per_member: list[list[dict[str, Any]]], budget: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Interleave the members' trails and drop the lowest-priority ones until the gzipped JSON fits
    the browser budget. Returns the trails and their size."""
    longest = max((len(t) for t in per_member), default=0)
    lo, hi = 0, longest
    best: list[dict[str, Any]] = []
    sizes = {"json_bytes": 2, "gzip_bytes": 22}
    while lo <= hi:  # binary search on trails per member
        mid = (lo + hi) // 2
        trails = [t for k in range(mid) for member in per_member if k < len(member) for t in [member[k]]]
        raw, gz = _gzip_size(trails)
        if gz <= budget:
            best, sizes = trails, {"json_bytes": raw, "gzip_bytes": gz}
            lo = mid + 1
        else:
            hi = mid - 1
    return best, sizes


def forward_run(
    members: list[Member],
    receptors: list[Receptor],
    fires: Fires | None,
    scars: Scars | None,
    labels: tj.LabelGrid | None,
    run_at: datetime,
    seed: int,
) -> dict[str, Any]:
    """The forward run: receptor series (both members), trails, inputs. Returns the row content."""
    t_now = run_at.timestamp()
    hour_now = math.floor(t_now / 3600.0) * 3600.0
    t0 = hour_now - LOOKBACK_H * 3600.0
    t_end = hour_now + HORIZON_H * 3600.0
    for m in members:  # every member must cover the window; clip to the shortest
        t0 = max(t0, math.ceil(m.wind.t0 / 3600.0) * 3600.0)
        t_end = min(t_end, math.floor(m.wind.t_end / 3600.0) * 3600.0)
    rng = np.random.default_rng(seed)
    p = build_forward_particles(fires, scars, t0, t_now, rng)
    origin_label = _labels_for(labels, p.lat, p.lon)
    _fill_origin_names(p, labels)
    n_labels = len(labels.labels) if labels is not None else 1
    n_rec = tj.n_records(t0, t_end)
    rlat = np.array([r.lat for r in receptors])
    rlon = np.array([r.lon for r in receptors])
    per_member_trails, member_info, series = [], [], []
    for k, m in enumerate(members):
        started = time.perf_counter()
        rec = tj.ReceptorSeries(rlat, rlon, p.weight, p.kind, origin_label, n_labels, n_rec)
        picks = choose_trails(p, FWD_TRAILS_PER_MEMBER, np.random.default_rng(seed + 101 * k))
        trail_rec = tj.TrailRecorder(picks, n_rec)
        integ = tj.integrate(m.wind, p.lat, p.lon, p.t_rel, t_start=t0, t_end=t_end,
                             seed=seed + k, observers=[rec, trail_rec])
        tag = "b" if k == 0 else "g" if m.model == "gfs_seamless" else f"m{k}"
        meta = []
        for n_trail, i in enumerate(picks):
            src = p.sources[int(p.source[i])]
            meta.append({
                "id": f"{tag}{n_trail}", "model": m.model, "weight": round(float(p.weight[i]), 4),
                "origin": {"type": src["type"], "id": src["id"], "district": src["district"],
                           "state": src["state"]},
            })
        per_member_trails.append(tj.trails_payload(trail_rec, integ.record_times, meta))
        series.append((integ.record_times, rec))
        member_info.append({
            "model": m.model, "met_run_id": m.run.id, "met_run_at": m.run.run_at,
            "upstream_runs": m.run.model_runs, "particles": int(p.lat.size),
            "left_grid_share": round(float(integ.exited.mean()), 4) if p.lat.size else None,
            "stopped_early": integ.stopped_early,
            "runtime_ms": round((time.perf_counter() - started) * 1000),
        })
    trails, sizes = fit_trails(per_member_trails, TRAIL_GZIP_BUDGET)
    receptor_out = []
    for c, r in enumerate(receptors):
        values = []
        hours = None
        for rec_times, rec in series:
            hours, total = tj.hourly_from_records(rec.total[c], rec_times)
            _, fires_only = tj.hourly_from_records(rec.by_kind[c, 0], rec_times)
            _, scars_only = tj.hourly_from_records(rec.by_kind[c, 1], rec_times)
            values.append((total, fires_only, scars_only))
        if hours is None:
            continue
        stack = np.array([v[0] for v in values])
        primary_total, primary_fires, primary_scars = values[0]
        pts = []
        for h, t in enumerate(hours):
            pts.append({
                "t": datetime.fromtimestamp(t, UTC),
                "smoke_index": round(float(primary_total[h]), 3),
                "from_fires": round(float(primary_fires[h]), 3),
                "from_scars": round(float(primary_scars[h]), 3),
                "range": [round(float(stack[:, h].min()), 3), round(float(stack[:, h].max()), 3)],
            })
        peak, arrival = tj.peak_and_arrival(hours, primary_total)
        rec0 = series[0][1]
        receptor_out.append({
            "city": r.name, "lat": round(r.lat, 4), "lon": round(r.lon, 4),
            "district": r.district, "state": r.state,
            "series": pts,
            "peak": None if peak is None else {"t": datetime.fromtimestamp(peak[0], UTC),
                                               "value": round(peak[1], 3)},
            "arrival_t": None if arrival is None else datetime.fromtimestamp(arrival, UTC),
            "by_state": tj.top_shares(rec0.by_label[c], labels.labels, "state", n=6) if labels else [],
            "unattributed_share": _unattributed(rec0.by_label[c]) if labels else None,
        })
    n_fires = sum(1 for s in p.sources if s["type"] == "fire")
    n_scars = sum(1 for s in p.sources if s["type"] == "scar")
    fires_by_state: dict[str, int] | None = None
    if labels is not None:  # SOI state polygons cover India, so "no state" means across the border
        fires_by_state = {}
        for s in p.sources:
            if s["type"] == "fire":
                key = s["state"] or "outside India"
                fires_by_state[key] = fires_by_state.get(key, 0) + 1
        fires_by_state = dict(sorted(fires_by_state.items(), key=lambda kv: -kv[1]))
    return {
        "t0": datetime.fromtimestamp(t0, UTC),
        "t_end": datetime.fromtimestamp(t_end, UTC),
        "hours": round((t_end - t0) / 3600.0, 3),
        "trails": trails,
        "receptors": receptor_out,
        "inputs": {
            "fires": n_fires,
            "scars": n_scars,
            "scars_kind": "candidate_unseen_burns",
            "fire_tonnes": round(sum(s["dm_t"] for s in p.sources if s["type"] == "fire"), 1),
            "scar_tonnes": round(sum(s["dm_t"] for s in p.sources if s["type"] == "scar"), 1),
            "particles_per_source": int(p.lat.size // max(len(p.sources), 1)),
            "release_window": [datetime.fromtimestamp(t0, UTC), run_at],
            "fires_by_state": fires_by_state,
        },
        "members": member_info,
        "payload": {"trails": len(trails), **sizes},
    }


# --- back runs ---------------------------------------------------------------------------------------


def _disk(lat: float, lon: float, n: int, radius_km: float, rng: np.random.Generator):
    r = radius_km * np.sqrt(rng.uniform(0.0, 1.0, n))
    theta = rng.uniform(0.0, 2.0 * np.pi, n)
    dlat = r * np.cos(theta) / tj.KM_PER_DEG
    dlon = r * np.sin(theta) / (tj.KM_PER_DEG * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def exposure_sources(
    fires: Fires | None, scars: Scars | None, labels: tj.LabelGrid | None, t_release: float, n_rec: int
) -> tj.Sources:
    """Emission events binned to the back run's 20-minute records (record r = t_release - r*20 min):
    a fire at the middle of its half hour, a scar's burning hours split over their three records."""
    lat, lon, rec, w, kind = [], [], [], [], []
    t_lo = t_release - (n_rec - 1) * tj.RECORD_S

    def add(la: float, lo: float, t: float, weight: float, k: int) -> None:
        r = int(round((t_release - t) / tj.RECORD_S))
        if 1 <= r <= n_rec - 1 and weight > 0:
            lat.append(la), lon.append(lo), rec.append(r), w.append(weight), kind.append(k)

    if fires is not None:
        for i in range(len(fires)):
            add(fires.lat[i], fires.lon[i], fires.t[i] + tj.FIRE_DURATION_S / 2, float(fires.dm[i]), 0)
    if scars is not None:
        for j in range(len(scars)):
            hours, shares = tj.scar_release_hours(scars.after[j], scars.before[j], t_lo, t_release)
            for h, s in zip(hours, shares, strict=True):
                for minute in (10.0, 30.0, 50.0):
                    add(scars.lat[j], scars.lon[j], h + minute * 60.0, float(scars.dm[j] * s / 3.0), 1)
    if not lat:
        return tj.Sources.empty()
    la, lo = np.array(lat), np.array(lon)
    return tj.Sources(la, lo, np.array(rec, dtype=np.int64), np.array(w), _labels_for(labels, la, lo),
                      np.array(kind, dtype=np.int8))


def back_runs(
    members: list[Member],
    receptors: list[Receptor],
    fires: Fires | None,
    scars: Scars | None,
    labels: tj.LabelGrid | None,
    belt_mask: np.ndarray | None,
    run_at: datetime,
    seed: int,
) -> list[dict[str, Any]]:
    """One back run per receptor city (all cities in one batch per member)."""
    if not receptors:
        return []
    t_rel = run_at.timestamp()
    t_end = t_rel - BACK_H * 3600.0
    n_rec = tj.n_records(t_rel, t_end)
    rng = np.random.default_rng(seed)
    lat, lon, group = [], [], []
    for c, r in enumerate(receptors):
        la, lo = _disk(r.lat, r.lon, BACK_PER_CITY, tj.RECEPTOR_RADIUS_KM, rng)
        lat.append(la), lon.append(lo), group.append(np.full(BACK_PER_CITY, c))
    lat, lon, group = np.concatenate(lat), np.concatenate(lon), np.concatenate(group)
    picks = np.concatenate([np.arange(BACK_TRAILS_PER_MEMBER) + c * BACK_PER_CITY for c in range(len(receptors))])
    sources = exposure_sources(fires, scars, labels, t_rel, n_rec)
    have_fire_data = fires is not None or scars is not None
    results = []
    for k, m in enumerate(members):
        started = time.perf_counter()
        summary = tj.BackSummary(group, len(receptors), labels, belt_mask, sources, t_rel)
        trails = tj.TrailRecorder(picks, n_rec)
        integ = tj.integrate(m.wind, lat, lon, np.full(lat.size, t_rel), t_start=t_rel, t_end=t_end,
                             seed=seed + 7 + k, observers=[summary, trails])
        results.append((m, summary, trails, integ, round((time.perf_counter() - started) * 1000)))
    out = []
    for c, r in enumerate(receptors):
        member_rows, paths, belts, exposures, trail_list = [], [], [], [], []
        for k, (m, summary, trails, integ, ms) in enumerate(results):
            times, cen, spread = summary.path(c)
            belt = summary.belt_share(c)
            exposure = float(summary.exposure[c]) if have_fire_data else None
            belts.append(belt.get("h48"))
            exposures.append(exposure)
            paths.append((cen, spread))
            tag = "b" if k == 0 else "g" if m.model == "gfs_seamless" else f"m{k}"
            cols = np.flatnonzero(group[picks] == c)
            sub = tj.TrailRecorder(picks[cols], n_rec)
            sub.lat, sub.lon = trails.lat[:, cols], trails.lon[:, cols]
            meta = [{"id": f"{tag}{n}", "model": m.model, "weight": round(1.0 / BACK_PER_CITY, 5),
                     "origin": {"type": "receptor", "id": r.name, "district": r.district, "state": r.state}}
                    for n in range(cols.size)]
            trail_list.append(tj.trails_payload(sub, integ.record_times, meta))
            member_rows.append({
                "model": m.model, "met_run_id": m.run.id, "met_run_at": m.run.run_at,
                "share_over_burn_belt": belt,
                "fire_exposure_index": tj.round_or_none(exposure),
                "exposure_from_scars": tj.round_or_none(float(summary.exposure_by_kind[c, 1])) if have_fire_data else None,
                "left_grid_share": summary.left_domain_share(c),
                "centerline": {
                    "path": [[round(float(x), 4), round(float(y), 4)] for x, y in cen if np.isfinite(x)],
                    "times": [int(t) for (x, _), t in zip(cen, times, strict=True) if np.isfinite(x)],
                },
                "stopped_early": integ.stopped_early,
                "runtime_ms": ms,
            })
        primary_m, primary, _, _, _ = results[0]
        wind_u, wind_v = primary_m.wind(np.array([r.lat]), np.array([r.lon]), t_rel)
        speed, from_deg = tj.wind_speed_from(float(wind_u[0]), float(wind_v[0]))
        blh_now = None
        if primary_m.blh_now is not None:
            blh_now = tj.bilinear(primary_m.run.grid, primary_m.blh_now, r.lon, r.lat)
        verdict, why = tj.verdict_hint(belts, exposures)
        exposure_vals = [e for e in exposures if e is not None]
        trails_all = [t for k in range(BACK_TRAILS_PER_MEMBER) for tl in trail_list if k < len(tl) for t in [tl[k]]]
        out.append({
            "city": r.name,
            "receptor": {"city": r.name, "lat": round(r.lat, 4), "lon": round(r.lon, 4),
                         "district": r.district, "state": r.state, "source": r.source},
            "summary": {
                "share_over_burn_belt": member_rows[0]["share_over_burn_belt"],
                "fire_exposure_index": member_rows[0]["fire_exposure_index"],
                "fire_exposure_range": [round(min(exposure_vals), 3), round(max(exposure_vals), 3)] if exposure_vals else None,
                "exposure_from_scars": member_rows[0]["exposure_from_scars"],
                "top_districts": tj.top_shares(primary.residence[c], labels.labels, "district") if labels else [],
                "top_source_districts": tj.top_shares(primary.exposure_by_label[c], labels.labels, "district") if labels else [],
                "exposure_unattributed_share": _unattributed(primary.exposure_by_label[c]) if labels else None,
                "blh_m_now": tj.round_or_none(blh_now, 0),
                "wind_from_deg_now": round(from_deg, 0),
                "wind_speed_kmh_now": round(speed * 3.6, 1),
                "wind_basis": "boundary-layer mean wind (10 m to 850 hPa), best_match",
                "left_grid_share": member_rows[0]["left_grid_share"],
                "verdict_reason": why,
                "cone": tj.cone_geometry(paths),
                "members": member_rows,
            },
            "verdict_hint": verdict,
            "trails": trails_all,
        })
    return out


# --- provenance, storage --------------------------------------------------------------------------


def run_sources(
    session: Session, members: list[Member], fires: Fires | None, scars: Scars | None,
    labels: tj.LabelGrid | None, receptors: list[Receptor],
) -> list[dict[str, Any]]:
    """SourceRefs for everything this run read: the wind fetches, the FIRMS files behind the fires,
    the scars' fetches, boundaries and places."""
    refs: list[SourceRef] = []
    seen: set[int] = set()

    def by_id(fetch_id: int | None) -> None:
        if fetch_id is not None and fetch_id not in seen:
            ref = ref_for_fetch(session, fetch_id)
            if ref is not None:
                seen.add(fetch_id)
                refs.append(ref)

    for m in members:
        by_id(m.run.fetch_id)
    for fetch_id in (fires.fetch_ids[:8] if fires else []):
        by_id(fetch_id)
    for fetch_id in (scars.fetch_ids[:4] if scars else []):
        by_id(fetch_id)
    if labels is not None:
        refs += [r for r in latest_refs(session, ["boundaries"]) if r.fetch_id is not None]
    for fetch_id in sorted({r.fetch_id for r in receptors if r.fetch_id is not None})[:4]:
        by_id(fetch_id)
    return [r.model_dump() for r in refs]


def method_params() -> dict[str, Any]:
    return {
        "wind": "boundary-layer mean of 10 m, 950, 925, 850 hPa (h = max(BLH, 100 m))",
        "integrator": "Heun RK2, bilinear in space, linear in time",
        "dt_s": tj.DT_S, "record_s": tj.RECORD_S, "k_h_m2_s": tj.K_H,
        "receptor_radius_km": tj.RECEPTOR_RADIUS_KM,
        "smoke_index_unit": "tonnes of crop residue (dry matter) burnt whose smoke is within 30 km",
        "fire_tonnes_per_mw": round(tj.FIRE_DURATION_S * tj.FRE_TO_DRY_MATTER_KG_PER_MJ / 1000.0, 4),
        "scar_tonnes_per_ha": tj.RICE_RESIDUE_CONSUMED_T_PER_HA,
        "burn_hour_share_ist": tj.BURN_HOUR_SHARE_IST,
        "citations": [
            "Wooster et al. 2005, JGR 110:D24311 (0.368 kg/MJ)",
            "Liu et al. 2020, Atmos. Environ. X (fire lifetime ~0.5 h)",
            "IPCC 2006 GL Vol.4 Ch.2 Table 2.4 (rice residues 5.5 t/ha)",
            "Singh et al. 2025, Current Science 129(10):921-923 (peak burning ~17:00 IST)",
            "Jethva 2026, Zenodo doi:10.5281/zenodo.20084790 (hourly shares, Punjab 2025)",
        ],
        "limits": "2-D; no deposition or rain-out; no terrain blocking over the Himalaya",
    }


def _forward_columns(forward: dict[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    """The jsonb columns of a forward row, as store() writes them."""
    return {
        "params": {**common["params"], "inputs": forward["inputs"], "members": forward["members"],
                   "payload": forward["payload"], "t_end": forward["t_end"]},
        "summary": {"inputs": forward["inputs"], "notes": common["notes"]},
        "trails": forward["trails"], "receptors": forward["receptors"],
    }


def _back_columns(b: dict[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    """The jsonb columns of a back row, as store() writes them."""
    return {
        "params": common["params"],
        "summary": {**b["summary"], "verdict_hint": b["verdict_hint"], "notes": common["notes"]},
        "trails": b["trails"], "receptors": [b["receptor"]],
    }


def store(forward: dict[str, Any] | None, backs: list[dict[str, Any]], common: dict[str, Any], run_at: datetime) -> dict[str, Any]:
    """Write the run in one transaction, then clear old trails and old rows."""
    ids: dict[str, Any] = {}
    with session_scope() as session:
        if forward is not None:
            row = SmokeRun(
                kind="forward", city=None, run_at=run_at, t0=forward["t0"], hours=forward["hours"],
                met_run_ids=common["met_run_ids"], **_forward_columns(forward, common),
            )
            session.add(row)
            session.flush()
            ids["forward"] = row.id
        ids["back"] = {}
        for b in backs:
            row = SmokeRun(
                kind="back", city=b["city"], run_at=run_at,
                t0=run_at - timedelta(hours=BACK_H), hours=float(BACK_H),
                met_run_ids=common["met_run_ids"], **_back_columns(b, common),
            )
            session.add(row)
            session.flush()
            ids["back"][b["city"]] = row.id
    prune()
    return ids


# --- the website's answers, stored with the run (Neon egress) -----------------------------------------
# GET /api/smoke/forward and /back read a run's whole trails column (12 MB for a forward run) to
# answer; on serverless every cold instance would. Right after writing a run, this job renders the
# answers the website asks for from the values it holds, and stores them (api/cache.store_payload),
# so no request has to read the trails back. The route reads the row back as Postgres renders it,
# so the values are rendered here the same way (api/cache.jsonb_text) and checked against
# md5(column::text) of the stored row (a few bytes from the database): when anything differs,
# that body is not stored and the first request computes and stores it instead.

FORWARD_VARIANTS: tuple[tuple[int | None, int], ...] = ((600, 2), (None, 1))  # website, then full
BACK_VARIANTS: tuple[tuple[int | None, int], ...] = ((None, 1),)  # the website asks ?city= only


def _as_read_back(value: Any) -> tuple[Any, str]:
    """(value as a route reads it back from jsonb, its jsonb text). Parsed from the text, so its
    objects have jsonb's key order, as a value read from the database has."""
    from blindspot.api.cache import jsonb_text
    from blindspot.db import _json_dumps

    rendered = jsonb_text(orjson.loads(_json_dumps(value)))
    return orjson.loads(rendered), rendered


def store_bodies(ids: dict[str, Any], forward: dict[str, Any] | None, backs: list[dict[str, Any]],
                 common: dict[str, Any]) -> dict[str, Any]:
    """Store the website's /forward and /back answers for the runs just written (see above)."""
    wanted: list[tuple[int, str, dict[str, Any]]] = []
    if forward is not None and ids.get("forward"):
        wanted.append((ids["forward"], "forward", _forward_columns(forward, common)))
    by_city = {b["city"]: b for b in backs}
    for city, run_id in (ids.get("back") or {}).items():
        wanted.append((run_id, "back", _back_columns(by_city[city], common)))
    return _store_runs(wanted)


def _store_runs(wanted: list[tuple[int, str, dict[str, Any]]]) -> dict[str, Any]:
    """Store the answers of runs (id, kind, the jsonb columns written) after checking each column
    against the database's md5 of its text."""
    import hashlib

    from blindspot.api.cache import store_payload
    from blindspot.api.routers import smoke as routes

    if not wanted:
        return {"stored": 0}
    with session_scope() as session:
        rows = {r["id"]: r for r in session.execute(text(
            "SELECT id, kind, city, run_at, t0, hours, met_run_ids, md5(params::text) AS params,"
            " md5(summary::text) AS summary, md5(trails::text) AS trails, md5(receptors::text) AS receptors"
            " FROM smoke_run WHERE id = ANY(:ids)"), {"ids": [w[0] for w in wanted]}).mappings()}
    stamp = routes.body_stamp()
    stored, mismatched = 0, []
    for run_id, kind, columns in wanted:
        row = rows.get(run_id)
        if row is None:
            continue
        run = {k: row[k] for k in ("id", "kind", "city", "run_at", "t0", "hours", "met_run_ids")}
        run["trails_json"] = None
        same = True
        for name, value in columns.items():
            plain, rendered = _as_read_back(value)
            if hashlib.md5(rendered.encode()).hexdigest() != row[name]:
                same = False
                mismatched.append(f"{kind} {run_id} {name}")
                break
            if name == "trails":
                run["trails_json"] = rendered
            else:
                run[name] = plain
        if not same:
            continue
        if kind == "validation":
            payload, sources, max_age = routes.validation_payload(run)
            stored += store_payload(routes.validation_key(run_id), stamp, payload, sources, max_age)
        elif kind == "forward":
            for max_trails, stride in FORWARD_VARIANTS:
                payload, sources, max_age = routes.forward_payload(run, max_trails, stride)
                stored += store_payload(routes.forward_key(run_id, max_trails, stride), stamp, payload,
                                        sources, max_age)
        else:
            for max_trails, stride in BACK_VARIANTS:
                payload, sources, max_age = routes.back_payload(run, BACK_H, BACK_H, max_trails, stride)
                stored += store_payload(routes.back_key(run_id, BACK_H, BACK_H, max_trails, stride), stamp,
                                        payload, sources, max_age)
    out: dict[str, Any] = {"stored": stored}
    if mismatched:
        out["not_stored"] = mismatched[:10]
    return out


def prune() -> dict[str, int]:
    """Clear trails on all but the newest KEEP_TRAIL_RUNS runs of each (kind, city); delete runs
    older than KEEP_DAYS. Summaries and series of recent runs stay for auditing."""
    with session_scope() as session:
        cleared = session.execute(
            text(
                """
                UPDATE smoke_run SET trails = NULL WHERE id IN (
                    SELECT id FROM (
                        SELECT id, row_number() OVER (PARTITION BY kind, lower(coalesce(city, ''))
                                                      ORDER BY run_at DESC, id DESC) AS k
                        FROM smoke_run WHERE trails IS NOT NULL
                    ) ranked WHERE k > :keep)
                """
            ),
            {"keep": KEEP_TRAIL_RUNS},
        ).rowcount
        deleted = session.execute(
            text("DELETE FROM smoke_run WHERE run_at < :cutoff"),
            {"cutoff": utcnow() - timedelta(days=KEEP_DAYS)},
        ).rowcount
    return {"trails_cleared": cleared, "deleted": deleted}


@job("smoke", after="met")
def smoke() -> dict[str, Any]:
    """Forward run from recent fires and candidate unseen scars; 72 h back runs for receptor cities."""
    started = time.perf_counter()
    run_at = utcnow()
    with session_scope() as session:
        members, notes = load_members(session, run_at)
        if not members:
            return {"skipped": "no wind grid stored yet; run the met job first", "notes": notes}
        bbox = members[0].wind.bbox
        receptors, receptor_note = load_receptors(session, bbox)
        labels, belt, label_note = load_labels(session, bbox)
        fires, fire_note = load_fires(session, run_at - timedelta(hours=BACK_H), run_at, bbox)
        scars, scar_note = load_scars(session, run_at - timedelta(hours=BACK_H), run_at, bbox)
        sources = run_sources(session, members, fires, scars, labels, receptors)
    notes += [receptor_note, label_note, fire_note, scar_note]
    seed = int(run_at.timestamp()) % 1_000_003
    forward = forward_run(members, receptors, fires, scars, labels, run_at, seed)
    if fires is None and scars is None:  # zero smoke would read as "no smoke"; say there is no data
        forward["receptors"] = []
        forward["inputs"]["reason"] = f"no fire data: {fire_note}; {scar_note}"
    backs = back_runs(members, receptors, fires, scars, labels, belt, run_at, seed)
    common = {
        "met_run_ids": [m.run.id for m in members],
        "notes": [n for n in notes if n],
        "params": {"method": method_params(), "sources": sources,
                   "members": [m.model for m in members], "runtime_ms": None},
    }
    common["params"]["runtime_ms"] = round((time.perf_counter() - started) * 1000)
    ids = store(forward, backs, common, run_at)
    try:
        bodies = store_bodies(ids, forward, backs, common)
    except Exception as exc:  # noqa: BLE001  (an optimisation: the routes compute live without it)
        log.warning("smoke: could not store the answers: %s", exc)
        bodies = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    verdicts = {b["city"]: b["verdict_hint"] for b in backs}
    return {
        "bodies": bodies,
        "forward_id": ids.get("forward"),
        "back_runs": len(ids.get("back", {})),
        "members": [m.model for m in members],
        "inputs": forward["inputs"] if forward else None,
        "payload": forward["payload"] if forward else None,
        "verdicts": verdicts,
        "notes": common["notes"],
        "runtime_s": round(time.perf_counter() - started, 1),
    }


# --- validation: modelled smoke vs observed PM2.5 ------------------------------------------------------

VALIDATION_SPINUP_H = 48  # fires this long before the first observation feed the first hours
VALIDATION_MAX_DAYS = 7
STATION_RADIUS_KM = tj.RECEPTOR_RADIUS_KM


def load_pm25(
    session: Session, receptors: list[Receptor], t_from: datetime, t_to: datetime
) -> dict[str, dict[str, Any]]:
    """Hourly PM2.5 near each receptor: measured (OpenAQ station hourly means within 30 km,
    averaged over stations) and the CAMS model (nearest model row within 30 km). µg/m³ only: the
    data.gov.in rows are NAQI sub-indices (parameter pm25_subindex) and are never mixed in."""
    if not _table_exists(session, "air_obs"):
        return {}
    rows = session.execute(
        text(
            """
            SELECT provider, station_id, lat, lon, value, period_minutes, is_model,
                   extract(epoch FROM observed_at)::float8 AS t
            FROM air_obs
            WHERE parameter = 'pm25' AND observed_at >= :t0 AND observed_at <= :t1
              AND value >= 0 AND value < 2000
            """
        ),
        {"t0": t_from, "t1": t_to},
    ).mappings().all()
    if not rows:
        return {}
    lat = np.array([r["lat"] for r in rows])
    lon = np.array([r["lon"] for r in rows])
    out: dict[str, dict[str, Any]] = {}
    for rec in receptors:
        d = np.hypot((lat - rec.lat) * tj.KM_PER_DEG,
                     (lon - rec.lon) * tj.KM_PER_DEG * math.cos(math.radians(rec.lat)))
        near = [rows[i] for i in np.flatnonzero(d <= STATION_RADIUS_KM)]
        measured = [r for r in near if not r["is_model"]]
        hourly_stations = {r["station_id"] for r in measured if r["period_minutes"] == 60}
        station_hour: dict[tuple[str, float], list[float]] = {}
        for r in measured:
            if r["station_id"] in hourly_stations and r["period_minutes"] != 60:
                continue  # that station reports hourly means; skip its 15-minute readings
            hour = math.ceil(r["t"] / 3600.0) * 3600.0
            station_hour.setdefault((r["station_id"], hour), []).append(float(r["value"]))
        by_hour: dict[float, list[float]] = {}
        for (_, hour), values in station_hour.items():
            by_hour.setdefault(hour, []).append(float(np.mean(values)))
        cams: dict[float, float] = {}
        model_rows = [r for r in near if r["is_model"]]
        if model_rows:
            nearest = min(model_rows, key=lambda r: (r["lat"] - rec.lat) ** 2 + (r["lon"] - rec.lon) ** 2)
            for r in model_rows:
                if r["station_id"] == nearest["station_id"]:
                    cams[math.ceil(r["t"] / 3600.0) * 3600.0] = float(r["value"])
        if by_hour:
            out[rec.name] = {
                "obs": {h: float(np.mean(v)) for h, v in by_hour.items()},
                "n_stations": {h: len(v) for h, v in by_hour.items()},
                "stations": len({sid for sid, _ in station_hour}),
                "cams": cams,
            }
    return out


def _lag0_r(entry: dict[str, Any], key: str) -> float | None:
    return ((entry.get(key) or {}).get("lag0") or {}).get("r")


@job("smoke_validate", every_minutes=360)
def smoke_validate() -> dict[str, Any]:
    """Hindcast the smoke index over the hours with PM2.5 observations and correlate it with them."""
    started = time.perf_counter()
    run_at = utcnow()
    with session_scope() as session:
        members, notes = load_members(session, run_at)
        if not members:
            return {"skipped": "no wind grid stored yet"}
        bbox = members[0].wind.bbox
        receptors, receptor_note = load_receptors(session, bbox)
        t_last = math.floor(run_at.timestamp() / 3600.0) * 3600.0
        t_first = max(run_at.timestamp() - VALIDATION_MAX_DAYS * 86400.0, max(m.wind.t0 for m in members))
        t_first = math.ceil(t_first / 3600.0) * 3600.0
        obs = load_pm25(session, receptors, datetime.fromtimestamp(t_first, UTC), run_at)
        if not obs:
            return {"skipped": "no measured PM2.5 near any receptor city yet", "notes": [receptor_note]}
        first_obs = min(min(o["obs"]) for o in obs.values())
        t0 = max(t_first, math.floor((first_obs - VALIDATION_SPINUP_H * 3600.0) / 3600.0) * 3600.0)
        t_end = min(t_last, min(math.floor(m.wind.t_end / 3600.0) * 3600.0 for m in members))
        window = (datetime.fromtimestamp(t0, UTC), datetime.fromtimestamp(t_end, UTC))
        fires, fire_note = load_fires(session, *window, bbox)
        scars, scar_note = load_scars(session, *window, bbox)
        if fires is None and scars is None:
            return {"skipped": f"no fire data: {fire_note}; {scar_note}"}
        primary_run = members[0].run
        blh_times, blh = load_level(session, primary_run, "10m", ("blh",))
        bl_times, bl = load_level(session, primary_run, "bl", ("u", "v"))
        sources = run_sources(session, members, fires, scars, None, receptors)
    cities = [r for r in receptors if r.name in obs]
    rng = np.random.default_rng(int(run_at.timestamp()) % 1_000_003)
    p = build_forward_particles(fires, scars, t0, t_end, rng)
    n_rec = tj.n_records(t0, t_end)
    rlat = np.array([r.lat for r in cities])
    rlon = np.array([r.lon for r in cities])
    member_series = []
    for k, m in enumerate(members):
        rec = tj.ReceptorSeries(rlat, rlon, p.weight, p.kind, np.zeros(p.lat.size, np.int32), 1, n_rec)
        integ = tj.integrate(m.wind, p.lat, p.lon, p.t_rel, t_start=t0, t_end=t_end, seed=11 + k,
                             observers=[rec])
        hours, total = tj.hourly_from_records(rec.total, integ.record_times)
        _, fires_only = tj.hourly_from_records(rec.by_kind[:, 0], integ.record_times)
        member_series.append((m.model, hours, total, fires_only))
    results, series_out = [], []
    _, hours, total0, fires0 = member_series[0]
    for c, city in enumerate(cities):
        o = obs[city.name]
        y = np.array([o["obs"].get(h, np.nan) for h in hours])
        cams = np.array([o["cams"].get(h, np.nan) for h in hours])
        blh_c = np.interp(hours, blh_times, tj.point_series(primary_run.grid, blh["blh"], city.lon, city.lat))
        u_c = tj.point_series(primary_run.grid, bl["u"], city.lon, city.lat)
        v_c = tj.point_series(primary_run.grid, bl["v"], city.lon, city.lat)
        speed = np.interp(hours, bl_times, np.hypot(u_c, v_c))
        ventilation = np.maximum(blh_c, tj.BL_FLOOR_M) * np.maximum(speed, 0.1)
        log_vent = np.log(ventilation)
        smoke = total0[c]
        dilute = smoke / np.maximum(blh_c, tj.BL_FLOOR_M) * 1000.0  # tonnes per km of mixing depth
        entry: dict[str, Any] = {
            "city": city.name, "lat": round(city.lat, 4), "lon": round(city.lon, 4),
            "stations": o["stations"], "hours_observed": int(np.isfinite(y).sum()),
            "smoke_index": tj.lagged_correlations(smoke, y, log_vent),
            "smoke_per_km_mixing_depth": tj.lagged_correlations(dilute, y, log_vent),
            "ventilation_only": tj.lagged_correlations(-log_vent, y),
            "cams_baseline": tj.lagged_correlations(cams, y, max_lag=0),
            "members": {},
        }
        for model, _, total_m, _ in member_series[1:]:
            entry["members"][model] = tj.lagged_correlations(total_m[c], y, log_vent)["lag0"]
        if float(np.nansum(total0[c] - fires0[c])) > 0:  # scars contributed: the key experiment
            entry["firms_only"] = tj.lagged_correlations(fires0[c], y, log_vent)
        lag0 = entry["smoke_per_km_mixing_depth"]["lag0"]
        entry["summary"] = tj.describe_r(lag0.get("r"), lag0.get("n_eff"))
        results.append(entry)
        series_out.append({
            "city": city.name,
            "series": [
                {"t": datetime.fromtimestamp(h, UTC), "smoke_index": round(float(smoke[i]), 3),
                 "smoke_per_km_mixing_depth": round(float(dilute[i]), 3),
                 "pm25_obs": None if not np.isfinite(y[i]) else round(float(y[i]), 1),
                 "n_stations": o["n_stations"].get(h),
                 "pm25_cams": None if not np.isfinite(cams[i]) else round(float(cams[i]), 1),
                 "ventilation_m2_s": round(float(ventilation[i]))}
                for i, h in enumerate(hours)
            ],
        })
    results.sort(key=lambda e: (e["city"] != "Delhi", -e["hours_observed"]))
    rs = [r for r in (_lag0_r(e, "smoke_per_km_mixing_depth") for e in results) if r is not None]
    cams_rs = [r for r in (_lag0_r(e, "cams_baseline") for e in results) if r is not None]
    vent_rs = [r for r in (_lag0_r(e, "ventilation_only") for e in results) if r is not None]
    summary = {
        "period": {"from": window[0], "to": window[1], "spinup_h": VALIDATION_SPINUP_H},
        "inputs": {"fires": sum(1 for s_ in p.sources if s_["type"] == "fire"),
                   "scars": sum(1 for s_ in p.sources if s_["type"] == "scar")},
        "headline": results[0]["summary"] if results else None,
        "median_r_smoke_per_km_mixing_depth": None if not rs else round(float(np.median(rs)), 3),
        "median_r_ventilation_only": None if not vent_rs else round(float(np.median(vent_rs)), 3),
        "median_r_cams": None if not cams_rs else round(float(np.median(cams_rs)), 3),
        "cities": results,
        "method": (
            "Forward hindcast from every FIRMS detection (and candidate unseen scar) in the period; "
            "hourly smoke index within 30 km of each city, also divided by the boundary-layer depth "
            "(a concentration proxy), against the hourly mean of OpenAQ PM2.5 stations within 30 km. "
            "Pearson r and Spearman rho at model leads of 0-12 h; partial r controls for log "
            "ventilation (BLH x transport wind), since stagnant air alone raises PM2.5; "
            "'ventilation_only' and CAMS are the baselines to beat. Correlation is not attribution: "
            "local sources dominate city PM2.5."
        ),
        "notes": notes + [receptor_note, fire_note, scar_note],
    }
    columns = {"params": {"method": method_params(), "sources": sources}, "summary": summary,
               "receptors": series_out}
    with session_scope() as session:
        row = SmokeRun(kind="validation", city=None, run_at=run_at, t0=window[0],
                       hours=(t_end - t0) / 3600.0, met_run_ids=[m.run.id for m in members],
                       trails=None, **columns)
        session.add(row)
        session.flush()
        row_id = row.id
    try:  # GET /api/smoke/validation's answer, stored now (see store_bodies)
        bodies = _store_runs([(row_id, "validation", columns)])
    except Exception as exc:  # noqa: BLE001
        log.warning("smoke_validate: could not store the answer: %s", exc)
        bodies = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    delhi = next((e for e in results if e["city"] == "Delhi"), None)
    return {
        "id": row_id,
        "bodies": bodies,
        "cities": len(results),
        "delhi": None if delhi is None else {
            "r_smoke_index": _lag0_r(delhi, "smoke_index"),
            "r_per_mixing_depth": _lag0_r(delhi, "smoke_per_km_mixing_depth"),
            "partial_r": delhi["smoke_per_km_mixing_depth"]["lag0"].get("partial_r"),
            "best_lag": delhi["smoke_per_km_mixing_depth"]["best_lag"],
            "r_ventilation_only": _lag0_r(delhi, "ventilation_only"),
            "r_cams": _lag0_r(delhi, "cams_baseline"),
            "summary": delhi["summary"],
        },
        "median_r": summary["median_r_smoke_per_km_mixing_depth"],
        "median_r_ventilation_only": summary["median_r_ventilation_only"],
        "median_r_cams": summary["median_r_cams"],
        "runtime_s": round(time.perf_counter() - started, 1),
    }
