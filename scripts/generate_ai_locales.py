"""
scripts/generate_ai_locales.py — batch-translate every tr()/tr_sync() call
site in the Discord cogs into locales/ai/<lang>.json, using Groq.

Run this:
  - once, to pre-populate translations so normal traffic never pays for a
    live translation call
  - again any time you add/edit a tr("...") string in one of the cogs

It's idempotent and additive: existing entries in locales/ai/<lang>.json
are left alone (and skipped, no re-translation / no wasted API calls) —
only new/changed English templates get sent to Groq. Delete a lang's file
(or a specific key inside it) to force a re-translation.

Usage:
    GROQ_API_KEY=... python scripts/generate_ai_locales.py
    GROQ_API_KEY=... python scripts/generate_ai_locales.py --lang fr,es
    GROQ_API_KEY=... python scripts/generate_ai_locales.py --dry-run
"""

import argparse
import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from i18n import (
    SUPPORTED_LANGUAGES,
    LLM_LANGUAGE_NAME,
    DEFAULT_LANGUAGE,
    _template_hash,
    _load_ai_locale,
    _save_ai_locale,
    _ai_cache,
)
from groq_service import translate_ui_string

COG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "discord_bot", "cogs")

# Call sites we scan for. Both `tr(` (async, awaited in cog code) and
# `tr_sync(` (sync reads) share the same locales/ai/<lang>.json cache, so
# both are collected here.
TARGET_FUNCS = {"tr", "tr_sync"}


def extract_templates_from_file(path: str) -> set[str]:
    """AST-parse a cog file and pull the first (English template) argument
    out of every tr(...)/tr_sync(...) call. AST rather than regex so this
    doesn't get confused by strings containing parens/commas/quotes."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)

    templates = set()

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node):
            func_name = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr

            if func_name in TARGET_FUNCS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    templates.add(first.value)
                # (f-strings / computed exprs as the first arg are skipped —
                # tr() is meant to be called with a literal English template
                # plus **kwargs, not a pre-formatted string. If this shows
                # up in output as "0 templates found" for a file, check for
                # that.)
            self.generic_visit(node)

    Visitor().visit(tree)
    return templates


def collect_all_templates() -> set[str]:
    all_templates = set()
    for fname in sorted(os.listdir(COG_DIR)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(COG_DIR, fname)
        found = extract_templates_from_file(path)
        print(f"  {fname}: {len(found)} template string(s)")
        all_templates |= found
    return all_templates


async def translate_missing(lang: str, templates: set[str], dry_run: bool) -> tuple[int, int]:
    cache = _load_ai_locale(lang)
    lang_name = LLM_LANGUAGE_NAME[lang]
    missing = [t for t in templates if _template_hash(t) not in cache]

    if not missing:
        print(f"[{lang}] up to date ({len(cache)} cached, 0 missing)")
        return 0, 0

    print(f"[{lang}] translating {len(missing)} missing string(s) into {lang_name}...")
    ok, failed = 0, 0
    for text in missing:
        if dry_run:
            print(f"  [dry-run] would translate: {text[:70]!r}")
            continue
        try:
            translated = await translate_ui_string(text, lang_name)
            cache[_template_hash(text)] = translated
            ok += 1
        except Exception as e:
            print(f"  FAILED ({e}): {text[:70]!r}")
            failed += 1

    if not dry_run and ok:
        _ai_cache[lang] = cache
        _save_ai_locale(lang)
    return ok, failed


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", help="Comma-separated language codes (default: all supported except en)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be translated without calling Groq")
    args = parser.parse_args()

    if not os.getenv("GROQ_API_KEY") and not args.dry_run:
        print("GROQ_API_KEY is not set. Set it, or pass --dry-run to just list missing strings.")
        sys.exit(1)

    langs = (
        [c.strip() for c in args.lang.split(",")]
        if args.lang
        else [c for c in SUPPORTED_LANGUAGES if c != DEFAULT_LANGUAGE]
    )
    for c in langs:
        if c not in SUPPORTED_LANGUAGES:
            print(f"Unknown language code: {c} (supported: {', '.join(SUPPORTED_LANGUAGES)})")
            sys.exit(1)

    print(f"Scanning {COG_DIR} for tr()/tr_sync() call sites...")
    templates = collect_all_templates()
    print(f"Found {len(templates)} distinct English template string(s) total.\n")

    total_ok, total_failed = 0, 0
    for lang in langs:
        ok, failed = await translate_missing(lang, templates, args.dry_run)
        total_ok += ok
        total_failed += failed

    print(f"\nDone. {total_ok} translated, {total_failed} failed.")
    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
