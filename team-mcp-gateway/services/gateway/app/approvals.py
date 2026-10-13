"""Write budgets per member and tool, and explicit admin approval for jobs bigger than them.

A budgeted tool may change at most N items (and, for refunds, an amount of money)
per member in any rolling hour. Over it, the call is refused and nothing is
created; the agent may wait, or call gateway__request_approval to ask an admin,
then gateway__wait_for_approval for the decision. An admin approves on the
dashboard's Alerts page and the control pod runs the frozen call as the
requester. Plain code decides everything here; there is no LLM in the path.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import anyio
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import upstream
from .api import HTTPError, body, handler, require_admin
from .db import allowed_tools

log = logging.getLogger("gateway")

WINDOW_SECONDS = 3600  # the budget looks back this far
LOCK_SECONDS = 3600  # after a rejection, no new requests for the same tool
EXPIRE_SECONDS = 86400  # undecided requests expire; stored results are cleared
# Edges answer stateless JSON, so a wait cannot stream progress; hold short of client timeouts.
WAIT_SECONDS = 25
POLL_SECONDS = 2
REFUND_TOOL = "bulk_refund_orders"
BUILTIN = "gateway"  # the server-name part of the built-in tools; no real server may take it
NOTE_MAX = 500
UNKNOWN_OUTCOME = ("The run did not finish (the gateway restarted?). Its outcome is unknown: "
                   "check the store before asking again.")

TOOLS = [
    Tool(
        name="gateway__request_approval",
        description=(
            "[gateway] Ask an admin to approve one call that is over your hourly write budget. "
            "Only for calls the budget refused; nothing changes until an admin approves. "
            "Then call gateway__wait_for_approval with the id you get back."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "The full tool name, <server>__<tool>"},
                "arguments": {"type": "object", "description": "The exact arguments to run it with"},
            },
            "required": ["tool", "arguments"],
        },
    ),
    Tool(
        name="gateway__wait_for_approval",
        description=(
            "[gateway] Wait for an admin's decision on an approval you requested. Returns the "
            "tool's result once approved and run, the admin's note if rejected, or 'still pending' "
            "(call again)."
        ),
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "The approval id"}},
            "required": ["id"],
        },
    ),
]


def _text(text: str, *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=error)


def _clock(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M UTC")


def _money(d: Decimal | None) -> str | None:
    return None if d is None else str(Decimal(d).quantize(Decimal("0.01")))


# ------------------------------------------------------------------ budgets


def budget_for(conn, user: dict, gateway_slug: str, server_id: int, tool: str) -> dict | None:
    """The strictest of the caller's teams' budgets for this tool; None = not budgeted."""
    if user["role"] == "admin":
        return None
    row = conn.execute(
        "SELECT MIN(b.max_items) AS max_items, MIN(b.max_refund_total) AS max_refund_total, COUNT(*) AS n"
        " FROM team_members tm JOIN team_budgets b ON b.team_id = tm.team_id"
        " JOIN gateways g ON g.id = b.gateway_id"
        " WHERE tm.user_id = ? AND g.slug = ? AND b.server_id = ? AND b.tool = ?",
        (user["id"], gateway_slug, server_id, tool),
    ).fetchone()
    return row if row["n"] else None


def has_budget(conn, user: dict, gateway_slug: str) -> bool:
    """Whether the approval tools mean anything to this caller on this gateway."""
    return user["role"] != "admin" and conn.execute(
        "SELECT 1 FROM team_members tm JOIN team_budgets b ON b.team_id = tm.team_id"
        " JOIN gateways g ON g.id = b.gateway_id WHERE tm.user_id = ? AND g.slug = ? LIMIT 1",
        (user["id"], gateway_slug),
    ).fetchone() is not None


def measure(tool: str, arguments: dict) -> tuple[int, Decimal | None] | str:
    """Items this call changes, and the money it refunds; or why it cannot be counted."""
    items = arguments.get("items")
    count = len(items) if isinstance(items, list) else 1
    if tool != REFUND_TOOL:
        return count, None
    try:
        amounts = [Decimal(str(i["amount"])) for i in items]  # TypeError/KeyError: no list or no amount
    except (TypeError, KeyError, InvalidOperation):
        return "Every refund item needs a numeric amount."
    if not all(a.is_finite() and a > 0 for a in amounts):
        return "Every refund item needs a positive amount."
    return count, sum(amounts, Decimal(0))


def describe(budget: dict) -> str:
    parts = []
    if budget["max_items"] is not None:
        parts.append(f"{budget['max_items']} items/hour")
    if budget["max_refund_total"] is not None:
        parts.append(f"{_money(budget['max_refund_total'])} refunded/hour")
    return ", ".join(parts)


def _window(conn, user_id: int, gateway_slug: str, name: str, now: float) -> list[dict]:
    """This member's counted rows in the last hour, oldest first. In-flight calls count
    too, so parallel calls cannot slip past each other; a call whose pod died counts
    until it leaves the window (fails closed)."""
    return conn.execute(
        "SELECT at, items, refund_total FROM audit_log WHERE user_id = ? AND gateway = ? AND tool = ?"
        " AND items IS NOT NULL AND approval_id IS NULL AND at > ?"
        " AND (ok = 1 OR (ok = 0 AND error = ?)) ORDER BY at",
        (user_id, gateway_slug, name, now - WINDOW_SECONDS, upstream.NO_RESULT),
    ).fetchall()


def _free_at(rows: list[dict], key: str, used, this, limit) -> tuple[float, Any] | None:
    """When enough rows have dropped out for this call to fit, and how much is free then."""
    need, freed = used + this - limit, 0
    for r in rows:
        freed += r[key] or 0
        if freed >= need:
            return r["at"] + WINDOW_SECONDS, limit - used + freed
    return None


def assess(conn, user: dict, gateway_slug: str, server_id: int, name: str, tool: str,
           arguments: dict, now: float) -> dict | None:
    """None when the tool is not budgeted for this caller. Otherwise the budget, this
    call's size, and `refusal`: None when it fits what is left of the hour."""
    budget = budget_for(conn, user, gateway_slug, server_id, tool)
    if budget is None:
        return None
    size = measure(tool, arguments)
    if isinstance(size, str):
        return {"budget": budget, "items": None, "refund_total": None, "refusal": size}
    items, total = size
    rows = _window(conn, user["id"], gateway_slug, name, now)
    out = {"budget": budget, "items": items, "refund_total": total, "refusal": None}
    over = []  # (what, left, free-up)
    if budget["max_items"] is not None:
        used = sum(r["items"] for r in rows)
        if used + items > budget["max_items"]:
            over.append((f"{max(budget['max_items'] - used, 0)} of {budget['max_items']} items",
                         _free_at(rows, "items", used, items, budget["max_items"]), items > budget["max_items"], "items"))
    if total is not None and budget["max_refund_total"] is not None:
        used = sum((r["refund_total"] or Decimal(0) for r in rows), Decimal(0))
        limit = budget["max_refund_total"]
        if used + total > limit:
            over.append((f"{_money(max(limit - used, Decimal(0)))} of {_money(limit)} refund money",
                         _free_at(rows, "refund_total", used, total, limit), total > limit, "refunded"))
    if not over:
        return out
    lines = []
    for left, free, never, unit in over:
        if never:
            line = f"this call alone is over the whole hourly budget, so it can never run directly"
            if rows:
                line += f" (your oldest use drops out at {_clock(rows[0]['at'] + WINDOW_SECONDS)})"
        else:
            at, amount = free
            amount = _money(amount) if unit == "refunded" else amount
            line = f"{amount} {'free up' if unit == 'items' else 'refund money frees up'} at {_clock(at)}"
        lines.append(f"{left} left; {line}")
    out["refusal"] = (
        f"Budget: {'; '.join(lines)} for {tool}. This call needs {items} items"
        + (f" and {_money(total)} refunded" if total is not None else "")
        + ". Wait and retry, or call gateway__request_approval with {\"tool\": \"" + name
        + "\", \"arguments\": <the same arguments>} to ask an admin."
    )
    return out


def charge(ups, user: dict, gateway_slug: str, row: dict, tool: str, name: str,
           arguments: dict, audit_id: int) -> str | None:
    """The budget check of a direct call: why it is refused, or None to run it.
    Records the call's size on its audit row, so it counts once it succeeds."""
    with ups.db.transaction() as tx:
        # One member's calls to one tool check and record one at a time, on every pod.
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"budget|{user['id']}|{gateway_slug}|{name}",))
        a = assess(tx, user, gateway_slug, row["id"], name, tool, arguments, time.time())
        if a is None:
            return None
        tx.execute("UPDATE audit_log SET items = ?, refund_total = ? WHERE id = ?",
                   (a["items"], a["refund_total"], audit_id))
        return a["refusal"]


# ------------------------------------------------------------- housekeeping


def housekeep(conn, run_timeout: float) -> None:
    """Expire, fail and clear old rows. Done on every list, request and wait, not on a timer."""
    now = time.time()
    conn.execute("UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND requested_at < ?",
                 (now - EXPIRE_SECONDS,))
    conn.execute(
        "UPDATE approvals SET status = 'failed', finished_at = ?, result = ? WHERE status = 'running' AND decided_at < ?",
        (now, upstream.error_result(UNKNOWN_OUTCOME).model_dump_json(by_alias=True, exclude_none=True),
         now - 2 * run_timeout),
    )
    # The stored result is the only unmasked copy of a call's output outside the run itself.
    conn.execute("UPDATE approvals SET result = NULL WHERE result IS NOT NULL AND finished_at < ?",
                 (now - EXPIRE_SECONDS,))


# ------------------------------------------------------------ built-in tools


async def builtin(ups, user: dict, gateway_slug: str, tool: str, arguments: dict,
                  client: str | None) -> tuple[CallToolResult, int | None, str | None]:
    """A gateway__* call: the result, the approval it concerns, and what to audit as its error."""
    if tool == "request_approval":
        return request(ups, user, gateway_slug, arguments, client)
    if tool == "wait_for_approval":
        return await wait(ups, user, arguments)
    return upstream.error_result(f"Unknown tool '{BUILTIN}__{tool}'. Tools are named <server>__<tool>."), None, "unknown tool"


def request(ups, user: dict, gateway_slug: str, arguments: dict, client: str | None):
    conn = ups.db
    name, args = arguments.get("tool"), arguments.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return _text("Pass {\"tool\": \"<server>__<tool>\", \"arguments\": {...}}.", error=True), None, "bad input"
    server_name, sep, tool = name.partition(upstream.SEP)
    row = conn.execute("SELECT * FROM servers WHERE name = ?", (server_name,)).fetchone()
    allowed = allowed_tools(conn, user, gateway_slug)
    names = allowed.get(row["id"], set()) if row else set()
    if not sep or not tool or row is None or row["id"] not in allowed or (names is not None and tool not in names):
        return upstream.error_result(f"Unknown tool '{name}'. Tools are named <server>__<tool>."), None, "unknown tool"
    now = time.time()
    a = assess(conn, user, gateway_slug, row["id"], name, tool, args, now)
    if a is None:
        return _text(f"{name} needs no approval; call it directly.", error=True), None, "not budgeted"
    if a["items"] is None:
        return _text(a["refusal"], error=True), None, "bad input"
    if a["refusal"] is None:
        return _text("This fits your budget; call the tool directly.", error=True), None, "fits budget"

    with conn.transaction() as tx:
        # Serialized per member and tool, so two requests cannot both pass the checks below.
        tx.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"approval|{user['id']}|{gateway_slug}|{name}",))
        housekeep(tx, ups.approval_timeout)
        key = (user["id"], gateway_slug, name)
        open_ = tx.execute("SELECT id FROM approvals WHERE user_id = ? AND gateway = ? AND tool = ?"
                           " AND status IN ('pending', 'running') ORDER BY id LIMIT 1", key).fetchone()
        if open_:
            return _text(f"Approval #{open_['id']} for {name} is still open. Call gateway__wait_for_approval "
                         f"with {{\"id\": {open_['id']}}}.", error=True), open_["id"], "already open"
        locked = tx.execute("SELECT id, decided_at FROM approvals WHERE user_id = ? AND gateway = ? AND tool = ?"
                            " AND status = 'rejected' AND decided_at > ? ORDER BY decided_at DESC LIMIT 1",
                            (*key, now - LOCK_SECONDS)).fetchone()
        if locked:
            return _text(f"Approval #{locked['id']} was rejected; new requests for this tool are locked until "
                         f"{_clock(locked['decided_at'] + LOCK_SECONDS)}. Your hourly budget still works.",
                         error=True), locked["id"], "locked"
        aid = tx.execute(
            "INSERT INTO approvals (status, user_id, gateway, tool, arguments, items, refund_total, budget, client,"
            " requested_at) VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (user["id"], gateway_slug, name, json.dumps(args), a["items"], a["refund_total"],
             describe(a["budget"]), client, now),
        ).fetchone()["id"]
    money = f" refunding {_money(a['refund_total'])}" if a["refund_total"] is not None else ""
    return _text(
        f"Approval #{aid} requested: {tool} on {a['items']} items{money} (budget {describe(a['budget'])}). "
        f"Nothing has changed yet. Call gateway__wait_for_approval with {{\"id\": {aid}}} to wait for the decision."
    ), aid, None


async def wait(ups, user: dict, arguments: dict):
    aid = arguments.get("id")
    if type(aid) is not int:
        return _text("Pass {\"id\": <approval id>}.", error=True), None, "bad input"
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        housekeep(ups.db, ups.approval_timeout)
        row = ups.db.execute("SELECT * FROM approvals WHERE id = ? AND user_id = ?", (aid, user["id"])).fetchone()
        if row is None:  # someone else's id answers like a missing one
            return _text(f"Unknown approval #{aid}.", error=True), None, "unknown approval"
        status = row["status"]
        if status in ("executed", "failed"):
            # Handed out once: the UPDATE that clears it is the one that delivers it.
            got = ups.db.execute(
                "UPDATE approvals a SET result = NULL FROM (SELECT id, result FROM approvals"
                " WHERE id = ? AND result IS NOT NULL FOR UPDATE) old WHERE a.id = old.id RETURNING old.result",
                (aid,)).fetchone()
            if got is None:
                return _text(f"Approval #{aid} {status}; its result was already delivered or has expired."), aid, None
            result = CallToolResult.model_validate_json(got["result"])
            return result, aid, status if result.is_error else None
        if status == "rejected":
            note = f" Note from the admin: {row['note']}" if row["note"] else ""
            return _text(f"Approval #{aid} was rejected.{note} New requests for {row['tool']} are locked until "
                         f"{_clock(row['decided_at'] + LOCK_SECONDS)}; your hourly budget still works.",
                         error=True), aid, "rejected"
        if status == "expired":
            return _text(f"Approval #{aid} expired: no admin decided within 24 hours.", error=True), aid, "expired"
        if time.monotonic() >= deadline:
            return _text(f"Approval #{aid} still {status}; call gateway__wait_for_approval again."), aid, None
        await anyio.sleep(POLL_SECONDS)


# ------------------------------------------------------------- admin routes


def _view(r: dict) -> dict:
    args = json.loads(r["arguments"])
    items = args.get("items") if isinstance(args.get("items"), list) else [args]
    return {
        "id": r["id"], "status": r["status"], "user_id": r["user_id"], "username": r["username"],
        "teams": r["teams"] or [], "gateway": r["gateway"], "tool": r["tool"], "client": r["client"],
        "items": r["items"], "refund_total": _money(r["refund_total"]), "budget": r["budget"],
        "preview": items[:20], "requested_at": r["requested_at"], "decided_by": r["decided_by_name"],
        "decided_at": r["decided_at"], "finished_at": r["finished_at"], "note": r["note"], "audit_id": r["audit_id"],
        "outcome": _outcome(r["result"]),
    }


def _outcome(result: str | None) -> str | None:
    """The run's text for the card; the full result goes to the requester only."""
    if not result:
        return None
    r = CallToolResult.model_validate_json(result)
    return " ".join(c.text for c in r.content if isinstance(c, TextContent))[:2000]


VIEW = (
    "SELECT a.*, u.username, d.username AS decided_by_name,"
    " (SELECT array_agg(t.name ORDER BY t.name) FROM team_members tm JOIN teams t ON t.id = tm.team_id"
    "  WHERE tm.user_id = a.user_id) AS teams"
    " FROM approvals a JOIN users u ON u.id = a.user_id LEFT JOIN users d ON d.id = a.decided_by"
)


@handler
async def list_approvals(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    housekeep(conn, request.app.state.upstreams.approval_timeout)
    which = request.query_params.get("status", "pending")
    if which not in ("pending", "history"):
        raise HTTPError(400, "status must be pending or history")
    where = "a.status = 'pending'" if which == "pending" else "a.status <> 'pending'"
    rows = conn.execute(f"{VIEW} WHERE {where} ORDER BY a.id DESC LIMIT 200").fetchall()
    return JSONResponse([_view(r) for r in rows])


@handler
async def count_approvals(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    housekeep(conn, request.app.state.upstreams.approval_timeout)
    n = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'").fetchone()["n"]
    return JSONResponse({"pending": n})


async def _decision(request: Request) -> tuple[dict, dict, str | None]:
    admin = require_admin(request)
    data = await body(request)
    note = data.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise HTTPError(400, f"note: text up to {NOTE_MAX} characters")
    row = request.app.state.db.execute("SELECT * FROM approvals WHERE id = ?", (request.path_params["id"],)).fetchone()
    if row is None:
        raise HTTPError(404, "no such approval")
    if row["user_id"] == admin["id"]:
        raise HTTPError(403, "you cannot decide your own request")
    return admin, row, (note or "").strip() or None


@handler
async def approve(request: Request) -> Response:
    admin, row, note = await _decision(request)
    conn = request.app.state.db
    ups = request.app.state.upstreams
    housekeep(conn, ups.approval_timeout)
    # The claim: one click, one run, however many admins or pods.
    claimed = conn.execute(
        "UPDATE approvals SET status = 'running', decided_by = ?, decided_at = ?, note = ?"
        " WHERE id = ? AND status = 'pending' RETURNING *",
        (admin["id"], time.time(), note, row["id"]),
    ).fetchone()
    if claimed is None:
        raise HTTPError(409, "already decided or expired")
    requester = conn.execute("SELECT * FROM users WHERE id = ?", (claimed["user_id"],)).fetchone()
    # Shielded: an admin closing the tab must not cancel a run that has started.
    with anyio.CancelScope(shield=True):
        try:
            result = await ups.call_tool(requester, claimed["gateway"], claimed["tool"],
                                         json.loads(claimed["arguments"]), approval_id=claimed["id"],
                                         timeout=ups.approval_timeout)
        except Exception:
            log.exception("approved run %s failed", claimed["id"])
            result = upstream.error_result("The approved run failed inside the gateway; an admin can check its log.")
        audit = conn.execute("SELECT id FROM audit_log WHERE approval_id = ? AND tool = ? ORDER BY id DESC LIMIT 1",
                             (claimed["id"], claimed["tool"])).fetchone()
        conn.execute(
            "UPDATE approvals SET status = ?, finished_at = ?, result = ?, audit_id = ? WHERE id = ? AND status = 'running'",
            ("failed" if result.is_error else "executed", time.time(),
             result.model_dump_json(by_alias=True, exclude_none=True), audit and audit["id"], claimed["id"]),
        )
    done = conn.execute(f"{VIEW} WHERE a.id = ?", (claimed["id"],)).fetchone()
    return JSONResponse(_view(done))


@handler
async def reject(request: Request) -> Response:
    admin, row, note = await _decision(request)
    conn = request.app.state.db
    housekeep(conn, request.app.state.upstreams.approval_timeout)
    done = conn.execute(
        "UPDATE approvals SET status = 'rejected', decided_by = ?, decided_at = ?, note = ?"
        " WHERE id = ? AND status = 'pending' RETURNING id",
        (admin["id"], time.time(), note, row["id"]),
    ).fetchone()
    if done is None:
        raise HTTPError(409, "already decided or expired")
    return JSONResponse(_view(conn.execute(f"{VIEW} WHERE a.id = ?", (row["id"],)).fetchone()))


routes = [
    Route("/api/approvals", list_approvals, methods=["GET"]),
    Route("/api/approvals/count", count_approvals, methods=["GET"]),
    Route("/api/approvals/{id:int}/approve", approve, methods=["POST"]),
    Route("/api/approvals/{id:int}/reject", reject, methods=["POST"]),
]
