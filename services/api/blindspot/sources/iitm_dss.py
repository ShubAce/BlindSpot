"""IITM's Decision Support System (DSS) for Delhi: the share of Delhi's PM2.5 it attributes to
stubble burning, day by day, plus a 4-day source breakdown.

Why we read it: it is the official benchmark for "how much of Delhi's air is crop-fire smoke",
and by IITM's own disclaimer it is driven by VIIRS active-fire counts from the previous evening:
the same polar-orbiter view that misses the late-afternoon burns BlindSpot looks for. Comparing
our numbers against it is our validation target.

There is no API. The page (https://ews.tropmet.res.in/dss/) renders its numbers server-side as
Chart.js arrays (`labels:[...]` followed by datasets `label: '...', ... data:[...]`), so one GET
plus a regex reads them (docs/research/air-quality-ground.md §3). The page is "All rights reserved,
IITM Pune": we show the values with attribution and a link and never mirror the page.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass
from datetime import date

import orjson

from blindspot.http import FetchResult, fetch, set_fetch_records
from blindspot.provenance import SourceInfo, register_source

DSS_URL = "https://ews.tropmet.res.in/dss/"
SOURCE_KEY = "iitm_dss"
STUBBLE = "Stubble Burning"
OTHERS = "Others"

register_source(
    SourceInfo(
        SOURCE_KEY,
        "IITM Decision Support System for Delhi (stubble-burning share of PM2.5)",
        "air_attribution",
        8 * 60,  # read three times a day; stale after a day without a successful read
        DSS_URL,
    )
)

_COMMENTS = re.compile(r"<!--.*?-->", re.S)
# Either a chart's x labels, or one dataset's label and its data array (in page order).
_CHART_PARTS = re.compile(
    r"labels\s*:\s*\[([^\]]*)\]|label\s*:\s*'([^']+)'[^{}]*?data\s*:\s*\[([^\]]*)\]", re.S
)
_PARAGRAPHS = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.S | re.I)
_METHOD_WORDS = re.compile(r"VIIRS|climatolog", re.I)


@dataclass(frozen=True)
class DssData:
    daily_pct: dict[str, float]  # {"2026-10-30": 1.74913, ...}: the season-to-date series
    forecast_pct: dict[str, dict[str, float]]  # {day: {source: % of Delhi PM2.5}}, 4 days
    basis: str | None  # IITM's own sentences on how the stubble share is derived

    @property
    def run_date(self) -> date:
        """The first day of the 4-day outlook, which is the day the DSS ran."""
        return date.fromisoformat(min(self.forecast_pct))

    def sha256(self) -> str:
        """A hash of the numbers only (the raw page changes on every hit: visitor counter)."""
        raw = orjson.dumps(
            {"daily": self.daily_pct, "forecast": self.forecast_pct}, option=orjson.OPT_SORT_KEYS
        )
        return hashlib.sha256(raw).hexdigest()


def _charts(page: str) -> list[tuple[list[str], dict[str, list[float]]]]:
    """Every chart on the page as (x labels, {dataset label: values})."""
    charts: list[tuple[list[str], dict[str, list[float]]]] = []
    for match in _CHART_PARTS.finditer(_COMMENTS.sub("", page)):
        labels, name, data = match.groups()
        if labels is not None:
            charts.append((re.findall(r'"([^"]+)"', labels), {}))
        elif charts and name is not None:
            charts[-1][1][name.strip()] = [float(x) for x in data.split(",") if x.strip()]
    return charts


def _is_stubble(name: str) -> bool:
    return re.fullmatch(r"stub+le burning", name.strip(), re.I) is not None  # page has a typo too


def parse_dss(page: str) -> DssData:
    """Read the two DSS charts. Raises ValueError when the page no longer has them."""
    charts = _charts(page)
    history = next(
        (c for c in charts if len(c[1]) == 1 and any(_is_stubble(k) for k in c[1])), None
    )
    outlook = next(
        (c for c in charts if any(_is_stubble(k) for k in c[1]) and OTHERS in c[1]), None
    )
    if history is None or outlook is None:
        raise ValueError("the DSS page has no stubble-burning charts (layout changed?)")
    labels, series = history
    values = next(iter(series.values()))
    if len(labels) != len(values):
        raise ValueError("DSS history: labels and values differ in length")
    daily = {label[:10]: value for label, value in zip(labels, values, strict=True)}
    days, sources = outlook
    forecast: dict[str, dict[str, float]] = {}
    for i, day in enumerate(days):
        shares = {
            (STUBBLE if _is_stubble(k) else k): round(v[i], 4)
            for k, v in sources.items()
            if i < len(v)
        }
        forecast[day[:10]] = shares
    if not forecast or any(not shares for shares in forecast.values()):
        raise ValueError("DSS outlook chart is empty")
    return DssData(daily, forecast, _method_notes(page))


def _method_notes(page: str) -> str | None:
    """IITM's own paragraphs on how the numbers are made (the VIIRS fire-count basis, the
    climatological outlook), as plain text, so the UI and the agent can quote the source itself."""
    notes = []
    for paragraph in _PARAGRAPHS.findall(_COMMENTS.sub("", page)):
        text = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", paragraph)).split())
        if _METHOD_WORDS.search(text):
            notes.append(text.removeprefix("Disclaimer:").strip())
    return " ".join(dict.fromkeys(notes)) or None


def fetch_dss() -> tuple[DssData, FetchResult]:
    """GET the DSS page and parse it. The host has intermittent TLS stalls; fetch() retries."""
    result = fetch(SOURCE_KEY, DSS_URL, expect="text", timeout=60.0)
    data = parse_dss(result.data or "")
    set_fetch_records(result.fetch_id, len(data.daily_pct) + len(data.forecast_pct))
    return data, result
