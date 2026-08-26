# path: discord_bot/cogs/roast.py

"""
Auto-triggered roast battles — no slash command starts this, the bot
proposes it on its own.

Two triggers, both checked by one poller tick (`_poller`, every
POLL_INTERVAL_SECONDS):
  1. Inactivity: guild has had no messages for >= its configured
     inactivity_minutes (default 60, see discord_roast_config).
  2. Random chance: even while active, every random_check_minutes the bot
     rolls random_chance_percent odds to propose one anyway — this is the
     "randomly" behavior from the spec, independent of inactivity.
Either firing sends a DM to every member with Administrator permission in
that guild, with a target picker (dropdown of guild members) and a channel
picker (dropdown of text channels). Whoever picks a target+channel first
locks the challenge — the DM is edited to reflect that in every admin's
inbox so there's no race where two admins both send challenges.

Flow after an admin picks target+channel:
  1. Row created in discord_roast_battles, status='pending', expires_at =
     now + CHALLENGE_EXPIRY_MINUTES.
  2. Bot DMs the target with an Accept/Decline view.
  3a. No response within 30 min -> _poller expires it, bot auto-wins,
      posts an announcement in the chosen channel, status='expired'.
  3b. Target declines -> status='ended', quiet cancel, no announcement
      (declining isn't a loss condition, just a no).
  3c. Target accepts -> status='active', bot posts the first roast in the
      channel with a RoastBattleView attached (Join Roast / Quit Roast).
  4. Every subsequent target message in that channel while status='active'
     gets a comeback roast from the bot (channel-scoped listener,
     `on_message`). Nothing else in the codebase currently listens for
     replies inside a specific active row like this, so state lives in
     `self._active_by_channel: dict[channel_id, battle_id]` for O(1)
     lookup on every message instead of a query per message.
  5. Joining: anyone who REPLIES to one of the bot's roast messages in the
     channel gets auto-pulled into joined_ids and roasted back — no
     button needed, lower friction than hunting for a "Join Roast" click.
  6. Quit Roast: pressable by the target, anyone in joined_ids, or any
     Administrator. Sets status='ended', disables the view, removes the
     channel from the active map.

Punchlines: PUNCHLINE_BANK below seeds both the AI system prompt (a few
random examples per call, so the model's roast style matches what was
supplied) and doubles as the offline fallback pool if GROQ_API_KEY is
unset or the API call fails.
"""

import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
ROAST_MODEL = "llama-3.1-70b-versatile"

POLL_INTERVAL_SECONDS = 60
# Minimum time between admin roast-suggestion DMs, regardless of trigger.
PROPOSAL_COOLDOWN_MINUTES = 60 * 24 * 2  # 2 days
# "Remind me later" snooze — shorter than the normal cooldown so the admin
# actually gets asked again soon instead of waiting out the full 2 days.
SNOOZE_MINUTES = 60 * 6  # 6 hours
CHALLENGE_EXPIRY_MINUTES = 30
DEFAULT_INACTIVITY_MINUTES = 60
DEFAULT_RANDOM_CHECK_MINUTES = 30
DEFAULT_RANDOM_CHANCE_PERCENT = 10
BOT_CONCEDE_CHANCE_PERCENT = 30  # odds the bot "takes the L" instead of roasting back

PUNCHLINE_BANK = [
    "You move through life like autocorrect — confident and always wrong.",
    "You're the reason we have warning labels on shampoo.",
    "Two billion years of evolution, and you turned out like this.",
    "Were you born this way, or did you take lessons?",
    "Your confidence and your results have never met.",
    "You're as useless as the \"g\" in lasagna.",
    "You bring joy... mostly to comedians.",
    "You are the human equivalent of a participation award.",
    "I'd take a bullet for you. From a water gun.",
    "You treat your own advice like terms and conditions — for others only.",
    "You built like a windshield wiper.",
    "You're the type of person to respond to spam emails.",
    "You're not stupid — you just have bad luck with thinking.",
    "I'll never forget the first time we met, but I'll keep trying.",
    "Every time I think you can't get any dumber, you prove me wrong.",
    "Our friendship is all about balance. You start talking... I stop listening.",
    "If laziness were a competition, you'd come in second because you'd be too lazy to compete.",
    "Do you exist to annoy people?",
    "If I give you a dollar, will you leave?",
    "You skipped the \"being normal\" gene.",
    "Congratulations on getting your PhD in annoyance.",
    "Let's play a game. For the rest of the week, don't talk to me.",
    "You're like a cloud. When you disappear, it's a beautiful day.",
    "Don't you ever get exhausted from talking about yourself all the time?",
    "Shock me. Say something intelligent.",
    "I'm not insulting you. I'm describing you.",
    "If you had two brains, you'd be twice as stupid.",
    "Remember when I asked for your opinion? Me either.",
    "Whoever told you to be yourself gave you really bad advice.",
    "You have your entire life to be a jerk. Why not take today off?",
    "I would say you're dumb as a rock, but at least a rock can hold the door open.",
]

CONCEDE_LINES = [
    "Okay okay, you got me with that one. 😭",
    "I have no comeback for that. You win this round.",
    "Bro really cooked me. Taking the L on that.",
    "That one actually hurt my circuits. GG.",
    "I'm a bot and even I felt that.",
    "Alright, that was actually kind of fire. Respect.",
]

ROAST_SYSTEM_PROMPT = (
    "You are a savage but PLAYFUL roast-battle comedian bot in a Discord "
    "server. Write ONE short, punchy roast (1-3 sentences, under 300 "
    "characters) aimed at the given display name, in the same style as "
    "these examples:\n"
    + "\n".join(f"- {line}" for line in random.sample(PUNCHLINE_BANK, 6))
    + "\n\nHard limits, never cross these:\n"
    "- No slurs, no racism, no sexism, no homophobia/transphobia\n"
    "- Nothing about real physical appearance, disability, or body weight\n"
    "- Nothing about family deaths, tragedy, self-harm, or mental health\n"
    "- No sexual content\n"
    "Keep it clever wordplay/comeback energy, not genuine cruelty."
)


async def _generate_roast(display_name: str, context: str = "") -> str:
    # Plan-conscious: the user's supplied punchlines are the primary
    # source now (zero API cost), not just an offline fallback. Groq only
    # gets called for the "context" case (target/joiner said something
    # specific worth roasting back at) where a canned line can't react to
    # what was actually said — and even then, if the call fails or the key
    # is missing, it falls back to the bank like before.
    if not context:
        return random.choice(PUNCHLINE_BANK)

    if not GROQ_API_KEY:
        return random.choice(PUNCHLINE_BANK)
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            user_msg = f"Roast {display_name}."
            if context:
                user_msg += f" They just said: \"{context[:200]}\" — you can roast that too."
            payload = {
                "model": ROAST_MODEL,
                "messages": [
                    {"role": "system", "content": ROAST_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.95,
                "max_tokens": 120,
            }
            async with session.post(
                GROQ_ENDPOINT, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return text.strip() or random.choice(PUNCHLINE_BANK)
                logger.warning(f"[v0] roast generation failed: HTTP {resp.status}")
                return random.choice(PUNCHLINE_BANK)
    except Exception as e:
        logger.warning(f"[v0] roast generation error: {e}")
        return random.choice(PUNCHLINE_BANK)


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


def _is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id in DISCORD_CLONE_ADMIN_IDS


class RoastTargetPickerView(discord.ui.View):
    """Sent in the admin's DM. First admin to pick target+channel locks
    the challenge in; a lock check right before insert prevents two admins
    racing to both create a battle for the same inactivity/random tick."""

    def __init__(self, cog: "RoastCog", guild: discord.Guild, proposing_admin_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.proposing_admin_id = proposing_admin_id
        self.chosen_target: discord.Member | None = None
        self.chosen_channel: discord.TextChannel | None = None

        members = [m for m in guild.members if not m.bot][:25]
        self.target_select = discord.ui.Select(
            placeholder=f"Pick a target in {guild.name}...",
            options=[discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members] or
                    [discord.SelectOption(label="No eligible members", value="none")],
            row=0,
        )
        self.target_select.callback = self._on_target
        self.add_item(self.target_select)

        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages][:25]
        self.channel_select = discord.ui.Select(
            placeholder=f"Pick a channel in {guild.name}...",
            options=[discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id)) for c in channels] or
                    [discord.SelectOption(label="No eligible channels", value="none")],
            row=1,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        self.confirm_btn = discord.ui.Button(label="Send Challenge 🔥", style=discord.ButtonStyle.danger, row=2, disabled=True)
        self.confirm_btn.callback = self._on_confirm
        self.add_item(self.confirm_btn)

        self.remind_later_btn = discord.ui.Button(label="Remind Me Later", style=discord.ButtonStyle.secondary, row=3)
        self.remind_later_btn.callback = self._on_remind_later
        self.add_item(self.remind_later_btn)

        self.dont_ask_btn = discord.ui.Button(label="Don't Ask Again", style=discord.ButtonStyle.secondary, row=3)
        self.dont_ask_btn.callback = self._on_dont_ask_again
        self.add_item(self.dont_ask_btn)

    def _status_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔥 Roast Opportunity — {self.guild.name}",
            description="This server looks quiet. Want to start a roast? Pick a target and channel below.",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Server ID: {self.guild.id}")
        embed.add_field(
            name="Target",
            value=self.chosen_target.mention if self.chosen_target else "*not picked yet*",
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=f"#{self.chosen_channel.name}" if self.chosen_channel else "*not picked yet*",
            inline=True,
        )
        return embed

    async def _on_target(self, interaction: discord.Interaction):
        try:
            val = self.target_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible members.", ephemeral=True)
                return
            self.chosen_target = self.guild.get_member(int(val))
            if self.chosen_target is None:
                logger.warning(f"[roast] target_select resolved to no member for id={val} guild={self.guild.id}")
                await interaction.response.send_message("Couldn't find that member — try again.", ephemeral=True)
                return
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception:
            logger.exception(f"[roast] target picker failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong picking the target — check Railway logs.", ephemeral=True)

    async def _on_channel(self, interaction: discord.Interaction):
        try:
            val = self.channel_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible channels.", ephemeral=True)
                return
            self.chosen_channel = self.guild.get_channel(int(val))
            if self.chosen_channel is None:
                logger.warning(f"[roast] channel_select resolved to no channel for id={val} guild={self.guild.id}")
                await interaction.response.send_message("Couldn't find that channel — try again.", ephemeral=True)
                return
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception:
            logger.exception(f"[roast] channel picker failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong picking the channel — check Railway logs.", ephemeral=True)

    async def _on_confirm(self, interaction: discord.Interaction):
        try:
            if not self.chosen_target or not self.chosen_channel:
                await interaction.response.send_message("Pick both a target and a channel first.", ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"✅ Challenge sent to {self.chosen_target.mention} in #{self.chosen_channel.name}.",
                embed=None,
                view=self,
            )
            logger.info(
                f"[roast] challenge queued guild={self.guild.id} target={self.chosen_target.id} "
                f"channel={self.chosen_channel.id} by_admin={interaction.user.id}"
            )
            await self.cog.start_challenge(
                guild=self.guild,
                target=self.chosen_target,
                channel=self.chosen_channel,
                proposed_by_admin_id=interaction.user.id,
            )
        except Exception:
            logger.exception(f"[roast] start_challenge failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Failed to send the challenge — check Railway logs.", ephemeral=True)
            else:
                try:
                    await interaction.followup.send("⚠️ Failed to send the challenge — check Railway logs.", ephemeral=True)
                except discord.HTTPException:
                    pass
        finally:
            self.stop()

    async def _on_remind_later(self, interaction: discord.Interaction):
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"⏰ Okay, I'll check back in about {SNOOZE_MINUTES // 60}h.",
                embed=None,
                view=self,
            )
            clone_id = _clone_id_of(self.cog.bot)
            # Push last_proposed_at back so the cooldown check in
            # _check_triggers clears again after SNOOZE_MINUTES instead of
            # the full PROPOSAL_COOLDOWN_MINUTES.
            await db.execute(
                f"""
                INSERT INTO discord_roast_activity (guild_id, clone_id, last_roast_proposed_at)
                VALUES ($1, $2, NOW() - INTERVAL '{PROPOSAL_COOLDOWN_MINUTES - SNOOZE_MINUTES} minutes')
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET last_roast_proposed_at = NOW() - INTERVAL '{PROPOSAL_COOLDOWN_MINUTES - SNOOZE_MINUTES} minutes'
                """,
                self.guild.id, clone_id,
            )
            logger.info(f"[roast] admin={interaction.user.id} snoozed guild={self.guild.id}")
        except Exception:
            logger.exception(f"[roast] remind_later failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong — check Railway logs.", ephemeral=True)
        finally:
            self.stop()

    async def _on_dont_ask_again(self, interaction: discord.Interaction):
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="🔕 Got it, I won't suggest auto-roasts for this server anymore. "
                        "Re-enable anytime with `/roast configure enabled:True`.",
                embed=None,
                view=self,
            )
            clone_id = _clone_id_of(self.cog.bot)
            current = await self.cog.get_config(self.guild.id, clone_id)
            await db.execute(
                """
                INSERT INTO discord_roast_config (guild_id, clone_id, inactivity_minutes, random_chance_percent, enabled)
                VALUES ($1, $2, $3, $4, FALSE)
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET enabled = FALSE
                """,
                self.guild.id, clone_id, current["inactivity_minutes"], current["random_chance_percent"],
            )
            logger.info(f"[roast] admin={interaction.user.id} disabled auto-roast guild={self.guild.id}")
        except Exception:
            logger.exception(f"[roast] dont_ask_again failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong — check Railway logs.", ephemeral=True)
        finally:
            self.stop()


class RoastMemberRequestView(discord.ui.View):
    """Sent as the response to /setup roastme — any member (not just
    admins) can request a roast on someone, but unlike an admin's own
    /setup roaststart this doesn't go straight to the target. Confirming
    here creates a battle row with status='awaiting_approval' and DMs
    every admin an Approve/Deny prompt (RoastApprovalView); only on
    approval does the target actually get challenged."""

    def __init__(self, cog: "RoastCog", guild: discord.Guild, requester_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.requester_id = requester_id
        self.chosen_target: discord.Member | None = None
        self.chosen_channel: discord.TextChannel | None = None

        members = [m for m in guild.members if not m.bot][:25]
        self.target_select = discord.ui.Select(
            placeholder=f"Pick who you want the bot to roast in {guild.name}...",
            options=[discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members] or
                    [discord.SelectOption(label="No eligible members", value="none")],
            row=0,
        )
        self.target_select.callback = self._on_target
        self.add_item(self.target_select)

        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages][:25]
        self.channel_select = discord.ui.Select(
            placeholder=f"Pick a channel in {guild.name}...",
            options=[discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id)) for c in channels] or
                    [discord.SelectOption(label="No eligible channels", value="none")],
            row=1,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        self.confirm_btn = discord.ui.Button(label="Request Roast 🔥", style=discord.ButtonStyle.danger, row=2, disabled=True)
        self.confirm_btn.callback = self._on_confirm
        self.add_item(self.confirm_btn)

    def _status_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔥 Request a Roast — {self.guild.name}",
            description="Pick who you want roasted and where. An admin has to approve before it goes out.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Target", value=self.chosen_target.mention if self.chosen_target else "*not picked yet*", inline=True)
        embed.add_field(name="Channel", value=f"#{self.chosen_channel.name}" if self.chosen_channel else "*not picked yet*", inline=True)
        return embed

    async def _on_target(self, interaction: discord.Interaction):
        try:
            val = self.target_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible members.", ephemeral=True)
                return
            self.chosen_target = self.guild.get_member(int(val))
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception:
            logger.exception(f"[roast] member request target picker failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong — check Railway logs.", ephemeral=True)

    async def _on_channel(self, interaction: discord.Interaction):
        try:
            val = self.channel_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible channels.", ephemeral=True)
                return
            self.chosen_channel = self.guild.get_channel(int(val))
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception:
            logger.exception(f"[roast] member request channel picker failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Something went wrong — check Railway logs.", ephemeral=True)

    async def _on_confirm(self, interaction: discord.Interaction):
        try:
            if not self.chosen_target or not self.chosen_channel:
                await interaction.response.send_message("Pick both a target and a channel first.", ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="📨 Request sent to the admins for approval.", embed=None, view=self,
            )
            await self.cog.create_member_request(
                guild=self.guild,
                target=self.chosen_target,
                channel=self.chosen_channel,
                requester_id=interaction.user.id,
            )
        except Exception:
            logger.exception(f"[roast] member roast request failed guild={self.guild.id}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Failed to send the request — check Railway logs.", ephemeral=True)
        finally:
            self.stop()


class _RoastApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_approve:(\d+)$"):
    """DM'd to every admin when a member requests a roast. DynamicItem
    (timeout=None) rather than a plain View(timeout=1800) so this survives
    a bot restart within the 30-minute approval window — same class of bug
    as the fixed ChallengeView (see discover_players.py). Names for the
    confirmation text are resolved from the battle row at click time
    instead of being carried on the instance, since a DynamicItem is
    rebuilt fresh from its custom_id on every dispatch."""

    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Approve ✅", style=discord.ButtonStyle.success,
            custom_id=f"roast_approve:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            allowed, battle = await cog._is_admin_for_battle(interaction.user.id, self.battle_id)
            if not allowed:
                await interaction.edit_original_response(content="🚫 Admins only.")
                return

            target_name, requester_name = await cog._battle_names(battle) if battle else ("someone", "someone")
            ok = await cog.approve_member_request(self.battle_id, interaction.user.id)
            if not ok:
                await interaction.edit_original_response(content="⚠️ Already resolved, expired, or unavailable.", view=_disabled_roast_approval_view(self.battle_id))
                return
            await interaction.edit_original_response(
                content=f"✅ Approved — {target_name} has been challenged.",
                view=_disabled_roast_approval_view(self.battle_id),
            )
        except Exception:
            logger.exception(f"[roast] approval failed battle_id={self.battle_id}")
            try:
                await interaction.edit_original_response(content="⚠️ Something went wrong — check Railway logs.")
            except Exception:
                pass


class _RoastDenyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_deny:(\d+)$"):
    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Deny ❌", style=discord.ButtonStyle.secondary,
            custom_id=f"roast_deny:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            allowed, battle = await cog._is_admin_for_battle(interaction.user.id, self.battle_id)
            if not allowed:
                await interaction.edit_original_response(content="🚫 Admins only.")
                return

            _, requester_name = await cog._battle_names(battle) if battle else ("someone", "someone")
            ok = await cog.deny_member_request(self.battle_id, interaction.user.id)
            if not ok:
                await interaction.edit_original_response(content="⚠️ Already resolved or expired.", view=_disabled_roast_approval_view(self.battle_id))
                return
            await interaction.edit_original_response(
                content=f"❌ Denied {requester_name}'s roast request.",
                view=_disabled_roast_approval_view(self.battle_id),
            )
        except Exception:
            logger.exception(f"[roast] denial failed battle_id={self.battle_id}")
            try:
                await interaction.edit_original_response(content="⚠️ Something went wrong — check Railway logs.")
            except Exception:
                pass


def _disabled_roast_approval_view(battle_id: int) -> "RoastApprovalView":
    view = RoastApprovalView(battle_id)
    for child in view.children:
        child.item.disabled = True
    return view


class RoastApprovalView(discord.ui.View):
    """DMed to every admin when a member requests a roast via /setup
    roastme. Any admin approving or denying resolves it for all of them —
    the message is only edited for the admin who acted, but the DB status
    change means a second admin clicking their own copy just gets told
    it's already resolved.

    timeout=None + DynamicItem buttons (see _RoastApproveButton) so this
    survives a bot restart instead of expiring in-memory."""

    def __init__(self, battle_id: int):
        super().__init__(timeout=None)
        self.battle_id = battle_id
        self.add_item(_RoastApproveButton(battle_id))
        self.add_item(_RoastDenyButton(battle_id))


class _RoastAcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_accept:(\d+)$"):
    """DM'd to the challenge target. DynamicItem (timeout=None) for the
    same restart-survival reason as _RoastApproveButton above — target
    identity is checked against the battle row's target_id at click time
    rather than an instance attribute."""

    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Accept Challenge 🔥", style=discord.ButtonStyle.danger,
            custom_id=f"roast_accept:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        battle = await cog.get_battle(self.battle_id)
        if not battle or interaction.user.id != battle["target_id"]:
            await interaction.response.send_message("This challenge isn't yours to answer.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            ok = await cog.accept_battle(self.battle_id)
            if not ok:
                await interaction.edit_original_response(content="⏰ This challenge already expired.", view=_disabled_roast_accept_view(self.battle_id))
                return
            await interaction.edit_original_response(content="✅ Accepted! Head to the server, it's on.", view=_disabled_roast_accept_view(self.battle_id))
        except Exception:
            logger.exception(f"[roast] accept button failed battle_id={self.battle_id}")
            try:
                await interaction.followup.send("⚠️ Something went wrong accepting — check Railway logs.", ephemeral=True)
            except Exception:
                pass


class _RoastDeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_decline:(\d+)$"):
    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.secondary,
            custom_id=f"roast_decline:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        battle = await cog.get_battle(self.battle_id)
        if not battle or interaction.user.id != battle["target_id"]:
            await interaction.response.send_message("This challenge isn't yours to answer.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await cog.decline_battle(self.battle_id)
            await interaction.edit_original_response(content="😌 Declined. No roast today.", view=_disabled_roast_accept_view(self.battle_id))
        except Exception:
            logger.exception(f"[roast] decline button failed battle_id={self.battle_id}")
            try:
                await interaction.followup.send("⚠️ Something went wrong declining — check Railway logs.", ephemeral=True)
            except Exception:
                pass


def _disabled_roast_accept_view(battle_id: int) -> "RoastAcceptView":
    view = RoastAcceptView(battle_id)
    for child in view.children:
        child.item.disabled = True
    return view


class RoastAcceptView(discord.ui.View):
    """Sent to the target's DM. Only the target can press these.
    timeout=None + DynamicItem buttons — see _RoastAcceptButton."""

    def __init__(self, battle_id: int):
        super().__init__(timeout=None)
        self.battle_id = battle_id
        self.add_item(_RoastAcceptButton(battle_id))
        self.add_item(_RoastDeclineButton(battle_id))


class RoastCancelPendingView(discord.ui.View):
    """Shown when someone tries to start a new roast while an existing one
    is still 'pending', 'awaiting_approval', or 'approving' — i.e. hasn't
    gone active yet, so RoastBattleView's Quit button doesn't apply (it
    only ends 'active' battles). Admin-only: an ordinary member shouldn't
    be able to kill someone else's outstanding request/challenge just by
    trying to start their own. Not persistent (default timeout) since it's
    only ever attached to the one-off ephemeral "already one running"
    reply — if the bot restarts before it's clicked, running the command
    again produces a fresh, working button."""

    def __init__(self, cog: "RoastCog", battle_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.battle_id = battle_id

    @discord.ui.button(label="End it", style=discord.ButtonStyle.danger, emoji="🛑")
    async def end_it(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not (isinstance(interaction.user, discord.Member) and _is_admin_member(interaction.user)):
            await interaction.followup.send("Only an admin can cancel this.", ephemeral=True)
            return
        cancelled = await self.cog.cancel_pending(self.battle_id)
        if not cancelled:
            await interaction.followup.send(
                "That request already resolved (or went active) — try starting a new roast again.", ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send("🛑 Cancelled — you can start a new roast now.", ephemeral=True)


ROAST_DYNAMIC_ITEMS = (_RoastApproveButton, _RoastDenyButton, _RoastAcceptButton, _RoastDeclineButton)


class RoastBattleView(discord.ui.View):
    """Attached to every roast message posted in the channel while a
    battle is active. Persistent (timeout=None) since a battle can run
    indefinitely and must survive a bot restart — cog_load re-adds it
    keyed by custom_id, which encodes the battle id.

    Only Quit Roast lives here now — joining is no longer a button click.
    Anyone who replies to one of the bot's roast messages gets pulled in
    and roasted back automatically (see on_message's reply-check), which
    is lower friction than hunting for a Join button."""

    def __init__(self, cog: "RoastCog", battle_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.battle_id = battle_id
        self.quit_btn.custom_id = f"roast:quit:{battle_id}"

    @discord.ui.button(label="Quit Roast", style=discord.ButtonStyle.secondary)
    async def quit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Same rule as Approve/Deny above: acknowledge FIRST, before any
        # DB round-trip. This used to fetch the battle (a DB await) before
        # ever calling response.*, so a slow query blew straight through
        # Discord's 3s ack window -> "didn't respond in time" -> the
        # battle stayed stuck 'active' forever -> every future /roast
        # start correctly-but-unhelpfully said "quit it first", and
        # clicking Quit hit the exact same timeout again.
        await interaction.response.defer(ephemeral=True)
        try:
            battle = await self.cog.get_battle(self.battle_id)
            if not battle or battle["status"] != "active":
                await interaction.followup.send("This roast battle already ended.", ephemeral=True)
                return
            member = interaction.user
            allowed = (
                member.id == battle["target_id"]
                or member.id in (battle["joined_ids"] or [])
                or (isinstance(member, discord.Member) and _is_admin_member(member))
            )
            if not allowed:
                await interaction.followup.send("Only someone in the roast, or an admin, can end it.", ephemeral=True)
                return
            await self.cog.end_battle(self.battle_id)
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(view=self)
            await interaction.channel.send(f"🏳️ Roast battle ended by {member.mention}.")
        except Exception:
            logger.exception(f"[roast] quit button failed battle_id={self.battle_id} user={interaction.user.id}")
            try:
                await interaction.followup.send("⚠️ Couldn't end the roast — check Railway logs.", ephemeral=True)
            except Exception:
                pass


class RoastCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_by_channel: dict[int, int] = {}  # channel_id -> battle_id

    async def cog_load(self):
        rows = await db.fetch("SELECT id FROM discord_roast_battles WHERE status = 'active'")
        for row in rows:
            self.bot.add_view(RoastBattleView(self, row["id"]))
        battles = await db.fetch("SELECT id, channel_id FROM discord_roast_battles WHERE status = 'active'")
        for b in battles:
            self._active_by_channel[b["channel_id"]] = b["id"]
        self._poller.start()
        logger.info(f"[roast] cog loaded, {len(rows)} active battle(s) restored, poller running every {POLL_INTERVAL_SECONDS}s")

    def cog_unload(self):
        self._poller.cancel()

    # ---------- DB-backed helpers ----------

    async def get_battle(self, battle_id: int):
        return await db.fetchrow("SELECT * FROM discord_roast_battles WHERE id = $1", battle_id)

    async def _is_admin_for_battle(self, user_id: int, battle_id: int) -> tuple[bool, object | None]:
        """Resolve a DM button click back to the guild for this roast request.

        Approval buttons are delivered in DMs, where interaction.user is a
        discord.User and therefore has no guild_permissions attribute. The
        guild_id stored on the battle is the source of truth for checking the
        user's Administrator permission. Configured clone owners remain an
        explicit global bypass.
        """
        battle = await self.get_battle(battle_id)
        if not battle:
            return False, None

        if user_id in DISCORD_CLONE_ADMIN_IDS:
            return True, battle

        guild = self.bot.get_guild(battle["guild_id"])
        if guild is None:
            logger.warning(
                f"[roast] admin check failed: guild {battle['guild_id']} not found for battle_id={battle_id}"
            )
            return False, battle

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False, battle

        return bool(member.guild_permissions.administrator), battle

    async def _battle_names(self, battle) -> tuple[str, str]:
        """(target_name, requester_name) for a battle row, resolved fresh
        from the guild rather than carried on a view instance — needed
        because RoastApprovalView/_RoastApproveButton are now DynamicItems
        rebuilt from just the battle_id on every dispatch (see
        RoastApprovalView's docstring)."""
        guild = self.bot.get_guild(battle["guild_id"]) if battle else None
        target_member = guild.get_member(battle["target_id"]) if guild else None
        requester_member = guild.get_member(battle["proposed_by_admin_id"]) if guild else None
        target_name = target_member.display_name if target_member else str(battle["target_id"]) if battle else "someone"
        requester_name = requester_member.display_name if requester_member else str(battle["proposed_by_admin_id"]) if battle else "someone"
        return target_name, requester_name

    async def get_config(self, guild_id: int, clone_id):
        row = await db.fetchrow(
            "SELECT * FROM discord_roast_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id,
        )
        if row:
            return row
        return {
            "inactivity_minutes": DEFAULT_INACTIVITY_MINUTES,
            "random_chance_enabled": True,
            "random_check_minutes": DEFAULT_RANDOM_CHECK_MINUTES,
            "random_chance_percent": DEFAULT_RANDOM_CHANCE_PERCENT,
            "enabled": True,
        }

    async def start_challenge(self, guild, target, channel, proposed_by_admin_id):
        clone_id = _clone_id_of(self.bot)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_EXPIRY_MINUTES)
        try:
            row = await db.fetchrow(
                """
                INSERT INTO discord_roast_battles
                    (guild_id, clone_id, channel_id, target_id, proposed_by_admin_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'pending', $6)
                RETURNING id
                """,
                guild.id, clone_id, channel.id, target.id, proposed_by_admin_id, expires_at,
            )
        except Exception:
            logger.exception(f"[roast] failed to insert battle row guild={guild.id} target={target.id}")
            return None
        battle_id = row["id"]
        try:
            embed = discord.Embed(
                title="⚠️ You've Been Challenged to a Roast Battle",
                description=(
                    f"An admin in **{guild.name}** wants to roast you in #{channel.name}.\n\n"
                    "Accept and it happens live in the server. Ignore it for "
                    f"{CHALLENGE_EXPIRY_MINUTES} minutes and the bot wins by default."
                ),
                color=discord.Color.orange(),
            )
            await target.send(embed=embed, view=RoastAcceptView(battle_id))
            logger.info(f"[roast] challenge DM sent battle_id={battle_id} target={target.id}")
        except discord.Forbidden:
            await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)
            logger.info(f"[roast] challenge to {target.id} failed: DMs closed, battle_id={battle_id} ended")
            return None
        except Exception:
            logger.exception(f"[roast] unexpected error DMing target={target.id} battle_id={battle_id}")
            await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)
            return None
        return battle_id

    async def accept_battle(self, battle_id: int) -> bool:
        battle = await self.get_battle(battle_id)
        if not battle or battle["status"] != "pending":
            logger.warning(f"[roast] accept_battle called on invalid battle_id={battle_id} status={battle['status'] if battle else 'missing'}")
            return False
        await db.execute("UPDATE discord_roast_battles SET status = 'active' WHERE id = $1", battle_id)
        channel = self.bot.get_channel(battle["channel_id"])
        if channel is None:
            logger.warning(f"[roast] accept_battle: channel {battle['channel_id']} not found/cached, battle_id={battle_id}")
            return True
        target = channel.guild.get_member(battle["target_id"])
        if target is None:
            logger.warning(f"[roast] accept_battle: target {battle['target_id']} not found in guild, battle_id={battle_id}")
            return True
        try:
            self._active_by_channel[channel.id] = battle_id
            roast_text = await _generate_roast(target.display_name)
            embed = discord.Embed(
                title="🔥 Roast Battle — LIVE",
                description=roast_text,
                color=discord.Color.red(),
            )
            embed.set_footer(text="Reply to this message to fire back or jump in.")
            view = RoastBattleView(self, battle_id)
            self.bot.add_view(view)
            await channel.send(content=target.mention, embed=embed, view=view)
            logger.info(f"[roast] battle_id={battle_id} went active in channel={channel.id}")
        except Exception:
            logger.exception(f"[roast] failed to post opening roast battle_id={battle_id} channel={channel.id}")
        await self._notify_owners(
            f"🔥 Roast battle started in **{channel.guild.name}** (#{channel.name}) — target: {target.display_name}, battle_id={battle_id}"
        )
        return True

    async def _notify_owners(self, text: str):
        """DMs every configured bot owner (DISCORD_CLONE_ADMIN_IDS) — best
        effort, one owner's closed DMs shouldn't block the others."""
        for owner_id in DISCORD_CLONE_ADMIN_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(text)
            except discord.Forbidden:
                logger.info(f"[roast] couldn't DM owner={owner_id} (DMs closed)")
            except Exception:
                logger.exception(f"[roast] failed to notify owner={owner_id}")

    async def decline_battle(self, battle_id: int):
        await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)

    async def join_battle(self, battle_id: int, user_id: int):
        await db.execute(
            "UPDATE discord_roast_battles SET joined_ids = array_append(joined_ids, $2) WHERE id = $1",
            battle_id, user_id,
        )

    async def end_battle(self, battle_id: int):
        await db.execute(
            "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id,
        )
        battle = await self.get_battle(battle_id)
        if battle and self._active_by_channel.get(battle["channel_id"]) == battle_id:
            del self._active_by_channel[battle["channel_id"]]

    def _blocking_battle_view(self, existing) -> discord.ui.View:
        """Picks the right "end it" control for whatever's blocking a new
        roast from starting, so people aren't just told to wait — they get
        a button that actually resolves it. 'active' battles use the same
        Quit Roast control already shown in-channel (restricted to the
        target/joined members/admins); anything not yet active (pending /
        awaiting_approval / approving) uses the admin-only cancel button
        instead, since Quit Roast only recognizes 'active' battles and
        would otherwise incorrectly claim it "already ended"."""
        if existing["status"] == "active":
            return RoastBattleView(self, existing["id"])
        return RoastCancelPendingView(self, existing["id"])

    async def cancel_pending(self, battle_id: int) -> bool:
        """Force-cancels a battle still stuck in 'pending', 'awaiting_approval',
        or 'approving' — the states _expire_stale_challenges would otherwise
        only clear after CHALLENGE_EXPIRY_MINUTES. Used by the "End it"
        button offered when someone tries to start a new roast while one of
        these is already blocking them. Re-checks the status before writing
        so this can't accidentally cancel a battle that became active (or
        was already resolved) between the button being shown and clicked.
        Returns False (no-op) if the battle is no longer in one of those
        states."""
        row = await db.fetchrow(
            "UPDATE discord_roast_battles SET status = 'expired', resolved_at = NOW() "
            "WHERE id = $1 AND status IN ('pending', 'awaiting_approval', 'approving') "
            "RETURNING id",
            battle_id,
        )
        return row is not None

    # ---------- listeners ----------

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        clone_id = _clone_id_of(self.bot)
        await db.execute(
            """
            INSERT INTO discord_roast_activity (guild_id, clone_id, last_message_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET last_message_at = NOW()
            """,
            message.guild.id, clone_id,
        )

        battle_id = self._active_by_channel.get(message.channel.id)
        if not battle_id:
            return
        battle = await self.get_battle(battle_id)
        if not battle or battle["status"] != "active":
            self._active_by_channel.pop(message.channel.id, None)
            return

        already_in = message.author.id == battle["target_id"] or message.author.id in (battle["joined_ids"] or [])

        if not already_in:
            # Not the target and hasn't joined yet — only pull them in if
            # they're replying TO one of the bot's own roast messages.
            # This replaces the old "Join Roast" button: replying is the
            # join action now, no click needed.
            ref = message.reference
            if ref is None:
                return
            try:
                replied_to = ref.resolved or await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.HTTPException):
                replied_to = None
            if replied_to is None or replied_to.author.id != self.bot.user.id:
                return
            await self.join_battle(battle_id, message.author.id)
            logger.info(f"[roast] user={message.author.id} auto-joined battle_id={battle_id} via reply")

        if random.randint(1, 100) <= BOT_CONCEDE_CHANCE_PERCENT:
            # Bot "roasted back" by the member — occasionally take the L
            # instead of always firing another roast, so it feels like a
            # real back-and-forth instead of the bot being unbeatable.
            # Skips the Groq call entirely in this branch — no point
            # generating a roast just to throw it away.
            roast_text = random.choice(CONCEDE_LINES)
        else:
            roast_text = await _generate_roast(message.author.display_name, context=message.content)
        embed = discord.Embed(description=roast_text, color=discord.Color.red())
        try:
            await message.reply(embed=embed, view=RoastBattleView(self, battle_id))
        except discord.HTTPException:
            await message.channel.send(embed=embed, view=RoastBattleView(self, battle_id))

    # ---------- background poller ----------

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self):
        try:
            await self._expire_stale_challenges()
        except Exception:
            logger.exception("[roast] _expire_stale_challenges failed")
        try:
            await self._check_triggers()
        except Exception:
            logger.exception("[roast] _check_triggers failed")

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    async def _expire_stale_challenges(self):
        rows = await db.fetch(
            "SELECT * FROM discord_roast_battles WHERE status IN ('pending', 'awaiting_approval', 'approving') AND expires_at <= NOW()"
        )
        for battle in rows:
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'expired', resolved_at = NOW() WHERE id = $1",
                battle["id"],
            )
            # An approval request expiring is not the same as the target
            # ignoring an actual challenge, so only pending challenges get
            # the public "bot wins" announcement.
            if battle["status"] == "pending":
                channel = self.bot.get_channel(battle["channel_id"])
                if channel:
                    target = channel.guild.get_member(battle["target_id"])
                    name = target.mention if target else "The challenged member"
                    try:
                        await channel.send(f"🏆 {name} didn't accept in time — bot wins by default. Coward. 😏")
                    except discord.HTTPException:
                        pass

    async def _check_triggers(self):
        clone_id = _clone_id_of(self.bot)
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            if getattr(guild, "unavailable", False):
                continue
            config = await self.get_config(guild.id, clone_id)
            if not config["enabled"]:
                continue
            # skip guilds with an unresolved pending/active battle already
            existing = await db.fetchrow(
                "SELECT id FROM discord_roast_battles WHERE guild_id = $1 AND status IN ('pending','active','awaiting_approval') LIMIT 1",
                guild.id,
            )
            if existing:
                continue

            activity = await db.fetchrow(
                "SELECT * FROM discord_roast_activity WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild.id, clone_id,
            )
            last_message_at = activity["last_message_at"] if activity else None
            last_proposed_at = activity["last_roast_proposed_at"] if activity else None

            # Hard cooldown: never DM admins a new roast suggestion more
            # than once every PROPOSAL_COOLDOWN_MINUTES, no matter which
            # trigger below would otherwise fire.
            on_cooldown = (
                last_proposed_at is not None
                and (now - last_proposed_at).total_seconds() / 60 < PROPOSAL_COOLDOWN_MINUTES
            )

            triggered = False
            if not on_cooldown and last_message_at:
                idle_minutes = (now - last_message_at).total_seconds() / 60
                # Only propose once per idle period: skip if we already
                # proposed since the last message came in.
                already_proposed_this_idle = (
                    last_proposed_at is not None and last_proposed_at >= last_message_at
                )
                if idle_minutes >= config["inactivity_minutes"] and not already_proposed_this_idle:
                    triggered = True

            if not triggered and not on_cooldown and config["random_chance_enabled"]:
                due_for_check = (
                    last_proposed_at is None
                    or (now - last_proposed_at).total_seconds() / 60 >= config["random_check_minutes"]
                )
                if due_for_check and random.randint(1, 100) <= config["random_chance_percent"]:
                    triggered = True

            if not triggered:
                continue

            logger.info(f"[roast] trigger fired guild={guild.id}")
            await db.execute(
                """
                INSERT INTO discord_roast_activity (guild_id, clone_id, last_roast_proposed_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET last_roast_proposed_at = NOW()
                """,
                guild.id, clone_id,
            )
            await self._propose_to_admins(guild)

    async def _propose_to_admins(self, guild: discord.Guild):
        admins = [m for m in guild.members if not m.bot and _is_admin_member(m)]
        if not admins:
            logger.warning(f"[roast] trigger fired but no admins found guild={guild.id}")
            return
        sent = 0
        for admin in admins:
            try:
                view = RoastTargetPickerView(self, guild, admin.id)
                await admin.send(embed=view._status_embed(), view=view)
                sent += 1
            except discord.Forbidden:
                logger.info(f"[roast] couldn't DM admin={admin.id} guild={guild.id} (DMs closed)")
                continue
            except Exception:
                logger.exception(f"[roast] failed to DM admin={admin.id} guild={guild.id}")
                continue
        logger.info(f"[roast] proposal sent to {sent}/{len(admins)} admins guild={guild.id}")

    # ---------- member-requested roast (needs admin approval) ----------

    async def request_from_member(self, interaction: discord.Interaction):
        """Entry point for /setup roastme — open to ALL members, not just
        admins. Blocks on an existing pending/active/awaiting-approval
        battle same as the admin path, so members can't stack requests."""
        await interaction.response.defer()
        existing = await db.fetchrow(
            "SELECT id, status FROM discord_roast_battles WHERE guild_id = $1 "
            "AND status IN ('pending','active','awaiting_approval') LIMIT 1",
            interaction.guild.id,
        )
        if existing:
            await interaction.followup.send(
                f"⚠️ There's already a {existing['status']} roast battle/request in this server.",
                view=self._blocking_battle_view(existing),
                ephemeral=True,
            )
            return
        view = RoastMemberRequestView(self, interaction.guild, interaction.user.id)
        await interaction.followup.send(embed=view._status_embed(), view=view, ephemeral=True)
        logger.info(f"[roast] member request flow opened by user={interaction.user.id} guild={interaction.guild.id}")

    async def create_member_request(self, guild, target, channel, requester_id):
        clone_id = _clone_id_of(self.bot)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_EXPIRY_MINUTES)
        try:
            row = await db.fetchrow(
                """
                INSERT INTO discord_roast_battles
                    (guild_id, clone_id, channel_id, target_id, proposed_by_admin_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'awaiting_approval', $6)
                RETURNING id
                """,
                guild.id, clone_id, channel.id, target.id, requester_id, expires_at,
            )
        except Exception:
            logger.exception(f"[roast] failed to insert member-request row guild={guild.id}")
            return
        battle_id = row["id"]
        requester = guild.get_member(requester_id)
        requester_name = requester.display_name if requester else str(requester_id)
        admins = [m for m in guild.members if not m.bot and _is_admin_member(m)]
        if not admins:
            logger.warning(f"[roast] member request battle_id={battle_id} has no admins to approve it")
            await db.execute("UPDATE discord_roast_battles SET status = 'ended' WHERE id = $1", battle_id)
            return
        sent = 0
        for admin in admins:
            try:
                embed = discord.Embed(
                    title=f"🔥 Roast Request — {guild.name}",
                    description=(
                        f"{requester_name} wants the bot to roast {target.display_name} "
                        f"in #{channel.name}. Approve to send the challenge."
                    ),
                    color=discord.Color.gold(),
                )
                await admin.send(embed=embed, view=RoastApprovalView(battle_id))
                sent += 1
            except discord.Forbidden:
                continue
            except Exception:
                logger.exception(f"[roast] failed to DM admin={admin.id} for approval battle_id={battle_id}")
        logger.info(f"[roast] member request battle_id={battle_id} sent to {sent}/{len(admins)} admins")

    async def approve_member_request(self, battle_id: int, admin_id: int) -> bool:
        # Atomically claim the request. If two admins click Approve, only the
        # first one can transition awaiting_approval -> approving.
        battle = await db.fetchrow(
            """
            UPDATE discord_roast_battles
               SET status = 'approving'
             WHERE id = $1
               AND status = 'awaiting_approval'
               AND expires_at > NOW()
         RETURNING *
            """,
            battle_id,
        )
        if not battle:
            return False

        guild = self.bot.get_guild(battle["guild_id"])
        if guild is None:
            logger.warning(f"[roast] approve_member_request: guild {battle['guild_id']} not found, battle_id={battle_id}")
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
                battle_id,
            )
            return False

        channel = self.bot.get_channel(battle["channel_id"])
        target = guild.get_member(battle["target_id"])
        if channel is None or target is None:
            logger.warning(f"[roast] approve_member_request: missing channel/target for battle_id={battle_id}")
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
                battle_id,
            )
            return False

        logger.info(f"[roast] member request battle_id={battle_id} approved by admin={admin_id}")
        new_battle_id = await self.start_challenge(
            guild=guild, target=target, channel=channel, proposed_by_admin_id=admin_id
        )
        if new_battle_id is None:
            # Keep the request available if creating/sending the real challenge
            # failed, rather than deleting the only source of truth.
            await db.execute(
                """
                UPDATE discord_roast_battles
                   SET status = 'awaiting_approval'
                 WHERE id = $1 AND status = 'approving'
                """,
                battle_id,
            )
            return False

        # The new pending row is now the real challenge. Resolve the approval
        # row only after the new challenge was successfully created and DM'd.
        await db.execute(
            "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
            battle_id,
        )
        return True

    async def deny_member_request(self, battle_id: int, admin_id: int) -> bool:
        row = await db.fetchrow(
            """
            UPDATE discord_roast_battles
               SET status = 'ended', resolved_at = NOW()
             WHERE id = $1
               AND status = 'awaiting_approval'
               AND expires_at > NOW()
         RETURNING id
            """,
            battle_id,
        )
        if not row:
            return False
        logger.info(f"[roast] member request battle_id={battle_id} denied by admin={admin_id}")
        return True

    # ---------- admin manual trigger ----------

    async def manual_trigger(self, interaction: discord.Interaction):
        """Lets an admin skip the inactivity/random wait and pop the
        target+channel picker immediately, right in the server instead of
        via DM. Same RoastTargetPickerView as the automatic flow, and
        still respects the one-battle-per-guild guard in start_challenge's
        caller — but since this is manual, we don't need the trigger-level
        existing-battle check from _check_triggers (an admin explicitly
        asking should still be told plainly if one's already running)."""
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        existing = await db.fetchrow(
            "SELECT id, status FROM discord_roast_battles WHERE guild_id = $1 AND status IN ('pending','active','awaiting_approval') LIMIT 1",
            interaction.guild.id,
        )
        if existing:
            await interaction.followup.send(
                f"⚠️ There's already a {existing['status']} roast battle in this server (battle_id={existing['id']}).",
                view=self._blocking_battle_view(existing),
                ephemeral=True,
            )
            return
        view = RoastTargetPickerView(self, interaction.guild, interaction.user.id)
        await interaction.followup.send(embed=view._status_embed(), view=view, ephemeral=True)
        logger.info(f"[roast] manual trigger opened by admin={interaction.user.id} guild={interaction.guild.id}")

    # ---------- admin config ----------
    # Deliberately NOT its own top-level app_commands.command — the bot's
    # already near Discord's 100-command cap, so config lives as a
    # subcommand on the existing /setup group (setup_channels.py) instead
    # of adding a new one. See configure_from_setup() below, called from
    # there.

    async def configure(self, interaction: discord.Interaction, inactivity_minutes: int = None,
                         random_chance_percent: int = None, enabled: bool = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        clone_id = _clone_id_of(self.bot)
        current = await self.get_config(interaction.guild.id, clone_id)
        new_inactivity = inactivity_minutes if inactivity_minutes is not None else current["inactivity_minutes"]
        new_chance = random_chance_percent if random_chance_percent is not None else current["random_chance_percent"]
        new_enabled = enabled if enabled is not None else current["enabled"]
        await db.execute(
            """
            INSERT INTO discord_roast_config (guild_id, clone_id, inactivity_minutes, random_chance_percent, enabled)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET inactivity_minutes = $3, random_chance_percent = $4, enabled = $5
            """,
            interaction.guild.id, clone_id, new_inactivity, new_chance, new_enabled,
        )
        await interaction.followup.send(
            f"✅ Auto-roast config updated — inactivity: {new_inactivity}m, "
            f"random chance: {new_chance}%, enabled: {new_enabled}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastCog(bot))
