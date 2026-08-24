"""
Plex connector, using Plex's official PIN-based OAuth-like login (no
password ever touches this bot). Same non-negotiable principle as the
Jellyfin/Drive connectors: we store only a token, and every stream link
resolves directly against the user's own Plex Media Server. Playback
flows user's-server -> user's-device.

Plex auth flow (https://forums.plex.tv PIN-login pattern):
  1. Request a PIN from plex.tv -> get {id, code}
  2. User opens https://app.plex.tv/auth#?...code=<code> and approves
  3. Poll plex.tv/api/v2/pins/<id> until it has an authToken
  4. Use that token against plex.tv/api/v2/resources to list the user's
     own servers, then talk to the chosen server directly.
"""

import logging
from typing import Optional, List, Dict

import aiohttp

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
PLEX_HEADERS_BASE = {
    "X-Plex-Product": "Discord Movie Connector",
    "X-Plex-Version": "1.0",
    "X-Plex-Client-Identifier": "discord-anime-bot-media-connect",
    "Accept": "application/json",
}


class PlexError(Exception):
    pass


async def create_pin() -> Dict:
    """Start a login: returns {id, code} — code goes into the auth URL."""
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(
            "https://plex.tv/api/v2/pins",
            headers=PLEX_HEADERS_BASE,
            data={"strong": "true"},
        ) as resp:
            if resp.status != 201:
                raise PlexError(f"Couldn't start Plex login (HTTP {resp.status}).")
            payload = await resp.json()
    return {"id": payload["id"], "code": payload["code"]}


def build_auth_url(pin_code: str) -> str:
    return (
        "https://app.plex.tv/auth#?"
        f"clientID={PLEX_HEADERS_BASE['X-Plex-Client-Identifier']}"
        f"&code={pin_code}"
        "&context%5Bdevice%5D%5Bproduct%5D=Discord%20Movie%20Connector"
    )


async def check_pin(pin_id: int) -> Optional[str]:
    """Poll a pin; returns the auth token once the user has approved it, else None."""
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers=PLEX_HEADERS_BASE,
        ) as resp:
            if resp.status != 200:
                raise PlexError(f"Couldn't check login status (HTTP {resp.status}).")
            payload = await resp.json()
    return payload.get("authToken") or None


async def list_servers(auth_token: str) -> List[Dict]:
    """The user's own Plex Media Servers (owned or shared with them)."""
    headers = {**PLEX_HEADERS_BASE, "X-Plex-Token": auth_token}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            "https://plex.tv/api/v2/resources",
            headers=headers,
            params={"includeHttps": "1"},
        ) as resp:
            if resp.status != 200:
                raise PlexError(f"Couldn't list your Plex servers (HTTP {resp.status}).")
            resources = await resp.json()

    servers = []
    for r in resources:
        if r.get("product") != "Plex Media Server":
            continue
        connections = r.get("connections", [])
        # Prefer a local connection, fall back to the first relay/remote one.
        best = next((c for c in connections if not c.get("relay")), connections[0] if connections else None)
        if not best:
            continue
        servers.append({
            "name": r.get("name", "Plex Server"),
            "client_identifier": r.get("clientIdentifier"),
            "base_url": best["uri"],
            "access_token": r.get("accessToken", auth_token),
        })
    return servers


async def search_movies(base_url: str, access_token: str, query: str, limit: int = 10) -> List[Dict]:
    headers = {"X-Plex-Token": access_token, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            f"{base_url}/search",
            headers=headers,
            params={"query": query, "limit": str(limit)},
        ) as resp:
            if resp.status != 200:
                raise PlexError(f"Search failed (HTTP {resp.status}).")
            payload = await resp.json()

    results = []
    for item in payload.get("MediaContainer", {}).get("Metadata", []):
        if item.get("type") != "movie":
            continue
        results.append({
            "id": item["ratingKey"],
            "name": item.get("title", "Unknown"),
            "year": item.get("year"),
            "rating": item.get("rating"),
            "summary": (item.get("summary") or "")[:300],
        })
    return results[:limit]


async def list_library(base_url: str, access_token: str, limit: int = 25) -> List[Dict]:
    headers = {"X-Plex-Token": access_token, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        # Find the first movie library section, then list its contents.
        async with session.get(f"{base_url}/library/sections", headers=headers) as resp:
            if resp.status != 200:
                raise PlexError(f"Couldn't read your library sections (HTTP {resp.status}).")
            sections = await resp.json()

        movie_section = next(
            (s for s in sections.get("MediaContainer", {}).get("Directory", []) if s.get("type") == "movie"),
            None,
        )
        if not movie_section:
            return []

        async with session.get(
            f"{base_url}/library/sections/{movie_section['key']}/all",
            headers=headers,
            params={"X-Plex-Container-Size": str(limit)},
        ) as resp:
            if resp.status != 200:
                raise PlexError(f"Couldn't list your library (HTTP {resp.status}).")
            payload = await resp.json()

    return [
        {"id": item["ratingKey"], "name": item.get("title", "Unknown"), "year": item.get("year")}
        for item in payload.get("MediaContainer", {}).get("Metadata", [])
    ]


def build_stream_url(base_url: str, access_token: str, rating_key: str) -> str:
    """Direct-play URL against the user's own Plex server. No proxying."""
    return f"{base_url}/library/parts/{rating_key}/file?X-Plex-Token={access_token}"


async def build_download_url(base_url: str, access_token: str, rating_key: str) -> str:
    """Plex needs the actual media Part key (not the item's ratingKey) to
    build a direct file link — look it up first."""
    headers = {"X-Plex-Token": access_token, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(f"{base_url}/library/metadata/{rating_key}", headers=headers) as resp:
            if resp.status != 200:
                raise PlexError(f"Couldn't resolve stream (HTTP {resp.status}).")
            payload = await resp.json()

    try:
        part_key = payload["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]["key"]
    except (KeyError, IndexError):
        raise PlexError("Couldn't find a playable file for that title.")

    return f"{base_url}{part_key}?X-Plex-Token={access_token}"
