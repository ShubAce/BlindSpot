"""Tests for the honest burn-risk scale (risk-v3) and backtest (analytics/risk.py, jobs/risk.py,
api/routers/risk.py, models/risk.py).

The field inputs are real: the factors of the ten harvest_cluster rows the risk job stored on
2026-10-30 (tests/fixtures/imagery_risk/risk_factors_rows.json). Scoring runs in temporary tables of
the *_test database; rows created for the route test sit in the Indian Ocean and are removed after.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from blindspot.analytics import risk as model
from blindspot.db import session_scope

FX = Path(__file__).parent / "fixtures" / "imagery_risk"
ROWS = json.loads((FX / "risk_factors_rows.json").read_text(encoding="utf-8"))["rows"]
AS_OF = datetime(2026, 10, 30, 23, 13, 2, 192298, tzinfo=UTC)


def _features(rows=ROWS, drop_history: bool = False, drop_rain: bool = False) -> list[model.FieldFeatures]:
    """The recorded rows' inputs as the job sees them."""
    out = []
    for i, r in enumerate(rows, start=1):  # the recording holds one id twice (two snapshots): number them
        f = r["risk_factors"]
        n, h, d = f["neighbourhood"], f["history"], f["dryness"]
        days = model.days_since_harvest(AS_OF, datetime.fromisoformat(r["harvested_after"]),
                                        datetime.fromisoformat(r["harvested_before"]))[0]
        out.append(model.FieldFeatures(
            id=i, days_since_harvest=days, fires_7d=n["fires_7d"], unseen_burns_7d=n["candidate_unseen_burns_7d"],
            burned_ha_7d=n["burned_ha_7d"], fires_last_season=None if drop_history else h["fires_last_season"],
            rain_next_72h_mm=None if drop_rain else d["rain_next_72h_mm"],
            rain_past_24h_mm=None if drop_rain else d["rain_past_24h_mm"]))
    return out


def _table(session, name: str, feats: list[model.FieldFeatures], groups: list[int] | None = None) -> None:
    from blindspot.jobs import risk as job

    session.execute(text(f"DROP TABLE IF EXISTS {name}"))
    session.execute(text(f"CREATE TEMP TABLE {name} ({job.FIELD_COLUMNS}grp int, {model.SCORE_COLUMNS}) ON COMMIT DROP"))
    session.execute(text(f"""INSERT INTO {name} (k, days, fires, unseen, ha, hist, r72, r24, grp)
                             VALUES (:k, :d, :f, :u, :ha, :h, :r72, :r24, :grp)"""),
                    [{"k": f.id, "d": f.days_since_harvest, "f": f.fires_7d, "u": f.unseen_burns_7d, "ha": f.burned_ha_7d,
                      "h": f.fires_last_season, "r72": f.rain_next_72h_mm, "r24": f.rain_past_24h_mm,
                      "grp": 0 if groups is None else groups[i]} for i, f in enumerate(feats)])


def _sql_values(session, name: str) -> dict[int, list]:
    return {int(k): orjson.loads(v) for k, v in session.execute(text(f"SELECT k, v FROM {name}")).all()}


def _close(want: list, got: list) -> bool:
    from blindspot.jobs.risk import _ROUNDING

    return all((a is None) == (b is None) and (a is None or abs(a - b) <= unit + 1e-9)
               for a, b, unit in zip(want, got, _ROUNDING, strict=True))


# --- the SQL scorer is score_fields -----------------------------------------------------------------------


@pytest.mark.parametrize("drop_history, drop_rain", [(False, False), (True, False), (False, True)])
def test_sql_scorer_matches_score_fields(drop_history, drop_rain) -> None:
    feats = _features(drop_history=drop_history, drop_rain=drop_rain)
    want = {s.id: model.row_values(s.factors) for s in model.score_fields(feats, AS_OF)}
    with session_scope() as s:
        _table(s, "t_score", feats)
        for stmt in model.score_sql("t_score"):
            s.execute(text(stmt))
        got = _sql_values(s, "t_score")
    assert set(got) == set(want)
    for k in want:
        assert _close(want[k], got[k]), (k, want[k], got[k])
    exact = sum(want[k] == got[k] for k in want)
    assert exact >= len(want) - 1  # rounding of an exact decimal half is the only allowed difference


def test_grouped_sql_scoring_uses_each_groups_typical_counts() -> None:
    """In the backtest each pass pair has its own typical counts: scoring two groups together must
    give what scoring each alone gives."""
    feats = _features()
    groups = [i % 2 for i in range(len(feats))]
    want = {}
    for g in (0, 1):
        part = [f for f, gg in zip(feats, groups, strict=True) if gg == g]
        want.update({s.id: model.row_values(s.factors) for s in model.score_fields(part, AS_OF)})
    with session_scope() as s:
        _table(s, "t_group", feats, groups)
        for stmt in model.score_sql("t_group", "grp"):
            s.execute(text(stmt))
        got = _sql_values(s, "t_group")
    assert all(_close(want[k], got[k]) for k in want)


def test_days_since_harvest_in_sql_is_the_same_double() -> None:
    from blindspot.jobs.risk import _days_sql

    with session_scope() as s:
        for r in ROWS:
            a, b = datetime.fromisoformat(r["harvested_after"]), datetime.fromisoformat(r["harvested_before"])
            got = s.execute(text(f"SELECT {_days_sql('CAST(:t AS timestamptz)', 'CAST(:a AS timestamptz)', 'CAST(:b AS timestamptz)')}"),
                            {"t": AS_OF, "a": a, "b": b}).scalar_one()
            assert got == model.days_since_harvest(AS_OF, a, b)[0]


# --- the scale ---------------------------------------------------------------------------------------------


def test_the_score_no_longer_saturates() -> None:
    """The recorded top field (risk-v2: 90.39) had 17 FIRMS fires, 92 candidate unseen burns and 379 ha
    burnt within 5 km, 50 fires last season, timing 1.0 and no rain; v2 put fields like it at 99.99-100.0.
    v3 keeps the order of each factor but no factor reaches 1, so busier fields still score higher and
    nothing reaches 100."""
    feats = _features()
    v3 = {s.id: s.score for s in model.score_fields(feats, AS_OF)}
    assert max(v3.values()) < 100
    busy = [model.FieldFeatures(id=i, days_since_harvest=5.0, fires_7d=17 * m, unseen_burns_7d=92 * m,
                                burned_ha_7d=379.4 * m, rain_next_72h_mm=0.0, fires_last_season=50 * m)
            for i, m in enumerate((1, 2, 4, 8, 16))]
    others = [model.FieldFeatures(id=10 + i, days_since_harvest=5.0, fires_7d=f, unseen_burns_7d=u, burned_ha_7d=ha,
                                  rain_next_72h_mm=0.0, fires_last_season=h)
              for i, (f, u, ha, h) in enumerate(((3, 12, 81.2, 6), (1, 30, 40.0, 3), (0, 0, 0.0, 0)))]
    scores = [s.score for s in model.score_fields(busy + others, AS_OF)][:len(busy)]
    assert scores == sorted(scores) and len(set(scores)) == len(scores) and scores[-1] < 100


def test_bands_and_bootstrap_intervals() -> None:
    assert [model.band_index(x) for x in (0.0001, 0.01, 0.0100001, 0.05, 0.3, 0.5, 0.51, 1.0)] == [0, 0, 1, 1, 4, 4, 5, 5]
    rng = random.Random(5)
    labels = [int(rng.random() < 0.3) for _ in range(300)]
    scores = [rng.random() + 0.4 * y for y in labels]
    lo, hi = model.auc_ci(scores, labels)
    assert lo < model.roc_auc(scores, labels) < hi and hi - lo < 0.2
    assert model.auc_ci(scores, labels) == (lo, hi)  # seeded: the same report twice
    pairs = [{"tile": f"T{i % 4}", "n": 100, "burnt": 20, "auc": 0.6 + 0.01 * (i % 3), "top_k": 10, "top_hits": 4,
              "bands": [[1, 1], [4, 2], [5, 1], [15, 4], [25, 5], [50, 7]]} for i in range(12)]
    tb = model.tile_bootstrap(pairs, reps=200)
    assert tb["top_10pct"]["burnt_share"] == 0.4 and tb["top_10pct"]["lift"] == 2.0
    assert tb["auc_ci95"][0] <= tb["auc_weighted_mean"] <= tb["auc_ci95"][1] and tb["tiles"] == 4
    assert [b["burnt_share"] for b in tb["bands"]] == [1.0, 0.5, 0.2, 0.267, 0.2, 0.14]


# --- the job: self-check and per-pair metrics --------------------------------------------------------------


def test_self_check_passes_on_agreement_and_refuses_a_wrong_number() -> None:
    from blindspot.jobs import risk as job

    feats = _features()
    history = job.History(job.np.empty((0, 2)), "2025-09-15/2025-11-30", [2910], True)
    rain = job.Rain({}, [5187])
    with session_scope() as s:
        _table(s, "rk", feats)
        for stmt in model.score_sql("rk"):
            s.execute(text(stmt))
        shared, checked = job._self_check(s, AS_OF, history, rain)
        assert checked == len(feats)
        assert shared["model"] == model.MODEL_VERSION and shared["fetch_ids"] == {"rain": [5187], "history": [2910]}
        assert shared["neighbourhood"]["combined"] == "mean of events and burnt hectares"
        top = s.execute(text("SELECT k FROM rk ORDER BY s DESC LIMIT 1")).scalar_one()
        s.execute(text("UPDATE rk SET v = regexp_replace(v, '^\\[[^,]+,', '[100,') WHERE k = :k"), {"k": top})
        with pytest.raises(RuntimeError, match="disagrees"):
            job._self_check(s, AS_OF, history, rain)


def test_tested_order_cuts_place_fields_like_the_route() -> None:
    """The job's cuts of the tested ordering (neighbourhood + history) put each field in the band its
    cume_dist gives, when the route places it from its stored factors with tested_band()."""
    from blindspot.jobs import risk as job

    feats = _features()
    with session_scope() as s:
        _table(s, "rk", feats)
        for stmt in model.score_sql("rk"):
            s.execute(text(stmt))
        out = job._tested_order(s)
        rows = s.execute(text(f"""SELECT k, {model.tested_score_sql('nv', 'hv')} AS x,
                                         cume_dist() OVER (ORDER BY {model.tested_score_sql('nv', 'hv')} DESC)
                                  FROM rk""")).all()
    assert out["fields"] == len(feats) and [c["band"] for c in out["cuts"]] == [b for _, b in model.BANDS[:-1]]
    assert -1 <= out["spearman_with_risk_score"] <= 1
    scored = {f.id: f for f in model.score_fields(feats, AS_OF)}
    cuts = [c["min_tested_score"] for c in out["cuts"]]
    for k, x, top in rows:
        f = scored[int(k)].factors
        mine = model.tested_score(f["neighbourhood"]["value"], f["history"]["value"])
        assert mine == x
        assert model.tested_band(mine, cuts) == model.band_index(top)


def test_pair_metrics_in_sql_match_the_python_metrics() -> None:
    from blindspot.jobs import risk as job

    rng = random.Random(11)
    t0 = datetime(2026, 10, 25, 5, 40, tzinfo=UTC)
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS bt"))
        s.execute(text(f"CREATE TEMP TABLE bt ({job.BT_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
        rows = []
        for pair in (0, 1):
            for i in range(60 + 23 * pair):
                y = int(rng.random() < 0.25)
                rows.append({"pair": pair, "y": y, "f": rng.randint(0, 3) + y, "u": rng.randint(0, 20),
                             "ha": round(rng.random() * 50 * (1 + y), 2), "h": rng.randint(0, 9), "t0": t0})
        s.execute(text("INSERT INTO bt (pair, label, fid, t0, fires, unseen, ha, hist) "
                       "VALUES (:pair, :y, 0, :t0, :f, :u, :ha, :h)"), rows)
        for stmt in model.score_sql("bt", "pair"):
            s.execute(text(stmt))
        sql = job._pair_metrics(s, history_ok=True)
        got = s.execute(text("SELECT pair, label, s, fires, ha FROM bt")).all()
    for pair in (0, 1):
        mine = [r for r in got if r[0] == pair]
        labels, scores = [r[1] for r in mine], [r[2] for r in mine]
        m = sql[pair]
        assert m["auc"] == round(model.roc_auc(scores, labels), 3)
        assert m["baselines_auc"]["fires_7d_only"] == round(model.roc_auc([r[3] for r in mine], labels), 3)
        k = max(1, round(len(mine) / 10))
        assert m["top_k"] == k and m["top_hits"] == pytest.approx(model.precision_at_k(scores, labels, k) * k, abs=1e-3)
        shares = [sum(1 for t in scores if t >= x) / len(scores) for x in scores]
        want = [[0, 0] for _ in model.BANDS]
        for share, y in zip(shares, labels, strict=True):
            want[model.band_index(share)][0] += 1
            want[model.band_index(share)][1] += y
        assert m["bands"] == want


def test_pooled_auc_by_block_matches_brute_force() -> None:
    """The pooled AUC compares burnt and unburnt fields within each pass pair only, so it is the mean
    of the pairs' AUCs weighted by burnt x unburnt; C and N split those comparisons by the blocks of
    the two fields (checked one comparison at a time here), so resampling blocks reweights them."""
    import numpy as np

    rng = random.Random(3)
    rows = []
    for pair in range(4):
        for _ in range(40 + 10 * pair):
            y = int(rng.random() < 0.2 + 0.1 * pair)
            rows.append((pair, y, rng.randrange(5), round(rng.random() * 4) + y * (pair % 2)))  # ties on purpose
    pair, label, block, score = (np.array(c) for c in zip(*rows, strict=True))
    c, n = model.concordance_by_block(pair, label, block, score, 5)
    want_c, want_n = np.zeros((5, 5)), np.zeros((5, 5))
    for p, y, a, s in rows:
        if not y:
            continue
        for q, y2, b, s2 in rows:
            if q == p and not y2:
                want_n[a, b] += 1
                want_c[a, b] += 1.0 if s > s2 else 0.5 if s == s2 else 0.0
    assert np.array_equal(n, want_n) and np.allclose(c, want_c)
    num = den = 0.0
    for p in range(4):
        m = pair == p
        pos = int(label[m].sum())
        num += model.roc_auc(list(score[m]), list(label[m])) * pos * (int(m.sum()) - pos)
        den += pos * (int(m.sum()) - pos)
    assert c.sum() / n.sum() == pytest.approx(num / den)
    out = model.pooled_block_bootstrap({"s": c, "same": c.copy(), "flip": n - c}, n, against="s", reps=300)
    assert out["s"]["auc_ci95"][0] <= out["s"]["auc"] <= out["s"]["auc_ci95"][1]
    assert out["same"]["minus_s"] == {"diff": 0.0, "ci95": [0.0, 0.0]}  # the same order: no doubt at all
    assert out["flip"]["auc"] == round(1 - c.sum() / n.sum(), 3) and out["flip"]["minus_s"]["ci95"][1] < 0
    assert model.pooled_block_bootstrap({"s": c}, n, reps=300) == model.pooled_block_bootstrap({"s": c}, n, reps=300)
    ids, k = model.block_ids([1000.0, 29_999.0, 30_001.0, 1000.0], [5.0, 29_000.0, 5.0, 31_000.0])
    assert k == 3 and ids[0] == ids[1] and len({int(ids[0]), int(ids[2]), int(ids[3])}) == 3


def test_recent_headline_pools_the_week_with_block_intervals() -> None:
    """The headline pools every pass pair of the last 7 days: its AUC is the mean of the pairs' AUCs
    weighted by burnt x unburnt (comparisons stay inside a pair), its interval resamples 30 km squares,
    the top tenth's share matches the per-pair counts, and a placebo that cannot tell the fields apart
    (no pass of this made-up tile covers them) scores exactly 0.5. A pair 9 days older stays out, and
    with fewer than 100 burnt or unburnt fields there is no figure, only the reason."""
    from datetime import timedelta

    from blindspot.jobs import risk as job

    t1 = datetime(2026, 11, 20, 5, 50, tzinfo=UTC)
    rng = random.Random(4)
    done, rows = [], []
    with session_scope() as s:
        s.execute(text("DROP TABLE IF EXISTS bt_win, bt"))
        s.execute(text("CREATE TEMP TABLE bt_win (pair int, tile text, scope text, t0 timestamptz, t1 timestamptz) "
                       "ON COMMIT DROP"))
        s.execute(text(f"CREATE TEMP TABLE bt ({job.BT_COLUMNS}{model.SCORE_COLUMNS}) ON COMMIT DROP"))
        for pair, (gap, back) in enumerate(((3, 0), (5, 2), (4, 9))):
            p_t1 = t1 - timedelta(days=back)
            p_t0 = p_t1 - timedelta(days=gap)
            s.execute(text("INSERT INTO bt_win VALUES (:p, 'TESTWEEK', NULL, :t0, :t1)"), {"p": pair, "t0": p_t0, "t1": p_t1})
            for i in range(130):
                y = int(i < 60)  # timing separates them, not perfectly: burnt 2-12 days after harvest, unburnt 5-35
                rows.append({"pair": pair, "y": y, "i": i, "lon": 60.0 + rng.random(), "lat": -5.0 + rng.random() / 2,
                             "t0": p_t0, "d": rng.uniform(2, 12) if y else rng.uniform(5, 35)})
            done.append({"tile": "TESTWEEK", "t0": p_t0, "t1": p_t1, "burnt": 60, "unburnt": 70, "pair": pair})
        s.execute(text("""INSERT INTO bt (pair, label, fid, g, t0, days)
                          VALUES (:pair, :y, :i, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :t0, :d)"""), rows)
        for stmt in model.score_sql("bt", "pair"):
            s.execute(text(stmt))
        per_pair = job._pair_metrics(s, history_ok=False, timing=True)
        for r in done:
            r.update(per_pair[r["pair"]])
        got = job._recent(s, done, history_ok=False, timing=True)
        few = job._recent(s, done[:1], history_ok=False, timing=True)
        scored = s.execute(text("SELECT pair, label, s FROM bt")).all()
    week = [model.roc_auc([r[2] for r in scored if r[0] == p], [r[1] for r in scored if r[0] == p]) for p in (0, 1)]
    assert got["shown"] and (got["pairs"], got["tiles"], got["burnt"], got["unburnt"]) == (2, 1, 120, 140)
    assert got["auc"] == round(sum(week) / 2, 3) and got["auc_ci95"][0] <= got["auc"] <= got["auc_ci95"][1]
    assert got["blocks"] >= 2 and got["block_km"] == model.BLOCK_KM
    assert got["placebos"]["observation_only"] == {"auc": 0.5, "auc_ci95": [0.5, 0.5],
                                                   "minus_score": got["placebos"]["observation_only"]["minus_score"]}
    assert got["placebos"]["observation_only"]["minus_score"]["diff"] == round(0.5 - sum(week) / 2, 3)
    timing = got["baselines"]["timing_only"]  # timing is the only factor here: the same order as the score
    assert timing["auc"] == got["auc"] and timing["minus_score"] == {"diff": 0.0, "ci95": [0.0, 0.0]}
    share = sum(per_pair[p]["top_hits"] for p in (0, 1)) / sum(per_pair[p]["top_k"] for p in (0, 1))
    assert got["top_10pct"]["burnt_share"] == pytest.approx(share, abs=0.002)
    assert got["top_10pct"]["base_rate"] == round(120 / 260, 3)
    assert few["shown"] is False and few["auc"] is None and "60 burnt and 70 unburnt" in few["reason"]


def test_village_level_keeps_same_named_villages_apart() -> None:
    """A village is a (name, sub-district) pair, as in the district brief: two "Kotli"s in different
    tehsils are two villages. Grouped by name alone they merged, so the unburnt one inherited the
    other's burn and their hectares were summed."""
    from blindspot.jobs.risk import _village_level

    villages = ["Kotli", "Kotli"] + [f"V{i}" for i in range(10)]
    subs = ["Malerkotla", "Dhuri"] + ["Dhuri"] * 10
    areas = [5.0, 1.0] + [2.0] * 10
    scores = [90.0, 10.0] + [50.0 - i for i in range(10)]
    labels = [1, 0] + [int(i < 3) for i in range(10)]
    v = _village_level(villages, areas, scores, labels, subs)
    assert v["villages"] == 12 and v["with_a_burn"] == 4  # the Dhuri Kotli did not burn
    merged = _village_level(villages, areas, scores, labels)  # no sub-districts known: names alone
    assert merged["villages"] == 11 and merged["with_a_burn"] == 4
    # the burnt Kotli ranks first by risk-weighted hectares (90/100 x 5 ha), the unburnt one last
    assert v["precision_at_k"]["5"] == 0.8 and v["auc"] == round(model.roc_auc(
        [0.9 * 5, 0.1 * 1] + [(50.0 - i) / 100 * 2 for i in range(10)], labels), 3)


def test_verdict_and_strength_follow_the_numbers() -> None:
    from blindspot.jobs.risk import ranking_strength, verdict

    a = {"pairs": 148, "field_pairs": 361031, "tiles": 12, "auc_weighted_mean": 0.56, "auc_ci95": [0.491, 0.663],
         "top_10pct": {"burnt_share": 0.216, "base_rate": 0.152, "lift": 1.418, "lift_ci95": [1.235, 1.733]}}
    text_ = verdict({"all_pairs": a, "headline": {"tile": "43REP", "n": 1173, "auc": 0.701, "auc_ci95": [0.664, 0.736]}})
    assert ranking_strength(a) == "weak" and text_.startswith("The ranking is weak.")
    assert "0.560 (95% CI 0.491-0.663" in text_ and "not clearly better than chance" in text_
    assert "1.42 times as often" in text_ and "21.6% against 15.2%" in text_
    assert ranking_strength({**a, "auc_weighted_mean": 0.72, "auc_ci95": [0.66, 0.78]}) == "moderate"


def test_verdict_states_the_pooled_week_not_one_pair() -> None:
    """The headline is the last 7 days of pass pairs pooled, with its interval from resampling squares
    of land and what the placebos scored; one pair (the old headline, 0.355 one day and 0.878 another)
    is no longer quoted, and with too few fields the verdict says so instead of giving a number.
    (Per-tile AUCs of the reviewer's recompute of 2026-10-31: 43REQ held 35% of the checks at 0.444.)
    Changed on purpose on 2026-11-23: this test used to check the latest-pair sentence."""
    from blindspot.jobs.risk import _all_pairs, verdict

    a = {"pairs": 148, "field_pairs": 361031, "tiles": 4, "auc_weighted_mean": 0.56, "auc_ci95": [0.491, 0.663],
         "top_10pct": {}, "auc_by_tile": [
             {"tile": "43REQ", "pairs": 40, "field_checks": 126082, "auc": 0.444},
             {"tile": "43REP", "pairs": 30, "field_checks": 90000, "auc": 0.718},
             {"tile": "43RFP", "pairs": 30, "field_checks": 80000, "auc": 0.61},
             {"tile": "43RFQ", "pairs": 10, "field_checks": 60, "auc": 0.9}]}  # too few checks to compare
    recent = {"shown": True, "window": {"from": "2026-11-15T06:00:00Z", "to": "2026-11-22T05:50:00Z", "days": 7},
              "pairs": 107, "tiles": 18, "burnt": 12345, "unburnt": 67890, "auc": 0.6, "auc_ci95": [0.55, 0.65],
              "block_km": 30.0, "min_each": 100,
              "placebos": {"location_only": {"auc": 0.58, "auc_ci95": [0.52, 0.63],
                                             "minus_score": {"diff": -0.02, "ci95": [-0.05, 0.01]}},
                           "observation_only": {"auc": 0.5, "auc_ci95": [0.49, 0.51],
                                                "minus_score": {"diff": -0.1, "ci95": [-0.15, -0.05]}}}}
    text_ = verdict({"all_pairs": a, "recent": recent,
                     "latest_pair": {"tile": "43REP", "n": 1173, "auc": 0.701, "auc_ci95": [0.664, 0.736]}})
    assert ("Over the last 7 days of passes (2026-11-15 to 2026-11-22: 107 pass pairs on 18 tiles, 12,345 burnt and "
            "67,890 unburnt fields, pooled) the AUC is 0.600 (95% CI 0.550-0.650, from resampling 30 km squares") in text_
    assert ("Placebos that know only where a field lies 0.580 (95% CI 0.520-0.630) or only how often it was seen "
            "0.500") in text_ and "A placebo above 0.5 means" in text_
    # a placebo whose difference from the score may be >= 0 is named; one clearly behind is not
    assert ("The score is not clearly ahead of the placebo that knows only where a field lies (-0.020, 95% CI "
            "-0.050 to +0.010)") in text_ and "how often it was seen (-0.100" not in text_
    assert "0.701" not in text_ and "latest pass pair" not in text_
    assert "Tile by tile (over 100 field checks) the AUC runs from 0.444 (43REQ, 126,082 checks) to 0.718 (43REP)." in text_
    few = verdict({"all_pairs": a, "recent": {"shown": False, "window": {"days": 7}, "burnt": 80, "unburnt": 4000,
                                              "min_each": 100}})
    assert "too few fields for a figure of their own (80 burnt, 4,000 unburnt; 100 of each are needed)" in few
    calm = verdict({"all_pairs": a, "recent": {**recent, "placebos": {"location_only": {"auc": 0.51,
                                                                                         "auc_ci95": [0.47, 0.55]}}}})
    assert "A placebo above 0.5" not in calm
    # per tile: the size-weighted mean of its pairs' AUCs
    t0, t1 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)
    pair = {"t0": t0, "t1": t1, "base_rate": 0.1, "baselines_auc": {"fires_7d_only": 0.5}, "top_k": 1, "top_hits": 0.0,
            "bands": [[1, 0]] * len(model.BANDS)}
    done = [{**pair, "tile": "A", "burnt": 10, "unburnt": 90, "auc": 0.6},
            {**pair, "tile": "A", "burnt": 30, "unburnt": 270, "auc": 0.8},
            {**pair, "tile": "B", "burnt": 5, "unburnt": 45, "auc": None}]
    tiles = {t["tile"]: t for t in _all_pairs(done)[0]["auc_by_tile"]}
    assert tiles["A"] == {"tile": "A", "pairs": 2, "field_checks": 400, "auc": 0.75}
    assert tiles["B"]["auc"] is None and tiles["B"]["field_checks"] == 50


# --- GET /api/risk: rank, percentile, calibrated probability -----------------------------------------------

TEST_BBOX = "63.9,-7.1,64.3,-6.9"  # Indian Ocean: no other test's rows are here


@pytest.fixture
def client():
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def test_route_states_rank_percentile_and_calibrated_probability(client) -> None:
    """risk_rank / risk_percentile / risk_band describe today's ordering by risk_score; burn_probability is
    the calibrated share of the field's band in the ordering the backtest tested (neighbourhood and
    history, no timing), placed by the run's stored cuts; the method note says the tested part is weak
    and how closely today's score follows it."""
    from blindspot.jobs.risk import ranking_strength

    scored = model.score_fields(_features(), AS_OF)
    a, b = max(scored, key=lambda s: s.score), min(scored, key=lambda s: s.score)
    tested = {s.id: model.tested_score(s.factors["neighbourhood"]["value"], s.factors["history"]["value"])
              for s in (a, b)}
    hi, lo = max(tested.values()), min(tested.values())
    assert hi - lo > 0.01  # the two recorded fields differ in the tested ordering
    cuts = [hi - 0.001, hi - 0.002, hi - 0.003, lo - 0.001, lo - 0.002]  # hi: top 1%, lo: top 10-25%
    want = {sid: ("top 1%" if t == hi else "top 10-25%") for sid, t in tested.items()}
    bands = [{"top_share_max": edge, "band": label, "fields": 100, "burnt": 10 * (6 - i),
              "burnt_share": round(0.1 * (6 - i), 3), "burnt_share_ci95": [round(0.1 * (5 - i), 3), round(0.1 * (7 - i), 3)],
              "lift": None} for i, (edge, label) in enumerate(model.BANDS)]
    all_pairs = {"pairs": 148, "field_pairs": 361031, "tiles": 12, "auc_weighted_mean": 0.56,
                 "auc_ci95": [0.491, 0.663], "auc_by_pair": []}
    t = AS_OF
    ids: list[int] = []
    with session_scope() as s:
        cols = {c for (c,) in s.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'harvest_cluster'"))}
        shared = model.run_constants(a.factors)
        run = s.execute(text("INSERT INTO risk_run (as_of, model, fields, factors) VALUES "
                             "('2099-01-01', :m, 2, CAST(:f AS jsonb)) RETURNING id"),
                        {"m": model.MODEL_VERSION, "f": orjson.dumps(shared, option=orjson.OPT_UTC_Z).decode()}).scalar_one()
        report = {"computed_at": "2099-01-01T00:00:00Z", "model": model.MODEL_VERSION, "windows": [{}, {}, {}],
                  "recent": {"shown": True, "auc": 0.61, "auc_ci95": [0.55, 0.66], "pairs": 3},
                  "latest_pair": {"tile": "TEST", "auc": 0.7},
                  "all_pairs": all_pairs, "verdict": "The ranking is weak.",
                  "strength": ranking_strength(all_pairs), "calibration": {"bands": bands, "base_rate": 0.15},
                  "tested_order": {"run_id": run, "fields": 2, "spearman_with_risk_score": 0.754, "top_1pct_overlap": 0.494,
                                   "cuts": [{"band": label, "min_tested_score": c}
                                            for (_, label), c in zip(model.BANDS[:-1], cuts, strict=True)]}}
        s.execute(text("UPDATE risk_run SET backtest = CAST(:b AS jsonb) WHERE id = :id"),
                  {"b": orjson.dumps(report).decode(), "id": run})
        rows = {i: r for i, r in enumerate(ROWS, start=1)}
        for i, field in enumerate((a, b, None)):
            lon = 64.0 + i / 100
            r = rows[(field or a).id]
            values = {
                "geom": f"SRID=4326;POLYGON(({lon} -7,{lon + 0.002} -7,{lon + 0.002} -6.998,{lon} -6.998,{lon} -7))",
                "centroid": f"SRID=4326;POINT({lon + 0.001} -6.999)", "area_ha": 2.0 + i, "n_px": 50, "res_m": 20,
                "mgrs_tile": "TEST", "harvested_after": datetime.fromisoformat(r["harvested_after"]),
                "harvested_before": datetime.fromisoformat(r["harvested_before"]), "last_seen_unburnt": t,
                "scene_last": "test", "collection": "test", "village": "Honestpur", "district": "Test district",
                "risk_score": None if field is None else field.score, "model_version": "test-honesty-risk",
                "risk_factors": None if field is None else orjson.dumps(
                    {"run": run, "v": model.pack_values(model.row_values(field.factors))}).decode(),
            }
            if "scope" in cols:
                values["scope"] = "test"
            vals = ", ".join(f"CAST(:{k} AS jsonb)" if k == "risk_factors" else f":{k}" for k in values)
            ids.append(s.execute(text(f"INSERT INTO harvest_cluster ({', '.join(values)}) VALUES ({vals}) RETURNING id"),
                                 values).scalar_one())
    try:
        body = client.get("/api/risk", params={"bbox": TEST_BBOX, "limit": 10}).json()
        props = [f["properties"] for f in body["features"]["features"]]
        assert [p["risk_score"] for p in props] == [round(a.score, 2), round(b.score, 2), None]
        pa, pb, pc = props
        assert pa["risk_rank"] < pb["risk_rank"] and pa["risk_rank_of"] == pb["risk_rank_of"] >= 2
        assert 0 <= pb["risk_percentile"] < pa["risk_percentile"] < 100
        for p, field in ((pa, a), (pb, b)):  # the band of the TESTED ordering carries the calibration
            band = next(x for x in bands if x["band"] == want[field.id])
            assert p["burn_probability_band"] == want[field.id]
            assert p["burn_probability"] == band["burnt_share"] and p["burn_probability_ci95"] == band["burnt_share_ci95"]
        assert {pa["risk_band"], pb["risk_band"]} <= {label for _, label in model.BANDS}  # today's ordering
        assert pc["risk_rank"] is None and pc["burn_probability"] is None and pc["burn_probability_band"] is None
        assert body["method"]["model"] == model.MODEL_VERSION and "not a probability" in body["method"]["scale"]
        note = body["method"]["note"]
        assert "the part of that ordering the backtest could test" in note
        assert "is weak (AUC 0.560 over 148 pass pairs, 95% CI 0.491-0.663" in note
        assert "Timing is untested; today's full score follows the tested ordering at Spearman 0.75" in note
        bt = body["backtest"]
        assert bt["verdict"] == "The ranking is weak." and bt["windows"] == 3 and bt["calibration"]["bands"] == bands
        assert bt["tested_order"]["run_id"] == run
        # the headline is the pooled week; one pair's figure is detail, never under "headline"
        assert bt["recent"]["auc"] == 0.61 and bt["latest_pair"]["auc"] == 0.7 and "headline" not in bt
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM harvest_cluster WHERE id = ANY(:ids) AND model_version = 'test-honesty-risk'"),
                      {"ids": ids})
            s.execute(text("DELETE FROM risk_run WHERE id = :id"), {"id": run})


def test_burn_probability_is_null_without_the_runs_tested_cuts() -> None:
    """A backtest whose tested_order belongs to another run (or has none) gives no burn_probability:
    the bands would describe a different ordering."""
    from blindspot.api.routers.risk import _placing, _tested_cuts

    bt = {"tested_order": {"run_id": 7, "cuts": [{"band": "top 1%", "min_tested_score": 50.0}]}}
    assert _tested_cuts(bt, {"id": 8}) is None and _tested_cuts({}, {"id": 7}) is None
    assert _tested_cuts(bt, {"id": 7}) == [50.0]
    cal = [{"band": label, "burnt_share": 0.2, "burnt_share_ci95": [0.1, 0.3]} for _, label in model.BANDS]
    out = _placing((1, 10, 0.1), cal, tested=60.0, cuts=None)
    assert out["burn_probability"] is None and out["risk_band"] == "top 5-10%"
    assert _placing((1, 10, 0.1), cal, tested=60.0, cuts=[50.0])["burn_probability_band"] == "top 1%"


def test_tested_score_sql_equals_python() -> None:
    """The job's band cuts (SQL) and the route's placement (Python) use the same tested score, to the bit."""
    scored = model.score_fields(_features(), AS_OF)
    with session_scope() as s:
        for f in scored:
            nv, hv = f.factors["neighbourhood"]["value"], f.factors["history"]["value"]
            got = s.execute(text(f"SELECT {model.tested_score_sql('CAST(:n AS float8)', 'CAST(:h AS float8)')}"),
                            {"n": nv, "h": hv}).scalar_one()
            assert got == model.tested_score(nv, hv)
        got = s.execute(text(f"SELECT {model.tested_score_sql('CAST(:n AS float8)', 'CAST(NULL AS float8)')}"),
                        {"n": 0.25}).scalar_one()
        assert got == model.tested_score(0.25, None) == 25.0


def test_method_says_when_stored_scores_are_from_an_older_model() -> None:
    from blindspot.api.routers.risk import METHOD, _method

    assert _method(None, {"model": model.MODEL_VERSION}) == METHOD
    old = _method(None, {"model": "risk-v2"})
    assert old["scores_model"] == "risk-v2" and old["scale"].startswith("Today's stored scores were made by risk-v2")


def test_scores_are_written_in_steps_on_one_connection(monkeypatch) -> None:
    """The job writes each field's score in steps, each committed (with a VACUUM between) so the
    table never needs room for a second copy of itself; risk_stage, a temporary table, has to
    outlive those commits on the job's one connection. Commits, so only on the tests' database."""
    from datetime import timedelta

    from sqlalchemy.orm import Session

    from blindspot.db import engine
    from blindspot.jobs import risk as job

    if not (engine.url.database or "").endswith("_test"):
        pytest.skip("commits: only on the *_test database")
    from blindspot import db

    monkeypatch.setattr(job, "STEP_ROWS", 2)
    monkeypatch.setattr(db, "VACUUM_EVERY", 1)  # vacuum after every step, as a big run does
    now = datetime.now(UTC)
    with session_scope() as s:
        ids = [s.execute(text("""
            INSERT INTO harvest_cluster (geom, centroid, area_ha, n_px, res_m, mgrs_tile, scope, harvested_after,
                                         harvested_before, last_seen_unburnt, scene_last, collection, ndvi_last,
                                         model_version)
            VALUES (ST_GeomFromText('POLYGON((75 30, 75.001 30, 75.001 30.001, 75 30))', 4326),
                    ST_SetSRID(ST_MakePoint(75.0005, 30.0003), 4326), 1, 25, 20, 'TESTR', 'hotspots',
                    :a, :b, :b, 'test', 'test', 0.2, 'test') RETURNING id"""),
            {"a": now - timedelta(days=4), "b": now}).scalar_one()
               for _ in range(5)]
        run_id = s.execute(text("INSERT INTO risk_run (as_of, model, fields, factors) "
                                "VALUES (:t, 'test', 5, '{}'::jsonb) RETURNING id"), {"t": now}).scalar_one()
    try:
        with engine.connect() as conn, Session(bind=conn, expire_on_commit=False) as session:
            session.execute(text("CREATE TEMP TABLE risk_stage (id bigint PRIMARY KEY, score float8, v jsonb)"))
            session.execute(text("INSERT INTO risk_stage SELECT id, id % 100, to_jsonb('[1]'::text) "
                                 "FROM harvest_cluster WHERE id = ANY(:i)"), {"i": ids})
            session.commit()
            changed = job._apply_stage_in_steps(session, run_id)
            again = job._apply_stage_in_steps(session, run_id)  # the same numbers: nothing to rewrite
            session.execute(text("DROP TABLE risk_stage"))
            session.commit()
        with session_scope() as s:
            got = dict(s.execute(text("SELECT id, risk_score FROM harvest_cluster WHERE id = ANY(:i)"), {"i": ids}).all())
            runs = {r.id: r for r in s.execute(text(
                "SELECT id, risk_factors ->> 'run' AS run FROM harvest_cluster WHERE id = ANY(:i)"), {"i": ids})}
            stored = s.execute(text("SELECT changed FROM risk_run WHERE id = :r"), {"r": run_id}).scalar()
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM harvest_cluster WHERE id = ANY(:i)"), {"i": ids})
            s.execute(text("DELETE FROM risk_run WHERE id = :r"), {"r": run_id})
    assert changed == 5 and again == 0 and stored == 5  # the run row adds up its steps
    assert got == {i: float(i % 100) for i in ids}
    assert {int(r.run) for r in runs.values()} == {run_id}
