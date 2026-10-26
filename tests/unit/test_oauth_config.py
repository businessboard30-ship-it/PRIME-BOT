"""Discord sign-in settings come from config.py ONLY and are always well formed.

Discord answers "not a well formed URL" when the redirect_uri it receives has no scheme, a stray quote/space, or a
newline. These values used to be read from Railway variables, where one bad paste broke sign-in for everyone.
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import config

ROOT = Path(__file__).resolve().parents[2]
REDIRECTS = {
    "DASH_OAUTH_REDIRECT_URI": "/api/dash",
    "BUMP_OAUTH_REDIRECT_URI": "/api/bump_oauth",
    "DISCORD_LOGIN_OAUTH_REDIRECT_URI": "/api/discord_login_oauth",
}


def _well_formed(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and bool(p.netloc) and not re.search(r"[\s\"'<>]", url)


def test_client_id_is_a_clean_snowflake():
    assert re.fullmatch(r"\d{17,20}", config.DISCORD_OAUTH_CLIENT_ID)


@pytest.mark.parametrize("name,path", REDIRECTS.items())
def test_every_redirect_uri_is_well_formed(name, path):
    value = getattr(config, name)
    assert _well_formed(value), value
    assert value.endswith(path) or value.endswith("/oauth/discord-login/")        # login may use the stable redirector


def test_base_urls_are_well_formed_and_have_no_trailing_slash():
    for value in (config.PUBLIC_BASE_URL, config.DASH_PAGES_URL):
        assert _well_formed(value) and not value.endswith("/"), value
    assert config.STABLE_BASE_URL == "" or (_well_formed(config.STABLE_BASE_URL) and not config.STABLE_BASE_URL.endswith("/"))


def _config_with_env(**env):
    base = dict(os.environ, DATABASE_URL="postgresql://x:x@localhost:5432/x", **env)
    code = ("import config as c, json; print(json.dumps({k: getattr(c, k) for k in ("
            "'DISCORD_OAUTH_CLIENT_ID','DISCORD_OAUTH_CLIENT_SECRET','DASH_OAUTH_REDIRECT_URI','DASH_PAGES_URL',"
            "'BUMP_OAUTH_REDIRECT_URI','DISCORD_LOGIN_OAUTH_REDIRECT_URI','STABLE_BASE_URL')}))")
    r = subprocess.run([sys.executable, "-c", code], env=base, capture_output=True, text=True, cwd=ROOT)
    import json
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr


def test_mistyped_railway_variables_can_no_longer_break_sign_in():
    clean, _ = _config_with_env()
    junk, err = _config_with_env(
        DISCORD_OAUTH_CLIENT_ID=' "junk" ', DASH_OAUTH_REDIRECT_URI="no-scheme.example/api/dash", DASH_PAGES_URL="bad url",
        BUMP_OAUTH_REDIRECT_URI="x", DISCORD_LOGIN_OAUTH_REDIRECT_URI="y", STABLE_BASE_URL="not a url")
    for key in ("DISCORD_OAUTH_CLIENT_ID", "DASH_OAUTH_REDIRECT_URI", "DASH_PAGES_URL", "BUMP_OAUTH_REDIRECT_URI",
                "DISCORD_LOGIN_OAUTH_REDIRECT_URI", "STABLE_BASE_URL"):
        assert junk[key] == clean[key], key                     # the environment changed nothing
    assert "not a well-formed https URL" in err                  # and the bad optional value was reported, not used


def test_the_secret_stays_in_the_environment_and_is_trimmed():
    out, _ = _config_with_env(DISCORD_OAUTH_CLIENT_SECRET="  abc123\n ")
    assert out["DISCORD_OAUTH_CLIENT_SECRET"] == "abc123"
    assert "abc123" not in (ROOT / "config.py").read_text(encoding="utf-8")


def test_a_valid_stable_url_is_cleaned_and_used_for_the_login_redirect():
    out, _ = _config_with_env(STABLE_BASE_URL=' "https://user.github.io/PRIME-BOT/" ')
    assert out["STABLE_BASE_URL"] == "https://user.github.io/PRIME-BOT"
    assert out["DISCORD_LOGIN_OAUTH_REDIRECT_URI"] == "https://user.github.io/PRIME-BOT/oauth/discord-login/"


def test_the_dashboard_sends_discord_exactly_the_configured_values():
    from api import dash
    q = parse_qs(urlparse(dash._authorize_url("state123", "none")).query)
    assert q["client_id"] == [config.DISCORD_OAUTH_CLIENT_ID]
    assert q["redirect_uri"] == [config.DASH_OAUTH_REDIRECT_URI] and _well_formed(q["redirect_uri"][0])
    assert q["response_type"] == ["code"] and q["scope"] == ["identify guilds"]


def test_the_dashboard_page_talks_to_the_same_backend_as_config():
    js = (ROOT / "dashboard" / "assets" / "config.js").read_text(encoding="utf-8")
    api_base = re.search(r'API_BASE:\s*"([^"]+)"', js).group(1)
    assert api_base == config.PUBLIC_BASE_URL
    assert f"client_id={config.DISCORD_OAUTH_CLIENT_ID}" in js
