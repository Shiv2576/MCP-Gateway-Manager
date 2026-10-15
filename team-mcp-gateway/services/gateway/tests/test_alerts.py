"""Team alerts: a rule on a team, a successful call by anyone, one alert the team sees."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from tests import KEY
from app import db as store
from app.main import Settings, create_app
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, add_team, edge_settings, gateway_client, http_app, serve, setup_admin, text,
    wait_tools,
)
from tests.test_identity import member

PLAN, RECEIVE, CRASH = "woo__create_restock_plan", "woo__receive_stock", "woo__crash"


class Result(BaseModel):
    id: int
    ok: bool
    error: str | None = None
    detail: str | None = None


class Bulk(BaseModel):
    ok: int
    failed: int
    results: list[Result]


def build_woo() -> MCPServer:
    """create_restock_plan answers like woo-mcp's bulk tools; an item with "bad" is refused."""
    server = MCPServer("woo")

    @server.tool(name="create_restock_plan", title="Create restock plan")
    async def plan(items: list[dict]) -> Bulk:
        results = [Result(id=i["product_id"], ok=False, error="refused") if i.get("bad") else
                   Result(id=i["product_id"], ok=True, detail=f"PO #7: {i['qty']} x 110.00 {i.get('note', '')}".strip())
                   for i in items]
        return Bulk(ok=sum(r.ok for r in results), failed=sum(not r.ok for r in results), results=results)

    @server.tool(name="receive_stock")
    async def receive(po_id: int) -> str:
        return f"received PO #{po_id}"

    @server.tool(name="crash")
    async def crash() -> str:
        raise ValueError("store down")

    return server


def plan(*items) -> dict:
    return {"items": [{"product_id": pid, "name": name, "qty": qty, **extra} for pid, name, qty, extra in items]}


class AlertWorld:
    """Control plane, the woo upstream and one edge. finance (priya) may plan; store-operators
    (ravi) and support (omar) may only receive. store-operators is alerted when a plan succeeds."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    async def __aenter__(self):
        self._stack = contextlib.ExitStack()
        woo = self._stack.enter_context(serve(http_app(build_woo())))
        self.gw = self._stack.enter_context(serve(create_app(
            Settings(database_url=self.db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
            gateways=FakeGateways())))
        self.admin = await setup_admin(self.gw)
        self.sid = (await self.admin.post("/api/servers", json={"name": "woo", "url": f"{woo}/mcp"})).json()["id"]
        await add_gateway(self.admin, "main", [self.sid])
        self.priya_id, self.priya = await member(self.admin, self.gw, "priya")
        self.ravi_id, self.ravi = await member(self.admin, self.gw, "ravi")
        self.omar_id, self.omar = await member(self.admin, self.gw, "omar")
        self.finance = await add_team(self.admin, "finance", [self.priya_id], "main",
                                      [(self.sid, "create_restock_plan"), (self.sid, "crash")])
        self.ops = await add_team(self.admin, "store-operators", [self.ravi_id], "main", [(self.sid, "receive_stock")])
        self.support = await add_team(self.admin, "support", [self.omar_id], "main", [(self.sid, "receive_stock")])
        r = await self.admin.put(f"/api/teams/{self.ops}/gateways/main/alert-rules", json={"rules": [
            {"server_id": self.sid, "tool": "create_restock_plan"}, {"server_id": self.sid, "tool": "crash"}]})
        assert r.status_code == 200, r.text
        self.edge = self._stack.enter_context(serve(create_app(edge_settings(self.db_url, "main", calls_per_minute=1000))))
        self.conn = store.connect(self.db_url, create_schema=False)
        await wait_tools(self.edge, self.priya, 2)
        return self

    async def __aexit__(self, *exc):
        self.conn.close()
        self._stack.close()

    def api(self, key: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(base_url=self.gw, headers={"Authorization": f"Bearer {key}"}, timeout=10)

    async def call(self, key: str, name: str, args: dict):
        async with gateway_client(self.edge, key) as mcp:
            r = await mcp.call_tool(name, args)
            return r.is_error, text(r)

    def alerts(self) -> list[dict]:
        return self.conn.execute("SELECT * FROM team_alerts ORDER BY id").fetchall()


def test_a_rule_turns_a_successful_call_into_one_team_alert(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            # store-operators cannot call the planning tool, yet hears when finance's call succeeds.
            err, _ = await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {}), (21, "Noodles", 5, {"bad": True})))
            assert not err
            [a] = w.alerts()
            assert (a["team_id"], a["gateway"], a["tool"], a["title"], a["requested_by"], a["approval_id"], a["done_at"]) == (
                w.ops, "main", PLAN, "Create restock plan", w.priya_id, None, None)
            assert json.loads(a["lines"]) == ["Fresh Deodorant 150ml (#20): PO #7: 279 x 110.00", "1 item refused"]

            assert not (await w.call(w.priya, PLAN, plan((21, "Noodles", 5, {"bad": True}))))[0]  # every item refused
            assert (await w.call(w.priya, CRASH, {}))[0]  # an error result
            assert not (await w.call(w.ravi, RECEIVE, {"po_id": 7}))[0]  # no rule for this tool
            assert len(w.alerts()) == 1

            # Content comes from the masked copy: an email in a detail never reaches the team.
            await w.call(w.priya, PLAN, plan((22, "Tea", 1, {"note": "call priya@example.com"})))
            assert "priya@example.com" not in w.alerts()[-1]["lines"]

    asyncio.run(run())


def test_an_approved_run_alerts_with_its_approval(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            r = await w.admin.put(f"/api/teams/{w.finance}/gateways/main/budgets",
                                  json={"budgets": [{"server_id": w.sid, "tool": "create_restock_plan", "max_items": 0}]})
            assert r.status_code == 200, r.text
            err, msg = await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            assert err and "Approval #1 requested" in msg, msg
            assert w.alerts() == []
            assert (await w.admin.post("/api/approvals/1/approve", json={})).status_code == 200
            [a] = w.alerts()
            assert (a["approval_id"], a["requested_by"]) == (1, w.priya_id)

    asyncio.run(run())


def test_alert_rules_api(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            other = (await w.admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            r = await w.admin.put(f"/api/teams/{w.ops}/gateways/main/alert-rules",
                                  json={"rules": [{"server_id": other, "tool": "x"}]})
            assert r.status_code == 400 and "not attached" in r.text
            async with w.api(w.ravi) as ravi:
                assert (await ravi.put(f"/api/teams/{w.ops}/gateways/main/alert-rules", json={"rules": []})).status_code == 403
            teams = {t["name"]: t for t in (await w.admin.get("/api/teams")).json()}
            assert [r["tool"] for r in teams["store-operators"]["alert_rules"]["main"]] == ["crash", "create_restock_plan"]
            # Detaching the server deletes its rules, like team tools.
            assert (await w.admin.put("/api/gateways/main/servers", json={"server_ids": []})).status_code == 200
            assert w.conn.execute("SELECT COUNT(*) AS n FROM team_alert_rules").fetchone()["n"] == 0

    asyncio.run(run())


def test_members_see_their_teams_alerts_and_mark_them_done(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            async with w.api(w.ravi) as ravi, w.api(w.omar) as omar, w.api(w.priya) as priya:
                [a] = (await ravi.get("/api/alerts")).json()
                assert (a["team"], a["title"], a["requested_by"], a["approved_by"], a["done_by"]) == (
                    "store-operators", "Create restock plan", "priya", None, None)
                assert (await ravi.get("/api/alerts/count")).json() == {"open": 1}
                assert (await omar.get("/api/alerts")).json() == []  # another team
                assert (await priya.get("/api/alerts")).json() == []  # the caller's own team has no rule
                assert len((await w.admin.get("/api/alerts")).json()) == 1  # admins see every team's

                assert (await omar.post(f"/api/alerts/{a['id']}/done")).status_code == 404
                r = await ravi.post(f"/api/alerts/{a['id']}/done")
                assert r.status_code == 200 and r.json()["done_by"] == "ravi"
                r = await w.admin.post(f"/api/alerts/{a['id']}/done")
                assert r.status_code == 409 and "already done by ravi" in r.text
                assert (await ravi.get("/api/alerts")).json() == []  # open is the default
                assert [x["done_by"] for x in (await ravi.get("/api/alerts?status=done")).json()] == ["ravi"]
                assert (await ravi.get("/api/alerts?status=nope")).status_code == 400

                # Leaving the team hides its alerts.
                assert (await w.admin.put(f"/api/teams/{w.ops}/members", json={"user_ids": []})).status_code == 200
                assert (await ravi.get("/api/alerts?status=all")).json() == []

    asyncio.run(run())


def test_agents_list_and_mark_alerts_through_mcp(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            async with gateway_client(w.edge, w.ravi) as mcp:
                names = {t.name for t in (await mcp.list_tools()).tools}
                assert {"gateway__list_team_alerts", "gateway__mark_team_alert_done"} <= names
                r = await mcp.call_tool("gateway__list_team_alerts", {})
                [a] = r.structured_content["alerts"]
                assert a["lines"] == ["Fresh Deodorant 150ml (#20): PO #7: 279 x 110.00"]
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert not r.is_error and f"Alert #{a['id']} marked done" in text(r)
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert r.is_error and "already done by ravi" in text(r)
                assert (await mcp.call_tool("gateway__list_team_alerts", {})).structured_content["alerts"] == []
            async with gateway_client(w.edge, w.omar) as mcp:
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert r.is_error and "no such alert" in text(r)

    asyncio.run(run())
