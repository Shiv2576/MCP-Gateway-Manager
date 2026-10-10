"""The admin REST API: login, users, API keys, upstream servers, audit log.

Browsers log in and get an HttpOnly session cookie. Scripts and MCP clients
use an API key as a bearer token. Every response is built from an explicit
list of fields, so an upstream credential can never leak into one by accident.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .upstream import Status, Upstreams

SESSION_COOKIE = "gw_session"
SESSION_TTL_S = 12 * 3600
SERVER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
USERNAME = re.compile(r"^[a-z0-9._-]{2,32}$")


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def user_for_api_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.* FROM api_keys k JOIN users u ON u.id = k.user_id"
        " WHERE k.key_hash = ? AND k.revoked_at IS NULL",
        (store.sha256(key),),
    ).fetchone()


def current_user(request: Request) -> sqlite3.Row:
    conn = request.app.state.db
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = user_for_api_key(conn, auth[7:].strip())
    else:
        token = request.cookies.get(SESSION_COOKIE, "")
        user = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires_at > ?",
            (store.sha256(token), time.time()),
        ).fetchone()
    if user is None:
        raise HTTPError(401, "not logged in")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPError(403, "admins only")
    return user


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPError(400, "body must be JSON") from None
    if not isinstance(data, dict):
        raise HTTPError(400, "body must be a JSON object")
    return data


def _str(data: dict, key: str, *, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPError(400, f"'{key}' must be a non-empty string")
    return value.strip()


def handler(fn):
    """Turn HTTPError into a JSON error response."""

    async def wrapped(request: Request) -> Response:
        try:
            return await fn(request)
        except HTTPError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        except sqlite3.IntegrityError:
            return JSONResponse({"error": "already exists"}, status_code=409)

    return wrapped


# ------------------------------------------------------------------- session


@handler
async def login(request: Request) -> Response:
    data = await body(request)
    username, password = _str(data, "username"), _str(data, "password")
    conn = request.app.state.db
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    ok = store.verify_password(password, user["password_hash"] if user else store.DUMMY_HASH)
    if not (user and ok):
        raise HTTPError(401, "wrong username or password")
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (store.sha256(token), user["id"], time.time() + SESSION_TTL_S),
    )
    response = JSONResponse(_user(user))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="strict",
        secure=request.app.state.settings.cookie_secure,
    )
    return response


@handler
async def logout(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    request.app.state.db.execute("DELETE FROM sessions WHERE token_hash = ?", (store.sha256(token),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@handler
async def me(request: Request) -> Response:
    return JSONResponse(_user(current_user(request)))


def _user(u: sqlite3.Row) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"]}


# --------------------------------------------------------------------- users


@handler
async def list_users(request: Request) -> Response:
    require_admin(request)
    rows = request.app.state.db.execute("SELECT * FROM users ORDER BY id").fetchall()
    return JSONResponse([_user(u) for u in rows])


@handler
async def create_user(request: Request) -> Response:
    require_admin(request)
    data = await body(request)
    username, password = _str(data, "username"), _str(data, "password")
    role = data.get("role", "member")
    if not USERNAME.match(username):
        raise HTTPError(400, "username: 2-32 of a-z 0-9 . _ -")
    if len(password) < 10:
        raise HTTPError(400, "password must be at least 10 characters")
    if role not in ("admin", "member"):
        raise HTTPError(400, "role must be admin or member")
    uid = store.create_user(request.app.state.db, username, password, role)
    return JSONResponse({"id": uid, "username": username, "role": role}, status_code=201)


# ------------------------------------------------------------------ API keys


@handler
async def list_keys(request: Request) -> Response:
    user = current_user(request)
    sql = (
        "SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id = k.user_id"
        + ("" if user["role"] == "admin" else " WHERE k.user_id = ?")
        + " ORDER BY k.id"
    )
    args = () if user["role"] == "admin" else (user["id"],)
    rows = request.app.state.db.execute(sql, args).fetchall()
    return JSONResponse(
        [
            {
                "id": k["id"],
                "name": k["name"],
                "username": k["username"],
                "prefix": k["prefix"],
                "created_at": k["created_at"],
                "revoked_at": k["revoked_at"],
            }
            for k in rows
        ]
    )


@handler
async def create_key(request: Request) -> Response:
    user = current_user(request)
    name = _str(await body(request), "name")
    key = "mcpg_" + secrets.token_urlsafe(32)
    cur = request.app.state.db.execute(
        "INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], name, store.sha256(key), key[:12], time.time()),
    )
    # The only time the key is ever shown. Only its hash is kept.
    return JSONResponse({"id": cur.lastrowid, "name": name, "key": key}, status_code=201)


@handler
async def revoke_key(request: Request) -> Response:
    user = current_user(request)
    key_id = request.path_params["id"]
    conn = request.app.state.db
    key = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if key is None or (user["role"] != "admin" and key["user_id"] != user["id"]):
        raise HTTPError(404, "no such key")
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (time.time(), key_id),
    )
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------- servers


def _server(row: sqlite3.Row, status: Status | None) -> dict:
    status = status or Status(state="disabled" if not row["enabled"] else "pending")
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        # Which header carries the credential, never its value.
        "auth_header": row["auth_header"],
        "has_credential": bool(row["auth_value"]),
        "status": status.state,
        "error": status.error,
        "checked_at": status.checked_at,
        "tools": [{"name": t.name, "description": t.description} for t in status.tools],
    }


def _auth_fields(data: dict) -> tuple[str | None, str | None]:
    """{"type": "none"} | {"type": "bearer", "token": ...} | {"type": "header", "name": ..., "value": ...}"""
    auth = data.get("auth") or {"type": "none"}
    if not isinstance(auth, dict):
        raise HTTPError(400, "'auth' must be an object")
    kind = auth.get("type")
    if kind == "none":
        return None, None
    if kind == "bearer":
        return "Authorization", "Bearer " + _str(auth, "token")
    if kind == "header":
        name, value = _str(auth, "name"), _str(auth, "value")
        if not HEADER_NAME.match(name):
            raise HTTPError(400, "auth.name is not a valid header name")
        if any(c in value for c in "\r\n"):
            raise HTTPError(400, "auth.value must be one line")
        return name, value
    raise HTTPError(400, "auth.type must be none, bearer or header")


def _url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPError(400, "url must be an http(s) URL")
    return value


@handler
async def list_servers(request: Request) -> Response:
    current_user(request)
    ups: Upstreams = request.app.state.upstreams
    rows = request.app.state.db.execute("SELECT * FROM servers ORDER BY name").fetchall()
    return JSONResponse([_server(r, ups.status.get(r["id"])) for r in rows])


@handler
async def create_server(request: Request) -> Response:
    require_admin(request)
    data = await body(request)
    name, url = _str(data, "name"), _url(_str(data, "url"))
    if not SERVER_NAME.match(name):
        raise HTTPError(400, "name: 1-32 of a-z 0-9 and -, used as the tool prefix")
    header, value = _auth_fields(data)
    conn = request.app.state.db
    cur = conn.execute(
        "INSERT INTO servers (name, url, auth_header, auth_value, enabled, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, url, header, value, 1 if data.get("enabled", True) else 0, time.time()),
    )
    request.app.state.upstreams.refresh_soon(cur.lastrowid)
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (cur.lastrowid,)).fetchone()
    return JSONResponse(_server(row, None), status_code=201)


@handler
async def update_server(request: Request) -> Response:
    """Change url, enabled or auth. Leaving "auth" out keeps the stored credential.

    The name is fixed: it is the prefix of every tool name clients already know.
    """
    require_admin(request)
    data = await body(request)
    conn = request.app.state.db
    sid = request.path_params["id"]
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise HTTPError(404, "no such server")
    if "name" in data and data["name"] != row["name"]:
        raise HTTPError(400, "name cannot be changed; remove and re-add the server")
    url = _url(_str(data, "url")) if "url" in data else row["url"]
    enabled = (1 if data["enabled"] else 0) if "enabled" in data else row["enabled"]
    header, value = _auth_fields(data) if "auth" in data else (row["auth_header"], row["auth_value"])
    conn.execute(
        "UPDATE servers SET url = ?, enabled = ?, auth_header = ?, auth_value = ? WHERE id = ?",
        (url, enabled, header, value, sid),
    )
    request.app.state.upstreams.refresh_soon(sid)
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
    return JSONResponse(_server(row, None))


@handler
async def delete_server(request: Request) -> Response:
    require_admin(request)
    sid = request.path_params["id"]
    cur = request.app.state.db.execute("DELETE FROM servers WHERE id = ?", (sid,))
    if not cur.rowcount:
        raise HTTPError(404, "no such server")
    request.app.state.upstreams.status.pop(sid, None)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------- tools


@handler
async def list_tools(request: Request) -> Response:
    """The same list MCP clients get from /mcp, for the dashboard."""
    current_user(request)
    tools = request.app.state.upstreams.list_tools()
    return JSONResponse([t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools])


@handler
async def call_tool(request: Request) -> Response:
    """Try a tool from the dashboard. Same path as /mcp, so it is audited too."""
    user = current_user(request)
    data = await body(request)
    arguments = data.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPError(400, "'arguments' must be an object")
    result = await request.app.state.upstreams.call_tool(user, _str(data, "name"), arguments)
    return JSONResponse(result.model_dump(mode="json", by_alias=True, exclude_none=True))


# --------------------------------------------------------------------- audit


@handler
async def audit(request: Request) -> Response:
    require_admin(request)
    try:
        limit = min(int(request.query_params.get("limit", 100)), 1000)
    except ValueError:
        raise HTTPError(400, "limit must be a number") from None
    rows = request.app.state.db.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


routes = [
    Route("/api/login", login, methods=["POST"]),
    Route("/api/logout", logout, methods=["POST"]),
    Route("/api/me", me, methods=["GET"]),
    Route("/api/users", list_users, methods=["GET"]),
    Route("/api/users", create_user, methods=["POST"]),
    Route("/api/keys", list_keys, methods=["GET"]),
    Route("/api/keys", create_key, methods=["POST"]),
    Route("/api/keys/{id:int}", revoke_key, methods=["DELETE"]),
    Route("/api/servers", list_servers, methods=["GET"]),
    Route("/api/servers", create_server, methods=["POST"]),
    Route("/api/servers/{id:int}", update_server, methods=["PATCH"]),
    Route("/api/servers/{id:int}", delete_server, methods=["DELETE"]),
    Route("/api/tools", list_tools, methods=["GET"]),
    Route("/api/tools/call", call_tool, methods=["POST"]),
    Route("/api/audit", audit, methods=["GET"]),
]
