# PRIME-BOT: Multi-language Plan

**Goal:** every person who uses PRIME-BOT, the bot, the dashboard and the website, can use it in their own language, with translations that are fixed, human-written and reliable.

**Status of this document:** plan only. Nothing here has been built yet. Numbers come from reading the repo (`businessboard30-ship-it/PRIME-BOT`, October 2026).

---

## 0. The rules this plan follows

1. **Static translations only.** Every string a user sees comes from a JSON file that ships with the bot. No AI call, no network call, no waiting. If Groq, Gemini or Railway has a bad day, language still works.
2. **Human-written, human-reviewed.** A native speaker writes or reviews every shipped string. No machine drafts are shipped. (Same as what `i18n.py` already promised in its docstring: "human-written/reviewed, not machine-translated at runtime".)
3. **English is the source and the safety net.** A missing translation shows English, never a raw key like `welcome.title` and never an error.
4. **A language only appears in the picker when its core strings are fully done.** Half-translated languages are worse than none.
5. **CI enforces it.** A build fails if a key is missing from English, or a translation breaks a `{placeholder}`. Nobody has to remember.
6. **Admin-written text is never translated.** If a server owner writes their own welcome message, it is shown exactly as written. Only the bot's own default text is localized.
7. **No new hard-coded English** in user-facing code after Phase 1. Every new feature uses keys from day one.

**Out of scope:** the AI chat feature replying in the member's language. That is the chat model answering a person, not UI text, and it already works through the prompt. It stays as it is.

---

## 1. Where we are today (what I found)

| Area | Finding |
|---|---|
| Languages declared | 10: English, French, Italian, Russian, Portuguese, Spanish, Arabic, German, Swahili, Japanese (`i18n.py`) |
| Static translation files | `locales/en.json` has **323 keys**. Each of the other 9 languages has only **13 keys (about 4%)**. The language picker offers 9 languages that are almost entirely English. |
| The `tr()` system | Only 3 cogs use it (`economy`, `automation`, `automod`, about 110 call sites). It translates **live through Groq** the first time a string is seen and saves it to `locales/ai/<lang>.json`. |
| `locales/ai/` | **Empty in the repo.** On Railway the disk is wiped on every deploy, so after each deploy the bot re-translates live, slowly, using your Groq limits, and sometimes returns English when Groq is busy. This is the unreliable part you are describing. |
| Hard-coded English | 176 cog files, about 73,000 lines. Roughly **1,500 or more** labels, titles, descriptions and messages are typed straight into the code (my count: ~1,470 literal label/title/description/content strings and ~990 literal send calls, with overlap). None of it can be translated. |
| Biggest string-heavy files | `bump` (84), `roast` (75), `_views_cat_features` (72), `ai_store` (70), `_views_join_dm` (61), `setup_channels` (59), `clone_admin` (57), `_views_cat_adopt` (53), `archive` (52), `ads_marketplace` (47) |
| Who gets which language | Language is stored **per user** only (`users.language`, shared with the Telegram bots). There is **no server language**, so public messages (welcome, goodbye, level-up, auto-mod notices, panels) cannot be localized properly. |
| Discord's own language hint | Discord sends every interaction with the user's app language (`interaction.locale`). The bot ignores it, so a French user must find `/language` first. |
| Slash commands | 276 command definitions, none localized, so the slash menu is English for everyone. |
| Web | Dashboard and website (about 310 KB of TSX/HTML plus `api/dash*.py` text) have no translation support. |
| **Good news** | The catch game already does it the right way: namespaced keys (`catch.hub.title`) and a test that checks every key exists and every placeholder is supplied. This plan copies that pattern to the whole bot. |

---

## 2. Target design

### 2.1 One lookup function, keys not sentences

```python
# before
await interaction.response.send_message("You need **500** coins to do that.")

# after
await interaction.response.send_message(
    t("economy.not_enough_coins", lang, amount=500))
```

- Keys are namespaced by feature: `welcome.title`, `leveling.level_up`, `mod.ban.success`.
- `locales/en.json` is the source. Every other language mirrors its keys.
- Named placeholders only: `{amount}`, `{user}`. Never build a sentence from pieces. Translators need the whole sentence to get word order right.
- `t()` fallback chain: language (e.g. `pt-BR`) → base language (`pt`) → `en` → log once and show English. Never the raw key in production.

### 2.2 Plurals

`"{count} members"` breaks in Russian, Arabic and Polish. Use suffixed keys:

```json
"members.count.one":   "{count} member",
"members.count.other": "{count} members"
```

`t_plural("members.count", n, lang, count=n)` picks the form from a small plural-rule table (about 40 lines, fully tested). Needed categories:

| Languages | Forms |
|---|---|
| English, French, Spanish, Portuguese, Italian, German, Swahili | one, other |
| Russian, Ukrainian, Polish | one, few, many, other |
| Arabic | zero, one, two, few, many, other |
| Japanese, Chinese, Korean, Vietnamese, Indonesian, Turkish | other |
| Twi, Ga, Fante, Hausa, Yoruba, Igbo | other (no plural inflection of nouns; native speaker confirms) |

### 2.3 Which language does a message use? (the most important rule)

| Message type | Language used |
|---|---|
| Private replies (ephemeral), DMs, the user's own buttons and wizards | 1) the user's saved language, 2) else their Discord app language (`interaction.locale`, mapped), 3) else the server language, 4) else English |
| **Public messages in a server** (welcome, goodbye, level-up posts, auto-mod notices, ticket panels, giveaways, bump, verification) | The **server language** set by an admin. Else English. |
| Admin-written custom text (custom welcome text, custom embeds) | Shown as written. Not translated. |
| Owner-only panels (`/admin`, clone admin, owner dashboard) | English. Only you see them. |

New data needed:
- A `language` column for servers (migration plus the usual `SCHEMA_VERSION` bump and test pin, same as migrations 052 and 053).
- Suggested starting value for each server: `guild.preferred_locale` when it is a Community server. Admin can change it.
- `/language` becomes two choices: **My language** (anyone) and **Server language** (needs Manage Server).

### 2.4 Discord's language list

Discord's app languages include roughly 30 locales (for example `fr`, `es-ES`, `es-419`, `pt-BR`, `de`, `ru`, `ja`, `ko`, `tr`, `id`, `hi`, `vi`, `pl`, `zh-CN`, `zh-TW`, `uk`). A small mapping table turns each into one of our language codes (`es-419` and `es-ES` both become `es`). Languages Discord does not offer (Twi, Ga, Fante, Hausa, Yoruba, Igbo, Swahili, Pidgin) can only be chosen in `/language`, so the picker must be easy to find and the join DM should offer it.

### 2.5 Slash commands in the user's language

Use discord.py's `app_commands.Translator`, reading `locales/<lang>/commands.json`:
- Translate **descriptions and parameter descriptions** first. Names are optional and risky (names must be lowercase, no spaces, max 32 characters; docs, the AI tool list and support answers all use English names).
- Limits are checked by a test: descriptions at most 100 characters.
- The AI allowlist (`modules/ai_command_allowlist.py`) keeps using English command paths. Translations are display-only, so nothing there breaks.

### 2.6 File layout

```
locales/
  en/                      <- source of truth
    common.json            buttons, errors, yes/no
    welcome.json
    leveling.json
    moderation.json
    ...one file per feature (namespace)
    commands.json          slash command descriptions
  fr/ es/ pt/ ...          same file names as en/
```

One file per feature keeps translator work and PR diffs small and lets a translator do "just moderation". The loader also keeps reading today's flat `locales/<lang>.json` during the migration so nothing breaks. (Decision D1 below.)

### 2.7 Quality tests (run in CI)

1. Every key used in code exists in `en`.
2. Every translation has exactly the same `{placeholders}` as English.
3. Every plural key has the forms its language needs.
4. No translation is empty or identical to English by accident (warning, not failure, because some words are the same).
5. Command descriptions respect Discord's length limits.
6. A generated report `docs/LOCALE_STATUS.md` shows coverage per language and per feature.
7. A lint that fails on a new hard-coded English string in a migrated file.

---

## 3. Languages: the order

**Coverage rule:** a language shows in the picker only when its **core set** is 100% human-reviewed. Everything else in the long tail falls back to English. This is what makes the job possible for one person.

**Core set (about 400 strings):** join DM and setup, `/start` and `/help`, welcome and goodbye defaults, leveling and level-up, verification, moderation replies, tickets, giveaways, errors, buttons, premium pitch. This is what nearly every member and server owner sees.

| Wave | Languages | Why | Who reviews |
|---|---|---|---|
| **1: finish what exists** | French, Spanish, Portuguese (Brazil), Russian, German, Arabic, Italian, Japanese, Swahili | Already in the picker, only 4% done | You for French and Spanish; community translators for the rest |
| **2: your edge in West Africa** | **Twi, Ga, Fante**, Hausa, Yoruba, Igbo, Nigerian Pidgin | Almost no Discord bot offers these. Big audience, and you speak Twi, Ga and Fante yourself, so you can review those three. | You for Twi, Ga, Fante. Native community reviewers for the others. |
| **3: biggest Discord markets** | Turkish, Indonesian, Hindi, Korean, Chinese (Simplified and Traditional), Vietnamese, Polish, Dutch, Ukrainian, Filipino | Large Discord populations | Community translators |

**Right-to-left (Arabic, later Urdu/Persian/Hebrew):** Discord handles direction itself, but embeds with mixed English and emoji can look wrong. Needs a visual check on every core screen, and no translation that ends with English punctuation or code at the line end.

---

## 4. Phases

Effort numbers are rough, for one developer working with Claude Code. Each phase ships as small pull requests, like the rest of the repo.

### Phase 0: Decisions and cleanup (1 to 2 days)
- Settle the decisions in section 7.
- Add a rule to the repo's contributor file (and the Claude Code instructions): *never type user-facing English into code; use `t()` with a key.*
- **Remove the live AI path.** `tr()` and `tr_sync()` become thin wrappers that only read static files (no Groq, no disk writes). Delete `translate_ui_string` usage for UI. Delete the `locales/ai/` idea.

### Phase 1: The engine (2 to 3 days)
- New loader: per-feature files, fallback chain, caching, `t()`, `t_plural()`.
- `resolve_lang(interaction, public=False)` implementing the table in 2.3.
- Server language column and migration; `/language` with My language and Server language.
- Discord locale mapping table.
- The quality tests in 2.7 and `scripts/check_locales.py`, plus the `docs/LOCALE_STATUS.md` generator.
- Move the existing 323 English keys and the catch game into the new layout.

### Phase 2: Move the text out of the code (the big one, about 4 to 6 weeks part-time)
Done cog by cog, one small PR each, in order of how many people see the text:

1. **Everyone sees it first:** `_views_join_dm`, `quickstart`/`/start`, help, `welcome`/`_views_welcome`, `leveling` and level-up, verification, `moderation`.
2. **Server features:** tickets, giveaways, `automod`/`automation`/`economy` (already use `tr()`, convert to keys), `setup_channels`, `role_setup`, premium pitch, store/shop.
3. **Fun features:** cats, catch, cards, roast, bump, music.
4. **Admin/owner only (lowest priority, may stay English):** `admin`, `_views_admin_panel_*`, `clone_admin`, `ai_store`, `ads_marketplace`, `bot_manager`, `media_connect`, `archive`.

Tooling to make this fast: `scripts/extract_strings.py` lists the hard-coded strings in one file with a suggested key, so each PR is mechanical and reviewable. Each PR must pass the lint and add the English keys.

**Definition of done for a cog:** no user-facing English literal left, English keys added, tests green, one screenshot of the feature in a second language.

### Phase 3: Translation workflow (starts as soon as Phase 1 is done, runs alongside Phase 2)
- English files are the only source. Translators never touch code.
- **Start simple:** `scripts/locale_sheet.py export` makes a Google Sheet (key, English, translation, notes). `import` reads it back, runs the placeholder check, and opens a PR. No paid tool needed.
- Later, if volunteers grow: Crowdin or Weblate.
- **Translator program** in your support server: a Translator role, a channel with a style guide and glossary (bot terms like "level", "ticket", "giveaway" translated the same way everywhere), credit on the website, and a reward such as one free month of Premium per approved batch (this ties into the monetization plan).
- Every language file carries a small status block (`reviewed_by`, `date`). The picker only lists languages above the core threshold (`LANG_MIN_CORE_COVERAGE = 100`).
- Beta option: languages between 70% and 99% can show with a "(beta)" label if you decide you want that.

### Phase 4: Slash commands (1 to 2 days)
- `Translator` class, `commands.json` per language.
- Descriptions for the top 40 commands in Wave 1 languages first.
- Test for length limits.

### Phase 5: Dashboard and website (1 to 2 weeks)
- Next.js pages: simple dictionary files or `next-intl`, language from a cookie, then the browser's language, then English; a visible language switcher.
- `api/dash*.py` returns keys or localized strings; the owner dashboard stays English.
- **Terms and Privacy (`api/legal_pages.py`):** keep English as the legal text. Offer translations as a courtesy with a line saying the English version prevails. Have a lawyer confirm; I am not one.

### Phase 6: Rendering (about 1 week, can run in parallel)
Level cards, welcome cards and any image text use Pillow with `DejaVuSans`/`FreeSansBold`. Needed:
- Fonts that cover the scripts: Arabic, CJK, Devanagari, and the Twi/Ga letters (ɛ, ɔ, ŋ). Check the current fonts first; they may already cover the African letters.
- Arabic needs text shaping and right-to-left ordering (`arabic-reshaper` plus `python-bidi`, or Pillow with raqm).
- Visual tests that render one sample card per language.

### Phase 7: Keeping it alive (ongoing)
- Every feature PR adds English keys; CI shows which languages are now behind.
- Monthly: look at `LOCALE_STATUS.md`, send translators the new strings.
- Track: coverage per language, share of interactions in non-English, language-related support questions, Premium conversion by language.

---

## 5. Rough timeline (one developer, part-time)

| Weeks | Work | Result |
|---|---|---|
| 1 | Phase 0 and Phase 1 | Engine live, live-AI path gone, tests and coverage report running |
| 2 to 6 | Phase 2 (priority 1 and 2) alongside Phase 3 | Core screens translated; French and Spanish ready first |
| 4 to 8 | Wave 1 languages reach the core threshold; Phase 4 | Real, complete languages in the picker; localized slash menu |
| 6 to 10 | Wave 2: Twi, Ga, Fante, then others | The West-African languages no other bot has |
| 8 to 12 | Phase 5 and 6 | Dashboard, site and cards localized |
| After | Wave 3, long-tail strings, Phase 7 | Steady state |

The long tail (fun features, admin panels) will take longest and can stay English for a long time without hurting anyone.

---

## 6. Risks and how this plan handles them

| Risk | Handling |
|---|---|
| Too many strings for one person to translate | Core-set rule: a language needs about 400 strings, not 2,000. Long tail falls back to English. |
| Bad translations hurting the bot's reputation | Human review only; status block per file; unreviewed languages never listed. |
| A translation breaks a placeholder and crashes a command | CI placeholder test; `t()` never raises (falls back to English). |
| Hard-coded English creeps back in | Lint in CI plus the contributor rule. |
| Mixed-language screens (some translated, some not) | Core set is translated together; the status report shows exactly what is left. |
| Slow or large locale loading | Files cached in memory after first load; one file per feature. |
| Telegram bots share `users.language` | Keep the same column and same language codes. New server language is Discord-only. |
| Dashboard and bot drift apart | One English glossary file used by both. |

---

## 7. Decisions needed from you (my recommendation in bold)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | File layout | Keep one flat file per language / one folder per language with a file per feature | **Folder per language, file per feature** |
| D2 | Public messages | Use the server language / use each member's language | **Server language** (members' languages can't be used in a shared channel) |
| D3 | Starting server language | English for all / `guild.preferred_locale` suggestion | **Suggest `preferred_locale`, admin can change** |
| D4 | Translate command names? | Descriptions only / names too | **Descriptions only at first** |
| D5 | Picker threshold | 100% of core / allow beta labels | **100% of core, beta only if you decide to** |
| D6 | Translator tool | Google Sheet scripts / Crowdin | **Sheet scripts first** |
| D7 | Translator reward | None / free Premium time | **Free Premium time per approved batch** |
| D8 | First language wave | Wave 1 and 2 together / Wave 1 first | **French and Spanish first, then Twi, Ga, Fante in parallel with the rest of Wave 1** |

---

## Appendix A: Key naming rules

- `feature.screen.element`, all lowercase, dots between parts: `welcome.setup.title`.
- Buttons end in `.button`, errors in `.error`, descriptions in `.description`.
- Shared words (Yes, No, Cancel, Confirm, Back) live in `common.*` and are reused.
- Never reuse one key for two different meanings, even if the English is the same.
- Keep emoji in the string only when it is part of the meaning; otherwise add it in code so translators can't remove it.
- Markdown (`**bold**`, `` `code` ``) stays in the string; translators are told to keep it.

## Appendix B: Discord locale to our code (starting table)

| Discord | Ours | Discord | Ours |
|---|---|---|---|
| en-US, en-GB | en | ko | ko |
| fr | fr | zh-CN | zh-CN |
| es-ES, es-419 | es | zh-TW | zh-TW |
| pt-BR | pt | tr | tr |
| de | de | id | id |
| ru | ru | hi | hi |
| it | it | vi | vi |
| ja | ja | pl | pl |
| uk | uk | nl | nl |

Languages Discord does not offer (`ak` Twi, `gaa` Ga, `fat` Fante, `ha`, `yo`, `ig`, `sw`, `pcm`) are picker-only.

## Appendix C: Pull request checklist (every cog)

- [ ] No user-facing English literal left in the files touched
- [ ] English keys added with correct placeholders
- [ ] `scripts/check_locales.py` passes
- [ ] Plural keys used for counts
- [ ] Public messages use the server language; private ones use the user's
- [ ] Screenshot of the feature in one other language
- [ ] `LOCALE_STATUS.md` regenerated

## Appendix D: Contributor rule to add to the repo

> User-facing text never goes in code as a literal. Add the English string to `locales/en/<feature>.json` under a namespaced key and call `t("<key>", lang, **vars)`. Counts use `t_plural`. Admin-written custom text is shown as written and never translated. Do not use any AI service to translate or generate UI strings.
