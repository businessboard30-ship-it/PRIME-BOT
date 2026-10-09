"""Developer mode C4: GitHub connect (read-only). Gate, step-up, state + PKCE bound to the user, token secrecy, validation, caps."""
import json
import logging
import time
from pathlib import Path

import pytest

import config
from api import dash, dash_dev
from modules import dev_github, dev_keys
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_member import mem  # noqa: F401  ("sid" = user 6, card plan only; "other" = 7777777777 with dev_monthly)

UID = "7777777777"
TOKEN = "gho_SECRETTOKENabcdefghijklmnopqrstuv1234"
VERIFIER = "v" * 64


@pytest.fixture
def gh(mem, monkeypatch):
    fake, seen = mem
    st = {"rows": {}, "states": {}, "exchanged": [], "revoked": [], "login": "octo", "exchange_err": None}

    async def lst(uid):
        return [{"provider": p, "last4": v["last4"], "created_at": None, "updated_at": None} for (u, p), v in sorted(st["rows"].items()) if u == uid]

    async def secret(uid, provider):
        v = st["rows"].get((uid, provider)); return v["enc"] if v else None

    async def upsert(uid, provider, enc, last4):
        st["rows"][(uid, provider)] = {"enc": enc, "last4": last4}

    async def delete(uid, provider):
        return st["rows"].pop((uid, provider), None) is not None

    async def create_state(state, return_to=None):
        st["states"][state] = return_to

    async def pop_state(state):
        rt = st["states"].pop(state, "__missing__")
        return None if rt == "__missing__" else {"return_to": rt}

    async def exchange(code, verifier):
        st["exchanged"].append((code, verifier))
        return (None, st["exchange_err"]) if st["exchange_err"] else (TOKEN, None)

    async def whoami(token):
        return st["login"]

    async def revoke(token):
        st["revoked"].append(token)
    for n, f in (("dev_connection_list", lst), ("dev_connection_secret", secret), ("dev_connection_upsert", upsert), ("dev_connection_delete", delete),
                 ("create_login_oauth_state", create_state), ("pop_login_oauth_state", pop_state)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(dev_github, "exchange_code", exchange)
    monkeypatch.setattr(dev_github, "whoami", whoami)
    monkeypatch.setattr(dev_github, "revoke", revoke)
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(config, "GITHUB_OAUTH_SCOPE", "", raising=False)
    dash._owner_hits.clear()
    return fake, st


def fresh(fake, token="other"):
    fake.sessions[token]["fresh_until"] = int(time.time()) + 300


def post(action, token="other", **body):
    return call("POST", None, token=token, body=dict({"action": action}, **body))


def get(action, token="other", **q):
    return call("GET", dict({"action": action}, **q), token=token)


def connect_now(fake, st):
    fresh(fake)
    _, p, _ = post("dev_github_connect")
    state = p["url"].split("state=")[1].split("&")[0]
    return state


def finish(state, code="abcdef123456", token="other"):
    return post("dev_github_finish", token=token, code=code, state=state)


# ---------- pure ----------
def test_pkce_challenge_matches_the_rfc_7636_vector():
    assert dev_github.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_return_packing_roundtrips_and_rejects_junk():
    assert dev_github.unpack_return(dev_github.pack_return(UID, VERIFIER)) == (UID, VERIFIER)
    for bad in (None, "", "dash", "dash_stepup_dev:other", "dash_github:abc:" + VERIFIER, "dash_github:1:short", 5):
        assert dev_github.unpack_return(bad) is None


@pytest.mark.parametrize("raw,ok", [("octo/repo", True), ("a/b.c-d_e", True), ("a/..", False), ("../x", False), ("a/b/c", False), ("a", False),
                                    ("a/b?x=1", False), ("a/b#f", False), ("a b/c", False), ("", False), (None, False), ("a" * 101 + "/b", False)])
def test_repo_validation(raw, ok):
    assert bool(dev_github.clean_repo(raw)) is ok


@pytest.mark.parametrize("raw,ok", [("", True), (None, True), ("src/app.py", True), ("/src/", True), ("a b/c d.txt", True), ("..", False), ("a/../b", False),
                                    ("a/./b", False), ("a\\b", False), ("a\x00b", False), ("x" * 301, False)])
def test_path_validation(raw, ok):
    assert (dev_github.clean_path(raw) is not False) is ok


def test_path_and_ref_cannot_smuggle_query_or_fragment():
    assert dev_github.clean_ref("main?x=1") is False and dev_github.clean_ref("a..b") is False and dev_github.clean_ref("/x") is False
    assert dev_github.clean_ref("feature/x-1") == "feature/x-1" and dev_github.clean_ref("") is None


def test_diff_spec_validation():
    assert dev_github.clean_diff_spec("commit", "ABC1234", None) == ("commits/abc1234", None)
    assert dev_github.clean_diff_spec("pull", "42", None) == ("pulls/42", None)
    assert dev_github.clean_diff_spec("compare", "main", "dev/x")[0] == "compare/main...dev%2Fx"
    for bad in (("commit", "xyz", None), ("commit", "../etc", None), ("pull", "-1", None), ("pull", "abc", None), ("compare", "a..b", "c"),
                ("compare", "a", None), ("nope", "a", "b"), ("commit", None, None)):
        assert dev_github.clean_diff_spec(*bad)[0] is None


def test_authorize_url_is_read_only_pkce_and_scopeless_by_default(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(config, "GITHUB_OAUTH_SCOPE", "", raising=False)
    u = dev_github.authorize_url("gh.state", VERIFIER)
    assert u.startswith("https://github.com/login/oauth/authorize?") and "scope=" not in u
    assert "code_challenge_method=S256" in u and "code_challenge=" + dev_github.challenge_for(VERIFIER) in u and "state=gh.state" in u
    assert VERIFIER not in u                                          # only the hash travels through the browser


# ---------- gate + step-up ----------
def test_every_github_route_is_gated_for_non_subscribers(gh):
    fake, st = gh
    for a in ("dev_github", "dev_github_repos", "dev_github_browse", "dev_github_diff"):
        assert get(a, token="sid")[0] == 402
    fresh(fake, "sid")
    for a, b in (("dev_github_connect", {}), ("dev_github_finish", {"code": "abcdef", "state": "gh." + "a" * 20}), ("dev_github_disconnect", {})):
        s, p, _ = post(a, token="sid", **b)
        assert s == 402 and p["code"] == "subscription_required"
    assert st["rows"] == {} and st["states"] == {} and st["exchanged"] == []


def test_connect_and_disconnect_need_a_fresh_sign_in(gh):
    fake, st = gh
    for a in ("dev_github_connect", "dev_github_disconnect"):
        s, p, _ = post(a)
        assert s == 403 and p["code"] == "stepup_required"
    fake.sessions["other"]["fresh_until"] = int(time.time()) - 5
    assert post("dev_github_connect")[0] == 403 and st["states"] == {}


def test_connect_503_until_the_owner_configures_the_oauth_app(gh, monkeypatch):
    fake, st = gh
    monkeypatch.setattr(config, "GITHUB_OAUTH_CLIENT_SECRET", "", raising=False)
    fresh(fake)
    assert post("dev_github_connect")[0] == 503 and st["states"] == {}
    s, p, _ = get("dev_github")
    assert s == 200 and p["configured"] is False and p["connected"] is False


def test_connect_binds_state_to_the_session_user_with_a_pkce_verifier(gh):
    fake, st = gh
    state = connect_now(fake, st)
    assert state.startswith("gh.")
    uid, verifier = dev_github.unpack_return(st["states"][state])
    assert uid == UID and len(verifier) >= 43


# ---------- callback (nothing is exchanged here) ----------
def test_callback_hands_code_and_state_to_the_browser_without_consuming_the_state(gh):
    fake, st = gh
    st["states"]["gh." + "a" * 24] = dev_github.pack_return(UID, VERIFIER)
    s, _, loc = call("GET", {"code": "abc123def", "state": "gh." + "a" * 24}, token=None)
    assert s == 302 and loc.startswith(config.DASH_PAGES_URL + "/#") and "gh_code=abc123def" in loc and "gh_state=gh." in loc
    assert st["exchanged"] == [] and ("gh." + "a" * 24) in st["states"]


def test_callback_cancel_goes_back_with_an_error_flag(gh):
    s, _, loc = call("GET", {"error": "access_denied", "state": "gh." + "a" * 24}, token=None)
    assert s == 302 and loc.endswith("#gh_error=1")


# ---------- finish ----------
def test_finish_exchanges_with_the_verifier_stores_ciphertext_and_never_returns_the_token(gh, caplog):
    fake, st = gh
    caplog.set_level(logging.DEBUG)
    state = connect_now(fake, st)
    verifier = dev_github.unpack_return(st["states"][state])[1]
    s, p, _ = finish(state)
    assert s == 200 and p["connected"] is True and p["login"] == "octo" and st["exchanged"] == [("abcdef123456", verifier)]
    row = st["rows"][(UID, "github")]
    assert row["enc"] != TOKEN and TOKEN not in row["enc"] and row["last4"] == TOKEN[-4:]
    from utils.crypto import secret_manager
    assert dev_github.unpack_secret(secret_manager.decrypt(row["enc"])) == (TOKEN, "octo")
    assert TOKEN not in json.dumps(p) and TOKEN not in caplog.text
    assert state not in st["states"]                                  # one use only


def test_finish_rejects_a_state_issued_to_someone_else_and_replays(gh):
    fake, st = gh
    state = connect_now(fake, st)
    st["states"][state] = dev_github.pack_return("42", VERIFIER)      # started by another user
    s, p, _ = finish(state)
    assert s == 422 and st["rows"] == {} and st["exchanged"] == []
    assert finish(state)[0] == 422                                    # already consumed
    assert finish("gh." + "z" * 24)[0] == 422                         # never issued


@pytest.mark.parametrize("code,state", [("", "gh." + "a" * 20), ("abc", "gh." + "a" * 20), ("abcdef!", "gh." + "a" * 20), ("abcdef", "nope"),
                                        ("abcdef", "gh.short"), (None, None), ("a" * 300, "gh." + "a" * 20)])
def test_finish_validates_input_before_touching_state(gh, code, state):
    fake, st = gh
    st["states"]["gh." + "a" * 20] = dev_github.pack_return(UID, VERIFIER)
    assert post("dev_github_finish", code=code, state=state)[0] == 422
    assert ("gh." + "a" * 20) in st["states"] and st["exchanged"] == []


def test_finish_surfaces_a_safe_error_when_the_exchange_fails(gh):
    fake, st = gh
    state = connect_now(fake, st)
    st["exchange_err"] = "GitHub didn't confirm the connection. Start again."
    s, p, _ = finish(state)
    assert s == 502 and "Start again" in p["message"] and st["rows"] == {}


# ---------- disconnect ----------
def test_disconnect_deletes_the_token_and_asks_github_to_revoke_it(gh):
    fake, st = gh
    finish(connect_now(fake, st))
    fresh(fake)
    s, p, _ = post("dev_github_disconnect")
    assert s == 200 and p["connected"] is False and (UID, "github") not in st["rows"] and st["revoked"] == [TOKEN]


# ---------- read routes ----------
def test_read_routes_say_connect_first_when_not_connected(gh):
    for a, q in (("dev_github_repos", {}), ("dev_github_browse", {"repo": "a/b"}), ("dev_github_diff", {"repo": "a/b", "kind": "commit", "a": "abc1234"})):
        s, p, _ = get(a, **q)
        assert s == 422 and p["code"] == "github_not_connected"


def test_read_routes_reject_bad_input_without_calling_github(gh, monkeypatch):
    fake, st = gh
    finish(connect_now(fake, st))
    calls = []

    async def boom(*a, **k):
        calls.append(a); return {}
    for n in ("browse", "get_diff", "list_repos"):
        monkeypatch.setattr(dev_github, n, boom)
    for q in ({"repo": "a/.."}, {"repo": "a/b", "path": "../x"}, {"repo": "a/b", "ref": "x..y"}, {"repo": ""}):
        assert get("dev_github_browse", **q)[0] == 422
    for q in ({"repo": "a/b", "kind": "commit", "a": "zz"}, {"repo": "bad", "kind": "commit", "a": "abc1234"}, {"repo": "a/b", "kind": "x"}):
        assert get("dev_github_diff", **q)[0] == 422
    assert calls == []


def test_read_routes_pass_clean_values_and_map_errors_to_502(gh, monkeypatch):
    fake, st = gh
    finish(connect_now(fake, st))
    seen = {}

    async def browse(token, repo, path, ref):
        seen["browse"] = (token, repo, path, ref); return {"type": "dir", "path": path, "entries": []}

    async def diff(token, repo, suffix):
        raise RuntimeError("GitHub is limiting requests right now. Try again in a few minutes.")
    monkeypatch.setattr(dev_github, "browse", browse)
    monkeypatch.setattr(dev_github, "get_diff", diff)
    s, p, _ = get("dev_github_browse", repo="a/b", path="/src/", ref="main")
    assert s == 200 and seen["browse"] == (TOKEN, "a/b", "src", "main") and TOKEN not in json.dumps(p)
    s, p, _ = get("dev_github_diff", repo="a/b", kind="commit", a="abc1234")
    assert s == 502 and "limiting" in p["message"] and TOKEN not in json.dumps(p)


# ---------- browse / diff shaping and the HTTP layer (no network) ----------
@pytest.fixture
def api_get(monkeypatch):
    box = {"data": None, "calls": []}

    async def fake(token, path, params=None, accept="", max_bytes=0, as_json=True):
        box["calls"].append((path, params, accept, max_bytes)); return 200, box["data"]
    monkeypatch.setattr(dev_github, "_get", fake)
    return box


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_browse_lists_folders_first_and_caps_entries(api_get):
    api_get["data"] = [{"name": "b.py", "path": "b.py", "type": "file", "size": 5}, {"name": "src", "path": "src", "type": "dir"}] + \
                      [{"name": f"f{i}", "path": f"f{i}", "type": "file", "size": 1} for i in range(500)]
    out = run(dev_github.browse(TOKEN, "a/b", "", None))
    assert out["type"] == "dir" and out["entries"][0]["name"] == "src" and len(out["entries"]) == dev_github.MAX_DIR_ENTRIES


def test_browse_file_text_binary_large_and_truncation(api_get):
    import base64
    enc = lambda b: base64.b64encode(b).decode()
    api_get["data"] = {"type": "file", "name": "a.py", "size": 5, "content": enc(b"hello")}
    assert run(dev_github.browse(TOKEN, "a/b", "a.py", None))["text"] == "hello"
    api_get["data"] = {"type": "file", "name": "x.bin", "size": 4, "content": enc(b"\xff\xfe\x00\x01")}
    out = run(dev_github.browse(TOKEN, "a/b", "x.bin", None)); assert out["text"] is None and "binary" in out["note"]
    api_get["data"] = {"type": "file", "name": "big", "size": dev_github.MAX_FILE_BYTES + 1, "content": ""}
    out = run(dev_github.browse(TOKEN, "a/b", "big", None)); assert out["text"] is None and "too large" in out["note"]
    api_get["data"] = {"type": "file", "name": "l", "size": 100, "content": enc(b"x" * (dev_github.MAX_TEXT_CHARS + 50))}
    out = run(dev_github.browse(TOKEN, "a/b", "l", None)); assert len(out["text"]) == dev_github.MAX_TEXT_CHARS and out["truncated"] is True


def test_browse_encodes_the_path_so_it_cannot_change_the_route(api_get):
    api_get["data"] = []
    run(dev_github.browse(TOKEN, "a/b", "dir with space/x#y?z", "main"))
    path, params, _, _ = api_get["calls"][0]
    assert path == "repos/a/b/contents/dir%20with%20space/x%23y%3Fz" and params == {"ref": "main"}


def test_diff_is_clipped_and_requests_the_diff_media_type(monkeypatch):
    seen = {}

    async def fake(token, path, params=None, accept="", max_bytes=0, as_json=True):
        seen.update(path=path, accept=accept, max_bytes=max_bytes, as_json=as_json); return 200, "d" * (dev_github.MAX_TEXT_CHARS + 9)
    monkeypatch.setattr(dev_github, "_get", fake)
    out = run(dev_github.get_diff(TOKEN, "a/b", "commits/abc1234"))
    assert len(out["diff"]) == dev_github.MAX_TEXT_CHARS and out["truncated"] and seen["accept"] == "application/vnd.github.diff" and seen["as_json"] is False


class _Resp:
    def __init__(self, status, body=b"{}"):
        self.status, self._b = status, body
        self.content = self

    async def read(self, n=-1):
        return self._b[:n] if n and n > 0 else self._b

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Sess:
    log = []
    status, body = 200, b"{}"

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def get(self, url, headers=None, params=None):
        _Sess.log.append((url, dict(headers or {}), params)); return _Resp(_Sess.status, _Sess.body)


@pytest.mark.parametrize("status,needle", [(401, "rejected"), (403, "limiting"), (429, "limiting"), (404, "Not found"), (500, "couldn't answer")])
def test_http_errors_are_fixed_sentences_that_never_echo_github_or_the_token(monkeypatch, status, needle):
    monkeypatch.setattr(dev_github.aiohttp, "ClientSession", _Sess)
    _Sess.log, _Sess.status, _Sess.body = [], status, ('{"message":"leak ' + TOKEN + '"}').encode()
    with pytest.raises(RuntimeError) as e:
        run(dev_github._get(TOKEN, "user"))
    assert needle in str(e.value) and TOKEN not in str(e.value) and "leak" not in str(e.value)


def test_only_api_github_com_is_ever_called_and_token_goes_in_the_header(monkeypatch):
    monkeypatch.setattr(dev_github.aiohttp, "ClientSession", _Sess)
    _Sess.log, _Sess.status, _Sess.body = [], 200, b'{"login": "octo"}'
    assert run(dev_github.whoami(TOKEN)) == "octo"
    url, headers, params = _Sess.log[0]
    assert url == "https://api.github.com/user" and headers["Authorization"] == f"Bearer {TOKEN}" and TOKEN not in url and not params


# ---------- the table is shared with the AI keys ----------
def test_github_row_is_hidden_from_the_ai_key_list_and_not_counted_against_the_key_cap(gh, monkeypatch):
    fake, st = gh
    finish(connect_now(fake, st))
    for p in ("anthropic", "groq"):
        st["rows"][(UID, p)] = {"enc": "x", "last4": "abcd"}

    async def validate(provider, key):
        return True, None
    monkeypatch.setattr(dev_keys, "validate", validate)
    s, p, _ = get("dev_keys")
    assert s == 200 and sorted(c["provider"] for c in p["connections"]) == ["anthropic", "groq"]
    fresh(fake)
    s, p, _ = post("dev_key_save", provider="openai", key="sk-" + "a" * 30)     # third AI key must still fit
    assert s == 200 and sorted(c["provider"] for c in p["connections"]) == ["anthropic", "groq", "openai"]


def test_status_and_features(gh):
    fake, st = gh
    s, p, _ = get("dev_github")
    assert s == 200 and p["connected"] is False and p["configured"] is True and "Read-only" in p["access"]
    finish(connect_now(fake, st))
    s, p, _ = get("dev_github")
    assert p["connected"] is True and p["login"] == "octo" and "token" not in json.dumps(p).lower().replace("read-only", "")
    assert any(f["key"] == "github" and f["ready"] for f in dash_dev.FEATURES)


# ---------- guard rails in the source ----------
def test_github_module_never_writes_and_front_end_has_no_innerhtml():
    src = Path(dev_github.__file__).read_text()
    assert "s.post(" not in src.replace("s.post(TOKEN_URL", "") and "s.put(" not in src and "s.patch(" not in src
    assert src.count("s.delete(") == 1 and "applications/" in src                  # only the grant revoke
    js = (Path(dash.__file__).resolve().parents[1] / "dashboard" / "assets" / "dash.js").read_text()
    page = js[js.index("function devGithub"):js.index("function renderDev")]
    assert "innerHTML" not in page and "dev_github_connect" in page and "dev_github_finish" in page


def test_every_github_write_and_read_handler_is_gated():
    import inspect
    for name in ("dev_github_status", "dev_github_connect", "dev_github_finish", "dev_github_disconnect",
                 "dev_github_repos", "dev_github_browse", "dev_github_diff"):
        body = inspect.getsource(getattr(dash_dev, name))
        assert body.split("\n", 1)[1].lstrip().split("\n")[0].strip() != "" and "await require_dev(uid, db)" in body
        assert body.index("await require_dev(uid, db)") < min([body.index(w) for w in ("clean_", "_gh_read", "_gh_secret", "db.") if w in body] or [10 ** 9])
