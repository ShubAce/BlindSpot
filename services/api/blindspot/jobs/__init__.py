"""Jobs: the code that calls upstream APIs on a schedule and writes PostGIS.

A job is a plain function in blindspot/jobs/<module>.py:

    from blindspot.jobs import job

    @job("firms_nrt", every_minutes=30)
    def firms_nrt() -> dict:
        '''Keyless FIRMS 24 h CSVs -> fire_detection.'''
        ...                                   # fetch with blindspot.http.fetch, upsert rows
        return {"rows": n}                    # stored in job_run.result

    @job("smoke", after="met")                # runs each time `met` succeeds
    @job("bulletin", cron="30 7 * * *")       # crontab fields, in Asia/Kolkata time

Jobs must be idempotent and may raise: run_job() catches, logs and records every run in job_run
and never lets an exception reach the scheduler. Run one with
`uv run python -m blindspot.jobs run firms_nrt`; the API runs them all when ENABLE_SCHEDULER=1.
Importing this package imports every module in blindspot/jobs/ (see the end of this file), so
JOBS is complete for whoever imports it: the CLI, the scheduler, a test that calls run_job().
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select, text

from blindspot import import_submodules
from blindspot.config import get_settings
from blindspot.db import engine, session_scope
from blindspot.geo import IST
from blindspot.models.core import JobRun
from blindspot.provenance import utcnow

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

JITTER_S = 30  # spread runs a little so jobs do not all hit upstream APIs in the same second
_LOCK_NAMESPACE = 0x4A4F4253  # advisory-lock namespace for jobs (any constant works)


@dataclass(frozen=True)
class Job:
    name: str
    func: Callable[[], Any]
    every_minutes: float | None = None
    cron: str | None = None  # "minute hour day month day_of_week", Asia/Kolkata time
    after: tuple[str, ...] = ()  # run whenever one of these jobs succeeds
    doc: str = ""

    @property
    def schedule(self) -> str:
        parts = []
        if self.every_minutes:
            parts.append(f"every {self.every_minutes:g} min")
        if self.cron:
            parts.append(f"cron '{self.cron}' IST")
        if self.after:
            parts.append("after " + ", ".join(self.after))
        return "; ".join(parts) or "manual"


@dataclass
class JobResult:
    name: str
    ok: bool
    started_at: datetime
    duration_ms: int
    trigger: str  # schedule / chain / cli / manual
    result: Any = None  # what the job returned
    error: str | None = None
    skipped: str | None = None  # why the job did not run at all
    chained: list[JobResult] = field(default_factory=list)  # dependents run after this one


JOBS: dict[str, Job] = {}
IMPORT_ERRORS: dict[str, str] = {}  # job modules that failed to import: {module: error}
_local_locks: dict[str, threading.Lock] = {}
_local_locks_guard = threading.Lock()


def job(
    name: str,
    every_minutes: float | None = None,
    cron: str | None = None,
    after: str | Sequence[str] | None = None,
) -> Callable[[F], F]:
    """Register the decorated function as job `name`. With neither a schedule nor `after` it only
    runs when asked (CLI run / run-all)."""
    if every_minutes is not None and cron is not None:
        raise ValueError(f"job {name!r}: give every_minutes or cron, not both")
    if every_minutes is not None and every_minutes <= 0:
        raise ValueError(f"job {name!r}: every_minutes must be positive")
    if cron is not None:
        CronTrigger.from_crontab(cron, timezone=IST)  # a bad expression fails at import time
    upstream = (after,) if isinstance(after, str) else tuple(after or ())

    def register(func: F) -> F:
        existing = JOBS.get(name)
        if existing is not None and existing.func is not func:
            raise ValueError(f"job name {name!r} is already used in {existing.func.__module__}")
        doc = (func.__doc__ or "").strip().splitlines()
        JOBS[name] = Job(name, func, every_minutes, cron, upstream, doc[0] if doc else "")
        return func

    return register


def load_jobs() -> dict[str, Job]:
    """Import every module in blindspot/jobs/ so that its @job functions register. Idempotent.

    It runs when this package is imported; call it again only to pick up files added since."""
    _, failed = import_submodules(__name__, __path__)
    IMPORT_ERRORS.clear()
    IMPORT_ERRORS.update(failed)
    return JOBS


def dependents_of(name: str) -> list[Job]:
    return [spec for spec in JOBS.values() if name in spec.after]


def run_all_order() -> list[Job]:
    """The order `run-all` uses: manual jobs (one-off loads such as boundaries), then interval
    jobs, then cron jobs (the daily bulletin last). `after` jobs run through chaining."""
    manual = [j for j in JOBS.values() if not (j.every_minutes or j.cron or j.after)]
    interval = [j for j in JOBS.values() if j.every_minutes]
    daily = [j for j in JOBS.values() if j.cron]
    return manual + interval + daily


# --- running -------------------------------------------------------------------------------------


def run_job(name: str, *, trigger: str = "manual", chain: bool = True) -> JobResult:
    """Run job `name` now and return what happened. Never raises for a failing job.

    The run is timed, logged and stored in job_run. When it succeeds and `chain` is true, the jobs
    declared `after=name` run next (recursively). A job already running, in this process or in
    another one sharing the database, is skipped rather than run twice at once.
    Raises KeyError only for an unknown job name.
    """
    return _run(name, trigger, chain, frozenset())


def _run(name: str, trigger: str, chain: bool, upstream_names: frozenset[str]) -> JobResult:
    spec = JOBS[name]
    started_at = utcnow()
    lock = _local_lock(name)
    if not lock.acquire(blocking=False):
        log.warning("job %s is already running in this process; skipped", name)
        return JobResult(name, False, started_at, 0, trigger, skipped="already running")
    try:
        with _cross_process_lock(name) as acquired:
            if not acquired:
                log.warning("job %s is already running in another process; skipped", name)
                return JobResult(name, False, started_at, 0, trigger, skipped="running elsewhere")
            result = _execute(spec, trigger, started_at)
    finally:
        lock.release()
    _record(result)
    if result.ok and chain:
        seen = upstream_names | {name}
        for dependent in dependents_of(name):
            if dependent.name not in seen:  # a cycle in `after` must not loop forever
                result.chained.append(_run(dependent.name, "chain", True, seen))
    return result


def _execute(spec: Job, trigger: str, started_at: datetime) -> JobResult:
    log.info("job %s started (%s)", spec.name, trigger)
    t0 = time.perf_counter()
    try:
        value = spec.func()
    except Exception as exc:  # a failing job must never take the scheduler or the API down
        duration_ms = round((time.perf_counter() - t0) * 1000)
        error = get_settings().redact(f"{type(exc).__name__}: {exc}")[:2000]
        log.exception("job %s failed after %.1f s", spec.name, duration_ms / 1000)
        return JobResult(spec.name, False, started_at, duration_ms, trigger, error=error)
    duration_ms = round((time.perf_counter() - t0) * 1000)
    summary = f": {value!r:.200}" if value is not None else ""
    log.info("job %s ok in %.1f s%s", spec.name, duration_ms / 1000, summary)
    return JobResult(spec.name, True, started_at, duration_ms, trigger, result=value)


def _record(result: JobResult) -> None:
    try:
        with session_scope() as session:
            session.add(
                JobRun(
                    name=result.name,
                    trigger=result.trigger,
                    started_at=result.started_at,
                    finished_at=result.started_at + timedelta(milliseconds=result.duration_ms),
                    ok=result.ok,
                    duration_ms=result.duration_ms,
                    error=result.error,
                    result=result.result,
                )
            )
    except Exception:
        log.warning("could not record the run of job %s in job_run", result.name, exc_info=True)


def _local_lock(name: str) -> threading.Lock:
    with _local_locks_guard:
        return _local_locks.setdefault(name, threading.Lock())


@contextmanager
def _cross_process_lock(name: str) -> Iterator[bool]:
    """A Postgres advisory lock, so one job never runs twice at once across processes (the API's
    scheduler, a CLI run, CI). Yields False when another process holds it. Without a database the
    job runs unlocked; it will then fail and say why."""
    key = {"ns": _LOCK_NAMESPACE, "key": zlib.crc32(name.encode()) & 0x7FFFFFFF}
    try:
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    except Exception as exc:
        log.warning("job %s runs without a cross-process lock: %s", name, exc)
        yield True
        return
    acquired = False
    try:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:ns, :key)"), key).scalar())
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), key)
            conn.close()
        except Exception:  # the lock dies with the connection anyway
            log.warning("could not release the lock of job %s cleanly", name, exc_info=True)


def last_runs() -> dict[str, JobRun]:
    """The latest run of every job that has run, from job_run ({} without a database)."""
    latest = (
        select(JobRun.name, func.max(JobRun.started_at).label("started_at"))
        .group_by(JobRun.name)
        .subquery()
    )
    query = select(JobRun).join(
        latest, (JobRun.name == latest.c.name) & (JobRun.started_at == latest.c.started_at)
    )
    try:
        with session_scope() as session:
            return {row.name: row for row in session.scalars(query)}
    except Exception as exc:
        log.warning("could not read job_run: %s", exc)
        return {}


# --- scheduling ----------------------------------------------------------------------------------


def _last_successes() -> dict[str, datetime]:
    try:
        with session_scope() as session:
            rows = session.execute(
                text("SELECT name, max(finished_at) FROM job_run WHERE ok GROUP BY name")
            )
            return {name: finished_at for name, finished_at in rows}
    except Exception as exc:
        log.warning("could not read job_run (%s); interval jobs will all run soon after start", exc)
        return {}


def build_scheduler(*, load: bool = False) -> BackgroundScheduler:
    """An APScheduler BackgroundScheduler holding every scheduled job (not started yet).

    An interval job first runs when it is next due according to job_run, so a restart does not
    re-run a job that ran a minute ago; overdue jobs start a few seconds apart. Cron jobs follow
    their crontab in IST. Runs are coalesced, never overlap (max_instances=1) and get up to
    JITTER_S seconds of jitter. Jobs with only `after=` are not scheduled: they are chained.
    The jobs were registered when this package was imported; `load=True` re-scans the folder.
    """
    if load:
        load_jobs()
    scheduler = BackgroundScheduler(
        timezone=IST,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 15 * 60},
    )
    last_ok = _last_successes()
    now = utcnow()
    overdue = 0
    for spec in JOBS.values():
        if spec.every_minutes:
            interval = timedelta(minutes=spec.every_minutes)
            due = last_ok[spec.name] + interval if spec.name in last_ok else now
            if due <= now:
                overdue += 1
                due = now + timedelta(seconds=5 * overdue)
            trigger: Any = IntervalTrigger(
                minutes=spec.every_minutes, start_date=due, timezone=IST, jitter=JITTER_S
            )
        elif spec.cron:
            minute, hour, day, month, day_of_week = spec.cron.split()
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone=IST,
                jitter=JITTER_S,
            )
        else:
            continue
        scheduler.add_job(
            run_job,
            trigger,
            args=[spec.name],
            kwargs={"trigger": "schedule"},
            id=spec.name,
            name=spec.name,
            replace_existing=True,
        )
    return scheduler


# Last, so that every name above exists when the job modules do `from blindspot.jobs import job`.
load_jobs()
