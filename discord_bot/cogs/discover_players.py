"""
Discover Players — user-created interest categories with member caps,
challenge-to-chat matching, and paid cap upgrades.

Deliberately a different cog/name from discover.py (anime/movie discovery)
— that one is unrelated and predates this feature.

Not guild-only: browsing a platform-wide category or accepting a challenge
works fine from a DM, same reasoning as Phase 1's DM-support pass. Only
`/discoverplayers create` (guild-scoped variant) needs a guild present, checked
inline rather than via GuildOnlyCog.

Contact info (phone/socials) is never shown by browse/list — only revealed
to both sides of a challenge once BOTH have accepted (see
_reveal_contacts), per the "auto-reveal after mutual accept" design
decision. There is currently no per-field private/public toggle — a user
who sets a phone/social in their profile is agreeing it can be shown to a
future mutual match; flag this as a possible follow-up if finer-grained
control turns out to be needed.
"""

import logging
import re
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCOVER_CAP_TIERS
from database import db
from payments import PaystackPayment
from utils import currency as fx

logger = logging.getLogger(__name__)

paystack = PaystackPayment()

DEFAULT_CATEGORIES = ["Gamer", "Developer", "FC Mobile Player", "eFootball Player"]
MAX_CATEGORY_NAME_LENGTH = 40
MAX_BROWSE_PAGE = 10


def _next_tier(current_cap: int):
    """First tier in DISCOVER_CAP_TIERS whose cap_from matches the
    category's current cap, or None if there's no further tier configured."""
    for cap_from, cap_to, price_usd in DISCOVER_CAP_TIERS:
        if cap_from == current_cap:
            return cap_to, price_usd
    return None


async def _resolve_currency(interaction: discord.Interaction) -> str:
    """Preference order: explicit /currency set > best-effort Discord
    locale guess > USD. Never blocks a charge — always returns something
    Paystack accepts."""
    stored = await db.get_user_currency(interaction.user.id)
    if stored:
        return stored
    guessed = fx.currency_from_locale(getattr(interaction, "locale", None))
    return guessed or "USD"


def _validate_category_name(name: str) -> str | None:
    """Returns an error message, or None if the name is acceptable."""
    name = name.strip()
    if not name:
        return "Category name can't be empty."
    if len(name) > MAX_CATEGORY_NAME_LENGTH:
        return f"Category name must be {MAX_CATEGORY_NAME_LENGTH} characters or fewer."
    return None


class _ChallengeAcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^discoverplayers_challenge_accept:(\d+)$"):
    """DynamicItem-backed (not a plain View) so the DM's Accept/Decline
    buttons survive a bot restart and never time out — the challenge_id is
    encoded straight into the custom_id and parsed back out in
    from_custom_id, same pattern as _views_join_dm.py's remind/dismiss
    buttons. A plain View(timeout=86400) looked like it lived 24h, but a
    restart inside that window silently killed the buttons while they kept
    rendering as clickable — this fixes that."""

    def __init__(self, challenge_id: int):
        self.challenge_id = challenge_id
        super().__init__(discord.ui.Button(
            label="Accept", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"discoverplayers_challenge_accept:{challenge_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("DiscoverPlayers")
        if cog is None:
            await interaction.response.send_message(
                "This feature is temporarily unavailable — please try again in a moment.", ephemeral=True
            )
            return
        await cog.resolve_challenge(interaction, self.challenge_id, "accepted")


class _ChallengeDeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^discoverplayers_challenge_decline:(\d+)$"):
    def __init__(self, challenge_id: int):
        self.challenge_id = challenge_id
        super().__init__(discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"discoverplayers_challenge_decline:{challenge_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("DiscoverPlayers")
        if cog is None:
            await interaction.response.send_message(
                "This feature is temporarily unavailable — please try again in a moment.", ephemeral=True
            )
            return
        await cog.resolve_challenge(interaction, self.challenge_id, "declined")


class ChallengeView(discord.ui.View):
    """Posted as a DM to the challenged user. timeout=None + DynamicItem
    buttons (above) so this survives a bot restart instead of expiring
    in-memory — see _ChallengeAcceptButton's docstring."""

    def __init__(self, challenge_id: int):
        super().__init__(timeout=None)
        self.add_item(_ChallengeAcceptButton(challenge_id))
        self.add_item(_ChallengeDeclineButton(challenge_id))


DYNAMIC_ITEMS = (_ChallengeAcceptButton, _ChallengeDeclineButton)


class ChallengeSelectView(discord.ui.View):
    """Posted alongside the browse embed. Lets the viewer pick a member
    from a dropdown instead of retyping `/discoverplayers challenge`."""

    def __init__(self, cog: "DiscoverPlayers", category_id: int, category_name: str,
                 viewer_id: int, member_options: list[tuple[int, str]]):
        super().__init__(timeout=300)  # embed goes stale after 5 min; re-run /browse for a fresh one
        self.cog = cog
        self.category_id = category_id
        self.category_name = category_name
        self.viewer_id = viewer_id

        select = discord.ui.Select(
            placeholder="Pick a member to challenge…",
            options=[
                discord.SelectOption(label=label, value=str(uid))
                for uid, label in member_options
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "This dropdown isn't yours — run `/discoverplayers browse` to get your own.", ephemeral=True
            )
            return

        target_id = int(interaction.data["values"][0])
        target_user = self.cog.bot.get_user(target_id) or await self.cog.bot.fetch_user(target_id)
        await self.cog.send_challenge(interaction, target_user, self.category_id, self.category_name)


class DiscoverPlayers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # NOTE: named "discoverplayers", not "discover" — discord_bot/cogs/discover.py
    # (unrelated anime/movie discovery feature) already registers a
    # top-level "/discover" command, and Discord's command tree doesn't
    # allow two top-level commands with the same name.
    group = app_commands.Group(name="discoverplayers", description="Find and connect with other players/devs")

    # ── create ──────────────────────────────────────────────────────

    @group.command(name="create", description="Create a new Discover Players category")
    @app_commands.describe(
        name="Category name (e.g. 'Valorant Duo', 'Backend Devs')",
        scope="Should this category be visible from any server, or just this one?",
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="This server only", value="guild"),
        app_commands.Choice(name="Platform-wide (any server or DM)", value="platform"),
    ])
    async def create(self, interaction: discord.Interaction, name: str,
                      scope: app_commands.Choice[str]):
        await interaction.response.defer()
        if scope.value == "guild" and interaction.guild is None:
            await interaction.followup.send(
                "A server-only category needs to be created from inside a server. "
                "Use `scope: Platform-wide` if you're in a DM.", ephemeral=True
            )
            return

        error = _validate_category_name(name)
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        guild_id = interaction.guild_id if scope.value == "guild" else None
        invite_code = secrets.token_urlsafe(6)
        row = await db.create_discover_category(name.strip(), guild_id, interaction.user.id, invite_code)
        if row is None:
            await interaction.followup.send(
                f"❌ A category named **{name.strip()}** already exists in this scope. "
                f"Try `/discoverplayers join` instead.", ephemeral=True
            )
            return

        await db.join_discover_category(row["id"], interaction.user.id)
        embed = discord.Embed(
            title=f"Created {row['name']}",
            description=f"You've joined automatically. Share it with `/discoverplayers invite category:{row['name']}`.",
            color=discord.Color.from_str("#1D9E75"),
        )
        embed.set_footer(text=f"0 / {row['member_cap']} members"
                               + (" • Platform-wide category" if guild_id is None else " • This server only"))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── join ────────────────────────────────────────────────────────

    @group.command(name="join", description="Join a Discover Players category")
    @app_commands.describe(
        category="Category name (leave blank if using an invite code)",
        code="Invite code, if you have one",
    )
    async def join(self, interaction: discord.Interaction, category: str = None, code: str = None):
        await interaction.response.defer()
        if not category and not code:
            await interaction.followup.send(
                "Give me either a `category` name or an invite `code`.", ephemeral=True
            )
            return

        if code:
            cat = await db.get_discover_category_by_code(code.strip())
            if not cat:
                await interaction.followup.send("❌ That invite code isn't valid.", ephemeral=True)
                return
        else:
            cat = await db.find_discover_category(category.strip(), interaction.guild_id)
            if not cat:
                await interaction.followup.send(
                    f"❌ No category named **{category}** found here. Try `/discoverplayers browse`.",
                    ephemeral=True,
                )
                return

        if cat["guild_id"] is not None and cat["guild_id"] != interaction.guild_id:
            await interaction.followup.send(
                "❌ That category is scoped to a different server.", ephemeral=True
            )
            return

        result = await db.join_discover_category(cat["id"], interaction.user.id)
        if result == "joined":
            await interaction.followup.send(f"✅ Joined **{cat['name']}**!", ephemeral=True)
        elif result == "already_member":
            await interaction.followup.send(f"You're already in **{cat['name']}**.", ephemeral=True)
        elif result == "full":
            tier = _next_tier(cat["member_cap"])
            msg = f"❌ **{cat['name']}** is full ({cat['member_cap']}/{cat['member_cap']} members)."
            if tier and interaction.user.id == cat["created_by"]:
                msg += f"\nAs the creator, you can run `/discoverplayers upgrade category:{cat['name']}` to raise the cap."
            elif tier:
                msg += " Ask the category creator to upgrade it."
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send("❌ That category no longer exists.", ephemeral=True)

    # ── browse ──────────────────────────────────────────────────────

    @group.command(name="browse", description="List Discover Players categories, or members of one")
    @app_commands.describe(category="Leave blank to list categories; give a name to see its members")
    async def browse(self, interaction: discord.Interaction, category: str = None):
        await interaction.response.defer(ephemeral=True)

        if category is None:
            cats = await db.list_discover_categories(interaction.guild_id)
            if not cats:
                await interaction.followup.send("No categories yet — create one with `/discoverplayers create`.")
                return
            embed = discord.Embed(title="Discover Players categories", color=discord.Color.from_str("#5865F2"))
            for c in cats:
                scope = "Platform-wide" if c["guild_id"] is None else "This server"
                embed.add_field(
                    name=c["name"],
                    value=f"{c['member_count']} / {c['member_cap']} members · {scope}",
                    inline=False,
                )
            await interaction.followup.send(embed=embed)
            return

        cat = await db.find_discover_category(category.strip(), interaction.guild_id)
        if not cat:
            await interaction.followup.send(f"❌ No category named **{category}** found.")
            return

        member_ids = await db.list_discover_category_members(cat["id"], limit=MAX_BROWSE_PAGE)
        if not member_ids:
            await interaction.followup.send(f"**{cat['name']}** has no members yet.")
            return

        embed = discord.Embed(title=cat["name"], color=discord.Color.from_str("#1D9E75"))
        embed.set_footer(text=f"{cat['member_count']} / {cat['member_cap']} members"
                               + (" • Platform-wide category" if cat["guild_id"] is None else ""))

        option_pairs: list[tuple[int, str]] = []
        lines = []
        for uid in member_ids:
            if uid == interaction.user.id:
                continue
            user = self.bot.get_user(uid)
            label = str(user) if user else f"User {uid}"
            mention = user.mention if user else f"<@{uid}>"
            lines.append(mention)
            option_pairs.append((uid, label))
        embed.description = "\n".join(lines) if lines else "You're the only member so far."

        view = None
        if option_pairs:
            view = ChallengeSelectView(self, cat["id"], cat["name"], interaction.user.id, option_pairs)

        await interaction.followup.send(embed=embed, view=view)

    # ── profile ─────────────────────────────────────────────────────

    @group.command(name="profile", description="Set your Discover Players profile (only shown to mutual matches)")
    @app_commands.describe(
        phone="Phone number (optional, only revealed after a mutual challenge accept)",
        socials="Social handles/links, comma-separated (optional)",
        availability="When you're usually free, e.g. 'weekday evenings GMT' (optional)",
    )
    async def profile(self, interaction: discord.Interaction, phone: str = None,
                       socials: str = None, availability: str = None):
        await interaction.response.defer()
        social_list = None
        if socials is not None:
            social_list = [s.strip() for s in socials.split(",") if s.strip()]

        if phone is None and social_list is None and availability is None:
            existing = await db.get_discover_profile(interaction.user.id)
            if not existing:
                await interaction.followup.send(
                    "You haven't set a profile yet. Give me a phone, socials, and/or availability.",
                    ephemeral=True,
                )
                return
            embed = discord.Embed(title="Your Discover Players profile", color=discord.Color.from_str("#8C54FF"))
            embed.add_field(name="Phone", value=existing["phone"] or "—", inline=True)
            embed.add_field(
                name="Socials",
                value=", ".join(existing["socials"]) if existing["socials"] else "—",
                inline=True,
            )
            embed.add_field(name="Availability", value=existing["availability"] or "—", inline=False)
            embed.set_footer(text="Only shown to someone after you both accept a challenge.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await db.set_discover_profile(interaction.user.id, phone, social_list, availability)
        await interaction.followup.send(
            "✅ Profile updated. This is only ever shown to someone after you both accept a challenge.",
            ephemeral=True,
        )

    # ── invite ──────────────────────────────────────────────────────

    @group.command(name="invite", description="Get a shareable link to join a category")
    @app_commands.describe(category="Category name")
    async def invite(self, interaction: discord.Interaction, category: str):
        await interaction.response.defer(ephemeral=True)
        cat = await db.find_discover_category(category.strip(), interaction.guild_id)
        if not cat:
            await interaction.followup.send(f"❌ No category named **{category}** found.", ephemeral=True)
            return

        # api/discover_oauth_join.py is served by api_server.py (the
        # Railway "web" process), not the Next.js dashboard site —
        # PUBLIC_BASE_URL is that process's domain (same one vote_webhook
        # and the clone /api/bot endpoint use), NOT DASHBOARD_BASE_URL.
        from config import PUBLIC_BASE_URL
        url = f"{PUBLIC_BASE_URL}/api/discover_oauth_join?state={cat['invite_code']}"
        embed = discord.Embed(
            title=f"Invite to {cat['name']}",
            description=f"Or in Discord: `/discoverplayers join code:{cat['invite_code']}`",
            color=discord.Color.from_str("#5865F2"),
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Join link", url=url, style=discord.ButtonStyle.link))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── challenge ───────────────────────────────────────────────────

    @group.command(name="challenge", description="Challenge another member of a shared category")
    @app_commands.describe(user="Who to challenge", category="Which category you're both in")
    async def challenge(self, interaction: discord.Interaction, user: discord.User, category: str):
        await interaction.response.defer(ephemeral=True)
        if user.id == interaction.user.id:
            await interaction.followup.send("You can't challenge yourself.", ephemeral=True)
            return

        cat = await db.find_discover_category(category.strip(), interaction.guild_id)
        if not cat:
            await interaction.followup.send(f"❌ No category named **{category}** found.", ephemeral=True)
            return

        await self.send_challenge(interaction, user, cat["id"], cat["name"])

    async def send_challenge(self, interaction: discord.Interaction, target_user: discord.abc.User,
                              category_id: int, category_name: str):
        """Shared by the `/discoverplayers challenge` command and the
        browse-embed dropdown — validates membership, creates the
        challenge row, and DMs the target with Accept/Decline buttons.

        Called from two places with different ack states: the `challenge`
        command already defers before calling in (so it can check
        self-challenge and category-lookup first), while
        ChallengeSelectView._on_select calls in on a fresh, unacknowledged
        interaction. Guard on is_done() rather than deferring
        unconditionally, so this doesn't double-defer/raise
        InteractionResponded on the command path."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if target_user.id == interaction.user.id:
            await interaction.followup.send("You can't challenge yourself.", ephemeral=True)
            return

        members = await db.list_discover_category_members(category_id, limit=1000)
        if interaction.user.id not in members:
            await interaction.followup.send(f"You're not in **{category_name}** yet.", ephemeral=True)
            return
        if target_user.id not in members:
            await interaction.followup.send(
                f"{target_user.mention} isn't in **{category_name}**.", ephemeral=True
            )
            return

        challenge_id = await db.create_discover_challenge(category_id, interaction.user.id, target_user.id)
        try:
            await target_user.send(
                f"🎮 **{interaction.user}** challenged you in **{category_name}**!",
                view=ChallengeView(challenge_id),
            )
            await interaction.followup.send(f"✅ Challenge sent to {target_user.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Couldn't DM {target_user.mention} — their DMs may be closed.", ephemeral=True
            )

    async def resolve_challenge(self, interaction: discord.Interaction, challenge_id: int, status: str):
        await interaction.response.defer(ephemeral=True)
        row = await db.resolve_discover_challenge(challenge_id, status)
        if row is None:
            await interaction.followup.send("This challenge was already resolved.", ephemeral=True)
            return

        if status == "declined":
            await interaction.edit_original_response(content="You declined this challenge.", view=None)
            challenger = self.bot.get_user(row["from_user_id"]) or await self.bot.fetch_user(row["from_user_id"])
            try:
                await challenger.send(f"❌ {interaction.user} declined your challenge.")
            except discord.HTTPException:
                pass
            return

        await interaction.edit_original_response(content="✅ Challenge accepted!", view=None)
        await self._reveal_contacts(row["from_user_id"], row["to_user_id"])

    async def _reveal_contacts(self, user_a: int, user_b: int):
        """Both sides just accepted — DM each their match's contact info,
        if that user has set any. Never called from anywhere except a
        resolved 'accepted' challenge."""
        for viewer_id, target_id in ((user_a, user_b), (user_b, user_a)):
            profile = await db.get_discover_contact_reveal(target_id)
            target_user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            viewer_user = self.bot.get_user(viewer_id) or await self.bot.fetch_user(viewer_id)
            if not profile or not (profile.get("phone") or profile.get("socials") or profile.get("availability")):
                text = f"You matched with {target_user}! They haven't set contact info yet — you can chat here on Discord."
            else:
                lines = [f"🎉 You matched with **{target_user}**!"]
                if profile.get("phone"):
                    lines.append(f"Phone: {profile['phone']}")
                if profile.get("socials"):
                    lines.append(f"Socials: {', '.join(profile['socials'])}")
                if profile.get("availability"):
                    lines.append(f"Available: {profile['availability']}")
                text = "\n".join(lines)
            try:
                await viewer_user.send(text)
            except discord.Forbidden:
                logger.info("Could not DM match reveal to %s — DMs closed", viewer_id)

    # ── upgrade ─────────────────────────────────────────────────────

    @group.command(name="upgrade", description="Pay to raise a category's member cap")
    @app_commands.describe(category="Category name")
    async def upgrade(self, interaction: discord.Interaction, category: str):
        await interaction.response.defer(ephemeral=True)
        cat = await db.find_discover_category(category.strip(), interaction.guild_id)
        if not cat:
            await interaction.followup.send(f"❌ No category named **{category}** found.", ephemeral=True)
            return

        members = await db.list_discover_category_members(cat["id"], limit=1000)
        if interaction.user.id not in members:
            await interaction.followup.send(
                "❌ Only members of the category can pay to raise its cap.", ephemeral=True
            )
            return

        tier = _next_tier(cat["member_cap"])
        if tier is None:
            await interaction.followup.send(
                f"**{cat['name']}** is already at the maximum configured cap ({cat['member_cap']}).",
                ephemeral=True,
            )
            return
        cap_to, price_usd = tier

        target_currency = await _resolve_currency(interaction)
        amount_minor, charge_currency = fx.usd_to_minor_units(price_usd, target_currency)

        email = f"discorduser_{interaction.user.id}@animebot.com"
        payment_result = paystack.initialize_payment(
            email,
            amount_minor,
            interaction.user.id,
            f"Discover_{cat['id']}",
            payment_type="discover_category_upgrade",
            currency=charge_currency,
        )
        if not payment_result or payment_result.get("status") != "success":
            await interaction.followup.send("❌ Couldn't start a payment right now — please try again shortly.")
            return

        reference = payment_result["reference"]
        await db.create_discover_category_payment(cat["id"], reference, interaction.user.id, cat["member_cap"], cap_to)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label=f"Pay to raise cap to {cap_to}", url=payment_result["authorization_url"],
            style=discord.ButtonStyle.link,
        ))
        display_amount = amount_minor / fx.MINOR_UNIT_MULTIPLIER.get(charge_currency, 100)
        embed = discord.Embed(
            title=f"Raise {cat['name']}'s cap",
            description=f"{cat['member_cap']} → {cap_to} members",
            color=discord.Color.from_str("#F0997B"),
        )
        embed.add_field(name="Price", value=f"{display_amount:.2f} {charge_currency} (~${price_usd})")
        if charge_currency != target_currency:
            embed.set_footer(text=f"Charging in {charge_currency} — {target_currency} isn't supported by Paystack for this bot.")
        await interaction.followup.send(embed=embed, view=view)

    # ── currency ────────────────────────────────────────────────────

    @app_commands.command(name="currency", description="Set your preferred currency for paid features")
    @app_commands.describe(currency="Your currency")
    @app_commands.choices(currency=[
        app_commands.Choice(name="Nigerian Naira (NGN)", value="NGN"),
        app_commands.Choice(name="Ghanaian Cedi (GHS)", value="GHS"),
        app_commands.Choice(name="South African Rand (ZAR)", value="ZAR"),
        app_commands.Choice(name="Kenyan Shilling (KES)", value="KES"),
        app_commands.Choice(name="US Dollar (USD)", value="USD"),
    ])
    async def set_currency(self, interaction: discord.Interaction, currency: app_commands.Choice[str]):
        await interaction.response.defer()
        await db.set_user_currency(interaction.user.id, currency.value)
        await interaction.followup.send(
            f"✅ Prices will now be shown and charged in **{currency.value}** where supported.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DiscoverPlayers(bot))
