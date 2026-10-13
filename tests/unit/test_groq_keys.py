"""Groq failover keys: primary, backup and backup2, de-duplicated, blanks ignored."""
import importlib


def _keys(monkeypatch, **env):
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_BACKUP", "GROQ_API_KEY_BACKUP2"):
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
