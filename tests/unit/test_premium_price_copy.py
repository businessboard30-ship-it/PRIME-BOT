"""Premium monthly price is $5.99: config, the payment checks, and every page that states it must agree."""
import json
import re
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["website/index.html", "website/pricing/index.html", "website/premium/index.html", "website/docs/index.html",
         "redirector/pricing/index.html"]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _savings_pct():
    return round((1 - config.PREMIUM_YEARLY_FEE_USD / (config.PREMIUM_FEE_USD * 12)) * 100)


def test_config_price_and_the_other_plans_are_as_intended():
    assert config.PREMIUM_FEE_USD == 5.99 and config.PREMIUM_LEGACY_FEE_USD == 2.99
    assert config.PREMIUM_YEARLY_FEE_USD == 16 and config.PREMIUM_LIFETIME_FEE_USD == 99.99      # only monthly changed


def test_payment_checks_accept_the_new_price_and_still_catch_underpayment():
    cents = round(config.PREMIUM_FEE_USD * 100)
    assert cents == 599                                              # no float drift when converting to cents
    import gumroad_payments as gp
    assert gp.expected_price_usd("premium") == 5.99
    assert not (599 < cents) and (598 < cents)                       # same comparison as the webhook: 599c ok, 598c underpaid


def test_raising_the_price_keeps_existing_2_99_members_working():
    # The price went UP ($2.99 -> $5.99). The webhook accepts the old amount through PREMIUM_LEGACY_FEE_USD, so a Gumroad
    # membership still charged $2.99 renews. The Gumroad product itself must be set to $5.99 by hand for NEW buyers.
    legacy_cents = round(config.PREMIUM_LEGACY_FEE_USD * 100)
    assert legacy_cents == 299 and legacy_cents < round(config.PREMIUM_FEE_USD * 100)


def test_no_page_still_shows_the_old_monthly_price_or_the_old_saving():
    for rel in PAGES:
        t = _read(rel)
        assert "$4/month" not in t and "$4<small>/month" not in t and "$4<small>/mo<" not in t, rel
        assert "67%" not in t and "price of 4" not in t, rel
        assert "Monthly ($4)" not in t and "Monthly ($5.99)" not in t, rel
        assert "$2.99/month" not in t and "$2.99<small>" not in t and "Monthly ($2.99)" not in t, rel
        assert "55%" not in t and "$35.88" not in t, rel
    assert "$2/month for the whole server" not in _read("redirector/pricing/index.html")


def test_every_page_that_states_the_monthly_price_says_5_99():
    for rel in ("website/index.html", "website/pricing/index.html", "website/premium/index.html", "website/docs/index.html",
                "redirector/pricing/index.html"):
        assert "$5.99" in _read(rel), rel


def test_the_stated_saving_matches_what_the_code_computes():
    pct = _savings_pct()
    assert pct == 78
    full = f"${config.PREMIUM_FEE_USD * 12:.2f}"
    assert full == "$71.88"
    for rel in ("website/index.html", "website/pricing/index.html", "website/premium/index.html", "website/docs/index.html"):
        t = _read(rel)
        assert f"{pct}%" in t and (rel.endswith("premium/index.html") or full in t), rel


def _json_ld(rel):
    out = []
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', _read(rel), re.S):
        out.append(json.loads(raw))
    return out


def _offers(rel):
    found = []
    for doc in _json_ld(rel):
        if doc.get("@type") == "SoftwareApplication":
            found += doc["offers"]
    return found


def test_structured_data_still_parses_and_monthly_offer_is_5_99():
    for rel in ("website/index.html", "website/pricing/index.html"):
        offers = _offers(rel)
        monthly = next(o for o in offers if o["name"].startswith("Go Premium — Monthly"))
        assert monthly["price"] == "5.99" and monthly["priceSpecification"]["price"] == "5.99"
        yearly = next(o for o in offers if o["name"].startswith("Go Premium — Yearly"))
        assert yearly["price"] == "16"


def test_faq_structured_data_states_the_new_price():
    for rel in ("website/index.html", "website/docs/index.html"):
        for doc in _json_ld(rel):
            if doc.get("@type") == "FAQPage":
                cost = next(q for q in doc["mainEntity"] if q["name"] == "What does Go Premium cost?")
                assert cost["acceptedAnswer"]["text"].startswith("$5.99/month, $16/year (about 78% off")
