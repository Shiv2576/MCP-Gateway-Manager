"""The web app: the MCP endpoint, a token check in front of it, and health checks.

Run with:  uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

`--factory` makes uvicorn call create_app() at startup, so a missing setting
stops the process immediately instead of failing on the first request.
"""

from __future__ import annotations

import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .server import build_server
from .woo import PERSONAL_KEY, WooClient

log = logging.getLogger("woo-mcp")
# The HTTP library logs every outgoing URL at INFO. Ours carry the API key id
# and a signature in the query string, so keep those lines out of the pod log.
logging.getLogger("httpx2").setLevel(logging.WARNING)

# Paths the kubelet probes. They carry no data, so they need no token.
OPEN_PATHS = frozenset({"/healthz", "/readyz"})


class BearerTokenGuard:
    """Reject every request that carries neither the shared token nor a personal key.

    This is the lock on the back door. The NetworkPolicy already limits who can
    reach this pod; the token means that even a pod that can reach it still
    needs the credential the gateway holds.

    A personal call from the gateway carries X-Woo-Credentials: <key>:<secret>
    instead: that person's own WooCommerce key, used for every store request
    this MCP request makes, never the key in our env. It opens nothing new:
    whoever holds a valid key could call WooCommerce with it directly.
    """

    def __init__(self, app: ASGIApp, token: str):
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self._app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        personal = headers.get(b"x-woo-credentials")
        if personal is not None:
            key, sep, secret = personal.decode("latin-1").strip().partition(":")
            if not (sep and key and secret):
                # Never echo the header: it may be a real key with a typo.
                response = JSONResponse(
                    {"error": "X-Woo-Credentials must be <consumer_key>:<consumer_secret>"},
                    status_code=400,
                )
                await response(scope, receive, send)
                return
            token = PERSONAL_KEY.set((key, secret))
            try:
                await self._app(scope, receive, send)
            finally:
                PERSONAL_KEY.reset(token)
            return
        supplied = headers.get(b"authorization", b"")
        # compare_digest takes the same time whether the first or last byte is
        # wrong, so an attacker cannot guess the token one character at a time.
        if not hmac.compare_digest(supplied, self._expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def create_app(
    settings: Settings | None = None, woo: WooClient | None = None
) -> ASGIApp:
    settings = settings or Settings.from_env()
    woo = woo or WooClient(settings)
    server = build_server(woo, settings.store_timezone)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        """Liveness: is this process able to answer at all?"""
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> Response:
        """Readiness: can this pod take traffic?

        Deliberately does NOT call WooCommerce. If it did, a store outage would
        mark this pod unready and the gateway would see a connection failure
        instead of this server's clear "store did not answer" tool error.
        """
        return JSONResponse({"status": "ready", "auth_mode": settings.woo_auth_mode})

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # No server-side sessions: any replica can answer any request, so this
        # scales to several pods behind a plain Service with no sticky routing.
        stateless_http=True,
        json_response=True,
        # The SDK turns on a Host-header allowlist (localhost only) when host is
        # 127.0.0.1. In a cluster the Host is the Service name, so bind-all here
        # and rely on the NetworkPolicy plus the token instead.
        host="0.0.0.0",
    )
    log.info(
        "woo-mcp ready: store=%s auth=%s",
        settings.woo_internal_url,
        settings.woo_auth_mode,
    )
    return BearerTokenGuard(app, settings.mcp_auth_token)
