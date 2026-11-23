"""The other scars inside a case file's pictures (GET /api/scars/{id}, `other_scars`).

A case's pictures show every field around it, and most dark fields in them are other stored scars,
so the case file lists those (same cut as the live map) for the pictures to outline, each with how
it stands to the case's two pictures. Fixture scars: small squares around a case near 28.3 N,
77.3 E on a tile of their own (TESTNB), removed afterwards.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text

from blindspot.analytics.burnscar import RULE
from blindspot.api.routers import scars as scars_router
from blindspot.db import SessionLocal, init_db
from blindspot.models.sentinel import BurnCluster

TILE = "TESTNB"
LON, LAT = 77.30, 28.30
T0 = dt.datetime(2026, 11, 17, 5, 50, 52, tzinfo=dt.UTC)  # the case's before picture
T1 = dt.datetime(2026, 11, 22, 5, 50, 50, tzinfo=dt.UTC)  # its after picture
DAY = dt.timedelta(days=1)
M_LON = 111_320 * math.cos(math.radians(LAT))  # metres per degree
M_LAT = 111_320


def _ring(dx_m: float, dy_m: float, side_m: float) -> list[tuple[float, float]]:
    x, y = LON + dx_m / M_LON, LAT + dy_m / M_LAT
    hx, hy = side_m / 2 / M_LON, side_m / 2 / M_LAT
    return [(x - hx, y - hy), (x + hx, y - hy), (x + hx, y + hy), (x - hx, y + hy), (x - hx, y - hy)]


def _circle(dx_m: float, dy_m: float, r_m: float, n: int) -> list[tuple[float, float]]:
    x, y = LON + dx_m / M_LON, LAT + dy_m / M_LAT
    pts = [(x + r_m * math.cos(2 * math.pi * k / n) / M_LON, y + r_m * math.sin(2 * math.pi * k / n) / M_LAT)
           for k in range(n)]
    return [*pts, pts[0]]


def _wkt(ring: list[tuple[float, float]]) -> str:
    return "SRID=4326;POLYGON((" + ",".join(f"{x:.9f} {y:.9f}" for x, y in ring) + "))"


def _row(name: str, ring: list[tuple[float, float]], after: dt.datetime, before: dt.datetime, area: float,
         confidence: float = 0.9, version: str = RULE.version) -> dict:
    cx = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    cy = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)
    return {
        "geom": _wkt(ring), "centroid": f"SRID=4326;POINT({cx:.9f} {cy:.9f})", "area_ha": area, "n_px": 10,
        "res_m": 40, "mgrs_tile": TILE, "scope": "haryana", "block": name, "burn_after": after,
        "burn_before": before, "gap_days": (before - after).total_seconds() / 86400, "scene_pre": f"PRE_{name}",
        "scene_post": f"POST_{name}", "collection": "sentinel-2-c1-l2a", "hazy": False, "dnbr": 0.3,
        "nbr_post": -0.1, "confidence": confidence, "model_version": version, "fetch_id": None,
    }


def _l_shape_outside(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """An L round the box's north-east corner: its bounding box overlaps the picture box, the field
    itself does not reach into it."""
    _, _, x1, y1 = bbox
    return [(x1 - 0.002, y1 + 0.0005), (x1 + 0.002, y1 + 0.0005), (x1 + 0.002, y1 - 0.003), (x1 + 0.003, y1 - 0.003),
            (x1 + 0.003, y1 + 0.001), (x1 - 0.002, y1 + 0.001), (x1 - 0.002, y1 + 0.0005)]


@pytest.fixture(scope="module")
def case_ids() -> dict[str, int]:
    init_db()
    case_ring = _ring(0, 0, 200)
    lat = sum(p[1] for p in case_ring[:-1]) / 4
    bbox = scars_router._chip_bbox({"type": "Polygon", "coordinates": [case_ring]}, lat)
    rows = {
        "case": _row("case", case_ring, T0, T1, 4.0, confidence=0.95),
        # same: the case's own window, and a pass between its two pictures
        "same_a": _row("same_a", _ring(300, 200, 100), T0, T1, 1.0),
        "same_b": _row("same_b", _ring(-300, 200, 100), T0 - 2 * DAY, T0 + 2 * DAY, 3.0),
        "dense": _row("dense", _circle(0, -350, 50, 720), T0, T1, 2.0),
        "tiny": _row("tiny", _ring(450, -350, 1), T0, T1, 0.01),  # smaller than one picture pixel
        # earlier: burnt by the before picture's pass (on this tile, or seconds later on the next one)
        "earlier_a": _row("earlier_a", _ring(-450, -300, 100), T0 - 5 * DAY, T0, 5.0),
        "earlier_b": _row("earlier_b", _ring(150, 420, 100), T0 - 5 * DAY, T0 + dt.timedelta(seconds=20), 0.5),
        # later: first seen after the after picture
        "later": _row("later", _ring(-150, -480, 100), T1, T1 + 5 * DAY, 6.0),
        # never listed: too weak for the live map, an older model, outside the box (also by shape)
        "weak": _row("weak", _ring(300, -150, 100), T0, T1, 1.5, confidence=0.3),
        "old": _row("old", _ring(-300, -150, 100), T0, T1, 1.5, version="test-old-rules"),
        "far": _row("far", _ring(3000, 0, 100), T0, T1, 1.5),
        "l_shape": _row("l_shape", _l_shape_outside(bbox), T0, T1, 1.5),
    }
    with SessionLocal() as session:
        session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": TILE})
        ids = session.execute(insert(BurnCluster).returning(BurnCluster.id, BurnCluster.block),
                              list(rows.values())).all()
        session.commit()
    yield {block: id_ for id_, block in ids}
    with SessionLocal() as session:
        session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": TILE})
        session.commit()


@pytest.fixture(scope="module")
def client() -> TestClient:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def _others(client: TestClient, case_id: int) -> dict:
    r = client.get(f"/api/scars/{case_id}")
    assert r.status_code == 200
    return r.json()


def test_other_scars_in_the_pictures(client, case_ids) -> None:
    body = _others(client, case_ids["case"])
    # additive: the case file keeps every field it had
    assert {"case", "imagery", "fires_nearby", "weather_at_burn", "method", "sources"} <= set(body)
    assert body["case"]["id"] == case_ids["case"] and body["case"]["geometry"]["type"] == "Polygon"
    o = body["other_scars"]
    assert o["bbox"] == body["imagery"]["bbox"]
    assert o["min_confidence"] == 0.5 and o["model_version"] == RULE.version
    by_id = {s["id"]: s for s in o["scars"]}
    names = {v: k for k, v in case_ids.items()}
    listed = {names[i] for i in by_id}
    # the case itself, the weak, the older model and the two outside the box are left out
    assert listed == {"same_a", "same_b", "dense", "tiny", "earlier_a", "earlier_b", "later"}
    assert {names[i]: s["relation"] for i, s in by_id.items()} == {
        "same_a": "same", "same_b": "same", "dense": "same", "tiny": "same",
        "earlier_a": "earlier", "earlier_b": "earlier", "later": "later"}
    assert o["totals"] == {"same": {"scars": 4, "area_ha": 6.01}, "earlier": {"scars": 2, "area_ha": 5.5},
                           "later": {"scars": 1, "area_ha": 6.0}}
    assert (o["total"], o["returned"], o["truncated"]) == (7, 7, False)
    # same first, then earlier, then later; larger first
    assert [names[s["id"]] for s in o["scars"]] == ["same_b", "dense", "same_a", "tiny", "earlier_a", "earlier_b",
                                                    "later"]
    s = by_id[case_ids["same_b"]]
    assert s["burn_after"].endswith("Z") and s["burn_before"].startswith("2026-11-19")
    assert set(s) == {"id", "relation", "burn_after", "burn_before", "unseen", "confidence", "area_ha", "outline"}


def test_outlines_are_simplified_to_a_picture_pixel(client, case_ids) -> None:
    o = _others(client, case_ids["case"])["other_scars"]
    by_id = {s["id"]: s for s in o["scars"]}
    for s in o["scars"]:
        assert s["outline"]["type"] == "Polygon"
        for ring in s["outline"]["coordinates"]:
            assert len(ring) >= 4 and ring[0] == ring[-1]
            assert all(round(c, 5) == c for p in ring for c in p)  # 5 decimals, about 1 m
    # a 720-point circle 100 m across, drawn 512 px to 1.2 km: a few dozen points keep it within a pixel
    dense = by_id[case_ids["dense"]]["outline"]["coordinates"][0]
    assert 8 <= len(dense) < 60
    x0, y0, x1, y1 = o["bbox"]
    px = min(x1 - x0, y1 - y0) / 512
    cx, cy = LON, LAT - 350 / M_LAT
    for x, y in dense:  # every kept point still lies on the circle, within a pixel
        r = math.hypot((x - cx) * M_LON, (y - cy) * M_LAT)
        assert abs(r - 50) < px * M_LAT
    # a field smaller than a pixel keeps an outline: the simplification never drops a scar
    assert len(by_id[case_ids["tiny"]]["outline"]["coordinates"][0]) >= 4


def test_the_list_is_capped_but_the_totals_are_not(client, case_ids, monkeypatch) -> None:
    monkeypatch.setattr(scars_router, "OTHERS_LIMIT", 2)
    o = _others(client, case_ids["case"])["other_scars"]
    names = {v: k for k, v in case_ids.items()}
    assert [names[s["id"]] for s in o["scars"]] == ["same_b", "dense"]  # the largest of the same window
    assert (o["total"], o["returned"], o["truncated"], o["limit"]) == (7, 2, True, 2)
    assert o["totals"]["earlier"] == {"scars": 2, "area_ha": 5.5} and o["totals"]["later"]["scars"] == 1


def test_relations_are_seen_from_each_case(client, case_ids) -> None:
    """From the later scar's own case (its box holds the case and part of the first window), every
    other scar burnt earlier."""
    o = _others(client, case_ids["later"])["other_scars"]
    names = {v: k for k, v in case_ids.items()}
    rel = {names[s["id"]]: s["relation"] for s in o["scars"]}
    assert "later" not in rel and rel["case"] == "earlier" and set(rel.values()) == {"earlier"}
    assert {"case", "dense", "earlier_a"} <= set(rel)
    assert o["totals"]["same"] == {"scars": 0, "area_ha": 0.0} and o["totals"]["later"]["scars"] == 0


def test_a_case_alone_in_its_pictures(client, case_ids) -> None:
    o = _others(client, case_ids["far"])["other_scars"]
    assert (o["total"], o["returned"], o["truncated"], o["scars"]) == (0, 0, False, [])
    assert o["totals"] == {r: {"scars": 0, "area_ha": 0.0} for r in ("same", "earlier", "later")}
