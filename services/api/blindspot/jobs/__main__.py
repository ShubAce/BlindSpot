"""Command line for jobs. Run from services/api:

    uv run python -m blindspot.jobs list          registered jobs, their schedule and last run
    uv run python -m blindspot.jobs run <name>    one job now, then the jobs that run after it
    uv run python -m blindspot.jobs run-all       every job once: one-off loads, periodic, daily
    uv run python -m blindspot.jobs scheduler     the scheduler in the foreground (Ctrl+C stops)

Exit status: 0 when everything asked for succeeded, 1 when a job failed or was skipped,
2 when the setup is wrong (unknown job, database not reachable).
"""

from __future__ import annotations

import argparse
import sys
import time

from blindspot.config import get_settings, setup_logging
from blindspot.db import init_db
from blindspot.jobs import (  # importing the package registers every job module
    IMPORT_ERRORS,
    JOBS,
    JobResult,
    build_scheduler,
    last_runs,
    run_all_order,
    run_job,
)
from blindspot.provenance import iso_z


def _print_result(result: JobResult, indent: str = "") -> None:
    seconds = result.duration_ms / 1000
    if result.skipped:
        status = f"skipped ({result.skipped})"
    elif result.ok:
        status = f"ok in {seconds:.1f} s"
    else:
        status = f"FAILED after {seconds:.1f} s: {result.error}"
    print(f"{indent}{result.name}: {status}")
    for child in result.chained:
        _print_result(child, indent + "  then ")


def _all_ok(result: JobResult) -> bool:
    return result.ok and all(_all_ok(child) for child in result.chained)


def _names(result: JobResult) -> set[str]:
    return {result.name}.union(*(_names(child) for child in result.chained))


def _list() -> int:
    if not JOBS:
        print("No jobs registered yet: add a module with an @job function to blindspot/jobs/.")
        return 0
    last = last_runs()
    width = max(len(name) for name in JOBS)
    for spec in JOBS.values():
        run = last.get(spec.name)
        when = "never run"
        if run is not None:
            when = f"last run {'ok' if run.ok else 'FAILED'} {iso_z(run.finished_at)}"
        print(f"{spec.name:<{width}}  {spec.schedule:<28}  {when:<38}  {spec.doc}")
    return 0


def _run_all() -> int:
    order = run_all_order()
    if not order:
        print("No jobs registered yet.")
        return 0
    done: set[str] = set()
    results = []
    for spec in order:
        if spec.name in done:  # already ran, chained after another job
            continue
        result = run_job(spec.name, trigger="cli")
        done |= _names(result)
        results.append(result)
    for result in results:
        _print_result(result)
    return 0 if all(_all_ok(r) for r in results) else 1


def _serve() -> int:
    scheduler = build_scheduler()
    if not scheduler.get_jobs():
        print("No scheduled jobs registered; nothing to run.")
        return 0
    scheduler.start()
    for scheduled in scheduler.get_jobs():
        print(f"{scheduled.id}: next run {iso_z(scheduled.next_run_time)}")
    print("Scheduler running. Ctrl+C stops it.")
    try:
        while True:  # a sleep loop, because Ctrl+C cannot interrupt a bare wait() on Windows
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m blindspot.jobs", description="BlindSpot jobs")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="registered jobs, their schedule and last run")
    run_parser = commands.add_parser("run", help="run one job now, then the jobs after it")
    run_parser.add_argument("name")
    run_parser.add_argument("--no-chain", action="store_true", help="skip the dependent jobs")
    commands.add_parser("run-all", help="run every job once")
    commands.add_parser("scheduler", aliases=["serve-scheduler"], help="run the scheduler")
    args = parser.parse_args(argv)

    setup_logging()
    for module, error in IMPORT_ERRORS.items():
        print(f"warning: {module} did not import: {error}", file=sys.stderr)

    if args.command == "list":
        return _list()
    if args.command == "run" and args.name not in JOBS:
        known = ", ".join(JOBS) or "none registered"
        print(f"error: no job named {args.name!r} (known: {known})", file=sys.stderr)
        return 2
    try:
        init_db()
    except Exception as exc:
        message = get_settings().redact(str(exc).strip().splitlines()[0] if str(exc) else repr(exc))
        print(
            f"error: the database is not reachable ({message}).\n"
            "Start it with: docker compose -f infra/docker-compose.yml up -d",
            file=sys.stderr,
        )
        return 2
    if args.command == "run":
        result = run_job(args.name, trigger="cli", chain=not args.no_chain)
        _print_result(result)
        return 0 if _all_ok(result) else 1
    if args.command == "run-all":
        return _run_all()
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
