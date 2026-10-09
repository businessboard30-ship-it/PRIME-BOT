# path: modules/dev_jobs.py
"""Developer mode D: scheduled jobs. Pure rules (no Discord, no database, no secrets).

A job is one of a few safe, bounded kinds that runs on an ALLOWLISTED preset schedule (never a free cron string), so nobody can
make a job run every minute. Times are UTC. A job payload only ever holds plain text and a model NAME; an API key is never part
of it (own-key chats read the encrypted key server-side at run time).
"""
import json
from datetime import datetime, timedelta, timezone

MAX_JOBS = 5                      # per person
MIN_INTERVAL = timedelta(hours=1)
MAX_FAILS = 5                     # consecutive failures, then the job is switched off
PAUSED_KEEP_DAYS = 30             # a job paused by a lapsed plan is purged after this, same as keys
NAME_MAX = 60
TEXT_MAX = 4000                   # note / reminder text
PROMPT_MAX = 2000
KINDS = {"reminder": "DM reminder", "note": "Scheduled note (saved to Exports)", "ai_prompt": "Scheduled AI prompt (saved to Exports)"}
PRESETS = ("hourly", "daily", "weekly")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_BAD = ("\x00",)


def _int(v, lo, hi):
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def clean_schedule(raw):
    """{preset: hourly|daily|weekly, minute?, hour?, day?} -> (canonical string, error). Canonical: hourly@MM, daily@HH:MM, weekly@D@HH:MM."""
    raw = raw if isinstance(raw, dict) else {}
    preset = str(raw.get("preset") or "").lower()
    if preset not in PRESETS:
        return None, "Pick hourly, daily or weekly."
    minute = _int(raw.get("minute", 0), 0, 59)
    if minute is None:
        return None, "That minute isn't valid."
    if preset == "hourly":
        return f"hourly@{minute:02d}", None
    hour = _int(raw.get("hour", 9), 0, 23)
    if hour is None:
        return None, "That hour isn't valid."
    if preset == "daily":
        return f"daily@{hour:02d}:{minute:02d}", None
    day = _int(raw.get("day", 0), 0, 6)
    if day is None:
        return None, "That day isn't valid."
    return f"weekly@{day}@{hour:02d}:{minute:02d}", None


def parse_schedule(s):
    """Canonical string -> dict, or None when it is not one of ours (a corrupt row is never run)."""
    try:
        parts = str(s).split("@")
        if parts[0] == "hourly" and len(parts) == 2:
            return {"preset": "hourly", "minute": _int(parts[1], 0, 59)} if _int(parts[1], 0, 59) is not None else None
        if parts[0] == "daily" and len(parts) == 2:
            h, m = parts[1].split(":")
            return {"preset": "daily", "hour": _int(h, 0, 23), "minute": _int(m, 0, 59)} if None not in (_int(h, 0, 23), _int(m, 0, 59)) else None
        if parts[0] == "weekly" and len(parts) == 3:
            h, m = parts[2].split(":")
            d = _int(parts[1], 0, 6)
            return {"preset": "weekly", "day": d, "hour": _int(h, 0, 23), "minute": _int(m, 0, 59)} if None not in (d, _int(h, 0, 23), _int(m, 0, 59)) else None
    except (ValueError, IndexError):
        return None
    return None


def next_run(schedule, after):
    """First scheduled slot strictly after `after` (UTC), or None for an invalid schedule. Consecutive slots are always >= 1 hour apart."""
    sc = parse_schedule(schedule)
    if not sc:
        return None
    after = after.astimezone(timezone.utc) if after.tzinfo else after.replace(tzinfo=timezone.utc)
    if sc["preset"] == "hourly":
        c = after.replace(minute=sc["minute"], second=0, microsecond=0)
        step = timedelta(hours=1)
    elif sc["preset"] == "daily":
        c = after.replace(hour=sc["hour"], minute=sc["minute"], second=0, microsecond=0)
        step = timedelta(days=1)
    else:
        c = after.replace(hour=sc["hour"], minute=sc["minute"], second=0, microsecond=0) + timedelta(days=(sc["day"] - after.weekday()) % 7)
        step = timedelta(days=7)
    while c <= after:
        c += step
    return c


def clean_job(body):
    """Validate a create/edit request. Returns (spec, error). The kind, limits and schedule are decided here, never by the client."""
    from modules import dev_keys
    body = body if isinstance(body, dict) else {}
    kind = str(body.get("kind") or "").lower()
    if kind not in KINDS:
        return None, "That kind of job isn't supported."
    name = " ".join(str(body.get("name") or "").split())[:NAME_MAX]
    if not name:
        return None, "Give the job a name."
    schedule, err = clean_schedule(body.get("schedule"))
    if err:
        return None, err
    text = body.get("text") if kind != "ai_prompt" else body.get("prompt")
    cap = PROMPT_MAX if kind == "ai_prompt" else TEXT_MAX
    if not isinstance(text, str) or not text.strip():
        return None, "Write what the job should say." if kind != "ai_prompt" else "Write the prompt to run."
    if any(b in text for b in _BAD) or len(text) > cap:
        return None, f"That text is too long (at most {cap} characters)." if len(text) > cap else "That text can't be used."
    payload = {"text": text.strip()} if kind != "ai_prompt" else {"prompt": text.strip(), "model": "default"}
    if kind == "ai_prompt":
        model = str(body.get("model") or "default").strip().lower()
        if model != "default" and not dev_keys.clean_provider(model):
            return None, "Pick a model from the list."
        payload["model"] = model
    return {"kind": kind, "name": name, "schedule": schedule, "payload": payload}, None


def payload_text(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


def load_payload(raw) -> dict:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def public_view(r) -> dict:
    """What the browser may see. The owner id is never returned; the prompt/text is the person's own."""
    p = load_payload(r.get("payload"))
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else None
    return {"id": int(r["id"]), "kind": r["kind"], "name": r["name"], "schedule": r["schedule"],
            "text": p.get("text") or p.get("prompt") or "", "model": p.get("model"),
            "enabled": bool(r["enabled"]), "paused": r.get("paused_at") is not None,
            "next_run_at": iso(r.get("next_run_at")), "last_run_at": iso(r.get("last_run_at")),
            "last_status": r.get("last_status"), "fail_count": int(r.get("fail_count") or 0)}
