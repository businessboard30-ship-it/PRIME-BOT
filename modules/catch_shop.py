"""Shop and Wallet (economy slice).

* ``purchase`` is the ONLY code path that spends coins. It runs in one transaction:
  a single guarded ``UPDATE ... WHERE coins >= total`` (which also locks the player row,
  so concurrent purchases serialise and can never overspend), then the item grant and an
  audit row. If any step fails the whole purchase rolls back, so coins and items never
  drift apart. ``catch_players.coins`` also has a ``CHECK (coins >= 0)`` as a backstop.
* Only items in ``CATALOG`` can be bought, and only in the quantities in ``QUANTITIES``;
  the client never chooses a price. Prices are balance numbers tuned in P11-06.
* ``load_wallet`` reads the balance and the player's recent coin movements from
  ``catch_audit`` (daily rewards and shop purchases).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from modules import catch_db
from modules.catch_items import grant_items, item_name

# Capsule and berry prices in coins. The sovereign capsule is a guaranteed catch, so it is
# never sold here; it stays a reward item.
CATALOG: dict[str, int] = {
    "capsule_basic": 50,
    "capsule_sturdy": 150,
    "capsule_prime": 500,
    "honeyberry": 40,
    "goldberry": 120,
}
QUANTITIES: tuple[int, ...] = (1, 5, 10)
WALLET_ACTIONS = ("daily", "shop_purchase")
WALLET_HISTORY = 10


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    ok: bool
    reason: str | None = None
    item_key: str | None = None
    quantity: int = 0
    total: int = 0
    coins_left: int = 0


@dataclass(frozen=True, slots=True)
class WalletEntry:
    action: str
    coins: int  # signed: positive earned, negative spent
    summary: str
    at: object


@dataclass(frozen=True, slots=True)
class Wallet:
    coins: int
    entries: tuple[WalletEntry, ...]


def price_of(item_key: str) -> int:
    return CATALOG[item_key]


def total_price(item_key: str, quantity: int) -> int:
    if item_key not in CATALOG:
        raise KeyError(f"item is not for sale: {item_key}")
    if quantity not in QUANTITIES:
        raise ValueError(f"quantity must be one of {QUANTITIES}")
    return CATALOG[item_key] * quantity


async def purchase(
    user_id: int,
    clone_id: int | None,
    item_key: str,
    quantity: int,
    *,
    guild_id: int | None = None,
    conn=None,
) -> PurchaseResult:
    """Buy ``quantity`` of ``item_key``. Never raises for "can't afford"; returns ok=False."""
    if item_key not in CATALOG:
        return PurchaseResult(False, "not_for_sale", item_key, quantity)
    if quantity not in QUANTITIES:
        return PurchaseResult(False, "bad_quantity", item_key, quantity)
    total = CATALOG[item_key] * quantity
    key = catch_db.clone_key(clone_id)
    async with catch_db.transaction(conn) as c:
        coins_left = await c.fetchval(
            "UPDATE catch_players SET coins = coins - $3, updated_at = now() "
            "WHERE user_id = $1 AND clone_key = $2 AND coins >= $3 RETURNING coins",
            user_id, key, total,
        )
        if coins_left is None:
            have = await c.fetchval(
                "SELECT coins FROM catch_players WHERE user_id = $1 AND clone_key = $2", user_id, key,
            )
            return PurchaseResult(False, "insufficient_coins", item_key, quantity, total, int(have or 0))
        await grant_items(user_id, clone_id, {item_key: quantity}, conn=c)
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, detail) "
            "VALUES ($1, $1, $2, $3, 'shop_purchase', $4::jsonb)",
            user_id, clone_id, guild_id,
            json.dumps({"item": item_key, "quantity": quantity, "unit_price": CATALOG[item_key], "total": total}),
        )
    return PurchaseResult(True, None, item_key, quantity, total, int(coins_left))


def _detail(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def entry_from_row(action: str, detail, at) -> WalletEntry | None:
    """Turn one audit row into a wallet line, or None if it moved no coins."""
    data = _detail(detail)
    if action == "daily":
        coins = int(data.get("coins", 0))
        return WalletEntry(action, coins, f"Daily reward (day {int(data.get('streak', 0))})", at) if coins else None
    if action == "shop_purchase":
        total = int(data.get("total", 0))
        label = f"{item_name(str(data.get('item', 'item')))} ×{int(data.get('quantity', 0))}"
        return WalletEntry(action, -total, label, at) if total else None
    return None


async def load_wallet(user_id: int, clone_id: int | None, *, conn=None) -> Wallet:
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as c:
        coins = await c.fetchval(
            "SELECT coins FROM catch_players WHERE user_id = $1 AND clone_key = $2", user_id, key,
        )
        rows = await c.fetch(
            "SELECT action, detail, at FROM catch_audit "
            "WHERE user_id = $1 AND COALESCE(clone_id, -1) = $2 AND action = ANY($3::text[]) "
            "ORDER BY id DESC LIMIT $4",
            user_id, key, list(WALLET_ACTIONS), WALLET_HISTORY,
        )
    entries = tuple(e for e in (entry_from_row(r["action"], r["detail"], r["at"]) for r in rows) if e)
    return Wallet(int(coins or 0), entries)


__all__ = [
    "CATALOG", "QUANTITIES", "PurchaseResult", "Wallet", "WalletEntry", "entry_from_row",
    "load_wallet", "price_of", "purchase", "total_price",
]
