"""Creature detail screen for the catch Collection (Phase 3: P3-04, P3-05, evolution).

Opened from the Collection list as an edit of the same ephemeral message, so Back returns to
the list. Every callback defers first (rule B.6; the nickname button opens a modal, which is
itself the response), then re-checks the ``view`` gate, then talks to the database. The
creature id is only a hint: every write re-validates ownership in SQL (see
``modules/catch_creature.py`` and ``modules/catch_evolve.py``). The view only accepts clicks
from its owner and ignores a second tap while one is running.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from modules import catch_emoji as emoji
from modules.catch_collection import set_favorite
from modules.catch_creature import (
    NICKNAME_MAX,
    CreatureDetail,
    load_creature,
    set_nickname,
    toggle_buddy,
    toggle_lock,
)
from modules.catch_evolve import EvolutionPreview, evolve, preview
from modules.catch_game import STAT_NAMES
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_items import item_name
from modules.catch_release import release_creature
from modules.catch_theme import button_style, rarity_color, state_color

logger = logging.getLogger(__name__)

BackCallback = Callable[[discord.Interaction], Awaitable[None]]


def safe(value: str) -> str:
    """Escape Discord markdown in player-chosen text (nicknames)."""
    return discord.utils.escape_markdown(value)


def _types_line(d: CreatureDetail) -> str:
    parts = [f"{emoji.mark('element', d.element)} {d.element.title()}"]
    if d.element2:
        parts.append(f"{emoji.mark('element', d.element2)} {d.element2.title()}")
    return text("creature.types", types=" · ".join(parts), habitat=d.habitat.title())


def stat_lines(stats: dict[str, int]) -> str:
    return "\n".join(
        f"{emoji.mark('stat', name)} `{name.title():<6}` {emoji.bar(stats.get(name, 0))} **{stats.get(name, 0)}**"
        for name in STAT_NAMES
    )


def _evolution_text(pv: EvolutionPreview | None) -> str:
    if pv is None:
        return text("creature.evo_none")
    verdict = pv.eligibility
    if verdict.status == "final_form":
        return text("creature.evo_final")
    if verdict.status == "ready":
        return text("creature.evo_ready", to=pv.to_name)
    if verdict.status == "level_too_low" and verdict.needed_level:
        return text("creature.evo_level", to=pv.to_name, level=verdict.needed_level)
    if verdict.status == "needs_item" and verdict.needed_item:
        return text("creature.evo_item", to=pv.to_name, item=item_name(verdict.needed_item))
    return text("creature.evo_none")


def creature_embed(d: CreatureDetail, pv: EvolutionPreview | None = None) -> discord.Embed:
    flag_text = emoji.flags(shiny=d.shiny, special=d.special, favorite=d.favorite, locked=d.locked, buddy=d.is_buddy)
    title = f"{emoji.mark('rarity', d.rarity)} {safe(d.display_name)}" + (f" {flag_text}" if flag_text else "")
    description = _types_line(d)
    if d.nickname:
        description = f"**{safe(d.name)}** · {description}"
    embed = discord.Embed(
        title=title[:256], description=description,
        colour=rarity_color(d.rarity, shiny=d.shiny, special=d.special),
    )
    embed.add_field(name=text("creature.stats"), value=stat_lines(d.stats), inline=False)
    when = f"<t:{int(d.caught_at.timestamp())}:D>" if d.caught_at else "—"
    embed.add_field(
        name=text("creature.details"),
        value=text("creature.details_value", level=d.level, xp=d.xp, iv=d.iv_percent, when=when,
                   source=d.source.replace("_", " ").title()),
        inline=False,
    )
    embed.add_field(name=f"{emoji.mark('ui', 'evolve')} {text('creature.evolution')}", value=_evolution_text(pv), inline=False)
    embed.set_footer(text=text("creature.footer", id=d.id))
    return embed


def evolve_confirm_embed(pv: EvolutionPreview, nickname: str | None = None) -> discord.Embed:
    name = safe(nickname or pv.from_name)
    body = text("evolve.body", from_name=safe(pv.from_name), to_name=safe(pv.to_name))
    item = pv.eligibility.needed_item
    if item:
        body += "\n" + text("evolve.cost", item=item_name(item))
    embed = discord.Embed(
        title=f"{emoji.mark('ui', 'evolve')} {text('evolve.title', name=name)}"[:256],
        description=body, colour=state_color("warning"),
    )
    lines = [
        f"{emoji.mark('stat', stat)} `{stat.title():<6}` {pv.before.get(stat, 0)} {emoji.mark('ui', 'arrow')} **{pv.after.get(stat, 0)}**"
        for stat in STAT_NAMES
    ]
    embed.add_field(name=text("evolve.stats"), value="\n".join(lines), inline=False)
    return embed


def release_confirm_embed(d: CreatureDetail) -> discord.Embed:
    body = text("release.body", name=safe(d.display_name), level=d.level)
    if d.is_buddy:
        body += "\n" + text("release.buddy_warning")
    return discord.Embed(
        title=f"{emoji.mark('ui', 'release')} {text('release.title', name=safe(d.display_name))}"[:256],
        description=body, colour=state_color("warning"),
    )


def _gate_message(gate) -> str:
    return text("catch.unavailable", reason=gate.reason or "disabled")


class CreatureView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, detail: CreatureDetail,
                 pv: EvolutionPreview | None, *, back: BackCallback):
        super().__init__(timeout=600)
        self.user_id, self.clone_id = user_id, clone_id
        self.detail, self.pv, self.back = detail, pv, back
        self._busy = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        d = self.detail
        fav = discord.ui.Button(
            label=text("creature.btn_unfavorite" if d.favorite else "creature.btn_favorite"),
            emoji=emoji.mark("flag", "favorite"),
            style=button_style("toggle_on" if d.favorite else "toggle_off"), row=0,
        )
        fav.callback = self._fav
        lock = discord.ui.Button(
            label=text("creature.btn_unlock" if d.locked else "creature.btn_lock"),
            emoji=emoji.mark("flag", "locked"),
            style=button_style("toggle_on" if d.locked else "toggle_off"), row=0,
        )
        lock.callback = self._lock
        buddy = discord.ui.Button(
            label=text("creature.btn_unbuddy" if d.is_buddy else "creature.btn_buddy"),
            emoji=emoji.mark("flag", "buddy"),
            style=button_style("toggle_on" if d.is_buddy else "toggle_off"), row=0,
        )
        buddy.callback = self._buddy
        nick = discord.ui.Button(
            label=text("creature.btn_nickname"), emoji=emoji.mark("ui", "nickname"),
            style=button_style("navigation"), row=0,
        )
        nick.callback = self._nickname
        ready = self.pv is not None and self.pv.eligibility.status == "ready"
        evo = discord.ui.Button(
            label=text("creature.btn_evolve"), emoji=emoji.mark("ui", "evolve"),
            style=button_style("main" if ready else "disabled"), disabled=not ready, row=1,
        )
        evo.callback = self._evolve
        back = discord.ui.Button(
            label=text("creature.btn_back"), emoji=emoji.mark("ui", "back"),
            style=button_style("navigation"), row=1,
        )
        back.callback = self._back
        release = discord.ui.Button(
            label=text("creature.btn_release"), emoji=emoji.mark("ui", "release"),
            style=button_style("danger"), row=1,
        )
        release.callback = self._release
        for item in (fav, lock, buddy, nick, evo, release, back):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        """Reload the creature and redraw this screen; if it is gone, go back to the list."""
        detail = await load_creature(self.detail.id, self.user_id, self.clone_id)
        if detail is None:
            await interaction.followup.send(text("creature.not_found"), ephemeral=True)
            await self.back(interaction)
            return
        self.detail = detail
        self.pv = await preview(detail.id, self.user_id, self.clone_id)
        self._build()
        await interaction.edit_original_response(content=notice, embed=creature_embed(detail, self.pv), view=self)

    async def _run(self, interaction: discord.Interaction, work: Callable[[], Awaitable[None]]) -> None:
        await interaction.response.defer()
        if self._busy:
            await interaction.followup.send(text("creature.busy"), ephemeral=True)
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(_gate_message(gate), ephemeral=True)
                return
            await work()
        except Exception:
            logger.exception("Catch creature action failed user=%s creature=%s", self.user_id, self.detail.id)
            await interaction.followup.send(text("creature.error"), ephemeral=True)
        finally:
            self._busy = False

    async def _fav(self, interaction: discord.Interaction) -> None:
        async def work() -> None:
            if await set_favorite(self.detail.id, self.user_id, self.clone_id) is None:
                await interaction.followup.send(text("creature.not_found"), ephemeral=True)
                await self.back(interaction)
                return
            await self._refresh(interaction)
        await self._run(interaction, work)

    async def _lock(self, interaction: discord.Interaction) -> None:
        async def work() -> None:
            result = await toggle_lock(self.detail.id, self.user_id, self.clone_id)
            if not result.ok:
                await interaction.followup.send(text("creature.not_found"), ephemeral=True)
                await self.back(interaction)
                return
            name = safe(self.detail.display_name)
            await self._refresh(interaction, text("creature.lock_on" if result.value else "creature.lock_off", name=name))
        await self._run(interaction, work)

    async def _buddy(self, interaction: discord.Interaction) -> None:
        async def work() -> None:
            result = await toggle_buddy(self.detail.id, self.user_id, self.clone_id, guild_id=interaction.guild_id)
            if not result.ok:
                key = "creature.buddy_no_player" if result.reason == "no_player" else "creature.not_found"
                await interaction.followup.send(text(key), ephemeral=True)
                if result.reason != "no_player":
                    await self.back(interaction)
                return
            notice = (text("creature.buddy_set", name=safe(self.detail.display_name)) if result.value
                      else text("creature.buddy_cleared"))
            await self._refresh(interaction, notice)
        await self._run(interaction, work)

    async def _nickname(self, interaction: discord.Interaction) -> None:
        # A modal is itself the interaction response; it does no database work here.
        await interaction.response.send_modal(NicknameModal(self))

    async def _evolve(self, interaction: discord.Interaction) -> None:
        async def work() -> None:
            pv = await preview(self.detail.id, self.user_id, self.clone_id)
            if pv is None:
                await interaction.followup.send(text("creature.not_found"), ephemeral=True)
                await self.back(interaction)
                return
            if pv.eligibility.status != "ready":
                self.pv = pv
                self._build()
                notice = text(f"evolve.refused_{pv.eligibility.status}")
                await interaction.edit_original_response(content=notice, embed=creature_embed(self.detail, pv), view=self)
                return
            confirm = EvolveConfirmView(self.user_id, self.clone_id, self)
            await interaction.edit_original_response(
                content=None, embed=evolve_confirm_embed(pv, self.detail.nickname), view=confirm,
            )
        await self._run(interaction, work)

    async def _release(self, interaction: discord.Interaction) -> None:
        async def work() -> None:
            d = self.detail
            if d.favorite or d.locked:
                key = "release.refused_favorite" if d.favorite else "release.refused_locked"
                await interaction.followup.send(text(key, name=safe(d.display_name)), ephemeral=True)
                return
            confirm = ReleaseConfirmView(self.user_id, self.clone_id, self)
            await interaction.edit_original_response(content=None, embed=release_confirm_embed(d), view=confirm)
        await self._run(interaction, work)

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.back(interaction)


class NicknameModal(discord.ui.Modal):
    def __init__(self, view: CreatureView):
        super().__init__(title=text("creature.nick_title"), timeout=300)
        self.creature_view = view
        self.nickname = discord.ui.TextInput(
            label=text("creature.nick_label"), placeholder=text("creature.nick_placeholder", max=NICKNAME_MAX),
            default=view.detail.nickname or None, required=False, max_length=NICKNAME_MAX,
        )
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = self.creature_view
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", view.clone_id)
            if not gate.allowed:
                await interaction.followup.send(_gate_message(gate), ephemeral=True)
                return
            result = await set_nickname(view.detail.id, view.user_id, view.clone_id, str(self.nickname.value or ""))
            if not result.ok:
                key = "creature.not_found" if result.reason == "not_found" else f"creature.nick_{result.reason}"
                await interaction.followup.send(text(key, max=NICKNAME_MAX), ephemeral=True)
                if result.reason == "not_found":
                    await view.back(interaction)
                return
            notice = text("creature.nick_saved", nickname=safe(str(result.value))) if result.value else text("creature.nick_cleared")
            await view._refresh(interaction, notice)
        except Exception:
            logger.exception("Catch nickname failed user=%s creature=%s", view.user_id, view.detail.id)
            await interaction.followup.send(text("creature.error"), ephemeral=True)


class EvolveConfirmView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, creature_view: CreatureView):
        super().__init__(timeout=300)
        self.user_id, self.clone_id, self.creature_view = user_id, clone_id, creature_view
        self._busy = False
        confirm = discord.ui.Button(
            label=text("evolve.confirm"), emoji=emoji.mark("ui", "evolve"), style=button_style("confirm"),
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label=text("evolve.cancel"), style=button_style("navigation"))
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await self.creature_view._refresh(interaction)
        except Exception:
            logger.exception("Catch evolve cancel failed user=%s", self.user_id)
            await interaction.followup.send(text("creature.error"), ephemeral=True)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._busy:
            await interaction.followup.send(text("creature.busy"), ephemeral=True)
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(_gate_message(gate), ephemeral=True)
                return
            result = await evolve(
                self.creature_view.detail.id, self.user_id, self.clone_id, guild_id=interaction.guild_id,
            )
            if result.ok:
                notice = f"{emoji.mark('ui', 'evolved')} " + text("evolve.done", from_name=safe(result.from_name), to_name=safe(result.to_name))
                if result.new_dex_entry:
                    notice += "\n" + text("evolve.new_dex", to_name=safe(result.to_name))
            else:
                notice = text(f"evolve.refused_{result.reason}") if result.reason else text("evolve.cannot")
            await self.creature_view._refresh(interaction, notice)
        except Exception:
            logger.exception("Catch evolve failed user=%s creature=%s", self.user_id, self.creature_view.detail.id)
            await interaction.followup.send(text("evolve.error"), ephemeral=True)
        finally:
            self._busy = False


class ReleaseConfirmView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, creature_view: CreatureView):
        super().__init__(timeout=300)
        self.user_id, self.clone_id, self.creature_view = user_id, clone_id, creature_view
        self._busy = False
        confirm = discord.ui.Button(
            label=text("release.confirm"), emoji=emoji.mark("ui", "release"), style=button_style("danger"),
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label=text("release.cancel"), style=button_style("navigation"))
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await self.creature_view._refresh(interaction, text("release.cancelled"))
        except Exception:
            logger.exception("Catch release cancel failed user=%s", self.user_id)
            await interaction.followup.send(text("creature.error"), ephemeral=True)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._busy:
            await interaction.followup.send(text("creature.busy"), ephemeral=True)
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(_gate_message(gate), ephemeral=True)
                return
            cv = self.creature_view
            result = await release_creature(cv.detail.id, self.user_id, self.clone_id, guild_id=interaction.guild_id)
            name = safe(result.name or cv.detail.display_name)
            if result.ok:
                notice = text("release.done", name=name)
                if result.was_buddy:
                    notice += "\n" + text("release.buddy_cleared")
                await interaction.followup.send(notice, ephemeral=True)
                await cv.back(interaction)
                return
            if result.reason in ("favorite", "locked"):
                await cv._refresh(interaction, text(f"release.refused_{result.reason}", name=name))
                return
            await interaction.followup.send(text("release.gone"), ephemeral=True)
            await cv.back(interaction)
        except Exception:
            logger.exception("Catch release failed user=%s creature=%s", self.user_id, self.creature_view.detail.id)
            await interaction.followup.send(text("release.error"), ephemeral=True)
        finally:
            self._busy = False


async def open_creature_detail(
    interaction: discord.Interaction, user_id: int, clone_id: int | None, owned_id: int, *, back: BackCallback,
) -> None:
    """Replace the (already deferred) message with this creature's detail screen."""
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(_gate_message(gate), ephemeral=True)
        return
    try:
        detail = await load_creature(owned_id, user_id, clone_id)
        if detail is None:
            await interaction.followup.send(text("creature.not_found"), ephemeral=True)
            await back(interaction)
            return
        pv = await preview(owned_id, user_id, clone_id)
    except Exception:
        logger.exception("Catch creature load failed user=%s creature=%s", user_id, owned_id)
        await interaction.followup.send(text("creature.error"), ephemeral=True)
        return
    view = CreatureView(user_id, clone_id, detail, pv, back=back)
    await interaction.edit_original_response(content=None, embed=creature_embed(detail, pv), view=view)


__all__ = [
    "CreatureView", "EvolveConfirmView", "NicknameModal", "ReleaseConfirmView", "creature_embed", "evolve_confirm_embed",
    "release_confirm_embed",
    "open_creature_detail", "safe", "stat_lines",
]
