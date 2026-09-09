# path: api/server_reviews.py
"""
Reviews & Ratings API for server listings

Endpoints:
  GET  /api/server_reviews?guild_id=X&page=1&sort=recent|helpful|rating
  POST /api/server_reviews?guild_id=X (submit new review, requires Discord OAuth)
  POST /api/server_reviews/helpful?review_id=X (mark review as helpful)

Reviews require Discord OAuth authentication - see discord_login_oauth.py
for the token flow. A user can only have one review per guild.

Sorting:
  - recent: most recent reviews first
  - helpful: most helpful first (by helpful_count)
  - rating: highest rated first
"""
import json
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from database import db

logger = logging.getLogger(__name__)

MAX_REVIEW_TEXT = 500
REVIEWS_PER_PAGE = 10


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

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
        Get reviews for a server listing.
        
        Query params:
          guild_id: REQUIRED, server guild ID
          page: optional, defaults to 1
          sort: optional, one of: recent|helpful|rating, defaults to recent
        
        Returns: {
            status: "ok",
            reviews: [
                {
                    review_id: int,
                    reviewer_user_id: int,
                    rating: 1-5,
                    review_text: str,
                    helpful_count: int,
                    is_verified_member: bool,
                    review_date: ISO datetime,
                    review_date_ago: "2 days ago"
                }
            ],
            total: int,
            page: int,
            page_size: int,
            avg_rating: float,
            rating_distribution: {1: count, 2: count, 3: count, 4: count, 5: count}
        }
        """
        import asyncio
        
        query = parse_qs(urlparse(self.path).query)
        
        guild_id_raw = query.get("guild_id", [""])[0]
        if not guild_id_raw.isdigit():
            self._json(400, {"status": "error", "message": "Missing or invalid guild_id"})
            return

        guild_id = int(guild_id_raw)
        
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            page_size = min(50, max(1, int(query.get("page_size", [str(REVIEWS_PER_PAGE)])[0])))
        except ValueError:
            page, page_size = 1, REVIEWS_PER_PAGE

        sort = query.get("sort", ["recent"])[0]
        if sort not in ("recent", "helpful", "rating"):
            sort = "recent"

        async def _run_get():
            return await db.get_server_reviews(
                guild_id=guild_id,
                limit=page_size,
                offset=(page - 1) * page_size,
                sort=sort
            )

        try:
            result = asyncio.run(_run_get())
        except Exception as e:
            logger.error(f"[server_reviews] GET error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        self._json(200, {
            "status": "ok",
            "reviews": result["reviews"],
            "total": result["total"],
            "page": page,
            "page_size": page_size,
            "avg_rating": result["avg_rating"],
            "review_count": result["review_count"],
            "rating_distribution": result["rating_distribution"]
        })

    def do_POST(self):
        """
        Submit a new review or mark review as helpful.
        
        Submit Review:
          Query: guild_id=X
          Body: {
              rating: 1-5 (required),
              review_text: str (optional, max 500 chars),
              discord_user_id: int,  # from OAuth token
              is_verified_member: bool  # optional, from Discord API
          }
          
        Mark Helpful:
          Query: review_id=X&helpful=1
          Body: {} (empty)
        
        Returns: {
            status: "ok",
            review_id: int (if submit),
            message: str
        }
        """
        import asyncio
        
        query = parse_qs(urlparse(self.path).query)
        
        # Check for "mark helpful" mode
        review_id_raw = query.get("review_id", [""])[0]
        if review_id_raw.isdigit():
            # Mark as helpful endpoint
            review_id = int(review_id_raw)
            
            async def _mark_helpful():
                return await db.increment_review_helpful(review_id)
            
            try:
                result = asyncio.run(_mark_helpful())
            except Exception as e:
                logger.error(f"[server_reviews] mark helpful error: {e}")
                self._json(500, {"status": "error", "message": "Internal error"})
                return
            
            if result:
                self._json(200, {"status": "ok", "message": "Review marked helpful"})
            else:
                self._json(404, {"status": "error", "message": "Review not found"})
            return

        # Submit new review mode
        guild_id_raw = query.get("guild_id", [""])[0]
        if not guild_id_raw.isdigit():
            self._json(400, {"status": "error", "message": "Missing or invalid guild_id"})
            return

        guild_id = int(guild_id_raw)

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"status": "error", "message": "Invalid JSON body"})
            return

        # Validate required fields
        rating_raw = body.get("rating")
        if not isinstance(rating_raw, int) or rating_raw < 1 or rating_raw > 5:
            self._json(400, {"status": "error", "message": "Rating must be 1-5"})
            return

        review_text = str(body.get("review_text", "")).strip()[:MAX_REVIEW_TEXT]
        discord_user_id = body.get("discord_user_id")
        
        if not discord_user_id or not isinstance(discord_user_id, int):
            self._json(401, {"status": "error", "message": "Authentication required (Discord OAuth)"})
            return

        is_verified_member = bool(body.get("is_verified_member", False))

        async def _submit_review():
            # Check if user already has a review for this guild
            existing = await db.get_user_review_for_guild(guild_id, discord_user_id)
            if existing:
                # Update existing review
                return await db.update_server_review(
                    review_id=existing["review_id"],
                    rating=rating_raw,
                    review_text=review_text,
                    is_verified_member=is_verified_member
                )
            else:
                # Create new review
                return await db.create_server_review(
                    guild_id=guild_id,
                    reviewer_user_id=discord_user_id,
                    rating=rating_raw,
                    review_text=review_text,
                    is_verified_member=is_verified_member
                )

        try:
            review = asyncio.run(_submit_review())
        except Exception as e:
            logger.error(f"[server_reviews] POST error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        if review:
            self._json(200, {
                "status": "ok",
                "review_id": review["review_id"],
                "message": "Review submitted successfully"
            })
        else:
            self._json(500, {"status": "error", "message": "Failed to submit review"})
