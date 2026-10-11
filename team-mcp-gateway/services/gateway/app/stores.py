"""Admins create and delete WooCommerce stores. This only writes Store
resources; the store operator provisions them and registers each store's MCP
server in the gateway when it is ready."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HTTPError, _str, body, handler, require_admin
from .kube import OWNER_LABEL, AlreadyExists

MAX_STORES = 3
TEMPLATES = ("catalog-a", "catalog-b", "blank")
# 2-20 chars: "<name>__<tool>" must stay within the 64 chars the chat agent accepts.
STORE_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$")
# A store named X gets the Ingress host X.localhost; these hosts are taken.
RESERVED = {"gateway", "dash", "store", "www"}
# One gateway replica, so this makes "count, then create" atomic: two creates at
# once cannot both see room under the cap.
_create_lock = asyncio.Lock()


def _backend(request: Request):
    stores = request.app.state.stores
    if stores is None:
        raise HTTPError(503, "store provisioning is not configured")
    return stores


def _view(cr: dict) -> dict:
    meta, status = cr["metadata"], cr.get("status") or {}
    created = meta.get("creationTimestamp")
    return {
        "name": meta["name"],
        "template": cr["spec"]["template"],
        "owner": cr["spec"].get("owner", ""),
        "owner_id": int(meta["labels"][OWNER_LABEL]),
        "phase": "Deleting" if meta.get("deletionTimestamp") else status.get("phase", "Provisioning"),
        "message": status.get("message", ""),
        "url": status.get("url") or f"http://{meta['name']}.localhost",
        "created": datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None,
    }


@handler
async def list_stores(request: Request) -> Response:
    require_admin(request)
    items = await _backend(request).list()
    return JSONResponse(sorted((_view(c) for c in items), key=lambda s: s["created"] or 0))


@handler
async def create_store(request: Request) -> Response:
    """Safe to retry: the same name, owner and template returns the existing store."""
    me = require_admin(request)
    stores = _backend(request)
    data = await body(request)
    name, template = _str(data, "name"), _str(data, "template")
    if not STORE_NAME.match(name):
        raise HTTPError(400, "name: 2-20 of a-z 0-9 and -, starting with a letter")
    if name in RESERVED:
        raise HTTPError(400, f"'{name}' is reserved (its host {name}.localhost is in use)")
    if template not in TEMPLATES:
        raise HTTPError(400, f"template must be one of {', '.join(TEMPLATES)}")
    async with _create_lock:
        existing = await stores.get(name)
        if existing is not None:
            view = _view(existing)
            if view["owner_id"] == me["id"] and view["template"] == template:
                return JSONResponse(view)
            raise HTTPError(409, f"a store named '{name}' already exists")
        if request.app.state.db.execute("SELECT 1 FROM servers WHERE name = ?", (name,)).fetchone():
            raise HTTPError(409, f"'{name}' is already a server name")
        if len(await stores.list(owner_id=me["id"])) >= MAX_STORES:
            raise HTTPError(400, f"store limit reached: at most {MAX_STORES} stores per admin")
        try:
            cr = await stores.create(name, template, me["id"], me["username"])
        except AlreadyExists:
            raise HTTPError(409, f"a store named '{name}' already exists") from None
    return JSONResponse(_view(cr), status_code=201)


@handler
async def delete_store(request: Request) -> Response:
    me = require_admin(request)
    stores = _backend(request)
    name = request.path_params["name"]
    cr = await stores.get(name)
    if cr is None or _view(cr)["owner_id"] != me["id"]:
        raise HTTPError(404, "no such store")
    await stores.delete(name)
    return JSONResponse({"ok": True}, status_code=202)


async def delete_all_for(request: Request, user_id: int) -> None:
    stores = request.app.state.stores
    if stores is None:
        return
    for cr in await stores.list(owner_id=user_id):
        await stores.delete(cr["metadata"]["name"])


routes = [
    Route("/api/stores", list_stores, methods=["GET"]),
    Route("/api/stores", create_store, methods=["POST"]),
    Route("/api/stores/{name}", delete_store, methods=["DELETE"]),
]
