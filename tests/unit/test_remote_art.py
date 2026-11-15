import os

from modules import remote_art


def test_local_file_wins(tmp_path, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(remote_art, "_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download")))
    assert remote_art.resolve_art(str(f), "tier") == str(f)


def test_downloads_once_then_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_art, "ART_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(remote_art, "_failed_at", {})
    calls = []

    def fake(url, dest):
        calls.append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(b"png")
        return True

    monkeypatch.setattr(remote_art, "_download", fake)
    missing = str(tmp_path / "gone" / "tier01.png")
    p1 = remote_art.resolve_art(missing, "tier")
    p2 = remote_art.resolve_art(missing, "tier")
    assert p1 == p2 and p1.endswith(os.path.join("tier", "tier01.png"))
    assert len(calls) == 1 and calls[0].endswith("/tier/tier01.png")


def test_failure_falls_back_and_backs_off(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_art, "ART_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(remote_art, "_failed_at", {})
    calls = []
    monkeypatch.setattr(remote_art, "_download", lambda u, d: calls.append(u) or False)
    missing = str(tmp_path / "gone" / "x.png")
    assert remote_art.resolve_art(missing, "clan") == missing
    assert remote_art.resolve_art(missing, "clan") == missing
    assert len(calls) == 1
