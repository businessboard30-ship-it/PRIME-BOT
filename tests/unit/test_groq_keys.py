"""Groq failover keys: primary, backup and backup2, de-duplicated, blanks ignored."""
import importlib


def _keys(monkeypatch, **env):
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_BACKUP", "GROQ_API_KEY_BACKUP2", "GROQ_API_KEYS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import groq_service
    return importlib.reload(groq_service).GROQ_API_KEYS


def test_three_keys_in_order(monkeypatch):
    assert _keys(monkeypatch, GROQ_API_KEY="a", GROQ_API_KEY_BACKUP="b", GROQ_API_KEY_BACKUP2="c") == ["a", "b", "c"]


def test_missing_and_duplicate_keys_are_dropped(monkeypatch):
    assert _keys(monkeypatch, GROQ_API_KEY="a", GROQ_API_KEY_BACKUP2="a") == ["a"]
    assert _keys(monkeypatch, GROQ_API_KEY="a", GROQ_API_KEY_BACKUP2="c") == ["a", "c"]


def test_extra_comma_separated_keys(monkeypatch):
    assert _keys(monkeypatch, GROQ_API_KEY="a", GROQ_API_KEY_BACKUP="b", GROQ_API_KEYS="x, y\nz,a") == ["a", "b", "x", "y", "z"]


def test_rotation_and_cooldown(monkeypatch):
    from modules import groq_keys as gk
    gk._cooling.clear()
    keys = ["a", "b", "c"]
    firsts = {gk.ordered_keys(keys)[0] for _ in range(6)}
    assert firsts == {"a", "b", "c"}                      # traffic is spread, not all on the first key
    gk.mark_failed("a", 429)
    for _ in range(6):
        order = gk.ordered_keys(keys)
        assert order[-1] == "a" and set(order) == set(keys)   # cooling key goes last but is still tried
    gk.mark_ok("a")
    assert "a" in {gk.ordered_keys(keys)[0] for _ in range(6)}
