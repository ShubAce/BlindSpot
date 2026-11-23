"""Stages as (state, district) pairs, a district moving from the 40 m to the 20 m stage, and case
links that survive a replaced scar (burn_cluster_forward, GET /api/scars/{id}).

The geometries are made up (boxes of a few hundred metres near 30.5 N, 75.6 E, or far from India
for the boundary rows), on tiles no real row uses; every test rolls back or deletes its own rows.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from shapely import affinity
from shapely.geometry import Point, box
from sqlalchemy import text

from blindspot.analytics.burnscar import RULE
from blindspot.db import SessionLocal, init_db
from blindspot.jobs import sentinel as job
from blindspot.models.sentinel import BurnCluster, HarvestCluster

SCOPE_SQL = "t.mgrs_tile = :tile AND t.scope = :scope"
AFTER = dt.datetime(2026, 11, 17, 5, 50, tzinfo=dt.UTC)
BEFORE = dt.datetime(2026, 11, 22, 5, 50, tzinfo=dt.UTC)


@pytest.fixture(scope="module", autouse=True)
def _tables() -> None:
    init_db()


def _scar(poly, scope: str, res: int, tile: str, block: str, before: dt.datetime = BEFORE) -> dict:
    """A burn_cluster row as _finish_tile builds it."""
    return {
        "geom": f"SRID=4326;{poly.wkt}", "centroid": f"SRID=4326;{poly.representative_point().wkt}",
        "area_ha": 1.0, "n_px": 25 if res == 20 else 6, "res_m": res, "mgrs_tile": tile, "scope": scope,
        "block": block, "burn_after": before - dt.timedelta(days=5), "burn_before": before, "gap_days": 5.0,
        "scene_pre": "S2X_TEST_PRE", "scene_post": "S2X_TEST_POST", "collection": "sentinel-2-c1-l2a",
        "hazy": False, "dnbr": 0.3, "nbr_post": -0.1, "d_b12": 0.05, "confidence": 0.5,
        "model_version": RULE.version, "fetch_id": None,
    }


def _field(poly, scope: str, tile: str) -> dict:
    """A harvest_cluster row (harvested, residue unburnt)."""
    return {
        "geom": f"SRID=4326;{poly.wkt}", "centroid": f"SRID=4326;{poly.representative_point().wkt}",
        "area_ha": 1.0, "n_px": 6, "res_m": 40, "mgrs_tile": tile, "scope": scope,
        "harvested_after": AFTER - dt.timedelta(days=5), "harvested_before": AFTER, "last_seen_unburnt": BEFORE,
        "scene_last": "S2X_TEST_POST", "collection": "sentinel-2-c1-l2a", "ndvi_last": 0.2,
        "model_version": RULE.version, "fetch_id": None,
    }


def _ids(session, tile: str) -> dict[str, int]:
    return dict(session.execute(text("SELECT block, id FROM burn_cluster WHERE mgrs_tile = :t"), {"t": tile}).all())


def test_stage_areas_follow_state_district_pairs(monkeypatch) -> None:
    """A hotspot district belongs to one state: its area goes to the 20 m stage and leaves that
    state's 40 m stage; a district of the same name in another state stays where it was."""
    monkeypatch.setattr(job, "HOTSPOT_DISTRICTS", {("Punjab", "Testpur"): ("testpur",),
                                                   ("Haryana", "Testabad"): ("testabad", "test abad")})

    def add(session, level: str, name: str, state: str, x0: float, x1: float) -> None:
        session.execute(text(
            "INSERT INTO boundary (level, name, state, geom, source) VALUES "
            "(:level, :name, :state, ST_Multi(ST_MakeEnvelope(:x0, 10, :x1, 11, 4326)), 'test')"),
            {"level": level, "name": name, "state": state, "x0": x0, "x1": x1})

    with SessionLocal() as session:
        add(session, "state", "Punjab", "Punjab", 10, 12)
        add(session, "district", "Testpur", "Punjab", 10, 11)
        add(session, "district", "Otherpur", "Punjab", 11, 12)
        add(session, "state", "Haryana", "Haryana", 12, 15)
        add(session, "district", "TESTABAD", "Haryana", 12, 13)
        add(session, "district", "Elsewhere", "Haryana", 13, 14)
        add(session, "district", "Testpur", "Haryana", 14, 15)  # same name, other state
        areas, notes = job.stage_geometries(session)
        session.rollback()
    where = {x: [name for name, g in areas.items() if g.contains(Point(x, 10.5))] for x in (10.5, 11.5, 12.5, 13.5, 14.5)}
    assert where == {10.5: ["hotspots"], 11.5: ["punjab"], 12.5: ["hotspots"], 13.5: ["haryana"], 14.5: ["haryana"]}
    assert {"Punjab: Testpur", "Haryana: TESTABAD"} <= set(notes["hotspot_districts_found"])
    assert "Haryana: Testpur" not in notes["hotspot_districts_found"]


def test_a_district_moved_to_20m_leaves_no_40m_rows_and_each_goes_once() -> None:
    """District D moves from the 40 m Haryana stage to the 20 m stage; E stays at 40 m.

    The 20 m stage stores the tile and at once deletes the 40 m rows lying wholly in D (so the
    moved area never holds both, even if the 40 m stage's turn never comes), forwarding their ids
    to its own scars of the same burn. The 40 m stage's rebuild then replaces the scar that
    straddled D and E by its E part, and finds nothing of D left to delete: every old row goes
    exactly once, and no 40 m row overlaps D afterwards."""
    tile = "TEST4"
    d_area, e_area = box(75.60, 30.50, 75.62, 30.52), box(75.62, 30.50, 75.64, 30.52)
    r_in, r_out = box(75.605, 30.505, 75.607, 30.507), box(75.63, 30.505, 75.632, 30.507)
    r_cross = box(75.6185, 30.505, 75.6225, 30.507)  # 37.5% in D, 62.5% in E
    n_in = box(75.6051, 30.5051, 75.6071, 30.5069)  # the 20 m outline of r_in's field
    n_cross_d, r_cross_e = r_cross.intersection(d_area), r_cross.intersection(e_area)
    haryana, hotspots = {"tile": tile, "scope": "haryana"}, {"tile": tile, "scope": "hotspots"}
    with SessionLocal() as session:
        job._replace_rows(session, BurnCluster, SCOPE_SQL, haryana, [
            _scar(r_in, "haryana", 40, tile, "a"), _scar(r_cross, "haryana", 40, tile, "b"),
            _scar(r_out, "haryana", 40, tile, "c")])
        job._replace_rows(session, HarvestCluster, SCOPE_SQL, haryana,
                          [_field(box(75.61, 30.51, 75.611, 30.511), "haryana", tile),
                           _field(box(75.635, 30.51, 75.636, 30.511), "haryana", tile)])
        old = _ids(session, tile)
        # 1. The 20 m stage now covers D.
        hot = job._replace_rows(session, BurnCluster, SCOPE_SQL, hotspots, [
            _scar(n_in, "hotspots", 20, tile, "d"), _scar(n_cross_d, "hotspots", 20, tile, "e")])
        claimed = job.claim_moved_rows(session, tile, "hotspots", d_area.wkt)
        # 2. The 40 m stage, Haryana without D now, rebuilds the tile.
        rest = job._replace_rows(session, BurnCluster, SCOPE_SQL, haryana, [
            _scar(r_out, "haryana", 40, tile, "c"), _scar(r_cross_e, "haryana", 40, tile, "f")])
        again = job.claim_moved_rows(session, tile, "haryana", e_area.wkt)
        now = _ids(session, tile)
        scopes = dict(session.execute(text("SELECT block, scope FROM burn_cluster WHERE mgrs_tile = :t"), {"t": tile}).all())
        fields = session.execute(text("SELECT scope, count(*) FROM harvest_cluster WHERE mgrs_tile = :t GROUP BY 1"),
                                 {"t": tile}).all()
        forwards = dict(session.execute(text("SELECT old_id, new_id FROM burn_cluster_forward WHERE old_id = ANY(:i)"),
                                        {"i": list(old.values())}).all())
        overlap = session.execute(text(
            "SELECT coalesce(sum(ST_Area(ST_Intersection(geom, ST_GeomFromText(:d, 4326)))), 0) FROM burn_cluster "
            "WHERE mgrs_tile = :t AND scope = 'haryana'"), {"d": d_area.wkt, "t": tile}).scalar()
        session.rollback()
    assert hot["inserted"] == 2 and hot["deleted"] == 0
    assert claimed == {"burn_cluster": 1, "harvest_cluster": 1, "forwarded": 1}  # r_in and the field in D
    assert (rest["deleted"], rest["kept"], rest["inserted"], rest["forwarded"]) == (1, 1, 1, 1)  # only r_cross
    assert again == {"burn_cluster": 0, "harvest_cluster": 0, "forwarded": 0}
    assert scopes == {"c": "haryana", "f": "haryana", "d": "hotspots", "e": "hotspots"}
    assert fields == [("haryana", 1)]
    assert overlap == pytest.approx(0.0, abs=1e-12)
    assert now["c"] == old["c"]  # untouched scar of E: same id
    # r_in -> its 20 m outline; r_cross -> its larger part (E, 62.5%) of the same burn
    assert forwards == {old["a"]: now["d"], old["b"]: now["f"]}


def test_a_row_across_the_new_area_edge_is_not_claimed() -> None:
    """A 40 m scar of the stage that keeps E, outlined half a pixel into D (outlines are simplified),
    is that stage's own: only rows wholly inside the moved area are claimed."""
    tile = "TEST4"
    d_area = box(75.60, 30.50, 75.62, 30.52)
    edge = box(75.6199, 30.505, 75.623, 30.507)
    with SessionLocal() as session:
        job._replace_rows(session, BurnCluster, SCOPE_SQL, {"tile": tile, "scope": "haryana"},
                          [_scar(edge, "haryana", 40, tile, "a")])
        claimed = job.claim_moved_rows(session, tile, "hotspots", d_area.wkt)
        left = session.execute(text("SELECT count(*) FROM burn_cluster WHERE mgrs_tile = :t"), {"t": tile}).scalar()
        session.rollback()
    assert claimed == {"burn_cluster": 0, "harvest_cluster": 0, "forwarded": 0} and left == 1


def test_area_key_changes_only_with_the_area() -> None:
    a = box(0, 0, 1000, 1000)
    same = box(0, 0, 1000, 1000).reverse()  # other vertex order
    assert job._area_key(a) == job._area_key(same) == job._area_key(affinity.translate(a, 0.2, 0.2))
    assert job._area_key(a) != job._area_key(box(0, 0, 1000, 990))


@pytest.fixture(scope="module")
def client() -> TestClient:
    from blindspot.api.main import app

    with TestClient(app) as c:
        yield c


def test_a_replaced_scar_link_answers_with_its_successor(client) -> None:
    """/api/scars/<old id> answers with the scar that replaced it (chains followed) and says so in
    forwarded_from; a scar that vanished without a successor of its burn stays 404, and so does a
    chain longer than MAX_FORWARD_HOPS or a cycle."""
    from blindspot.api.routers.scars import MAX_FORWARD_HOPS

    tile = "TEST5"
    poly = box(75.70, 30.40, 75.702, 30.402)
    haryana, punjab = {"tile": tile, "scope": "haryana"}, {"tile": tile, "scope": "punjab"}
    fake = 9_000_000_000_000  # ids no scar has
    try:
        with SessionLocal() as session:
            ids = []
            for i in range(3):  # A, then B replacing A, then C replacing B: same burn, moved outline
                job._replace_rows(session, BurnCluster, SCOPE_SQL, haryana,
                                  [_scar(affinity.translate(poly, 0.0002 * i), "haryana", 40, tile, f"x{i}")])
                ids.append(_ids(session, tile)[f"x{i}"])
            job._replace_rows(session, BurnCluster, SCOPE_SQL, punjab, [_scar(box(75.8, 30.4, 75.802, 30.402),
                                                                            "punjab", 40, tile, "z")])
            gone = _ids(session, tile)["z"]
            job._replace_rows(session, BurnCluster, SCOPE_SQL, punjab, [])  # vanished: no successor
            # fake+1 reaches C in MAX_FORWARD_HOPS hops, fake in one more
            chain = [(fake + k, fake + k + 1) for k in range(MAX_FORWARD_HOPS)] + [(fake + MAX_FORWARD_HOPS, ids[2])]
            chain += [(fake + 100, fake + 101), (fake + 101, fake + 100)]
            session.execute(text("INSERT INTO burn_cluster_forward (old_id, new_id, overlap_share) VALUES (:o, :n, 1)"),
                            [{"o": o, "n": n} for o, n in chain])
            session.commit()
        a, b, c = (client.get(f"/api/scars/{i}") for i in ids)
        far, too_far = client.get(f"/api/scars/{fake + 1}"), client.get(f"/api/scars/{fake}")
        missing, cycle = client.get(f"/api/scars/{gone}"), client.get(f"/api/scars/{fake + 100}")
    finally:
        with SessionLocal() as session:
            session.execute(text("DELETE FROM burn_cluster_forward WHERE old_id = ANY(:i) OR old_id BETWEEN :f AND :g"),
                            {"i": ids + [gone], "f": fake, "g": fake + 200})
            session.execute(text("DELETE FROM burn_cluster WHERE mgrs_tile = :t"), {"t": tile})
            session.commit()
    assert (a.status_code, b.status_code, c.status_code) == (200, 200, 200)
    assert a.json()["case"]["id"] == ids[2] and a.json()["forwarded_from"] == ids[0]
    assert b.json()["case"]["id"] == ids[2] and b.json()["forwarded_from"] == ids[1]
    assert c.json()["case"]["id"] == ids[2] and "forwarded_from" not in c.json()
    assert far.status_code == 200 and far.json()["case"]["id"] == ids[2]  # MAX_FORWARD_HOPS hops
    assert too_far.status_code == 404 and missing.status_code == 404 and cycle.status_code == 404
