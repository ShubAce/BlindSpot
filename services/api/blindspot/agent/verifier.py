"""Checks every number in an answer against what the tools actually returned.

Why: a language model can write a plausible number that no tool gave it. Before an answer is
shown as final, this module pulls every numeric claim out of the text (counts, decimals,
percentages, areas, speeds, clock times, dates; Hindi and Punjabi digits too) and looks for each
one in the evidence the model was shown: the tool outputs, the tool arguments, the question and
the current date. A claim passes when some evidence value matches it within rounding:

- "312" matches 312.4; "about 300" matches 312 (a round number may be rounded, by at most 10%);
- "42%" matches 42 or a share of 0.42; "16 km/h" matches 4.4 m/s; "12 km²" matches 1200 ha;
- "13:20 IST" matches a time within 10 minutes; "1 pm" matches 13:00; "30 Oct" matches a date.

Anything else is "unverified". The runner then gives the model one revision pass with the list.
Numbers that are names, not claims (PM2.5, NOAA-20, Sentinel-2, 925 hPa, list numbering, the
citation markers themselves) are masked first. Citation markers that point at no source fail too.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from blindspot.geo import IST

REL_TOL = 0.015  # 1.5%: unit conversions and float noise
ROUND_NUMBER_MAX_REL = 0.10  # "about 300" may stand for 270-330, never 200 for 312
TIME_TOL_MIN = 10

# Indic digits -> ASCII: Devanagari (Hindi) U+0966-096F and Gurmukhi (Punjabi) U+0A66-0A6F.
_DIGITS = {0x0966 + i: str(i) for i in range(10)} | {0x0A66 + i: str(i) for i in range(10)}

MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    (
        "january jan जनवरी ਜਨਵਰੀ",
        "february feb फ़रवरी फरवरी ਫ਼ਰਵਰੀ ਫਰਵਰੀ",
        "march mar मार्च ਮਾਰਚ",
        "april apr अप्रैल ਅਪ੍ਰੈਲ ਅਪਰੈਲ",
        "may मई ਮਈ",
        "june jun जून ਜੂਨ",
        "july jul जुलाई ਜੁਲਾਈ",
        "august aug अगस्त ਅਗਸਤ",
        "september sept sep सितंबर सितम्बर ਸਤੰਬਰ ਸਿਤੰਬਰ",
        "october oct अक्टूबर अक्तूबर ਅਕਤੂਬਰ",
        "november nov नवंबर नवम्बर ਨਵੰਬਰ",
        "december dec दिसंबर दिसम्बर ਦਸੰਬਰ",
    ),
    start=1,
):
    for _name in _names.split():
        MONTHS[_name] = _number
_MONTH_RE = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

# --- patterns ---------------------------------------------------------------------------------

_CITATION = re.compile(r"\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]")
_NOT_CLAIMS = [
    re.compile(r"https?://\S+"),
    # pollutant names (PM2.5, PM 10, Hindi/Punjabi transliterations)
    re.compile(r"(?:PM|पीएम|ਪੀਐਮ|ਪੀਐੱਮ|ਪੀ\.ਐਮ\.)\s*[-‐]?\s*(?:2[.·]5|₂\.₅|10)(?!\d)", re.I),
    re.compile(r"\b(?:NO2|SO2|O3|CO2|NH3)\b"),
    # satellite / instrument names glued to a number: NOAA-20, Sentinel-2A, सेंटिनल-2, S-NPP
    re.compile(r"(?<=[^\W\d_])[-‐]\d+[A-Za-z]?(?![\d.])"),
    re.compile(r"\b(?:NOAA|Sentinel|Landsat|INSAT|Himawari|GOES|Meteosat|MetOp|GK)\s?\d+[A-Za-z]?\b",
               re.I),  # fmt: skip
    # pressure levels and standard heights are names of levels, not measurements
    re.compile(r"(?<![\d.])\d{3,4}\s?hPa\b", re.I),
    re.compile(r"(?<![\d.])(?:2|10)\s?(?:m|metre|meter)s?(?=\s+(?:wind|temperature|level))", re.I),
    re.compile(r"UTC\s*[+−-]\s*0?5:30|IST\s*\(UTC\s*[+−-]\s*0?5:30\)", re.I),
    # list numbering at the start of a line: "1. " or "2) "
    re.compile(r"(?m)^\s*\d{1,2}[.)]\s"),
]
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d])")
_DAY_MONTH = re.compile(
    rf"(?<![\d.,])(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_RE})\.?(?:,?\s*(\d{{4}}))?",
    re.I,
)
_MONTH_DAY = re.compile(
    rf"({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?![\d:]|\.\d)(?:,?\s*(\d{{4}}))?", re.I
)
_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{2,4})(?![\d/])")
_AMPM = r"(am|pm|a\.m\.|p\.m\.)"
_TIME_COLON = re.compile(
    rf"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])\s*(?:{_AMPM}\s*)?(IST|UTC|GMT)?", re.I
)
_TIME_DOT = re.compile(rf"(?<![\d:.])(1[0-2]|0?[1-9])\.([0-5]\d)\s*{_AMPM}", re.I)
_TIME_HOUR = re.compile(rf"(?<![\d:.])(1[0-2]|0?[1-9])\s*{_AMPM}(?!\w)", re.I)
_TIME_INDIC = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])(?::([0-5]\d))?\s*(?:बजे|ਵਜੇ)")
_UNIT = (
    r"(%|per\s?cent|percent|प्रतिशत|ਪ੍ਰਤੀਸ਼ਤ|ਫੀਸਦੀ|फीसदी"
    r"|µg/m³|μg/m³|ug/m3|µg/m3|μg/m3|micrograms?"
    r"|km²|km2|sq\.?\s?km|square\s+kilomet(?:re|er)s?"
    r"|ha\b|hectares?|हेक्टेयर|ਹੈਕਟੇਅਰ"
    r"|km/h|kmph|km\s?per\s?hour|किमी/घंटा|ਕਿਮੀ/ਘੰਟਾ|m/s"
    r"|km\b|kilomet(?:re|er)s?|किमी|ਕਿਲੋਮੀਟਰ|m\b|metres?|meters?|मीटर|ਮੀਟਰ"
    r"|°C|°|degrees?|MW|mm\b|lakh|crore|लाख|करोड़|ਲੱਖ|ਕਰੋੜ)?"
)
_NUMBER = re.compile(
    r"(?<![\w.])([-−]?)(\d{1,3}(?:,\d{2,3})*,\d{3}(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w.]*\d)\s*" + _UNIT,
    re.I,
)
_YEAR_ONLY = re.compile(r"^(19|20)\d\d$")
_SENTENCE_END = re.compile(r"[.!?।](?:\s|$)|\n")  # a sentence or line ends (। is the danda)
# Hindi is written in Devanagari, Punjabi in Gurmukhi: letters of the other script are a defect.
SCRIPTS = {"hi": (0x0900, 0x097F), "pa": (0x0A00, 0x0A7F)}


_SHARED_MARKS = {0x0964, 0x0965}  # the danda and double danda: Gurmukhi text uses them too


def _foreign_letter(ch: str, low: int, high: int) -> bool:
    """A letter, vowel sign or digit that is neither the expected script nor Latin/Greek (IST,
    CPCB, µg/m³ and English place names are fine in a Hindi or Punjabi text)."""
    code = ord(ch)
    if low <= code <= high or code < 0x0250 or 0x0370 <= code <= 0x03FF or code in _SHARED_MARKS:
        return False
    category = unicodedata.category(ch)
    return category[0] in "LM" or category == "Nd"


def foreign_script(text: str, lang: str) -> list[str]:
    """Words of `text` holding letters of another script than `lang` expects: Devanagari in
    Punjabi and Gurmukhi in Hindi, but also any other script (a live Punjabi bulletin on
    2026-10-31 wrote "quality" with two Sinhala letters)."""
    if lang not in SCRIPTS:
        return []
    low, high = SCRIPTS[lang]
    return [word for word in text.split() if any(_foreign_letter(ch, low, high) for ch in word)]


# --- claims -----------------------------------------------------------------------------------


@dataclass
class Claim:
    text: str
    kind: str  # number / time / date
    start: int
    end: int
    value: float | None = None
    decimals: int = 0
    unit: str | None = None
    minutes: tuple[int, ...] = ()  # candidate minute-of-day values (time claims)
    zone: str = "IST"
    day: int | None = None
    month: int | None = None
    year: int | None = None


def normalise_digits(text: str) -> str:
    return text.translate(_DIGITS)


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def _hour_candidates(hour: int, ampm: str | None) -> list[int]:
    marker = (ampm or "").lower().replace(".", "")
    if marker == "pm":
        return [hour % 12 + 12]
    if marker == "am":
        return [hour % 12]
    return [hour] if hour > 12 or hour == 0 else [hour, (hour + 12) % 24]


def extract_claims(text: str) -> list[Claim]:
    """Every numeric claim in `text`, in order: dates first, then times, then plain numbers
    (each pass masks what it took, so "13:20" is one time, not the numbers 13 and 20)."""
    text = normalise_digits(text)
    chars = list(text)
    for pattern in (_CITATION, *_NOT_CLAIMS):
        for m in pattern.finditer(text):
            _blank(chars, m.start(), m.end())
    work = "".join(chars)
    claims: list[Claim] = []

    def take(m: re.Match[str], claim: Claim) -> None:
        nonlocal work
        claims.append(claim)
        w = list(work)
        _blank(w, m.start(), m.end())
        work = "".join(w)

    for m in list(_ISO_DATE.finditer(work)):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            take(m, Claim(m[0], "date", m.start(), m.end(), day=d, month=mo, year=y))
    for pattern, day_group, month_group in ((_DAY_MONTH, 1, 2), (_MONTH_DAY, 2, 1)):
        for m in list(pattern.finditer(work)):
            if m[month_group] == "may":  # "25 may rise" is a verb; only "May" is the month
                continue
            day, month = int(m[day_group]), MONTHS.get(m[month_group].lower())
            if month and 1 <= day <= 31:
                year = int(m[3]) if m[3] else None
                take(m, Claim(m[0].strip(), "date", m.start(), m.end(),
                              day=day, month=month, year=year))  # fmt: skip
    for m in list(_SLASH_DATE.finditer(work)):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            take(m, Claim(m[0], "date", m.start(), m.end(), day=d, month=mo,
                          year=y if y > 99 else 2000 + y))  # fmt: skip
    for m in list(_TIME_COLON.finditer(work)):
        hours = _hour_candidates(int(m[1]), m[3])
        minute = int(m[2])
        zone = (m[4] or "IST").upper().replace("GMT", "UTC")
        take(m, Claim(m[0].strip(), "time", m.start(), m.end(),
                      minutes=tuple(h * 60 + minute for h in hours), zone=zone))  # fmt: skip
    for pattern in (_TIME_DOT, _TIME_HOUR):
        for m in list(pattern.finditer(work)):
            minute = int(m[2]) if pattern is _TIME_DOT else 0
            ampm = m[3] if pattern is _TIME_DOT else m[2]
            hours = _hour_candidates(int(m[1]), ampm)
            take(m, Claim(m[0].strip(), "time", m.start(), m.end(),
                          minutes=tuple(h * 60 + minute for h in hours)))  # fmt: skip
    for m in list(_TIME_INDIC.finditer(work)):
        minute = int(m[2] or 0)
        hours = _hour_candidates(int(m[1]), None)
        take(m, Claim(m[0].strip(), "time", m.start(), m.end(),
                      minutes=tuple(h * 60 + minute for h in hours)))  # fmt: skip
    for m in list(_NUMBER.finditer(work)):
        raw = m[2].replace(",", "")
        value = float(raw) * (-1 if m[1] else 1)
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        unit = _canonical_unit(m[3])
        if unit == "lakh":
            value, unit = value * 1e5, None
        elif unit == "crore":
            value, unit = value * 1e7, None
        # The end stops at the number (and unit) itself: `\s*` in the pattern can run over a
        # blanked-out "[13]", and the marker that follows must stay after the claim.
        end = m.start() + len(m[0].rstrip())
        claims.append(Claim(m[0].strip(), "number", m.start(), end, value=value,
                            decimals=decimals, unit=unit))  # fmt: skip
    claims.sort(key=lambda c: c.start)
    return claims


def _canonical_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    u = raw.lower().replace(" ", "")
    if u in ("%", "percent", "percent", "प्रतिशत", "ਪ੍ਰਤੀਸ਼ਤ", "ਫੀਸਦੀ", "फीसदी") or u.startswith("per"):
        return "%"
    if u.startswith(("µg", "μg", "ug", "microgram")):
        return "ugm3"
    if u.startswith(("km²", "km2", "sq", "square")):
        return "km2"
    if u.startswith(("ha", "hectare", "हेक्टेयर", "ਹੈਕਟੇਅਰ")):
        return "ha"
    if u.startswith(("km/h", "kmph", "kmper", "किमी/", "ਕਿਮੀ/")):
        return "kmh"
    if u == "m/s":
        return "ms"
    if u.startswith(("km", "kilomet", "किमी", "ਕਿਲੋਮੀਟਰ")):
        return "km"
    if u in ("m", "metre", "metres", "meter", "meters", "मीटर", "ਮੀਟਰ"):
        return "m"
    if u in ("lakh", "लाख", "ਲੱਖ"):
        return "lakh"
    if u in ("crore", "करोड़", "ਕਰੋੜ"):
        return "crore"
    return u


# --- evidence ---------------------------------------------------------------------------------


@dataclass
class Evidence:
    """Everything a claim may legitimately match: numbers, IST/UTC clock minutes, dates."""

    numbers: list[float] = field(default_factory=list)
    minutes_ist: set[int] = field(default_factory=set)
    minutes_utc: set[int] = field(default_factory=set)
    dates: set[date] = field(default_factory=set)

    def add_text(self, text: str) -> None:
        """Numbers, times and dates written inside a string (an IST time, a note, a question)."""
        for claim in extract_claims(text):
            if claim.kind == "number" and claim.value is not None:
                self.numbers.append(abs(claim.value))
            elif claim.kind == "time":
                bucket = self.minutes_utc if claim.zone == "UTC" else self.minutes_ist
                bucket.update(claim.minutes[:1])  # an exact time in data has one reading
            elif claim.kind == "date" and claim.day and claim.month:
                year = claim.year or 2000
                try:
                    self.dates.add(date(year, claim.month, claim.day))
                except ValueError:
                    pass

    def add_time(self, when: datetime) -> None:
        """A timestamp counts as its IST clock time, its UTC clock time and both dates."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)  # the codebase convention: naive means UTC
        local, utc = when.astimezone(IST), when.astimezone(UTC)
        self.minutes_ist.add(local.hour * 60 + local.minute)
        self.minutes_utc.add(utc.hour * 60 + utc.minute)
        self.dates.update({local.date(), utc.date()})

    def add_value(self, value: Any) -> None:
        """Walk a JSON value: numbers, strings, list lengths, digits in keys."""
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            if math.isfinite(value):
                self.numbers.append(abs(float(value)))
            return
        if isinstance(value, str):
            when = _iso_time(value)
            if when is not None:
                self.add_time(when)
            else:
                self.add_text(value)
            return
        if isinstance(value, dict):
            self.numbers.append(float(len(value)))
            for key, item in value.items():
                for digits in re.findall(r"\d+(?:\.\d+)?", str(key)):
                    self.numbers.append(float(digits))
                self.add_value(item)
            return
        if isinstance(value, (list, tuple)):
            self.numbers.append(float(len(value)))
            for item in value:
                self.add_value(item)


def _iso_time(value: str) -> datetime | None:
    if len(value) < 16 or value[4] != "-" or "T" not in value[:11]:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def build_evidence(
    payloads: list[Any], *, question: str = "", now: datetime | None = None, extra: str = ""
) -> Evidence:
    """Evidence from tool outputs and arguments, the question, the clock and reference text."""
    evidence = Evidence()
    for payload in payloads:
        evidence.add_value(payload)
    if question:
        evidence.add_text(question)
    if extra:
        evidence.add_text(extra)
    if now is not None:
        local = now.astimezone(IST)
        evidence.add_time(now)
        evidence.dates.update({local.date(), local.date() - timedelta(days=1)})
        evidence.numbers.append(float(local.year))
    return evidence


# --- matching ---------------------------------------------------------------------------------


def _tolerance(claim: Claim) -> float:
    value = abs(claim.value or 0.0)
    tol = max(value * REL_TOL, 0.5 * 10 ** (-claim.decimals))
    if claim.decimals == 0 and value >= 10:
        digits = str(int(value))
        trailing = len(digits) - len(digits.rstrip("0"))
        if trailing:  # a round number may be a rounded one
            tol = max(tol, min(0.5 * 10**trailing, ROUND_NUMBER_MAX_REL * value))
    return tol


def _conversions(unit: str | None, p: float) -> tuple[float, ...]:
    if unit == "%":
        return (p, p * 100) if p <= 1 else (p,)
    if unit == "kmh":
        return (p, p * 3.6)
    if unit == "ms":
        return (p, p / 3.6)
    if unit == "km2":
        return (p, p / 100)
    if unit == "ha":
        return (p, p * 100)
    if unit == "km":
        return (p, p / 1000)
    if unit == "m":
        return (p, p * 1000)
    return (p,)


def number_supported(claim: Claim, evidence: Evidence) -> bool:
    value = abs(claim.value or 0.0)
    tol = _tolerance(claim)
    return any(
        abs(candidate - value) <= tol
        for p in evidence.numbers
        for candidate in _conversions(claim.unit, p)
    )


def _close_minutes(a: int, b: int) -> bool:
    diff = abs(a - b) % (24 * 60)
    return min(diff, 24 * 60 - diff) <= TIME_TOL_MIN


def time_supported(claim: Claim, evidence: Evidence) -> bool:
    pool = evidence.minutes_utc if claim.zone == "UTC" else evidence.minutes_ist
    if any(_close_minutes(m, p) for m in claim.minutes for p in pool):
        return True
    # A whole hour may come from an hour-of-day bin ("13" in detections_by_ist_hour).
    return any(m % 60 == 0 and float(m // 60) in evidence.numbers for m in claim.minutes)


def date_supported(claim: Claim, evidence: Evidence) -> bool:
    return any(
        d.day == claim.day and d.month == claim.month and claim.year in (None, d.year)
        for d in evidence.dates
    )


def supported(claim: Claim, evidence: Evidence) -> bool:
    if claim.kind == "time":
        return time_supported(claim, evidence)
    if claim.kind == "date":
        return date_supported(claim, evidence)
    return number_supported(claim, evidence)


# --- report -----------------------------------------------------------------------------------


@dataclass
class Report:
    checked: int
    verified: int
    unverified: list[str]
    passed: bool
    uncited: list[str] = field(default_factory=list)
    bad_markers: list[int] = field(default_factory=list)

    def as_event(self) -> dict[str, Any]:
        """The `verification` SSE event fields (uncited and bad_markers are additive)."""
        return {
            "checked": self.checked,
            "verified": self.verified,
            "unverified": self.unverified,
            "passed": self.passed,
            "uncited": self.uncited,
            "bad_markers": self.bad_markers,
        }


def cited_markers(text: str) -> list[int]:
    """Markers used in the text, in order of first use: [3], [1, 2], [2-4] and [2][5]."""
    found: list[int] = []
    for m in _CITATION.finditer(normalise_digits(text)):
        body = m[0].strip("[] ")
        for part in re.split(r"\s*,\s*", body):
            if re.fullmatch(r"\d+\s*[-–]\s*\d+", part):
                a, b = (int(x) for x in re.split(r"\s*[-–]\s*", part))
                found.extend(range(a, b + 1) if 0 < b - a < 20 else [a, b])
            elif part.isdigit():
                found.append(int(part))
    return list(dict.fromkeys(found))


def _snippet(text: str, start: int, end: int, width: int = 40) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    piece = " ".join(text[left:right].split())
    return ("…" if left else "") + piece + ("…" if right < len(text) else "")


def _markers_after(text: str, end: int) -> list[int]:
    """The citation markers that follow a claim before its sentence ends ([] when none)."""
    tail = text[end : end + 200]
    stop = _SENTENCE_END.search(tail)
    window = tail[: stop.start() + 1] if stop else tail
    return cited_markers(window)


@dataclass
class Attributed:
    """One tool output as evidence, with the citation markers of its sources."""

    evidence: Evidence
    markers: set[int]


def verify(
    text: str,
    evidence: Evidence,
    known_markers: set[int] | None = None,
    by_source: list[Attributed] | None = None,
) -> Report:
    """Check every numeric claim in `text` against `evidence`, every marker against the known
    sources and, given `by_source`, that each cited number comes from a tool whose sources carry
    the marker it cites (so "72 µg/m³ [4]" fails when [4] is the wind data, not the air data)."""
    normalised = normalise_digits(text)
    claims = extract_claims(text)
    unverified: list[str] = []
    uncited: list[str] = []
    verified = 0
    for claim in claims:
        if claim.kind == "number" and claim.value is not None and _YEAR_ONLY.match(claim.text):
            ok = claim.value in evidence.numbers or any(d.year == claim.value for d in evidence.dates)
        else:
            ok = supported(claim, evidence)
        cited = _markers_after(normalised, claim.end)
        if ok and by_source and cited and claim.kind == "number":
            holders = set().union(*(a.markers for a in by_source if supported(claim, a.evidence)))
            if holders and not holders & set(cited):
                ok = False
                where = ", ".join(f"[{m}]" for m in sorted(holders))
                unverified.append(f"{claim.text} cites {', '.join(f'[{m}]' for m in cited)} "
                                  f"but comes from {where}")  # fmt: skip
                continue
        if ok:
            verified += 1
        else:
            unverified.append(f'{claim.text} ("{_snippet(normalised, claim.start, claim.end)}")')
        if claim.kind == "number" and not cited:
            uncited.append(claim.text)
    bad = []
    if known_markers is not None:
        bad = [m for m in cited_markers(text) if m not in known_markers]
        unverified.extend(f"[{m}] cites no source" for m in bad)
    return Report(
        checked=len(claims),
        verified=verified,
        unverified=unverified,
        passed=not unverified,
        uncited=uncited,
        bad_markers=bad,
    )
