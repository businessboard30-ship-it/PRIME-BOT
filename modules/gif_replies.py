"""Fixed GIF replies for certain messages (no AI call, costs no daily cap).

Each kind has a trigger regex and a GIF. Direct media.tenor.com/*.gif links are
sent as an embed image; tenor.com/view/... page links can't be embedded, so
they are sent as a plain message and Discord unfurls them itself.
"""
import re
from typing import Optional

GIFS = {
    "love_no": "https://media.tenor.com/DgQ_vQpye88AAAAM/hell-no-alexis.gif",
    "real": "https://tenor.com/view/ai-ai-art-ai-vid-ai-video-ai-warning-gif-13767229340000976483",
    "insult": "https://media.tenor.com/93OaPgicQccAAAAM/idc-benjammins.gif",
    "no": "https://media.tenor.com/6JHdjWa9wmYAAAAM/benjammins-no.gif",
    "agree": "https://media.tenor.com/vkjKG6Kd6U8AAAAM/hug-bros.gif",
}

_I = re.IGNORECASE
_PATTERNS = {
    # "be my girlfriend", "date me", "marry me"
    "love_no": re.compile(
        r"\b(?:date|dating|go\s+out\s+with|going\s+out\s+with)\s+(?:with\s+)?(?:me|us)\b|"
        r"\b(?:can|could|may|will|would|do|wanna|want\s+to)\s+(?:i\s+|you\s+)?(?:date|go\s+on\s+a\s+date)\b.*\b(?:me|you)\b|"
        r"\b(?:date|ask|asking)\s+you(?:\s+out)?\b|\bgo\s+on\s+a\s+date\b|"
        r"\b(?:be|become)\s+my\s+(?:girlfriend|boyfriend|gf|bf|partner|lover|wife|husband|valentine)\b|"
        r"\bmarry\s+me\b", _I),
    # "are you real / a bot / human?"
    "real": re.compile(
        r"\b(?:are|r)\s+(?:you|u)\s+(?:a\s+|an\s+)?(?:real|human|person|bot|robot|ai|alive|fake)\b|"
        r"\b(?:is\s+this|you\s+a)\s+(?:a\s+)?(?:real\s+person|bot|robot|ai)\b", _I),
    # insults aimed at the bot
    "insult": re.compile(
        r"\b(?:you|u|ur|you're|youre|you\s+are)\s*(?:are\s+|r\s+)?(?:so\s+|very\s+|such\s+an?\s+|an?\s+)?"
        r"(?:dumb|stupid|useless|trash|garbage|ugly|annoying|idiot|moron|bad|worthless)\b|"
        r"\b(?:you|u)\s+suck\b|^\s*(?:just\s+)?shut\s+up\W*$|\bstfu\b", _I),
    # asks the bot should turn down
    "no": re.compile(
        r"\b(?:give|gimme|send)\s+(?:me\s+)?(?:free\s+)?(?:premium|coins|money)\s+(?:for\s+)?free\b|"
        r"\bfree\s+(?:premium|coins)\s+(?:please|pls|plz)\b|\bsend\s+nudes?\b|\bsex\s+with\s+(?:me|you)\b", _I),
    # asking the bot to agree / be friends
    "agree": re.compile(
        r"\b(?:do\s+)?(?:you|u)\s+agree\b|\bagree\s+with\s+me\b|\bam\s+i\s+right\b|"
        r"\bare\s+we\s+(?:cool|good|friends)\b|\bbe\s+(?:my\s+)?friends?\b", _I),
}
# first match wins
_ORDER = ("love_no", "real", "insult", "no", "agree")


def gif_kind(text: str) -> Optional[str]:
    if not text:
        return None
    for kind in _ORDER:
        if _PATTERNS[kind].search(text):
            return kind
    return None


def gif_for(text: str) -> Optional[str]:
    kind = gif_kind(text)
    return GIFS[kind] if kind else None


def is_gif_url(text) -> bool:
    return bool(text) and str(text).strip() in GIFS.values()


def is_view_link(url: str) -> bool:
    return "tenor.com/view/" in str(url)
