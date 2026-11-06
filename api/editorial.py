"""Editorial site visits, article submissions and public Discord owner profile lookup."""
import asyncio, hashlib, json, logging, os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)
ALLOWED_ORIGINS = {"https://discordbotreviewer.com","https://www.discordbotreviewer.com","https://discordbotreviewer.pages.dev"}
OWNER_IDS = {"alka": "696795851888263230"}

async def _pool():
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS editorial_visits (
          id BIGSERIAL PRIMARY KEY, page_slug TEXT NOT NULL,
          visitor_hash CHAR(64) NOT NULL, visited_on DATE NOT NULL DEFAULT CURRENT_DATE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(page_slug, visitor_hash, visited_on)
        );
        CREATE TABLE IF NOT EXISTS editorial_submissions (
          id BIGSERIAL PRIMARY KEY, author_name VARCHAR(80) NOT NULL,
          email VARCHAR(254), title VARCHAR(160) NOT NULL, category VARCHAR(50) NOT NULL,
          summary VARCHAR(500) NOT NULL, article_body TEXT NOT NULL,
          source_url VARCHAR(500), ip_hash CHAR(64) NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
    return pool

async def _visit(slug, ip):
    pool = await _pool()
    salt = os.getenv("PUBLIC_REVIEW_HASH_SALT","editorial-visit-salt")
    digest = hashlib.sha256((salt+ip).encode()).hexdigest()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO editorial_visits(page_slug,visitor_hash) VALUES($1,$2) ON CONFLICT DO NOTHING",slug,digest)
        return int(await c.fetchval("SELECT COUNT(*) FROM editorial_visits WHERE page_slug=$1",slug) or 0)

async def _submit(data, ip):
    name=" ".join(str(data.get("author_name") or "").split())[:80]
    email=str(data.get("email") or "").strip()[:254]
    title=" ".join(str(data.get("title") or "").split())[:160]
    category=str(data.get("category") or "").strip()[:50]
    summary=str(data.get("summary") or "").strip()[:500]
    body=str(data.get("article_body") or "").strip()
    source=str(data.get("source_url") or "").strip()[:500]
    if not all((name,title,category,summary,body)) or len(body)<100:
        raise ValueError("Please complete all required fields. Article drafts must be at least 100 characters.")
    if source and not source.startswith(("https://","http://")):
        raise ValueError("Source links must start with http:// or https://.")
    salt=os.getenv("PUBLIC_REVIEW_HASH_SALT","editorial-submit-salt")
    digest=hashlib.sha256((salt+ip).encode()).hexdigest()
    pool=await _pool()
    async with pool.acquire() as c:
        recent=await c.fetchval("SELECT COUNT(*) FROM editorial_submissions WHERE ip_hash=$1 AND created_at>NOW()-INTERVAL '24 hours'",digest)
        if recent: raise ValueError("You've already submitted an article recently. Please wait before sending another.")
        await c.execute("""INSERT INTO editorial_submissions(author_name,email,title,category,summary,article_body,source_url,ip_hash)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",name,email or None,title,category,summary,body,source or None,digest)

def _discord_user(user_id):
    token=os.getenv("DISCORD_BOT_TOKEN","").strip()
    if not token: raise RuntimeError("Discord profile lookup is not configured on the server.")
    req=urllib.request.Request("https://discord.com/api/v10/users/"+user_id,headers={"Authorization":"Bot "+token,"User-Agent":"DiscordBotReviewer/1.0"})
    with urllib.request.urlopen(req,timeout=8) as response: data=json.loads(response.read().decode())
    avatar=data.get("avatar")
    if avatar:
        ext="gif" if avatar.startswith("a_") else "png"
        avatar_url=f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{ext}?size=128"
    else:
        avatar_url=f"https://cdn.discordapp.com/embed/avatars/{int(user_id)%6}.png"
    return {"status":"ok","id":user_id,"name":data.get("global_name") or data.get("username") or "Alka owner","avatar_url":avatar_url}

class handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin=self.headers.get("Origin","")
        self.send_header("Access-Control-Allow-Origin",origin if origin in ALLOWED_ORIGINS else "https://discordbotreviewer.com")
        self.send_header("Vary","Origin"); self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
    def _send(self,status,data):
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store"); self._cors(); self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        params=parse_qs(urlparse(self.path).query); action=(params.get("action") or [""])[0]
        try:
            if action=="owner":
                user_id=(params.get("user_id") or [""])[0]
                if user_id not in OWNER_IDS.values(): return self._send(404,{"status":"error","message":"Owner profile not configured."})
                try: return self._send(200,_discord_user(user_id))
                except urllib.error.HTTPError as e:
                    logger.warning("Discord owner lookup returned %s",e.code)
                    return self._send(502,{"status":"error","message":"Discord could not return this public profile."})
            if action=="visit":
                slug=(params.get("page") or ["home"])[0][:80]
                count=asyncio.run(_visit(slug,self.client_address[0]))
                return self._send(200,{"status":"ok","visits":count})
            return self._send(400,{"status":"error","message":"Unknown action."})
        except Exception:
            logger.exception("Editorial API GET failed"); self._send(500,{"status":"error","message":"This feature is temporarily unavailable."})
    def do_POST(self):
        try:
            n=int(self.headers.get("Content-Length","0"))
            if n<1 or n>30000: return self._send(400,{"status":"error","message":"Invalid article submission."})
            data=json.loads(self.rfile.read(n).decode())
            if data.get("website"): return self._send(400,{"status":"error","message":"Invalid submission."})
            if data.get("action")!="submit": return self._send(400,{"status":"error","message":"Unknown action."})
            asyncio.run(_submit(data,self.client_address[0]))
            self._send(201,{"status":"ok","message":"Your article has been sent to the editorial team for review."})
        except ValueError as e: self._send(400,{"status":"error","message":str(e)})
        except Exception:
            logger.exception("Editorial article submission failed"); self._send(500,{"status":"error","message":"Your article could not be submitted right now."})
