"""Teams and tool-level policies: who may use which tools on which gateway."""

from __future__ import annotations

import asyncio

import httpx2

from tests import KEY  # noqa: E402
from app import db as store
from app.main import Settings, create_app
from tests.test_gateway import SETUP, FakeGateways, add_gateway, serve, setup_admin


def test_allowed_tools(db_url):
    conn = store.connect(db_url)
    admin = {"id": store.create_user(conn, "root", "root-password-1", "admin"), "role": "admin"}
    ann = {"id": store.create_user(conn, "ann", "ann-password-12", "member"), "role": "member"}
    lone = {"id": store.create_user(conn, "lone", "lone-password-1", "member"), "role": "member"}
    s1, s2 = (conn.execute("INSERT INTO servers (name, url, created_at) VALUES (?, 'http://x', 0) RETURNING id", (n,))
              .fetchone()["id"] for n in ("shop1", "notes"))
    prod, dev = (conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES (?, ?, 0) RETURNING id", (g, g))
                 .fetchone()["id"] for g in ("prod", "dev"))
    conn.executemany("INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?)", [(prod, s1), (prod, s2), (dev, s2)])
    t1, t2 = (conn.execute("INSERT INTO teams (name, created_at) VALUES (?, 0) RETURNING id", (t,)).fetchone()["id"]
              for t in ("ops", "sales"))
    conn.executemany("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", [(t1, ann["id"]), (t2, ann["id"])])
    conn.executemany(
        "INSERT INTO team_tools (team_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)",
        [(t1, prod, s1, "list_orders"), (t2, prod, s1, "list_orders"), (t2, prod, s1, "get_order"),
         (t2, prod, s2, "list"), (t1, dev, s2, "save_note")],
    )

    assert store.allowed_tools(conn, admin, "prod") == {s1: None, s2: None}
    assert store.allowed_tools(conn, admin, "dev") == {s2: None}
    # Union over both teams, per gateway.
    assert store.allowed_tools(conn, ann, "prod") == {s1: {"list_orders", "get_order"}, s2: {"list"}}
    assert store.allowed_tools(conn, ann, "dev") == {s2: {"save_note"}}
    assert store.allowed_tools(conn, lone, "prod") == {}
    assert store.allowed_tools(conn, ann, "nope") == {}
    # Detaching deletes the attachment's policies; re-attaching starts empty.
    conn.execute("DELETE FROM gateway_servers WHERE gateway_id = ? AND server_id = ?", (prod, s1))
    assert store.allowed_tools(conn, ann, "prod") == {s2: {"list"}}
    assert store.allowed_tools(conn, admin, "prod") == {s2: None}
    assert conn.execute("SELECT count(*) AS n FROM team_tools WHERE gateway_id = ? AND server_id = ?",
                        (prod, s1)).fetchone()["n"] == 0
    conn.execute("INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?)", (prod, s1))
    assert s1 not in store.allowed_tools(conn, ann, "prod")
    conn.close()


def test_user_denies(db_url):
    conn = store.connect(db_url)
    admin = {"id": store.create_user(conn, "root", "root-password-1", "admin"), "role": "admin"}
    ann = {"id": store.create_user(conn, "ann", "ann-password-12", "member"), "role": "member"}
    s1 = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('shop1', 'http://x', 0) RETURNING id").fetchone()["id"]
    prod, dev = (conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES (?, ?, 0) RETURNING id", (g, g))
                 .fetchone()["id"] for g in ("prod", "dev"))
    conn.executemany("INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?)", [(prod, s1), (dev, s1)])
    t = conn.execute("INSERT INTO teams (name, created_at) VALUES ('ops', 0) RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO team_members (team_id, user_id) VALUES (?, ?)", (t, ann["id"]))
    conn.executemany("INSERT INTO team_tools (team_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)",
                     [(t, prod, s1, "list_orders"), (t, prod, s1, "get_order"), (t, dev, s1, "get_order")])
    deny = "INSERT INTO user_denies (user_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)"

    conn.execute(deny, (ann["id"], prod, s1, "get_order"))
    assert store.allowed_tools(conn, ann, "prod") == {s1: {"list_orders"}}
    assert store.allowed_tools(conn, ann, "dev") == {s1: {"get_order"}}  # a deny is per gateway
    conn.execute(deny, (ann["id"], prod, s1, "list_orders"))
    assert store.allowed_tools(conn, ann, "prod") == {}  # every tool denied: the server is gone
    conn.execute(deny, (admin["id"], prod, s1, "list_orders"))
    assert store.allowed_tools(conn, admin, "prod") == {s1: None}
    # Detaching deletes the denies with the allows.
    conn.execute("DELETE FROM gateway_servers WHERE gateway_id = ? AND server_id = ?", (prod, s1))
    assert conn.execute("SELECT count(*) AS n FROM user_denies WHERE gateway_id = ?", (prod,)).fetchone()["n"] == 0
    assert store.connection_identity(conn, "prod", s1) is None
    assert store.connection_identity(conn, "dev", s1) == "shared"
    conn.close()


def test_schema_drops_orphaned_team_tools(db_url):
    conn = store.connect(db_url)
    s1 = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('shop1', 'http://x', 0) RETURNING id").fetchone()["id"]
    g = conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES ('prod', 'p', 0) RETURNING id").fetchone()["id"]
    t = conn.execute("INSERT INTO teams (name, created_at) VALUES ('ops', 0) RETURNING id").fetchone()["id"]
    # A part-2 install: no attachment constraint, a row left over from a detach.
    conn.execute("ALTER TABLE team_tools DROP CONSTRAINT team_tools_attachment")
    conn.execute("INSERT INTO team_tools (team_id, gateway_id, server_id, tool) VALUES (?, ?, ?, 'x')", (t, g, s1))
    conn.close()
    conn = store.connect(db_url)
    assert conn.execute("SELECT count(*) AS n FROM team_tools").fetchone()["n"] == 0
    assert conn.execute("SELECT 1 FROM pg_constraint WHERE conname = 'team_tools_attachment'").fetchone()
    conn.close()
    store.connect(db_url).close()  # and again: nothing to do


def test_denies_api(db_url):
    from tests.test_gateway import add_denies, add_team

    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP), gateways=FakeGateways())) as gw:
            admin = await setup_admin(gw)
            me = (await admin.get("/api/me")).json()
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            s1 = (await admin.post("/api/servers", json={"name": "shop1", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            s2 = (await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            await add_gateway(admin, "prod", [s1, s2])
            await add_gateway(admin, "dev", [s2])
            await add_team(admin, "Ops", [bob_id], "prod", [(s1, "list_orders"), (s1, "get_order"), (s2, "list")])

            url = f"/api/users/{bob_id}/gateways/prod/denies"
            ok = {"tools": [{"server_id": s1, "tool": "get_order"}]}
            assert (await bob.put(url, json=ok)).status_code == 403
            assert (await admin.put(f"/api/users/{me['id']}/gateways/prod/denies", json=ok)).status_code == 400
            assert (await admin.put("/api/users/999/gateways/prod/denies", json=ok)).status_code == 404
            assert (await admin.put(f"/api/users/{bob_id}/gateways/nope/denies", json=ok)).status_code == 404
            assert (await admin.put(f"/api/users/{bob_id}/gateways/dev/denies", json=ok)).status_code == 400  # s1 not on dev
            assert (await admin.put(url, json={"tools": [{"server_id": s1, "tool": ""}]})).status_code == 400
            await add_denies(admin, bob_id, "prod", [(s1, "list_orders")])
            await add_denies(admin, bob_id, "prod", [(s1, "get_order")])  # replaces
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["denies"] == {"prod": [{"server_id": s1, "server": "shop1", "tool": "get_order"}]}
            assert users["admin"]["denies"] == {}

            # Re-saving attachments keeps what stays: identity, allows, denies.
            conn = store.connect(db_url, create_schema=False)
            conn.execute("UPDATE gateway_servers SET identity = 'personal' WHERE server_id = ?", (s1,))
            await add_gateway_servers(admin, "prod", [s1, s2])
            count = lambda table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]  # noqa: E731
            assert (count("team_tools"), count("user_denies")) == (3, 1)
            gws = {g["slug"]: g for g in (await admin.get("/api/gateways")).json()}
            assert {s["name"]: s["identity"] for s in gws["prod"]["servers"]} == {"shop1": "personal", "notes": "shared"}
            # Dropping one server deletes only its rows.
            await add_gateway_servers(admin, "prod", [s2])
            assert (count("team_tools"), count("user_denies")) == (1, 0)
            conn.close()

            # Identity switch: admin only, attached servers only, personal needs a personal header.
            iurl = f"/api/gateways/prod/servers/{s2}/identity"
            assert (await bob.put(iurl, json={"identity": "shared"})).status_code == 403
            assert (await admin.put(iurl, json={"identity": "mine"})).status_code == 400
            assert (await admin.put(f"/api/gateways/prod/servers/{s1}/identity", json={"identity": "shared"})).status_code == 404
            assert (await admin.put(iurl, json={"identity": "personal"})).status_code == 400
            assert (await admin.put(iurl, json={"identity": "shared"})).status_code == 200

    async def add_gateway_servers(admin, slug, ids):
        r = await admin.put(f"/api/gateways/{slug}/servers", json={"server_ids": ids})
        assert r.status_code == 200, r.text

    asyncio.run(run())


def test_teams_api(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP), gateways=FakeGateways())) as gw:
            admin = await setup_admin(gw)
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            s1 = (await admin.post("/api/servers", json={"name": "shop1", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            s2 = (await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            await add_gateway(admin, "prod", [s1])
            await add_gateway(admin, "dev", [s2])

            assert (await bob.post("/api/teams", json={"name": "ops"})).status_code == 403
            for name in (" ", "x" * 41):
                assert (await admin.post("/api/teams", json={"name": name})).status_code == 400
            r = await admin.post("/api/teams", json={"name": "Ops"})
            assert r.status_code == 201, r.text
            tid = r.json()["id"]
            assert (await admin.post("/api/teams", json={"name": "Ops"})).status_code == 409
            other = (await admin.post("/api/teams", json={"name": "Sales"})).json()["id"]

            assert (await bob.put(f"/api/teams/{tid}/members", json={"user_ids": [bob_id]})).status_code == 403
            assert (await admin.put(f"/api/teams/{tid}/members", json={"user_ids": [999]})).status_code == 400
            assert (await admin.put("/api/teams/999/members", json={"user_ids": []})).status_code == 404
            assert (await admin.put(f"/api/teams/{tid}/members", json={"user_ids": [bob_id]})).status_code == 200

            url = f"/api/teams/{tid}/gateways/prod/tools"
            ok = {"tools": [{"server_id": s1, "tool": "list_orders"}, {"server_id": s1, "tool": "get_order"}]}
            assert (await bob.put(url, json=ok)).status_code == 403
            assert (await admin.put(url, json={"tools": [{"server_id": s2, "tool": "list"}]})).status_code == 400  # on dev only
            assert (await admin.put(f"/api/teams/{tid}/gateways/nope/tools", json=ok)).status_code == 404
            for bad in ({"server_id": s1, "tool": ""}, {"server_id": s1, "tool": "x" * 129}, {"server_id": "1", "tool": "a"}):
                assert (await admin.put(url, json={"tools": [bad]})).status_code == 400, bad
            assert (await admin.put(url, json=ok)).status_code == 200
            assert (await admin.put(url, json=ok)).status_code == 200  # replaces, no duplicates

            teams = {t["name"]: t for t in (await admin.get("/api/teams")).json()}
            assert teams["Ops"]["members"] == [{"id": bob_id, "username": "bob"}]
            assert teams["Ops"]["policies"] == {"prod": [
                {"server_id": s1, "server": "shop1", "tool": "get_order"},
                {"server_id": s1, "server": "shop1", "tool": "list_orders"}]}
            assert teams["Sales"]["policies"] == {}
            # Members read only their own teams.
            assert [t["name"] for t in (await bob.get("/api/teams")).json()] == ["Ops"]

            assert (await bob.delete(f"/api/teams/{other}")).status_code == 403
            assert (await admin.delete("/api/teams/999")).status_code == 404
            assert (await admin.delete(f"/api/teams/{tid}")).status_code == 200
            assert (await bob.get("/api/teams")).json() == []
            conn = store.connect(db_url, create_schema=False)
            assert conn.execute("SELECT COUNT(*) AS n FROM team_tools").fetchone()["n"] == 0
            conn.close()

    asyncio.run(run())


def test_edge_enforces_team_policies(db_url):
    from tests.test_gateway import (
        FX_TOKEN, NOTES_KEY, add_team, edge_settings, gateway_client, http_app, text, wait_tools,
    )
    from demo_mcp import build_fx, build_notes  # noqa: E402  (tests.test_gateway puts it on sys.path)

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
            await add_gateway(admin, "main", [nid, fid])
            keys = {}
            for name in ("bob", "carol", "dave"):
                await admin.post("/api/users", json={"username": name, "password": f"{name}-password-123"})
                c = httpx2.AsyncClient(base_url=gw, timeout=10)
                await c.post("/api/login", json={"username": name, "password": f"{name}-password-123"})
                keys[name] = (await c.post("/api/keys", json={"name": "k"})).json()["key"]
            users = {u["username"]: u["id"] for u in (await admin.get("/api/users")).json()}
            team_a = await add_team(admin, "A", [users["bob"]], "main", [(fid, "list")])
            # carol: two teams that overlap, and a tool notes does not offer.
            await add_team(admin, "B", [users["carol"]], "main", [(nid, "list"), (nid, "save_note"), (nid, "gone")])
            await add_team(admin, "C", [users["carol"]], "main", [(nid, "list")])

            with serve(create_app(edge_settings(db_url, "main"))) as edge:
                assert await wait_tools(edge, keys["bob"], 1) == {"fx__list"}
                carol = await wait_tools(edge, keys["carol"], 2)
                assert sorted(carol) == ["notes__list", "notes__save_note"]  # no notes__gone, no duplicates
                async with gateway_client(edge, keys["dave"]) as mcp:  # in no team
                    assert (await mcp.list_tools()).tools == []

                # A hand-typed call outside the policy: refused, audited, never forwarded.
                async with gateway_client(edge, keys["bob"]) as mcp:
                    r = await mcp.call_tool("fx__convert", {"amount_inr": 100, "to": "usd"})
                    assert r.is_error and "Unknown tool" in text(r)
                    r = await mcp.call_tool("notes__save_note", {"text": "sneaky"})
                    assert r.is_error and "Unknown tool" in text(r)
                    assert not (await mcp.call_tool("fx__list", {})).is_error
                log = (await admin.get("/api/audit")).json()
                assert [(e["username"], e["tool"], e["gateway"], e["ok"]) for e in log[:3]] == [
                    ("bob", "fx__list", "main", 1), ("bob", "notes__save_note", "main", 0),
                    ("bob", "fx__convert", "main", 0)]
                async with gateway_client(edge, keys["carol"]) as mcp:
                    assert "sneaky" not in text(await mcp.call_tool("notes__list", {}))

                # Adding the tool applies on the next request.
                await admin.put(f"/api/teams/{team_a}/gateways/main/tools",
                                json={"tools": [{"server_id": fid, "tool": "list"}, {"server_id": fid, "tool": "convert"}]})
                async with gateway_client(edge, keys["bob"]) as mcp:
                    assert not (await mcp.call_tool("fx__convert", {"amount_inr": 100, "to": "usd"})).is_error

                # Detaching wins over the policy.
                await admin.put("/api/gateways/main/servers", json={"server_ids": [nid]})
                async with gateway_client(edge, keys["bob"]) as mcp:
                    assert (await mcp.list_tools()).tools == []
                    assert (await mcp.call_tool("fx__list", {})).is_error

    asyncio.run(run())


def test_dashboard_uses_the_gateway_policy(db_url):
    from tests.test_gateway import NOTES_KEY, add_team, edge_settings, http_app, wait_tools
    from demo_mcp import build_fx, build_notes  # noqa: E402

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx())) as fx,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
                             gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            fid = (await admin.post("/api/servers", json={"name": "fx", "url": f"{fx}/mcp"})).json()["id"]
            await add_gateway(admin, "main", [nid, fid])
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
            await add_team(admin, "A", [bob_id], "main", [(nid, "list")])

            assert (await bob.get("/api/tools")).status_code == 400
            assert (await bob.get("/api/tools?gateway=nope")).status_code == 404
            call = {"name": "notes__list", "arguments": {}}
            assert (await bob.post("/api/tools/call", json=call)).status_code == 400
            assert (await bob.post("/api/tools/call", json={**call, "gateway": "nope"})).status_code == 404
            assert (await admin.get("/api/audit")).json() == []  # nothing called

            # The dashboard shows exactly what the edge serves.
            with serve(create_app(edge_settings(db_url, "main"))) as edge:
                on_edge = await wait_tools(edge, key, 1)
            deadline = asyncio.get_running_loop().time() + 5
            while not (await bob.get("/api/tools?gateway=main")).json():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.1)
            listed = {t["name"] for t in (await bob.get("/api/tools?gateway=main")).json()} - {"gateway__request_approval", "gateway__wait_for_approval", "gateway__list_team_alerts", "gateway__mark_team_alert_done"}
            assert listed == on_edge == {"notes__list"}
            r = await bob.post("/api/tools/call", json={"gateway": "main", "name": "fx__list", "arguments": {}})
            assert r.json()["isError"] is True
            log = (await admin.get("/api/audit")).json()
            assert (log[0]["username"], log[0]["tool"], log[0]["gateway"], log[0]["ok"]) == ("bob", "fx__list", "main", 0)

            # Members see only servers they have a tool on; admins see all.
            assert [s["name"] for s in (await bob.get("/api/servers")).json()] == ["notes"]
            assert len((await admin.get("/api/servers")).json()) == 2
            # Grants are gone.
            assert (await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": []})).status_code in (404, 405)
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["teams"] == ["A"] and "server_ids" not in users["bob"]

    asyncio.run(run())
