# path: discord_bot/cogs/_ai_interaction_proxy.py

"""
ProxyInteraction stands in for a real discord.Interaction ONLY when a
command is selected from reply/mention chat (a plain discord.Message),
where no real Interaction exists at all.

Deliberately narrow. It implements exactly the surface that
modules.ai_command_guard's resolve_and_check/get_qualifying_commands/
send_cap_reached_prompt and a NON-mutating, non-confirmation-required
command callback actually touch: guild, guild_id, user, channel,
channel_id, client, permissions, command, response.send_message/defer/
edit_message/is_done, followup.send.

What it deliberately does NOT attempt to fully support:
  - Any command that needs real Discord-issued interaction internals
    beyond that surface — modals, edit_original_response, multi-step
    wizards with component state keyed to the interaction, cooldown
    buckets keyed off interaction internals.
  - Ephemeral replies — there's no such thing on a plain channel
    message, so `ephemeral=` is accepted and silently ignored; every
    reply here is a normal visible message.

This is why discord_bot.cogs.ai_tools only ever uses ProxyInteraction,
with execute_ai_command(invoke_directly=True), for commands whose
AICommandSpec.requires_confirmation is False — i.e. pure lookups with
nothing to mutate. Anything that mutates state (kick/ban/timeout/etc.)
always runs off a genuine Interaction: either it isn't reachable from
chat at all without a confirm click, and that confirm click is a real
Discord button-click Interaction regardless of whether the original
request came from /aichat or a plain reply — see
AIToolsCog._dispatch_tool_call.
"""

import discord


class _ProxyResponse:
    def __init__(self, message: discord.Message):
        self._message = message
        self._done = False
        self.sent_message = None

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, embeds=None,
                            view=None, ephemeral: bool = False, **_kw):
        self._done = True
        kwargs = {}
        if embed is not None:
            kwargs["embed"] = embed
        if embeds is not None:
            kwargs["embeds"] = embeds
        if view is not None:
            kwargs["view"] = view
        self.sent_message = await self._message.reply(content, mention_author=False, **kwargs)
        return self.sent_message

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False):
        self._done = True

    async def edit_message(self, *, content=None, embed=None, view=None, **_kw):
        # There's no "original response" object outside a real interaction
        # to edit in place — best effort is a fresh message instead.
        await self.send_message(content=content, embed=embed, view=view)


class _ProxyFollowup:
    def __init__(self, message: discord.Message):
        self._message = message

    async def send(self, content=None, *, embed=None, embeds=None, view=None,
                    ephemeral: bool = False, wait: bool = False, file=None, **_kw):
        kwargs = {}
        if embed is not None:
            kwargs["embed"] = embed
        if embeds is not None:
            kwargs["embeds"] = embeds
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        return await self._message.channel.send(content, **kwargs)


class ProxyInteraction:
    """See module docstring. Only ever pass to execute_ai_command with
    invoke_directly=True, and only for non-confirmation-required specs."""

    def __init__(self, message: discord.Message, bot: discord.Client):
        self.message = message
        self.guild = message.guild
        self.guild_id = message.guild.id if message.guild else None
        self.user = message.author
        self.channel = message.channel
        self.channel_id = message.channel.id
        self.client = bot
        self.command = None
        self.permissions = (
            message.channel.permissions_for(message.author)
            if message.guild else discord.Permissions.none()
        )
        self.response = _ProxyResponse(message)
        self.followup = _ProxyFollowup(message)

    @property
    def created_at(self):
        return self.message.created_at
