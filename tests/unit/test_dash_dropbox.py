import pytest

import config
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call  # noqa: F401  (fixture + helper reuse)


@pytest.fixture
def box(env, monkeypatch):
    fake, _ = env
    fake.msgs, fake.reads, fake.next_id = {}, set(), 1

    async def create(title, body, kind, announce, created_by, expires_hours=None, audience="all", min_members=None, target_guild_id=None, push_dm=False):
        i = fake.next_id; fake.next_id += 1
        fake.msgs[i] = dict(id=str(i), title=title, body=body, kind=kind, announce=announce,
                            created_at="2026-01-01T00:00:00+00:00", by=created_by, hours=expires_hours,
                            audience=audience, min_members=min_members, guild=target_guild_id, push=push_dm)
        return i
    async def lst(uid, limit=50):
        return [dict(m, read=(m["id"], uid) in fake.reads) for m in sorted(fake.msgs.values(), key=lambda m: -int(m["id"]))]
    async def mark(uid, mid=None):
        for m in fake.msgs.values():
            if mid is None or int(m["id"]) == mid:
                fake.reads.add((m["id"], uid))
    async def delete(mid):
        return fake.msgs.pop(mid, None) is not None
    fake.dropbox_create, fake.dropbox_list, fake.dropbox_mark_read, fake.dropbox_delete = create, lst, mark, delete
    fake.owners, fake.queued, fake.resolve_args = [1, 2, 3], [], []
    async def resolve(audience, min_members=None, target_guild_id=None):
        fake.resolve_args.append((audience, min_members, target_guild_id))
        return list(fake.owners)
    async def enqueue(mid, users, push):
        fake.queued.append((mid, list(users), push)); return len(users)
    async def delivery(mid):
        return {"sent": 2, "failed": 1, "pending": 0, "reads": 4}
    fake.dropbox_resolve_recipients, fake.dropbox_enqueue, fake.dropbox_delivery = resolve, enqueue, delivery
    fake.sessions["owner"] = dict(fake.sessions["sid"], user={"id": "77", "username": "o", "avatar_url": ""})
    monkeypatch.setattr(config, "DISCORD_OWNER_BROADCAST_IDS", {77})
    return fake


MSG = {"action": "dropbox_send", "title": "Maintenance", "body": "Tonight 10pm.", "kind": "maintenance", "announce": True}


def test_validate_dropbox():
    ok, err = S.validate_dropbox(dict(MSG, expires_hours=24))
    assert err is None and ok["expires_hours"] == 24 and ok["announce"] is True
    assert S.validate_dropbox(dict(MSG, title="  "))[1]
    assert S.validate_dropbox(dict(MSG, body="x" * 2001))[1]
    assert S.validate_dropbox(dict(MSG, kind="evil"))[1]
    assert S.validate_dropbox(dict(MSG, expires_hours=0))[1]
    assert S.validate_dropbox(dict(MSG, expires_hours=True))[1]
    assert S.validate_dropbox("nope")[1]


def test_only_owner_can_send_or_delete(box):
    assert call("POST", token="sid", body=MSG)[0] == 403
    assert call("POST", token="sid", body={"action": "dropbox_delete", "id": "1"})[0] == 403
    assert not box.msgs
    assert call("POST", token="owner", body=MSG)[0] == 200 and len(box.msgs) == 1
    assert call("POST", token="owner", body=dict(MSG, title=""))[0] == 422


def test_admins_see_unread_and_mark_read(box):
    call("POST", token="owner", body=MSG)
    st, p, _ = call("GET", {"action": "me"}, token="sid")
    assert p["unread"] == 1 and p["is_owner"] is False
    st, p, _ = call("GET", {"action": "dropbox"}, token="sid")
    assert st == 200 and p["messages"][0]["title"] == "Maintenance" and p["unread"] == 1
    assert call("POST", token="sid", body={"action": "dropbox_read", "id": "1"})[0] == 200
    assert call("GET", {"action": "dropbox"}, token="sid")[1]["unread"] == 0
    assert call("GET", {"action": "dropbox"}, token="owner")[1]["unread"] == 1   # read state is per user
    assert call("POST", token="sid", body={"action": "dropbox_read", "id": "abc"})[0] == 400


def test_mark_all_read_and_delete(box):
    call("POST", token="owner", body=MSG); call("POST", token="owner", body=MSG)
    call("POST", token="sid", body={"action": "dropbox_read"})
    assert call("GET", {"action": "dropbox"}, token="sid")[1]["unread"] == 0
    assert call("POST", token="owner", body={"action": "dropbox_delete", "id": "1"})[0] == 200
    assert call("POST", token="owner", body={"action": "dropbox_delete", "id": "1"})[0] == 404


def test_dropbox_needs_session(box):
    assert call("GET", {"action": "dropbox"}, token=None)[0] == 401


def test_validate_targeting():
    ok, err = S.validate_dropbox(dict(MSG, audience="premium", push_dm=True))
    assert err is None and ok["audience"] == "premium" and ok["push_dm"] is True
    ok, err = S.validate_dropbox(dict(MSG, audience="min_members", min_members=500))
    assert err is None and ok["min_members"] == 500
    assert S.validate_dropbox(dict(MSG, audience="min_members"))[1]
    assert S.validate_dropbox(dict(MSG, audience="min_members", min_members=0))[1]
    assert S.validate_dropbox(dict(MSG, audience="min_members", min_members=True))[1]
    ok, err = S.validate_dropbox(dict(MSG, audience="guild", target_guild_id="123456789012345678"))
    assert err is None and ok["target_guild_id"] == 123456789012345678
    assert S.validate_dropbox(dict(MSG, audience="guild", target_guild_id="abc"))[1]
    assert S.validate_dropbox(dict(MSG, audience="guild"))[1]
    assert S.validate_dropbox(dict(MSG, audience="everyone"))[1]
    assert S.validate_dropbox(MSG)[0]["audience"] == "all"


def test_dm_text_fits_discord_limit():
    t = S.format_dropbox_dm("T" * 100, "x" * 2000, "warning")
    assert len(t) <= 2000 and t.startswith("\u26a0\ufe0f **")


def test_plain_message_does_not_queue_anything(box):
    st, p, _ = call("POST", token="owner", body=MSG)
    assert st == 200 and p["recipients"] == 0 and not box.queued and not box.resolve_args


def test_push_and_targeting_queue_recipients(box):
    st, p, _ = call("POST", token="owner", body=dict(MSG, audience="premium", push_dm=True))
    assert st == 200 and p["recipients"] == 3
    assert box.queued == [(1, [1, 2, 3], True)] and box.resolve_args == [("premium", None, None)]
    call("POST", token="owner", body=dict(MSG, audience="min_members", min_members=100))
    assert box.queued[-1][2] is False                      # targeted but not pushed: visible in the panel only
    assert box.resolve_args[-1] == ("min_members", 100, None)


def test_targeted_send_with_no_matching_servers_is_rejected(box):
    box.owners = []
    st, _, _ = call("POST", token="owner", body=dict(MSG, audience="guild", target_guild_id="123456789012345678"))
    assert st == 422 and not box.msgs                      # nothing is created if nobody would get it


def test_delivery_stats_are_owner_only(box):
    assert call("GET", {"action": "dropbox_delivery", "id": "1"}, token="sid")[0] == 403
    st, p, _ = call("GET", {"action": "dropbox_delivery", "id": "1"}, token="owner")
    assert st == 200 and p["sent"] == 2 and p["failed"] == 1 and p["reads"] == 4
    assert call("GET", {"action": "dropbox_delivery", "id": "x"}, token="owner")[0] == 400
