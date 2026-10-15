"""Team alerts: tell a team when a tool call it must act on has succeeded.

An admin sets rules per team on the Teams page ("alert this team when <tool> succeeds
on <gateway>"). Upstreams.call_tool calls fire() after every successful upstream call,
on edges and approved runs alike; each matching team gets one alert, open until a
member marks it done. The content is the PII-masked copy of the result.
"""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HTTPError, current_user, handler
from .upstream import error_result

MAX_LINES = 50


def _lines(result: CallToolResult, arguments: dict) -> list[str] | None:
    """What happened, one line per item that succeeded; None when nothing did."""
    data = result.structured_content
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = [i for i in arguments.get("items") or [] if isinstance(i, dict)]

        def name_of(item_id: Any) -> str | None:
            # The call's own item with that id (id, product_id, order_id, ...) names it.
            for i in items:
                if any(v == item_id for k, v in i.items() if k == "id" or k.endswith("_id")):
                    return i.get("name")
            return None

        ok = [r for r in data["results"] if isinstance(r, dict) and r.get("ok")]
        if not ok:
            return None
        lines = []
        for r in ok[:MAX_LINES]:
            name = name_of(r.get("id"))
            lines.append(f"{name + ' ' if name else ''}(#{r.get('id')}): {r.get('detail') or 'done'}")
        if len(ok) > MAX_LINES:
            lines.append(f"…and {len(ok) - MAX_LINES} more")
        refused = len(data["results"]) - len(ok)
        if refused:
            lines.append(f"{refused} item{'s' if refused != 1 else ''} refused")
        return lines
    words = " ".join(c.text for c in result.content if isinstance(c, TextContent)).strip()
    return [words[:500] or "done"]


def fire(conn, *, user: dict, gateway_slug: str, server_id: int, tool: str, title: str, arguments: dict,
         result: CallToolResult, approval_id: int | None, audit_id: int) -> None:
    """One alert per team with a rule for this tool on this gateway. `result` is the masked copy."""
    teams = [r["team_id"] for r in conn.execute(
        "SELECT r.team_id FROM team_alert_rules r JOIN gateways g ON g.id = r.gateway_id"
        " WHERE g.slug = ? AND r.server_id = ? AND r.tool = ?",
        (gateway_slug, server_id, tool),
    )]
    if not teams:
        return
    lines = _lines(result, arguments)
    if lines is None:
        return
    name = f"{conn.execute('SELECT name FROM servers WHERE id = ?', (server_id,)).fetchone()['name']}__{tool}"
    now = time.time()
    with conn.transaction() as tx:
        tx.executemany(
            "INSERT INTO team_alerts (team_id, gateway, tool, title, lines, requested_by, approval_id, audit_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(t, gateway_slug, name, title, json.dumps(lines), user["id"], approval_id, audit_id, now) for t in teams],
        )


STATUSES = ("open", "done", "all")
VIEW = (
    "SELECT a.*, t.name AS team, r.username AS requested_by_name, d.username AS done_by_name,"
    " au.username AS approved_by_name FROM team_alerts a JOIN teams t ON t.id = a.team_id"
    " LEFT JOIN users r ON r.id = a.requested_by LEFT JOIN users d ON d.id = a.done_by"
    " LEFT JOIN approvals ap ON ap.id = a.approval_id LEFT JOIN users au ON au.id = ap.decided_by"
)


def _visible(user: dict) -> tuple[str, tuple]:
    """Admins see every team's alerts; members, their current teams'."""
    if user["role"] == "admin":
        return "TRUE", ()
    return "a.team_id IN (SELECT team_id FROM team_members WHERE user_id = ?)", (user["id"],)


def _view(r: dict) -> dict:
    return {
        "id": r["id"], "team": r["team"], "gateway": r["gateway"], "tool": r["tool"], "title": r["title"],
        "lines": json.loads(r["lines"]), "requested_by": r["requested_by_name"], "approved_by": r["approved_by_name"],
        "created_at": r["created_at"], "done_by": r["done_by_name"], "done_at": r["done_at"],
    }


def query(conn, user: dict, status: str) -> list[dict]:
    where, args = _visible(user)
    if status != "all":
        where += " AND a.done_at IS NULL" if status == "open" else " AND a.done_at IS NOT NULL"
    return [_view(r) for r in conn.execute(f"{VIEW} WHERE {where} ORDER BY a.id DESC LIMIT 200", args)]


def mark_done(conn, user: dict, alert_id: int) -> dict:
    where, args = _visible(user)
    if conn.execute(f"{VIEW} WHERE a.id = ? AND {where}", (alert_id, *args)).fetchone() is None:
        raise HTTPError(404, "no such alert")
    # The claim: one of two clicks wins; the other is told who did it.
    won = conn.execute("UPDATE team_alerts SET done_by = ?, done_at = ? WHERE id = ? AND done_at IS NULL RETURNING id",
                       (user["id"], time.time(), alert_id)).fetchone()
    row = conn.execute(f"{VIEW} WHERE a.id = ?", (alert_id,)).fetchone()
    if won is None:
        raise HTTPError(409, f"already done by {row['done_by_name'] or 'a removed user'}")
    return _view(row)


@handler
async def list_alerts(request: Request) -> Response:
    status = request.query_params.get("status", "open")
    if status not in STATUSES:
        raise HTTPError(400, "status must be open, done or all")
    return JSONResponse(query(request.app.state.db, current_user(request), status))


@handler
async def count_alerts(request: Request) -> Response:
    where, args = _visible(current_user(request))
    n = request.app.state.db.execute(
        f"SELECT COUNT(*) AS n FROM team_alerts a WHERE a.done_at IS NULL AND {where}", args).fetchone()["n"]
    return JSONResponse({"open": n})


@handler
async def done_alert(request: Request) -> Response:
    return JSONResponse(mark_done(request.app.state.db, current_user(request), request.path_params["id"]))


routes = [
    Route("/api/alerts", list_alerts, methods=["GET"]),
    Route("/api/alerts/count", count_alerts, methods=["GET"]),
    Route("/api/alerts/{id:int}/done", done_alert, methods=["POST"]),
]

TOOLS = [
    Tool(
        name="gateway__list_team_alerts",
        description=(
            "[gateway] Alerts for your teams: what a teammate's or another team's tool call did that your "
            "team should act on (for example a purchase order whose goods you will receive). Open alerts "
            "wait for someone to act; after handling one, mark it with gateway__mark_team_alert_done."
        ),
        input_schema={"type": "object", "properties": {
            "status": {"type": "string", "enum": list(STATUSES), "description": "open (default), done or all"}}},
        annotations=ToolAnnotations(read_only_hint=True),
    ),
    Tool(
        name="gateway__mark_team_alert_done",
        description="[gateway] Mark one of your teams' alerts done once it is handled; your teammates see who did it.",
        input_schema={"type": "object", "properties": {"alert_id": {"type": "integer"}}, "required": ["alert_id"]},
    ),
]


def builtin(conn, user: dict, tool: str, arguments: dict) -> CallToolResult | None:
    """A gateway__ alerts call, or None when the tool is not one of these."""
    if tool == "list_team_alerts":
        status = arguments.get("status", "open")
        if status not in STATUSES:
            return error_result("status must be open, done or all")
        rows = query(conn, user, status)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(rows))], structured_content={"alerts": rows})
    if tool == "mark_team_alert_done":
        alert_id = arguments.get("alert_id")
        if type(alert_id) is not int:
            return error_result("alert_id must be a whole number")
        try:
            mark_done(conn, user, alert_id)
        except HTTPError as exc:
            return error_result(exc.message)
        return CallToolResult(content=[TextContent(type="text", text=f"Alert #{alert_id} marked done.")])
    return None
