"""Members' own upstream credentials, for gateway-server connections set to personal identity.

Stored encrypted with CREDENTIALS_KEY; only the gateway decrypts them, just
before a tool call. No endpoint ever returns a value.
"""

from __future__ import annotations

import collections
import time

from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import HTTPError, body, current_user, handler

VALUE_MAX = 4096


class Vault:
    """Encrypts members' upstream credentials at rest (Fernet: AES-128-CBC + HMAC)."""

    def __init__(self, key: str):
        self._f = Fernet(key.encode())  # ValueError when malformed

    def seal(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def open(self, token: str) -> str | None:
        """None when it cannot be decrypted, e.g. it was sealed with another key."""
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def _audit(conn, user: dict, action: str, server: str) -> None:
    """Who changed which credential and when; never the value."""
    conn.execute(
        "INSERT INTO audit_log (at, user_id, username, tool, server, ok, duration_ms) VALUES (?, ?, ?, ?, ?, 1, 0)",
        (time.time(), user["id"], user["username"], f"credential:{action}", server),
    )


def _server(request: Request) -> dict:
    row = request.app.state.db.execute(
        "SELECT id, name, personal_header FROM servers WHERE id = ?", (request.path_params["server_id"],)
    ).fetchone()
    if row is None:
        raise HTTPError(404, "no such server")
    return row


@handler
async def list_mine(request: Request) -> Response:
    """Servers that need this user's credential (a personal connection they have a tool
    on), plus any they already stored one for, so it can be removed."""
    user = current_user(request)
    conn = request.app.state.db
    gateways = collections.defaultdict(list)
    for r in conn.execute(
        "SELECT g.slug, gs.server_id FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id"
        " WHERE gs.identity = 'personal' ORDER BY g.slug"
    ):
        if r["server_id"] in store.allowed_tools(conn, user, r["slug"]):
            gateways[r["server_id"]].append(r["slug"])
    stored = {r["server_id"]: r["updated_at"] for r in conn.execute(
        "SELECT server_id, updated_at FROM user_credentials WHERE user_id = ?", (user["id"],))}
    rows = conn.execute(
        "SELECT id, name, personal_header FROM servers WHERE id = ANY(?) ORDER BY name", (list(set(gateways) | set(stored)),)
    )
    return JSONResponse([
        {"server_id": r["id"], "server": r["name"], "personal_header": r["personal_header"],
         "set": r["id"] in stored, "updated_at": stored.get(r["id"]), "gateways": gateways[r["id"]]}
        for r in rows
    ])


@handler
async def put_mine(request: Request) -> Response:
    user = current_user(request)
    value = (await body(request)).get("value")
    value = value.strip() if isinstance(value, str) else ""
    # The error never repeats the value.
    if not 0 < len(value) <= VALUE_MAX or "\r" in value or "\n" in value:
        raise HTTPError(400, f"value: 1-{VALUE_MAX} characters on one line")
    server = _server(request)
    if not server["personal_header"]:
        raise HTTPError(400, "this server does not take personal credentials")
    conn = request.app.state.db
    now = time.time()
    inserted = conn.execute(
        "INSERT INTO user_credentials (user_id, server_id, secret, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (user_id, server_id) DO UPDATE SET secret = EXCLUDED.secret, updated_at = EXCLUDED.updated_at"
        " RETURNING (xmax = 0) AS inserted",
        (user["id"], server["id"], request.app.state.vault.seal(value), now),
    ).fetchone()["inserted"]
    _audit(conn, user, "set" if inserted else "replace", server["name"])
    return JSONResponse({"set": True, "updated_at": now})


@handler
async def delete_mine(request: Request) -> Response:
    user = current_user(request)
    server = _server(request)
    conn = request.app.state.db
    cur = conn.execute("DELETE FROM user_credentials WHERE user_id = ? AND server_id = ?", (user["id"], server["id"]))
    if not cur.rowcount:
        raise HTTPError(404, "no credential stored for this server")
    _audit(conn, user, "delete", server["name"])
    return JSONResponse({"ok": True})


routes = [
    Route("/api/me/credentials", list_mine, methods=["GET"]),
    Route("/api/me/credentials/{server_id:int}", put_mine, methods=["PUT"]),
    Route("/api/me/credentials/{server_id:int}", delete_mine, methods=["DELETE"]),
]
