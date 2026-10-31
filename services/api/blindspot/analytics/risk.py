"""Burn risk: which harvested fields, with their straw still lying there, are likely to burn next.

A transparent score, on purpose. The research found that a naive machine-learned burn model did worse
than single features (AUC 0.59, docs/research/sentinel2-burnscars.md section 3.6), and the build
plan says to ship a transparent score with a live hit-rate backtest and to call it "AI" only if a
model beats AUC 0.72 (SYNTHESIS section 6, P1-10). So every field gets four plain factors, each
between 0 and 1, and the score is a weighted mean of three of them, multiplied by the fourth:

    risk = 100 x dryness x (0.4 timing + 0.4 neighbourhood + 0.2 history)

- timing: how far into the burning window the field is. Farmers clear paddy straw between harvest
  and wheat sowing; that gap is quoted as 10-15 days (Arbain & Imasu 2025, Sensors 25:5588) and
  20-30 days (Govt. of India CRM guidelines 2018) (docs/research/domain-science-and-prior-art.md
  section 1). So a field harvested up to 10 days ago is fully inside its window (1.0); the window
  closes linearly between 10 and 30 days (0.0 after 30 days: the field is usually sown by then).
  No study measures the harvest-to-burn lag per field (same doc), which is why this is a window and
  not a fitted curve.
- neighbourhood: burning around the field in the last 7 days, within 5 km. Fires cluster in space and
  time (shared harvest dates, machines, enforcement), so a field whose neighbours are burning is more
  likely to burn. Two independent views of that burning, combined as "either one" (noisy-OR,
  1 - (1 - a)(1 - b)): (a) FIRMS detections (nominal or high confidence) plus Sentinel-2 burn scars
  no FIRMS detection explains (candidate unseen burns, so one fire is not counted twice), and
  (b) the hectares of Sentinel-2 burn scars (all of them). risk-v1 used (a) only; its backtest on
  this season showed FIRMS counts within 5 km rank fields no better than a coin flip (AUC ~0.5),
  while burnt hectares nearby did (AUC 0.65-0.9 on most pass pairs): a 375 m thermal snapshot at
  13:30 misses most small afternoon burns that the 10 m optical scar records. v2 adds (b); no weight
  was fitted, and the backtest still reports each factor alone as a baseline.
- history: FIRMS vegetation-fire detections within 5 km last season (15 Sep - 30 Nov of the previous
  year, the official monitoring window). Burning is a habit of places as much as of years.
- dryness: rain forecast for the next 72 h. Wet straw does not burn, whatever else is true, so this
  one multiplies the rest: exp(-rain / 10 mm). 10 mm is the WMO/ETCCDI "heavy precipitation day"
  threshold (R10mm), where the factor falls to 1/e (0.37); 1 mm (the "wet day" threshold) leaves 0.9.

Counts become 0-1 with 1 - exp(-n / typical), where `typical` is today's median among fields that
have any: a field with a typical busy neighbourhood scores 0.63, a field with none scores 0. The
weights (0.4 / 0.4 / 0.2) are a stated prior, not fitted; the backtest below reports how well the
ranking works on this season's real burns, and against which baselines.

Everything here is pure (numbers in, numbers out), so it can be tested and explained on its own.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

import orjson

MODEL_VERSION = "risk-v2"
# Where the `risk` job leaves its backtest report, under settings.cache_dir (GET /api/risk reads it).
BACKTEST_FILE = ("risk", "backtest.json")

# Harvest-to-sowing window, days (docs/research/domain-science-and-prior-art.md section 1).
WINDOW_FULL_DAYS = 10.0  # all sources agree the field is still before sowing
WINDOW_CLOSED_DAYS = 30.0  # the longest quoted gap; after this the field is usually sown
NEIGHBOUR_RADIUS_KM = 5.0
NEIGHBOUR_DAYS = 7
HEAVY_RAIN_MM = 10.0  # ETCCDI R10mm heavy precipitation day
WET_DAY_MM = 1.0  # ETCCDI R1mm wet day
# NDVI below this means bare soil or residue, not a green crop: the sentinel module's harvest rule
# (NDVI falls by > 0.25 to below 0.35; sentinel2-burnscars.md section 4 step 4).
HARVESTED_NDVI_MAX = 0.35


@dataclass(frozen=True)
class Weights:
    timing: float = 0.4
    neighbourhood: float = 0.4
    history: float = 0.2


PRIOR = Weights()


@dataclass
class FieldFeatures:
    """What we know about one harvested field at the moment of scoring."""

    id: int
    days_since_harvest: float | None  # midpoint of the harvest interval to the scoring time
    fires_7d: int  # FIRMS detections within 5 km in the last 7 days (nominal/high confidence)
    unseen_burns_7d: int  # Sentinel-2 burn scars within 5 km in the last 7 days with no FIRMS match
    burned_ha_7d: float = 0.0  # all Sentinel-2 burn scars within 5 km in the last 7 days, hectares
    rain_next_72h_mm: float | None = None
    rain_past_24h_mm: float | None = None
    fires_last_season: int | None = None  # None when last season's data could not be fetched
    harvest_days_range: tuple[float, float] | None = None  # (fewest, most) days since harvest


@dataclass
class Scored:
    id: int
    score: float  # 0-100
    factors: dict[str, Any] = field(default_factory=dict)


# --- the factors ------------------------------------------------------------------------------------


def days_since_harvest(as_of: datetime, after: datetime, before: datetime) -> tuple[float, float, float]:
    """(midpoint, fewest, most) days between the harvest and `as_of`.

    The harvest happened somewhere between the last pass that saw a standing crop (`after`) and the
    first pass that saw it harvested (`before`); we only know that interval, so we use its middle
    and keep both ends for the explanation."""
    most = (as_of - after).total_seconds() / 86400
    fewest = (as_of - before).total_seconds() / 86400
    return max(0.0, (most + fewest) / 2), max(0.0, fewest), max(0.0, most)


def timing_factor(days: float | None) -> float | None:
    """1.0 inside the first 10 days after harvest, falling linearly to 0.0 at 30 days."""
    if days is None:
        return None
    if days <= WINDOW_FULL_DAYS:
        return 1.0
    if days >= WINDOW_CLOSED_DAYS:
        return 0.0
    return 1.0 - (days - WINDOW_FULL_DAYS) / (WINDOW_CLOSED_DAYS - WINDOW_FULL_DAYS)


def dryness_factor(rain_mm: float | None) -> float | None:
    """exp(-rain / 10 mm): 1.0 when dry, 0.9 after 1 mm, 0.37 after 10 mm, 0.14 after 20 mm."""
    if rain_mm is None:
        return None
    return math.exp(-max(0.0, rain_mm) / HEAVY_RAIN_MM)


def saturating(n: float | None, typical: float) -> float | None:
    """A count as 0-1: 1 - exp(-n / typical). Zero stays zero; a typical count gives 0.63."""
    if n is None:
        return None
    return 1.0 - math.exp(-max(0.0, n) / max(typical, 1e-9))


def neighbourhood_factor(events: float | None, burned_ha: float | None) -> float | None:
    """Noisy-OR of the two views of nearby burning: high if either one is high, never above 1."""
    parts = [v for v in (events, burned_ha) if v is not None]
    if not parts:
        return None
    miss = 1.0
    for v in parts:
        miss *= 1.0 - v
    return 1.0 - miss


def typical(values: Sequence[float | None]) -> float:
    """Median of the non-zero values (at least 1): the scale that makes a count 'typical'."""
    positive = [v for v in values if v is not None and v > 0]
    return max(1.0, float(median(positive))) if positive else 1.0


def combine(timing: float | None, neighbourhood: float | None, history: float | None,
            dryness: float | None, weights: Weights = PRIOR) -> tuple[float, dict[str, float]]:
    """The score (0-100) and each factor's contribution in points.

    A factor we could not compute is left out and the remaining weights are rescaled, so a missing
    source lowers nothing by itself; the factors JSON says which one was missing. Unknown dryness
    counts as dry (1.0), and says so."""
    parts = {"timing": (timing, weights.timing), "neighbourhood": (neighbourhood, weights.neighbourhood),
             "history": (history, weights.history)}
    used = {k: (v, w) for k, (v, w) in parts.items() if v is not None and w > 0}
    total_w = sum(w for _, w in used.values())
    if total_w == 0:
        return 0.0, {}
    dry = 1.0 if dryness is None else dryness
    points = {k: round(100 * dry * v * w / total_w, 2) for k, (v, w) in used.items()}
    score = round(sum(100 * dry * v * w / total_w for v, w in used.values()), 2)
    return score, points


def score_fields(fields: Sequence[FieldFeatures], as_of: datetime, weights: Weights = PRIOR) -> list[Scored]:
    """Score every field, with an explanation that a person can read (risk_factors)."""
    neigh_typ = typical([f.fires_7d + f.unseen_burns_7d for f in fields])
    ha_typ = typical([f.burned_ha_7d for f in fields])
    hist_typ = typical([f.fires_last_season for f in fields])
    out = []
    for f in fields:
        timing = timing_factor(f.days_since_harvest)
        events = f.fires_7d + f.unseen_burns_7d
        neigh = neighbourhood_factor(saturating(events, neigh_typ), saturating(f.burned_ha_7d, ha_typ))
        hist = saturating(f.fires_last_season, hist_typ)
        dry = dryness_factor(f.rain_next_72h_mm)
        score, points = combine(timing, neigh, hist, dry, weights)
        out.append(Scored(f.id, score, {
            "model": MODEL_VERSION,
            "as_of": as_of,
            "score": score,
            "points": points,
            "weights": asdict(weights),
            "timing": {
                "value": _r(timing), "days_since_harvest": _r(f.days_since_harvest, 1),
                "days_range": None if f.harvest_days_range is None else [_r(d, 1) for d in f.harvest_days_range],
                "window_days": [WINDOW_FULL_DAYS, WINDOW_CLOSED_DAYS],
            },
            "neighbourhood": {
                "value": _r(neigh), "fires_7d": f.fires_7d, "candidate_unseen_burns_7d": f.unseen_burns_7d,
                "burned_ha_7d": _r(f.burned_ha_7d, 2), "radius_km": NEIGHBOUR_RADIUS_KM,
                "days": NEIGHBOUR_DAYS, "typical_events": _r(neigh_typ, 1),
                "typical_burned_ha": _r(ha_typ, 1), "combined": "noisy-or of events and burnt hectares",
            },
            "history": {
                "value": _r(hist), "fires_last_season": f.fires_last_season,
                "typical_fires": _r(hist_typ, 1), "radius_km": NEIGHBOUR_RADIUS_KM,
                "missing": f.fires_last_season is None,
            },
            "dryness": {
                "value": _r(dry), "rain_next_72h_mm": _r(f.rain_next_72h_mm, 1),
                "rain_past_24h_mm": _r(f.rain_past_24h_mm, 1), "missing": dry is None,
            },
        }))
    return out


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


# --- storage: what one run shares is stored once, each field keeps only its own numbers -------------
#
# The factors above repeat the same run-level values on every field (model, weights, the season,
# today's typical counts, the upstream fetch ids...): about 1.3 kB per field, 220 MB for 172k fields.
# So the `risk` job stores those once per run (table risk_run, column `factors`) and each field's
# harvest_cluster.risk_factors holds {"run": <risk_run.id>, "v": "[the numbers below, in order]"}.
# `v` is the JSON array as text in a jsonb string: jsonb keeps a string at a byte per character
# (~70 bytes here) but spends ~12 bytes on every number of an array (~200 bytes); on 172k fields that
# is 19 MB. expand_factors() puts the two parts back together into exactly the dict score_fields()
# made. Timing is not stored at all: it follows from the field's harvest dates and the run's as_of.

ROW_VALUES = (
    "score",
    "points.timing", "points.neighbourhood", "points.history",  # null: that factor was left out
    "neighbourhood.value", "neighbourhood.fires_7d", "neighbourhood.candidate_unseen_burns_7d",
    "neighbourhood.burned_ha_7d",
    "history.value", "history.fires_last_season",
    "dryness.value", "dryness.rain_next_72h_mm", "dryness.rain_past_24h_mm",
)
POINT_KEYS = ("timing", "neighbourhood", "history")
# Derived on read (from ROW_VALUES, the harvest dates and the run): left out of both stored parts.
DERIVED = ("timing.value", "timing.days_since_harvest", "timing.days_range", "history.missing", "dryness.missing")


def is_compact(factors: Any) -> bool:
    """True for the stored {"run", "v"} form, False for a full factors dict (rows scored before
    risk_run existed, which stay readable until the job rewrites them)."""
    return isinstance(factors, dict) and "v" in factors and "run" in factors


def _get(d: dict[str, Any], path: str) -> Any:
    head, _, tail = path.partition(".")
    return d.get(head) if not tail else (d.get(head) or {}).get(tail)


def run_constants(factors: dict[str, Any]) -> dict[str, Any]:
    """The part of one field's factors that every field of the same run shares (stored once)."""
    per_row = {*ROW_VALUES, *DERIVED}
    out: dict[str, Any] = {}
    for key, value in factors.items():
        if key in ("score", "points"):
            continue
        if isinstance(value, dict) and key in ("timing", "neighbourhood", "history", "dryness"):
            out[key] = {k: v for k, v in value.items() if f"{key}.{k}" not in per_row}
        else:
            out[key] = value
    return out


def row_values(factors: dict[str, Any]) -> list[Any]:
    """The field's own numbers, in ROW_VALUES order."""
    return [_get(factors, path) for path in ROW_VALUES]


def pack_values(values: Sequence[Any]) -> str:
    """The numbers as JSON text, for the `v` string (shortest exact form of every float)."""
    return orjson.dumps(list(values)).decode()


def expand_factors(stored: Any, run: dict[str, Any] | None, harvested_after: datetime | None,
                   harvested_before: datetime | None) -> dict[str, Any] | None:
    """The full factors dict of one field, whichever way it was stored.

    `run` is the risk_run's `factors` (run_constants() of the newest run). After every run, each
    field's stored numbers are the ones that run computed (the job rewrites only the rows whose
    numbers changed; the others already held them), so the newest run's shared values belong with
    every compact row; `run` inside the row only says which run last changed it. Keys come out in
    the order Postgres keeps jsonb keys, so a re-hydrated row serialises exactly like a full one.
    Returns a full (legacy) dict unchanged, and None when there is nothing to expand."""
    if not stored:
        return None
    if not is_compact(stored):
        return stored
    if not run:
        return None
    out: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in run.items()}
    packed = stored["v"]
    values = dict(zip(ROW_VALUES, orjson.loads(packed) if isinstance(packed, str) else packed, strict=True))
    out["score"] = values["score"]
    out["points"] = {k: values[f"points.{k}"] for k in POINT_KEYS if values[f"points.{k}"] is not None}
    for path in ROW_VALUES[4:]:
        head, _, tail = path.partition(".")
        out.setdefault(head, {})[tail] = values[path]
    out["history"]["missing"] = values["history.fires_last_season"] is None
    out["dryness"]["missing"] = values["dryness.value"] is None
    days = rng = None
    if harvested_after and harvested_before:
        as_of = datetime.fromisoformat(str(run["as_of"]))
        days, fewest, most = days_since_harvest(as_of, harvested_after, harvested_before)
        rng = [_r(fewest, 1), _r(most, 1)]
    timing = out.setdefault("timing", {})
    timing.update(value=_r(timing_factor(days)), days_since_harvest=_r(days, 1), days_range=rng)
    return _jsonb_order(out)


def _jsonb_order(value: Any) -> Any:
    """Dict keys sorted as jsonb stores them: shorter keys first, then byte order."""
    if isinstance(value, dict):
        return {k: _jsonb_order(value[k]) for k in sorted(value, key=lambda k: (len(k.encode()), k.encode()))}
    if isinstance(value, list):
        return [_jsonb_order(v) for v in value]
    return value


# --- backtest metrics ---------------------------------------------------------------------------------


def precision_at_k(scores: Sequence[float], labels: Sequence[int], k: int) -> float | None:
    """Of the k highest-scored fields, the share that burned.

    Fields tied with the k-th score are taken at random, so we use the expected value: everything
    strictly above the cut, plus the burnt share of the tied group for the remaining places.
    Breaking ties by list order would leak the labels whenever burnt fields are listed first (and
    integer-count baselines tie a lot), making a coin flip look like a 90% hit rate."""
    if k <= 0 or len(scores) < k:
        return None
    cut = sorted(scores, reverse=True)[k - 1]
    above = [y for s, y in zip(scores, labels, strict=True) if s > cut]
    tied = [y for s, y in zip(scores, labels, strict=True) if s == cut]
    take = k - len(above)
    return (sum(above) + take * sum(tied) / len(tied)) / k


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """Probability that a random burnt field outranks a random unburnt one (ties count half):
    0.5 is a coin flip, 1.0 is perfect. Mann-Whitney U over average ranks."""
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for m in range(i, j + 1):
            ranks[order[m]] = avg
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels, strict=True) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion (better than +-1.96 sd for small n)."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
