"""Shared helpers for the offline GK-2A evaluations in this folder. Read-only by construction.

- Scans come from .npz files in the job's cache layout (`<dir>/YYYYmmdd/HHMM.npz`, as
  sources/gk2a.py writes them) or the flat layout gk2a_fetch_scans.py writes
  (`<dir>/YYYYmmdd_HHMM.npz`).
- Everything else (state polygons, FIRMS detections, orbital elements, the stored GK-2A record) is
  read from a copy of the production database in transactions opened read-only
  (default_transaction_read_only=on), so no evaluation can write to it.
- Scans are analysed with the production code (jobs/geostationary.window_geometry and
  analytics/geofire.detect_fires), so what is measured is what the job runs.

The evaluation database defaults to the local copy of production; pass --eval-db to change it.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import shapely
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api (not installed)

from blindspot.analytics import geofire as gf  # noqa: E402
from blindspot.analytics import passes as ps  # noqa: E402
from blindspot.jobs import geostationary as job  # noqa: E402
from blindspot.sources import gk2a  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)

EVAL_DB = "postgresql+psycopg://blindspot:blindspot@localhost:5434/blindspot_eval"
IST = timedelta(hours=5, minutes=30)


# --- database (read-only) ---------------------------------------------------------------------------


def read_only_engine(url: str = EVAL_DB) -> Engine:
    """An engine whose every transaction is read-only (the server refuses any write)."""
    return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})


def region_polygons(eng: Engine) -> dict[str, Any]:
    """Punjab and Haryana polygons from the boundary table, exactly as the job loads them."""
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT props->>'iso3166_2' AS iso, ST_AsBinary(geom) AS wkb FROM boundary "
                "WHERE level = 'state' AND props->>'iso3166_2' = ANY(:isos)"
            ),
            {"isos": list(job.REGION_ISO)},
        ).all()
    return {job.REGION_ISO[r.iso]: shapely.from_wkb(bytes(r.wkb)) for r in rows}


def polar_detections(eng: Engine, start: datetime, end: datetime) -> gk2a.PolarDetections:
    """FIRMS VIIRS/MODIS detections inside the state polygons, as job.polar_reference reads them."""
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT f.lon, f.lat, f.acq_utc, f.satellite, f.instrument, f.frp_mw, f.scan, f.track "
                "FROM fire_detection f JOIN boundary b ON b.level = 'state' "
                "AND b.props->>'iso3166_2' = ANY(:isos) AND ST_Covers(b.geom, f.geom) "
                "WHERE f.acq_utc BETWEEN :start AND :end"
            ),
            {"isos": list(job.REGION_ISO), "start": start, "end": end},
        ).all()

    def col(name: str) -> np.ndarray:
        return np.array([np.nan if getattr(r, name) is None else getattr(r, name) for r in rows])

    return gk2a.PolarDetections(
        lon=col("lon").astype(float),
        lat=col("lat").astype(float),
        acq_utc=np.array(
            [r.acq_utc.astimezone(UTC).replace(tzinfo=None) for r in rows], dtype="datetime64[s]"
        ),
        satellite=np.array([r.satellite for r in rows], dtype=object),
        instrument=np.array([r.instrument for r in rows], dtype=object),
        frp_mw=col("frp_mw").astype(float),
        scan_km=col("scan").astype(float),
        track_km=col("track").astype(float),
        source="fire_detection",
    )


def orbits(eng: Engine) -> dict[str, ps.OrbitElements]:
    """Stored orbital elements by satellite name (the names fire_detection uses)."""
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT norad_id, key, name, instrument, line1, line2, epoch FROM tle")
        ).mappings()
        return {r["name"]: ps.OrbitElements(**dict(r)) for r in rows}


def stored_validation(eng: Engine, algorithm: str = "gk2a-ctx-2") -> list[dict[str, Any]]:
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM geo_fire_validation WHERE algorithm = :a ORDER BY pass_start_utc"),
            {"a": algorithm},
        ).mappings()
        return [dict(r) for r in rows]


# --- scans ---------------------------------------------------------------------------------------------


def scan_paths(dirs: Iterable[Path]) -> dict[datetime, Path]:
    """slot -> npz path over several folders (the first folder holding a slot wins)."""
    out: dict[datetime, Path] = {}
    for d in dirs:
        d = Path(d)
        for p in sorted(d.glob("*/*.npz")) + sorted(d.glob("*.npz")):
            if ".partial" in p.name:
                continue
            stamp = (p.parent.name + p.stem) if p.parent != d else p.stem.replace("_", "")
            try:
                slot = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=UTC)
            except ValueError:
                continue
            out.setdefault(slot, p)
    return out


def load_npz(path: Path) -> gk2a.Scan:
    with np.load(path) as z:
        meta = orjson.loads(z["meta"].tobytes())
        win = gk2a.GridWindow(*meta["window"])
        slot = datetime.fromisoformat(meta["slot"])
        channels = {
            c: gk2a.ChannelWindow(
                file=gk2a.ScanFile(slot, c, meta["channels"][c]["key"], 0, None),
                window=win,
                counts=z[f"{c}_counts"],
                quality=z[f"{c}_quality"],
                attrs=meta["channels"][c]["attrs"],
            )
            for c in ("sw038", "ir112")
        }
    return gk2a.Scan(slot=slot, window=win, channels=channels, from_cache=True)


@dataclass
class Prepared:
    """One scan with everything detect_fires needs, computed as jobs/geostationary.analyse does."""

    scan: gk2a.Scan
    geom: job.WindowGeometry
    t_utc: datetime
    sza: np.ndarray
    back: np.ndarray
    bt38: np.ndarray
    bt11: np.ndarray

    def detect(self, exclude: np.ndarray | None = None, p: gf.FireTestParams = gf.DEFAULT_PARAMS):
        return gf.detect_fires(
            self.bt38, self.bt11, self.sza, self.geom.area_km2, self.back, exclude=exclude, p=p
        )


def prepare(scan: gk2a.Scan, polys: dict[str, Any]) -> Prepared:
    geom = job.window_geometry(scan, polys)
    t = gk2a.row_time(scan.attrs, scan.window.row0 + geom.region_mid_row)
    sza, saz = gf.solar_position(geom.lon, geom.lat, t)
    back = gf.sun_satellite_angle(sza, saz, geom.sat_dir)
    return Prepared(scan, geom, t, sza, back, scan.bt("sw038"), scan.bt("ir112"))


class Scans:
    """Lazily loaded, prepared scans by slot."""

    def __init__(self, dirs: Iterable[Path], polys: dict[str, Any]) -> None:
        self.paths = scan_paths(dirs)
        self.polys = polys
        self._prepared: dict[datetime, Prepared | None] = {}

    def __contains__(self, slot: datetime) -> bool:
        return slot in self.paths

    def get(self, slot: datetime) -> Prepared | None:
        if slot not in self._prepared:
            path = self.paths.get(slot)
            try:
                self._prepared[slot] = prepare(load_npz(path), self.polys) if path else None
            except (ValueError, OSError, KeyError) as exc:
                print(f"  scan {slot:%Y-%m-%d %H:%M} unreadable: {exc}", file=sys.stderr)
                self._prepared[slot] = None
        return self._prepared[slot]

    def forget(self, slot: datetime) -> None:
        self._prepared.pop(slot, None)


# --- polar passes ------------------------------------------------------------------------------------


@dataclass
class PassData:
    """One polar pass, the GK-2A scan the job compared it with, and its detections on that grid."""

    p: gf.PolarPass
    slot: datetime
    stored: dict[str, Any] | None  # the geo_fire_validation row, when there is one
    rows: np.ndarray  # window row of each polar detection of the pass (region only)
    cols: np.ndarray
    frp: np.ndarray
    scan_deg: np.ndarray | None = None  # polar scan angle at every window pixel (NaN off-region)

    @property
    def regime(self) -> str:
        return self.stored["regime"] if self.stored else "?"

    @property
    def group(self) -> str:
        if self.p.satellite == "Aqua":
            return "modis_aqua_" + self.regime
        if self.p.satellite == "Terra":
            return "modis_terra"
        return "viirs_" + self.regime


def stored_passes(
    eng: Engine, scans: Scans, algorithm: str = "gk2a-ctx-2"
) -> tuple[list[PassData], gk2a.PolarDetections]:
    """The polar passes of the stored validation, each with the scan the job used for it."""
    stored = stored_validation(eng, algorithm)
    if not stored:
        return [], gk2a.empty_polar()
    t0 = min(r["pass_start_utc"] for r in stored) - timedelta(minutes=30)
    t1 = max(r["pass_end_utc"] for r in stored) + timedelta(minutes=30)
    ref = polar_detections(eng, t0, t1)
    groups = {
        (p.satellite, p.start): p
        for p in gf.group_passes(ref.acq_utc, ref.satellite, ref.instrument, ref.scan_km, ref.frp_mw)
    }
    out = []
    for r in stored:
        p = groups.get((r["satellite"], r["pass_start_utc"]))
        if p is None:
            print(f"  pass {r['satellite']} {r['pass_start_utc']} not regrouped", file=sys.stderr)
            continue
        out.append(PassData(p, r["slot_utc"], r, *_pass_points(ref, p)))
    return out, ref


def _pass_points(ref: gk2a.PolarDetections, p: gf.PolarPass) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    st = np.datetime64(p.start.replace(tzinfo=None), "s")
    en = np.datetime64(p.end.replace(tzinfo=None), "s")
    sel = (ref.satellite == p.satellite) & (ref.acq_utc >= st) & (ref.acq_utc <= en)
    return ref.lon[sel], ref.lat[sel], ref.frp_mw[sel]


def to_window(pd_: PassData, a: Prepared) -> tuple[np.ndarray, np.ndarray]:
    """Window (row, col) of the pass's detections (stored as lon/lat until a scan is known)."""
    nav, w = a.scan.navigation, a.scan.window
    rr, cc = nav.to_rowcol(pd_.rows, pd_.cols)
    return rr - w.row0, cc - w.col0


# --- pooled rates and bootstrap ---------------------------------------------------------------------------

RATES = {  # name: (numerator, denominator); false-alarm rate = 1 - geo_hits / geo_pixels
    "hit_rate": ("ref_hits", "ref_pixels"),
    "hit_rate_strong": ("ref_hits_strong", "ref_pixels_strong"),
    "false_alarm_rate": ("geo_unconfirmed", "geo_pixels"),
}


def _num(rows: Sequence[dict[str, Any]], key: str) -> np.ndarray:
    if key == "geo_unconfirmed":
        return np.array([r["geo_pixels"] - r["geo_hits"] for r in rows], dtype=float)
    return np.array([r[key] for r in rows], dtype=float)


def pooled(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"passes": len(rows)}
    for name, (num, den) in RATES.items():
        n, d = _num(rows, num).sum(), _num(rows, den).sum()
        out[name] = round(float(n / d), 4) if d else None
        out[f"{name}_n"] = int(d)
        out[f"{name}_k"] = int(n)
    return out


def bootstrap(
    rows: Sequence[dict[str, Any]], b: int = 4000, seed: int = 20261123
) -> dict[str, tuple[float, float] | None]:
    """95% percentile intervals of each pooled rate, resampling passes with replacement."""
    if not rows:
        return {k: None for k in RATES}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(rows), size=(b, len(rows)))
    out: dict[str, tuple[float, float] | None] = {}
    for name, (num, den) in RATES.items():
        n, d = _num(rows, num)[idx].sum(axis=1), _num(rows, den)[idx].sum(axis=1)
        ok = d > 0
        if ok.sum() < 0.5 * b:
            out[name] = None
            continue
        r = n[ok] / d[ok]
        out[name] = (round(float(np.percentile(r, 2.5)), 4), round(float(np.percentile(r, 97.5)), 4))
    return out


def paired_bootstrap(
    a_rows: Sequence[dict[str, Any]],
    b_rows: Sequence[dict[str, Any]],
    b: int = 4000,
    seed: int = 20261123,
) -> dict[str, tuple[float, float, float] | None]:
    """(difference, lo, hi) of each pooled rate, B minus A, resampling the same passes for both."""
    assert len(a_rows) == len(b_rows)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a_rows), size=(b, len(a_rows)))
    out: dict[str, tuple[float, float, float] | None] = {}
    for name, (num, den) in RATES.items():
        na, da = _num(a_rows, num), _num(a_rows, den)
        nb, db = _num(b_rows, num), _num(b_rows, den)
        if not da.sum() or not db.sum():
            out[name] = None
            continue
        point = nb.sum() / db.sum() - na.sum() / da.sum()
        sa, sda, sb, sdb = na[idx].sum(1), da[idx].sum(1), nb[idx].sum(1), db[idx].sum(1)
        ok = (sda > 0) & (sdb > 0)
        diff = sb[ok] / sdb[ok] - sa[ok] / sda[ok]
        out[name] = (
            round(float(point), 4),
            round(float(np.percentile(diff, 2.5)), 4),
            round(float(np.percentile(diff, 97.5)), 4),
        )
    return out


def fmt_rate(pool: dict[str, Any], ci: dict[str, Any] | None, name: str) -> str:
    v = pool.get(name)
    if v is None:
        return f"{name} n/a"
    s = f"{name} {100 * v:.1f}% ({pool[name + '_k']}/{pool[name + '_n']})"
    if ci and ci.get(name):
        lo, hi = ci[name]
        s += f" [95% CI {100 * lo:.1f}-{100 * hi:.1f}]"
    return s


def ist(t: datetime) -> datetime:
    return t.astimezone(UTC) + IST


def night_of(t: datetime) -> Any:
    """The evening a night starts on (IST), as analytics/fires_stats.night_of."""
    return (ist(t) - timedelta(hours=12)).date()


def each(seq: Iterable[Any], fn: Callable[[Any], Any]) -> list[Any]:
    return [fn(x) for x in seq]
