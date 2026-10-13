"""Write budgets and explicit admin approvals: MCP client -> edge -> a fake shop upstream."""

from __future__ import annotations

import asyncio
import json
import time

import httpx2
from mcp.server.mcpserver import MCPServer

from tests import KEY
from app import approvals
from app import db as store
from app.main import Settings, create_app
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, add_team, edge_settings, gateway_client, http_app, serve, setup_admin, text,
    wait_tools,
)
from tests.test_identity import member

DESC, REFUND, STATUS = "shop__bulk_update_product_descriptions", "shop__bulk_refund_orders", "shop__update_order_status"


def build_shop(calls: list) -> MCPServer:
    """Records every run; an item with "fail" makes the call fail."""
    server = MCPServer("shop")

    @server.tool(name="bulk_update_product_descriptions")
    async def descs(items: list[dict]) -> str:
        if any(i.get("fail") for i in items):
            raise ValueError("store said no")
        calls.append(("desc", len(items)))
        return f"updated {len(items)}"

    @server.tool(name="bulk_refund_orders")
    async def refunds(items: list[dict]) -> str:
        calls.append(("refund", len(items)))
        return f"refunded {len(items)}"

    @server.tool(name="update_order_status")
    async def status(order_id: int) -> str:
        calls.append(("status", 1))
        return "ok"

    return server


def items(n: int, **extra) -> dict:
    return {"items": [{"product_id": i + 1, "description": "x", **extra} for i in range(n)]}


def refunds(*amounts) -> dict:
    return {"items": [{"order_id": i + 1, "amount": a} for i, a in enumerate(amounts)]}


async def put_budgets(admin, tid: int, budgets: list[dict], status: int = 200):
    r = await admin.put(f"/api/teams/{tid}/gateways/main/budgets", json={"budgets": budgets})
    assert r.status_code == status, r.text
    return r


class World:
    """Control plane, shop upstream and one edge, with members bob and carol in team A."""

    def __init__(self, db_url: str):
        self.db_url, self.calls = db_url, []

    async def __aenter__(self):
        self._stack = __import__("contextlib").ExitStack()
        shop = self._stack.enter_context(serve(http_app(build_shop(self.calls))))
        self.gw = self._stack.enter_context(serve(create_app(
            Settings(database_url=self.db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
            gateways=FakeGateways())))
        self.admin = await setup_admin(self.gw)
        self.sid = (await self.admin.post("/api/servers", json={"name": "shop", "url": f"{shop}/mcp"})).json()["id"]
        await add_gateway(self.admin, "main", [self.sid])
        self.bob_id, self.bob = await member(self.admin, self.gw, "bob")
        self.carol_id, self.carol = await member(self.admin, self.gw, "carol")
        tools = [(self.sid, t) for t in ("bulk_update_product_descriptions", "bulk_refund_orders", "update_order_status")]
        self.team = await add_team(self.admin, "A", [self.bob_id, self.carol_id], "main", tools)
        await put_budgets(self.admin, self.team, [
            {"server_id": self.sid, "tool": "bulk_update_product_descriptions", "max_items": 20},
            {"server_id": self.sid, "tool": "bulk_refund_orders", "max_items": 20, "max_refund_total": "1000"},
            {"server_id": self.sid, "tool": "update_order_status", "max_items": 3},
        ])
        self.edge = self._stack.enter_context(serve(create_app(edge_settings(self.db_url, "main", calls_per_minute=1000))))
        self.conn = store.connect(self.db_url, create_schema=False)
        await wait_tools(self.edge, self.bob, 5)
        return self

    async def __aexit__(self, *exc):
        self.conn.close()
        self._stack.close()

    async def call(self, key: str, name: str, args: dict):
        async with gateway_client(self.edge, key) as mcp:
            r = await mcp.call_tool(name, args)
            return r.is_error, text(r)

    def approvals(self) -> list[dict]:
        return self.conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()

    def age_audit(self, seconds: float) -> None:
        self.conn.execute("UPDATE audit_log SET at = at - ?", (seconds,))


def test_budget_counts_items_in_a_rolling_hour(db_url):
    async def run():
        async with World(db_url) as w:
            assert await w.call(w.bob, DESC, items(19)) == (False, "updated 19")
            assert await w.call(w.bob, DESC, items(1)) == (False, "updated 1")
            err, msg = await w.call(w.bob, DESC, items(1))
            assert err and "Budget: 0 of 20 items left" in msg and "19 free up at" in msg, msg
            assert "gateway__request_approval" in msg and w.approvals() == []
            assert w.calls == [("desc", 19), ("desc", 1)]

            # Carol has her own budget; another tool is counted apart.
            assert not (await w.call(w.carol, DESC, items(20)))[0]
            assert not (await w.call(w.bob, REFUND, refunds("10")))[0]

            # 40 minutes on: still full. 61 minutes on: the 19 dropped out, the 1 (moved later) did not.
            w.age_audit(40 * 60)
            assert (await w.call(w.bob, DESC, items(1)))[0]
            w.conn.execute("UPDATE audit_log SET at = at - 21 * 60 WHERE tool = ? AND items = 19", (DESC,))
            assert (await w.call(w.bob, DESC, items(20)))[0]
            assert await w.call(w.bob, DESC, items(19)) == (False, "updated 19")

            # 19 + 19: the second is refused; bigger than the whole budget says so.
            w.age_audit(3600)
            assert not (await w.call(w.bob, DESC, items(19)))[0]
            assert (await w.call(w.bob, DESC, items(19)))[0]
            err, msg = await w.call(w.bob, DESC, items(21))
            assert err and "can never run directly" in msg, msg

            # Failed calls use no budget.
            w.age_audit(3600)
            assert (await w.call(w.bob, DESC, items(20, fail=True)))[0]
            assert not (await w.call(w.bob, DESC, items(20)))[0]

            # A single-item tool counts 1 per call.
            for _ in range(3):
                assert not (await w.call(w.bob, STATUS, {"order_id": 1}))[0]
            err, msg = await w.call(w.bob, STATUS, {"order_id": 1})
            assert err and "0 of 3 items left" in msg

            # Refund money: refused while under the item budget; bad amounts refused.
            assert not (await w.call(w.carol, REFUND, refunds("600")))[0]
            err, msg = await w.call(w.carol, REFUND, refunds("300", "100.01"))
            assert err and "400.00 of 1000.00 refund money left" in msg, msg
            assert (await w.call(w.carol, REFUND, refunds("abc")))[0]
            assert (await w.call(w.carol, REFUND, refunds("-5", "5")))[0]
            row = w.conn.execute("SELECT items, refund_total FROM audit_log WHERE tool = ? AND ok = 1 AND user_id = ?",
                                 (REFUND, w.carol_id)).fetchone()
            assert (row["items"], str(row["refund_total"])) == (1, "600.00")

    asyncio.run(run())


def test_strictest_budget_admins_and_unbudgeted(db_url):
    async def run():
        async with World(db_url) as w:
            t2 = await add_team(w.admin, "B", [w.bob_id], "main", [(w.sid, "bulk_update_product_descriptions")])
            await put_budgets(w.admin, t2, [{"server_id": w.sid, "tool": "bulk_update_product_descriptions", "max_items": 5}])
            assert (await w.call(w.bob, DESC, items(6)))[0]  # B's 5 wins over A's 20
            assert not (await w.call(w.carol, DESC, items(6)))[0]  # carol is not in B

            admin_key = (await w.admin.post("/api/keys", json={"name": "k"})).json()["key"]
            assert not (await w.call(admin_key, DESC, items(100)))[0]
            async with gateway_client(w.edge, admin_key) as mcp:  # the approval tools mean nothing to admins
                assert not {t.name for t in (await mcp.list_tools()).tools} & {"gateway__request_approval"}

            await put_budgets(w.admin, w.team, [])
            await put_budgets(w.admin, t2, [])
            assert not (await w.call(w.bob, DESC, items(100)))[0]
            assert await wait_tools(w.edge, w.bob, 3) == {DESC, REFUND, STATUS}  # no budget, no approval tools

    asyncio.run(run())


def test_parallel_calls_cannot_slip_past_the_budget(db_url):
    async def run():
        async with World(db_url) as w:
            results = await asyncio.gather(*(w.call(w.bob, DESC, items(5)) for _ in range(8)))
            assert sum(not err for err, _ in results) == 4
            assert sum(n for _, n in w.calls) == 20

    asyncio.run(run())


def test_request_approve_and_wait(db_url):
    async def run():
        async with World(db_url) as w:
            # Fits: run it directly. Hidden or unknown tools: unknown. Unbudgeted: no approval needed.
            err, msg = await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(5)})
            assert err and "fits your budget" in msg
            for name in ("shop__delete_everything", "nope"):
                err, msg = await w.call(w.bob, "gateway__request_approval", {"tool": name, "arguments": {}})
                assert err and "Unknown tool" in msg

            err, msg = await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(60)})
            assert not err and "Approval #1 requested" in msg and "60 items" in msg, msg
            assert [(a["status"], a["items"], a["budget"]) for a in w.approvals()] == [("pending", 60, "20 items/hour")]
            err, msg = await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(70)})
            assert err and "#1" in msg and "still open" in msg
            assert w.calls == []

            # Still pending: the wait comes back after its hold with "call again".
            approvals.WAIT_SECONDS = 0.1
            try:
                assert await w.call(w.bob, "gateway__wait_for_approval", {"id": 1}) == (
                    False, "Approval #1 still pending; call gateway__wait_for_approval again.")
            finally:
                approvals.WAIT_SECONDS = 25
            # Somebody else's id is unknown.
            assert (await w.call(w.carol, "gateway__wait_for_approval", {"id": 1}))[0]

            # Admins see it; approve runs it once, as bob.
            assert (await w.admin.get("/api/approvals/count")).json() == {"pending": 1}
            [card] = (await w.admin.get("/api/approvals?status=pending")).json()
            assert (card["username"], card["teams"], card["items"], len(card["preview"])) == ("bob", ["A"], 60, 20)
            r = await w.admin.post("/api/approvals/1/approve", json={"note": "ok, go"})
            assert r.status_code == 200 and r.json()["status"] == "executed" and r.json()["outcome"] == "updated 60"
            assert (await w.admin.post("/api/approvals/1/approve", json={})).status_code == 409
            assert (await w.admin.post("/api/approvals/1/reject", json={})).status_code == 409
            assert w.calls == [("desc", 60)]
            run_row = w.conn.execute("SELECT * FROM audit_log WHERE approval_id = 1 AND tool = ?", (DESC,)).fetchone()
            assert (run_row["user_id"], run_row["ok"], w.approvals()[0]["audit_id"]) == (w.bob_id, 1, run_row["id"])

            # The result reaches the waiting agent once, then is cleared.
            assert await w.call(w.bob, "gateway__wait_for_approval", {"id": 1}) == (False, "updated 60")
            assert w.approvals()[0]["result"] is None
            err, msg = await w.call(w.bob, "gateway__wait_for_approval", {"id": 1})
            assert not err and "already delivered" in msg

            # Approved items do not use the hourly budget.
            assert not (await w.call(w.bob, DESC, items(20)))[0]
            assert (await w.admin.get("/api/approvals/count")).json() == {"pending": 0}
            assert [h["id"] for h in (await w.admin.get("/api/approvals?status=history")).json()] == [1]

    asyncio.run(run())


def test_reject_locks_requests_for_an_hour(db_url):
    async def run():
        async with World(db_url) as w:
            await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)})
            r = await w.admin.post("/api/approvals/1/reject", json={"note": "too many at once"})
            assert r.status_code == 200 and r.json()["status"] == "rejected"
            err, msg = await w.call(w.bob, "gateway__wait_for_approval", {"id": 1})
            assert err and "too many at once" in msg and "locked until" in msg

            err, msg = await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)})
            assert err and "Approval #1 was rejected" in msg
            # The budget still works; other tools and other members are not locked.
            assert not (await w.call(w.bob, DESC, items(5)))[0]
            assert not (await w.call(w.bob, "gateway__request_approval", {"tool": REFUND, "arguments": refunds(*["1"] * 21)}))[0]
            assert not (await w.call(w.carol, "gateway__request_approval", {"tool": DESC, "arguments": items(30)}))[0]

            w.conn.execute("UPDATE approvals SET decided_at = decided_at - 3601 WHERE id = 1")
            assert not (await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)}))[0]
            assert w.calls == [("desc", 5)]

    asyncio.run(run())


def test_expiry_stuck_runs_and_cleared_results(db_url):
    async def run():
        async with World(db_url) as w:
            await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)})
            w.conn.execute("UPDATE approvals SET requested_at = requested_at - 86401")
            err, msg = await w.call(w.bob, "gateway__wait_for_approval", {"id": 1})
            assert err and "expired" in msg
            # No lock after an expiry.
            assert not (await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)}))[0]

            # A run whose pod died: failed with an unknown outcome.
            w.conn.execute("UPDATE approvals SET status = 'running', decided_at = ? WHERE id = 2", (time.time() - 241,))
            err, msg = await w.call(w.bob, "gateway__wait_for_approval", {"id": 2})
            assert err and "outcome is unknown" in msg

            # An undelivered result is cleared 24 h after the run.
            await w.call(w.carol, "gateway__request_approval", {"tool": DESC, "arguments": items(30)})
            assert (await w.admin.post("/api/approvals/3/approve", json={})).status_code == 200
            w.conn.execute("UPDATE approvals SET finished_at = finished_at - 86401 WHERE id = 3")
            await w.admin.get("/api/approvals/count")
            assert w.approvals()[2]["result"] is None

    asyncio.run(run())


def test_approve_rechecks_policy_and_who_decides(db_url):
    async def run():
        async with World(db_url) as w:
            await w.call(w.bob, "gateway__request_approval", {"tool": DESC, "arguments": items(30)})
            r = await w.admin.put(f"/api/teams/{w.team}/members", json={"user_ids": [w.carol_id]})
            assert r.status_code == 200
            r = await w.admin.post("/api/approvals/1/approve", json={})
            assert r.json()["status"] == "failed" and "Unknown tool" in r.json()["outcome"]
            assert w.calls == []
            err, msg = await w.call(w.bob, "gateway__wait_for_approval", {"id": 1})
            assert err and "Unknown tool" in msg

            # Nobody decides their own request; members cannot decide at all.
            admin_id = (await w.admin.get("/api/me")).json()["id"]
            w.conn.execute(
                "INSERT INTO approvals (status, user_id, gateway, tool, arguments, items, budget, requested_at)"
                " VALUES ('pending', ?, 'main', ?, ?, 30, '20 items/hour', ?)",
                (admin_id, DESC, json.dumps(items(30)), time.time()))
            assert (await w.admin.post("/api/approvals/2/approve", json={})).status_code == 403
            carol = httpx2.AsyncClient(base_url=w.gw, timeout=10, headers={"Authorization": f"Bearer {w.carol}"})
            assert (await carol.post("/api/approvals/2/reject", json={})).status_code == 403
            assert (await carol.get("/api/approvals")).status_code == 403

    asyncio.run(run())


def test_budgets_api_validates(db_url):
    async def run():
        async with World(db_url) as w:
            bad = [
                {"server_id": 999, "tool": "bulk_update_product_descriptions", "max_items": 5},
                {"server_id": w.sid, "tool": "bulk_update_product_descriptions", "max_items": 0},
                {"server_id": w.sid, "tool": "bulk_update_product_descriptions", "max_items": -3},
                {"server_id": w.sid, "tool": "bulk_update_product_descriptions", "max_items": 2.5},
                {"server_id": w.sid, "tool": "bulk_update_product_descriptions", "max_refund_total": "100"},
                {"server_id": w.sid, "tool": "bulk_refund_orders", "max_refund_total": "10.005"},
                {"server_id": w.sid, "tool": "bulk_refund_orders"},
            ]
            for b in bad:
                await put_budgets(w.admin, w.team, [b], status=400)
            [team] = (await w.admin.get("/api/teams")).json()
            assert {b["tool"]: (b["max_items"], b["max_refund_total"]) for b in team["budgets"]["main"]} == {
                "bulk_update_product_descriptions": (20, None), "bulk_refund_orders": (20, "1000.00"),
                "update_order_status": (3, None)}
            # Detaching the server drops its budgets.
            await w.admin.put("/api/gateways/main/servers", json={"server_ids": []})
            assert w.conn.execute("SELECT COUNT(*) AS n FROM team_budgets").fetchone()["n"] == 0
            r = await w.admin.post("/api/servers", json={"name": "gateway", "url": "http://x/mcp"})
            assert r.status_code == 400 and "reserved" in r.text

    asyncio.run(run())
