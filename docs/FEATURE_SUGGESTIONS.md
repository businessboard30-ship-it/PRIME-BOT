# Feature & Maintenance Suggestions

Review snapshot of PRIME-BOT (main @ 84b12200). Suggestions only, no code changes.

## New feature ideas

1. **Seasonal events & battle pass** - time-boxed events (limited creatures/cards, event quests, a free + premium reward track) layered on the catch game, cards and heist systems. Reuses existing quests, shop and card-reward modules and gives a reason to return daily.
2. **Server-vs-server leaderboards** - weekly guild competitions (XP, catches, counting streaks) with rewards. Fits the existing guild activity, counting and leaderboard code.
3. **Mod-case dashboard & appeals** - numbered cases for warns/mutes/bans, a member-facing appeal flow (modal -> staff ticket), and an exportable case history. Complements automod, quarantine and tickets.
4. **Auto-moderation analytics digest** - weekly summary to the log channel: top rule triggers, raid attempts blocked, false-positive candidates. Builds on the analytics and log-feed modules.
5. **Scheduled & recurring announcements with templates** - extend schedule/autopost with cron-style repeats, per-channel timezone and embed templates.
6. **Per-guild AI persona & usage caps** - let admins pick AI tone, allowed channels and a daily token budget; surface usage in the dashboard. Protects Groq quota and gives premium upsell room.
7. **Player-to-player trading hub for creatures** - card_trade exists; add creature trades with escrow confirmation, trade history and anti-scam cooldowns (reuse scam_risk).
8. **Public status page / uptime widget** - expose bot latency, shard health and clone-health data on the website.
9. **Onboarding quest for new members** - a short guided checklist (read rules, pick roles, say hi, first level-up) with a reward, tied to join_gate, self-roles and welcome.
10. **Referral milestone badges** - visible profile badges for referral tiers, shown on level and profile cards.

## Maintenance / quality suggestions

- **Repo weight**: `.git` is ~150 MB, mostly 1-3 MB PNGs at the repo root (tier*, clan_*, godhood_*, welcome_bg_*). Move them under `assets/` (or object storage/CDN) and consider `git lfs`; add `.gitattributes`. Root also holds screenshots and 10 MB of fonts.
- **`database.py` is ~1.1 MB / ~20k lines**: split by domain (leveling, economy, catch, ads, clones) into a `database/` package to cut merge conflicts and import time.
- **Stale docs**: `CHANGELOG.md` still describes the old Telegram "Anime Bot" (2025); `sql/schema.sql` is documented as stale. Refresh or archive them.
- **CI breadth**: the main lint step only selects crash-class rules (E9, F63, F7, F82). Consider adding `ruff format --check` and a broader rule set incrementally, plus a dependency audit (`pip-audit`) and Dependabot for `requirements.txt`/pnpm.
- **Pin dependencies**: `requirements.txt` uses `>=` everywhere; add a lock file (pip-tools/uv) so deploys are reproducible.
- **Secrets hygiene**: a pattern scan of the working tree found no committed tokens. Keep it that way with `gitleaks` in CI, and rotate any token ever pasted into chat or issues.
- **Branch protection on `main`**: require the CI check to pass before merge, so auto-merge flows can't ship a broken build to the Railway deploy.
