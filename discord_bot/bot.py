# path: discord_bot/bot.py

"""
Entry point for the Discord port. Run with:
    python -m discord_bot.bot                    # the main bot
    python -m discord_bot.bot --clone-id 3        # a specific registered clone

Reuses database.py/payments.py exactly as the Telegram bot does — this file
only wires up discord.py itself.

Normally you don't invoke `--clone-id` by hand: discord_bot/clone_manager.py
is the supervisor that spawns one of these processes per active row in
discord_cloned_bots and keeps it running. Running a clone manually with
this flag is mainly useful for local testing of a single clone.
"""

import argparse
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DISCORD_BOT_TOKEN, DISCORD_DEV_GUILD_ID, DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.views import PremiumPayView, VerifyPaymentView
from discord_bot.cogs.ai_store import VerifyCreditsView, VerifyBoostView, AIStoreMenuView
from discord_bot.cogs._dm_support import GUILD_ONLY_MESSAGE
from discord_bot.cogs._views_join_dm import build_join_dm_view, DYNAMIC_ITEMS
from discord_bot.cogs._views_welcome import DYNAMIC_ITEMS as WELCOME_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_invites import DYNAMIC_ITEMS as INVITES_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_automod_wizard import DYNAMIC_ITEMS as AUTOMOD_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_automod_reminders import DYNAMIC_ITEMS as AUTOMOD_REMINDER_DYNAMIC_ITEMS
from discord_bot.cogs._views_ticket_wizard import DYNAMIC_ITEMS as TICKET_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_community_wizard import DYNAMIC_ITEMS as COMMUNITY_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_economy_wizard import DYNAMIC_ITEMS as ECONOMY_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_leveling_wizard import DYNAMIC_ITEMS as LEVELING_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_download_wizard import DYNAMIC_ITEMS as DOWNLOAD_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_leaderboard_links import DYNAMIC_ITEMS as LEADERBOARD_LINKS_DYNAMIC_ITEMS
from discord_bot.cogs._views_registry_invite_consent import DYNAMIC_ITEMS as REGISTRY_INVITE_CONSENT_DYNAMIC_ITEMS
from discord_bot.cogs._views_giveaway_wizard import DYNAMIC_ITEMS as GIVEAWAY_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs.discover_players import DYNAMIC_ITEMS as DISCOVER_PLAYERS_DYNAMIC_ITEMS
from discord_bot.cogs.roast import ROAST_DYNAMIC_ITEMS
from discord_bot.cogs._views_roast_arena_consent import DYNAMIC_ITEMS as ROAST_ARENA_CONSENT_DYNAMIC_ITEMS
from discord_bot.cogs._views_roast_arena_challenge import DYNAMIC_ITEMS as ROAST_ARENA_CHALLENGE_DYNAMIC_ITEMS
from discord_bot.cogs._views_roast_arena_host_wizard import DYNAMIC_ITEMS as ROAST_ARENA_HOST_WIZARD_DYNAMIC_ITEMS
from discord_bot.cogs._views_music_panel import DYNAMIC_ITEMS as MUSIC_PANEL_DYNAMIC_ITEMS
from discord_bot.cogs._views_direct_paid import DYNAMIC_ITEMS as DIRECT_PAID_DYNAMIC_ITEMS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot")

# Server Members Intent is privileged — must also be turned ON in the
# Discord Developer Portal (Bot -> Privileged Gateway Intents) or on_ready
# will fail to connect. Required here for on_member_join (the paid-at-join
# gate) and for role-hierarchy checks that rely on member/role caches.
# Every clone needs this same intent enabled on its own application in the
# Developer Portal — it's a per-bot setting, not something this code can
# turn on for someone else's registered token.
intents = discord.Intents.default()
intents.members = True
# message_content IS enabled here (unlike before) — discord_bot/cogs/automod.py
# needs raw message text for the word/invite/mention filters. This is a
# privileged intent: it must also be turned ON in the Discord Developer
# Portal (Bot -> Privileged Gateway Intents) for the main bot AND for every
# clone owner's own application, same caveat as Server Members Intent above.
intents.message_content = True


class AnimeBotDiscord(commands.Bot):
    def __init__(self, clone_id: int = None):
        # Tree-wide *default* install/context scope: DM-and-guild-friendly.
        # Individual guild-dependent commands/groups override this back down
        # to guild-only with @app_commands.guild_only() (see
        # discord_bot/cogs/_dm_support.py) — most commands don't need to,
        # they just inherit this default. Enabling "User install" on the
        # Discord Developer Portal (Installation tab) is still a required
        # manual step for any of this to actually reach DMs — it can't be
        # toggled from code.
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=True, private_channel=True
            ),
        )
        # None = this process is the main bot. Cogs read this off
        # self.bot.clone_id to scope premium groups (and anything else
        # clone-specific added later) to the right bot instance — a clone
        # running in the same guild as the main bot must never see or sell
        # into the main bot's premium groups, and vice versa.
        self.clone_id = clone_id

    async def setup_hook(self):
        # Database pool: database.py's get_pool() lazily creates the pool on
        # first use. But table creation (db.init()'s 94 CREATE TABLE IF NOT
        # EXISTS statements) is NOT automatic — it must be called explicitly.
        # Safe to run on every startup (idempotent), so we do it here before
        # any cog tries to query a table that might not exist yet.
        from database import db
        await db.init()
        logger.info("Database tables verified/created.")

        # Persistent views MUST be registered before on_ready fires, so
        # buttons on messages sent before a restart keep working immediately
        # on reconnect. Both views use fixed custom_ids (see views.py) —
        # that's what makes global registration correct across every guild.
        self.add_view(PremiumPayView())
        self.add_view(VerifyPaymentView())
        self.add_view(VerifyCreditsView())
        self.add_view(VerifyBoostView())
        self.add_view(AIStoreMenuView())

        # Dynamic items (per-guild custom_id, e.g. the owner join DM's
        # Remind me later / Don't ask again buttons) — matched by regex
        # rather than registered per fixed custom_id, so one call here
        # covers every guild's buttons, past and future.
        self.add_dynamic_items(*DYNAMIC_ITEMS)
        self.add_dynamic_items(*WELCOME_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*INVITES_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*AUTOMOD_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*AUTOMOD_REMINDER_DYNAMIC_ITEMS)
        self.add_dynamic_items(*TICKET_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*COMMUNITY_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*ECONOMY_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*LEVELING_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*DOWNLOAD_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*LEADERBOARD_LINKS_DYNAMIC_ITEMS)
        self.add_dynamic_items(*REGISTRY_INVITE_CONSENT_DYNAMIC_ITEMS)
        self.add_dynamic_items(*GIVEAWAY_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*DIRECT_PAID_DYNAMIC_ITEMS)
        self.add_dynamic_items(*DISCOVER_PLAYERS_DYNAMIC_ITEMS)
        self.add_dynamic_items(*ROAST_DYNAMIC_ITEMS)
        self.add_dynamic_items(*ROAST_ARENA_CONSENT_DYNAMIC_ITEMS)
        self.add_dynamic_items(*ROAST_ARENA_CHALLENGE_DYNAMIC_ITEMS)
        self.add_dynamic_items(*ROAST_ARENA_HOST_WIZARD_DYNAMIC_ITEMS)
        self.add_dynamic_items(*MUSIC_PANEL_DYNAMIC_ITEMS)

        await self.load_extension("discord_bot.cogs.help")
        await self.load_extension("discord_bot.cogs.archive")
        await self.load_extension("discord_bot.cogs.archive_automation")
        await self.load_extension("discord_bot.cogs.language")
        await self.load_extension("discord_bot.cogs.premium")
        await self.load_extension("discord_bot.cogs.moderation")
        await self.load_extension("discord_bot.cogs.automod")
        await self.load_extension("discord_bot.cogs.reaction_roles")
        await self.load_extension("discord_bot.cogs.leveling")
        await self.load_extension("discord_bot.cogs.music")
        await self.load_extension("discord_bot.cogs.voice_xp")
        await self.load_extension("discord_bot.cogs.starboard")
        await self.load_extension("discord_bot.cogs.suggestions")
        await self.load_extension("discord_bot.cogs.ticket")
        await self.load_extension("discord_bot.cogs.giveaways")
        await self.load_extension("discord_bot.cogs.schedule")
        await self.load_extension("discord_bot.cogs.welcome")
        await self.load_extension("discord_bot.cogs.invites")
        await self.load_extension("discord_bot.cogs.quickstart")
        await self.load_extension("discord_bot.cogs.setup_channels")
        await self.load_extension("discord_bot.cogs.analytics")
        await self.load_extension("discord_bot.cogs.economy")
        await self.load_extension("discord_bot.cogs.heist")
        await self.load_extension("discord_bot.cogs.heist_inventory")
        await self.load_extension("discord_bot.cogs.cards")
        await self.load_extension("discord_bot.cogs.automation")
        await self.load_extension("discord_bot.cogs.discover")
        await self.load_extension("discord_bot.cogs.ai_tools")
        await self.load_extension("discord_bot.cogs.roast")
        await self.load_extension("discord_bot.cogs.roast_arena")
        await self.load_extension("discord_bot.cogs.ship")
        await self.load_extension("discord_bot.cogs.ai_store")
        await self.load_extension("discord_bot.cogs.external_tools")
        await self.load_extension("discord_bot.cogs.crypto_alerts")
        await self.load_extension("discord_bot.cogs.ads_marketplace")
        await self.load_extension("discord_bot.cogs.bump")
        await self.load_extension("discord_bot.cogs.referrals")
        await self.load_extension("discord_bot.cogs.botstore")
        await self.load_extension("discord_bot.cogs.bot_manager")
        await self.load_extension("discord_bot.cogs.submissions")
        await self.load_extension("discord_bot.cogs.media_connect")
        await self.load_extension("discord_bot.cogs.image_search")
        await self.load_extension("discord_bot.cogs.link_buttons")
        await self.load_extension("discord_bot.cogs.autopost")
        await self.load_extension("discord_bot.cogs.feedback")
        await self.load_extension("discord_bot.cogs.discover_players")
        await self.load_extension("discord_bot.cogs.admin")
        if self.clone_id is None:
            # Clone registration/management commands only make sense on the
            # main bot — a clone registering its own clones would need its
            # own gateway token to hand out, which defeats the point.
            await self.load_extension("discord_bot.cogs.clone_admin")

        self.join_dm_reminder_loop.start()

        # Slash command sync: to a single dev guild (near-instant propagation)
        # if DISCORD_DEV_GUILD_ID is set, otherwise global (works everywhere
        # this bot is installed, but can take up to ~1hr to show up).
        # DISCORD_DEV_GUILD_ID only applies to the main bot. It speeds up
        # command propagation to one dev/test guild during development, but
        # that guild var is a single shared env value — a clone process has
        # no guarantee (and usually no reason) to be a member of that guild,
        # so guild-scoped sync there raises 403 Missing Access and crashes
        # the clone on every startup. Clones always sync globally instead.
        if DISCORD_DEV_GUILD_ID and self.clone_id is None:
            guild = discord.Object(id=DISCORD_DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash commands to dev guild {DISCORD_DEV_GUILD_ID}")
        else:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands globally")

        if self.clone_id is not None:
            self._heartbeat_loop.start()

        # Catch-all for anything Phase 1's per-cog guild_only()/interaction_check
        # doesn't cover (e.g. a raw NoPrivateMessage from a command that has
        # @app_commands.guild_only() but no GuildOnlyCog.interaction_check
        # fallback) — friendly ephemeral reply instead of a raw traceback
        # surfaced to the user. Cog-level handling still runs first; this
        # only fires for what reaches the tree unhandled.
        self.tree.on_error = self._on_app_command_error

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # This bot has no legacy prefix commands at all — everything is a
        # slash command via app_commands. command_prefix="!" is still set
        # (some internal machinery expects a Bot instance to have one), so
        # any ordinary message starting with "!" that a user sends (often
        # meant for a different bot, or a guess at a feature) triggers
        # CommandNotFound. Without this override, discord.py's default
        # behavior logs each one as "Ignoring exception in command None" —
        # not an actual failure, just noise on every stray "!" message.
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error(f"Unhandled prefix command error: {error}", exc_info=error)

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.NoPrivateMessage):
            if not interaction.response.is_done():
                await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
            return
        if isinstance(error, app_commands.CheckFailure):
            # Cog-level interaction_check already sent its own message in the
            # normal case; only fall back here if nothing was sent yet.
            if not interaction.response.is_done():
                await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
            return
        if isinstance(error, app_commands.TransformerError) and error.type == discord.AppCommandOptionType.channel:
            # Most common cause: user typed a channel name as plain text
            # (e.g. "general") instead of picking it from Discord's #channel
            # autocomplete dropdown, which is the only thing that actually
            # resolves to a real discord.TextChannel object.
            logger.error(
                f"TransformerError on channel option in /{interaction.command.qualified_name if interaction.command else '?'}: "
                f"{error!r} (value={getattr(error, 'value', None)!r})",
                exc_info=error,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That didn't look like a real channel selection. When filling in a channel field, "
                    "type `#` and pick the channel from the dropdown that appears — don't type the name "
                    "as plain text. Try the command again.",
                    ephemeral=True,
                )
            return
        if isinstance(error, app_commands.TransformerError) and error.type in (
            discord.AppCommandOptionType.user, discord.AppCommandOptionType.mentionable,
        ):
            # Most common cause: the selected person isn't currently a
            # member of this server (left between opening the picker and
            # submitting, or a User was picked where a Member is required
            # — e.g. moderation commands that need someone actually in
            # the guild to act on).
            logger.error(
                f"TransformerError on user/member option in /{interaction.command.qualified_name if interaction.command else '?'}: "
                f"{error!r} (value={getattr(error, 'value', None)!r})",
                exc_info=error,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That person doesn't appear to be a member of this server right now — they may have left. "
                    "Try again with someone currently in the server.",
                    ephemeral=True,
                )
            return
        logger.error(f"Unhandled app command error in /{interaction.command.qualified_name if interaction.command else '?'}: {error}", exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong running that command. Try again shortly.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Something went wrong running that command. Try again shortly.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    @staticmethod
    async def _best_effort_invite(guild: discord.Guild) -> Optional[str]:
        """Tries, in order: guild's vanity URL, or an existing invite the
        bot can read. Both are invites the server itself already made
        visible/available — nothing new gets created here.

        Deliberately does NOT create a fresh invite on its own anymore.
        Top.gg's review flagged that as a privacy issue: silently minting
        an invite the moment the bot joins, with no owner say-so, counts
        as creating access to that server without consent — even though
        it was only ever used for this bot owner's own /admin guilds
        registry, never shown to anyone else. See
        _offer_registry_invite_consent below for the opt-in replacement:
        the owner gets asked via DM, and a fresh invite is only created
        if they say yes.
        """
        try:
            if guild.me.guild_permissions.manage_guild:
                vanity = await guild.vanity_invite()
                if vanity:
                    return vanity.url
        except (discord.HTTPException, discord.Forbidden):
            pass

        try:
            if guild.me.guild_permissions.manage_guild:
                existing = await guild.invites()
                if existing:
                    return existing[0].url
        except (discord.HTTPException, discord.Forbidden):
            pass

        return None

    async def on_ready(self):
        label = "main bot" if self.clone_id is None else f"clone #{self.clone_id}"
        logger.info(f"Logged in as {self.user} (id={self.user.id}) — {label}")
        logger.info(f"In {len(self.guilds)} guild(s)")
        if self.clone_id is not None:
            await db.touch_discord_clone_heartbeat(self.clone_id)
        # Catches up discord_guilds for any server joined while the bot was
        # offline, and backfills invite_url for guilds the bot was already
        # in before this column existed. Checked by ROW EXISTENCE
        # (guild.id not in existing_ids), not by invite_url being set —
        # a guild the bot already knows about can legitimately have a null
        # invite_url (e.g. no create-invite permission in any channel), and
        # keying off that would re-fire _handle_new_guild on every single
        # restart for that guild. Row existence only flips once, when the
        # guild is first ever seen.
        #
        # This "genuinely new" branch matters a lot more for clones than
        # the main bot: a clone's actual gateway connection doesn't start
        # until clone_manager.py's next poll (up to POLL_INTERVAL_SECONDS
        # after /registerclone), so by the time a freshly-registered clone
        # is invited to a server and actually comes online, the real
        # on_guild_join event already fired-and-was-missed entirely — this
        # first on_ready is the only place that join is ever observed.
        # Invite lookup is skipped for already-known guilds, so this stays
        # cheap on every restart after the first.
        existing_rows = await db.list_discord_guilds(self.clone_id, include_left=True)
        existing_ids = {g["guild_id"] for g in existing_rows}
        for guild in self.guilds:
            if guild.id in existing_ids:
                await db.upsert_discord_guild(guild.id, guild.name, guild.member_count, self.clone_id)
            else:
                await self._handle_new_guild(guild)

    async def on_guild_join(self, guild: discord.Guild):
        logger.info(f"Joined guild: {guild.name} ({guild.id})")
        await self._handle_new_guild(guild)

    async def _handle_new_guild(self, guild: discord.Guild):
        """Everything that should happen the first time this bot process
        (main or clone) ever observes itself being in this guild — shared
        by the live on_guild_join event and on_ready's catch-up pass for
        joins that happened while this process wasn't connected yet."""
        invite_url = await self._best_effort_invite(guild)
        await db.upsert_discord_guild(guild.id, guild.name, guild.member_count, self.clone_id, invite_url)
        if invite_url is None:
            from discord_bot.cogs._views_registry_invite_consent import offer_registry_invite_consent
            await offer_registry_invite_consent(self, guild)
        await self._alert_owners_of_join(guild)
        await self._send_combined_owner_join_dm(guild)
        welcome_cog = self.get_cog("WelcomeCog")
        if welcome_cog:
            await welcome_cog.post_setup_wizard_on_join(guild)

    async def _send_combined_owner_join_dm(self, guild: discord.Guild, *, is_initial_send: bool = True):
        """Single consolidated DM to the server owner covering everything
        that used to be several separate on-join DMs (quickstart tips,
        automod's log-channel/word-filter notices, ship's onboarding
        blurb). Best-effort throughout: any individual section that fails
        to build is just skipped, never blocks the others.

        Carries "Remind me later" / "Don't ask again" buttons
        (_views_join_dm.OwnerJoinDMView) — skips sending entirely if the
        owner already hit "Don't ask again" on a previous send (including
        re-sends from join_dm_reminder_loop below).

        The quickstart cog's interactive "want me to create these
        channels?" follow-up still has to be its own message (it carries
        buttons a plain embed field can't hold), so it's sent right after
        as a second, unavoidable message — everything else is now one."""
        clone_id = getattr(self, "clone_id", None)
        if await db.is_join_dm_dismissed(guild.id, clone_id):
            return

        try:
            owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
        except (discord.HTTPException, discord.Forbidden):
            owner = None
        if owner is None or owner.bot:
            return

        # Components V2 rebuild: the message is now one JoinDMLayoutView
        # (see _views_join_dm.py) where every feature's text and its
        # "Turn on" button live in the same Section component — there's
        # no longer a separate embed-fields list and a separate button
        # list that can drift out of sync (that mismatch is exactly what
        # produced the owner-reported bug of buttons floating apart from
        # their text). intro/notices below just supply the content;
        # build_join_dm_view does all the actual layout.
        title = "🚀 Thanks for adding me!"
        intro = f"Here's everything worth knowing about **{guild.name}** in one message:"
        # /download needs no setup, so it has no toggle button and no
        # Section of its own — mentioned in the intro instead so it
        # isn't silently dropped from the DM entirely.
        intro += "\n\n⬇️ **Media downloads** work right away, no setup — grab audio/video from a link with `/download`."

        feature_keys = []
        quickstart_cog = self.get_cog("QuickstartCog")
        record_quickstart_sent = False
        if quickstart_cog:
            # Feature Sections are built from FEATURE_TOGGLES itself
            # (single source of truth — see _views_join_dm.py). Previously
            # a separate embed-fields loop pulled from
            # QuickstartCog.QUICKSTART_ITEMS — a different 7-item list
            # (including /download, which has no toggle at all) — while
            # the buttons were built from a different 10-item
            # feature_keys list. Two lists, two orderings, two lengths:
            # that's what let a field and its "Turn on" button end up
            # unrelated once pagination split them apart. Now there's
            # only ever this one list, and build_join_dm_view builds both
            # the text and the button for each key together.
            feature_keys = [
                "welcome", "automod", "reactionroles", "leveling",
                "analytics", "bump", "channels", "tickets",
                "starboard", "suggestions",
            ]
            record_quickstart_sent = True

        notices = []
        automod_cog = self.get_cog("AutomodCog")
        if automod_cog:
            try:
                for notice_title, body in await automod_cog.build_join_notice_fields(guild, clone_id=clone_id):
                    notices.append((notice_title, body))
            except Exception:
                logger.exception(f"[join-dm] automod section failed for guild {guild.id}")

        ship_cog = self.get_cog("ShipCog")
        if ship_cog:
            try:
                field = await ship_cog.build_join_notice_field(guild)
                if field:
                    notices.append((field[0], field[1]))
            except Exception:
                logger.exception(f"[join-dm] ship section failed for guild {guild.id}")

        welcome_cog = self.get_cog("WelcomeCog")
        if welcome_cog:
            try:
                field = await welcome_cog.build_join_notice_field(guild, clone_id=clone_id)
                if field:
                    notices.append((field[0], field[1]))
            except Exception:
                logger.exception(f"[join-dm] ultra pack section failed for guild {guild.id}")

        try:
            from discord_bot.cogs._views_join_dm import _enabled_feature_keys
            enabled = await _enabled_feature_keys(guild.id, clone_id)
            view = build_join_dm_view(
                guild.id, clone_id=clone_id, feature_keys=feature_keys,
                intro=intro, title=title, notices=notices, enabled_keys=enabled,
            )
            await owner.send(view=view)
            # Only recorded as "sent" once the DM actually goes out — this
            # used to fire right after building the quickstart section's
            # embed fields, before owner.send was even attempted, so an
            # owner with closed DMs (Forbidden below) was silently marked
            # as notified and never got a retry or a follow-up. Still
            # gated on record_quickstart_sent so behavior matches before:
            # only recorded when QuickstartCog was loaded and its section
            # built successfully.
            if record_quickstart_sent:
                await db.mark_quickstart_dm_sent(guild.id, clone_id)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            logger.info(f"[join-dm] Could not DM combined join notice to owner of guild {guild.id}")
        except Exception:
            logger.exception(f"[join-dm] Unexpected failure sending combined join DM for guild {guild.id}")

        # Backup copy posted in-server too — the DM above is the primary
        # copy, but a closed-DMs owner (the exact case caught above) would
        # otherwise never see any of this. Best-effort and independent of
        # whether the DM succeeded: even when the DM lands fine, an owner
        # who reads the server before their DMs still gets it, and every
        # feature button below already re-checks Manage Server permission
        # per-click, so it's safe for any staff member to see and use, not
        # just the owner.
        #
        # Gated on is_initial_send so this only ever posts once, on the
        # actual join — join_dm_reminder_loop calls this same function
        # again (with is_initial_send=False) for owners who haven't
        # dismissed the DM, and without this gate every hourly reminder
        # resend would also dump a fresh backup copy into the server,
        # which is redundant once the first one is already sitting there.
        if is_initial_send:
            try:
                from discord_bot.cogs._views_join_dm import _default_text_channel, _enabled_feature_keys
                channel = _default_text_channel(guild)
                if channel is not None:
                    enabled = await _enabled_feature_keys(guild.id, clone_id)
                    backup_view = build_join_dm_view(
                        guild.id, clone_id=clone_id, feature_keys=feature_keys,
                        intro=intro, title=title, notices=notices, enabled_keys=enabled,
                    )
                    await channel.send(view=backup_view)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                logger.info(f"[join-dm] Could not post backup join notice in guild {guild.id}")
            except Exception:
                logger.exception(f"[join-dm] Unexpected failure posting backup join notice for guild {guild.id}")

        # No automatic second message here anymore. Channel-creation used
        # to always get its own follow-up DM, sent unconditionally right
        # after this one — meaning the owner was offered channel setup
        # twice, in two disconnected messages, before touching either.
        # Now the "📁 Create suggested channels" field/button above is the
        # only entry point: tapping it swaps this same message into the
        # picker in place (see _enable_channels in _views_join_dm.py), so
        # there's a single live channel-creation UI, never two out of sync.

    @tasks.loop(hours=1)
    async def join_dm_reminder_loop(self):
        """Resends _send_combined_owner_join_dm once for any guild whose
        owner hit "Remind me later" and whose remind_at has come due.
        _send_combined_owner_join_dm itself re-checks the dismissed flag,
        so a "Don't ask again" click in the meantime still short-circuits
        this. remind_at is cleared either way so a guild is never
        re-queued after this pass."""
        clone_id = getattr(self, "clone_id", None)
        try:
            guild_ids = await db.list_due_join_dm_reminders(clone_id)
        except Exception:
            logger.exception("[join-dm] reminder loop: failed to list due reminders")
            return
        for guild_id in guild_ids:
            guild = self.get_guild(guild_id)
            try:
                if guild is not None:
                    await self._send_combined_owner_join_dm(guild, is_initial_send=False)
            except Exception:
                logger.exception(f"[join-dm] reminder loop: resend failed for guild {guild_id}")
            finally:
                await db.clear_join_dm_remind_at(guild_id, clone_id)

    @join_dm_reminder_loop.before_loop
    async def _before_join_dm_reminder_loop(self):
        await self.wait_until_ready()

    async def _owner_ids_for_alert(self) -> list:
        """Who gets DM'd when this bot is added to a new server.

        Main bot: every configured clone admin (DISCORD_CLONE_ADMIN_IDS) —
        there's no single "owner" for the main bot, it's run by the whole
        admin team.
        Clone: just the one Discord user who registered this clone via
        /registerclone (discord_cloned_bots.owner_id) — clones are
        single-owner by design.
        """
        if self.clone_id is None:
            return list(DISCORD_CLONE_ADMIN_IDS)
        clone = await db.get_discord_clone(self.clone_id)
        if clone and clone.get("owner_id"):
            return [clone["owner_id"]]
        return []

    async def _alert_owners_of_join(self, guild: discord.Guild):
        """DMs the bot's owner(s) immediately when it's added to a new
        server. Best-effort: a closed-DM owner or a rate-limited send
        should never block on_guild_join or crash the gateway handler."""
        owner_ids = await self._owner_ids_for_alert()
        if not owner_ids:
            return

        try:
            owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
        except (discord.HTTPException, discord.Forbidden):
            owner = None

        embed = discord.Embed(
            title="🔔 Bot added to a new server",
            description=f"**{guild.name}**",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Server ID", value=str(guild.id), inline=True)
        embed.add_field(name="Members", value=str(guild.member_count or "?"), inline=True)
        if owner:
            embed.add_field(name="Server Owner", value=f"{owner} ({owner.id})", inline=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        for user_id in owner_ids:
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(embed=embed)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                logger.warning(f"Could not DM join alert to owner {user_id}")

    async def on_guild_remove(self, guild: discord.Guild):
        logger.info(f"Left guild: {guild.name} ({guild.id})")
        await db.mark_discord_guild_left(guild.id, self.clone_id)

    @tasks.loop(minutes=5)
    async def _heartbeat_loop(self):
        """Lets /myclones show "last seen" rather than just "registered" —
        clone_manager.py only knows whether the *process* is alive, not
        whether the bot ever actually completed login, so this is the
        stronger liveness signal."""
        if self.clone_id is not None:
            await db.touch_discord_clone_heartbeat(self.clone_id)


async def _resolve_token(clone_id: int = None) -> str:
    """Returns the bot token to log in with. clone_id=None means the main
    bot (token from env). Otherwise looks up and decrypts the clone's
    stored token — raises if the clone doesn't exist or isn't active, so a
    stale/removed clone_manager entry fails loudly instead of silently
    logging in as the main bot with the wrong identity."""
    if clone_id is None:
        if not DISCORD_BOT_TOKEN:
            raise RuntimeError(
                "DISCORD_BOT_TOKEN is not set. Set it in your environment "
                "(Railway Variables tab, or a local .env) before running the Discord bot."
            )
        return DISCORD_BOT_TOKEN

    from utils.crypto import secret_manager  # local import: only needed in clone mode

    clone = await db.get_discord_clone(clone_id)
    if not clone:
        raise RuntimeError(f"No discord_cloned_bots row with clone_id={clone_id}.")
    if clone["status"] != "active":
        raise RuntimeError(f"Clone #{clone_id} is not active (status={clone['status']}).")

    token = secret_manager.decrypt(clone["bot_token_encrypted"])
    if not token:
        raise RuntimeError(f"Could not decrypt stored token for clone #{clone_id} — check ENCRYPTION_KEY.")
    return token


async def main():
    parser = argparse.ArgumentParser(description="Run the Discord bot (main or a specific clone).")
    parser.add_argument(
        "--clone-id", type=int, default=None,
        help="Run as this registered clone instead of the main bot (see discord_cloned_bots).",
    )
    args = parser.parse_args()

    token = await _resolve_token(args.clone_id)
    bot = AnimeBotDiscord(clone_id=args.clone_id)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
