"""End-to-end: MCP client -> gateway -> real upstream MCP servers on local ports.

Run:  uv run pytest          (from the gateway folder)

Upstreams are the real notes and fx servers from services/demo-mcp, plus a
"slow" server whose tool sleeps longer than the gateway's call timeout, plus a
URL where nothing listens.
"""

from __future__ import annotations

import asyncio
import json
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
def serve(app, port: int | None = None):
    port = port or _free_port()
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
            bob_id = r.json()["id"]
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
            r = await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": list(ids.values())})
            assert r.status_code == 200
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


def test_rate_limit_per_user(tmp_path):
    from app import db as store
    from app.upstream import Upstreams

    ups = Upstreams(store.connect(str(tmp_path / "g.db")), call_timeout=1, list_timeout=1, refresh_interval=30, calls_per_minute=2)
    alice = {"id": 1, "username": "alice", "role": "admin"}
    bob = {"id": 2, "username": "bob", "role": "admin"}

    async def run():
        for _ in range(2):
            assert "Unknown tool" in text(await ups.call_tool(alice, "x__y", {}))
        assert "Too many calls" in text(await ups.call_tool(alice, "x__y", {}))
        assert "Unknown tool" in text(await ups.call_tool(bob, "x__y", {}))

    asyncio.run(run())


def test_customer_role_is_removed_from_an_old_database(tmp_path):
    import sqlite3

    from app import db as store

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'customer')), created_at REAL NOT NULL, customer_id INTEGER);
        CREATE TABLE api_keys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL);
        CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL, auth_header TEXT,
            auth_value TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, customer_tools TEXT NOT NULL DEFAULT '');
        INSERT INTO users VALUES (1, 'admin', 'h', 'admin', 0, NULL), (2, 'alice', 'h', 'customer', 0, 7);
        INSERT INTO api_keys VALUES (1, 1, 'k', 'h1', 'mcpg_', 0, NULL), (2, 2, 'k', 'h2', 'mcpg_', 0, NULL);
        INSERT INTO servers (name, url, created_at, customer_tools) VALUES ('woo', 'http://w', 0, 'my_orders');
        """
    )
    old.commit()
    old.close()

    conn = store.connect(path)
    assert [r["username"] for r in conn.execute("SELECT username FROM users")] == ["admin"]
    assert [r["user_id"] for r in conn.execute("SELECT user_id FROM api_keys")] == [1]
    assert "customer_tools" not in {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    try:
        store.create_user(conn, "bob", "pw-bob-12345", "customer")
        raise AssertionError("customer role still accepted")
    except sqlite3.IntegrityError:
        pass
    conn.close()
    store.connect(path)  # second start is a no-op


def fake_llm(seen: list):
    """OpenAI-style chat completions: asks for one tool call, then answers."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def complete(request):
        payload = await request.json()
        seen.append((request.headers.get("authorization"), payload))
        if payload["messages"][-1]["role"] == "tool":
            message = {"role": "assistant", "content": "Saved it."}
        else:
            call = {"id": "c1", "type": "function",
                    "function": {"name": "notes__save_note", "arguments": json.dumps({"text": "from chat"})}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
        return JSONResponse({"choices": [{"message": message}]})

    return Starlette(routes=[Route("/v1/chat/completions", complete, methods=["POST"])])


def test_chat_agent_calls_tools_as_the_logged_in_user(tmp_path):
    seen = []

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(fake_llm(seen)) as llm,
        ):
            settings = Settings(
                database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1],
                refresh_interval_s=0.5, openrouter_api_key="test-llm-key", llm_url=f"{llm}/v1/chat/completions",
            )
            with serve(create_app(settings)) as gw:
                admin = httpx2.AsyncClient(base_url=gw, timeout=10)
                await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
                notes_id = (await admin.post("/api/servers", json={
                    "name": "notes", "url": f"{notes}/mcp",
                    "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
                await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "server_ids": [notes_id]})
                bob = httpx2.AsyncClient(base_url=gw, timeout=10)
                await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
                deadline = time.time() + 5
                while not (await bob.get("/api/tools")).json():
                    assert time.time() < deadline
                    await asyncio.sleep(0.1)

                msg = {"messages": [{"role": "user", "content": "save a note"}]}
                assert (await httpx2.AsyncClient(base_url=gw).post("/api/chat", json=msg)).status_code == 401
                r = await bob.post("/api/chat", json=msg)
                assert r.status_code == 200, r.text
                out = r.json()
                assert out["reply"] == "Saved it."
                assert [(c["name"], c["arguments"], c["ok"]) for c in out["calls"]] == [
                    ("notes__save_note", {"text": "from chat"}, True)]

                auth, first = seen[0]
                assert auth == "Bearer test-llm-key"
                assert "notes__save_note" in {t["function"]["name"] for t in first["tools"]}
                assert "bob" in first["messages"][0]["content"]

                log = (await admin.get("/api/audit")).json()
                assert [(e["username"], e["tool"], e["ok"]) for e in log] == [("bob", "notes__save_note", 1)]

    asyncio.run(run())


def test_grants_upgrade_keeps_members_on_their_servers(tmp_path):
    from app import db as store

    path = str(tmp_path / "g.db")
    conn = store.connect(path)
    store.create_user(conn, "root", "root-password-123", "admin")
    bob = store.create_user(conn, "bob", "bob-password-123", "member")
    conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('notes', 'http://n', 0), ('fx', 'http://f', 0)")
    conn.execute("DROP TABLE user_servers")  # a database from before grants
    conn.close()

    conn = store.connect(path)
    granted = conn.execute(
        "SELECT u.username, s.name FROM user_servers g JOIN users u ON u.id = g.user_id"
        " JOIN servers s ON s.id = g.server_id ORDER BY s.name"
    ).fetchall()
    assert [tuple(r) for r in granted] == [("bob", "fx"), ("bob", "notes")]

    root = conn.execute("SELECT * FROM users WHERE username = 'root'").fetchone()
    member = conn.execute("SELECT * FROM users WHERE id = ?", (bob,)).fetchone()
    assert store.granted_server_ids(conn, root) is None
    assert len(store.granted_server_ids(conn, member)) == 2

    store.set_grants(conn, bob, [])
    conn.close()
    conn = store.connect(path)  # a later start does not grant again
    assert store.granted_server_ids(conn, member) == set()


def test_members_see_only_granted_servers(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0],
                        admin_password=ADMIN[1], refresh_interval_s=0.5)

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(create_app(settings)) as gw,
        ):
            admin = httpx2.AsyncClient(base_url=gw, timeout=10)
            await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
            ids = {}
            for body in (
                {"name": "notes", "url": f"{notes}/mcp", "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}},
                {"name": "fx", "url": f"{fx}/mcp", "auth": {"type": "bearer", "token": FX_TOKEN}},
            ):
                ids[body["name"]] = (await admin.post("/api/servers", json=body)).json()["id"]

            bad = await admin.post("/api/users", json={"username": "eve", "password": "eve-password-123", "server_ids": [999]})
            assert bad.status_code == 400
            r = await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "server_ids": [ids["notes"]]})
            assert r.status_code == 201 and r.json()["server_ids"] == [ids["notes"]]
            bob_id = r.json()["id"]
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["server_ids"] == [ids["notes"]] and users["admin"]["server_ids"] == []

            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
            deadline = time.time() + 5
            while len((await admin.get("/api/tools")).json()) < 4:
                assert time.time() < deadline
                await asyncio.sleep(0.1)

            assert {t["name"] for t in (await bob.get("/api/tools")).json()} == {"notes__save_note", "notes__list"}
            assert [s["name"] for s in (await bob.get("/api/servers")).json()] == ["notes"]
            async with gateway_client(gw, key) as mcp:
                assert {t.name for t in (await mcp.list_tools()).tools} == {"notes__save_note", "notes__list"}
                r = await mcp.call_tool("fx__list", {})
                assert r.is_error and "Unknown tool" in text(r)
            log = (await admin.get("/api/audit")).json()
            assert (log[0]["username"], log[0]["tool"], log[0]["ok"]) == ("bob", "fx__list", 0)

            assert (await bob.put(f"/api/users/{bob_id}/servers", json={"server_ids": []})).status_code == 403
            admin_id = users["admin"]["id"]
            assert (await admin.put(f"/api/users/{admin_id}/servers", json={"server_ids": []})).status_code == 400
            r = await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": [ids["fx"]]})
            assert r.status_code == 200
            assert {t["name"] for t in (await bob.get("/api/tools")).json()} == {"fx__convert", "fx__list"}

            # Removing a server removes its grants.
            await admin.delete(f"/api/servers/{ids['fx']}")
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["server_ids"] == []

    asyncio.run(run())


def test_admin_deletes_a_user(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings)) as gw:
            admin = httpx2.AsyncClient(base_url=gw, timeout=10)
            me = (await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})).json()
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            assert (await bob.delete(f"/api/users/{me['id']}")).status_code == 403
            assert (await admin.delete(f"/api/users/{me['id']}")).status_code == 400
            assert (await admin.delete("/api/users/999")).status_code == 404
            assert (await admin.delete(f"/api/users/{bob_id}")).status_code == 200

            assert (await bob.get("/api/me")).status_code == 401
            r = await httpx2.AsyncClient(base_url=gw).post(
                "/mcp", headers={"Authorization": f"Bearer {key}"}, json={})
            assert r.status_code == 401
            assert [u["username"] for u in (await admin.get("/api/users")).json()] == ["admin"]

    asyncio.run(run())
