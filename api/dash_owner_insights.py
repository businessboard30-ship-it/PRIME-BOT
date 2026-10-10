# path: api/dash_owner_insights.py
"""Owner area, Phase 6: global search, alerts (notifications) and the growth chart series.

ROUTES only (read-only). A handler marked `wants_sections` also receives the caller's owner
sections, so results are filtered the same way the pages are gated; the router still enforces the
single section named in ROUTES first.
"""
import logging

logger = logging.getLogger(__name__)

SEARCH_SERVERS = 8


def wants_sections(fn):
    fn.wants_sections = True
    return fn


def _db_snapshot_age():
    from api import dash_owner
    return dash_owner._snapshot("main")


@wants_sections
async def search(q, sections) -> dict:
    """One box for everything: a server name or id, a user id, a payment reference."""
    from api import dash_owner
    term = (q("q") or "").strip()[:100]
    if len(term) < 2:
        return {"_error": (422, "Type at least 2 characters.")}
    out = {"q": term, "servers": [], "user_id": None, "payment": None}
    flake = dash_owner._snowflake(term)
    if flake is not None and "inspect" in sections:
        out["user_id"] = flake
    if "servers" in sections:
        from modules import admin_inspect as ai
        try:
            res = await ai.server_list(term, None, False, 0, SEARCH_SERVERS)
            out["servers"] = [{"guild_id": r["guild_id"], "clone_id": r.get("clone_id"), "guild_name": r.get("guild_name"),
                               "member_count": r.get("member_count"), "left": r.get("left_at") is not None}
                              for r in res["rows"]]
        except Exception:
            logger.exception("owner search: server lookup failed")
    if "money" in sections and dash_owner._REF_RE.match(term):
        from modules import admin_money as am
        try:
            row = await am.find_payment(term)
        except Exception:
            logger.exception("owner search: payment lookup failed")
            row = None
        if row:
            out["payment"] = {k: row.get(k) for k in ("paystack_reference", "status", "amount", "payment_type", "user_id")}
    return out


@wants_sections
async def alerts(q, sections) -> dict:
    from modules import admin_insights as ins, admin_snapshot as sn
    row = await _db_snapshot_age()
    age = None if not row else sn.age_seconds(row["updated_at"])
    facts = await ins.alert_facts(sections)
    items = ins.build_alerts(facts, age, sn.STALE_AFTER_S, sections)
    return {"alerts": items, "count": len(items), "bad": sum(1 for a in items if a["level"] == "bad")}


async def growth(q) -> dict:
    from modules import admin_insights as ins
    days = q("days") or "30"
    if not days.isdigit() or not 1 <= int(days) <= ins.MAX_GROWTH_DAYS:
        return {"_error": (422, "Bad range.")}
    series = await ins.growth_series(int(days))
    return {"days": int(days), "series": series,
            "net": sum(r["joined"] - r["left"] for r in series)}


async def visitors(q) -> dict:
    """OWNER ONLY (section 'access', which is never grantable to helpers). Who opened the dashboard today
    (UTC), a daily unique-visitor series, and sign-up / plan counts. Read-only; ids are shown because only the
    owner can open this."""
    import datetime as _dt
    days = q("days") or "14"
    if not days.isdigit() or not 1 <= int(days) <= 30:
        return {"_error": (422, "Bad range.")}
    days = int(days)
    from api import dash as _dash          # lazy: dash imports this module; tests fake dash.db
    r = await _dash.db.dash_visits_report(days)
    today = r["today"]
    series = []
    for i in range(days - 1, -1, -1):
        d = today - _dt.timedelta(days=i)
        series.append({"day": d.isoformat(), "visitors": r["series"].get(d, 0)})
    out_rows = []
    for v in r["visitors"]:
        out_rows.append({"user_id": str(v["user_id"]), "name": v.get("display_name"), "first_at": v["first_at"],
                         "last_at": v["last_at"], "touches": int(v["touches"] or 0), "new_today": bool(v.get("new_today")),
                         "returning": int(v.get("earlier_days") or 0) > 0, "plans": sorted(set(v.get("plans") or []))})
    return {"as_of_day": today.isoformat(), "timezone": "UTC", "today_count": len(out_rows), "visitors": out_rows,
            "yesterday": series[-2]["visitors"] if len(series) > 1 else 0, "series": series,
            "unique_7d": r["unique_7d"], "unique_30d": r["unique_30d"], "total_users": r["total_users"],
            "new_today": r["new_today"], "new_7d": r["new_7d"], "paying": r["paying"],
            "returning_today": sum(1 for v in out_rows if v["returning"])}


ROUTES = {
    "owner_visitors": ("access", visitors),
    "owner_search": ("inspect", search),
    "owner_alerts": ("health", alerts),
    "owner_growth": ("servers", growth),
}
WRITES = {}
