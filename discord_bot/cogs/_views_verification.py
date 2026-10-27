# path: discord_bot/cogs/_views_verification.py

"""
Join-verification gate: the persistent parts that live in the #verify
channel long-term (posted once by the wizard, clicked by new members
indefinitely afterward).

Two modes, one shared role-swap:
  - "button": VerifyButton just needs a click.
  - "captcha": VerifyButton opens a CaptchaModal with a small addition
    question instead of verifying immediately.

Built as discord.ui.DynamicItem (same pattern as every other persistent
button in this codebase — see _views_direct_paid.py's docstring) so the
button keeps working after bot restarts and never times out. The
guild_id rides in the custom_id itself; registered once via
bot.add_dynamic_items(...) in setup_hook.

CaptchaModal is deliberately NOT a DynamicItem — modals aren't shown
until a (persistent) button is clicked, so there's no restart-survival
problem to solve for it. Its correct answer is encoded directly in its
own custom_id (verify_captcha:<guild_id>:<a>:<b>) instead of kept in
memory, so a bot restart between "modal opened" and "modal submitted"
can't lose the question — the tradeoff is the answer is visible to
anyone inspecting the interaction payload client-side, which is fine
for a bot-filtering captcha (it only needs to stop naive script kiddies
hammering /interactions, not a determined human).
"""

import re
import logging
import random

import discord

import config as _cfg
from database import db
from utils.verify_captcha import sign_challenge

logger = logging.getLogger(__name__)

# custom_id shapes:
#   verify_btn:<guild_id>
#   verify_captcha:<guild_id>:<a>:<b>   (modal, not a DynamicItem — see above)
_VERIFY_BTN_RE = re.compile(r"^verify_btn:(\d+)$")
_CAPTCHA_MODAL_RE = re.compile(r"^verify_captcha:(\d+):(\d+):(\d+)$")
_VERIFY_DONE_RE = re.compile(r"^verify_done:(\d+)$")
_VERIFY_DM_RE = re.compile(r"^verify_dm:(\d+)$")


def _turnstile_ready() -> bool:
    return bool(getattr(_cfg, "TURNSTILE_ENABLED", False))


def _diagnose_role_failure(guild: discord.Guild, role: discord.Role | None) -> str:
    """Best-effort guess at *why* the bot couldn't touch `role`, phrased so
    a server admin can act on it immediately without digging through logs."""
    me = guild.me
    if role is None:
        return "the configured role no longer exists — a staff member needs to re-run verification setup and pick the role again"
    if role.is_default():
        return "that role can't be managed by any bot (it's the @everyone role) — a staff member needs to fix the verification setup"
    if role.managed:
        return f"the **{role.name}** role is managed by another bot/integration, so I'm not allowed to touch it — staff should pick a different role in verification setup"
    if not me.guild_permissions.manage_roles:
        return "I'm missing the **Manage Roles** permission in this server — a staff member needs to grant it to my role"
    if role >= me.top_role:
        return (
            f"my highest role sits **below {role.name}** in Server Settings → Roles — a staff member needs to drag "
            f"my role above {role.name} (Discord bots can only manage roles ranked below their own)"
        )
    return "something unexpected went wrong on Discord's end — a staff member should check the bot's audit log or try again shortly"


async def _after_verified(member: discord.Member, config: dict) -> None:
    """Premium follow-ups (log line, custom welcome DM). Never raises: a verified member stays verified."""
    try:
        from modules import verification_pro
        cfg = await verification_pro.effective_for(member.guild.id, config.get("clone_id"), config)
        await verification_pro.on_verified(member, cfg)
    except Exception:
        logger.exception("verification: premium follow-up failed for %s in guild %s", member.id, member.guild.id)


async def do_verify(member: discord.Member, config: dict) -> tuple[bool, str | None]:
    """Swaps Unverified -> Verified (or just drops Unverified if no
    verified_role_id is configured). Returns (success, reason). Best-effort:
    a missing role / stale role id / missing bot permission just logs and
    reports failure (with a human-readable reason) rather than raising into
    the interaction handler."""
    guild = member.guild
    unverified_role = guild.get_role(config.get("unverified_role_id")) if config.get("unverified_role_id") else None
    verified_role = guild.get_role(config.get("verified_role_id")) if config.get("verified_role_id") else None
    try:
        if unverified_role and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason="Passed join verification")
        if verified_role:
            await member.add_roles(verified_role, reason="Passed join verification")
    except discord.Forbidden:
        # Figure out which of the two roles was actually the problem so the
        # staff-facing message points at the right one.
        culprit = verified_role
        if unverified_role and unverified_role in member.roles:
            me = guild.me
            if unverified_role.managed or unverified_role >= me.top_role or not me.guild_permissions.manage_roles:
                culprit = unverified_role
        reason = _diagnose_role_failure(guild, culprit)
        logger.warning(
            "verification: missing permission to swap roles for %s in guild %s — %s", member.id, guild.id, reason
        )
        return False, reason
    except discord.HTTPException as e:
        logger.warning("verification: role swap failed for %s in guild %s: %s", member.id, guild.id, e)
        return False, "Discord rejected the role update — a staff member should check the bot's permissions and try again"
    await _after_verified(member, config)
    return True, None


class CaptchaModal(discord.ui.Modal, title="Verify you're human"):
    def __init__(self, guild_id: int, a: int, b: int):
        super().__init__(custom_id=f"verify_captcha:{guild_id}:{a}:{b}")
        self.guild_id = guild_id
        self.a = a
        self.b = b
        # TextInput.label is deprecated as of discord.py 2.6 in favor of
        # wrapping the input in a Label component (the new Components v2
        # pattern). The question text is only known once a/b are picked in
        # __init__, so this can't be a static class-level TextInput like it
        # used to be — build it here and add it explicitly instead.
        self.answer = discord.ui.TextInput(placeholder="Type the number", max_length=5)
        self.add_item(discord.ui.Label(text=f"What is {a} + {b}?", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This challenge isn't for this server.", ephemeral=True)
            return
        try:
            given = int(str(self.answer.value).strip())
        except ValueError:
            given = None
        if given != self.a + self.b:
            await interaction.response.send_message(
                "❌ That's not right. Click the verify button again to get a new question.", ephemeral=True
            )
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        config = await db.get_verification_config(self.guild_id, clone_id=clone_id)
        ok, reason = await do_verify(interaction.user, config)
        if ok:
            await interaction.response.send_message("✅ You're verified — welcome in!", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"⚠️ Verified, but I couldn't update your roles: {reason}.", ephemeral=True
            )


async def apply_verification_pass(bot_or_client, pass_row: dict) -> bool:
    """Swaps roles for a claimed Turnstile pass. Used by the background poller and
    the 'I've completed it' button. Returns True if roles were updated."""
    guild = bot_or_client.get_guild(pass_row["guild_id"])
    if guild is None:
        return False
    member = guild.get_member(pass_row["user_id"])
    if member is None:
        try:
            member = await guild.fetch_member(pass_row["user_id"])
        except discord.HTTPException:
            return False
    clone_id = getattr(bot_or_client, "clone_id", None)
    cfg = await db.get_verification_config(guild.id, clone_id=clone_id)
    if not cfg.get("enabled"):
        return False
    ok, _reason = await do_verify(member, cfg)
    if ok:
        from modules import verification_pro
        custom = (await verification_pro.effective_for(guild.id, clone_id, cfg)).get("welcome_text")
        if not custom:      # a premium custom welcome was already sent by do_verify
            try:
                await member.send(f"✅ You're verified in **{guild.name}** — welcome in!")
            except discord.HTTPException:
                pass
    return ok


class VerifyDoneButton(discord.ui.DynamicItem[discord.ui.Button], template=_VERIFY_DONE_RE.pattern):
    """'I've completed it' — instant check for a solved Turnstile page, so people
    don't have to wait for the poller's next tick."""

    def __init__(self, guild_id: int):
        super().__init__(
            discord.ui.Button(
                label="I've completed it", emoji="🔄", style=discord.ButtonStyle.secondary,
                custom_id=f"verify_done:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This button isn't for this server.", ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        passes = await db.claim_verification_passes(clone_id, guild_id=self.guild_id, user_id=interaction.user.id)
        if not passes:
            await interaction.response.send_message(
                "I don't see a completed check yet. Finish the captcha on the page, then tap this again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        ok = await apply_verification_pass(interaction.client, passes[0])
        await interaction.followup.send(
            "✅ You're verified — welcome in!" if ok
            else "⚠️ Captcha passed, but I couldn't update your roles — ask a staff member to check my permissions.",
            ephemeral=True,
        )


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=_VERIFY_BTN_RE.pattern):
    def __init__(self, guild_id: int):
        super().__init__(
            discord.ui.Button(
                label="I'm not a bot",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"verify_btn:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This button isn't for this server.", ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        config = await db.get_verification_config(self.guild_id, clone_id=clone_id)
        if not config.get("enabled"):
            await interaction.response.send_message("Verification isn't currently active here.", ephemeral=True)
            return
        if config.get("mode") == "captcha":
            if _turnstile_ready():
                token = sign_challenge(_cfg.VERIFY_SIGNING_SECRET, self.guild_id, interaction.user.id, clone_id)
                try:    # a link just went out: keep the pass poller at full speed for the next 10 minutes
                    interaction.client.get_cog("VerificationCog").wake_poll()
                except Exception:
                    logger.debug("swallowed exception in VerifyButton.callback", exc_info=True)
                    pass
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(
                    label="Open captcha", emoji="🧩", style=discord.ButtonStyle.link,
                    url=f"{_cfg.TURNSTILE_PAGES_URL}/?t={token}",
                ))
                await interaction.response.send_message(
                    "Solve the quick Cloudflare check on the page (valid for 10 minutes). "
                    "You'll be verified automatically within a few seconds.",
                    view=view, ephemeral=True,
                )
                return
            # Turnstile not configured -> fall back to the in-Discord question.
            a, b = random.randint(1, 9), random.randint(1, 9)
            await interaction.response.send_modal(CaptchaModal(self.guild_id, a, b))
            return
        ok, reason = await do_verify(interaction.user, config)
        if ok:
            await interaction.response.send_message("✅ You're verified — welcome in!", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"⚠️ I couldn't update your roles: {reason}.", ephemeral=True
            )


# ── verify by DM (free) ─────────────────────────────────────────────────────
# The new member gets a DM with this button. It is a DynamicItem like every other persistent button, so a DM sent
# before a restart still works after it. The guild id rides in the custom_id; the member is looked up from the
# guild (a DM interaction has no guild of its own).

async def _member_for_dm(client, guild_id: int, user_id: int):
    """(guild, member) for a DM click. member is None when they have left the server."""
    guild = client.get_guild(guild_id)
    if guild is None:
        return None, None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    return guild, member


def _already_verified(member: discord.Member, config: dict) -> bool:
    """True when the member no longer has the Unverified role (so there is nothing left to do)."""
    rid = config.get("unverified_role_id")
    return bool(rid) and all(r.id != rid for r in member.roles)


class DmCaptchaModal(discord.ui.Modal, title="Verify you're human"):
    """The in-Discord question for DM verification. Same idea as CaptchaModal, but the member is looked up from the
    guild id because a DM interaction has no guild."""

    def __init__(self, guild_id: int, a: int, b: int):
        super().__init__(custom_id=f"verify_dmcaptcha:{guild_id}:{a}:{b}")
        self.guild_id, self.a, self.b = guild_id, a, b
        self.answer = discord.ui.TextInput(placeholder="Type the number", max_length=5)
        self.add_item(discord.ui.Label(text=f"What is {a} + {b}?", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            given = int(str(self.answer.value).strip())
        except ValueError:
            given = None
        if given != self.a + self.b:
            await interaction.response.send_message("❌ That's not right. Tap the verify button again for a new question.")
            return
        guild, member = await _member_for_dm(interaction.client, self.guild_id, interaction.user.id)
        if guild is None or member is None:
            await interaction.response.send_message("I can't find you in that server any more.")
            return
        config = await db.get_verification_config(self.guild_id, clone_id=getattr(interaction.client, "clone_id", None))
        ok, reason = await do_verify(member, config)
        await interaction.response.send_message(
            f"✅ You're verified in **{guild.name}**. Welcome in!" if ok
            else f"⚠️ You passed, but I couldn't update your roles: {reason}.")
        if ok:
            await _retire_dm_prompt(interaction.message, guild.name)


async def _retire_dm_prompt(message, guild_name: str) -> None:
    """Swap the DM's button for a done line so it can't be pressed again. Best effort."""
    if message is None:
        return
    try:
        view = discord.ui.LayoutView(timeout=None)
        c = discord.ui.Container(accent_colour=discord.Color.green())
        c.add_item(discord.ui.TextDisplay(f"## ✅ Verified\nYou're in **{guild_name}**."))
        view.add_item(c)
        await message.edit(view=view)
    except discord.HTTPException:
        logger.debug("verification: couldn't retire the DM prompt", exc_info=True)


class VerifyDmButton(discord.ui.DynamicItem[discord.ui.Button], template=_VERIFY_DM_RE.pattern):
    def __init__(self, guild_id: int):
        super().__init__(discord.ui.Button(
            label="Verify me", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"verify_dm:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        guild, member = await _member_for_dm(interaction.client, self.guild_id, interaction.user.id)
        if guild is None:
            await interaction.response.send_message("I can't reach that server right now. Try again in a minute.")
            return
        if member is None:
            await interaction.response.send_message(f"You're not in **{guild.name}** any more, so there's nothing to verify.")
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        config = await db.get_verification_config(self.guild_id, clone_id=clone_id)
        if not config.get("enabled"):
            await interaction.response.send_message("Verification isn't active in that server right now.")
            return
        if _already_verified(member, config):
            await interaction.response.send_message(f"You're already verified in **{guild.name}** ✅")
            await _retire_dm_prompt(interaction.message, guild.name)
            return
        if config.get("mode") == "captcha":
            if _turnstile_ready():
                token = sign_challenge(_cfg.VERIFY_SIGNING_SECRET, self.guild_id, interaction.user.id, clone_id)
                try:
                    interaction.client.get_cog("VerificationCog").wake_poll()
                except Exception:
                    logger.debug("swallowed exception in VerifyDmButton.callback", exc_info=True)
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(
                    label="Open captcha", emoji="🧩", style=discord.ButtonStyle.link,
                    url=f"{_cfg.TURNSTILE_PAGES_URL}/?t={token}"))
                await interaction.response.send_message(
                    "Solve the quick Cloudflare check on the page (valid for 10 minutes). "
                    "You'll be verified automatically within a few seconds, and I'll DM you when it's done.", view=view)
                return
            a, b = random.randint(1, 9), random.randint(1, 9)
            await interaction.response.send_modal(DmCaptchaModal(self.guild_id, a, b))
            return
        ok, reason = await do_verify(member, config)
        await interaction.response.send_message(
            f"✅ You're verified in **{guild.name}**. Welcome in!" if ok
            else f"⚠️ I couldn't update your roles: {reason}.")
        if ok:
            await _retire_dm_prompt(interaction.message, guild.name)


def build_dm_prompt(guild: discord.Guild, mode: str, look: dict | None = None) -> discord.ui.LayoutView:
    """The DM a new member gets. `look` is the Premium panel look of an EFFECTIVE config (empty for free servers)."""
    look = look or {}
    text = look.get("panel_text") or (
        "Tap the button below to prove you're a real person and unlock the rest of the server."
        + ("\nYou'll get a quick security check." if mode == "captcha" else ""))
    colour = discord.Color(look["panel_color"]) if look.get("panel_color") is not None else discord.Color.green()
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=colour)
    c.add_item(discord.ui.TextDisplay(f"## {look.get('panel_title') or f'Welcome to {guild.name} 👋'}\n{text}"))
    if look.get("panel_image_url"):
        c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(look["panel_image_url"])))
    c.add_item(discord.ui.ActionRow(VerifyDmButton(guild.id)))
    view.add_item(c)
    return view


async def send_verify_dm(member: discord.Member, config: dict) -> bool:
    """DM the new member their verify prompt. False when their DMs are closed (they use the verify channel instead).
    Never raises."""
    try:
        from modules import verification_pro
        eff = await verification_pro.effective_for(member.guild.id, config.get("clone_id"), config)
        view = build_dm_prompt(member.guild, config.get("mode") or "captcha", verification_pro.panel_look(eff))
        await member.send(view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
    except Exception:
        logger.exception("verification: DM prompt failed for %s in guild %s", member.id, member.guild.id)
        return False


def build_verify_panel_embed(guild_name: str, mode: str, look: dict | None = None) -> discord.Embed:
    """`look` is the premium custom-look dict (modules.verification_pro.panel_look of an EFFECTIVE config).
    Empty/None = the default look every server gets."""
    look = look or {}
    custom_text = look.get("panel_text")
    desc = custom_text or "Click the button below to verify you're a real person and unlock the rest of the server."
    if mode == "captcha" and not custom_text:
        # Deliberately generic: the check is a Cloudflare page when Turnstile is
        # configured, otherwise a short in-Discord question — the description
        # shouldn't promise one specific kind of challenge.
        desc += "\nYou'll be asked to pass a quick security check."
    color = discord.Color(look["panel_color"]) if look.get("panel_color") is not None else discord.Color.green()
    embed = discord.Embed(title=look.get("panel_title") or f"Welcome to {guild_name} 👋", description=desc, color=color)
    if look.get("panel_image_url"):
        embed.set_image(url=look["panel_image_url"])
    return embed


def build_verify_panel_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton(guild_id))
    return view


async def lockdown_guild_channels(guild: discord.Guild, unverified_role: discord.Role, verify_channel_id: int) -> int:
    """Applies the Unverified: view_channel=False overwrite across every
    existing channel except the verify channel (which gets the opposite
    overwrite so it's the one thing new members can see). Returns the
    number of channels touched. Best-effort per-channel — one failed
    overwrite (e.g. missing Manage Channels in that specific channel)
    doesn't stop the rest from being locked down."""
    touched = 0
    for channel in guild.channels:
        try:
            if channel.id == verify_channel_id:
                overwrite = channel.overwrites_for(unverified_role)
                overwrite.view_channel = True
                overwrite.send_messages = False
                await channel.set_permissions(unverified_role, overwrite=overwrite, reason="Verification setup")
            else:
                overwrite = channel.overwrites_for(unverified_role)
                overwrite.view_channel = False
                await channel.set_permissions(unverified_role, overwrite=overwrite, reason="Verification setup")
            touched += 1
        except discord.Forbidden:
            logger.warning("verification lockdown: missing permission on channel %s in guild %s", channel.id, guild.id)
        except discord.HTTPException as e:
            logger.warning("verification lockdown: failed on channel %s in guild %s: %s", channel.id, guild.id, e)
    return touched


DYNAMIC_ITEMS = [VerifyButton, VerifyDoneButton, VerifyDmButton]
