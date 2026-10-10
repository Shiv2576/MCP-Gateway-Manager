"""The gateway web app: one MCP endpoint, the admin API, health checks.

Run with:  uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field

from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, ListToolsResult
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import api, chat, oauth, stores as store_api
from . import db as store
from .kube import Kube
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
    calls_per_minute: int = 60
    cookie_secure: bool = False
    # Chat agent. Any OpenAI-compatible chat completions URL works.
    openrouter_api_key: str = field(default="", repr=False)
    llm_model: str = "openai/gpt-4.1-mini"
    llm_url: str = "https://openrouter.ai/api/v1/chat/completions"
    # Namespace holding Store resources; empty turns store provisioning off.
    stores_namespace: str = ""
    # The gateway's public https URL. Set, it turns on OAuth for /mcp so
    # clients can connect with a browser login; API keys work either way.
    public_url: str = ""
    # Where OAuth codes may be sent, besides loopback URLs. Claude's callback.
    oauth_redirect_uris: tuple[str, ...] = (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )

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
            calls_per_minute=int(env("CALLS_PER_MINUTE", cls.calls_per_minute)),
            cookie_secure=env("COOKIE_SECURE", "false").lower() == "true",
            openrouter_api_key=env("OPENROUTER_API_KEY", "").strip(),
            llm_model=env("LLM_MODEL", cls.llm_model),
            llm_url=env("LLM_URL", cls.llm_url),
            stores_namespace=env("STORES_NAMESPACE", ""),
            public_url=env("PUBLIC_URL", "").strip().rstrip("/"),
            oauth_redirect_uris=tuple(u for u in env("OAUTH_REDIRECT_URIS", "").split(",") if u.strip())
            or cls.oauth_redirect_uris,
        )


class ApiKeyGuard:
    """/mcp needs a live API key or OAuth access token. Both are looked up on
    every request, so a revoked one stops working on the very next call."""

    def __init__(self, app: ASGIApp, conn, public_url: str = ""):
        self._app, self._conn = app, conn
        self._resource = f"{public_url}/mcp" if public_url else ""
        challenge = "Bearer"
        if public_url:
            # How an OAuth client finds where to log in (RFC 9728).
            challenge += f' resource_metadata="{build_resource_metadata_url(self._resource)}"'
        self._challenge = challenge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            user = None
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
                user = api.user_for_api_key(self._conn, token)
                if user is None and self._resource:
                    user = oauth.user_for_oauth_token(self._conn, token, self._resource)
            if user is None:
                response = JSONResponse(
                    {"error": "a valid API key or OAuth token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": self._challenge},
                )
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["user"] = user
        await self._app(scope, receive, send)


def create_app(settings: Settings | None = None, stores=None) -> ASGIApp:
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
        calls_per_minute=settings.calls_per_minute,
    )

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=upstreams.list_tools(ctx.request.state.user))

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

    auth = provider = None
    if settings.public_url:
        auth = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # ApiKeyGuard checks the token's resource itself.
            validate_token_resource=False,
        )
        provider = oauth.Provider(conn, settings.public_url, settings.oauth_redirect_uris)

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
        # No token_verifier: the SDK serves the OAuth endpoints, ApiKeyGuard guards /mcp.
        auth=auth,
        auth_server_provider=provider,
        custom_starlette_routes=[
            *(oauth.routes if provider else []),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", healthz, methods=["GET"]),
            *api.routes,
            *store_api.routes,
            Route("/api/chat", chat.chat, methods=["POST"]),
        ],
    )
    app.state.db = conn
    app.state.settings = settings
    app.state.upstreams = upstreams
    app.state.login_throttle = api.LoginThrottle()
    app.state.stores = stores if stores is not None else (
        Kube(settings.stores_namespace) if settings.stores_namespace else None
    )

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
    return ApiKeyGuard(app, conn, settings.public_url)
