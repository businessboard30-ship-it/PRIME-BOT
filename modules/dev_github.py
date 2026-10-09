# path: modules/dev_github.py
"""Developer mode C4: connect GitHub, read-only (v1). Pure rules plus server-to-GitHub calls.

Rules this module enforces:
- OAuth with `state` + PKCE (S256). The authorize URL carries no write scope by default; nothing here ever writes to GitHub.
- The access token is encrypted by the caller, never returned, never logged, never put in an error message. Every error
  that leaves this module is a fixed, safe sentence (never GitHub's body).
- Only https://api.github.com is ever called. repo / path / ref / sha are validated and percent-encoded, so a request
  can't change the host, the route or add query parameters.
- Responses are size-capped before they reach the browser. No code execution, deploys or webhooks.
"""
import base64
import hashlib
import re
import secrets
from urllib.parse import quote, urlencode

import aiohttp

import config

PROVIDER = "github"                   # row in dev_connections (ciphertext is JSON {"token", "login"})
STATE_PREFIX = "gh."                  # tells api/dash.py's callback this is GitHub, not a Discord sign-in
RETURN_PREFIX = "dash_github:"        # return_to stored beside the state: dash_github:<user id>:<PKCE verifier>
API = "https://api.github.com"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_AGENT = "PRIME-BOT-dashboard"
HTTP_TIMEOUT = 15
MAX_REPOS = 30
MAX_DIR_ENTRIES = 200
MAX_FILE_BYTES = 200_000              # bigger files are listed but not opened
MAX_TEXT_CHARS = 60_000               # what the browser may receive for a file or a diff
MAX_DIFF_BYTES = 400_000              # read at most this much from GitHub for a diff
ATTACH_CHARS = 3500                   # one chat message is capped at 4000 characters (modules.dev_chat)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{10,300}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{6,200}$")
_STATE_RE = re.compile(r"^gh\.[A-Za-z0-9_\-]{16,80}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./\-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BAD_PATH = re.compile(r"[\x00-\x1f\x7f\\]")


def configured() -> bool:
    return bool(config.GITHUB_OAUTH_CLIENT_ID and config.GITHUB_OAUTH_CLIENT_SECRET)


# ---------- OAuth (state + PKCE) ----------
def new_state() -> str:
    return STATE_PREFIX + secrets.token_urlsafe(24)


def new_verifier() -> str:
    return secrets.token_urlsafe(48)              # 64 URL-safe characters (RFC 7636 allows 43 to 128)


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def pack_return(uid, verifier: str) -> str:
    return f"{RETURN_PREFIX}{uid}:{verifier}"


def unpack_return(rt):
    """(user id, verifier) or None."""
    if not isinstance(rt, str) or not rt.startswith(RETURN_PREFIX):
        return None
    uid, _, verifier = rt[len(RETURN_PREFIX):].partition(":")
    return (uid, verifier) if uid.isdigit() and 43 <= len(verifier) <= 128 else None


def authorize_url(state: str, verifier: str) -> str:
    q = {"client_id": config.GITHUB_OAUTH_CLIENT_ID, "redirect_uri": config.DASH_OAUTH_REDIRECT_URI, "state": state,
         "code_challenge": challenge_for(verifier), "code_challenge_method": "S256", "allow_signup": "false"}
    if config.GITHUB_OAUTH_SCOPE:
        q["scope"] = config.GITHUB_OAUTH_SCOPE
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


def clean_code(raw):
    return raw.strip() if isinstance(raw, str) and _CODE_RE.match(raw.strip()) else None


def clean_state(raw):
    return raw.strip() if isinstance(raw, str) and _STATE_RE.match(raw.strip()) else None


async def exchange_code(code: str, verifier: str):
    """Returns (token, error). Never raises; the error is a fixed sentence."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as s:
            async with s.post(TOKEN_URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                              data={"client_id": config.GITHUB_OAUTH_CLIENT_ID, "client_secret": config.GITHUB_OAUTH_CLIENT_SECRET,
                                    "code": code, "redirect_uri": config.DASH_OAUTH_REDIRECT_URI,
                                    "code_verifier": verifier}) as r:
                status = r.status
                data = await r.json(content_type=None) if status == 200 else None
    except Exception:
        return None, "Couldn't reach GitHub. Try again in a moment."
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None, "GitHub didn't confirm the connection. Start again."
    return token, None


# ---------- stored secret ----------
def pack_secret(token: str, login: str) -> str:
    import json
    return json.dumps({"token": token, "login": login})


def unpack_secret(plain):
    """(token, login) from a decrypted secret, or (None, None)."""
    import json
    try:
        d = json.loads(plain)
        t, login = d.get("token"), d.get("login")
        return (t, login) if isinstance(t, str) and _TOKEN_RE.match(t) and isinstance(login, str) else (None, None)
    except Exception:
        return None, None


def last4(token: str) -> str:
    return token[-4:]


# ---------- input validation (every value is checked before it reaches a URL) ----------
def clean_repo(raw):
    r = raw.strip() if isinstance(raw, str) else ""
    if not _REPO_RE.match(r):
        return None
    return None if any(seg in (".", "..") for seg in r.split("/")) else r


def clean_ref(raw):
    if raw in (None, ""):
        return None
    r = raw.strip() if isinstance(raw, str) else ""
    return r if _REF_RE.match(r) and ".." not in r and not r.startswith("/") and not r.endswith("/") else False


def clean_path(raw):
    """'' for the repo root, a clean relative path, or False when it is not acceptable."""
    if raw in (None, ""):
        return ""
    p = raw.strip().strip("/") if isinstance(raw, str) else None
    if p is None or len(p) > 300 or _BAD_PATH.search(p):
        return False
    return False if any(seg in ("..", ".") for seg in p.split("/")) else p


def clean_diff_spec(kind, a, b):
    """Returns (path_suffix, error). kind: commit (a = sha) | compare (a = base ref, b = head ref) | pull (a = number)."""
    if kind == "commit":
        return (f"commits/{a.lower()}", None) if isinstance(a, str) and _SHA_RE.match(a.strip()) else (None, "Enter a commit hash (7 to 40 characters).")
    if kind == "pull":
        s = str(a or "").strip()
        return (f"pulls/{int(s)}", None) if s.isdigit() and 0 < int(s) < 10 ** 7 else (None, "Enter a pull request number.")
    if kind == "compare":
        ra, rb = clean_ref(a), clean_ref(b)
        if not ra or not rb:
            return None, "Enter two branch names, tags or commit hashes."
        return f"compare/{quote(ra, safe='')}...{quote(rb, safe='')}", None
    return None, "Pick commit, compare or pull request."


# ---------- GitHub calls (read-only GETs) ----------
def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT}


def _safe_error(status: int) -> str:
    if status == 401:
        return "GitHub rejected the saved connection. Disconnect it and connect again."
    if status in (403, 429):
        return "GitHub is limiting requests right now. Try again in a few minutes."
    if status in (404, 422):
        return "Not found. The repository, path or reference doesn't exist, or it is private."
    return "GitHub couldn't answer right now. Try again in a moment."


async def _get(token: str, path: str, params=None, accept="application/vnd.github+json", max_bytes=1_000_000, as_json=True):
    """Returns (status, data). Raises RuntimeError with a SAFE message on any failure (never the body or the token)."""
    url = f"{API}/{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as s:
            async with s.get(url, headers=_headers(token, accept), params=params) as r:
                status = r.status
                if status != 200:
                    raise RuntimeError(_safe_error(status))
                raw = await r.content.read(max_bytes)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("Couldn't reach GitHub. Try again in a moment.") from None
    if as_json:
        import json
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise RuntimeError("GitHub sent something unexpected. Try again.") from None
    return status, raw.decode("utf-8", "replace")


def _clip(v, n):
    return str(v)[:n] if v is not None else ""


async def whoami(token: str):
    _, d = await _get(token, "user")
    login = d.get("login") if isinstance(d, dict) else None
    if not isinstance(login, str) or not re.match(r"^[A-Za-z0-9\-]{1,39}$", login):
        raise RuntimeError("GitHub sent something unexpected. Try again.")
    return login


async def list_repos(token: str) -> list:
    _, d = await _get(token, "user/repos", params={"sort": "updated", "per_page": str(MAX_REPOS), "affiliation": "owner,collaborator"})
    out = []
    for r in (d if isinstance(d, list) else [])[:MAX_REPOS]:
        if isinstance(r, dict) and _REPO_RE.match(str(r.get("full_name") or "")):
            out.append({"full_name": r["full_name"], "private": bool(r.get("private")), "description": _clip(r.get("description"), 140),
                        "default_branch": _clip(r.get("default_branch"), 100), "updated_at": _clip(r.get("pushed_at") or r.get("updated_at"), 30)})
    return out


def _decode_text(raw: bytes):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def browse(token: str, repo: str, path: str, ref):
    """A folder (entries) or a text file (content, clipped). Binary and oversized files are described, not sent."""
    params = {"ref": ref} if ref else None
    _, d = await _get(token, f"repos/{repo}/contents/{quote(path, safe='/')}".rstrip("/"), params=params)
    if isinstance(d, list):
        entries = [{"name": _clip(e.get("name"), 120), "path": _clip(e.get("path"), 300), "type": "dir" if e.get("type") == "dir" else "file",
                    "size": int(e.get("size") or 0)} for e in d[:MAX_DIR_ENTRIES] if isinstance(e, dict)]
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"type": "dir", "path": path, "entries": entries}
    if not isinstance(d, dict) or d.get("type") != "file":
        raise RuntimeError("That isn't a file or folder you can open here.")
    size = int(d.get("size") or 0)
    base = {"type": "file", "path": path, "name": _clip(d.get("name"), 120), "size": size}
    if size > MAX_FILE_BYTES:
        return {**base, "text": None, "note": "This file is too large to open here."}
    try:
        raw = base64.b64decode(d.get("content") or "", validate=False)
    except Exception:
        raw = b""
    text = _decode_text(raw)
    if text is None or "\x00" in text:
        return {**base, "text": None, "note": "This looks like a binary file, so it can't be shown."}
    return {**base, "text": text[:MAX_TEXT_CHARS], "truncated": len(text) > MAX_TEXT_CHARS}


async def get_diff(token: str, repo: str, suffix: str) -> dict:
    _, text = await _get(token, f"repos/{repo}/{suffix}", accept="application/vnd.github.diff", max_bytes=MAX_DIFF_BYTES, as_json=False)
    return {"diff": text[:MAX_TEXT_CHARS], "truncated": len(text) > MAX_TEXT_CHARS}


async def revoke(token: str) -> None:
    """Best effort: ask GitHub to drop the grant. Never raises; a failure just leaves the token to expire on GitHub's side."""
    if not configured():
        return
    try:
        auth = aiohttp.BasicAuth(config.GITHUB_OAUTH_CLIENT_ID, config.GITHUB_OAUTH_CLIENT_SECRET)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as s:
            async with s.delete(f"{API}/applications/{quote(config.GITHUB_OAUTH_CLIENT_ID, safe='')}/grant", auth=auth,
                                headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
                                json={"access_token": token}) as r:
                await r.read()
    except Exception:
        return
