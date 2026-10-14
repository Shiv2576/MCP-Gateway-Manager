"""Teams and their tool policies: which upstream tools each team may use on each gateway.

Only admins change them. Edges read team_tools on every request (db.allowed_tools),
so a change applies on the next call on every pod.
"""

from __future__ import annotations

import collections
import time
from decimal import Decimal, InvalidOperation

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HTTPError, _str, body, current_user, handler, require_admin
from .approvals import REFUND_TOOL

NAME_MAX = 40
TOOL_MAX = 128  # MCP's limit for a tool name


def _views(conn, teams: list[dict]) -> list[dict]:
    ids = [t["id"] for t in teams]
    members, policies = collections.defaultdict(list), collections.defaultdict(lambda: collections.defaultdict(list))
    budgets = collections.defaultdict(lambda: collections.defaultdict(list))
    if ids:
        for r in conn.execute(
            "SELECT tm.team_id, u.id, u.username FROM team_members tm JOIN users u ON u.id = tm.user_id"
            " WHERE tm.team_id = ANY(?) ORDER BY u.username",
            (ids,),
        ):
            members[r["team_id"]].append({"id": r["id"], "username": r["username"]})
        for r in conn.execute(
            "SELECT tt.team_id, g.slug, s.id AS server_id, s.name AS server, tt.tool FROM team_tools tt"
            " JOIN gateways g ON g.id = tt.gateway_id JOIN servers s ON s.id = tt.server_id"
            " WHERE tt.team_id = ANY(?) ORDER BY g.slug, s.name, tt.tool",
            (ids,),
        ):
            policies[r["team_id"]][r["slug"]].append({"server_id": r["server_id"], "server": r["server"], "tool": r["tool"]})
        for r in conn.execute(
            "SELECT b.team_id, g.slug, b.server_id, b.tool, b.max_items, b.max_refund_total FROM team_budgets b"
            " JOIN gateways g ON g.id = b.gateway_id WHERE b.team_id = ANY(?) ORDER BY g.slug, b.server_id, b.tool",
            (ids,),
        ):
            budgets[r["team_id"]][r["slug"]].append({
                "server_id": r["server_id"], "tool": r["tool"], "max_items": r["max_items"],
                "max_refund_total": r["max_refund_total"] and str(r["max_refund_total"]),
            })
    return [{"id": t["id"], "name": t["name"], "members": members[t["id"]], "policies": policies[t["id"]],
             "budgets": budgets[t["id"]]} for t in teams]


def _team(request: Request) -> dict:
    row = request.app.state.db.execute("SELECT * FROM teams WHERE id = ?", (request.path_params["id"],)).fetchone()
    if row is None:
        raise HTTPError(404, "no such team")
    return row


@handler
async def list_teams(request: Request) -> Response:
    """Admins see every team; members see the teams they are in."""
    user = current_user(request)
    conn = request.app.state.db
    if user["role"] == "admin":
        rows = conn.execute("SELECT * FROM teams ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            "SELECT t.* FROM teams t JOIN team_members tm ON tm.team_id = t.id WHERE tm.user_id = ? ORDER BY t.name",
            (user["id"],),
        ).fetchall()
    return JSONResponse(_views(conn, rows))


@handler
async def create_team(request: Request) -> Response:
    require_admin(request)
    name = _str(await body(request), "name")
    if len(name) > NAME_MAX:
        raise HTTPError(400, f"name: at most {NAME_MAX} characters")
    tid = request.app.state.db.execute(
        "INSERT INTO teams (name, created_at) VALUES (?, ?) RETURNING id", (name, time.time())
    ).fetchone()["id"]
    return JSONResponse({"id": tid, "name": name}, status_code=201)


@handler
async def delete_team(request: Request) -> Response:
    require_admin(request)
    team = _team(request)
    request.app.state.db.execute("DELETE FROM teams WHERE id = ?", (team["id"],))
    return JSONResponse({"ok": True})


@handler
async def set_members(request: Request) -> Response:
    """Replace the team's members."""
    require_admin(request)
    team = _team(request)
    conn = request.app.state.db
    ids = (await body(request)).get("user_ids", [])
    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
        raise HTTPError(400, "user_ids must be a list of user ids")
    known = {r["id"] for r in conn.execute("SELECT id FROM users")}
    if not set(ids) <= known:
        raise HTTPError(400, "user_ids contains an unknown user")
    with conn.transaction() as tx:
        tx.execute("DELETE FROM team_members WHERE team_id = ?", (team["id"],))
        tx.executemany("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", [(team["id"], u) for u in set(ids)])
    return JSONResponse({"ok": True, "user_ids": sorted(set(ids))})


def _gateway(request: Request) -> dict:
    row = request.app.state.db.execute("SELECT id FROM gateways WHERE slug = ?", (request.path_params["slug"],)).fetchone()
    if row is None:
        raise HTTPError(404, "no such gateway")
    return row


async def _tool_rows(request: Request, gateway_id: int) -> list[tuple[int, str]]:
    """The body's {tools: [{server_id, tool}]}, each server attached to this gateway.
    Tool names are not checked against the upstream, which may be down."""
    tools = (await body(request)).get("tools", [])
    if not isinstance(tools, list) or not all(
        isinstance(t, dict) and type(t.get("server_id")) is int
        and isinstance(t.get("tool"), str) and 0 < len(t["tool"]) <= TOOL_MAX
        for t in tools
    ):
        raise HTTPError(400, f"tools must be a list of {{server_id, tool}} with tool 1-{TOOL_MAX} characters")
    attached = {r["server_id"] for r in request.app.state.db.execute(
        "SELECT server_id FROM gateway_servers WHERE gateway_id = ?", (gateway_id,))}
    if not {t["server_id"] for t in tools} <= attached:
        raise HTTPError(400, "tools names a server that is not attached to this gateway")
    return sorted({(t["server_id"], t["tool"]) for t in tools})


@handler
async def set_tools(request: Request) -> Response:
    """Replace the team's allowed tools on one gateway."""
    require_admin(request)
    team = _team(request)
    gateway = _gateway(request)
    rows = await _tool_rows(request, gateway["id"])
    with request.app.state.db.transaction() as tx:
        tx.execute("DELETE FROM team_tools WHERE team_id = ? AND gateway_id = ?", (team["id"], gateway["id"]))
        tx.executemany("INSERT INTO team_tools (team_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)",
                       [(team["id"], gateway["id"], s, t) for s, t in rows])
    return JSONResponse({"ok": True, "count": len(rows)})


def _limit_money(value) -> Decimal | None:
    """A positive amount with at most 2 decimals, as a number or a string."""
    if value is None:
        return None
    try:
        d = Decimal(str(value)) if type(value) in (int, float, str) else None
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite() or d <= 0 or d != d.quantize(Decimal("0.01")) or d >= 10**10:
        raise HTTPError(400, "max_refund_total must be a positive amount with at most 2 decimals")
    return d


@handler
async def set_budgets(request: Request) -> Response:
    """Replace the team's write budgets on one gateway. Empty list: nothing budgeted."""
    require_admin(request)
    team = _team(request)
    gateway = _gateway(request)
    budgets = (await body(request)).get("budgets", [])
    if not isinstance(budgets, list) or not all(
        isinstance(b, dict) and type(b.get("server_id")) is int
        and isinstance(b.get("tool"), str) and 0 < len(b["tool"]) <= TOOL_MAX
        for b in budgets
    ):
        raise HTTPError(400, f"budgets must be a list of {{server_id, tool, max_items?, max_refund_total?}}")
    attached = {r["server_id"] for r in request.app.state.db.execute(
        "SELECT server_id FROM gateway_servers WHERE gateway_id = ?", (gateway["id"],))}
    rows = {}
    for b in budgets:
        if b["server_id"] not in attached:
            raise HTTPError(400, "budgets names a server that is not attached to this gateway")
        if request.app.state.upstreams.read_only(b["server_id"], b["tool"]):
            raise HTTPError(400, f"{b['tool']} is read-only; budgets apply to write tools only")
        items = b.get("max_items")
        if items is not None and (type(items) is not int or items < 0):
            raise HTTPError(400, "max_items must be a whole number, 0 or more (0: every call needs approval)")
        money = _limit_money(b.get("max_refund_total"))
        if money is not None and b["tool"] != REFUND_TOOL:
            raise HTTPError(400, f"max_refund_total applies to {REFUND_TOOL} only")
        if items is None and money is None:
            raise HTTPError(400, f"{b['tool']}: set max_items, max_refund_total or both")
        rows[(b["server_id"], b["tool"])] = (items, money)
    with request.app.state.db.transaction() as tx:
        tx.execute("DELETE FROM team_budgets WHERE team_id = ? AND gateway_id = ?", (team["id"], gateway["id"]))
        tx.executemany(
            "INSERT INTO team_budgets (team_id, gateway_id, server_id, tool, max_items, max_refund_total)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(team["id"], gateway["id"], s, t, i, m) for (s, t), (i, m) in sorted(rows.items())],
        )
    return JSONResponse({"ok": True, "count": len(rows)})


@handler
async def set_denies(request: Request) -> Response:
    """Replace one member's denied tools on one gateway: taken away whatever their teams allow."""
    require_admin(request)
    conn = request.app.state.db
    user = conn.execute("SELECT * FROM users WHERE id = ?", (request.path_params["id"],)).fetchone()
    if user is None:
        raise HTTPError(404, "no such user")
    if user["role"] == "admin":
        raise HTTPError(400, "admins have every tool; denies apply to members only")
    gateway = _gateway(request)
    rows = await _tool_rows(request, gateway["id"])
    with conn.transaction() as tx:
        tx.execute("DELETE FROM user_denies WHERE user_id = ? AND gateway_id = ?", (user["id"], gateway["id"]))
        tx.executemany("INSERT INTO user_denies (user_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)",
                       [(user["id"], gateway["id"], s, t) for s, t in rows])
    return JSONResponse({"ok": True, "count": len(rows)})


routes = [
    Route("/api/teams", list_teams, methods=["GET"]),
    Route("/api/teams", create_team, methods=["POST"]),
    Route("/api/teams/{id:int}", delete_team, methods=["DELETE"]),
    Route("/api/teams/{id:int}/members", set_members, methods=["PUT"]),
    Route("/api/teams/{id:int}/gateways/{slug}/tools", set_tools, methods=["PUT"]),
    Route("/api/teams/{id:int}/gateways/{slug}/budgets", set_budgets, methods=["PUT"]),
    Route("/api/users/{id:int}/gateways/{slug}/denies", set_denies, methods=["PUT"]),
]
