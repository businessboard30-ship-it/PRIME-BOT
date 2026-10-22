"""The AI's "please choose" panel (Components V2). Built from a spec cleaned by modules.ai_choice.sanitize.

Layout: one Container with a title, optional intro and the questions. A single question with up to 5 options is drawn
as rows with a Choose button each (one click answers). Anything bigger uses select menus plus a Submit button that
stays disabled until every question has an answer. Cancel is always there. Only the person who asked can use it, it
answers once, and what it hands back is just the pick -- see modules/ai_choice.py for why that is safe.
"""
import discord

from modules import ai_choice

_FAST_MAX = 5


class _ChooseButton(discord.ui.Button):
    def __init__(self, panel: "AIChoiceView", qid: str, index: int):
        super().__init__(label="Choose", style=discord.ButtonStyle.primary)
        self._panel, self._qid, self._index = panel, qid, index

    async def callback(self, interaction: discord.Interaction):
        self._panel.answers[self._qid] = [self._index]
        await self._panel.finish(interaction)


class _QuestionSelect(discord.ui.Select):
    def __init__(self, panel: "AIChoiceView", q: dict):
        chosen = set(panel.answers.get(q["id"], []))
        options = [discord.SelectOption(label=o["label"], value=str(i), description=o["description"] or None,
                                        default=i in chosen) for i, o in enumerate(q["options"])]
        super().__init__(placeholder="Pick one" if not q["multiple"] else "Pick one or more", options=options,
                         min_values=1, max_values=len(options) if q["multiple"] else 1)
        self._panel, self._qid = panel, q["id"]

    async def callback(self, interaction: discord.Interaction):
        self._panel.answers[self._qid] = [int(v) for v in self.values]
        await interaction.response.edit_message(view=self._panel.render())


class _SubmitButton(discord.ui.Button):
    def __init__(self, panel: "AIChoiceView", ready: bool):
        super().__init__(label="Submit", style=discord.ButtonStyle.success, disabled=not ready)
        self._panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self._panel.finish(interaction)


class _CancelButton(discord.ui.Button):
    def __init__(self, panel: "AIChoiceView"):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)
        self._panel = panel

    async def callback(self, interaction: discord.Interaction):
        self._panel._done = True
        self._panel.clear_items()
        c = discord.ui.Container(accent_colour=discord.Color.light_grey())
        c.add_item(discord.ui.TextDisplay("Cancelled -- nothing was done."))
        self._panel.add_item(c)
        await interaction.response.edit_message(view=self._panel)
        self._panel.stop()


class AIChoiceView(discord.ui.LayoutView):
    def __init__(self, spec: dict, asker_id: int, on_submit, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.spec, self.asker_id, self.on_submit = spec, asker_id, on_submit
        self.answers = {}
        self._done = False
        self.render()

    @property
    def _fast(self) -> bool:
        qs = self.spec["questions"]
        return len(qs) == 1 and not qs[0]["multiple"] and len(qs[0]["options"]) <= _FAST_MAX

    def render(self) -> "AIChoiceView":
        self.clear_items()
        c = discord.ui.Container(accent_colour=discord.Color.blurple())
        c.add_item(discord.ui.TextDisplay(f"## 🤔 {self.spec['title']}"))
        if self.spec["intro"]:
            c.add_item(discord.ui.TextDisplay(self.spec["intro"]))
        for q in self.spec["questions"]:
            c.add_item(discord.ui.Separator())
            c.add_item(discord.ui.TextDisplay(f"**{q['prompt']}**"))
            if self._fast:
                for i, o in enumerate(q["options"]):
                    text = f"**{o['label']}**" + (f"\n{o['description']}" if o["description"] else "")
                    c.add_item(discord.ui.Section(discord.ui.TextDisplay(text), accessory=_ChooseButton(self, q["id"], i)))
            else:
                c.add_item(discord.ui.ActionRow(_QuestionSelect(self, q)))
        c.add_item(discord.ui.Separator())
        ready = all(self.answers.get(q["id"]) for q in self.spec["questions"])
        row = [_CancelButton(self)] if self._fast else [_SubmitButton(self, ready), _CancelButton(self)]
        c.add_item(discord.ui.ActionRow(*row))
        self.add_item(c)
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.asker_id:
            await interaction.response.send_message("This question isn't for you -- ask me yourself.", ephemeral=True)
            return False
        if self._done:
            await interaction.response.send_message("Already answered.", ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction) -> None:
        self._done = True
        self.clear_items()
        c = discord.ui.Container(accent_colour=discord.Color.green())
        c.add_item(discord.ui.TextDisplay("✅ **You chose**\n" + "\n".join(ai_choice.picked_labels(self.spec, self.answers))))
        self.add_item(c)
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.on_submit(interaction, self.answers)

    async def on_timeout(self) -> None:
        self._done = True
        self.stop()
