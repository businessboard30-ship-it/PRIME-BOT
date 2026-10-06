#!/usr/bin/env python3
"""Validate a Catch asset manifest before it is loaded by the bot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules.catch_assets import import_manifest


def read_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("asset manifest must contain a JSON array")
    records = [record for record in payload if isinstance(record, dict)]
    if len(records) != len(payload):
        raise ValueError("every asset manifest entry must be an object")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Catch asset manifest")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        refs = import_manifest(read_manifest(args.manifest))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"validated {len(refs)} asset version(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
