"""
Process supervisor for Discord clone bots.

Why this exists: Telegram clones are cheap to multiplex — one shared
serverless webhook routes incoming updates to the right token by clone_id
(see api/bot.py, clone_service.py). Discord bots don't work that way: each
bot token needs its own persistent gateway (WebSocket) connection, so a
"Discord clone" can't just be a routing rule — it has to be an actual
running process, one per clone.

This script is that supervisor. It polls discord_cloned_bots for rows with
status='active' and keeps exactly one `python -m discord_bot.bot --clone-id
N` subprocess alive per row: starting new ones, stopping ones that were
deactivated, and restarting ones that crashed (with exponential backoff so
a bad/revoked token doesn't spin-loop forever).

Run with (from the bot/ directory): python -m discord_bot.clone_manager

Deploy this as its own long-running service on a host that keeps processes
alive — a Railway service, a VPS with systemd, a Docker container with a
restart policy. It will NOT work on Vercel or any other request-driven
serverless platform, because there is no request to trigger it and no way
to keep a background loop running between invocations.
"""

import asyncio
import logging
import subprocess
import sys
import time
from typing import Dict, Optional

from database import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("discord_bot.clone_manager")

POLL_INTERVAL_SECONDS = 30
INITIAL_RESTART_BACKOFF_SECONDS = 15
MAX_RESTART_BACKOFF_SECONDS = 300


class ManagedClone:
    """Tracks one clone's subprocess and its own restart backoff, so one
    misbehaving clone's crash loop doesn't affect any other clone."""

    def __init__(self, clone_id: int, label: str):
        self.clone_id = clone_id
        self.label = label
        self.process: Optional[subprocess.Popen] = None
        self.backoff = INITIAL_RESTART_BACKOFF_SECONDS
        self.next_restart_at = 0.0

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self):
        logger.info(f"Starting clone #{self.clone_id} ({self.label})")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "discord_bot.bot", "--clone-id", str(self.clone_id)],
        )

    def stop(self):
        if self.is_running():
            logger.info(f"Stopping clone #{self.clone_id} ({self.label})")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning(f"Clone #{self.clone_id} didn't exit in time — killing it")
                self.process.kill()
        self.process = None


def _kill_orphaned_clone_processes():
    """Finds and kills any `discord_bot.bot --clone-id ...` processes still
    alive from a PREVIOUS run of this supervisor.

    `managed` (in _reconcile) only tracks subprocesses THIS process object
    has spawned, in memory. If the supervisor itself gets restarted — host
    redeploy, crash, systemd/Railway auto-restart, anything that doesn't
    go through main()'s graceful `finally: m.stop()` — those child
    processes are never terminated; they just keep running as orphans. The
    next supervisor start then has an empty `managed` dict, sees every
    active clone as "not yet started", and spawns a brand new subprocess
    for each one on top of the orphans that are still there.

    Net effect: two live gateway connections for the same clone token,
    both with every cog (including automod's on_message_delete/
    on_member_join listeners) loaded — so every event gets handled twice
    and posted to the log/welcome channel twice. Since this runs before
    `managed` has anything in it, ANY matching process found here is
    necessarily a leftover from before this boot, so it's always safe to
    kill.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", "discord_bot.bot"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Couldn't scan for orphaned clone processes ({e}) — skipping cleanup")
        return

    if result.returncode not in (0, 1):  # 1 = pgrep found nothing, not an error
        logger.warning(f"pgrep exited {result.returncode} scanning for orphaned clones — skipping cleanup")
        return

    my_pid = str(subprocess.os.getpid())
    killed = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        pid_str, _, cmdline = line.partition(" ")
        if "--clone-id" not in cmdline or pid_str == my_pid:
            continue
        try:
            pid = int(pid_str)
            subprocess.os.kill(pid, 15)  # SIGTERM
            logger.warning(f"Killed orphaned clone process from a previous supervisor run: pid={pid} ({cmdline.strip()})")
            killed += 1
        except (ValueError, ProcessLookupError, PermissionError) as e:
            logger.warning(f"Couldn't kill orphaned process pid={pid_str}: {e}")

    if killed:
        # Give them a moment to actually exit their gateway connections
        # before _reconcile spawns the real, freshly-managed replacements.
        time.sleep(2)


async def _reconcile(managed: Dict[int, ManagedClone]):
    try:
        active = await db.get_active_discord_clones()
    except Exception as e:
        logger.error(f"Failed to fetch active clones from the database: {e}")
        return

    active_ids = {c["clone_id"] for c in active}

    # Stop and drop anything that's no longer marked active (deactivated
    # via /removeclone, or the row was deleted).
    for clone_id in list(managed.keys()):
        if clone_id not in active_ids:
            managed[clone_id].stop()
            del managed[clone_id]

    # Start anything newly active that we're not already managing.
    for c in active:
        if c["clone_id"] not in managed:
            label = c.get("bot_username") or f"clone-{c['clone_id']}"
            m = ManagedClone(c["clone_id"], label)
            managed[c["clone_id"]] = m
            m.start()

    # Restart anything that died, backing off per-clone so a bad token
    # (invalid/revoked, missing privileged intent, etc.) doesn't spin the
    # CPU restarting it every poll forever.
    now = time.monotonic()
    for m in managed.values():
        if m.is_running():
            m.backoff = INITIAL_RESTART_BACKOFF_SECONDS  # reset once stable
            continue
        if now >= m.next_restart_at:
            logger.warning(f"Clone #{m.clone_id} ({m.label}) is down — restarting (next backoff={m.backoff}s)")
            m.start()
            m.next_restart_at = now + m.backoff
            m.backoff = min(m.backoff * 2, MAX_RESTART_BACKOFF_SECONDS)


async def main():
    logger.info("Starting Discord clone supervisor loop")
    _kill_orphaned_clone_processes()
    managed: Dict[int, ManagedClone] = {}
    try:
        while True:
            await _reconcile(managed)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        # Best-effort clean shutdown (Ctrl+C, service stop) so clone
        # processes don't get orphaned when the supervisor exits.
        for m in managed.values():
            m.stop()


if __name__ == "__main__":
    asyncio.run(main())
