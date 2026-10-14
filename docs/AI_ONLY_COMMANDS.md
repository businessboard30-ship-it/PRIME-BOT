# AI-only commands

Some operations exist **only** for the AI to run. They are not slash commands that people can see or type in Discord.

## Why (the owner's decision)

These operations are only worth having when someone can describe the job in plain words:

- "make a channel for every day of the week, in a fancy font" -> `setup bulk-create`
- "remove the old event channels" -> `setup bulk-delete`
- "clear out that spammer's messages" -> `messages user-purge`
- "make a role for each team" -> `role bulk-create`
- "remove the old event roles" -> `role bulk-delete`
- "add a gold VIP role" -> `role create`

Typed by hand as slash commands they are clunky and rarely used, and every slash command costs a slot of Discord's hard
**100-command cap**, which this project is actively trying to free up (see the `/tools` and `/economy` merges).
So they are AI-callable operations and nothing else.

## What that means for anyone (human or AI) editing this code

- **Do not re-expose them to Discord**, and do not "restore" them as slash commands, without asking the owner.
- **Do not delete the `@group.command(...)` definitions.** The AI builds its tool schema, parameters and permission
  checks from those command objects, and the cog's `AI_CONFIRMED_HANDLERS` / `AI_TOOL_PARAMS` / `AI_CONFIRM_PROMPTS`
  hang off them.
- They do **not** count toward the 100-command budget, because they are never registered with Discord.
- They still go through the full guard: allowlist, the asker's real permissions (`needs_perms`), the daily AI cap, and an
  explicit Confirm click before anything runs.

## How it works

1. A spec in `modules/ai_command_allowlist.py` is marked `ai_only=True` (see the long note on `AICommandSpec.ai_only`).
2. At startup, `discord_bot/bot.py` calls `detach_ai_only_commands(bot)` after all cogs load and **before** the slash
   sync. That moves the command objects out of `bot.tree` into a private registry in `modules/ai_command_guard.py`.
3. `_resolve_command` checks that registry first, so the AI still finds them (with their cog binding and parent group).
4. `tests/unit/test_ai_only_commands.py` proves they are absent from the payload sent to Discord but still resolvable.

## Adding another AI-only operation

Define it like any other subcommand, add its `_sub(..., ai_only=True)` spec, and add a note on the command explaining why
it is AI-only. Update `AI_ONLY_PATHS` in the test.
