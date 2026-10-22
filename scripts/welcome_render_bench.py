"""Welcome Card Studio: how long does one card take to draw?  (Phase 0 audit item, done in Phase 9d.)

Usage:  python scripts/welcome_render_bench.py [runs]        (default 5 runs per case, prints min / median / max in ms)

Draws a synthetic 1536 px wide photo (noisy, so JPEG-like decode cost is realistic) and a synthetic avatar. Cases go from
the plain Ultra look to every studio effect switched on at once. It only measures the renderer (`render_welcome_card`);
it does not download anything. Numbers depend on the machine, so compare cases with each other, not with a fixed budget.
"""
import io
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402

from modules.welcome_card import render_welcome_card  # noqa: E402


def _photo(w=1536, h=1024) -> bytes:
    rnd = random.Random(7)
    img = Image.effect_noise((w, h), 60).convert("RGB")
    tint = Image.new("RGB", (w, h), (rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(40, 200)))
    img = Image.blend(img, tint, 0.55)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _avatar() -> bytes:
    img = Image.effect_noise((256, 256), 80).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


CASES = {
    "plain Ultra look": {},
    "zoom + pan + frame thick": {"zoom": 150, "pan_x": 30, "pan_y": -20, "frame": "thick", "frame_color": "gold"},
    "photo-print frame": {"frame": "polaroid"},
    "film-strip frame": {"frame": "film"},
    "blur picture": {"effect": "blur"},
    "grain picture": {"effect": "grain"},
    "thick text outline": {"text_outline": "thick"},
    "text glow + avatar glow": {"text_outline": "glow", "avatar_glow": True},
    "everything on": {"zoom": 125, "frame": "glow", "effect": "sparkle", "brightness": "light", "text_outline": "glow",
                      "avatar_glow": True, "avatar_pos": "top-right", "text_pos": "bottom-left",
                      "heading_font": "gothic", "font": "gothic", "glass": True, "shadow": True, "ring": True},
}


def main(runs: int = 5) -> None:
    photo, avatar = _photo(), _avatar()
    print(f"photo {len(photo) // 1024} KB, 1536x1024 source, {runs} runs each\n")
    print(f"{'case':28} {'min':>7} {'median':>7} {'max':>7}   output")
    for name, opts in CASES.items():
        times = []
        size = ""
        for _ in range(runs):
            t0 = time.perf_counter()
            data, fmt = render_welcome_card(avatar, "Sample Member", "Member #1234", guild_name="Sample Server",
                                            custom_background_bytes=photo, ultra_options=opts)
            times.append((time.perf_counter() - t0) * 1000)
            size = f"{fmt} {len(data) // 1024} KB"
        print(f"{name:28} {min(times):7.0f} {statistics.median(times):7.0f} {max(times):7.0f}   {size}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
