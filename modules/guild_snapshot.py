# path: modules/guild_snapshot.py

"""
Server snapshot / restore for the bot test harness (phase 2 of docs/BOT_TEST_HARNESS_PLAN.md).

Duck-typed on purpose: `capture` only reads plain attributes of the guild, roles and channels, so the whole module is
tested against a fake guild. Nothing here decides WHETHER a restore may run (that is modules/test_harness.check_gates,
checked by the cog before every call); this module only knows HOW.

Saved: roles (name, colour, permissions, position, hoist, mentionable), categories, text/news/voice/stage/forum channels
(name, topic, nsfw, slowmode, bitrate, user limit, parent, position), permission overwrites, and a small settings subset
(verification level, system channel, rules channel).
NOT saved / not restorable (said up front in every report): message history, deleted messages, bans of real users,
webhook tokens, integrations, threads, member role assignments (no test members exist yet), managed (bot/integration)
roles and channel types other than the five above.

Restore is diff-based: capture the live server, plan the smallest set of operations that turns it back into the snapshot,
apply them slowly, then capture again and plan again. An empty second plan IS the verification.
Items are matched by id first; a snapshot item whose id is gone is matched to a live one with the same name/type/parent
(that is what happens when anti-nuke already recreated a deleted channel), so we fix it instead of duplicating it.
"""

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import discord

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
CATEGORY = "category"
RESTORABLE_CHANNEL_TYPES = ("text", "news", "voice", "stage_voice", "forum")


# ── capture ───────────────────────────────────────────────────────────────

def _val(x):
    return getattr(x, "value", x)


def _type_name(ch) -> str:
    t = getattr(ch, "type", None)
    return str(getattr(t, "name", t))


def _target_kind(target) -> str:
    return "role" if hasattr(target, "permissions") and not hasattr(target, "guild_permissions") else "member"


def _overwrites(ch) -> List[Dict[str, Any]]:
    out = []
    for target, ow in (getattr(ch, "overwrites", None) or {}).items():
        allow, deny = ow.pair()
        out.append({"id": int(target.id), "kind": _target_kind(target), "allow": int(_val(allow)), "deny": int(_val(deny))})
    out.sort(key=lambda o: (o["kind"], o["id"]))
    return out


def _channel_dict(ch) -> Dict[str, Any]:
    return {
        "id": int(ch.id), "name": ch.name, "type": _type_name(ch),
        "category_id": (int(ch.category_id) if getattr(ch, "category_id", None) else None),
        "position": int(getattr(ch, "position", 0) or 0),
        "topic": getattr(ch, "topic", None), "nsfw": bool(getattr(ch, "nsfw", False)),
        "slowmode": int(getattr(ch, "slowmode_delay", 0) or 0),
        "bitrate": getattr(ch, "bitrate", None), "user_limit": getattr(ch, "user_limit", None),
        "overwrites": _overwrites(ch),
    }


def capture(guild) -> Dict[str, Any]:
    """Deterministic, JSON-safe picture of the server's structure (no timestamps, so two captures compare equal)."""
    roles = [{
        "id": int(r.id), "name": r.name, "color": int(_val(r.color)), "permissions": int(_val(r.permissions)),
        "position": int(r.position), "hoist": bool(r.hoist), "mentionable": bool(r.mentionable),
        "managed": bool(r.managed), "default": bool(r.is_default()),
    } for r in guild.roles]
    categories, channels = [], []
    for ch in guild.channels:
        d = _channel_dict(ch)
        (categories if d["type"] == CATEGORY else channels).append(d)
    sysch, rules = getattr(guild, "system_channel", None), getattr(guild, "rules_channel", None)
    return {
        "version": SNAPSHOT_VERSION, "guild_id": int(guild.id),
        "settings": {"verification_level": int(_val(getattr(guild, "verification_level", 0)) or 0),
                     "system_channel_id": int(sysch.id) if sysch else None,
                     "rules_channel_id": int(rules.id) if rules else None},
        "roles": sorted(roles, key=lambda r: r["id"]),
        "categories": sorted(categories, key=lambda c: c["id"]),
        "channels": sorted(channels, key=lambda c: c["id"]),
    }


def _hash(snap: Dict[str, Any]) -> str:
    body = {k: v for k, v in snap.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(snap: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(snap)
    out["hash"] = _hash(out)
    return out


def verify(snap: Any) -> bool:
    """True only for an intact snapshot of the supported version (a corrupt one must never start a run)."""
    try:
        return bool(snap.get("hash")) and snap["hash"] == _hash(snap) and snap.get("version") == SNAPSHOT_VERSION
    except Exception:
        return False


def summary(snap: Dict[str, Any]) -> str:
    return (f"{len(snap['roles'])} roles, {len(snap['categories'])} categories, {len(snap['channels'])} channels")


# ── matching ──────────────────────────────────────────────────────────────

def _match(snap_items, cur_items, key, skip=lambda s: False) -> Dict[int, int]:
    """snapshot id -> live id. By id first; then a leftover live item with the same `key` (only when unambiguous)."""
    cur_by_id = {c["id"]: c for c in cur_items}
    mapping: Dict[int, int] = {}
    used = set()
    for s in snap_items:
        if s["id"] in cur_by_id:
            mapping[s["id"]] = s["id"]
            used.add(s["id"])
    for s in snap_items:
        if s["id"] in mapping or skip(s):
            continue
        k = key(s, mapping)
        if k is None:
            continue
        cands = [c for c in cur_items if c["id"] not in used and key(c, None) == k and not skip(c)]
        if len(cands) == 1:
            mapping[s["id"]] = cands[0]["id"]
            used.add(cands[0]["id"])
    return mapping


def build_idmap(snap: Dict[str, Any], cur: Dict[str, Any]) -> Dict[int, int]:
    roles = _match(snap["roles"], cur["roles"], lambda r, m: r["name"], skip=lambda r: r["managed"] or r["default"])
    cats = _match(snap["categories"], cur["categories"], lambda c, m: c["name"])

    def chan_key(c, m):
        if m is None:                       # live side: its parent is already a live id
            return (c["type"], c["name"], c["category_id"])
        if c["category_id"] is None:
            return (c["type"], c["name"], None)
        parent = cats.get(c["category_id"])
        return None if parent is None else (c["type"], c["name"], parent)
    chans = _match(snap["channels"], cur["channels"], chan_key)
    return {**roles, **cats, **chans}


# ── planning ──────────────────────────────────────────────────────────────

def _expected_overwrites(sov, idmap) -> Tuple[Dict[Tuple[str, int], Tuple[int, int]], bool]:
    """Snapshot overwrites expressed in LIVE ids. Second value is True when some role target has no live role yet."""
    exp, unresolved = {}, False
    for o in sov:
        if o["kind"] == "role":
            live = idmap.get(o["id"])
            if live is None:
                unresolved = True
                continue
            exp[("role", live)] = (o["allow"], o["deny"])
        else:
            exp[("member", o["id"])] = (o["allow"], o["deny"])
    return exp, unresolved


def _order(items, idmap, position_of) -> List[int]:
    return [idmap[i["id"]] for i in sorted(items, key=lambda i: (position_of(i), i["id"])) if i["id"] in idmap]


def plan_restore(snap: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
    """Return {"ops": [...], "idmap": {...}, "notes": [...]}. Ops are plain dicts (see apply_plan)."""
    idmap = build_idmap(snap, cur)
    notes: List[str] = []
    cur_roles = {r["id"]: r for r in cur["roles"]}
    cur_cats = {c["id"]: c for c in cur["categories"]}
    cur_chans = {c["id"]: c for c in cur["channels"]}
    ops: List[Dict[str, Any]] = []

    def add(op, **kw):
        ops.append({"op": op, **kw})

    # roles
    role_struct = False
    for s in snap["roles"]:
        if s["managed"]:
            if s["id"] not in idmap:
                notes.append(f"managed role {s['name']!r} is gone and cannot be recreated by the bot")
            continue
        live = cur_roles.get(idmap.get(s["id"]))
        if live is None:
            role_struct = True
            add("create_role", sid=s["id"], name=s["name"], data={k: s[k] for k in ("name", "color", "permissions", "hoist", "mentionable")})
            continue
        ch = {k: s[k] for k in ("name", "color", "permissions", "hoist", "mentionable") if live[k] != s[k]}
        if s["default"]:
            ch = {k: v for k, v in ch.items() if k == "permissions"}   # @everyone: only its permissions
        if ch:
            role_struct = True
            add("edit_role", sid=s["id"], name=s["name"], changes=ch)

    # categories
    cat_struct = False
    for s in snap["categories"]:
        live = cur_cats.get(idmap.get(s["id"]))
        if live is None:
            cat_struct = True
            add("create_category", sid=s["id"], name=s["name"])
        elif live["name"] != s["name"]:
            cat_struct = True
            add("edit_category", sid=s["id"], name=s["name"], changes={"name": s["name"]})

    # channels
    chan_struct = False
    for s in snap["channels"]:
        if s["type"] not in RESTORABLE_CHANNEL_TYPES:
            notes.append(f"channel {s['name']!r} has unsupported type {s['type']!r}: not restorable")
            continue
        live = cur_chans.get(idmap.get(s["id"]))
        fields = ("name", "topic", "nsfw", "slowmode", "bitrate", "user_limit")
        if live is None:
            chan_struct = True
            add("create_channel", sid=s["id"], name=s["name"], ctype=s["type"], category_sid=s["category_id"],
                data={k: s[k] for k in fields})
            continue
        ch = {k: s[k] for k in fields if live[k] != s[k]}
        want_parent = idmap.get(s["category_id"]) if s["category_id"] is not None else None
        # a parent that does not exist yet (it is about to be recreated) can never equal the channel's current one
        parent_changes = (s["category_id"] is not None and (s["category_id"] not in idmap or want_parent != live["category_id"])) or \
                         (s["category_id"] is None and live["category_id"] is not None)
        if parent_changes:
            ch["category_sid"] = s["category_id"]
        if ch:
            chan_struct = True
            add("edit_channel", sid=s["id"], name=s["name"], changes=ch)

    # overwrites (categories + channels share one id namespace)
    for s in snap["categories"] + [c for c in snap["channels"] if c["type"] in RESTORABLE_CHANNEL_TYPES]:
        live = cur_cats.get(idmap.get(s["id"])) or cur_chans.get(idmap.get(s["id"]))
        exp, unresolved = _expected_overwrites(s["overwrites"], idmap)
        if live is None:
            if s["overwrites"]:
                add("set_overwrites", sid=s["id"], name=s["name"], overwrites=s["overwrites"])
            continue
        have = {(o["kind"], o["id"]): (o["allow"], o["deny"]) for o in live["overwrites"]}
        if have != exp or unresolved:
            add("set_overwrites", sid=s["id"], name=s["name"], overwrites=s["overwrites"])

    # extras: whatever is live but not in the snapshot was added by the test run
    mapped_live = set(idmap.values())
    for c in cur["channels"]:
        if c["id"] not in mapped_live:
            add("delete_channel", live_id=c["id"], name=c["name"])
    for c in cur["categories"]:
        if c["id"] not in mapped_live:
            add("delete_category", live_id=c["id"], name=c["name"])
    for r in cur["roles"]:
        if r["id"] not in mapped_live and not r["managed"] and not r["default"]:
            add("delete_role", live_id=r["id"], name=r["name"])

    # order (relative order only: absolute numbers shift whenever anything is created or deleted)
    deleted = {o["live_id"] for o in ops if o["op"].startswith("delete_")}
    live_roles = [r for r in cur["roles"] if r["id"] not in deleted and not r["managed"] and not r["default"]]
    snap_roles = [r for r in snap["roles"] if not r["managed"] and not r["default"]]
    want_roles = _order(snap_roles, idmap, lambda r: r["position"])
    have_roles = [r["id"] for r in sorted(live_roles, key=lambda r: (r["position"], r["id"])) if r["id"] in set(want_roles)]
    if role_struct or have_roles != want_roles:
        if want_roles:
            add("reorder_roles", order=[r["id"] for r in sorted(snap_roles, key=lambda r: (r["position"], r["id"]))])

    live_cats = [c for c in cur["categories"] if c["id"] not in deleted]
    want_cats = _order(snap["categories"], idmap, lambda c: c["position"])
    have_cats = [c["id"] for c in sorted(live_cats, key=lambda c: (c["position"], c["id"])) if c["id"] in set(want_cats)]
    groups: Dict[Optional[int], List[int]] = {}
    for s in sorted([c for c in snap["channels"] if c["type"] in RESTORABLE_CHANNEL_TYPES], key=lambda c: (c["position"], c["id"])):
        groups.setdefault(s["category_id"], []).append(s["id"])
    order_wrong = have_cats != want_cats
    for parent, sids in groups.items():
        want = [idmap[i] for i in sids if i in idmap]
        live_parent = idmap.get(parent) if parent is not None else None
        have = [c["id"] for c in sorted(cur["channels"], key=lambda c: (c["position"], c["id"]))
                if c["id"] not in deleted and c["category_id"] == live_parent and c["id"] in set(want)]
        if have != want:
            order_wrong = True
    if cat_struct or chan_struct or order_wrong:
        add("reorder_channels", categories=[c["id"] for c in sorted(snap["categories"], key=lambda c: (c["position"], c["id"]))],
            groups=[{"parent": p, "order": sids} for p, sids in groups.items()])

    # settings
    want = snap.get("settings") or {}
    have = cur.get("settings") or {}
    changes = {}
    if "verification_level" in want and want["verification_level"] != have.get("verification_level"):
        changes["verification_level"] = want["verification_level"]
    for k in ("system_channel_id", "rules_channel_id"):
        sid = want.get(k)
        if sid is not None and idmap.get(sid) != have.get(k):
            changes[k] = sid
    if changes:
        add("edit_settings", changes=changes)

    return {"ops": ops, "idmap": idmap, "notes": notes}


def describe(op: Dict[str, Any]) -> str:
    kind = op["op"]
    name = op.get("name")
    if kind in ("create_role", "create_category", "create_channel"):
        return f"recreate {kind.split('_')[1]} {name!r}"
    if kind in ("edit_role", "edit_category", "edit_channel"):
        return f"fix {kind.split('_')[1]} {name!r} ({', '.join(sorted(op['changes']))})"
    if kind.startswith("delete_"):
        return f"remove added {kind.split('_')[1]} {name!r}"
    if kind == "set_overwrites":
        return f"fix permission overwrites on {name!r}"
    if kind == "edit_settings":
        return f"fix server settings ({', '.join(sorted(op['changes']))})"
    return kind.replace("_", " ")


# ── apply ─────────────────────────────────────────────────────────────────

def _live_overwrites(guild, sov, idmap) -> Dict[Any, discord.PermissionOverwrite]:
    out = {}
    for o in sov:
        target = guild.get_role(idmap.get(o["id"])) if o["kind"] == "role" and idmap.get(o["id"]) else \
            (guild.get_member(o["id"]) if o["kind"] == "member" else None)
        if target is not None:
            out[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(o["allow"]), discord.Permissions(o["deny"]))
    return out


class _Skip(Exception):
    """An op that cannot run because something it depends on was not restored. Reported, but it is not a second failure."""


async def _do(guild, op, idmap, reason):
    kind = op["op"]
    cat = lambda sid: (guild.get_channel(idmap[sid]) if sid is not None and sid in idmap else None)  # noqa: E731
    if kind == "create_role":
        d = op["data"]
        r = await guild.create_role(name=d["name"], colour=discord.Colour(d["color"]), permissions=discord.Permissions(d["permissions"]),
                                    hoist=d["hoist"], mentionable=d["mentionable"], reason=reason)
        idmap[op["sid"]] = r.id
    elif kind == "edit_role":
        if op["sid"] not in idmap:
            raise _Skip("its role was not restored")
        c = dict(op["changes"])
        if "color" in c:
            c["colour"] = discord.Colour(c.pop("color"))
        if "permissions" in c:
            c["permissions"] = discord.Permissions(c["permissions"])
        await guild.get_role(idmap[op["sid"]]).edit(reason=reason, **c)
    elif kind == "create_category":
        idmap[op["sid"]] = (await guild.create_category(op["name"], reason=reason)).id
    elif kind == "edit_category":
        if op["sid"] not in idmap:
            raise _Skip("its category was not restored")
        await guild.get_channel(idmap[op["sid"]]).edit(name=op["changes"]["name"], reason=reason)
    elif kind == "create_channel":
        d, parent = op["data"], cat(op["category_sid"])
        if op["category_sid"] is not None and parent is None:
            raise RuntimeError("its category could not be restored first")
        common = dict(category=parent, reason=reason)
        t = op["ctype"]
        if t in ("text", "news"):
            ch = await guild.create_text_channel(d["name"], topic=d["topic"], nsfw=d["nsfw"], slowmode_delay=d["slowmode"],
                                                 news=(t == "news"), **common)
        elif t == "voice":
            kw = {k: v for k, v in (("bitrate", d["bitrate"]), ("user_limit", d["user_limit"])) if v is not None}
            ch = await guild.create_voice_channel(d["name"], **kw, **common)
        elif t == "stage_voice":
            ch = await guild.create_stage_channel(d["name"], **common)
        else:
            ch = await guild.create_forum(d["name"], topic=d["topic"], nsfw=d["nsfw"], slowmode_delay=d["slowmode"], **common)
        idmap[op["sid"]] = ch.id
    elif kind == "edit_channel":
        if op["sid"] not in idmap:
            raise _Skip("its channel was not restored")
        c = dict(op["changes"])
        if "category_sid" in c:
            sid = c.pop("category_sid")
            parent = cat(sid)
            if sid is not None and parent is None:
                raise RuntimeError("its category could not be restored first")
            c["category"] = parent
        if "slowmode" in c:
            c["slowmode_delay"] = c.pop("slowmode")
        await guild.get_channel(idmap[op["sid"]]).edit(reason=reason, **c)
    elif kind == "set_overwrites":
        if op["sid"] not in idmap:
            raise _Skip("its channel was not restored")
        ch = guild.get_channel(idmap[op["sid"]])
        await ch.edit(overwrites=_live_overwrites(guild, op["overwrites"], idmap), reason=reason)
    elif kind in ("delete_channel", "delete_category"):
        ch = guild.get_channel(op["live_id"])
        if ch is not None:
            await ch.delete(reason=reason)
    elif kind == "delete_role":
        r = guild.get_role(op["live_id"])
        if r is not None:
            await r.delete(reason=reason)
    elif kind == "reorder_roles":
        positions = {}
        for pos, sid in enumerate(op["order"], start=1):
            r = guild.get_role(idmap.get(sid)) if sid in idmap else None
            if r is not None:
                positions[r] = pos
        if positions:
            await guild.edit_role_positions(positions=positions, reason=reason)
    elif kind == "reorder_channels":
        for i, sid in enumerate(op["categories"]):
            c = cat(sid)
            if c is not None and c.position != i:
                await c.edit(position=i, reason=reason)
        for g in op["groups"]:
            for i, sid in enumerate(g["order"]):
                c = cat(sid)
                if c is not None and c.position != i:
                    await c.edit(position=i, reason=reason)
    elif kind == "edit_settings":
        c, kw = op["changes"], {}
        if "verification_level" in c:
            kw["verification_level"] = discord.VerificationLevel(c["verification_level"])
        if "system_channel_id" in c:
            kw["system_channel"] = cat(c["system_channel_id"])
        if "rules_channel_id" in c:
            kw["rules_channel"] = cat(c["rules_channel_id"])
        await guild.edit(reason=reason, **kw)
    else:
        raise ValueError(f"unknown op {kind}")


async def apply_plan(guild, plan: Dict[str, Any], *, reason: str, pause: float = 0.5, sleep=asyncio.sleep,
                     max_consecutive_failures: int = 5, still_allowed=lambda: True) -> Dict[str, Any]:
    """Run the ops one at a time with a pause between Discord calls. A failed op is recorded and skipped (a restore
    that gives up on the first hiccup is worse than one that reports what it could not do); five failures in a row
    abort. `still_allowed()` is the handler gate, re-checked before EVERY op."""
    idmap = dict(plan["idmap"])
    done, failed, skipped, aborted, streak = 0, [], [], False, 0
    for op in plan["ops"]:
        if not still_allowed():
            aborted = True
            failed.append(("gate closed mid-restore", "the test-guild gate no longer passes"))
            break
        try:
            await _do(guild, op, idmap, reason)
            done += 1
            streak = 0
        except asyncio.CancelledError:
            raise
        except _Skip as exc:
            skipped.append((describe(op), str(exc)))
        except Exception as exc:  # noqa: BLE001 -- reported, never swallowed silently
            failed.append((describe(op), f"{type(exc).__name__}: {exc}"[:200]))
            streak += 1
            if streak >= max_consecutive_failures:
                aborted = True
                break
        await sleep(pause)
    return {"done": done, "failed": failed, "skipped": skipped, "aborted": aborted, "idmap": idmap}


async def restore(guild, snap: Dict[str, Any], *, reason: str, pause: float = 0.5, sleep=asyncio.sleep,
                  still_allowed=lambda: True) -> Dict[str, Any]:
    """Plan, apply, then re-capture and re-plan. result["remaining"] is the list of differences still left (empty =
    verified restored); result["notes"] and result["failed"] are the honest could-not-restore lists."""
    if not verify(snap):
        return {"ok": False, "error": "snapshot failed its integrity check", "done": 0, "failed": [], "skipped": [], "remaining": [], "notes": [], "idmap": {}}
    plan = plan_restore(snap, capture(guild))
    res = await apply_plan(guild, plan, reason=reason, pause=pause, sleep=sleep, still_allowed=still_allowed)
    after = plan_restore(snap, capture(guild))
    remaining = [describe(o) for o in after["ops"]]
    ok = not res["failed"] and not res["aborted"] and not remaining
    return {"ok": ok, "error": None, "planned": len(plan["ops"]), "done": res["done"], "failed": res["failed"],
            "aborted": res["aborted"], "skipped": res["skipped"], "remaining": remaining, "notes": sorted(set(plan["notes"] + after["notes"])),
            "idmap": res["idmap"] if res["idmap"] else {}}
