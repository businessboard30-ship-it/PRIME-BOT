"""Developer mode C2: export to the private storage channel. Gate + 7-day grace, encrypted upload, opaque names,
caps, kill switch, per-user isolation, safe errors."""
import importlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api import dash, dash_dev
from modules import dev_export
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  ("sid" = user 6 card plan only; "other" = 7777777777 dev_monthly)

CHANNEL = 1541141079913660446
SECRET_TEXT = "my private chat about project Zebra"
OTHER = "7777777777"


class FakeForm:
    pass


@pytest.fixture
def ex(mem, monkeypatch):
    fake, seen = mem
    st = {"receipts": {}, "uploads": [], "dms": [], "deleted": [], "switches": set(), "post_fail": False, "dm_fail": False,
          "cdn": {}, "n": 0, "audit": []}

    async def count_since(uid, hours=24):
        return sum(1 for r in st["receipts"].values() if r["user_id"] == uid)

    async def count(uid):
        return sum(1 for r in st["receipts"].values() if r["user_id"] == uid)

    async def add(eid, uid, mid, name, size):
        st["receipts"][eid] = {"id": eid, "user_id": uid, "message_id": mid, "name": name, "size_bytes": size,
                               "created_at": datetime.now(timezone.utc)}

    async def lst(uid, limit=50):
        return [{k: v for k, v in r.items() if k not in ("user_id", "message_id")} for r in st["receipts"].values() if r["user_id"] == uid]

    async def get(uid, eid):
        r = st["receipts"].get(eid)
        return dict(r) if r and r["user_id"] == uid else None

    async def delete(uid, eid):
        r = st["receipts"].get(eid)
        if r and r["user_id"] == uid:
            del st["receipts"][eid]
            return True
        return False

    async def audit(*a, **k):
        st["audit"].append((a, k))
        return 1

    async def bot_post(path, json_body=None, form=None):
        if path == "/users/@me/channels":
            if st["dm_fail"]:
                raise dash.DiscordError(403)
            return {"id": "dm1"}
        if st["post_fail"] and path.startswith(f"/channels/{CHANNEL}"):
            raise dash.DiscordError(403)
        if path.startswith("/channels/dm1"):
            if st["dm_fail"]:
                raise dash.DiscordError(403)
            st["dms"].append(form)
            return {"id": "dmmsg"}
        st["n"] += 1
        mid = str(900 + st["n"])
        st["uploads"].append((path, form))
        return {"id": mid}

    async def bot_get(path):
        mid = path.rsplit("/", 1)[1]
        for r in st["receipts"].values():
            if r["message_id"] == mid:
                return {"attachments": [{"filename": dev_export.opaque_filename(r["id"]), "url": f"https://cdn.test/{r['id']}"}]}
        raise dash.DiscordError(404)

    async def bot_request(method, path, reason=None, json_body=None):
        st["deleted"].append((method, path))
        return 204

    async def fetch(url, limit):
        return st["cdn"].get(url.rsplit("/", 1)[1])

    async def switches():
        return set(st["switches"])

    for n, f in (("dev_export_count_since", count_since), ("dev_export_count", count), ("dev_export_add", add),
                 ("dev_export_list", lst), ("dev_export_get", get), ("dev_export_delete", delete), ("owner_audit_add", audit)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(dash, "_bot_post", bot_post)
    monkeypatch.setattr(dash, "_bot_get", bot_get)
    monkeypatch.setattr(dash, "_bot_request", bot_request)
    monkeypatch.setattr(dash_dev, "_fetch_bytes", fetch)
    monkeypatch.setattr(dash_dev, "_upload_form", lambda payload, filename, data, mime: {"payload": payload, "filename": filename, "data": data, "mime": mime})
    monkeypatch.setattr(dash_dev, "_storage_channel", lambda: CHANNEL)
    ac = importlib.import_module("modules.admin_controls")
    monkeypatch.setattr(ac, "current_switches", switches)
    dash._owner_hits.clear()
    st["fake"] = fake
    return st


def create(token="other", **extra):
    body = {"action": "dev_export_create", "kind": "note", "name": "my chat", "content": SECRET_TEXT}
    body.update(extra)
    return call("POST", None, token=token, body=body)


def finish_cdn(st):
    """Pretend the CDN serves what the bot uploaded."""
    for path, form in st["uploads"]:
        eid = form["filename"][:-4]
        st["cdn"][eid] = form["data"]


# ---------- gate and grace ----------
def test_export_is_gated_for_people_without_a_dev_plan(ex):
    for st_, p, _ in (create(token="sid"), call("GET", {"action": "dev_export_list"}, token="sid"),
                      call("GET", {"action": "dev_export_download", "id": "a" * 32}, token="sid"),
                      call("POST", None, token="sid", body={"action": "dev_export_delete", "id": "a" * 32})):
        assert st_ == 402 and p["code"] == "subscription_required"
    assert ex["uploads"] == []


def test_export_works_for_seven_days_after_the_plan_ends_but_not_longer(ex):
    fake = ex["fake"]

    def plan(days_ago):
        async def lst(uid):
            return [{"product": "dev_monthly", "status": "expired", "expires_at": datetime.now(timezone.utc) - timedelta(days=days_ago)}]
        fake.entitlements_list = lst
    plan(3)
    assert create()[0] == 200                                                     # inside the grace window
    assert call("GET", {"action": "dev_status"}, token="other")[1]["unlocked"] is False
    assert call("POST", None, token="other", body={"action": "dev_chat", "messages": [{"role": "user", "content": "x"}]})[0] == 402
    plan(8)
    dash._owner_hits.clear()
    assert create()[0] == 402                                                     # grace over


# ---------- upload is ciphertext with an opaque name ----------
def test_upload_is_encrypted_and_opaquely_named(ex):
    st, p, _ = create()
    assert st == 200 and p["export"]["name"] == "my chat.md"
    path, form = ex["uploads"][0]
    assert path == f"/channels/{CHANNEL}/messages"
    assert SECRET_TEXT.encode() not in form["data"] and form["data"] != SECRET_TEXT.encode()
    eid = p["export"]["id"]
    assert form["filename"] == eid + ".bin" and "my chat" not in json.dumps(form["payload"]) and OTHER not in json.dumps(form["payload"])
    assert form["payload"]["allowed_mentions"] == {"parse": []}
    assert dev_export.decrypt(form["data"]) == SECRET_TEXT.encode()


def test_receipt_is_minimal_and_audited(ex):
    create()
    (row,) = ex["receipts"].values()
    assert set(row) == {"id", "user_id", "message_id", "name", "size_bytes", "created_at"} and row["user_id"] == OTHER
    assert SECRET_TEXT not in json.dumps(row, default=str)
    assert ex["audit"] and ex["audit"][0][0][2] == "dev_export"


def test_dm_gets_the_plain_file_and_failure_does_not_fail_the_export(ex):
    st, p, _ = create()
    assert p["dm_sent"] is True and ex["dms"][0]["data"] == SECRET_TEXT.encode()
    ex["dm_fail"] = True
    dash._owner_hits.clear()
    st, p, _ = create()
    assert st == 200 and p["dm_sent"] is False and len(ex["receipts"]) == 2


def test_storage_failure_is_a_safe_502_and_no_receipt(ex):
    ex["post_fail"] = True
    st, p, _ = create()
    assert st == 502 and ex["receipts"] == {} and "403" not in p["message"] and "discord" not in p["message"].lower()


# ---------- download and delete ----------
def test_download_round_trips_the_plain_text_for_the_owner_only(ex):
    eid = create()[1]["export"]["id"]
    finish_cdn(ex)
    st, p, _ = call("GET", {"action": "dev_export_download", "id": eid}, token="other")
    assert st == 200 and p["content"] == SECRET_TEXT and p["name"] == "my chat.md"
    assert "message_id" not in json.dumps(p)


def test_nobody_can_download_or_delete_someone_elses_export(ex):
    eid = create()[1]["export"]["id"]
    finish_cdn(ex)
    fake = ex["fake"]
    fake.sessions["third"] = {"kind": "dash", "user": {"id": "8888888888", "username": "t", "avatar_url": ""}, "guilds": []}
    orig = fake.entitlements_list

    async def lst(uid):
        return await orig("7777777777") if uid == "8888888888" else await orig(uid)
    fake.entitlements_list = lst                     # third user also has a dev plan
    assert call("GET", {"action": "dev_export_download", "id": eid}, token="third")[0] == 404
    assert call("POST", None, token="third", body={"action": "dev_export_delete", "id": eid})[0] == 404
    assert eid in ex["receipts"] and ex["deleted"] == []
    assert call("GET", {"action": "dev_export_list"}, token="third")[1]["exports"] == []


def test_tampered_or_missing_ciphertext_is_410_not_garbage(ex):
    eid = create()[1]["export"]["id"]
    assert call("GET", {"action": "dev_export_download", "id": eid}, token="other")[0] == 410      # CDN has nothing
    ex["cdn"][eid] = b"not our ciphertext"
    assert call("GET", {"action": "dev_export_download", "id": eid}, token="other")[0] == 410


def test_bad_ids_are_rejected(ex):
    for bad in ("", "x", "../../etc", "A" * 32, "a" * 31):
        assert call("GET", {"action": "dev_export_download", "id": bad}, token="other")[0] == 422
    assert call("POST", None, token="other", body={"action": "dev_export_delete", "id": 5})[0] == 422


def test_delete_removes_the_storage_message_and_the_receipt(ex):
    eid = create()[1]["export"]["id"]
    st, p, _ = call("POST", None, token="other", body={"action": "dev_export_delete", "id": eid})
    assert st == 200 and ex["receipts"] == {} and ex["deleted"][0][0] == "DELETE" and ex["deleted"][0][1].startswith(f"/channels/{CHANNEL}/messages/")


# ---------- caps, kill switch, validation ----------
def test_kill_switch_stops_new_exports_only(ex):
    create()
    ex["switches"].add("dev_export")
    dash._owner_hits.clear()
    st, p, _ = create()
    assert st == 503 and len(ex["uploads"]) == 1
    assert call("GET", {"action": "dev_export_list"}, token="other")[0] == 200


def test_daily_limit_and_kept_limit(ex, monkeypatch):
    monkeypatch.setattr(dev_export, "MAX_PER_DAY", 2)
    assert create()[0] == 200
    assert create()[0] == 200
    st, p, _ = create()
    assert st == 429 and p["code"] == "daily_limit" and len(ex["uploads"]) == 2


def test_size_and_content_validation(ex):
    assert create(content="")[0] == 422
    assert create(content=None)[0] == 422
    assert create(content="x" * (dev_export.MAX_BYTES + 1))[0] == 422
    assert create(content="a\x00b")[0] == 422
    assert create(kind="exe")[0] == 422
    assert create(kind="json", content="{not json")[0] == 422
    dash._owner_hits.clear()
    assert create(kind="json", content='{"a": 1}')[0] == 200
    assert len(ex["uploads"]) == 1


def test_export_is_rate_limited(ex):
    codes = [create()[0] for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429


def test_client_cannot_pick_the_user_or_the_channel(ex):
    create(user_id="6", uid="6", channel_id="123", channel=5)
    assert all(r["user_id"] == OTHER for r in ex["receipts"].values())
    assert all(path.startswith(f"/channels/{CHANNEL}/") for path, _ in ex["uploads"])


# ---------- pure helpers ----------
def test_clean_name_blocks_paths_and_odd_characters():
    assert dev_export.clean_name("../../etc/passwd", ".md") == "etc passwd.md"
    assert dev_export.clean_name("", ".txt") == "export.txt"
    assert dev_export.clean_name("note.md", ".md") == "note.md"
    assert dev_export.clean_name(".hidden", ".md") == "hidden.md"
    assert dev_export.clean_name("caf\u00e9 \u202e evil", ".md").isascii()
    assert len(dev_export.clean_name("a" * 500, ".md")) <= dev_export.NAME_MAX + 3
    assert dev_export.clean_name("x" * 5 + "\n\r\t", ".md") == "xxxxx.md"


def test_ids_and_encryption_helpers():
    a, b = dev_export.new_export_id(), dev_export.new_export_id()
    assert a != b and dev_export.valid_export_id(a) and not dev_export.valid_export_id(a.upper())
    blob = dev_export.encrypt(b"hello")
    assert b"hello" not in blob and dev_export.decrypt(blob) == b"hello" and dev_export.decrypt(blob[:-3] + b"xxx") is None


def test_public_view_never_includes_the_storage_message_id():
    v = dev_export.public_view({"id": "i", "message_id": "999", "name": "n", "size_bytes": 5, "created_at": None})
    assert "999" not in json.dumps(v) and set(v) == {"id", "name", "size", "created_at"}


def test_list_query_never_selects_the_message_id_and_config_has_the_channel():
    import config
    src = inspect.getsource(importlib.import_module("database").Database.dev_export_list)
    assert "message_id" not in src
    assert config.DEV_STORAGE_CHANNEL_ID == CHANNEL


def test_front_end_export_ui_has_no_innerhtml():
    js = Path("dashboard/assets/dash.js").read_text()
    assert ".innerHTML" not in js and "dev_export_create" in js and "Save chat" in js
