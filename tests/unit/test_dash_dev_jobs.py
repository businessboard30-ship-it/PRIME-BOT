"""Phase C: Developer scheduled jobs. Presets only, atomic claim, plan/kill-switch/failure rules, no secrets, worker wiring."""
import asyncio
import inspect
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import database
from api import cron_dev_scheduled as cron
from api import dash, dash_dev
from modules import dev_jobs as J
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  (token "other" = dev subscriber 7777777777; "sid" = user 6, no dev plan)

UTC = timezone.utc
T0 = datetime(2026, 10, 9, 10, 35, tzinfo=UTC)          # a Friday


# ---------- pure rules ----------
@pytest.mark.parametrize("raw,ok", [
    ({"preset": "hourly"}, "hourly@00"), ({"preset": "hourly", "minute": 30}, "hourly@30"),
    ({"preset": "daily", "hour": 7, "minute": 5}, "daily@07:05"), ({"preset": "weekly", "day": 2, "hour": 23, "minute": 59}, "weekly@2@23:59"),
    ({"preset": "minutely"}, None), ({"preset": "*/1 * * * *"}, None), ("* * * * *", None), ({"preset": "daily", "hour": 24}, None),
    ({"preset": "daily", "hour": "x"}, None), ({"preset": "hourly", "minute": 60}, None), ({"preset": "weekly", "day": 7}, None),
    ({"preset": "daily", "hour": True}, None), (None, None)])
def test_only_allowlisted_presets_are_accepted(raw, ok):
    sched, err = J.clean_schedule(raw)
    assert (sched == ok) and (err is None) == (ok is not None)


def test_next_run_is_strictly_in_the_future_and_consecutive_slots_are_an_hour_apart():
    for sched in ("hourly@00", "hourly@59", "daily@00:00", "daily@10:35", "weekly@4@10:35", "weekly@0@00:00"):
        t = T0
        slots = []
        for _ in range(30):
            t = J.next_run(sched, t)
            assert t is not None and t.tzinfo is not None
            slots.append(t)
        assert slots[0] > T0
        assert all(b - a >= J.MIN_INTERVAL for a, b in zip(slots, slots[1:])), sched


def test_next_run_values():
    assert J.next_run("hourly@40", T0) == datetime(2026, 10, 9, 10, 40, tzinfo=UTC)
    assert J.next_run("hourly@30", T0) == datetime(2026, 10, 9, 11, 30, tzinfo=UTC)
    assert J.next_run("daily@10:35", T0) == datetime(2026, 10, 10, 10, 35, tzinfo=UTC)     # equal is not "after"
    assert J.next_run("weekly@4@09:00", T0) == datetime(2026, 10, 16, 9, 0, tzinfo=UTC)    # Friday = 4
    assert J.next_run("weekly@6@09:00", T0) == datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["", "* * * * *", "hourly", "hourly@99", "daily@25:00", "weekly@9@01:00", "daily@xx", None])
def test_corrupt_schedule_never_runs(bad):
    assert J.parse_schedule(bad) is None and J.next_run(bad, T0) is None


def good(**kw):
    b = {"kind": "note", "name": "Standup", "schedule": {"preset": "daily", "hour": 9}, "text": "hello"}
    b.update(kw)
    return b


def test_clean_job_validation_and_caps():
    spec, err = J.clean_job(good())
    assert err is None and spec["schedule"] == "daily@09:00" and spec["payload"] == {"text": "hello"}
    assert J.clean_job(good(kind="shell"))[1] and J.clean_job(good(name="  "))[1] and J.clean_job(good(text=""))[1]
    assert J.clean_job(good(text="x" * (J.TEXT_MAX + 1)))[1] and J.clean_job(good(text="a\x00b"))[1]
    assert J.clean_job(good(kind="ai_prompt", prompt="x" * (J.PROMPT_MAX + 1)))[1]
    s2, e2 = J.clean_job(good(kind="ai_prompt", prompt="hi", model="Groq"))
    assert e2 is None and s2["payload"] == {"prompt": "hi", "model": "groq"}
    assert J.clean_job(good(kind="ai_prompt", prompt="hi", model="gpt-evil"))[1]
    assert len(J.clean_job(good(name="n" * 500))[0]["name"]) == J.NAME_MAX


def test_payload_and_view_never_hold_a_key_or_owner_id():
    spec, _ = J.clean_job(good(kind="ai_prompt", prompt="hi", model="anthropic", api_key="sk-ant-SECRET", key="sk-SECRET", user_id="1"))
    assert "SECRET" not in J.payload_text(spec["payload"]) and set(spec["payload"]) == {"prompt", "model"}
    v = J.public_view({"id": 1, "user_id": "6", "kind": "note", "name": "n", "schedule": "daily@09:00", "payload": '{"text":"t"}',
                       "enabled": True, "next_run_at": T0, "last_run_at": None, "last_status": "ok", "fail_count": 0})
    assert "user_id" not in v and "6" not in v.values()


# ---------- routes ----------
@pytest.fixture
def jobs(mem, monkeypatch):
    fake, _ = mem
    st = {"rows": [], "audit": [], "next": 1, "seen": []}

    async def lst(uid):
        st["seen"].append(uid)
        return [dict(r) for r in st["rows"] if r["user_id"] == uid]

    async def add(uid, kind, name, schedule, payload, nxt, max_jobs):
        if len([r for r in st["rows"] if r["user_id"] == uid]) >= max_jobs:
            return None
        r = {"id": st["next"], "user_id": uid, "kind": kind, "name": name, "schedule": schedule, "payload": payload, "enabled": True,
             "next_run_at": nxt, "last_run_at": None, "last_status": None, "fail_count": 0, "paused_at": None}
        st["next"] += 1
        st["rows"].append(r)
        return r["id"]

    async def upd(uid, jid, name, schedule, payload, nxt):
        for r in st["rows"]:
            if r["id"] == jid and r["user_id"] == uid:
                r.update(name=name, schedule=schedule, payload=payload, next_run_at=nxt)
                return True
        return False

    async def tog(uid, jid, enabled, nxt=None):
        for r in st["rows"]:
            if r["id"] == jid and r["user_id"] == uid:
                r["enabled"] = enabled
                return True
        return False

    async def dele(uid, jid):
        n = len(st["rows"])
        st["rows"] = [r for r in st["rows"] if not (r["id"] == jid and r["user_id"] == uid)]
        return len(st["rows"]) < n
    for n, f in (("dev_job_list", lst), ("dev_job_add", add), ("dev_job_update", upd), ("dev_job_set_enabled", tog), ("dev_job_delete", dele)):
        monkeypatch.setattr(fake, n, f, raising=False)
    from modules import admin_controls

    async def rec(admin_id, action, gid, details):
        st["audit"].append((admin_id, action, details))

    async def sw():
        return set()
    monkeypatch.setattr(admin_controls, "record_audit", rec)
    monkeypatch.setattr(admin_controls, "current_switches", sw)
    dash._owner_hits.clear()
    return st


DEV = "other"
DEV_UID = "7777777777"


def test_every_job_route_is_gated_by_the_plan(jobs):
    assert call("GET", {"action": "dev_jobs"})[0] == 402
    for body in ({"action": "dev_job_save", **good()}, {"action": "dev_job_toggle", "id": 1, "enabled": False}, {"action": "dev_job_delete", "id": 1}):
        assert call("POST", body=body)[0] == 402
    assert jobs["rows"] == [] and jobs["audit"] == []
    for token in (None,):
        assert call("GET", {"action": "dev_jobs"}, token=token)[0] == 401


def test_create_list_edit_pause_delete_are_session_keyed_and_audited(jobs):
    st, p, _ = call("POST", body={"action": "dev_job_save", "user_id": "6", **good()}, token=DEV)
    assert st == 200 and p["id"] == 1 and jobs["rows"][0]["user_id"] == DEV_UID            # body user id ignored
    st, p, _ = call("GET", {"action": "dev_jobs", "user_id": "6"}, token=DEV)
    assert st == 200 and p["max_jobs"] == 5 and len(p["jobs"]) == 1 and "user_id" not in str(p["jobs"])
    assert call("POST", body={"action": "dev_job_save", "id": 1, **good(name="Renamed")}, token=DEV)[0] == 200
    assert jobs["rows"][0]["name"] == "Renamed"
    assert call("POST", body={"action": "dev_job_toggle", "id": 1, "enabled": False}, token=DEV)[0] == 200 and jobs["rows"][0]["enabled"] is False
    assert call("POST", body={"action": "dev_job_delete", "id": 1}, token=DEV)[0] == 200 and jobs["rows"] == []
    assert [a[1] for a in jobs["audit"]] == ["dev_job_create", "dev_job_edit", "dev_job_pause", "dev_job_delete"]
    assert all(a[0] == int(DEV_UID) for a in jobs["audit"])


def test_per_user_cap_of_five(jobs):
    codes = [call("POST", body={"action": "dev_job_save", **good(name=f"j{i}")}, token=DEV) for i in range(6)]
    assert [c[0] for c in codes] == [200] * 5 + [422] and codes[5][1]["code"] == "job_limit" and len(jobs["rows"]) == 5


def test_cannot_touch_someone_elses_job_or_change_kind(jobs):
    jobs["rows"].append({"id": 99, "user_id": "6", "kind": "note", "name": "x", "schedule": "daily@09:00", "payload": "{}", "enabled": True,
                         "next_run_at": T0, "last_run_at": None, "last_status": None, "fail_count": 0, "paused_at": None})
    assert call("POST", body={"action": "dev_job_delete", "id": 99}, token=DEV)[0] == 404
    assert call("POST", body={"action": "dev_job_toggle", "id": 99, "enabled": False}, token=DEV)[0] == 404
    assert call("POST", body={"action": "dev_job_save", "id": 99, **good()}, token=DEV)[0] == 404
    assert len(jobs["rows"]) == 1
    call("POST", body={"action": "dev_job_save", **good()}, token=DEV)
    mine = [r for r in jobs["rows"] if r["user_id"] == DEV_UID][0]["id"]
    assert call("POST", body={"action": "dev_job_save", "id": mine, **good(kind="reminder")}, token=DEV)[0] == 404


@pytest.mark.parametrize("body", [good(schedule={"preset": "minutely"}), good(schedule="* * * * *"), good(kind="x"), good(text="")])
def test_invalid_jobs_are_rejected_before_any_write(jobs, body):
    assert call("POST", body={"action": "dev_job_save", **body}, token=DEV)[0] == 422 and jobs["rows"] == [] and jobs["audit"] == []


@pytest.mark.parametrize("b", [{"id": "1", "enabled": True}, {"id": True, "enabled": True}, {"id": 1, "enabled": "yes"}])
def test_toggle_and_delete_validate_types(jobs, b):
    assert call("POST", body={"action": "dev_job_toggle", **b}, token=DEV)[0] == 422


def test_job_writes_are_rate_limited(jobs):
    codes = [call("POST", body={"action": "dev_job_delete", "id": 1}, token=DEV)[0] for _ in range(21)]
    assert codes[20] == 429


def test_dev_handlers_for_jobs_start_with_require_dev():
    for name in ("dev_jobs_list", "dev_job_save", "dev_job_toggle", "dev_job_delete"):
        src = inspect.getsource(getattr(dash_dev, name))
        assert "gate = await require_dev(uid, db)" in src.split('"""')[-1][:80] or "require_dev" in src[:400]
    assert not (set(dash_dev.WRITES) & set(dash._owner_writes())) and "dev_jobs" not in dash._owner_routes()


# ---------- cron runner ----------
class CronDB:
    def __init__(self, rows, plan=True):
        self.rows, self.plan, self.log = rows, plan, []

    async def dev_job_claim(self, limit, lease_minutes=60):
        self.log.append("claim")
        return self.rows

    async def dev_job_advance(self, jid, nxt):
        self.log.append(("advance", jid, nxt))

    async def entitlements_list(self, uid):
        self.log.append(("ent", uid))
        return [{"product": "dev_monthly", "status": "active", "expires_at": datetime.now(UTC) + timedelta(days=5)}] if self.plan else []

    async def dev_job_stop(self, jid, status, plan_lapsed=False):
        self.log.append(("stop", jid, status, plan_lapsed))

    async def dev_job_finish(self, jid, status, ok, max_fails=5, count_failure=True):
        self.log.append(("finish", jid, status, ok, count_failure))


def job(i=1, kind="reminder", payload='{"text":"hi"}', sched="hourly@00", slot=None):
    return {"id": i, "user_id": "7777777777", "kind": kind, "name": "J", "schedule": sched, "payload": payload,
            "slot": slot or datetime.now(UTC) - timedelta(minutes=2)}


@pytest.fixture
def runner(monkeypatch):
    def make(rows, plan=True, switches=(), dm=None, export=None, chat=None):
        fake = CronDB(rows, plan)
        monkeypatch.setattr(cron, "db", fake)
        monkeypatch.setattr(cron, "DISCORD_BOT_TOKEN", "x")
        monkeypatch.setattr(cron, "JOB_DELAY_SECONDS", 0)

        async def sw():
            return set(switches)
        from modules import admin_controls
        monkeypatch.setattr(admin_controls, "current_switches", sw)

        async def _dm(session, token, uid, text, *a):
            fake.log.append(("dm", uid, text))
            return dm
        import api.cron_discord_owner_broadcast as b
        monkeypatch.setattr(b, "_dm_user", _dm)

        async def _exp(uid, body, db_):
            fake.log.append(("export", uid, body["name"]))
            return export or {"export": {}}

        async def _chat(uid, body, db_):
            fake.log.append(("chat", uid, body["model"]))
            return chat or {"reply": "answer"}
        monkeypatch.setattr(dash_dev, "dev_export_create", _exp)
        monkeypatch.setattr(dash_dev, "dev_chat_send", _chat)
        return fake
    return make


def run():
    return asyncio.run(cron.run_due_jobs())


def kinds(fake):
    return [e if isinstance(e, str) else e[0] for e in fake.log]


def test_advances_next_run_before_running_and_runs_reminder(runner):
    fake = runner([job()])
    out = run()
    assert kinds(fake)[:4] == ["claim", "advance", "ent", "dm"] and out["ok"] == 1
    adv = [e for e in fake.log if e[0] == "advance"][0]
    assert adv[2] > datetime.now(UTC)
    assert fake.log[-1] == ("finish", 1, "ok", True, False)


def test_missed_slots_are_not_replayed(runner):
    fake = runner([job(slot=datetime.now(UTC) - timedelta(days=3))])
    run()
    assert [e for e in fake.log if e[0] == "advance"][0][2] > datetime.now(UTC)


def test_lapsed_plan_pauses_and_never_runs(runner):
    fake = runner([job(kind="ai_prompt", payload='{"prompt":"p","model":"default"}')], plan=False)
    out = run()
    assert ("stop", 1, "plan_lapsed", True) in fake.log and out["paused"] == 1
    assert not any(e[0] in ("dm", "chat", "export") for e in fake.log if isinstance(e, tuple))


def test_owner_kill_switch_claims_nothing(runner):
    fake = runner([job()], switches={"dev_jobs"})
    assert run()["skipped"] == "switched_off" and fake.log == []


def test_note_and_ai_prompt_export_and_ai_spends_through_dash_code(runner):
    fake = runner([job(1, "note", '{"text":"t"}'), job(2, "ai_prompt", '{"prompt":"p","model":"groq"}')])
    out = run()
    assert [e[:2] for e in fake.log if e[0] in ("export", "chat")] == [("export", "7777777777"), ("chat", "7777777777"), ("export", "7777777777")]
    assert ("chat", "7777777777", "groq") in fake.log and out["ok"] == 2


def test_allowance_used_stops_the_job_without_exporting_or_counting_a_failure(runner):
    fake = runner([job(kind="ai_prompt", payload='{"prompt":"p","model":"default"}')], chat={"_status": 429, "code": "weekly_limit", "message": "m"})
    out = run()
    assert ("stop", 1, "allowance_used", False) in fake.log and out["stopped"] == 1
    assert not any(e[0] == "export" for e in fake.log if isinstance(e, tuple))


def test_failures_count_but_owner_switches_do_not(runner):
    fake = runner([job(kind="note", payload='{"text":"t"}')], export={"_status": 502, "message": "boom"})
    run()
    assert fake.log[-1] == ("finish", 1, "error_502", False, True)
    fake = runner([job(kind="note", payload='{"text":"t"}')], export={"_status": 503, "message": "off"})
    run()
    assert fake.log[-1] == ("finish", 1, "switched_off", False, False)
    fake = runner([job()], dm="open_dm_failed:403")
    run()
    assert fake.log[-1] == ("finish", 1, "dm_failed", False, True)


def test_corrupt_schedule_is_stopped_and_a_crash_is_a_failure_not_a_loop(runner, monkeypatch):
    fake = runner([job(sched="* * * * *")])
    assert run()["stopped"] == 1 and ("stop", 1, "bad_schedule", False) in fake.log
    fake = runner([job()])

    async def boom(*a, **k):
        raise ValueError("secret-in-message")
    monkeypatch.setattr(cron, "run_job", boom)
    run()
    assert fake.log[-1] == ("finish", 1, "crashed", False, True)


def test_wall_clock_budget_skips_the_rest(runner, monkeypatch):
    monkeypatch.setattr(cron, "WALL_CLOCK_BUDGET_SECONDS", -1)
    fake = runner([job(1), job(2)])
    out = run()
    assert out["ran"] == 0 and kinds(fake).count("advance") == 2 and "dm" not in kinds(fake)


def test_classify_never_copies_messages():
    assert cron.classify({}) == (True, "ok", False)
    assert cron.classify({"_status": 429, "code": "weekly_limit"}) == (False, "allowance_used", False)
    assert cron.classify({"_status": 502, "message": "key sk-123"}) == (False, "error_502", True)


# ---------- cron endpoint auth ----------
def _call(secret_header=None, path="/api/cron_dev_scheduled"):
    h = cron.handler.__new__(cron.handler)
    h.headers = {"Authorization": secret_header} if secret_header else {}
    h.path = path
    out = {}
    h._send = lambda code, payload: out.update(code=code, payload=payload)
    h._handle()
    return out


def test_endpoint_rejects_missing_or_wrong_secret(monkeypatch):
    monkeypatch.setattr(cron, "CRON_SECRET", "s3cret")

    async def never():
        raise AssertionError("must not run")
    monkeypatch.setattr(cron, "run_due_jobs", never)
    assert _call()["code"] == 401 and _call("Bearer nope")["code"] == 401 and _call(path="/x?secret=bad")["code"] == 401
    monkeypatch.setattr(cron, "CRON_SECRET", "")
    assert _call("Bearer ")["code"] == 401


def test_endpoint_runs_with_the_right_secret(monkeypatch):
    monkeypatch.setattr(cron, "CRON_SECRET", "s3cret")

    async def fake():
        return {"ran": 0}
    monkeypatch.setattr(cron, "run_due_jobs", fake)
    assert _call("Bearer s3cret")["code"] == 200 and _call(path="/x?secret=s3cret")["code"] == 200


# ---------- database / wiring (static) ----------
def test_claim_is_atomic_and_lease_based():
    src = inspect.getsource(database.Database.dev_job_claim)
    assert "FOR UPDATE SKIP LOCKED" in src and "next_run_at <= NOW()" in src and "enabled" in src and "RETURNING" in src
    assert "next_run_at = NOW() +" in src                       # the lease: a second tick can't see it as due
    assert "WHERE (SELECT COUNT(*)" in inspect.getsource(database.Database.dev_job_add)      # cap enforced inside the INSERT


def test_five_consecutive_failures_switch_a_job_off_and_success_resets():
    src = inspect.getsource(database.Database.dev_job_finish)
    assert "fail_count + 1 >= $3 THEN FALSE" in src and "fail_count = 0" in src and J.MAX_FAILS == 5


def test_paused_jobs_are_purged_after_30_days_by_the_existing_daily_job():
    assert "paused_at < NOW()" in inspect.getsource(database.Database.dev_jobs_purge_paused) and J.PAUSED_KEEP_DAYS == 30
    assert "dev_jobs_purge_paused" in Path("api/cron_expire_monetization.py").read_text()


def test_delete_my_data_removes_scheduled_jobs():
    src = Path("database.py").read_text()
    seg = src[src.index("async def member_data_delete"):src.index("async def card_asset_blocked")]
    assert "DELETE FROM dev_jobs WHERE user_id = $1" in seg


def test_schema_has_dev_jobs_and_the_kill_switch_exists():
    src = Path("database.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS dev_jobs" in src and "dev_jobs_due_idx" in src
    from modules import admin_controls as ac
    assert "dev_jobs" in ac.FEATURES and "dev_jobs" in ac.WEBSITE_ONLY


def test_no_secret_handling_in_the_jobs_code():
    for f in ("modules/dev_jobs.py", "api/cron_dev_scheduled.py"):
        t = Path(f).read_text()
        assert "secret_manager" not in t and "decrypt" not in t and "dev_connection_secret" not in t, f
    assert "logger.error(\"[dev jobs] job %s crashed: %s\", job[\"id\"], type(e).__name__)" in Path("api/cron_dev_scheduled.py").read_text()


def test_worker_calls_the_new_endpoint_every_five_minutes_and_it_is_registered():
    js = Path("cron-worker/src/index.js").read_text()
    frequent = js[js.index("frequent:"):js.index("],", js.index("frequent:"))]
    assert "cron_dev_scheduled" in frequent and '"*/5 * * * *": "frequent"' in js
    assert js.count("cron_dev_scheduled") == 1                                  # no second trigger or worker
    assert '"/api/cron_dev_scheduled": "api.cron_dev_scheduled"' in Path("api_server.py").read_text()
    assert "cron_dev_scheduled" in Path("cron-worker/README.md").read_text()
    assert "[triggers]" in Path("cron-worker/wrangler.toml").read_text() and "cron_dev" not in Path("cron-worker/wrangler.toml").read_text()


def test_front_end_has_the_jobs_section_and_no_innerhtml():
    js = Path("dashboard/assets/dash.js").read_text()
    seg = js[js.index("function devJobs"):js.index("function devGithub")]
    assert ".innerHTML" not in seg and "dev_job_save" in seg and "dev_job_toggle" in seg and "dev_job_delete" in seg
    assert "devJobs()" in js
