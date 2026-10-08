# path: modules/card_assets.py
"""Custom level-up card assets (card plan): an uploaded background and/or logo.

Pipeline: size cap -> decode with PIL (PNG/JPEG only, pixel cap, no animation) -> shape check -> RE-ENCODE to PNG
(strips metadata and anything appended to the file) -> hash block-list -> OpenAI free image moderation -> approved/pending/rejected.
Fails safe: any moderation error leaves the asset 'pending' (the bot only ever draws 'approved' assets).

Card zones (900x300): avatar x0-280, text x280-660 (darkened automatically), logo box x660-880 y20-280.
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
MAX_BYTES = {"background": 1_500_000, "logo": 1_000_000}
MAX_PIXELS = 16_000_000
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
    if not isinstance(data, str) or len(data) > 4_000_000:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
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
        if w * h > MAX_PIXELS or w < 1 or h < 1:
            return None, "Image dimensions are too large."
        if getattr(img, "n_frames", 1) > 1:
            return None, "Animated images are not accepted."
        img.load()
    except Exception:
        return None, "That file isn't a valid image."
    if kind == "background":
        if w < 600 or h < 200 or not (2.85 <= w / h <= 3.15):
            return None, "Background must be about 3:1 (ideally 900x300, at least 600x200)."
        out = img.convert("RGB").resize((CARD_W, CARD_H), Image.LANCZOS)
    else:
        if w < 64 or h < 64:
            return None, "Logo must be at least 64x64."
        out = img.convert("RGBA")
        out.thumbnail(((LOGO_BOX[2] - LOGO_BOX[0]) * 2, (LOGO_BOX[3] - LOGO_BOX[1]) * 2), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


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
    verdict = await moderate(png)
    await db.card_asset_status_set(str(uid), kind, h, verdict["status"], verdict["reason"])
    if verdict["status"] == "rejected":
        await db.card_asset_block(h, verdict["reason"])
    return {"ok": True, "status": verdict["status"], "reason": verdict["reason"]}
