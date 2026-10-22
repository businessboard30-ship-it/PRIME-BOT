# Dashboard 2.0 - Phase 0 Audit (in progress)

Status: PARTIAL. Only what was verified from the repository is listed. Nothing here is a coverage claim.

## Verified so far
- Frontend: dashboard/ (index.html, assets/: bg.js config.js dash.css dash.js owner.js theme.js tiers/)
- API: api/dash.py (~1732 lines), request router `_route(method, query, headers, body)`; member/dev/messaging route tables merged via dash_member, dash_dev, dash_msg ROUTES
- Schema: utils/dash_schema.py (~859 lines), 17 schema modules, ~93 `F(...)` field definitions, categories Onboarding/Security/Community/Fun
- Bot actions already wired: verify_panel, ticket_panel, welcome_test
- Existing feature APIs in dash_schema: schedules, announcements, tickets, moderation, analytics, giveaways, raid, audit, settings export/import, dropbox
- Cogs: 197 files under discord_bot/cogs
- Tests: ~55 files matching tests/unit/test_dash_*.py

## Schema modules (editable settings today)
| Module | Category |
|---|---|
| welcome | Onboarding |
| verification | Onboarding |
| antiraid | Security |
| automod | Security |
| modlog | Security |
| honeypot | Security |
| joingate | Security |
| leveling | Community |
| voicexp | Community |
| starboard | Community |
| tickets | Community |
| suggestions | Community |
| customrole | Community |
| invites | Community |
| autopost | Community |
| bumpnet | Community |
| economy | Fun |

## Coverage matrix
TODO: per-feature matrix (command, module, getter/setter, tables, dashboard status, premium, perms, tests, remaining work, verification) still to be built by walking every cog. Candidates not yet in the schema: language settings, applications, polls, clans, music, cat/catch games, anime feed, AI settings, scam shield, bot profile.

## Coverage metrics
Configuration coverage: not yet measured.
Routine-action coverage: not yet measured.
