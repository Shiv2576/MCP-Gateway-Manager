"""Edges: separate gateway deployments, each serving only the servers attached to it."""

from __future__ import annotations

import asyncio
import time

import httpx2

from tests import KEY  # noqa: E402
from app import db as store
from app.main import ConfigError, Settings, create_app
from tests.test_gateway import (
    FX_TOKEN, NOTES_KEY, SETUP, FakeGateways, _free_port, add_gateway, add_team, edge_settings,
    gateway_client, http_app, serve, setup_admin, text, wait_tools,
)

from demo_mcp import build_fx, build_notes  # noqa: E402  (tests.test_gateway puts it on sys.path)


def test_mode_and_slug_are_checked(db_url):
    for s in (Settings(mode="edge", database_url=db_url, credentials_key=KEY), Settings(mode="nope", database_url=db_url, credentials_key=KEY, setup_token=SETUP)):
        try:
            create_app(s)
        except ConfigError:
            continue
        raise AssertionError(f"started with mode={s.mode!r} slug={s.gateway_slug!r}")


def test_each_edge_serves_only_its_servers(db_url):
    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
                             gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            fid = (await admin.post("/api/servers", json={"name": "fx", "url": f"{fx}/mcp",
                   "auth": {"type": "bearer", "token": FX_TOKEN}})).json()["id"]
            await add_gateway(admin, "prod", [fid])
            await add_gateway(admin, "dev", [nid])
            # bob may use both servers; the gateway he connects to decides what he gets.
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            await add_team(admin, "prod-team", [bob_id], "prod", [(fid, "convert"), (fid, "list")])
            await add_team(admin, "dev-team", [bob_id], "dev", [(nid, "save_note"), (nid, "list")])
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            with (
                serve(create_app(edge_settings(db_url, "prod"))) as prod,
                serve(create_app(edge_settings(db_url, "prod"))) as prod_b,  # a second replica of prod
                serve(create_app(edge_settings(db_url, "dev"))) as dev,
            ):
                assert await wait_tools(prod, key, 2) == {"fx__convert", "fx__list"}
                assert await wait_tools(prod_b, key, 2) == {"fx__convert", "fx__list"}
                assert await wait_tools(dev, key, 2) == {"notes__save_note", "notes__list"}

                # Stateless: initialize on one replica, call on the other. No session to stick to.
                h = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}
                async with httpx2.AsyncClient(timeout=10) as raw:
                    init = await raw.post(f"{prod}/mcp", headers=h, json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}}})
                    assert init.status_code == 200, init.text
                    assert "mcp-session-id" not in init.headers
                    version = init.json()["result"]["protocolVersion"]
                    r = await raw.post(f"{prod_b}/mcp", headers={**h, "MCP-Protocol-Version": version}, json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "fx__list", "arguments": {}}})
                    assert r.status_code == 200 and r.json()["result"]["isError"] is False, r.text

                    # A valid key is not enough with a foreign Host header.
                    r = await raw.post(f"{prod}/mcp", headers={**h, "Host": "evil.example"}, json={})
                    assert r.status_code == 421

                # A hand-typed call for a server not attached here: refused, audited, never forwarded.
                async with gateway_client(prod, key) as mcp:
                    r = await mcp.call_tool("notes__save_note", {"text": "leaked"})
                    assert r.is_error and "Unknown tool" in text(r)
                    assert not (await mcp.call_tool("fx__list", {})).is_error
                log = (await admin.get("/api/audit")).json()
                assert [(e["gateway"], e["tool"], e["ok"]) for e in log[:3]] == [
                    ("prod", "fx__list", 1), ("prod", "notes__save_note", 0), ("prod", "fx__list", 1)]
                async with gateway_client(dev, key) as mcp:
                    assert "leaked" not in text(await mcp.call_tool("notes__list", {}))

                # Detaching applies on the next request.
                await admin.put("/api/gateways/prod/servers", json={"server_ids": []})
                async with gateway_client(prod, key) as mcp:
                    assert (await mcp.list_tools()).tools == []

                # A deleted gateway's edge is no longer ready.
                assert (await httpx2.AsyncClient(base_url=prod).get("/readyz")).status_code == 200
                await admin.delete("/api/gateways/prod")
                assert (await httpx2.AsyncClient(base_url=prod).get("/readyz")).status_code == 503

    asyncio.run(run())


def test_rate_limit_is_shared_by_every_pod(db_url):
    from app.upstream import Upstreams

    def pod(**kw):
        return Upstreams(store.connect(db_url, create_schema=False), call_timeout=1, list_timeout=1,
                         refresh_interval=30, calls_per_minute=3, **kw)

    store.connect(db_url).close()  # schema
    control, prod, dev = pod(), pod(gateway_slug="prod"), pod(gateway_slug="dev")
    alice = {"id": 1, "username": "alice", "role": "admin"}

    async def run():
        for ups, slug in ((control, "ctl"), (prod, "prod"), (dev, "dev")):  # one call each: three pods, one budget
            assert "Unknown tool" in text(await ups.call_tool(alice, slug, "x__y", {}))
        assert "Too many calls" in text(await prod.call_tool(alice, "prod", "x__y", {}))

    asyncio.run(run())


def test_planes_serve_only_their_own_routes(db_url):
    async def run():
        with (
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP))) as ctl,
            serve(create_app(edge_settings(db_url, "main"))) as edge,
        ):
            await setup_admin(ctl)
            assert (await httpx2.AsyncClient(base_url=ctl).post("/mcp", json={})).status_code == 404
            assert (await httpx2.AsyncClient(base_url=edge).get("/api/me")).status_code == 404
            assert (await httpx2.AsyncClient(base_url=edge).get("/healthz")).status_code == 200

    asyncio.run(run())


def test_oauth_token_works_only_at_the_gateway_it_was_issued_for(db_url):
    conn = store.connect(db_url)
    uid = store.create_user(conn, "bob", "bob-password-123", "admin")
    conn.execute("INSERT INTO org (id, name, created_at) VALUES (1, 'Acme', 0)")
    conn.execute("INSERT INTO oauth_clients (client_id, info, created_at) VALUES ('c1', '{}', 0)")
    conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES ('prod', 'Prod', 0), ('dev', 'Dev', 0)")
    p_port, d_port = _free_port(), _free_port()
    prod_url, dev_url = f"http://127.0.0.1:{p_port}", f"http://127.0.0.1:{d_port}"
    token = "mcpo_issued-for-dev"
    conn.execute(
        "INSERT INTO oauth_tokens (token_hash, kind, user_id, client_id, scopes, resource, created_at, expires_at)"
        " VALUES (?, 'access', ?, 'c1', '', ?, 0, ?)",
        (store.sha256(token), uid, f"{dev_url}/mcp", time.time() + 600),
    )

    async def run():
        with (
            serve(create_app(edge_settings(db_url, "prod", public_url=prod_url)), port=p_port),
            serve(create_app(edge_settings(db_url, "dev", public_url=dev_url)), port=d_port),
        ):
            r = await httpx2.AsyncClient().post(f"{prod_url}/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
            async with gateway_client(dev_url, token) as mcp:
                assert (await mcp.list_tools()).tools == []  # accepted; nothing attached

    asyncio.run(run())
