"""The area we watch, and how we name a location in it (state, district, subdistrict, village).

The lookups read the boundary and place tables, which the boundaries module fills. Until it
has, every lookup returns None: a name is left blank rather than guessed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.orm import Session

from blindspot.config import BBox, get_settings

IST = ZoneInfo("Asia/Kolkata")  # local time, for display and for daily schedules
AOI_NAME = "Punjab, Haryana and Delhi NCR"
AOI_BBOX: BBox = get_settings().aoi_bbox  # min_lon, min_lat, max_lon, max_lat
MET_BBOX: BBox = get_settings().met_bbox  # wider, so wind fields cover smoke leaving the AOI


@dataclass(frozen=True)
class AdminArea:
    state: str | None
    district: str | None
    subdistrict: str | None = None


@dataclass(frozen=True)
class NearestPlace:
    id: int
    kind: str
    name: str
    district: str | None
    state: str | None
    lat: float
    lon: float
    population: int | None
    distance_km: float


def parse_bbox(value: str | None, default: BBox = AOI_BBOX) -> BBox:
    """Parse a `bbox=minLon,minLat,maxLon,maxLat` query parameter; empty gives `default`.

    Raises ValueError with a readable message (blindspot.api.BBoxDep turns it into a 400).
    """
    if value is None or not value.strip():
        return default
    usage = "bbox must be four numbers: minLon,minLat,maxLon,maxLat"
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError:
        raise ValueError(usage) from None
    if len(parts) != 4:
        raise ValueError(usage)
    min_lon, min_lat, max_lon, max_lat = parts
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError(f"{usage}, with min < max, inside -180..180 and -90..90")
    return (min_lon, min_lat, max_lon, max_lat)


def in_bbox(lon: float, lat: float, bbox: BBox = AOI_BBOX) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def point_ewkt(lon: float, lat: float) -> str:
    """A value a Geometry column accepts on insert: 'SRID=4326;POINT(lon lat)'."""
    return f"SRID=4326;POINT({lon} {lat})"


def bbox_envelope(bbox: BBox) -> ColumnElement[Any]:
    """The bbox as a SQL polygon: `.where(Fire.geom.ST_Intersects(bbox_envelope(bbox)))`."""
    return func.ST_MakeEnvelope(*bbox, 4326)


# One query for any number of points: each point looks up the polygon of each level it falls in
# (index-assisted). A district row carries its state; the state level is only a fallback.
_ADMIN_SQL = text(
    """
    WITH pts AS (
        SELECT p.i, ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326) AS g
        FROM unnest(CAST(:lons AS float8[]), CAST(:lats AS float8[]))
             WITH ORDINALITY AS p(lon, lat, i)
    )
    SELECT pts.i, COALESCE(d.state, s.name) AS state, d.name AS district, sd.name AS subdistrict
    FROM pts
    LEFT JOIN LATERAL (
        SELECT b.name, b.state FROM boundary b
        WHERE b.level = 'district' AND ST_Intersects(b.geom, pts.g) ORDER BY b.id LIMIT 1
    ) d ON true
    LEFT JOIN LATERAL (
        SELECT b.name FROM boundary b
        WHERE b.level = 'subdistrict' AND ST_Intersects(b.geom, pts.g) ORDER BY b.id LIMIT 1
    ) sd ON true
    LEFT JOIN LATERAL (
        SELECT b.name FROM boundary b
        WHERE b.level = 'state' AND ST_Intersects(b.geom, pts.g) ORDER BY b.id LIMIT 1
    ) s ON true
    ORDER BY pts.i
    """
)


def admin_for_points(
    session: Session, points: Sequence[tuple[float, float]]
) -> list[AdminArea | None]:
    """State, district and subdistrict for many (lon, lat) points in one SQL query, in input order.

    A point outside every boundary (or any point before boundaries are loaded) gets None.
    """
    if not points:
        return []
    params = {"lons": [float(p[0]) for p in points], "lats": [float(p[1]) for p in points]}
    out: list[AdminArea | None] = []
    for _i, state, district, subdistrict in session.execute(_ADMIN_SQL, params):
        found = state is not None or district is not None or subdistrict is not None
        out.append(AdminArea(state, district, subdistrict) if found else None)
    return out


def district_for_point(session: Session, lon: float, lat: float) -> AdminArea | None:
    """State, district and subdistrict of one point, or None when no boundary contains it."""
    return admin_for_points(session, [(lon, lat)])[0]


# Take the 16 nearest by planar distance in degrees (the spatial index can answer that), then
# rank them by true distance on the ellipsoid: here a degree of longitude is ~13% shorter than
# a degree of latitude, so the planar nearest is not always the real nearest.
_NEAREST_SQL = text(
    """
    SELECT id, kind, name, district, state, lat, lon, population,
           ST_Distance(geom::geography,
                       ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) / 1000.0
               AS distance_km
    FROM (
        SELECT id, kind, name, district, state, lat, lon, population, geom FROM place
        WHERE kind = ANY(:kinds)
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
        LIMIT 16
    ) candidates
    ORDER BY distance_km
    LIMIT 1
    """
)


def nearest_place(
    session: Session,
    lon: float,
    lat: float,
    kinds: Sequence[str] = ("village", "town"),
    max_km: float | None = None,
) -> NearestPlace | None:
    """The closest place of the given kinds with its distance in km, or None when there is none
    (or none within max_km)."""
    params = {"lon": float(lon), "lat": float(lat), "kinds": list(kinds)}
    row = session.execute(_NEAREST_SQL, params).mappings().first()
    if row is None or (max_km is not None and row["distance_km"] > max_km):
        return None
    return NearestPlace(**dict(row))


def fill_admin(session: Session, table: str, *, only_missing: bool = True) -> int:
    """Set `state` and `district` on rows of `table` from the district polygons, in one UPDATE.

    For any table with geom, state and district columns (fire_detection, burn_cluster, ...). Run
    it after boundaries load to label rows stored before they existed. A polygon is placed by a
    point on its surface, so a scar on a district border still gets exactly one district.
    Returns the number of rows updated; the caller commits.
    """
    quoted = session.get_bind().dialect.identifier_preparer.quote(table)
    missing = "AND t.district IS NULL" if only_missing else ""
    sql = text(
        f"""
        UPDATE {quoted} AS t
        SET state = b.state, district = b.name
        FROM boundary AS b
        WHERE b.level = 'district' AND ST_Intersects(b.geom, ST_PointOnSurface(t.geom)) {missing}
        """
    )
    return session.execute(sql).rowcount
