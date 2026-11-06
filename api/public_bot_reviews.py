"""
Public comments and ratings for editorial bot features.
GET /api/public_bot_reviews?bot_id=843183839869665280
POST /api/public_bot_reviews with display_name, rating (1-5), and/or comment.
"""
import asyncio
import hashlib
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)
REVIEWABLE_SLUGS = set("""alka mee6 dyno carl-bot yagpdb probot arcane tatsu nadekobot tatsumaki dank-memer unbelievaboat poketwo mudae fredboat jockie-music hydra groovy rythm octave ticket-tool helper-gg wick beemo double-counter captcha-bot altdentifier xenon statbot serverstats invite-tracker sesh apollo giveawaybot disboard monitorss streamcord sapphire zira mimu owobot epic-rpg karuta maki amaribot bloxlink flavibot luna easypoll tempvoice birthdaybot reminderbot welcome-bots discord-native old-directory discord-security""".split())
DEFAULT_SLUG = "alka"
def _slug(value):
    value = str(value or DEFAULT_SLUG).strip().lower()
    if value not in REVIEWABLE_SLUGS:
        raise ValueError("This article does not accept reviews.")
    return value
ALLOWED_ORIGINS = {
    "https://discordbotreviewer.com",
    "https://www.discordbotreviewer.com",
    "https://discordbotreviewer.pages.dev",
}
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS discordbotreviewer_reviews (
    id BIGSERIAL PRIMARY KEY,
    bot_id TEXT NOT NULL,
    display_name VARCHAR(40) NOT NULL,
    rating SMALLINT,
    comment VARCHAR(1200),
    ip_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT discordbotreviewer_rating_range CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    CONSTRAINT discordbotreviewer_review_content CHECK (rating IS NOT NULL OR LENGTH(TRIM(comment)) > 0)
)
"""

async def _db():
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_SQL)
    return pool

async def _read(bot_id):
    pool = await _db()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            "SELECT ROUND(AVG(rating)::numeric, 1) AS average, COUNT(rating)::int AS rating_count, COUNT(*)::int AS review_count FROM discordbotreviewer_reviews WHERE bot_id = $1",
            bot_id,
        )
        rows = await conn.fetch(
            "SELECT display_name, rating, comment, created_at FROM discordbotreviewer_reviews WHERE bot_id = $1 ORDER BY created_at DESC LIMIT 50",
            bot_id,
        )
    return {
        "status": "ok",
        "average": float(summary["average"]) if summary and summary["average"] is not None else None,
        "rating_count": int(summary["rating_count"] or 0) if summary else 0,
        "review_count": int(summary["review_count"] or 0) if summary else 0,
        "reviews": [{"name": r["display_name"], "rating": r["rating"], "comment": r["comment"] or "", "created_at": r["created_at"].isoformat()} for r in rows],
    }

async def _write(data, ip, bot_id):
    name = " ".join(str(data.get("display_name") or "").split())[:40]
    comment = str(data.get("comment") or "").strip()[:1200]
    rating = data.get("rating")
    if not name:
        raise ValueError("Please enter a display name.")
    if not comment and rating in (None, ""):
        raise ValueError("Add a comment or choose a rating.")
    if comment and len(comment) < 3:
        raise ValueError("Your comment is too short.")
    if rating not in (None, ""):
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise ValueError("Choose a rating from 1 to 5.")
        if rating < 1 or rating > 5:
            raise ValueError("Choose a rating from 1 to 5.")
    else:
        rating = None
    salt = os.environ.get("PUBLIC_REVIEW_HASH_SALT", "discordbotreviewer-public-review")
    ip_hash = hashlib.sha256((salt + ip).encode("utf-8")).hexdigest()
    pool = await _db()
    async with pool.acquire() as conn:
        recent = await conn.fetchval(
            "SELECT COUNT(*) FROM discordbotreviewer_reviews WHERE bot_id = $1 AND ip_hash = $2 AND created_at > NOW() - INTERVAL '12 hours'",
            bot_id, ip_hash,
        )
        if recent:
            raise ValueError("You have already sent feedback recently. Please try again later.")
        await conn.execute(
            "INSERT INTO discordbotreviewer_reviews (bot_id, display_name, rating, comment, ip_hash) VALUES ($1, $2, $3, $4, $5)",
            bot_id, name, rating, comment or None, ip_hash,
        )
    return await _read(bot_id)

class handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin if origin in ALLOWED_ORIGINS else "https://discordbotreviewer.com")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
    def _send(self, status, body):
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(payload)
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            try:
                bot_id = _slug((params.get("bot_id") or [DEFAULT_SLUG])[0])
            except ValueError:
                return self._send(404, {"status":"error","message":"Review page not found."})
            self._send(200, asyncio.run(_read(bot_id)))
        except Exception:
            logger.exception("public_bot_reviews read failed")
            self._send(500, {"status":"error","message":"Reviews are temporarily unavailable."})
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 5000:
                return self._send(400, {"status":"error","message":"Invalid review submission."})
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if data.get("website"):
                return self._send(400, {"status":"error","message":"Invalid submission."})
            bot_id = _slug(data.get('bot_id') or DEFAULT_SLUG)
            result = asyncio.run(_write(data, self.client_address[0], bot_id))
            self._send(201, result)
        except ValueError as error:
            self._send(400, {"status":"error","message":str(error)})
        except Exception:
            logger.exception("public_bot_reviews write failed")
            self._send(500, {"status":"error","message":"Your feedback could not be saved right now."})
