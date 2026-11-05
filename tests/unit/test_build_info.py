from modules import build_info as b


def test_uses_railway_variables(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234567890")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_MESSAGE", "fix(ai): thing\n\nbody")
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "deadbeefcafe")
    i = b.info()
    assert i == {"sha": "abcdef1", "message": "fix(ai): thing", "branch": "main", "deployment": "deadbeef"}
    s = b.summary("main bot")
    assert "abcdef1" in s and "main bot" in s and "Up for" in s


def test_never_raises_without_any_info(monkeypatch):
    for k in ("RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT_MESSAGE", "RAILWAY_GIT_BRANCH", "RAILWAY_DEPLOYMENT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(b, "_git", lambda *a: "")
    assert b.info()["sha"] == "unknown" and "Railway's git variables are missing" in b.summary()


def test_feature_markers_prove_recent_code_is_loaded():
    f = b.features()
    assert f["AI act-don't-ask rule (#549/#550)"] is True
    assert f["AI second turn (#551)"] is True


def test_uptime_text():
    assert b.uptime_text(b.STARTED_AT + 125) == "2m 5s" and b.uptime_text(b.STARTED_AT + 7500) == "2h 5m"


def test_log_channel_wiring():
    src = open("discord_bot/cogs/log_channel.py").read()
    assert "build_info.summary" in src and "message.channel.id != target" in src and "message.author.bot" in src
