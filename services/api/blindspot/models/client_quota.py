"""client_hit: per-client request counts shared by every API instance (abuse guards).

Why: on Vercel every function instance has its own memory and a cold start forgets everything, so
an in-memory "3 questions a minute per address" limit resets whenever a request lands on a new
instance. The counts that must hold across instances (questions to the investigator, live picture
renders, live map tiles) are kept here instead: one row per counted request, kept for a day.

What is stored: never the address itself, only a salted HMAC-SHA256 of it (32 hex characters). The
salt is CLIENT_HASH_SALT when set, otherwise derived from the database URL (a secret every instance
of one deployment shares), so the same address gives the same hash on every instance and nobody
without the salt can turn a hash back into an address by trying all 2^32 IPv4 addresses.

Which address: `client_address()`. Behind Vercel's proxy the socket peer is the proxy, so when the
VERCEL environment variable is set (Vercel sets it on every deployment) the first X-Forwarded-For
entry, else X-Real-IP, is used: Vercel overwrites both with the real client address, so a client
cannot spoof them there. Anywhere else those headers are ignored (any client could send them) and
the socket peer is used. IPv6 addresses count per /64, the block one subscriber gets, so rotating
addresses inside it does not reset the limit.

Shared counting is on when SHARED_CLIENT_LIMITS=1, or by default whenever VERCEL is set. A single
local process counts exactly in memory and needs no database round trip.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base, session_scope

log = logging.getLogger(__name__)

KEEP = "1 day"  # rows older than the longest window are useless
SWEEP_CHANCE = 0.02  # share of counted requests that also delete every client's expired rows
SCOPES = ("ask", "render", "tiles")


class ClientHit(Base):
    __tablename__ = "client_hit"
    __table_args__ = (Index("ix_client_hit_scope_client_at", "scope", "client", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16))  # ask / render / tiles
    client: Mapped[str] = mapped_column(String(32))  # salted hash of the address, never the address
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# --- which client --------------------------------------------------------------------------------


def on_vercel() -> bool:
    """True on a Vercel deployment (Vercel sets VERCEL=1 in every function's environment)."""
    return bool(os.environ.get("VERCEL"))


def shared_limits_enabled() -> bool:
    raw = os.environ.get("SHARED_CLIENT_LIMITS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return on_vercel()


def _canonical_ip(value: str) -> str | None:
    """The address as a counting key: IPv4 as is, IPv6 as its /64; None if `value` is not an IP."""
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


def client_address(headers: Mapping[str, str], peer: str | None) -> str:
    """The address one client's requests are counted under (see the module docstring)."""
    if on_vercel():
        forwarded = (headers.get("x-forwarded-for") or "").split(",")[0]
        for candidate in (forwarded, headers.get("x-real-ip") or ""):
            if (ip := _canonical_ip(candidate)) is not None:
                return ip
    if peer:
        return _canonical_ip(peer) or peer
    return "unknown"


def _salt() -> bytes:
    explicit = os.environ.get("CLIENT_HASH_SALT", "").strip()
    if explicit:
        return explicit.encode()
    from blindspot.config import get_settings

    return hashlib.sha256(b"blindspot client hash|" + get_settings().database_url.encode()).digest()


def client_hash(address: str) -> str:
    """Salted HMAC-SHA256 of the address, 32 hex characters: what the table stores."""
    return hmac.new(_salt(), address.encode(), hashlib.sha256).hexdigest()[:32]


# --- counting ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    retry_after_s: float
    reason: str


def hit(scope: str, address: str, per_minute: int, per_day: int, noun: str) -> Refusal | None:
    """Count one request of `scope` from `address`, or refuse it (without counting) when it would
    exceed `per_minute` in the last 60 s or `per_day` in the last 24 h across every instance.

    A transaction-scoped advisory lock on (scope, client) makes two instances counting the same
    client at the same moment take turns, so neither limit can be overshot by a race.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    client = client_hash(address)
    with session_scope() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                        {"k": f"client_hit:{scope}:{client}"})
        row = session.execute(text("""
            SELECT count(*) FILTER (WHERE at > now() - interval '60 seconds') AS minute,
                   count(*) AS day,
                   extract(epoch FROM now() - min(at) FILTER (WHERE at > now() - interval '60 seconds')) AS minute_age,
                   extract(epoch FROM now() - min(at)) AS day_age
            FROM client_hit
            WHERE scope = :scope AND client = :client AND at > now() - interval '1 day'
        """), {"scope": scope, "client": client}).mappings().one()
        if row["minute"] >= per_minute:
            return Refusal(max(1.0, 60 - float(row["minute_age"] or 0)), f"at most {per_minute} {noun} a minute")
        if row["day"] >= per_day:
            return Refusal(max(1.0, 86400 - float(row["day_age"] or 0)), f"at most {per_day} {noun} a day")
        session.execute(text("INSERT INTO client_hit (scope, client) VALUES (:scope, :client)"),
                        {"scope": scope, "client": client})
        session.execute(text(f"""
            DELETE FROM client_hit
            WHERE scope = :scope AND client = :client AND at <= now() - interval '{KEEP}'
        """), {"scope": scope, "client": client})
        if random.random() < SWEEP_CHANCE:
            session.execute(text(f"DELETE FROM client_hit WHERE at <= now() - interval '{KEEP}'"))
    return None


def hit_or_allow(scope: str, address: str, per_minute: int, per_day: int, noun: str) -> Refusal | None:
    """hit(), but a database failure lets the request through (the in-memory limit still applies):
    a broken counter must not take the investigator or the pictures down."""
    try:
        return hit(scope, address, per_minute, per_day, noun)
    except Exception as exc:
        log.warning("shared %s limit unavailable, counting in memory only: %s", scope, type(exc).__name__)
        return None


def count(scope: str, address: str) -> int:
    """Requests of `scope` counted for `address` in the last day (tests, diagnostics)."""
    with session_scope() as session:
        return int(session.execute(text("""
            SELECT count(*) FROM client_hit
            WHERE scope = :scope AND client = :client AND at > now() - interval '1 day'
        """), {"scope": scope, "client": client_hash(address)}).scalar_one())


def forget(address: str) -> None:
    """Delete every row of `address` (tests clean up after themselves)."""
    with session_scope() as session:
        session.execute(text("DELETE FROM client_hit WHERE client = :client"), {"client": client_hash(address)})


__all__ = ["ClientHit", "Refusal", "client_address", "client_hash", "count", "forget", "hit",
           "hit_or_allow", "on_vercel", "shared_limits_enabled"]
