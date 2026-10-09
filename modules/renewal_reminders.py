# path: modules/renewal_reminders.py
"""Renewal reminders for per-user plans (card plan, Developer mode). One DM per user + plan + period, claimed in the
database first so a repeated cron tick (or two overlapping ones) can never send it twice. Pure wording lives here so
it can be tested without Discord."""
from modules import user_subs

WITHIN_DAYS = 3

_KINDS = {"renews": "renews", "ends": "ends", "failed": "failed"}


def kind_for(row: dict) -> str:
    """renews = auto-renews soon; ends = cancelled, access stops at the period end; failed = renewal charge failed."""
    if row.get("status") == "past_due":
        return "failed"
    if row.get("status") == "cancelled" or row.get("cancel_at_period_end"):
        return "ends"
    return "renews"


def message(row: dict, site_url: str = "") -> str:
    """The DM text. Never contains payment links or secrets; points at the dashboard instead."""
    plan = user_subs.PLANS.get(row.get("product")) or {}
    label = plan.get("label") or "your plan"
    exp = row.get("expires_at")
    day = exp.strftime("%d %b %Y") if hasattr(exp, "strftime") else "soon"
    where = f"\nManage it here: {site_url}/#/me/plans" if site_url else ""
    k = kind_for(row)
    if k == "failed":
        return (f"Your **{label}** renewal payment didn't go through. You keep access for a short grace period after {day}. "
                f"Please update your payment method or subscribe again to avoid losing it.{where}")
    if k == "ends":
        return f"Your **{label}** was cancelled and ends on **{day}**. Subscribe again any time to keep it.{where}"
    return f"Your **{label}** renews automatically on **{day}**. You can cancel any time before then.{where}"


async def run(db, send, site_url: str = "", limit: int = 50) -> dict:
    """send(user_id:int, text:str) -> None on success, or an error string. Closed DMs (403/404) are final and not retried."""
    totals = {"sent": 0, "skipped": 0, "retry": 0}
    for row in await db.renewal_reminder_candidates(WITHIN_DAYS, limit):
        if row.get("product") not in user_subs.PLANS:
            continue
        kind = kind_for(row)
        if not await db.renewal_reminder_claim(row["user_id"], row["product"], row["expires_at"], kind):
            totals["skipped"] += 1
            continue
        error = await send(int(row["user_id"]), message(row, site_url))
        if error is None:
            totals["sent"] += 1
        elif error.startswith("network_error") or error.rsplit(":", 1)[-1] in ("429",) or error.rsplit(":", 1)[-1].startswith("5"):
            await db.renewal_reminder_release(row["user_id"], row["product"], row["expires_at"])   # transient: try next tick
            totals["retry"] += 1
        else:
            totals["skipped"] += 1                                                                 # closed DMs: final
    return totals
