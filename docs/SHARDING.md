# Sharding guide (PRIME-BOT)

## The short version

- PRIME-BOT now runs as **one process** that manages its own shards (`AutoShardedBot`).
- It asks Discord how many shards it needs every time it starts. **You do not calculate or set anything.**
- The start command is unchanged (`sh start.sh`, role `bot`). No Railway setting needs to change.
- Check health in Discord with `/admin shards` (bot owners only).

## Why 2,500 guilds, and why prepare now

Discord **requires** sharding once a bot is in **2,500 guilds** (servers). 1,000 is not the mandatory number.
Discord's recommended shard count grows at roughly one shard per 1,000 guilds, so the bot moves to more shards
by itself as it grows. We switched early so the change is already tested and running well before 2,500, instead
of during an emergency. Below 1,000 guilds you will see 1 shard, and the bot behaves as it did before.

## How the automatic count works

1. On start, discord.py calls Discord's `GET /gateway/bot` and uses the shard count Discord recommends.
2. Every shard runs inside the same process, so all commands, buttons, caches and background jobs work the
   same as before.
3. Discord says the recommended count can change over time, so it is read at every start and never saved. Every shard connection also uses one of Discord's daily session starts (1,000 per day for standard bots), so avoid restart loops.
4. The shard count is re-read on every restart. After a big jump in servers, restart the bot once so the new
   recommendation is used. `/admin shards` warns you when a shard is getting crowded.

### Optional setting

| Name | Required | Default | Purpose |
| --- | --- | --- | --- |
| `PRIME_SHARD_COUNT` | No | automatic | Force a fixed count (1 to 1000) for the **main bot only**. Leave it unset for normal use. Use only if Discord or an operator tells you to. A bad value stops startup with a clear message. Clones always use automatic. |

## How to deploy

Same as always: merge the pull request into `main`, and Railway deploys. After deploy, check the bot's logs
(see below) and run `/admin shards`.

Do the first deploy at a quiet time and watch the first few minutes of logs.

## How to check shard health

- In Discord: `/admin shards` shows the shard count, guilds per shard, latency, and any shard that is down.
  Only bot owners can run it, and the reply is private. One down shard shows as a warning, never as "all good".
- In the logs, look for lines starting with `[shards]`:
  - `[startup] shard count setting: ...` once at start
  - `[shards] shard N connected` / `ready (X guild(s))` while starting
  - `[shards] shard N disconnected; discord.py will reconnect it` (a warning). A few of these are normal; discord.py
    reconnects on its own. Many in a row for the same shard is not.
  - `[shards] X shard(s), Y guild(s) total` once everything is ready

## If a shard won't start

1. Run `/admin shards` or read the `[shards]` log lines to find which shard is stuck.
2. If you see `Login rate-limited` or `HTTP 429`: the bot backs off by itself (30 seconds, doubling to 10 minutes).
   Don't redeploy repeatedly; each restart makes the block last longer.
3. If startup says `PRIME_SHARD_COUNT must be ...`: delete that variable or set it to `auto`.
4. If `Slash command sync failed` appears: the bot still starts. Restart later and it will retry.
5. Never run a second copy of the bot with the same token (for example on your own computer while Railway is
   running). Two copies cause duplicated messages. On start the bot logs `instance=... pid=...`; two different
   ids for the same bot around the same time means two copies are running.

## Slash commands

Commands are synced once per start (one process, not once per shard) and only through the existing code. If
Discord's API fails at that moment, the bot logs it and continues with the commands already registered.

## Scheduled jobs: why nothing runs twice

The bot has about 78 background loops (premium and trial expiry, birthdays, giveaways, bump workers, stats
posts, cleanup and more). They are created once when the process starts. Because all shards live in that one
process, **each job has exactly one owner and runs once**. Sharding does not duplicate them.

The same is true for the database: all shards share the one pool and the one set of tables.

This protection depends on **running exactly one main-bot process**. Do not scale the Railway bot service to
more than one replica.

## When one process is no longer enough

Plan for this when any of these is true: memory or CPU on the Railway bot service stays high, `/admin shards`
shows rising latency on most shards, or the bot has grown far beyond a few thousand guilds. There is no exact number; watch memory and latency. At that point:

1. Move to several worker processes, each handling a range of shard IDs.
2. Before doing so, the global jobs listed above must get a database-backed lease (one owner at a time) so two
   processes never both send birthday messages or expire the same premium. This is **not built yet** because it
   is not needed while there is one process.
3. Discord requires large bots (150,000+ guilds) to use shard counts that are multiples of a set number
   (the "large bot sharding" rules). Check Discord's current developer docs then.

## What was verified, and what was not

Unit tests cover the configuration rules, the health report, the bot class and the owner-only command.
They do **not** talk to Discord. Real shard connect, resume and reconnect behavior, and a live `/admin shards`
check, must be confirmed on your staging or live bot after deploy.
