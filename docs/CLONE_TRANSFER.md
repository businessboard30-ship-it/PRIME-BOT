# Replacing a clone (moving a server's data from clone A to clone B)

Per-server data is keyed by (server, clone), so a replacement clone starts empty. `/admin clonedata transfer` moves it.

## Steps
1. Invite clone B to the servers. (Until you approve, nothing is moved: B sees empty settings.)
2. **Shut clone A down** (set it inactive / stop it). **Blocker:** while A is online (heartbeat within 20 min) the command refuses, and so does
   the automatic step. Two live bots in one server would be a conflict, not a replacement.
3. Run `/admin clonedata transfer source_clone_id:A target_clone_id:B`. It shows how many servers/rows/tables will move and what is never moved,
   then asks you to **Confirm**. The blocker is re-checked at the moment you press Confirm.
4. Confirming (a) moves every server where B is present, and (b) approves the pair A -> B **once**: servers B joins later are carried over
   automatically. `/admin clonedata status` lists approvals, servers waiting for approval (data found under another clone, nothing moved) and
   recent runs, and can revoke an approval.
5. In each server, an admin runs `/reactionrole repost` on B: Discord buttons belong to the bot that posted them, so old panels are dead and another
   bot cannot edit them. The command posts a copy from B (same embed and roles) and points the saved roles at it. Other panels (tickets,
   verification, giveaways...) must be posted again from B; the confirmation message lists which ones apply.

## What moves / what never moves
Moves: every table with `guild_id` + `clone_id` (discovered from the live database, so new features are included automatically) except:
billing/entitlements (premium groups, subscriptions, pro guilds, boost payments, verification purchases), the per-bot server registry, login tokens, invite cache, public listings/bump
network, terms acceptance, retired features (heist) and card trades. See `DENY_EXACT` / `DENY_PREFIX` in `modules/clone_transfer.py`.
A row B already has wins: the old row is skipped, never overwritten. One server = one transaction with a savepoint per table, so one broken table
is reported and cannot undo or block the rest. Every run is written to `clone_transfer_log`.

## Behaviour change
Previously, when an inactive clone's data existed for a server and a new clone joined, 9 tables were moved silently. That unconditional step is
replaced by this gated one (confirmation + live blocker + all tables). Data of a clone left in a server is still purged 14 days after it left,
so confirm before that.

## Not covered
- Per-person data keyed by (user, clone) that is not tied to a server (e.g. some wallets) stays with the old clone.
- Moving to/from the main bot (clone_id NULL) is not supported.
- Tested against a real Postgres with representative tables and constraints, not against the full production schema.
