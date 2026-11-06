"""Health loop: a failed check keeps the last good tools and rechecks soon."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from mcp.types import Tool

from app import db as store, upstream
from app.upstream import STALE_TOOLS_S, Upstreams


def test_failed_check_keeps_tools_and_rechecks_soon(db_url, monkeypatch):
    conn = store.connect(db_url)
    sid = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('s', 'http://x', 0) RETURNING id").fetchone()["id"]
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
    up = {"ok": True, "checks": 0}
    tool = Tool(name="read", input_schema={"type": "object"})

    @contextlib.asynccontextmanager
    async def fake_connect(row, timeout, headers):
        up["checks"] += 1
        if not up["ok"]:
            raise ConnectionError("down")

        async def list_tools(cursor=None):
            return SimpleNamespace(tools=[tool], next_cursor=None)

        yield SimpleNamespace(list_tools=list_tools)

    monkeypatch.setattr(upstream, "connect", fake_connect)
    ups = Upstreams(conn, call_timeout=1, list_timeout=1, refresh_interval=30)

    async def run():
        await ups.refresh_one(row)
        up["ok"] = False
        await ups.refresh_one(row)  # one blip
        st = ups.status[sid]
        assert st.state == "unreachable" and [t.name for t in st.tools] == ["read"]

        up["ok"] = True
        for _ in range(30):  # the first recheck lands in 0.5-1s, not at the 30s tick
            await asyncio.sleep(0.1)
            if ups.status[sid].state == "ok":
                break
        assert ups.status[sid].state == "ok" and ups.status[sid].failures == 0 and up["checks"] == 3

        # Down for longer than STALE_TOOLS_S: the old tools go.
        ups.status[sid].ok_at -= STALE_TOOLS_S + 1
        up["ok"] = False
        await ups.refresh_one(row)
        assert ups.status[sid].tools == [] and ups.status[sid].failures == 1
        for task in list(ups._tasks):
            task.cancel()

    asyncio.run(run())
