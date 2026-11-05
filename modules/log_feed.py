"""Send the bot's WARNING/ERROR log lines (and a few key INFO lines) to a Discord channel, so nobody has to open Railway.

Set LOG_CHANNEL_ID (the channel's id) on the process. Unset = feature is off. Only the process that has it set posts, so put it
on the main bot (or on whichever clone you want to watch). Lines are masked with admin_ops.mask_secrets, batched (one message per
flush), capped, and never loop: discord.py's own loggers and this module are ignored, so a failed post can't log its way into
another post. Pure queue/format code here; the sender loop is discord_bot/cogs/log_channel.py.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Deque, List, Optional

from modules.admin_ops import mask_secrets

MAX_QUEUE = 300
MAX_MESSAGE = 1900
LINE_WIDTH = 300
IGNORED_PREFIXES = ("discord", "modules.log_feed", "discord_bot.cogs.log_channel", "aiohttp", "asyncio")
# INFO lines worth seeing live: the AI tool path and bot startup. Everything else INFO stays out (too noisy).
INFO_KEYS = ("[aichat]", "[ai_command]", "[ai_plan]", "[aijobs]", "Logged in as")


AUTO_NAME = "bot-logs"
AUTO_TOPIC = "Private live log feed from the bot (auto-created). Type `version` to see which code is running."


def find_existing(channels) -> Optional[object]:
    """The auto-created channel if it already exists (so a restart never makes a second one)."""
    return next((c for c in channels if getattr(c, "name", "") == AUTO_NAME), None)


def channel_id() -> Optional[int]:
    raw = (os.getenv("LOG_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def wanted(record: logging.LogRecord) -> bool:
    if any(record.name == p or record.name.startswith(p + ".") for p in IGNORED_PREFIXES):
        return False
    if record.levelno >= logging.WARNING:
        return True
    return record.levelno == logging.INFO and any(k in record.getMessage() for k in INFO_KEYS)


def format_record(record: logging.LogRecord) -> str:
    msg = record.getMessage()
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        msg = f"{msg} | {type(exc).__name__}: {exc}"
    body = mask_secrets(re.sub(r"\s+", " ", f"{record.name}: {msg}").strip()).replace("```", "'''")
    stamp = time.strftime("%H:%M:%S", time.gmtime(record.created))
    line = f"{stamp} {logging.getLevelName(record.levelno)[:4]} {body}"
    return line if len(line) <= LINE_WIDTH else line[: LINE_WIDTH - 1] + "…"


class QueueHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: Deque[str] = deque(maxlen=MAX_QUEUE)
        self.dropped = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not wanted(record):
                return
            line = format_record(record)
            with self._lock:
                if len(self.lines) == self.lines.maxlen:
                    self.dropped += 1
                self.lines.append(line)
        except Exception:
            self.handleError(record)

    def drain(self) -> List[str]:
        with self._lock:
            out = list(self.lines)
            self.lines.clear()
            dropped, self.dropped = self.dropped, 0
        if dropped:
            out.insert(0, f"... {dropped} older line(s) dropped (too many at once)")
        return out


_handler: Optional[QueueHandler] = None


def install() -> QueueHandler:
    """Attach once to the root logger. Safe to call repeatedly."""
    global _handler
    root = logging.getLogger()
    if _handler is None:
        _handler = QueueHandler()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    return _handler


def chunks(lines: List[str], label: str = "") -> List[str]:
    """Pack lines into code-block messages under Discord's limit. Never returns an empty message."""
    head = f"**{label}**\n" if label else ""
    out, cur = [], []
    size = len(head) + 8
    for ln in lines:
        if cur and size + len(ln) + 1 > MAX_MESSAGE:
            out.append(cur)
            cur, size = [], len(head) + 8
        cur.append(ln[: MAX_MESSAGE - len(head) - 12])
        size += len(cur[-1]) + 1
    if cur:
        out.append(cur)
    return [f"{head}```\n" + "\n".join(c) + "\n```" for c in out]
