"""Find a guild's owner as something you can DM.

discord.py has no owner-fetch method on Guild. The old inline lookup called one anyway and raised
AttributeError whenever the owner wasn't cached (clones, cold member cache), which aborted on_guild_join before the
owner join DM was sent and broke the automod reminders. This helper uses the real API: the cache first, then an HTTP
fetch of the owner as a member, then as a plain user. Never raises for Discord errors; returns None instead.
"""
import discord


async def resolve_guild_owner(guild: discord.Guild):
    cached = guild.owner
    if cached is not None:
        return cached
    owner_id = getattr(guild, "owner_id", None)
    if not owner_id:
        return None
    try:
        return await guild.fetch_member(owner_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    try:
        return await guild._state._get_client().fetch_user(owner_id)
    except Exception:
        return None
