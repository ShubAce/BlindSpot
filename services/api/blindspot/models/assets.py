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
from sqlalchemy.dialects.postgresql import insert
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
    """The stored asset as a dict (data included), or None. Marks it used (at most once a day)."""
    with session_scope() as session:
        row = session.execute(
            text("""
                SELECT key, role, media_type, data, source_key, collection, item, url, fetch_id,
                       rendered_at, note, last_used_at
                FROM rendered_asset WHERE key = :key
            """), {"key": key},
        ).mappings().first()
        if row is None:
            return None
        out = dict(row)
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


__all__ = ["KEEP_DAYS", "RenderedAsset", "load_asset", "prune_assets", "save_asset", "stored_keys"]
