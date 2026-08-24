"""
Shared live currency-conversion helper.

Scope note: this is being introduced for Discover Players' category-cap
upgrade (config.DISCOVER_CAP_TIERS) as the first call site. Every OTHER
flat-fee Paystack charge in this codebase (CLONE_BOT_FEE_GHS in
clone_admin.py, discord_premium_groups.fee_ghs, botstore premium, AI
subscription pricing, etc.) still bills a hardcoded GHS amount and was
deliberately left alone here — that's a separate, larger pass. See
CURRENCY_CONVERSION_HANDOFF.md at the repo root for exactly what's left
and a ready-to-paste prompt for doing it.

Design:
- Almost every price in this codebase is a hardcoded GHS constant in
  config.py (CLONE_BOT_FEE_GHS, DISCORD_CLONE_FEE_GHS, CLONE_MONETIZATION_FEE_GHS,
  etc. — the *_GHS naming is the convention, not *_USD). convert_ghs_to_usd
  below converts one of those GHS prices to USD at charge time for the
  Stripe path, which can't settle in GHS (see payments.gateway_charge_amount).
- DISCOVER_CAP_TIERS (config.py) is the one deliberate exception: its
  tier prices are USD, not GHS, and convert_from_usd/usd_to_minor_units
  below convert THAT to the payer's target currency at checkout time —
  never stored pre-converted, since rates move. Don't assume any other
  price in config.py is USD; check the constant's own name/comment.
- Rates come from exchangerate.host (no API key required for the free
  tier) and are cached in-process for RATE_CACHE_SECONDS so a burst of
  checkouts doesn't hit the API per-request; a stale-but-present cache
  entry is preferred over a hard failure if a refetch errors out.
- Only Paystack-supported currencies are offered: NGN, GHS, ZAR, USD, KES.
  Anything else (a Discord locale we can't map, or a user's explicit
  request for an unsupported code) falls back to USD, which Paystack
  always accepts.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FX_API_URL = "https://api.exchangerate.host/latest"
RATE_CACHE_SECONDS = 3600  # 1 hour — FX rates don't need to be more current than this for a $5 charge

SUPPORTED_CURRENCIES = {"NGN", "GHS", "ZAR", "USD", "KES"}

# Paystack's minor-unit multiplier per currency (all 5 supported currencies
# happen to use 2 decimal places today, but keep this explicit rather than
# hardcoding "* 100" at every call site in case that ever changes).
MINOR_UNIT_MULTIPLIER = {"NGN": 100, "GHS": 100, "ZAR": 100, "USD": 100, "KES": 100}

# Best-effort Discord locale -> Paystack-supported currency. discord.py's
# interaction.locale gives a Locale enum (e.g. Locale.british_english);
# str(locale) yields values like "en-GB". Discord doesn't expose
# country-level locales (only language ones), so there's no reliable
# locale for Nigeria/Ghana/Kenya/South Africa specifically — this map is
# therefore intentionally empty for now rather than guessing wrong. A user
# should always be able to override with /currency set; extend this map
# only with locales you're confident map to one of SUPPORTED_CURRENCIES.
LOCALE_TO_CURRENCY: dict[str, str] = {}

_rate_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate_from_usd, fetched_at)


def _fetch_rate(currency: str) -> Optional[float]:
    now = time.time()
    cached = _rate_cache.get(currency)
    if cached and (now - cached[1]) < RATE_CACHE_SECONDS:
        return cached[0]

    try:
        resp = requests.get(FX_API_URL, params={"base": "USD", "symbols": currency}, timeout=8)
        resp.raise_for_status()
        rate = resp.json()["rates"][currency]
        _rate_cache[currency] = (rate, now)
        return rate
    except (requests.RequestException, KeyError, ValueError):
        logger.warning("FX rate fetch failed for USD->%s", currency)
        if cached:
            logger.info("Falling back to stale cached rate for %s (age %.0fs)", currency, now - cached[1])
            return cached[0]
        return None


def convert_from_usd(usd_amount: float, currency: str) -> tuple[float, str]:
    """Returns (converted_amount, currency_actually_used). Falls back to
    USD (no conversion) if the target currency isn't supported or the rate
    fetch fails outright with no cached fallback available — a charge
    should never be blocked by an FX API being down."""
    currency = (currency or "USD").upper()
    if currency not in SUPPORTED_CURRENCIES or currency == "USD":
        return round(usd_amount, 2), "USD"

    rate = _fetch_rate(currency)
    if rate is None:
        logger.warning("No FX rate available for %s — charging in USD instead", currency)
        return round(usd_amount, 2), "USD"

    return round(usd_amount * rate, 2), currency


def convert_ghs_to_usd(ghs_amount: float) -> Optional[float]:
    """Every price in this bot is set in GHS. Stripe doesn't settle in GHS,
    so any GHS-priced item charged through Stripe needs to be converted to
    USD first. Returns None (never a guessed/static rate) if the live rate
    can't be fetched and there's no cached one to fall back on — callers
    MUST treat None as "don't charge, ask the user to retry", since
    guessing a rate here risks badly over/under-charging a real card."""
    rate = _fetch_rate("GHS")  # USD -> GHS
    if not rate:
        return None
    return round(ghs_amount / rate, 2)


def usd_to_minor_units(usd_amount: float, currency: str) -> tuple[int, str]:
    """Convenience wrapper for the common case: convert a USD base price
    and return (amount_in_minor_units, currency) ready to pass straight
    into PaystackPayment.initialize_payment(amount_minor_units=..., currency=...)."""
    converted, used_currency = convert_from_usd(usd_amount, currency)
    multiplier = MINOR_UNIT_MULTIPLIER.get(used_currency, 100)
    return round(converted * multiplier), used_currency


def currency_from_locale(locale) -> Optional[str]:
    """locale: a discord.Locale (or None). Returns a supported currency
    code, or None if the locale doesn't map to one — callers should treat
    None the same as "ask the user" / fall back to USD, not guess further."""
    if locale is None:
        return None
    return LOCALE_TO_CURRENCY.get(str(locale))
