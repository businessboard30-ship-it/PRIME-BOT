# FULL PATH: PRIME-BOT-main/api_server.py

"""
Standalone dashboard/webhook/cron server for Railway.

The files under api/ were written as Vercel Python serverless functions —
each is a self-contained `class handler(BaseHTTPRequestHandler)` with no
custom __init__. Vercel instantiates one per request behind its own routing.
This script does the same thing ourselves: one real long-running HTTP
server (what Railway needs) that looks at the request path and delegates
to the matching handler class's do_GET/do_POST/do_OPTIONS, unbound-method
style. This works because those methods only touch attributes that any
BaseHTTPRequestHandler instance already has (self.path, self.headers,
self.rfile, self.wfile, self.send_response, ...) — nothing module-specific
is stashed on self elsewhere.

Run with: python api_server.py
Railway sets $PORT automatically; do not hardcode a port.
"""
import os
import importlib
import logging
import threading
import asyncio
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_server")

# ── Shared event loop (fixes the EMAXCONN connection-pool leak) ────────────
# Every api/*.py handler was written as a standalone Vercel-style serverless
# function that calls `asyncio.run(...)` once per invocation. That's fine on
# Vercel (one process per invocation, torn down entirely afterward) but this
# script serves requests with a ThreadingHTTPServer instead — a persistent
# process handling MANY requests over its lifetime. asyncio.run() creates a
# brand-new event loop and destroys it at the end of every single request.
#
# database.get_pool() caches its asyncpg connection pool as a module-level
# singleton keyed to "the currently running loop" (see its docstring) —
# reusing a pool from a closed loop is unsafe, since asyncpg's connections
# are physically bound to the loop they were opened on. So on every request
# whose loop differs from the pool's loop, get_pool() drops the old pool
# reference and opens a fresh one. But asyncio.run() has already destroyed
# that old loop by the time the NEXT request notices it's stale, so there's
# no safe way to gracefully close those old connections at that point — they
# were simply abandoned. Under a ThreadingHTTPServer taking one request after
# another (cron hits, webhooks, dashboard calls), this leaked a few
# server-side Postgres connections per request until the database's
# connection cap was hit, at which point EVERY route started 500ing with
# "(EMAXCONN) max client connections reached" — exactly what took down
# /api/cron_discord_owner_broadcast and /api/cron_discord_announcements.
#
# The real fix: give this whole process ONE persistent event loop, exactly
# like the always-on bot process (discord_bot/bot.py) already has, so
# get_pool()'s singleton is actually reused across requests instead of
# recreated (and leaked) every time. We do this by monkeypatching
# asyncio.run at import time — every api/*.py handler's internal
# `asyncio.run(...)` call transparently becomes "run this coroutine on our
# one shared loop and block until it's done" instead of "spin up and tear
# down a whole new loop". No changes needed to any individual handler file.
_shared_loop = asyncio.new_event_loop()
_shared_loop_thread = threading.Thread(
    target=_shared_loop.run_forever, name="api-server-loop", daemon=True
)
_shared_loop_thread.start()


def _run_on_shared_loop(coro, *, debug=None):
    """Drop-in replacement for asyncio.run(), submitting to the one
    persistent loop above instead of creating/destroying a new one."""
    future = asyncio.run_coroutine_threadsafe(coro, _shared_loop)
    return future.result()


asyncio.run = _run_on_shared_loop

# URL path -> module under api/ that defines `class handler` (Vercel
# serverless convention). Use "module:ClassName" instead of a bare module
# path when the module defines a differently-named or multiple handler
# classes (e.g. api.legal_pages's TermsHandler/PrivacyHandler).
ROUTES = {
    "/api/paystack_webhook": "api.paystack_webhook",
    "/api/vote_webhook": "api.vote_webhook",
    "/api/discord_dashboard": "api.discord_dashboard",
    "/api/server_listings": "api.server_listings",
    "/api/cron_discord_announcements": "api.cron_discord_announcements",
    "/api/cron_discord_owner_broadcast": "api.cron_discord_owner_broadcast",
    "/api/cron_expire_monetization": "api.cron_expire_monetization",
    "/api/cron_renew_yandex_search": "api.cron_renew_yandex_search",
    "/api/oauth_gdrive": "api.oauth_gdrive",
    "/api/discover_oauth_join": "api.discover_oauth_join",
    "/api/bump_oauth": "api.bump_oauth",
    "/api/server_listing_vote_oauth": "api.server_listing_vote_oauth",
    "/api/discord_login_oauth": "api.discord_login_oauth",
    # Discord app-verification requires real, permanently reachable ToS/
    # Privacy URLs — these were written in api/legal_pages.py but never
    # wired into the dispatcher (and its class names don't match the
    # `handler` convention above), so they were unreachable until now.
    "/terms": "api.legal_pages:TermsHandler",
    "/privacy": "api.legal_pages:PrivacyHandler",
}

_handler_classes = {}


def _get_handler_class(path):
    route = ROUTES.get(path)
    if route is None:
        return None
    if route not in _handler_classes:
        module_name, _, class_name = route.partition(":")
        mod = importlib.import_module(module_name)
        _handler_classes[route] = getattr(mod, class_name) if class_name else mod.handler
    return _handler_classes[route]


class Dispatcher(BaseHTTPRequestHandler):
    def _dispatch(self, method):
        path = urlparse(self.path).path
        if path == "/" or path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        cls = _get_handler_class(path)
        if cls is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        fn = getattr(cls, f"do_{method}", None)
        if fn is None:
            self.send_response(405)
            self.end_headers()
            return
        # Route modules are written as self-contained `handler` classes
        # (Vercel serverless style) and some define their own instance
        # helpers (e.g. discord_dashboard.py's _json/_cors) beyond what
        # BaseHTTPRequestHandler provides. Since we're calling `fn`
        # unbound-method style with THIS Dispatcher as self, any such
        # helper must be bound onto self too, or a call like self._json(...)
        # inside the handler raises AttributeError. Bind every callable
        # the handler class defines (skipping dunders) so it behaves as if
        # self really were an instance of `cls`.
        for name, attr in vars(cls).items():
            if callable(attr) and not name.startswith("__"):
                setattr(self, name, types.MethodType(attr, self))
        fn = getattr(self, f"do_{method}")
        try:
            fn()
        except Exception:
            logger.exception("Unhandled error in %s for %s", cls.__module__, path)
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Internal server error"}')
            except Exception:
                pass  # headers already sent

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), Dispatcher)
    logger.info("Dashboard/webhook server listening on 0.0.0.0:%d", port)
    logger.info("Routes: %s", ", ".join(ROUTES.keys()))
    server.serve_forever()
