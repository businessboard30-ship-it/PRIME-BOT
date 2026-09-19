# path: modules/connections_api.py

"""
Thin async clients for the /connections cog (discord_bot/cogs/connect.py).

YouTube
  - Data API v3 (needs YOUTUBE_API_KEY, free quota): video / channel /
    playlist info, trending by country, random video, channel resolution.
  - Public RSS feed (no key): new-upload polling for notifications.

Roblox
  - Public web APIs, no key. If roblox.com answers with an error/blocked
    response (some hosts get 403/429), each call retries once through the
    roproxy.com mirror.

Every function returns plain dicts (or None when not found) and raises
ConnectError with a user-safe message on real failures.
"""

import logging
import os
import random
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse, parse_qs

import aiohttp

logger = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class ConnectError(Exception):
    """User-safe failure message."""


# ══ YouTube ═══════════════════════════════════════════════════════════════

YT_API = "https://www.googleapis.com/youtube/v3"

# Popular regions for the trending picker (Discord selects hold 25 options).
TRENDING_REGIONS = {
    "GH": "Ghana", "NG": "Nigeria", "ZA": "South Africa", "KE": "Kenya", "EG": "Egypt",
    "US": "United States", "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina",
    "GB": "United Kingdom", "FR": "France", "DE": "Germany", "ES": "Spain", "IT": "Italy",
    "NL": "Netherlands", "PL": "Poland", "TR": "Turkey", "SA": "Saudi Arabia", "AE": "UAE",
    "IN": "India", "PK": "Pakistan", "ID": "Indonesia", "JP": "Japan", "KR": "South Korea",
}
_RANDOM_CATEGORIES = ["1", "10", "15", "17", "20", "22", "23", "24", "25", "26", "28"]

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_PLAYLIST_ID = re.compile(r"^(PL|UU|LL|FL|OL|RD)[A-Za-z0-9_-]{10,}$")


def youtube_configured() -> bool:
    return bool(YOUTUBE_API_KEY)


async def _yt_get(path: str, **params) -> dict:
    if not YOUTUBE_API_KEY:
        raise ConnectError("YouTube isn't configured yet — the bot owner needs to set `YOUTUBE_API_KEY`.")
    params["key"] = YOUTUBE_API_KEY
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"{YT_API}/{path}", params=params) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    err = (data.get("error") or {}) if isinstance(data, dict) else {}
                    reason = ((err.get("errors") or [{}])[0]).get("reason", "")
                    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                        raise ConnectError("YouTube's daily lookup quota is used up — try again later.")
                    if reason == "keyInvalid" or r.status == 403:
                        raise ConnectError("The YouTube API key was rejected.")
                    raise ConnectError(err.get("message", f"YouTube error (HTTP {r.status})")[:200])
                return data
    except ConnectError:
        raise
    except Exception as e:
        logger.warning(f"[connect] YouTube API call failed: {e}")
        raise ConnectError("Couldn't reach YouTube right now — try again in a moment.")


def parse_video_id(text: str):
    t = (text or "").strip()
    if _VIDEO_ID.match(t):
        return t
    try:
        u = urlparse(t if "://" in t else "https://" + t)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host in ("youtu.be", "www.youtu.be"):
        vid = u.path.strip("/").split("/")[0]
        return vid if _VIDEO_ID.match(vid) else None
    if "youtube.com" in host:
        q = parse_qs(u.query)
        if q.get("v") and _VIDEO_ID.match(q["v"][0]):
            return q["v"][0]
        m = re.match(r"^/(shorts|embed|live|v)/([A-Za-z0-9_-]{11})", u.path)
        if m:
            return m.group(2)
    return None


def parse_playlist_id(text: str):
    t = (text or "").strip()
    if _PLAYLIST_ID.match(t):
        return t
    try:
        u = urlparse(t if "://" in t else "https://" + t)
    except ValueError:
        return None
    q = parse_qs(u.query)
    if q.get("list") and _PLAYLIST_ID.match(q["list"][0]):
        return q["list"][0]
    return None


def parse_channel_ref(text: str):
    """-> ("id"|"handle"|"user"|"search", value)"""
    t = (text or "").strip()
    if _CHANNEL_ID.match(t):
        return "id", t
    if t.startswith("@"):
        return "handle", t
    try:
        u = urlparse(t if "://" in t else "https://" + t)
    except ValueError:
        return "search", t
    host = (u.hostname or "").lower()
    if "youtube.com" in host:
        m = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})", u.path)
        if m:
            return "id", m.group(1)
        m = re.match(r"^/(@[^/]+)", u.path)
        if m:
            return "handle", m.group(1)
        m = re.match(r"^/user/([^/]+)", u.path)
        if m:
            return "user", m.group(1)
        m = re.match(r"^/c/([^/]+)", u.path)
        if m:
            return "search", m.group(1)
    return "search", t


def _thumb(snippet: dict) -> str:
    th = snippet.get("thumbnails") or {}
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in th:
            return th[k]["url"]
    return ""


def _iso_duration(d: str) -> str:
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", d or "")
    if not m:
        return "—"
    days, h, mi, sec = (int(x or 0) for x in m.groups())
    h += days * 24
    return f"{h}:{mi:02d}:{sec:02d}" if h else f"{mi}:{sec:02d}"


def _video_dict(item: dict) -> dict:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    cd = item.get("contentDetails", {})
    return {
        "id": item["id"] if isinstance(item["id"], str) else item["id"].get("videoId"),
        "title": sn.get("title", "Untitled"),
        "description": sn.get("description", ""),
        "channel": sn.get("channelTitle", "Unknown"),
        "channel_id": sn.get("channelId"),
        "published": sn.get("publishedAt"),
        "thumbnail": _thumb(sn),
        "tags": sn.get("tags") or [],
        "duration": _iso_duration(cd.get("duration", "")),
        "views": int(st["viewCount"]) if "viewCount" in st else None,
        "likes": int(st["likeCount"]) if "likeCount" in st else None,
        "comments": int(st["commentCount"]) if "commentCount" in st else None,
        "live": sn.get("liveBroadcastContent", "none"),
        "definition": (cd.get("definition") or "").upper(),
    }


async def _search_first(query: str, kind: str):
    data = await _yt_get("search", part="snippet", q=query, type=kind, maxResults=1)
    items = data.get("items") or []
    if not items:
        return None
    return items[0]["id"].get(f"{kind}Id")


async def get_video(query: str):
    vid = parse_video_id(query)
    if vid is None:
        vid = await _search_first(query, "video")
        if vid is None:
            return None
    data = await _yt_get("videos", part="snippet,contentDetails,statistics", id=vid)
    items = data.get("items") or []
    return _video_dict(items[0]) if items else None


async def get_channel(query: str):
    kind, val = parse_channel_ref(query)
    if kind == "search":
        cid = await _search_first(val, "channel")
        if cid is None:
            return None
        kind, val = "id", cid
    params = {"part": "snippet,statistics,brandingSettings,contentDetails"}
    if kind == "id":
        params["id"] = val
    elif kind == "handle":
        params["forHandle"] = val
    else:
        params["forUsername"] = val
    data = await _yt_get("channels", **params)
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    sn, st = it.get("snippet", {}), it.get("statistics", {})
    return {
        "id": it["id"],
        "title": sn.get("title", "Unknown"),
        "handle": sn.get("customUrl"),
        "description": sn.get("description", ""),
        "created": sn.get("publishedAt"),
        "country": sn.get("country"),
        "thumbnail": _thumb(sn),
        "banner": (it.get("brandingSettings", {}).get("image") or {}).get("bannerExternalUrl"),
        "subs": None if st.get("hiddenSubscriberCount") else (int(st["subscriberCount"]) if "subscriberCount" in st else None),
        "views": int(st["viewCount"]) if "viewCount" in st else None,
        "videos": int(st["videoCount"]) if "videoCount" in st else None,
        "uploads_playlist": (it.get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads"),
    }


async def get_playlist(query: str):
    pid = parse_playlist_id(query)
    if pid is None:
        return None
    data = await _yt_get("playlists", part="snippet,contentDetails", id=pid)
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    sn = it.get("snippet", {})
    first = await _yt_get("playlistItems", part="snippet", playlistId=pid, maxResults=5)
    return {
        "id": pid,
        "title": sn.get("title", "Untitled"),
        "description": sn.get("description", ""),
        "channel": sn.get("channelTitle", "Unknown"),
        "channel_id": sn.get("channelId"),
        "created": sn.get("publishedAt"),
        "thumbnail": _thumb(sn),
        "count": (it.get("contentDetails") or {}).get("itemCount"),
        "first": [
            {"title": x["snippet"].get("title", ""), "id": (x["snippet"].get("resourceId") or {}).get("videoId")}
            for x in (first.get("items") or [])
        ],
    }


async def get_trending(region: str, limit: int = 10) -> list:
    region = (region or "").strip().upper()
    if not re.match(r"^[A-Z]{2}$", region):
        raise ConnectError("Use a 2-letter country code like `GH` or `US`.")
    data = await _yt_get(
        "videos", part="snippet,contentDetails,statistics",
        chart="mostPopular", regionCode=region, maxResults=limit,
    )
    return [_video_dict(i) for i in (data.get("items") or [])]


async def get_random_video():
    """A random popular video: random region x random category from the
    mostPopular chart (1 quota unit per try, unlike search's 100). Some
    region/category pairs have no chart and return an error — just retry."""
    regions = list(TRENDING_REGIONS)
    last_err = None
    for _ in range(5):
        region = random.choice(regions)
        params = dict(part="snippet,contentDetails,statistics", chart="mostPopular", regionCode=region, maxResults=25)
        if random.random() < 0.7:
            params["videoCategoryId"] = random.choice(_RANDOM_CATEGORIES)
        try:
            data = await _yt_get("videos", **params)
        except ConnectError as e:
            last_err = e
            if "quota" in str(e).lower() or "configured" in str(e).lower() or "key" in str(e).lower():
                raise
            continue
        items = data.get("items") or []
        if items:
            v = _video_dict(random.choice(items))
            v["region"] = region
            return v
    if last_err:
        raise last_err
    return None


async def get_channel_description(channel_id: str):
    """For verification codes."""
    data = await _yt_get("channels", part="snippet", id=channel_id)
    items = data.get("items") or []
    return (items[0].get("snippet", {}).get("description") or "") if items else None


async def fetch_channel_feed(channel_id: str) -> list:
    """Newest-first list of {id, title, published, author} from the public
    RSS feed (no API key, no quota). Empty list if the channel has no
    videos; raises ConnectError if the feed can't be read."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id)}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status == 404:
                    raise ConnectError("That YouTube channel doesn't exist.")
                if r.status != 200:
                    raise ConnectError(f"YouTube feed error (HTTP {r.status})")
                text = await r.text()
    except ConnectError:
        raise
    except Exception as e:
        logger.debug(f"[connect] feed fetch failed for {channel_id}: {e}")
        raise ConnectError("Couldn't read that channel's feed right now.")
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ConnectError("YouTube returned an unreadable feed.")
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", default="", namespaces=ns)
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.findtext("a:title", default="New video", namespaces=ns),
            "published": e.findtext("a:published", default="", namespaces=ns),
            "author": e.findtext("a:author/a:name", default="", namespaces=ns),
        })
    return out


# ══ Roblox ════════════════════════════════════════════════════════════════

_RBX_HEADERS = {"User-Agent": "Mozilla/5.0 (PRIME-BOT connect)", "Accept": "application/json"}


async def _rbx(method: str, url: str, **kw):
    """One call, retried once via the roproxy mirror. Returns parsed JSON
    or None on 404."""
    attempts = [url, url.replace("roblox.com", "roproxy.com")]
    last = None
    for u in attempts:
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_RBX_HEADERS) as s:
                async with s.request(method, u, **kw) as r:
                    if r.status == 404:
                        return None
                    if r.status == 200:
                        return await r.json(content_type=None)
                    last = r.status
        except Exception as e:
            last = e
    logger.warning(f"[connect] Roblox call failed {url}: {last}")
    raise ConnectError("Couldn't reach Roblox right now — try again in a moment.")


async def rbx_user_by_name(username: str):
    data = await _rbx(
        "POST", "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username.strip()], "excludeBannedUsers": False},
    )
    rows = (data or {}).get("data") or []
    return rows[0]["id"] if rows else None


def parse_roblox_user_ref(text: str):
    """-> ("id", int) | ("name", str)"""
    t = (text or "").strip()
    m = re.search(r"roblox\.com/users/(\d+)", t)
    if m:
        return "id", int(m.group(1))
    if t.isdigit():
        return "id", int(t)
    return "name", t.lstrip("@")


async def rbx_get_user(query: str):
    kind, val = parse_roblox_user_ref(query)
    uid = val if kind == "id" else await rbx_user_by_name(val)
    if uid is None:
        return None
    u = await _rbx("GET", f"https://users.roblox.com/v1/users/{uid}")
    if not u:
        return None
    friends = await _safe_count(f"https://friends.roblox.com/v1/users/{uid}/friends/count")
    followers = await _safe_count(f"https://friends.roblox.com/v1/users/{uid}/followers/count")
    following = await _safe_count(f"https://friends.roblox.com/v1/users/{uid}/followings/count")
    thumb = await _rbx(
        "GET", "https://thumbnails.roblox.com/v1/users/avatar-headshot",
        params={"userIds": uid, "size": "420x420", "format": "Png", "isCircular": "false"},
    )
    img = ((thumb or {}).get("data") or [{}])[0].get("imageUrl")
    return {
        "id": uid, "name": u.get("name"), "display": u.get("displayName"),
        "description": u.get("description") or "", "created": u.get("created"),
        "banned": bool(u.get("isBanned")), "verified_badge": bool(u.get("hasVerifiedBadge")),
        "friends": friends, "followers": followers, "following": following, "avatar": img,
    }


async def _safe_count(url: str):
    try:
        d = await _rbx("GET", url)
        return (d or {}).get("count")
    except ConnectError:
        return None


async def rbx_user_description(user_id: int):
    u = await _rbx("GET", f"https://users.roblox.com/v1/users/{user_id}")
    return (u or {}).get("description") if u else None


def parse_place_id(text: str):
    t = (text or "").strip()
    m = re.search(r"roblox\.com/(?:[a-z]{2}/)?games/(\d+)", t)
    if m:
        return int(m.group(1))
    return int(t) if t.isdigit() else None


async def rbx_universe_of_place(place_id: int):
    d = await _rbx("GET", f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
    return (d or {}).get("universeId")


async def rbx_get_game(query: str):
    place = parse_place_id(query)
    if place is None:
        raise ConnectError("Paste a Roblox game link (roblox.com/games/…) or its place ID.")
    uni = await rbx_universe_of_place(place)
    if not uni:
        return None
    d = await _rbx("GET", "https://games.roblox.com/v1/games", params={"universeIds": uni})
    rows = (d or {}).get("data") or []
    if not rows:
        return None
    g = rows[0]
    icon = await _rbx(
        "GET", "https://thumbnails.roblox.com/v1/games/icons",
        params={"universeIds": uni, "size": "512x512", "format": "Png", "isCircular": "false"},
    )
    img = ((icon or {}).get("data") or [{}])[0].get("imageUrl")
    creator = g.get("creator") or {}
    return {
        "universe": uni, "place": place, "name": g.get("name"),
        "description": g.get("description") or "", "creator": creator.get("name"),
        "creator_type": creator.get("type"), "playing": g.get("playing"),
        "visits": g.get("visits"), "favorites": g.get("favoritedCount"),
        "max_players": g.get("maxPlayers"), "genre": g.get("genre"),
        "created": g.get("created"), "updated": g.get("updated"), "icon": img,
    }


def parse_group_id(text: str):
    t = (text or "").strip()
    m = re.search(r"roblox\.com/(?:[a-z]{2}/)?(?:communities|groups)/(\d+)", t)
    if m:
        return int(m.group(1))
    return int(t) if t.isdigit() else None


async def rbx_get_group(query: str):
    gid = parse_group_id(query)
    if gid is None:
        raise ConnectError("Paste a Roblox group link or its numeric ID.")
    g = await _rbx("GET", f"https://groups.roblox.com/v1/groups/{gid}")
    if not g or g.get("errors"):
        return None
    icon = await _rbx(
        "GET", "https://thumbnails.roblox.com/v1/groups/icons",
        params={"groupIds": gid, "size": "420x420", "format": "Png", "isCircular": "false"},
    )
    img = ((icon or {}).get("data") or [{}])[0].get("imageUrl")
    owner = g.get("owner") or {}
    return {
        "id": gid, "name": g.get("name"), "description": g.get("description") or "",
        "members": g.get("memberCount"), "owner": owner.get("username") or owner.get("displayName"),
        "public": bool(g.get("publicEntryAllowed")), "verified_badge": bool(g.get("hasVerifiedBadge")),
        "shout": (g.get("shout") or {}).get("body"), "icon": img,
    }


async def rbx_game_state(universe_id: int):
    """For the update poller: {name, updated, playing} or None."""
    d = await _rbx("GET", "https://games.roblox.com/v1/games", params={"universeIds": universe_id})
    rows = (d or {}).get("data") or []
    if not rows:
        return None
    g = rows[0]
    return {"name": g.get("name"), "updated": g.get("updated"), "playing": g.get("playing")}
