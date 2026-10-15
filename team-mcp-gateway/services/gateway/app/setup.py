"""First-run setup: name the organization and create its first admin, once.

Needs the setup token from the gateway-admin Secret, so the first person to
reach a fresh install cannot take it over. The org row's fixed id makes two
racing setups (even on different pods) end with exactly one winner.
"""

from __future__ import annotations

import hmac
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import USERNAME, HTTPError, LoginThrottle, _str, body, handler, session_response

SEED_SERVER = "woo-mcp"
log = logging.getLogger("gateway.setup")


async def _stores_exist(request: Request) -> bool:
    """Stores already deployed bring their own servers; then nothing is seeded.
    Unknown (the cluster cannot be asked) counts as yes."""
    if request.app.state.db.execute("SELECT 1 FROM servers WHERE store IS NOT NULL").fetchone():
        return True
    stores = request.app.state.stores
    if stores is None:
        return False
    try:
        return bool(await stores.list())
    except Exception:
        log.warning("could not list stores; not seeding %s", SEED_SERVER, exc_info=True)
        return True
# Not a valid username, so it never shares a throttle bucket with a real login.
THROTTLE_KEY = "@setup"


@handler
async def status(request: Request) -> Response:
    row = request.app.state.db.execute("SELECT name FROM org").fetchone()
    return JSONResponse({"needed": row is None, "org": row["name"] if row else None})


@handler
async def setup(request: Request) -> Response:
    conn = request.app.state.db
    if conn.execute("SELECT 1 FROM org").fetchone():
        raise HTTPError(409, "already set up")
    throttle: LoginThrottle = request.app.state.login_throttle
    if throttle.blocked(THROTTLE_KEY):
        raise HTTPError(429, "too many wrong setup tokens; try again in 15 minutes")
    data = await body(request)
    token, expected = data.get("setup_token"), request.app.state.settings.setup_token
    if not (isinstance(token, str) and expected and hmac.compare_digest(token.encode(), expected.encode())):
        throttle.failed(THROTTLE_KEY)
        raise HTTPError(403, "wrong setup token")
    throttle.succeeded(THROTTLE_KEY)

    org_name, username, password = _str(data, "org_name"), _str(data, "username"), _str(data, "password")
    if len(org_name) > 80:
        raise HTTPError(400, "organization name: at most 80 characters")
    if not USERNAME.match(username):
        raise HTTPError(400, "username: 2-32 of a-z 0-9 . _ -")
    if len(password) < 10:
        raise HTTPError(400, "password must be at least 10 characters")
    settings, seed = request.app.state.settings, None
    seeding = bool(settings.seed_server_url) and not await _stores_exist(request)
    try:
        with conn.transaction() as tx:
            tx.execute("INSERT INTO org (id, name, created_at) VALUES (1, ?, ?)", (org_name, time.time()))
            uid = store.create_user(tx, username, password, "admin")
            if seeding:
                # Same transaction: the admin's first login already lists it.
                token = settings.seed_server_token
                seed = tx.execute(
                    "INSERT INTO servers (name, url, auth_header, auth_value, enabled, created_at)"
                    " VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT (name) DO NOTHING RETURNING id",
                    (SEED_SERVER, settings.seed_server_url, "Authorization" if token else None,
                     request.app.state.vault.seal("Bearer " + token) if token else None, time.time()),
                ).fetchone()
    except store.IntegrityError:
        raise HTTPError(409, "already set up") from None
    request.app.state.org_ready = True
    if seed:
        request.app.state.upstreams.refresh_soon(seed["id"])
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return session_response(request, user, status=201)


routes = [
    Route("/api/setup", status, methods=["GET"]),
    Route("/api/setup", setup, methods=["POST"]),
]
