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
See v1 below; remaining columns TODO: per-feature matrix (command, module, getter/setter, tables, dashboard status, premium, perms, tests, remaining work, verification) still to be built by walking every cog. Candidates not yet in the schema: language settings, applications, polls, clans, music, cat/catch games, anime feed, AI settings, scam shield, bot profile.

## Coverage metrics
Configuration coverage: not yet measured.
Routine-action coverage: not yet measured.

## Coverage matrix v1 (first pass)
`Complete*` = schema module exists AND a dashboard test file exists; NOT yet verified field-by-field against the Discord flow. Command scan is regex-based and likely undercounts (36 cogs, 56 commands found, 32 schema getters/setters).

| Feature | Discord entry | Dashboard source | Status | Notes / remaining |
|---|---|---|---|---|
| Welcome | /welcome (views) | welcome | Complete* | Designer + test action |
| Verification | /setupverification | verification | Partial | Panel post action exists; verify wizard parity untested |
| Anti-raid | /antiraid | antiraid | Complete* | Raid ops approve/kick/ban exist |
| Auto-moderation | automod views | automod | Complete* | Word list editor to confirm |
| Server logs | /modlog | modlog | Complete* |  |
| Honeypot | /honeypot | honeypot | Complete* |  |
| Join gate | join gate views | joingate | Complete* |  |
| Leveling | /rank /levelcard /leaderboard | leveling,voicexp,starboard | Partial | Level roles, multipliers, exclusions to audit |
| Tickets | ticket views | tickets | Complete* | History + panel action; transcripts to audit |
| Suggestions | /suggest | suggestions | Complete* |  |
| Custom role | /customrole | customrole | Complete* |  |
| Invites | invite views | invites | Complete* |  |
| Economy | economy wizard | economy | Complete* | Range validation to verify |
| Auto-post/announce | /announce /cancelannouncement | autopost + announcements API | Partial | Edit/pause parity to verify |
| Scheduling | /serversetup | schedules API | Partial |  |
| Bump | /bumpsetup | bumpnet | Partial |  |
| Moderation cases | /warn /kick /ban /timeout | moderation API | Partial | Warning management to verify |
| Giveaways | giveaway wizard | giveaways API | Partial | Reroll/cancel to verify |
| Analytics | /serveranalytics | analytics API | Partial | Rollup history to verify |
| Language | /language | - | Missing | Not in schema |
| Applications | /application | - | Missing | Not in schema |
| Scam Shield | /scamshield | - | Missing | Not in schema |
| Birthdays | /birthday | - | Missing | Not in schema |
| Bot profile | /botprofile | - | Missing | Not in schema |
| Cat/Catch/Heist games | /cat /catch /heist | - | Missing | Needs web-appropriateness review |
| Music Pro | /activate-pro | - | Missing | Premium activation |
| Media storage | /set-storage-channel | - | Missing | Not in schema |
| Premium | /premium | billing API | Partial | Entitlement lifecycle tests to verify |
| Clones | /myclones /removeclone | clone access API | Partial | Isolation tests exist; compatibility registry to audit |

Still missing from matrix columns: DB tables, Premium flag, required Discord perms, per-feature test names, final verification method.
