"""
Part 3.3 data-audit script.

Run this BEFORE deciding what to do about existing `cloned_bots` rows (every
one of which has a fake, pre-rebuild token). This only reads data — it makes
no changes and calls no external APIs.

Usage:
    python3 scripts/check_existing_clones.py

Requires DATABASE_URL to be set in the environment (same one the bot uses).
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_pool  # noqa: E402


async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM cloned_bots")
        paid = await conn.fetchval(
            "SELECT COUNT(*) FROM cloned_bots WHERE payment_status IN ('verified', 'paid')"
        )
        rows = await conn.fetch("""
            SELECT clone_id, owner_id, bot_name, payment_id, payment_status, status, created_date
            FROM cloned_bots
            ORDER BY created_date ASC
        """)

    print(f"Total cloned_bots rows: {total}")
    print(f"Rows with a payment marked verified/paid: {paid}")
    print()

    if total == 0:
        print("No clone rows exist at all. Nothing to remediate — the recommendation")
        print("in this case (per Part 3.3) is: ship the real system, no customer")
        print("outreach needed.")
        return

    print("Per-row detail (all of these currently hold a FAKE, non-functional token):")
    for r in rows:
        print(
            f"  clone_id={r['clone_id']:<5} owner_id={r['owner_id']:<12} "
            f"bot_name={r['bot_name']!r:<30} payment_id={r['payment_id']!r:<25} "
            f"payment_status={r['payment_status']!r:<10} status={r['status']!r:<10} "
            f"created={r['created_date']}"
        )

    print()
    print("Recommendation logic (fill in after reading the output above):")
    print(" - If `paid` count is 0: no real paying customer ever completed the flow.")
    print("   -> Ship the real system, no customer-facing remediation needed.")
    print(" - If `paid` count > 0: those are real customers holding a bot that never")
    print("   worked. Recommended path: proactively message them (Part 3.3 option 1) —")
    print("   walk them through the new Step A/B token-paste flow using their existing")
    print("   payment_id as already-settled, don't charge twice. Reserve refunds for")
    print("   anyone who no longer wants to go through BotFather.")


if __name__ == "__main__":
    asyncio.run(main())
