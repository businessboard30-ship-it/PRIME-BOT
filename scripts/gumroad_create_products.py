"""Create PRIME-BOT products on Gumroad. Run locally (the container can't reach api.gumroad.com).

    export GUMROAD_TOKEN=...   # your access token
    python gumroad_create_products.py

Pulls prices from config.py (run from the repo root). If POST /products is
rejected (Gumroad's public API may not support creation), writes
gumroad_products_checklist.md for manual creation and exits cleanly.
"""
import os, sys, json
import requests

sys.path.insert(0, os.getcwd())
import config

TOKEN = os.environ.get("GUMROAD_TOKEN")
if not TOKEN:
    sys.exit("Set GUMROAD_TOKEN first.")

tiers = config.XP_SERVER_BOOST_TIERS
tier_list = list(tiers.values()) if isinstance(tiers, dict) else list(tiers)
t24 = next(t for t in tier_list if t["duration_hours"] == 24)
t30 = next(t for t in tier_list if t["duration_hours"] == 24 * 30)

PRODUCTS = [
    ("Welcome Card Pack", config.WELCOME_CARD_PACK_FEE_USD, "Custom welcome card pack for your Discord server."),
    ("Ultra Welcome Pack", config.ULTRA_PACK_FEE_USD, "Ultra welcome pack upgrade for your Discord server."),
    ("Custom Role", config.CUSTOM_ROLE_FEE_USD, "Custom role perk in a PRIME-BOT server."),
    ("Music Pro", config.MUSIC_PRO_FEE_USD, "Music Pro upgrade, one-time, per server."),
    ("Discord Clone", config.DISCORD_CLONE_ACTIVATION_FEE_USD, "Discord clone bot activation."),
    ("Discord Clone Monetization", config.CLONE_MONETIZATION_FEE_USD, "Monetization feature for your Discord clone bot."),
    ("XP Boost", config.XP_BOOST_FEE_USD, "Personal 2x XP boost for 7 days."),
    ("XP Server Boost - 24 Hours", t24["fee_usd"], f"{t24['multiplier']}x XP for the whole server for 24 hours."),
    ("XP Server Boost - 1 Month", t30["fee_usd"], f"{t30['multiplier']}x XP for the whole server for 30 days."),
]

api = "https://api.gumroad.com/v2/products"
created, failed = [], False
for name, price, desc in PRODUCTS:
    r = requests.post(api, data={
        "access_token": TOKEN, "name": name,
        "price": int(round(float(price) * 100)),  # cents
        "description": desc,
    }, timeout=30)
    ok = r.status_code == 200 and r.json().get("success")
    print(f"{'OK  ' if ok else 'FAIL'} {name} ${price} -> {r.status_code} {r.text[:150]}")
    if ok:
        p = r.json()["product"]
        created.append({"name": name, "price": price, "id": p["id"], "url": p.get("short_url")})
    else:
        failed = True
        break

if created:
    json.dump(created, open("gumroad_products.json", "w"), indent=2)
    print("Saved gumroad_products.json")

if failed:
    with open("gumroad_products_checklist.md", "w") as f:
        f.write("# Create these manually at gumroad.com/products/new (Digital product, one-time)\n\n")
        for name, price, desc in PRODUCTS:
            f.write(f"- **{name}** - ${price}\n  {desc}\n")
    print("Creation not supported/failed. Wrote gumroad_products_checklist.md")
