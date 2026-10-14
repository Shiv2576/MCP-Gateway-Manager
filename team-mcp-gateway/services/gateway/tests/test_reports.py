"""/api/reports: headline counts and call stats from the audit log."""

from __future__ import annotations

import asyncio
import time

import httpx2
import psycopg

from tests import KEY  # noqa: E402
from app.main import Settings, create_app
from tests.test_gateway import SETUP, serve, setup_admin


def test_reports(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP))) as gw:
            admin = await setup_admin(gw)
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})
            await admin.post("/api/keys", json={"name": "agent"})
            now = time.time()
            rows = [(now, "bob", "search", "notes", d) for d in range(1, 101)]
            rows += [(now, "alice", "write", "notes", 5), (now - 40 * 86400, "old", "stale", "x", 1),
                     (now, "carol", "gateway__wait_for_approval", "gateway", 30000)]
            with psycopg.connect(db_url, autocommit=True) as c:
                c.cursor().executemany(
                    "INSERT INTO audit_log (at, username, tool, server, ok, duration_ms) VALUES (%s, %s, %s, %s, 1, %s)", rows)

            r = (await admin.get("/api/reports")).json()
            assert r["counts"] == {"mcps": 1, "gateways": 0, "apps": 1, "connections": 0}
            assert r["by_tool"] == [{"name": "search", "value": 100}, {"name": "write", "value": 1}]
            assert r["by_server"] == [{"name": "notes", "value": 101}]
            assert [u["name"] for u in r["by_user"]] == ["bob", "alice"]  # "old" is outside the window
            assert r["p99_by_tool"][0] == {"name": "search", "value": 99}

            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            assert (await bob.get("/api/reports")).status_code == 403

    asyncio.run(run())
