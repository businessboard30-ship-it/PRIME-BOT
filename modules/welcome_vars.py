# path: modules/welcome_vars.py
"""Welcome / goodbye message placeholders, one implementation for every place a template is filled in.

Placeholders: {member} mention, {name} display name (never pings), {guild} server name, {count} member count,
{count_ordinal} "1,204th", {account_age} "3 years" / "12 days", {date} "9 Oct 2026" (UTC), {rules} the server's rules channel,
{channel:ID} a channel mention. Unknown placeholders are left exactly as typed.

Values are substituted in ONE pass, so a display name like "{count}" is never expanded a second time, and any "@" coming from a
name or server name is defanged so a welcome can never ping @everyone / @here / a role on someone's behalf.
"""
import re
from datetime import datetime, timezone
from typing import Optional

HELP = "{member} {name} {guild} {count} {count_ordinal} {account_age} {date} {rules} {channel:ID}"
NAME_MAX = 80
_TOKEN = re.compile(r"\{(member|name|guild|count|count_ordinal|account_age|date|rules|channel:(\d{15,20}))\}")


def ordinal(n: int) -> str:
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n:,}{suffix}"


def account_age(created: Optional[datetime], now: Optional[datetime] = None) -> str:
    if not isinstance(created, datetime):
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = max(0, (now - created).days)
    if days >= 365:
        y = days // 365
        return f"{y} year{'s' if y != 1 else ''}"
    if days >= 60:
        return f"{days // 30} months"
    return f"{days} day{'s' if days != 1 else ''}"


def safe_text(s) -> str:
    """Display text from a user/server: no pings, no template re-expansion, bounded length."""
    s = str(s or "")[:NAME_MAX]
    return s.replace("@", "@\u200b").replace("{", "{\u200b")


def fill(template: str, *, mention: str, name: str, guild_name: str, count: int,
         created_at: Optional[datetime] = None, rules_channel_id: Optional[int] = None,
         now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    values = {
        "member": mention, "name": safe_text(name), "guild": safe_text(guild_name), "count": str(count),
        "count_ordinal": ordinal(count), "account_age": account_age(created_at, now),
        "date": f"{now.day} {now.strftime('%b %Y')}",
        "rules": f"<#{rules_channel_id}>" if isinstance(rules_channel_id, int) and rules_channel_id else "the rules channel",
    }

    def one(m):
        key = m.group(1)
        return f"<#{m.group(2)}>" if m.group(2) else values[key]
    return _TOKEN.sub(one, template)


def fill_for_member(template: str, member, now: Optional[datetime] = None) -> str:
    """discord.Member / discord.User + its guild -> filled text."""
    g = member.guild
    rules = getattr(g, "rules_channel", None)
    return fill(template, mention=member.mention, name=getattr(member, "display_name", None) or getattr(member, "name", "Someone"),
                guild_name=g.name, count=g.member_count or 0, created_at=getattr(member, "created_at", None),
                rules_channel_id=getattr(rules, "id", None), now=now)
