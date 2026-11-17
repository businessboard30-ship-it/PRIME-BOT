"""Apology for a payment that was processed late.

A Gumroad ping carries the time of the sale. When the bot only unlocks it long after that (a missed or retried ping,
a short outage), the buyer is told what happened and thanked for waiting, in the same DM that confirms the purchase.
Nothing is sent for normal payments, and the time of the *checkout* is never used (someone can open a pay link and
pay hours later; that isn't a glitch).
"""
import time
from datetime import datetime, timezone
from typing import Optional

LATE_AFTER_MINUTES = 20


def parse_sale_time(value) -> Optional[float]:
    """Epoch seconds from Gumroad's sale_timestamp (ISO-8601 or epoch seconds/milliseconds); None if absent/unreadable."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        s = str(value).strip()
        if s.replace(".", "", 1).isdigit():
            n = float(s)
            if n <= 0:
                return None
            return n / 1000.0 if n > 1e11 else n
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def minutes_late(fields: dict, now: Optional[float] = None) -> Optional[int]:
    """How many minutes after the sale the bot unlocked it, or None when that's normal / unknown."""
    sold = parse_sale_time((fields or {}).get("sale_timestamp"))
    if sold is None:
        return None
    late = ((time.time() if now is None else now) - sold) / 60.0
    return int(late) if late >= LATE_AFTER_MINUTES else None


def _how_long(minutes: int) -> str:
    if minutes < 90:
        return f"about {minutes} minutes"
    hours = round(minutes / 60)
    if hours < 36:
        return f"about {hours} hours"
    return f"about {round(hours / 24)} days"


def apology_note(minutes: int) -> str:
    return ("🙏 **Sorry for the wait!** We had a small glitch on our side, so your payment took "
            f"{_how_long(minutes)} to show up. It's all sorted now and your purchase is fully active. "
            "Thank you for your patience, and for supporting us 💙")
