# path: modules/member_msg.py
"""Member Drop Box and friends (Part D): the pure rules. No I/O here, so every limit and check is unit-testable.

Who can talk to whom is decided on the SERVER from the session user's id, a friendship row both people agreed to,
and Discord itself (shared server). Nothing in this file trusts the browser.
"""
import json
import re
import unicodedata

MAX_BODY = 1000                # characters per message (text only, no attachments in v1)
REQUESTS_PER_DAY = 10          # new friend requests one person may send in 24 h
MAX_PENDING_OUT = 20           # requests a person may have waiting at once
MAX_PENDING_IN = 50            # requests that may wait for one person; extras are dropped silently
MAX_FRIENDS = 100
MSGS_PER_MINUTE = 10
MSGS_PER_DAY = 300
MAX_UNREAD_TO_ONE = 20         # unanswered messages one sender may have waiting for one person
REPORTS_PER_DAY = 10
RETENTION_DAYS = 30            # messages are deleted after this
REPORT_RETENTION_DAYS = 90     # a report (and its message snapshot) is deleted this long after the owner resolves it
SNAPSHOT_MESSAGES = 20         # last messages kept with a report for the owner
THREAD_LIMIT = 50
SEARCH_MIN = 2
SEARCH_LIMIT = 10
NAME_MAX = 40
SWITCH = "messaging"           # admin_controls kill switch

STATUSES = ("pending", "accepted", "blocked")
NOTICE_TEXT = ("You have a new message on the dashboard. Open the website's Messages page to read it. "
               "You can switch these notices off there at any time.")

# bidi controls and zero-width characters: they hide text or reorder it, so they never belong in a chat line
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_BLANKS = re.compile(r"\n{3,}")


def snowflake(raw):
    """A Discord id as a string, or None. Digits only, plausible length, in range."""
    s = str(raw if raw is not None else "").strip()
    if not s.isdigit() or not 15 <= len(s) <= 20:
        return None
    n = int(s)
    return str(n) if 0 < n < 2 ** 64 else None


def pair(a, b):
    """Canonical (lower, higher) order by integer value: the same two people always map to the same row."""
    a, b = str(int(a)), str(int(b))
    return (a, b) if int(a) < int(b) else (b, a)


def clean_body(raw):
    """-> (text, error). Text only: control characters, bidi tricks and zero-width characters are removed."""
    if not isinstance(raw, str):
        return None, "Write a message first."
    t = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    t = _INVISIBLE.sub("", t)
    t = "".join(ch for ch in t if ch == "\n" or (ch.isprintable() or ch == "\t"))
    t = _BLANKS.sub("\n\n", t).strip()
    if not t:
        return None, "Write a message first."
    if len(t) > MAX_BODY:
        return None, f"Messages can be at most {MAX_BODY} characters."
    return t, None


def clip_name(raw, fallback="Member"):
    t = "".join(ch for ch in str(raw or "") if ch.isprintable())
    t = _INVISIBLE.sub("", t).strip()[:NAME_MAX]
    return t or fallback


def relation(row, me):
    """What the SESSION user may see about a friendship row: none | pending_out | pending_in | friends | blocked_by_me.
    A person who blocked you is reported as 'none' so you cannot tell."""
    if not row:
        return "none"
    st = row.get("status")
    if st == "accepted":
        return "friends"
    if st == "pending":
        return "pending_out" if str(row.get("requested_by")) == str(me) else "pending_in"
    if st == "blocked":
        return "blocked_by_me" if str(row.get("blocked_by")) == str(me) else "none"
    return "none"


def request_block_reason(counts):
    """Own limits only (these never say anything about the other person). -> error text or None."""
    if counts["sent_today"] >= REQUESTS_PER_DAY:
        return f"You can send {REQUESTS_PER_DAY} friend requests a day. Try again tomorrow."
    if counts["pending_out"] >= MAX_PENDING_OUT:
        return f"You already have {MAX_PENDING_OUT} requests waiting. Wait for answers or cancel some."
    if counts["friends"] >= MAX_FRIENDS:
        return f"You can have up to {MAX_FRIENDS} friends."
    return None


def send_block_reason(counts):
    """Sender's own message limits. counts = db.msg_send_counts()."""
    if counts["minute"] >= MSGS_PER_MINUTE:
        return "You're sending messages too fast. Wait a moment."
    if counts["day"] >= MSGS_PER_DAY:
        return "You've reached today's message limit."
    if counts["unread_to_them"] >= MAX_UNREAD_TO_ONE:
        return "They haven't read your earlier messages yet. Wait for a reply before sending more."
    return None


def snapshot_json(messages, reporter):
    """The last SNAPSHOT_MESSAGES of a conversation for the owner's report queue (oldest first).
    'from' is reporter|reported, never an id, so the queue shows who said what without extra identifiers."""
    out = []
    for m in (messages or [])[-SNAPSHOT_MESSAGES:]:
        at = m.get("created_at")
        out.append({"from": "reporter" if str(m.get("sender_id")) == str(reporter) else "reported",
                    "body": str(m.get("body") or "")[:MAX_BODY],
                    "at": at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else None)})
    return json.dumps(out, separators=(",", ":"))


def parse_snapshot(raw):
    """Back from the DB text to a safe list for the owner page (bad data -> empty)."""
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    out = []
    for m in data if isinstance(data, list) else []:
        if isinstance(m, dict):
            out.append({"from": "reporter" if m.get("from") == "reporter" else "reported",
                        "body": str(m.get("body") or "")[:MAX_BODY], "at": str(m.get("at") or "")[:40]})
    return out[:SNAPSHOT_MESSAGES]
