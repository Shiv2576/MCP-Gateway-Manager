"""The gateway web app, in one of two modes: control (the /api behind the dashboard) or edge (/mcp for one gateway's servers).

Run with:  uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import alerts, api, approvals, chat, credentials, oauth, pii, pii_api, setup, teams
from . import db as store
from . import gateways as gateway_api
from . import hosted as hosted_api
from . import stores as store_api
from .kube import Kube, StoreMetrics, TokenReview
from .upstream import Upstreams

log = logging.getLogger("gateway")
# Upstream URLs are logged at INFO by the HTTP library; keep the pod log quiet.
logging.getLogger("httpx2").setLevel(logging.WARNING)


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. The process should not start."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="", repr=False)
    mode: str = (
        "control"  # control: the /api behind the dashboard; edge: /mcp for one gateway
    )
    gateway_slug: str = ""  # edge only: which gateway's attached servers to serve
    # Fernet key that encrypts members' own upstream credentials. Both modes.
    credentials_key: str = field(default="", repr=False)
    # Needed until first-run setup is done; then unused.
    setup_token: str = field(default="", repr=False)
    call_timeout_s: float = 10
    list_timeout_s: float = 5
    refresh_interval_s: float = 30
    calls_per_minute: int = 20
    # Items/hour per member for a write tool no team budgeted. 0: every such call needs approval.
    default_items_per_hour: int = 20
    # Approved bulk jobs run longer than a normal call.
    approval_call_timeout_s: float = 120
    cookie_secure: bool = False
    # Chat agent. Any OpenAI-compatible chat completions URL works.
    openrouter_api_key: str = field(default="", repr=False)
    llm_model: str = "openai/gpt-4.1-mini"
    # Cheap model for the scope check run before every chat message.
    llm_guard_model: str = "openai/gpt-4.1-nano"
    llm_url: str = "https://openrouter.ai/api/v1/chat/completions"
    # Namespace holding Store resources; empty turns store provisioning off.
    stores_namespace: str = ""
    # The store operator's ServiceAccount (system:serviceaccount:<ns>:<name>). Its
    # projected token, checked with TokenReview, may use /api/servers. Empty: off.
    operator_service_account: str = ""
    # The gateway's public https URL. Set, it turns on OAuth for /mcp so
    # clients can connect with a browser login; API keys work either way.
    public_url: str = ""
    # Stores are <name>.<base_domain>, edges <slug>.gw.<base_domain>, over url_scheme.
    base_domain: str = "localhost"
    url_scheme: str = "http"
    # Tailscale tailnet DNS name (e.g. tail1234.ts.net). Set, a gateway created
    # without a public URL gets https://<slug>-gw.<tailnet>, published by Funnel.
    tailnet_domain: str = ""
    # Hosts an admin may add as upstreams although they resolve to private or
    # cluster addresses (e.g. demo servers in this namespace). Exact names only.
    upstream_allow_hosts: tuple[str, ...] = ()
    # Control only: the store's WooCommerce MCP server, registered by first-run
    # setup so it is the first server the admin sees. Empty: no seed.
    seed_server_url: str = ""
    seed_server_token: str = field(default="", repr=False)
    # Where OAuth codes may be sent, besides loopback URLs. Claude's callback.
    oauth_redirect_uris: tuple[str, ...] = (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            database_url=env("DATABASE_URL", ""),
            mode=env("GATEWAY_MODE", cls.mode).strip(),
            gateway_slug=env("GATEWAY_SLUG", "").strip(),
            setup_token=env("SETUP_TOKEN", "").strip(),
            credentials_key=env("CREDENTIALS_KEY", "").strip(),
            call_timeout_s=float(env("CALL_TIMEOUT_S", cls.call_timeout_s)),
            list_timeout_s=float(env("LIST_TIMEOUT_S", cls.list_timeout_s)),
            refresh_interval_s=float(env("REFRESH_INTERVAL_S", cls.refresh_interval_s)),
            calls_per_minute=int(env("CALLS_PER_MINUTE", cls.calls_per_minute)),
            default_items_per_hour=int(
                env("DEFAULT_ITEMS_PER_HOUR", cls.default_items_per_hour)
            ),
            approval_call_timeout_s=float(
                env("APPROVAL_CALL_TIMEOUT_S", cls.approval_call_timeout_s)
            ),
            cookie_secure=env("COOKIE_SECURE", "false").lower() == "true",
            openrouter_api_key=env("OPENROUTER_API_KEY", "").strip(),
            llm_model=env("LLM_MODEL", cls.llm_model),
            llm_guard_model=env("LLM_GUARD_MODEL", cls.llm_guard_model),
            llm_url=env("LLM_URL", cls.llm_url),
            stores_namespace=env("STORES_NAMESPACE", ""),
            operator_service_account=env("OPERATOR_SERVICE_ACCOUNT", "").strip(),
            public_url=env("PUBLIC_URL", "").strip().rstrip("/"),
            base_domain=env("BASE_DOMAIN", cls.base_domain).strip(),
            url_scheme=env("URL_SCHEME", cls.url_scheme).strip(),
            tailnet_domain=env("TAILNET_DOMAIN", "").strip().strip("."),
            upstream_allow_hosts=tuple(
                h.strip().lower() for h in env("UPSTREAM_ALLOW_HOSTS", "").split(",") if h.strip()
            ),
            seed_server_url=env("SEED_SERVER_URL", "").strip(),
            seed_server_token=env("SEED_SERVER_TOKEN", "").strip(),
            oauth_redirect_uris=tuple(
                u for u in env("OAUTH_REDIRECT_URIS", "").split(",") if u.strip()
            )
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
            challenge += (
                f' resource_metadata="{build_resource_metadata_url(self._resource)}"'
            )
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


def _edge_app(settings: Settings, conn, upstreams: Upstreams, health: list[Route]):
    """/mcp for one gateway's attached servers, plus OAuth when PUBLIC_URL is set."""

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(
            tools=upstreams.list_tools(ctx.request.state.user, settings.gateway_slug)
        )

    async def on_call_tool(ctx, params) -> CallToolResult:
        info = getattr(
            ctx.session.client_params, "client_info", None
        )  # None on stateless requests without initialize
        return await upstreams.call_tool(
            ctx.request.state.user,
            settings.gateway_slug,
            params.name,
            params.arguments,
            client=info and info.name,
        )

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
        provider = oauth.Provider(
            conn, settings.public_url, settings.oauth_redirect_uris
        )

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        # No MCP sessions are kept: any replica answers any request, no sticky routing.
        # The gateway needs no session features (no server-to-client requests, no resumption).
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=_transport_security(settings),
        # No token_verifier: the SDK serves the OAuth endpoints, ApiKeyGuard guards /mcp.
        auth=auth,
        auth_server_provider=provider,
        custom_starlette_routes=[*(oauth.routes if provider else []), *health],
    )


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection: only this edge's own hostnames. The SDK's
    "host:*" matches only a Host header with a port, so list both forms."""
    own = f"{settings.gateway_slug}.gw.{settings.base_domain}"
    names = [own, "localhost", "127.0.0.1"]  # Ingress; port-forward and tests
    if settings.public_url:
        names.append(urlsplit(settings.public_url).hostname)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h for n in names for h in (n, f"{n}:*")],
        allowed_origins=[o for n in names for o in (f"http://{n}", f"http://{n}:*")]
        + [f"{settings.url_scheme}://{own}"]
        + ([settings.public_url] if settings.public_url else []),
    )


def create_app(
    settings: Settings | None = None, stores=None, gateways=None, hosted=None, token_review=None,
    store_metrics=None,
) -> ASGIApp:
    settings = settings or Settings.from_env()
    if settings.mode not in ("control", "edge"):
        raise ConfigError("GATEWAY_MODE must be control or edge")
    edge = settings.mode == "edge"
    if edge and not settings.gateway_slug:
        raise ConfigError("an edge needs GATEWAY_SLUG")
    if not settings.database_url:
        raise ConfigError("DATABASE_URL is required")
    try:
        vault = credentials.Vault(settings.credentials_key)
    except ValueError:
        raise ConfigError(
            "CREDENTIALS_KEY must be a Fernet key (32 url-safe base64 bytes)"
        ) from None
    conn = store.connect(settings.database_url, create_schema=not edge)
    if not edge:
        pii.seed_defaults(conn)
    if (
        not edge
        and conn.execute("SELECT 1 FROM org").fetchone() is None
        and len(settings.setup_token) < 16
    ):
        raise ConfigError(
            "not set up yet: set SETUP_TOKEN (16+ chars) and finish setup in the dashboard"
        )
    if (
        not edge
    ):  # edges only read; the control plane owns the schema and its migrations
        credentials.seal_shared_credentials(conn, vault)

    upstreams = Upstreams(
        conn,
        call_timeout=settings.call_timeout_s,
        list_timeout=settings.list_timeout_s,
        refresh_interval=settings.refresh_interval_s,
        calls_per_minute=settings.calls_per_minute,
        default_items_per_hour=settings.default_items_per_hour,
        gateway_slug=settings.gateway_slug if edge else None,
        vault=vault,
        approval_timeout=settings.approval_call_timeout_s,
    )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_: Request) -> Response:
        # An edge is ready only while its gateway exists; both need the database.
        try:
            if edge:
                ok = (
                    conn.execute(
                        "SELECT 1 FROM gateways WHERE slug = ?",
                        (settings.gateway_slug,),
                    ).fetchone()
                    is not None
                )
            else:
                ok = conn.execute("SELECT 1").fetchone() is not None
        except Exception:
            ok = False
        return JSONResponse(
            {"status": "ok" if ok else "not ready"}, status_code=200 if ok else 503
        )

    health = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
    ]
    if edge:
        app = _edge_app(settings, conn, upstreams, health)
    else:
        app = Starlette(
            routes=[
                *health,
                *api.routes,
                *setup.routes,
                *store_api.routes,
                *hosted_api.routes,
                *gateway_api.routes,
                *teams.routes,
                *credentials.routes,
                *pii_api.routes,
                *approvals.routes,
                *alerts.routes,
                Route("/api/chat", chat.chat, methods=["POST"]),
            ]
        )
    app.state.db = conn
    app.state.settings = settings
    app.state.upstreams = upstreams
    app.state.vault = vault
    app.state.login_throttle = api.LoginThrottle()
    app.state.stores = (
        stores
        if stores is not None
        else (Kube(settings.stores_namespace) if settings.stores_namespace else None)
    )
    app.state.gateways = (
        gateways
        if gateways is not None
        else (
            Kube(settings.stores_namespace, "mcpgateways")
            if settings.stores_namespace
            else None
        )
    )
    app.state.store_metrics = store_metrics or (StoreMetrics() if settings.stores_namespace else None)
    app.state.token_review = token_review or (
        TokenReview() if settings.operator_service_account and not edge else None
    )
    app.state.hosted = (
        hosted
        if hosted is not None
        else (
            Kube(settings.stores_namespace, "hostedservers")
            if settings.stores_namespace
            else None
        )
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
                conn.close()

    app.router.lifespan_context = lifespan
    return ApiKeyGuard(app, conn, settings.public_url) if edge else app
