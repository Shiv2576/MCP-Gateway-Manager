"""Members' own upstream credentials, for gateway-server connections set to personal identity.

Stored encrypted with CREDENTIALS_KEY; only the gateway decrypts them, just
before a tool call. No endpoint ever returns a value.
"""

from __future__ import annotations

import base64
import collections
import logging
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import HTTPError, body, current_user, handler

VALUE_MAX = 4096


V2 = "v2:"
FERNET = "gAAAAA"  # every Fernet token starts so; a header value never does
log = logging.getLogger("gateway.credentials")


def _label(server_id: int, url: str, user_id: int | None) -> bytes:
    return f"server:{server_id}\nurl:{url}\nuser:{'' if user_id is None else user_id}".encode()


class Vault:
    """Upstream credentials at rest: AES-256-GCM, each value sealed with a label naming the
    server it's for (its id and URL) and, for a personal one, the member. A value copied to
    another row, or left in a row whose URL was changed, no longer opens: database write
    access alone can't send a stored secret anywhere it wasn't entered for.

    The AES key is derived from CREDENTIALS_KEY (the Fernet key the gateway always had), so
    no Secret changes. Fernet stays only to read values written before this format."""

    def __init__(self, key: str):
        self._f = Fernet(key.encode())  # ValueError when malformed
        raw = base64.urlsafe_b64decode(key.encode())
        self._gcm = AESGCM(HKDF(SHA256(), length=32, salt=None, info=b"team-mcp-gateway credentials v2").derive(raw))

    def seal(self, value: str, *, server_id: int, url: str, user_id: int | None = None) -> str:
        nonce = os.urandom(12)
        ct = self._gcm.encrypt(nonce, value.encode(), _label(server_id, url, user_id))
        return V2 + base64.urlsafe_b64encode(nonce + ct).decode()

    def open(self, token: str, *, server_id: int, url: str, user_id: int | None = None) -> str | None:
        """None when it can't be opened for this server (and member): another key, another
        row's value, a changed URL, or the old unlabeled format, which is never accepted here."""
        if not token.startswith(V2):
            return None
        try:
            blob = base64.urlsafe_b64decode(token[len(V2):].encode())
            return self._gcm.decrypt(blob[:12], blob[12:], _label(server_id, url, user_id)).decode()
        except (InvalidTag, ValueError):
            return None

    def open_legacy(self, token: str) -> str | None:
        """A Fernet value from before labels; only upgrade_credentials uses it."""
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def upgrade_credentials(conn, vault: Vault) -> None:
    """On the control plane's start: seal every shared and personal credential still in the old
    format (Fernet, or plaintext from before encryption) with its label. Each old Fernet value
    is first copied into credential_backup_v1, so a rollback past this release can put them back
    (docs/gitops.md, Rollback). Values that already use the new format are left alone."""
    now = time.time()
    with conn.transaction() as tx:
        for row in tx.execute("SELECT id, url, auth_value FROM servers WHERE auth_value IS NOT NULL"
                              " AND left(auth_value, 3) <> 'v2:' FOR UPDATE").fetchall():
            old = row["auth_value"]
            plain = vault.open_legacy(old) if old.startswith(FERNET) else old
            if plain is None:
                log.warning("server %s: its credential can't be read with this CREDENTIALS_KEY; left as is", row["id"])
                continue
            if old.startswith(FERNET):
                tx.execute("INSERT INTO credential_backup_v1 (source, server_id, user_id, value, saved_at)"
                           " VALUES ('servers', ?, NULL, ?, ?)", (row["id"], old, now))
            tx.execute("UPDATE servers SET auth_value = ? WHERE id = ?",
                       (vault.seal(plain, server_id=row["id"], url=row["url"]), row["id"]))
        for row in tx.execute("SELECT uc.user_id, uc.server_id, uc.secret, s.url FROM user_credentials uc"
                              " JOIN servers s ON s.id = uc.server_id WHERE left(uc.secret, 3) <> 'v2:'"
                              " FOR UPDATE OF uc").fetchall():
            plain = vault.open_legacy(row["secret"])
            if plain is None:
                log.warning("a personal credential for server %s can't be read; left as is", row["server_id"])
                continue
            tx.execute("INSERT INTO credential_backup_v1 (source, server_id, user_id, value, saved_at)"
                       " VALUES ('user_credentials', ?, ?, ?, ?)", (row["server_id"], row["user_id"], row["secret"], now))
            tx.execute("UPDATE user_credentials SET secret = ? WHERE user_id = ? AND server_id = ?",
                       (vault.seal(plain, server_id=row["server_id"], url=row["url"], user_id=row["user_id"]),
                        row["user_id"], row["server_id"]))


def _audit(conn, user: dict, action: str, server: str) -> None:
    """Who changed which credential and when; never the value."""
    conn.execute(
        "INSERT INTO audit_log (at, user_id, username, tool, server, ok, duration_ms, outcome)"
        " VALUES (?, ?, ?, ?, ?, 1, 0, 'ok')",
        (time.time(), user["id"], user["username"], f"credential:{action}", server),
    )


def _server(request: Request) -> dict:
    row = request.app.state.db.execute(
        "SELECT id, name, url, personal_header FROM servers WHERE id = ?", (request.path_params["server_id"],)
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
        (user["id"], server["id"],
         request.app.state.vault.seal(value, server_id=server["id"], url=server["url"], user_id=user["id"]), now),
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
