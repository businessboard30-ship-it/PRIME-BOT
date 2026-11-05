"""Which code is this process running? Railway sets RAILWAY_GIT_COMMIT_SHA / _MESSAGE / _BRANCH on every deploy; locally we fall back to git.
Used by the log channel (startup line + typing `version` there). Pure, never raises."""
import os
import subprocess
import time
from typing import Dict

STARTED_AT = time.time()


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def info() -> Dict[str, str]:
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or _git("rev-parse", "HEAD")
    msg = os.getenv("RAILWAY_GIT_COMMIT_MESSAGE") or _git("log", "-1", "--format=%s")
    branch = os.getenv("RAILWAY_GIT_BRANCH") or _git("rev-parse", "--abbrev-ref", "HEAD")
    return {"sha": (sha or "unknown")[:7], "message": (msg or "unknown").splitlines()[0][:120] if (msg or "unknown") else "unknown",
            "branch": branch or "unknown", "deployment": (os.getenv("RAILWAY_DEPLOYMENT_ID") or "")[:8]}


def features() -> Dict[str, bool]:
    """Markers that prove specific recent code is loaded (checked by import, not by trusting the commit text)."""
    out = {}
    try:
        from discord_bot.cogs import ai_tools
        out["AI second turn (#551)"] = hasattr(ai_tools.AITools if hasattr(ai_tools, "AITools") else ai_tools, "_second_turn") or any(
            hasattr(v, "_second_turn") for v in vars(ai_tools).values() if isinstance(v, type))
    except Exception:
        out["AI second turn (#551)"] = False
    try:
        from modules import command_reference
        out["AI act-don't-ask rule (#549/#550)"] = "ACT, don't ask" in command_reference.build_context(runnable={"x"})
    except Exception:
        out["AI act-don't-ask rule (#549/#550)"] = False
    return out


def uptime_text(now: float = None) -> str:
    secs = int((now or time.time()) - STARTED_AT)
    h, rem = divmod(max(0, secs), 3600)
    return f"{h}h {rem // 60}m" if h else f"{rem // 60}m {rem % 60}s"


def summary(label: str = "") -> str:
    i = info()
    lines = [f"**Running{(' ' + label) if label else ''}:** `{i['sha']}` on `{i['branch']}` — {i['message']}"]
    if i["deployment"]:
        lines.append(f"Deployment `{i['deployment']}`")
    lines.append(f"Up for {uptime_text()}")
    lines += [f"{'✅' if ok else '❌'} {name}" for name, ok in features().items()]
    if i["sha"] == "unknown":
        lines.append("⚠️ No commit info: Railway's git variables are missing on this service.")
    return "\n".join(lines)
