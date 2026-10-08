# User / Developer / Messaging dashboard: build status and test handoff

Written 8 Oct 2026. Plan: `docs/USER_AND_DEVELOPER_DASHBOARD_PLAN.md` (on branch `docs/user-dev-plan`, not merged yet). Read it first.
Workflow agreed with the owner: one Claude **builds** a phase and opens a PR; another Claude **runs the tests phase by phase and fixes bugs**.

## Credentials
No token is in the repo, `.git/config` or this doc. Ask the owner for a fine-grained token (Contents + Pull requests, PRIME-BOT only) and pass it inline on single git/curl commands. Always open a PR (owner preference). The classic `ghp_` token pasted in an earlier chat must be revoked.

## Phase status
| Phase | Plan part | Branch / PR | Schema | State |
|---|---|---|---|---|
| 0a | E light theme | `feat/dash-light-theme`, PR #132 | none | built, unit tests only |
| 0b | B0 member shell, `user_entitlements`, `dash_web_users` | `feat/dash-member-b0` | 61 -> **62** | built (this PR), read-only |
| 0c | B0 user checkout + webhook grants + renewals | NOT BUILT | none or 62 -> 63 | next; needs real gateway tests |
| 1+ | A1 to A7 server panel, B1 to B4, D messaging, C developer mode | NOT BUILT | see plan | |

Merge order: #132 (independent), then this PR. This PR bumps `SCHEMA_VERSION` 61 -> 62: after deploy check Railway logs for `[db init] schema_version '61' != '62'` and the DDL pass.

## What 0b contains
- `modules/entitlements.py`: pure access rules (active needs a future expiry; cancelled keeps access to period end; past_due 3-day grace; unknown/missing fails closed; card plan never unlocks Developer mode; export grace 7 days).
- `database.py`: tables `user_entitlements(user_id, product, ...)` PK (user_id, product) and `dash_web_users`; methods `dash_web_user_touch/exists`, `entitlements_list`, `entitlement_upsert` (**webhook only**, a test asserts no route imports it).
- `api/dash_member.py` + `dash._require_member` + `dash._member_routes`: `GET ?action=member_status` (any signed-in user, keyed to the session id, rate limited 60/min). `me` now returns `member: true`.
- Sign-in writes `dash_web_users` (best effort, never blocks login; not on step-up).
- Front end: `#/me` "My account" page + header link.
- Tests: `tests/unit/test_dash_member.py` (17). SQL was run on a real Postgres 16 (idempotent DDL, touch keeps `first_seen`, upsert keeps `subscription_id` when None).

## Tester Claude: run these, in this order
1. `pip install -r requirements.txt pytest --break-system-packages`
2. `python3 -m pytest tests/unit/test_dash_*.py tests/unit/test_schema_version_guard.py -q` (fast), then the full `python3 -m pytest tests/unit -q` (~2.5 min).
3. Per branch, fix failures in a commit on that branch (do not squash other people's work), re-run, push, comment on the PR.
4. Theme (#132): in a real browser (Playwright if you can install it) open every page in Light, Dark, Auto: login, server list, a server panel, owner area, tier gallery, inbox, toasts, save bar. Look for unreadable text and dark remnants. Confirm no dark flash on reload in Light.
5. Member (0b): sign in, open `#/me`, confirm the plan list loads and an empty state shows. Check `dash_web_users` gets a row after sign-in. Manually insert a `user_entitlements` row and confirm states (active, cancelled, past_due, expired).

## Cannot be done by a tester Claude alone (human gates from the plan)
- A real small purchase on **Gumroad and Paystack** before any paid phase ships (B4 card plan, Developer plan), including renewal, failed renewal, cancel at period end, duplicate webhook.
- A real-browser run with **two Discord accounts** before member messaging (Phase 5) ships.
- Real Discord sign-in and step-up round trip (still unverified from earlier owner-dashboard work).

## Gotchas (from the earlier handoff, still true)
- Bump `SCHEMA_VERSION` whenever `_create_tables` changes and update `PINNED_VERSION`/`PINNED_HASH` in `test_schema_version_guard.py` in the same PR.
- Body/query field named `clone_id` or `action` is reserved by the router.
- `textContent` only for server data; CSP is `script-src 'self'; style-src 'self'`.
- Member routes never accept a user id from the client; every query keys off `sess.user.id`.
