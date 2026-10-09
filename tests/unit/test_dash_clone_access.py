"""Clone bots must never be silently left out of the dashboard server list."""
import asyncio

from api import dash


def _clones(n, owner="42", start=1):
    return [{"clone_id": start + i, "bot_username": f"c{start + i}", "owner_id": owner} for i in range(n)]


def test_own_clones_are_scanned_first_and_never_capped():
    others = _clones(dash.MAX_CLONE_SCAN + 50, owner="1", start=1)          # more than the cap, listed first
    mine = _clones(5, owner="42", start=10_000)
    order = dash._scan_order(others + mine, 42)
    assert [c["clone_id"] for c in order[:5]] == [c["clone_id"] for c in mine]
    assert len(order) == 5 + dash.MAX_CLONE_SCAN


def test_a_user_owning_more_than_the_cap_sees_every_one_of_their_clones():
    mine = _clones(dash.MAX_CLONE_SCAN + 25, owner="42")
    assert len(dash._scan_order(mine, "42")) == len(mine)


def test_no_uid_falls_back_to_the_cap():
    assert len(dash._scan_order(_clones(dash.MAX_CLONE_SCAN + 30), None)) == dash.MAX_CLONE_SCAN


def test_clone_presence_includes_a_late_clone_owned_by_the_user(monkeypatch):
    listed = _clones(dash.MAX_CLONE_SCAN + 10, owner="1") + _clones(1, owner="42", start=9999)

    async def lst():
        return listed

    async def row(cid):
        return {"clone_id": cid}

    async def ids(r):
        return {"555"} if r["clone_id"] == 9999 else set()
    monkeypatch.setattr(dash.db, "list_active_discord_clones", lst)
    monkeypatch.setattr(dash, "_clone_row", row)
    monkeypatch.setattr(dash, "_clone_guild_ids", ids)
    out = asyncio.run(dash._clone_presence({"555"}, 42))
    assert out == {"555": [{"clone_id": 9999, "name": "c9999"}]}


def test_a_failing_clone_is_logged_not_swallowed(monkeypatch, caplog):
    async def boom():
        raise RuntimeError("secret-token-xyz")
    monkeypatch.setattr(dash, "_bot_guild_ids", boom)
    monkeypatch.setattr(dash, "_clone_token", lambda row: "secret-token-xyz")
    with caplog.at_level("WARNING"):
        assert asyncio.run(dash._clone_guild_ids({"clone_id": 7})) == set()
    assert "clone #7" in caplog.text and "secret-token-xyz" not in caplog.text
