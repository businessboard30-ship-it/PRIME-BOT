"""
Thin async client for the Jellyfin REST API.

Design principle: this bot never stores, caches, or proxies actual video
bytes. Every function here either (a) reads metadata from the user's own
server, or (b) hands back a URL that resolves directly against the user's
own server. Playback always flows user's-server -> user's-device.
"""

import logging
from typing import List, Dict

import aiohttp

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class JellyfinError(Exception):
    pass


def _normalize_url(server_url: str) -> str:
    url = server_url.strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


async def verify_connection(server_url: str, api_key: str) -> Dict:
    """Validate a server URL + API key by hitting /System/Info, and resolve
    the API-key owner's Jellyfin user id (needed for library-scoped calls).
    Raises JellyfinError with a user-facing message on failure."""
    base = _normalize_url(server_url)
    headers = {"X-Emby-Token": api_key}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        try:
            async with session.get(f"{base}/System/Info", headers=headers) as resp:
                if resp.status == 401:
                    raise JellyfinError("That API key was rejected by the server.")
                if resp.status != 200:
                    raise JellyfinError(f"Server responded with HTTP {resp.status}.")
                info = await resp.json()
        except aiohttp.ClientError as e:
            raise JellyfinError(f"Couldn't reach that server: {e}")

        # Resolve which Jellyfin user this API key acts as, via /Users
        try:
            async with session.get(f"{base}/Users", headers=headers) as resp:
                if resp.status != 200:
                    raise JellyfinError("Connected, but couldn't list server users to bind this key.")
                users = await resp.json()
        except aiohttp.ClientError as e:
            raise JellyfinError(f"Couldn't reach that server: {e}")

    if not users:
        raise JellyfinError("No users found on that server for this API key.")

    return {
        "server_name": info.get("ServerName", "Jellyfin Server"),
        "version": info.get("Version", "unknown"),
        "jellyfin_user_id": users[0]["Id"],
    }


async def search_movies(server_url: str, api_key: str, jellyfin_user_id: str, query: str, limit: int = 10) -> List[Dict]:
    base = _normalize_url(server_url)
    headers = {"X-Emby-Token": api_key}
    params = {
        "searchTerm": query,
        "IncludeItemTypes": "Movie",
        "Recursive": "true",
        "Limit": str(limit),
        "Fields": "Overview,ProductionYear,CommunityRating,RunTimeTicks",
    }
    url = f"{base}/Users/{jellyfin_user_id}/Items"

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    raise JellyfinError(f"Search failed (HTTP {resp.status}).")
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise JellyfinError(f"Couldn't reach your server: {e}")

    results = []
    for item in data.get("Items", []):
        results.append({
            "id": item["Id"],
            "name": item.get("Name", "Unknown"),
            "year": item.get("ProductionYear"),
            "rating": item.get("CommunityRating"),
            "overview": (item.get("Overview") or "")[:300],
            "poster_url": f"{base}/Items/{item['Id']}/Images/Primary?api_key={api_key}",
            "runtime_minutes": round(item["RunTimeTicks"] / 600_000_000) if item.get("RunTimeTicks") else None,
        })
    return results


async def list_library(server_url: str, api_key: str, jellyfin_user_id: str, limit: int = 25) -> List[Dict]:
    base = _normalize_url(server_url)
    headers = {"X-Emby-Token": api_key}
    params = {
        "IncludeItemTypes": "Movie",
        "Recursive": "true",
        "Limit": str(limit),
        "SortBy": "SortName",
        "Fields": "ProductionYear",
    }
    url = f"{base}/Users/{jellyfin_user_id}/Items"

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    raise JellyfinError(f"Couldn't list your library (HTTP {resp.status}).")
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise JellyfinError(f"Couldn't reach your server: {e}")

    return [
        {"id": item["Id"], "name": item.get("Name", "Unknown"), "year": item.get("ProductionYear")}
        for item in data.get("Items", [])
    ]


def build_stream_url(server_url: str, api_key: str, item_id: str) -> str:
    """A direct-play URL against the user's own server. No proxying —
    the returned link points straight at their Jellyfin instance."""
    base = _normalize_url(server_url)
    return f"{base}/Items/{item_id}/Download?api_key={api_key}"
