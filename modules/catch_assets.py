"""Phase 1 asset vault helpers (P1-12).

Assets are Discord vault message references, never binary blobs in Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from modules import catch_db


@dataclass(frozen=True, slots=True)
class AssetRef:
    kind: str
    key: str
    variant: str = "normal"
    version: int = 1
    status: str = "placeholder"
    vault_channel_id: int | None = None
    vault_message_id: int | None = None
    mime: str = "image/png"
    content_hash: str = "placeholder"


PLACEHOLDER = AssetRef(kind="creature", key="placeholder")


def validate_ref(ref: AssetRef) -> AssetRef:
    if not ref.kind or not ref.key:
        raise ValueError("asset kind and key are required")
    if ref.variant not in {"normal", "shiny"}:
        raise ValueError("asset variant must be normal or shiny")
    if ref.version < 1:
        raise ValueError("asset version must be positive")
    if ref.status not in {"placeholder", "draft", "live"}:
        raise ValueError("invalid asset status")
    return ref


async def configure_vault(guild_id: int, channel_id: int, *, conn=None) -> None:
    if guild_id <= 0 or channel_id <= 0:
        raise ValueError("guild and channel ids must be positive")
    async with catch_db.connection(conn) as db:
        await db.execute(
            """UPDATE catch_guild_config
               SET announce_channel_id = COALESCE(announce_channel_id, $2), updated_at = now()
             WHERE guild_id = $1 AND clone_key = -1""",
            guild_id, channel_id,
        )


async def load_asset(kind: str, key: str, variant: str = "normal", *, conn=None) -> AssetRef:
    async with catch_db.connection(conn) as db:
        row = await db.fetchrow(
            """SELECT kind, key, variant, version, status, vault_channel_id,
                      vault_message_id, mime, content_hash
                 FROM catch_assets
                WHERE kind = $1 AND key = $2 AND variant = $3 AND status = 'live'
                ORDER BY version DESC LIMIT 1""", kind, key, variant,
        )
    return validate_ref(AssetRef(**dict(row))) if row else PLACEHOLDER


async def begin_draft(ref: AssetRef, *, uploaded_by: int | None = None, conn=None) -> AssetRef:
    ref = validate_ref(ref)
    if ref.status == "placeholder":
        raise ValueError("drafts cannot use placeholder status")
    async with catch_db.transaction(conn) as db:
        await db.execute(
            """INSERT INTO catch_assets
                (kind, key, variant, version, status, content_hash, mime,
                 vault_channel_id, vault_message_id, uploaded_by)
               VALUES ($1,$2,$3,$4,'draft',$5,$6,$7,$8,$9)
               ON CONFLICT (kind, key, variant, version) DO UPDATE SET
                 content_hash = EXCLUDED.content_hash, mime = EXCLUDED.mime,
                 vault_channel_id = EXCLUDED.vault_channel_id,
                 vault_message_id = EXCLUDED.vault_message_id,
                 uploaded_by = EXCLUDED.uploaded_by""",
            ref.kind, ref.key, ref.variant, ref.version, ref.content_hash,
            ref.mime, ref.vault_channel_id, ref.vault_message_id, uploaded_by,
        )
    return ref


async def publish(kind: str, key: str, variant: str = "normal", version: int = 1, *, conn=None) -> bool:
    async with catch_db.transaction(conn) as db:
        result = await db.execute(
            """UPDATE catch_assets SET status = 'live'
                WHERE kind = $1 AND key = $2 AND variant = $3 AND version = $4
                  AND status = 'draft'""", kind, key, variant, version,
        )
    return result.endswith("1")


async def missing_assets(*, conn=None) -> list[dict[str, Any]]:
    async with catch_db.connection(conn) as db:
        rows = await db.fetch(
            """SELECT s.id, s.slug, s.art_key FROM catch_species s
                WHERE s.enabled AND s.art_key IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM catch_assets a WHERE a.kind = 'creature'
                      AND a.key = s.art_key AND a.variant = 'normal' AND a.status = 'live'
                ) ORDER BY s.id"""
        )
    return [dict(row) for row in rows]


def import_manifest(records: list[Mapping[str, Any]]) -> list[AssetRef]:
    refs = [validate_ref(AssetRef(**dict(record))) for record in records]
    keys = {(r.kind, r.key, r.variant, r.version) for r in refs}
    if len(keys) != len(refs):
        raise ValueError("manifest contains duplicate asset versions")
    return refs


__all__ = ["AssetRef", "PLACEHOLDER", "begin_draft", "configure_vault", "import_manifest", "load_asset", "missing_assets", "publish", "validate_ref"]
