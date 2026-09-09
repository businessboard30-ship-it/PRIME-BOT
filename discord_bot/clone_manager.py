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
import errno
import fcntl
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
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

# Held for the lifetime of the process so a second supervisor can never
# start on the same host while this one is alive (see _acquire_singleton_lock).
LOCK_PATH = "/tmp/prime_bot_clone_manager.lock"
_lock_fh = None


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


def _acquire_singleton_lock():
    """Grabs an exclusive, non-blocking flock on LOCK_PATH and holds it for
    the life of the process (fd is kept alive in the global `_lock_fh` —
    the OS releases the lock automatically if this process dies, however
    it dies, so nothing needs to clean this up on exit).

    This is the second, independent guard against double-running clones:
    _kill_orphaned_clone_processes() below cleans up leftover *child*
    processes from a previous supervisor, but does nothing to stop two
    *supervisors* from being alive at the same time (e.g. a redeploy that
    starts the new instance before the platform has finished tearing down
    the old one, or someone manually running the module twice). Two live
    supervisors each spawn their own full set of clone subprocesses, and
    neither one's in-memory `managed` dict knows about the other's, so the
    orphan-scan at startup can't catch this case — it only runs once, at
    the moment each supervisor boots.

    If the lock is already held, this process exits immediately rather
    than silently running as a duplicate supervisor.
    """
    global _lock_fh
    _lock_fh = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            logger.error(
                f"Another clone supervisor already holds {LOCK_PATH} — refusing to start a "
                f"second one (this is exactly what causes every clone to double-log events). "
                f"Exiting."
            )
            sys.exit(1)
        raise
    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()


def _iter_processes():
    """Yields (pid, cmdline) for every process visible under /proc, one
    entry per PID directory. Reads directly from /proc instead of shelling
    out to `pgrep` — `pgrep` comes from the procps package, which isn't
    guaranteed to be installed in a minimal container image, and its
    absence previously made the orphan scan below fail with
    FileNotFoundError and silently skip cleanup entirely (see git history:
    the pgrep-based version logged a warning and returned early whenever
    the binary was missing, which meant orphans were never actually
    killed on hosts without procps). /proc has no such dependency on
    Linux, which is what this bot always deploys to.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue  # process exited between listdir() and open(), or we can't read it
        if not raw:
            continue
        cmdline = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part)
        yield pid, cmdline


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
    my_pid = os.getpid()
    killed = 0
    try:
        candidates = list(_iter_processes())
    except OSError as e:
        logger.warning(f"Couldn't scan /proc for orphaned clone processes ({e}) — skipping cleanup")
        return

    for pid, cmdline in candidates:
        if "discord_bot.bot" not in cmdline or "--clone-id" not in cmdline or pid == my_pid:
            continue
        try:
            os.kill(pid, 15)  # SIGTERM
            logger.warning(f"Killed orphaned clone process from a previous supervisor run: pid={pid} ({cmdline.strip()})")
            killed += 1
        except (ValueError, ProcessLookupError, PermissionError) as e:
            logger.warning(f"Couldn't kill orphaned process pid={pid}: {e}")

    if killed:
        # Give them a moment to actually exit their gateway connections
        # before _reconcile spawns the real, freshly-managed replacements.
        time.sleep(2)


async def _wait_for_clone_ready(
    clone_id: int,
    spawned_at: datetime,
    timeout_seconds: float,
    poll_seconds: float,
) -> bool:
    """Polls last_heartbeat for this clone until it's newer than
    spawned_at (proof this process instance reached on_ready, i.e. its
    IDENTIFY succeeded), or the timeout elapses. Returns True if the
    clone confirmed in time, False if the timeout was hit.

    Comparing against spawned_at, not just "is last_heartbeat set", matters
    because a clone that's been active before (redeploy, restart) will
    already have a stale last_heartbeat from its previous run sitting in
    the DB — without the spawned_at comparison this would return True
    immediately without actually waiting for the NEW process to connect.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            last_heartbeat = await db.get_discord_clone_heartbeat(clone_id)
        except Exception as e:
            logger.warning(f"Couldn't check heartbeat for clone #{clone_id} while staggering startup: {e}")
            last_heartbeat = None
        if last_heartbeat is not None and last_heartbeat > spawned_at:
            return True
        await asyncio.sleep(poll_seconds)
    return False


async def _reconcile(managed: Dict[int, ManagedClone]):
    try:
        dupes = await db.get_duplicate_active_clone_tokens()
        for d in dupes:
            keep, drop = d["clone_ids"][0], d["clone_ids"][1:]
            logger.error(
                f"[DUPLICATE CLONE] bot '{d['bot_username']}' (bot_user_id={d['bot_user_id']}) "
                f"has {len(d['clone_ids'])} ACTIVE clone_id rows: {d['clone_ids']} — this token would "
                f"otherwise get one live process PER id, so every event in its guilds gets handled/logged "
                f"that many times. Auto-remediating: keeping clone_id={keep} active and deactivating "
                f"{drop}."
            )
            for clone_id in drop:
                try:
                    await db.set_discord_clone_status(clone_id, "inactive")
                except Exception as e:
                    logger.error(f"Failed to deactivate duplicate clone_id={clone_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to check for duplicate active clone tokens: {e}")

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
    #
    # A fixed asyncio.sleep() here used to be the stagger mechanism (first
    # 3s, then 6s), meant to avoid Discord's gateway IDENTIFY rate limit —
    # on a full cold start all clones land here in the same tight loop and
    # each opens its own gateway connection within milliseconds of the
    # others, which is exactly what triggers the
    # "WebSocket ... is ratelimited, waiting ~59s" warning: Discord throttles
    # bursts of near-simultaneous identifies from the same process/host.
    #
    # That approach staggered the wrong thing: it spaced out when each
    # subprocess was *spawned*, not when it actually reached IDENTIFY. Each
    # subprocess has its own variable startup overhead before it gets there
    # (DB pool creation, cog imports, REST login), and that overhead isn't
    # constant across clones — so spawn times spaced exactly N seconds
    # apart do NOT guarantee IDENTIFY attempts stay N seconds apart. Both
    # 3s and 6s eventually hit the same failure, just at a later position
    # in the sequence each time (drift needs more clones to accumulate
    # enough to close a wider gap), which is the signature of a value
    # that's papering over the real problem rather than fixing it.
    #
    # Instead: wait for confirmation that a clone actually got through
    # its gateway handshake (on_ready fires -> touch_discord_clone_heartbeat
    # updates last_heartbeat, see bot.py) before starting the next one, up
    # to a bounded timeout so one hung/bad-token clone can't stall the
    # whole startup sequence forever.
    STAGGER_TIMEOUT_SECONDS = 30
    STAGGER_POLL_SECONDS = 1
    for c in active:
        if c["clone_id"] not in managed:
            label = c.get("bot_username") or f"clone-{c['clone_id']}"
            m = ManagedClone(c["clone_id"], label)
            managed[c["clone_id"]] = m
            spawned_at = datetime.now(timezone.utc)
            m.start()
            ready = await _wait_for_clone_ready(
                c["clone_id"], spawned_at, STAGGER_TIMEOUT_SECONDS, STAGGER_POLL_SECONDS
            )
            if not ready:
                logger.warning(
                    f"Clone #{c['clone_id']} ({label}) hadn't confirmed its gateway "
                    f"connection {STAGGER_TIMEOUT_SECONDS}s after spawning — proceeding "
                    f"to the next clone anyway (it may still connect, or may be a bad/"
                    f"revoked token — the restart-backoff loop below will keep retrying it)."
                )

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
    _acquire_singleton_lock()
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
