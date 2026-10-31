"""Dispatch data-jobs.yml at each of its own cron times, for scheduler.yml.

GitHub's scheduler has been unreliable in 2026 (scheduled runs hours late or never created, while
workflow_dispatch works), so this loop keeps the timetable. It reads the cron strings from
.github/workflows/data-jobs.yml on the default branch (again every hour, so a schedule change applies
without a restart) and, at each minute one matches, dispatches data-jobs.yml with `slot` set to that cron
string; data-jobs.yml then picks the jobs exactly as for a scheduled run.

Usage: python3 timetable.py --minutes 270   (runs that long, then until the next :20, a quiet minute)
Needs GH_TOKEN (actions: write, contents: read) and GITHUB_REPOSITORY; uses the preinstalled gh CLI.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import re
import subprocess
import time

WORKFLOW = "data-jobs.yml"
CRON_LINE = re.compile(r'^\s*-\s*cron:\s*"([^"]+)"', re.M)
RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))  # minute hour day-of-month month day-of-week


def field_values(spec: str, lo: int, hi: int, dow: bool = False) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        base, _, step = part.partition("/")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-")
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step else start
        values.update(range(start, end + 1, int(step) if step else 1))
    if dow and 7 in values:  # cron accepts 7 for Sunday
        values.add(0)
    return values


def cron_matches(expr: str, t: dt.datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        return False
    sets = [field_values(f, lo, hi, dow=(i == 4)) for i, (f, (lo, hi)) in enumerate(zip(fields, RANGES))]
    minute_hour_month = t.minute in sets[0] and t.hour in sets[1] and t.month in sets[3]
    dom_ok, dow_ok = t.day in sets[2], (t.isoweekday() % 7) in sets[4]
    if fields[2] != "*" and fields[4] != "*":  # cron's rule: either day field may match
        return minute_hour_month and (dom_ok or dow_ok)
    return minute_hour_month and dom_ok and dow_ok


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def load_crons(repo: str, ref: str) -> list[str]:
    proc = gh("api", f"repos/{repo}/contents/.github/workflows/{WORKFLOW}?ref={ref}", "--jq", ".content")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300])
    text = base64.b64decode(proc.stdout).decode("utf-8")
    return CRON_LINE.findall(text)


def dispatch(repo: str, ref: str, cron: str) -> bool:
    for attempt in range(3):
        proc = gh("workflow", "run", WORKFLOW, "--repo", repo, "--ref", ref, "-f", f"slot={cron}")
        if proc.returncode == 0:
            return True
        print(f"  dispatch failed (attempt {attempt + 1}): {proc.stderr.strip()[:200]}", flush=True)
        time.sleep(10 * (attempt + 1))
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=270)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    ref = os.environ.get("GITHUB_REF_NAME") or "main"

    start = dt.datetime.now(dt.timezone.utc)
    end = start + dt.timedelta(minutes=args.minutes)
    end = end.replace(second=0, microsecond=0)
    while end.minute != 20:  # hand over at :20, ten minutes clear of any slot
        end += dt.timedelta(minutes=1)
    print(f"timetable from {start:%H:%M} to {end:%H:%M} UTC", flush=True)

    crons = load_crons(repo, ref)
    print("slots:", crons, flush=True)
    last_minute = None
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        time.sleep(60 - now.second - now.microsecond / 1e6 + 1)  # one second into the next minute
        minute = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        if minute == last_minute:
            continue
        last_minute = minute
        if minute >= end:
            break
        if minute.minute == 0:
            try:
                crons = load_crons(repo, ref)
            except RuntimeError as exc:  # keep the last good timetable
                print(f"could not reload the timetable: {exc}", flush=True)
        for cron in crons:
            if cron_matches(cron, minute):
                ok = dispatch(repo, ref, cron)
                print(f"{minute:%Y-%m-%d %H:%M} UTC  {cron!r}: {'dispatched' if ok else 'FAILED'}", flush=True)
    print("handing over", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
