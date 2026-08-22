"""
Google Drive connector, scoped to files the user picks via Google's own
Picker/consent flow. We store only an OAuth token (encrypted) and a
folder id — never the files themselves. Streaming uses Drive's own
range-request download endpoint, so playback flows straight from
Google's servers to the user's device; this bot only ever returns a URL.

Uses the drive.file scope, which only grants access to files the user
explicitly opens/creates through this app — not their whole Drive.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")  # e.g. https://yourapp.vercel.app/api/oauth_gdrive
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class GDriveError(Exception):
    pass


def build_authorize_url(state: str) -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


async def exchange_code(code: str) -> Dict:
    """Exchange an OAuth authorization code for access + refresh tokens."""
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post("https://oauth2.googleapis.com/token", data=data) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise GDriveError(payload.get("error_description", "Token exchange failed."))
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=payload.get("expires_in", 3600)),
    }


async def refresh_access_token(refresh_token: str) -> Dict:
    data = {
        "refresh_token": refresh_token,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post("https://oauth2.googleapis.com/token", data=data) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise GDriveError(payload.get("error_description", "Token refresh failed."))
    return {
        "access_token": payload["access_token"],
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=payload.get("expires_in", 3600)),
    }


async def ensure_valid_token(connection: Dict) -> str:
    """Given a gdrive_connections row (dict, already decrypted by database.py),
    refresh the access token if it's expired and return a usable one. Caller
    is responsible for persisting a refreshed token back to the DB."""
    expires_at = connection["token_expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=2):
        return connection["access_token"]

    refreshed = await refresh_access_token(connection["refresh_token"])
    from database import db  # local import to avoid a circular import at module load
    await db.update_gdrive_access_token(connection["user_id"], refreshed["access_token"], refreshed["expires_at"])
    return refreshed["access_token"]


async def get_or_create_app_folder(access_token: str, folder_name: str = "Discord Bot Movies") -> Dict:
    """Find (or create) a single folder for this app to work in, so the
    connection is scoped to one folder rather than the whole Drive."""
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": query, "fields": "files(id,name)"},
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise GDriveError(payload.get("error", {}).get("message", "Couldn't search Drive."))
            files = payload.get("files", [])
            if files:
                return {"id": files[0]["id"], "name": files[0]["name"]}

        async with session.post(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            json={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        ) as resp:
            payload = await resp.json()
            if resp.status not in (200, 201):
                raise GDriveError(payload.get("error", {}).get("message", "Couldn't create folder."))
            return {"id": payload["id"], "name": payload["name"]}


async def search_files(access_token: str, folder_id: str, query: str, limit: int = 10) -> List[Dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    q = f"'{folder_id}' in parents and trashed = false and name contains '{query}'"

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": q, "pageSize": str(limit), "fields": "files(id,name,size,mimeType)"},
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise GDriveError(payload.get("error", {}).get("message", "Search failed."))

    return [
        {"id": f["id"], "name": f["name"], "size_bytes": int(f.get("size", 0)), "mime_type": f.get("mimeType")}
        for f in payload.get("files", [])
        if str(f.get("mimeType", "")).startswith("video/")
    ]


def build_stream_url(file_id: str, access_token: str) -> str:
    """Drive's own range-request download endpoint — playback flows
    Google's servers -> user's device, never through this bot."""
    return f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&access_token={access_token}"
