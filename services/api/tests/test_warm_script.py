"""infra/aws/warm.sh against the API it keeps warm (read as text: the script runs only on the server).

warm.sh refreshes the cached answers a daily, weekly or bulletin job feeds once the job has settled.
These checks keep its constants in step with the API's: the settle time outlasts the in-process
caches, every job it waits for is a registered job, and every answer on one of those long CDN clocks
that a job feeds is one it refreshes.
"""

from __future__ import annotations

import re
from pathlib import Path

from blindspot.api import cache

WARM = Path(__file__).resolve().parents[3] / "infra" / "aws" / "warm.sh"
# The clocks whose answers live long enough (up to a day or a week) that warm.sh refreshes them after
# their job; the hourly and six-hourly ones tick within the hour.
LONG_CLOCKS = (cache.DAILY, cache.WEEKLY, cache.BULLETIN)
# Rules on those clocks whose answers no job changes, so warm.sh rightly leaves them to the clock.
NOT_FED = {
    r"^/api/agent/runs/": "a finished agent run never changes (unfinished ones are never cached)",
    r"^/api/imagery/tilejson/": "one Sentinel-2 item's TileJSON, from its STAC record",
}


def _script() -> str:
    return WARM.read_text(encoding="utf-8")


def _routes() -> dict[str, str]:
    """warm.sh's ROUTES: job name -> the regex (ERE) of the request URIs its run refreshes."""
    block = re.search(r"^declare -A ROUTES=\(\n(.*?)^\)", _script(), re.MULTILINE | re.DOTALL)
    assert block, "warm.sh has no ROUTES array"
    routes = dict(re.findall(r"^\s*\[(\w+)\]='([^']*)'\s*$", block.group(1), re.MULTILINE))
    assert routes, "warm.sh's ROUTES did not parse"
    return routes


def test_settle_time_outlasts_the_in_process_caches() -> None:
    """A job's answers are refreshed SETTLE_MIN after it ends. An answer built in process just before
    the job ended lives INPROCESS_TTL_S more, and one built from a district list kept that long (the
    brief's) up to twice that; 5 minutes more leave room for a slow build."""
    found = re.search(r"^SETTLE_MIN=(\d+)\s*$", _script(), re.MULTILINE)
    assert found, "warm.sh has no SETTLE_MIN"
    assert int(found.group(1)) * 60 >= 2 * cache.INPROCESS_TTL_S + 300


def test_routes_name_registered_jobs() -> None:
    from blindspot.jobs import IMPORT_ERRORS, JOBS

    unknown = sorted(set(_routes()) - set(JOBS))
    assert not unknown, f"warm.sh waits for jobs that do not exist: {unknown} (import errors: {IMPORT_ERRORS})"


def test_every_answer_a_job_feeds_on_a_long_clock_is_refreshed() -> None:
    """Each GET route of the API whose CDN rule is on a long clock (path parameters filled in) is
    matched by the ROUTES regex of some job, unless no job changes its answer (NOT_FED)."""
    from blindspot.api.main import app

    routes = [re.compile(regex) for regex in _routes().values()]
    long_rules = {rule.pattern.pattern for rule in cache.CDN_RULES if rule.clock in LONG_CLOCKS}
    tried: set[str] = set()
    unrefreshed: dict[str, list[str]] = {}
    for template, methods in app.openapi()["paths"].items():
        if "get" not in methods:
            continue
        path = re.sub(r"\{[^}]+\}", "1", template)
        rule = cache.cdn_rule(path)
        if rule is None or rule.pattern.pattern not in long_rules:
            continue
        tried.add(rule.pattern.pattern)
        if not any(r.search(path) for r in routes):
            unrefreshed.setdefault(rule.pattern.pattern, []).append(path)
    assert tried == long_rules, f"no API route tried these rules: {sorted(long_rules - tried)}"
    assert set(unrefreshed) == set(NOT_FED), unrefreshed
