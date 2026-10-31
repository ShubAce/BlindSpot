"""rendered_asset: pictures (and the featured manifest) kept in the database, for serverless serving.

Why: on Vercel every function instance has its own empty /tmp, so a picture rendered by one instance
(or by the `featured` job on a GitHub Actions runner) is not on the disk of the next. The landing
page's hero frames and the case-file chips must open at once, never after a 5-20 s live render, so
they are stored here and read back by any instance: the image cache reads disk, then this table,
then renders (blindspot.sources.pcdata.render).

Only the featured frames, the case-file chips and the featured manifest (`featured.json`, the
payload of GET /api/imagery/featured) are stored; arbitrary map tiles never are, so the table stays
a few tens of MB on Neon's 0.5 GB free tier. The `featured` job writes them and deletes assets that
nothing references and nobody used for KEEP_DAYS.

A row carries the provenance the image routes send as headers (X-Imagery-Source, -Collection,
-Item, X-Fetch-Id, X-Rendered-At, X-Imagery-Note), so a picture served from here is answered with
exactly the headers it had when it was rendered. The ETag is the key itself.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from blindspot.db import Base, session_scope

KEEP_DAYS = 7  # an unreferenced asset used within this many days is kept
TOUCH_EVERY = timedelta(days=1)  # last_used_at is refreshed at most this often (a write per read otherwise)
ROLES = ("frame", "chip", "manifest")


class RenderedAsset(Base):
    __tablename__ = "rendered_asset"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # pcdata cache key, or a name
    role: Mapped[str] = mapped_column(String(16))  # frame / chip / manifest
    media_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    source_key: Mapped[str | None] = mapped_column(String(32))  # pc_data or s2_cog_e84
    collection: Mapped[str | None] = mapped_column(String(32))  # what was actually drawn
    item: Mapped[str | None] = mapped_column(String(80))
    url: Mapped[str | None] = mapped_column(Text)  # the logged upstream URL
    fetch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_fetch.id", ondelete="SET NULL"), index=True
    )
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)  # why a fallback renderer was used, if one was
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


def load_asset(key: str) -> dict[str, Any] | None:
    """The stored asset as a dict (data included), or None. Marks it used (at most once a day).

    Read in binary format: in the text format a bytea comes back hex-encoded, twice its size,
    and every byte Neon sends counts against its egress allowance."""
    columns = ("key", "role", "media_type", "data", "source_key", "collection", "item", "url",
               "fetch_id", "rendered_at", "note", "last_used_at")
    with session_scope() as session:
        with session.connection().connection.driver_connection.cursor(binary=True) as cur:
            cur.execute(f"SELECT {', '.join(columns)} FROM rendered_asset WHERE key = %s", (key,))
            found = cur.fetchone()
        if found is None:
            return None
        out = dict(zip(columns, found, strict=True))
        out["data"] = bytes(out["data"])
        if out["rendered_at"] is not None:  # the session time zone must not leak into headers
            out["rendered_at"] = out["rendered_at"].astimezone(UTC)
        if out["last_used_at"] < datetime.now(UTC) - TOUCH_EVERY:
            session.execute(text("UPDATE rendered_asset SET last_used_at = now() WHERE key = :key"),
                            {"key": key})
        return out


def save_asset(key: str, role: str, media_type: str, data: bytes, **meta: Any) -> None:
    """Insert or replace one asset. `meta` holds the provenance columns (source_key, collection,
    item, url, fetch_id, rendered_at, note)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    values = {"key": key, "role": role, "media_type": media_type, "data": data,
              **{k: meta.get(k) for k in ("source_key", "collection", "item", "url", "fetch_id",
                                         "rendered_at", "note")}}
    stmt = insert(RenderedAsset).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={**{k: stmt.excluded[k] for k in values if k != "key"}, "last_used_at": func.now()},
    )
    with session_scope() as session:
        session.execute(stmt)


def stored_keys(keys: Collection[str]) -> set[str]:
    """Which of `keys` are stored; marks them used, so a referenced asset is never pruned."""
    if not keys:
        return set()
    with session_scope() as session:
        found = session.execute(
            text("UPDATE rendered_asset SET last_used_at = now() WHERE key = ANY(:keys) RETURNING key"),
            {"keys": list(keys)},
        ).scalars().all()
    return set(found)


def prune_assets(keep: Collection[str], keep_days: int = KEEP_DAYS) -> dict[str, int]:
    """Delete the assets not in `keep` that nobody used for `keep_days` days."""
    with session_scope() as session:
        removed = session.execute(
            text("""
                DELETE FROM rendered_asset
                WHERE NOT (key = ANY(:keep)) AND last_used_at < now() - make_interval(days => :days)
                RETURNING octet_length(data)
            """), {"keep": list(keep), "days": keep_days},
        ).scalars().all()
        left = session.execute(
            text("SELECT count(*), coalesce(sum(octet_length(data)), 0) FROM rendered_asset")
        ).one()
    return {"removed": len(removed), "removed_bytes": int(sum(removed)),
            "stored": int(left[0]), "stored_bytes": int(left[1])}


# --- response_body: finished API answers kept for serverless instances (Neon egress budget) -------
#
# Why: a cold request on Vercel (CDN expiry, a new region, a new instance) recomputes its answer
# from Neon, and some answers pull megabytes of rows for a few hundred KB of JSON (smoke trails,
# scar polygons, boundaries). Neon's free plan has 5 GB of egress a month. So the job that changes
# the data (or the first request after it changed) stores the finished body here, xz-compressed,
# and later cold requests read ~10-25 % of the bytes. A separate table from rendered_asset, so
# imagery's pruning of unreferenced pictures can never delete a stored body.
#
# A row is valid only while its `stamp` equals what the route computes now (the data version plus
# a hash of the code that renders it); otherwise the route computes live and replaces it.
# `body` is the JSON payload without `generated_at` and `sources` (the request-time parts); the
# route adds them, so the bytes sent are exactly what a live computation sends.

RESPONSE_KEEP_DAYS = 3  # prune: rows nobody read for this long go (the next request re-stores)


class ResponseBody(Base):
    __tablename__ = "response_body"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)  # route + canonical query
    stamp: Mapped[str] = mapped_column(Text)  # data version + code hash it was rendered for
    codec: Mapped[str] = mapped_column(String(8))  # xz
    body: Mapped[bytes] = mapped_column(LargeBinary)  # compressed payload JSON (no generated_at/sources)
    sources: Mapped[bytes | None] = mapped_column(LargeBinary)  # JSON array, when stored with the body
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    raw_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def load_body(session: Any, key: str, stamp: str) -> dict[str, Any] | None:
    """The stored body under `key` if it was rendered for `stamp`, else None (see probe_body)."""
    return probe_body(session, key, stamp)[0]


def probe_body(session: Any, key: str, stamp: str) -> tuple[dict[str, Any] | None, bool | None]:
    """(the stored body under `key` if it was rendered for `stamp`, whether any row is stored
    under `key`); the second is None when the store cannot be read.

    The body and sources are sent only when the stamp matches: a stale row costs a few bytes of
    egress, not the body it would throw away. Fetched in binary format: bytea in the text
    protocol would come back hex-encoded, doubling the bytes Neon sends. Never raises for a
    missing table (a deployment older than the table): the caller then computes live."""
    connection = session.connection()
    raw = connection.connection.driver_connection
    fresh = "stamp = %(stamp)s"
    try:
        with connection.begin_nested(), raw.cursor(binary=True) as cur:
            cur.execute(
                f"SELECT {fresh}, CASE WHEN {fresh} THEN codec END, CASE WHEN {fresh} THEN body END,"
                f" CASE WHEN {fresh} THEN sources END, CASE WHEN {fresh} THEN headers END,"
                " raw_bytes, last_used_at FROM response_body WHERE key = %(key)s",
                {"key": key, "stamp": stamp})
            row = cur.fetchone()
    except Exception:  # noqa: BLE001  (table missing, or the store is unreadable: compute live)
        return None, None
    if row is None:
        return None, False
    matches, codec, body, sources, headers, raw_bytes, last_used_at = row
    if not matches:
        return None, True
    if last_used_at is not None and last_used_at < datetime.now(UTC) - TOUCH_EVERY:
        _touch(key)
    return ({"codec": codec, "body": bytes(body), "sources": None if sources is None else bytes(sources),
             "headers": headers if isinstance(headers, dict) else None, "raw_bytes": raw_bytes}, True)


def _touch(key: str) -> None:
    """Mark a body used (at most once a day), in its own transaction: the route's session stays
    read-only."""
    try:
        with session_scope() as session:
            session.execute(text("UPDATE response_body SET last_used_at = now() WHERE key = :k"), {"k": key})
    except Exception:  # noqa: BLE001  (not worth failing a read for)
        pass


def save_body(key: str, stamp: str, codec: str, body: bytes, raw_bytes: int, *,
              sources: bytes | None = None, headers: dict[str, str] | None = None) -> bool:
    """Insert or replace one stored body (own transaction). False when it could not be written."""
    stmt = insert(ResponseBody).values(key=key, stamp=stamp, codec=codec, body=body, sources=sources,
                                       headers=headers, raw_bytes=raw_bytes)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={k: stmt.excluded[k] for k in ("stamp", "codec", "body", "sources", "headers", "raw_bytes")}
        | {"created_at": func.now(), "last_used_at": func.now()},
    )
    try:
        with session_scope() as session:
            session.execute(stmt)
        return True
    except Exception:  # noqa: BLE001  (missing table on an old schema: serving still works)
        return False


def prune_bodies(keep_days: int = RESPONSE_KEEP_DAYS, dry_run: bool = False) -> dict[str, Any]:
    """Delete stored bodies nobody read for `keep_days` days (one-off queries, old variants)."""
    where = "last_used_at < now() - make_interval(days => :days)"
    with session_scope() as session:
        if session.execute(text("SELECT to_regclass('public.response_body')")).scalar() is None:
            return {"removed": 0, "removed_bytes": 0, "stored": 0, "stored_bytes": 0, "dry_run": dry_run}
        if dry_run:
            removed = session.execute(
                text(f"SELECT octet_length(body) FROM response_body WHERE {where}"), {"days": keep_days}
            ).scalars().all()
        else:
            removed = session.execute(
                text(f"DELETE FROM response_body WHERE {where} RETURNING octet_length(body)"),
                {"days": keep_days},
            ).scalars().all()
        left = session.execute(
            text("SELECT count(*), coalesce(sum(octet_length(body)), 0) FROM response_body")
        ).one()
    return {"removed": len(removed), "removed_bytes": int(sum(removed)),
            "stored": int(left[0]), "stored_bytes": int(left[1]), "dry_run": dry_run}


__all__ = ["KEEP_DAYS", "RESPONSE_KEEP_DAYS", "RenderedAsset", "ResponseBody", "load_asset",
           "load_body", "probe_body", "prune_assets", "prune_bodies", "save_asset", "save_body",
           "stored_keys"]
