"""Server health: last-hour stats from the audit log, and every checker's view from Postgres."""

from __future__ import annotations

import asyncio
import time

import httpx2

from tests import KEY
from app import db as store
from app.main import Settings, create_app
from app.upstream import Status, Upstreams
from tests.test_gateway import (
    NOTES_KEY, SETUP, FakeGateways, _free_port, add_gateway, add_team, edge_settings, gateway_client,
    http_app, serve, setup_admin, wait_tools,
)

from demo_mcp import build_notes  # noqa: E402


def test_servers_report_stats_and_each_checkers_view(db_url):
    async def run():
        dead_url = f"http://127.0.0.1:{_free_port()}/mcp"  # nothing listens
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
                             gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            did = (await admin.post("/api/servers", json={"name": "dead", "url": dead_url})).json()["id"]
            assert (await admin.post("/api/gateways", json={"slug": "control", "name": "C"})).status_code == 400
            await add_gateway(admin, "prod", [nid, did])
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            await add_team(admin, "t", [bob_id], "prod", [(nid, "list"), (did, "list")])
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            with serve(create_app(edge_settings(db_url, "prod"))) as edge:
                await wait_tools(edge, key, 1)
                async with gateway_client(edge, key) as mcp:
                    for _ in range(3):
                        assert not (await mcp.call_tool("notes__list", {})).is_error
                    assert (await mcp.call_tool("dead__list", {})).is_error  # reaches the upstream: unreachable
                    assert (await mcp.call_tool("notes__save_note", {"text": "x"})).is_error  # policy: not counted

                servers = {s["name"]: s for s in (await admin.get("/api/servers")).json()}
                h = servers["notes"]["health"]
                assert h["calls"] == 3 and h["error_rate"] == 0 and h["last_error"] is None
                assert isinstance(h["p50_ms"], int) and h["p95_ms"] >= h["p50_ms"]
                d = servers["dead"]["health"]
                assert d["calls"] == 1 and d["error_rate"] == 1 and "unreachable" in d["last_error"]
                assert d["last_error_at"] <= time.time()

                # Both the control plane and the prod edge report, each with when its state began.
                deadline = time.time() + 5
                while {c["checker"] for c in servers["dead"]["checks"]} != {"control", "prod"}:
                    assert time.time() < deadline, servers["dead"]["checks"]
                    await asyncio.sleep(0.2)
                    servers = {s["name"]: s for s in (await admin.get("/api/servers")).json()}
                for c in servers["dead"]["checks"]:
                    assert c["state"] == "unreachable" and c["since"] <= c["checked_at"]

    asyncio.run(run())


def test_since_only_moves_when_the_state_changes(db_url):
    conn = store.connect(db_url)
    sid = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('s', 'http://x', 0) RETURNING id").fetchone()["id"]
    ups = Upstreams(conn, call_timeout=1, list_timeout=1, refresh_interval=30, gateway_slug="prod")
    ups._record(sid, Status(state="ok", checked_at=100.0))
    ups._record(sid, Status(state="ok", checked_at=130.0))
    row = conn.execute("SELECT since, checked_at FROM server_status WHERE server_id = ?", (sid,)).fetchone()
    assert (row["since"], row["checked_at"]) == (100.0, 130.0)
    ups._record(sid, Status(state="unreachable", error="down", checked_at=160.0))
    row = conn.execute("SELECT state, since, error FROM server_status WHERE server_id = ?", (sid,)).fetchone()
    assert (row["state"], row["since"], row["error"]) == ("unreachable", 160.0, "down")


def test_members_never_see_another_users_tool_error_text(db_url):
    from app.api import server_health

    conn = store.connect(db_url)
    now = time.time()
    for outcome, error in (("tool_error", "order 7 for alice@example.com not found"), ("ok", None)):
        conn.execute("INSERT INTO audit_log (at, username, tool, server, ok, duration_ms, error, outcome)"
                     " VALUES (?, 'alice', 'shop__get_order', 'shop', ?, 40, ?, ?)",
                     (now - 60, 1 if outcome == "ok" else 0, error, outcome))
    admin_view, _ = server_health(conn, now, admin=True)
    member_view, _ = server_health(conn, now, admin=False)
    assert "alice@example.com" in admin_view["shop"]["last_error"]
    assert "alice" not in member_view["shop"]["last_error"] and member_view["shop"]["last_error_at"] == now - 60
    assert member_view["shop"]["calls"] == 2 and member_view["shop"]["error_rate"] == 0.5
