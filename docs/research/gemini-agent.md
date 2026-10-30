# Gemini free API: investigator agent (BlindSpot)

> Researched 2026-10-30 (IST) from live ai.google.dev pages (most "Last updated 2026-10-28..30"), keyless calls to
> `generativelanguage.googleapis.com`, and the installed **google-genai 2.34.0** (source inspection + offline wire tests).
> **No API key exists yet, so no Gemini model output, latency or quota number was observed.** Items that need a key are
> marked **UNVERIFIED**.

## 0. Decisions (TL;DR)
1. Use **generateContent** (`client.models.generate_content` / `generate_content_stream`) with a **manual** tool loop
   and AFC disabled. Google now calls generateContent "legacy" (the Interactions API went GA in June 2026) but says it
   "remains fully supported". The explicit loop gives us fallback, citations and SSE control. Its wire format is verified (section 4).
2. Free models for a **new** key: `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`,
   `gemini-3-flash-preview`. **Not free:** `gemini-4-argon`, `gemini-3.1-pro-preview`. Since 2026-09-18 the 2.5 family is
   only served to users who actively used it before.
3. Free quotas are tiny and counted **per project, per model**. Community measurements: Flash ≈ 5 RPM / 20 RPD, Flash-Lite ≈ 10-15 RPM / 500 RPD.
   So the investigator runs on **3.5 Flash-Lite** behind a fallback chain, and 3.8 Flash is kept for the daily bulletin and precomputed runs.
4. **No Google Search grounding on the free tier** for any 3.x model (the pricing page says "Not available"). Citations come
   from our own tool outputs (FIRMS, CPCB and Open-Meteo URLs), which is also what the no-dummy-data rule demands.
5. Never send `temperature`/`top_p`/`top_k`: 3.6+ models ignore them with a deprecation header, and future models will return HTTP 400. Never end
   `contents` with a model turn (HTTP 400). Get determinism from the system instruction and structured output.
6. Thought signatures: append `resp.candidates[0].content` to history **verbatim**. Answer parallel calls in **one** user turn,
   each part echoing the call `id` and `name`. In 2.34.0, `Part.from_function_response()` has **no `id`** parameter, so build
   `types.FunctionResponse(id=...)` directly.
7. SDK 2.34.0 does **not retry by default** (the troubleshooting page claims otherwise). Set `HttpRetryOptions` for 5xx and
   handle 429/503 in our router, which reads RetryInfo, cools the model down and falls back to another.
8. Free tier means prompts are used to improve Google products and may be read by human reviewers. Send only public data.

## 1. Free-tier models (pricing, model pages and thinking guide, fetched 2026-10-30)
| model id | status | free tier | in / out tokens | thinking levels (default) | Search grounding, free | free RPM / RPD (community, UNVERIFIED) |
|---|---|---|---|---|---|---|
| `gemini-3.8-flash` | Stable, GA 2026-09-02 | yes | 1,048,576 / 65,536 | low, medium, high (medium); `minimal` returns an error | No | 5 / 20, TPM 250k [S2] |
| `gemini-3.6-flash` | Stable, GA 2026-07-21 | yes | 1,048,576 / 65,536 | minimal, low, medium, high (medium) | No (AI Studio only) | 5 / 20 [S1][S2] |
| `gemini-3.5-flash-lite` | Stable, GA 2026-07-21 | yes | 1,048,576 / 65,536 | minimal, low, medium, high (minimal) | No (AI Studio only) | 10-15 / 500, TPM 250k [S1][S2] |
| `gemini-3.1-flash-lite` | Stable, earliest shutdown 2027-05-07 | yes | 1,048,576 / 65,536 | minimal (default) to high | No (AI Studio only) | 10-15 / 500 [S1][S2] |
| `gemini-3-flash-preview` | Preview ("legacy") | yes | 1,048,576 / 65,536 | minimal to high (high) | No (AI Studio only) | 5 / 20 [S1] |
| `gemini-2.5-flash` / `-flash-lite` / `-pro` | prior users only, since 2026-09-18 | listed | 1M | thinking_budget | Flash+Lite 500 RPD shared | new keys get 404 "no longer available to new users" [S1] |
| `gemini-4-argon`, `gemini-3.1-pro-preview` | Stable / Preview | **no** (pricing) | | | | 3.1 Pro on a free key returns 429 `limit: 0` [S3] |

* All five free 3.x models list function calling, structured outputs, thinking, caching, code execution and URL context as
  "Supported". Parallel and compositional calls are documented generically, and so is streaming (`streamGenerateContent`).
  Thought summaries come via `include_thoughts=True`. Structured output **combined with** function calling is "Preview" (Gemini 3/4 only).
* Limits (rate-limits page): applied **per project, not per API key**. RPD resets at **midnight Pacific**, which is 12:30 IST today
  and 13:30 IST after US DST ends on 2026-11-01. One Oct-2026 community test claims the reset is at 00:00 UTC (UNVERIFIED). Preview models
  get tighter limits. Google **no longer publishes free-tier numbers**; the real ones are at https://aistudio.google.com/rate-limit (login required).
  Free projects have no spend cap. Each model has its own bucket (`quotaId ...PerProjectPerModel-FreeTier`), so a fallback chain
  multiplies daily capacity. Do **not** create extra projects to pool quota: Google APIs ToS §2(d) says you "will not attempt to circumvent" limits.
* Grounding with Google Search: the free-tier cell is "Not available" for every 3.x model (all except 3.8 Flash add "Can be tested in Google AI Studio"). The paid tier
  includes 5,000 free searches/month and then $14 per 1k, capped at 1,500 RPD. Free Search grounding exists only on 2.5 Flash/Flash-Lite (500 RPD), which new keys cannot use.
* Recent changes (changelog): **10-26** new *paid* projects store generateContent logs by default (`store=true`); **10-13**
  4 Argon GA (paid only); **10-08** `gemini-3.7-flash` now routes to 3.8 Flash and `gemini-3.5-flash` to 3.6 Flash; **09-18** 2.5 models restricted
  to prior users; **07-21** sampling params deprecated and prefilled model turns rejected with 400; **06-01** 2.0 Flash/Flash-Lite shut down;
  **05-28** new AI Studio keys are service-account "auth keys", and unrestricted standard keys are rejected (api-key page; the second change is undated). The free RPD has been 20 for
  Flash-class models since at least Jan 2026 (forum 429 bodies [S4]).
* Hindi (`hi`) and Punjabi (`pa`) are in Google's Gemini language list: "All Gemini models can understand ... and respond"
  (docs.cloud.google.com/gemini-enterprise-agent-platform/models/google-models, updated 2026-10-30). India is an available region.

## 2. Endpoints verified today (2026-10-30)
| what | URL template | auth | verified | measured |
|---|---|---|---|---|
| generateContent | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | header `x-goog-api-key` | 403 without key, 400 `API_KEY_INVALID` with a bad key; **no model output** | 0.17-0.24 s for error replies |
| streaming | `POST .../v1beta/models/{model}:streamGenerateContent?alt=sse` | same | path captured from SDK wire (offline) | |
| models / tokens | `GET .../v1beta/models[/{model}]`, `POST .../models/{model}:countTokens` | same | list returns 403 without key; others from discovery | 0.20 s |
| REST schema | `GET https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta` | none | 200, revision **20261030**, 387 KB | 0.59 s |
| OpenAI-compat | `POST .../v1beta/openai/chat/completions` | `Authorization: Bearer` | 400 with a bad key | 0.20 s |
| Interactions (alt.) | `POST .../v1beta/interactions`; SDK `client.interactions.create(model=, input=, ...)` | key | docs and SDK signature only | |
| FIRMS 24 h (tool 2) | `https://firms.modaps.eosdis.nasa.gov/data/active_fire/{dir}/csv/{F}_South_Asia_24h.csv` with `{dir}/{F}` one of `suomi-npp-viirs-c2/SUOMI_VIIRS_C2`, `noaa-20-viirs-c2/J1_VIIRS_C2`, `noaa-21-viirs-c2/J2_VIIRS_C2`, `modis-c6.1/MODIS_C6_1` | none | 200 x4, `last-modified: 15:49 UTC` | 1.7-2.6 s, 43-326 KB |
| Open-Meteo (tool 1) | `https://api.open-meteo.com/v1/forecast?latitude=&longitude=&current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,surface_pressure,precipitation&wind_speed_unit=kmh&timezone=Asia%2FKolkata` | none | 200 | 0.38-0.73 s |
| Open-Meteo geocoding | `https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json&countryCode=IN` | none | 200 | 1.1-1.5 s cold |

Trimmed real responses:
```text
bad key  -> 400 {"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT",
            "details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"API_KEY_INVALID","domain":"googleapis.com",...},
                       {"@type":"type.googleapis.com/google.rpc.LocalizedMessage",...}]}}
no key   -> 403 {"error":{"code":403,"message":"Method doesn't allow unregistered callers (callers without established identity)...","status":"PERMISSION_DENIED"}}
FIRMS    -> latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
            39.96186,97.39194,339.33,0.47,0.4,2026-10-29,0650,N20,nominal,2.0NRT,295.33,4.12,D      (no 'instrument' column; MODIS: brightness/bright_t31, confidence 0-100)
OpenMeteo-> "current":{"time":"2026-10-30T21:15","interval":900,"temperature_2m":19.6,"relative_humidity_2m":92,"wind_speed_10m":2.6,"wind_direction_10m":12,...}
tool 2   -> count_firms_fires(73.8,27.6,77.6,32.6) @16:06 UTC: VIIRS_SNPP 478, VIIRS_NOAA20 611, VIIRS_NOAA21 215, MODIS 193 (total 1497);
            by_ist_hour {"2026-10-29 13:00 IST":718,"2026-10-29 16:00 IST":192,...,"2026-10-30 13:00 IST":463,"2026-10-30 14:00 IST":74}
```
Side finding for the FIRMS owner: today's MODIS detections over Punjab are **Aqua** (`satellite=A`) at about **16:10 IST**, a drifted
orbit, while Terra passed at about 20:50 IST (night). The VIIRS satellites passed at 12:40-14:20 IST.

## 3. google-genai 2.34.0: facts verified locally
* `genai.Client(api_key=..., http_options=types.HttpOptions(timeout=<ms>, retry_options=...))`. Env fallback order is `GOOGLE_API_KEY`
  then `GEMINI_API_KEY`, and the SDK warns if both are set. The key goes in the `x-goog-api-key` header. Async versions live at `client.aio.models.*`.
* `generate_content(*, model, contents, config)` returns `GenerateContentResponse`. `generate_content_stream(...)` returns an iterator,
  and HTTP errors surface on the first `next()`. Streaming function calls arrive whole in one chunk.
* Config fields used: `system_instruction, tools, tool_config, automatic_function_calling, thinking_config, response_mime_type,
  response_schema, response_json_schema, safety_settings, max_output_tokens`. `max_output_tokens` **includes thinking tokens**:
  a small cap yields `MAX_TOKENS` with empty output, so lower the thinking level instead.
* `ThinkingConfig(include_thoughts, thinking_budget, thinking_level=MINIMAL|LOW|MEDIUM|HIGH)`. Sending level and budget together returns 400.
  Wire form: `"thinkingConfig":{"include_thoughts":true,"thinking_level":"MEDIUM"}`.
* `FunctionCallingConfig(mode=AUTO|ANY|NONE|VALIDATED, allowed_function_names=[...])`. Docs on VALIDATED: "ensures function schema
  adherence... reduces malformed function calls (compared to AUTO mode)". `FunctionDeclaration(name, description,
  parameters_json_schema=<plain JSON Schema>)`. Docs advise keeping 10-20 active tools at most.
* AFC only runs when Python callables are passed in `tools`; with plain declarations the call comes back to us. `disable=True` makes that explicit.
* `resp.function_calls` returns every call (parallel). `resp.text` joins the non-thought text (None if there is none). Thought summaries are parts with `thought=True`.
* **`types.Part.from_function_response(*, name, response, parts)` has no `id`**: the official snippet `(..., id=tool_call.id)` raises
  `TypeError` on 2.34.0. Build `types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response={"output": ...}))`;
  the SDK convention is an `"output"` or `"error"` key.
* **Default retry is 1 attempt**: `retry_options=None` gives `stop_after_attempt(1)`. `HttpRetryOptions()` gives 5 attempts with exponential
  backoff plus jitter from 1 s to 60 s on 408/429/500/502/503/504, and it **ignores RetryInfo**.
* Errors are `errors.ClientError` (4xx) or `errors.ServerError` (5xx), both subclasses of `errors.APIError` with `.code .status .message`
  and `.details` (the full JSON body). Server enum values unknown to the SDK, such as `MISSING_THOUGHT_SIGNATURE` (present in the discovery doc),
  become pseudo-members with a `UserWarning`, so compare on `.name`.
* `thought_signature` is `bytes` and the SDK encodes it urlsafe-base64. The documented bypass value goes in as
  `base64.urlsafe_b64decode("skip_thought_signature_validator")` and appears literally on the wire (verified).
* `response_schema=<Pydantic>` with a `dict[...]` field fails client-side with `ValueError: additionalProperties is only supported in
  Gemini Enterprise Agent Platform mode`. Use `response_json_schema=Model.model_json_schema()` instead (it works). `.parsed` is only filled for `response_schema`.
* Thought-signature rules (generate-content/thought-signatures). For parallel calls only the **first** functionCall carries a signature. Every
  signature in the current turn is validated, and a missing one returns 400 ("...content block is missing a thought_signature"). Send results as
  `FC1+sig, FC2, FR1, FR2`; interleaving them returns 400. While streaming, the signature can arrive on an empty-text part, so keep every part.

## 4. Complete manual tool loop (tested)
`uv run python gemini_agent.py --tools-only` needs no key. With a key: `GEMINI_API_KEY=... uv run python gemini_agent.py "question"`.
```python
"""BlindSpot investigator: minimal MANUAL Gemini tool loop (google-genai 2.34.0, Gemini Developer API free tier).

  uv run python gemini_agent.py --tools-only          # runs both real tools, no key needed
  GEMINI_API_KEY=... uv run python gemini_agent.py "Is stubble smoke reaching Delhi today?"
"""
from __future__ import annotations

import base64, csv, io, itertools, json, os, re, sys, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import httpx
from google import genai
from google.genai import errors, types

IST = timezone(timedelta(hours=5, minutes=30))
_http = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "BlindSpot/0.1 (Forge'26)"})
_cache: dict[str, tuple[float, Any]] = {}

def _get(url: str, params: dict | None = None, ttl: float = 600, as_json: bool = True) -> tuple[Any, str]:
    """GET with a small in-process TTL cache (be gentle with public APIs). Returns (payload, exact_url)."""
    key = str(httpx.URL(url, params=params or {}))
    if (hit := _cache.get(key)) and time.time() - hit[0] < ttl:
        return hit[1], key
    r = _http.get(url, params=params)
    r.raise_for_status()
    val = r.json() if as_json else r.text
    _cache[key] = (time.time(), val)
    return val, key

# ---------- tool 1: Open-Meteo current weather (keyless, CC BY 4.0) ----------
def get_current_weather(city: str) -> dict:
    geo, geo_url = _get("https://geocoding-api.open-meteo.com/v1/search",
                        {"name": city, "count": 1, "language": "en", "format": "json", "countryCode": "IN"}, ttl=86400)
    if not geo.get("results"):
        return {"error": f"no place named {city!r} in India (Open-Meteo geocoding)"}
    p = geo["results"][0]
    wx, wx_url = _get("https://api.open-meteo.com/v1/forecast", {
        "latitude": p["latitude"], "longitude": p["longitude"], "wind_speed_unit": "kmh", "timezone": "Asia/Kolkata",
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,surface_pressure,precipitation"},
        ttl=900)
    return {"place": f"{p['name']}, {p.get('admin1', '')}", "lat": p["latitude"], "lon": p["longitude"],
            "current": wx["current"], "units": wx["current_units"], "sources": [geo_url, wx_url],
            "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)"}

# ---------- tool 2: NASA FIRMS keyless 24 h South Asia CSVs, count detections in a bbox ----------
FIRMS = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
FIRMS_24H = {"VIIRS_SNPP": "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_South_Asia_24h.csv",
             "VIIRS_NOAA20": "noaa-20-viirs-c2/csv/J1_VIIRS_C2_South_Asia_24h.csv",
             "VIIRS_NOAA21": "noaa-21-viirs-c2/csv/J2_VIIRS_C2_South_Asia_24h.csv",
             "MODIS": "modis-c6.1/csv/MODIS_C6_1_South_Asia_24h.csv"}

def count_firms_fires(west: float, south: float, east: float, north: float, sensors: list[str] | None = None) -> dict:
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return {"error": "bbox must satisfy west < east and south < north (decimal degrees)"}
    out: dict[str, Any] = {"bbox_wsen": [west, south, east, north], "window": "rolling last 24 h, FIRMS NRT",
                           "by_sensor": {}, "by_ist_hour": {}, "sources": []}
    for s in sensors or list(FIRMS_24H):
        if s not in FIRMS_24H:
            return {"error": f"unknown sensor {s!r}; valid: {list(FIRMS_24H)}"}
        text, url = _get(FIRMS + FIRMS_24H[s], ttl=600, as_json=False)
        n, hi, frp = 0, 0, 0.0
        for r in csv.DictReader(io.StringIO(text)):
            lat, lon = float(r["latitude"]), float(r["longitude"])
            if not (west <= lon <= east and south <= lat <= north):
                continue
            n += 1
            frp += float(r["frp"] or 0)
            c = r["confidence"]  # VIIRS: low/nominal/high (or l/n/h); MODIS: 0-100
            hi += c in ("h", "high") or (c.isdigit() and int(c) >= 80)
            t = datetime.strptime(r["acq_date"] + r["acq_time"].zfill(4), "%Y-%m-%d%H%M")
            k = t.replace(tzinfo=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:00 IST")
            out["by_ist_hour"][k] = out["by_ist_hour"].get(k, 0) + 1
        out["by_sensor"][s] = {"detections": n, "high_confidence": hi, "frp_mw_sum": round(frp, 1)}
        out["sources"].append(url)
    out["total_detections"] = sum(v["detections"] for v in out["by_sensor"].values())
    out["by_ist_hour"] = dict(sorted(out["by_ist_hour"].items()))
    out["caveat"] = "Pixels, not fields; the three VIIRS satellites overlap, so one fire can appear several times."
    out["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out

TOOLS: dict[str, Callable[..., dict]] = {"get_current_weather": get_current_weather,
                                         "count_firms_fires": count_firms_fires}
DECLS = [
    types.FunctionDeclaration(
        name="get_current_weather",
        description="Current surface weather for a place in India (Open-Meteo): temperature, humidity, 10 m wind "
                    "speed km/h and direction in degrees (direction the wind blows FROM), pressure.",
        parameters_json_schema={"type": "object", "required": ["city"], "properties": {
            "city": {"type": "string", "description": "Place in India, e.g. 'Sangrur' or 'Delhi'"}}}),
    types.FunctionDeclaration(
        name="count_firms_fires",
        description="Count NASA FIRMS active-fire detections (VIIRS 375 m x3 satellites + MODIS 1 km, rolling last "
                    "24 h) inside a lon/lat box; returns per-sensor counts, per-IST-hour counts, FRP and source URLs. "
                    "Punjab+Haryana is about west=73.8 south=27.6 east=77.6 north=32.6.",
        parameters_json_schema={"type": "object", "required": ["west", "south", "east", "north"], "properties": {
            "west": {"type": "number"}, "south": {"type": "number"},
            "east": {"type": "number"}, "north": {"type": "number"},
            "sensors": {"type": "array", "items": {"type": "string", "enum": list(FIRMS_24H)}}}}),
]
SYSTEM = """You are BlindSpot's air-quality investigator for Punjab, Haryana and Delhi.
Gather evidence with the tools before concluding; call independent tools in parallel.
Every number you state must come from a tool result: cite it as [n] and list the source URLs at the end.
Use IST. State what the data cannot show (FIRMS only sees fires burning during satellite overpasses, not under thick
cloud/smoke). If a tool returns an error, say so. Never invent values."""

def make_config(final: bool = False, strict: bool = False) -> types.GenerateContentConfig:
    mode = "NONE" if final else ("VALIDATED" if strict else "AUTO")
    return types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[types.Tool(function_declarations=DECLS)],
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=mode)),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # we run tools ourselves
        thinking_config=types.ThinkingConfig(thinking_level="MEDIUM", include_thoughts=True),
        # no temperature/top_p/top_k: ignored on Gemini 3.6+ (deprecation header today, HTTP 400 in future models)
    )

# ---------- model router: per-model cooldowns, RetryInfo-aware, client-side RPM guard ----------
DUMMY_SIG = base64.urlsafe_b64decode("skip_thought_signature_validator")  # SDK urlsafe-encodes -> literal on wire
RETRYABLE = {404, 408, 429, 500, 502, 503, 504}

def cooldown_s(e: errors.APIError) -> float:
    det = e.details.get("error", {}).get("details", []) if isinstance(e.details, dict) else []
    if e.code == 404:
        return 6 * 3600  # model not served to this key (e.g. 2.5 family for new projects)
    if e.code == 429:
        quota_ids = " ".join(v.get("quotaId", "") for d in det for v in d.get("violations", []))
        if "PerDay" in quota_ids or "limit: 0" in (e.message or ""):
            return 3600  # daily quota gone (resets midnight Pacific); re-probe hourly
        for d in det:
            if d.get("@type", "").endswith("google.rpc.RetryInfo"):
                return float(str(d.get("retryDelay", "30s")).rstrip("s") or 30)
        m = re.search(r"retry in ([\d.]+)s", e.message or "")
        return float(m.group(1)) if m else 30.0
    return 30.0 if e.code == 503 else 10.0

def _neutralize(contents: list[types.Content]) -> list[types.Content]:
    """History made by model A sent to model B: swap FC signatures for the documented validator-skip value."""
    out = [c.model_copy(deep=True) for c in contents]
    for c in out:
        for p in (c.parts or []) if c.role == "model" else []:
            if p.function_call is not None and p.thought_signature:
                p.thought_signature = DUMMY_SIG
    return out

class Router:
    def __init__(self, client: genai.Client, chain: list[str], rpm: dict[str, int], max_wait: float = 20.0):
        self.client, self.chain, self.rpm, self.max_wait = client, chain, rpm, max_wait
        self.cool: dict[str, float] = {}
        self.sent: dict[str, deque] = {m: deque() for m in chain}

    def _ready_at(self, m: str) -> float:
        q, now = self.sent[m], time.time()
        while q and now - q[0] > 60:
            q.popleft()
        rpm_at = q[0] + 60 if len(q) >= self.rpm.get(m, 10) else 0.0
        return max(self.cool.get(m, 0.0), rpm_at)

    def call(self, contents: list[types.Content], prefer: str | None = None, *, final=False, strict=False, stream=False):
        last: Exception | None = None
        for _ in range(2):  # 2nd pass: wait for the soonest cooldown if it is short
            for m in sorted(self.chain, key=lambda x: x != prefer):  # stay on the model that owns the history
                if self._ready_at(m) > time.time():
                    continue
                payload = contents if prefer in (None, m) else _neutralize(contents)
                cfg = make_config(final=final, strict=strict)
                self.sent[m].append(time.time())
                try:
                    if stream:
                        it = self.client.models.generate_content_stream(model=m, contents=payload, config=cfg)
                        resp: Any = itertools.chain([next(it)], it)  # HTTP errors surface on the first chunk
                    else:
                        resp = self.client.models.generate_content(model=m, contents=payload, config=cfg)
                except errors.APIError as e:
                    if e.code not in RETRYABLE:
                        raise  # 400/401/403: bad request, key or project problem -> do not hide it
                    last, self.cool[m] = e, time.time() + cooldown_s(e)
                    continue
                except (httpx.TransportError, StopIteration) as e:
                    last, self.cool[m] = e, time.time() + 5
                    continue
                if payload is not contents:
                    contents[:] = payload  # history now continues on model m
                return m, resp
            wait = min(self._ready_at(m) for m in self.chain) - time.time()
            if wait > self.max_wait:
                break
            time.sleep(max(wait, 0.5))
        raise RuntimeError(f"Gemini unavailable on every model in the chain: {last}")

def _run_tool(fc: types.FunctionCall) -> dict:
    fn = TOOLS.get(fc.name or "")
    if fn is None:
        return {"error": f"unknown tool {fc.name}"}
    try:
        res = fn(**(fc.args or {}))
    except Exception as e:  # bad args, HTTP errors -> let the model see and recover
        return {"error": f"{type(e).__name__}: {e}"}
    return {"error": res["error"]} if "error" in res else {"output": res}

def _tool_turn(calls: list[types.FunctionCall]) -> tuple[types.Content, list[dict]]:
    """All parallel results go in ONE user turn, same order as the calls, each echoing call id + name."""
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(_run_tool, calls))
    parts = [types.Part(function_response=types.FunctionResponse(id=c.id, name=c.name, response=r))
             for c, r in zip(calls, results)]  # NB: Part.from_function_response() has no id= in 2.34.0
    return types.Content(role="user", parts=parts), results

def investigate(router: Router, question: str, max_steps: int = 6) -> dict:
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]
    model, trace, strict = None, [], False
    for step in range(max_steps):
        model, resp = router.call(contents, prefer=model, final=step == max_steps - 1, strict=strict)
        if not resp.candidates:  # whole prompt blocked
            return {"model": model, "answer": None, "blocked": str(resp.prompt_feedback.block_reason), "trace": trace}
        cand = resp.candidates[0]
        finish = cand.finish_reason.name if cand.finish_reason else None
        if not (cand.content and cand.content.parts):  # MALFORMED_FUNCTION_CALL, SAFETY, ... (no content)
            trace.append({"step": step, "model": model, "finish": finish, "message": cand.finish_message})
            if finish in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "MALFORMED_RESPONSE"):
                strict = True  # retry this step once with schema-validated calling
                continue
            return {"model": model, "answer": None, "finish": finish, "trace": trace}
        contents.append(cand.content)  # verbatim: thought parts + function_call parts keep thought_signature
        calls = resp.function_calls or []
        u = resp.usage_metadata
        trace.append({"step": step, "model": model, "finish": finish,
                      "thoughts": [p.text for p in cand.content.parts if p.thought and p.text],
                      "calls": [{"id": c.id, "name": c.name, "args": c.args} for c in calls],
                      "tokens": u and {"in": u.prompt_token_count, "out": u.candidates_token_count,
                                       "thought": u.thoughts_token_count, "cached": u.cached_content_token_count}})
        if not calls:
            return {"model": model, "answer": resp.text, "finish": finish, "trace": trace}
        turn, _ = _tool_turn(calls)
        contents.append(turn)
    return {"model": model, "answer": None, "finish": "STEP_LIMIT", "trace": trace}

def investigate_stream(router: Router, question: str, max_steps: int = 6) -> Iterator[tuple[str, Any]]:
    """Yields (event, data) for SSE: thought | text | tool_call | tool_result | done."""
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]
    model, strict = None, False
    for step in range(max_steps):
        model, chunks = router.call(contents, prefer=model, final=step == max_steps - 1, strict=strict, stream=True)
        parts, calls, finish = [], [], None
        for ch in chunks:
            cand = ch.candidates[0] if ch.candidates else None
            if cand is None:
                continue
            for p in (cand.content.parts or []) if cand.content else []:
                parts.append(p)  # keep EVERY part: a signature may ride on an empty-text part
                if p.function_call:
                    calls.append(p.function_call)
                    yield "tool_call", {"name": p.function_call.name, "args": p.function_call.args}
                elif p.text and p.thought:
                    yield "thought", p.text
                elif p.text:
                    yield "text", p.text
            finish = cand.finish_reason or finish
        if not parts and finish is not None and finish.name in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"):
            strict = True  # nothing to keep; retry the step with schema-validated calling
            continue
        contents.append(types.Content(role="model", parts=parts))
        if not calls:
            yield "done", {"model": model, "finish": finish.name if finish else None}
            return
        turn, results = _tool_turn(calls)
        for c, r in zip(calls, results):
            yield "tool_result", {"name": c.name, "result": r}
        contents.append(turn)
    yield "done", {"model": model, "finish": "STEP_LIMIT"}

def make_router() -> Router:
    client = genai.Client(  # api_key falls back to GEMINI_API_KEY / GOOGLE_API_KEY env vars
        api_key=os.environ.get("GEMINI_API_KEY"),
        http_options=types.HttpOptions(timeout=90_000,  # milliseconds
                                       retry_options=types.HttpRetryOptions(  # SDK default (None) = 1 attempt
                                           attempts=2, initial_delay=1, max_delay=4,
                                           http_status_codes=[408, 500, 502, 504])))  # 429/503 -> Router fallback
    chain = os.getenv("GEMINI_CHAIN", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash,gemini-3.6-flash")
    rpm = {"gemini-3.8-flash": 5, "gemini-3.6-flash": 5, "gemini-3-flash-preview": 5}  # others default to 10
    return Router(client, [m.strip() for m in chain.split(",")], rpm)

if __name__ == "__main__":
    if sys.argv[1:] == ["--tools-only"]:
        print(json.dumps(get_current_weather("Sangrur"), indent=1, ensure_ascii=False))
        print(json.dumps(count_firms_fires(73.8, 27.6, 77.6, 32.6), indent=1))
    else:
        q = " ".join(sys.argv[1:]) or "Are crop fires in Punjab likely affecting Delhi right now? Check winds."
        for ev, data in investigate_stream(make_router(), q):
            print(f"[{ev}]", data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))
```
**What was tested today:**
* `py_compile` passes.
* `--tools-only` ran both tools against live APIs (output in section 2).
* An offline harness injected `httpx.MockTransport` via `HttpOptions(httpx_client=...)` and returned responses shaped like Google's examples, with the tools running live. It showed:
  * A 429 with RetryInfo `27s` put a 27 s cooldown on 3.5-Flash-Lite and the request fell back to 3.1-Flash-Lite.
  * The step-2 wire was `user[text]`, then `model[thought, FC1+thoughtSignature, FC2]`, then `user[FR(id fc-1), FR(id fc-2)]`, each FR with an `output` key.
  * The signature was echoed byte-exact.
  * The stream emitted `thought, tool_call, tool_result, text, done`.
  * A 503 mid-loop switched models, with the FC signature sent as the literal `skip_thought_signature_validator`.

**Not tested:** any real Gemini answer, latency, or whether `VALIDATED` mode is accepted by each model.
FastAPI: run the sync loop in `run_in_threadpool`, or port it to `client.aio`, and stream `investigate_stream` events via
`sse_starlette.EventSourceResponse` (already a dependency).

## 5. Error handling, retries and fallback for a judged live demo
| condition | what it looks like | action (implemented above) |
|---|---|---|
| 429 per-minute | `RESOURCE_EXHAUSTED`; QuotaFailure `quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier`; RetryInfo `retryDelay:"27s"` | cool the model for retryDelay and switch model immediately |
| 429 per-day | `quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier` (also `GenerateContentInputTokensPerModelPerDay-FreeTier`); message "retry in 10h17m..." | check `PerDay` **before** RetryInfo (it can still say 27s), cool for 1 h and re-probe |
| 429 `limit: 0` | model not in your tier (e.g. 3.1 Pro on a free key [S3]; Argon is "not available" on free per the pricing page) | remove it from the chain |
| 503 `UNAVAILABLE` "model is overloaded" | `ServerError` | cool 30 s, next model |
| 500/502/504 | `ServerError` | SDK retries once (HttpRetryOptions), then the router switches |
| 404 | model not served to this key (2.5 for new users) | drop it for 6 h |
| 400 | bad parameter: `minimal` on 3.8 Flash, level+budget, prefilled model turn, missing signature, oversized schema | **don't retry**; log and fix |
| 403 | key or project problem (an unrestricted standard key is rejected) | fail loudly |
| prompt blocked | no candidates, `prompt_feedback.block_reason` set (SAFETY/OTHER/BLOCKLIST/PROHIBITED_CONTENT) | show "blocked" alongside the deterministic evidence |
| response blocked | `finish_reason` SAFETY / PROHIBITED_CONTENT / SPII / BLOCKLIST / RECITATION | same; adjustable filters are already **Off by default** on 2.5+ |
| malformed call | `finish_reason` MALFORMED_FUNCTION_CALL / UNEXPECTED_TOOL_CALL, `content=None` | retry the step with `mode="VALIDATED"`; never require XML/JSON text before a tool call (docs' fix: an `update()` tool) |
| step cap / TOO_MANY_TOOL_CALLS | | final step with `mode="NONE"` to force a text answer |
| error mid-stream (after the first chunk) | exception escapes `investigate_stream` (the router only guards the first chunk) | send an SSE `error` event and let the UI offer a retry |

The 429 body below is a community-sourced shape (forum posts, Jan 2026) and was **not observed today**:
`{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"You exceeded your current quota ... limit: 20, model: gemini-2.5-flash ... Please retry in 27.966937085s.","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure","violations":[{"quotaMetric":"generativelanguage.googleapis.com/generate_content_free_tier_requests","quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaDimensions":{"location":"global","model":"..."},"quotaValue":"20"}]},{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"27s"}]}}`

Demo policy:
* Use the fallback chain plus a client-side RPM guard (`Router.rpm`; set it to the real AI Studio numbers).
* Put a per-IP limit on `/api/investigate` (about 3/min) and use single-flight so identical concurrent questions share one call.
* Keep at least 20% of the Flash-Lite RPD in reserve for the judging slot, and don't test on 3.8 Flash on demo day.
* If every model is cooling down, return the **deterministic evidence pack** (FIRMS counts, winds, AQI) labelled "AI narrative unavailable (quota)". Never return a canned answer.
* Show the model id, a fallback badge and token usage. Stream thought summaries into a "reasoning" panel and tool calls as timeline chips.

## 6. Caching strategy
1. **Tool data:** cache FIRMS for 10 min (files are rewritten throughout the day), Open-Meteo current for 15 min (`interval: 900`) and geocoding for 24 h. In production,
   APScheduler loads these into PostGIS and the tools query PostGIS, not the internet.
2. **Answers:** key on `sha256(normalised question + data snapshot ids)`, using FIRMS `Last-Modified` and Open-Meteo `current.time`, with a 15 min TTL.
   Replay the cached SSE events with a "narrated at HH:MM IST" label: the data is still live, only the narration is reused.
3. **Precompute:** generate the daily bulletin and 2-3 "featured investigations" once after each data refresh and serve them from the DB.
4. **Implicit prompt caching** is automatic (at least 4,096 input tokens on 3.8/3.6 Flash). Keep the system prompt and tool declarations byte-identical and first.
   It saves latency or cost but **not RPD**, which counts requests. Explicit `cachedContents` isn't worth it for our short prompts.
5. Never cache Google-Search-grounded output (ToS). That only matters if billing is ever enabled.

## 7. Bulletin (EN/HI/PA in one request) and a cheap verification pass
```python
# needs: json, pydantic BaseModel/Field, google.genai types, `client` (as in make_router), `facts` = dict loaded from PostGIS/tools
class Bulletin(BaseModel):
    headline: dict[str, str] = Field(description="keys en, hi, pa")
    body_en: str
    body_hi: str = Field(description="Hindi, Devanagari script")
    body_pa: str = Field(description="Punjabi, Gurmukhi script")
    source_urls: list[str]
cfg = types.GenerateContentConfig(
    system_instruction="Write a public air-quality bulletin from the JSON facts only; identical numbers in all languages.",
    response_mime_type="application/json", response_json_schema=Bulletin.model_json_schema(),  # NOT response_schema (dict field)
    thinking_config=types.ThinkingConfig(thinking_level="LOW"))
b = Bulletin.model_validate_json(client.models.generate_content(
    model="gemini-3.8-flash", contents=json.dumps(facts, ensure_ascii=False), config=cfg).text)

class Verdict(BaseModel):
    faithful: bool
    unsupported_claims: list[str]
v = client.models.generate_content(model="gemini-3.5-flash-lite",
    contents=f"FACTS:\n{json.dumps(facts, ensure_ascii=False)}\nTEXT:\n{b.body_hi}\nList every claim not supported by FACTS.",
    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Verdict,
                                       thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"))).parsed
```
Wire format verified offline: (a) `generationConfig{responseJsonSchema, responseMimeType, thinkingConfig}`; (b) `responseSchema` in OpenAPI
form with `property_ordering` added, and `.parsed` returns a `Verdict`. Run a deterministic check first: every number and URL in each language
body must appear in `facts`. Wrap both calls in the same `Router`-style fallback.

## 8. Free-tier data-use terms
Sources: https://ai.google.dev/gemini-api/terms (effective 2026-03-23) and https://ai.google.dev/gemini-api/docs/logs-policy.
* "Unpaid Services" covers AI Studio and unpaid API quota: "Google uses the content you submit ... and any generated responses to provide,
  improve, and develop Google products and services and machine learning technologies"; "human reviewers may read, annotate, and process your API
  input and output" (after it is disconnected from the account, key and project); "Do not submit sensitive, confidential, or personal information".
* EEA/CH/UK: the paid-service data terms apply even to free quota, and only Paid Services may be used to serve users there. Users must be **18+**,
  the service may not be directed at under-18s, and it is for "professional or business purposes".
* Paid tier: content is not used for improvement. AI Studio logs and datasets exist only for billing-enabled projects (55-day default retention).
  If we switch to the Interactions API, it stores calls by default (`store=true`; free tier keeps them for 1 day).
* Search grounding (if paid): show the Search Suggestions, and don't cache or store grounded results except in the narrow ToS cases.

What this means for us: prompts contain only public environmental data and the judge's question, and the UI carries a footnote
"processed by Google Gemini (free tier); don't enter personal information".

## 9. Model recommendations
* **(a) Investigator loop:** `gemini-3.5-flash-lite` at `thinking_level=MEDIUM` with `include_thoughts=True`. Google's migration guide says to raise
  the level to medium or high "for autonomous subagents with tool calls... to prevent premature tool termination". It is GA, supports parallel function
  calls, and has the only free bucket big enough for judges (about 500 RPD vs about 20 for Flash). Fallback: `gemini-3.1-flash-lite`, then
  `gemini-3.8-flash`, then `gemini-3.6-flash`, each with its own quota. An optional "deep investigation" toggle starts on `gemini-3.8-flash`
  (medium), Google's flagship Flash for "autonomous agents", and falls back automatically.
* **(b) Daily multilingual bulletin:** `gemini-3.8-flash` at low/medium thinking, **one** request producing EN+HI+PA JSON (section 7), run on a schedule
  after the data refresh. Fallback: `gemini-3.6-flash`, then `gemini-3.5-flash-lite`. That is about 1-3 requests a day.
* **(c) Cheap verification:** a deterministic Python check first, then `gemini-3.5-flash-lite` at `MINIMAL` with `response_schema=Verdict`, falling back to
  `gemini-3.1-flash-lite`.
* Budget (community limits): about 4 requests per investigation, so roughly 120 live investigations a day on 3.5 Flash-Lite before any fallback is needed.

## 10. UNVERIFIED: do these first once a key exists (about 6 requests)
1. `curl -s -H "x-goog-api-key: $GEMINI_API_KEY" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"` to list the models the key
   can use, with `inputTokenLimit`, `outputTokenLimit` and `supportedGenerationMethods`.
2. Read the real RPM, TPM and RPD at https://aistudio.google.com/rate-limit and put them into `Router(rpm=...)` and the budgets.
3. Make one `thinking_level` LOW call per chain model, then run `gemini_agent.py` once (3-4 requests). Record latency and `usage_metadata`.
4. Still open: whether each model accepts `mode="VALIDATED"`, the real 429 body, whether rejected 429 calls count toward RPD, and the
   reset time (12:30 or 13:30 IST vs 05:30 IST).

## Sources
Official (fetched 2026-10-30): ai.google.dev/gemini-api/docs/{models, models/gemini-3.8-flash, models/gemini-3.6-flash, models/gemini-3.5-flash-lite,
models/gemini-3.1-flash-lite, pricing, rate-limits, changelog, deprecations, whats-new-gemini-3.8, whats-new-gemini-3.6, thinking, interactions-overview,
migrate-to-interactions, troubleshooting, api-errors, caching, safety-settings, api-key, logs-policy, available-regions};
ai.google.dev/gemini-api/docs/generate-content/{function-calling, thought-signatures, thinking, structured-output, google-search};
ai.google.dev/gemini-api/terms; developers.google.com/terms §2(d); open-meteo.com/en/terms (free API is non-commercial, <10,000 calls/day, 5,000/h, 600/min, CC BY 4.0).
Community (UNVERIFIED numbers): [S1] dev.to/romeroyang/geminis-free-tier-measured-20-requests-a-day-and-google-no-longer-publishes-the-number-4gf2
(measured 2026-09-02); [S2] scriptbyai.com/gemini-api-free-tier-limits (undated, mentions 2026-10-08); [S3]
dev.to/nils44344/gemini-api-429-resourceexhausted-why-your-limit-is-0-68o (tested 2026-10-07); [S4]
discuss.ai.google.dev/t/gemini-api-429-resource-exhausted-error-on-tier-1/114413 (429 bodies, Jan 2026).
