"""Meteorology tables: met_run (one row per model fetch) and met_grid (one row per hour per level).

Storage choice (architecture.md section 4 left it to this module; documented here and in the
module report):
- One `met_run` row per model fetch holds the grid definition, the time span, the upstream model
  runs it came from (Open-Meteo meta.json) and the fetch it was parsed from.
- One `met_grid` row per (run, level, hour) holds that level's whole grid as flat `real[]` arrays,
  row-major from the south-west corner (index = j * nx + i, rows south to north). That keeps every
  read the API needs to a single indexed row: the wind field at one hour is one row; the value at
  a city is one row plus a bilinear interpolation; the smoke job reads the ~290 'bl' rows of a run
  in well under a second.
- Levels: '10m', '950hPa', '925hPa', '850hPa' as in the contract, plus 'bl', the boundary-layer
  mean wind the smoke particles ride on (precomputed by the met job, see
  analytics/trajectory.bl_mean_wind). `blh` (boundary-layer height, m) and `precip` (mm in the
  preceding hour) are on the 10m row only; `t` is the air temperature at the level in deg C
  (2 m on the 10m row, 925 hPa on the 925hPa row when the model run includes it).
- Rows of a run are written in one transaction, so readers see a run whole or not at all. The
  job keeps the latest few runs per model; deleting a run cascades to its grid rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL
from sqlalchemy.orm import Mapped, Session, mapped_column

from blindspot.db import Base

PRIMARY_MODEL = "best_match"  # what single numbers (wind now, BLH now) are read from
LEVELS_STORED = ("10m", "950hPa", "925hPa", "850hPa", "bl")


class MetRun(Base):
    """One fetch of one model over the whole grid (all levels, all hours)."""

    __tablename__ = "met_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model: Mapped[str] = mapped_column(String(32))  # Open-Meteo model: best_match / gfs_seamless
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when we fetched it
    t_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # first valid hour
    t_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # last valid hour
    n_times: Mapped[int]
    step_s: Mapped[int]
    lon0: Mapped[float] = mapped_column(Double)
    lat0: Mapped[float] = mapped_column(Double)
    dlon: Mapped[float] = mapped_column(Double)
    dlat: Mapped[float] = mapped_column(Double)
    nx: Mapped[int]
    ny: Mapped[int]
    levels: Mapped[list[str]] = mapped_column(ARRAY(String(8)))
    variables: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    elevation: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))  # DEM height of each node
    # Upstream model runs behind this fetch, from Open-Meteo meta.json:
    # {"ecmwf_ifs": {"init": "...Z", "available": "...Z"}, "dwd_icon": {...}}
    model_runs: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))  # latency, NaNs, cell offsets
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_met_run_model_run_at", "model", "run_at"),)

    @property
    def grid(self) -> dict[str, float | int]:
        return {
            "lon0": self.lon0, "lat0": self.lat0, "dlon": self.dlon, "dlat": self.dlat,
            "nx": self.nx, "ny": self.ny,
        }


class MetGrid(Base):
    """One level of one run at one hour, as flat row-major arrays (see the module docstring)."""

    __tablename__ = "met_grid"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("met_run.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(32))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8))  # 10m / 950hPa / 925hPa / 850hPa / bl
    lon0: Mapped[float] = mapped_column(Double)
    lat0: Mapped[float] = mapped_column(Double)
    dlon: Mapped[float] = mapped_column(Double)
    dlat: Mapped[float] = mapped_column(Double)
    nx: Mapped[int]
    ny: Mapped[int]
    u: Mapped[list[float]] = mapped_column(ARRAY(REAL))  # m/s, towards the east
    v: Mapped[list[float]] = mapped_column(ARRAY(REAL))  # m/s, towards the north
    blh: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))  # m, 10m row only
    t: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))  # deg C at this level
    precip: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))  # mm, preceding hour; 10m row
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("run_id", "level", "valid_at", name="uq_met_grid_run_level_valid"),
        Index("ix_met_grid_model_level_valid", "model", "level", "valid_at"),
    )


# --- reading ---------------------------------------------------------------------------------------


def latest_runs(session: Session, models: Iterable[str] | None = None) -> dict[str, MetRun]:
    """The newest stored run of each model ({} before the met job has run)."""
    where = "WHERE model = ANY(:models)" if models is not None else ""
    ids = session.execute(
        text(
            f"SELECT DISTINCT ON (model) id FROM met_run {where}"
            " ORDER BY model, run_at DESC, id DESC"
        ),
        {"models": list(models)} if models is not None else {},
    ).scalars().all()
    if not ids:
        return {}
    return {run.model: run for run in session.scalars(select(MetRun).where(MetRun.id.in_(ids)))}


def load_level(
    session: Session,
    run: MetRun,
    level: str,
    columns: Sequence[str] = ("u", "v"),
    t_from: datetime | None = None,
    t_to: datetime | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """All hours of one level of a run as (unix-second times, {column: (T, ny, nx) float32}).

    A column that is NULL on some rows (say `t` on a model run without it) comes back as NaN.
    """
    allowed = {"u", "v", "blh", "t", "precip"}
    if not set(columns) <= allowed:
        raise ValueError(f"columns must be among {sorted(allowed)}")
    where = ["run_id = :run_id", "level = :level"]
    params: dict[str, Any] = {"run_id": run.id, "level": level}
    if t_from is not None:
        where.append("valid_at >= :t_from")
        params["t_from"] = t_from
    if t_to is not None:
        where.append("valid_at <= :t_to")
        params["t_to"] = t_to
    sql = text(
        f"SELECT extract(epoch FROM valid_at)::bigint AS t, {', '.join(columns)} FROM met_grid"
        f" WHERE {' AND '.join(where)} ORDER BY valid_at"
    )
    rows = session.execute(sql, params).all()
    times = np.array([r[0] for r in rows], dtype=np.int64)
    shape = (len(rows), run.ny, run.nx)
    out: dict[str, np.ndarray] = {}
    for k, name in enumerate(columns, start=1):
        values = [r[k] if r[k] is not None else [np.nan] * (run.nx * run.ny) for r in rows]
        out[name] = np.array(values, dtype=np.float32).reshape(shape) if rows else np.empty(shape, np.float32)
    return times, out


def row_at(session: Session, run: MetRun, level: str, valid_at: datetime) -> MetGrid | None:
    """The row of `level` whose valid hour is closest to `valid_at` (None when the run is empty)."""
    query = (
        select(MetGrid)
        .where(MetGrid.run_id == run.id, MetGrid.level == level)
        .order_by(func.abs(func.extract("epoch", MetGrid.valid_at - valid_at)))
        .limit(1)
    )
    return session.scalars(query).first()
