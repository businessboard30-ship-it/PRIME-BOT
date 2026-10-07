# path: discord_bot/cogs/_views_applications.py

"""Application-form wizard (/application), the public Apply panel, the applicant modal and the
staff review buttons. Same shape as the other wizards: persistent DynamicItems keyed by id,
💎 options visible to everyone but gated on click with the Go Premium pitch.

custom_id families:  appw_<field>:<form_id>   wizard controls
                     appl_apply:<form_id>     public Apply button
                     appl_rev:<sub_id>:<a|d>  staff Accept / Decline
"""

import logging
import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access, user_can_manage_guild
from modules import applications as apps

logger = logging.getLogger(__name__)

_NOTE = "-# Free: 1 live form · 3 questions · 2 colours.  💎 Premium: 10 forms · 5 questions · 10 colours · auto-role."


def _cid(field: str, form_id: int) -> str:
    return f"appw_{field}:{form_id}"


def _pat(field: str) -> str:
    return rf"^appw_{field}:(\d+)$"


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


# ── wizard rendering ──────────────────────────────────────────────────────

def _status_lines(form: dict, premium: bool) -> list:
    qs = form["questions"]
    post, review = form["post_channel_id"], form["review_channel_id"]
    lines = [
        f"{'✅' if qs else '⬜'} **Questions** — {len(qs)}/{apps.max_questions(premium)}"
        + ("".join(f"\n-# {i + 1}. {q}" for i, q in enumerate(qs)) if qs else " *not set*"),
        f"{'✅' if post else '⬜'} **Post the form in** — {f'<#{post}>' if post else '*not set*'}",
        f"{'✅' if review else '⬜'} **Send applications to** — {f'<#{review}>' if review else '*not set*'}",
    ]
    if premium:
        role = form["accept_role_id"]
        lines.append(f"{'✅' if role else '⬜'} **💎 Role given on accept** — {f'<@&{role}>' if role else 'none (optional)'}")
    return lines


def panel_text(form: dict) -> str:
    return f"## {form['title']}\n{form['description']}"


def build_wizard_view(form: dict, premium: bool, note: str = "") -> discord.ui.LayoutView:
    fid = form["id"]
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=apps.colour_for(form["color_key"]))
    head = "### 📝 Create an application form"
    if form["status"] == "active":
        head += "  ·  🟢 live"
    container.add_item(discord.ui.TextDisplay(
        "\n".join([head, *([note] if note else []), "", "**Preview**", panel_text(form), "", *_status_lines(form, premium)])))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.ActionRow(AppDetailsButton(fid), AppQuestionsButton(fid)))
    container.add_item(discord.ui.ActionRow(AppColorSelect(fid, form["color_key"], premium)))
    container.add_item(discord.ui.ActionRow(AppPostChannelSelect(fid)))
    container.add_item(discord.ui.ActionRow(AppReviewChannelSelect(fid)))
    container.add_item(discord.ui.ActionRow(AppRoleSelect(fid, premium)))
    container.add_item(discord.ui.Separator())
    row = [AppPublishButton(fid, form["status"] == "active")]
    if not premium:
        row.append(AppGoPremiumButton(fid))
    container.add_item(discord.ui.ActionRow(*row))
    container.add_item(discord.ui.TextDisplay(_NOTE))
    view.add_item(container)
    return view


def build_panel_view(form: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=apps.colour_for(form["color_key"]))
    container.add_item(discord.ui.TextDisplay(panel_text(form)))
    container.add_item(discord.ui.ActionRow(AppApplyButton(form["id"], form["button_label"], form["button_emoji"], form["color_key"])))
    view.add_item(container)
    return view


async def _guard(interaction: discord.Interaction, form_id: int):
    form = await apps.get_form(form_id)
    if form is None:
        await _say(interaction, "This form no longer exists — run `/application` again.")
        return None
    if not await check_wizard_access(interaction, form["creator_id"], "application", "manage_guild", "Manage Server", admin_override=True):
        return None
    return form


async def _say(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _rerender(interaction: discord.Interaction, form_id: int, note: str = "") -> None:
    form = await apps.get_form(form_id)
    premium = await apps.is_premium(form["guild_id"], form.get("clone_id"))
    view = build_wizard_view(form, premium, note)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


async def _pitch(interaction: discord.Interaction, form: dict) -> None:
    from discord_bot.cogs._views_premium import send_premium_pitch
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await send_premium_pitch(interaction, form["guild_id"], form.get("clone_id"))


# ── wizard controls ───────────────────────────────────────────────────────

class _DetailsModal(discord.ui.Modal, title="Form details"):
    def __init__(self, form: dict):
        super().__init__()
        self.form_id = form["id"]
        self.f_title = discord.ui.TextInput(label="Title (emojis welcome)", default=form["title"], max_length=80)
        self.f_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                           default=form["description"], max_length=500)
        self.f_label = discord.ui.TextInput(label="Button text", default=form["button_label"], max_length=30)
        self.f_emoji = discord.ui.TextInput(label="Button emoji (optional)", default=form["button_emoji"] or "",
                                            required=False, max_length=40)
        for i in (self.f_title, self.f_desc, self.f_label, self.f_emoji):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        raw_emoji = self.f_emoji.value.strip()
        emoji = apps.clean_emoji(raw_emoji)
        note = "" if (emoji or not raw_emoji) else "⚠️ That wasn't a valid emoji, so the button has none."
        await apps.update_form(self.form_id, title=self.f_title.value.strip() or "📝 Apply here",
                               description=self.f_desc.value.strip() or "Press the button below to apply.",
                               button_label=self.f_label.value.strip() or "Apply", button_emoji=emoji)
        await _rerender(interaction, self.form_id, note)


class AppDetailsButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("details")):
    def __init__(self, form_id: int):
        self.form_id = form_id
        super().__init__(discord.ui.Button(label="Title & text", emoji="✏️", style=discord.ButtonStyle.primary,
                                           custom_id=_cid("details", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if form:
            await interaction.response.send_modal(_DetailsModal(form))


class _QuestionsModal(discord.ui.Modal, title="Questions"):
    def __init__(self, form: dict, premium: bool):
        super().__init__()
        self.form_id, self.premium = form["id"], premium
        n = apps.max_questions(premium)
        self.f_q = discord.ui.TextInput(
            label=f"One question per line (max {n})", style=discord.TextStyle.paragraph,
            default="\n".join(form["questions"]), max_length=600,
            placeholder="Why do you want this role?\nHow active are you?")
        self.add_item(self.f_q)

    async def on_submit(self, interaction: discord.Interaction):
        raw_count = len([l for l in self.f_q.value.splitlines() if l.strip()])
        qs = apps.parse_questions(self.f_q.value, self.premium)
        await apps.update_form(self.form_id, questions=qs)
        note = ""
        if raw_count > len(qs):
            note = (f"⚠️ Kept the first {len(qs)} questions."
                    + ("" if self.premium else " 💎 Premium allows 5."))
        await _rerender(interaction, self.form_id, note)


class AppQuestionsButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("questions")):
    def __init__(self, form_id: int):
        self.form_id = form_id
        super().__init__(discord.ui.Button(label="Questions", emoji="❓", style=discord.ButtonStyle.primary,
                                           custom_id=_cid("questions", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if form:
            premium = await apps.is_premium(form["guild_id"], form.get("clone_id"))
            await interaction.response.send_modal(_QuestionsModal(form, premium))


class AppColorSelect(discord.ui.DynamicItem[discord.ui.Select], template=_pat("color")):
    def __init__(self, form_id: int, current: str = "blurple", premium: bool = True):
        self.form_id = form_id
        options = [
            discord.SelectOption(label=label + ("" if (premium or not locked) else "  💎"), value=key,
                                 default=(key == current), emoji="🎨" if not locked else "💎")
            for key, label, _hex, locked in apps.COLORS
        ]
        super().__init__(discord.ui.Select(placeholder="Pick a colour (2 free · 10 with 💎 Premium)",
                                           options=options, custom_id=_cid("color", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if not form:
            return
        key = self.item.values[0]
        locked = apps.COLOR_BY_KEY.get(key, (None, None, None, True))[3]
        if locked and not await apps.is_premium(form["guild_id"], form.get("clone_id")):
            await _pitch(interaction, form)
            return
        await apps.update_form(self.form_id, color_key=key)
        await _rerender(interaction, self.form_id)


class _ChannelSelectMixin:
    FIELD = ""
    COLUMN = ""
    PLACEHOLDER = ""

    def __init__(self, form_id: int):
        self.form_id = form_id
        super().__init__(discord.ui.ChannelSelect(placeholder=self.PLACEHOLDER, channel_types=[discord.ChannelType.text],
                                                  min_values=1, max_values=1, custom_id=_cid(self.FIELD, form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if not form:
            return
        await apps.update_form(self.form_id, **{self.COLUMN: self.item.values[0].id})
        await _rerender(interaction, self.form_id)


class AppPostChannelSelect(_ChannelSelectMixin, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("postch")):
    FIELD, COLUMN, PLACEHOLDER = "postch", "post_channel_id", "📣 Channel where the form is posted"


class AppReviewChannelSelect(_ChannelSelectMixin, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("revch")):
    FIELD, COLUMN, PLACEHOLDER = "revch", "review_channel_id", "📥 Channel where applications arrive"


class AppRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_pat("role")):
    def __init__(self, form_id: int, premium: bool = True):
        self.form_id = form_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="🎭 Role given when accepted" + ("" if premium else "  💎"),
            min_values=1, max_values=1, custom_id=_cid("role", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if not form:
            return
        if not await apps.is_premium(form["guild_id"], form.get("clone_id")):
            await _pitch(interaction, form)
            return
        role = self.item.values[0]
        guild = interaction.guild or interaction.client.get_guild(form["guild_id"])
        me = guild.me if guild else None
        if me and (role.managed or role >= me.top_role):
            await _say(interaction, "I can't give that role — move my role above it (and pick a non-bot role).")
            return
        await apps.update_form(self.form_id, accept_role_id=role.id)
        await _rerender(interaction, self.form_id)


class AppGoPremiumButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("premium")):
    def __init__(self, form_id: int):
        self.form_id = form_id
        super().__init__(discord.ui.Button(label="Go Premium", emoji="💎", style=discord.ButtonStyle.secondary,
                                           custom_id=_cid("premium", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if form:
            await _pitch(interaction, form)


class AppPublishButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("publish")):
    def __init__(self, form_id: int, live: bool = False):
        self.form_id = form_id
        super().__init__(discord.ui.Button(label="Update live form" if live else "Publish form", emoji="🚀",
                                           style=discord.ButtonStyle.success, custom_id=_cid("publish", form_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await _guard(interaction, self.form_id)
        if not form:
            return
        guild = interaction.client.get_guild(form["guild_id"])
        if guild is None:
            await _say(interaction, "I'm not in that server any more.")
            return
        premium = await apps.is_premium(form["guild_id"], form.get("clone_id"))
        missing = [n for n, ok in (("questions", form["questions"]), ("the channel to post in", form["post_channel_id"]),
                                   ("the channel for applications", form["review_channel_id"])) if not ok]
        if missing:
            await _say(interaction, "Still needed: **" + "**, **".join(missing) + "**.")
            return
        if form["status"] != "active" and await apps.count_active_forms(
                form["guild_id"], form.get("clone_id"), form["id"]) >= apps.max_forms(premium):
            if premium:
                await _say(interaction, f"You already have {apps.MAX_FORMS_PREMIUM} live forms — close one first.")
            else:
                await _pitch(interaction, form)
            return
        post = guild.get_channel(form["post_channel_id"])
        review = guild.get_channel(form["review_channel_id"])
        if post is None or review is None:
            await _say(interaction, "I can't see one of those channels any more — pick them again.")
            return
        for ch in (post, review):
            perms = ch.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages):
                await _say(interaction, f"I need **View Channel** and **Send Messages** in {ch.mention}.")
                return
        await interaction.response.defer()
        view = build_panel_view(form)
        try:
            if form["panel_message_id"] and form["panel_channel_id"] == post.id:
                try:
                    msg = await post.fetch_message(form["panel_message_id"])
                    await msg.edit(view=view)
                except discord.NotFound:
                    msg = await post.send(view=view)
            else:
                msg = await post.send(view=view)
        except discord.HTTPException:
            logger.exception("application panel post failed (form %s)", form["id"])
            await interaction.followup.send("I couldn't post the form there — check my permissions and try again.", ephemeral=True)
            return
        await apps.update_form(form["id"], status="active", panel_channel_id=post.id, panel_message_id=msg.id)
        await _rerender(interaction, form["id"], f"✅ Live in {post.mention} — [jump to it]({msg.jump_url})")


# ── applicant side ────────────────────────────────────────────────────────

class _ApplyModal(discord.ui.Modal):
    def __init__(self, form: dict):
        super().__init__(title=re.sub(r"<a?:\w+:\d+>", "", form["title"]).replace("#", "").strip()[:45] or "Application")
        self.form = form
        self.inputs = []
        for q in form["questions"][:apps.MAX_QUESTIONS_PREMIUM]:
            ti = discord.ui.TextInput(label=q[:45], placeholder=q[:100] if len(q) > 45 else None,
                                      style=discord.TextStyle.paragraph, max_length=700)
            self.inputs.append((q, ti))
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        form = await apps.get_form(self.form["id"])
        if not form or form["status"] != "active":
            await interaction.response.send_message("This form isn't accepting applications right now.", ephemeral=True)
            return
        guild = interaction.client.get_guild(form["guild_id"])
        review = guild.get_channel(form["review_channel_id"]) if guild else None
        if review is None:
            await interaction.response.send_message("Applications are unavailable right now — tell the staff.", ephemeral=True)
            return
        answers = [{"q": q, "a": ti.value} for q, ti in self.inputs]
        sub = await apps.add_submission(form, interaction.user.id, answers)
        if sub is None:
            await interaction.response.send_message("You already have a pending application — wait for staff to answer it.", ephemeral=True)
            return
        try:
            msg = await review.send(view=build_review_view(form, sub, interaction.user),
                                    allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("application review post failed (form %s)", form["id"])
            await apps.delete_submission(sub["id"])  # don't leave a pending row the applicant can't retry
            await interaction.response.send_message("I couldn't deliver your application — please try again later.", ephemeral=True)
            return
        await apps.set_review_message(sub["id"], review.id, msg.id)
        await interaction.response.send_message("✅ Application sent! You'll get a DM when staff decide.", ephemeral=True)


class AppApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^appl_apply:(\d+)$"):
    def __init__(self, form_id: int, label: str = "Apply", emoji=None, color_key: str = "blurple"):
        self.form_id = form_id
        btn = discord.ui.Button(label=label, style=apps.button_style_for(color_key), custom_id=f"appl_apply:{form_id}")
        if emoji:
            try:
                btn.emoji = emoji
            except Exception:
                pass
        super().__init__(btn)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        form = await apps.get_form(self.form_id)
        if not form or form["status"] != "active" or not form["questions"]:
            await interaction.response.send_message("This form is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(_ApplyModal(form))


# ── staff review ──────────────────────────────────────────────────────────

def build_review_view(form: dict, sub: dict, user, result: str = "") -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=apps.colour_for(form["color_key"]))
    body = [f"### 📨 New application — {form['title']}", f"{user.mention} · `{sub['user_id']}`", ""]
    for qa in sub["answers"]:
        body.append(f"**{qa['q']}**\n{qa['a']}\n")
    container.add_item(discord.ui.TextDisplay("\n".join(body)[:3900]))
    if result:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(result))
    else:
        container.add_item(discord.ui.ActionRow(AppDecideButton(sub["id"], True), AppDecideButton(sub["id"], False)))
    view.add_item(container)
    return view


class AppDecideButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^appl_rev:(\d+):(a|d)$"):
    def __init__(self, sub_id: int, accept: bool):
        self.sub_id, self.accept = sub_id, accept
        super().__init__(discord.ui.Button(
            label="Accept" if accept else "Decline", emoji="✅" if accept else "❌",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"appl_rev:{sub_id}:{'a' if accept else 'd'}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match.group(1)), match.group(2) == "a")

    async def callback(self, interaction: discord.Interaction):
        sub = await apps.get_submission(self.sub_id)
        if sub is None:
            await _say(interaction, "That application no longer exists.")
            return
        guild = interaction.guild or interaction.client.get_guild(sub["guild_id"])
        if guild is None or not await user_can_manage_guild(guild, interaction.user.id):
            await _say(interaction, "You need the **Manage Server** permission to review applications.")
            return
        done = await apps.decide(self.sub_id, self.accept, interaction.user.id)
        if done is None:
            await _say(interaction, "Someone already answered this application.")
            return
        form = await apps.get_form(done["form_id"])
        applicant = guild.get_member(done["user_id"])
        if applicant is None:
            try:
                applicant = await guild.fetch_member(done["user_id"])
            except discord.HTTPException:
                applicant = None
        user = applicant or discord.Object(id=done["user_id"])
        mention = applicant.mention if applicant else f"<@{done['user_id']}>"

        role_note = ""
        if self.accept and form.get("accept_role_id") and applicant and await apps.is_premium(guild.id, form.get("clone_id")):
            role = guild.get_role(form["accept_role_id"])
            try:
                if role is None:
                    raise discord.HTTPException(response=None, message="role missing")
                await applicant.add_roles(role, reason=f"Application accepted by {interaction.user}")
                role_note = f" · gave {role.mention}"
            except Exception:
                role_note = " · ⚠️ couldn't give the role (check my role position)"

        verdict = "✅ Accepted" if self.accept else "❌ Declined"
        result = f"{verdict} by {interaction.user.mention}{role_note}"
        view = build_review_view(form, done, _Mention(mention), result=result)
        await interaction.response.edit_message(view=view)

        dm_text = (f"🎉 Your application **{form['title']}** in **{guild.name}** was accepted!" if self.accept
                   else f"Your application **{form['title']}** in **{guild.name}** was declined.")
        if applicant:
            try:
                await applicant.send(dm_text)
            except discord.HTTPException:
                pass


class _Mention:
    """Review-card helper: build_review_view only needs `.mention`."""
    def __init__(self, mention: str):
        self.mention = mention


DYNAMIC_ITEMS = (
    AppDetailsButton, AppQuestionsButton, AppColorSelect, AppPostChannelSelect, AppReviewChannelSelect,
    AppRoleSelect, AppGoPremiumButton, AppPublishButton, AppApplyButton, AppDecideButton,
)
