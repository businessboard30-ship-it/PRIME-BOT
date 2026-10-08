# path: modules/admin_access.py
"""Who may open which owner-panel section.

Single source of truth shared by the Discord Owner panel
(discord_bot/cogs/_views_admin_panel.allowed_sections) and the web owner area
(api/dash.py). Pure and synchronous: no Discord, no DB, so both can import it
and a parity test can compare them.

Rules (unchanged from the Discord panel):
  * DISCORD_CLONE_ADMIN_IDS  -> every owner section except broadcast/feedback.
  * DISCORD_OWNER_BROADCAST_IDS -> broadcast + feedback.
  * Helpers get ONLY sections in admin_controls.GRANTABLE (passed in as
    ``helper_sections``); owner-only sections can never be granted.
"""
from typing import Iterable, Set

# Sections granted by DISCORD_CLONE_ADMIN_IDS membership.
CLONE_ADMIN_SECTIONS = frozenset({
    "payments", "servers", "bump", "system",
    "controls", "blacklist", "premium", "audit",
    "access", "logs", "config", "database",
    "watchlist", "reports", "status", "honeypot",
    "money", "scamshield", "referral", "ads",
    "health", "inspect",
})
# Sections granted by DISCORD_OWNER_BROADCAST_IDS membership.
BROADCAST_SECTIONS = frozenset({"broadcast", "feedback"})
# Never grantable to helpers, whatever admin_controls.GRANTABLE says.
OWNER_ONLY = frozenset({"access", "config", "database"})


def compute_sections(user_id: int, clone_admin_ids: Iterable[int], broadcast_ids: Iterable[int],
                     helper_sections: Iterable[str] = ()) -> Set[str]:
    out: Set[str] = set()
    if user_id in set(clone_admin_ids):
        out |= CLONE_ADMIN_SECTIONS
    if user_id in set(broadcast_ids):
        out |= BROADCAST_SECTIONS
    out |= {s for s in helper_sections if s not in OWNER_ONLY}
    return out
