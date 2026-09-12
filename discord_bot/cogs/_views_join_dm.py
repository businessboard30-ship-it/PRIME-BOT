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
# Reused business logic for the listing/registry-invite offer now embedded
# on the join DM's last page (see JoinDMLayoutView's join_offer handling
# below) — same functions the standalone _views_auto_listing_offer.py /
# _views_registry_invite_consent.py / _views_combined_join_offer.py use,
# so there's exactly one place each of those actions is actually performed.
from discord_bot.cogs._views_auto_listing_offer import (
    MAX_DESCRIPTION_LEN,
    MAX_LONG_DESCRIPTION_LEN,
    MAX_TAGS,
    _build_listing_from_guild,
    _parse_tags,
)
from discord_bot.cogs._views_registry_invite_consent import _create_invite_for_registry

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

    async def _check_one(fetch, field):
        # Best-effort: any single lookup failing must not sink the others,
        # since asyncio.gather would otherwise propagate the first
        # exception and cancel every other in-flight lookup.
        try:
            config = await fetch()
            return bool(config and config.get(field))
        except Exception:
            return False

    # Independent per-feature lookups used to run one-by-one, so total
    # latency was the sum of all 8 round trips (~2s+). None of these
    # depend on each other's results, so fire them all concurrently and
    # only wait as long as the slowest single lookup takes.
    results = await asyncio.gather(*(_check_one(fetch, field) for _, fetch, field in checks))
    return {key for (key, _, _), ok in zip(checks, results) if ok}


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
        guild_name: str = None, join_offer: dict = None,
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
        #
        # Only shown on the LAST page now, not every page — page 1 used to
        # spend space on these plain-text notices even though they're not
        # the first thing a brand-new owner needs; moving them to the end
        # frees that space for an extra "Turn on" button up front instead
        # (see FEATURES_PER_PAGE below).
        is_last_page = page == total_pages - 1
        if notices and is_last_page:
            container.add_item(discord.ui.Separator())
            for notice_title, notice_body in notices:
                container.add_item(discord.ui.TextDisplay(f"**{notice_title}**\n{notice_body}"))

        # The one-time "list this server?" / "OK to create a registry
        # invite?" asks — used to be their own separate DM(s) fired
        # right after this one (_views_combined_join_offer.py /
        # _views_auto_listing_offer.py / _views_registry_invite_consent.py).
        # Folded onto the last page here instead so it's one message, not
        # two "shots" landing back-to-back in the owner's inbox. Only
        # rendered when bot.py's _send_combined_owner_join_dm determined
        # (via the one-time DB claim / missing-invite check) that the
        # relevant question still needs asking.
        name = guild_name or "your server"
        if join_offer and is_last_page:
            show_listing = join_offer.get("show_listing")
            show_invite = join_offer.get("show_invite")
            if show_listing or show_invite:
                container.add_item(discord.ui.Separator())
            if show_listing:
                container.add_item(discord.ui.TextDisplay(
                    f"📋 Want me to list **{name}** on the public server directory right now? "
                    "No form to fill in — I'll pull the name, icon, member count, and a permanent "
                    "invite straight from Discord and it goes live immediately. You can add tags or "
                    "edit anything later.\n\nThis is a one-time ask — tap **Deny** and I won't bring "
                    "it up again (you can still list manually anytime with `/setup servers`)."
                ))
                container.add_item(discord.ui.ActionRow(
                    _JoinOfferListingButton("agree", guild_id, clone_id),
                    _JoinOfferListingButton("deny", guild_id, clone_id),
                ))
            if show_invite:
                container.add_item(discord.ui.TextDisplay(
                    f"One more thing about **{name}** — I keep a private admin registry of servers "
                    "I'm in (only visible to my own bot owner, never shared publicly), and it's more "
                    "useful with an invite link on file. Your server doesn't have a vanity URL or a "
                    "readable existing invite, so I'd need to create a fresh one. Only do this if "
                    "you're OK with that — otherwise just decline, everything else about the bot "
                    "works exactly the same either way."
                ))
                container.add_item(discord.ui.ActionRow(
                    _JoinOfferInviteButton("allow", guild_id, clone_id),
                    _JoinOfferInviteButton("decline", guild_id, clone_id),
                ))

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
        # "List your server" link — points at the same public directory
        # /setup servers hands owners a personal link into (see
        # discord_bot/cogs/server_listing.py). Plain link button like the
        # two below: no custom_id, so it needs no DynamicItem registration
        # and _RebuiltCopyView carries it over unchanged on every rebuild
        # (Remind/Dismiss/feature-toggle clicks) since it matches
        # component.style == link there already.
        listing_button = discord.ui.Button(
            label="List your server", style=discord.ButtonStyle.link,
            emoji="🌐", url=f"{DASHBOARD_BASE_URL}/servers",
        )
        bottom_children = [*bottom_children, listing_button]

        if DISCORD_SUPPORT_SERVER_INVITE:
            support_button = discord.ui.Button(
                label="Join our support server", style=discord.ButtonStyle.link,
                emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
            )
            manual_button = discord.ui.Button(
                label="Read bot manual", style=discord.ButtonStyle.link,
                emoji="📖", url="https://prime-bot-sigma.vercel.app/manual#moderation",
            )
            bottom_children = [manual_button, *bottom_children, support_button]
        container.add_item(discord.ui.ActionRow(*bottom_children))

        self.add_item(container)


def build_join_dm_view(guild_id: int, clone_id=None, feature_keys=None, page: int = 0,
                        intro: str = "", title: str = "🚀 Thanks for adding me!", notices=None,
                        enabled_keys=None, guild_name: str = None, join_offer: dict = None) -> "JoinDMLayoutView":
    """Thin wrapper kept so existing call sites (bot.py, the nav/back
    button callbacks below) don't all need to construct JoinDMLayoutView
    directly. Callers now send this view on its own — `await
    owner.send(view=...)` — with no separate `embed=` argument, since the
    title/intro/features/notices/footer all live inside the view's
    Container now."""
    return JoinDMLayoutView(
        guild_id, clone_id, feature_keys, page, intro, title=title,
        notices=notices, enabled_keys=enabled_keys, guild_name=guild_name, join_offer=join_offer,
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


def _offer_state_from_message(message) -> dict:
    """Reads which half(s) of the listing/registry-invite offer are still
    unanswered straight off the message's current buttons (same
    read-live-state idea as _views_combined_join_offer.py's
    _open_groups), rather than re-deriving it from the DB — so a page-nav
    click that lands on the last page shows the offer buttons again only
    if they hadn't been answered yet, and never resurrects a half the
    owner already agreed/declined."""
    show_listing = show_invite = False
    for row in getattr(message, "components", []) or []:
        for child in _iter_components(row):
            cid = getattr(child, "custom_id", None) or ""
            if cid.startswith("join_dm_offer:listing:"):
                show_listing = True
            elif cid.startswith("join_dm_offer:invite:"):
                show_invite = True
    return {"show_listing": show_listing, "show_invite": show_invite}


def _iter_components(component):
    """Flattens one Components V2 tree node into itself plus any nested
    children (Container > Section/ActionRow > Button etc.), so callers
    can scan for a custom_id anywhere in the tree without knowing its
    exact shape."""
    yield component
    for child in getattr(component, "children", []) or []:
        yield from _iter_components(child)


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
        join_offer = _offer_state_from_message(interaction.message)
        new_view = build_join_dm_view(
            self.guild_id, clone_id=self.clone_id, feature_keys=all_feature_keys, page=target_page,
            intro=intro, title=title, notices=notices, enabled_keys=enabled,
            guild_name=guild.name if guild else None, join_offer=join_offer,
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

        # Guard against a duplicate/late dispatch of this same interaction
        # (e.g. a gateway resume replaying the event, or the DB call above
        # eating enough of the 3s window that a race lets this callback run
        # twice). send_modal() must be a first response, so if something
        # already acknowledged this interaction, don't try again.
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_modal(
                WelcomeNudgeEditModal(self.guild_id, channel_id, current_template)
            )
        except discord.HTTPException as e:
            if e.code != 40060:  # already acknowledged — safe to swallow
                raise


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
    """Same trick as _enable_invites below: WizardView's ChannelSelect/
    RoleSelect and its role-creating/channel-locking button callbacks
    (verification.py) all read interaction.guild/interaction.user
    directly rather than a stored guild_id — which only fails when the
    wizard is attached to a DM message (interaction.guild is None
    there). A component interaction otherwise always carries the guild
    of whatever channel its message lives in, regardless of how that
    message got posted — so sending this same WizardView as a normal
    (non-ephemeral) message straight into the guild, instead of as the
    ephemeral followup /setupverification uses, gives every one of its
    pickers/buttons a real guild to act on. Not persistent (WizardView
    is a plain discord.ui.View with timeout=600, not a DynamicItem) and
    doesn't survive a bot restart mid-setup — same limitation
    /setupverification itself already has, nothing new introduced here."""
    from discord_bot.cogs.verification import WizardView

    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/setupverification`."

    current = await db.get_verification_config(guild.id, clone_id=clone_id)
    wizard = WizardView(interaction.user.id, current)
    try:
        await channel.send(embed=wizard.build_embed(), view=wizard)
    except (discord.Forbidden, discord.HTTPException):
        return False, "I couldn't post there — try `/setupverification` directly in the server instead."
    return True, f"Posted the join verification setup in {channel.mention} — head over and pick your options there."


async def _enable_invites(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """Same trick as _enable_verification just above — see that
    docstring for the full reasoning. build_wizard_view/
    _views_invites.py's ChannelSelect and create-channel button both
    need a live guild-bound interaction to work, but building the view itself doesn't; it's the
    same view InvitesCog.post_setup_wizard_on_join posts via a plain
    channel.send(view=...), no interaction involved. So instead of
    pointing the owner back at a slash command, this posts that exact
    wizard straight into the guild right now and tells them where to
    find it — clicking its picker/create-channel button there gives it a
    normal in-guild interaction, so those work exactly as they do when
    /invites setup posts them. invoker_id is set to whoever tapped this
    button (already permission-checked in _FeatureToggleButton.callback
    above), same invoker-gated pattern check_wizard_access uses
    everywhere else."""
    from discord_bot.cogs._views_invites import build_wizard_view, remember_wizard_message

    channel = _default_text_channel(guild)
    if channel is None:
        return False, "I couldn't find a channel I'm able to post in — create one and try `/invites setup`."

    config = await db.get_invite_tracker_config(guild.id, clone_id=clone_id)
    intro = (
        f"{interaction.user.mention} started this from the setup DM — pick a channel below "
        f"(or create one) to finish turning on the invite tracker."
    )
    view = build_wizard_view(guild.id, clone_id, interaction.user.id, config, intro=intro)
    try:
        message = await channel.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        return False, "I couldn't post there — try `/invites setup` directly in the server instead."
    await remember_wizard_message(guild.id, clone_id, interaction.user.id, message.channel.id, message.id)
    return True, f"Posted the invite tracker setup in {channel.mention} — head over and pick your channel there."


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


class _BuildBotTokenModal(discord.ui.Modal, title="Paste your bot's token"):
    """Single-field modal that takes over from clone_admin.py's
    /registerclone slash command for the button-driven wizard. A modal
    field is never visible in a channel's history the way a slash
    command's typed argument briefly can be, so this doesn't need the
    DM-only restriction /registerclone enforces for that reason — this
    whole wizard already only runs from a DM anyway (the join DM), so
    it's moot either way."""

    token = discord.ui.TextInput(
        label="Bot token (Bot tab → Reset Token)",
        placeholder="Paste the token here — only I ever see it",
        style=discord.TextStyle.short, required=True, max_length=100,
    )

    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Shared with /registerclone — see clone_admin.py's
        # register_clone_token, which both entry points call so
        # validation/payment/creation logic only ever lives in one place.
        # Wrapped in try/except now — register_clone_token used to be
        # called bare here, so any unhandled exception inside it (a DB
        # hiccup, an unexpected Discord API error neither validate_bot_
        # token nor set_default_install_params' own try/excepts caught)
        # left the deferred "thinking..." spinner stuck with no follow-up
        # ever sent, for the owner to stare at until the interaction
        # token quietly expired ~15 minutes later — exactly the "been
        # thinking 5 minutes" bug report. Logging + a followup here means
        # the owner always gets *something* back, and the traceback is
        # actually findable in the logs.
        from discord_bot.cogs.clone_admin import register_clone_token
        try:
            await register_clone_token(
                interaction, self.token.value.strip(),
                owner_id=interaction.user.id, hosting_clone_id=self.clone_id,
            )
        except Exception:
            logger.exception(
                "join_dm build-bot token registration failed for guild %s (user %s)",
                self.guild_id, interaction.user.id,
            )
            await interaction.followup.send(
                "Something went wrong registering that token — double check you copied the full "
                "token from the **Bot** tab (not the application ID or public key), then tap "
                "**Build Bot** again to retry.",
                ephemeral=True,
            )


class _BuildBotPasteButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=re.compile(r"^join_dm_buildbot_paste:(\d+):(-|\d+)$").pattern):
    """Was a plain (non-persistent) discord.ui.Button with a 5-minute
    View(timeout=300) — the reasoning at the time was that opening a
    modal has to be the very first response to a fresh interaction, so
    this button's own click always counts as "fresh" regardless. True,
    but that reasoning ignored Railway redeploys: a plain View's
    button-to-interaction routing only lives in that process' memory,
    so any restart between the instructions being sent and the owner
    actually tapping "I have my token" (which, per the owner-reported
    bug, keeps happening — restarts, or just more than 5 minutes passing)
    left the button pointing at nothing, and Discord shows that to the
    owner as the interaction simply timing out. DynamicItem fixes this
    the same way every other button in this file already does: the
    guild_id/clone_id live in the custom_id itself, so any process
    (post-restart or not) can reconstruct this button and open the modal
    — send_modal() is still the first response to whatever fresh
    interaction the tap produces, so the modal-must-be-first rule is
    still satisfied either way."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="I have my token — paste it", style=discord.ButtonStyle.success, emoji="📋",
            custom_id=f"join_dm_buildbot_paste:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_part = match.group(2)
        clone_id = None if clone_part == "-" else int(clone_part)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_BuildBotTokenModal(self.guild_id, self.clone_id))


async def _start_build_bot_wizard(interaction: discord.Interaction, guild: discord.Guild, clone_id):
    """"Build Bot" — a button-driven stand-in for /registerclone, kept as
    plain as possible on purpose (project owner wants this explained "for
    a 3-year-old"). This is only ever reached via _FeatureToggleButton,
    which already called interaction.response.defer(ephemeral=True)
    before invoking this handler — so a modal can't be opened directly on
    THIS interaction; instead this sends the instructions plus a fresh
    button, and that button's own (undeferred) click is what opens the
    modal. timeout=None now (was 300) since the button itself is a
    DynamicItem and no longer depends on this View surviving — see
    _BuildBotPasteButton. Returns None/None: nothing is enabled yet, and
    this never behaves like a toggle."""
    view = discord.ui.View(timeout=None)
    view.add_item(_BuildBotPasteButton(guild.id, clone_id))
    await interaction.followup.send(
        "**Let's get your own bot running — 4 quick steps:**\n\n"
        "1️⃣ Go to the Discord Developer Portal: https://discord.com/developers/applications\n"
        "2️⃣ Click **New Application**, give it any name.\n"
        "3️⃣ Open the **Installation** tab on the left. Under **Guild Install → Scopes**, add "
        "**bot** (it's not there by default — just `applications.commands` is). Then under "
        "**Permissions**, just add **Administrator** — simplest option, covers everything the "
        "bot needs so you don't have to hunt through the full permissions list one by one.\n"
        "4️⃣ Open the **Bot** tab, click **Reset Token** (or **Copy** if you already have one) — "
        "this copies a long code to your clipboard. That's your bot's token.\n\n"
        "Once you've copied it, tap the button below and paste it in. I never show it to anyone "
        "else, and nothing goes live until you've pasted it here.\n\n"
        "💰 Once it's running, you can start earning from it too — `/clonemonetize` lets you set "
        "your own prices for premium features and route payments to your own payment link, so you "
        "keep what your server(s) pay, not just get a free bot.",
        view=view, ephemeral=True,
    )
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
    "build_bot": ("Build Bot", "🤖", _start_build_bot_wizard, None,
                  "Run your own copy of this bot under your own name — takes about 2 minutes, no coding needed."),
    "welcome": ("Welcome messages", "👋", _enable_welcome, None,
                "Greet new members automatically in a channel of your choice."),
    "channels": ("Create suggested channels", "📁", _enable_channels, None,
                 "Create commonly-useful channels for this server in one tap."),
    "downloadhub": ("Downloadhub", "📥", _enable_downloadhub, None,
                     "Auto-creates a #downloads channel where members submit music/video links or upload files, with playback right in voice."),
    "invites": ("Invite tracker", "🔗", _enable_invites, None,
                "See who invited each new member, with a leaderboard and join announcements."),
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

# How many feature buttons show per page. Each feature is a
# discord.ui.Section (button as its accessory) inside the one shared
# Container, not a literal ActionRow — so unlike the nav row / bottom
# link row below, these are NOT subject to Discord's 5-action-row cap.
# The real ceiling here is Components V2's ~40-component-per-message
# limit and plain message length, not row count.
FEATURES_PER_PAGE = 5


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


def _drop_answered_pair(prefix: str):
    """Button-patch factory for _RebuiltCopyView: drops both buttons of
    whichever offer pair (listing or invite) was just answered, leaving
    every other join_dm_ button (the sibling pair, feature toggles, nav,
    remind/dismiss) exactly as shown."""
    def _patch(custom_id, component):
        if custom_id.startswith(prefix):
            return None
        return discord.ui.Button(
            label=component.label, style=component.style, emoji=component.emoji,
            custom_id=custom_id, disabled=component.disabled,
        )
    return _patch


class _JoinOfferListingDescriptionModal(discord.ui.Modal, title="List this server"):
    """Shown from _JoinOfferListingButton's Agree click when the guild has
    no Community-mode description to auto-fill from — same fields/shape
    as _views_auto_listing_offer.py's AutoListingDescriptionModal, kept
    as its own copy here since it finishes by patching THIS message's
    Components V2 tree (join_dm_offer buttons) rather than editing a
    plain content+view message."""

    short_description = discord.ui.TextInput(
        label=f"Short description (max {MAX_DESCRIPTION_LEN} chars)",
        placeholder="Shows on the directory card",
        max_length=MAX_DESCRIPTION_LEN, required=True,
    )
    long_description = discord.ui.TextInput(
        label="Long description (optional)", style=discord.TextStyle.paragraph,
        placeholder="Shown on the full listing page — write as much as you like",
        max_length=MAX_LONG_DESCRIPTION_LEN, required=False,
    )
    tags = discord.ui.TextInput(
        label=f"Tags, comma-separated (up to {MAX_TAGS})",
        placeholder="gaming, anime, chill", required=False,
    )

    def __init__(self, guild_id: int, clone_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.followup.send("I'm not in that server anymore, so I couldn't list it.", ephemeral=True)
            return
        try:
            from discord_bot.cogs.server_listing import _auto_generate_invite
            invite_url = await _auto_generate_invite(guild)
            await db.upsert_server_listing(
                guild_id=guild.id, clone_id=None, guild_name=guild.name,
                guild_icon_url=guild.icon.url if guild.icon else None,
                member_count=guild.member_count or 0, invite_url=invite_url or "",
                description=str(self.short_description), tags=_parse_tags(str(self.tags)),
                long_description=str(self.long_description) or None,
            )
        except Exception:
            logger.exception("join_dm listing offer modal build failed for guild %s", self.guild_id)
            await interaction.followup.send(
                "Something went wrong creating the listing — try `/setup servers` instead.", ephemeral=True,
            )
            return
        await db.set_auto_listing_offer_status(self.guild_id, "agreed", self.clone_id)
        rebuilt = _RebuiltCopyView(interaction.message.components, _drop_answered_pair("join_dm_offer:listing:"))
        await interaction.edit_original_response(view=rebuilt)
        await interaction.followup.send(
            f"✅ **{guild.name}** is live on the directory: {DASHBOARD_BASE_URL}/servers/{guild.id}", ephemeral=True,
        )


class _JoinOfferListingButton(discord.ui.DynamicItem[discord.ui.Button],
                               template=re.compile(r"^join_dm_offer:listing:(agree|deny):(\d+):(-|\d+)$").pattern):
    """Agree/Deny for the "list this server?" ask, folded onto the join
    DM's last page — see JoinDMLayoutView. Business logic shared with
    _views_auto_listing_offer.py's Agree/Deny buttons (_build_listing_
    from_guild, db.set_auto_listing_offer_status); the only real
    difference is that answering here patches this message's Components
    V2 tree in place instead of editing a plain content+view message."""

    def __init__(self, action: str, guild_id: int, clone_id=None):
        self.action = action
        self.guild_id = guild_id
        self.clone_id = clone_id
        is_agree = action == "agree"
        super().__init__(discord.ui.Button(
            label="Agree" if is_agree else "Deny",
            style=discord.ButtonStyle.success if is_agree else discord.ButtonStyle.secondary,
            emoji="✅" if is_agree else None,
            custom_id=f"join_dm_offer:listing:{action}:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        action = match.group(1)
        guild_id = int(match.group(2))
        clone_part = match.group(3)
        clone_id = None if clone_part == "-" else int(clone_part)
        return cls(action, guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        if self.action == "agree":
            guild = interaction.client.get_guild(self.guild_id)
            if guild is None:
                await interaction.response.send_message(
                    "I'm not in that server anymore, so I can't list it.", ephemeral=True,
                )
                return
            # Description check MUST happen before any response.defer()/
            # send() — discord.py requires send_modal() to be the
            # interaction's first response.
            if not guild.description:
                await interaction.response.send_modal(_JoinOfferListingDescriptionModal(self.guild_id, self.clone_id))
                return
            await interaction.response.defer(ephemeral=True)
            try:
                await _build_listing_from_guild(guild)
            except Exception:
                logger.exception("join_dm listing offer build failed for guild %s", self.guild_id)
                await interaction.followup.send(
                    "Something went wrong creating the listing — try `/setup servers` instead.", ephemeral=True,
                )
                return
            await db.set_auto_listing_offer_status(self.guild_id, "agreed", self.clone_id)
            result_msg = f"✅ **{guild.name}** is live on the directory: {DASHBOARD_BASE_URL}/servers/{guild.id}"
        else:
            await interaction.response.defer(ephemeral=True)
            await db.set_auto_listing_offer_status(self.guild_id, "declined", self.clone_id)
            result_msg = "No problem — I won't ask again. `/setup servers` works anytime."

        rebuilt = _RebuiltCopyView(interaction.message.components, _drop_answered_pair("join_dm_offer:listing:"))
        await interaction.edit_original_response(view=rebuilt)
        await interaction.followup.send(result_msg, ephemeral=True)


class _JoinOfferInviteButton(discord.ui.DynamicItem[discord.ui.Button],
                              template=re.compile(r"^join_dm_offer:invite:(allow|decline):(\d+):(-|\d+)$").pattern):
    """Allow/No-thanks for the private-registry invite-creation consent
    ask, folded onto the join DM's last page — see JoinDMLayoutView.
    Business logic shared with _views_registry_invite_consent.py's
    Allow/Decline buttons (_create_invite_for_registry,
    db.set_guild_invite_url)."""

    def __init__(self, action: str, guild_id: int, clone_id=None):
        self.action = action
        self.guild_id = guild_id
        self.clone_id = clone_id
        is_allow = action == "allow"
        super().__init__(discord.ui.Button(
            label="Allow" if is_allow else "No thanks",
            style=discord.ButtonStyle.success if is_allow else discord.ButtonStyle.secondary,
            emoji="✅" if is_allow else None,
            custom_id=f"join_dm_offer:invite:{action}:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        action = match.group(1)
        guild_id = int(match.group(2))
        clone_part = match.group(3)
        clone_id = None if clone_part == "-" else int(clone_part)
        return cls(action, guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.action == "allow":
            try:
                invite_url = await _create_invite_for_registry(interaction.client, self.guild_id)
            except Exception:
                logger.exception("join_dm invite offer failed for guild %s", self.guild_id)
                await interaction.followup.send(
                    "Something went wrong creating the invite — check the bot logs.", ephemeral=True,
                )
                return
            if not invite_url:
                result_msg = (
                    "Thanks for saying yes — but I don't have permission to create an invite in any "
                    "channel there, so I couldn't make one."
                )
            else:
                await db.set_guild_invite_url(self.guild_id, invite_url, clone_id=self.clone_id)
                result_msg = "✅ Thanks — invite link saved to the registry."
        else:
            result_msg = "No problem — nothing was created, everything else works the same."

        rebuilt = _RebuiltCopyView(interaction.message.components, _drop_answered_pair("join_dm_offer:invite:"))
        await interaction.edit_original_response(view=rebuilt)
        await interaction.followup.send(result_msg, ephemeral=True)


# Registered in discord_bot/bot.py's setup_hook via bot.add_dynamic_items(...).
DYNAMIC_ITEMS = (
    _RemindLaterButton, _DontAskAgainButton, _FeatureToggleButton, _PageNavButton,
    _WelcomeEditButton, _WelcomeChannelButton, _WelcomeBackButton, _WelcomeDeliveryButton,
    _JoinOfferListingButton, _JoinOfferInviteButton, _BuildBotPasteButton,
)
