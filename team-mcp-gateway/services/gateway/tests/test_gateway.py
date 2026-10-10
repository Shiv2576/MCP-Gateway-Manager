"""End-to-end: MCP client -> gateway -> real upstream MCP servers on local ports.

Run:  uv run pytest          (from the gateway folder)

Upstreams are the real notes and fx servers from services/demo-mcp, plus a
"slow" server whose tool sleeps longer than the gateway's call timeout, plus a
URL where nothing listens.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
import time
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "demo-mcp"))
from demo_mcp import TokenGuard, build_fx, build_notes  # noqa: E402

from app.main import ConfigError, Settings, create_app  # noqa: E402

ADMIN = ("admin", "admin-password-123")
NOTES_KEY = "notes-key-0123456789abcdefghij"
FX_TOKEN = "fx-token-0123456789abcdefghijk"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(app):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def http_app(server: MCPServer, header: str | None = None, value: str | None = None):
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )
    return TokenGuard(app, header, value) if header else app


def build_slow() -> MCPServer:
    server = MCPServer("slow")

    @server.tool(name="list")
    async def slow_list() -> str:
        """Answers after 5 seconds."""
        await asyncio.sleep(5)
        return "too late"

    return server


@contextlib.asynccontextmanager
async def gateway_client(url: str, key: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=10) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
            yield client


def text(result) -> str:
    return " ".join(c.text for c in result.content if hasattr(c, "text"))


def test_refuses_to_start_without_a_first_admin(tmp_path):
    try:
        create_app(Settings(database_path=str(tmp_path / "g.db")))
    except ConfigError:
        return
    raise AssertionError("started with no way to log in")


def test_the_whole_gateway(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "g.db"),
        admin_username=ADMIN[0],
        admin_password=ADMIN[1],
        call_timeout_s=1,
        list_timeout_s=1,
        refresh_interval_s=0.5,
    )

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(http_app(build_slow())) as slow,
            serve(create_app(settings)) as gw,
        ):
            admin = httpx2.AsyncClient(base_url=gw)
            member = httpx2.AsyncClient(base_url=gw)

            # Real login: wrong password refused, right one sets a session cookie.
            r = await admin.post("/api/login", json={"username": "admin", "password": "nope-nope-nope"})
            assert r.status_code == 401
            r = await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
            assert r.status_code == 200 and r.json()["role"] == "admin"
            assert "httponly" in r.headers["set-cookie"].lower()
            assert (await httpx2.AsyncClient(base_url=gw).get("/api/servers")).status_code == 401

            # Admin creates a member; the member logs in and makes an API key.
            r = await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            assert r.status_code == 201
            await member.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await member.post("/api/keys", json={"name": "inspector"})).json()["key"]

            # Members cannot manage servers.
            r = await member.post("/api/servers", json={"name": "x", "url": "http://x"})
            assert r.status_code == 403

            # Admin adds four upstreams: both credential kinds, a slow one, a dead one.
            add = [
                {"name": "notes", "url": f"{notes}/mcp",
                 "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}},
                {"name": "fx", "url": f"{fx}/mcp", "auth": {"type": "bearer", "token": FX_TOKEN}},
                {"name": "slow", "url": f"{slow}/mcp"},
                {"name": "dead", "url": f"http://127.0.0.1:{_free_port()}/mcp"},
            ]
            ids = {}
            for body in add:
                r = await admin.post("/api/servers", json=body)
                assert r.status_code == 201, r.text
                ids[body["name"]] = r.json()["id"]
            assert (await admin.post("/api/servers", json=add[0])).status_code == 409
            bad = await admin.post("/api/servers", json={"name": "a__b", "url": "http://x"})
            assert bad.status_code == 400

            async def statuses():
                return {s["name"]: s for s in (await admin.get("/api/servers")).json()}

            deadline = time.time() + 5
            while True:
                st = await statuses()
                if all(st[n]["status"] != "pending" for n in st):
                    break
                assert time.time() < deadline, st
                await asyncio.sleep(0.1)
            assert st["notes"]["status"] == st["fx"]["status"] == st["slow"]["status"] == "ok"
            assert st["dead"]["status"] == "unreachable" and "unreachable" in st["dead"]["error"]

            # Credentials never leave the server.
            for client in (admin, member):
                raw = (await client.get("/api/servers")).text
                assert NOTES_KEY not in raw and FX_TOKEN not in raw
                assert '"has_credential":true' in raw.replace(" ", "")

            # One MCP endpoint: every enabled server's tools, prefixed.
            async with gateway_client(gw, key) as mcp:
                names = {t.name for t in (await mcp.list_tools()).tools}
                assert names == {
                    "notes__save_note", "notes__list",
                    "fx__convert", "fx__list",
                    "slow__list",
                }

                # Routed to the right server, even with the same upstream name.
                await mcp.call_tool("notes__save_note", {"text": "ship it"})
                r = await mcp.call_tool("notes__list", {})
                assert not r.is_error and "ship it" in text(r)
                r = await mcp.call_tool("fx__list", {})
                assert not r.is_error and "USD" in text(r)
                r = await mcp.call_tool("fx__convert", {"amount_inr": 1000, "to": "usd"})
                assert r.structured_content["amount"] == 12.0

                # A slow upstream times out with a clear error, and costs only its own call.
                started = time.monotonic()
                r = await mcp.call_tool("slow__list", {})
                assert r.is_error and "did not answer within 1s" in text(r)
                assert time.monotonic() - started < 3
                r = await mcp.call_tool("fx__list", {})
                assert not r.is_error

                # Listing stays fast while the slow server hangs on a call.
                started = time.monotonic()
                await mcp.list_tools()
                assert time.monotonic() - started < 0.5

                r = await mcp.call_tool("nope", {})
                assert r.is_error and "<server>__<tool>" in text(r)

                # Disable: tools vanish at once and calls are refused.
                await admin.patch(f"/api/servers/{ids['notes']}", json={"enabled": False})
                names = {t.name for t in (await mcp.list_tools()).tools}
                assert "notes__list" not in names and "fx__list" in names
                r = await mcp.call_tool("notes__list", {})
                assert r.is_error and "disabled" in text(r)

                # Edit without "auth" keeps the stored credential.
                await admin.patch(f"/api/servers/{ids['notes']}", json={"enabled": True})
                r = await mcp.call_tool("notes__list", {})
                assert not r.is_error, text(r)

                # Remove.
                assert (await admin.delete(f"/api/servers/{ids['dead']}")).status_code == 200
                assert "dead" not in await statuses()

            # Every call is in the audit log under the member who made it.
            log = (await admin.get("/api/audit")).json()
            assert {e["username"] for e in log} == {"bob"}
            assert any(e["tool"] == "slow__list" and not e["ok"] for e in log)
            assert (await member.get("/api/audit")).status_code == 403

            # Revoke the key: the next MCP request is refused.
            key_id = (await member.get("/api/keys")).json()[0]["id"]
            assert (await member.delete(f"/api/keys/{key_id}")).status_code == 200
            async with httpx2.AsyncClient() as raw:
                r = await raw.post(
                    f"{gw}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {key}"},
                )
                assert r.status_code == 401

            # The dashboard sees the same tools and can try one, audited as the admin.
            tools = {t["name"] for t in (await admin.get("/api/tools")).json()}
            assert "fx__convert" in tools and "inputSchema" in (await admin.get("/api/tools")).json()[0]
            r = await admin.post("/api/tools/call", json={"name": "fx__convert", "arguments": {"amount_inr": 100, "to": "EUR"}})
            assert r.status_code == 200 and r.json()["structuredContent"]["amount"] == 1.1
            assert (await admin.get("/api/audit")).json()[0]["username"] == "admin"

            await admin.aclose()
            await member.aclose()

    asyncio.run(run())
