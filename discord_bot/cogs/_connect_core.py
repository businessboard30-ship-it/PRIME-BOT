# path: discord_bot/cogs/_connect_core.py

"""
Storage + embed builders for /connections (see connect.py / _views_connect.py).

Tables are created here with CREATE TABLE IF NOT EXISTS (called from the
cog's cog_load) so this feature needs no SCHEMA_VERSION bump.

  connect_links    verified YouTube / Roblox account per Discord user
  connect_pending  an in-progress link waiting for its code to be found
  connect_feeds    per-server notification feeds (YouTube uploads, Roblox
                   game updates). clone_id 0 = the main bot.
  connect_guild    per-server settings (Roblox-verified role)
"""

import json
import secrets
from datetime import datetime, timezone

import discord

from database import get_pool

_READY = False
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
YT_RED = discord.Color.from_rgb(255, 0, 0)
RBX_BLUE = discord.Color.from_rgb(0, 162, 255)


async def ensure_tables() -> None:
    global _READY
    if _READY:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS connect_links (
                user_id BIGINT NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                external_name TEXT NOT NULL,
                verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, platform)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS connect_links_ext_idx ON connect_links (platform, external_id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS connect_pending (
                user_id BIGINT NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                external_name TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, platform)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS connect_feeds (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                external_id TEXT NOT NULL,
                external_name TEXT NOT NULL,
                channel_id BIGINT NOT NULL,
                role_id BIGINT,
                state TEXT NOT NULL DEFAULT '{}',
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, clone_id, kind, external_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS connect_guild (
                guild_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                roblox_role_id BIGINT,
                PRIMARY KEY (guild_id, clone_id)
            )
        """)
    _READY = True


def clone_key(clone_id) -> int:
    return int(clone_id or 0)


def new_code() -> str:
    return "PRIME-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


# ── links ─────────────────────────────────────────────────────────────────

async def get_links(user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM connect_links WHERE user_id = $1", user_id)
    return {r["platform"]: dict(r) for r in rows}


async def get_link(user_id: int, platform: str):
    return (await get_links(user_id)).get(platform)


async def save_link(user_id: int, platform: str, external_id: str, external_name: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Proving control of the account moves it to this Discord user.
            await conn.execute(
                "DELETE FROM connect_links WHERE platform = $1 AND external_id = $2 AND user_id <> $3",
                platform, external_id, user_id,
            )
            await conn.execute("""
                INSERT INTO connect_links (user_id, platform, external_id, external_name, verified_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (user_id, platform) DO UPDATE
                SET external_id = EXCLUDED.external_id, external_name = EXCLUDED.external_name, verified_at = NOW()
            """, user_id, platform, external_id, external_name)
            await conn.execute(
                "DELETE FROM connect_pending WHERE user_id = $1 AND platform = $2", user_id, platform
            )


async def remove_link(user_id: int, platform: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM connect_links WHERE user_id = $1 AND platform = $2", user_id, platform)


async def set_pending(user_id: int, platform: str, external_id: str, external_name: str, code: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO connect_pending (user_id, platform, external_id, external_name, code, created_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (user_id, platform) DO UPDATE
            SET external_id = EXCLUDED.external_id, external_name = EXCLUDED.external_name,
                code = EXCLUDED.code, created_at = NOW()
        """, user_id, platform, external_id, external_name, code)


async def get_pending(user_id: int, platform: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM connect_pending WHERE user_id = $1 AND platform = $2 "
            "AND created_at > NOW() - INTERVAL '1 hour'", user_id, platform,
        )
    return dict(row) if row else None


# ── feeds ─────────────────────────────────────────────────────────────────

async def list_feeds(guild_id: int, clone_id) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM connect_feeds WHERE guild_id = $1 AND clone_id = $2 ORDER BY id",
            guild_id, clone_key(clone_id),
        )
    return [dict(r) for r in rows]


async def add_feed(guild_id, clone_id, kind, external_id, external_name, channel_id, role_id, state: dict, user_id):
    """Returns the new feed id, or None if that feed already exists here."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM connect_feeds WHERE guild_id = $1 AND clone_id = $2",
            guild_id, clone_key(clone_id),
        )
        if n >= MAX_FEEDS_PER_GUILD:
            raise ValueError(f"limit: {MAX_FEEDS_PER_GUILD}")
        return await conn.fetchval("""
            INSERT INTO connect_feeds
                (guild_id, clone_id, kind, external_id, external_name, channel_id, role_id, state, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (guild_id, clone_id, kind, external_id) DO NOTHING
            RETURNING id
        """, guild_id, clone_key(clone_id), kind, str(external_id), external_name,
            channel_id, role_id, json.dumps(state), user_id)


MAX_FEEDS_PER_GUILD = 10


async def remove_feed(feed_id: int, guild_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM connect_feeds WHERE id = $1 AND guild_id = $2", feed_id, guild_id)


async def feeds_of_kind(kind: str, clone_id) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM connect_feeds WHERE kind = $1 AND clone_id = $2", kind, clone_key(clone_id)
        )
    return [dict(r) for r in rows]


async def set_feed_state(feed_id: int, state: dict) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE connect_feeds SET state = $2 WHERE id = $1", feed_id, json.dumps(state))


def feed_state(row: dict) -> dict:
    try:
        v = json.loads(row.get("state") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


# ── per-guild settings ────────────────────────────────────────────────────

async def get_roblox_role(guild_id: int, clone_id):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT roblox_role_id FROM connect_guild WHERE guild_id = $1 AND clone_id = $2",
            guild_id, clone_key(clone_id),
        )


async def set_roblox_role(guild_id: int, clone_id, role_id) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO connect_guild (guild_id, clone_id, roblox_role_id) VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, clone_id) DO UPDATE SET roblox_role_id = EXCLUDED.roblox_role_id
        """, guild_id, clone_key(clone_id), role_id)


# ── formatting ────────────────────────────────────────────────────────────

def num(n) -> str:
    if n is None:
        return "hidden"
    n = int(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def ts(iso: str, style: str = "D") -> str:
    """ISO-8601 string -> Discord timestamp markup."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:{style}>"
    except ValueError:
        return "—"


# ── YouTube embeds ────────────────────────────────────────────────────────

def video_embed(v: dict) -> discord.Embed:
    url = f"https://www.youtube.com/watch?v={v['id']}"
    e = discord.Embed(title=clip(v["title"], 256), url=url, color=YT_RED,
                      description=clip(v["description"], 600) or None)
    if v.get("thumbnail"):
        e.set_image(url=v["thumbnail"])
    e.add_field(name="📺 Channel", value=f"[{clip(v['channel'], 60)}](https://www.youtube.com/channel/{v['channel_id']})", inline=True)
    e.add_field(name="⏱️ Length", value=("🔴 LIVE" if v["live"] == "live" else v["duration"]), inline=True)
    e.add_field(name="📅 Published", value=ts(v["published"]), inline=True)
    e.add_field(name="👁️ Views", value=num(v["views"]), inline=True)
    e.add_field(name="👍 Likes", value=num(v["likes"]), inline=True)
    e.add_field(name="💬 Comments", value=num(v["comments"]), inline=True)
    if v.get("tags"):
        e.add_field(name="🏷️ Tags", value=clip(", ".join(v["tags"][:8]), 200), inline=False)
    foot = [x for x in (v.get("definition"), f"ID {v['id']}") if x]
    e.set_footer(text=" · ".join(foot))
    return e


def channel_embed(c: dict) -> discord.Embed:
    url = f"https://www.youtube.com/channel/{c['id']}"
    e = discord.Embed(title=clip(c["title"], 256), url=url, color=YT_RED,
                      description=clip(c["description"], 600) or None)
    if c.get("thumbnail"):
        e.set_thumbnail(url=c["thumbnail"])
    if c.get("banner"):
        e.set_image(url=c["banner"])
    if c.get("handle"):
        e.add_field(name="🔗 Handle", value=c["handle"], inline=True)
    e.add_field(name="👥 Subscribers", value=num(c["subs"]), inline=True)
    e.add_field(name="🎞️ Videos", value=num(c["videos"]), inline=True)
    e.add_field(name="👁️ Total views", value=num(c["views"]), inline=True)
    e.add_field(name="📅 Created", value=ts(c["created"]), inline=True)
    if c.get("country"):
        e.add_field(name="🌍 Country", value=c["country"], inline=True)
    e.set_footer(text=f"ID {c['id']}")
    return e


def playlist_embed(p: dict) -> discord.Embed:
    url = f"https://www.youtube.com/playlist?list={p['id']}"
    e = discord.Embed(title=clip(p["title"], 256), url=url, color=YT_RED,
                      description=clip(p["description"], 500) or None)
    if p.get("thumbnail"):
        e.set_image(url=p["thumbnail"])
    e.add_field(name="📺 Channel", value=f"[{clip(p['channel'], 60)}](https://www.youtube.com/channel/{p['channel_id']})", inline=True)
    e.add_field(name="🎞️ Videos", value=num(p["count"]), inline=True)
    e.add_field(name="📅 Created", value=ts(p["created"]), inline=True)
    if p.get("first"):
        lines = [f"{i}. [{clip(x['title'], 70)}](https://www.youtube.com/watch?v={x['id']})"
                 for i, x in enumerate(p["first"], 1) if x.get("id")]
        if lines:
            e.add_field(name="▶️ First up", value="\n".join(lines), inline=False)
    e.set_footer(text=f"ID {p['id']}")
    return e


def trending_embed(region_label: str, region: str, videos: list) -> discord.Embed:
    e = discord.Embed(title=f"🔥 Trending on YouTube — {region_label}", color=YT_RED)
    lines = []
    for i, v in enumerate(videos[:10], 1):
        lines.append(
            f"**{i}.** [{clip(v['title'], 80)}](https://www.youtube.com/watch?v={v['id']})\n"
            f"　{clip(v['channel'], 40)} · 👁️ {num(v['views'])} · ⏱️ {v['duration']}"
        )
    e.description = "\n".join(lines) or "Nothing trending found."
    if videos and videos[0].get("thumbnail"):
        e.set_thumbnail(url=videos[0]["thumbnail"])
    e.set_footer(text=f"Region {region}")
    return e


def random_embed(v: dict) -> discord.Embed:
    e = video_embed(v)
    e.title = "🎲 " + (e.title or "")
    if v.get("region"):
        e.set_footer(text=f"Random pick · popular in {v['region']} · ID {v['id']}")
    return e


# ── Roblox embeds ─────────────────────────────────────────────────────────

def roblox_user_embed(u: dict) -> discord.Embed:
    title = u["display"] if u["display"] == u["name"] else f"{u['display']} (@{u['name']})"
    e = discord.Embed(title=clip(title, 256), url=f"https://www.roblox.com/users/{u['id']}/profile",
                      color=RBX_BLUE, description=clip(u["description"], 500) or None)
    if u.get("avatar"):
        e.set_thumbnail(url=u["avatar"])
    e.add_field(name="🆔 User ID", value=str(u["id"]), inline=True)
    e.add_field(name="📅 Joined", value=ts(u["created"]), inline=True)
    flags = []
    if u["verified_badge"]:
        flags.append("✅ Verified badge")
    if u["banned"]:
        flags.append("🚫 Banned")
    e.add_field(name="Status", value=", ".join(flags) or "Active", inline=True)
    e.add_field(name="🤝 Friends", value=num(u["friends"]) if u["friends"] is not None else "—", inline=True)
    e.add_field(name="👥 Followers", value=num(u["followers"]) if u["followers"] is not None else "—", inline=True)
    e.add_field(name="➡️ Following", value=num(u["following"]) if u["following"] is not None else "—", inline=True)
    return e


def roblox_game_embed(g: dict) -> discord.Embed:
    e = discord.Embed(title=clip(g["name"] or "Unknown game", 256), url=f"https://www.roblox.com/games/{g['place']}",
                      color=RBX_BLUE, description=clip(g["description"], 600) or None)
    if g.get("icon"):
        e.set_thumbnail(url=g["icon"])
    if g.get("creator"):
        e.add_field(name="🛠️ Creator", value=f"{g['creator']} ({(g.get('creator_type') or '').lower()})", inline=True)
    e.add_field(name="🟢 Playing now", value=num(g["playing"]), inline=True)
    e.add_field(name="👁️ Visits", value=num(g["visits"]), inline=True)
    e.add_field(name="⭐ Favorites", value=num(g["favorites"]), inline=True)
    if g.get("max_players"):
        e.add_field(name="👥 Max players", value=str(g["max_players"]), inline=True)
    if g.get("genre"):
        e.add_field(name="🎮 Genre", value=g["genre"], inline=True)
    e.add_field(name="📅 Created", value=ts(g["created"]), inline=True)
    e.add_field(name="🔄 Last updated", value=ts(g["updated"], "R"), inline=True)
    e.set_footer(text=f"Universe {g['universe']} · Place {g['place']}")
    return e


def roblox_group_embed(g: dict) -> discord.Embed:
    e = discord.Embed(title=clip(g["name"] or "Unknown group", 256), url=f"https://www.roblox.com/communities/{g['id']}",
                      color=RBX_BLUE, description=clip(g["description"], 600) or None)
    if g.get("icon"):
        e.set_thumbnail(url=g["icon"])
    e.add_field(name="👥 Members", value=num(g["members"]), inline=True)
    if g.get("owner"):
        e.add_field(name="👑 Owner", value=g["owner"], inline=True)
    e.add_field(name="🚪 Joining", value="Open" if g["public"] else "Approval needed", inline=True)
    if g.get("shout"):
        e.add_field(name="📣 Shout", value=clip(g["shout"], 300), inline=False)
    e.set_footer(text=f"Group {g['id']}")
    return e
