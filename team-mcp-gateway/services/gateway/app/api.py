"""The admin REST API: login, users, API keys, upstream servers, audit log.

Browsers log in and get an HttpOnly session cookie. Scripts and MCP clients
use an API key as a bearer token (MCP clients may also use OAuth, see oauth.py). Every response is built from an explicit
list of fields, so an upstream credential can never leak into one by accident.
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import re
import secrets
import socket
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
# Set by the HTTP or MCP transport; a member's credential must not replace them.
RESERVED_HEADERS = {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding"}
# The only cluster-internal upstreams allowed without UPSTREAM_ALLOW_HOSTS: MCP servers
# the store operator runs in their own locked-down namespaces (store-<name>, mcp-<name>).
OPERATOR_HOST = re.compile(r"^[a-z0-9-]+\.(?:store|mcp)-[a-z0-9-]+\.svc\.cluster\.local$")


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class LoginThrottle:
    """Refuses a username for a while after too many wrong passwords.

    Counted per username, not per address: behind the ingress (or a tunnel)
    every client has the same address. The cost is that someone can lock a
    user out for the window by guessing wrong on purpose.
    shortcut: in memory, so a restart clears it and replicas would not share it
    (the gateway runs one replica); move to the database if that changes.
    """

    def __init__(self, limit: int = 5, window_s: float = 900):
        self.limit, self.window_s = limit, window_s
        self._fails: dict[str, collections.deque] = {}

    def _recent(self, username: str) -> collections.deque:
        fails = self._fails.get(username.lower(), collections.deque())
        while fails and fails[0] < time.monotonic() - self.window_s:
            fails.popleft()
        return fails

    def blocked(self, username: str) -> bool:
        return len(self._recent(username)) >= self.limit

    def failed(self, username: str) -> None:
        if len(self._fails) > 10_000:  # usernames tried in a spray: drop the stale ones
            self._fails = {u: f for u, f in self._fails.items() if self._recent(u)}
        self._fails[username.lower()] = fails = self._recent(username)
        fails.append(time.monotonic())

    def succeeded(self, username: str) -> None:
        self._fails.pop(username.lower(), None)


def check_password(request: Request, username: str, password: str) -> dict:
    """The user, if the password is right. Shared by the dashboard and OAuth logins."""
    throttle: LoginThrottle = request.app.state.login_throttle
    if throttle.blocked(username):
        raise HTTPError(429, "too many wrong passwords; try again in 15 minutes")
    user = request.app.state.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    ok = store.verify_password(password, user["password_hash"] if user else store.DUMMY_HASH)
    if not (user and ok):
        throttle.failed(username)
        raise HTTPError(401, "wrong username or password")
    throttle.succeeded(username)
    return user


def user_for_api_key(conn: store.Db, key: str) -> dict | None:
    return conn.execute(
        "SELECT u.* FROM api_keys k JOIN users u ON u.id = k.user_id"
        " WHERE k.key_hash = ? AND k.revoked_at IS NULL",
        (store.sha256(key),),
    ).fetchone()


# Who the store operator acts as. Not a row in users: it can't log in, own keys or be
# deleted, and it is admin only on the server registry, the one thing it changes.
OPERATOR = {"id": None, "username": "store-operator", "role": "admin"}


def _operator(request: Request, token: str) -> dict | None:
    review = request.app.state.token_review
    path = request.url.path
    if review is None or not (path == "/api/servers" or path.startswith("/api/servers/")):
        return None
    if review(token) == request.app.state.settings.operator_service_account:
        return OPERATOR
    return None


def current_user(request: Request) -> dict:
    conn = request.app.state.db
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        user = user_for_api_key(conn, token) or _operator(request, token)
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


def require_admin(request: Request) -> dict:
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


def org_ready(request: Request) -> bool:
    state = request.app.state
    if not getattr(state, "org_ready", False):  # cached once true: setup never undoes itself
        state.org_ready = state.db.execute("SELECT 1 FROM org").fetchone() is not None
    return state.org_ready


def handler(fn):
    """Turn HTTPError into a JSON error response. Until first-run setup is done,
    only /api/setup answers."""

    async def wrapped(request: Request) -> Response:
        try:
            if not request.url.path.startswith("/api/setup") and not org_ready(request):
                raise HTTPError(503, "setup needed: open the dashboard to set up the organization")
            return await fn(request)
        except HTTPError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        except store.IntegrityError:
            return JSONResponse({"error": "already exists"}, status_code=409)

    return wrapped


# ------------------------------------------------------------------- session


def session_response(request: Request, user: dict, status: int = 200) -> JSONResponse:
    """Start a browser session for this user: the HttpOnly cookie and the user as JSON."""
    conn = request.app.state.db
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (store.sha256(token), user["id"], time.time() + SESSION_TTL_S),
    )
    response = JSONResponse(_user(user), status_code=status)
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
async def login(request: Request) -> Response:
    data = await body(request)
    user = check_password(request, _str(data, "username"), _str(data, "password"))
    return session_response(request, user)


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


def gateway_param(request: Request, slug) -> str:
    """The gateway a dashboard request acts through: its policy applies, as on its edge."""
    if not isinstance(slug, str) or not slug:
        raise HTTPError(400, "'gateway' is required: tools are used through a gateway")
    if request.app.state.db.execute("SELECT 1 FROM gateways WHERE slug = ?", (slug,)).fetchone() is None:
        raise HTTPError(404, "no such gateway")
    return slug


def _user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"]}


# --------------------------------------------------------------------- users


def _server_ids(conn: store.Db, data: dict) -> list[int]:
    ids = data.get("server_ids", [])
    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
        raise HTTPError(400, "server_ids must be a list of server ids")
    known = {r["id"] for r in conn.execute("SELECT id FROM servers")}
    if not set(ids) <= known:
        raise HTTPError(400, "server_ids contains an unknown server")
    return sorted(set(ids))


@handler
async def list_users(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    teams = collections.defaultdict(list)
    for t in conn.execute(
        "SELECT tm.user_id, t.name FROM team_members tm JOIN teams t ON t.id = tm.team_id ORDER BY t.name"
    ):
        teams[t["user_id"]].append(t["name"])
    denies = collections.defaultdict(lambda: collections.defaultdict(list))
    for d in conn.execute(
        "SELECT ud.user_id, g.slug, s.id AS server_id, s.name AS server, ud.tool FROM user_denies ud"
        " JOIN gateways g ON g.id = ud.gateway_id JOIN servers s ON s.id = ud.server_id ORDER BY g.slug, s.name, ud.tool"
    ):
        denies[d["user_id"]][d["slug"]].append({"server_id": d["server_id"], "server": d["server"], "tool": d["tool"]})
    creds = collections.defaultdict(list)  # which servers, never the values
    for c in conn.execute(
        "SELECT uc.user_id, s.name, uc.updated_at FROM user_credentials uc JOIN servers s ON s.id = uc.server_id ORDER BY s.name"
    ):
        creds[c["user_id"]].append({"server": c["name"], "updated_at": c["updated_at"]})
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return JSONResponse([{**_user(u), "teams": teams[u["id"]], "denies": denies[u["id"]], "credentials": creds[u["id"]]}
                         for u in rows])


@handler
async def create_user(request: Request) -> Response:
    require_admin(request)
    data = await body(request)
    username, password = _str(data, "username"), _str(data, "password")
    role = data.get("role", "member")
    if role not in ("admin", "member"):
        raise HTTPError(400, "role must be admin or member")
    uid = store.create_user(request.app.state.db, username, password, role)
    return JSONResponse({"id": uid, "username": username, "role": role}, status_code=201)


@handler
async def delete_user(request: Request) -> Response:
    """Keys, sessions and team memberships go with the user; audit rows keep the username.
    The caller is an admin and cannot delete themself, so an admin always remains."""
    me = require_admin(request)
    conn = request.app.state.db
    user = conn.execute("SELECT * FROM users WHERE id = ?", (request.path_params["id"],)).fetchone()
    if user is None:
        raise HTTPError(404, "no such user")
    if user["id"] == me["id"]:
        raise HTTPError(400, "you cannot delete yourself")
    if user["role"] == "admin":
        from .stores import delete_all_for  # stores imports api

        await delete_all_for(request, user["id"])
    conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    return JSONResponse({"ok": True})


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
    kid = request.app.state.db.execute(
        "INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (user["id"], name, store.sha256(key), key[:12], time.time()),
    ).fetchone()["id"]
    # The only time the key is ever shown. Only its hash is kept.
    return JSONResponse({"id": kid, "name": name, "key": key}, status_code=201)


@handler
async def revoke_key(request: Request) -> Response:
    user = current_user(request)
    key_id = request.path_params["id"]
    conn = request.app.state.db
    key = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if key is None or (user["role"] != "admin" and key["user_id"] != user["id"]):
        raise HTTPError(404, "no such key")
    # First DELETE revokes; a second one, on a key already revoked, removes the row.
    if key["revoked_at"] is not None:
        conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    else:
        conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), key_id),
        )
    return JSONResponse({"ok": True})


# ------------------------------------------------------- OAuth connections


@handler
async def list_connections(request: Request) -> Response:
    """Apps signed in through OAuth: one row per user and app with a live refresh token."""
    user = current_user(request)
    rows = request.app.state.db.execute(
        "SELECT t.user_id, u.username, t.client_id, c.info::json ->> 'client_name' AS client_name,"
        " MAX(t.created_at) AS last_used"
        " FROM oauth_tokens t JOIN users u ON u.id = t.user_id JOIN oauth_clients c ON c.client_id = t.client_id"
        " WHERE t.kind = 'refresh' AND t.expires_at > ? AND (? OR t.user_id = ?)"
        " GROUP BY t.user_id, u.username, t.client_id, c.info ORDER BY last_used DESC",
        (time.time(), user["role"] == "admin", user["id"]),
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@handler
async def delete_connection(request: Request) -> Response:
    user = current_user(request)
    uid = request.path_params["user_id"]
    if user["role"] != "admin" and uid != user["id"]:
        raise HTTPError(404, "no such connection")
    cur = request.app.state.db.execute(
        "DELETE FROM oauth_tokens WHERE user_id = ? AND client_id = ?", (uid, request.path_params["client_id"])
    )
    if not cur.rowcount:
        raise HTTPError(404, "no such connection")
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------- servers


def _server(row: dict, status: Status | None, gateways=()) -> dict:
    status = status or Status(state="disabled" if not row["enabled"] else "pending")
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "store": row["store"],
        "gateways": list(gateways),
        # Which header carries the credential, never its value.
        "auth_header": row["auth_header"],
        "has_credential": bool(row["auth_value"]),
        # Where a member's own credential goes on personal connections; None = not offered.
        "personal_header": row["personal_header"],
        "status": status.state,
        "error": status.error,
        "checked_at": status.checked_at,
        "tools": [{"name": t.name, "description": t.description,
                   "read_only": bool(t.annotations and t.annotations.read_only_hint)} for t in status.tools],
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


def personal_header(value) -> str | None:
    """The header a member's own credential goes in; None = personal identity off."""
    if value is None:
        return None
    if not isinstance(value, str) or not HEADER_NAME.match(value):
        raise HTTPError(400, "personal_header is not a valid header name")
    if value.lower() in RESERVED_HEADERS or value.lower().startswith("mcp-"):
        raise HTTPError(400, f"personal_header cannot be {value}: the transport sets it")
    return value


def check_upstream_url(value: str, allow: tuple[str, ...] = ()) -> str:
    """SSRF guard: an admin-added URL must resolve only to public addresses, so the
    gateway cannot be aimed at cloud metadata, Postgres, the Kubernetes API or other
    pods. The gateway's egress NetworkPolicy catches a DNS record changed later."""
    try:
        parts = urlsplit(value)
        scheme, host, port = parts.scheme, (parts.hostname or "").rstrip("."), parts.port
    except ValueError:  # bad port or IPv6 literal
        scheme = host = ""
    if scheme not in ("http", "https") or not host:
        raise HTTPError(400, "url must be an http(s) URL")
    if host in allow or OPERATOR_HOST.match(host):
        return value
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise HTTPError(400, f"url: cannot resolve {host}") from None
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise HTTPError(400, f"url: {host} is a private or internal address"
                                 " (add it to UPSTREAM_ALLOW_HOSTS if it is a trusted in-cluster server)")
    return value


def _url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPError(400, "url must be an http(s) URL")
    return value


async def _upstream_url(request: Request, value: str) -> str:
    allow = request.app.state.settings.upstream_allow_hosts
    return await asyncio.to_thread(check_upstream_url, value, allow)


@handler
async def list_servers(request: Request) -> Response:
    user = current_user(request)
    ups: Upstreams = request.app.state.upstreams
    conn = request.app.state.db
    allowed = None if user["role"] == "admin" else {
        # Servers this member has at least one allowed tool on, through any gateway.
        r["server_id"] for r in conn.execute(
            "SELECT DISTINCT tt.server_id FROM team_members tm JOIN team_tools tt ON tt.team_id = tm.team_id"
            " JOIN gateway_servers gs ON gs.gateway_id = tt.gateway_id AND gs.server_id = tt.server_id"
            " WHERE tm.user_id = ?",
            (user["id"],),
        )
    }
    on = collections.defaultdict(list)
    for r in conn.execute(
        "SELECT gs.server_id, g.slug FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id ORDER BY g.slug"
    ):
        on[r["server_id"]].append(r["slug"])
    rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()  # oldest first: the seeded store server leads
    return JSONResponse([_server(r, ups.status.get(r["id"]), on[r["id"]])
                         for r in rows if allowed is None or r["id"] in allowed])


@handler
async def create_server(request: Request) -> Response:
    require_admin(request)
    data = await body(request)
    name = _str(data, "name")
    if not SERVER_NAME.match(name):
        raise HTTPError(400, "name: 1-32 of a-z 0-9 and -, used as the tool prefix")
    if name == "gateway":  # the prefix of the gateway's own tools (approvals.BUILTIN)
        raise HTTPError(400, "name: 'gateway' is reserved for the gateway's own tools")
    url = await _upstream_url(request, _str(data, "url"))
    header, value = _auth_fields(data)
    value = value and request.app.state.vault.seal(value)
    store_name = _str(data, "store", required=False)
    conn = request.app.state.db
    sid = conn.execute(
        "INSERT INTO servers (name, url, auth_header, auth_value, enabled, store, personal_header, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, url, header, value, 1 if data.get("enabled", True) else 0, store_name,
         personal_header(data.get("personal_header")), time.time()),
    ).fetchone()["id"]
    request.app.state.upstreams.refresh_soon(sid)
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
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
    url = await _upstream_url(request, _str(data, "url")) if "url" in data else row["url"]
    enabled = (1 if data["enabled"] else 0) if "enabled" in data else row["enabled"]
    if "auth" in data:
        header, value = _auth_fields(data)
        value = value and request.app.state.vault.seal(value)
    else:  # kept as stored, already sealed
        header, value = row["auth_header"], row["auth_value"]
    personal = personal_header(data["personal_header"]) if "personal_header" in data else row["personal_header"]
    if personal is None and conn.execute(
        "SELECT 1 FROM gateway_servers WHERE server_id = ? AND identity = 'personal'", (sid,)
    ).fetchone():
        raise HTTPError(400, "a gateway uses this server with personal identity; switch it to shared first")
    conn.execute(
        "UPDATE servers SET url = ?, enabled = ?, auth_header = ?, auth_value = ?, personal_header = ? WHERE id = ?",
        (url, enabled, header, value, personal, sid),
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
    """The same list MCP clients get from that gateway's /mcp, for the dashboard."""
    user = current_user(request)
    tools = request.app.state.upstreams.list_tools(user, gateway_param(request, request.query_params.get("gateway")))
    return JSONResponse([t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools])


@handler
async def call_tool(request: Request) -> Response:
    """Try a tool from the dashboard. Same path as /mcp, so it is audited too."""
    user = current_user(request)
    data = await body(request)
    arguments = data.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPError(400, "'arguments' must be an object")
    gateway = gateway_param(request, data.get("gateway"))
    result = await request.app.state.upstreams.call_tool(user, gateway, _str(data, "name"), arguments)
    return JSONResponse(result.model_dump(mode="json", by_alias=True, exclude_none=True))


# --------------------------------------------------------------------- audit


AUDIT_LIST = ("id, at, user_id, username, tool, server, gateway, ok, duration_ms, error, identity,"
              " pii, payload_masked_for_log")


@handler
async def audit(request: Request) -> Response:
    require_admin(request)
    try:
        limit = min(int(request.query_params.get("limit", 100)), 1000)
    except ValueError:
        raise HTTPError(400, "limit must be a number") from None
    # Without payload: up to 64 KB each, fetched one row at a time by audit_entry.
    rows = request.app.state.db.execute(
        f"SELECT {AUDIT_LIST} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@handler
async def audit_entry(request: Request) -> Response:
    require_admin(request)
    row = request.app.state.db.execute(
        "SELECT * FROM audit_log WHERE id = ?", (request.path_params["id"],)
    ).fetchone()
    if row is None:
        raise HTTPError(404, "no such audit entry")
    return JSONResponse({**row, "refund_total": row["refund_total"] and str(row["refund_total"])})


# ------------------------------------------------------------------- reports


REPORT_DAYS = 30


@handler
async def reports(request: Request) -> Response:
    require_admin(request)
    db, now = request.app.state.db, time.time()

    def count(sql, args=()):
        return db.execute(sql, args).fetchone()["n"]

    def top(select, group):
        return [dict(r) for r in db.execute(
            f"SELECT {group} AS name, {select} AS value FROM audit_log"
            # wait_for_approval is a polling loop, not a feature anyone uses.
            f" WHERE at > ? AND {group} IS NOT NULL AND tool <> 'gateway__wait_for_approval'"
            f" GROUP BY {group} ORDER BY value DESC LIMIT 10",
            (now - REPORT_DAYS * 86400,),
        ).fetchall()]

    return JSONResponse({
        "days": REPORT_DAYS,
        "counts": {
            "mcps": count("SELECT COUNT(*) AS n FROM servers"),
            "gateways": count("SELECT COUNT(*) AS n FROM gateways"),
            # Agents use API keys; apps sign in through OAuth.
            "apps": count("SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL")
            + count("SELECT COUNT(DISTINCT client_id) AS n FROM oauth_tokens WHERE kind = 'refresh' AND expires_at > ?", (now,)),
            "connections": count("SELECT COUNT(*) AS n FROM (SELECT DISTINCT user_id, client_id FROM oauth_tokens"
                                 " WHERE kind = 'refresh' AND expires_at > ?) c", (now,)),
        },
        "by_tool": top("COUNT(*)", "tool"),
        "by_server": top("COUNT(*)", "server"),
        "by_user": top("COUNT(*)", "username"),
        "p99_by_tool": top("ROUND(percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms))::int", "tool"),
    })


routes = [
    Route("/api/reports", reports, methods=["GET"]),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/logout", logout, methods=["POST"]),
    Route("/api/me", me, methods=["GET"]),
    Route("/api/users", list_users, methods=["GET"]),
    Route("/api/users", create_user, methods=["POST"]),
    Route("/api/users/{id:int}", delete_user, methods=["DELETE"]),
    Route("/api/keys", list_keys, methods=["GET"]),
    Route("/api/keys", create_key, methods=["POST"]),
    Route("/api/keys/{id:int}", revoke_key, methods=["DELETE"]),
    Route("/api/oauth/connections", list_connections, methods=["GET"]),
    Route("/api/oauth/connections/{user_id:int}/{client_id}", delete_connection, methods=["DELETE"]),
    Route("/api/servers", list_servers, methods=["GET"]),
    Route("/api/servers", create_server, methods=["POST"]),
    Route("/api/servers/{id:int}", update_server, methods=["PATCH"]),
    Route("/api/servers/{id:int}", delete_server, methods=["DELETE"]),
    Route("/api/tools", list_tools, methods=["GET"]),
    Route("/api/tools/call", call_tool, methods=["POST"]),
    Route("/api/audit", audit, methods=["GET"]),
    Route("/api/audit/{id:int}", audit_entry, methods=["GET"]),
]
