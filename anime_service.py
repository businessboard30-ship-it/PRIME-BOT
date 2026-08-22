import aiohttp
from typing import List, Dict, Optional
from datetime import datetime

from config import ANILIST_ENDPOINT, JIKAN_ENDPOINT

# Shared HTTP session, reused across warm serverless invocations (same pattern
# as get_pool() in database.py and _get_event_loop() in api/bot.py). Opening a
# brand-new TCP+TLS session on every single AniList/Jikan call - with no
# timeout - was a major source of the reported slowness: a slow upstream call
# could hang for the bot's entire request lifetime with nothing to cut it off.
_session = None
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=3)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
    return _session

class AnimeService:
    """Service for fetching anime data from AniList and Jikan APIs"""
    
    def __init__(self):
        self.cache = {}
        self.cache_ttl = 3600  # 1 hour
        self.request_count = {}  # Track API requests for rate limiting
        self.rate_limit_max = 50  # Max requests per hour
    
    def _get_cache_key(self, key: str) -> Optional[Dict]:
        """Get cached data if not expired"""
        if key in self.cache:
            data, timestamp = self.cache[key]
            if datetime.now().timestamp() - timestamp < self.cache_ttl:
                return data
            else:
                del self.cache[key]
        return None
    
    def _set_cache(self, key: str, data: Dict):
        """Cache data with timestamp"""
        self.cache[key] = (data, datetime.now().timestamp())
    
    async def get_trending_anime(self, page: int = 1) -> List[Dict]:
        """Fetch trending anime from AniList"""
        cache_key = f"trending_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, sort: TRENDING_DESC, status: RELEASING) {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                    status
                }
            }
        }
        """ % page
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching trending anime: {e}")
        
        return []
    
    async def get_latest_anime(self, page: int = 1) -> List[Dict]:
        """Fetch latest anime releases"""
        cache_key = f"latest_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, sort: START_DATE_DESC, status: RELEASING) {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                    status
                    startDate { year month day }
                }
            }
        }
        """ % page
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching latest anime: {e}")
        
        return []
    
    async def get_ongoing_anime(self, page: int = 1) -> List[Dict]:
        """Fetch ongoing anime series"""
        cache_key = f"ongoing_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, status: RELEASING, sort: TRENDING_DESC) {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                    nextAiringEpisode { airingAt episode }
                }
            }
        }
        """ % page
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching ongoing anime: {e}")
        
        return []
    
    async def get_anime_movies(self, page: int = 1) -> List[Dict]:
        """Fetch real anime movies from AniList (format: MOVIE)"""
        cache_key = f"movies_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached

        query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, format: MOVIE, sort: POPULARITY_DESC) {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                    status
                }
            }
        }
        """ % page

        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching anime movies: {e}")

        return []

    async def get_seasonal_anime(self, page: int = 1) -> List[Dict]:
        """Fetch this season's anime"""
        cache_key = f"seasonal_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        current_year = datetime.now().year
        current_month = datetime.now().month
        
        if current_month in [12, 1, 2]:
            season = "WINTER"
        elif current_month in [3, 4, 5]:
            season = "SPRING"
        elif current_month in [6, 7, 8]:
            season = "SUMMER"
        else:
            season = "FALL"
        
        query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, season: %s, seasonYear: %d) {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                }
            }
        }
        """ % (page, season, current_year)
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching seasonal anime: {e}")
        
        return []
    
    async def search_anime(self, query: str, page: int = 1) -> List[Dict]:
        """Search for anime by title"""
        cache_key = f"search_{query}_{page}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        anilist_query = """
        query {
            Page(page: %d, perPage: 5) {
                media(type: ANIME, search: "%s") {
                    id
                    title { romaji english }
                    episodes
                    genres
                    averageScore
                    description
                    coverImage { large }
                }
            }
        }
        """ % (page, query.replace('"', '\\"'))
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": anilist_query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_anilist_response(data.get("data", {}).get("Page", {}).get("media", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error searching anime: {e}")
        
        return []
    
    async def get_anime_details(self, anime_id: int) -> Optional[Dict]:
        """Get detailed info about an anime"""
        cache_key = f"anime_details_{anime_id}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        query = """
        query {
            Media(id: %d, type: ANIME) {
                id
                title { romaji english }
                episodes
                genres
                averageScore
                description
                coverImage { large }
                startDate { year month day }
                endDate { year month day }
                status
                studios { nodes { name } }
            }
        }
        """ % anime_id
        
        try:
            session = await get_session()
            async with session.post(ANILIST_ENDPOINT, json={"query": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    media = data.get("data", {}).get("Media", {})
                    if media:
                        result = {
                            "id": media.get("id"),
                            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
                            "episodes": media.get("episodes"),
                            "genres": ", ".join(media.get("genres", [])),
                            "rating": media.get("averageScore", 0) / 10,
                            "description": media.get("description", ""),
                            "image": media.get("coverImage", {}).get("large"),
                            "status": media.get("status")
                        }
                        self._set_cache(cache_key, result)
                        return result
        except Exception as e:
            print(f"[v0] Error fetching anime details: {e}")
        
        return None
    
    async def get_jikan_anime(self, query: str = "", limit: int = 5) -> List[Dict]:
        """Fetch anime from Jikan API (MyAnimeList)"""
        cache_key = f"jikan_{query}_{limit}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        try:
            session = await get_session()
            # Get top anime if no query, otherwise search
            if not query:
                endpoint = f"{JIKAN_ENDPOINT}/top/anime?limit={limit}"
            else:
                endpoint = f"{JIKAN_ENDPOINT}/anime?query={query}&limit={limit}"
                
            async with session.get(endpoint) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = self._format_jikan_response(data.get("data", []))
                    self._set_cache(cache_key, result)
                    return result
        except Exception as e:
            print(f"[v0] Error fetching from Jikan: {e}")
        
        return []
    
    async def get_anime_reviews(self, anime_id: int, source: str = "anilist") -> List[Dict]:
        """Get user reviews for an anime (from database submissions)"""
        # This would fetch from the submissions database
        # Implemented in future versions
        return []
    
    def _format_jikan_response(self, anime_list: List[Dict]) -> List[Dict]:
        """Format Jikan response to standard format"""
        result = []
        for anime in anime_list:
            result.append({
                "id": anime.get("mal_id"),
                "title": anime.get("title"),
                "episodes": anime.get("episodes"),
                "genres": ", ".join([g.get("name", "") for g in anime.get("genres", [])]),
                "rating": anime.get("score", 0),
                "description": anime.get("synopsis", "")[:100] + "..." if anime.get("synopsis") else "",
                "image": anime.get("images", {}).get("jpg", {}).get("large_image_url")
            })
        return result

    def _format_anilist_response(self, media_list: List[Dict]) -> List[Dict]:
        """Format AniList response to standard format"""
        result = []
        for media in media_list:
            result.append({
                "id": media.get("id"),
                "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
                "episodes": media.get("episodes"),
                "genres": ", ".join(media.get("genres", [])),
                "rating": (media.get("averageScore", 0) or 0) / 10,
                "description": media.get("description", "")[:100] + "..." if media.get("description") else "",
                "image": media.get("coverImage", {}).get("large")
            })
        return result

# Global service instance
anime_service = AnimeService()
