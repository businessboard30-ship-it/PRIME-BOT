"""Cat screens are public in a server, but only their owner can use them."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _cat_visibility as vis


def run(coro):
    return asyncio.run(coro)


def _inter(user_id, guild_id=7, done=False):
    i = MagicMock()
    i.user.id = user_id
    i.guild_id = guild_id
    i.response.is_done = lambda: done
    i.response.send_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def _view():
    return discord.ui.View(timeout=60)


def test_public_in_a_server_private_in_dms_and_for_private_screens():
    assert vis.is_public(_inter(1)) is True
    assert vis.is_public(_inter(1, guild_id=None)) is False        # DMs: unchanged
    assert vis.is_public(_inter(1), public=False) is False         # errors, hidden profile, etc.


def test_send_is_public_and_locks_the_view_to_the_opener():
    i, v = _inter(1), _view()
    run(vis.send(i, embed=discord.Embed(title="x"), view=v))
    assert i.response.send_message.await_args.kwargs["ephemeral"] is False
    stranger, owner = _inter(2), _inter(1)
    assert run(v.interaction_check(stranger)) is False
    assert "belongs to <@1>" in stranger.response.send_message.await_args.args[0]
    assert stranger.response.send_message.await_args.kwargs["ephemeral"] is True    # the note is private
    assert run(v.interaction_check(owner)) is True


def test_private_send_does_not_lock_and_uses_followup_when_deferred():
    i, v = _inter(1, done=True), _view()
    run(vis.send(i, public=False, content="oops", view=v))
    assert i.followup.send.await_args.kwargs["ephemeral"] is True
    assert not hasattr(v, "_cat_owner_lock")


def test_lock_chains_with_a_views_own_check_and_is_idempotent():
    class V(discord.ui.View):
        async def interaction_check(self, interaction):
            return interaction.user.id != 99          # its own rule: user 99 is never allowed
    v = V(timeout=60)
    vis.lock_to_owner(v, 99)
    vis.lock_to_owner(v, 99)
    assert run(v.interaction_check(_inter(99))) is False           # owner, but the view's own rule still applies
    assert run(v.interaction_check(_inter(5))) is False            # not the owner
    v2 = V(timeout=60)
    vis.lock_to_owner(v2, 5)
    assert run(v2.interaction_check(_inter(5))) is True


def test_a_public_menu_cannot_be_used_by_someone_else_through_its_select():
    from discord_bot.cogs import _views_cat_hub as hub
    v = hub.HubView(None, can_setup=False)
    vis.lock_to_owner(v, 1)
    assert run(v.interaction_check(_inter(2))) is False
