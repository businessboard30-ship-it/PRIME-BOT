# path: api/server_analytics.py
"""
Server Owner Analytics API

Endpoints:
  GET  /api/server_analytics?token=X&guild_id=Y&days=30
       Returns analytics for that server owner
  
  POST /api/server_analytics/verify-purchase?guild_id=X&tier=gold
       Process verification tier purchase (requires Paystack integration)
  
  GET  /api/server_analytics/referral-stats?guild_id=X
       Get referral link performance data

Requires listing_token authentication (see server_listings.py _resolve pattern).
"""
import json
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from database import db

logger = logging.getLogger(__name__)


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


async def _resolve_analytics_token(token: str, guild_id: int):
    """Authenticate via listing token - same pattern as server_listings.py"""
    resolved = await db.resolve_listing_token(token)
    if not resolved or resolved["guild_id"] != guild_id:
        return False, None
    return True, resolved


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=_json_default).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        """
        Get analytics dashboard data for a server listing.
        Requires valid listing token + guild_id.
        
        Query params:
          token: listing token (required)
          guild_id: server guild ID (required)
          days: number of days to analyze (optional, default 30, max 90)
          
        Returns: {
            status: "ok",
            server: {
                guild_name: str,
                member_count: int,
                verification_tier: str,
                created_at: ISO datetime
            },
            metrics: {
                clicks_total: int,
                clicks_today: int,
                clicks_by_day: [{date: str, count: int}],
                unique_visitors: int,
                
                votes_total: int,
                votes_by_day: [{date: str, count: int}],
                
                avg_rating: float,
                review_count: int,
                recent_reviews: [{rating: int, text: str, date: ISO}],
                
                members_estimated: int  # from referral tracking
            },
            referral: {
                ref_code: str,
                ref_url: str,
                total_clicks: int,
                conversions: int,
                conversion_rate: float (0-100),
                clicks_by_source: {
                    direct: int,
                    discord: int,
                    twitter: int,
                    reddit: int
                }
            },
            verification: {
                current_tier: str,
                next_tier: str,
                qualifications: {
                    clicks_30d: int,
                    votes_total: int,
                    avg_rating: float,
                    age_days: int,
                    auto_verified: bool
                }
            }
        }
        """
        import asyncio
        
        query = parse_qs(urlparse(self.path).query)
        
        token = query.get("token", [""])[0]
        guild_id_raw = query.get("guild_id", [""])[0]
        
        if not guild_id_raw.isdigit():
            self._json(400, {"status": "error", "message": "Invalid guild_id"})
            return
        
        guild_id = int(guild_id_raw)
        
        try:
            days = min(90, max(1, int(query.get("days", ["30"])[0])))
        except ValueError:
            days = 30

        async def _run_get():
            ok, resolved = await _resolve_analytics_token(token, guild_id)
            if not ok:
                return None
            
            return await db.get_server_analytics(
                guild_id=guild_id,
                clone_id=resolved.get("clone_id"),
                days=days
            )

        try:
            analytics = asyncio.run(_run_get())
        except Exception as e:
            logger.error(f"[server_analytics] GET error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        if analytics is None:
            self._json(403, {"status": "error", "message": "Invalid or unauthorized token"})
            return

        self._json(200, {
            "status": "ok",
            **analytics
        })

    def do_POST(self):
        """
        Handle analytics-related POST requests.
        
        Modes:
        1. Log click: ?log_click=1&guild_id=X
        2. Purchase verification tier: ?purchase_tier=1&guild_id=X&tier=gold
        
        For log_click:
          Body: {
              ref_code: str (optional),
              source: str (optional: direct|discord|twitter|reddit)
          }
          
        For purchase_tier:
          Body: {
              payment_reference: str  # Paystack payment reference
          }
        """
        import asyncio
        
        query = parse_qs(urlparse(self.path).query)
        
        guild_id_raw = query.get("guild_id", [""])[0]
        if not guild_id_raw.isdigit():
            self._json(400, {"status": "error", "message": "Invalid guild_id"})
            return
        
        guild_id = int(guild_id_raw)
        
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"status": "error", "message": "Invalid JSON body"})
            return

        # Mode 1: Log a click on the listing
        if query.get("log_click", ["0"])[0] == "1":
            ref_code = str(body.get("ref_code", "")).strip() or None
            source = str(body.get("source", "direct")).strip()
            
            async def _log_click():
                return await db.log_listing_click(
                    guild_id=guild_id,
                    ref_code=ref_code,
                    source=source
                )
            
            try:
                asyncio.run(_log_click())
            except Exception as e:
                logger.error(f"[server_analytics] click log error: {e}")
                self._json(500, {"status": "error", "message": "Internal error"})
                return
            
            self._json(200, {"status": "ok", "message": "Click logged"})
            return

        # Mode 2: Purchase verification tier (requires token auth)
        if query.get("purchase_tier", ["0"])[0] == "1":
            token = query.get("token", [""])[0]
            tier = query.get("tier", [""])[0]
            
            if not token or not tier:
                self._json(400, {"status": "error", "message": "Missing token or tier"})
                return
            
            payment_reference = body.get("payment_reference", "")
            
            async def _purchase():
                ok, resolved = await _resolve_analytics_token(token, guild_id)
                if not ok:
                    return None
                
                return await db.purchase_verification_tier(
                    guild_id=guild_id,
                    clone_id=resolved.get("clone_id"),
                    tier_name=tier,
                    payment_reference=payment_reference
                )
            
            try:
                result = asyncio.run(_purchase())
            except Exception as e:
                logger.error(f"[server_analytics] tier purchase error: {e}")
                self._json(500, {"status": "error", "message": "Internal error"})
                return
            
            if result:
                self._json(200, {
                    "status": "ok",
                    "message": f"Verification upgraded to {tier}",
                    "verification_tier": tier,
                    "expires_at": result.get("expires_at")
                })
            else:
                self._json(400, {"status": "error", "message": "Purchase failed"})
            return

        self._json(400, {"status": "error", "message": "Invalid request mode"})
