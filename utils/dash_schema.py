# path: utils/dash_schema.py

"""
Settings catalogue + validation for the web dashboard (dashboard/ on Cloudflare Pages).

Each module maps to one existing database get_*/set_*_config pair, so the dashboard
reads and writes exactly what the slash commands and in-Discord wizards use. The
frontend renders forms straight from this catalogue, so adding a setting is one entry
here and nothing else.

Pure functions only (no I/O): everything is unit-tested in tests/unit/test_dash_schema.py.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def F(key, label, type_, help_="", **kw) -> dict:
    d = {"key": key, "label": label, "type": type_, "help": help_}
    d.update(kw)
    return d


def opts(*pairs) -> list:
    return [[v, l] for v, l in pairs]


MODULES: List[dict] = [
    {
        "id": "welcome", "title": "Welcome", "category": "Onboarding", "icon": "wave",
        "desc": "Greet new members with a welcome card and message.",
        "get": "get_welcome_config", "set": "set_welcome_config",
        "fields": [
            F("enabled", "Welcome messages", "toggle", "Post a welcome when someone joins."),
            F("channel_id", "Welcome channel", "channel", "Where the welcome is posted.", kind="text"),
            F("message_template", "Message", "textarea", "Placeholders: {member}, {guild}, {count}.", maxlen=500),
            F("use_template", "Designed card", "toggle", "On: artwork card with a theme. Off: plain colour card (uses the colours below)."),
            F("card_theme", "Card theme", "select", "Artwork used by the designed card. Only Wolf is free.",
              options=opts(("wolf", "Wolf (free)"), ("reaper", "Reaper"), ("shadow", "Shadow"), ("sorcerer", "Sorcerer"),
                           ("spider", "Spider"), ("spider_pro", "Spider Pro")),
              premium_values=["reaper", "shadow", "sorcerer", "spider", "spider_pro"]),
            F("avatar_shape", "Avatar shape", "select", "Shape of the member's picture on the card.",
              options=opts(("circle", "Circle"), ("rounded_square", "Rounded square"), ("square", "Square"),
                           ("hexagon", "Hexagon"), ("diamond", "Diamond"))),
            F("accent_color", "Accent colour", "color", "Used by the plain colour card."),
            F("background_color", "Background colour", "color", "Used by the plain colour card."),
        ],
        "designer": "welcome",
        "actions": [{"id": "welcome_test", "label": "Send test welcome",
                     "help": "Posts a sample card for you in the welcome channel (saved settings)."}],
    },
    {
        "id": "verification", "title": "Join verification", "category": "Onboarding", "icon": "shield-check",
        "desc": "Hold new members in an Unverified role until they pass a check.",
        "get": "get_verification_config", "set": "set_verification_config", "no_quick": True,
        "note": "The verify button message is posted by the bot. Run /setupverification in Discord once, then fine-tune everything here.",
        "fields": [
            F("enabled", "Verification", "toggle", "Turn the join check on or off."),
            F("mode", "Check type", "select", "Captcha sends members to a Cloudflare Turnstile page.",
              options=opts(("button", "Button click"), ("captcha", "Captcha (Cloudflare)"))),
            F("channel_id", "Verify channel", "channel", "The channel holding the verify button.", kind="text"),
            F("unverified_role_id", "Unverified role", "role", "Given on join, removed after passing."),
            F("verified_role_id", "Verified role", "role", "Given after passing."),
            F("timeout_seconds", "Time limit (seconds)", "number", "How long a member has to verify.", min=30, max=3600),
            F("max_attempts", "Max attempts", "number", "Wrong answers allowed before a kick.", min=1, max=10),
        ],
        "actions": [{"id": "verify_panel", "label": "Post verify panel",
                     "help": "Posts the verify button message in the verify channel (needs the channel and Unverified role saved). Does not lock any channels; /setupverification does that."}],
    },
    {
        "id": "antiraid", "title": "Anti-raid", "category": "Security", "icon": "siren",
        "desc": "Detect join spikes and respond automatically.",
        "get": "get_antiraid_config", "set": "set_antiraid_config",
        "fields": [
            F("enabled", "Anti-raid", "toggle", "Watch for sudden join spikes."),
            F("sensitivity", "Sensitivity", "select", "How fast joins must arrive to count as a raid.",
              options=opts(("relaxed", "Relaxed: 12 joins in 60s"), ("balanced", "Balanced: 7 joins in 30s"),
                           ("strict", "Strict: 4 joins in 15s"))),
            F("response", "When a raid starts", "select", "",
              options=opts(("alert", "Alert staff only"), ("lockdown", "Alert and lock the server down"))),
            F("joiner_action", "New joiners during a raid", "select", "",
              options=opts(("none", "Leave them alone"), ("timeout", "Timeout for 1 hour"), ("kick", "Kick"),
                           ("quarantine", "Quarantine for review (Premium)")), premium_values=["quarantine"]),
            F("lockdown_minutes", "Lockdown length", "select", "",
              options=opts((5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"), (180, "3 hours")), numeric=True),
            F("log_channel_id", "Alert channel", "channel", "Where raid alerts go.", kind="text"),
            F("alert_role_id", "Role to ping", "role", "Pinged when a raid starts."),
            F("filter_age_days", "Minimum account age", "select", "Premium: catch brand-new accounts at the door.",
              options=opts((0, "Off"), (1, "1 day"), (3, "3 days"), (7, "7 days"), (14, "14 days"), (30, "30 days")),
              numeric=True, premium=True),
            F("filter_default_avatar", "Flag default avatars", "toggle", "Premium.", premium=True),
            F("filter_suspicious_name", "Flag suspicious names", "toggle", "Premium.", premium=True),
            F("filter_action", "Filter action", "select", "Premium: what happens to flagged accounts.",
              options=opts(("flag", "Flag in the log"), ("quarantine", "Quarantine for review"), ("kick", "Kick")), premium=True),
        ],
    },
    {
        "id": "automod", "title": "Auto-moderation", "category": "Security", "icon": "gavel",
        "desc": "Filter words, invites, mention spam and message floods.",
        "get": "get_automod_config", "set": "set_automod_config",
        "fields": [
            F("action", "Punishment", "select", "What happens when a rule is broken.",
              options=opts(("delete", "Delete the message"), ("warn", "Delete and warn"),
                           ("timeout", "Timeout the member"), ("kick", "Kick the member"))),
            F("timeout_minutes", "Timeout length (minutes)", "number", "Used when the punishment is Timeout.", min=1, max=40320),
            F("log_channel_id", "Log channel", "channel", "Where actions are recorded.", kind="text"),
            F("word_filter_enabled", "Word filter", "toggle", "Block the words listed below."),
            F("banned_words", "Banned words", "list", "One per line.", maxitems=200, maxlen=60),
            F("anti_invite_enabled", "Block invite links", "toggle", "Delete other servers' invites."),
            F("anti_mention_enabled", "Mention spam", "toggle", "Punish mass pings."),
            F("anti_mention_threshold", "Mentions allowed", "number", "Per message.", min=2, max=50),
            F("spam_enabled", "Flood protection", "toggle", "Punish rapid-fire messages."),
            F("spam_flood_threshold", "Messages allowed", "number", "Within the window below.", min=3, max=50),
            F("spam_flood_window_seconds", "Window (seconds)", "number", "", min=3, max=120),
            F("min_account_age_hours", "Minimum account age (hours)", "number", "0 turns this off.", min=0, max=8760),
        ],
    },
    {
        "id": "modlog", "title": "Server logs", "category": "Security", "icon": "scroll",
        "desc": "Choose which server events are written to your log channel.",
        "get": "get_automod_config", "set": "set_automod_config",
        "fields": [
            F("log_server_enabled", "Server changes", "toggle"),
            F("log_channels_enabled", "Channel changes", "toggle"),
            F("log_roles_enabled", "Role changes", "toggle"),
            F("log_members_enabled", "Member joins and leaves", "toggle"),
            F("log_moderation_enabled", "Moderation actions", "toggle"),
            F("log_voice_enabled", "Voice activity", "toggle"),
            F("log_invites_enabled", "Invites", "toggle"),
        ],
    },
    {
        "id": "honeypot", "title": "Honeypot", "category": "Security", "icon": "bug",
        "desc": "A trap channel that catches spam bots and hacked accounts.",
        "get": "get_honeypot_config", "set": "set_honeypot_config",
        "fields": [
            F("enabled", "Honeypot", "toggle", "Act on anyone who posts in the trap channel."),
            F("action", "Punishment", "select", "Premium: free servers always ban.",
              options=opts(("ban", "Ban"), ("kick", "Kick"), ("timeout", "Timeout (28 days)")), premium=True),
            F("delete_seconds", "Delete their messages from", "select", "Premium: how much history to wipe.",
              options=opts((0, "Don't delete"), (3600, "Last hour"), (86400, "Last 24 hours"), (604800, "Last 7 days")),
              numeric=True, premium=True),
            F("log_channel_id", "Log channel", "channel", "", kind="text"),
            F("alert_role_id", "Role to ping", "role", ""),
        ],
    },
    {
        "id": "joingate", "title": "Join gate", "category": "Security", "icon": "door",
        "desc": "Screen accounts at the door by age and avatar.",
        "get": "get_join_gate_config", "set": "set_join_gate_config",
        "fields": [
            F("enabled", "Join gate", "toggle"),
            F("min_age_days", "Minimum account age (days)", "number", "", min=0, max=365),
            F("block_default_avatar", "Block default avatars", "toggle"),
            F("action", "Action", "select", "", options=opts(("alert", "Alert staff"), ("kick", "Kick"))),
        ],
    },
    {
        "id": "leveling", "title": "Leveling", "category": "Community", "icon": "trophy",
        "desc": "XP, level-up cards and leaderboards.",
        "get": "get_leveling_config", "set": "set_leveling_config",
        "fields": [
            F("announce_channel_id", "Level-up channel", "channel", "Where level-ups are announced.", kind="text"),
            F("xp_rate", "XP speed", "select", "",
              options=opts(("slow", "Slow (0.5x)"), ("default", "Normal (1x)"), ("fast", "Fast (1.5x)"))),
            F("card_style", "Level-up style", "select", "",
              options=opts(("card", "Illustrated card"), ("text", "Text only"), ("off", "No announcement"))),
            F("leaderboard_autopost_channel_id", "Auto-post leaderboard in", "channel", "Leave empty to turn off.", kind="text"),
        ],
    },
    {
        "id": "voicexp", "title": "Voice XP", "category": "Community", "icon": "mic",
        "desc": "Reward time spent in voice channels.",
        "get": "get_voice_xp_config", "set": "set_voice_xp_config", "explicit_kwargs": True,
        "fields": [
            F("enabled", "Voice XP", "toggle"),
            F("xp_per_minute", "XP per minute", "number", "", min=1, max=100),
            F("afk_channel_excluded", "Skip the AFK channel", "toggle"),
        ],
    },
    {
        "id": "starboard", "title": "Starboard", "category": "Community", "icon": "star",
        "desc": "Pin the best messages to a showcase channel.",
        "get": "get_starboard_config", "set": "set_starboard_config", "explicit_kwargs": True,
        "fields": [
            F("channel_id", "Starboard channel", "channel", "Empty turns the starboard off.", kind="text"),
            F("threshold", "Reactions needed", "number", "", min=1, max=50),
            F("emoji", "Emoji", "text", "A single emoji.", maxlen=16),
        ],
    },
    {
        "id": "tickets", "title": "Tickets", "category": "Community", "icon": "ticket",
        "desc": "Private support channels for your members.",
        "get": "get_ticket_config", "set": "set_ticket_config",
        "fields": [
            F("panel_channel_id", "Panel channel", "channel", "Where the Open Ticket button message is posted.", kind="text"),
            F("support_role_id", "Support role", "role", "Can see and answer tickets."),
            F("category_id", "Ticket category", "channel", "New tickets are created here.", kind="category"),
            F("welcome_message", "Opening message", "textarea", "Posted inside each new ticket.", maxlen=1000),
        ],
        "actions": [{"id": "ticket_panel", "label": "Post ticket panel",
                     "help": "Posts the Open Ticket button message in the panel channel (saved settings)."}],
    },
    {
        "id": "suggestions", "title": "Suggestions", "category": "Community", "icon": "bulb",
        "desc": "Where approved suggestions are announced.",
        "get": "get_suggestion_config", "set": "set_suggestion_config", "explicit_kwargs": True,
        "fields": [
            F("approved_log_channel_id", "Approved suggestions channel", "channel", "", kind="text"),
        ],
    },
    {
        "id": "invites", "title": "Invite tracker", "category": "Community", "icon": "link",
        "desc": "See who invited each member.",
        "get": "get_invite_tracker_config", "set": "set_invite_tracker_config",
        "fields": [
            F("enabled", "Invite tracking", "toggle"),
            F("channel_id", "Invite log channel", "channel", "", kind="text"),
            F("leaderboard_autopost_channel_id", "Auto-post leaderboard in", "channel", "Leave empty to turn off.", kind="text"),
        ],
    },
    {
        "id": "economy", "title": "Economy", "category": "Fun", "icon": "coins",
        "desc": "Your server's currency and how people earn it.",
        "get": "get_economy_config", "set": "set_economy_config",
        "fields": [
            F("currency_name", "Currency name", "text", "", maxlen=24),
            F("currency_symbol", "Currency symbol", "text", "An emoji or short symbol.", maxlen=16),
            F("daily_amount", "Daily reward", "number", "", min=0, max=1000000),
            F("work_min", "Work: minimum", "number", "", min=0, max=1000000),
            F("work_max", "Work: maximum", "number", "", min=0, max=1000000),
            F("beg_min", "Beg: minimum", "number", "", min=0, max=100000),
            F("beg_max", "Beg: maximum", "number", "", min=0, max=100000),
            F("rob_cooldown_hours", "Rob cooldown (hours)", "number", "", min=0, max=168),
            F("rob_success_chance", "Rob success chance (%)", "number", "", min=0, max=100),
            F("vote_bonus_enabled", "Vote bonus", "toggle", "Bonus for voting on Top.gg."),
            F("vote_bonus_amount", "Vote bonus amount", "number", "", min=0, max=1000000),
            F("vote_cooldown_hours", "Vote cooldown (hours)", "number", "", min=1, max=168),
        ],
    },
    {
        "id": "bumpnet", "title": "Bump network", "category": "Community", "icon": "link",
        "desc": "Receive bump posts from other servers and choose which ones.",
        "get": "get_bump_settings_config", "set": "set_bump_settings_config", "no_quick": True,
        "note": "Same settings as /bumpsetup. The owner can switch the whole bump feature off; if so nothing is posted whatever you set here.",
        "fields": [
            F("receives_bumps", "Receive bumps", "toggle", "Let the bump network post in your bump channel."),
            F("bump_channel_id", "Bump channel", "channel", "Where incoming bumps are posted. Pick a channel to change it.", kind="text"),
            F("language", "Language filter", "select", "Only receive bumps in this language.",
              options=opts(("any", "Any language"), ("en", "English"), ("fr", "French"), ("es", "Spanish"),
                           ("pt", "Portuguese"), ("ar", "Arabic"))),
            F("nsfw_opt_in", "Allow NSFW listings", "toggle", "Off keeps 18+ servers out of your bump channel."),
            F("intensity_level", "Intensity", "select", "How many incoming bumps you want.",
              options=opts(("1", "1 - Low"), ("2", "2 - Light"), ("3", "3 - Normal"), ("4", "4 - Frequent"), ("5", "5 - High"))),
        ],
    },
    {
        "id": "customrole", "title": "Custom roles", "category": "Community", "icon": "star", "no_quick": True,
        "desc": "Let members create and restyle their own role.",
        "get": "get_custom_role_settings_config", "set": "set_custom_role_settings_config",
        "note": "Same switch as /customrole disable_feature. Members still need Premium or a purchase to use it; the panel itself is posted from Discord.",
        "fields": [
            F("enabled", "Custom roles", "toggle", "Turn the feature on or off for this server."),
        ],
    },
]

BY_ID: Dict[str, dict] = {m["id"]: m for m in MODULES}
CATEGORIES = ["Onboarding", "Security", "Community", "Fun"]


def public_schema() -> dict:
    """What the browser needs to render forms: no DB method names."""
    mods = []
    for m in MODULES:
        mods.append({k: v for k, v in m.items() if k not in ("get", "set", "explicit_kwargs")})
    return {"categories": CATEGORIES, "modules": mods}


# ───────────────────────── permissions ─────────────────────────

def can_manage(owner_id: Optional[int], user_id: int, member_role_ids: List[int],
               roles_by_id: Dict[int, int], guild_id: int) -> bool:
    """roles_by_id maps role id -> permission bitfield. Mirrors Discord's own rule:
    owner, or Administrator, or Manage Server."""
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    perms = int(roles_by_id.get(int(guild_id), 0))
    for rid in member_role_ids:
        perms |= int(roles_by_id.get(int(rid), 0))
    return bool(perms & (ADMINISTRATOR | MANAGE_GUILD))


KICK_MEMBERS = 0x2
BAN_MEMBERS = 0x4
RAID_REASON_PREFIX = "[anti-raid]"          # same marker modules/antiraid_pro.py writes
RAID_OPS = ("approve", "kick", "ban")


def has_permission(owner_id: Optional[int], user_id: int, member_role_ids: List[int],
                   roles_by_id: Dict[int, int], guild_id: int, mask: int) -> bool:
    """Owner, Administrator, or any bit in `mask` held through @everyone or the member's roles."""
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    perms = int(roles_by_id.get(int(guild_id), 0))
    for rid in member_role_ids:
        perms |= int(roles_by_id.get(int(rid), 0))
    return bool(perms & (ADMINISTRATOR | mask))


def top_position(member_role_ids: List[int], positions: Dict[int, int]) -> int:
    return max([int(positions.get(int(r), 0)) for r in member_role_ids] or [0])


def raid_op_allowed(op: str, may_review: bool, may_ban: bool, may_kick: bool) -> Optional[str]:
    """None when allowed, else the reason. Mirrors the in-Discord review: releasing needs Ban
    Members or Manage Server; banning needs Ban Members; kicking needs Kick Members."""
    if op not in RAID_OPS:
        return "Unknown action."
    if op == "approve" and not (may_review or may_ban):
        return "You need Manage Server or Ban Members to release people."
    if op == "ban" and not may_ban:
        return "You need the Ban Members permission to ban people."
    if op == "kick" and not may_kick:
        return "You need the Kick Members permission to kick people."
    return None


def is_raid_row(row: dict) -> bool:
    return str(row.get("reason") or "").startswith(RAID_REASON_PREFIX)


def snowflake_ms(user_id: int) -> int:
    return (int(user_id) >> 22) + 1420070400000


def guild_list_manageable(guilds: List[dict]) -> List[dict]:
    """From Discord's /users/@me/guilds payload: only servers the user can manage."""
    out = []
    for g in guilds or []:
        try:
            perms = int(g.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        if g.get("owner") or perms & (ADMINISTRATOR | MANAGE_GUILD):
            out.append({"id": str(g["id"]), "name": g.get("name") or "Server", "icon": g.get("icon"),
                        "owner": bool(g.get("owner"))})
    return out


# ───────────────────────── values in / out ─────────────────────────

def export_values(module: dict, cfg: dict) -> dict:
    """DB row -> JSON-safe dict of just this module's fields. Snowflakes become strings
    (JavaScript numbers lose precision above 2^53)."""
    out = {}
    for f in module["fields"]:
        v = cfg.get(f["key"])
        if f["type"] in ("channel", "role"):
            v = str(v) if v else None
        elif f["type"] == "list":
            v = list(v or [])
        elif f["type"] == "toggle":
            v = bool(v)
        out[f["key"]] = v
    return out


class ValidationError(ValueError):
    pass


def _coerce(f: dict, raw: Any, channels: Dict[str, set], roles: set) -> Any:
    t, label = f["type"], f["label"]
    if t == "toggle":
        if not isinstance(raw, bool):
            raise ValidationError(f"{label}: must be on or off.")
        return raw
    if t == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValidationError(f"{label}: enter a number.")
        try:
            n = int(float(raw))
        except ValueError:
            raise ValidationError(f"{label}: enter a number.")
        if n < f["min"] or n > f["max"]:
            raise ValidationError(f"{label}: must be between {f['min']} and {f['max']}.")
        return n
    if t == "select":
        for value, _ in f["options"]:
            if str(value) == str(raw):
                return int(value) if f.get("numeric") else value
        raise ValidationError(f"{label}: pick one of the listed options.")
    if t in ("text", "textarea"):
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ValidationError(f"{label}: must be text.")
        s = raw.strip()
        if len(s) > f.get("maxlen", 200):
            raise ValidationError(f"{label}: keep it under {f.get('maxlen', 200)} characters.")
        return s if s else None
    if t == "color":
        if not isinstance(raw, str) or not _HEX.match(raw):
            raise ValidationError(f"{label}: use a colour like #5865F2.")
        return raw.lower()
    if t == "list":
        if not isinstance(raw, list):
            raise ValidationError(f"{label}: must be a list.")
        items, seen = [], set()
        for x in raw:
            if not isinstance(x, str):
                raise ValidationError(f"{label}: entries must be text.")
            s = x.strip()
            if not s or s.lower() in seen:
                continue
            if len(s) > f.get("maxlen", 60):
                raise ValidationError(f"{label}: each entry must be under {f.get('maxlen', 60)} characters.")
            seen.add(s.lower())
            items.append(s)
        if len(items) > f.get("maxitems", 200):
            raise ValidationError(f"{label}: at most {f.get('maxitems', 200)} entries.")
        return items
    if t in ("channel", "role"):
        if raw in (None, ""):
            return None
        s = str(raw)
        if not s.isdigit():
            raise ValidationError(f"{label}: pick one from the list.")
        pool = roles if t == "role" else channels.get(f.get("kind", "text"), set())
        if s not in pool:
            raise ValidationError(f"{label}: that {t} isn't in this server.")
        return int(s)
    raise ValidationError(f"{label}: unsupported field.")


def validate_values(module: dict, values: Any, channels: Dict[str, set], roles: set,
                    is_premium: bool) -> Tuple[Dict[str, Any], List[str]]:
    """Only keys present in `values` are validated and returned, so the browser can
    send just what changed. Unknown keys are rejected, never silently written."""
    clean, errors = {}, []
    if not isinstance(values, dict) or not values:
        return {}, ["Nothing to save."]
    fields = {f["key"]: f for f in module["fields"]}
    for key, raw in values.items():
        f = fields.get(key)
        if f is None:
            errors.append(f"Unknown setting: {str(key)[:40]}")
            continue
        try:
            v = _coerce(f, raw, channels, roles)
        except ValidationError as e:
            errors.append(str(e))
            continue
        if not is_premium:
            if f.get("premium") or (v in (f.get("premium_values") or [])):
                errors.append(f"{f['label']}: this needs Premium.")
                continue
        clean[key] = v
    if not errors:
        for lo, hi in (("work_min", "work_max"), ("beg_min", "beg_max")):
            if module["id"] == "economy" and lo in clean and hi in clean and clean[lo] > clean[hi]:
                errors.append("Minimum can't be higher than maximum.")
    return clean, errors


# ───────────────────────── reset to defaults + import / export ─────────────────────────

EXPORT_FORMAT = "prime-bot-settings"
EXPORT_VERSION = 1
IMPORT_MAX_BYTES = 48 * 1024


def default_values(module: dict, default_cfg: dict) -> Tuple[Dict[str, Any], List[str]]:
    """Values that put a module back to factory settings. `default_cfg` is what the module's
    getter returns for a server with no saved row. A field whose default can't be expressed
    through the dashboard (e.g. an unset number) is left alone and named in the second item."""
    exported = export_values(module, default_cfg or {})
    clean, left = {}, []
    for f in module["fields"]:
        try:
            clean[f["key"]] = _coerce(f, exported.get(f["key"]), {}, set())
        except ValidationError:
            left.append(f["label"])
    return clean, left


def export_payload(module: dict, cfg: dict, exported_at: str) -> dict:
    """The JSON a user downloads. Only declared keys, same shape the dashboard saves."""
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "module": module["id"],
            "exported_at": exported_at, "values": export_values(module, cfg)}


def import_values(module: dict, payload: Any, channels: Dict[str, set], roles: set,
                  is_premium: bool) -> Tuple[Dict[str, Any], List[str], Optional[str]]:
    """Check an uploaded settings file against THIS server. Returns (values, skipped, error).

    Nothing is written: the dashboard loads `values` into the form as unsaved changes, and
    the normal save path validates them again. Anything that doesn't fit this server (a
    channel or role from another server, a Premium option on a free server, an out-of-range
    number, a key this version doesn't know) is skipped and named, never guessed at."""
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        return {}, [], "That isn't a PRIME BOT settings file."
    if payload.get("version") != EXPORT_VERSION:
        return {}, [], "That settings file is from a different version."
    if payload.get("module") != module["id"]:
        return {}, [], f"That file is for a different module ({str(payload.get('module'))[:40]}), not {module['title']}."
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        return {}, [], "That settings file is empty."
    fields = {f["key"]: f for f in module["fields"]}
    clean, skipped = {}, []
    for key, raw in values.items():
        f = fields.get(key)
        if f is None:
            skipped.append(f"{str(key)[:40]}: not a setting here")
            continue
        try:
            v = _coerce(f, raw, channels, roles)
        except ValidationError as e:
            skipped.append(str(e))
            continue
        if not is_premium and (f.get("premium") or v in (f.get("premium_values") or [])):
            skipped.append(f"{f['label']}: needs Premium")
            continue
        clean[key] = v
    if not clean:
        return {}, skipped, "Nothing in that file fits this server."
    return clean, skipped, None


# ───────────────────────── drop box (owner -> every dashboard admin) ─────────────────────────

DROPBOX_KINDS = ("info", "update", "warning", "maintenance")
DROPBOX_TITLE_MAX, DROPBOX_BODY_MAX = 100, 2000
DROPBOX_AUDIENCES = ("all", "premium", "min_members", "guild")   # who gets DM-pushed / sees a targeted message


def validate_dropbox(raw: Any) -> Tuple[Optional[dict], Optional[str]]:
    """Validate an owner's drop box message. Returns (clean, error)."""
    if not isinstance(raw, dict):
        return None, "Invalid message."
    title = str(raw.get("title") or "").strip()
    body = str(raw.get("body") or "").strip()
    kind = str(raw.get("kind") or "info")
    if not title:
        return None, "Title is required."
    if len(title) > DROPBOX_TITLE_MAX:
        return None, f"Title must be at most {DROPBOX_TITLE_MAX} characters."
    if not body:
        return None, "Message is required."
    if len(body) > DROPBOX_BODY_MAX:
        return None, f"Message must be at most {DROPBOX_BODY_MAX} characters."
    if kind not in DROPBOX_KINDS:
        return None, "Unknown message type."
    hours = raw.get("expires_hours")
    if hours in (None, ""):
        hours = None
    else:
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= 24 * 90:
            return None, "Expiry must be between 1 hour and 90 days."
        hours = int(hours)
    audience = str(raw.get("audience") or "all")
    if audience not in DROPBOX_AUDIENCES:
        return None, "Unknown audience."
    min_members, target_guild = None, None
    if audience == "min_members":
        mm = raw.get("min_members")
        if isinstance(mm, bool) or not isinstance(mm, (int, float)) or not 1 <= mm <= 10_000_000:
            return None, "Minimum members must be between 1 and 10,000,000."
        min_members = int(mm)
    if audience == "guild":
        gid = str(raw.get("target_guild_id") or "").strip()
        if not gid.isdigit() or not 15 <= len(gid) <= 20:
            return None, "Enter a valid server ID."
        target_guild = int(gid)
    return {"title": title, "body": body, "kind": kind,
            "announce": bool(raw.get("announce")), "expires_hours": hours,
            "push_dm": bool(raw.get("push_dm")), "audience": audience,
            "min_members": min_members, "target_guild_id": target_guild}, None


def format_dropbox_dm(title: str, body: str, kind: str) -> str:
    """Discord DM text for a drop box message (Discord's limit is 2000 characters)."""
    icon = {"info": "📢", "update": "✨", "warning": "⚠️", "maintenance": "🛠️"}.get(kind, "📢")
    head = f"{icon} **{title}**\n\n"
    tail = "\n\n_Sent from the PRIME BOT dashboard._"
    room = 2000 - len(head) - len(tail)
    if len(body) > room:
        body = body[:max(0, room - 1)] + "…"
    return head + body + tail


# ───────────────────────── audit log ─────────────────────────

AUDIT_VALUE_MAX = 200
AUDIT_RETENTION_DAYS = 180


def _audit_val(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= AUDIT_VALUE_MAX else v[:AUDIT_VALUE_MAX] + "…"
    if isinstance(v, list):
        return [_audit_val(x) for x in v[:50]]
    return v


def diff_values(module: dict, before: dict, after: dict) -> Dict[str, dict]:
    """{key: {"from": old, "to": new}} for declared fields whose value changed.
    Only schema keys are ever recorded, and long text is truncated."""
    out = {}
    for f in module["fields"]:
        k = f["key"]
        if before.get(k) != after.get(k):
            out[k] = {"from": _audit_val(before.get(k)), "to": _audit_val(after.get(k))}
    return out


# ───────────────────────── bot actions (the bot posts something) ─────────────────────────

BOT_ACTIONS = {"verify_panel": "verification", "ticket_panel": "tickets", "welcome_test": "welcome"}


RAID_REASON_MAX = 160


def raid_row_view(row: dict, member: Optional[dict]) -> dict:
    """One quarantined person as the review screen shows it. Text is plain; the page renders it
    with textContent. `member` is Discord's guild-member object, or None if they left."""
    uid = int(row["user_id"])
    user = (member or {}).get("user") or {}
    name = user.get("global_name") or user.get("username") or "Unknown user"
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=64" if user.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(uid >> 22) % 6}.png")
    created = row.get("created_at")
    reason = str(row.get("reason") or "")[len(RAID_REASON_PREFIX):].strip()
    return {"user_id": str(uid), "name": str(name)[:40], "avatar_url": avatar, "in_server": member is not None,
            "account_created_ms": snowflake_ms(uid), "reason": reason[:RAID_REASON_MAX],
            "quarantined_at": created.isoformat() if hasattr(created, "isoformat") else None,
            "roles_held": len(row.get("saved_role_ids") or [])}
# ───────────────────────── scheduled messages (same table the /schedule command uses) ─────────────────────────

SCHEDULE_MODES = ("once", "interval", "daily")
SCHEDULE_TEXT_MAX = 2000
SCHEDULE_MIN_INTERVAL_MIN = 5            # the command allows 1m; the dashboard is stricter to avoid channel spam
SCHEDULE_MAX_INTERVAL_MIN = 60 * 24 * 365
SCHEDULE_MAX_DELAY_MIN = 60 * 24 * 365
SCHEDULE_MAX_ACTIVE = 25
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def validate_schedule(raw: Any, text_channels: set, now) -> Tuple[Optional[dict], Optional[str]]:
    """-> ({channel_id, content, run_at, interval_seconds}, None) or (None, error).
    `now` is a timezone-aware datetime (injected so this stays pure and testable)."""
    from datetime import timedelta
    if not isinstance(raw, dict):
        return None, "Invalid request."
    cid = str(raw.get("channel_id") or "")
    if not cid.isdigit() or cid not in text_channels:
        return None, "Pick a text channel from this server."
    text = raw.get("content")
    if not isinstance(text, str) or not text.strip():
        return None, "Write the message to post."
    text = text.strip()
    if len(text) > SCHEDULE_TEXT_MAX:
        return None, f"Keep the message under {SCHEDULE_TEXT_MAX} characters."
    mode = raw.get("mode")

    def minutes(key, label, lo, hi):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None, f"{label}: enter a number."
        try:
            n = int(float(v))
        except ValueError:
            return None, f"{label}: enter a number."
        if n < lo or n > hi:
            return None, f"{label}: must be between {lo} and {hi} minutes."
        return n, None

    if mode == "once":
        n, err = minutes("minutes", "Delay", 1, SCHEDULE_MAX_DELAY_MIN)
        if err:
            return None, err
        return {"channel_id": int(cid), "content": text, "run_at": now + timedelta(minutes=n), "interval_seconds": None}, None
    if mode == "interval":
        n, err = minutes("minutes", "Repeat every", SCHEDULE_MIN_INTERVAL_MIN, SCHEDULE_MAX_INTERVAL_MIN)
        if err:
            return None, err
        return {"channel_id": int(cid), "content": text, "run_at": now + timedelta(minutes=n), "interval_seconds": n * 60}, None
    if mode == "daily":
        m = _TIME_RE.match(str(raw.get("time_utc") or "").strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None, "Use a 24-hour UTC time like 09:00."
        run_at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return {"channel_id": int(cid), "content": text, "run_at": run_at, "interval_seconds": 86400}, None
    return None, "Pick once, repeating or daily."


def schedule_row_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    iv = row.get("interval_seconds")
    return {"id": str(row["id"]), "channel_id": str(row["channel_id"]), "content": str(row.get("content") or "")[:SCHEDULE_TEXT_MAX],
            "next_run_at": iso(row.get("next_run_at")), "interval_seconds": int(iv) if iv else None,
            "enabled": bool(row.get("enabled")), "created_by": str(row.get("created_by")) if row.get("created_by") else None}
TICKET_STATUSES = ("open", "closed")
TICKET_MSG_MAX = 2000
TICKET_HISTORY_MAX = 500


def ticket_row_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(row["id"]), "opener_id": str(row["opener_id"]),
            "claimed_by": str(row["claimed_by"]) if row.get("claimed_by") else None,
            "status": row.get("status") if row.get("status") in TICKET_STATUSES else "open",
            "created_at": iso(row.get("created_at")), "closed_at": iso(row.get("closed_at"))}


def ticket_message_view(msg: dict) -> dict:
    """One Discord message as the transcript viewer shows it: plain text only, bounded."""
    author = msg.get("author") or {}
    return {"id": str(msg.get("id")), "at": msg.get("timestamp"),
            "author": str(author.get("global_name") or author.get("username") or "Unknown")[:40],
            "bot": bool(author.get("bot")),
            "text": str(msg.get("content") or "")[:TICKET_MSG_MAX],
            "files": [str(a.get("filename") or "file")[:80] for a in (msg.get("attachments") or [])][:10],
            "embeds": len(msg.get("embeds") or [])}
