"""
Server Owners Panel — Phase 9 data helpers (Join gate + Scam Shield for this server).

Same rules as modules/server_panel.py: reads and writes go through db.* functions, every change
writes one audit row, everything is keyed by (guild_id, clone_id). After each write the matching
in-memory cache is dropped so the change applies immediately.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from modules.server_panel import record_change
from modules import join_gate as jg
from modules import scam_shield as ss

logger = logging.getLogger(__name__)


async def _audited(guild_id, clone_id, actor_id, prefix: str, old: dict, fields: dict) -> None:
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"{prefix}.{k}", (old or {}).get(k), v)


def _drop_gate_cache(guild_id, clone_id) -> None:
    try:
        from discord_bot.cogs import join_gate as cog
        cog.invalidate(guild_id, clone_id)
    except Exception:
        logger.debug("[server-panel] join-gate cache not dropped", exc_info=True)


# ── join gate ────────────────────────────────────────────────────────────

async def join_gate_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    from database import db
    try:
        return await db.get_join_gate_config(guild_id, clone_id=clone_id)
    except Exception:
        logger.debug("[server-panel] join gate read failed", exc_info=True)
        return {}


async def set_join_gate(guild_id, clone_id, actor_id, **fields) -> Optional[str]:
    """None on success, else a reason. Validates before writing."""
    from database import db
    if "min_age_days" in fields and jg.validate_min_age(fields["min_age_days"]) is None:
        return f"Minimum account age must be a whole number of days from 0 to {jg.MAX_AGE_DAYS}."
    if "action" in fields and fields["action"] not in jg.ACTIONS:
        return "Unknown action."
    old = await db.get_join_gate_config(guild_id, clone_id=clone_id)
    await db.set_join_gate_config(guild_id, clone_id=clone_id, **fields)
    _drop_gate_cache(guild_id, clone_id)
    await _audited(guild_id, clone_id, actor_id, "join_gate", old, fields)
    return None


# ── scam shield (this server) ────────────────────────────────────────────

async def scam_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    """This server's switch and allowed domains, plus how many scams were caught here."""
    from database import db
    try:
        cfg = await db.get_scam_shield_guild(guild_id, clone_id=clone_id)
    except Exception:
        logger.debug("[server-panel] scam shield read failed", exc_info=True)
        cfg = {"enabled": True, "allowed_domains": []}
    caught = await ss.guild_hit_count(guild_id, clone_id)
    return {"cfg": cfg, "caught": caught, "global_on": ss.is_enabled()}


def clean_domains(text: str, existing) -> Tuple[list, list, list]:
    """Parse a pasted list (one per line or comma separated) -> (added, rejected, final list)."""
    final = [d.lower() for d in existing]
    added, rejected = [], []
    for raw in (text or "").replace(",", "\n").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        dom = ss.parse_domain(raw)
        if dom is None:
            rejected.append(raw[:40])
        elif dom not in final:
            if len(final) >= ss.MAX_ALLOWED_DOMAINS:
                rejected.append(raw[:40])
            else:
                final.append(dom)
                added.append(dom)
    return added, rejected, final


async def set_scam_enabled(guild_id, clone_id, actor_id, enabled: bool) -> None:
    from database import db
    old = await db.get_scam_shield_guild(guild_id, clone_id=clone_id)
    await db.set_scam_shield_guild(guild_id, clone_id=clone_id, enabled=enabled)
    ss.invalidate_guild(guild_id, clone_id)
    await _audited(guild_id, clone_id, actor_id, "scam_shield", old, {"enabled": enabled})


async def set_allowed_domains(guild_id, clone_id, actor_id, domains: list) -> None:
    from database import db
    old = await db.get_scam_shield_guild(guild_id, clone_id=clone_id)
    await db.set_scam_shield_guild(guild_id, clone_id=clone_id, allowed_domains=list(domains))
    ss.invalidate_guild(guild_id, clone_id)
    await record_change(guild_id, clone_id, actor_id, "scam_shield.allowed_domains",
                        ", ".join(old.get("allowed_domains") or []) or None, ", ".join(domains) or None)
