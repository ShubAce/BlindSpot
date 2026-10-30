"""JSON responses for routes.

`respond(payload, sources)` is how a route returns data. It adds `generated_at` and `sources`
(the contract in docs/architecture.md section 5), can set a cache lifetime, and serialises with
orjson: fast for big GeoJSON, fine with numpy arrays, and every datetime comes out as UTC with a
Z. Returning the response object also skips FastAPI's slow per-element encoding of plain dicts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

import orjson
from pydantic import BaseModel
from starlette.responses import JSONResponse

from blindspot.provenance import utcnow

_OPTIONS = (
    orjson.OPT_UTC_Z | orjson.OPT_NAIVE_UTC | orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY
)


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):  # SourceRef and any other pydantic model
        return value.model_dump()
    if isinstance(value, Decimal):  # Postgres numeric columns
        return float(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


class ORJSONResponse(JSONResponse):
    """application/json rendered by orjson. It is also the app's default response class."""

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=_OPTIONS, default=_default)


def respond(
    payload: Mapping[str, Any],
    sources: Iterable[Any] | None = None,
    *,
    status_code: int = 200,
    max_age: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> ORJSONResponse:
    """`payload` plus `generated_at` and `sources` (SourceRef objects), as an orjson response.

    `max_age` adds `Cache-Control: public, max-age=<seconds>`. A payload that already has its
    own `sources` key keeps it when `sources` is not given (GET /api/sources does this).
    """
    body: dict[str, Any] = {"generated_at": utcnow(), **payload}
    if sources is not None or "sources" not in body:
        body["sources"] = list(sources or [])
    all_headers = dict(headers or {})
    if max_age is not None:
        all_headers.setdefault("Cache-Control", f"public, max-age={max_age}")
    return ORJSONResponse(body, status_code=status_code, headers=all_headers)
