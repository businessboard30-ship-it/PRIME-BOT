"""
Turns raw links inside a sponsored ad (its title, description and target_url)
into Discord link buttons, so an ad doesn't post a bare URL that Discord
expands into a huge preview card.

Pure Python on purpose (no discord.py import): api/cron_ad_placement.py is a
serverless function that posts over REST and must stay light, while the join
DM builds real discord.ui buttons from the same (label, url) pairs.
"""
import re
from urllib.parse import urlparse

# Discord allows up to 5 buttons in one action row; one row is plenty for an ad.
MAX_BUTTONS = 5
MAX_LABEL = 80  # Discord's button-label limit

_URL_RE = re.compile(r"https?://[^\s<>\[\]]+", re.IGNORECASE)
_TRAILING = ".,;:!?)'\"”’»"

# (host suffix, label) — first match wins. Anything else falls back to the domain.
_KNOWN = (
    (("discord.gg", "discord.com/invite", "discordapp.com/invite"), "Join Server"),
    (("t.me", "telegram.me", "telegram.org"), "Open Telegram"),
    (("youtube.com", "youtu.be"), "Watch on YouTube"),
    (("twitch.tv",), "Watch on Twitch"),
    (("tiktok.com",), "Open TikTok"),
    (("instagram.com",), "Open Instagram"),
    (("facebook.com", "fb.com"), "Open Facebook"),
    (("x.com", "twitter.com"), "Open on X"),
    (("wa.me", "whatsapp.com", "chat.whatsapp.com"), "Chat on WhatsApp"),
    (("github.com",), "Open GitHub"),
    (("reddit.com",), "Open Reddit"),
    (("patreon.com",), "Open Patreon"),
    (("ko-fi.com",), "Support on Ko-fi"),
)


def _label_for(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    host_path = host + (parsed.path or "")
    for needles, label in _KNOWN:
        for n in needles:
            if host == n or host.endswith("." + n) or host_path.startswith(n):
                return label
    return f"Visit {host}"[:MAX_LABEL] if host else "Open link"


def _clean_match(raw: str) -> str:
    return raw.rstrip(_TRAILING)


def split_ad_links(texts, target_url=None, max_buttons: int = MAX_BUTTONS):
    """
    texts: iterable of strings (title, description, ...).
    target_url: the ad's own link field ("N/A"/empty = none) — always the
    first button.

    Returns (cleaned_texts, buttons):
      cleaned_texts — same order as `texts`, with every URL that became a
        button removed (so Discord has nothing to auto-expand). URLs beyond
        the button limit are kept but wrapped in <...>, which suppresses the
        preview.
      buttons — list of (label, url), deduplicated, at most `max_buttons`.
    """
    buttons, seen = [], set()

    def add(url: str) -> bool:
        if url in seen:
            return True
        if len(buttons) >= max_buttons:
            return False
        seen.add(url)
        buttons.append((_label_for(url), url))
        return True

    tu = (target_url or "").strip()
    if tu and tu.upper() != "N/A":
        m = _URL_RE.search(tu)
        if m:
            add(_clean_match(m.group(0)))

    cleaned = []
    for text in texts:
        text = text or ""

        def repl(m):
            raw = m.group(0)
            url = _clean_match(raw)
            tail = raw[len(url):]
            if add(url):
                return "👇" + tail  # link became a button below the text
            return f"<{url}>{tail}"  # over the limit: keep, but no big preview

        out = _URL_RE.sub(repl, text)
        # tidy spaces/blank lines left behind where a link was removed
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
        out = re.sub(r"(👇[ \t]*[,;]?[ \t]*){2,}", "👇 ", out).strip()
        # A text that was nothing but links carries no words — drop it and let
        # the buttons speak for themselves.
        if not re.sub(r"[👇\s.,;:!?()\-–—]", "", out):
            out = ""
        cleaned.append(out)
    return cleaned, buttons
