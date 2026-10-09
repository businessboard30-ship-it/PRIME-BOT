"""Fails when database._create_tables changes without a SCHEMA_VERSION bump.

Why: the DDL pass is skipped whenever the stored schema_version equals SCHEMA_VERSION, so a table
added without a bump silently never gets created (this caused the dash_* outage fixed in PR #121).

How: we hash the body of _create_tables (whitespace-normalised, comments ignored) and pin
(SCHEMA_VERSION, hash) below. If the hash changes you must bump SCHEMA_VERSION, then update BOTH
numbers here in the same PR. Changing only the hash (leaving the version) fails the test.
"""
import hashlib
import re
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database.py"

PINNED_VERSION = "67"
PINNED_HASH = "692887246f0cec2b"


def _current():
    src = DB.read_text()
    version = re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)
    start = src.index("async def _create_tables")
    m = re.search(r"\n    (?:async )?def |\n(?:async )?def |\nclass ", src[start + 10:])
    body = src[start: start + 10 + (m.start() if m else len(src))]
    body = re.sub(r"(?m)^\s*#.*$", "", body)      # whole-line comments only (a # inside SQL stays)
    body = re.sub(r"\s+", " ", body).strip()
    return version, hashlib.sha256(body.encode()).hexdigest()[:16]


def test_create_tables_change_requires_schema_bump():
    version, digest = _current()
    if digest != PINNED_HASH:
        assert version != PINNED_VERSION, (
            f"_create_tables changed (hash {digest}) but SCHEMA_VERSION is still {version!r}. "
            f"Bump SCHEMA_VERSION in database.py, then set PINNED_VERSION={version!r}->new and "
            f"PINNED_HASH={digest!r} in this test.")
        raise AssertionError(
            f"SCHEMA_VERSION was bumped ({PINNED_VERSION} -> {version}); now update PINNED_VERSION="
            f"{version!r} and PINNED_HASH={digest!r} in {Path(__file__).name}.")
    assert version == PINNED_VERSION, "SCHEMA_VERSION changed without a schema change; revert it (forces a DDL storm)."
