# path: modules/remote_art.py
"""Card artwork that can live off-repo (Cloudinary) instead of in git.

resolve_art(filename, folder) returns a local file path for a bundled card image:

1. the file next to the code (repo root) if it still exists -> behaviour unchanged,
2. else the on-disk cache (ART_CACHE_DIR, default <tmp>/prime_bot_art),
3. else it is downloaded once from ART_BASE_URL, cached atomically, and the cached path is returned.

If the download fails the original local path is returned, so the caller raises the
same FileNotFoundError it always did (and its existing error handling still applies).
Failures are remembered for a short while so a Cloudinary outage can't stall every render.
"""
import logging
import os
import tempfile
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

ART_BASE_URL = os.environ.get(
    "ART_BASE_URL", "https://res.cloudinary.com/owvhpwyd/image/upload/prime-bot"
).rstrip("/")
ART_CACHE_DIR = os.environ.get("ART_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "prime_bot_art")
DOWNLOAD_TIMEOUT = float(os.environ.get("ART_DOWNLOAD_TIMEOUT", "20"))
FAIL_COOLDOWN_SECONDS = 60

_lock = threading.Lock()
_failed_at: dict = {}


def _download(url: str, dest: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return False
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        if os.path.getsize(tmp) == 0:
            return False
        os.replace(tmp, dest)
        return True
    except Exception:
        logger.warning("remote art: download failed for %s", url, exc_info=True)
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def resolve_art(local_path: str, folder: str) -> str:
    """local_path: where the file used to be read from; folder: tier / clan / godhood / welcome."""
    if os.path.exists(local_path):
        return local_path
    name = os.path.basename(local_path)
    cached = os.path.join(ART_CACHE_DIR, folder, name)
    if os.path.exists(cached):
        return cached
    with _lock:
        if os.path.exists(cached):
            return cached
        last = _failed_at.get(cached)
        if last and time.monotonic() - last < FAIL_COOLDOWN_SECONDS:
            return local_path
        if _download(f"{ART_BASE_URL}/{folder}/{name}", cached):
            return cached
        _failed_at[cached] = time.monotonic()
    return local_path
