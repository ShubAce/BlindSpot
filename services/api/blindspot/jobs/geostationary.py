"""Jobs `geostationary` and `geostationary_backfill`: the GK-2A fire watch over Punjab and Haryana.

Polar satellites (VIIRS, MODIS) see Punjab a handful of times a day; GK-2A scans it every 10
minutes. For each scan we Range-read only our window of two infrared channels (sources/gk2a.py),
run the contextual fire test (analytics/geofire.py), keep only pixels whose centre lies inside
India's Punjab or Haryana polygons (boundary table, ISO 3166-2 IN-PB / IN-HR; the AOI box also
holds Pakistan's Punjab), and store:
- geo_fire_slot: one row per scan (the diurnal record), geo_fire_pixel: the fire pixels;
- geo_fire_validation: GK-2A against every polar pass that has a GK-2A scan within 6 minutes,
  on the GK-2A grid (fire_detection rows, or FIRMS' keyless CSVs if that table has none).

`geostationary` (every 30 min) does the last 3 hours; `geostationary_backfill` (daily, and once by
hand) the last 3 days. Both skip scans already stored and retry failed ones, so they are idempotent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import shapely
from shapely import contains_xy
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from blindspot.analytics import geofire as gf
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX, point_ewkt
from blindspot.http import FetchError, SourceUnavailable
from blindspot.jobs import job
from blindspot.models.geofires import GeoFirePixel, GeoFireSlot, GeoFireValidation
from blindspot.provenance import utcnow
from blindspot.sources import gk2a

log = logging.getLogger(__name__)

CHANNELS = ("sw038", "ir112")  # the fire test is thermal only (see geofire.cloud_mask)
REGION_ISO = {"IN-PB": "Punjab", "IN-HR": "Haryana"}
LIVE_LOOKBACK_H = 3
BACKFILL_DAYS = 3
DOWNLOAD_WORKERS = 4
VALIDATE_MAX_DT_MIN = (
    6.0  # GK-2A scan within this many minutes of the polar pass mid-time
)
VALIDATE_RECENT_H = (
    24  # each live run re-checks passes this recent (FIRMS NRT arrives late)
)
STRONG_FRP_MW = (
    30.0  # a polar pixel this strong (summed FRP) "should" be visible to GK-2A
)
CACHE_KEEP_DAYS = 8


# --- the region ----------------------------------------------------------------------------------


def region_polygons(session: Session) -> dict[str, Any]:
    """Punjab and Haryana state polygons (shapely) from the boundary table, keyed by state name."""
    rows = session.execute(
        text(
            "SELECT name, props->>'iso3166_2' AS iso, ST_AsBinary(geom) AS wkb FROM boundary "
            "WHERE level = 'state' AND props->>'iso3166_2' = ANY(:isos)"
        ),
        {"isos": list(REGION_ISO)},
    ).all()
    polys = {REGION_ISO[r.iso]: shapely.from_wkb(bytes(r.wkb)) for r in rows}
    missing = set(REGION_ISO.values()) - set(polys)
    if missing:
        raise RuntimeError(
            f"state boundaries for {', '.join(sorted(missing))} are not loaded; run the boundaries job"
        )
    return polys


@dataclass
class WindowGeometry:
    """Everything about a scan window that depends only on where its pixels are."""

    lon: np.ndarray
    lat: np.ndarray
    corner_lon: np.ndarray
    corner_lat: np.ndarray
    area_km2: np.ndarray
    region: np.ndarray  # bool: centre inside Punjab or Haryana
    state_idx: np.ndarray  # int8: index into states, -1 outside
    states: tuple[str, ...]
    sat_dir: tuple[np.ndarray, np.ndarray, np.ndarray]
    region_mid_row: float  # window row of the region's centre, for the scan time


_geometry_cache: dict[tuple[Any, ...], WindowGeometry] = {}


def window_geometry(scan: gk2a.Scan, polys: dict[str, Any]) -> WindowGeometry:
    nav, w = scan.navigation, scan.window
    key = (w.row0, w.row1, w.col0, w.col1, nav, tuple(sorted(polys)))
    cached = _geometry_cache.get(key)
    if cached is not None:
        return cached
    lon, lat = w.centres(nav)
    clon, clat = w.corners(nav)
    states = tuple(sorted(polys))
    state_idx = np.full(lon.shape, -1, dtype=np.int8)
    for i, name in enumerate(states):
        state_idx[contains_xy(polys[name], np.nan_to_num(lon), np.nan_to_num(lat))] = i
    region = state_idx >= 0
    rows = np.nonzero(region)[0]
    geom = WindowGeometry(
        lon=lon,
        lat=lat,
        corner_lon=clon,
        corner_lat=clat,
        area_km2=gf.pixel_area_km2(clon, clat),
        region=region,
        state_idx=state_idx,
        states=states,
        sat_dir=gf.satellite_direction(lon, lat, nav.sub_lon_deg, nav.sat_distance_m),
        region_mid_row=float(rows.mean()) if rows.size else (w.shape[0] - 1) / 2,
    )
    if len(_geometry_cache) > 8:
        _geometry_cache.clear()
    _geometry_cache[key] = geom
    return geom


# --- one scan --------------------------------------------------------------------------------------


@dataclass
class Analysis:
    scan: gk2a.Scan
    geom: WindowGeometry
    t_utc: datetime  # when the scan swept the region
    sza: np.ndarray
    bt38: np.ndarray
    bt11: np.ndarray
    result: gf.FireTestResult

    @property
    def region_fire(self) -> np.ndarray:
        return self.result.fire & self.geom.region


def analyse(scan: gk2a.Scan, polys: dict[str, Any]) -> Analysis:
    """Run the fire test on one scan, with sun geometry for the moment it swept Punjab."""
    geom = window_geometry(scan, polys)
    t = gk2a.row_time(scan.attrs, scan.window.row0 + geom.region_mid_row)
    sza, saz = gf.solar_position(geom.lon, geom.lat, t)
    back = gf.sun_satellite_angle(sza, saz, geom.sat_dir)
    bt38, bt11 = scan.bt("sw038"), scan.bt("ir112")
    result = gf.detect_fires(bt38, bt11, sza, geom.area_km2, back)
    return Analysis(
        scan=scan, geom=geom, t_utc=t, sza=sza, bt38=bt38, bt11=bt11, result=result
    )


def _existing_fetch_id(session: Session, ids: list[int]) -> int | None:
    """The first of `ids` still present in source_fetch (cached scans carry the ids of the
    download that filled the cache; a foreign key must not point at a row that is gone)."""
    if not ids:
        return None
    found = set(
        session.execute(
            text("SELECT id FROM source_fetch WHERE id = ANY(:ids)"), {"ids": ids}
        )
        .scalars()
        .all()
    )
    return next((i for i in ids if i in found), None)


def _r(value: float, nd: int = 2) -> float | None:
    return round(float(value), nd) if np.isfinite(value) else None


def store_analysis(session: Session, a: Analysis) -> int:
    """Replace this scan's slot row and fire pixels. Returns the region's fire-pixel count."""
    res, g, w = a.result, a.geom, a.scan.window
    fid = _existing_fetch_id(session, a.scan.fetch_ids)
    fire = a.region_fire
    rr, cc = np.nonzero(fire)
    per_state = {
        name: int((fire & (g.state_idx == i)).sum()) for i, name in enumerate(g.states)
    }
    n_region = int(g.region.sum())
    with np.errstate(invalid="ignore"):
        clear_frac = (
            float((res.clear & g.region).sum() / n_region) if n_region else None
        )
    stats = {
        **res.stats,
        "window": [w.row0, w.row1, w.col0, w.col1],
        "bytes_fetched": a.scan.bytes_fetched,
        "from_cache": a.scan.from_cache,
        "file": a.scan.attrs.get("file_name"),
    }
    values = {
        "t_utc": a.t_utc,
        "status": "ok",
        "count": int(fire.sum()),
        "count_pb": per_state.get("Punjab", 0),
        "count_hr": per_state.get("Haryana", 0),
        "count_high": int((fire & (res.confidence == 2)).sum()),
        "frp_sum_mw": _r(np.nansum(res.frp_mw[fire]), 1) if fire.any() else 0.0,
        "clear_frac": round(clear_frac, 4) if clear_frac is not None else None,
        "region_pixels": n_region,
        "sza_deg": _r(np.nanmedian(a.sza[g.region]), 2) if n_region else None,
        "stats": stats,
        "error": None,
        "fetch_id": fid,
    }
    stmt = insert(GeoFireSlot).values(
        slot_utc=a.scan.slot, algorithm=gf.ALGORITHM_VERSION, **values
    )
    session.execute(
        stmt.on_conflict_do_update(constraint="uq_geo_fire_slot", set_=values)
    )
    session.execute(
        delete(GeoFirePixel).where(
            GeoFirePixel.slot_utc == a.scan.slot,
            GeoFirePixel.algorithm == gf.ALGORITHM_VERSION,
        )
    )
    pixels = []
    for i, j in zip(rr.tolist(), cc.tolist(), strict=True):
        ring = [
            (g.corner_lon[i, j], g.corner_lat[i, j]),
            (g.corner_lon[i, j + 1], g.corner_lat[i, j + 1]),
            (g.corner_lon[i + 1, j + 1], g.corner_lat[i + 1, j + 1]),
            (g.corner_lon[i + 1, j], g.corner_lat[i + 1, j]),
        ]
        wkt = (
            "SRID=4326;POLYGON(("
            + ", ".join(f"{x:.5f} {y:.5f}" for x, y in [*ring, ring[0]])
            + "))"
        )
        pixels.append(
            {
                "slot_utc": a.scan.slot,
                "t_utc": a.t_utc,
                "algorithm": gf.ALGORITHM_VERSION,
                "row": w.row0 + i,
                "col": w.col0 + j,
                "lon": round(float(g.lon[i, j]), 5),
                "lat": round(float(g.lat[i, j]), 5),
                "state": g.states[int(g.state_idx[i, j])],
                "confidence": int(res.confidence[i, j]),
                "bt38_k": _r(a.bt38[i, j]),
                "bt11_k": _r(a.bt11[i, j]),
                "btd_k": _r(res.btd[i, j]),
                "excess_k": _r(res.excess[i, j]),
                "frp_mw": _r(res.frp_mw[i, j], 1),
                "area_km2": _r(g.area_km2[i, j], 2),
                "absolute": bool(res.absolute[i, j]),
                "geom": point_ewkt(float(g.lon[i, j]), float(g.lat[i, j])),
                "footprint": wkt,
                "fetch_id": fid,
            }
        )
    if pixels:
        session.execute(insert(GeoFirePixel), pixels)
    return int(fire.sum())


def store_failure(session: Session, slot: datetime, error: str) -> None:
    values = {"status": "failed", "error": error[:500], "count": None}
    stmt = insert(GeoFireSlot).values(
        slot_utc=slot, algorithm=gf.ALGORITHM_VERSION, **values
    )
    session.execute(
        stmt.on_conflict_do_update(constraint="uq_geo_fire_slot", set_=values)
    )


# --- many scans ------------------------------------------------------------------------------------


def _list_scans(
    start: datetime, end: datetime
) -> tuple[dict[datetime, dict[str, gk2a.ScanFile]], int]:
    """Complete scans (both channels present) whose slot lies in [start, end], and how many hourly
    listings failed. A listing that fails is already recorded in source_fetch."""
    hour = start.replace(minute=0, second=0, microsecond=0)
    hours = []
    while hour <= end:
        hours.append(hour)
        hour += timedelta(hours=1)
    files: list[gk2a.ScanFile] = []
    failed = 0
    with ThreadPoolExecutor(8) as pool:
        for fut in as_completed([pool.submit(gk2a.list_hour, h) for h in hours]):
            try:
                files += fut.result()
            except FetchError:
                failed += 1
    if hours and failed == len(hours):
        raise FetchError(
            f"every GK-2A listing failed ({failed} hours); see source_fetch"
        )
    scans = gk2a.complete_scans(files, CHANNELS)
    return {slot: f for slot, f in scans.items() if start <= slot <= end}, failed


def process_range(start: datetime, end: datetime) -> dict[str, Any]:
    """Analyse and store every GK-2A scan in [start, end] not yet stored as 'ok'."""
    t0 = time.monotonic()
    with session_scope() as session:
        polys = region_polygons(session)
        done = set(
            session.execute(
                select(GeoFireSlot.slot_utc).where(
                    GeoFireSlot.algorithm == gf.ALGORITHM_VERSION,
                    GeoFireSlot.status == "ok",
                    GeoFireSlot.slot_utc.between(start, end),
                )
            )
            .scalars()
            .all()
        )
    scans, listing_failures = _list_scans(start, end)
    todo = {slot: files for slot, files in scans.items() if slot not in done}
    summary: dict[str, Any] = {
        "range": [start.isoformat(), end.isoformat()],
        "scans_listed": len(scans),
        "already_stored": len(scans) - len(todo),
        "listing_failures": listing_failures,
        "processed": 0,
        "from_cache": 0,
        "failed": 0,
        "fire_pixels": 0,
        "bytes_fetched": 0,
    }
    paused: SourceUnavailable | None = None
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(gk2a.load_scan, files, AOI_BBOX, CHANNELS): slot
            for slot, files in sorted(todo.items())
        }
        for fut in as_completed(futures):
            slot = futures[fut]
            try:
                scan = fut.result()
                analysis = analyse(scan, polys)
            except (
                SourceUnavailable
            ) as exc:  # the source is paused: nothing was sent, stop asking
                paused = exc
                for other in futures:
                    other.cancel()
                continue
            except (FetchError, ValueError, OSError, KeyError) as exc:
                log.warning("GK-2A scan %s failed: %s", slot, exc)
                with session_scope() as session:
                    store_failure(session, slot, f"{type(exc).__name__}: {exc}")
                summary["failed"] += 1
                continue
            with session_scope() as session:
                summary["fire_pixels"] += store_analysis(session, analysis)
            summary["processed"] += 1
            summary["from_cache"] += int(scan.from_cache)
            summary["bytes_fetched"] += 0 if scan.from_cache else scan.bytes_fetched
    summary["seconds"] = round(time.monotonic() - t0, 1)
    if paused is not None and summary["processed"] == 0:
        raise paused
    return summary


# --- validation against polar passes ---------------------------------------------------------------


def polar_reference(
    session: Session, start: datetime, end: datetime
) -> gk2a.PolarDetections:
    """VIIRS and MODIS detections inside Punjab or Haryana between start and end, from the
    fire_detection table (the fires module's FIRMS ingest)."""
    rows = session.execute(
        text(
            "SELECT f.lon, f.lat, f.acq_utc, f.satellite, f.instrument, f.frp_mw, f.scan, f.track "
            "FROM fire_detection f JOIN boundary b ON b.level = 'state' "
            "AND b.props->>'iso3166_2' = ANY(:isos) AND ST_Covers(b.geom, f.geom) "
            "WHERE f.acq_utc BETWEEN :start AND :end"
        ),
        {"isos": list(REGION_ISO), "start": start, "end": end},
    ).all()
    if not rows:
        return gk2a.empty_polar(source="fire_detection")

    def col(name: str) -> np.ndarray:
        return np.array(
            [np.nan if getattr(r, name) is None else getattr(r, name) for r in rows]
        )

    return gk2a.PolarDetections(
        lon=col("lon").astype(float),
        lat=col("lat").astype(float),
        acq_utc=np.array(
            [r.acq_utc.astimezone(UTC).replace(tzinfo=None) for r in rows],
            dtype="datetime64[s]",
        ),
        satellite=np.array([r.satellite for r in rows], dtype=object),
        instrument=np.array([r.instrument for r in rows], dtype=object),
        frp_mw=col("frp_mw").astype(float),
        scan_km=col("scan").astype(float),
        track_km=col("track").astype(float),
        source="fire_detection",
    )


def _firms_csv_reference(polys: dict[str, Any]) -> gk2a.PolarDetections:
    """Fallback when fire_detection has nothing for the period: FIRMS' keyless 7-day CSVs,
    clipped to the state polygons."""
    ref = gk2a.fetch_firms_reference(AOI_BBOX, "7d")
    if not len(ref):
        return ref
    union = shapely.union_all(list(polys.values()))
    return ref.subset(contains_xy(union, ref.lon, ref.lat))


def _load_for_validation(slot: datetime) -> gk2a.Scan | None:
    """A stored scan again, from the disk cache, or downloaded if the cache was pruned."""
    scan = gk2a.load_cached_scan(slot, CHANNELS)
    if scan is not None:
        return scan
    scans, _ = _list_scans(slot, slot)
    files = scans.get(slot)
    return gk2a.load_scan(files, AOI_BBOX, CHANNELS) if files else None


def _nearest_slot(mid: datetime) -> tuple[datetime, float | None] | None:
    """The stored 'ok' scan that swept the region closest to `mid`, within VALIDATE_MAX_DT_MIN."""
    tol = timedelta(minutes=VALIDATE_MAX_DT_MIN)
    with session_scope() as session:
        rows = session.execute(
            select(GeoFireSlot.slot_utc, GeoFireSlot.t_utc, GeoFireSlot.sza_deg).where(
                GeoFireSlot.algorithm == gf.ALGORITHM_VERSION,
                GeoFireSlot.status == "ok",
                GeoFireSlot.t_utc.between(mid - tol, mid + tol),
            )
        ).all()
    if not rows:
        return None
    best = min(rows, key=lambda r: abs((r.t_utc - mid).total_seconds()))
    return best.slot_utc, best.sza_deg


def validate_range(
    start: datetime,
    end: datetime,
    loader: Callable[[datetime], gk2a.Scan | None] = _load_for_validation,
) -> dict[str, Any]:
    """Compare GK-2A with every polar pass that started in [start, end]; replaces those rows."""
    with session_scope() as session:
        polys = region_polygons(session)
        ref = polar_reference(session, start - timedelta(minutes=30), end)
    if not len(ref):
        ref = _firms_csv_reference(polys)
    passes = [
        p
        for p in gf.group_passes(
            ref.acq_utc, ref.satellite, ref.instrument, ref.scan_km, ref.frp_mw
        )
        if start <= p.start <= end
    ]
    rows: list[dict[str, Any]] = []
    no_scan = 0
    for p in passes:
        nearest = _nearest_slot(p.mid)
        a = None
        if nearest is not None:
            try:
                scan = loader(nearest[0])
                a = analyse(scan, polys) if scan is not None else None
            except (FetchError, ValueError, OSError) as exc:
                log.warning("validation scan %s unavailable: %s", nearest[0], exc)
        if a is None or nearest is None:
            no_scan += 1
            continue
        slot, sza = nearest
        start64 = np.datetime64(p.start.replace(tzinfo=None), "s")
        end64 = np.datetime64(p.end.replace(tzinfo=None), "s")
        sel = (
            (ref.satellite == p.satellite)
            & (ref.acq_utc >= start64)
            & (ref.acq_utc <= end64)
        )
        nav, w = a.scan.navigation, a.scan.window
        rr, cc = nav.to_rowcol(ref.lon[sel], ref.lat[sel])
        cmp = gf.compare_pass(
            a.result.fire, a.result.clear, a.geom.region,
            rr - w.row0, cc - w.col0, ref.frp_mw[sel], STRONG_FRP_MW,
        )  # fmt: skip
        n_region = int(a.geom.region.sum())
        clear = (
            float((a.result.clear & a.geom.region).sum() / n_region)
            if n_region
            else None
        )
        rows.append(
            {
                "algorithm": gf.ALGORITHM_VERSION,
                "satellite": p.satellite,
                "instrument": p.instrument,
                "pass_start_utc": p.start,
                "pass_end_utc": p.end,
                "slot_utc": slot,
                "dt_min": round((a.t_utc - p.mid).total_seconds() / 60, 2),
                "regime": "day" if sza is not None and sza < 90 else "night",
                "sza_deg": sza,
                "clear_frac": round(clear, 4) if clear is not None else None,
                "n_detections": int(sel.sum()),
                "ref_pixels": cmp.ref_pixels,
                "ref_hits": cmp.ref_hits,
                "ref_pixels_strong": cmp.ref_pixels_strong,
                "ref_hits_strong": cmp.ref_hits_strong,
                "ref_pixels_clear": cmp.ref_pixels_clear,
                "geo_pixels": cmp.geo_pixels,
                "geo_hits": cmp.geo_hits,
                "strong_frp_mw": STRONG_FRP_MW,
                "reference": ref.source,
            }
        )
    with session_scope() as session:
        session.execute(
            delete(GeoFireValidation).where(
                GeoFireValidation.algorithm == gf.ALGORITHM_VERSION,
                GeoFireValidation.pass_start_utc.between(start, end),
            )
        )
        if rows:
            session.execute(insert(GeoFireValidation).on_conflict_do_nothing(), rows)
    return {
        "reference": ref.source,
        "passes": len(passes),
        "compared": len(rows),
        "no_gk2a_scan": no_scan,
        **{k: v for k, v in gf.pooled_rates(rows).items() if k != "passes"},
    }


# --- the jobs --------------------------------------------------------------------------------------


@job("geostationary", every_minutes=30)
def geostationary() -> dict[str, Any]:
    """GK-2A fire pixels over Punjab + Haryana from the last 3 hours of 10-minute scans."""
    now = utcnow()
    scans = process_range(now - timedelta(hours=LIVE_LOOKBACK_H), now)
    validation = validate_range(now - timedelta(hours=VALIDATE_RECENT_H), now)
    return {"scans": scans, "validation": validation}


@job("geostationary_backfill", cron="20 4 * * *")
def geostationary_backfill() -> dict[str, Any]:
    """GK-2A fire pixels and VIIRS/MODIS validation for the last 3 days (fills gaps, idempotent)."""
    now = utcnow()
    start = (now - timedelta(days=BACKFILL_DAYS)).replace(
        minute=0, second=0, microsecond=0
    )
    scans = process_range(start, now)
    validation = validate_range(start, now)
    removed = gk2a.prune_cache(CACHE_KEEP_DAYS)
    return {"scans": scans, "validation": validation, "cache_files_pruned": removed}
