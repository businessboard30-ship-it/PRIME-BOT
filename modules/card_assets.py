# path: modules/card_assets.py
"""Custom level-up card assets (card plan): an uploaded background and/or logo.

Pipeline: size cap -> decode with PIL (PNG/JPEG only, pixel cap, no animation) -> shape check -> RE-ENCODE to PNG
(strips metadata and anything appended to the file) -> hash block-list -> OpenAI free image moderation -> approved/pending/rejected.
Fails safe: any moderation error leaves the asset 'pending' (the bot only ever draws 'approved' assets).

The uploaded background decides the card size: it is stored with its OWN width and height (any aspect ratio, nothing
cropped, nothing shrunk), and the card is drawn at that size (see modules/level_card_design.py for the layout rules).
Zones of the 900x300 reference layout (scaled to the image): avatar x0-280, text x280-660 (darkened automatically),
logo box x660-880 y20-280.
"""
import base64
import hashlib
import io
import json
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

KINDS = ("background", "logo")
MB = 1024 * 1024
# Same size limit as the welcome-card backgrounds (discord_bot/cogs/welcome.py CUSTOM_BG_MAX_BYTES = 8 MB).
MAX_BYTES = {"background": 8 * MB, "logo": 8 * MB}
MAX_PIXELS = 25_000_000                   # decoded size cap (header is checked before the pixels are loaded); RGBA is ~100 MB
MAX_SIDE = 10_000                         # longest side, so a 1x20000 strip can't slip under the pixel cap
MIN_BG = (450, 150)                       # smallest background: the layout scales by min(w/900, h/300), so this keeps text >= 0.5x
JPEG_QUALITY = 92
CARD_W, CARD_H = 900, 300
LOGO_BOX = (660, 20, 880, 280)          # x0, y0, x1, y1
TEXT_ZONE = (280, 0, 660, 300)

PROMPT = (
    "Create a 900x300 pixel PNG background for a Discord level-up card. Style: {idea}. "
    "Rules: no text, letters, numbers or watermarks anywhere. No faces of real people, no real brand logos. "
    "Keep the left area (x 0-280) calm and low-detail because an avatar is drawn there. "
    "Keep the middle area (x 280-660) dark and plain, no objects, because white text is drawn there. "
    "Only the right area (x 660-880, y 20-280) may hold a logo, mascot or picture, centred with a clean edge and some margin."
)


def prompt_template() -> str:
    return PROMPT


def decode_b64(data) -> Optional[bytes]:
    if not isinstance(data, str) or len(data) > 11_600_000:     # 8 MB of file is about 10.7M of base64
        return None
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        logger.debug("swallowed exception in decode_b64", exc_info=True)
        return None


def process(kind: str, raw: bytes) -> tuple:
    """-> (png_bytes, None) or (None, message). Pure and synchronous (run it in a thread)."""
    if kind not in KINDS:
        return None, "Unknown asset type."
    if not raw or len(raw) > MAX_BYTES[kind]:
        return None, f"File must be under {MAX_BYTES[kind] // 1_000_000 if MAX_BYTES[kind] >= 1_000_000 else 1} MB."
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format not in ("PNG", "JPEG"):
            return None, "Only PNG or JPEG files are accepted."
        w, h = img.size
        if w * h > MAX_PIXELS or w < 1 or h < 1 or max(w, h) > MAX_SIDE:
            return None, "Image dimensions are too large."
        if getattr(img, "n_frames", 1) > 1:
            return None, "Animated images are not accepted."
        img.load()
    except Exception:
        logger.debug("swallowed exception in process", exc_info=True)
        return None, "That file isn't a valid image."
    if kind == "background":
        if w < MIN_BG[0] or h < MIN_BG[1]:
            return None, f"Background must be at least {MIN_BG[0]}x{MIN_BG[1]} pixels."
        # Own width and height, any aspect ratio: nothing is cropped or resized. Re-encoded (strips metadata and anything
        # appended to the file). A JPEG stays a JPEG so a photo doesn't balloon as PNG in the database.
        rgb = img.convert("RGB")
        return _encode_background(rgb, prefer_jpeg=(img.format == "JPEG"), cap=MAX_BYTES[kind])
    if w < 64 or h < 64:
        return None, "Logo must be at least 64x64."
    out = img.convert("RGBA")
    out.thumbnail(((LOGO_BOX[2] - LOGO_BOX[0]) * 3, (LOGO_BOX[3] - LOGO_BOX[1]) * 3), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


def _encode_background(rgb: Image.Image, prefer_jpeg: bool, cap: int) -> tuple:
    """-> (bytes, None) or (None, message). Stored size is capped at `cap` (the upload limit) by switching to JPEG,
    never by changing the image's dimensions."""
    if not prefer_jpeg:
        buf = io.BytesIO()
        rgb.save(buf, format="PNG", optimize=True)
        if len(buf.getvalue()) <= cap:
            return buf.getvalue(), None
    for q in (JPEG_QUALITY, 85, 75):
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=q, optimize=True)
        if len(buf.getvalue()) <= cap:
            return buf.getvalue(), None
    return None, f"That image is too detailed to store ({cap // MB} MB max). Try a smaller file."


def moderation_copy(png: bytes) -> bytes:
    """A small copy for the moderation call: a big card image would make the request huge, and the check does not need
    the full size. Returned as PNG bytes (that is what the endpoint call is built for)."""
    try:
        im = Image.open(io.BytesIO(png))
        if max(im.size) > 1024:
            im.thumbnail((1024, 1024), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:
        logger.debug("swallowed exception in moderation_copy", exc_info=True)
        pass
    return png


def sha(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


MODERATION_URL = "https://api.openai.com/v1/moderations"
MODERATION_MODEL = "omni-moderation-latest"          # free endpoint; image support covers sexual/violence/self-harm categories


async def _post_moderation(png: bytes) -> dict:
    """One call to OpenAI's free moderation endpoint. Raises on any problem (no key, HTTP error, bad JSON)."""
    import os
    import httpx
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        from modules import owner_env as _oe
        _oe.note_missing("OPENAI_API_KEY", "card art")
        raise RuntimeError("OPENAI_API_KEY is not set")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            MODERATION_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": MODERATION_MODEL, "input": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]})
        resp.raise_for_status()
        return resp.json()


async def moderate(png: bytes) -> dict:
    """-> {'status': 'approved'|'rejected'|'pending', 'reason': str}. Flagged -> rejected; any failure -> pending (never approved)."""
    try:
        data = await _post_moderation(png)
        res = (data.get("results") or [None])[0]
        if not isinstance(res, dict) or not isinstance(res.get("flagged"), bool):
            return {"status": "pending", "reason": "Waiting for a check."}
        if res["flagged"]:
            cats = sorted(k for k, v in (res.get("categories") or {}).items() if v is True)
            return {"status": "rejected", "reason": ("Not allowed: " + ", ".join(cats))[:200] if cats else "Not allowed."}
        return {"status": "approved", "reason": ""}
    except Exception as e:
        logger.warning(f"[card_assets] moderation failed, leaving pending: {type(e).__name__}")
        return {"status": "pending", "reason": "Waiting for a check."}


async def submit(db, uid, kind: str, raw: bytes) -> dict:
    """Whole upload pipeline for one asset. -> {'ok': bool, 'status'|'message': ...}"""
    import asyncio
    png, err = await asyncio.to_thread(process, kind, raw)
    if err:
        return {"ok": False, "message": err}
    h = sha(png)
    if await db.card_asset_blocked(h):
        return {"ok": False, "message": "That image isn't allowed."}
    await db.card_asset_set(str(uid), kind, png, h, "pending", "")
    verdict = await moderate(await asyncio.to_thread(moderation_copy, png))
    await db.card_asset_status_set(str(uid), kind, h, verdict["status"], verdict["reason"])
    if verdict["status"] == "rejected":
        await db.card_asset_block(h, verdict["reason"])
    return {"ok": True, "status": verdict["status"], "reason": verdict["reason"]}
