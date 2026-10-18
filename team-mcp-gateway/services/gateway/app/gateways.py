"""Gateways: separate edge deployments, each serving /mcp for the servers attached to it.

The control plane keeps the gateway's row (what it serves, read by the edge on
every request) and an McpGateway resource (that it runs); the store operator
installs the edge release gw-<slug> from that resource.
"""

from __future__ import annotations

import collections
import re
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import HTTPError, _server_ids, _str, _url, body, current_user, handler, require_admin
from .kube import AlreadyExists, gateway_resource

# 2-20 chars: it is the release name gw-<slug> and the host <slug>.gw.<base domain>.
SLUG = re.compile(r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$")


def _backend(request: Request):
    kube = request.app.state.gateways
    if kube is None:
        raise HTTPError(503, "gateway deployment is not configured")
    return kube


def _view(request: Request, row: dict, servers: list[dict], cr: dict | None) -> dict:
    meta, status = (cr or {}).get("metadata") or {}, (cr or {}).get("status") or {}
    s = request.app.state.settings
    url = status.get("url") or f"{s.url_scheme}://{row['slug']}.gw.{s.base_domain}"
    if meta.get("deletionTimestamp"):
        phase = "Deleting"
    else:
        phase = status.get("phase", "Provisioning") if cr else "Missing"
    return {
        "slug": row["slug"],
        "name": row["name"],
        "public_url": row["public_url"],
        "pii_protection": bool(row.get("pii_protection")),
        "url": url,
        "mcp_url": (row["public_url"] or url) + "/mcp",
        "phase": phase,
        "message": status.get("message", ""),
        "servers": servers,
    }


def _orphan(request: Request, cr: dict) -> dict:
    """An McpGateway with no row, e.g. its create response was lost and nobody retried."""
    spec = cr.get("spec") or {}
    row = {"slug": cr["metadata"]["name"], "name": spec.get("displayName", ""), "public_url": spec.get("publicUrl") or None}
    return {**_view(request, row, [], cr), "phase": "Orphaned", "message": "running without a gateway row: retry the create or delete it"}


def _by_slug(conn, slug: str) -> dict | None:
    return conn.execute("SELECT * FROM gateways WHERE slug = ?", (slug,)).fetchone()


def _row(request: Request) -> dict:
    row = _by_slug(request.app.state.db, request.path_params["slug"])
    if row is None:
        raise HTTPError(404, "no such gateway")
    return row


def _attached(conn) -> dict[int, list[dict]]:
    out = collections.defaultdict(list)
    for r in conn.execute(
        "SELECT gs.gateway_id, gs.identity, s.id, s.name FROM gateway_servers gs"
        " JOIN servers s ON s.id = gs.server_id ORDER BY s.name"
    ):
        out[r["gateway_id"]].append({"id": r["id"], "name": r["name"], "identity": r["identity"]})
    return out


@handler
async def list_gateways(request: Request) -> Response:
    current_user(request)  # members read it too: it tells them which URL to use
    crs = {c["metadata"]["name"]: c for c in await _backend(request).list()}
    conn = request.app.state.db
    attached = _attached(conn)
    rows = conn.execute("SELECT * FROM gateways ORDER BY created_at").fetchall()
    known = {g["slug"] for g in rows}
    return JSONResponse(
        [_view(request, g, attached[g["id"]], crs.get(g["slug"])) for g in rows]
        + [_orphan(request, cr) for slug, cr in sorted(crs.items()) if slug not in known]
    )


@handler
async def create_gateway(request: Request) -> Response:
    """Safe to retry. Whatever failed before (the cluster refused, the response
    was lost after the resource was made, a crash between the row and the
    resource), sending the same request again finishes the job."""
    require_admin(request)
    kube = _backend(request)
    data = await body(request)
    slug, name = _str(data, "slug"), _str(data, "name")
    if not SLUG.match(slug):
        raise HTTPError(400, "slug: 2-20 of a-z 0-9 and -, starting with a letter")
    public_url = _url(_str(data, "public_url")).rstrip("/") if data.get("public_url") else None
    tailnet = request.app.state.settings.tailnet_domain
    if public_url is None and tailnet:
        public_url = f"https://{slug}-gw.{tailnet}"  # the edge chart gets a Tailscale Funnel Ingress for it
    pii = data.get("pii_protection", False)
    if not isinstance(pii, bool):
        raise HTTPError(400, "pii_protection must be true or false")
    conn = request.app.state.db
    taken = HTTPError(409, f"a gateway named '{slug}' already exists")

    row = _by_slug(conn, slug)
    inserted = row is None
    if inserted:
        try:
            row = conn.execute(
                "INSERT INTO gateways (slug, name, public_url, pii_protection, created_at) VALUES (?, ?, ?, ?, ?) RETURNING *",
                (slug, name, public_url, pii, time.time()),
            ).fetchone()
        except store.IntegrityError:  # a concurrent create of the same slug
            raise taken from None
    elif (row["name"], row["public_url"]) != (name, public_url):
        raise taken

    def undo():
        if inserted:
            conn.execute("DELETE FROM gateways WHERE id = ?", (row["id"],))

    want = gateway_resource(slug, name, public_url)
    try:
        cr = await kube.create_object(want)
    except AlreadyExists:
        cr = await kube.get(slug)
        if cr is None or cr.get("spec") != want["spec"]:
            undo()
            raise taken from None
        # An earlier attempt made it: adopt it.
    except Exception as e:  # no row without a resource, so a retry starts clean
        undo()
        raise HTTPError(502, f"could not create the gateway deployment: {e}"[:300]) from None
    return JSONResponse(_view(request, row, _attached(conn)[row["id"]], cr), status_code=201 if inserted else 200)


@handler
async def delete_gateway(request: Request) -> Response:
    """Also removes an orphaned McpGateway that has no row."""
    require_admin(request)
    kube = _backend(request)
    slug = request.path_params["slug"]
    conn = request.app.state.db
    row = _by_slug(conn, slug)
    if row is None and await kube.get(slug) is None:
        raise HTTPError(404, "no such gateway")
    await kube.delete(slug)  # the operator uninstalls the edge
    if row is not None:
        conn.execute("DELETE FROM gateways WHERE id = ?", (row["id"],))
    return JSONResponse({"ok": True}, status_code=202)


@handler
async def set_gateway_servers(request: Request) -> Response:
    """Replace the attached set. Edges read it on every request, so it applies at once."""
    require_admin(request)
    row = _row(request)
    conn = request.app.state.db
    ids = _server_ids(conn, await body(request))
    with conn.transaction() as tx:
        # Only removed servers go, and their policies and denies with them (cascade);
        # servers that stay keep their identity and policies.
        tx.execute("DELETE FROM gateway_servers WHERE gateway_id = ? AND NOT (server_id = ANY(?))", (row["id"], ids))
        tx.executemany(
            "INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            [(row["id"], s) for s in ids],
        )
    return JSONResponse({"ok": True, "server_ids": ids})


@handler
async def set_identity(request: Request) -> Response:
    """Whose credential calls through this attachment use: the server's (shared) or each member's (personal)."""
    require_admin(request)
    row = _row(request)
    conn = request.app.state.db
    identity = (await body(request)).get("identity")
    if identity not in ("shared", "personal"):
        raise HTTPError(400, "identity must be shared or personal")
    server = conn.execute(
        "SELECT s.personal_header FROM gateway_servers gs JOIN servers s ON s.id = gs.server_id"
        " WHERE gs.gateway_id = ? AND gs.server_id = ?",
        (row["id"], request.path_params["id"]),
    ).fetchone()
    if server is None:
        raise HTTPError(404, "that server is not attached to this gateway")
    if identity == "personal" and not server["personal_header"]:
        raise HTTPError(400, "set the server's personal credential header first")
    conn.execute(
        "UPDATE gateway_servers SET identity = ? WHERE gateway_id = ? AND server_id = ?",
        (identity, row["id"], request.path_params["id"]),
    )
    return JSONResponse({"ok": True, "identity": identity})


@handler
async def set_pii(request: Request) -> Response:
    """PII protection on or off. Edges read it on every call, so it applies at once."""
    require_admin(request)
    row = _row(request)
    enabled = (await body(request)).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPError(400, "enabled must be true or false")
    request.app.state.db.execute("UPDATE gateways SET pii_protection = ? WHERE id = ?", (enabled, row["id"]))
    return JSONResponse({"ok": True, "pii_protection": enabled})


routes = [
    Route("/api/gateways", list_gateways, methods=["GET"]),
    Route("/api/gateways", create_gateway, methods=["POST"]),
    Route("/api/gateways/{slug}", delete_gateway, methods=["DELETE"]),
    Route("/api/gateways/{slug}/servers", set_gateway_servers, methods=["PUT"]),
    Route("/api/gateways/{slug}/servers/{id:int}/identity", set_identity, methods=["PUT"]),
    Route("/api/gateways/{slug}/pii", set_pii, methods=["PUT"]),
]
