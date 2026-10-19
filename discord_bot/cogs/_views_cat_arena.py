# path: discord_bot/cogs/_views_cat_arena.py

"""Arena screens (fights plan, phase 3): turn Arena on, challenge a player, answer a challenge, watch the fight.
Play coins only. Nothing happens without an accept: a challenge only moves coins when the other player taps Accept.
Everything opens from the Cat menu (Arena) and edits one message in place; a challenge also reaches the other player
as a private message with Accept / Decline buttons that keep working after a restart."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from modules import cat_arena as arena
from modules import cat_battle as cb
from modules import cat_wallet

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
REPLAY_DELAY = 1.4
OOPS = "😿 Something went wrong, try again in a moment."


def _clone_id(client):
    return getattr(client, "clone_id", None)


def _box(text: str, rows=()) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=PINK)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    if rows:
        box.add_item(discord.ui.Separator())
        for r in rows:
            box.add_item(r)
    return box


def _screen(text: str, rows=()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=600)
    view.add_item(_box(text, rows))
    return view


def _btn(label, cb_, style=discord.ButtonStyle.secondary, emoji=None, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    b.callback = cb_
    return b


# ── pure text (the budget test measures these) ──────────────────────────

def rules_line() -> str:
    return (f"Wagers: {', '.join(f'{w:,}' for w in arena.LADDER)} {cat_wallet.COINS}. Both wagers go in only when the other player "
            f"accepts; the winner gets both minus a {arena.FEE_PERCENT}% fee. Nobody ever loses a cat here: the loser's cat is just "
            f"😴 tired for {arena.TIRED_HOURS}h (and 🩹 hurt for {arena.HURT_HOURS}h after a knock-out).")


def cat_line(r: arena.Ready) -> str:
    f = r.fighter
    extra = arena.effect_text(r.fx)
    return (f"**{r.cat.name}** · {r.cat.rarity} · rank {r.cat.cat_rank} · power {f.power} · "
            f"💪{f.strength} 🛡️{f.guard} 💨{f.speed} ✨{f.spirit}" + (f" · {extra}" if extra else ""))


def arena_text(st: arena.ArenaState, incoming_n: int, outgoing_n: int, recent_lines: List[str], note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## ⚔️ Arena",
              "Fight other players, only when **both** say yes. Just for fun, play coins only.", "",
              f"**Arena:** {'🟢 ON' if st.on else '⚫ OFF'}" + ("" if st.on else " · turn it on to challenge or be challenged"),
              f"🏅 Rating **{st.rating:,}** · {st.wins} won · {st.losses} lost",
              f"📅 Today: {st.fights_today}/{arena.FIGHTS_PER_DAY} fights · net {st.net_today:+,} {cat_wallet.COINS} "
              f"(you can still lose {arena.loss_room(st.net_today):,})",
              f"📨 Waiting for your answer: **{incoming_n}** · your open challenges: **{outgoing_n}**"]
    if recent_lines:
        parts += ["", "**Latest fights**"] + recent_lines
    parts += ["", f"-# {rules_line()}"]
    return "\n".join(parts)


def recent_line(f: arena.Fight, me: int) -> str:
    won = f.winner_id == me
    other = f.challenger_id if f.challenged_id == me else f.challenged_id
    net = (f.wager - f.fee) if won else -f.wager
    return f"{'🏆 Won' if won else '🫧 Lost'} against <@{other}> · {net:+,} {cat_wallet.COINS}"


def challenge_text(f: arena.Fight, theirs: arena.Ready, mine: Optional[arena.Ready], band=None, mismatch: bool = False,
                   note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## ⚔️ Challenge!", f"<@{f.challenger_id}> challenges you for **{f.wager:,}** {cat_wallet.COINS}.", "",
              f"**Their cat:** {cat_line(theirs)}"]
    if mine is not None:
        parts += [f"**Your cat:** {cat_line(mine)}"]
        if band:
            parts += [f"{band[1]} Odds for you: **{band[0]}**"]
        if mismatch:
            parts += ["⚠️ These cats are far apart in power. The weaker side loses no rating if it loses."]
    else:
        parts += ["You have no cat that is ready to fight right now."]
    parts += ["", f"Win and you get **{arena.payout_for(f.wager):,}** {cat_wallet.COINS} "
                  f"(both wagers minus {arena.FEE_PERCENT}%); lose and you lose **{f.wager:,}**. You never lose your cat."]
    if f.expires_at:
        parts += [f"⏳ Answer <t:{int(f.expires_at.timestamp())}:R>, or it goes away."]
    return "\n".join(parts)


def replay_text(a: cb.Fighter, b: cb.Fighter, result: cb.BattleResult, upto: int, wager: int) -> str:
    shown = result.rounds[:upto]
    head = [f"## ⚔️ {a.name} vs {b.name}", f"-# {wager:,} {cat_wallet.COINS} each · HP {a.hp} vs {b.hp}", ""]
    return "\n".join(head + [line for r in shown[-2:] for line in r])


def result_text(res: arena.AcceptResult, viewer_id: int) -> str:
    f, r = res.fight, res.result
    won = res.winner_id == viewer_id
    mine = res.a if viewer_id == f.challenger_id else res.b
    foe = res.b if viewer_id == f.challenger_id else res.a
    how = "A knock-out!" if r.ended_by == "ko" else f"After {len(r.rounds)} rounds, the most HP left wins."
    verdict = f"🏵️ **{mine.name} wins!**" if won else f"🫧 **{foe.name} wins this one.**"
    net = (res.payout - res.wager) if won else -res.wager
    new_rating, change = res.rating.get(viewer_id, (0, 0))
    lines = [f"## ⚔️ {res.a.name} vs {res.b.name}", verdict, how, "", r.rounds[-1][-1], "",
             f"{cat_wallet.COINS} **{net:+,}** coins" + (f" (fee {res.fee:,})" if won else "")
             + f" · 🏅 rating {new_rating:,} ({change:+d})"]
    if not won:
        lines.append(f"😴 **{mine.name}** is tired for {arena.TIRED_HOURS}h" + (f" and 🩹 hurt for {arena.HURT_HOURS}h." if res.hurt else "."))
    if res.mismatch:
        lines.append("-# The cats were far apart in power, so the weaker side loses no rating.")
    lines.append(f"-# Fight code `{r.seed}` · just for fun, play coins only")
    return "\n".join(lines)


# ── the Arena screen ────────────────────────────────────────────────────

class ArenaButton(discord.ui.Button):
    """The Arena entry in the Cat menu."""

    def __init__(self):
        super().__init__(label="Arena", emoji="⚔️", style=discord.ButtonStyle.primary, custom_id="cat_feature:arena")

    async def callback(self, interaction: discord.Interaction):
        try:
            view = ArenaView()
            await view.reload(interaction)
        except Exception:
            logger.exception("[cat-arena] couldn't open the arena")
            await interaction.response.send_message(OOPS, ephemeral=True)
            return
        await vis.send(interaction, view=view)


class ArenaView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=600)
        self.st = arena.ArenaState()
        self.incoming: List[arena.Fight] = []
        self.out_n = 0
        self.recent: List[str] = []
        self.note: Optional[str] = None
        self.user_id = 0
        self.clone_id = None

    async def reload(self, interaction: discord.Interaction) -> None:
        self.user_id, self.clone_id = interaction.user.id, _clone_id(interaction.client)
        self.st = await arena.get_state(self.user_id, self.clone_id)
        self.incoming = await arena.incoming(self.user_id, self.clone_id)
        self.out_n = await arena.outgoing_count(self.user_id, self.clone_id)
        self.recent = [recent_line(f, self.user_id) for f in await arena.recent(self.user_id, self.clone_id)]
        self._build()

    def _build(self) -> None:
        self.clear_items()
        on = self.st.on
        row = [_btn("Turn Arena off" if on else "Turn Arena on", self._toggle,
                    discord.ButtonStyle.secondary if on else discord.ButtonStyle.success, "🟢" if not on else "⚫"),
               _btn("Challenge a player", self._challenge, discord.ButtonStyle.primary, "⚔️", disabled=not on),
               _btn(f"Answer ({len(self.incoming)})", self._answer, discord.ButtonStyle.success, "📨",
                    disabled=not (on and self.incoming)),
               _btn("Refresh", self._refresh, discord.ButtonStyle.secondary, "🔄")]
        self.add_item(_box(arena_text(self.st, len(self.incoming), self.out_n, self.recent, self.note), [discord.ui.ActionRow(*row)]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def push(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        self.note = note
        await self.reload(interaction)
        await vis.edit(interaction, view=self)

    async def _refresh(self, interaction: discord.Interaction):
        await self.push(interaction)

    async def _toggle(self, interaction: discord.Interaction):
        try:
            await arena.set_on(interaction.user.id, _clone_id(interaction.client), not self.st.on)
        except Exception:
            logger.exception("[cat-arena] couldn't switch the arena")
            await self.push(interaction, OOPS)
            return
        await self.push(interaction, "🟢 Arena is on. You can challenge players and be challenged." if not self.st.on
                        else "⚫ Arena is off. Nobody can challenge you.")

    async def _challenge(self, interaction: discord.Interaction):
        ready = await arena.ready_cats(interaction.user.id, _clone_id(interaction.client))
        await vis.edit(interaction, view=ChallengeView(self, ready))

    async def _answer(self, interaction: discord.Interaction):
        if not self.incoming:
            await self.push(interaction, "No challenges are waiting right now.")
            return
        await open_incoming(interaction, self, self.incoming, 0)


# ── challenge someone ───────────────────────────────────────────────────

class ChallengeView(discord.ui.LayoutView):
    def __init__(self, parent: ArenaView, ready: List[arena.Ready], note: Optional[str] = None):
        super().__init__(timeout=600)
        self.parent, self.ready, self.note = parent, ready, note
        self.target: Optional[int] = None
        self.cat_id: Optional[int] = ready[0].cat.id if ready else None
        self.wager = arena.LADDER[0]
        self.sent: Optional[int] = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    def text(self) -> str:
        parts = [self.note, ""] if self.note else []
        parts += ["## ⚔️ Challenge a player", "Pick a player (they need Arena on), your cat and a wager. "
                  "They choose which of their cats to fight with and see the odds before they accept. No coins move until they accept.", ""]
        parts += [f"**Player:** {f'<@{self.target}>' if self.target else '—'}",
                  f"**Wager:** {self.wager:,} {cat_wallet.COINS}"]
        cur = next((r for r in self.ready if r.cat.id == self.cat_id), None)
        parts += [f"**Your cat:** {cat_line(cur)}" if cur else "You have no cat that is ready to fight right now (tired, for sale or out)."]
        parts += ["", f"-# {arena.EXPIRE_MINUTES} minutes to answer · {rules_line()}"]
        return "\n".join(parts)

    def _build(self) -> None:
        self.clear_items()
        rows = []
        users = discord.ui.UserSelect(placeholder="Pick a player", min_values=1, max_values=1)
        users.callback = self._pick_user
        rows.append(discord.ui.ActionRow(users))
        if self.ready:
            cats = discord.ui.Select(placeholder="Your cat", options=[
                discord.SelectOption(label=f"{r.cat.name} · power {r.fighter.power}"[:100], value=str(r.cat.id),
                                     description=f"{r.cat.rarity}, rank {r.cat.cat_rank}", default=r.cat.id == self.cat_id)
                for r in self.ready[:25]])
            cats.callback = self._pick_cat
            rows.append(discord.ui.ActionRow(cats))
        wag = discord.ui.Select(placeholder="Wager", options=[
            discord.SelectOption(label=f"{w:,} coins", value=str(w), emoji="🪙", default=w == self.wager) for w in arena.LADDER])
        wag.callback = self._pick_wager
        rows.append(discord.ui.ActionRow(wag))
        if self.sent:
            rows.append(discord.ui.ActionRow(_btn("Take it back", self._cancel, discord.ButtonStyle.danger),
                                             _btn("Back to Arena", self._back)))
        else:
            rows.append(discord.ui.ActionRow(
                _btn("Send challenge", self._send, discord.ButtonStyle.success, "⚔️", disabled=not (self.target and self.cat_id)),
                _btn("Back", self._back)))
        self.add_item(_box(self.text(), rows))

    async def _again(self, interaction: discord.Interaction, note: Optional[str] = None):
        self.note = note
        self._build()
        await vis.edit(interaction, view=self)

    async def _pick_user(self, interaction: discord.Interaction):
        user = interaction.data.get("values", [None])[0]
        member = interaction.data.get("resolved", {}).get("users", {}).get(user, {}) if user else {}
        if not user or member.get("bot"):
            await self._again(interaction, "Pick a real player, not a bot.")
            return
        if int(user) == interaction.user.id:
            await self._again(interaction, arena.STATUS_TEXT["self"])
            return
        self.target = int(user)
        await self._again(interaction)

    async def _pick_cat(self, interaction: discord.Interaction):
        self.cat_id = int(interaction.data["values"][0])
        await self._again(interaction)

    async def _pick_wager(self, interaction: discord.Interaction):
        self.wager = int(interaction.data["values"][0])
        await self._again(interaction)

    async def _send(self, interaction: discord.Interaction):
        if not (self.target and self.cat_id):
            await self._again(interaction, "Pick a player and a cat first.")
            return
        try:
            res = await arena.send_challenge(interaction.user.id, _clone_id(interaction.client), self.target, self.cat_id,
                                             self.wager, getattr(interaction, "guild_id", None))
        except Exception:
            logger.exception("[cat-arena] couldn't send a challenge")
            await self._again(interaction, OOPS)
            return
        if res.status != "ok":
            await self._again(interaction, "🚫 " + arena.STATUS_TEXT.get(res.status, "That didn't work."))
            return
        self.sent = res.fight_id
        dm = await notify_challenge(interaction.client, res.fight_id)
        await self._again(interaction, f"✅ Challenge sent to <@{self.target}>. They have {arena.EXPIRE_MINUTES} minutes to answer."
                          + ("" if dm else " I couldn't message them, so they'll see it under **Answer** in their Arena."))

    async def _cancel(self, interaction: discord.Interaction):
        ok = self.sent and await arena.cancel(self.sent, interaction.user.id)
        self.sent = None
        await self._again(interaction, "Challenge taken back." if ok else "That challenge was already answered.")

    async def _back(self, interaction: discord.Interaction):
        await self.parent.push(interaction)


# ── answer a challenge ──────────────────────────────────────────────────

async def open_incoming(interaction: discord.Interaction, parent: ArenaView, fights: List[arena.Fight], index: int,
                        cat_id: Optional[int] = None, note: Optional[str] = None) -> None:
    index = max(0, min(index, len(fights) - 1))
    f = fights[index]
    ready = await arena.ready_cats(interaction.user.id, _clone_id(interaction.client))
    theirs = await arena.get_ready(f.challenger_cat)
    chosen = next((r for r in ready if r.cat.id == cat_id), None) or (ready[0] if ready else None)
    if theirs is None:
        await arena.decline(f.id, interaction.user.id)
        await parent.push(interaction, "That challenge was cancelled: the cat is gone.")
        return
    band = None
    if chosen is not None:
        band = await asyncio.to_thread(arena.odds_for_defender, theirs.fighter, chosen.fighter)
    mis = chosen is not None and arena.mismatched(theirs.fighter.power, chosen.fighter.power)
    await vis.edit(interaction, view=IncomingView(parent, fights, index, theirs, ready, chosen, band, mis, note))


class IncomingView(discord.ui.LayoutView):
    def __init__(self, parent, fights, index, theirs, ready, chosen, band, mismatch, note=None):
        super().__init__(timeout=600)
        self.parent, self.fights, self.index = parent, fights, index
        self.theirs, self.ready, self.chosen = theirs, ready, chosen
        rows = []
        if ready:
            sel = discord.ui.Select(placeholder="Fight with which cat?", options=[
                discord.SelectOption(label=f"{r.cat.name} · power {r.fighter.power}"[:100], value=str(r.cat.id),
                                     description=f"{r.cat.rarity}, rank {r.cat.cat_rank}", default=chosen is not None and r.cat.id == chosen.cat.id)
                for r in ready[:25]])
            sel.callback = self._pick
            rows.append(discord.ui.ActionRow(sel))
        buttons = [_btn("Accept", self._accept, discord.ButtonStyle.success, "✅", disabled=chosen is None),
                   _btn("Decline", self._decline, discord.ButtonStyle.danger, "✖️")]
        if len(fights) > 1:
            buttons.append(_btn(f"Next ({index + 1}/{len(fights)})", self._next))
        buttons.append(_btn("Back", self._back))
        rows.append(discord.ui.ActionRow(*buttons))
        self.add_item(_box(challenge_text(fights[index], theirs, chosen, band, mismatch, note), rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def _pick(self, interaction: discord.Interaction):
        await open_incoming(interaction, self.parent, self.fights, self.index, int(interaction.data["values"][0]))

    async def _next(self, interaction: discord.Interaction):
        await open_incoming(interaction, self.parent, self.fights, (self.index + 1) % len(self.fights))

    async def _back(self, interaction: discord.Interaction):
        await self.parent.push(interaction)

    async def _decline(self, interaction: discord.Interaction):
        ok = await arena.decline(self.fights[self.index].id, interaction.user.id)
        await self.parent.push(interaction, "Challenge declined. No penalty." if ok else "That challenge was already answered.")

    async def _accept(self, interaction: discord.Interaction):
        f = self.fights[self.index]
        cat_id = self.chosen.cat.id if self.chosen else 0

        async def done(i: discord.Interaction, res: arena.AcceptResult):
            again = _btn("Back to Arena", self._back_after)
            await vis.edit(i, view=_screen(result_text(res, i.user.id), [discord.ui.ActionRow(again)]), original=True)
        await run_accept(interaction, f.id, cat_id, done, edit_first=lambda i, v: vis.edit(i, view=v))

    async def _back_after(self, interaction: discord.Interaction):
        await self.parent.push(interaction)


# ── accept: shared by the Arena screen and the private message ──────────

async def run_accept(interaction: discord.Interaction, fight_id: int, cat_id: int, finish, edit_first) -> None:
    """Accept, then play the replay on the message the person tapped. `finish(interaction, result)` shows the end screen;
    `edit_first(interaction, view)` answers the tap. The fight is already decided when the replay starts: it is only a show."""
    try:
        res = await arena.accept(fight_id, interaction.user.id, _clone_id(interaction.client), cat_id)
    except Exception:
        logger.exception("[cat-arena] accept failed for fight %s", fight_id)
        await interaction.response.send_message(OOPS, ephemeral=True)
        return
    if res.status != "ok":
        text = arena.STATUS_TEXT.get(res.status, "That didn't work.")
        if res.status == "poor":
            w = await cat_wallet.safe_wallet(interaction.user.id, _clone_id(interaction.client))
            text += f" You have {cat_wallet.wallet_text(w)}."
        await interaction.response.send_message("🚫 " + text, ephemeral=True)
        return
    total = len(res.result.rounds)
    await edit_first(interaction, _screen(replay_text(res.a, res.b, res.result, 1, res.wager)))
    try:
        for n in range(2, total + 1):
            await asyncio.sleep(REPLAY_DELAY)
            await interaction.edit_original_response(view=_screen(replay_text(res.a, res.b, res.result, n, res.wager)))
        await asyncio.sleep(REPLAY_DELAY)
    except discord.HTTPException:
        logger.debug("[cat-arena] the replay message went away", exc_info=True)
    try:
        await finish(interaction, res)
    except discord.HTTPException:
        logger.debug("[cat-arena] couldn't show the result", exc_info=True)
    await notify_result(interaction.client, res)


# ── private messages (best effort) ──────────────────────────────────────

_ACCEPT_RE = re.compile(r"^cat_fight:accept:(\d+)$")
_DECLINE_RE = re.compile(r"^cat_fight:decline:(\d+)$")


class AcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=_ACCEPT_RE.pattern):
    """Accept on the private challenge card. Uses your strongest cat that is ready; pick a different one in Arena > Answer."""

    def __init__(self, fight_id: int):
        self.fight_id = fight_id
        super().__init__(discord.ui.Button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"cat_fight:accept:{fight_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        f = await arena.get_fight(self.fight_id)
        if f is None or f.challenged_id != interaction.user.id:
            await interaction.response.send_message("🚫 " + arena.STATUS_TEXT["not_yours"], ephemeral=True)
            return
        ready = await arena.ready_cats(interaction.user.id, f.clone_key if f.clone_key != -1 else None)
        if not ready:
            await interaction.response.send_message("🚫 " + arena.STATUS_TEXT["no_cat"], ephemeral=True)
            return

        async def done(i: discord.Interaction, res: arena.AcceptResult):
            await i.edit_original_response(view=_screen(result_text(res, i.user.id)))

        async def first(i: discord.Interaction, v):
            await i.response.edit_message(view=v)
        await run_accept(interaction, self.fight_id, ready[0].cat.id, done, first)


class DeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=_DECLINE_RE.pattern):
    def __init__(self, fight_id: int):
        self.fight_id = fight_id
        super().__init__(discord.ui.Button(label="Decline", emoji="✖️", style=discord.ButtonStyle.danger, custom_id=f"cat_fight:decline:{fight_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        ok = await arena.decline(self.fight_id, interaction.user.id)
        await interaction.response.edit_message(view=_screen("✖️ Challenge declined. No penalty." if ok else "That challenge was already answered."))


DYNAMIC_ITEMS = (AcceptButton, DeclineButton)


async def notify_challenge(client, fight_id: int) -> bool:
    """DM the challenged player a card with Accept / Decline. False when they can't be messaged."""
    try:
        f = await arena.get_fight(fight_id)
        theirs = await arena.get_ready(f.challenger_cat)
        ready = await arena.ready_cats(f.challenged_id, f.clone_key if f.clone_key != -1 else None)
        chosen = ready[0] if ready else None
        band = await asyncio.to_thread(arena.odds_for_defender, theirs.fighter, chosen.fighter) if chosen else None
        mis = bool(chosen and arena.mismatched(theirs.fighter.power, chosen.fighter.power))
        view = discord.ui.LayoutView(timeout=None)
        text = challenge_text(f, theirs, chosen, band, mis) + "\n\n-# Accept uses your strongest ready cat. To pick another, open /cat > Arena > Answer."
        view.add_item(_box(text, [discord.ui.ActionRow(AcceptButton(f.id), DeclineButton(f.id))]))
        user = client.get_user(f.challenged_id) or await client.fetch_user(f.challenged_id)
        await user.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception:
        logger.debug("[cat-arena] couldn't DM the challenge", exc_info=True)
        return False


async def notify_result(client, res: arena.AcceptResult) -> None:
    """Tell the challenger how it went (best effort; they also see it under Latest fights)."""
    try:
        uid = res.fight.challenger_id
        user = client.get_user(uid) or await client.fetch_user(uid)
        await user.send(view=_screen(result_text(res, uid)), allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.debug("[cat-arena] couldn't DM the result", exc_info=True)
