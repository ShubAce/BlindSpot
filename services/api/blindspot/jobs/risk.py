"""Jobs: `risk` (burn risk of harvested fields, with a backtest) and `featured` (landing page frames).

risk, every 3 h and after each `sentinel` run:
  1. put every harvested field whose straw is still unburnt (harvest_cluster, from the sentinel
     module; a snapshot it rebuilds on each run) into a temporary table;
  2. fill in its factors there, set-based: burning within 5 km in the last 7 days (fire_detection +
     burn_cluster), last season's FIRMS fires within 5 km (NASA FIRMS archive, fetched once, kept on
     disk and copied in), and the rain forecast for the next 72 h (Open-Meteo, per 0.1 degree cell);
  3. score them in SQL with the formula of blindspot.analytics.risk (re-checked in Python on a sample
     every run); store what the run shares once (risk_run) and each field's own numbers on its row
     (risk_score, risk_factors), rewriting only rows that changed;
  4. backtest, also in the database: for each pair of clear passes this season, score the fields
     that were harvested and unburnt on the first pass using only what was known then, and check
     which ones had burnt by the second pass (AUC, precision@k and lift with 95% intervals, against
     baselines, and a calibration table). The report is stored on the run and served by GET /api/risk.
  Per field nothing leaves the database: a run pulls well under 1 MB (it pulled ~105 MB before
  2026-10-31, which the hosted database bills as egress).

featured, every 6 h and after `sentinel`: rebuild the landing page's Sentinel-2 frames when the
window has moved or they are older than 6 h (blindspot.api.routers.imagery.refresh_featured), then
pre-render the before/after chips of the largest candidate unseen burns, so the case pages a judge
is most likely to open load at once.

Both are idempotent; upstream failures degrade a factor (and say so) instead of inventing values.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import orjson
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import text
from sqlalchemy.orm import Session

from blindspot.analytics import risk as model
from blindspot.config import get_settings
from blindspot.db import session_scope
from blindspot.geo import AOI_BBOX
from blindspot.http import FetchError, fetch, set_fetch_records
from blindspot.jobs import job
from blindspot.provenance import utcnow

log = logging.getLogger(__name__)

FIRMS_AREA = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
# Last season's fires: S-NPP VIIRS (the satellite India's official counts rely on), the standard
# science-quality product, which FIRMS publishes for anything older than a few months.
HISTORY_PRODUCT = "VIIRS_SNPP_SP"
SEASON = ((9, 15), (11, 30))  # official stubble-fire monitoring window, 15 Sep - 30 Nov (PIB 01-Dec-2025)
FIRMS_MAX_DAYS = 5  # the area API's DAY_RANGE limit (SYNTHESIS section 2 #2)
RAIN_CELL_DEG = 0.1  # rain is asked once per 0.1 degree cell (~10 km; ECMWF IFS runs at 9 km)
OPEN_METEO_MAX_POINTS = 1000  # per request (meteo research doc section 1)
KM_PER_DEG = 111.32
BACKTEST_MIN_EACH = 5  # a pass pair needs at least this many burnt and unburnt fields
BACKTEST_KS = (10, 25, 50, 100, 250)


def _exists(session: Session, table: str) -> bool:
    return session.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}).scalar_one()


def _columns(session: Session, table: str) -> set[str]:
    rows = session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).scalars()
    return set(rows)


# --- 1. the fields ----------------------------------------------------------------------------------


@dataclass
class Field:
    id: int
    lon: float
    lat: float
    area_ha: float
    harvested_after: datetime | None = None
    harvested_before: datetime | None = None


def load_fields(session: Session) -> list[Field]:
    rows = session.execute(text("""
        SELECT id, ST_X(coalesce(centroid, ST_PointOnSurface(geom))), ST_Y(coalesce(centroid, ST_PointOnSurface(geom))),
               area_ha, harvested_after, harvested_before
        FROM harvest_cluster ORDER BY id
    """)).all()
    return [Field(int(r[0]), float(r[1]), float(r[2]), float(r[3] or 0), r[4], r[5]) for r in rows]


# --- 2a. burning nearby (database) ---------------------------------------------------------------------


def fill_neighbourhood(session: Session, table: str, t_end: datetime, where: str = "TRUE",
                       params: dict[str, Any] | None = None, days: int = model.NEIGHBOUR_DAYS,
                       radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """Set `fires`, `unseen` and `ha` on the rows of a temporary table (columns k, g geography) that
    match `where`: FIRMS detections (nominal/high confidence), candidate unseen burn scars, and burnt
    hectares within `radius_km` during (t_end - days, t_end]. Set-based, in a few statements
    whatever the number of rows, and nothing comes back to the client.

    Distances are true metres on the ellipsoid (geography). The window's fires and scars are first
    copied into temporary tables as geography, each with its own spatial index, so the search for
    a field only meets this week's rows (not the whole season's), no polygon is converted again for
    every field it is compared with, and one pass over the scars gives both the unseen count and
    the burnt hectares. (Three correlated subqueries per field over the season's tables took 13 min
    for 172k fields on the dev box.)"""
    has_fires, has_burns = _exists(session, "fire_detection"), _exists(session, "burn_cluster")
    window = {"t0": t_end - timedelta(days=days), "t1": t_end}
    session.execute(text("DROP TABLE IF EXISTS nb_fire, nb_burn"))
    # The fires module flags spots hot on 3+ nights (kilns, industry) as static_source: not crop fires.
    static = "AND NOT static_source" if has_fires and "static_source" in _columns(session, "fire_detection") else ""
    session.execute(text(f"""
        CREATE TEMP TABLE nb_fire ON COMMIT DROP AS
        SELECT geom::geography AS g FROM fire_detection
        WHERE acq_utc > :t0 AND acq_utc <= :t1 AND confidence <> 'low' {static}
    """ if has_fires else "CREATE TEMP TABLE nb_fire (g geography) ON COMMIT DROP"), window)
    session.execute(text("""
        CREATE TEMP TABLE nb_burn ON COMMIT DROP AS
        SELECT geom::geography AS g, area_ha, unseen FROM burn_cluster
        WHERE burn_before > :t0 AND burn_before <= :t1
    """ if has_burns else "CREATE TEMP TABLE nb_burn (g geography, area_ha float8, unseen boolean) ON COMMIT DROP"),
        window)
    for name in ("nb_fire", "nb_burn"):
        session.execute(text(f"CREATE INDEX ON {name} USING gist (g)"))
        session.execute(text(f"ANALYZE {name}"))
    session.execute(text(f"""
        UPDATE {table} AS t SET fires = x.n, unseen = x.unseen, ha = x.ha
        FROM (
            SELECT p.k, f.n, b.unseen, b.ha
            FROM {table} p
            CROSS JOIN LATERAL (SELECT count(*) AS n FROM nb_fire f WHERE ST_DWithin(f.g, p.g, :m)) f
            CROSS JOIN LATERAL (
                SELECT count(*) FILTER (WHERE b.unseen IS TRUE) AS unseen, coalesce(sum(b.area_ha), 0) AS ha
                FROM nb_burn b WHERE ST_DWithin(b.g, p.g, :m)
            ) b
            WHERE {where}
        ) AS x
        WHERE t.k = x.k
    """), {"m": radius_km * 1000, **(params or {})})
    session.execute(text("DROP TABLE nb_fire, nb_burn"))


def neighbourhood(session: Session, points: list[tuple[int, float, float]], t_end: datetime,
                  days: int = model.NEIGHBOUR_DAYS, radius_km: float = model.NEIGHBOUR_RADIUS_KM
                  ) -> dict[int, tuple[int, int, float]]:
    """fill_neighbourhood() for a list of (id, lon, lat), returned as {id: (fires, unseen, hectares)}."""
    if not points:
        return {}
    session.execute(text("DROP TABLE IF EXISTS nb_point"))
    session.execute(text("CREATE TEMP TABLE nb_point (k bigint PRIMARY KEY, g geography, fires int, unseen int, "
                         "ha float8) ON COMMIT DROP"))
    driver = session.connection().connection.driver_connection  # the psycopg 3 connection
    with driver.cursor() as cur, cur.copy("COPY nb_point (k, g) FROM STDIN") as copy:
        for pid, lon, lat in points:
            copy.write_row((pid, f"SRID=4326;POINT({float(lon)!r} {float(lat)!r})"))
    fill_neighbourhood(session, "nb_point", t_end, days=days, radius_km=radius_km)
    rows = session.execute(text("SELECT k, fires, unseen, ha FROM nb_point")).all()
    session.execute(text("DROP TABLE nb_point"))
    return {int(r[0]): (int(r[1]), int(r[2]), float(r[3])) for r in rows}


# --- 2b. last season's fires (NASA FIRMS archive, cached on disk) ---------------------------------------


@dataclass
class History:
    points: np.ndarray  # (n, 2) lon, lat of vegetation fires (nominal/high confidence)
    season: str
    fetch_ids: list[int]
    clipped_to_india: bool
    reason: str | None = None
    kept: int | None = None  # points left after the clip to India (load_history)


def _season(as_of: datetime) -> tuple[date, date]:
    year = as_of.year - 1
    return date(year, *SEASON[0]), date(year, *SEASON[1])


def last_season_fires(as_of: datetime) -> History:
    """Last season's FIRMS vegetation fires over the area we watch.

    Fetched once through the area API (needs FIRMS_MAP_KEY; 5 days per call, ~16 calls) and kept
    on disk, since a past season never changes. Only `type` 0 ("presumed vegetation fire") is
    kept, which drops brick kilns and other static industrial heat sources, and low-confidence
    pixels are dropped. A season is only cached when every call succeeded."""
    start, end = _season(as_of)
    label = f"{start.isoformat()}/{end.isoformat()}"
    cache = get_settings().cache_path("risk", f"firms_{HISTORY_PRODUCT}_{start}_{end}.csv")
    meta_path = cache.with_suffix(".json")
    if cache.exists() and meta_path.exists():
        df = pd.read_csv(cache)
        meta = orjson.loads(meta_path.read_bytes())
        return History(df[["longitude", "latitude"]].to_numpy(float), label, meta.get("fetch_ids", []), False)
    key = get_settings().firms_map_key
    if key is None:
        return History(np.empty((0, 2)), label, [], False, "FIRMS_MAP_KEY is not set, so last season cannot be fetched")
    area = ",".join(f"{v:g}" for v in AOI_BBOX)
    frames, fetch_ids = [], []
    day = start
    try:
        while day <= end:
            span = min(FIRMS_MAX_DAYS, (end - day).days + 1)
            result = fetch("firms", f"{FIRMS_AREA}/{key.get_secret_value()}/{HISTORY_PRODUCT}/{area}/{span}/{day}",
                           expect="text", meta={"product": HISTORY_PRODUCT, "day": str(day), "days": span,
                                                "purpose": "last season, burn-risk history"})
            df = pd.read_csv(io.StringIO(result.data), dtype={"acq_time": str})
            if "type" in df.columns:
                df = df[df["type"] == 0]
            df = df[df["confidence"].astype(str).str.lower().str[0] != "l"]
            set_fetch_records(result.fetch_id, len(df))
            if result.fetch_id is not None:
                fetch_ids.append(result.fetch_id)
            frames.append(df[["latitude", "longitude", "acq_date", "acq_time", "confidence", "frp"]])
            day += timedelta(days=span)
    except FetchError as exc:
        return History(np.empty((0, 2)), label, fetch_ids, False, f"FIRMS archive fetch failed: {exc}")
    season_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["latitude", "longitude", "acq_date", "acq_time", "confidence", "frp"])
    tmp = cache.with_suffix(".tmp")
    season_df.to_csv(tmp, index=False)
    tmp.replace(cache)
    meta_path.write_bytes(orjson.dumps({"fetch_ids": fetch_ids, "product": HISTORY_PRODUCT, "season": label,
                                        "rows": len(season_df), "fetched_at": utcnow()},
                                       option=orjson.OPT_UTC_Z))
    return History(season_df[["longitude", "latitude"]].to_numpy(float), label, fetch_ids, False)


def load_history(session: Session, history: History) -> History:
    """Copy last season's points into a temporary table `rk_hist` (geography, indexed) and keep
    only those inside our (Indian) state polygons: the area we watch also holds Pakistan's Punjab
    (SYNTHESIS section 1 #1b). The clip runs in the database, so the state outlines (17.8 MB at
    full resolution) never travel; unclipped when boundaries are not loaded yet. Returns the
    history with `kept` = how many points are left."""
    session.execute(text("DROP TABLE IF EXISTS rk_hist"))
    session.execute(text("CREATE TEMP TABLE rk_hist (lon float8, lat float8, g geography) ON COMMIT DROP"))
    if history.reason is not None or len(history.points) == 0:
        return history
    driver = session.connection().connection.driver_connection
    with driver.cursor() as cur, cur.copy("COPY rk_hist (lon, lat) FROM STDIN") as copy:
        for lon, lat in history.points:
            copy.write_row((float(lon), float(lat)))
    session.execute(text("UPDATE rk_hist SET g = ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography"))
    clipped = False
    if _exists(session, "boundary") and session.execute(
            text("SELECT EXISTS (SELECT 1 FROM boundary WHERE level = 'state')")).scalar_one():
        session.execute(text("""
            DELETE FROM rk_hist h WHERE NOT EXISTS (
                SELECT 1 FROM boundary b WHERE b.level = 'state' AND ST_Intersects(b.geom, h.g::geometry))
        """))
        clipped = True
    session.execute(text("CREATE INDEX ON rk_hist USING gist (g)"))
    session.execute(text("ANALYZE rk_hist"))
    kept = session.execute(text("SELECT count(*) FROM rk_hist")).scalar_one()
    return History(history.points, history.season, history.fetch_ids, clipped, history.reason, kept=int(kept))


def fill_history(session: Session, table: str, history: History,
                 radius_km: float = model.NEIGHBOUR_RADIUS_KM) -> None:
    """Set `hist` (last season's fires within radius_km, from rk_hist) on every row of `table`; left
    NULL when last season could not be fetched, so the factor is reported missing, not zero."""
    if history.reason is not None:
        return
    session.execute(text(f"""
        UPDATE {table} AS t SET hist = (SELECT count(*) FROM rk_hist h WHERE ST_DWithin(h.g, t.g, :m))
    """), {"m": radius_km * 1000})


def count_within(points: np.ndarray, lons: np.ndarray, lats: np.ndarray, radius_km: float) -> np.ndarray:
    """How many `points` lie within radius_km of each (lon, lat), on a local flat-earth projection
    (accurate to well under 1% over 5 km). A k-d tree makes it fast for thousands of each."""
    if len(points) == 0 or len(lons) == 0:
        return np.zeros(len(lons), dtype=int)
    lat0 = math.radians(float(np.mean(lats)))
    kx = KM_PER_DEG * math.cos(lat0)
    tree = cKDTree(np.c_[points[:, 0] * kx, points[:, 1] * KM_PER_DEG])
    hits = tree.query_ball_point(np.c_[lons * kx, lats * KM_PER_DEG], r=radius_km, return_length=True)
    return np.asarray(hits, dtype=int)


# --- 2c. rain forecast (Open-Meteo) ---------------------------------------------------------------------


@dataclass
class Rain:
    by_cell: dict[tuple[float, float], tuple[float, float]]  # cell -> (next 72 h, past 24 h) mm
    fetch_ids: list[int]
    reason: str | None = None


def _centre(ix: int, iy: int) -> tuple[float, float]:
    """The centre of rain cell (ix, iy) = (floor(lon / 0.1), floor(lat / 0.1))."""
    return round((ix + 0.5) * RAIN_CELL_DEG, 3), round((iy + 0.5) * RAIN_CELL_DEG, 3)


def _cell(lon: float, lat: float) -> tuple[float, float]:
    return _centre(math.floor(lon / RAIN_CELL_DEG), math.floor(lat / RAIN_CELL_DEG))


def rain_forecast(cells: list[tuple[float, float]], now: datetime) -> Rain:
    """Hourly precipitation (Open-Meteo best_match) summed over the next 72 h and the past 24 h,
    for each cell centre. POST, since a long list of coordinates does not fit in a URL."""
    out: dict[tuple[float, float], tuple[float, float]] = {}
    ids: list[int] = []
    t_now = now.timestamp()
    for i in range(0, len(cells), OPEN_METEO_MAX_POINTS):
        chunk = cells[i:i + OPEN_METEO_MAX_POINTS]
        try:
            result = fetch("openmeteo", OPEN_METEO, method="POST", data={
                "latitude": ",".join(f"{c[1]:.3f}" for c in chunk),
                "longitude": ",".join(f"{c[0]:.3f}" for c in chunk),
                "hourly": "precipitation", "past_days": 1, "forecast_days": 4,
                "timeformat": "unixtime", "timezone": "GMT", "models": "best_match",
            }, meta={"purpose": "burn-risk rain forecast", "points": len(chunk)})
        except FetchError as exc:
            return Rain(out, ids, f"Open-Meteo forecast failed: {exc}")
        data = result.data if isinstance(result.data, list) else [result.data]
        for cell, loc in zip(chunk, data, strict=False):
            hourly = loc.get("hourly") or {}
            times = np.asarray(hourly.get("time") or [], dtype=float)
            rain = np.asarray([v if v is not None else np.nan for v in hourly.get("precipitation") or []], dtype=float)
            nxt = (times > t_now) & (times <= t_now + 72 * 3600)
            past = (times > t_now - 24 * 3600) & (times <= t_now)
            if nxt.sum() < 60:  # fewer than 60 of the 72 hours: do not pretend to know
                continue
            out[cell] = (float(np.nansum(rain[nxt])), float(np.nansum(rain[past])))
        set_fetch_records(result.fetch_id, len(data))
        if result.fetch_id is not None:
            ids.append(result.fetch_id)
    return Rain(out, ids)


# --- the job --------------------------------------------------------------------------------------------
#
# Everything per field stays in the database: the fields go into a temporary table, their factors are
# filled in by set-based SQL, the score is computed by analytics.risk.score_sql (the same formula as
# score_fields, checked against it on a sample every run) and written back from there. Only the rain
# cells, a sample of a few hundred rows and a few summary numbers travel (Neon bills egress; pulling
# every field out and back cost ~105 MB a run).

FIELD_COLUMNS = ("k bigint PRIMARY KEY, g geography, days float8, ix int, iy int, fires int NOT NULL DEFAULT 0, "
                 "unseen int NOT NULL DEFAULT 0, ha float8 NOT NULL DEFAULT 0, hist int, r72 float8, r24 float8, ")
SELF_CHECK_TOP = 100  # the self-check re-scores the top of the list in Python ...
SELF_CHECK_EVERY = 997  # ... and every 997th field by id (~170 more)
# the rounding unit of each ROW_VALUES position: Python rounds binary doubles, Postgres rounds decimals,
# so an exact half may come out one unit apart; anything more is a real disagreement
_ROUNDING = (0.01, 0.01, 0.01, 0.01, 0.001, 0, 0, 0.01, 0.001, 0, 0.001, 0.1, 0.1)


def _days_sql(t: str, after: str, before: str) -> str:
    """The midpoint of days_since_harvest(), operation for operation (so the same double)."""
    day = "CAST(86400 AS float8)"
    return (f"greatest(CAST(0 AS float8), (CAST(extract(epoch FROM {t} - {after}) AS float8) / {day}"
            f" + CAST(extract(epoch FROM {t} - {before}) AS float8) / {day}) / CAST(2 AS float8))")


@job("risk", every_minutes=180, after="sentinel")
def risk() -> dict[str, Any]:
    """Score harvested-but-unburnt fields for burn risk (harvest_cluster.risk_*), then backtest."""
    as_of = utcnow()
    season = last_season_fires(as_of)
    summary: dict[str, Any] = {"as_of": as_of, "history_season": season.season, "history_reason": season.reason}
    run_id, changed, n = None, 0, 0
    tested: dict[str, Any] | None = None
    with session_scope() as session:
        if not _exists(session, "harvest_cluster"):
            return {"skipped": "harvest_cluster does not exist yet (the sentinel module creates it)"}
        n = _load_field_table(session, as_of)
        summary["fields"] = n
        if n:
            history = load_history(session, season)
            fill_neighbourhood(session, "rk", as_of)
            fill_history(session, "rk", history)
            rain = _fill_rain(session, as_of)
            for stmt in model.score_sql("rk"):
                session.execute(text(stmt))
            shared, checked = _self_check(session, as_of, history, rain)
            run_id = _insert_run(session, shared, as_of, n)
            session.execute(text("DROP TABLE IF EXISTS risk_stage"))
            session.execute(text("CREATE TEMP TABLE risk_stage ON COMMIT DROP AS "
                                 "SELECT k AS id, s AS score, to_jsonb(v) AS v FROM rk"))
            changed = _apply_stage(session, run_id)
            tested = {"run_id": run_id, **_tested_order(session)}
            summary["tested_order"] = {k: tested[k] for k in ("spearman_with_risk_score", "top_1pct_overlap")}
            summary.update(scored=n, run_id=run_id, rows_rewritten=changed, rain_cells=len(rain.by_cell),
                           rain_reason=rain.reason, history_fires=history.kept, self_checked=checked,
                           **_score_stats(session))
        else:
            summary["skipped_scoring"] = "no harvested-but-unburnt fields yet"
    if run_id is not None and changed > VACUUM_SHARE * n:
        vacuum("harvest_cluster")
    try:
        with session_scope() as session:
            report = backtest(session, season, as_of)
        if tested is not None and "headline" in report and not report.get("timing_tested"):
            report["tested_order"] = tested  # timing tested one day: the full score is then the tested one
        _store_report(report, run_id)
        head = report.get("headline") or {}
        all_pairs = report.get("all_pairs") or {}
        summary["backtest"] = ({**{k: head.get(k) for k in ("tile", "auc", "auc_ci95", "burnt", "unburnt")},
                                "all_pairs": {k: all_pairs.get(k) for k in ("pairs", "auc_weighted_mean", "auc_ci95")}}
                               if head else report.get("reason"))
    except Exception as exc:  # the scores above are saved; a backtest problem must not undo them
        log.exception("risk backtest failed")
        summary["backtest_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return summary


def _load_field_table(session: Session, as_of: datetime) -> int:
    """Every harvested-but-unburnt field into the temporary table `rk` (its point, days since
    harvest at `as_of`, its 0.1 degree rain cell). Returns how many."""
    session.execute(text("DROP TABLE IF EXISTS rk"))
    session.execute(text(f"CREATE TEMP TABLE rk ({FIELD_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
    session.execute(text(f"""
        INSERT INTO rk (k, g, days, ix, iy)
        SELECT id, c::geography,
               CASE WHEN harvested_after IS NULL OR harvested_before IS NULL THEN NULL
                    ELSE {_days_sql("CAST(:t AS timestamptz)", "harvested_after", "harvested_before")} END,
               CAST(floor(ST_X(c) / :cell) AS int), CAST(floor(ST_Y(c) / :cell) AS int)
        FROM (SELECT id, coalesce(centroid, ST_PointOnSurface(geom)) AS c, harvested_after, harvested_before
              FROM harvest_cluster) AS h
    """), {"t": as_of, "cell": RAIN_CELL_DEG})
    session.execute(text("ANALYZE rk"))
    return int(session.execute(text("SELECT count(*) FROM rk")).scalar_one())


def _fill_rain(session: Session, as_of: datetime) -> Rain:
    """Ask Open-Meteo for the rain of every cell that holds a field and set r72/r24 on `rk`."""
    cells = [(int(ix), int(iy)) for ix, iy in session.execute(text("SELECT DISTINCT ix, iy FROM rk")).all()]
    rain = rain_forecast(sorted({_centre(ix, iy) for ix, iy in cells}), as_of)
    session.execute(text("DROP TABLE IF EXISTS rk_rain"))
    session.execute(text("CREATE TEMP TABLE rk_rain (ix int, iy int, r72 float8, r24 float8) ON COMMIT DROP"))
    driver = session.connection().connection.driver_connection
    with driver.cursor() as cur, cur.copy("COPY rk_rain (ix, iy, r72, r24) FROM STDIN") as copy:
        for ix, iy in cells:
            wet = rain.by_cell.get(_centre(ix, iy))
            if wet is not None:
                copy.write_row((ix, iy, wet[0], wet[1]))
    session.execute(text("UPDATE rk SET r72 = r.r72, r24 = r.r24 FROM rk_rain r WHERE rk.ix = r.ix AND rk.iy = r.iy"))
    return rain


def _annotate(factors: dict[str, Any], history: History, rain: Rain) -> dict[str, Any]:
    """Say where the inputs came from, so a reader can follow every number (the exact upstream
    calls, source_fetch ids, let GET /api/risk cite them)."""
    factors["history"].update(season=history.season, source="NASA FIRMS " + HISTORY_PRODUCT,
                              clipped_to_india=history.clipped_to_india, reason=history.reason)
    factors["dryness"].update(source="Open-Meteo best_match", reason=rain.reason)
    factors["fetch_ids"] = {"rain": rain.fetch_ids, "history": history.fetch_ids}
    return factors


def _self_check(session: Session, as_of: datetime, history: History, rain: Rain) -> tuple[dict[str, Any], int]:
    """Re-score the top of the list and a spread of other fields with score_fields() (using the run's
    typical counts) and compare every stored number with what SQL computed. Raises before anything
    is stored if they disagree. Returns the run's shared factors (risk_run.factors) and the count."""
    nt, ht, hst = (float(v) for v in session.execute(text("SELECT nt, ht, hst FROM rk_typ")).one())
    rows = session.execute(text("""
        (SELECT k, days, fires, unseen, ha, hist, r72, r24, v FROM rk ORDER BY s DESC NULLS LAST, k LIMIT :top)
        UNION (SELECT k, days, fires, unseen, ha, hist, r72, r24, v FROM rk WHERE k % :every = 0)
    """), {"top": SELF_CHECK_TOP, "every": SELF_CHECK_EVERY}).all()
    feats = [model.FieldFeatures(id=int(r[0]), days_since_harvest=r[1], fires_7d=int(r[2]), unseen_burns_7d=int(r[3]),
                                 burned_ha_7d=float(r[4]), fires_last_season=r[5], rain_next_72h_mm=r[6],
                                 rain_past_24h_mm=r[7]) for r in rows]
    scored = model.score_fields(feats, as_of, typ=(nt, ht, hst))
    bad = []
    for r, s in zip(rows, scored, strict=True):
        want, got = model.row_values(s.factors), orjson.loads(r[8])
        for i, (a, b) in enumerate(zip(want, got, strict=True)):
            if (a is None) != (b is None) or (a is not None and abs(a - b) > _ROUNDING[i] + 1e-9):
                bad.append((r[0], model.ROW_VALUES[i], a, b))
    if bad:
        raise RuntimeError(f"the SQL scorer disagrees with score_fields on {len(bad)} values, e.g. {bad[:3]}")
    return model.run_constants(_annotate(scored[0].factors, history, rain)), len(rows)


def _score_stats(session: Session) -> dict[str, Any]:
    """How the run's scores spread out, and how many fields share the top score (should be few)."""
    r = session.execute(text("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s), percentile_cont(0.9) WITHIN GROUP (ORDER BY s),
               max(s), count(*) FILTER (WHERE s = (SELECT max(s) FROM rk)),
               (SELECT count(DISTINCT s) FROM (SELECT s FROM rk ORDER BY s DESC NULLS LAST LIMIT 500) t),
               (SELECT min(s) FROM (SELECT s FROM rk ORDER BY s DESC NULLS LAST LIMIT 500) t)
        FROM rk
    """)).one()
    return {"score_p50": _r(r[0], 1), "score_p90": _r(r[1], 1), "score_max": r[2], "fields_at_max": r[3],
            "top500_distinct_scores": r[4], "top500_min": r[5]}


def _tested_order(session: Session) -> dict[str, Any]:
    """Where today's fields fall in the ordering the backtest tested (analytics/risk.py tested_score:
    neighbourhood and history, without timing and rain). The calibration bands (burn_probability)
    describe that ordering, so a field's burn_probability comes from its band in it, not from its
    band by risk_score. Returns the cuts (the lowest tested score inside each band's top share) and
    how closely today's full score follows the tested one (Spearman, top-1% overlap)."""
    x = model.tested_score_sql("nv", "hv")
    edges = [edge for edge, _ in model.BANDS[:-1]]
    cuts = ", ".join(f"min(x) FILTER (WHERE tx <= {edge + 1e-12!r})" for edge in edges)
    r = session.execute(text(f"""
        WITH t AS (SELECT s, {x} AS x FROM rk WHERE nv IS NOT NULL AND s IS NOT NULL),
        r AS (
            SELECT x, cume_dist() OVER (ORDER BY x DESC) AS tx, cume_dist() OVER (ORDER BY s DESC) AS ts,
                   rank() OVER (ORDER BY s) + (count(*) OVER (PARTITION BY s) - 1) / 2.0 AS rs,
                   rank() OVER (ORDER BY x) + (count(*) OVER (PARTITION BY x) - 1) / 2.0 AS rx
            FROM t
        )
        SELECT count(*), corr(rs, rx), {cuts},
               count(*) FILTER (WHERE ts <= 0.01 AND tx <= 0.01), count(*) FILTER (WHERE ts <= 0.01)
        FROM r
    """)).one()
    n, rho = int(r[0]), r[1]
    both, top = int(r[2 + len(edges)]), int(r[3 + len(edges)])
    return {
        "what": ("today's fields ranked by the part of the score the backtest tested (neighbourhood and history, "
                 "without timing and rain): burn_probability is the calibrated share of the field's band in this "
                 "ordering"),
        "fields": n,
        "cuts": [{"band": label, "min_tested_score": None if c is None else float(c)}
                 for (_, label), c in zip(model.BANDS[:-1], r[2:2 + len(edges)], strict=True)],
        "spearman_with_risk_score": _r(float(rho)) if rho is not None else None,
        "top_1pct_overlap": _r(both / top) if top else None,
    }


def _score(fields: list[Field], near: dict[int, tuple[int, int, float]], history: History, rain: Rain,
           as_of: datetime) -> list[model.Scored]:
    """The same scoring in Python, from values already in hand (tests, and small re-scores)."""
    lons = np.array([f.lon for f in fields])
    lats = np.array([f.lat for f in fields])
    hist = count_within(history.points, lons, lats, model.NEIGHBOUR_RADIUS_KM) if history.reason is None else None
    feats = []
    for i, f in enumerate(fields):
        days = rng = None
        if f.harvested_after and f.harvested_before:
            mid, fewest, most = model.days_since_harvest(as_of, f.harvested_after, f.harvested_before)
            days, rng = mid, (fewest, most)
        fires, unseen, burned = near.get(f.id, (0, 0, 0.0))
        wet = rain.by_cell.get(_cell(f.lon, f.lat))
        feats.append(model.FieldFeatures(
            id=f.id, days_since_harvest=days, harvest_days_range=rng, fires_7d=fires, unseen_burns_7d=unseen,
            burned_ha_7d=burned, rain_next_72h_mm=None if wet is None else wet[0],
            rain_past_24h_mm=None if wet is None else wet[1],
            fires_last_season=None if hist is None else int(hist[i]),
        ))
    scored = model.score_fields(feats, as_of)
    for s in scored:
        _annotate(s.factors, history, rain)
    return scored


# --- 3. store -------------------------------------------------------------------------------------------


def _insert_run(session: Session, shared: dict[str, Any], as_of: datetime, n: int) -> int:
    return session.execute(text("""
        INSERT INTO risk_run (as_of, model, fields, factors) VALUES (:as_of, :model, :n, CAST(:f AS jsonb))
        RETURNING id
    """), {"as_of": as_of, "model": model.MODEL_VERSION, "n": n,
           "f": orjson.dumps(shared, option=orjson.OPT_UTC_Z).decode()}).scalar_one()


def _apply_stage(session: Session, run_id: int) -> int:
    """Rewrite only the harvest_cluster rows whose numbers changed, from the temporary risk_stage
    (id, score, v): Postgres writes a whole new row version on every UPDATE, so rewriting all 172k
    rows every run would keep the table twice its size between vacuums. Rows left alone already
    hold what this run computed, which is why readers pair every row with the newest run."""
    changed = session.execute(text("""
        UPDATE harvest_cluster AS h
        SET risk_score = s.score, risk_factors = jsonb_build_object('run', CAST(:run AS bigint), 'v', s.v)
        FROM risk_stage AS s
        WHERE h.id = s.id
          AND (h.risk_score IS DISTINCT FROM s.score OR h.risk_factors -> 'v' IS DISTINCT FROM s.v)
    """), {"run": run_id}).rowcount
    session.execute(text("UPDATE risk_run SET changed = :c WHERE id = :id"), {"c": changed, "id": run_id})
    return changed


def store_scores(scored: list[model.Scored], as_of: datetime) -> tuple[int, int]:
    """Store fields scored in Python: what all fields share into a new risk_run row, each field's own
    numbers onto its harvest_cluster row ({"run": id, "v": "[...]"}, see analytics/risk.py), through
    COPY into risk_stage. Returns (run id, rows rewritten). The run row and the updates commit
    together. (The job itself stages straight from its SQL-scored table.)"""
    shared = model.run_constants(scored[0].factors)
    with session_scope() as session:
        run_id = _insert_run(session, shared, as_of, len(scored))
        session.execute(text(
            "CREATE TEMP TABLE risk_stage (id bigint PRIMARY KEY, score float8, v jsonb) ON COMMIT DROP"))
        driver = session.connection().connection.driver_connection  # the psycopg 3 connection
        with driver.cursor() as cur, cur.copy("COPY risk_stage (id, score, v) FROM STDIN") as copy:
            for s in scored:
                # v is a jsonb *string* holding the numbers as JSON text (analytics/risk.py says why)
                copy.write_row((s.id, s.score, orjson.dumps(model.pack_values(model.row_values(s.factors))).decode()))
        changed = _apply_stage(session, run_id)
    return run_id, changed


VACUUM_SHARE = 0.1  # vacuum harvest_cluster when a run rewrote more than this share of its rows


def vacuum(table: str) -> None:
    """VACUUM (ANALYZE) one table, so the space of the old row versions is reused at once instead
    of whenever autovacuum gets to it (on a 0.5 GB database that matters). Never VACUUM FULL: it
    locks the table and needs a second copy of it on disk."""
    from blindspot.db import engine

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"VACUUM (ANALYZE) {table}"))


# --- 4. backtest ----------------------------------------------------------------------------------------

BT_COLUMNS = ("k bigserial PRIMARY KEY, pair int, label int, fid bigint, g geography, t0 timestamptz, "
              "village text, area float8, days float8, fires int NOT NULL DEFAULT 0, unseen int NOT NULL DEFAULT 0, "
              "ha float8 NOT NULL DEFAULT 0, hist int, r72 float8, r24 float8, ")
TOP_SHARE = 0.1  # "top 10%": the k = round(n / 10) best-ranked fields of each pass pair
PAIRS_ARE = (
    "A pass pair is two consecutive clear Sentinel-2 passes (t0, t1) over one tile, a few days apart. "
    "Its fields are those bare and unburnt at t0: burnt = a burn scar appeared on the field between t0 and "
    "t1; not burnt = still harvested and unburnt on a pass at or after t1. Each field is scored with what "
    "was known at t0 (burning within 5 km in the 7 days before t0, last season's fires); rain and timing "
    "are not tested.")


def backtest(session: Session, history: History, as_of: datetime) -> dict[str, Any]:
    """How well would the score have ranked fields that really burned?

    For every pair of consecutive clear passes (t0 -> t1) of a tile with burn scars:
    - burnt (label 1): scars that appeared between t0 and t1 on fields that were already bare at t0
      (cluster NDVI at t0 below 0.35, the harvest rule's threshold), so they were "harvested and
      unburnt" at t0, exactly the fields the score ranks;
    - not burnt (label 0): fields harvested by t0 and still unburnt on the latest pass (>= t1).
    Each field is scored with what was known at t0: burning within 5 km in the 7 days before t0,
    and last season's fires. Rain is left out (one forecast covers a whole tile, so it cannot
    reorder fields within it), and so is timing: the burn-scar table does not record when a burnt
    field was harvested, so burnt and unburnt fields cannot be compared on it.
    Caveat: fields harvested by t0 that were tilled or sown (not burnt) by t1 are in neither table,
    so the unburnt group only holds fields still waiting.
    Runs in the database (temporary table bt): only per-pair counts and the headline pair's fields
    come back. Intervals: the headline AUC by resampling its fields (optimistic: neighbouring fields
    share their neighbourhood), the all-pairs numbers by resampling whole tiles.
    """
    if not (_exists(session, "burn_cluster") and _exists(session, "harvest_cluster")):
        return {"reason": "burn_cluster / harvest_cluster do not exist yet", "windows": []}
    burn_cols = _columns(session, "burn_cluster")
    cols = burn_cols & _columns(session, "harvest_cluster")
    scope = "scope" if "scope" in cols else None
    # If the burn-scar table records when each burnt field was harvested, timing can be tested too.
    # Today it does not, so this stays off until it does.
    timing_cols = {"harvested_after", "harvested_before"} <= burn_cols
    windows = session.execute(text(f"""
        SELECT mgrs_tile, {scope or "NULL"} AS scope, burn_after, burn_before, count(*) AS n
        FROM burn_cluster WHERE ndvi_pre < :ndvi
        GROUP BY 1, 2, 3, 4 HAVING count(*) >= :min ORDER BY burn_before, mgrs_tile
    """), {"ndvi": model.HARVESTED_NDVI_MAX, "min": BACKTEST_MIN_EACH}).all()
    report: dict[str, Any] = {
        "computed_at": as_of, "model": model.MODEL_VERSION, "windows": [],
        "method": backtest.__doc__.strip().replace("\n    ", " ") if backtest.__doc__ else None,
        "pairs_are": PAIRS_ARE, "timing_tested": timing_cols,
        "timing_note": None if timing_cols else (
            "timing is not backtested: burn_cluster has no harvested_after/harvested_before, so the "
            "harvest date of a field that burnt is unknown"),
    }
    if not windows:
        report["reason"] = "no burn scars on fields that were bare before burning yet"
        return report
    _load_pairs(session, windows, scope, timing_cols)
    counts = {int(p): (int(b), int(u)) for p, b, u in session.execute(text(
        "SELECT pair, sum(label), count(*) - sum(label) FROM bt GROUP BY pair")).all()}
    keep = [i for i in range(len(windows)) if min(counts.get(i, (0, 0))) >= BACKTEST_MIN_EACH]
    session.execute(text("DELETE FROM bt WHERE NOT (pair = ANY(:keep))"), {"keep": keep})
    if not keep:
        report["windows"] = [_window(w, counts.get(i, (0, 0))) for i, w in enumerate(windows)]
        report["reason"] = "no pass pair has enough burnt and unburnt fields yet"
        return report
    for (t0,) in session.execute(text("SELECT DISTINCT t0 FROM bt ORDER BY 1")).all():
        fill_neighbourhood(session, "bt", t0, "p.t0 = :t0", {"t0": t0})
    loaded = load_history(session, history)
    fill_history(session, "bt", loaded)
    for stmt in model.score_sql("bt", "pair"):
        session.execute(text(stmt))
    history_ok = history.reason is None
    per_pair = _pair_metrics(session, history_ok, timing_cols)
    done = []
    for i, w in enumerate(windows):
        row = _window(w, counts.get(i, (0, 0)))
        if i in per_pair:
            row.update(per_pair[i], pair=i)
            done.append(row)
        report["windows"].append(row)
    # The headline is the latest pass pair, not the best one: its unburnt fields were seen waiting
    # at t0 and still waiting a few days later. On earlier pairs the unburnt group is fields that sat
    # unburnt for 10-20 days (the table only keeps today's waiting fields), a different population.
    latest = max(done, key=lambda r: (r["t1"], r["burnt"] + r["unburnt"]))
    report["headline"] = _headline(session, latest, history_ok, timing_cols)
    report["all_pairs"], report["calibration"] = _all_pairs(done)
    report["verdict"] = verdict(report)
    report["strength"] = ranking_strength(report["all_pairs"])
    return report


def _window(w: Any, counts: tuple[int, int]) -> dict[str, Any]:
    tile, _scope, t0, t1, _n = w
    out: dict[str, Any] = {"tile": tile, "t0": t0, "t1": t1, "burnt": counts[0], "unburnt": counts[1]}
    if min(counts) < BACKTEST_MIN_EACH:
        out["skipped"] = f"needs at least {BACKTEST_MIN_EACH} of each"
    return out


def _load_pairs(session: Session, windows: list[Any], scope: str | None, timing_cols: bool) -> None:
    """The fields of every pass pair into the temporary table `bt`, burnt and not burnt, in two
    statements (pair = the window's index)."""
    session.execute(text("DROP TABLE IF EXISTS bt_win, bt"))
    session.execute(text("CREATE TEMP TABLE bt_win (pair int, tile text, scope text, t0 timestamptz, "
                         "t1 timestamptz) ON COMMIT DROP"))
    session.execute(text(f"CREATE TEMP TABLE bt ({BT_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
    session.execute(text("INSERT INTO bt_win VALUES (:pair, :tile, :scope, :t0, :t1)"),
                    [{"pair": i, "tile": w[0], "scope": w[1], "t0": w[2], "t1": w[3]} for i, w in enumerate(windows)])
    same_scope = "AND x.scope IS NOT DISTINCT FROM w.scope" if scope else ""

    def days(t: str) -> str:
        return _days_sql(t, "x.harvested_after", "x.harvested_before") if timing_cols else "NULL"

    session.execute(text(f"""
        INSERT INTO bt (pair, label, fid, g, t0, village, area, days)
        SELECT w.pair, 1, x.id, coalesce(x.centroid, ST_PointOnSurface(x.geom))::geography, w.t0, x.village,
               x.area_ha, {days("w.t0")}
        FROM bt_win w JOIN burn_cluster x ON x.mgrs_tile = w.tile {same_scope}
             AND x.burn_after = w.t0 AND x.burn_before = w.t1 AND x.ndvi_pre < :ndvi
        {"WHERE x.harvested_after IS NOT NULL AND x.harvested_before IS NOT NULL" if timing_cols else ""}
    """), {"ndvi": model.HARVESTED_NDVI_MAX})
    tol = timedelta(hours=12)  # passes of one tile are days apart; this only absorbs rounding
    session.execute(text(f"""
        INSERT INTO bt (pair, label, fid, g, t0, village, area, days)
        SELECT w.pair, 0, x.id, coalesce(x.centroid, ST_PointOnSurface(x.geom))::geography, w.t0, x.village,
               x.area_ha, {days("w.t0")}
        FROM bt_win w JOIN harvest_cluster x ON x.mgrs_tile = w.tile {same_scope}
             AND x.harvested_before <= w.t0 + :tol AND x.last_seen_unburnt >= w.t1 - :tol
    """), {"tol": tol})
    session.execute(text("CREATE INDEX ON bt (t0)"))
    session.execute(text("ANALYZE bt"))


def _avg_rank(expr: str) -> str:
    """Rank within the pair, ties sharing their average rank (as roc_auc does)."""
    return (f"(rank() OVER (PARTITION BY pair ORDER BY {expr}) "
            f"+ (count(*) OVER (PARTITION BY pair, {expr}) - 1) / 2.0)")


def _band_case(expr: str) -> str:
    whens = " ".join(f"WHEN {expr} <= {edge + 1e-12!r} THEN {i}" for i, (edge, _) in enumerate(model.BANDS))
    return f"CASE {whens} ELSE {len(model.BANDS) - 1} END"


def _pair_metrics(session: Session, history_ok: bool, timing: bool = False) -> dict[int, dict[str, Any]]:
    """Per pass pair, computed in the database: AUC of the score and of each single-factor baseline
    (rank sums), the burnt fields among the top 10% (ties at the cut counted at their average, as in
    precision_at_k) and the fields and burns in each calibration band."""
    feats = {"s": "s", "fires_7d_only": "fires", "neighbourhood_only": "fires + unseen",
             "burned_ha_7d_only": "ha", "history_only": "coalesce(hist, 0)"}
    if timing:
        feats["timing_only"] = "coalesce(tv, 0)"
    ranks = ", ".join(f"{_avg_rank(e)} AS r_{i}" for i, e in enumerate(feats.values()))
    sums = ", ".join(f"sum(r_{i}) FILTER (WHERE label = 1)" for i in range(len(feats)))
    bands = ", ".join(f"count(*) FILTER (WHERE band = {i}), coalesce(sum(label) FILTER (WHERE band = {i}), 0)"
                      for i in range(len(model.BANDS)))
    rows = session.execute(text(f"""
        WITH r0 AS (
            SELECT pair, label, s, {ranks},
                   cume_dist() OVER (PARTITION BY pair ORDER BY s DESC) AS top,
                   row_number() OVER (PARTITION BY pair ORDER BY s DESC) AS rn,
                   count(*) OVER (PARTITION BY pair) AS n
            FROM bt
        ), r AS (
            SELECT *, {_band_case("top")} AS band FROM r0
        ), cut AS (
            SELECT pair, s AS cut, rn AS k FROM r WHERE rn = greatest(1, round(n * {TOP_SHARE!r}))
        )
        SELECT r.pair, count(*), sum(label), {sums}, c.k,
               coalesce(sum(label) FILTER (WHERE r.s > c.cut), 0), count(*) FILTER (WHERE r.s > c.cut),
               avg(label) FILTER (WHERE r.s = c.cut), {bands}
        FROM r JOIN cut c USING (pair) GROUP BY r.pair, c.k
    """)).all()
    out = {}
    nf = len(feats)
    for row in rows:
        n, pos = int(row[1]), int(row[2])
        neg = n - pos
        aucs = [None if v is None else (float(v) - pos * (pos + 1) / 2) / (pos * neg) for v in row[3:3 + nf]]
        k, above_hits, above_n, tied = int(row[3 + nf]), int(row[4 + nf]), int(row[5 + nf]), float(row[6 + nf])
        b = row[7 + nf:]
        out[int(row[0])] = {
            "base_rate": _r(pos / n), "auc": _r(aucs[0]),
            "baselines_auc": {name: (_r(a) if name != "history_only" or history_ok else None)
                              for name, a in zip(list(feats)[1:], aucs[1:], strict=True)},
            "top_k": k, "top_hits": round(above_hits + (k - above_n) * tied, 3),
            "bands": [[int(b[2 * i]), int(b[2 * i + 1])] for i in range(len(model.BANDS))],
        }
    return out


def _headline(session: Session, pair: dict[str, Any], history_ok: bool, timing: bool = False) -> dict[str, Any]:
    """Everything about one pass pair, from its fields (a thousand or so rows): precision and lift at
    k with Wilson intervals, the AUC with a bootstrap interval, the baselines, and the villages."""
    rows = session.execute(text("""
        SELECT label, s, fires, fires + unseen, ha, coalesce(hist, 0), village, area, coalesce(tv, 0) FROM bt
        WHERE pair = :pair ORDER BY label DESC, k
    """), {"pair": pair["pair"]}).all()
    labels = [int(r[0]) for r in rows]
    full = [float(r[1]) for r in rows]
    n, pos = len(labels), sum(labels)
    base = pos / n
    ks = [k for k in BACKTEST_KS if k <= n // 2]

    def prec(scores: list[float]) -> dict[str, float | None]:
        return {str(k): _r(model.precision_at_k(scores, labels, k)) for k in ks}

    p = prec(full)
    hits = {k: round((v or 0) * int(k)) for k, v in p.items()}
    ci = {k: [_r(v) for v in (model.wilson_interval(hits[k], int(k)) or (None, None))] for k in p}
    baselines: dict[str, Any] = {"random": {"auc": 0.5, "precision": _r(base)}}
    singles = [("fires_7d_only", 2), ("neighbourhood_only", 3), ("burned_ha_7d_only", 4), ("history_only", 5)]
    for name, col in singles + ([("timing_only", 8)] if timing else []):
        if name == "history_only" and not history_ok:
            baselines[name] = {"auc": None, "precision_at_k": None}
            continue
        x = [float(r[col]) for r in rows]
        baselines[name] = {"auc": _r(model.roc_auc(x, labels)), "precision_at_k": prec(x)}
    auc_ci = model.auc_ci(full, labels)
    return {
        "tile": pair["tile"], "t0": pair["t0"], "t1": pair["t1"],
        "gap_days": round((pair["t1"] - pair["t0"]).total_seconds() / 86400, 1),
        "burnt": pos, "unburnt": n - pos, "n": n, "base_rate": _r(base),
        "auc": _r(model.roc_auc(full, labels)), "auc_ci95": None if auc_ci is None else [_r(v) for v in auc_ci],
        "precision_at_k": p, "precision_ci95": ci,
        "lift_at_k": {k: _r(v / base) if v is not None and base > 0 else None for k, v in p.items()},
        "lift_ci95": {k: [_r(v / base) if v is not None and base > 0 else None for v in ci[k]] for k in ci},
        "hits_at_k": hits,
        "ci_note": ("precision and lift: Wilson 95% intervals (at k = 10 one field more or less moves "
                    "precision by 0.1); AUC: 95% bootstrap over this pair's fields, narrower than the "
                    "truth because neighbouring fields share their neighbourhood"),
        "baselines": baselines,
        "factors_used": (["timing"] if timing else []) + ["neighbourhood"] + (["history"] if history_ok else []),
        "villages": _village_level([r[6] for r in rows], [float(r[7] or 0) for r in rows], full, labels),
        "why_this_pair": "the latest pass pair (the cleanest unburnt control group)",
    }


def _all_pairs(done: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The all-pairs summary and the calibration table, with tile-bootstrap intervals."""
    pairs = [{**r, "n": r["burnt"] + r["unburnt"]} for r in done]
    tb = model.tile_bootstrap(pairs)
    n = sum(r["n"] for r in pairs)
    baselines = {}
    for name in pairs[0]["baselines_auc"]:
        have = [r for r in pairs if r["baselines_auc"][name] is not None]
        baselines[name] = (_r(sum(r["baselines_auc"][name] * r["n"] for r in have) / sum(r["n"] for r in have))
                           if have else None)
    gaps = [(r["t1"] - r["t0"]).total_seconds() / 86400 for r in pairs]
    by_tile: dict[str, list[dict[str, Any]]] = {}
    for r in pairs:
        by_tile.setdefault(r["tile"], []).append(r)
    tiles = []
    for tile, rows in by_tile.items():
        scored = [r for r in rows if r["auc"] is not None]
        weight = sum(r["n"] for r in scored)
        tiles.append({"tile": tile, "pairs": len(rows), "field_checks": sum(r["n"] for r in rows),
                      "auc": _r(sum(r["auc"] * r["n"] for r in scored) / weight) if weight else None})
    tiles.sort(key=lambda t: -t["field_checks"])
    all_pairs = {
        "pairs": len(pairs), "field_pairs": n, "tiles": tb["tiles"],
        "auc_weighted_mean": tb["auc_weighted_mean"], "auc_ci95": tb["auc_ci95"],
        "top_10pct": tb["top_10pct"], "baselines_auc_weighted_mean": baselines,
        "what": PAIRS_ARE,
        "ci_method": f"95% interval from {tb['reps']} resamples of whole tiles (pairs of one tile share fields)",
        "auc_by_pair": [{"tile": r["tile"], "t0": r["t0"], "t1": r["t1"], "auc": r["auc"], "base_rate": r["base_rate"],
                         "burnt": r["burnt"], "unburnt": r["unburnt"]} for r in pairs],
        "auc_by_tile": tiles,  # size-weighted mean AUC of each tile's pairs, most field checks first
    }
    for r in done:  # the stored windows keep their counts, not the per-band detail
        r.pop("bands", None)
        r.pop("pair", None)
    calibration = {
        "bands": [{"top_share_max": edge, **b} for (edge, _), b in zip(model.BANDS, tb["bands"], strict=True)],
        "base_rate": tb["top_10pct"]["base_rate"],
        "horizon": f"by the next clear Sentinel-2 pass ({min(gaps):.0f}-{max(gaps):.0f} days later)",
        "what": ("Of the fields ranked in this band of their pass pair, the share that burnt by the next clear "
                 "pass, pooled over every pair; 95% interval from resampling whole tiles. Pairs differ a lot "
                 "(early in the season most waiting fields burnt), so read it as a rough frequency."),
    }
    return all_pairs, calibration


def ranking_strength(all_pairs: dict[str, Any]) -> str | None:
    """"weak" when the all-pairs AUC is under 0.6 or its interval reaches 0.5, else "modest" (under
    0.7) or "moderate". Words for the method note and the verdict, decided by the numbers."""
    point, ci = all_pairs.get("auc_weighted_mean"), all_pairs.get("auc_ci95") or [None, None]
    if point is None or ci[0] is None:
        return None
    return "weak" if point < 0.6 or ci[0] <= 0.5 else "modest" if point < 0.7 else "moderate"


def verdict(report: dict[str, Any]) -> str | None:
    """One plain paragraph on how good the ranking is, every number taken from the report."""
    a, h = report.get("all_pairs") or {}, report.get("headline") or {}
    if not a or a.get("auc_weighted_mean") is None or None in (a.get("auc_ci95") or [None]):
        return None
    lo, hi = a["auc_ci95"]
    top = a["top_10pct"]
    strength = ranking_strength(a)
    if lo <= 0.5:
        chance = " The interval includes 0.5, so on this evidence it is not clearly better than chance."
    elif lo < 0.55:
        chance = " The interval excludes 0.5: better than chance, but not by much."
    else:
        chance = ""
    out = (f"The ranking is {strength}. Across {a['pairs']} pass pairs ({a['field_pairs']:,} field checks, "
           f"{a['tiles']} tiles) its AUC is {a['auc_weighted_mean']:.3f} (95% CI {lo:.3f}-{hi:.3f}; 0.5 is "
           f"chance, 1 is perfect).{chance}")
    if top.get("lift") is not None and None not in top.get("lift_ci95", [None]):
        out += (f" Fields in the top 10% of each pass burnt by the next clear pass {top['lift']:.2f} times as "
                f"often as the average field (95% CI {top['lift_ci95'][0]:.2f}-{top['lift_ci95'][1]:.2f}; "
                f"{top['burnt_share']:.1%} against {top['base_rate']:.1%}).")
    if h.get("auc") is not None and h.get("auc_ci95"):
        out += (f" On the latest pass pair alone ({h['tile']}, {h['n']:,} fields) the AUC is {h['auc']:.3f} "
                f"(95% CI {h['auc_ci95'][0]:.3f}-{h['auc_ci95'][1]:.3f}, from resampling that pair's fields: narrower "
                f"than the truth, because neighbouring fields share their neighbourhood).")
        out += _tile_spread(a, h["tile"])
    return out + " Use it to choose where to look first, not as a forecast for any single field."


TILE_MIN_CHECKS = 100  # tiles with fewer field checks say too little to compare


def _tile_spread(all_pairs: dict[str, Any], tile: str) -> str:
    """How the headline pair's tile compares with the other tiles (its pair is one tile's luck)."""
    tiles = sorted((t for t in all_pairs.get("auc_by_tile") or []
                    if t.get("auc") is not None and t["field_checks"] > TILE_MIN_CHECKS), key=lambda t: -t["auc"])
    if len(tiles) < 2:
        return ""
    rank = next((i + 1 for i, t in enumerate(tiles) if t["tile"] == tile), None)
    low, high = tiles[-1], tiles[0]
    out = (f" Tile by tile (over {TILE_MIN_CHECKS} field checks) the AUC runs from {low['auc']:.3f} ({low['tile']}, "
           f"{low['field_checks']:,} checks) to {high['auc']:.3f} ({high['tile']})")
    if rank is not None:
        out += f"; {tile} ranks {rank} of {len(tiles)}"
        if rank <= max(1, len(tiles) // 4):
            out += ", so that one pair flatters the ranking"
    return out + "."


def _village_level(villages: list[str | None], areas: list[float], scores: list[float],
                   labels: list[int]) -> dict[str, Any] | None:
    """The same test for villages, which is how field teams work: rank each village by its
    risk-weighted hectares at t0 (sum of score/100 x area, the order GET /api/risk uses) and check
    which had at least one field burn by t1. The naive rival ranks villages by harvested area alone
    (bigger villages have more fields that can burn)."""
    rows: dict[str, list[float]] = {}
    for v, a, s, y in zip(villages, areas, scores, labels, strict=True):
        if v:
            acc = rows.setdefault(v, [0.0, 0.0, 0.0])
            acc[0] += s / 100 * a
            acc[1] += a
            acc[2] = max(acc[2], y)
    if len(rows) < 2 * BACKTEST_MIN_EACH:
        return None if not rows else {"villages": len(rows), "skipped": "too few named villages"}
    weighted = [r[0] for r in rows.values()]
    area = [r[1] for r in rows.values()]
    burnt = [int(r[2]) for r in rows.values()]
    ks = [k for k in (5, 10, 20, 50) if k <= len(rows) // 2]
    return {
        "villages": len(rows), "with_a_burn": sum(burnt), "base_rate": _r(sum(burnt) / len(burnt)),
        "auc": _r(model.roc_auc(weighted, burnt)),
        "precision_at_k": {str(k): _r(model.precision_at_k(weighted, burnt, k)) for k in ks},
        "baseline_area_only": {"auc": _r(model.roc_auc(area, burnt)),
                               "precision_at_k": {str(k): _r(model.precision_at_k(area, burnt, k)) for k in ks}},
        "unnamed_fields": sum(1 for v in villages if not v),
    }


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _store_report(report: dict[str, Any], run_id: int | None = None) -> None:
    """Keep the report on the run's risk_run row (what GET /api/risk serves, also on a serverless
    host with no shared disk) and on disk. A run that found nothing to test (say, while the
    sentinel module is rebuilding its tables) does not erase the last real result: that one stays,
    with a note saying when the newer attempt ran and why it had nothing."""
    path = get_settings().cache_path(*model.BACKTEST_FILE)
    if "headline" not in report:
        previous = load_backtest()
        if previous and "headline" in previous:
            previous["latest_attempt"] = {"computed_at": report.get("computed_at"), "reason": report.get("reason")}
            report = previous
    body = orjson.dumps(report, option=orjson.OPT_UTC_Z | orjson.OPT_NAIVE_UTC, default=str)
    if run_id is not None:
        with session_scope() as session:
            session.execute(text("UPDATE risk_run SET backtest = CAST(:b AS jsonb) WHERE id = :id"),
                            {"b": body.decode(), "id": run_id})
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)


def load_backtest() -> dict[str, Any] | None:
    """The latest backtest report written by the `risk` job (risk_run, else the file), or None."""
    from blindspot.models.risk import latest_backtest

    try:
        with session_scope() as session:
            stored = latest_backtest(session)
        if stored:
            return stored
    except Exception:  # no database: the file is still worth reading
        log.warning("could not read the stored backtest", exc_info=True)
    try:
        return orjson.loads(get_settings().cache_path(*model.BACKTEST_FILE).read_bytes())
    except (OSError, ValueError):
        return None


# --- featured frames --------------------------------------------------------------------------------------


@job("featured", every_minutes=360, after="sentinel")
def featured() -> dict[str, Any]:
    """Refresh the landing page's Sentinel-2 frames, and pre-render the largest scars' chips."""
    from blindspot.api.routers.imagery import (
        refresh_featured,  # the endpoint owns the frame logic
    )

    summary = {"frames": refresh_featured()}
    summary["case_chips"] = prewarm_case_chips()
    return summary


CHIP_PREWARM = 24  # the case pages most likely to be opened: the largest candidate unseen burns


def prewarm_case_chips(limit: int = CHIP_PREWARM) -> dict[str, Any]:
    """Render the before/after chips of the largest candidate unseen burns ahead of time, so their
    case pages (GET /api/scars/{id}) open at once instead of waiting 5-20 s on a first render.

    The chip box and URLs come from the scars module's own helpers, so the cache keys are exactly
    the ones its pages ask for; if those helpers change, this step is skipped and says why."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import parse_qs, urlsplit

    from blindspot.sources import pcdata

    try:
        from blindspot.api.routers import imagery, scars

        chip_bbox, render_url = scars._chip_bbox, scars.render_url
    except Exception as exc:  # the scars router is another module's; never fail because of it
        return {"skipped": f"scars router helpers unavailable: {type(exc).__name__}: {exc}"}
    with session_scope() as session:
        if not _exists(session, "burn_cluster"):
            return {"skipped": "burn_cluster does not exist yet"}
        rows = session.execute(text("""
            SELECT ST_AsGeoJSON(geom)::json, ST_Y(centroid), scene_pre, scene_post, collection
            FROM burn_cluster WHERE unseen IS TRUE ORDER BY area_ha DESC LIMIT :n
        """), {"n": limit}).all()
    requests: dict[str, pcdata.RenderRequest] = {}
    try:
        for geom, lat, pre, post, collection in rows:
            bbox = chip_bbox(geom, lat)
            for item in (pre, post):
                for kind in pcdata.KINDS:
                    q = parse_qs(urlsplit(render_url(collection, item, bbox, kind)).query)
                    req = pcdata.RenderRequest(
                        q["collection"][0], q["item"][0], q["kind"][0], int(q["w"][0]), int(q["h"][0]),
                        pcdata.default_format(kind), bounds=imagery._round_bbox(map(float, q["bbox"][0].split(","))))
                    requests[req.cache_key()] = req
    except Exception as exc:
        return {"skipped": f"could not build chip requests: {type(exc).__name__}: {exc}"}

    def one(req: pcdata.RenderRequest) -> str:
        try:
            return "cached" if pcdata.render(req, min_valid_fraction=imagery.CROP_MIN_VALID).cached else "rendered"
        except Exception as exc:  # recorded in source_fetch already; one bad chip must not stop the rest
            log.warning("chip %s %s failed: %s", req.item, req.kind, exc)
            return "failed"

    # Chips already in rendered_asset (refresh_featured stores them) are ready for every instance;
    # rendering them here would only read them back from the database on a runner with an empty disk.
    stored = pcdata.stored_keys(list(requests))
    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(one, [req for key, req in requests.items() if key not in stored]))
    counts = {k: outcomes.count(k) for k in ("cached", "rendered", "failed")}
    return {"scars": len(rows), "chips": len(requests), "stored": len(stored), **counts}


__all__ = ["backtest", "count_within", "featured", "fill_history", "fill_neighbourhood", "last_season_fires",
           "load_backtest", "load_history", "neighbourhood", "prewarm_case_chips", "rain_forecast", "risk",
           "store_scores", "verdict"]
