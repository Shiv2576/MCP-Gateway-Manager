"""Admins run an MCP server from a container image. This only writes
HostedServer resources; the store operator deploys the image into its own
namespace and registers it as an ordinary server once it is up, and on delete
unregisters it (its tools disappear) before removing the namespace."""

from __future__ import annotations

import re
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HEADER_NAME, SERVER_NAME, HTTPError, _str, body, handler, require_admin
from .kube import AlreadyExists, hosted_resource

IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_ENV, MAX_ARGS = 30, 20


def _backend(request: Request):
    hosted = request.app.state.hosted
    if hosted is None:
        raise HTTPError(503, "hosted servers are not configured")
    return hosted


def _view(cr: dict) -> dict:
    meta, spec, status = cr["metadata"], cr["spec"], cr.get("status") or {}
    created = meta.get("creationTimestamp")
    return {
        "name": meta["name"],
        "image": spec["image"],
        "args": spec.get("args", []),
        "port": spec["port"],
        "path": spec["path"],
        "env": sorted(spec.get("env", {})),  # names only: values may be config an admin typed
        "token_env": spec.get("tokenEnv", ""),
        "owner": spec.get("owner", ""),
        "phase": "Deleting" if meta.get("deletionTimestamp") else status.get("phase", "Provisioning"),
        "message": status.get("message", ""),
        "url": status.get("url", ""),
        "limits": status.get("limits"),  # set by the operator: quota and container resources
        "created": datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None,
    }


def _spec(data: dict) -> dict:
    image = _str(data, "image")
    if not IMAGE.match(image):
        raise HTTPError(400, "image: a reference like ghcr.io/org/name:tag")
    args = data.get("args") or []
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(isinstance(a, str) and len(a) <= 200 for a in args):
        raise HTTPError(400, f"args: a list of at most {MAX_ARGS} strings")
    port = data.get("port", 8000)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HTTPError(400, "port: 1-65535")
    path = data.get("path") or "/mcp"
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 200 or any(c.isspace() for c in path):
        raise HTTPError(400, "path: must start with /, e.g. /mcp")
    env = data.get("env") or {}
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise HTTPError(400, f"env: an object of at most {MAX_ENV} variables")
    for k, v in env.items():
        if not ENV_NAME.match(k) or not isinstance(v, str) or len(v) > 2000:
            raise HTTPError(400, f"env: bad variable {k!r}")
    spec = {"image": image, "args": args, "port": port, "path": path, "env": env}
    # The operator makes a random token, gives it to the container in this
    # variable and to the gateway in auth_header, so no secret is ever typed in.
    token_env = _str(data, "token_env", required=False)
    if token_env:
        if not ENV_NAME.match(token_env) or token_env in env:
            raise HTTPError(400, "token_env: a variable name not already in env")
        header = _str(data, "auth_header", required=False) or "Authorization"
        if not HEADER_NAME.match(header):
            raise HTTPError(400, "auth_header is not a valid header name")
        spec |= {"tokenEnv": token_env, "authHeader": header}
    return spec


@handler
async def list_hosted(request: Request) -> Response:
    require_admin(request)
    items = await _backend(request).list()
    return JSONResponse(sorted((_view(c) for c in items), key=lambda s: s["created"] or 0))


@handler
async def create_hosted(request: Request) -> Response:
    me = require_admin(request)
    hosted = _backend(request)
    data = await body(request)
    name = _str(data, "name")
    if not SERVER_NAME.match(name) or name == "gateway":
        raise HTTPError(400, "name: 1-32 of a-z 0-9 and -, used as the tool prefix")
    spec = _spec(data)
    if request.app.state.db.execute("SELECT 1 FROM servers WHERE name = ?", (name,)).fetchone():
        raise HTTPError(409, f"'{name}' is already a server name")
    stores = request.app.state.stores
    if stores is not None and await stores.get(name) is not None:
        raise HTTPError(409, f"'{name}' is already a store name")
    try:
        cr = await hosted.create_object(hosted_resource(name, spec, me["id"], me["username"]))
    except AlreadyExists:
        raise HTTPError(409, f"a hosted server named '{name}' already exists") from None
    return JSONResponse(_view(cr), status_code=201)


@handler
async def delete_hosted(request: Request) -> Response:
    require_admin(request)  # org-wide like servers: any admin may remove one
    hosted = _backend(request)
    name = request.path_params["name"]
    if await hosted.get(name) is None:
        raise HTTPError(404, "no such hosted server")
    await hosted.delete(name)
    return JSONResponse({"ok": True}, status_code=202)


routes = [
    Route("/api/hosted", list_hosted, methods=["GET"]),
    Route("/api/hosted", create_hosted, methods=["POST"]),
    Route("/api/hosted/{name}", delete_hosted, methods=["DELETE"]),
]
