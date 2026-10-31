"""The agent's tools: thin wrappers over BlindSpot's own HTTP API, called in-process.

Each tool calls one or more of our endpoints through httpx.ASGITransport on the FastAPI app. So the
agent reuses exactly the module logic and the provenance the map uses: it cannot see anything the
UI cannot, and every number it gets carries the `sources` of the endpoint it came from.

A tool returns a ToolResult: `data` trimmed for the model (small, no geometry, times as IST text
such as "2026-10-30 13:16 IST" so the model never does time-zone arithmetic), the endpoint
`sources`, and a one-line `summary` for the UI timeline. When an endpoint is missing or failing,
the tool says so in `error` and returns no data: the model is told what is missing instead of
being handed a guess.

The runner turns each SourceRef into a citation marker [n] (CitationBook) and shows the model the
marker next to the data, which is how "cite every number" becomes checkable.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import httpx
from google.genai import types
from sqlalchemy import text

from blindspot.config import get_settings
from blindspot.geo import IST, MET_BBOX
from blindspot.provenance import SourceInfo, SourceRef, register_source, utcnow

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 25.0  # one in-process endpoint call; on-demand analyses can take a few seconds
INTERNAL_BASE_URL = "http://blindspot.internal"

# Official reference values the air tool shows next to measurements, so the model can say "above
# the national standard" without recalling a number from memory. Each is cited by its SourceRef.
# CPCB, National Ambient Air Quality Standards, Gazette notification of 18 Nov 2009:
#   PM2.5 24-hour 60 ug/m3, PM10 24-hour 100 ug/m3.
NAAQS_REF = SourceRef(
    key="naaqs_2009",
    label="CPCB National Ambient Air Quality Standards (2009)",
    url="https://cpcb.nic.in/uploads/National_Ambient_Air_Quality_Standards.pdf",
)
NAAQS_PM25_24H_UGM3 = 60
NAAQS_PM10_24H_UGM3 = 100
# WHO Global Air Quality Guidelines (2021): PM2.5 24-hour 15 ug/m3, PM10 24-hour 45 ug/m3.
WHO_REF = SourceRef(
    key="who_aqg_2021",
    label="WHO Global Air Quality Guidelines (2021)",
    url="https://www.who.int/publications/i/item/9789240034228",
)
WHO_PM25_24H_UGM3 = 15
WHO_PM10_24H_UGM3 = 45
# CPCB National Air Quality Index (2014) PM2.5 sub-index bands, 24-hour mean in ug/m3:
# Good 0-30, Satisfactory 31-60, Moderate 61-90, Poor 91-120, Very Poor 121-250, Severe >250.
# https://app.cpcbccr.com/ccr_docs/About_AQI.pdf
NAQI_PM25_BANDS: tuple[tuple[float, str], ...] = (
    (30, "Good"),
    (60, "Satisfactory"),
    (90, "Moderate"),
    (120, "Poor"),
    (250, "Very Poor"),
    (math.inf, "Severe"),
)
# The official crop-fire monitoring window starts on 15 September ("standard protocol developed
# by ISRO"; PIB release of 1 Dec 2025, https://www.pib.gov.in/PressReleasePage.aspx?PRID=2197201).
SEASON_START_MONTH_DAY = (9, 15)

register_source(
    SourceInfo(
        "openmeteo_geocoding",
        "Open-Meteo geocoding (GeoNames)",
        "places",
        None,
        "https://open-meteo.com/en/docs/geocoding-api",
    )
)


# --- small formatting helpers ------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """An aware datetime from an ISO-8601 string (Z or offset; naive means UTC), else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or len(value) < 16 or value[4] != "-" or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ist(value: Any) -> str | None:
    """'2026-10-30 13:16 IST' for an ISO time or datetime; None when it is not a time."""
    when = parse_time(value)
    return f"{when.astimezone(IST):%Y-%m-%d %H:%M} IST" if when else None


def rnd(value: Any, digits: int = 1) -> Any:
    """Round floats for the model (fewer tokens, the same rounding the verifier sees)."""
    if isinstance(value, float) and math.isfinite(value):
        rounded = round(value, digits)
        return int(rounded) if digits == 0 else rounded
    return value


def pruned(value: dict[str, Any]) -> dict[str, Any]:
    """The dict without None values or empty lists: fewer tokens, nothing for the model to
    misread as a zero."""
    return {k: v for k, v in value.items() if v is not None and v != [] and v != {}}


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")  # fmt: skip


def compass(degrees: Any) -> str | None:
    """16-point compass name of a 'from' direction in degrees (315 -> 'NW')."""
    if not isinstance(degrees, (int, float)) or not math.isfinite(degrees):
        return None
    return COMPASS[round((degrees % 360) / 22.5) % 16]


def naqi_band(pm25_24h: Any) -> str | None:
    """CPCB NAQI category of a 24-hour mean PM2.5 (see NAQI_PM25_BANDS)."""
    if not isinstance(pm25_24h, (int, float)) or not math.isfinite(pm25_24h) or pm25_24h < 0:
        return None
    return next(name for upper, name in NAQI_PM25_BANDS if pm25_24h <= upper)


_DROP_KEYS = {"geometry", "coordinates", "trails", "path", "paths", "times", "u", "v", "grid"}
_COORD_KEYS = {"centroid", "center", "lat", "lon", "bbox", "centroid_lon_lat"}  # keep ~10 m


def compact(value: Any, *, max_list: int = 8, max_str: int = 160, depth: int = 0) -> Any:
    """A model-sized copy of an endpoint payload whose exact shape we do not depend on.

    Drops geometry and big arrays, keeps the first `max_list` items of a list (and says how many
    there were), rounds floats, shows ISO times as IST text and caps string length.
    """
    if depth > 5:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _DROP_KEYS:
                continue
            if key in _COORD_KEYS:
                out[key] = _coords(item)
                continue
            out[key] = compact(item, max_list=max_list, max_str=max_str, depth=depth + 1)
        return out
    if isinstance(value, list):
        items = [compact(v, max_list=max_list, max_str=max_str, depth=depth + 1)
                 for v in value[:max_list]]  # fmt: skip
        if len(value) > max_list:
            items.append(f"... {len(value) - max_list} more")
        return items
    if isinstance(value, str):
        return ist(value) or value[:max_str]
    if isinstance(value, float):
        return rnd(value, 2 if abs(value) < 100 else 1)
    return value


def _coords(value: Any) -> Any:
    """Coordinates rounded to 4 decimals (about 10 m), enough to fly to a single field."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_coords(v) for v in value]
    return value


def season_start(today: date) -> date:
    """15 September of the current burning season (last year's before 15 September)."""
    month, day = SEASON_START_MONTH_DAY
    start = date(today.year, month, day)
    return start if today >= start else date(today.year - 1, month, day)


def _features(body: Any) -> list[dict[str, Any]]:
    """The properties of every feature of a FeatureCollection found under body['features']."""
    fc = body.get("features") if isinstance(body, dict) else None
    if isinstance(fc, dict):
        fc = fc.get("features")
    return [f.get("properties") or {} for f in fc or [] if isinstance(f, dict)]


def _centroid(props: dict[str, Any]) -> list[float] | None:
    centre = props.get("centroid")
    if isinstance(centre, dict):  # GeoJSON point
        centre = centre.get("coordinates")
    if isinstance(centre, (list, tuple)) and len(centre) == 2:
        return [rnd(float(centre[0]), 4), rnd(float(centre[1]), 4)]
    return None


# --- calling our own API in-process -----------------------------------------------------------


@dataclass
class ApiReply:
    """One in-process call to our API: the JSON body, or why there is none."""

    path: str
    status: int  # 0 when the call did not complete
    body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def sources(self) -> list[SourceRef]:
        """The endpoint's SourceRefs that point at a real fetch (a never-fetched source is
        dropped: it has nothing to cite)."""
        listed = self.body.get("sources") if isinstance(self.body, dict) else None
        refs = []
        for item in listed or []:
            try:
                ref = SourceRef.model_validate(item)
            except ValueError:
                continue
            if ref.fetched_at is not None:
                refs.append(ref)
        return refs


class InternalApi:
    """An httpx client wired straight into the FastAPI app (no network, no port).

    Use one per run: `async with InternalApi() as api: reply = await api.get("/api/fires")`.
    """

    def __init__(self, app: Any = None, timeout_s: float = CALL_TIMEOUT_S) -> None:
        if app is None:
            from blindspot.api.main import app as main_app  # late: main imports our router

            app = main_app
        self.timeout_s = timeout_s
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url=INTERNAL_BASE_URL,
            headers={"accept": "application/json", "user-agent": "blindspot-agent"},
        )

    async def __aenter__(self) -> InternalApi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> ApiReply:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = await asyncio.wait_for(
                self._client.get(path, params=clean), timeout=self.timeout_s
            )
        except TimeoutError:
            return ApiReply(path, 0, error=f"{path} did not answer within {self.timeout_s:.0f} s")
        except Exception as exc:  # a broken route must not break the agent
            return ApiReply(path, 0, error=f"{path} failed: {type(exc).__name__}")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success and isinstance(body, dict):
            return ApiReply(path, response.status_code, body)
        detail = body.get("detail") if isinstance(body, dict) else None
        if response.status_code == 404 and detail == "Not Found":
            reason = "this data is not available yet (the endpoint is not deployed)"
        else:
            reason = str(detail)[:200] if detail else response.reason_phrase
        return ApiReply(path, response.status_code, body,
                        error=f"{path} returned HTTP {response.status_code}: {reason}")  # fmt: skip


# --- results and citations --------------------------------------------------------------------


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[SourceRef] = field(default_factory=list)
    summary: str = ""
    error: str | None = None
    map_event: dict[str, Any] | None = None  # show_on_map only: {action, payload}


def failed(summary: str, *replies: ApiReply, error: str | None = None) -> ToolResult:
    """A ToolResult for evidence that could not be read, saying exactly what failed."""
    reasons = [r.error for r in replies if r.error] or ([error] if error else [])
    message = "; ".join(r for r in reasons if r) or "no data"
    return ToolResult(False, {}, [], f"{summary}: unavailable", error=message)


def _dedupe(refs: list[SourceRef]) -> list[SourceRef]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for ref in refs:
        identity = (ref.key, ref.fetch_id, ref.url)
        if identity not in seen:
            seen.add(identity)
            out.append(ref)
    return out


class CitationBook:
    """Gives every distinct source of a run one marker, [1] .. [n], in the order first seen."""

    def __init__(self) -> None:
        self._by_identity: dict[tuple[Any, ...], int] = {}
        self.refs: dict[int, SourceRef] = {}

    @staticmethod
    def _identity(ref: SourceRef) -> tuple[Any, ...]:
        return (ref.key, ref.fetch_id) if ref.fetch_id is not None else (ref.key, ref.url)

    def mark(self, ref: SourceRef) -> int:
        identity = self._identity(ref)
        if identity not in self._by_identity:
            marker = len(self.refs) + 1
            self._by_identity[identity] = marker
            self.refs[marker] = ref
        return self._by_identity[identity]

    def for_model(self, refs: list[SourceRef]) -> list[dict[str, Any]]:
        """What the model sees for each source: its marker, name and how fresh it is."""
        listed = []
        for ref in refs:
            entry: dict[str, Any] = {"marker": f"[{self.mark(ref)}]", "source": ref.label}
            if ref.fetched_at is not None:
                entry["fetched"] = ist(ref.fetched_at)
            listed.append(entry)
        return listed

    def for_client(self, refs: list[SourceRef]) -> list[dict[str, Any]]:
        """SourceRefs for SSE events, each with its marker (an additive field)."""
        return [{**ref.model_dump(mode="json"), "marker": self.mark(ref)} for ref in refs]

    def citation(self, marker: int) -> dict[str, Any] | None:
        ref = self.refs.get(marker)
        return {"marker": marker, "source": ref.model_dump(mode="json")} if ref else None


# --- places -----------------------------------------------------------------------------------

_PLACE_SQL = text(
    """
    SELECT name, kind AS level, district, state, lon, lat, NULL::float8[] AS bbox
    FROM public.place WHERE lower(name) = lower(:name)
    ORDER BY CASE kind WHEN 'city' THEN 0 WHEN 'town' THEN 1 ELSE 2 END,
             population DESC NULLS LAST
    LIMIT 1
    """
)
_BOUNDARY_SQL = text(
    """
    SELECT name, level, NULL AS district, state,
           ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat,
           ARRAY[ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)] AS bbox
    FROM public.boundary WHERE lower(name) = lower(:name)
    ORDER BY CASE level WHEN 'district' THEN 0 WHEN 'state' THEN 1 ELSE 2 END
    LIMIT 1
    """
)


def _resolve_place_db(name: str) -> dict[str, Any] | None:
    from blindspot.db import SessionLocal

    with SessionLocal() as session:
        for sql in (_PLACE_SQL, _BOUNDARY_SQL):
            row = session.execute(sql, {"name": name}).mappings().first()
            if row is not None:
                return dict(row) | {"resolved_by": "boundary and place tables"}
    return None


def _resolve_place_geocoder(name: str) -> tuple[dict[str, Any] | None, SourceRef | None]:
    from blindspot.http import fetch, set_fetch_records

    result = fetch(
        "openmeteo_geocoding",
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 5, "language": "en", "format": "json", "countryCode": "IN"},
    )
    hits = (result.data or {}).get("results") or []
    set_fetch_records(result.fetch_id, len(hits))
    lon0, lat0, lon1, lat1 = MET_BBOX
    for hit in hits:  # the first match inside the region we model, if any
        lon, lat = hit.get("longitude"), hit.get("latitude")
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            if lon0 <= lon <= lon1 and lat0 <= lat <= lat1:
                place = {"name": hit.get("name"), "level": hit.get("feature_code"),
                         "district": hit.get("admin2"), "state": hit.get("admin1"),
                         "lon": lon, "lat": lat, "bbox": None,
                         "resolved_by": "Open-Meteo geocoding"}  # fmt: skip
                return place, result.ref()
    return None, result.ref()


async def resolve_place(name: str) -> tuple[dict[str, Any] | None, list[SourceRef], str | None]:
    """A place name -> {name, level, district, state, lon, lat, bbox} from our boundary and place
    tables, falling back to Open-Meteo geocoding (restricted to the modelled region).

    Returns (place or None, sources, error)."""
    name = name.strip()
    if not name:
        return None, [], "empty place name"
    try:
        place = await asyncio.to_thread(_resolve_place_db, name)
    except Exception as exc:
        log.warning("place lookup failed for %r: %s", name, exc)
        place = None
    if place is not None:
        return place, [], None
    try:
        place, ref = await asyncio.to_thread(_resolve_place_geocoder, name)
    except Exception as exc:  # FetchError / SourceUnavailable are already logged by fetch()
        return None, [], f"could not look up {name!r}: {type(exc).__name__}"
    if place is None:
        return None, [ref] if ref else [], f"no place named {name!r} in Punjab, Haryana or Delhi"
    return place, [ref] if ref else [], None


def _check_point(lon: Any, lat: Any) -> str | None:
    lon0, lat0, lon1, lat1 = MET_BBOX
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return "lon and lat must be numbers"
    if not (lon0 <= lon <= lon1 and lat0 <= lat <= lat1):
        return f"({lon}, {lat}) is outside the area BlindSpot models ({lon0}-{lon1}E, {lat0}-{lat1}N)"
    return None


async def _site(args: dict[str, Any]) -> tuple[float, float, str, list[SourceRef], str | None]:
    """lon/lat from the args, or from args['place'] when no coordinates were given."""
    lon, lat = args.get("lon"), args.get("lat")
    if lon is not None and lat is not None:
        problem = _check_point(lon, lat)
        if problem:
            return 0.0, 0.0, "", [], problem
        return float(lon), float(lat), f"{lat:.4f}N {lon:.4f}E", [], None
    place, refs, error = await resolve_place(str(args.get("place") or ""))
    if place is None:
        return 0.0, 0.0, "", refs, error or "give a place name or lon and lat"
    return float(place["lon"]), float(place["lat"]), str(place["name"]), refs, None


# --- the tools --------------------------------------------------------------------------------

ToolFn = Callable[["InternalApi", dict[str, Any]], Awaitable[ToolResult]]


async def _canonical_city(api: InternalApi, city: str) -> tuple[str, ApiReply]:
    """Match a city name to a receptor city of /api/air/latest, case-insensitively."""
    latest = await api.get("/api/air/latest")
    if latest.ok:
        for receptor in latest.body.get("receptors") or []:
            name = str(receptor.get("city") or "")
            if name.lower() == city.strip().lower():
                return name, latest
    return city.strip(), latest


def _pct(value: Any) -> Any:
    """A 0-1 fraction as a percentage with one decimal (other values unchanged)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return rnd(value * 100.0)
    return value


def _with_pct(items: Any, key: str = "share") -> Any:
    """Each dict of a list with its 0-1 `<key>` replaced by `<key>_pct` (the model reads
    percentages and never has to multiply)."""
    if not isinstance(items, list):
        return items
    out = []
    for d in items:
        if isinstance(d, dict) and key in d:
            d = {k: v for k, v in d.items() if k != key} | {f"{key}_pct": _pct(d[key])}
        out.append(d)
    return out


async def get_fire_activity(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """FIRMS detections in a recent window, where and when they were seen, the season curve, and
    the official CREAMS count beside them."""
    hours = int(min(max(args.get("hours") or 24, 1), 168))
    region = str(args.get("region") or "all").lower()
    if region not in ("all", "punjab", "haryana"):
        region = "all"
    since = season_start(utcnow().astimezone(IST).date()).isoformat()
    fires, hours_of_day, daily, official = await asyncio.gather(
        api.get("/api/fires", {"hours": hours, "region": region}),
        api.get("/api/fires/hours", {"days": 7, "region": region}),
        api.get("/api/fires/daily", {"since": since, "region": region}),
        api.get("/api/fires/official", {"since": since, "region": region}),
    )
    if not fires.ok:
        return failed("Fire detections", fires)
    body = fires.body
    props = _features(body)
    by_state = Counter(str(p.get("state")) for p in props if p.get("state"))
    district_n: Counter[tuple[str, str]] = Counter()
    district_frp: defaultdict[tuple[str, str], float] = defaultdict(float)
    for p in props:
        if p.get("district"):
            key = (str(p["district"]), str(p.get("state") or ""))
            district_n[key] += 1
            district_frp[key] += float(p.get("frp_mw") or 0)
    times = sorted(t for t in (parse_time(p.get("acq_utc")) for p in props) if t)
    strongest = sorted(props, key=lambda p: float(p.get("frp_mw") or 0), reverse=True)[:5]
    window = body.get("window") or {}
    data: dict[str, Any] = {
        "window": f"{ist(window.get('from_utc'))} to {ist(window.get('to_utc'))}",
        # as a number too: "in the last 24 hours [1]" must verify against this tool's source
        "window_hours": hours,
        "region": region,
        "detections": body.get("count"),
        "by_satellite": {k: v for k, v in (body.get("by_satellite") or {}).items() if v},
        "satellites_with_none": [k for k, v in (body.get("by_satellite") or {}).items() if not v],
        "by_state": dict(by_state),
        "top_districts": [
            {"district": d, "state": st, "detections": n, "frp_mw_sum": rnd(district_frp[(d, st)])}
            for (d, st), n in district_n.most_common(10)
        ],
        "first_detection": ist(times[0]) if times else None,
        "latest_detection": ist(body.get("latest_acq_utc")) or (ist(times[-1]) if times else None),
        "strongest": [
            pruned({"id": p.get("id"), "district": p.get("district"),
                    "satellite": p.get("satellite"), "time": ist(p.get("acq_utc")),
                    "frp_mw": rnd(p.get("frp_mw")), "confidence": p.get("confidence")})
            for p in strongest
        ],  # fmt: skip
        "clipped_to": body.get("clip"),
        "note": "Detections are 375 m (VIIRS) or 1 km (MODIS) pixels seen at overpass times, not "
        "fields; overlapping satellites can see one fire more than once.",
    }
    if body.get("truncated"):
        data["truncated"] = True
    sources = fires.sources()
    if hours_of_day.ok:
        b = hours_of_day.body
        span = dict(b.get("unobserved_span_ist") or {})
        span.pop("minor_windows", None)
        data["time_of_day_last_7_days"] = {
            "total": b.get("total"),
            "detections_by_ist_hour": {
                str(x.get("hour_ist")): x.get("count") for x in b.get("bins") or [] if x.get("count")
            },
            "observed_windows_ist": _with_pct(b.get("observed_windows_ist")),
            "unobserved_span_ist": {**span, "share_pct": _pct(span.get("share"))} if span else None,
        }
        sources += hours_of_day.sources()
    if daily.ok:
        b = daily.body
        series = [x for x in b.get("series") or [] if isinstance(x, dict)]
        peak = max(series, key=lambda x: x.get("count") or 0, default=None)
        data["season_daily"] = pruned({
            "since": b.get("since") or since,
            "detections_total": (b.get("totals") or {}).get("count"),
            "frp_mw_total": rnd((b.get("totals") or {}).get("frp_sum")),
            "complete_days": b.get("complete_days"),
            "peak_day": {"date": peak.get("date"), "detections": peak.get("count")} if peak else None,
            "last_7_days": [{"date": x.get("date"), "detections": x.get("count"),
                             "complete": x.get("complete")} for x in series[-7:]],
        })  # fmt: skip
        sources += daily.sources()
    if official.ok:  # contract addition (SYNTHESIS build-time update): CREAMS official counts
        b = official.body
        totals = b.get("totals") or {}
        series = [x for x in b.get("series") or [] if isinstance(x, dict)]
        data["official_counts_creams"] = pruned({
            "what": "fire events in the official CREAMS (IARI) rice-residue layers",
            "since": b.get("since"),
            "total": totals.get("count"),
            "by_state": totals.get("by_state"),
            "by_satellite": totals.get("by_satellite"),
            "top_districts": compact(b.get("by_district"), max_list=8),
            "last_7_days": [{"date": x.get("date"), "events": x.get("count"),
                             "published": x.get("published")} for x in series[-7:]],
            "note": (b.get("layer_totals") or {}).get("note"),
        })  # fmt: skip
        sources += official.sources()
    top = data["top_districts"][0] if data["top_districts"] else None
    summary = f"{data['detections']} fire detections in the last {hours} h" + (
        f" ({region.title()})" if region != "all" else ""
    )
    if top:
        summary += f"; most in {top['district']} ({top['detections']})"
    return ToolResult(True, data, _dedupe(sources), summary)


async def get_satellite_passes(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """When fire-detecting satellites looked (and will look) at a place: the blind-spot clock."""
    lon, lat, label, refs, problem = await _site(args)
    if problem:
        return failed("Satellite passes", error=problem)
    hours_back = int(min(max(args.get("hours_back") or 24, 1), 72))
    hours_ahead = int(min(max(args.get("hours_ahead") or 24, 1), 72))
    reply = await api.get(
        "/api/orbits/passes",
        {"lat": round(lat, 5), "lon": round(lon, 5), "hours_back": hours_back,
         "hours_ahead": hours_ahead},
    )  # fmt: skip
    if not reply.ok:
        return failed("Satellite passes", reply)
    now = utcnow()
    passes = [p for p in reply.body.get("passes") or [] if isinstance(p, dict)]

    def trim(p: dict[str, Any]) -> dict[str, Any]:
        return pruned({"satellite": p.get("satellite"), "instrument": p.get("instrument"),
                       "peak": ist(p.get("peak_utc")), "start": ist(p.get("start_utc")),
                       "end": ist(p.get("end_utc")), "day_or_night": p.get("daynight"),
                       "peak_elevation_deg": rnd(p.get("peak_elevation_deg")),
                       "min_ground_distance_km": rnd(p.get("min_ground_distance_km"), 0)})  # fmt: skip

    def moment(m: Any) -> Any:
        if not isinstance(m, dict):
            return m
        return pruned({"satellite": m.get("satellite"), "instrument": m.get("instrument"),
                       "at": ist(m.get("at_utc")), "until": ist(m.get("until_utc"))})  # fmt: skip

    past = [p for p in passes if (parse_time(p.get("peak_utc")) or now) < now]
    future = [p for p in passes if (parse_time(p.get("peak_utc")) or now) >= now]
    current = reply.body.get("current") or {}
    data = pruned({
        "place": label,
        "window_hours": {"back": hours_back, "ahead": hours_ahead},
        "site": compact(reply.body.get("site")),
        "watching_now": [moment(w) for w in current.get("watching") or []],
        "last_look": moment(current.get("last")),
        "next_look": moment(current.get("next")),
        "no_fire_satellite_for_minutes": current.get("blind_for_minutes"),
        "recent_passes": [trim(p) for p in past[-8:]],
        "upcoming_passes": [trim(p) for p in future[:8]],
        "satellites": [pruned({"satellite": x.get("satellite"), "instrument": x.get("instrument"),
                               "latest_fire_data": ist(x.get("last_fire_data_utc")),
                               "orbit_data_age_hours": rnd(x.get("tle_age_hours"))})
                       for x in reply.body.get("satellites") or []],
    })  # fmt: skip
    nxt = current.get("next") or {}
    summary = f"{len(past)} passes over {label} in the last {hours_back} h"
    if nxt.get("satellite"):
        summary += f"; next {nxt['satellite']} at {ist(nxt.get('at_utc'))}"
    return ToolResult(True, data, _dedupe(refs + reply.sources()), summary)


def unseen_audit_block(summary: Any) -> dict[str, Any] | None:
    """The burn-scar audit of candidate unseen burns from a /api/scars/summary body, as served (no
    rounding): the share an audit found to be real burns, its 95% interval and how many were
    audited. Any candidate unseen share or area must come with it (verifier.check_honesty). None if
    the body carries no audit."""
    method = summary.get("method") if isinstance(summary, dict) else None
    audit = (method.get("audit") if isinstance(method, dict) else None) or {}
    unseen = audit.get("candidate_unseen") or {}
    if unseen.get("precision") is None or not unseen.get("precision_ci95"):
        return None
    return {
        "precision": unseen["precision"], "precision_ci95": unseen["precision_ci95"],
        # the same numbers as percentages, for the answer's wording (the site writes 87%, 68%-96%)
        "precision_pct": round(unseen["precision"] * 100, 1),
        "precision_ci95_pct": [round(v * 100, 1) for v in unseen["precision_ci95"]],
        "audited": unseen.get("audited"), "method": audit.get("method"),
        "reading": "precision = share of audited candidate unseen burns that were real burns; "
                   "so the candidate unseen share is an upper bound",
    }


def _share_pct(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """A copy with `unseen_share` (a 0-1 fraction in the scars API) also as a percentage."""
    if not isinstance(block, dict):
        return block
    out = {k: v for k, v in block.items() if k != "unseen_share"}
    share = block.get("unseen_share")
    if isinstance(share, (int, float)) and 0 <= share <= 1:
        out["unseen_share_pct"] = rnd(share * 100.0)
    return out


async def get_burn_scars(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Sentinel-2 burn scars: the season summary by district and the most recent clusters."""
    limit = int(min(max(args.get("limit") or 8, 1), 20))
    since = args.get("since")  # default: the scars API's own season start
    unseen = True if args.get("unseen_only") else None
    summary_reply, scars = await asyncio.gather(
        api.get("/api/scars/summary", {"since": since}),
        api.get("/api/scars", {"since": since, "until": args.get("until"),
                               "district": args.get("district"), "unseen": unseen,
                               "limit": limit}),  # fmt: skip
    )
    if not summary_reply.ok and not scars.ok:
        return failed("Burn scars", summary_reply, scars)
    data: dict[str, Any] = {}
    sources: list[SourceRef] = []
    if summary_reply.ok:
        body = summary_reply.body
        data["season"] = compact(_share_pct(body.get("season")))
        by_district = sorted(body.get("by_district") or [],
                             key=lambda d: d.get("area_ha") or 0, reverse=True)  # fmt: skip
        data["by_district"] = [compact(_share_pct(d)) for d in by_district[:10]]
        if body.get("coverage"):
            data["coverage"] = compact(body.get("coverage"), max_list=5)
        method = body.get("method") or {}
        data["method"] = {"summary": method.get("summary"), "unseen_rule": method.get("unseen_rule"),
                          "status": "candidates: tillage can look like a burn"}  # fmt: skip
        # Given with the share, so the first draft can qualify it (the answer check requires it).
        audit = unseen_audit_block(body)
        if audit is not None:
            data["candidate_unseen_audit"] = audit
        sources += summary_reply.sources()
    if scars.ok:
        body = scars.body
        props = _features(body)
        data["filter"] = pruned({"district": args.get("district"), "unseen_only": unseen,
                                 "since": since, "until": args.get("until")})  # fmt: skip
        data["matching"] = compact(body.get("totals"))
        data["passes_window"] = compact(body.get("window"))
        data["clusters"] = [
            pruned({"id": p.get("id"), "village": p.get("village"),
                    "subdistrict": p.get("subdistrict"), "district": p.get("district"),
                    "area_ha": rnd(p.get("area_ha")),
                    "burnt_between": [ist(p.get("burn_after")), ist(p.get("burn_before"))],
                    "candidate_unseen": p.get("unseen"), "fire_matches": p.get("fire_matches"),
                    "nearest_fire_m": rnd(p.get("nearest_fire_m"), 0),
                    "confidence": rnd(p.get("confidence"), 2), "hazy": p.get("hazy"),
                    "centroid_lon_lat": _centroid(p)})
            for p in props[:limit]
        ]  # fmt: skip
        sources += scars.sources()
    season = data.get("season") or {}
    summary = "Burn scars"
    if season:
        summary += (f": {season.get('clusters')} clusters, {season.get('area_ha')} ha since "
                    f"{season.get('since')}, candidate unseen {season.get('unseen_share_pct')}%")  # fmt: skip
        audit = data.get("candidate_unseen_audit")
        if audit:  # the step line shows the share, so it carries its audit too
            lo, hi = audit["precision_ci95_pct"]
            summary += f" (audited precision {audit['precision_pct']:g}%, 95% CI {lo:g}-{hi:g}%)"
    elif "matching" in data:
        summary += f": {data['matching']}"
    return ToolResult(True, data, _dedupe(sources), summary)


async def get_case_file(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Everything about one burn scar: dates, imagery, nearby fire detections, weather."""
    scar_id = str(args.get("scar_id") or "").strip()
    if not scar_id.isdigit():
        return failed("Case file", error="scar_id must be a number (ids come from get_burn_scars)")
    reply = await api.get(f"/api/scars/{scar_id}")
    if not reply.ok:
        return failed(f"Case file {scar_id}", reply)
    body = reply.body
    case = {k: v for k, v in (body.get("case") or {}).items() if k != "geometry"}
    imagery = {}
    for when in ("pre", "post"):
        frame = (body.get("imagery") or {}).get(when) or {}
        imagery[when] = pruned({"date": frame.get("date"), "item_id": frame.get("item_id"),
                                "cloud_pct": rnd(frame.get("cloud_pct"))})
    fires = [f for f in body.get("fires_nearby") or [] if isinstance(f, dict)]
    explaining = [f for f in fires if f.get("explains_scar")]
    weather = dict(body.get("weather_at_burn") or {})
    weather.pop("series", None)
    weather.pop("fetch_id", None)
    data = {
        "case": compact(case),
        "imagery": imagery,
        "fires_nearby": {
            "within_5_km_and_days_around": len(fires),
            "explaining_the_scar": len(explaining),
            "nearest": [pruned({"satellite": f.get("satellite"), "time": ist(f.get("acq_utc")),
                                "distance_m": rnd(f.get("distance_m"), 0),
                                "match_radius_m": rnd(f.get("match_radius_m"), 0),
                                "in_burn_window": f.get("in_window"),
                                "explains_scar": f.get("explains_scar"),
                                "frp_mw": rnd(f.get("frp_mw"))}) for f in fires[:5]],
        },  # fmt: skip
        "weather_at_burn": compact(weather),
        "unseen_rule": (body.get("method") or {}).get("unseen_rule"),
    }
    where = case.get("village") or case.get("district") or f"scar {scar_id}"
    summary = (f"Case {scar_id} ({where}): {case.get('area_ha')} ha, "
               f"{len(explaining)} of {len(fires)} nearby detections explain it")  # fmt: skip
    return ToolResult(True, data, reply.sources(), summary)


async def get_air_quality(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Ground PM2.5 in a city now, over the last hours, and in the other monitored cities."""
    hours = int(min(max(args.get("hours") or 48, 6), 168))
    city, latest = await _canonical_city(api, str(args.get("city") or "Delhi"))
    series = await api.get("/api/air/series", {"city": city, "hours": hours})
    if not latest.ok and not series.ok:
        return failed(f"Air quality in {city}", latest, series)
    data: dict[str, Any] = {"city": city, "unit": "µg/m³"}
    sources: list[SourceRef] = []
    if latest.ok:
        receptors = latest.body.get("receptors") or []
        mine = next((r for r in receptors if str(r.get("city")).lower() == city.lower()), None)
        if mine:
            mean = mine.get("pm25_24h_mean")
            data["now"] = pruned({
                "pm25": rnd(mine.get("pm25_now")), "pm10": rnd(mine.get("pm10_now")),
                "observed": ist(mine.get("observed_at")),
                "stations_reporting": mine.get("n_stations"),
                "stations_stale": mine.get("n_stale"), "provider": mine.get("provider"),
                "method": "median of the city's stations that reported in the last 2 hours",
            })  # fmt: skip
            if mine.get("pm25_now") is None:
                data["now"]["note"] = "no station reported PM2.5 in µg/m³ in the last 2 hours"
            if mean is not None:
                data["pm25_24h_mean"] = rnd(mean)
                data["pm25_24h_mean_hours"] = mine.get("pm25_24h_hours")
                data["pm25_24h_naqi_category"] = naqi_band(mean)
            aqi = mine.get("aqi") or {}
            if aqi.get("value") is not None:
                data["cpcb_aqi_now"] = {"aqi": aqi.get("value"), "category": aqi.get("category"),
                                        "stations": aqi.get("n_stations"),
                                        "observed": ist(aqi.get("observed_at"))}  # fmt: skip
            model = mine.get("model") or {}
            if model.get("pm25") is not None:
                data["cams_model_now"] = {"pm25": rnd(model.get("pm25")),
                                          "valid": ist(model.get("valid_at")),
                                          "note": "model estimate, not a measurement"}  # fmt: skip
        else:
            data["now"] = None
            data["note"] = f"no current receptor reading for {city}"
        stations = [s for s in latest.body.get("stations") or []
                    if str(s.get("city") or "").lower() == city.lower()
                    and not s.get("is_model") and s.get("pm25") is not None]  # fmt: skip
        stations.sort(key=lambda s: s.get("pm25") or -1, reverse=True)
        data["stations_in_city"] = {
            "count": len(stations),
            "highest_pm25": [{"name": s.get("name"), "pm25": rnd(s.get("pm25")),
                              "pm10": rnd(s.get("pm10")), "observed": ist(s.get("observed_at")),
                              "stale": bool(s.get("stale"))} for s in stations[:5]],
        }  # fmt: skip
        data["other_cities_now"] = [
            pruned({"city": r.get("city"), "pm25": rnd(r.get("pm25_now")),
                    "pm25_24h_mean": rnd(r.get("pm25_24h_mean")),
                    "observed": ist(r.get("observed_at")),
                    "cpcb_aqi": (r.get("aqi") or {}).get("value"),
                    "cpcb_category": (r.get("aqi") or {}).get("category")})
            for r in receptors if str(r.get("city")).lower() != city.lower()
        ][:12]  # fmt: skip
        sources += latest.sources()
    if series.ok:
        body = series.body
        points = [p for p in body.get("series") or [] if p.get("pm25") is not None]
        window: dict[str, Any] = {"hours_with_data": len(points), "as_of": ist(body.get("as_of"))}
        if points:
            hi = max(points, key=lambda p: p["pm25"])
            lo = min(points, key=lambda p: p["pm25"])
            window |= {
                "max": {"pm25": rnd(hi["pm25"]), "hour_ending": ist(hi.get("t"))},
                "min": {"pm25": rnd(lo["pm25"]), "hour_ending": ist(lo.get("t"))},
                "hourly_last_24": [[ist(p.get("t")), rnd(p["pm25"])] for p in points[-24:]],
            }
        elif body.get("reason"):
            window["reason"] = body["reason"]
        data[f"last_{hours}_hours"] = window
        model = [p for p in body.get("model_series") or [] if p.get("pm25") is not None]
        if model:
            peak = max(model, key=lambda p: p["pm25"])
            data["cams_model_series"] = {
                "note": "Copernicus CAMS model estimate, not a measurement",
                "latest": {"pm25": rnd(model[-1]["pm25"]), "time": ist(model[-1].get("t"))},
                "max": {"pm25": rnd(peak["pm25"]), "time": ist(peak.get("t"))},
            }
        sources += series.sources()
    data["reference_24h_limits"] = {
        "india_naaqs_pm25": NAAQS_PM25_24H_UGM3, "india_naaqs_pm10": NAAQS_PM10_24H_UGM3,
        "who_2021_pm25": WHO_PM25_24H_UGM3, "who_2021_pm10": WHO_PM10_24H_UGM3,
    }  # fmt: skip
    sources += [NAAQS_REF, WHO_REF]
    now = data.get("now") or {}
    summary = f"{city} PM2.5 " + (
        f"{now.get('pm25')} µg/m³ at {now.get('observed')}" if now.get("pm25") is not None
        else "no current reading"
    )  # fmt: skip
    if data.get("pm25_24h_mean") is not None:
        summary += f"; 24 h mean {data['pm25_24h_mean']}"
    if data.get("cpcb_aqi_now"):
        summary += f"; CPCB AQI {data['cpcb_aqi_now']['aqi']} ({data['cpcb_aqi_now']['category']})"
    return ToolResult(True, data, _dedupe(sources), summary)


def _percentages(summary: dict[str, Any]) -> dict[str, Any]:
    """The back-run summary for the model: shares (0-1) also as percentages, so it never
    multiplies; the uncertainty cone and the member centerlines (geometry) dropped."""
    out = {k: v for k, v in summary.items()
           if k not in ("cone", "members", "share_over_burn_belt")}  # fmt: skip
    shares = summary.get("share_over_burn_belt")
    if isinstance(shares, dict):
        out["share_over_burn_belt_pct"] = {k: _pct(v) for k, v in shares.items()}
    for key in ("top_districts", "top_source_districts"):
        out[key] = _with_pct(summary.get(key))
    members = [m.get("model") for m in summary.get("members") or [] if isinstance(m, dict)]
    if members:
        out["models"] = members
    return out


async def trace_air_back(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Where a city's air came from: our back-trajectories over the burn belt in the last 24-72 h
    and, for Delhi, IITM's official stubble share of PM2.5 (its VIIRS-driven estimate)."""
    hours = int(min(max(args.get("hours") or 72, 12), 120))
    city, _ = await _canonical_city(api, str(args.get("city") or "Delhi"))
    is_delhi = "delhi" in city.lower()
    back = api.get("/api/smoke/back", {"city": city, "hours": hours})
    if is_delhi:
        back_reply, dss = await asyncio.gather(back, api.get("/api/air/stubble-share"))
    else:
        back_reply, dss = await back, ApiReply("/api/air/stubble-share", 0, error="Delhi only")
    dss_ok = is_delhi and dss.ok and bool(dss.body.get("run_date"))
    if not back_reply.ok and not dss_ok:
        return failed(f"Back-trajectories for {city}", back_reply)
    data: dict[str, Any] = {"city": city}
    sources: list[SourceRef] = []
    summary = f"{city}:"
    if back_reply.ok:
        body = back_reply.body
        block = _percentages(body.get("summary") or {})
        data["back_trajectories"] = {
            "model_run": ist(body.get("run_at")),
            "start": ist(body.get("t0")),
            "hours_back": hours,
            "trajectories": len(body.get("trails") or []),
            "summary": compact(block, max_list=6),
            "verdict_hint": body.get("verdict_hint"),
            "method": "particles carried backwards by Open-Meteo winds; a share is the fraction "
            "of particles that passed over the Punjab/Haryana burn belt",
        }
        if body.get("reason"):
            data["back_trajectories"]["reason"] = body["reason"]
        shares = block.get("share_over_burn_belt_pct") or block.get("share_over_burn_belt")
        summary += f" trajectories verdict {body.get('verdict_hint')}"
        if isinstance(shares, dict):
            summary += f", burn-belt share {shares}"
        sources += back_reply.sources()
    else:
        data["back_trajectories"] = {"error": back_reply.error}
    if dss_ok:
        body = dss.body
        data["iitm_dss_official_estimate"] = {
            "what": body.get("unit") or "% of Delhi's daily mean PM2.5 from stubble burning",
            "run_date": body.get("run_date"),
            "stale": body.get("stale"),
            "today": compact(body.get("today")),
            "recent_days": compact((body.get("daily") or [])[-5:]),
            "method_note": str(body.get("method_note") or "")[:300] or None,
        }  # fmt: skip
        # IITM's next days run on climatological fire input: a forecast, never the estimate (a
        # verified answer once quoted the outlook's 19.2% as "IITM estimated" beside today's 1.7%).
        outlook = [{"date": o.get("date"), "forecast_stubble_pct": rnd(o.get("stubble_pct"), 1),
                    "fire_input": o.get("fire_input")} for o in body.get("outlook") or []][:4]  # fmt: skip
        if outlook:
            data["iitm_dss_forecast_not_an_estimate"] = {
                "what": "IITM's forecast for the coming days, driven by climatological fire input; "
                        "quote it only as a forecast, never as IITM's estimate for today",
                "days": outlook,
            }
        today = rnd((body.get("today") or {}).get("stubble_pct"), 1)
        summary += f" IITM DSS stubble share {today}% ({body.get('run_date')})"
        sources += dss.sources()
    return ToolResult(True, data, _dedupe(sources), summary.strip())


async def forecast_smoke(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Where recent fires' smoke is heading: the forward particle run and arrival at cities."""
    reply = await api.get("/api/smoke/forward")
    if not reply.ok:
        return failed("Smoke forecast", reply)
    body = reply.body
    receptors = []
    for r in body.get("receptors") or []:
        peak = r.get("peak") or {}
        at_peak = next((x for x in r.get("series") or [] if x.get("t") == peak.get("t")), {})
        states = sorted(r.get("by_state") or [], key=lambda x: x.get("share") or 0, reverse=True)
        receptors.append(pruned({
            "city": r.get("city"), "peak_smoke_index": rnd(peak.get("value")),
            "peak_range_across_models": [rnd(v) for v in at_peak.get("range") or []],
            "peak_time": ist(peak.get("t")), "arrival": ist(r.get("arrival_t")),
            "from_state_pct": {x.get("state"): _pct(x.get("share")) for x in states[:3]},
        }))  # fmt: skip
    receptors.sort(key=lambda r: r.get("peak_smoke_index") or 0, reverse=True)
    method = body.get("method") or {}
    inputs = dict(body.get("inputs") or {})
    window = inputs.pop("release_window", None)
    data = pruned({
        "model_run": ist(body.get("run_at")),
        "released_from": ist(body.get("t0")),
        "until": ist(body.get("t_end")),
        "hours": body.get("hours"),
        "inputs": compact(inputs),
        "release_window": [ist(t) for t in window or []],
        "trajectories": len(body.get("trails") or []),
        "models": [m.get("model") for m in body.get("members") or [] if isinstance(m, dict)],
        "cities": receptors[:10],
        "smoke_index_unit": method.get("smoke_index_unit"),
        "limits": method.get("limits"),
        "reason": body.get("reason"),
    })  # fmt: skip
    top = receptors[0] if receptors else None
    summary = "Smoke forecast" + (
        f": highest at {top['city']} (index {top.get('peak_smoke_index')}, {top.get('peak_time')})"
        if top else ": no city reached"
    )  # fmt: skip
    return ToolResult(True, data, reply.sources(), summary)


def _wind(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    return pruned({"speed_kmh": rnd(block.get("speed_kmh")), "from_deg": rnd(block.get("from_deg"), 0),
                   "from": compass(block.get("from_deg"))})  # fmt: skip


async def get_weather(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Winds at the surface, at 925 hPa and through the mixing layer, mixing height, inversion
    and rain: can smoke travel, and is it trapped near the ground?"""
    city, _ = await _canonical_city(api, str(args.get("city") or "Delhi"))
    reply = await api.get("/api/met/now", {"city": city})
    if not reply.ok:
        return failed(f"Weather in {city}", reply)
    body = reply.body
    inversion = body.get("inversion") if isinstance(body.get("inversion"), dict) else None
    second = {}
    for name, block in (body.get("ensemble") or {}).items():
        if isinstance(block, dict):
            second[name] = pruned({"blh_m": rnd(block.get("blh_m"), 0),
                                   "transport_wind": _wind(block.get("transport_wind")),
                                   "wind_10m": _wind(block.get("wind_10m"))})  # fmt: skip
    data = pruned({
        "city": body.get("city") or city,
        "time": ist(body.get("t")),
        "model": body.get("model"),
        "wind_10m": _wind(body.get("wind_10m")),
        "wind_925hpa": _wind(body.get("wind_925")),
        "transport_wind": _wind(body.get("transport_wind")),
        "mixing_height_m": rnd(body.get("blh_m"), 0),
        "ventilation_m2_s": rnd(body.get("ventilation_m2_s"), 0),
        "temp_2m_c": rnd(body.get("temp_2m_c")),
        "rain_mm_last_24h": rnd(body.get("rain_mm_24h")),
        "inversion": pruned({"present": inversion.get("present"),
                             "t925_minus_t2m_c": rnd(inversion.get("t925_minus_t2m_c")),
                             "basis": inversion.get("basis")}) if inversion else None,
        "second_model": second or None,
        "meaning": "wind 'from' is the direction the wind blows from; transport wind is the mean "
        "through the mixing layer, which carries smoke; a low mixing height traps it",
    })  # fmt: skip
    wind = data.get("transport_wind") or data.get("wind_925hpa") or {}
    summary = (f"{data['city']} at {data.get('time')}: transport wind from {wind.get('from')} "
               f"{wind.get('speed_kmh')} km/h, mixing height {data.get('mixing_height_m')} m, "
               f"rain {data.get('rain_mm_last_24h')} mm in 24 h")  # fmt: skip
    return ToolResult(True, data, reply.sources(), summary)


async def get_burn_risk(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Harvested-but-unburnt fields ranked by burning risk, by village."""
    limit = int(min(max(args.get("limit") or 8, 1), 20))
    reply = await api.get("/api/risk", {"district": args.get("district"), "limit": limit})
    if not reply.ok:
        return failed("Burn risk", reply)
    body = reply.body
    clusters = sorted(_features(body), key=lambda p: p.get("risk_score") or 0, reverse=True)
    method = body.get("method") or {}
    totals = body.get("totals") or {}
    mapped, scored = totals.get("clusters") or 0, totals.get("scored") or 0
    if not mapped:
        status = "no harvested-but-unburnt fields are mapped yet"
    elif not scored:
        status = (f"{mapped} harvested-but-unburnt fields ({rnd(totals.get('area_ha'))} ha) are "
                  "mapped; none has a risk score yet")  # fmt: skip
    else:
        status = f"{scored} of {mapped} harvested-but-unburnt fields have a risk score"
    data = pruned({
        "as_of": ist(body.get("as_of")) or body.get("as_of"),
        "status": status,
        "district_filter": args.get("district"),
        "reason": body.get("reason"),
        "totals": compact(body.get("totals")),
        "village_ranking": compact(body.get("ranking"), max_list=limit),
        "ranking_order": body.get("ranking_order"),
        "top_fields": [
            pruned({"id": p.get("id"), "village": p.get("village"),
                    "village_inferred": p.get("village_inferred"), "district": p.get("district"),
                    "area_ha": rnd(p.get("area_ha")), "risk_score": rnd(p.get("risk_score")),
                    "days_since_harvest": rnd(p.get("days_since_harvest")),
                    "harvested_between": [ist(p.get("harvested_after")),
                                          ist(p.get("harvested_before"))],
                    "risk_factors": compact(p.get("risk_factors"), max_list=4),
                    "centroid_lon_lat": _centroid(p)})
            for p in clusters[:limit]
        ],
        "method": pruned({"formula": method.get("formula"), "note": method.get("note")}),
        "backtest": compact(body.get("backtest")),
    })  # fmt: skip
    ranking = body.get("ranking") or []
    totals = body.get("totals") or {}
    summary = f"Burn risk: {totals.get('clusters', 0)} harvested-unburnt fields"
    if ranking:
        summary += f"; top village {ranking[0].get('village')} ({ranking[0].get('district')})"
    elif body.get("reason"):
        summary += f" ({body['reason']})"
    return ToolResult(True, data, reply.sources(), summary)


FIELD_BRIEF_ROWS = 10  # villages per list the model sees (the brief's page and CSV list them all)
FIELD_BRIEF_OUTSIDE_ROWS = 3  # best-ranked villages outside the top tenth: enough to say where next
FIELD_BRIEF_WATCHLIST_ORDER = (
    "watchlist_top_tenth: villages with harvested-but-unburnt fields in the risk run's top tenth, the "
    "most such fields first, then the best rank (rank 1 = the highest of risk_run.fields_ranked). "
    "best_ranked_outside_top_tenth: the district's best-ranked other villages; none of their fields "
    "is in the top tenth."
)


async def get_field_brief(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Where field teams could go in one district: GET /api/brief/{district}, trimmed for the model.

    The keys the answer check reads are kept as the brief serves them: every candidate unseen area
    stays `unseen_area_ha` (so a sentence quoting one must call it a candidate and give the audit),
    the audit is `candidate_unseen_audit` as get_burn_scars gives it, and the backtest stays under
    `all_pairs` beside its `strength`. The brief has no risk scores at all, only ranks and counts.

    The brief's watchlist lists every village with harvested fields, those with fields in the run's
    top tenth first. The model gets those under a name that says so, and the best-ranked of the rest
    under a name that says they are not: most districts have few villages in the top tenth, some
    none, and no number check would catch "these villages rank in the top tenth" if it were false."""
    district = str(args.get("district") or "").strip()
    if not district:
        return failed("Field brief", error="give a district name, e.g. 'Sangrur' (get_burn_scars lists districts)")
    days = int(min(max(args.get("days") or 7, 1), 30))
    reply = await api.get(f"/api/brief/{quote(district, safe='')}", {"days": days, "limit": FIELD_BRIEF_ROWS})
    if not reply.ok:
        return failed(f"Field brief for {district}", reply)
    body = reply.body
    window = body.get("window") or {}
    totals = body.get("totals") or {}
    unnamed = "unnamed (no census village close enough)"

    def place(row: dict[str, Any]) -> dict[str, Any]:
        point = [row.get("lon"), row.get("lat")]
        return {"village": row.get("village") or unnamed, "subdistrict": row.get("subdistrict"),
                "centroid_lon_lat": _coords(point) if None not in point else None}

    villages = [pruned({**place(v), "unseen_clusters": v.get("unseen_clusters"),
                        "unseen_area_ha": rnd(v.get("unseen_area_ha")), "newest_pass": v.get("newest_pass"),
                        "case_ids": (v.get("case_ids") or [])[:3]})
                for v in body.get("villages") or []]  # fmt: skip

    def watched(row: dict[str, Any], top: bool) -> dict[str, Any]:
        return pruned({**place(row), "harvested_fields": row.get("fields"), "area_ha": rnd(row.get("area_ha")),
                       "fields_in_top_tenth": row.get("top_tenth_fields") if top else None,
                       "best_rank": row.get("best_rank"), "field_ids": (row.get("field_ids") or [])[:3]})

    rows = body.get("watchlist") or []
    # in_top_tenth missing (it never is) counts as outside: never call a village top tenth unasked
    in_top = [watched(w, True) for w in rows if w.get("in_top_tenth") is True]
    outside = [watched(w, False) for w in rows if w.get("in_top_tenth") is not True][:FIELD_BRIEF_OUTSIDE_ROWS]
    top_total = body.get("watchlist_in_top_tenth") or len(in_top)
    run = body.get("risk_run") or {}
    reliability = body.get("ranking_reliability") or {}
    audit = body.get("candidate_unseen_audit") or {}
    audit_block = unseen_audit_block({"method": {"audit": {"candidate_unseen": audit, "method": audit.get("method")}}})
    data = pruned({
        "district": body.get("district"),
        "state": body.get("state"),
        "window": pruned({"first_pass_day": window.get("from"), "last_pass_day": window.get("to"),
                          "days": window.get("days"), "newest_pass": ist(window.get("newest_pass")),
                          "anchor": window.get("anchor")}),
        "label": body.get("label"),
        "totals": pruned({**totals, "unseen_area_ha": rnd(totals.get("unseen_area_ha")),
                          "harvested_area_ha": rnd(totals.get("harvested_area_ha"))}),
        "villages_with_candidate_unseen_burns": villages,
        "more_villages": max(0, (body.get("villages_total") or 0) - len(villages)) or None,
        "villages_order": body.get("villages_order"),
        "watchlist_top_tenth": in_top,
        "more_watchlist_top_tenth_villages": max(0, top_total - len(in_top)) or None,
        "watchlist_note": (f"No harvested field in {body.get('district') or district} ranks in the risk run's "
                           "top tenth." if rows and not in_top else None),
        "best_ranked_outside_top_tenth": outside,
        "villages_outside_top_tenth": max(0, (body.get("watchlist_total") or 0) - top_total) if rows else None,
        "watchlist_order": FIELD_BRIEF_WATCHLIST_ORDER if rows else None,
        "candidate_unseen_audit": audit_block,
        "risk_run": pruned({"model": run.get("model"), "as_of": ist(run.get("as_of")),
                            "fields_ranked": run.get("fields_ranked"), "top_tenth": run.get("top_tenth")}),
        "ranking_reliability": pruned({
            "strength": reliability.get("strength"),
            "all_pairs": pruned(reliability.get("all_pairs") or {}),
            "reading": reliability.get("reading"),
        }),
        "notes": body.get("notes"),
        "reason": body.get("reason"),
    })  # fmt: skip
    summary = (f"Field brief {data.get('district', district)} ({window.get('from')} to {window.get('to')}): "
               f"{totals.get('unseen_clusters', 0)} candidate unseen burns, {rnd(totals.get('unseen_area_ha'))} ha in "
               f"{totals.get('villages', 0)} villages")  # fmt: skip
    if audit_block:  # the step line shows the area, so it carries its audit too
        lo, hi = audit_block["precision_ci95_pct"]
        summary += f" (audited precision {audit_block['precision_pct']:g}%, 95% CI {lo:g}-{hi:g}%)"
    summary += f"; {totals.get('top_tenth_villages', 0)} villages with fields in the risk run's top tenth"
    return ToolResult(True, data, reply.sources(), summary)


MAP_LAYERS = ("fires", "scars", "unseen_scars", "risk", "smoke_forward", "smoke_back",
              "air_stations", "wind", "satellite_passes", "imagery")  # fmt: skip
HIGHLIGHT_TYPES = ("scar", "risk_cluster", "fire", "station", "district", "city", "village")


async def show_on_map(api: InternalApi, args: dict[str, Any]) -> ToolResult:
    """Ask the map to fly somewhere, switch a layer on, or highlight a feature. Fetches no data:
    its only effect is the `map` SSE event, with a payload the web map applies as is."""
    action = args.get("action")
    label = str(args.get("label") or args.get("place") or "")[:80] or None
    if action == "show_layer":
        layer = args.get("layer")
        if layer not in MAP_LAYERS:
            return failed("Map", error=f"layer must be one of {', '.join(MAP_LAYERS)}")
        payload: dict[str, Any] = {"layer": layer, "visible": True}
        if args.get("city"):
            payload["params"] = {"city": args["city"]}
        if label:
            payload["label"] = label
        event = {"action": "show_layer", "payload": payload}
        return ToolResult(True, {"shown": f"layer {layer}"}, [], f"Map: show {layer}",
                          map_event=event)  # fmt: skip
    if action in ("fly_to", "highlight"):
        payload = {}
        refs: list[SourceRef] = []
        if args.get("lon") is not None and args.get("lat") is not None:
            problem = _check_point(args.get("lon"), args.get("lat"))
            if problem:
                return failed("Map", error=problem)
            payload["center"] = [round(float(args["lon"]), 5), round(float(args["lat"]), 5)]
        elif args.get("place"):
            place, refs, error = await resolve_place(str(args["place"]))
            if place is None:
                return failed("Map", error=error)
            payload["center"] = [round(float(place["lon"]), 5), round(float(place["lat"]), 5)]
            if place.get("bbox"):
                payload["bbox"] = [round(float(v), 4) for v in place["bbox"]]
            label = label or str(place["name"])
        if action == "fly_to":
            if "center" not in payload:
                return failed("Map", error="fly_to needs a place name or lon and lat")
            zoom = args.get("zoom")
            payload["zoom"] = float(min(max(zoom, 4), 16)) if isinstance(zoom, (int, float)) else 9.0
        else:
            feature_type = args.get("feature_type")
            if feature_type not in HIGHLIGHT_TYPES:
                return failed("Map", error=f"feature_type must be one of {', '.join(HIGHLIGHT_TYPES)}")
            payload["feature_type"] = feature_type
            if args.get("feature_id") not in (None, ""):
                payload["id"] = args["feature_id"]
            elif not label and "center" not in payload:
                return failed("Map", error="highlight needs feature_id, a place or lon/lat")
        if label:
            payload["label"] = label
        event = {"action": action, "payload": payload}
        where = label or payload.get("id") or payload.get("center")
        return ToolResult(True, {"shown": f"{action} {where}"}, refs, f"Map: {action} {where}",
                          map_event=event)  # fmt: skip
    return failed("Map", error="action must be fly_to, show_layer or highlight")


# --- declarations (what Gemini is told about each tool) -----------------------------------------

_CITY = {"type": "string", "description": "City name, e.g. 'Delhi', 'Ludhiana', 'Chandigarh'."}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn

    def declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.parameters
        )


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "get_fire_activity",
            "NASA FIRMS satellite fire detections over Punjab and Haryana in the last N hours: "
            "count, satellites, top districts, strongest fires, detections by IST hour over 7 "
            "days with the hours no satellite observes, the season's daily curve since 15 "
            "September, and official CREAMS counts when available.",
            {"type": "object", "properties": {
                "hours": {"type": "integer", "description": "Window in hours, 1-168. Default 24."},
                "region": {"type": "string", "enum": ["all", "punjab", "haryana"]}}},
            get_fire_activity,
        ),
        Tool(
            "get_satellite_passes",
            "When fire-detecting satellites (VIIRS, MODIS) and Sentinel-2 passed over, or will "
            "pass over, a place: recent and upcoming passes in IST, which satellite is watching "
            "now and the next one. Use it to explain why a fire could have been missed.",
            {"type": "object", "properties": {
                "place": {"type": "string", "description": "Village, town, district or city."},
                "lon": {"type": "number"}, "lat": {"type": "number"},
                "hours_back": {"type": "integer", "description": "1-72, default 24."},
                "hours_ahead": {"type": "integer", "description": "1-72, default 24."}}},
            get_satellite_passes,
        ),
        Tool(
            "get_burn_scars",
            "Sentinel-2 burn scars (burnt fields) this season: totals and burnt area by district, "
            "the share with no satellite fire detection (candidate unseen burns), and the largest "
            "clusters with village, dates and ids for get_case_file.",
            {"type": "object", "properties": {
                "since": {"type": "string", "description": "YYYY-MM-DD; default season start."},
                "until": {"type": "string", "description": "YYYY-MM-DD."},
                "district": {"type": "string"},
                "unseen_only": {"type": "boolean",
                                "description": "Only candidate unseen burns."},
                "limit": {"type": "integer", "description": "Clusters to list, 1-20."}}},
            get_burn_scars,
        ),
        Tool(
            "get_case_file",
            "Full evidence for one burn scar: area, village, burn dates, the Sentinel-2 images "
            "before and after, fire detections nearby and the weather when it burnt.",
            {"type": "object", "required": ["scar_id"], "properties": {
                "scar_id": {"type": "string", "description": "id from get_burn_scars."}}},
            get_case_file,
        ),
        Tool(
            "get_air_quality",
            "Measured ground PM2.5 (CPCB stations) in a city: now, the 24 h mean with its CPCB "
            "category, the max and min of the last hours, hourly values, the CAMS model estimate, "
            "other cities now, and the Indian and WHO 24 h limits.",
            {"type": "object", "properties": {
                "city": _CITY,
                "hours": {"type": "integer", "description": "Look-back hours, 6-168. Default 48."}}},
            get_air_quality,
        ),
        Tool(
            "trace_air_back",
            "Back-trajectories from a city: the share of its air that passed over the "
            "Punjab/Haryana burn belt in the last 24/48/72 h, the districts it crossed, current "
            "mixing height and wind, and a verdict hint on whether stubble smoke is likely.",
            {"type": "object", "properties": {
                "city": _CITY,
                "hours": {"type": "integer", "description": "12-120, default 72."}}},
            trace_air_back,
        ),
        Tool(
            "forecast_smoke",
            "Forward smoke forecast from recent fires and fresh burn scars: which cities the smoke "
            "reaches, when it arrives and when it peaks (relative smoke index).",
            {"type": "object", "properties": {}},
            forecast_smoke,
        ),
        Tool(
            "get_weather",
            "Current weather that controls smoke in a city: wind at 10 m and 925 hPa (speed and "
            "direction it blows from), mixing height, temperature inversion, rain in 24 h.",
            {"type": "object", "properties": {"city": _CITY}},
            get_weather,
        ),
        Tool(
            "get_burn_risk",
            "Harvested fields not yet burnt, ranked by burning risk: village ranking, top "
            "clusters with risk score, days since harvest and the factors behind the score.",
            {"type": "object", "properties": {
                "district": {"type": "string"},
                "limit": {"type": "integer", "description": "1-20, default 8."}}},
            get_burn_risk,
        ),
        Tool(
            "get_field_brief",
            "Where field teams (PPCB, HSPCB) could look first in one district. Lists the villages with "
            "candidate unseen burns in the latest days of Sentinel-2 data (count, hectares, newest pass, "
            "a point, case ids for get_case_file); the villages whose harvested-but-unburnt fields rank "
            "in the risk run's top tenth (watchlist_top_tenth: field counts and best ranks, never a "
            "probability), which may be none; and the district's best-ranked villages outside the top "
            "tenth (best_ranked_outside_top_tenth: never call them top tenth or high risk). With the "
            "audit precision and the ranking's backtest that qualify them.",
            {"type": "object", "required": ["district"], "properties": {
                "district": {"type": "string", "description": "District in Punjab or Haryana, e.g. 'Sangrur'."},
                "days": {"type": "integer",
                         "description": "Days of Sentinel-2 passes up to the newest one, 1-30. Default 7."}}},
            get_field_brief,
        ),
        Tool(
            "show_on_map",
            "Show evidence on the user's map: fly_to a place or point, show_layer to switch a "
            "layer on, or highlight a feature (a scar or risk cluster by id, a district or city "
            "by name). Returns no data.",
            {"type": "object", "required": ["action"], "properties": {
                "action": {"type": "string", "enum": ["fly_to", "show_layer", "highlight"]},
                "layer": {"type": "string", "enum": list(MAP_LAYERS)},
                "place": {"type": "string", "description": "Place name to fly to or highlight."},
                "lon": {"type": "number"}, "lat": {"type": "number"},
                "zoom": {"type": "number", "description": "4 (region) to 16 (field)."},
                "feature_type": {"type": "string", "enum": list(HIGHLIGHT_TYPES)},
                "feature_id": {"type": "string"},
                "city": {"type": "string", "description": "City for smoke_back."},
                "label": {"type": "string"}}},
            show_on_map,
        ),
    )
}  # fmt: skip


def declarations() -> list[types.FunctionDeclaration]:
    return [tool.declaration() for tool in TOOLS.values()]


async def run_tool(api: InternalApi, name: str, args: dict[str, Any] | None) -> ToolResult:
    """Run one tool by name. Never raises: a bad name, bad arguments or a crash become an
    error result the model can read and recover from."""
    tool = TOOLS.get(name)
    if tool is None:
        return failed("Unknown tool", error=f"there is no tool named {name!r}")
    try:
        return await tool.fn(api, dict(args or {}))
    except Exception as exc:
        log.exception("tool %s failed", name)
        detail = get_settings().redact(f"{type(exc).__name__}: {exc}")[:200]
        return failed(name, error=f"the tool failed: {detail}")


def newest_per_source(refs: list[SourceRef]) -> list[SourceRef]:
    """One SourceRef per source key: its most recent fetch. An endpoint may list every fetch it
    drew on (say each FIRMS file); the model gets one marker per source to cite, while the UI
    timeline still shows them all."""
    newest: dict[str, SourceRef] = {}
    for ref in refs:
        held = newest.get(ref.key)
        when = ref.fetched_at or datetime.min.replace(tzinfo=UTC)
        if held is None or when > (held.fetched_at or datetime.min.replace(tzinfo=UTC)):
            newest[ref.key] = ref
    return list(newest.values())


def model_payload(result: ToolResult, book: CitationBook) -> dict[str, Any]:
    """The FunctionResponse body for Gemini: {"output": data+sources} or {"error": ...}."""
    if not result.ok:
        return {"error": result.error or "no data"}
    return {"output": {**result.data, "sources": book.for_model(newest_per_source(result.sources))}}


__all__ = [
    "HIGHLIGHT_TYPES",
    "MAP_LAYERS",
    "TOOLS",
    "ApiReply",
    "CitationBook",
    "InternalApi",
    "Tool",
    "ToolResult",
    "declarations",
    "model_payload",
    "newest_per_source",
    "resolve_place",
    "run_tool",
]
