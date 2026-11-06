# path: modules/th_scenarios.py

"""
Bot test harness, phase 3 -- SIMULATED scenarios (docs/BOT_TEST_HARNESS_PLAN.md, category A).

Pure and in-memory: every scenario feeds synthetic events to the bot's real detection functions and records what they
decided. Nothing here touches Discord, the database or any payment/premium/economy table, so no snapshot is needed.
Caps are fixed (plan rule 10). Callers (the cog) re-check the gates; this module only builds results.

Scenarios: join_burst (join_gate + antiraid_pro profile filter), scam_links (Scam Shield text rules + strict mode).
Deferred (need a live handler, not a pure function): mass mention (inline in cogs/automod.py), dangerous role grant.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional

from modules import antiraid_pro, join_gate

MAX_JOINERS = 15          # synthetic joiners per join_burst (plan: <= 15 test members)
MAX_MESSAGES = 25         # synthetic messages per scam_links run

SCAM_SAMPLES = (
    ("fake giveaway + promo bait", "giveaway!! use promo code WIN100, register and withdraw instantly"),
    ("MrBeast casino bait", "MrBeast is giving away a casino bonus, join now"),
    ("@ link trick", "verify here https://discord.com@evil-login.xyz/auth"),
    ("fake discord subdomain", "check https://discord.com.verify-login.xyz/nitro"),
    ("nitro bait + link", "free nitro gift https://dlscord-gift.xyz/claim"),
)
CLEAN_SAMPLES = (
    ("normal chat", "hey everyone, anyone up for a game tonight?"),
    ("real discord link", "docs are at https://discord.com/developers"),
)


def _result(name: str, title: str, checks: List[dict], elapsed: float, notes: Optional[List[str]] = None) -> dict:
    passed = all(c["ok"] for c in checks)
    return {"name": name, "title": title, "pass": passed, "checks": checks,
            "gaps": [c["label"] for c in checks if not c["ok"]], "ms": round(elapsed * 1000, 1), "notes": notes or []}


def join_burst(n: int = 10, *, gate_cfg: Optional[dict] = None, raid_cfg: Optional[dict] = None,
               now: Optional[datetime] = None) -> dict:
    """n brand-new, default-avatar, bait-named accounts join; plus 2 control members who must be let in."""
    t0 = time.perf_counter()
    n = max(1, min(int(n), MAX_JOINERS))
    now = now or datetime.now(timezone.utc)
    gate_cfg = gate_cfg if gate_cfg is not None else {"enabled": True, "min_age_days": 7, "block_default_avatar": True}
    raid_cfg = raid_cfg if raid_cfg is not None else {"filter_age_days": 7, "filter_default_avatar": True,
                                                       "filter_suspicious_name": True}
    raiders = [SimpleNamespace(name=f"freenitro{100000 + i}", global_name=None, avatar=None,
                               created_at=now - timedelta(minutes=5 + i)) for i in range(n)]
    controls = [SimpleNamespace(name="olduser", global_name="Old User", avatar="abc",
                                created_at=now - timedelta(days=900)) for _ in range(2)]
    gate_hit = sum(1 for m in raiders if join_gate.reasons_for(m.created_at, m.avatar is not None, gate_cfg, now))
    prof_hit = sum(1 for m in raiders if antiraid_pro.evaluate_profile(m, raid_cfg, now))
    gate_ctl = sum(1 for m in controls if join_gate.reasons_for(m.created_at, m.avatar is not None, gate_cfg, now))
    prof_ctl = sum(1 for m in controls if antiraid_pro.evaluate_profile(m, raid_cfg, now))
    checks = [
        {"label": f"join gate caught {gate_hit}/{n} fresh accounts", "ok": gate_hit == n},
        {"label": f"antiraid profile filter flagged {prof_hit}/{n} raiders", "ok": prof_hit == n},
        {"label": "join gate let both control members in", "ok": gate_ctl == 0},
        {"label": "antiraid filter left both control members alone", "ok": prof_ctl == 0},
    ]
    return _result("join_burst", f"Join burst ({n} accounts)", checks, time.perf_counter() - t0,
                   ["Evaluates the decision functions only; lockdown timing needs the live handler (phase 4)."])


def scam_links(*, check: Optional[Callable] = None, strict: bool = True, allowed=()) -> dict:
    """Known scam texts must match; clean texts must not. `check` defaults to Scam Shield's real check_text."""
    t0 = time.perf_counter()
    if check is None:
        from modules import scam_shield
        check = scam_shield.check_text
    checks: List[dict] = []
    for label, text in SCAM_SAMPLES[:MAX_MESSAGES]:
        hit = check(text, allowed, strict)
        checks.append({"label": f"caught: {label}" + (f" ({hit[0]})" if hit else ""), "ok": hit is not None})
    for label, text in CLEAN_SAMPLES:
        hit = check(text, allowed, strict)
        checks.append({"label": f"left alone: {label}", "ok": hit is None})
    return _result("scam_links", "Scam links / bait messages", checks, time.perf_counter() - t0)


SCENARIOS: Dict[str, Callable[..., dict]] = {"join_burst": join_burst, "scam_links": scam_links}


def run(name: str, **kw) -> dict:
    fn = SCENARIOS.get(name)
    if fn is None:
        return {"name": name, "title": name, "pass": False, "checks": [], "gaps": ["unknown scenario"], "ms": 0.0, "notes": []}
    return fn(**kw)


def run_all(**kw) -> List[dict]:
    return [join_burst(n=kw.get("n", 10)), scam_links()]


def format_results(results: List[dict]) -> str:
    ok = sum(1 for r in results if r["pass"])
    lines = [f"🧪 **Simulated scenarios: {ok}/{len(results)} passed** (no changes made to the server)"]
    for r in results:
        lines.append(f"{'✅ PASS' if r['pass'] else '❌ FAIL'} **{r['title']}** — {r['ms']} ms")
        for c in r["checks"]:
            lines.append(f"  {'✓' if c['ok'] else '✗'} {c['label']}")
        for n in r["notes"]:
            lines.append(f"  note: {n}")
    gaps = [f"{r['title']}: {g}" for r in results for g in r["gaps"]]
    lines.append("**Gaps:** " + ("; ".join(gaps) if gaps else "none found"))
    return "\n".join(lines)[:1990]
