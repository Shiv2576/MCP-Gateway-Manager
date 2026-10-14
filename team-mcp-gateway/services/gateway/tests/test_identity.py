"""Shared vs personal upstream identity: whose credential a tool call carries."""

from __future__ import annotations

import asyncio

import httpx2
from cryptography.fernet import Fernet
from mcp.types import ToolAnnotations
from mcp.server.mcpserver import Context, MCPServer

from tests import KEY
from app import db as store
from app.credentials import Vault
from app.main import ConfigError, Settings, create_app
from tests.test_gateway import (
    SETUP, FakeGateways, add_denies, add_gateway, add_team, edge_settings, gateway_client, http_app, serve,
    setup_admin, text, wait_tools,
)

SHARED = "shared-secret"
MISSING = ("Server 'echo' uses your personal credential on gateway 'main', and you have not added one."
           " Add it in the dashboard under My credentials.")
UNREADABLE = "Your saved credential for server 'echo' cannot be read. Enter it again in the dashboard under My credentials."


def build_echo(calls: list) -> MCPServer:
    """Answers with the two headers a call arrived with, reversed so the gateway's
    credential scrub leaves them readable, and counts calls."""
    server = MCPServer("echo")

    @server.tool(name="whoami", annotations=ToolAnnotations(read_only_hint=True))
    async def whoami(ctx: Context) -> str:
        headers = ctx.request_context.request.headers
        calls.append(1)
        return f"{headers.get('x-person')}|{headers.get('x-api-key')}"[::-1]

    return server


def test_config_needs_credentials_key(db_url):
    for key in ("", "nope"):
        try:
            create_app(Settings(database_url=db_url, setup_token=SETUP, credentials_key=key))
        except ConfigError as e:
            assert "CREDENTIALS_KEY" in str(e)
        else:
            raise AssertionError(f"started with credentials_key={key!r}")


async def echo_gateway(admin, echo_url: str) -> int:
    sid = (await admin.post("/api/servers", json={"name": "echo", "url": f"{echo_url}/mcp",
           "auth": {"type": "header", "name": "X-API-Key", "value": SHARED}})).json()["id"]
    await add_gateway(admin, "main", [sid])
    return sid


async def member(admin, gw: str, name: str) -> tuple[int, str]:
    uid = (await admin.post("/api/users", json={"username": name, "password": f"{name}-password-123"})).json()["id"]
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    await c.post("/api/login", json={"username": name, "password": f"{name}-password-123"})
    return uid, (await c.post("/api/keys", json={"name": "k"})).json()["key"]


def test_personal_identity(db_url):
    calls: list = []

    async def run():
        with (
            serve(http_app(build_echo(calls))) as echo,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                                      refresh_interval_s=0.5), gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            sid = await echo_gateway(admin, echo)
            bob_id, bob = await member(admin, gw, "bob")
            carol_id, carol = await member(admin, gw, "carol")
            await add_team(admin, "A", [bob_id, carol_id], "main", [(sid, "whoami")])
            conn = store.connect(db_url, create_schema=False)
            audit = lambda: conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()  # noqa: E731
            vault = Vault(KEY)

            async def whoami(key):
                async with gateway_client(edge, key) as mcp:
                    r = await mcp.call_tool("echo__whoami", {})
                    return r.is_error, text(r) if r.is_error else text(r)[::-1]

            with serve(create_app(edge_settings(db_url, "main", calls_per_minute=60))) as edge:
                await wait_tools(edge, bob, 1)
                # Shared: the server's credential, as before.
                assert await whoami(bob) == (False, f"None|{SHARED}")
                assert (audit()["identity"], audit()["ok"]) == ("shared", 1)

                conn.execute("UPDATE servers SET personal_header = 'X-Person' WHERE id = ?", (sid,))
                conn.execute("UPDATE gateway_servers SET identity = 'personal' WHERE server_id = ?", (sid,))
                # No credential: refused, never called, never the shared one.
                before = len(calls)
                assert await whoami(bob) == (True, MISSING)
                assert (audit()["identity"], audit()["ok"]) == (None, 0)
                # Sealed with another key: unreadable, also never called.
                other = Fernet(Fernet.generate_key()).encrypt(b"bob-key").decode()
                conn.execute("INSERT INTO user_credentials (user_id, server_id, secret, updated_at) VALUES (?, ?, ?, 0)",
                             (bob_id, sid, other))
                assert await whoami(bob) == (True, UNREADABLE)
                assert len(calls) == before

                # Each member's own value, and only it, even when calls overlap.
                conn.execute("UPDATE user_credentials SET secret = ? WHERE user_id = ?", (vault.seal("bob-key"), bob_id))
                conn.execute("INSERT INTO user_credentials (user_id, server_id, secret, updated_at) VALUES (?, ?, ?, 0)",
                             (carol_id, sid, vault.seal("carol-key")))
                results = await asyncio.gather(*(whoami(k) for k in [bob, carol] * 20))
                assert results == [(False, "bob-key|None"), (False, "carol-key|None")] * 20
                assert (audit()["identity"], audit()["ok"]) == ("personal", 1)

                # Same header name as the shared credential: the member's value replaces it.
                conn.execute("UPDATE servers SET personal_header = 'X-API-Key' WHERE id = ?", (sid,))
                assert await whoami(bob) == (False, "None|bob-key")

                # Header cleared behind the API's back: fails closed.
                conn.execute("UPDATE servers SET personal_header = NULL WHERE id = ?", (sid,))
                before = len(calls)
                assert await whoami(bob) == (True, MISSING)
                assert len(calls) == before

                # A denied tool: no credential is even looked at.
                conn.execute("UPDATE servers SET personal_header = 'X-Person' WHERE id = ?", (sid,))
                await add_denies(admin, bob_id, "main", [(sid, "whoami")])
                ok, msg = await whoami(bob)
                assert ok and "Unknown tool" in msg and len(calls) == before
            conn.close()

    asyncio.run(run())


def test_credentials_api(db_url, caplog):
    calls: list = []

    async def run():
        with (
            serve(http_app(build_echo(calls))) as echo,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                                      refresh_interval_s=0.5), gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            sid = await echo_gateway(admin, echo)
            plain = (await admin.post("/api/servers", json={"name": "plain", "url": f"{echo}/mcp"})).json()["id"]
            bob_id, bob_key = await member(admin, gw, "bob")
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            team = await add_team(admin, "A", [bob_id], "main", [(sid, "whoami")])
            server = f"/api/servers/{sid}"
            identity = f"/api/gateways/main/servers/{sid}/identity"

            # personal_header: a real header name the transport does not own.
            for bad in ("Host", "content-type", "Mcp-Session-Id", "bad header", 7):
                assert (await admin.patch(server, json={"personal_header": bad})).status_code == 400, bad
            assert (await admin.put(identity, json={"identity": "personal"})).status_code == 400  # none yet
            assert (await admin.patch(server, json={"personal_header": "X-Person"})).status_code == 200
            assert {s["name"]: s["personal_header"] for s in (await admin.get("/api/servers")).json()} == {
                "echo": "X-Person", "plain": None}
            assert (await admin.put(identity, json={"identity": "personal"})).status_code == 200
            assert (await admin.patch(server, json={"personal_header": None})).status_code == 400  # in use

            mine = (await bob.get("/api/me/credentials")).json()
            assert mine == [{"server_id": sid, "server": "echo", "personal_header": "X-Person", "set": False,
                             "updated_at": None, "gateways": ["main"]}]
            url = f"/api/me/credentials/{sid}"
            for bad in ("", " ", "x" * 4097, "a\nb-secret", 5):
                r = await bob.put(url, json={"value": bad})
                assert r.status_code == 400 and (not isinstance(bad, str) or bad.strip() == "" or bad not in r.text), bad
            assert (await bob.put(f"/api/me/credentials/{plain}", json={"value": "v"})).status_code == 400
            assert (await bob.put("/api/me/credentials/999", json={"value": "v"})).status_code == 404
            r = await bob.put(url, json={"value": "  bob-key "})
            assert r.status_code == 200 and r.json()["set"] is True and "bob-key" not in r.text
            conn = store.connect(db_url, create_schema=False)
            secret = conn.execute("SELECT secret FROM user_credentials").fetchone()["secret"]
            assert secret.startswith("gAAAAA") and Vault(KEY).open(secret) == "bob-key"
            assert (await bob.put(url, json={"value": "bob-key-2"})).status_code == 200

            # The call goes out with it.
            with serve(create_app(edge_settings(db_url, "main"))) as edge:
                await wait_tools(edge, bob_key, 1)
                async with gateway_client(edge, bob_key) as mcp:
                    assert text(await mcp.call_tool("echo__whoami", {}))[::-1] == "bob-key-2|None"

            users = (await admin.get("/api/users")).json()
            assert [u["credentials"] for u in users if u["username"] == "bob"][0][0]["server"] == "echo"
            # No longer reachable: still listed, so it can be removed.
            await admin.put(f"/api/teams/{team}/members", json={"user_ids": []})
            mine = (await bob.get("/api/me/credentials")).json()
            assert [(m["server"], m["set"], m["gateways"]) for m in mine] == [("echo", True, [])]
            assert (await bob.delete(url)).status_code == 200
            assert (await bob.delete(url)).status_code == 404
            assert (await bob.get("/api/me/credentials")).json() == []

            log = [(e["username"], e["tool"], e["server"], e["ok"]) for e in (await admin.get("/api/audit")).json()]
            assert [e for e in log if e[1].startswith("credential:")] == [
                ("bob", "credential:delete", "echo", 1), ("bob", "credential:replace", "echo", 1),
                ("bob", "credential:set", "echo", 1)]
            everything = str(log) + str(users) + str(conn.execute("SELECT * FROM audit_log").fetchall()) + caplog.text
            assert "bob-key" not in everything and "b-secret" not in everything
            conn.close()

    asyncio.run(run())


def test_shared_credential_is_encrypted_at_rest(db_url):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5)
        with (
            serve(http_app(build_echo([]), "X-API-Key", SHARED)) as echo,
            serve(create_app(settings, gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            sid = await echo_gateway(admin, echo)
            key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
            conn = store.connect(db_url, create_schema=False)
            stored = lambda: conn.execute("SELECT auth_value FROM servers WHERE id = ?", (sid,)).fetchone()["auth_value"]  # noqa: E731
            assert SHARED not in stored() and Vault(KEY).open(stored()) == SHARED
            with serve(create_app(edge_settings(db_url, "main"))) as edge:
                await wait_tools(edge, key, 1)  # the echo's guard let the decrypted header in

            # A row from before encryption is sealed when the control plane starts.
            conn.execute("UPDATE servers SET auth_value = ? WHERE id = ?", (SHARED, sid))
            create_app(settings, gateways=FakeGateways())
            assert Vault(KEY).open(stored()) == SHARED

            # Sealed with another key: refused, never sent as garbage.
            conn.execute("UPDATE servers SET auth_value = ? WHERE id = ?", (Vault(Fernet.generate_key().decode()).seal(SHARED), sid))
            with serve(create_app(edge_settings(db_url, "main"))) as edge:
                async with gateway_client(edge, key) as mcp:
                    r = await mcp.call_tool("echo__whoami", {})
            assert r.is_error and "cannot be read" in text(r)

    asyncio.run(run())
