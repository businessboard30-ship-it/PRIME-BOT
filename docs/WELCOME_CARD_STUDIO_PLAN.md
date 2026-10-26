# PRIME-BOT: Welcome Card Studio plan

Goal: grow the welcome-background feature (your best seller) into a full "studio": 10 images per server with
auto-change for each newcomer, framing, positioning, fonts, effects, borders, saved designs, and DM options,
while the wizard stays **simple**. Complexity lives behind "More controls"; the first screen is always 3 taps.

Status: PLAN ONLY. Nothing has been built. Based on a read of the repo at `main` (after the sharding PR work).

### Decisions locked (v2 of this plan)

1. **Gated:** the studio features are part of the **Premium subscription and the other plans (Premium Plus)**. The existing one-time Ultra pack stays but does **not** cover the new features (section 6).
2. **Rotation default:** *Random, never the same twice in a row* (chosen for you; the other modes stay selectable).
3. **Saved designs:** **5 per server**.
4. **Channel-and-DM "both" mode:** **not included** (removed from the plan).
5. **Image safety:** **you review the hosting channel and delete bad images yourself.** No bot-side scanning (section 7).

---

## 1. What you already have (so we build on it, not beside it)

| Area | Today | Where |
| --- | --- | --- |
| Custom background | **1 image per server**, upload or link, stored as a Discord message in the image-host channel; refreshed from that message when the link expires | `custom_bg_channel_id`, `custom_bg_message_id`, `custom_background_url`; `welcome.py` `_custom_bg_bytes_for_render` |
| Wizard | "Customize Card" in the `/welcome` wizard, 3 tabs (Background, Layout, Text), preset menu, **live preview**, Reset, Done | `_views_card_customize.py` `build_customize_view` |
| Look options | banner (top/bottom/none), dim, avatar side/shape, centered layout, 10 fonts, brackets, 6 text colors, crop focus (incl. smart crop), 6 card ratios + auto ratio, ring, soft edge, shadow, big name, glass, auto contrast, custom heading, member number | `modules/welcome_card.py` `ULTRA_*`, `parse_ultra_options`, `_draw_custom_bg_card` |
| DM | **Already exists**: delivery mode "Post in channel" or "Direct message the member" | `WelcomeDeliverySelect` (`delivery_mode`) |
| Money | One-time Ultra pack (`ULTRA_PACK_FEE_USD = 1.99`) + card pack; 5-day free trial; non-unlocked users get a watermarked preview and nothing is stored | `config.py`, `database.py` `start_ultra_trial` |

So the real gaps are: more than one image, rotation, per-image control, frames/borders, effects, free positioning,
saved designs, and "channel + DM" / DM fallback.

---

## 2. The simple-wizard rule (the most important design decision)

Discord has no drag-and-drop, so "simple" has to come from **fewer choices per screen and good defaults**.

1. **One live preview at the top of every screen.** Every tap re-renders it. (Already true today; keep it.)
2. **Four steps, always in this order:** 1 Pictures, 2 Look, 3 Text, 4 Send. Big step buttons, current one highlighted.
3. **Easy mode by default.** Each step shows at most 3 controls. A single "More controls" button reveals the advanced ones for that step only.
4. **Presets first.** "Pick a style" gives a finished look in one tap (e.g. Clean, Neon, Gothic, Gold). Everything after is optional tweaking.
5. **Arrows, not numbers.** Position and zoom use ◀ ▲ ▼ ▶ and ➖ ➕ buttons with a visible step, never typed coordinates.
6. **Every change is undoable.** "Undo" and "Reset this step" on every screen. Nothing is saved to the live welcome until **Apply**.
7. **Plain words.** "Picture frame", "Move text", "Make text easier to read". No jargon like "focus" or "dim alpha".
8. **No new top-level slash command.** Everything lives inside the existing `/welcome` wizard (you are near Discord's 100-command cap).

Discord limit to design around: Components V2 messages allow about 40 components total, so each screen must stay at
roughly 5 to 6 rows. This is why advanced controls sit behind "More controls" instead of all on one page.

---

## 3. Feature design

### 3.1 Picture gallery (up to 10 images) and auto-change

- **Upload up to 10 at once** in one modal (Discord's file-upload field accepts multiple files; confirm the exact max when building) or add one at a time by link.
- Gallery screen shows thumbnails as a numbered strip. Buttons: Add, Remove, Make first, Replace.
- On upload, **downscale to the card width (1536 px) and re-encode as JPEG/WebP**. This cuts storage and render time and is the main protection against 10x the cost.
- **Auto-change modes** (one select, plain names). **Default: Random, never the same twice in a row.**
  - **Same picture every time** (pin one)
  - **Random, never the same twice in a row** (default; remembers only the last picture used)
  - **In order, one after another** (uses a database counter, safe across restarts and shards)
  - **By member number** (member #N gets picture N mod count; stateless, so zero race risk)
- Optional per-picture tweaks (under "More controls"): its own crop position, its own text color. Solves "this photo is too bright for white text".
- Single-image servers keep working exactly as today (their image becomes picture 1; nothing deleted).

### 3.2 Framing and cropping

- **Pan and zoom** the crop: ◀ ▲ ▼ ▶ and ➖ ➕ with the preview updating. Keeps today's smart crop as the default "Auto".
- **Card shape** stays (ratios + auto).
- **Picture frame** presets: none, thin border, thick border, double border, rounded corners, polaroid, film strip, glow edge.
- Frame color from the same small color list as text, plus "match the avatar color" (already exists for text).

### 3.3 Advanced positioning

- **3x3 position grid** for the text block and the avatar (nine plain buttons or one select: top left ... bottom right). Replaces "banner top/bottom, avatar left/right" while keeping those as presets.
- **Nudge** buttons for fine moves (small, medium, large step).
- **Text size**: Small / Medium / Large / Extra large (today only "big name" on/off).
- Safe-area guard: text can never be nudged off the card or under the avatar.

### 3.4 Fonts

- Keep the 10 bundled fonts. Add a few more OFL fonts (e.g. a bold sans, a handwriting, a serif) only if licensing is checked.
- Separate fonts for **name** and **heading** under "More controls" (default: same).
- Known limit already in the code: style fonts cover Latin characters only, other scripts fall back to the normal font. Say so in the wizard when a non-Latin name appears.

### 3.5 Effects (all off by default, each one tap)

Blur background, vignette, color tint, black and white, brightness, film grain, avatar glow, soft gradient overlay,
sparkle overlay. Keep each cheap in CPU (see risks). Existing: shadow, glass, soft edge, ring, auto contrast.

### 3.6 Borders

Card border (outer), avatar ring (exists), text outline (new). Thickness: thin / medium / thick. Color from the list.

### 3.7 Saved designs

- A **design** = the look only (layout, fonts, colors, effects, frame, positions), not the pictures.
- Buttons: **Save as...** (name), **Load**, **Rename**, **Delete**, **Make live**.
- Limit: **5 saved designs per server**.
- Later phase: **schedule a design** (e.g. Halloween look from Oct 25 to Nov 1, then back). Needs a daily check, so build it event-driven or reuse an existing daily loop rather than a new polling loop.

### 3.8 DM options

DM delivery already exists ("Post in channel" or "Direct message the member"). The **"channel and DM both"** mode is **not** part of this plan.
Small extras that remain (Phase 6, drop any you don't want):
- **If their DMs are closed, post in the channel instead** (toggle).
- **Separate DM text** with the existing variables, and optionally a different design for the DM.
- Rate-limit safe: reuse the existing DM helper (`discord_bot/dm_send.py`).

### 3.9 "More control over the final result"

- **Preview as a different member** (pick a name/number) and **preview each picture**.
- **Send me a test** button that delivers the real card to the admin (channel or DM, whichever is live).
- **Compare**: current live card vs. the draft, side by side.
- **Export**: download the final card PNG.

---

## 4. Data model (additive only, no destructive changes)

All changes are `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, matching how `database.py` already migrates.

- `discord_welcome_backgrounds`: `id`, `guild_id`, `clone_id`, `slot` (1 to 10), `channel_id`, `message_id`, `url_cache`, `label`, `options_override_json` (optional per-picture crop/color), `created_at`. Unique on (`guild_id`, `clone_id`, `slot`).
- `discord_welcome_designs`: `id`, `guild_id`, `clone_id`, `name`, `options_json`, `avatar_shape`, `created_by`, `created_at`. Limit enforced in code.
- New columns on `discord_welcome_config`: `rotation_mode`, `rotation_counter`, `active_design_id`, `dm_fallback_to_channel`, `last_slot_used` (for no-repeat random).
- **Migration:** on first read, an existing single custom background is shown as picture 1 (copy, never delete the old columns). Old columns stay as fallback so a rollback is safe.
- Options JSON stays inside `parse_ultra_options`: every new option gets a validated key with a safe default, so bad data can never crash a join (same rule as today).
- Round-robin counter uses one atomic `UPDATE ... RETURNING`. Works with the single-process, multi-shard setup and later with several processes.

---

## 5. Build phases (one PR each, small and reviewable)

| Phase | Ships | Why this order |
| --- | --- | --- |
| 0. Audit and design lock | Host-channel limits, render timing on a 1536 px card, and the **premium gate helper** (one function, `studio_unlocked(guild, clone)`, used by the wizard and the join render) | Cheap, and the gate is needed by every later phase |
| 1. Wizard restructure | 4-step shell (Pictures, Look, Text, Send), Easy/More controls, Undo, test-send. **No new features yet** | Simplicity first; low risk because behavior stays identical |
| 2. Gallery + rotation | 10 pictures, multi-upload, thumbnails strip, 4 rotation modes, migration of the single image | The headline feature and the clearest reason to pay |
| 3. Framing and positioning | Pan/zoom, frames, 3x3 grid, nudge, text size | Biggest "control over the result" |
| 4. Effects, borders, fonts | Effect toggles, border/outline, font per element, new fonts | Pure renderer work, no storage changes |
| 5. Saved designs | Save/load/rename/delete, per-server limit | Needs phases 1 to 4 to be worth saving |
| 6. DM extras | DM fallback to channel, DM text/design (no "both" mode) | Small, builds on existing DM code |
| 7. Scheduled designs | Date-based design switch (optional) | Nice upsell, build last |

Every phase: tests first, a PR against `main`, not merged until you approve, no deploy without your OK.

---

## 6. Gating and plans (decided)

**How Premium works today (from `config.py`):** Premium is $2.99/month per server (also yearly $16 and lifetime $99.99) and already
unlocks "every current and future package", including the Ultra welcome pack. Premium Plus ($6.99/month, $59/year) includes everything in
Premium. Buying Plus also keeps Premium active, so one check covers both: `db.is_guild_premium_active(guild_id, clone_id)`.

**The new rule:**

| Server has | Old Ultra features (1 picture, current look options) | Studio features (gallery, rotation, frames, positioning, effects, borders, saved designs) |
| --- | --- | --- |
| Nothing | Preview only, watermarked, nothing stored (unchanged) | Locked, preview only |
| **One-time Ultra pack only ($1.99)** | Yes, exactly as today | **No** (shows "Studio is part of Premium") |
| **Premium** (monthly, yearly) or **Premium Plus** | Yes | **Yes** |
| 5-day Ultra trial | Yes, as today | **No** (trial stays Ultra-only) |

Nothing changes for the $1.99 pack: price, what it covers, and what buyers already own. No refunds or migrations are needed.

**Rules so gating cannot be bypassed or cause harm:**
1. **Check on the server, not only in the buttons.** The wizard hides locked controls, but Save/Apply and the **welcome render at join time** also check `is_guild_premium_active`. Same approach as the level-card wizard (it checks when Save is pressed).
2. **Fail closed, softly.** If the premium lookup errors, render the plain Ultra look with picture 1. A join must never fail because a lookup failed.
3. **When Premium lapses, nothing is deleted.** Pictures and saved designs are kept. The card falls back to picture 1 and the Ultra look; rotation and studio-only options stop; saved designs become view-only with a "Renew Premium" button. Renewing restores everything instantly. The existing 3-day grace period applies.
4. **One-time Ultra owners who also have Premium** just see everything unlocked (Premium already includes Ultra).
5. **Clones:** the check is per `(guild_id, clone_id)`, matching how Premium is stored, so a clone never inherits the main bot's Premium.
6. **Upgrade nudge, not a nag:** locked studio steps show one line and one button ("Unlock with Premium"). No repeated pop-ups.
7. **No new product or price to build.** Because Premium and Plus already exist, there is no new Gumroad/Paystack product, no new unlock handler, and no price change. That removes the biggest payments risk from this project.

**Please confirm one assumption:** I treated **Premium lifetime ($99.99, one-time)** as covered, since it is a Premium plan. If you want
lifetime to exclude studio features, say so and the check becomes "active monthly/yearly plan only".

## 7. Risks and how we handle them

- **Image safety (decided: you handle it).** You review the hosting channel regularly and delete bad images yourself, so the bot does **no scanning**. Things that keep this workable without extra bot load:
  - Each upload posts to the hosting channel with a short caption (server name and ID, picture number) so you can see whose it is in one glance.
  - A **deleted image is handled gracefully**: the bot notices the missing message, marks that slot "needs a new picture", skips to the next picture, and shows the admin a one-line note in the wizard (no DM spam, no crash on join).
  - Cap: **10 pictures per server** plus a daily upload limit per server so one server cannot flood the channel.
  - Remaining risk, stated plainly: a bad image stays live until you see it. That is your accepted tradeoff; the plan adds no bot-side checks.
- **Image hosting:** pictures live as messages in a Discord channel. Deleting the message removes the picture (covered above). Do not delete or rename the hosting channel itself.
- **Render cost:** more effects means more CPU per join. Set a render time budget, decode and cache each picture once, downscale on upload, keep GIF/animation out of scope, and test a burst of joins (raid case).
- **Raids:** many joins at once must not rotate or render 500 cards unbounded. Reuse the existing anti-raid/join-gate behavior and cap concurrent renders.
- **Discord UI limits:** about 40 components per message, 25 options per select, 5 buttons per row. The 4-step layout is designed around this.
- **Free users:** keep the current rule (preview only, watermarked, nothing stored) so the free path never costs storage.
- **Regression:** the default look of an untouched server must render **pixel-identical** to today. Add golden-image tests.
- **Clones:** the feature must respect `clone_id` everywhere, like the current welcome code.

---

## 8. Testing plan

- Golden-image tests: untouched config renders the same as `main`.
- Option parsing: every new key validated, bad input falls back to defaults.
- Rotation: random never repeats back to back, in-order wraps around, by-member-number is stable, counter is atomic.
- Gallery: add, remove, reorder, 10-picture cap, missing-message fallback, migration of the old single image.
- Saved designs: limit, rename, delete, load restores the exact look.
- DM: closed-DM fallback to the channel, no crash when the member blocks the bot.
- Gating: Ultra-only server cannot save studio options or get rotation at render time; Premium and Plus can; lapsed Premium falls back to picture 1 and keeps data; lookup error fails closed; clone Premium is separate from the main bot's.
- Deleted image: missing message marks the slot, falls back to the next picture, no crash on join.
- Wizard: each step stays under the component limit, access checks (only admins), draft never touches the live card until Apply.
- Load: burst of joins stays within the render budget.
- Live Discord checks still needed on a staging server (uploads, DM delivery, the real message component limits).

---

## 9. Decisions

**Settled:** gating under Premium and Plus (one-time Ultra excluded), random-no-repeat default, 5 saved designs, no "both" DM mode, owner reviews the hosting channel.

**Still open (small):**
1. **Premium lifetime** includes studio features? (Plan assumes yes.)
2. **Phase 6 DM extras:** keep "DM fallback to channel" and "separate DM text", or drop them?
3. **Per-picture overrides** (own crop and text color per picture): v1 or later? (Plan puts them under "More controls" in Phase 2.)
4. **Start with** Phase 0 + 1 (gate helper + wizard restructure), then Phase 2 (gallery + rotation)?

---

## 10. Out of scope for now

Animated/GIF backgrounds, user-drawn editors, AI-generated backgrounds, per-member custom cards, and a web designer.
Any of these can be a later plan once the studio is live.
