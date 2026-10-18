# path: discord_bot/listing_tokens.py

"""
Lets the owner of the *listed* bot hand it a Top.gg / DiscordThings API token
through Discord instead of a Railway variable.

Flow (driven by discord_bot/cogs/topgg_stats.py and dthings_stats.py):
  1. The listed bot has no token (no env var, none stored) -> it DMs its
     owner(s) once per 24h with a "Add token" button.
  2. The button opens a modal; the owner pastes the token.
  3. The cog test-posts with it. Only a token the listing site accepts is
     encrypted (utils.crypto.secret_manager) and saved to listing_api_tokens,
     so every later start picks it up with no variable.

The token is never logged or echoed back. Only the bot's owner(s)
(bot._owner_ids_for_alert()) can submit one.
"""

import logging

import discord

from database import db

logger = logging.getLogger(__name__)

SERVICES = {
    "topgg": {
        "label": "Top.gg",
        "cog": "TopggStatsCog",
        "where": "Top.gg → your bot → **Integrations & API** → copy the *project token* "
                 "(not the webhook secret).",
    },
    "dthings": {
        "label": "DiscordThings",
        "cog": "DthingsStatsCog",
        "where": "the dsc.sh dashboard → your bot → copy the **API token**.",
    },
}

PROMPT_COOLDOWN_HOURS = 24


def bot_app_id(bot) -> int | None:
    me = getattr(bot, "application_id", None) or getattr(getattr(bot, "user", None), "id", None)
    return int(me) if me is not None else None


async def load_token(service: str, bot_id: int) -> str:
    """Decrypted stored token, or "" when none/undecryptable. Never raises."""
    try:
        enc = await db.get_listing_token_encrypted(service, bot_id)
        if not enc:
            return ""
        from utils.crypto import secret_manager  # local import, like bot._resolve_token
        return secret_manager.decrypt(enc) or ""
    except Exception:
        logger.exception("[listing-token] could not load %s token", service)
        return ""


async def save_token(service: str, bot_id: int, token: str) -> None:
    from utils.crypto import secret_manager
    await db.set_listing_token_encrypted(service, bot_id, secret_manager.encrypt(token))


async def _is_owner(bot, user_id: int) -> bool:
    try:
        return int(user_id) in {int(i) for i in await bot._owner_ids_for_alert()}
    except Exception:
        return False


class TokenModal(discord.ui.Modal):
    def __init__(self, service: str):
        info = SERVICES[service]
        super().__init__(title=f"{info['label']} API token", custom_id=f"listing_token:modal:{service}")
        self.service = service
        self.token_input = discord.ui.TextInput(
            placeholder="Paste the token here", min_length=10, max_length=2000,
            style=discord.TextStyle.short,
        )
        self.add_item(discord.ui.Label(text=f"{info['label']} token", component=self.token_input))

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        if not await _is_owner(bot, interaction.user.id):
            await interaction.response.send_message("Only this bot's owner can set that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        label = SERVICES[self.service]["label"]
        cog = bot.get_cog(SERVICES[self.service]["cog"])
        if cog is None:
            await interaction.followup.send(f"❌ The {label} sync isn't loaded on this bot.", ephemeral=True)
            return
        token = str(self.token_input.value).strip()
        ok, detail = await cog.try_token(token)
        if not ok:
            await interaction.followup.send(
                f"❌ {label} rejected that token ({detail}). Copy it again from {SERVICES[self.service]['where']}",
                ephemeral=True,
            )
            return
        try:
            await save_token(self.service, bot_app_id(bot), token)
        except Exception:
            logger.exception("[listing-token] saving %s token failed", self.service)
            await interaction.followup.send(
                f"⚠️ {label} accepted the token and the server count is posting now, but I couldn't "
                "store it, so I'll ask again after a restart.", ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Saved. {label} accepted the token and your server count is posted. "
            "It's stored encrypted — you won't be asked again.", ephemeral=True,
        )


class TokenPromptView(discord.ui.View):
    """Persistent (timeout=None) so the button still works after a restart."""

    def __init__(self, service: str):
        super().__init__(timeout=None)
        self.service = service
        button = discord.ui.Button(
            label=f"Add {SERVICES[service]['label']} token", style=discord.ButtonStyle.primary,
            emoji="🔑", custom_id=f"listing_token:open:{service}",
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction):
        if not await _is_owner(interaction.client, interaction.user.id):
            await interaction.response.send_message("Only this bot's owner can set that.", ephemeral=True)
            return
        await interaction.response.send_modal(TokenModal(self.service))


async def prompt_owner_for_token(bot, service: str) -> bool:
    """DMs the bot's owner(s) the token request, at most once per cooldown.
    Best-effort — returns True if at least one DM went out."""
    app_id = bot_app_id(bot)
    if app_id is None:
        return False
    try:
        if not await db.claim_listing_token_prompt(service, app_id, PROMPT_COOLDOWN_HOURS):
            return False
        owner_ids = await bot._owner_ids_for_alert()
    except Exception:
        logger.exception("[listing-token] prompt bookkeeping failed for %s", service)
        return False

    info = SERVICES[service]
    embed = discord.Embed(
        title=f"🔑 {info['label']} token needed",
        description=(
            f"**{bot.user}** is the bot listed on {info['label']}, but it has no API token, so its "
            f"server count isn't being sent.\n\nGet the token from {info['where']}\n"
            "Then press the button below and paste it. It's stored encrypted and only asked once."
        ),
        color=discord.Color.blurple(),
    )
    sent = False
    for owner_id in owner_ids:
        try:
            user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            await user.send(embed=embed, view=TokenPromptView(service))
            sent = True
        except (discord.HTTPException, discord.Forbidden):
            logger.warning("[listing-token] couldn't DM owner %s for the %s token", owner_id, service)
    return sent
