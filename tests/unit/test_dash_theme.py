"""Static checks for the dashboard light theme (Part E of the user/developer dashboard plan)."""
import re
from pathlib import Path

DASH = Path(__file__).resolve().parents[2] / "dashboard"
CSS = (DASH / "assets" / "dash.css").read_text(encoding="utf-8")
HTML = (DASH / "index.html").read_text(encoding="utf-8")
THEME_JS = (DASH / "assets" / "theme.js").read_text(encoding="utf-8")


def _block(selector_regex):
    m = re.search(selector_regex + r"\{(.*?)\}", CSS, re.S | re.M)
    assert m, selector_regex
    return m.group(1)


def _vars(block):
    return dict(re.findall(r"--([a-z0-9-]+):([^;]+);", block))


DARK = _vars(_block(r"^:root"))
LIGHT = _vars(_block(r':root\[data-theme="light"\]'))
AUTO_LIGHT = _vars(re.search(r"@media \(prefers-color-scheme: light\)\{\s*:root:not\(\[data-theme\]\)\{(.*?)\}\s*\}", CSS, re.S).group(1))


def _rgba(value, over=(255, 255, 255)):
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    nums = [float(n) for n in re.findall(r"[\d.]+", value)]
    r, g, b = nums[:3]
    a = nums[3] if len(nums) > 3 else 1.0
    return tuple(round(a * c + (1 - a) * o) for c, o in zip((r, g, b), over))


def _lum(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_light_palette_defines_every_dark_variable():
    layout_only = {"mono", "sans", "hdr", "side"}
    missing = {k for k in DARK if k not in LIGHT and k not in layout_only}
    assert not missing, f"light palette missing: {sorted(missing)}"


def test_auto_block_matches_explicit_light_block():
    assert AUTO_LIGHT == LIGHT


def test_components_use_variables_not_colour_literals():
    body = CSS[CSS.index("*{box-sizing"):]
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", body)
    allowed = {"#fff", "#000"}  # skip-to-content link is deliberately fixed high contrast
    leftovers = [x for x in literals if x not in allowed and not x.startswith("rgba(var")]
    assert not leftovers, f"hard-coded colours outside the palette: {leftovers}"


def test_light_text_contrast_meets_wcag_aa():
    bg = _rgba(LIGHT["bg"])
    surface = _rgba(LIGHT["surface"], over=bg)
    field = _rgba(LIGHT["field"], over=bg)
    pairs = {
        "text/bg": (LIGHT["text"], bg), "text/surface": (LIGHT["text"], surface),
        "strong/bg": (LIGHT["strong"], bg), "muted/bg": (LIGHT["muted"], bg), "muted/surface": (LIGHT["muted"], surface),
        "ok/bg": (LIGHT["ok"], bg), "bad/bg": (LIGHT["bad"], bg), "warn/bg": (LIGHT["warn"], bg),
        "accent/bg": (LIGHT["accent"], bg), "accent/surface": (LIGHT["accent"], surface),
        "strong/field": (LIGHT["strong"], field), "pfg/pbg": (LIGHT["pfg"], _rgba(LIGHT["pbg"])),
        "ok/surface": (LIGHT["ok"], surface), "bad/surface": (LIGHT["bad"], surface), "warn/surface": (LIGHT["warn"], surface),
    }
    for name, (fg, back) in pairs.items():
        r = _ratio(_rgba(fg, over=back), back)
        assert r >= 4.5, f"{name} contrast {r:.2f} < 4.5"


def test_dark_text_contrast_still_ok():
    bg = _rgba(DARK["bg"])
    for k in ("text", "muted", "ok", "bad", "warn", "accent"):
        assert _ratio(_rgba(DARK[k], over=bg), bg) >= 4.5, k


def test_theme_script_loads_first_and_is_csp_safe():
    assert '<script src="assets/theme.js"></script>' in HTML
    assert HTML.index("assets/theme.js") < HTML.index("assets/dash.css")
    assert "<script>" not in HTML and "onclick=" not in HTML  # CSP is script-src 'self'
    assert 'content="light dark"' in HTML and 'id="themeBtn"' in HTML


def test_theme_script_stores_only_the_display_preference():
    keys = set(re.findall(r'localStorage\.(?:get|set|remove)Item\(([^,)]+)', THEME_JS))
    assert keys == {"KEY"}
    assert 'var KEY = "primebot.dash.theme"' in THEME_JS
    assert "innerHTML" not in THEME_JS and "eval(" not in THEME_JS


def test_theme_modes_and_background_follow_theme():
    assert 'MODES = ["auto", "light", "dark"]' in THEME_JS
    bg = (DASH / "assets" / "bg.js").read_text(encoding="utf-8")
    assert "--fx-line" in bg and "pb-theme" in bg
    assert "--fx-line" in CSS and "--fx-op" in CSS


def test_no_hardcoded_logo_colours_that_vanish_on_light():
    dash_js = (DASH / "assets" / "dash.js").read_text(encoding="utf-8")
    assert "#f2faff" not in HTML and "#f2faff" not in dash_js
