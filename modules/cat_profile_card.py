# path: modules/cat_profile_card.py

"""Drawn card for a player's cat profile.

Shows only what the plain profile embed already shows publicly: the player's Discord picture and display name, their
bio, collection size, rarest cat, badge count and up to 3 showcase cats with their pictures. Nothing private (no
coins, no IDs, no care state).

Pillow at 2x then shrunk (same approach as the catch cards). Pictures are downloaded only from Discord's own CDN,
with short timeouts and a size cap. ANY failure means "no card": the caller keeps the plain embed.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from PIL import Image, ImageDraw

from modules.catch_card import SCALE, _font, _fit_font, _mix, _s, _text_w

logger = logging.getLogger(__name__)

W, H = 900, 470
FILE_NAME = "cat_profile.png"
MAX_THUMBS = 3
FETCH_TIMEOUT = 3.0
MAX_IMAGE_BYTES = 6 * 1024 * 1024
CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

_PINK_TOP, _CREAM = (255, 226, 236), (255, 248, 230)
_INK, _MUTED, _GOLD, _ROSE, _WHITE = (60, 36, 52), (140, 112, 128), (240, 180, 60), (232, 120, 160), (255, 255, 255)
_RARITY_TINT = {"common": (150, 150, 160), "uncommon": (90, 170, 120), "rare": (90, 130, 220), "legendary": (240, 180, 60)}

_gate = None


def clean_text(text, limit: int = 200) -> str:
    """Text the font can draw: the font has no emoji or symbol glyphs, so those would show as empty boxes."""
    out = "".join(ch for ch in str(text or "") if ch == "\n" or (ord(ch) >= 32 and ord(ch) < 0x2190))
    return " ".join(out.split())[:limit]


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> List[str]:
    lines: List[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if _text_w(draw, trial, font) <= max_w * SCALE or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and " ".join(lines) != text:       # cut: mark it
        last = lines[-1]
        while last and _text_w(draw, last + "...", font) > max_w * SCALE:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rounded(img: Image.Image, w: int, h: int, radius: int) -> Image.Image:
    """Crop to fill w x h (no stretching), with rounded corners."""
    img = img.convert("RGBA")
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    left, top = (img.width - w) // 2, (img.height - h) // 2
    img = img.crop((left, top, left + w, top + h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _open(data: Optional[bytes]) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None


def draw_card(name: str, bio: str, collection: int, rarest: Optional[tuple], badge_count: int,
              showcase: Sequence[tuple], avatar: Optional[bytes], thumbs: Sequence[Optional[bytes]]) -> bytes:
    """`rarest` = (name, rarity) or None. `showcase` = [(name, trait, rarity)], `thumbs` = their pictures (or None)."""
    img = Image.new("RGB", (W * SCALE, H * SCALE), _CREAM)
    draw = ImageDraw.Draw(img)
    for y in range(H * SCALE):                                              # soft vertical gradient
        draw.line((0, y, W * SCALE, y), fill=_mix(_PINK_TOP, _CREAM, y / (H * SCALE)))
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((_s(8), _s(8), _s(W - 8), _s(H - 8)), radius=_s(28), outline=_GOLD, width=_s(4))

    # avatar with a gold ring
    ring, size = 176, 160
    draw.ellipse((_s(36), _s(36), _s(36 + ring), _s(36 + ring)), fill=_GOLD)
    pic = _open(avatar)
    face = _circle(pic, _s(size)) if pic is not None else None
    if face is None:                                                         # no picture: a plain rose disc
        face = Image.new("RGBA", (_s(size), _s(size)), (0, 0, 0, 0))
        ImageDraw.Draw(face).ellipse((0, 0, _s(size) - 1, _s(size) - 1), fill=_ROSE)
    img.alpha_composite(face, (_s(36 + (ring - size) / 2), _s(36 + (ring - size) / 2)))
    draw = ImageDraw.Draw(img)

    # name, caption, bio
    x0 = 242
    draw.text((_s(x0), _s(44)), "CAT PROFILE", font=_font(18), fill=_ROSE, anchor="lm")
    draw.text((_s(x0), _s(86)), clean_text(name, 40) or "Player", font=_fit_font(draw, clean_text(name, 40) or "Player", W - x0 - 40, 44, 24),
              fill=_INK, anchor="lm")
    bio_font = _font(22)
    text = clean_text(bio, 240)
    lines = _wrap(draw, text, bio_font, W - x0 - 40, 3) if text else []
    if not lines:
        lines, color = ["No bio yet. Their cats are waiting for a story."], _MUTED
    else:
        color = _INK
    for i, line in enumerate(lines):
        draw.text((_s(x0), _s(138 + i * 30)), line, font=bio_font, fill=color, anchor="lm")

    # three stat tiles
    tiles = (("COLLECTION", f"{int(collection):,}", "cats"),
             ("RAREST", clean_text(rarest[0], 16) if rarest else "None yet", rarest[1].title() if rarest else ""),
             ("BADGES", f"{int(badge_count):,}", "earned"))
    tw, gap, ty, th = 270, 18, 236, 92
    for i, (label, big, small) in enumerate(tiles):
        x = 36 + i * (tw + gap)
        draw.rounded_rectangle((_s(x), _s(ty), _s(x + tw), _s(ty + th)), radius=_s(18), fill=_WHITE, outline=(245, 214, 226), width=_s(2))
        draw.text((_s(x + 18), _s(ty + 20)), label, font=_font(14), fill=_MUTED, anchor="lm")
        draw.text((_s(x + 18), _s(ty + 49)), big, font=_fit_font(draw, big, tw - 36, 30, 16), fill=_INK, anchor="lm")
        if small:
            draw.text((_s(x + 18), _s(ty + 78)), small, font=_font(14), fill=_ROSE, anchor="lm")

    # showcase
    draw.text((_s(36), _s(352)), "SHOWCASE", font=_font(14), fill=_MUTED, anchor="lm")
    if not showcase:
        draw.text((_s(36), _s(404)), "No cats pinned yet.", font=_font(20), fill=_MUTED, anchor="lm")
    for i, (cname, trait, rarity) in enumerate(list(showcase)[:MAX_THUMBS]):
        x, y, cw_, ch_ = 36 + i * (tw + gap), 368, tw, 78
        tint = _RARITY_TINT.get(rarity, _MUTED)
        draw.rounded_rectangle((_s(x), _s(y), _s(x + cw_), _s(y + ch_)), radius=_s(16), fill=_WHITE, outline=tint, width=_s(3))
        thumb = _open(thumbs[i]) if i < len(thumbs) else None
        if thumb is not None:
            img.alpha_composite(_rounded(thumb, _s(62), _s(62), _s(12)), (_s(x + 8), _s(y + 8)))
            draw = ImageDraw.Draw(img)
        tx = x + (80 if thumb is not None else 16)
        title = clean_text(cname, 16) or "Cat"
        draw.text((_s(tx), _s(y + 28)), title, font=_fit_font(draw, title, cw_ - (tx - x) - 10, 22, 14), fill=_INK, anchor="lm")
        draw.text((_s(tx), _s(y + 54)), f"{clean_text(trait, 14)}  -  {rarity.title()}", font=_font(14), fill=tint, anchor="lm")

    out = io.BytesIO()
    img.convert("RGB").resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


# ── fetching the pictures ─────────────────────────────────────────────────

def _allowed(url: str) -> bool:
    try:
        p = urlparse(str(url))
        return p.scheme == "https" and (p.hostname or "") in CDN_HOSTS
    except Exception:
        return False


async def _download(url: Optional[str]) -> Optional[bytes]:
    if not url or not _allowed(url):
        return None
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200 or int(resp.headers.get("Content-Length") or 0) > MAX_IMAGE_BYTES:
                    return None
                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
                return data if len(data) <= MAX_IMAGE_BYTES else None
    except Exception:
        return None


async def _avatar_bytes(user) -> Optional[bytes]:
    try:
        asset = user.display_avatar.replace(size=256, format="png")
        return await asyncio.wait_for(asset.read(), FETCH_TIMEOUT)
    except Exception:
        return None


async def render(client, user, display_name: str, profile: dict, badges) -> Optional[bytes]:
    """The finished PNG, or None (the caller then keeps the plain embed). Never raises."""
    global _gate
    try:
        from modules import cat_pool
        if _gate is None:
            _gate = asyncio.Semaphore(3)
        pinned = list(profile.get("pinned") or [])[:MAX_THUMBS]

        async def thumb(c):
            try:
                return await _download(await asyncio.wait_for(cat_pool.url_for(client, c.pool_id), FETCH_TIMEOUT))
            except Exception:
                return None
        avatar, *pics = await asyncio.gather(_avatar_bytes(user), *[thumb(c) for c in pinned])
        rarest = profile.get("rarest")
        async with _gate:
            return await asyncio.to_thread(
                draw_card, display_name, profile.get("bio") or "", int(profile.get("collection_size") or 0),
                (rarest.name, rarest.rarity) if rarest else None, len(badges or ()),
                [(c.name, c.trait, c.rarity) for c in pinned], avatar, pics)
    except Exception:
        logger.warning("[cat-profile-card] couldn't draw the card", exc_info=True)
        return None
