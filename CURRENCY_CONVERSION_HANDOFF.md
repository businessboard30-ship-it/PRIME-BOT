# Currency conversion handoff

## What's done (this pass)

- `utils/currency.py` — shared FX helper. `convert_from_usd(usd_amount, currency)`
  and `usd_to_minor_units(usd_amount, currency)` do live USD-based conversion
  via exchangerate.host, 1hr in-process cache, falls back to USD (never blocks
  a charge) if the target currency is unsupported or the FX API is down.
  `SUPPORTED_CURRENCIES = {NGN, GHS, ZAR, USD, KES}` — Paystack-supported set.
- `payments.PaystackPayment.initialize_payment(...)` now takes a `currency`
  param (default `"GHS"` — every pre-existing call site is unaffected).
- `user_currency_prefs` table + `db.set_user_currency` / `db.get_user_currency`
  — one currency preference per user, shared across all paid features.
- `/currency set` command (`discord_bot/cogs/discover_players.py`) — lets a
  user pick NGN/GHS/ZAR/KES/USD explicitly.
- **Wired up for Discover Players only**: `/discover upgrade` now converts its
  USD tier price (`config.DISCOVER_CAP_TIERS`, now `(cap_from, cap_to,
  price_usd)`) live via `fx.usd_to_minor_units`, using `/currency set` if the
  user has one, else a best-effort Discord-locale guess (currently returns
  None for everything — see note below), else USD.

## What's NOT done — still hardcoded flat GHS

Every other Paystack charge in the codebase still bills a fixed GHS amount
and was deliberately left alone:

- `discord_bot/cogs/clone_admin.py` — `CLONE_BOT_FEE_GHS` / `DISCORD_CLONE_FEE_GHS`
- `database.py`'s `discord_premium_groups.fee_ghs` (per-guild premium role pricing,
  set via whatever command creates a premium group — grep for `fee_ghs`)
- botstore premium tier pricing (`payment_type == 'botstore_premium'` in
  `api/paystack_webhook.py` — find the initiating command)
- AI subscription pricing (`payment_type == 'ai_subscription'`)
- clone monetization subscription pricing (`payment_type == 'clone_monetization'`)
- SuperBot tier pricing (`payment_type == 'superbot_tier'`)
- any other `paystack.initialize_payment(...)` call site — grep the repo for
  `initialize_payment(` to find them all

## Known gap: locale currency mapping is empty

`utils/currency.py`'s `LOCALE_TO_CURRENCY` dict is currently empty — Discord's
`interaction.locale` only exposes language locales (e.g. `en-GB`), not country,
so there's no reliable way to infer NGN/GHS/ZAR/KES from it alone. Right now
every user who hasn't run `/currency set` gets charged in USD. If a better
signal becomes available (e.g. a guild's configured region, or IP-based
lookup at the OAuth/dashboard layer), wire it in here.

## Suggested prompt for the next Claude session

```
Continue the currency-conversion work described in
CURRENCY_CONVERSION_HANDOFF.md. utils/currency.py and
payments.PaystackPayment already support live USD-based conversion and a
per-user currency preference (db.get_user_currency/set_user_currency).

Go through every remaining flat-GHS Paystack charge listed under "What's
NOT done" in that doc, one at a time as separate reviewable changes:
1. Convert its hardcoded *_GHS constant to a *_USD base price in config.py
2. At the point it calls paystack.initialize_payment(...), resolve the
   payer's currency the same way discover_players.py's upgrade command
   does (_resolve_currency: /currency set preference, else USD), convert
   with utils.currency.usd_to_minor_units, and pass the result plus
   currency= into initialize_payment
3. Make sure the corresponding api/paystack_webhook.py case still works
   unchanged (it shouldn't need to — webhook metadata/logic doesn't care
   what currency was charged)
Flag anywhere a price was chosen somewhat arbitrarily when it was
originally in GHS, in case the USD-equivalent should be reconsidered
rather than just converted 1:1 at today's rate.
```
