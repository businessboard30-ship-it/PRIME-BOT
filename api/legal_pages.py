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

_PAGE_CSS = """
body{font-family:sans-serif;max-width:720px;margin:2rem auto;padding:0 1.5rem;
     line-height:1.6;color:#1a1a1a}
h1{margin-bottom:0.25rem}
.updated{color:#666;font-size:0.9rem;margin-bottom:2rem}
h2{margin-top:2rem}
"""


def _page(title: str, body_html: str) -> bytes:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} — {BOT_NAME}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{body_html}</body></html>"
    ).encode()


TERMS_HTML = f"""
<h1>Terms of Service</h1>
<p class="updated">{BOT_NAME}</p>
<p>By adding or using {BOT_NAME} ("the bot") in a Discord server, or by
installing it to your own account, you agree to these terms.</p>

<h2>Use of the bot</h2>
<p>The bot provides Discord commands and features including moderation
tools, an in-server economy, leveling, AI chat/image features, and optional
paid premium roles or credits configured by individual server admins. You
must comply with Discord's own Terms of Service and Community Guidelines
while using it.</p>

<h2>Paid features</h2>
<p>Some features (premium roles, AI Store credits, bot-clone registration)
involve real payment. Prices and what they unlock are set by the server
admin or bot owner and are shown to you before you pay. Payments are
processed by a third-party payment provider; refunds, where offered, are
described in-product at the time of purchase (see /aistore flagbad and
/aistore refundqueue for the AI Store's refund flow).</p>

<h2>No warranty</h2>
<p>The bot is provided "as is," without warranty of any kind. Features may
change, be added, or be removed at any time. We are not liable for lost
data, lost currency/credits, moderation actions taken by the bot on a
server's behalf, or service interruptions.</p>

<h2>Account/token linking</h2>
<p>If you link an external account (e.g. a media server, Google Drive
folder, or your own Discord bot token) to the bot, you are responsible for
that account/token and can disconnect it at any time using the relevant
command.</p>

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
<li>Optional linked-account tokens (media server, cloud folder, your own
bot token) only if you choose to connect one, stored so the relevant
feature keeps working until you disconnect it.</li>
</ul>

<h2>What we don't do</h2>
<p>We don't sell your data. We don't read or store messages outside of
what a feature you're actively using needs in order to work.</p>

<h2>Third parties</h2>
<p>Depending on which features you use, data may be sent to: the AI
provider powering chat/image features, the payment processor handling paid
features, and (only if you connect one yourself) a media server, cloud
storage provider, or another Discord bot token's application.</p>

<h2>Data retention & deletion</h2>
<p>Feature-related data (balances, XP, warns, linked accounts) is kept
while you continue using the bot in a server. You can disconnect a linked
account at any time with its corresponding command. To request full
deletion of your data, contact us using the details below.</p>

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


async def _handle_terms():
    return 200, _page("Terms of Service", TERMS_HTML)


async def _handle_privacy():
    return 200, _page("Privacy Policy", PRIVACY_HTML)


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
