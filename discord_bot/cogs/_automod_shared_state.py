# FULL PATH: PRIME-BOT-main/discord_bot/cogs/_automod_shared_state.py

"""
Tiny in-process, cross-cog dedup trackers shared between automod.py and
server_logs.py.

Why this exists: Discord's audit log always attributes a bot-performed
kick/timeout to the BOT itself, regardless of which admin ran the slash
command that triggered it — there's no way to tell, from the audit log
alone, "automod kicked this" apart from "an admin's /kick command kicked
this" (both show the bot as executor). But automod.py's _enforce() (and
its raid-protection on_member_join handler) already post their own
"🛡️ Auto-mod action" embed for kicks/timeouts THEY perform, so
server_logs.py's Moderation-category listeners need a way to recognize
"this specific kick/timeout was automod's own doing" and skip it, while
still logging a plain /kick-command kick (which posts no embed of its
own) or manual timeout.

Same idea as AutomodCog._self_deleted_ids (mark right before performing
the action, consumed exactly once by the listener that would otherwise
double-log it) — just promoted to module level since the marking side
(AutomodCog) and the consuming side (ServerLogsCog) are different cog
instances in different files.
"""

# (guild_id, member_id) pairs automod.py is ABOUT to kick via its own
# _enforce()/raid-protection, added just before the kick() call and
# consumed (popped) by ServerLogsCog.on_member_remove.
SELF_KICKED_MEMBER_IDS: set[tuple[int, int]] = set()

# (guild_id, member_id) pairs automod.py is ABOUT to timeout via its own
# _enforce(), added just before the timeout() call and consumed (popped)
# by ServerLogsCog.on_member_update.
SELF_TIMED_OUT_MEMBER_IDS: set[tuple[int, int]] = set()


def mark_self_kick(guild_id: int, member_id: int) -> None:
    SELF_KICKED_MEMBER_IDS.add((guild_id, member_id))


def unmark_self_kick(guild_id: int, member_id: int) -> None:
    """Call if the kick attempt actually failed, so a stale marker can't
    suppress a later, genuinely-different kick of the same member."""
    SELF_KICKED_MEMBER_IDS.discard((guild_id, member_id))


def consume_self_kick(guild_id: int, member_id: int) -> bool:
    """Returns True (and clears the marker) if this member was just kicked
    by automod's own logic; False otherwise. Consumes on read so it only
    suppresses the ONE on_member_remove event it was set for."""
    key = (guild_id, member_id)
    if key in SELF_KICKED_MEMBER_IDS:
        SELF_KICKED_MEMBER_IDS.discard(key)
        return True
    return False


def mark_self_timeout(guild_id: int, member_id: int) -> None:
    SELF_TIMED_OUT_MEMBER_IDS.add((guild_id, member_id))


def unmark_self_timeout(guild_id: int, member_id: int) -> None:
    SELF_TIMED_OUT_MEMBER_IDS.discard((guild_id, member_id))


def consume_self_timeout(guild_id: int, member_id: int) -> bool:
    key = (guild_id, member_id)
    if key in SELF_TIMED_OUT_MEMBER_IDS:
        SELF_TIMED_OUT_MEMBER_IDS.discard(key)
        return True
    return False
