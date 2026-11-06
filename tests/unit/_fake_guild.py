"""A small fake Discord guild for snapshot/restore tests. Models ids, per-category positions, role positions,
overwrites, and the way Discord un-parents channels when their category is deleted."""
import itertools
from types import SimpleNamespace

import discord

_ids = itertools.count(9000)


class FakeRole:
    def __init__(self, guild, name, color=0, permissions=0, hoist=False, mentionable=False, managed=False, default=False, id=None, position=1):
        self.guild, self.id, self.name = guild, id or next(_ids), name
        self.color, self.permissions = discord.Colour(int(getattr(color, "value", color))), discord.Permissions(int(getattr(permissions, "value", permissions)))
        self.hoist, self.mentionable, self.managed, self._default, self.position = hoist, mentionable, managed, default, position

    def is_default(self):
        return self._default

    async def edit(self, *, reason=None, **kw):
        self.guild.calls.append(("role.edit", self.name, sorted(kw)))
        self.guild._maybe_fail("role.edit")
        if "colour" in kw:
            self.color = kw.pop("colour")
        for k, v in kw.items():
            setattr(self, k, v)

    async def delete(self, *, reason=None):
        self.guild.calls.append(("role.delete", self.name))
        self.guild._maybe_fail("role.delete")
        self.guild.roles.remove(self)
        for ch in self.guild.channels:
            ch.overwrites.pop(self, None)


class FakeMember:
    def __init__(self, id):
        self.id = id
        self.guild_permissions = discord.Permissions.none()


class FakeChannel:
    def __init__(self, guild, name, ctype, category=None, topic=None, nsfw=False, slowmode_delay=0, bitrate=None, user_limit=None, id=None):
        self.guild, self.id, self.name = guild, id or next(_ids), name
        self.type = SimpleNamespace(name=ctype)
        self.category_id = category.id if category else None
        self.topic, self.nsfw, self.slowmode_delay, self.bitrate, self.user_limit = topic, nsfw, slowmode_delay, bitrate, user_limit
        self.overwrites = {}
        self.position = 0
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)

    def _group(self):
        if self.type.name == "category":
            return [c for c in self.guild.channels if c.type.name == "category"]
        return [c for c in self.guild.channels if c.type.name != "category" and c.category_id == self.category_id]

    async def edit(self, *, reason=None, **kw):
        self.guild.calls.append(("channel.edit", self.name, sorted(kw)))
        self.guild._maybe_fail("channel.edit")
        if "category" in kw:
            cat = kw.pop("category")
            self.category_id = cat.id if cat else None
            self.position = len(self._group())
        pos = kw.pop("position", None)
        for k, v in kw.items():
            setattr(self, k, v)
        if pos is not None:
            grp = sorted([c for c in self._group() if c is not self], key=lambda c: c.position)
            grp.insert(min(pos, len(grp)), self)
            for i, c in enumerate(grp):
                c.position = i

    async def delete(self, *, reason=None):
        self.guild.calls.append(("channel.delete", self.name))
        self.guild._maybe_fail("channel.delete")
        self.guild.channels.remove(self)
        if self.type.name == "category":
            for c in self.guild.channels:
                if c.category_id == self.id:
                    c.category_id = None
        for i, c in enumerate(sorted(self._group(), key=lambda c: c.position)):
            c.position = i


class FakeGuild:
    def __init__(self, id=1536047270321979514):
        self.id = id
        self.calls, self.fail = [], {}
        self.roles = [FakeRole(self, "@everyone", default=True, id=id, position=0, permissions=1024)]
        self.channels, self.members = [], {}
        self.verification_level = discord.VerificationLevel.low
        self.system_channel = self.rules_channel = None

    def _maybe_fail(self, what):
        n = self.fail.get(what)
        if n:
            self.fail[what] = n - 1 if n > 0 else n
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    @property
    def categories(self):
        return [c for c in self.channels if c.type.name == "category"]

    def get_role(self, i):
        return next((r for r in self.roles if r.id == i), None)

    def get_channel(self, i):
        return next((c for c in self.channels if c.id == i), None)

    def get_member(self, i):
        return self.members.get(i)

    # builders used by tests (no API accounting)
    def add_role(self, name, **kw):
        for r in self.roles:
            if not r.is_default():
                r.position += 1
        r = FakeRole(self, name, **kw)
        self.roles.append(r)
        return r

    def add_channel(self, name, ctype="text", category=None, **kw):
        c = FakeChannel(self, name, ctype, category, **kw)
        c.position = len(c._group())
        self.channels.append(c)
        return c

    # API surface used by restore
    async def create_role(self, *, name, colour, permissions, hoist, mentionable, reason=None):
        self.calls.append(("create_role", name))
        self._maybe_fail("create_role")
        r = self.add_role(name, color=colour, permissions=permissions, hoist=hoist, mentionable=mentionable)
        return r

    async def create_category(self, name, *, reason=None):
        self.calls.append(("create_category", name))
        self._maybe_fail("create_category")
        return self.add_channel(name, "category")

    async def _create(self, kind, name, category=None, **kw):
        self.calls.append((f"create_{kind}", name))
        self._maybe_fail("create_channel")
        kw.pop("reason", None)
        news = kw.pop("news", False)
        kw = {("slowmode_delay" if k == "slowmode_delay" else k): v for k, v in kw.items()}
        return self.add_channel(name, "news" if news else kw.pop("_t", kind), category, **kw)

    async def create_text_channel(self, name, **kw):
        return await self._create("text", name, **kw)

    async def create_voice_channel(self, name, **kw):
        return await self._create("voice", name, _t="voice", **kw)

    async def create_stage_channel(self, name, **kw):
        return await self._create("stage_voice", name, _t="stage_voice", **kw)

    async def create_forum(self, name, **kw):
        return await self._create("forum", name, _t="forum", **kw)

    async def edit_role_positions(self, positions, *, reason=None):
        self.calls.append(("edit_role_positions", len(positions)))
        self._maybe_fail("edit_role_positions")
        for r, p in positions.items():
            r.position = p

    async def edit(self, *, reason=None, **kw):
        self.calls.append(("guild.edit", sorted(kw)))
        for k, v in kw.items():
            setattr(self, k, v)
