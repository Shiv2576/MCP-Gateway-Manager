"""The PII Protection tab: the rule library, each tool's fields, and a test box. Admins only."""

from __future__ import annotations

import time

import anyio
import regex
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import pii
from .api import HTTPError, _str, body, handler, require_admin
from .upstream import Status


def _view(r: dict) -> dict:
    return {k: r[k] for k in ("id", "name", "type", "match", "server_id", "tool")}


@handler
async def list_rules(request: Request) -> Response:
    require_admin(request)
    rows = request.app.state.db.execute("SELECT * FROM pii_rules ORDER BY id").fetchall()
    return JSONResponse({
        "builtin": [{"name": r.name, "type": r.type, "match": r.match, "note": r.note} for r in pii.BUILTIN],
        "custom": [_view(r) for r in rows],
    })


@handler
async def create_rule(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    data = await body(request)
    name, kind, match = _str(data, "name"), _str(data, "type"), _str(data, "match")
    if not pii.NAME.match(name):
        raise HTTPError(400, "name: 2-40 of A-Z 0-9 and _, starting with a letter")
    server_id = tool = None
    if kind == "pattern":
        if len(match) > 200:
            raise HTTPError(400, "match: a regex of at most 200 characters")
        try:
            rx = regex.compile(match)  # the engine the filter uses
        except regex.error as e:
            raise HTTPError(400, f"match is not a valid regex: {e}") from None
        if any(m.start() == m.end() for m in rx.finditer("a a 1", timeout=0.5)):
            raise HTTPError(400, "match must not match an empty string (e.g. z* or \\b): it would match everywhere")
    elif kind == "key":
        if len(match) > 64:
            raise HTTPError(400, "match: a key of at most 64 characters")
    elif kind == "field":
        server_id, tool = data.get("server_id"), _str(data, "tool")
        if not isinstance(server_id, int) or isinstance(server_id, bool) or len(tool) > 128:
            raise HTTPError(400, "a field rule needs server_id and tool")
        if not pii.FIELD_PATH.match(match):
            raise HTTPError(400, "match must be a field path like orders[].customer")
        if conn.execute("SELECT 1 FROM servers WHERE id = ?", (server_id,)).fetchone() is None:
            raise HTTPError(400, "no such server")
    else:
        raise HTTPError(400, "type must be pattern, key or field")
    if kind != "field" and name in pii.BUILTIN_NAMES:
        raise HTTPError(400, f"{name} is a built-in rule; pick another name")
    row = conn.execute(  # a duplicate is a 409 from @handler
        "INSERT INTO pii_rules (name, type, match, server_id, tool, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (name, kind, match, server_id, tool, time.time()),
    ).fetchone()
    return JSONResponse(_view(row), status_code=201)


@handler
async def delete_rule(request: Request) -> Response:
    require_admin(request)
    gone = request.app.state.db.execute(
        "DELETE FROM pii_rules WHERE id = ? RETURNING id", (request.path_params["id"],)
    ).fetchone()
    if gone is None:
        raise HTTPError(404, "no such rule")
    return JSONResponse({"ok": True})


@handler
async def fields(request: Request) -> Response:
    """Every field each enabled server's tools return, from the output schemas the
    health loop already cached; never calls an upstream."""
    require_admin(request)
    conn = request.app.state.db
    keys = pii.key_map(pii.load_rules(conn))
    status = request.app.state.upstreams.status
    out = []
    for s in conn.execute("SELECT id, name FROM servers WHERE enabled = 1 ORDER BY name").fetchall():
        ticked: dict[str, dict[str, dict]] = {}
        for r in conn.execute(
            "SELECT id, name, tool, match FROM pii_rules WHERE type = 'field' AND server_id = ?", (s["id"],)
        ):
            ticked.setdefault(r["tool"], {})[r["match"]] = {"id": r["id"], "name": r["name"]}
        tools = []
        for t in sorted(status.get(s["id"], Status()).tools, key=lambda t: t.name):
            found = pii.schema_fields(t.output_schema)
            mine = ticked.get(t.name, {})
            paths = {f["path"] for f in found}
            tools.append({
                "tool": t.name,
                "has_schema": bool(found),
                "fields": [{**f, "rule": mine.get(f["path"]), "covered_by": pii.covered_by(f["path"], keys)}
                           for f in found],
                "stale": [{"path": p, **r} for p, r in mine.items() if p not in paths],
            })
        out.append({"server_id": s["id"], "server": s["name"], "tools": tools})
    return JSONResponse(out)


@handler
async def test_rules(request: Request) -> Response:
    """Run the rules over a pasted payload. Nothing is stored or audited."""
    require_admin(request)
    data = await body(request)
    if "payload" not in data:
        raise HTTPError(400, "'payload' is required")
    rules = pii.load_rules(request.app.state.db)
    try:
        with anyio.fail_after(pii.FILTER_TIMEOUT):
            value, counts = await anyio.to_thread.run_sync(
                pii.redact_value, data["payload"], rules, data.get("server_id"), data.get("tool"),
                abandon_on_cancel=True)
    except TimeoutError:
        raise HTTPError(400, "the PII filter took too long on this payload: check the custom patterns") from None
    return JSONResponse({"payload": value, "counts": dict(counts)})


routes = [
    Route("/api/pii-rules", list_rules, methods=["GET"]),
    Route("/api/pii-rules", create_rule, methods=["POST"]),
    Route("/api/pii-rules/fields", fields, methods=["GET"]),
    Route("/api/pii-rules/test", test_rules, methods=["POST"]),
    Route("/api/pii-rules/{id:int}", delete_rule, methods=["DELETE"]),
]
