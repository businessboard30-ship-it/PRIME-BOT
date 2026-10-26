"""The hero ring must never turn into a black disc: it has to look right even with a stale or missing stylesheet."""
import glob
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "website"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "assets/site.css").read_text(encoding="utf-8")
HEADERS = (ROOT / "_headers").read_text(encoding="utf-8")
SVG = re.search(r'<svg viewBox="0 0 520 520".*?</svg>', INDEX, re.S).group(0)


def _tree():
    xml = SVG.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" ', 1)
    xml = xml.replace(" href=", " xlink:href=").replace("&#160;", "\u00a0").replace("focusable=", "data-f=")
    return ET.fromstring(xml)


def test_no_inline_style_attributes_because_the_csp_blocks_them():
    assert "style-src 'self'" in HEADERS and "unsafe-inline" not in HEADERS
    assert ' style="' not in SVG
    assert ' style="' not in INDEX                                     # nothing else on the page relies on them either


def test_every_shape_has_its_own_fill_and_stroke_so_missing_css_is_harmless():
    ns = "{http://www.w3.org/2000/svg}"
    root = _tree()
    shapes = [e for e in root.iter() if e.tag in (ns + "circle", ns + "polygon")]
    rings = [e for e in shapes if "orb-ring" in e.get("class", "")]
    assert len(rings) == 5
    for e in rings:
        assert e.get("fill") == "none" and e.get("stroke"), e.attrib             # without this a ring paints as a solid black disc
    core = [e for e in shapes if "orb-core" in e.get("class", "")][0]
    assert core.get("fill") and core.get("fill") != "none"
    text = next(root.iter(ns + "text"))
    assert text.get("fill") and text.get("font-size") and text.get("font-family")


def test_the_svg_has_a_size_even_when_the_stylesheet_does_not_load():
    root = _tree()
    assert root.get("width") and root.get("height") and root.get("viewBox") == "0 0 520 520"


def test_css_still_sizes_and_colours_it_and_the_attributes_never_win_over_it():
    assert ".orb svg{display:block;width:100%;height:100%" in CSS               # CSS beats the width/height attributes
    assert ".orb-ring{fill:none;stroke:currentColor" in CSS and ".orb-name text{" in CSS


def test_the_colour_stagger_comes_from_css_classes_not_inline_styles():
    tspans = len(re.findall(r"<tspan", SVG))
    rules = re.findall(r"\.orb-name tspan:nth-child\((\d+)\)\{animation-delay:(-[\d.]+)s\}", CSS)
    assert tspans >= 40 and len(rules) == tspans - 1                            # the first letter needs no delay
    assert rules[0] == ("2", "-0.27") and float(rules[-1][1]) == round(-0.27 * (tspans - 1), 2)
    for d in set(re.findall(r'class="orb-ring[^"]* d(\d+)', SVG)):
        assert f".orb-ring.d{d}{{--d:-{d}s}}" in CSS


def test_css_and_js_always_revalidate_so_a_new_page_never_gets_an_old_stylesheet():
    assert "stale-while-revalidate" not in HEADERS and "/assets/*\n" not in HEADERS
    for name in ("site.css", "site.js", "boot.js", "config.js", "theme.js"):
        block = re.search(rf"^/assets/{re.escape(name)}\n  Cache-Control: ([^\n]+)", HEADERS, re.M)
        assert block and "must-revalidate" in block.group(1) and "max-age=0" in block.group(1), name


def test_every_page_links_the_same_versioned_assets():
    pages = glob.glob(str(ROOT / "*.html")) + glob.glob(str(ROOT / "*/index.html"))
    assert len(pages) >= 14
    versions = set()
    for page in pages:
        html = Path(page).read_text(encoding="utf-8")
        for name in ("site.css", "site.js", "boot.js", "config.js", "theme.js"):
            links = re.findall(rf'/assets/{re.escape(name)}([^"]*)"', html)
            for q in links:
                assert re.fullmatch(r"\?v=\d+", q), (page, name, q)
                versions.add(q)
    assert len(versions) == 1                                                    # one version everywhere, bumped together
