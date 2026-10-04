"""Every literal text("...") key used by the catch code must exist in locales/en.json,
and the call must supply every {placeholder} the string uses. text() silently falls
back to the raw key, so a typo would otherwise ship as visible junk."""
import ast
import json
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FILES = [
    ROOT / "discord_bot" / "cogs" / "catch.py",
    *sorted((ROOT / "discord_bot" / "cogs").glob("_views_catch_*.py")),
    *sorted((ROOT / "modules").glob("catch_*.py")),
]
LOCALE = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))


def _calls():
    for path in FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "text"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                key = node.args[0].value
                key = key if key.startswith("catch.") else f"catch.{key}"
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                has_splat = any(kw.arg is None for kw in node.keywords)
                yield path.name, node.lineno, key, kwargs, has_splat


def test_found_calls():
    assert len(list(_calls())) > 50


def test_every_used_key_exists_in_english_locale():
    missing = [f"{f}:{ln} {key}" for f, ln, key, _, _ in _calls() if key not in LOCALE]
    assert not missing, "missing locale keys:\n" + "\n".join(missing)


def test_calls_supply_every_placeholder():
    problems = []
    for f, ln, key, kwargs, has_splat in _calls():
        value = LOCALE.get(key)
        if not isinstance(value, str) or has_splat:
            continue
        needed = {name for _, name, _, _ in string.Formatter().parse(value) if name}
        absent = needed - kwargs
        if absent:
            problems.append(f"{f}:{ln} {key} lacks {sorted(absent)}")
    assert not problems, "\n".join(problems)
