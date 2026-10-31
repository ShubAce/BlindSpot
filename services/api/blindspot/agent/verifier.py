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
        # The season's months also in their Hindi and Punjabi abbreviations ("31 अक्टू 03:00 IST"):
        # without them every dated Hindi sentence failed the number check and was dropped. Short
        # forms that are also ordinary words (जन) are left out.
        "september sept sep सितंबर सितम्बर सितं ਸਤੰਬਰ ਸਿਤੰਬਰ ਸਤੰ",
        "october oct अक्टूबर अक्तूबर अक्टू अक्तू ਅਕਤੂਬਰ ਅਕਤੂ",
        "november nov नवंबर नवम्बर नवं ਨਵੰਬਰ ਨਵੰ",
        "december dec दिसंबर दिसम्बर दिसं ਦਸੰਬਰ ਦਸੰ",
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


# --- honesty: candidate unseen burns, audit precision, risk scores, backtest AUC, fire detections ---
#
# The number check above proves a number came from a tool; it cannot tell whether the sentence
# around it overclaims. Ways the bulletin did (2026-10-31): it stated the candidate unseen share
# and area as fact, without the audit precision and its interval; it quoted raw burn-risk scores
# ("risk scores of 100.0", a relative index, read as certainty); it called FIRMS detections
# "crop-residue fires" (they are thermal anomalies: most, not all, are crop fires); and it gave the
# AUC of one pass pair's village ranking as "backtest validation". This check finds such sentences in
# any language by their numbers: the unseen shares and areas, the audit precision, the risk scores,
# the AUCs and the FIRMS counts are taken from the tool outputs, so nothing is typed here.
#
# A number that merely sits near one of those (a humidity of 75% next to a district's 74.8% unseen
# share, "98% of the detections" next to Ludhiana's 97.7%) is not a claim about them: an unseen
# figure must come with words saying it is about burns no satellite saw (or repeat an unseen figure
# to its last decimal), and a risk score must not be a count ("88 harvested fields", "100 villages").

CANDIDATE_WORDS: dict[str, tuple[str, ...]] = {
    "en": ("candidate",),
    "hi": ("संभावित", "उम्मीदवार"),
    "pa": ("ਸੰਭਾਵੀ", "ਸੰਭਾਵਿਤ", "ਉਮੀਦਵਾਰ"),
}
# Words that make a sentence about burns the satellites did not see (the candidate words count too).
UNSEEN_CUES: dict[str, tuple[str, ...]] = {
    "en": ("unseen", "missed", "miss ", "undetected", "unnoticed", "no fire", "without a fire", "without fire",
           "without any fire", "no matching", "no satellite", "not detected", "not seen", "never detected",
           "no alert", "no detection", "hidden", "invisible", "escaped"),
    "hi": ("अनदेख", "छूट", "बिना", "दर्ज नहीं", "नहीं देख", "नहीं पकड़", "पकड़ में नहीं", "छिप"),
    "pa": ("ਅਣਦੇਖ", "ਖੁੰਝ", "ਬਿਨਾਂ", "ਦਰਜ ਨਹੀਂ", "ਨਹੀਂ ਦੇਖ", "ਨਹੀਂ ਫੜ", "ਛੁਪ"),
}
RISK_WORDS: dict[str, tuple[str, ...]] = {
    "en": ("risk", "score"),
    "hi": ("जोखिम", "स्कोर", "खतरा", "ख़तरा"),
    "pa": ("ਜੋਖਮ", "ਜੋਖ਼ਮ", "ਸਕੋਰ", "ਖ਼ਤਰ", "ਖਤਰ"),
}
# A number followed (within two words) by one of these counts things; it is not a score.
COUNT_NOUNS: tuple[str, ...] = (
    "field", "village", "farm", "plot", "district", "pair", "pass", "day", "hour", "fire", "detection",
    "hectare", "scar", "tile", "time", "year", "town", "cluster", "km", "percent",
    "खेत", "गांव", "गाँव", "ज़िल", "जिल", "दिन", "घंट", "आग", "बार", "किमी", "शहर",
    "ਖੇਤ", "ਪਿੰਡ", "ਜ਼ਿਲ", "ਜਿਲ", "ਦਿਨ", "ਘੰਟ", "ਅੱਗ", "ਵਾਰ", "ਕਿਮੀ", "ਸ਼ਹਿਰ",
)
AUC_WORDS: tuple[str, ...] = ("auc", "area under", "एयूसी", "ਏਯੂਸੀ")
_UNSEEN_SHARE_KEYS = {"unseen_share", "unseen_share_pct"}
_RISK_KEYS = {"risk_score", "max_risk_score", "risk_weighted_ha"}
_CROP = (r"(?:crop(?:[- ](?:residue|burning))?|stubble(?:[- ]burning)?|paddy(?:[- ](?:straw|residue))?|straw|farm"
         r"|residue(?:[- ]burning)?|parali)")
_FIRMS_AS_CROP_FIRES = re.compile(
    rf"(?<![\w.])(\d[\d,]*)\s+(?:(?:active|new|fresh|more)\s+)?{_CROP}(?:[- ]burning)?\s+"
    r"(?:fires?|incidents?|cases?|events?)\b", re.I)
# Hindi and Punjabi word order differs ("ਫਸਲ ਦੇ ਰਹਿੰਦ-ਖੂੰਹਦ ਨੂੰ ਅੱਗ ਲਗਾਉਣ ਦੇ 232 ਮਾਮਲਿਆਂ"): a sentence with a
# FIRMS count, a crop word and a fire/case word, and no word for "detection", calls detections crop fires.
_CROP_WORDS = {"hi": ("पराली", "फसल", "फ़सल", "अवशेष", "ठूंठ"), "pa": ("ਪਰਾਲੀ", "ਰਹਿੰਦ", "ਫਸਲ", "ਫ਼ਸਲ", "ਨਾੜ")}
_FIRE_WORDS = {"hi": ("आग", "मामल", "मामले", "घटना"), "pa": ("ਅੱਗ", "ਮਾਮਲ", "ਘਟਨਾ")}
_DETECTION_WORDS = {"hi": ("पहचान", "डिटेक्शन", "थर्मल"), "pa": ("ਪਛਾਣ", "ਡਿਟੈਕਸ਼ਨ", "ਥਰਮਲ", "ਖੋਜ")}
# "95% CI" names the interval; it is not a bound of it
_CI_NAME = re.compile(r"95\s?(?:%|प्रतिशत|ਪ੍ਰਤੀਸ਼ਤ)\s*(?:CI\b|confidence|विश्वास|ਭਰੋਸੇ|ਵਿਸ਼ਵਾਸ)", re.I)
# "67.9-95.5%": both ends are percentages
_PCT_RANGE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(%|प्रतिशत|ਪ੍ਰਤੀਸ਼ਤ)")
_SENTENCES = re.compile(r"((?<=[.!?।])[ \t]+|\s*\n\s*)")  # sentences and lines (bullets); split() keeps them
_BULLET = re.compile(r"^\s*[-•*]\s+")
_WORD = re.compile(r"[^\s\d.,;:!?।()\[\]]+")
_CLAUSE_END = re.compile(r"[.,;:!?।(\[]")


def _sentences(unit: str) -> list[str]:
    return [p for i, p in enumerate(_SENTENCES.split(unit)) if i % 2 == 0 and p.strip()]


def _prep(text: str) -> str:
    """The text the honesty check reads: digits normalised, "95% CI" masked, "a-b%" as "a% - b%"."""
    text = _CI_NAME.sub(" ", normalise_digits(text))
    return _PCT_RANGE.sub(lambda m: f"{m[1]}{m[3]} – {m[2]}{m[3]}", text)


@dataclass
class HonestyFacts:
    """What the honesty check needs from the tool outputs (all numbers as the tools gave them)."""

    unseen_share_pct: list[float] = field(default_factory=list)
    unseen_area_ha: list[float] = field(default_factory=list)
    risk_scores: list[float] = field(default_factory=list)
    precision: float | None = None  # audit: share of candidate unseen burns that were real burns
    precision_ci: tuple[float, float] | None = None
    audited: int | None = None
    season: dict[str, Any] | None = None  # {area_ha, unseen_area_ha, unseen_share_pct, markers}
    audit_markers: list[int] = field(default_factory=list)
    top_village: dict[str, Any] | None = None  # {village, district, markers}
    # the backtest over all pass pairs: {auc, ci (lo, hi) or None, pairs, strength, markers}
    auc_all: dict[str, Any] | None = None
    auc_other: list[float] = field(default_factory=list)  # one pair's, the villages', baselines', per pair
    firms_counts: list[float] = field(default_factory=list)  # FIRMS detection counts (not CREAMS events)


def _markers_of(block: dict[str, Any]) -> list[int] | None:
    sources = block.get("sources")
    if not isinstance(sources, list):
        return None
    return [int(m) for s in sources if isinstance(s, dict) for m in re.findall(r"\d+", str(s.get("marker")))]


def honesty_facts(payloads: list[Any]) -> HonestyFacts:
    """Walk the tool outputs for the unseen shares and areas, the audit precision, the season's
    unseen totals, the risk scores, the top-ranked village, the backtest's AUCs and the FIRMS
    counts (with the markers of their sources)."""
    facts = HonestyFacts()

    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

    def pair(v: Any) -> tuple[float, float] | None:
        if isinstance(v, list) and len(v) == 2 and all(num(x) is not None for x in v):
            return float(v[0]), float(v[1])
        return None

    def walk(value: Any, path: tuple[str, ...], markers: list[int]) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, path, markers)
            return
        if not isinstance(value, dict):
            return
        markers = _markers_of(value) or markers
        if num(value.get("unseen_area_ha")) is not None:
            share = num(value.get("unseen_share_pct"))
            if share is None and num(value.get("unseen_share")) is not None:
                share = num(value["unseen_share"]) * (100 if num(value["unseen_share"]) <= 1 else 1)
            area = num(value.get("area_ha")) or 0.0
            if share is not None and (facts.season is None or area > facts.season["area_ha"]):
                facts.season = {"area_ha": area, "unseen_area_ha": num(value["unseen_area_ha"]),
                                "unseen_share_pct": share, "markers": markers}
        p, ci = num(value.get("precision")), value.get("precision_ci95")
        if (p is not None and isinstance(ci, list) and len(ci) == 2 and "unseen" in " ".join(path)
                and facts.precision is None):
            facts.precision, facts.precision_ci = p, (float(ci[0]), float(ci[1]))
            n = value.get("audited", value.get("precision_n"))
            facts.audited = int(n) if isinstance(n, (int, float)) else None
            facts.audit_markers = markers
        ranking = value.get("village_ranking") or value.get("ranking")
        if facts.top_village is None and isinstance(ranking, list) and ranking and isinstance(ranking[0], dict) \
                and ranking[0].get("village"):
            facts.top_village = {"village": ranking[0]["village"], "district": ranking[0].get("district"),
                                 "markers": markers}
        all_pairs = value.get("all_pairs")
        if isinstance(all_pairs, dict) and num(all_pairs.get("auc_weighted_mean")) is not None \
                and facts.auc_all is None:
            facts.auc_all = {"auc": num(all_pairs["auc_weighted_mean"]), "ci": pair(all_pairs.get("auc_ci95")),
                             "pairs": all_pairs.get("pairs"), "markers": markers,
                             "strength": value.get("strength") if isinstance(value.get("strength"), str) else None}
        creams = any("creams" in p for p in path)
        for key, item in value.items():
            x = num(item)
            if x is not None:
                if key == "auc" and 0 < x <= 1:  # one pair's, the villages' or a baseline's AUC
                    facts.auc_other.append(x)
                if not creams and (key in ("detections", "detections_total") or path[-1:] == ("by_state",)):
                    facts.firms_counts.append(x)
                if x <= 0:  # "0.0 ha of candidate unseen burns" claims nothing
                    continue
                if key == "unseen_area_ha":
                    facts.unseen_area_ha.append(x)
                elif key in _UNSEEN_SHARE_KEYS:
                    facts.unseen_share_pct.append(x * 100 if key == "unseen_share" and x <= 1 else x)
                elif key in _RISK_KEYS or (key == "score" and "risk_factors" in path):
                    facts.risk_scores.append(x)
            elif key == "auc_ci95" and pair(item) and "auc_weighted_mean" not in value:
                facts.auc_other.extend(pair(item) or ())
            else:
                walk(item, (*path, str(key)), markers)

    for payload in payloads:
        walk(payload, (), [])
    return facts


@dataclass
class HonestyIssue:
    unit: int  # which text unit (title, summary, a section body, or the answer)
    sentence: str
    # unseen_not_candidate / unseen_without_precision / raw_risk_score / firms_called_crop_fires /
    # auc_not_all_pairs / auc_without_interval
    kind: str

    def message(self, facts: HonestyFacts) -> str:
        quoted = f'"{" ".join(self.sentence.split())[:160]}"'
        a = facts.auc_all or {}
        ci = a.get("ci")
        all_pairs = (f"the AUC over all {a.get('pairs')} pass pairs, {a.get('auc')}"
                     + (f" (95% CI {ci[0]}-{ci[1]})" if ci else "")) if a else None
        if self.kind == "unseen_not_candidate":
            return f"{quoted} states a share or area of unseen burns without calling them candidate unseen burns"
        if self.kind == "unseen_without_precision":
            if facts.precision is None:
                return f"{quoted} states a share or area of candidate unseen burns, but no audit precision is available: leave the number out"
            lo, hi = facts.precision_ci or (None, None)
            return (f"{quoted} states a share or area of candidate unseen burns without the audit precision "
                    f"{facts.precision} and its 95% interval {lo}-{hi} from the tool output (and that the share is an upper bound)")
        if self.kind == "raw_risk_score":
            return f"{quoted} quotes a raw burn-risk score: it is a relative index, not a probability; name the top-ranked villages instead"
        if self.kind == "auc_not_all_pairs":
            return (f"{quoted} gives the AUC of one pass pair, of the village ranking or of a baseline as the "
                    f"ranking's accuracy: " + (f"use {all_pairs} instead" if all_pairs else "leave it out"))
        if self.kind == "auc_without_interval":
            return f"{quoted} gives the all-pairs AUC without its 95% interval: write {all_pairs}"
        return f"{quoted} calls FIRMS detections crop-residue fires: they are fire detections (thermal anomalies), most but not all crop fires"


def _pool(numbers: list[float]) -> Evidence:
    return Evidence(numbers=list(numbers))


def _has(claims: list[Claim], numbers: list[float], units: tuple[str | None, ...] | None = None) -> bool:
    pool = _pool(numbers)
    return any(c.kind == "number" and (units is None or c.unit in units) and number_supported(c, pool) for c in claims)


def _gap(claim: Claim, numbers: list[float]) -> float:
    value = abs(claim.value or 0.0)
    return min((abs(c - value) for p in numbers for c in _conversions(claim.unit, p)), default=math.inf)


def _is_count(text: str, claim: Claim) -> bool:
    """"88 harvested fields", "100 villages", "12 दिन": the number counts things, it is not a score."""
    after = text[claim.end:claim.end + 60]
    stop = _CLAUSE_END.search(after)
    words = _WORD.findall(after[:stop.start()] if stop else after)[:2]
    return any(w.lower().startswith(noun) for w in words for noun in COUNT_NOUNS)


def _unseen_hits(claims: list[Claim], facts: HonestyFacts) -> tuple[list[Claim], bool]:
    """The claims that state an unseen share or area (closer to it than to an audit number), and
    whether one repeats an unseen figure to its last decimal."""
    audit = [v for v in (facts.precision, *(facts.precision_ci or ())) if v is not None]
    hits, exact = [], False
    for c in claims:
        if c.kind != "number":
            continue
        pool = facts.unseen_share_pct if c.unit == "%" else facts.unseen_area_ha if c.unit in ("ha", "km2") else []
        if not pool or not number_supported(c, _pool(pool)):
            continue
        gap = _gap(c, pool)
        if audit and _gap(c, audit) <= gap:
            continue  # the audit's own precision or interval
        hits.append(c)
        exact = exact or (c.decimals >= 1 and gap <= 0.5 * 10 ** (-c.decimals) + 1e-9)
    return hits, exact


def _firms_crop_claim(text: str, lower: str, claims: list[Claim], lang: str, facts: HonestyFacts) -> bool:
    if lang == "en" or lang not in _CROP_WORDS:
        for m in _FIRMS_AS_CROP_FIRES.finditer(text):
            value = float(m[1].replace(",", ""))
            if not facts.firms_counts or any(abs(value - x) <= max(0.5, REL_TOL * x) for x in facts.firms_counts):
                return True
        return False
    if not facts.firms_counts or not any(w in lower for w in _CROP_WORDS[lang]) \
            or not any(w in lower for w in _FIRE_WORDS[lang]) or any(w in lower for w in _DETECTION_WORDS[lang]):
        return False
    pool = _pool(facts.firms_counts)
    return any(c.kind == "number" and c.unit is None and (c.value or 0) >= 1 and number_supported(c, pool)
               for c in claims)


def _auc_issue(claims: list[Claim], unit_claims: list[Claim], facts: HonestyFacts) -> str | None:
    """auc_not_all_pairs when an AUC of the tool outputs other than the all-pairs one is given (as the
    ranking's accuracy); auc_without_interval when the all-pairs AUC comes without its interval."""
    values = [c for c in claims if c.kind == "number" and c.unit is None and 0 < (c.value or 0) <= 1]
    if not values:
        return None
    a = facts.auc_all or {}
    ci = a.get("ci")
    allowed = [v for v in (a.get("auc"), *(ci or ())) if v is not None] + [0.5, 1.0]  # 0.5 chance, 1 perfect
    if facts.auc_other and any(number_supported(c, _pool(facts.auc_other))
                               and not number_supported(c, _pool(allowed)) for c in values):
        return "auc_not_all_pairs"
    if a.get("auc") is not None and ci and any(number_supported(c, _pool([a["auc"]])) for c in values) \
            and not all(_has(unit_claims, [v], (None,)) for v in ci):
        return "auc_without_interval"
    return None


def check_honesty(units: list[str], lang: str, facts: HonestyFacts) -> list[HonestyIssue]:
    """Every sentence of `units` that overclaims (see the section comment); [] when all is well."""
    issues: list[HonestyIssue] = []
    cand_words = CANDIDATE_WORDS.get(lang, CANDIDATE_WORDS["en"]) + CANDIDATE_WORDS["en"]
    cues = cand_words + UNSEEN_CUES.get(lang, ()) + UNSEEN_CUES["en"]
    risk_words = RISK_WORDS.get(lang, RISK_WORDS["en"]) + RISK_WORDS["en"]
    for index, unit in enumerate(units):
        unit_claims = extract_claims(_prep(unit))
        qualified = facts.precision is not None and facts.precision_ci is not None and all(
            _has(unit_claims, [v], ("%", None)) for v in (facts.precision, *facts.precision_ci))
        for sentence in _sentences(unit):
            text = _prep(sentence)
            claims = extract_claims(text)
            lower = sentence.lower()
            hits, exact = _unseen_hits(claims, facts)
            if hits and (exact or any(w in lower for w in cues)):
                if not any(w in lower for w in cand_words):
                    issues.append(HonestyIssue(index, sentence, "unseen_not_candidate"))
                elif not qualified:
                    issues.append(HonestyIssue(index, sentence, "unseen_without_precision"))
            if facts.risk_scores and any(w in lower for w in risk_words) and any(
                    c.kind == "number" and c.unit in (None, "ha") and (c.value or 0) > 1
                    and number_supported(c, _pool(facts.risk_scores)) and not _is_count(text, c) for c in claims):
                issues.append(HonestyIssue(index, sentence, "raw_risk_score"))
            if any(w in lower for w in AUC_WORDS) and (kind := _auc_issue(claims, unit_claims, facts)):
                issues.append(HonestyIssue(index, sentence, kind))
            if _firms_crop_claim(text, lower, claims, lang, facts):
                issues.append(HonestyIssue(index, sentence, "firms_called_crop_fires"))
    return issues


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%" if x <= 1 else f"{x:.0f}%"


def _pct1(x: float) -> str:
    return f"{x * 100:.1f}%" if x <= 1 else f"{x:.1f}%"


def _cite(markers: list[int]) -> str:
    return f" [{markers[0]}]" if markers else ""


def unseen_sentence(lang: str, facts: HonestyFacts) -> str | None:
    """The candidate unseen burns of the season with the audit precision, in `lang`, every number
    from the tool outputs; None when either is missing (then the share is not stated at all)."""
    s, p, ci = facts.season, facts.precision, facts.precision_ci
    if not s or p is None or ci is None:
        return None
    area, share, n = f"{s['unseen_area_ha']:.0f}", f"{s['unseen_share_pct']:.1f}%", facts.audited
    a, b = _cite(s["markers"]), _cite(facts.audit_markers or s["markers"])
    audit = f"{n} " if n else ""
    if lang == "hi":
        return (f"संभावित अनदेखी आग (ऐसे जले हुए खेत जिनके लिए किसी उपग्रह ने कोई आग दर्ज नहीं की) {area} हेक्टेयर में हैं, "
                f"जो मानचित्र पर दर्ज जले क्षेत्र का {share} है{a}। यह हिस्सा ऊपरी सीमा है: {audit}संभावित मामलों की जाँच में "
                f"{_pct(p)} सचमुच जले हुए निकले (95% CI {_pct(ci[0])}-{_pct(ci[1])}){b}।")
    if lang == "pa":
        return (f"ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ (ਸੜੇ ਹੋਏ ਖੇਤ ਜਿਨ੍ਹਾਂ ਲਈ ਕਿਸੇ ਉਪਗ੍ਰਹਿ ਨੇ ਕੋਈ ਅੱਗ ਦਰਜ ਨਹੀਂ ਕੀਤੀ) {area} ਹੈਕਟੇਅਰ ਵਿੱਚ ਹਨ, "
                f"ਜੋ ਨਕਸ਼ੇ ਵਿੱਚ ਦਰਜ ਸੜੇ ਖੇਤਰ ਦਾ {share} ਹੈ{a}। ਇਹ ਹਿੱਸਾ ਉਪਰਲੀ ਹੱਦ ਹੈ: {audit}ਸੰਭਾਵੀ ਮਾਮਲਿਆਂ ਦੀ ਜਾਂਚ ਵਿੱਚ "
                f"{_pct(p)} ਸੱਚਮੁੱਚ ਸੜੇ ਹੋਏ ਨਿਕਲੇ (95% CI {_pct(ci[0])}-{_pct(ci[1])}){b}।")
    return (f"Candidate unseen burns (burn scars with no satellite fire detection) cover {area} ha, {share} of the "
            f"mapped burn-scar area{a}. That share is an upper bound: in an audit of {audit}candidates, {_pct(p)} were "
            f"real burns (95% CI {_pct(ci[0])}-{_pct(ci[1])}){b}.")


def audit_sentence(lang: str, facts: HonestyFacts) -> str | None:
    """The audit precision alone, to follow a sentence that already names candidate unseen burns
    (an investigator's district figure keeps its number and gains its qualification)."""
    p, ci, n = facts.precision, facts.precision_ci, facts.audited
    if p is None or ci is None:
        return None
    c = _cite(facts.audit_markers or (facts.season or {}).get("markers") or [])
    lo, hi = _pct1(ci[0]), _pct1(ci[1])
    if lang == "hi":
        return (f"{f'{n} ' if n else ''}संभावित अनदेखी आग की जाँच में {_pct(p)} सचमुच जले हुए खेत निकले (95% CI {lo}-{hi}), "
                f"इसलिए संभावित अनदेखी आग के आँकड़े ऊपरी सीमा हैं{c}।")
    if lang == "pa":
        return (f"{f'{n} ' if n else ''}ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ ਦੀ ਜਾਂਚ ਵਿੱਚ {_pct(p)} ਸੱਚਮੁੱਚ ਸੜੇ ਹੋਏ ਖੇਤ ਨਿਕਲੇ "
                f"(95% CI {lo}-{hi}), ਇਸ ਲਈ ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ ਦੇ ਅੰਕੜੇ ਉਪਰਲੀ ਹੱਦ ਹਨ{c}।")
    return (f"In an audit of {f'{n} ' if n else ''}candidate unseen burns, {_pct(p)} were real burns (95% CI {lo}-{hi}), "
            f"so candidate unseen figures are upper bounds{c}.")


def risk_sentence(lang: str, facts: HonestyFacts) -> str | None:
    """The top-ranked village for burning next, without its score; None without a ranking."""
    v = facts.top_village
    if not v:
        return None
    # census village names are cut at 40 characters upstream: drop a dangling "( R"
    village = re.sub(r"\s*\([^)]*$", "", str(v["village"])).strip(" ,") or str(v["village"])
    place = f"{village} ({v['district']})" if v.get("district") else village
    c = _cite(v["markers"])
    if lang == "hi":
        return (f"BlindSpot की जलने के जोखिम की रैंकिंग, जो संभावना नहीं बल्कि एक सापेक्ष सूचकांक है, अभी तक न जले कटे खेतों में "
                f"{place} को सबसे ऊपर रखती है{c}।")
    if lang == "pa":
        return (f"BlindSpot ਦੀ ਸੜਨ ਦੇ ਜੋਖਮ ਦੀ ਦਰਜਾਬੰਦੀ, ਜੋ ਸੰਭਾਵਨਾ ਨਹੀਂ ਸਗੋਂ ਇੱਕ ਸਾਪੇਖ ਸੂਚਕ ਹੈ, ਅਜੇ ਤੱਕ ਨਾ ਸੜੇ ਵੱਢੇ ਖੇਤਾਂ ਵਿੱਚ "
                f"{place} ਨੂੰ ਸਭ ਤੋਂ ਉੱਪਰ ਰੱਖਦੀ ਹੈ{c}।")
    return (f"BlindSpot's burn-risk ranking, a relative index and not a probability, puts {place} first among "
            f"harvested fields not yet burnt{c}.")


def auc_sentence(lang: str, facts: HonestyFacts) -> str | None:
    """How well the ranking did over all pass pairs (the AUC, its interval, the number of pairs);
    None without the all-pairs backtest."""
    a = facts.auc_all
    if not a or a.get("auc") is None:
        return None
    c, ci, pairs = _cite(a.get("markers") or []), a.get("ci"), a.get("pairs")
    auc = f"{a['auc']:.2f}"
    chance = ci is not None and ci[0] <= 0.5
    over = f"{pairs} " if pairs else ""
    if lang == "hi":
        interval = f"95% CI {ci[0]:.2f}-{ci[1]:.2f}; " if ci else ""
        tail = ", यानी यह संयोग से साफ़ तौर पर बेहतर नहीं है" if chance else ""
        return (f"इस मौसम के जले खेतों पर {over}पास-जोड़ियों में जाँचने पर जोखिम रैंकिंग का AUC {auc} है "
                f"({interval}0.5 संयोग के बराबर है){tail}{c}।")
    if lang == "pa":
        interval = f"95% CI {ci[0]:.2f}-{ci[1]:.2f}; " if ci else ""
        tail = ", ਭਾਵ ਇਹ ਸੰਜੋਗ ਨਾਲੋਂ ਸਾਫ਼ ਤੌਰ ਤੇ ਬਿਹਤਰ ਨਹੀਂ ਹੈ" if chance else ""
        return (f"ਇਸ ਮੌਸਮ ਦੇ ਸੜੇ ਖੇਤਾਂ ਉੱਤੇ {over}ਪਾਸ-ਜੋੜੀਆਂ ਵਿੱਚ ਜਾਂਚਣ ਤੇ ਜੋਖਮ ਦਰਜਾਬੰਦੀ ਦਾ AUC {auc} ਹੈ "
                f"({interval}0.5 ਸੰਜੋਗ ਦੇ ਬਰਾਬਰ ਹੈ){tail}{c}।")
    interval = f"95% CI {ci[0]:.2f}-{ci[1]:.2f}; " if ci else ""
    tail = ", so it is not clearly better than chance" if chance else ""
    return (f"Checked against this season's burns over {over}pass pairs, the burn-risk ranking's AUC is {auc} "
            f"({interval}0.5 is chance){tail}{c}.")


def firms_sentence(lang: str, sentence: str, facts: HonestyFacts) -> str | None:
    """A sentence that called FIRMS detections crop fires, said as fire detections: English keeps the
    sentence and swaps the words; Hindi and Punjabi say the count again with its own markers."""
    if lang not in ("hi", "pa"):
        return _FIRMS_AS_CROP_FIRES.sub(lambda m: f"{m[1]} fire detections", sentence)
    pool = _pool(facts.firms_counts)
    count = next((c for c in extract_claims(_prep(sentence)) if c.kind == "number" and c.unit is None
                  and (c.value or 0) >= 1 and number_supported(c, pool)), None)
    if count is None:
        return None
    n = count.text
    cites = "".join(f" {m}" for m in _CITATION.findall(sentence))
    if lang == "hi":
        return (f"उपग्रहों ने {n} आग की पहचान (थर्मल विसंगतियाँ) दर्ज कीं, जिनमें ज़्यादातर, पर सभी नहीं, "
                f"फसल अवशेष की आग होती हैं{cites}।")
    return (f"ਉਪਗ੍ਰਹਿਆਂ ਨੇ {n} ਅੱਗ ਦੀਆਂ ਪਛਾਣਾਂ (ਥਰਮਲ ਵਿਗਾੜ) ਦਰਜ ਕੀਤੀਆਂ, ਜਿਨ੍ਹਾਂ ਵਿੱਚੋਂ ਜ਼ਿਆਦਾਤਰ, ਪਰ ਸਾਰੀਆਂ ਨਹੀਂ, "
            f"ਫਸਲ ਦੀ ਰਹਿੰਦ-ਖੂੰਹਦ ਦੀਆਂ ਅੱਗਾਂ ਹੁੰਦੀਆਂ ਹਨ{cites}।")


_LABEL = {"en": "candidate unseen burns", "hi": "संभावित अनदेखी आग", "pa": "ਸੰਭਾਵੀ ਅਣਦੇਖੀਆਂ ਅੱਗਾਂ"}
_TAIL = re.compile(r"(?:\s*\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\])*\s*[.!?।]?\s*$")


def _labelled(sentence: str, lang: str) -> str:
    """The sentence with "(candidate unseen burns)" before its citation markers and full stop."""
    tail = _TAIL.search(sentence)
    cut = tail.start() if tail else len(sentence)
    return f"{sentence[:cut].rstrip()} ({_LABEL.get(lang, _LABEL['en'])}){sentence[cut:]}"


def _states_season(sentence: str, facts: HonestyFacts) -> bool:
    """Whether the sentence gives the season's own unseen area or share (not a district's)."""
    s = facts.season
    if not s:
        return False
    claims = extract_claims(_prep(sentence))
    share = _has(claims, [s["unseen_share_pct"]], ("%",)) and _gap_ok(claims, s["unseen_share_pct"], facts, "%")
    area = _has(claims, [s["unseen_area_ha"]], ("ha", "km2")) and _gap_ok(claims, s["unseen_area_ha"], facts, "ha")
    return share or area


def _gap_ok(claims: list[Claim], value: float, facts: HonestyFacts, unit: str) -> bool:
    """A claim is closer to the season's figure than to any district's."""
    pool = facts.unseen_share_pct if unit == "%" else facts.unseen_area_ha
    others = [v for v in pool if v != value]
    units = ("%",) if unit == "%" else ("ha", "km2")
    return any(c.kind == "number" and c.unit in units and _gap(c, [value]) <= _gap(c, others) for c in claims)


def honest_fallback(units: list[str], lang: str, facts: HonestyFacts) -> tuple[list[str], list[str]]:
    """Rewrite what check_honesty() flags without asking the model again, from the tool outputs:
    - the season's unseen share or area not called candidate becomes unseen_sentence();
    - any other unseen figure keeps its words, gains "(candidate unseen burns)" when it lacks the word,
      and is followed by audit_sentence() (once per unit), so a district's figure is not lost;
    - a raw risk score becomes risk_sentence(), a single-pair AUC auc_sentence();
    - "N crop-residue fires" becomes "N fire detections" (firms_sentence()).
    Each whole-sentence replacement is used at most once per text; a sentence with no replacement is
    dropped. Returns (units, the sentences replaced or dropped)."""
    issues = check_honesty(units, lang, facts)
    flagged: dict[tuple[int, str], set[str]] = {}
    for issue in issues:
        flagged.setdefault((issue.unit, issue.sentence), set()).add(issue.kind)
    used: set[str] = set()
    changed: list[str] = []
    out = []

    def once(key: str, make: Any) -> str | None:
        if key in used:
            return None
        used.add(key)
        return make(lang, facts)

    for index, unit in enumerate(units):
        parts = _SENTENCES.split(unit)  # sentence, separator, sentence, ...
        kept: list[str] = []
        audited = False
        for i in range(0, len(parts), 2):
            sentence, sep = parts[i], (parts[i + 1] if i + 1 < len(parts) else "")
            kinds = flagged.get((index, sentence))
            if not kinds:
                kept += [sentence, sep]
                continue
            changed.append(sentence)
            new: str | None = None
            season = "unseen_not_candidate" in kinds and "unseen" not in used and _states_season(sentence, facts)
            if season and (new := unseen_sentence(lang, facts)):
                used.add("unseen")
            elif "raw_risk_score" in kinds:
                new = once("risk", risk_sentence)
            elif kinds & {"auc_not_all_pairs", "auc_without_interval"}:
                new = once("auc", auc_sentence)
            else:  # repair the sentence itself, so a district's figure is not lost
                new = sentence
                if "firms_called_crop_fires" in kinds:
                    new = firms_sentence(lang, new, facts)
                if new and "unseen_not_candidate" in kinds:
                    new = _labelled(new, lang)
                if new and kinds & {"unseen_not_candidate", "unseen_without_precision"}:
                    extra = None if audited else audit_sentence(lang, facts)
                    if facts.precision is None or facts.precision_ci is None:
                        new = None  # no audit to qualify it with: leave the number out
                    elif extra:
                        new, audited = f"{new.rstrip()} {extra}", True
            if new:
                bullet = _BULLET.match(sentence)
                kept += [(bullet[0] if bullet and not _BULLET.match(new) else "") + new, sep]
            elif kept and "\n" in sep and not kept[-1].strip():
                kept[-1] = sep  # a dropped line: keep one line break before the next
        out.append("".join(kept).strip())
    return out, changed


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
