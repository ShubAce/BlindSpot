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


def test_verdict_and_strength_follow_the_numbers() -> None:
    from blindspot.jobs.risk import ranking_strength, verdict

    a = {"pairs": 148, "field_pairs": 361031, "tiles": 12, "auc_weighted_mean": 0.56, "auc_ci95": [0.491, 0.663],
         "top_10pct": {"burnt_share": 0.216, "base_rate": 0.152, "lift": 1.418, "lift_ci95": [1.235, 1.733]}}
    text_ = verdict({"all_pairs": a, "headline": {"tile": "43REP", "n": 1173, "auc": 0.701, "auc_ci95": [0.664, 0.736]}})
    assert ranking_strength(a) == "weak" and text_.startswith("The ranking is weak.")
    assert "0.560 (95% CI 0.491-0.663" in text_ and "not clearly better than chance" in text_
    assert "1.42 times as often" in text_ and "21.6% against 15.2%" in text_
    assert ranking_strength({**a, "auc_weighted_mean": 0.72, "auc_ci95": [0.66, 0.78]}) == "moderate"


def test_verdict_puts_the_latest_pair_in_its_place() -> None:
    """The latest pair's AUC comes with the caveat that its interval is too narrow, and with where its
    tile stands among the tiles (per-tile AUCs of the reviewer's recompute of 2026-10-31: the headline
    tile 43REP was the best tile with over 100 checks, 43REQ held 35% of the checks at 0.444)."""
    from blindspot.jobs.risk import _all_pairs, verdict

    a = {"pairs": 148, "field_pairs": 361031, "tiles": 4, "auc_weighted_mean": 0.56, "auc_ci95": [0.491, 0.663],
         "top_10pct": {}, "auc_by_tile": [
             {"tile": "43REQ", "pairs": 40, "field_checks": 126082, "auc": 0.444},
             {"tile": "43REP", "pairs": 30, "field_checks": 90000, "auc": 0.718},
             {"tile": "43RFP", "pairs": 30, "field_checks": 80000, "auc": 0.61},
             {"tile": "43RFQ", "pairs": 10, "field_checks": 60, "auc": 0.9}]}  # too few checks to compare
    text_ = verdict({"all_pairs": a, "headline": {"tile": "43REP", "n": 1173, "auc": 0.701, "auc_ci95": [0.664, 0.736]}})
    assert "On the latest pass pair alone (43REP, 1,173 fields) the AUC is 0.701" in text_
    assert "narrower than the truth" in text_
    assert "from 0.444 (43REQ, 126,082 checks) to 0.718 (43REP); 43REP ranks 1 of 3, so that one pair flatters" in text_
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
                  "headline": {"tile": "TEST", "auc": 0.7}, "all_pairs": all_pairs, "verdict": "The ranking is weak.",
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
