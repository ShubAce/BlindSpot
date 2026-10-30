"""The HTTP API. Routers import their shared helpers from here:

    from blindspot.api import BBoxDep, SessionDep, respond

    router = APIRouter(prefix="/api/fires", tags=["fires"])

    @router.get("")
    def fires(session: SessionDep, bbox: BBoxDep, hours: int = 24):
        ...
        return respond({"hours": hours, "features": features}, latest_refs(session, ["firms"]))

Write routes as plain `def` (not async): the database and blindspot.http calls are blocking, and
FastAPI runs plain functions in a thread pool.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from blindspot.api.responses import ORJSONResponse, respond
from blindspot.config import BBox
from blindspot.db import get_session
from blindspot.geo import parse_bbox


def _bbox_param(bbox: str | None = None) -> BBox:
    """The `bbox=minLon,minLat,maxLon,maxLat` query parameter; the AOI when absent."""
    try:
        return parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


SessionDep = Annotated[Session, Depends(get_session)]
BBoxDep = Annotated[BBox, Depends(_bbox_param)]

__all__ = ["BBoxDep", "ORJSONResponse", "SessionDep", "respond"]
