# path: discord_bot/cogs/_views_join_dm.py

"""
Buttons on the combined owner join DM (discord_bot/bot.py's
_send_combined_owner_join_dm): "Remind me later" reschedules that same DM
once via discord_quickstart_dm.remind_at (picked up by
AnimeBotDiscord.join_dm_reminder_loop), "Don't ask again" sets the
`dismissed` flag so neither the original send path nor the reminder loop
ever contacts this owner about it again. Plus one "Turn on" wizard button
per quickstart feature (see FeatureToggleButton below) that flips the
feature on with sane defaults, no slash command required.

Built with discord.ui.DynamicItem (not a plain discord.ui.View) so the
buttons keep working across bot restarts and NEVER time out — the
guild_id/clone_id (and, for feature buttons, the feature key) are encoded
straight into each button's custom_id and parsed back out by the class'
`from_custom_id`, rather than relying on an in-memory view instance
staying alive or a View(timeout=...) window. Registered once via
bot.add_dynamic_items(...) in setup_hook, same idea as the fixed-custom_id
persistent views already registered there (PremiumPayView etc.) — this
just needs a per-guild (and per-feature) id in the custom_id, which a
plain persistent View can't do.
"""

import re
import asyncio
import logging

import discord

from database import db
from config import DASHBOARD_BASE_URL, DISCORD_SUPPORT_SERVER_INVITE

logger = logging.getLogger(__name__)

# custom_id shape: join_dm_feat:<feature_key>:<guild_id>:<clone_id or "-">
_FEATURE_ID_RE = re.compile(r"^join_dm_feat:([a-z_]+):(\d+):(-|\d+)$")


def _encode(action: str, guild_id: int, clone_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    return f"join_dm_{action}:{guild_id}:{clone_part}"


def _decode(match: "re.Match"):
    """Used only by _RemindLaterButton/_DontAskAgainButton, whose
    templates are now each their own distinct 2-group pattern
    (^join_dm_remind:(\\d+):(-|\\d+)$ / ^join_dm_dismiss:...$) — group(1)
    is guild_id, group(2) is the clone-id-or-dash. Previously both
    buttons shared one 3-group template with the action name as group(1)
    (see the fixed dynamic-item template collision above), which is why
    this used to read group(2)/group(3) instead."""
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    clone_id = None if clone_part == "-" else int(clone_part)
    return guild_id, clone_id


async def _enabled_feature_keys(guild_id: int, clone_id) -> set:
    """Which FEATURE_TOGGLES keys are already turned on for this guild,
    so rebuilding the button set (pagination, Back) can show "On: ..."
    instead of re-offering a "Turn on" a feature already has. Checked
    per-feature since each one persists its enabled state differently;
    reactionroles/analytics/channels have no single enabled flag (they're
    one-tap actions, not toggles) so they're never reported as "on" here.
    Best-effort: a lookup failure just means that one feature falls back
    to showing as not-yet-enabled rather than blocking the rebuild."""
    enabled = set()
    checks = (
        ("welcome", lambda: db.get_welcome_config(guild_id, clone_id=clone_id), "enabled"),
        ("automod", lambda: db.get_automod_config(guild_id, clone_id=clone_id), "word_filter_enabled"),
        ("leveling", lambda: db.get_voice_xp_config(guild_id, clone_id=clone_id), "enabled"),
        ("bump", lambda: db.bump_get_guild_config(guild_id, clone_id), "receives_bumps"),
        ("tickets", lambda: db.get_ticket_config(guild_id, clone_id=clone_id), "panel_channel_id"),
        ("starboard", lambda: db.get_starboard_config(guild_id, clone_id=clone_id), "channel_id"),
        ("suggestions", lambda: db.get_suggestion_config(guild_id, clone_id=clone_id), "approved_log_channel_id"),
        ("downloadhub", lambda: db.get_download_config(guild_id, clone_id=clone_id), "channel_id"),
    )
    for key, fetch, field in checks:
        try:
            config = await fetch()
            if config and config.get(field):
                enabled.add(key)
        except Exception:
            pass
    return enabled


def _apply_enabled_state(view, enabled_keys: set) -> None:
    """Deprecated no-op, kept only so any straggler caller doesn't crash.
    JoinDMLayoutView now takes enabled_keys directly in its constructor
    (see build_join_dm_view) and applies the post-toggle "On: ..." look
    while building the Section/accessory pair in one place, instead of
    mutating a flat view.children list after the fact — there's no flat
    button list to walk anymore now that buttons live nested inside
    per-feature Sections."""
    return


def _paginate(feature_keys) -> tuple:
    """Shared page-math helper — the single source of truth for how
    feature_keys splits into pages, so the layout builder and the nav
    button below can never compute a different page count for the same
    list."""
    keys = [k for k in (feature_keys or []) if k in FEATURE_TOGGLES]
    total_pages = max(1, -(-len(keys) // FEATURES_PER_PAGE))  # ceil div
    return keys, total_pages


class JoinDMLayoutView(discord.ui.LayoutView):
    """Components V2 rebuild of the combined owner join DM. Replaces the
    old embed-fields-plus-a-separate-button-tree approach — which is what
    let a "Turn on" button visually drift away from the feature text it
    belonged to (an owner-reported bug: word-filter/Ship notice fields
    had no button under them at all, while the DM read as if every field
    should have one).

    Each feature is now a discord.ui.Section: its text and its "Turn on"
    button are ONE component, accessory-attached, so they can never be
    laid out apart from each other — there's no longer a separate embed
    field list and a separate button row list to fall out of sync.
    Informational notices (word-filter status, Ship) get their own
    Section too, with no accessory — visually grouped the same way, so
    it reads as "these are notices" rather than "these buttons are
    missing," matching the bordered/no-button notice card in the
    reference mockup rather than looking like a broken toggle.

    timeout=None + DynamicItem accessories (unchanged from before) means
    this still survives bot restarts and never expires."""

    def __init__(
        self, guild_id: int, clone_id, feature_keys, page: int, intro: str,
        title: str = "🚀 Thanks for adding me!", notices=None, enabled_keys=None,
    ):
        super().__init__(timeout=None)
        keys, total_pages = _paginate(feature_keys)
        page = max(0, min(page, total_pages - 1))
        page_keys = keys[page * FEATURES_PER_PAGE:(page + 1) * FEATURES_PER_PAGE]
        enabled_keys = enabled_keys or set()

        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(f"## {title}\n{intro}"))

        for key in page_keys:
            label, emoji, _, _, blurb = FEATURE_TOGGLES[key]
            btn = _FeatureToggleButton(key, guild_id, clone_id)
            if key in enabled_keys:
                btn.item.label = f"On: {label}"
                btn.item.style = discord.ButtonStyle.secondary
                btn.item.emoji = None
                btn.item.disabled = True
            section = discord.ui.Section(accessory=btn)
            section.add_item(f"{emoji} **{label}**\n{blurb}")
            container.add_item(section)

        # Informational-only notices (word filter status, Ship, etc.) —
        # no accessory button on purpose, grouped separately with a
        # divider so they read as "FYI" rather than "toggle missing its
        # button." Matches the bordered notice-card treatment in the
        # reference mockup, which also ships these as text + a slash-
        # command hint, never a button.
        if notices:
            container.add_item(discord.ui.Separator())
            for notice_title, notice_body in notices:
                container.add_item(discord.ui.TextDisplay(f"**{notice_title}**\n{notice_body}"))

        footer = "Run /help anytime for the full command list."
        if total_pages > 1:
            footer = f"Page {page + 1}/{total_pages} — {footer}"
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))

        nav_children = []
        if total_pages > 1:
            if page > 0:
                nav_children.append(_PageNavButton("prev", guild_id, clone_id, page))
            if page < total_pages - 1:
                nav_children.append(_PageNavButton("next", guild_id, clone_id, page))
        if nav_children:
            container.add_item(discord.ui.ActionRow(*nav_children))

        bottom_children = [_RemindLaterButton(guild_id, clone_id), _DontAskAgainButton(guild_id, clone_id)]
        # Support-server link and manual link — shown on every page (not
        # just the last) so neither is hidden behind pagination the owner
        # may never click through. Plain link buttons — no custom_id, so
        # they need no DynamicItem registration and are unaffected by
        # timeouts or restarts on their own.
        # One at the front of the row and one at the back, so at least
        # one is visible no matter which end of the row the owner's eye
        # lands on first. (Previously this was the support-server link
        # shown twice; the manual link now takes the second slot instead
        # of a duplicate.)
        if DISCORD_SUPPORT_SERVER_INVITE:
            support_button = discord.ui.Button(
                label="Join our support server", style=discord.ButtonStyle.link,
                emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
            )
            manual_button = discord.ui.Button(
                label="Read bot manual", style=discord.ButtonStyle.link,
                emoji="📖", url=f"{DASHBOARD_BASE_URL}/manual",
            )
            bottom_children = [manual_button, *bottom_children, support_button]
        container.add_item(discord.ui.ActionRow(*bottom_children))

        self.add_item(container)


def build_join_dm_view(guild_id: int, clone_id=None, feature_keys=None, page: int = 0,
                        intro: str = "", title: str = "🚀 Thanks for adding me!", notices=None,
                        enabled_keys=None) -> "JoinDMLayoutView":
    """Thin wrapper kept so existing call sites (bot.py, the nav/back
    button callbacks below) don't all need to construct JoinDMLayoutView
    directly. Callers now send this view on its own — `await
    owner.send(view=...)` — with no separate `embed=` argument, since the
    title/intro/features/notices/footer all live inside the view's
    Container now."""
    return JoinDMLayoutView(
        guild_id, clone_id, feature_keys, page, intro, title=title,
        notices=notices, enabled_keys=enabled_keys,
    )


class _RebuiltCopyView(discord.ui.LayoutView):
    """Rebuilds the message that was just clicked, walking the raw
    Components V2 tree (Container > Section/ActionRow/TextDisplay >
    Button, recursively) — needed because the whole message is now one
    nested Container rather than a flat embed + separate button rows, so
    there's no flat `view.children` list to patch after the fact the way
    the old embed-based DM allowed.

    `button_patch(custom_id, component)` is called for every join_dm_*
    button found; return a replacement discord.ui.Button (or None to drop
    it). Two callers reuse this: Remind/Dismiss disable every button,
    while a feature-toggle click only flips the one button that was
    clicked and leaves the rest exactly as shown. Link buttons (the
    support-server one) have no custom_id and are always carried over
    as-is."""

    def __init__(self, source_components, button_patch):
        super().__init__(timeout=None)
        self._button_patch = button_patch
        for component in source_components:
            rebuilt = self._rebuild(component)
            if rebuilt is not None:
                self.add_item(rebuilt)

    def _rebuild(self, component):
        if isinstance(component, discord.Container):
            container = discord.ui.Container(
                accent_colour=component.accent_colour, spoiler=component.spoiler,
            )
            for child in component.children:
                rebuilt_child = self._rebuild(child)
                if rebuilt_child is not None:
                    container.add_item(rebuilt_child)
            return container
        if isinstance(component, discord.ActionRow):
            row = discord.ui.ActionRow()
            for child in component.children:
                rebuilt_child = self._rebuild(child)
                if rebuilt_child is not None:
                    row.add_item(rebuilt_child)
            return row
        if isinstance(component, discord.SectionComponent):
            accessory = self._rebuild(component.accessory) if component.accessory else None
            if accessory is None:
                return None
            section = discord.ui.Section(accessory=accessory)
            for child in component.children:
                rebuilt_child = self._rebuild(child)
                if rebuilt_child is not None:
                    section.add_item(rebuilt_child)
            return section
        if isinstance(component, discord.TextDisplay):
            return discord.ui.TextDisplay(component.content)
        if isinstance(component, discord.SeparatorComponent):
            return discord.ui.Separator()
        if isinstance(component, discord.Button):
            if component.style == discord.ButtonStyle.link and component.url:
                return discord.ui.Button(
                    label=component.label, style=discord.ButtonStyle.link,
                    emoji=component.emoji, url=component.url,
                )
            if component.custom_id and component.custom_id.startswith("join_dm_"):
                return self._button_patch(component.custom_id, component)
            return None
        return None


def _disabled_view(interaction: discord.Interaction) -> discord.ui.LayoutView:
    def _disable_all(custom_id, component):
        return discord.ui.Button(
            label=component.label, style=component.style, emoji=component.emoji,
            custom_id=custom_id, disabled=True,
        )
    return _RebuiltCopyView(interaction.message.components, _disable_all)


class _PageNavButton(discord.ui.DynamicItem[discord.ui.Button], template=re.compile(r"^join_dm_page:(prev|next):(\d+):(-|\d+):(\d+)$").pattern):
    """Prev/Next on the main join-DM feature list. DynamicItem like every
    other button here, so paging survives bot restarts and never times
    out. Rebuilds the whole main view (not just itself) via
    build_join_dm_view — same as _WelcomeBackButton does — using the
    canonical full FEATURE_TOGGLES key order, since bot.py always shows
    every feature (nothing here depends on the specific set that was
    live when this button was first sent, so it's safe to recompute)."""

    def __init__(self, direction: str, guild_id: int, clone_id, current_page: int):
        self.direction = direction
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.current_page = current_page
        label = "← Prev" if direction == "prev" else "Next →"
        super().__init__(discord.ui.Button(
            # row=3: rows 0-2 are reserved for the (up to 3) per-feature
            # buttons on this page, so nav has to sit below all of them,
            # not on row 1 — which used to collide/interleave with feature
            # buttons once there were more than 2 on a page.
            label=label, style=discord.ButtonStyle.secondary, row=3,
            custom_id=f"join_dm_page:{direction}:{guild_id}:{'-' if clone_id is None else clone_id}:{current_page}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        direction = match.group(1)
        guild_id = int(match.group(2))
        clone_part = match.group(3)
        clone_id = None if clone_part == "-" else int(clone_part)
        current_page = int(match.group(4))
        return cls(direction, guild_id, clone_id, current_page)

    async def callback(self, interaction: discord.Interaction):
        # Defer FIRST, before either await below — Discord expires the
        # interaction token 3 seconds after it's sent, and both
        # _enabled_feature_keys and _extract_layout_intro_title_notices
        # hit the DB (and the second one can also hit Discord's API for
        # notices). Under load that pair can easily blow past 3 seconds,
        # which used to surface to the owner as "Welcome Bot didn't
        # respond in time" and a 404 Unknown interaction in the logs when
        # edit_message ran on an already-expired token. Deferring is a
        # near-instant local ack with no DB dependency, so it beats the
        # window even when the rest of this doesn't.
        await interaction.response.defer()
        target_page = self.current_page + 1 if self.direction == "next" else self.current_page - 1
        all_feature_keys = list(FEATURE_TOGGLES.keys())
        enabled = await _enabled_feature_keys(self.guild_id, self.clone_id)
        guild = interaction.client.get_guild(self.guild_id)
        intro, title, notices = await _extract_layout_intro_title_notices(interaction, guild, self.guild_id, self.clone_id)
        new_view = build_join_dm_view(
            self.guild_id, clone_id=self.clone_id, feature_keys=all_feature_keys, page=target_page,
            intro=intro, title=title, notices=notices, enabled_keys=enabled,
        )
        # One edit with the whole rebuilt layout — the embed and buttons
        # can no longer be two separate calls with two separate sources
        # of truth (that mismatch is exactly what let a page's fields and
        # buttons drift apart before). Since the response was already
        # deferred above, this has to go through edit_original_response
        # rather than response.edit_message (the initial response slot is
        # already used).
        await interaction.edit_original_response(view=new_view)


class _RemindLaterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^join_dm_remind:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(
            discord.ui.Button(
                # row=4: always the last row, below any per-feature rows
                # (0-2) and Prev/Next (3) — pinned explicitly rather than
                # left to auto-placement so it can't drift onto row 3 and
                # collide with the nav buttons on a short (unpaginated) page.
                label="Remind me later", style=discord.ButtonStyle.secondary,
                emoji="⏰", custom_id=_encode("remind", guild_id, clone_id), row=4,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.set_join_dm_remind_later(self.guild_id, clone_id=self.clone_id, hours=24)
        await interaction.edit_original_response(view=_disabled_view(interaction))
        await interaction.followup.send("Got it — I'll send this again in a day.", ephemeral=True)


class _DontAskAgainButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^join_dm_dismiss:(\d+):(-|\d+)$"):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(
            discord.ui.Button(
                # row=4: sits next to Remind me later, same reasoning as above.
                label="Don't ask again", style=discord.ButtonStyle.danger,
                emoji="🚫", custom_id=_encode("dismiss", guild_id, clone_id), row=4,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.set_join_dm_dismissed(self.guild_id, clone_id=self.clone_id)
        await interaction.edit_original_response(view=_disabled_view(interaction))
        await interaction.followup.send("Understood — I won't send this again.", ephemeral=True)


def _default_text_channel(guild: discord.Guild):
    """Best-effort channel to default a feature into when it needs one and
    the owner hasn't picked one: system channel first, else the first text
    channel the bot can actually post in."""
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
    return None


class _AutomodOptionsView(discord.ui.View):
    """Short-lived ephemeral follow-up shown right after "Turn on:
    Auto-moderation" — lets the owner pick the violation action and
    mass-mention threshold without typing `/automod action` /
    `/automod mentionthreshold`. Ephemeral and only useful in the few
    seconds after the tap, so a plain timeout=180 View (not a
    DynamicItem) is fine here — the toggle itself already happened and
    never expires; this is just a nicety on top of it."""

    def __init__(self, guild_id: int, clone_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.clone_id = clone_id

    @discord.ui.select(
        placeholder="Violation action", options=[
            discord.SelectOption(label="Warn", value="warn"),
            discord.SelectOption(label="Timeout 10 min", value="timeout"),
            discord.SelectOption(label="Delete only", value="delete"),
        ],
    )
    async def action_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)
        fields = {"action": select.values[0]}
        if select.values[0] == "timeout":
            fields["timeout_minutes"] = 10
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, **fields)
        await interaction.followup.send(f"Violations now trigger: **{select.values[0]}**.", ephemeral=True)

    @discord.ui.select(
        placeholder="Mass-mention threshold", options=[
            discord.SelectOption(label=f"{n} mentions", value=str(n)) for n in (3, 5, 8, 12)
        ],
    )
    async def threshold_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, anti_mention_threshold=int(select.values[0]))
        await interaction.followup.send(f"Mass-mention threshold set to {select.values[0]}.", ephemeral=True)


class _LevelingOptionsView(discord.ui.View):
    """Same idea as _AutomodOptionsView, for the XP-per-minute rate."""

    def __init__(self, guild_id: int, clone_id):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.clone_id = clone_id

    @discord.ui.select(
        placeholder="XP per minute (voice + text)", options=[
            discord.SelectOption(label="Slow (5 xp/min)", value="5"),
            discord.SelectOption(label="Default (10 xp/min)", value="10"),
            discord.SelectOption(label="Fast (20 xp/min)", value="20"),
        ],
    )
    async def rate_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)
        await db.set_voice_xp_config(self.guild_id, clone_id=self.clone_id, xp_per_minute=int(select.values[0]))
        await interaction.followup.send(f"XP rate set to {select.values[0]}/min.", ephemeral=True)


class _WelcomeBackButton(discord.ui.DynamicItem[discord.ui.Button], template=re.compile(r"^join_dm_wsub_back:(\d+):(-|\d+)$").pattern):
    """Back button on the welcome sub-screen. DynamicItem (not a plain
    View button) for the same reason the main feature buttons are — the
    guild_id/clone_id ride in the custom_id, so this survives bot
    restarts and never times out, matching the rest of the wizard."""

    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1,
            custom_id=f"join_dm_wsub_back:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        clone_part = match.group(2)
        return cls(int(match.group(1)), None if clone_part == "-" else int(clone_part))

    async def callback(self, interaction: discord.Interaction):
        # Defer first — see _PageNavButton.callback for why: the two
        # awaits below can outrun Discord's 3-second interaction-token
        # window, and deferring is the near-instant ack that beats it.
        await interaction.response.defer()
        all_feature_keys = list(FEATURE_TOGGLES.keys())
        enabled = await _enabled_feature_keys(self.guild_id, self.clone_id)
        enabled.add("welcome")  # got here by just turning welcome on
        intro, title, notices = await _build_main_join_dm_parts(interaction.client, self.guild_id, self.clone_id)
        main_view = build_join_dm_view(
            self.guild_id, clone_id=self.clone_id, feature_keys=all_feature_keys,
            intro=intro, title=title, notices=notices, enabled_keys=enabled,
        )
        # attachments=[] clears the welcome-card image left on the message
        # from the preview screen — otherwise Discord keeps serving it even
        # though the new layout no longer references it. Goes through
        # edit_original_response now that the initial response is deferred.
        await interaction.edit_original_response(view=main_view, attachments=[])


class _WelcomeEditButton(discord.ui.DynamicItem[discord.ui.Button], template=re.compile(r"^join_dm_wsub_edit:(\d+):(-|\d+)$").pattern):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Edit message", style=discord.ButtonStyle.secondary, emoji="📝",
            custom_id=f"join_dm_wsub_edit:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        clone_part = match.group(2)
        return cls(int(match.group(1)), None if clone_part == "-" else int(clone_part))

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs.welcome import WelcomeNudgeEditModal
        # send_modal() must be this interaction's literal first response —
        # there's no deferring around a slow DB call the way other buttons
        # do. A short timeout here means a slow/contended pool still opens
        # the modal (with an empty default the user can retype) instead of
        # blowing the 3-second ack window and showing "didn't respond in
        # time" — see get_pool()'s comment in database.py for the deeper
        # fix (pool size) this is paired with.
        try:
            config = await asyncio.wait_for(
                db.get_welcome_config(self.guild_id, clone_id=self.clone_id), timeout=2.0
            )
            current_template = config.get("message_template") or ""
            channel_id = config.get("channel_id")
        except asyncio.TimeoutError:
            current_template = ""
            channel_id = None
        await interaction.response.send_modal(
            WelcomeNudgeEditModal(self.guild_id, channel_id, current_template)
        )


class _WelcomeChannelButton(discord.ui.DynamicItem[discord.ui.Button], template=re.compile(r"^join_dm_wsub_chan:(\d+):(-|\d+)$").pattern):
    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Change channel", style=discord.ButtonStyle.secondary, emoji="📌",
            custom_id=f"join_dm_wsub_chan:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        clone_part = match.group(2)
        return cls(int(match.group(1)), None if clone_part == "-" else int(clone_part))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Pick a channel with `/welcome setup` — this DM can't browse your server's channel list.",
            ephemeral=True,
        )


class _WelcomeDeliveryButton(discord.ui.DynamicItem[discord.ui.Button], template=re.compile(r"^join_dm_wsub_deliv:(\d+):(-|\d+)$").pattern):
    """Toggles delivery_mode between 'channel' and 'dm' right from the
    join DM's welcome sub-screen. DynamicItem for the same restart-proof/
    no-timeout reason as its siblings here — guild_id/clone_id ride in
    the custom_id instead of any in-memory state."""

    def __init__(self, guild_id: int, clone_id=None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Switch to DM delivery", style=discord.ButtonStyle.secondary, emoji="✉️",
            custom_id=f"join_dm_wsub_deliv:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        clone_part = match.group(2)
        return cls(int(match.group(1)), None if clone_part == "-" else int(clone_part))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        new_mode = "channel" if config.get("delivery_mode") == "dm" else "dm"
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, delivery_mode=new_mode)
        guild = interaction.client.get_guild(self.guild_id)
        config["delivery_mode"] = new_mode
        container, file = await _welcome_preview_container(guild, config, interaction.user)
        sub_view = build_welcome_sub_view(self.guild_id, self.clone_id, container, delivery_mode=new_mode)
        await interaction.edit_original_response(view=sub_view, attachments=[file])


def build_welcome_sub_view(guild_id: int, clone_id, welcome_container: discord.ui.Container, delivery_mode: str = "channel") -> "WelcomeSubLayoutView":
    """Wraps the already-built welcome-preview Container (see
    _welcome_preview_container) together with its action buttons into one
    LayoutView. Components V2, not the old classic View+embed: once the
    parent join-DM message is sent as Components V2 (which the main
    quickstart wizard now always is), Discord permanently forbids embeds
    on that message, even on a later edit — there is no opting back out.
    So this sub-screen has to be V2 too, or editing into it would be
    rejected by Discord outright."""
    return WelcomeSubLayoutView(guild_id, clone_id, welcome_container, delivery_mode)


class WelcomeSubLayoutView(discord.ui.LayoutView):
    def __init__(self, guild_id: int, clone_id, welcome_container: discord.ui.Container, delivery_mode: str):
        super().__init__(timeout=None)
        row = discord.ui.ActionRow(
            _WelcomeEditButton(guild_id, clone_id),
            _WelcomeChannelButton(guild_id, clone_id),
        )
        delivery_btn = _WelcomeDeliveryButton(guild_id, clone_id)
        # Label reflects the mode this button will switch TO, matching
        # the rest of the sub-screen's action-button phrasing.
        delivery_btn.item.label = "Switch to channel delivery" if delivery_mode == "dm" else "Switch to DM delivery"
        row.add_item(delivery_btn)
        row.add_item(_WelcomeBackButton(guild_id, clone_id))
        welcome_container.add_item(row)
        self.add_item(welcome_container)


async def _build_main_join_dm_parts(client, guild_id: int, clone_id) -> tuple:
    """Rebuilds the main join-DM's title/intro/notices live from the same
    sources bot.py's _send_combined_owner_join_dm used originally
    (automod's log-channel/word-filter notice, ship's onboarding blurb —
    QUICKSTART_ITEMS itself isn't needed here since the feature Sections
    are rebuilt straight from FEATURE_TOGGLES by build_join_dm_view).
    Used by Back and page-nav so both work even after a bot restart, when
    nothing about the original message object is available — only the
    guild_id/clone_id encoded in the button's own custom_id. Returns
    (intro, title, notices) for build_join_dm_view."""
    guild = client.get_guild(guild_id)
    title = "🚀 Thanks for adding me!"
    intro = f"Here's everything worth knowing about **{guild.name if guild else 'your server'}** in one message:"
    intro += "\n\n⬇️ **Media downloads** work right away, no setup — grab audio/video from a link with `/download`."
    notices = []
    automod_cog = client.get_cog("AutomodCog")
    if automod_cog and guild:
        try:
            for notice_title, body in await automod_cog.build_join_notice_fields(guild, clone_id=clone_id):
                notices.append((notice_title, body))
        except Exception:
            pass
    ship_cog = client.get_cog("ShipCog")
    if ship_cog and guild:
        try:
            field = await ship_cog.build_join_notice_field(guild)
            if field:
                notices.append((field[0], field[1]))
        except Exception:
            pass
    return intro, title, notices


async def _extract_layout_intro_title_notices(interaction, guild, guild_id: int, clone_id) -> tuple:
    """Thin alias so page-nav can share the exact same rebuild logic Back
    uses — always recomputed fresh rather than parsed back out of the
    clicked message, since the live word-filter/Ship status can have
    changed since the message was first sent."""
    return await _build_main_join_dm_parts(interaction.client, guild_id, clone_id)


async def _welcome_preview_container(guild: discord.Guild, config: dict, owner: discord.abc.User):
    """Renders the same default welcome-card image members actually see
    (modules/welcome_card.render_welcome_card — identical call to the one
    /welcome preview uses), not a text-only mockup. Uses the server
    owner's own avatar as the stand-in member (there's no real new
    member to render yet) since they're the one viewing this DM. Returns
    (Container, File) — Components V2's TextDisplay + MediaGallery
    replace the old embed title/description + set_image, since this
    sub-screen now has to render on the same (already Components V2)
    message as the main quickstart wizard, where embeds are permanently
    disallowed. The caller must still pass the returned File via
    attachments=/files= alongside the view — referencing it from
    MediaGalleryItem doesn't upload it by itself."""
    import io
    import aiohttp
    from modules.welcome_card import render_welcome_card
    from discord_bot.cogs.welcome import _fetch_sticker_bytes

    channel = guild.get_channel(config.get("channel_id")) if config.get("channel_id") else None
    if config.get("delivery_mode") == "dm":
        destination_note = "Sending as a **direct message** to each new member."
    else:
        destination_note = f"Posting in {channel.mention if channel else 'no channel set'}."
    async with aiohttp.ClientSession() as session:
        async with session.get(str(owner.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            avatar_bytes = await resp.read()
        sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
    card_bytes, image_format = await asyncio.to_thread(
        render_welcome_card,
        avatar_bytes, owner.display_name, f"Member #{guild.member_count}",
        background_color=config.get("background_color"), accent_color=config.get("accent_color"),
        sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
        guild_name=guild.name, use_template=config.get("use_template", True),
    )
    ext = "gif" if image_format == "GIF" else "png"
    file = discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}")

    container = discord.ui.Container(accent_colour=discord.Color.green())
    container.add_item(discord.ui.TextDisplay(
        f"## ✅ Welcome messages are on\n{destination_note} Here's the default card new members will see:"
    ))
    container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=file)))
    return container, file


async def _enable_downloadhub(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Same auto-create-#downloads path as DownloadCreateChannelButton in
    _views_download_wizard.py (that file's the source of truth for the
    actual channel-creation + panel-posting logic — reused here rather
    than duplicated so the two never drift apart). Only difference: if a
    downloads channel/config already exists for this guild, this doesn't
    touch it or create a second one — just points the owner at the
    existing one, since re-running channel creation from a DM button
    (unlike the explicit /setup downloadhub wizard) isn't something an
    owner would expect to duplicate."""
    existing = await db.get_download_config(guild.id, clone_id=clone_id)
    existing_channel_id = existing.get("channel_id")
    if existing_channel_id and guild.get_channel(existing_channel_id) is not None:
        return True, f"Downloadhub is already set up in <#{existing_channel_id}>."

    from discord_bot.cogs._views_download_wizard import _post_submit_panel
    try:
        channel = await guild.create_text_channel("downloads", reason="Set up via the join-DM Downloadhub button")
    except discord.Forbidden:
        return False, "I don't have permission to create channels here — create one and try `/setup downloadhub`."
    await db.set_download_config(guild.id, clone_id=clone_id, channel_id=channel.id, channel_auto_created=True)
    await _post_submit_panel(interaction, guild.id, clone_id, channel)
    return True, f"Downloadhub is set up in {channel.mention} — members can submit music/video links or upload files there."


async def _enable_channels(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Unlike every other FEATURE_TOGGLES handler, this doesn't toggle
    anything itself — it responds directly (editing this same message into
    setup_channels.SetupSuggestView) and returns None/None so the generic
    success/rebuilt-button path in _FeatureToggleButton.callback is skipped
    entirely. That view already does this job properly (per-channel
    Create/Skip/Rename + Create All) — this used to also bulk-create every
    missing channel on a single tap AND a second, separate DM with that
    same picker view was sent right after, so the owner got asked to set
    up channels twice through two different, disconnected messages, and
    whichever one they used first left the other showing stale Create
    buttons for channels that no longer existed. Routing through one
    message removes both problems: there's only ever one live
    channel-creation UI, so nothing is left behind to go stale."""
    from discord_bot.cogs.setup_channels import scan_missing_channels, build_suggestions_layout_view
    missing = await scan_missing_channels(guild, clone_id)
    layout = build_suggestions_layout_view(guild, missing)
    # NOT interaction.response.edit_message(...): the caller
    # (_FeatureToggleButton.callback) already called
    # interaction.response.defer(...) before reaching this handler, so the
    # response slot is used up — a second interaction.response.* call here
    # raises discord.InteractionResponded. edit_original_response is the
    # correct way to edit the message after a defer (see how the "welcome"
    # sub-screen swap and the generic rebuilt-button path both do this a
    # few lines below in that same callback).
    await interaction.edit_original_response(view=layout)
    return None, None


async def _enable_tickets(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Auto-creates a dedicated #tickets channel for the support panel,
    same pattern as _enable_downloadhub's #downloads — NOT
    _default_text_channel (an arbitrary existing channel, usually
    #general or the system channel), which is what this used to post the
    panel into. A panel sitting in a random already-busy channel isn't
    what an owner expects from "set up tickets", and it's inconsistent
    with every other channel-creating one-tap button in this file. If a
    panel channel is already configured and still exists, reuses it
    rather than creating a second one, same as downloadhub's existing-
    config check."""
    existing = await db.get_ticket_config(guild.id, clone_id=clone_id)
    existing_channel_id = existing.get("panel_channel_id")
    existing_channel = guild.get_channel(existing_channel_id) if existing_channel_id else None
    if existing_channel is not None:
        return True, f"Ticket panel is already set up in {existing_channel.mention}. Set a support role anytime with `/ticket setup`."

    try:
        channel = await guild.create_text_channel("tickets", reason="Ticket panel set up via the join-DM button")
    except discord.Forbidden:
        return False, "I don't have permission to create channels here — create one and try `/ticket setup`."

    from discord_bot.cogs.ticket import TicketPanelView
    cog = interaction.client.get_cog("TicketCog")
    embed = discord.Embed(
        title="🎫 Need help?", description="Click below to open a private ticket with our support team.",
        color=discord.Color.blurple(),
    )
    panel_message = await channel.send(embed=embed, view=TicketPanelView(cog))
    await db.set_ticket_config(
        guild.id, clone_id=clone_id, panel_channel_id=channel.id, panel_message_id=panel_message.id,
    )
    return True, f"Ticket panel posted in {channel.mention}. Set a support role anytime with `/ticket setup`."


async def _enable_welcome(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/welcome setup`."
    await db.set_welcome_config(guild.id, clone_id=clone_id, enabled=True, channel_id=channel.id)
    return True, f"Welcome messages are on in {channel.mention}. Change it anytime with `/welcome setup`."


async def _enable_automod(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    await db.set_automod_config(guild.id, clone_id=clone_id, word_filter_enabled=True)
    return True, "Word filter is on with the starter list."


async def _enable_reaction_roles(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    # A panel needs at least one role to be useful, and we can't safely
    # guess which role the owner wants — so this posts an empty starter
    # panel (same as /reactionrole create with defaults) in one tap, and
    # points them at /reactionrole add for the one step that genuinely
    # needs a role picker.
    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/reactionrole create`."
    embed = discord.Embed(
        title="Get your roles", description="Tap a button below to get a role.",
        color=discord.Color.blurple(),
    )
    msg = await channel.send(embed=embed)
    return True, (
        f"Panel posted in {channel.mention}. Add roles to it with "
        f"`/reactionrole add message_id:{msg.id} role:<role> label:<text>`."
    )


async def _enable_leveling(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    channel = _default_text_channel(guild)
    await db.set_voice_xp_config(guild.id, clone_id=clone_id, enabled=True)
    if channel is not None:
        await db.set_leveling_config(guild.id, clone_id=clone_id, announce_channel_id=channel.id)
    return True, "Leveling / XP is on."


async def _enable_analytics(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    # Analytics is a read-only report, not a toggle — nothing to persist.
    return True, "Run `/serveranalytics` anytime for a snapshot — nothing to turn on here."


async def _enable_bump(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/bumpsetup`."
    await db.bump_set_guild_config(
        guild_id=guild.id, clone_id=clone_id, configured_by=interaction.user.id,
        bump_channel_id=channel.id, receives_bumps=True,
    )
    return True, f"Bump network is on in {channel.mention}. Fine-tune the listing with `/bumpsetup`."


async def _enable_starboard(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    # set_starboard_config only overrides the fields you pass — threshold
    # and emoji fall back to their existing (or default 5 / ⭐) values, so
    # a channel is the only thing this one-tap needs to supply.
    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/starboard setup`."
    await db.set_starboard_config(guild.id, clone_id=clone_id, channel_id=channel.id)
    return True, f"Starboard is on in {channel.mention} — posts hitting 5 ⭐ get pinned there. Adjust with `/starboard setup`."


async def _enable_suggestions(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    # /suggest already works with zero config — this just points approved
    # suggestions somewhere so they don't only live as reaction state on
    # the original message.
    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/suggestions setup`."
    await db.set_suggestion_config(guild.id, clone_id=clone_id, approved_log_channel_id=channel.id)
    return True, f"Suggestions are on — `/suggest` works anywhere, and approved ones now log to {channel.mention}."


async def _enable_verification(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Unlike rolesetup/leveling's wizards, this one can't just open as an
    ephemeral follow-up here: WizardView's channel/unverified-role/
    verified-role pickers are native discord.ui.ChannelSelect/RoleSelect
    components, which only resolve options against a guild — and this
    button (see _FeatureToggleButton.callback) is only ever clicked from
    a DM, where interaction.guild is None. Posting the wizard here would
    render those pickers with nothing to choose from. So instead of
    opening it in place like rolesetup does, point the admin at running
    /setupverification in the server itself, where the pickers actually
    have guild data to draw from. Returns None/None: nothing is enabled
    yet, so the "Turn on" button shouldn't flip state."""
    await interaction.followup.send(
        "Join verification needs channel and role pickers that only work inside the server "
        f"— head to **{guild.name}** and run `/setupverification` there to set it up.",
        ephemeral=True,
    )
    return None, None


async def _enable_role_setup_wizard(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Own personal button (not combined with channels/downloadhub — see
    "channels" and "downloadhub" below for those). Opens the real
    /role setup wizard (RoleSetupWizard from role_setup.py) as an
    ephemeral follow-up — the full preset-role picker + Create
    selected/Create all + panel-channel picker + Post role panel flow,
    not the bare empty-panel shortcut the standalone "reactionroles"
    button posts. Reused directly rather than copied, so any future
    change to the wizard is picked up here automatically. Doesn't touch
    the join-DM message itself — the wizard is self-contained in its own
    ephemeral message — so this returns None/None rather than True:
    nothing is actually created yet until the admin uses the wizard, so
    the "Turn on" button shouldn't flip to a misleading "On: ..." state
    here."""
    if not guild.me.guild_permissions.manage_roles:
        return False, ("I need the **Manage Roles** permission before I can set up self-roles here — "
                        "grant that, then try `/role setup`.")

    from discord_bot.cogs.role_setup import RoleSetupWizard
    wizard = RoleSetupWizard(interaction.user.id, guild=guild)
    await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)
    return None, None


# key -> (label, emoji, handler, options_view_builder | None, blurb). Handler
# returns (success: bool | None, message: str | None); None/None means it
# already responded itself. options_view_builder(guild_id, clone_id) -> View
# | None is an optional short-lived follow-up (see _AutomodOptionsView etc.)
# for features that have a couple of quick knobs worth surfacing right away.
# blurb is the one-line description shown in the embed field that this
# feature's "Turn on" button sits directly under (see JoinDMLayoutView) —
# this is the single source of truth for both, so the field and its button
# can never drift out of sync or out of order the way two separately
# maintained lists (embed fields vs. button keys) used to.
FEATURE_TOGGLES = {
    "welcome": ("Welcome messages", "👋", _enable_welcome, None,
                "Greet new members automatically in a channel of your choice."),
    "channels": ("Create suggested channels", "📁", _enable_channels, None,
                 "Create commonly-useful channels for this server in one tap."),
    "downloadhub": ("Downloadhub", "📥", _enable_downloadhub, None,
                     "Auto-creates a #downloads channel where members submit music/video links or upload files, with playback right in voice."),
    "verification": ("Join verification", "🔐", _enable_verification, None,
                      "Anti-raid gate — new members get an Unverified role until they pass a captcha or button click."),
    "reactionroles": ("Reaction roles", "🎭", _enable_reaction_roles, None,
                       "Let members self-assign roles by reacting to a message."),
    "leveling": ("Leveling / XP", "📈", _enable_leveling, _LevelingOptionsView,
                 "Reward active members with levels and roles over time."),
    "analytics": ("Server analytics", "📊", _enable_analytics, None,
                  "See member/activity stats and where to find more members."),
    "bump": ("Bump network", "📣", _enable_bump, None,
             "List your server for growth — I can even create the channel for you."),
    "tickets": ("Support tickets", "🎫", _enable_tickets, None,
                "Let members open a private ticket channel with staff."),
    "starboard": ("Starboard", "⭐", _enable_starboard, None,
                  "Pin standout messages to a channel once they hit a star threshold."),
    "rolesetup": ("Role setup", "🧩", _enable_role_setup_wizard, None,
                  "Open the full self-role wizard — bulk-create preset roles and post a member panel."),
    "suggestions": ("Suggestions", "💡", _enable_suggestions, None,
                     "Let members submit ideas for staff and members to vote on."),
    "automod": ("Auto-moderation", "🛡️", _enable_automod, _AutomodOptionsView,
                "Filter spam, invite links, and mass-mention raids."),
}

# How many feature buttons show per page. Each feature now takes its own
# row (row=idx, see build_join_dm_view) so its button stays lined up with
# that feature's embed instead of sharing a row with others. That leaves
# row 3 for Prev/Next and row 4 for Remind/Dismiss out of Discord's 5-row
# cap, so 3 features per page is the max that still fits.
FEATURES_PER_PAGE = 3


class _FeatureToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_FEATURE_ID_RE.pattern):
    """One-tap "Turn on" button for a single quickstart feature. Applies
    sane defaults immediately (or, where a feature genuinely needs more
    input than we can safely guess, opens the same setup modal a slash
    command would) — the owner never has to type a command. DynamicItem +
    timeout=None on the parent view means this button works whenever it's
    tapped, minutes or months after the join DM was sent, and survives
    bot restarts."""

    def __init__(self, feature_key: str, guild_id: int, clone_id=None, row: int = None):
        self.feature_key = feature_key
        self.guild_id = guild_id
        self.clone_id = clone_id
        label, emoji, _, _, _ = FEATURE_TOGGLES[feature_key]
        super().__init__(
            discord.ui.Button(
                label=f"Turn on: {label}", style=discord.ButtonStyle.success,
                emoji=emoji, custom_id=f"join_dm_feat:{feature_key}:{guild_id}:{'-' if clone_id is None else clone_id}",
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        feature_key = match.group(1)
        guild_id = int(match.group(2))
        clone_part = match.group(3)
        clone_id = None if clone_part == "-" else int(clone_part)
        # Preserve the row the button was actually sent on (read off the
        # live component, `item`) rather than defaulting to row 0 — this
        # runs on every click/restart reconstruction, and dropping the row
        # here would snap the button back to the top row on the next
        # rebuild, undoing the per-feature row alignment.
        return cls(feature_key, guild_id, clone_id, row=item.row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # No interaction.guild_id sanity check here on purpose: this button
        # is only ever clicked from a DM, so interaction.guild_id is always
        # None — the real guild binding is self.guild_id from the
        # custom_id, verified below by actually fetching that guild and
        # checking the clicking user's membership/permissions in it.
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None or not isinstance(interaction.user, discord.abc.User):
            await interaction.followup.send("I'm not in that server anymore.", ephemeral=True)
            return
        member = guild.get_member(interaction.user.id)
        if member is None or not (member.guild_permissions.manage_guild or member == guild.owner):
            await interaction.followup.send(
                "You need Manage Server permission in that server to turn this on.", ephemeral=True,
            )
            return

        # Handlers take guild explicitly (interaction.guild is None here —
        # this button is clicked from a DM, and Interaction.guild has no
        # setter, so it can't be patched onto the interaction).
        label, _, handler, options_view_cls, _ = FEATURE_TOGGLES[self.feature_key]
        try:
            success, message = await handler(interaction, guild, self.clone_id)
        except Exception:
            # Nothing here logged errors at all before this — a handler
            # exception (DB hiccup, unexpected guild state, a Discord API
            # error the try/excepts inside the handler itself didn't
            # anticipate) used to just vanish into discord.py's default
            # stderr traceback with no guild/feature context, and the
            # owner's tap would silently do nothing. Log it with enough
            # context to actually find the guild/feature involved, and
            # tell the owner it failed instead of leaving the click
            # looking like it did nothing.
            logger.exception(
                "join_dm feature toggle '%s' failed for guild %s (clone_id=%s)",
                self.feature_key, self.guild_id, self.clone_id,
            )
            await interaction.followup.send(
                "Something went wrong turning that on — try again in a moment, "
                "or use the matching slash command directly.", ephemeral=True,
            )
            return
        if success is None:
            return  # handler already responded itself (e.g. opened a modal)

        if self.feature_key == "welcome" and success:
            # Welcome gets a full sub-screen (live preview + edit/change-
            # channel + Back) instead of just disabling the button —
            # swap the whole message in place, same edit_message the
            # generic path below uses for everything else.
            config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
            container, file = await _welcome_preview_container(guild, config, interaction.user)
            sub_view = build_welcome_sub_view(self.guild_id, self.clone_id, container, delivery_mode=config.get("delivery_mode", "channel"))
            await interaction.edit_original_response(attachments=[file], view=sub_view)
            return

        def _patch_clicked(custom_id, component):
            is_this_one = custom_id == self.item.custom_id
            return discord.ui.Button(
                label=(f"On: {label}" if is_this_one and success else component.label),
                style=(discord.ButtonStyle.secondary if is_this_one and success else component.style),
                emoji=(None if is_this_one and success else component.emoji),
                custom_id=custom_id,
                disabled=(is_this_one and bool(success)),
            )
        rebuilt = _RebuiltCopyView(interaction.message.components, _patch_clicked)
        await interaction.edit_original_response(view=rebuilt)

        if success and options_view_cls:
            await interaction.followup.send(message, view=options_view_cls(self.guild_id, self.clone_id), ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


# Registered in discord_bot/bot.py's setup_hook via bot.add_dynamic_items(...).
DYNAMIC_ITEMS = (
    _RemindLaterButton, _DontAskAgainButton, _FeatureToggleButton, _PageNavButton,
    _WelcomeEditButton, _WelcomeChannelButton, _WelcomeBackButton, _WelcomeDeliveryButton,
)
