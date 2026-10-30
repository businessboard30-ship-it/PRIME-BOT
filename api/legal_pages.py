"""
Static Terms of Service / Privacy Policy pages, served by api_server.py.

These exist purely to satisfy Discord's app-verification requirement
("Your app must have a link to Terms of Service" / "...Privacy Policy") —
each just needs to be a real, permanently reachable URL. Content here is a
generic, honest starting point covering what this bot actually does
(Discord commands, optional AI features, optional paid premium roles/
credits, optional linked media accounts) — NOT a substitute for real legal
review if you're operating at scale or in a jurisdiction with specific
requirements (e.g. GDPR). Edit BOT_NAME/CONTACT_EMAIL below and the body
text as needed before relying on this for verification.
"""

import asyncio
from http.server import BaseHTTPRequestHandler

BOT_NAME = "Prime Bot"
CONTACT_EMAIL = "maxwelldumenya5@outlook.com"
# Paddle's domain-review checklist requires the company name or sole
# proprietor's legal name to appear IN the Terms & Conditions specifically
# (a product/brand name like BOT_NAME above doesn't satisfy this on its
# own) — https://www.paddle.com help center, "Domain Review". Inferred
# from the contact email pending confirmation; correct this if the exact
# legal name/spelling is wrong.
OPERATOR_NAME = "Maxwell Dumenya"
OPERATOR_LOCATION = ""

# Pricing table below is generated from these — edit config.py's own
# constants (WELCOME_CARD_PACK_FEE_USD, XP_SERVER_BOOST_TIERS, etc.), not the
# numbers here directly, so this page can't silently drift from what
# payments_manual.py actually charges.
from config import (
    WELCOME_CARD_PACK_FEE_USD, ULTRA_PACK_FEE_USD, DISCORD_CLONE_ACTIVATION_FEE_USD,
    CLONE_MONETIZATION_FEE_USD, CUSTOM_ROLE_FEE_USD, MUSIC_PRO_PRICE_LABEL,
    LEVEL_PASS_FEE_USD, LEVEL_PASS_CARD_DAYS, XP_BOOST_MULTIPLIER, XP_BOOST_DURATION_DAYS, XP_BOOST_BUNDLES, GUMROAD_PRODUCT_LINKS,
    XP_SERVER_BOOST_TIERS, PREMIUM_FEE_USD, PREMIUM_YEARLY_FEE_USD, PREMIUM_LIFETIME_FEE_USD,
)

_PAGE_CSS = """
body{font-family:sans-serif;max-width:720px;margin:2rem auto;padding:0 1.5rem;
     line-height:1.6;color:#1a1a1a}
h1{margin-bottom:0.25rem}
.updated{color:#666;font-size:0.9rem;margin-bottom:2rem}
h2{margin-top:2rem}
"""


def _boost_bundle_items() -> str:
    """<li> rows for XP boost bundles — only bundles whose Gumroad product is
    configured, so the public pricing page never advertises something that
    can't be bought yet."""
    rows = []
    for key, b in XP_BOOST_BUNDLES.items():
        if not GUMROAD_PRODUCT_LINKS.get(key):
            continue
        days = b["boosts"] * XP_BOOST_DURATION_DAYS
        rows.append(
            f"\n<li>${b['fee_usd']:g} — {b['label']}: {b['boosts']} boosts, "
            f"{XP_BOOST_MULTIPLIER:g}x XP for {days} days, just for you, plus the custom level-up card for "
            f"{LEVEL_PASS_CARD_DAYS} days</li>"
        )
    return "".join(rows)


def _page(title: str, body_html: str) -> bytes:
    nav = (
        '<nav style="margin-top:2.5rem;padding-top:1rem;border-top:1px solid #ddd">'
        '<a href="/">Home</a> &middot; '
        '<a href="/pricing">Pricing</a> &middot; '
        '<a href="/terms">Terms of Service</a> &middot; '
        '<a href="/privacy">Privacy Policy</a> &middot; '
        '<a href="/refund">Refund Policy</a>'
        '</nav>'
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} — {BOT_NAME}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{body_html}{nav}</body></html>"
    ).encode()


TERMS_HTML = f"""
<h1>Terms of Service</h1>
<p class="updated">{BOT_NAME}</p>
<p>{BOT_NAME} is operated by {OPERATOR_NAME}, a sole proprietor based in
Contact: {CONTACT_EMAIL}.</p>
<p>By adding or using {BOT_NAME} ("the bot") in a Discord server, or by
installing it to your own account, you agree to these terms. These terms also
apply to every bot run from the same service, including {BOT_NAME}'s
white-label bots such as Welcome Bot, and the bot's website and dashboard.</p>

<h2>Use of the bot</h2>
<p>The bot provides Discord commands and features including moderation
tools, an in-server economy, leveling, AI chat/image features, and optional
paid premium roles or credits configured by individual server admins. You
must comply with Discord's own Terms of Service and Community Guidelines
while using it.</p>

<h2>Paid features</h2>
<p>Some features (premium roles, AI Store credits)
involve real payment. See our <a href="/pricing">Pricing</a> page for a
full list of paid features and current prices. Payments are processed by
a third-party payment provider; see our <a href="/refund">Refund
Policy</a> for how refunds work.</p>

<h2>No warranty</h2>
<p>The bot is provided "as is," without warranty of any kind. Features may
change, be added, or be removed at any time. We are not liable for lost
data, lost currency/credits, moderation actions taken by the bot on a
server's behalf, or service interruptions.</p>

<h2>Server owners</h2>
<p>If you add the bot to a server, you are responsible for how its features
(welcome messages, verification, join gate, leveling, automod and moderation)
are configured there, and for following Discord's Terms and Community
Guidelines and the rules that apply to you for your community.</p>

<h2>Linked accounts</h2>
<p>If you link an external account (e.g. a media server or Google Drive
folder) to the bot, you are responsible for that account and can disconnect it
at any time using the relevant command.</p>

<h2>Termination</h2>
<p>We may suspend or remove the bot's access to a server, or disable a
user's access to specific features, for violations of these terms or
Discord's own policies.</p>

<h2>Changes</h2>
<p>These terms may be updated from time to time. Continued use of the bot
after a change means you accept the updated terms.</p>

<h2>Contact</h2>
<p>Questions about these terms: {CONTACT_EMAIL}, or use the bot's
/feedback command.</p>
"""

PRIVACY_HTML = f"""
<h1>Privacy Policy</h1>
<p class="updated">{BOT_NAME}</p>
<p>This page explains what data {BOT_NAME} ("the bot") collects and how
it's used.</p>

<h2>What we collect</h2>
<ul>
<li>Discord IDs (user, server, and channel IDs) needed to operate features
you use — e.g. your economy balance, XP/level, warns, or premium status are
all stored against your Discord user ID and the server's ID.</li>
<li>Message content only where a feature requires it (e.g. automod's
word/invite/spam filters scan message text to enforce rules you or a
server admin configured; AI chat/image features send your prompt to the
underlying AI provider to generate a response).</li>
<li>Payment references (not full card details — payments are handled by a
third-party payment processor) when you use a paid feature.</li>
<li>Messages that Scam Shield removes as known scams: a copy (text and attachments, with the server and user IDs) is saved to a private channel run by the operator, so the scam rules can be improved.</li>
<li>Optional linked accounts (media server, cloud folder) only if you
choose to connect one, stored so the relevant feature keeps working until you
disconnect it.</li>
<li>Server membership events: when someone joins or leaves a server where the
bot is added, the bot sees their Discord ID, username, account creation date
and join or leave time, and which invite they used if the invite tracker is
turned on. This is used for welcome messages, verification, the join gate,
invite tracking and moderation, and it is stored with the server's ID.</li>
</ul>

<h2>Why the bot reads members and messages</h2>
<p>The bot uses two Discord permissions that need Discord's approval. The
<b>Server Members</b> permission lets it react when someone joins or leaves and
look up members for the features above. The <b>Message Content</b> permission
lets it read messages as they are sent to award XP, to run automod filters,
and to answer commands and triggers a server admin configured. Message text is
processed as it arrives and is not kept, apart from the Scam Shield copy
described above. The bot does not use message content to train AI or machine
learning models, and does not store messages outside Discord and the
operator's private review channel.</p>

<h2>Bots run from this service</h2>
<p>This policy also covers every bot run from the same service, including
white-label bots such as Welcome Bot. For those bots the operator is still
{OPERATOR_NAME}. If a server owner registers their own bot with us, we keep the
details needed to run it, encrypted, until they remove it.</p>

<h2>What we don't do</h2>
<p>We don't sell your data. We don't read or store messages outside of
what a feature you're actively using needs in order to work.</p>

<h2>Website dashboard</h2>
<ul>
<li>AI chats you have on the website are not stored by us. Only a weekly
count of how many you used is kept. Closing the page clears the conversation.</li>
<li>If you add your own AI provider key, it is encrypted, never shown again,
and used only to answer your own chats. It is deleted 30 days after your
Developer plan ends, or whenever you remove it.</li>
<li>Member messages (friends from your servers) are text only. Both people
can delete a conversation or unfriend at any time, and messages are deleted
automatically after 30 days. If you report a person, the last messages of
that conversation (up to 20) are copied to the owner's review queue and are
deleted 90 days after the report is resolved. Friend requests can be switched
off, and an optional Discord DM notice is off unless you turn it on.</li>
</ul>

<h2>Third parties</h2>
<p>Depending on which features you use, data may be sent to: the AI
provider powering chat/image features, the payment processor handling paid
features, and (only if you connect one yourself) a media server, cloud
storage provider, or another Discord bot's application.</p>

<h2>Data retention & deletion</h2>
<p>Feature-related data (balances, XP, warns, linked accounts, join records)
is kept while you continue using the bot in a server. You can disconnect a
linked account at any time with its corresponding command, and you can mute
level-up pings from the leveling leaderboard. Server admins can turn off
leveling, automod and invite tracking in their own server. To request full
deletion of your data, or to opt out of having your activity tracked,
email {CONTACT_EMAIL} with your Discord user ID; we will delete it or tell you
why we cannot (for example payment records we must keep).</p>

<h2>Web dashboard</h2>
<p>If you sign in to the web dashboard with Discord, we store a record that you
signed in, with your Discord display name and avatar (used to show you on the global
leaderboard unless you switch that off on the Leaderboard page; everyone else sees
"Hidden player" and no user ID is shown), the days you used the dashboard (kept 90 days
and visible only to the bot owner), your level-up card design and uploads, any AI
provider keys you add (encrypted, never shown again), your scheduled jobs, renewal
reminders, and receipts for exports you save (the files are encrypted). If you connect
GitHub (read-only) we keep an encrypted access key until you disconnect, which deletes
it. If you use member messaging, we store your friends list and messages (deleted after
30 days, or when you delete your data). AI chats on the dashboard are not stored. You can delete all of this
yourself under Privacy &amp; data in the dashboard. Payment records, your plans,
weekly chat counters and in-server records (XP, coins, moderation) are kept.</p>

<h2>Children's privacy</h2>
<p>The bot is not directed at children under 13, consistent with Discord's
own minimum age requirement.</p>

<h2>Changes</h2>
<p>This policy may be updated from time to time; continued use after a
change means you accept the updated policy.</p>

<h2>Contact</h2>
<p>Questions about this policy, or to request data deletion:
{CONTACT_EMAIL}, or use the bot's /feedback command.</p>
"""


from modules.user_subs import PLANS as _USER_PLANS

PRICING_HTML = f"""
<h1>Pricing</h1>
<p class="updated">{BOT_NAME}</p>
<p>{BOT_NAME} is free to add and use. Paid features are optional
per-server or per-user unlocks. Go Premium comes as a monthly, yearly or lifetime plan; everything
else below is a one-time purchase. All prices in USD.</p>

<h2>Go Premium (per server: monthly, yearly or lifetime)</h2>
<ul>
<li>Yearly: ${PREMIUM_YEARLY_FEE_USD:g} one-time for 365 days. Lifetime: ${PREMIUM_LIFETIME_FEE_USD:g} one-time, no expiry (for as long as the bot service operates).</li>
<li>${PREMIUM_FEE_USD:g}/month for the whole server — unlocks every premium
feature (giveaways, tickets, Music Pro, welcome cards, fonts, custom roles,
higher AI chat limit and future features).</li>
<li>Global checkout (Gumroad) renews automatically and can be cancelled any
time. local Paystack checkout covers 30 days per payment and does not
auto-renew.</li>
</ul>

<h2>Personal plans (web dashboard)</h2>
<ul>
<li>Card plan: ${_USER_PLANS['card_plan']['price_usd']:g}/month, custom level-up card.</li>
<li>Developer mode: ${_USER_PLANS['dev_monthly']['price_usd']:g}/month or ${_USER_PLANS['dev_yearly']['price_usd']:g}/year.</li>
<li>Anime feed upgrade (Discord): ${_USER_PLANS['anime_plus']['price_usd']:g}/month, more follows, DM alerts and quiet hours.</li>
<li>Renew automatically; cancel any time and keep access to the end of the period.</li>
</ul>

<h2>One-time unlocks</h2>
<ul>
<li>Welcome Card Pack — ${WELCOME_CARD_PACK_FEE_USD:g}</li>
<li>Ultra Welcome Pack — ${ULTRA_PACK_FEE_USD:g}</li>
<li>Custom Role — ${CUSTOM_ROLE_FEE_USD:g}</li>
<li>Music Pro — {MUSIC_PRO_PRICE_LABEL}</li>
<li>Level Pass — ${LEVEL_PASS_FEE_USD:g} once: your own level-up card for {LEVEL_PASS_CARD_DAYS} days plus one XP boost
({XP_BOOST_MULTIPLIER:g}x XP for {XP_BOOST_DURATION_DAYS} days). One payment, no subscription.</li>
</ul>

<h2>XP Boost (personal)</h2>
<ul>
<li>The Level Pass (above) includes one personal boost: {XP_BOOST_MULTIPLIER:g}x XP for {XP_BOOST_DURATION_DAYS} days,
just for you</li>{_boost_bundle_items()}
</ul>

<h2>XP Server Boost (whole-server multiplier)</h2>
<ul>
{"".join(f'<li>{t["label"]} — ${t["fee_usd"]:g}</li>' for t in XP_SERVER_BOOST_TIERS.values())}
</ul>

<p>Exact checkout amounts may be shown in a local currency equivalent at
payment time. See our <a href="/refund">Refund Policy</a> for how refunds
work.</p>

<h2>Contact</h2>
<p>Questions about pricing: {CONTACT_EMAIL}, or use the bot's /feedback
command.</p>
"""

REFUND_HTML = f"""
<h1>Refund Policy</h1>
<p class="updated">{BOT_NAME}</p>
<p>All purchases are digital — one-time unlocks, plus the optional monthly
Go Premium plan. See our <a href="/pricing">Pricing</a> page for what's
available.</p>

<h2>Go Premium (monthly)</h2>
<ul>
<li>Cancel any time. Cancelling stops future renewals; Premium stays active
until the end of the period you already paid for.</li>
<li>Paystack (local checkout) payments are a single 30-day purchase and never renew
on their own, so there is nothing to cancel.</li>
<li>If Premium was paid for but never activated on your server, or doesn't
work as described because of a fault on our end, contact us and we'll fix it
or refund that payment.</li>
<li>A period of Premium that was already active and working is generally not
refundable, and we don't give partial refunds for unused days after
cancelling.</li>
</ul>

<h2>How refunds work (one-time unlocks)</h2>
<ul>
<li>If a purchase hasn't been delivered/activated yet (e.g. still pending
manual approval), you can request a full refund at any time before it's
approved.</li>
<li>If a purchase was delivered/activated but doesn't work as described
due to a fault on our end, contact us and we'll fix it or refund it.</li>
<li>Digital unlocks that have already been delivered and are working as
described (e.g. a server-wide XP boost that already ran its full
duration, or XP wallet credit that's already been spent/gifted) are
generally not refundable, since the value has already been received —
but reach out if something looks wrong and we'll look into it.</li>
</ul>

<h2>How to request one</h2>
<p>Email {CONTACT_EMAIL} with your Discord user ID, the server ID (for
server-wide purchases), and your payment reference. We aim to respond
within a few business days.</p>

<h2>Changes</h2>
<p>This policy may be updated from time to time; continued use after a
change means you accept the updated policy.</p>
"""


async def _handle_terms():
    return 200, _page("Terms of Service", TERMS_HTML)


async def _handle_privacy():
    return 200, _page("Privacy Policy", PRIVACY_HTML)


async def _handle_pricing():
    return 200, _page("Pricing", PRICING_HTML)


async def _handle_refund():
    return 200, _page("Refund Policy", REFUND_HTML)


class TermsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = asyncio.run(_handle_terms())
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


class PrivacyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = asyncio.run(_handle_privacy())
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


class PricingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = asyncio.run(_handle_pricing())
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


class RefundHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = asyncio.run(_handle_refund())
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)
