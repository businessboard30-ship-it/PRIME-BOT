"""
Owner-panel operations helpers (Batch 2): log tail, config viewer and database
tools. Pure functions + small async DB helpers; the screens live in
discord_bot/cogs/_views_admin_panel_ops.py.

Nothing here is a new command. Everything that could leak a secret goes through
`mask_secrets()` / the config classifier below, and the rule is "when unsure,
hide it": a harmless setting shown as masked is fine, a token shown is not.
"""

from __future__ import annotations

import logging
import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── secret masking ───────────────────────────────────────────────────────

MASK = "***"

_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Discord bot token: id.timestamp.hmac
    (re.compile(r"[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"), MASK),
    # JWT-looking blobs
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), MASK),
    # Authorization headers
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + MASK),
    # Provider-prefixed keys (Stripe/Paystack, OpenAI/Groq, GitHub, Slack, Google)
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{6,}"), MASK),
    (re.compile(r"\b(?:sk|gsk)[-_][A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"), MASK),
    # Credentials inside connection URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s:@/]+:[^\s@]+@"), r"\1" + MASK + "@"),
    # key=value / key: value where the key name looks sensitive
    (re.compile(r"(?i)\b(token|secret|password|passwd|pwd|api[_-]?key|apikey|authorization|credential)s?"
                r"(\s*[=:]\s*)(?!\*\*\*)[^\s,;&'\"]+"), r"\1\2" + MASK),
    # Any other long unbroken token-like run (hex, base64, urlsafe)
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), MASK),
]


def mask_secrets(text: Any) -> str:
    """Return `text` with anything that looks like a credential replaced by ***."""
    out = "" if text is None else str(text)
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


# ── log tail ─────────────────────────────────────────────────────────────

LOG_CAPACITY = 200
LEVELS = {"errors": logging.ERROR, "warnings": logging.WARNING}


@dataclass
class LogEntry:
    ts: float
    level: int
    logger_name: str
    message: str


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` WARNING+ records in memory (this process only)."""

    def __init__(self, capacity: int = LOG_CAPACITY, level: int = logging.WARNING):
        super().__init__(level=level)
        self.entries: Deque[LogEntry] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                msg = f"{msg} | {type(exc).__name__}: {exc}"
            self.entries.append(LogEntry(record.created, record.levelno, record.name, msg))
        except Exception:  # a logging handler must never raise
            self.handleError(record)


_handler: Optional[RingBufferHandler] = None


def install_log_buffer() -> RingBufferHandler:
    """Attach the ring buffer to the root logger once. Safe to call repeatedly."""
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = RingBufferHandler()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def recent_logs(limit: int = 20, mode: str = "errors") -> List[LogEntry]:
    """Newest first. mode: 'errors' (ERROR+) or 'warnings' (WARNING+)."""
    if _handler is None:
        return []
    floor = LEVELS.get(mode, logging.ERROR)
    picked = [e for e in _handler.entries if e.level >= floor]
    return list(reversed(picked))[:limit]


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def format_log_line(entry: LogEntry, width: int = 140) -> str:
    """One masked, single-line, code-block-safe line."""
    stamp = time.strftime("%H:%M:%S", time.gmtime(entry.ts))
    level = logging.getLevelName(entry.level)[:4]
    body = mask_secrets(_one_line(f"{entry.logger_name}: {entry.message}")).replace("```", "'''")
    line = f"{stamp} {level} {body}"
    return line if len(line) <= width else line[: width - 1] + "…"


def logs_as_text(mode: str = "warnings") -> str:
    """Whole buffer, oldest first, masked, for the 'send as file' button."""
    if _handler is None:
        return "(log buffer is not installed)\n"
    floor = LEVELS.get(mode, logging.WARNING)
    lines = []
    for e in _handler.entries:
        if e.level < floor:
            continue
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e.ts))
        lines.append(f"{stamp}Z {logging.getLevelName(e.level)} {e.logger_name}: "
                     f"{mask_secrets(_one_line(e.message))}")
    return ("\n".join(lines) + "\n") if lines else "(nothing logged at this level yet)\n"


# ── config viewer ────────────────────────────────────────────────────────

# Names containing any of these are treated as secrets and never displayed.
_SECRET_NAME_HINTS = (
    "TOKEN", "SECRET", "KEY", "PASSWORD", "PASSWD", "DSN", "DATABASE_URL", "WEBHOOK",
    "CREDENTIAL", "PRIVATE", "SALT", "AUTH", "COOKIE", "SESSION", "SIGNATURE",
)
_CRED_URL = re.compile(r"://[^\s/@]*@")


@dataclass
class ConfigEntry:
    name: str
    shown: str
    secret: bool


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(h in upper for h in _SECRET_NAME_HINTS)


def _looks_secret_value(value: str) -> bool:
    return mask_secrets(value) != value or bool(_CRED_URL.search(value))


def config_entries(module: Any, query: str = "") -> List[ConfigEntry]:
    """Describe the UPPER_CASE settings of `module`, secrets masked.

    * secret-looking names (or values) show only 'set' / 'not set'
    * numbers and booleans are shown as-is
    * strings are clipped and passed through mask_secrets
    * collections (e.g. ID sets) show only their size
    Everything else (functions, modules, objects) is skipped.
    """
    q = query.strip().lower()
    out: List[ConfigEntry] = []
    for name in sorted(vars(module)):
        if name.startswith("_") or not name.isupper():
            continue
        if q and q not in name.lower():
            continue
        value = getattr(module, name)
        if callable(value) or isinstance(value, type(logging)):
            continue
        secret_name = _is_secret_name(name)
        if isinstance(value, (set, frozenset, list, tuple, dict)):
            if secret_name:
                out.append(ConfigEntry(name, "🔒 set" if value else "🔒 not set", True))
            else:
                out.append(ConfigEntry(name, f"{len(value)} item{'s' if len(value) != 1 else ''}", False))
        elif isinstance(value, bool):
            out.append(ConfigEntry(name, str(value), False))
        elif isinstance(value, (int, float)):
            out.append(ConfigEntry(name, "🔒 set" if secret_name else str(value), secret_name))
        elif isinstance(value, str) or value is None:
            text = value or ""
            if secret_name or _looks_secret_value(text):
                out.append(ConfigEntry(name, "🔒 set" if text else "🔒 not set", True))
            else:
                clipped = text if len(text) <= 60 else text[:59] + "…"
                out.append(ConfigEntry(name, clipped.replace("`", "'") or "(empty)", False))
    return out


# ── database tools ───────────────────────────────────────────────────────

# Fixed whitelist: names are interpolated into SQL, so they must never come
# from user input. Tables that don't exist on a given deployment show "n/a".
COUNT_TABLES = (
    "users", "discord_guilds", "discord_guild_subscriptions", "discord_cloned_bots",
    "payment_logs", "discord_clone_pending_payments", "submissions", "ad_submissions",
    "admin_panel_audit", "bot_blacklist",
)
_TABLE_OK = re.compile(r"^[a-z_][a-z0-9_]*$")
STALE_HOURS = 72   # same window db.expire_old_pending_payments() already uses
COUNT_TIMEOUT = 2.5      # per table; a slow exact COUNT(*) falls back to Postgres' own estimate
STALE_TIMEOUT = 10.0


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


def pool_status(pool: Any) -> dict:
    """Connections in use / idle / limits, tolerant of a pool that lacks a getter."""
    def call(name):
        fn = getattr(pool, name, None)
        try:
            return int(fn()) if fn else None
        except Exception:
            return None
    size, idle = call("get_size"), call("get_idle_size")
    return {"size": size, "idle": idle,
            "busy": (size - idle) if size is not None and idle is not None else None,
            "min": call("get_min_size"), "max": call("get_max_size")}


async def _estimate(conn: Any, table: str) -> Optional[int]:
    """Postgres' own row estimate (instant, from planner stats). None when the
    table has never been analysed (reltuples = -1) or the lookup fails."""
    try:
        n = await conn.fetchval("SELECT reltuples::bigint FROM pg_class WHERE relname = $1", table, timeout=COUNT_TIMEOUT)
        return int(n) if n is not None and int(n) >= 0 else None
    except Exception:
        return None


async def table_counts(conn: Any, approx: Optional[set] = None) -> List[Tuple[str, Optional[int]]]:
    """Row counts. A table whose exact COUNT(*) TIMES OUT (big table, slow
    database) falls back to the planner estimate and is added to `approx` so
    the screen can mark it with "~". A missing table or any other error is
    "n/a", exactly as before."""
    out: List[Tuple[str, Optional[int]]] = []
    for table in COUNT_TABLES:
        if not _TABLE_OK.match(table):
            continue
        try:
            n = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"', timeout=COUNT_TIMEOUT)
            out.append((table, int(n)))
        except asyncio.TimeoutError:
            est = await _estimate(conn, table)
            if est is not None and approx is not None:
                approx.add(table)
            out.append((table, est))
        except Exception:
            out.append((table, None))
    return out


async def db_overview() -> dict:
    pool = await _pool()
    approx: set = set()
    async with pool.acquire() as conn:
        counts = await table_counts(conn, approx)
        stale = await conn.fetchval(
            "SELECT COUNT(*) FROM payment_logs WHERE status = 'pending' "
            "AND created_date < NOW() - ($1 || ' hours')::INTERVAL", str(STALE_HOURS), timeout=STALE_TIMEOUT)
    return {"pool": pool_status(pool), "counts": counts, "approx": approx, "stale_payments": int(stale or 0)}


async def run_stale_payment_cleanup() -> int:
    """Marks abandoned 'pending' checkouts as 'expired' using the existing,
    already-tested sweep. Non-destructive: rows are kept, only their status
    changes. Returns how many were flipped."""
    from database import db
    return int(await db.expire_old_pending_payments(STALE_HOURS))
