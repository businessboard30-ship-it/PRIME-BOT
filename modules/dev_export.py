# path: modules/dev_export.py
"""Developer mode C2: export a file to the private storage channel. Pure rules (no Discord, no database).

The browser builds the text (for example a saved chat). The server sanitises it, ENCRYPTS it, and the bot uploads only
the ciphertext to the private storage channel under an opaque name. Only a minimal receipt row is stored. The person
gets a plain copy by DM (their own file, best effort) and can re-download it from the Export page, where the server
fetches the ciphertext back from the storage channel and decrypts it for the signed-in owner.
"""
import json
import re
import secrets
import unicodedata

MAX_BYTES = 256 * 1024            # plaintext cap per export
MAX_PER_DAY = 10                  # exports per user per rolling 24 hours
MAX_KEPT = 50                     # receipts a user can hold at once
NAME_MAX = 60
KINDS = {"note": ("text/markdown", ".md"), "text": ("text/plain", ".txt"), "json": ("application/json", ".json")}
_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def clean_name(raw, ext: str) -> str:
    """A safe display/file name: ASCII letters, digits, dot, dash, underscore, space. No paths, no hidden files."""
    base = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode()
    base = _SAFE.sub("", base.replace("/", " ").replace("\\", " ")).strip(" .-_")
    base = re.sub(r"\s+", " ", base)[:NAME_MAX].strip(" .-_") or "export"
    stem = base[: -len(ext)] if base.lower().endswith(ext) else base
    return (stem or "export") + ext


def clean_request(body):
    """Validate the browser's request. Returns (spec, error). The server decides the type, extension and size."""
    body = body if isinstance(body, dict) else {}
    kind = str(body.get("kind") or "note").strip().lower()
    if kind not in KINDS:
        return None, "That kind of export isn't supported."
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, "There is nothing to export."
    if "\x00" in content:
        return None, "That content can't be exported."
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return None, f"An export can be at most {MAX_BYTES // 1024} KB."
    if kind == "json":
        try:
            json.loads(content)
        except ValueError:
            return None, "That isn't valid JSON."
    mime, ext = KINDS[kind]
    return {"kind": kind, "mime": mime, "name": clean_name(body.get("name"), ext), "data": raw}, None


def new_export_id() -> str:
    return secrets.token_hex(16)


def opaque_filename(export_id: str) -> str:
    """What the storage channel sees: no user id, no title, no extension that hints at the content."""
    return f"{export_id}.bin"


def valid_export_id(raw) -> bool:
    return isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{32}", raw) is not None


def encrypt(data: bytes) -> bytes:
    from utils.crypto import secret_manager
    return secret_manager.cipher.encrypt(data)


def decrypt(blob: bytes):
    """Plaintext bytes, or None when the blob is not ours / was altered."""
    from utils.crypto import secret_manager
    try:
        return secret_manager.cipher.decrypt(blob)
    except Exception:
        return None


def storage_message(export_id: str) -> dict:
    """payload_json for the storage upload. No mentions, and nothing about the person: just the opaque id."""
    return {"content": f"export {export_id}", "attachments": [{"id": 0, "filename": opaque_filename(export_id)}],
            "allowed_mentions": {"parse": []}}


def dm_message(name: str, page_url: str) -> dict:
    link = f"\nYou can also download it again from {page_url}" if page_url else ""
    return {"content": f"Here is your export, **{name}**.{link}", "attachments": [{"id": 0, "filename": name}],
            "allowed_mentions": {"parse": []}}


def public_view(row: dict) -> dict:
    """The only shape of a receipt that leaves the server (never the storage message id)."""
    c = row.get("created_at")
    return {"id": row.get("id"), "name": row.get("name") or "export", "size": int(row.get("size_bytes") or 0),
            "created_at": c.isoformat() if hasattr(c, "isoformat") else (str(c) if c else None)}
