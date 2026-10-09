"""
Cron-triggered runner for Developer-mode scheduled jobs (dev_jobs, see database.py and modules/dev_jobs.py).

ONE endpoint, called every 5 minutes by the existing Cloudflare worker (cron-worker/, group `frequent`). The backend decides which
jobs are due; no per-user Cloudflare trigger exists. Same conventions as api/cron_dash_dropbox_dm.py: Authorization: Bearer <CRON_SECRET>
(or ?secret=), small batch, hard wall-clock budget.

Safety rules, in order for each claimed job:
  * claimed atomically (FOR UPDATE SKIP LOCKED + a lease), so overlapping ticks can never run a job twice;
  * next_run_at is advanced BEFORE the job runs;
  * a person whose Developer plan is no longer effective gets the job PAUSED, never run (purged after 30 days elsewhere);
  * the owner's `dev_jobs` switch stops all runs (nothing is claimed, so nothing is lost);
  * 5 failures in a row switch the job off; nothing is ever retried forever and a failed run is not retried before its next slot;
  * keys never appear here: own-key chats are handled entirely inside dash_dev (this file never reads a key), and only plain text is logged or stored.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import aiohttp

from config import CRON_SECRET, DISCORD_BOT_TOKEN
from database import db
from modules import dev_jobs
from modules import entitlements as ent

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
WALL_CLOCK_BUDGET_SECONDS = 40
JOB_DELAY_SECONDS = 0.3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def classify(res: dict):
    """(ok, status, count_failure) from a dash_dev handler reply. Messages are never copied (they could echo request data)."""
    code = res.get("_status")
    if not code:
        return True, "ok", False
    if res.get("code") == "weekly_limit":
        return False, "allowance_used", False
    if code == 503:
        return False, "switched_off", False         # an owner switch is on: skipped, not the person's fault
    return False, f"error_{code}", True


async def _export(uid, name, text):
    from api import dash_dev
    return await dash_dev.dev_export_create(uid, {"kind": "note", "name": name, "content": text}, db)


async def run_job(job, session) -> tuple:
    """Run one claimed job. Returns (ok, status, count_failure, stop) where stop switches the job off without deleting it."""
    from api import dash_dev
    from api.cron_discord_owner_broadcast import _dm_user
    uid, kind, payload = str(job["user_id"]), job["kind"], dev_jobs.load_payload(job["payload"])
    if kind == "reminder":
        error = await _dm_user(session, DISCORD_BOT_TOKEN, int(uid), f"\N{ALARM CLOCK} Reminder: {job['name']}\n{payload.get('text', '')}"[:1900])
        return (True, "ok", False, False) if error is None else (False, "dm_failed", True, False)
    if kind == "note":
        ok, status, count = classify(await _export(uid, job["name"], payload.get("text", "")))
        return ok, status, count, False
    if kind == "ai_prompt":
        res = await dash_dev.dev_chat_send(uid, {"model": payload.get("model") or "default",
                                                 "messages": [{"role": "user", "content": payload.get("prompt", "")}]}, db)
        ok, status, count = classify(res)
        if not ok:
            return ok, status, count, status == "allowance_used"
        ok, status, count = classify(await _export(uid, job["name"], f"# {job['name']}\n\n{res.get('reply', '')}"))
        return ok, status, count, False
    return False, "unknown_kind", True, False


async def run_due_jobs() -> dict:
    totals = {"ran": 0, "ok": 0, "failed": 0, "paused": 0, "stopped": 0}
    if not DISCORD_BOT_TOKEN:
        return {**totals, "error": "no_bot_token"}
    from modules import admin_controls
    if "dev_jobs" in await admin_controls.current_switches():
        return {**totals, "skipped": "switched_off"}
    deadline = asyncio.get_event_loop().time() + WALL_CLOCK_BUDGET_SECONDS
    batch = await db.dev_job_claim(BATCH_SIZE)
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for i, job in enumerate(batch):
            now = datetime.now(timezone.utc)
            nxt = dev_jobs.next_run(job["schedule"], job.get("slot") or now)
            if nxt is not None and nxt <= now:                  # a long outage: skip the missed slots, don't replay them
                nxt = dev_jobs.next_run(job["schedule"], now)
            if nxt is None:                                     # corrupt schedule: never runs
                await db.dev_job_stop(job["id"], "bad_schedule")
                totals["stopped"] += 1
                continue
            await db.dev_job_advance(job["id"], nxt)            # advance BEFORE running
            if asyncio.get_event_loop().time() >= deadline:
                continue                                         # not run this tick; its slot is simply missed, never replayed twice
            try:
                if not ent.has_access(await db.entitlements_list(str(job["user_id"])), ent.DEV_PRODUCTS):
                    await db.dev_job_stop(job["id"], "plan_lapsed", plan_lapsed=True)
                    totals["paused"] += 1
                    continue
                ok, status, count, stop = await run_job(job, session)
            except Exception as e:                               # type only: an exception message could carry request data
                logger.error("[dev jobs] job %s crashed: %s", job["id"], type(e).__name__)
                ok, status, count, stop = False, "crashed", True, False
            totals["ran"] += 1
            if stop:
                await db.dev_job_stop(job["id"], status)
                totals["stopped"] += 1
            else:
                await db.dev_job_finish(job["id"], status, ok, dev_jobs.MAX_FAILS, count_failure=count)
            totals["ok" if ok else "failed"] += 1
            await asyncio.sleep(JOB_DELAY_SECONDS)
    return totals


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        if self.headers.get("Authorization", "") == f"Bearer {CRON_SECRET}":
            return True
        return parse_qs(urlparse(self.path).query).get("secret", [""])[0] == CRON_SECRET

    def _send(self, code: int, payload: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            return self._send(401, {"status": "error", "message": "Unauthorized"})
        try:
            self._send(200, {"status": "ok", **asyncio.run(run_due_jobs())})
        except Exception as e:
            logger.error("[cron_dev_scheduled] error: %s", type(e).__name__)
            self._send(500, {"status": "error", "message": "internal error"})
