"""What the AI can do, grouped for display (the "what can you do?" panel).

Pure logic, no Discord objects: it turns a list of AICommandSpec (already filtered to what THIS asker is allowed to
run -- see ai_command_guard.get_qualifying_commands) into pages of text. Nothing here decides who may see what;
the caller must only ever pass specs the asker qualifies for, so a command the person cannot run is never listed.
"""
import re

# cog module (last part) -> category key
_CATEGORY_OF = {
    "moderation": "mod", "automod": "mod", "verification": "mod", "antiraid": "mod", "honeypot": "mod",
    "scam_shield": "mod", "server_logs": "mod", "applications": "mod",
    "setup_channels": "setup", "role_setup": "setup", "automation": "setup", "quickstart": "setup",
    "ticket": "setup", "welcome": "setup", "reaction_roles": "setup", "link_buttons": "setup", "invites": "setup",
    "language": "setup", "style": "setup", "starboard": "setup", "schedule": "setup", "autopost": "setup",
    "suggestions": "setup", "submissions": "setup", "dashboard": "setup", "archive": "setup", "analytics": "setup",
    "leveling": "fun", "voice_xp": "fun", "giveaways": "fun", "cat": "fun", "roast_arena": "fun",
    "botstore": "fun", "referrals": "fun",
    "external_tools": "tools", "ai_tools": "tools", "help": "tools", "feedback": "tools", "discover": "tools",
    "bump": "tools", "bump_setup": "tools", "clone_admin": "tools", "admin": "tools",
}

CATEGORIES = (            # (key, emoji, label) in display order
    ("mod", "🛡️", "Moderation & safety"),
    ("setup", "⚙️", "Server setup"),
    ("admin", "🔧", "Server admin (roles, channels, threads, events...)"),
    ("fun", "🎮", "Community & fun"),
    ("tools", "🧰", "Tools & more"),
)
_LABEL = {k: (e, l) for k, e, l in CATEGORIES}

_SLANG = (("wat", "what"), ("wut", "what"), ("ur", "your"), ("u", "you"), ("ya", "you"), ("cmds", "commands"), ("cmd", "command"), ("abilities", "abilities"))
_YOU = r"(you|your|yourself|the ai|ai|the bot|bot|primebot|available|there)"
_THINGS = r"(commands|features|abilities|capabilities|powers|skills|tools|functions)"
_ASK = re.compile(
    r"\bwhat (can|could|do|are|is|will|would)( (you|the ai|the bot|ai|bot))? (be )?"
    r"(able to )?(do|perform|run|handle|execute|help( me)? with|offer|support|automate)\b(?! (for fun|for a living|at work|all day))"
    r"|\bwhat (are|is) (all )?(the )?" + _THINGS + r"\b"
    r"|\bwhat " + _THINGS + r" (do|can|does|are|is|have|has)\b"
    r"|\b(show|list|display|see|give|tell)( me)?( all)?( of)?( the| your| ur| all)* " + _THINGS + r"\b"
    r"|\b" + _YOU + r" " + _THINGS + r"\b"
    r"|\b" + _THINGS + r" (you|u|the ai|ai|the bot) (can|could|have|got|support|know)\b"
    r"|\bwhat can i (ask|do with|use|tell) (you|the ai|the bot|this|it)\b"
    r"|\bhow (do|can) i use (the )?(ai|you|this bot)\b", re.I)


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, and expand chat shorthand ("u", "ur", "cmds") so phrasing variants all match."""
    t = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())
    words = [dict(_SLANG).get(w, w) for w in t.split()]
    return " ".join(words)


def is_capabilities_question(text: str) -> bool:
    """True for 'what can you do', 'what are the commands u can perform', 'show me your commands' style asks.
    A question about ONE thing ('how do i ban someone', 'what command makes a role') is not a match: that goes to
    the normal AI, which answers it specifically."""
    t = _normalise(text)
    if not t or len(t) > 120:
        return False
    return bool(_ASK.search(t))


# The AI-only server-admin commands all live in one cog, so they are grouped by their command root instead.
_ADMIN_ROOT_CATEGORY = {"member": "mod", "voice": "mod", "messageops": "mod"}


def category_of(spec) -> str:
    module = spec.cog_module.rsplit(".", 1)[-1]
    if module == "ai_server_ops":
        return _ADMIN_ROOT_CATEGORY.get(spec.command_path.split(" ", 1)[0], "admin")
    return _CATEGORY_OF.get(module, "tools")


def _line(spec) -> str:
    desc = " ".join((spec.description or "").split())
    desc = re.split(r"(?<=[.!?])\s|\sArgs:", desc, maxsplit=1)[0][:110]
    tail = " 🤖" if getattr(spec, "ai_only", False) else ""
    return f"`{spec.command_path}`{tail} — {desc}"


def build_pages(specs) -> dict:
    """{category key: text} for the categories that have at least one spec. Each text stays well under Discord's
    4000-character limit for a message's text; if a category is long the tail is summarised ("...and N more")."""
    by_cat = {k: [] for k, _, _ in CATEGORIES}
    for s in specs:
        by_cat[category_of(s)].append(s)
    pages = {}
    for key, items in by_cat.items():
        if not items:
            continue
        items.sort(key=lambda s: s.command_path)
        lines, size = [], 0
        for i, s in enumerate(items):
            ln = _line(s)
            if size + len(ln) + 1 > 3000:
                lines.append(f"-# …and {len(items) - i} more. Just ask me.")
                break
            lines.append(ln)
            size += len(ln) + 1
        pages[key] = "\n".join(lines)
    return pages


def label_for(key: str) -> tuple:
    return _LABEL.get(key, ("✨", "More"))


def overview_text(specs, in_server: bool) -> str:
    if not in_server:
        return ("I can chat, answer questions about the bot, and — inside a server — run commands for you when you "
                "describe the job in plain words. Ask me in a server to see what **you** can use.")
    n = len(specs)
    ai_only = sum(1 for s in specs if getattr(s, "ai_only", False))
    extra = f" **{ai_only}** of them (🤖) exist only here, with no slash command." if ai_only else ""
    return (f"**How to use me:** @mention me (or reply to me) and say what you want in plain words, like "
            f"*\"make a role for each team\"* or *\"clear that spammer's messages\"*.\n"
            f"Anything that changes your server shows you the plan first and waits for your **Confirm**.\n\n"
            f"Right now I can run **{n}** command{'s' if n != 1 else ''} for you, based on your permissions in this "
            f"server.{extra} Pick a category below.")
