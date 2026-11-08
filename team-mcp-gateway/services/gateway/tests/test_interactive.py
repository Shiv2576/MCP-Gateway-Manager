"""Interactive MCP through the gateway: progress, and (later) cancellation, elicitation, sampling."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import threading
import time
from types import SimpleNamespace

import httpx2
import psycopg
from mcp import Client
from mcp.server import request_state
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ElicitResult, InputRequiredResult
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context, MCPServer

from tests import KEY  # noqa: E402
from app.main import Settings, create_app
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, add_team, edge_settings, gateway_client, serve, setup_admin, wait_tools,
)


def build_steps(saw_end: threading.Event | None = None, notes: list | None = None) -> MCPServer:
    server = MCPServer("steps")

    @server.tool(title="Write a note")
    async def note(text: str) -> str:
        """A quick write; notes counts the runs."""
        if notes is not None:
            notes.append(text)
        return "noted"

    @server.tool(annotations={"readOnlyHint": True})
    async def steps(n: int, ctx: Context) -> str:
        """Reports progress n times."""
        for i in range(1, n + 1):
            await ctx.report_progress(i, n, f"step {i}")
        return "done"

    @server.tool()
    async def slow_write() -> str:
        """A write that takes 5 seconds."""
        try:
            await asyncio.sleep(5)
        finally:
            if saw_end is not None:
                saw_end.set()
        return "written"

    return server


@contextlib.asynccontextmanager
async def modern_client(url: str, key: str, **client_kwargs):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=10) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http), mode="auto", **client_kwargs) as client:
            yield client


async def steps_edge(db_url, stack, tools=("steps", "slow_write"), budget=None, saw_end=None, notes=None,
                     budget_tool="slow_write"):
    """A control plane, an edge 'main' with the steps upstream, and bob's key. Returns (edge_url, key, admin).
    budget: items an hour for bob on budget_tool. saw_end: an Event set when slow_write is cut short."""
    steps_app = build_steps(saw_end, notes)
    up = stack.enter_context(serve(steps_app.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=False, host="0.0.0.0")))
    gw = stack.enter_context(serve(create_app(
        Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
        gateways=FakeGateways())))
    admin = await setup_admin(gw)
    sid = (await admin.post("/api/servers", json={"name": "st", "url": f"{up}/mcp", "auth": {"type": "none"}})).json()["id"]
    await add_gateway(admin, "main", [sid])
    bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
    tid = await add_team(admin, "team", [bob_id], "main", [(sid, t) for t in tools])
    if budget is not None:
        r = await admin.put(f"/api/teams/{tid}/gateways/main/budgets",
                            json={"budgets": [{"server_id": sid, "tool": budget_tool, "max_items": budget}]})
        assert r.status_code == 200, r.text
    bob = httpx2.AsyncClient(base_url=gw, timeout=10)
    await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
    key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
    edge = stack.enter_context(serve(create_app(edge_settings(db_url, "main"))))
    assert await wait_tools(edge, key, len(tools))
    return edge, key, admin


def test_progress_reaches_the_client(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            edge, key, _ = await steps_edge(db_url, stack)
            got = []

            async def on_progress(progress, total, message):
                got.append((progress, total, message))

            async with modern_client(edge, key) as mcp:
                r = await mcp.call_tool("st__steps", {"n": 3}, progress_callback=on_progress)
            assert not r.is_error
            assert got == [(1, 3, "step 1"), (2, 3, "step 2"), (3, 3, "step 3")]

    asyncio.run(run())


async def cancel_slow_write(edge, key):
    """Start slow_write, drop it after 0.5 s (closing the call's stream)."""
    async with modern_client(edge, key) as mcp:
        task = asyncio.create_task(mcp.call_tool("st__slow_write", {}))
        await asyncio.sleep(0.5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def audit_error(admin, timeout=5.0):
    for _ in range(int(timeout * 10)):
        rows = (await admin.get("/api/audit")).json()
        if rows and rows[0]["error"] == "cancelled by the client":
            return rows[0]
        await asyncio.sleep(0.1)
    return (await admin.get("/api/audit")).json()[0]


def test_closing_the_stream_cancels_the_call(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            saw_end = threading.Event()
            edge, key, admin = await steps_edge(db_url, stack, saw_end=saw_end)
            await cancel_slow_write(edge, key)
            assert (await audit_error(admin))["outcome"] == "cancelled"
            assert await asyncio.to_thread(saw_end.wait, 5)  # the upstream saw the request end

    asyncio.run(run())


def test_cancelled_write_still_counts_against_the_budget(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            edge, key, admin = await steps_edge(db_url, stack, budget=1)
            await cancel_slow_write(edge, key)
            assert (await audit_error(admin))["error"] == "cancelled by the client"  # not still NO_RESULT
            async with modern_client(edge, key) as mcp:
                r = await mcp.call_tool("st__slow_write", {})
            assert r.is_error and "budget" in r.content[0].text.lower()

    asyncio.run(run())


# ------------------------------------------------------------- elicitation

NOTE_TOOLS = ("steps", "slow_write", "note")
DECLINED = "Cancelled by the user. Nothing was changed."
ACCEPT = {"confirm": ElicitResult(action="accept", content={"confirm": True})}


def answering(action="accept", content=None):
    """An elicitation callback that answers every question the same way and counts them."""
    asked = []

    async def cb(context, params):
        asked.append(params)
        return ElicitResult(action=action, content=content if content is not None or action != "accept"
                            else {"confirm": True})

    return cb, asked


def note_outcomes(db_url):
    with psycopg.connect(db_url) as c:
        return [r[0] for r in c.execute("SELECT outcome FROM audit_log WHERE tool = 'st__note' ORDER BY id")]


def note_audit(db_url):
    with psycopg.connect(db_url) as c:
        return c.execute("SELECT ok, error, items FROM audit_log WHERE tool = 'st__note' ORDER BY id").fetchall()


def test_accept_runs_the_write_once(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes)
            cb, asked = answering()
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert not r.is_error, r
            assert len(asked) == 1 and notes == ["hi"]
            assert "Write a note" in asked[0].message and "text: hi" in asked[0].message
            rows = note_audit(db_url)
            assert [(x[0], x[1]) for x in rows] == [(0, "confirmation asked"), (1, None)]
            assert [x[2] is not None for x in rows] == [False, True]
            assert note_outcomes(db_url) == ["asked", "ok"]

    asyncio.run(run())


def test_decline_runs_nothing_and_charges_nothing(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes)
            cb, asked = answering("decline")
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert r.content[0].text == DECLINED
            assert len(asked) == 1 and notes == []
            assert note_audit(db_url) == [(0, "confirmation asked", None), (0, "declined by the user", None)]
            assert note_outcomes(db_url) == ["asked", "denied"]

    asyncio.run(run())


def test_read_only_tools_are_not_confirmed(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS)
            cb, asked = answering()
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__steps", {"n": 1})
            assert not r.is_error and asked == []

    asyncio.run(run())


async def round_one(edge, key, args):
    cb, _ = answering()  # declares the capability; this round is driven by hand
    async with modern_client(edge, key, elicitation_callback=cb) as mcp:
        r = await mcp.session.call_tool("st__note", args, allow_input_required=True)
    assert isinstance(r, InputRequiredResult), r
    assert set(r.input_requests) == {"confirm"}
    return r.request_state


async def retry(edge, key, args, state):
    """The retry's result, or the MCPError it raised."""
    cb, _ = answering()
    async with modern_client(edge, key, elicitation_callback=cb) as mcp:
        try:
            return await mcp.session.call_tool("st__note", args, input_responses=ACCEPT, request_state=state,
                                               allow_input_required=True)
        except MCPError as exc:
            return exc


def test_retry_on_another_replica(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes)
            other = stack.enter_context(serve(create_app(edge_settings(db_url, "main"))))
            assert await wait_tools(other, key, len(NOTE_TOOLS))
            state = await round_one(edge, key, {"text": "hi"})
            r = await retry(other, key, {"text": "hi"}, state)
            assert isinstance(r, CallToolResult) and not r.is_error, r
            assert notes == ["hi"]

    asyncio.run(run())


def test_tampered_retry_is_refused(db_url, monkeypatch):
    def refused(r):
        return (isinstance(r, MCPError) and r.code == -32602) or (isinstance(r, CallToolResult) and r.is_error)

    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, admin = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes)
            sid = (await admin.get("/api/servers")).json()[0]["id"]
            await add_gateway(admin, "dev", [sid])
            carol_id = (await admin.post("/api/users", json={"username": "carol", "password": "carol-password-123"})).json()["id"]
            await add_team(admin, "team2", [carol_id], "main", [(sid, "note")])
            carol = httpx2.AsyncClient(base_url=admin.base_url, timeout=10)
            await carol.post("/api/login", json={"username": "carol", "password": "carol-password-123"})
            carol_key = (await carol.post("/api/keys", json={"name": "k"})).json()["key"]
            dev = stack.enter_context(serve(create_app(edge_settings(db_url, "dev"))))

            state = await round_one(edge, key, {"text": "hi"})
            assert refused(await retry(edge, key, {"text": "other"}, state))
            assert refused(await retry(edge, carol_key, {"text": "hi"}, state))
            assert refused(await retry(dev, key, {"text": "hi"}, state))
            real = time.time
            monkeypatch.setattr(request_state, "time", SimpleNamespace(time=lambda: real() + 601))
            assert refused(await retry(edge, key, {"text": "hi"}, state))
            assert notes == []

    asyncio.run(run())


def test_clients_without_elicitation_are_unchanged(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes)
            async with gateway_client(edge, key) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert not r.is_error and notes == ["hi"]
            assert [(x[0], x[1]) for x in note_audit(db_url)] == [(1, None)]

    asyncio.run(run())


def test_state_for_another_call_is_refused():
    """The gateway's own hash check, behind the SDK's binding (which the e2e tests hit first)."""
    from app.interaction import Ask, call_hash
    from app.upstream import Upstreams

    ups = Upstreams(None, call_timeout=1, list_timeout=1, refresh_interval=1)
    row = {"id": 1}
    for state in ({"ask": "confirm", "call": call_hash("st__note", {"text": "other"})},
                  {"ask": "reason", "call": call_hash("st__note", {"text": "other"})},
                  {"ask": "bogus", "call": call_hash("st__note", {"text": "hi"})}):
        r, note = ups._confirm(Ask(True, state, ACCEPT), {}, "main", row, "note", "st__note", {"text": "hi"})
        assert r.is_error and note == "answer for another call"
    ok = {"ask": "confirm", "call": call_hash("st__note", {"text": "hi"})}
    assert ups._confirm(Ask(True, ok, ACCEPT), {}, "main", row, "note", "st__note", {"text": "hi"}) is None
    no = {"confirm": ElicitResult(action="accept", content={"confirm": False})}
    assert ups._confirm(Ask(True, ok, no), {}, "main", row, "note", "st__note", {"text": "hi"})[1] == "declined by the user"


# ------------------------------------------------------- over budget: a reason

REASON = "restock for the sale"


async def approvals_list(admin):
    return (await admin.get("/api/approvals?status=pending")).json()


def test_over_budget_asks_for_a_reason_and_files_it(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, admin = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes, budget=0, budget_tool="note")
            cb, asked = answering(content={"reason": REASON})
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert r.is_error and "gateway__wait_for_approval" in r.content[0].text, r
            assert len(asked) == 1 and "reason" in asked[0].requested_schema["properties"]
            assert "note needs an admin's approval for every call." in asked[0].message
            [card] = await approvals_list(admin)
            assert card["summary"] == REASON and notes == []
            assert note_audit(db_url)[0] == (0, "reason asked", None)
            assert note_outcomes(db_url) == ["asked", "over_budget"]

    asyncio.run(run())


def test_over_budget_decline_files_nothing(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            edge, key, admin = await steps_edge(db_url, stack, tools=NOTE_TOOLS, budget=0, budget_tool="note")
            cb, asked = answering("decline")
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert r.is_error and r.content[0].text == "note needs an admin's approval for every call."
            assert len(asked) == 1 and await approvals_list(admin) == []
            assert note_audit(db_url) == [(0, "reason asked", None), (0, "declined by the user", None)]

    asyncio.run(run())


def test_open_request_is_not_asked(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            edge, key, admin = await steps_edge(db_url, stack, tools=NOTE_TOOLS, budget=0, budget_tool="note")
            async with gateway_client(edge, key) as mcp:
                r = await mcp.call_tool("gateway__request_approval", {"tool": "st__note", "arguments": {"text": "a"}})
            assert not r.is_error, r
            cb, asked = answering(content={"reason": REASON})
            async with modern_client(edge, key, elicitation_callback=cb) as mcp:
                r = await mcp.call_tool("st__note", {"text": "hi"})
            assert asked == [] and r.is_error and "Approval #1 for st__note is still open" in r.content[0].text
            assert len(await approvals_list(admin)) == 1

    asyncio.run(run())


def test_budget_used_up_between_rounds_asks_for_a_reason(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            notes = []
            edge, key, _ = await steps_edge(db_url, stack, tools=NOTE_TOOLS, notes=notes, budget=1, budget_tool="note")
            state = await round_one(edge, key, {"text": "hi"})
            async with gateway_client(edge, key) as mcp:  # no elicitation: runs at once, using the budget up
                assert not (await mcp.call_tool("st__note", {"text": "first"})).is_error
            r = await retry(edge, key, {"text": "hi"}, state)
            assert isinstance(r, InputRequiredResult) and set(r.input_requests) == {"reason"}, r
            assert notes == ["first"]
            # charge() sized the retry's row before the question; that row must not count.
            assert note_audit(db_url)[-1] == (0, "reason asked", 1)
            with psycopg.connect(db_url) as c:  # room for one more, only if the "reason asked" row is not counted
                c.execute("UPDATE team_budgets SET max_items = 2 WHERE tool = 'note'")
            async with gateway_client(edge, key) as mcp:
                r = await mcp.call_tool("st__note", {"text": "later"})
            assert not r.is_error, r
            assert notes == ["first", "later"]

    asyncio.run(run())


# ------------------------------------------------------------------ push: subscriptions/listen

def build_two() -> MCPServer:
    server = MCPServer("two")

    @server.tool()
    async def ping() -> str:
        return "pong"

    return server


async def listen_setup(db_url, stack, edges: dict[str, int]):
    """A control plane, upstreams 'st' (attached to every gateway in edges) and 'two' (attached nowhere),
    edges[slug] replicas per gateway and an admin key. The edges' health loop ticks once a minute,
    so a change they pick up within seconds came by push. Returns (urls by slug, key, admin, two's id)."""
    st = stack.enter_context(serve(build_steps().streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=False, host="0.0.0.0")))
    two = stack.enter_context(serve(build_two().streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=False, host="0.0.0.0")))
    gw = stack.enter_context(serve(create_app(
        Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=60),
        gateways=FakeGateways())))
    admin = await setup_admin(gw)
    sid = (await admin.post("/api/servers", json={"name": "st", "url": f"{st}/mcp", "auth": {"type": "none"}})).json()["id"]
    two_id = (await admin.post("/api/servers", json={"name": "two", "url": f"{two}/mcp", "auth": {"type": "none"}})).json()["id"]
    key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
    urls = {}
    for slug, n in edges.items():
        await add_gateway(admin, slug, [sid])
        urls[slug] = [stack.enter_context(serve(create_app(edge_settings(db_url, slug, refresh_interval_s=60))))
                      for _ in range(n)]
        for url in urls[slug]:
            assert await wait_tools(url, key, 3)
    return urls, key, admin, (sid, two_id)


async def next_event(it, timeout=5.0):
    return await asyncio.wait_for(anext(it), timeout)


async def tool_names(mcp) -> set[str]:
    return {t.name for t in (await mcp.list_tools()).tools if not t.name.startswith("gateway__")}


def test_attach_pushes_to_a_listener_on_another_replica(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            urls, key, admin, (sid, two_id) = await listen_setup(db_url, stack, {"main": 2})
            async with modern_client(urls["main"][1], key) as mcp, mcp.listen(tools_list_changed=True) as sub:
                it = aiter(sub)
                await next_event(it)  # the one at connect
                assert "two__ping" not in await tool_names(mcp)
                r = await admin.put("/api/gateways/main/servers", json={"server_ids": [sid, two_id]})
                assert r.status_code == 200, r.text
                await next_event(it)
                assert "two__ping" in await tool_names(mcp)  # refreshed before the event went out

    asyncio.run(run())


def test_new_listener_gets_one_event_at_once(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            urls, key, _, _ = await listen_setup(db_url, stack, {"main": 1})
            async with modern_client(urls["main"][0], key) as mcp, mcp.listen(tools_list_changed=True) as sub:
                it = aiter(sub)
                await next_event(it, 2)
                with contextlib.suppress(TimeoutError):
                    await next_event(it, 1)
                    raise AssertionError("a second event without a change")

    asyncio.run(run())


def _listen_backends(db_url) -> list[int]:
    with psycopg.connect(db_url, autocommit=True) as c:
        return [r[0] for r in c.execute(
            "SELECT pid FROM pg_stat_activity WHERE query LIKE 'LISTEN%' AND datname = current_database()")]


def test_bus_reconnects(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            urls, key, admin, (sid, two_id) = await listen_setup(db_url, stack, {"main": 1})
            async with modern_client(urls["main"][0], key) as mcp, mcp.listen(tools_list_changed=True) as sub:
                it = aiter(sub)
                await next_event(it)
                (old,) = _listen_backends(db_url)
                with psycopg.connect(db_url, autocommit=True) as c:
                    c.execute("SELECT pg_terminate_backend(%s)", (old,))
                await next_event(it)  # the one after reconnecting: changes may have been missed
                assert _listen_backends(db_url) not in ([], [old])
                r = await admin.put("/api/gateways/main/servers", json={"server_ids": [sid, two_id]})
                assert r.status_code == 200, r.text
                await next_event(it)
                assert "two__ping" in await tool_names(mcp)

    asyncio.run(run())


def test_other_gateways_are_not_told(db_url):
    async def run():
        with contextlib.ExitStack() as stack:
            urls, key, admin, (sid, two_id) = await listen_setup(db_url, stack, {"prod": 1, "dev": 1})
            async with modern_client(urls["dev"][0], key) as mcp, mcp.listen(tools_list_changed=True) as sub:
                it = aiter(sub)
                await next_event(it)
                r = await admin.put("/api/gateways/prod/servers", json={"server_ids": [sid, two_id]})
                assert r.status_code == 200, r.text
                with contextlib.suppress(TimeoutError):
                    await next_event(it, 2)
                    raise AssertionError("dev was told about a change to prod")
                r = await admin.put("/api/gateways/dev/servers", json={"server_ids": [sid, two_id]})
                await next_event(it)  # dev's own change still arrives

    asyncio.run(run())


def test_health_loop_notifies_when_a_servers_tools_change(db_url, monkeypatch):
    from mcp.types import Tool

    from app import db as store
    from app import events
    from app.upstream import Upstreams

    conn = store.connect(db_url)
    sid = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('s', 'http://x', 0) RETURNING id").fetchone()["id"]
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
    up = {"tools": ["a"], "ok": True}

    @contextlib.asynccontextmanager
    async def fake_connect(row, timeout, headers):
        if not up["ok"]:
            raise ConnectionError("down")

        async def list_tools(cursor=None):
            return SimpleNamespace(tools=[Tool(name=n, input_schema={"type": "object"}) for n in up["tools"]],
                                   next_cursor=None)

        yield SimpleNamespace(list_tools=list_tools)

    sent = []
    monkeypatch.setattr(Upstreams, "connect", lambda self, *a: fake_connect(*a))
    monkeypatch.setattr(events, "notify", lambda c, slug: sent.append(slug))
    ups = Upstreams(conn, call_timeout=1, list_timeout=1, refresh_interval=30, gateway_slug="main")

    async def run():
        await ups.refresh_one(row)  # first good list: nothing to compare with
        await ups.refresh_one(row)  # same names
        assert sent == []
        up["ok"] = False
        await ups.refresh_one(row)  # a failed check is not a change
        up.update(ok=True, tools=["a", "b"])
        await ups.refresh_one(row)  # previous check was not good: no comparison
        assert sent == []
        up["tools"] = ["b"]
        await ups.refresh_one(row)
        assert sent == ["main"]
        for task in list(ups._tasks):
            task.cancel()

    asyncio.run(run())
    conn.close()


def test_a_flapping_upstream_does_not_echo_notices(db_url, monkeypatch):
    """Two edge processes and an upstream whose tool list differs on every check: one notice
    makes each refresh once, and refreshes caused by a notice never send another."""
    from mcp.types import Tool

    from app import db as store
    from app import events
    from app.upstream import Upstreams

    conn = store.connect(db_url)
    sid = conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('s', 'http://x', 0) RETURNING id").fetchone()["id"]
    gid = conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES ('main', 'Main', 0) RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?)", (gid, sid))
    checks = [0]

    @contextlib.asynccontextmanager
    async def fake_connect(row, timeout, headers):
        checks[0] += 1
        name = "a" if checks[0] % 2 else "b"

        async def list_tools(cursor=None):
            return SimpleNamespace(tools=[Tool(name=name, input_schema={"type": "object"})], next_cursor=None)

        yield SimpleNamespace(list_tools=list_tools)

    sent = []
    real = events.notify
    monkeypatch.setattr(events, "notify", lambda c, slug: (sent.append(slug), real(c, slug)))
    monkeypatch.setattr(Upstreams, "connect", lambda self, *a: fake_connect(*a))

    async def run():
        procs = [Upstreams(conn, call_timeout=1, list_timeout=1, refresh_interval=60, gateway_slug="main")
                 for _ in range(2)]
        for ups in procs:
            await ups.refresh_all()  # each has a good list to compare with
        # As create_app wires it: a refresh after a notice does not notify.
        buses = [events.PostgresBus(db_url, "main", functools.partial(ups.refresh_all, notify=False)) for ups in procs]
        tasks = [asyncio.create_task(b.run()) for b in buses]
        try:
            await asyncio.sleep(1)  # connected and past the refresh at connect
            sent.clear()
            await asyncio.to_thread(real, conn, "main")
            await asyncio.sleep(2)
            assert sent == [], f"{len(sent)} notices echoed"
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    conn.close()


def test_a_burst_of_notices_is_one_refresh(db_url):
    from app import db as store
    from app import events

    conn = store.connect(db_url)
    refreshes = [0]

    async def slow_refresh():
        refreshes[0] += 1
        await asyncio.sleep(0.3)

    async def run():
        bus = events.PostgresBus(db_url, "main", slow_refresh)
        got = []
        bus.subscribe(got.append)
        task = asyncio.create_task(bus.run())
        try:
            await asyncio.sleep(1)
            before = refreshes[0]
            await asyncio.to_thread(lambda: [events.notify(conn, "main") for _ in range(20)])
            await asyncio.sleep(1.5)
            assert 1 <= refreshes[0] - before <= 2, refreshes[0] - before
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    conn.close()
