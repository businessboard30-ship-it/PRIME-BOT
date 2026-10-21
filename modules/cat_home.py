# path: modules/cat_home.py

"""Cat homes (Phase 8): buy a home with coins (and Primole for the big ones), keep coins safe in its vault.

  * Five tiers, cheapest to dearest: Beach Hut, Wooden Cottage, Sky Penthouse, Rocket Nest, Stone Castle. The three dearest
    also cost Primole. Renting in Cat Life stays the cheap starter; buying is the upgrade path.
  * One home per player per bot. Moving up to a bigger tier pays only the difference (coins and Primole), the vault comes along.
  * Every purchase is ONE database transaction: coins, Primole, the home row. If anything is short, nothing is taken.
  * The vault holds coins only (never Primole). Deposit takes coins from the wallet, withdraw gives them back. Vault size grows with the tier.
  * `earn_pct`, `capacity` and `defence` are stored for later phases (earnings, extra beds, raids) and are NOT used yet.

Pure rules at the top (no Discord, no database), queries at the bottom. All numbers are starting points (the balance sheet may move them).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from modules import cat_wallet as cw

logger = logging.getLogger(__name__)

IMAGE_BASE = os.getenv(
    "CAT_HOME_IMAGE_BASE",
    "https://raw.githubusercontent.com/businessboard30-ship-it/PRIME-BOT/main/assets/images/cat_homes/")


@dataclass(frozen=True)
class Tier:
    key: str
    emoji: str
    name: str
    coins: int
    primole: int
    vault: int           # most coins the vault can hold
    capacity: int        # later phase (extra beds)
    earn_pct: int        # later phase (earnings bonus, 100 = none)
    defence: int         # later phase (raids)
    blurb: str


TIERS: Tuple[Tier, ...] = (
    Tier("beach", "🏖️", "Beach Hut", 150_000, 0, 50_000, 3, 105, 5, "A breezy straw hut with a hammock, right on the sand."),
    Tier("cottage", "🏡", "Wooden Cottage", 1_000_000, 0, 400_000, 5, 110, 10, "A snug garden cottage with a round window and a scratching post."),
    Tier("penthouse", "🌃", "Sky Penthouse", 8_000_000, 50, 3_000_000, 8, 120, 20, "A glass penthouse above the city lights."),
    Tier("rocket", "🚀", "Rocket Nest", 60_000_000, 300, 25_000_000, 12, 135, 35, "A cosy rocket parked among the stars."),
    Tier("castle", "🏰", "Stone Castle", 400_000_000, 1_500, 150_000_000, 16, 150, 50, "Towers, banners and a drawbridge. The top of the street."),
)
BY_KEY = {t.key: t for t in TIERS}
DEFAULT_KEY = TIERS[0].key
MAX_ROWS = 25


def tier(key: Optional[str]) -> Optional[Tier]:
    return BY_KEY.get(key or "")


def index(key: Optional[str]) -> int:
    """Position of a tier (0 = cheapest), -1 for no home / unknown."""
    t = tier(key)
    return TIERS.index(t) if t else -1


def image_url(key: str) -> str:
    return f"{IMAGE_BASE}{key}.png"


def cost_to_reach(current_key: Optional[str], target_key: str) -> Optional[Tuple[int, int]]:
    """(coins, Primole) to buy `target_key` while owning `current_key` (None: no home yet, full price). Moving up pays
    only the difference. None when the target is unknown, or is the same tier or a smaller one."""
    target = tier(target_key)
    if target is None:
        return None
    cur = tier(current_key)
    if cur is None:
        return target.coins, target.primole
    if index(target.key) <= index(cur.key):
        return None
    return target.coins - cur.coins, target.primole - cur.primole


def price_text(coins: int, primole: int) -> str:
    return f"{coins:,} {cw.COINS}" + (f" + {primole:,} {cw.PRIMOLE}" if primole else "")


def vault_room(t: Tier, vault: int) -> int:
    return max(0, t.vault - int(vault))


def vault_bar(vault: int, cap: int, width: int = 10) -> str:
    filled = 0 if cap <= 0 else min(width, int(width * max(0, int(vault)) / cap))
    return "▰" * filled + "▱" * (width - filled)


_UNITS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_amount(text: str) -> Optional[int]:
    """'12,500', '10k', '2.5m' -> a whole number of coins. None when it isn't a positive amount."""
    s = (text or "").strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", s)
    if not m:
        return None
    value = float(m.group(1)) * _UNITS.get(m.group(2), 1)
    n = int(round(value))
    return n if n >= 1 and n < 10 ** 13 else None


@dataclass(frozen=True)
class Home:
    tier: str = ""
    vault: int = 0
    invested_coins: int = 0
    invested_primole: int = 0

    @property
    def t(self) -> Optional[Tier]:
        return tier(self.tier)


def screen_text(home: Optional[Home], wallet: cw.Wallet, shown: Tier, armed: bool = False, note: Optional[str] = None) -> str:
    """The whole text of the Home screen (pure, so the budget test can measure it)."""
    parts: List[str] = []
    if note:
        parts += [note, ""]
    parts.append("## 🏡 Home")
    own = home.t if home else None
    if own:
        parts.append(f"{own.emoji} **{own.name}**\nVault {vault_bar(home.vault, own.vault)} **{home.vault:,}** / {own.vault:,} {cw.COINS}")
    else:
        parts.append("You don't own a home yet. Renting in **Cat Life** is the cheap starter; a home is the long-term goal.")
    parts += ["", cw.wallet_text(wallet), ""]
    parts.append(f"### {shown.emoji} {shown.name}")
    parts.append(shown.blurb)
    cost = cost_to_reach(own.key if own else None, shown.key)
    if own and shown.key == own.key:
        parts.append("✅ **This is your home.**")
    elif cost is None:
        parts.append("You already own a bigger home than this one.")
    else:
        coins, prim = cost
        verb = "Move up for" if own else "Price"
        parts.append(f"**{verb}:** {price_text(coins, prim)}" + (f" (the difference from your {own.name})" if own else ""))
        if armed:
            parts.append("⚠️ Tap **Confirm** to pay. This can't be undone here.")
    parts.append(f"**Vault size:** {shown.vault:,} {cw.COINS}")
    return "\n".join(parts)


# ── database ──────────────────────────────────────────────────────────────

class _Rollback(Exception):
    """Raised inside a transaction to undo everything taken so far."""


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


def _home(row) -> Optional[Home]:
    if not row:
        return None
    return Home(str(row["tier"]), int(row["vault"]), int(row["invested_coins"]), int(row["invested_primole"]))


async def get_home(owner_id: int, clone_id) -> Optional[Home]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tier, vault, invested_coins, invested_primole FROM cat_homes WHERE owner_id = $1 AND clone_key = $2",
            owner_id, _key(clone_id))
    return _home(row)


async def _spend_coins(conn, owner_id: int, amount: int) -> Optional[int]:
    row = await conn.fetchrow(
        "UPDATE cat_wallets SET coins = coins - $2, updated_at = NOW() "
        "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = -1 AND coins >= $2 RETURNING coins", owner_id, int(amount))
    return int(row["coins"]) if row else None


async def _add_coins(conn, owner_id: int, amount: int) -> None:
    await conn.execute(
        "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1, NULL, $2, 0) "
        "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
        "coins = cat_wallets.coins + EXCLUDED.coins, updated_at = NOW()", owner_id, int(amount))


@dataclass(frozen=True)
class BuyResult:
    status: str          # ok | unknown | same | lower | poor | poor_primole
    tier: str = ""
    coins: int = 0       # coins paid
    primole: int = 0     # Primole paid


async def buy(owner_id: int, clone_id, tier_key: str) -> BuyResult:
    """Buy a home, or move up to a bigger one (pay the difference). One transaction: nothing is taken unless everything is."""
    target = tier(tier_key)
    if target is None:
        return BuyResult("unknown")
    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT tier FROM cat_homes WHERE owner_id = $1 AND clone_key = $2 FOR UPDATE", owner_id, _key(clone_id))
                current = str(row["tier"]) if row else None
                cost = cost_to_reach(current, target.key)
                if cost is None:
                    return BuyResult("same" if current == target.key else "lower", target.key)
                coins, prim = cost
                if prim:
                    if await cw.spend_primole(conn, owner_id, prim) is None:
                        return BuyResult("poor_primole", target.key, coins, prim)
                if coins and await _spend_coins(conn, owner_id, coins) is None:
                    raise _Rollback()          # undo the Primole that was just taken
                await conn.execute(
                    "INSERT INTO cat_homes (owner_id, clone_key, tier, invested_coins, invested_primole) VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (owner_id, clone_key) DO UPDATE SET tier = EXCLUDED.tier, "
                    "invested_coins = cat_homes.invested_coins + EXCLUDED.invested_coins, "
                    "invested_primole = cat_homes.invested_primole + EXCLUDED.invested_primole, updated_at = NOW()",
                    owner_id, _key(clone_id), target.key, coins, prim)
                return BuyResult("ok", target.key, coins, prim)
    except _Rollback:
        return BuyResult("poor", target.key, coins, prim)


@dataclass(frozen=True)
class VaultResult:
    status: str          # ok | no_home | bad | full | poor | empty
    moved: int = 0
    vault: int = 0
    coins: int = 0       # wallet coins afterwards (when known)


async def deposit(owner_id: int, clone_id, amount: Optional[int]) -> VaultResult:
    """Wallet -> vault. amount None: as much as fits. One transaction with the home row locked, so two quick taps can't overfill it."""
    if amount is not None and int(amount) < 1:
        return VaultResult("bad")
    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT tier, vault FROM cat_homes WHERE owner_id = $1 AND clone_key = $2 FOR UPDATE", owner_id, _key(clone_id))
                if not row:
                    return VaultResult("no_home")
                t = tier(row["tier"])
                room = vault_room(t, row["vault"]) if t else 0
                want = room if amount is None else int(amount)
                if want < 1 or room < 1:
                    return VaultResult("full", 0, int(row["vault"]))
                if want > room:
                    return VaultResult("full", 0, int(row["vault"]))
                left = await _spend_coins(conn, owner_id, want)
                if left is None:
                    raise _Rollback()
                new = await conn.fetchval(
                    "UPDATE cat_homes SET vault = vault + $3, updated_at = NOW() WHERE owner_id = $1 AND clone_key = $2 RETURNING vault",
                    owner_id, _key(clone_id), want)
                return VaultResult("ok", want, int(new), left)
    except _Rollback:
        return VaultResult("poor")


async def withdraw(owner_id: int, clone_id, amount: Optional[int]) -> VaultResult:
    """Vault -> wallet. amount None: everything. The vault row is locked and the subtraction is guarded, so it can't go below zero."""
    if amount is not None and int(amount) < 1:
        return VaultResult("bad")
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT vault FROM cat_homes WHERE owner_id = $1 AND clone_key = $2 FOR UPDATE", owner_id, _key(clone_id))
            if not row:
                return VaultResult("no_home")
            have = int(row["vault"])
            want = have if amount is None else int(amount)
            if have < 1 or want < 1:
                return VaultResult("empty", 0, have)
            if want > have:
                return VaultResult("empty", 0, have)
            new = await conn.fetchval(
                "UPDATE cat_homes SET vault = vault - $3, updated_at = NOW() WHERE owner_id = $1 AND clone_key = $2 AND vault >= $3 RETURNING vault",
                owner_id, _key(clone_id), want)
            if new is None:
                return VaultResult("empty", 0, have)
            await _add_coins(conn, owner_id, want)
            return VaultResult("ok", want, int(new))
