# path: modules/ai_skills.py

"""
Phase 5 of the autonomous-AI plan: reusable AI skills.

A skill is a DEVELOPER-DEFINED recipe (code, never user-supplied text) that turns a few validated inputs into a short list of
EXISTING allow-listed AI commands. It adds no new powers:

  * the AI asks for `skill__<name>`; ai_tools.py expands it into ordinary command calls and sends them down the SAME path as
    any other AI command: allowlist, the asker's real permission, role hierarchy, the daily cap, the Confirm click and the
    ai_plan runner (ordered, fail-fast, bounded, retry-safe) -- each step is re-checked when it runs;
  * a skill can only name commands that are in the allowlist (checked on every expansion), can never name another skill
    (no recursion) and is capped at MAX_SKILL_STEPS steps;
  * inputs are validated against a typed schema (unknown fields are dropped; strings are cleaned and length-limited);
  * a skill has its own permission requirement (checked in addition to each command's), a per-person cooldown, and a
    per-server on/off switch (admins use /aiskills; the AI itself can never toggle skills);
  * every refusal is logged.

Extending: call register_skill(Skill(...)) from any module. Nothing here reads user memory or user-written text as code.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME

logger = logging.getLogger(__name__)

PREFIX = "skill__"
MAX_SKILL_STEPS = 5
MAX_STR = 300
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True)
class SkillField:
    name: str
    kind: str = "str"                       # str | int | choice
    description: str = ""
    required: bool = True
    max_len: int = 100
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Tuple[str, ...] = ()
    default: Optional[object] = None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    fields: Tuple[SkillField, ...]
    build: Callable[[dict], List[dict]]     # clean inputs -> [{"name": <allowlisted command>, "arguments": {...}}]
    needs_perms: Tuple[str, ...] = ()
    cooldown_seconds: int = 20
    max_steps: int = MAX_SKILL_STEPS
    enabled_by_default: bool = True


_REGISTRY: Dict[str, Skill] = {}
_last_used: Dict[Tuple[int, str], float] = {}


def register_skill(skill: Skill, *, replace: bool = False) -> Skill:
    if not _NAME_RE.match(skill.name):
        raise ValueError(f"bad skill name {skill.name!r}")
    if skill.name in _REGISTRY and not replace:
        raise ValueError(f"skill {skill.name!r} already registered")
    if not skill.description.strip() or not (1 <= skill.max_steps <= MAX_SKILL_STEPS):
        raise ValueError("skill needs a description and 1..MAX_SKILL_STEPS steps")
    seen = set()
    for f in skill.fields:
        if f.name in seen or f.kind not in ("str", "int", "choice") or (f.kind == "choice" and not f.choices):
            raise ValueError(f"bad field {f.name!r} on skill {skill.name!r}")
        seen.add(f.name)
    _REGISTRY[skill.name] = skill
    return skill


def all_skills() -> List[Skill]:
    return list(_REGISTRY.values())


def get_skill(name: str) -> Optional[Skill]:
    return _REGISTRY.get(name[len(PREFIX):] if name.startswith(PREFIX) else name)


def is_skill_call(name: str) -> bool:
    return isinstance(name, str) and name.startswith(PREFIX)


# ---- schema shown to the model -----------------------------------------------------------------------------------

def tool_schema(skill: Skill) -> dict:
    props, required = {}, []
    for f in skill.fields:
        p = {"type": "integer" if f.kind == "int" else "string", "description": (f.description or f.name)[:200]}
        if f.kind == "choice":
            p["enum"] = list(f.choices)
        if not f.required:
            from modules.ai_tool_schema import nullable
            nullable(p)
        props[f.name] = p
        if f.required:
            required.append(f.name)
    return {"type": "function", "function": {
        "name": PREFIX + skill.name,
        "description": f"SKILL: {skill.description}"[:400],
        "parameters": {"type": "object", "properties": props, "required": required}}}


# ---- validation ------------------------------------------------------------------------------------------------

def _clean_str(v) -> str:
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(v))).strip()


def validate_inputs(skill: Skill, raw) -> Tuple[Optional[dict], str]:
    """(clean_inputs, "") or (None, plain reason). Unknown keys are dropped; every value is type-checked and bounded."""
    if not isinstance(raw, dict):
        return None, "The skill needs its inputs as an object."
    clean = {}
    for f in skill.fields:
        v = raw.get(f.name)
        if v is None or (isinstance(v, str) and not v.strip()):
            if f.default is not None:
                clean[f.name] = f.default
                continue
            if f.required:
                return None, f"Missing `{f.name}`."
            continue
        if f.kind == "int":
            if isinstance(v, bool):
                return None, f"`{f.name}` must be a whole number."
            try:
                n = int(str(v).strip())
            except ValueError:
                return None, f"`{f.name}` must be a whole number."
            if (f.minimum is not None and n < f.minimum) or (f.maximum is not None and n > f.maximum):
                return None, f"`{f.name}` must be between {f.minimum} and {f.maximum}."
            clean[f.name] = n
        elif f.kind == "choice":
            s = _clean_str(v).lower()
            if s not in f.choices:
                return None, f"`{f.name}` must be one of: {', '.join(f.choices)}."
            clean[f.name] = s
        else:
            if isinstance(v, (dict, list, bool)):
                return None, f"`{f.name}` must be text."
            s = _clean_str(v)
            if len(s) > min(f.max_len, MAX_STR):
                return None, f"`{f.name}` is too long (max {min(f.max_len, MAX_STR)} characters)."
            if f.required and not s:
                return None, f"Missing `{f.name}`."
            clean[f.name] = s
    return clean, ""


def check_cooldown(user_id: int, skill: Skill, now: Optional[float] = None) -> Tuple[bool, int]:
    now = time.monotonic() if now is None else now
    last = _last_used.get((user_id, skill.name))
    if last is not None and now - last < skill.cooldown_seconds:
        return False, int(skill.cooldown_seconds - (now - last)) + 1
    return True, 0


def mark_used(user_id: int, skill: Skill, now: Optional[float] = None) -> None:
    if len(_last_used) > 10000:
        _last_used.clear()
    _last_used[(user_id, skill.name)] = time.monotonic() if now is None else now


def expand(skill: Skill, raw_inputs, perms) -> Tuple[Optional[List[dict]], str]:
    """Inputs -> the concrete command calls, or (None, reason). `perms` is the asker's discord.Permissions (or any object with
    the same attribute names). Everything returned is re-checked by the normal command path before anything runs."""
    for p in skill.needs_perms:
        if not getattr(perms, p, False):
            logger.warning("[ai_skills] %s refused: missing %s", skill.name, p)
            return None, f"That skill needs the **{p.replace('_', ' ').title()}** permission."
    clean, err = validate_inputs(skill, raw_inputs)
    if clean is None:
        logger.info("[ai_skills] %s bad input: %s", skill.name, err)
        return None, err
    try:
        calls = skill.build(clean)
    except Exception:
        logger.exception("[ai_skills] %s build failed", skill.name)
        return None, "That skill couldn't be prepared."
    if not isinstance(calls, list) or not calls or len(calls) > skill.max_steps:
        logger.error("[ai_skills] %s produced %r", skill.name, type(calls).__name__)
        return None, "That skill couldn't be prepared."
    out = []
    for c in calls:
        name = c.get("name") if isinstance(c, dict) else None
        args = c.get("arguments") if isinstance(c, dict) else None
        if name not in AI_COMMANDS_BY_NAME or is_skill_call(str(name)) or not isinstance(args, dict):
            logger.error("[ai_skills] %s tried a non-allowlisted step %r", skill.name, name)
            return None, "That skill couldn't be prepared."
        out.append({"name": name, "arguments": {str(k): (v if isinstance(v, (int, float)) else str(v)[:MAX_STR])
                                                  for k, v in args.items() if v is not None}})
    return out, ""


# ---- per-server on/off (table created on first use, like ai_prefs / ai_memory) ---------------------------------------

_table_ready = False
_cache: Dict[int, Tuple[Dict[str, bool], float]] = {}
_CACHE_TTL = 60


async def _pool():
    from database import get_pool
    return await get_pool()


async def _ensure(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_skill_settings ("
        " guild_id BIGINT NOT NULL, skill TEXT NOT NULL, enabled BOOLEAN NOT NULL,"
        " updated_by BIGINT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (guild_id, skill))")
    _table_ready = True


async def overrides(guild_id: int) -> Dict[str, bool]:
    hit = _cache.get(guild_id)
    if hit and time.monotonic() - hit[1] < _CACHE_TTL:
        return hit[0]
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await _ensure(conn)
            rows = await conn.fetch("SELECT skill, enabled FROM ai_skill_settings WHERE guild_id = $1", guild_id)
        data = {r["skill"]: bool(r["enabled"]) for r in rows}
    except Exception:
        logger.debug("[ai_skills] settings unavailable; using defaults", exc_info=True)
        return {}
    if len(_cache) > 5000:
        _cache.clear()
    _cache[guild_id] = (data, time.monotonic())
    return data


async def is_enabled(guild_id: int, skill: Skill) -> bool:
    return (await overrides(guild_id)).get(skill.name, skill.enabled_by_default)


async def set_enabled(guild_id: int, skill_name: str, enabled: bool, updated_by: int) -> bool:
    if skill_name not in _REGISTRY:
        return False
    pool = await _pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "INSERT INTO ai_skill_settings (guild_id, skill, enabled, updated_by) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (guild_id, skill) DO UPDATE SET enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, "
            "updated_at = NOW()", guild_id, skill_name, enabled, updated_by)
    _cache.pop(guild_id, None)
    return True


async def tools_for(guild_id: int, perms) -> List[dict]:
    """Tool schemas for the skills this person may use here right now (enabled in the server + skill permission held)."""
    ov = await overrides(guild_id)
    out = []
    for s in _REGISTRY.values():
        if not ov.get(s.name, s.enabled_by_default):
            continue
        if all(getattr(perms, p, False) for p in s.needs_perms):
            out.append(tool_schema(s))
    return out


# ---- built-in skills (compositions of existing allow-listed commands) ---------------------------------------------

def _channel(v: str) -> str:
    return v or "this"


register_skill(Skill(
    name="lockdown_channel",
    description="Lock a channel (nobody but staff can post) AND put a notice in its topic explaining why. Use for 'lock this "
                "channel because ...' requests.",
    fields=(SkillField("channel", description="#channel, name, or 'this'", required=False, default="this"),
            SkillField("reason", description="Short reason shown in the channel topic", max_len=100, default="temporarily locked")),
    needs_perms=("manage_channels", "manage_roles"),
    build=lambda i: [{"name": "channeledit_lock", "arguments": {"channel": _channel(i["channel"])}},
                     {"name": "channeledit_topic", "arguments": {"channel": _channel(i["channel"]),
                                                                  "topic": f"\U0001f512 Locked: {i['reason']}"}}]))

register_skill(Skill(
    name="reopen_channel",
    description="Unlock a locked channel AND clear the lock notice from its topic. Use for 'unlock / reopen this channel'.",
    fields=(SkillField("channel", description="#channel, name, or 'this'", required=False, default="this"),),
    needs_perms=("manage_channels", "manage_roles"),
    build=lambda i: [{"name": "channeledit_unlock", "arguments": {"channel": _channel(i["channel"])}},
                     {"name": "channeledit_topic", "arguments": {"channel": _channel(i["channel"]), "topic": ""}}]))

register_skill(Skill(
    name="welcome_quickstart",
    description="Turn welcome cards on in a channel AND set the welcome text in one go. Use for 'set up a welcome message in ...'.",
    fields=(SkillField("channel", description="#channel to post welcome cards in"),
            SkillField("message", description="Welcome text (may use the bot's placeholders)", max_len=250)),
    needs_perms=("manage_guild",),
    build=lambda i: [{"name": "welcome_enable", "arguments": {"channel": i["channel"]}},
                     {"name": "welcome_message", "arguments": {"template": i["message"]}}]))
