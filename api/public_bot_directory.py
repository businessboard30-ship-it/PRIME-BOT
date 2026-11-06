"""
Public, read-only directory feed for discordbotreviewer.com.
Only approved archive listings are exposed; submitter IDs, guild IDs,
moderation notes and private webhook fields never leave the database.
GET /api/public_bot_directory?q=&category=&sort=trending|newest|popular|oldest&limit=12&offset=0
"""
import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)
CACHE_SECONDS = 45
_cache = {}

async def _fetch(params):
    import time
    from database import get_pool
    from modules import archive_adapter as arc
    await arc.ensure_ready()
    q = (params.get("q") or [""])[0].strip()[:100]
    category = (params.get("category") or [""])[0].strip()[:40]
    sort = (params.get("sort") or ["trending"])[0]
    if sort not in {"trending", "newest", "popular", "oldest"}:
        sort = "trending"
    try:
        limit = min(30, max(1, int((params.get("limit") or ["12"])[0])))
        offset = min(10000, max(0, int((params.get("offset") or ["0"])[0])))
    except (TypeError, ValueError):
        limit, offset = 12, 0
    key = (q.lower(), category.lower(), sort, limit, offset)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    pool = await get_pool()
    where = ["l.status = 'approved'"]
    args = []
    if q:
        args.append("%" + q.replace("%", "\\%").replace("_", "\\_") + "%")
        where.append("(l.bot_name ILIKE $%d OR l.description ILIKE $%d OR l.application_id::text = $%d)" % (len(args),len(args),len(args)))
    if category:
        args.append(category)
        where.append("LOWER(l.category) = LOWER($%d)" % len(args))
    clause = " AND ".join(where)
    order = {
        "trending": "votes DESC, l.created_at DESC",
        "popular": "votes DESC, l.created_at DESC",
        "newest": "l.created_at DESC",
        "oldest": "l.created_at ASC",
    }[sort]
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM archive_listings l WHERE " + clause, *args)
        rows = await conn.fetch(
            """SELECT l.application_id, l.bot_name, l.bot_icon_url, l.description,
                      l.category, l.tags, l.invite_link, l.created_at,
                      COUNT(v.id)::int AS votes
               FROM archive_listings l
               LEFT JOIN archive_votes v ON v.listing_id = l.id
               WHERE """ + clause + """
               GROUP BY l.id
               ORDER BY """ + order + """
               LIMIT $%d OFFSET $%d""" % (len(args)+1, len(args)+2),
            *args, limit, offset
        )
    items = []
    for row in rows:
        created = row["created_at"]
        items.append({
            "id": str(row["application_id"]),
            "application_id": str(row["application_id"]),
            "name": row["bot_name"],
            "avatar_url": row["bot_icon_url"],
            "description": row["description"] or "",
            "category": row["category"] or "Other",
            "tags": list(row["tags"] or []),
            "invite_url": row["invite_link"] or "",
            "votes": int(row["votes"] or 0),
            "age_label": created.strftime("%b %Y") if created else "Listed",
        })
    body = {"status":"ok","total":int(total or 0),"items":items}
    _cache[key] = (time.time(), body)
    return body

class handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        allowed = {
            "https://discordbotreviewer.com",
            "https://www.discordbotreviewer.com",
            "https://discordbotreviewer.pages.dev",
        }
        if origin in allowed or (origin.startswith("https://") and origin.endswith(".discordbotreviewer.pages.dev")):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "https://discordbotreviewer.com")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        try:
            body = asyncio.run(_fetch(parse_qs(urlparse(self.path).query)))
            status = 200
        except Exception:
            logger.exception("public_bot_directory error")
            body, status = {"status":"error","items":[],"total":0}, 500
        payload = json.dumps(body, default=str)
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","public, max-age=30")
        self._cors()
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))
