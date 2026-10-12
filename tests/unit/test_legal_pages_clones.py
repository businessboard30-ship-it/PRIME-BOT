"""Terms and privacy cover clones and member/join data, and never use the word 'token'."""
import importlib
from pathlib import Path


def _legal(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h/d")
    return importlib.import_module("api.legal_pages")


def test_pages_cover_clones_members_and_message_content_without_the_word_token(monkeypatch):
    L = _legal(monkeypatch)
    for html in (L.TERMS_HTML, L.PRIVACY_HTML):
        assert "token" not in html.lower() and "Welcome Bot" in html
    assert "Server Members" in L.PRIVACY_HTML and "Message Content" in L.PRIVACY_HTML
    assert "train AI" in L.PRIVACY_HTML and "opt out" in L.PRIVACY_HTML


def test_stable_static_pages_match_the_served_pages():
    root = Path(__file__).resolve().parents[2] / "redirector"
    for name in ("terms", "privacy"):
        html = (root / name / "index.html").read_text()
        assert "token" not in html.lower() and "Welcome Bot" in html


def test_website_pages_keep_the_site_theme_and_cover_clones_without_the_word_token():
    root = Path(__file__).resolve().parents[2] / "website"
    for name in ("terms", "privacy"):
        html = (root / name / "index.html").read_text()
        assert "token" not in html.lower() and "Welcome Bot" in html
        assert '/assets/site.css' in html and 'content="dark"' in html and "12 October 2026" in html
    assert "Server Members" in (root / "privacy/index.html").read_text()
