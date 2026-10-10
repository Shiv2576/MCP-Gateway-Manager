"""The gateway web app: one MCP endpoint, the admin API, health checks.

Run with:  uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass

from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import api
from . import db as store
from .upstream import Upstreams

log = logging.getLogger("gateway")
# Upstream URLs are logged at INFO by the HTTP library; keep the pod log quiet.
logging.getLogger("httpx2").setLevel(logging.WARNING)


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. The process should not start."""


@dataclass(frozen=True)
class Settings:
    database_path: str = "/data/gateway.db"
    # Only used to create the first admin when the users table is empty.
    admin_username: str = ""
    admin_password: str = ""
    call_timeout_s: float = 10
    list_timeout_s: float = 5
    refresh_interval_s: float = 30
    cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            database_path=env("DATABASE_PATH", cls.database_path),
            admin_username=env("ADMIN_USERNAME", "").strip(),
            admin_password=env("ADMIN_PASSWORD", ""),
            call_timeout_s=float(env("CALL_TIMEOUT_S", cls.call_timeout_s)),
            list_timeout_s=float(env("LIST_TIMEOUT_S", cls.list_timeout_s)),
            refresh_interval_s=float(env("REFRESH_INTERVAL_S", cls.refresh_interval_s)),
            cookie_secure=env("COOKIE_SECURE", "false").lower() == "true",
        )


class ApiKeyGuard:
    """/mcp needs a live API key. The key is looked up on every request, so a
    revoked key stops working on the very next call."""

    def __init__(self, app: ASGIApp, conn):
        self._app, self._conn = app, conn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            user = None
            if auth.lower().startswith("bearer "):
                user = api.user_for_api_key(self._conn, auth[7:].strip())
            if user is None:
                response = JSONResponse(
                    {"error": "a valid API key is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["user"] = user
        await self._app(scope, receive, send)


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or Settings.from_env()
    conn = store.connect(settings.database_path)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        if not (settings.admin_username and len(settings.admin_password) >= 10):
            raise ConfigError(
                "no users yet: set ADMIN_USERNAME and ADMIN_PASSWORD (10+ chars) to create the first admin"
            )
        store.create_user(conn, settings.admin_username, settings.admin_password, "admin")
        log.info("created first admin %s", settings.admin_username)

    upstreams = Upstreams(
        conn,
        call_timeout=settings.call_timeout_s,
        list_timeout=settings.list_timeout_s,
        refresh_interval=settings.refresh_interval_s,
    )

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=upstreams.list_tools())

    async def on_call_tool(ctx, params) -> CallToolResult:
        return await upstreams.call_tool(ctx.request.state.user, params.name, params.arguments)

    server = Server(
        "team-mcp-gateway",
        instructions=(
            "Tools from several MCP servers. Each tool is named <server>__<tool>; "
            "the part before the double underscore says which server runs it."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # No MCP sessions are kept, so restarts never strand a client.
        stateless_http=True,
        json_response=True,
        # In a cluster the Host header is the Service or Ingress name; the
        # API key check stands in for the SDK's localhost-only Host allowlist.
        host="0.0.0.0",
        custom_starlette_routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", healthz, methods=["GET"]),
            *api.routes,
        ],
    )
    app.state.db = conn
    app.state.settings = settings
    app.state.upstreams = upstreams

    sdk_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with sdk_lifespan(a):
            task = asyncio.create_task(upstreams.run_forever())
            try:
                yield
            finally:
                task.cancel()

    app.router.lifespan_context = lifespan
    return ApiKeyGuard(app, conn)
