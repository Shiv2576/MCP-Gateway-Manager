"""PII protection end to end: MCP client -> edge -> a shop upstream with real PII."""

from __future__ import annotations

import asyncio
import json
import time

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

import app.upstream as upstream_mod
from app import db as store, pii
from app.main import Settings, create_app
from tests import KEY
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, edge_settings, gateway_client, http_app, serve, setup_admin, text, wait_tools,
)

EMAIL = "alice.backup@gmail.com"


class Item(BaseModel):
    product_id: int
    name: str | None = None


class Order(BaseModel):
    id: int
    total: str
    customer: str | None = None
    customer_note: str | None = None
    ship_to: str | None = None
    payment_method: str | None = None
    items: list[Item]


ORDER = Order(id=2042, total="149.00", customer="Alice Example", customer_note=f"mail {EMAIL} or +91 98765 43210",
              ship_to="Pune, MH, IN", payment_method="UPI", items=[Item(product_id=7, name="Mug")])


def build_shop() -> MCPServer:
    server = MCPServer("shop")

    @server.tool()
    async def get_order() -> Order:
        """One order, typed, so the tool advertises an output schema."""
        return ORDER

    @server.tool()
    async def fail() -> str:
        """An upstream error that echoes PII."""
        raise ToolError(f"no order for {EMAIL}")

    @server.tool()
    async def slow_text() -> str:
        """Text that a slow custom pattern takes seconds on."""
        return "a" * 60 + "!"

    return server


async def shop_setup(gw: str, shop: str):
    admin = await setup_admin(gw)
    sid = (await admin.post("/api/servers", json={"name": "shop", "url": f"{shop}/mcp"})).json()["id"]
    r = await admin.post("/api/gateways", json={"slug": "support", "name": "Support", "pii_protection": True})
    assert r.status_code == 201 and r.json()["pii_protection"] is True, r.text
    await admin.put("/api/gateways/support/servers", json={"server_ids": [sid]})
    await add_gateway(admin, "ops", [sid])
    key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
    return admin, sid, key


def control(db_url):
    return create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
                      gateways=FakeGateways())


def test_protection_masks_per_gateway_and_audits_the_payload(db_url):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            with serve(create_app(edge_settings(db_url, "support"))) as support, \
                 serve(create_app(edge_settings(db_url, "ops"))) as ops:
                await wait_tools(support, key, 3)
                await wait_tools(ops, key, 3)

                async with gateway_client(support, key) as mcp:
                    listed = {t.name: t for t in (await mcp.list_tools()).tools}
                    assert listed["shop__get_order"].output_schema is None  # amendment 1
                    r = await mcp.call_tool("shop__get_order", {})
                    assert r.structured_content == {
                        "id": 2042, "total": "149.00", "customer": "[PERSON]", "customer_note": "[FREE_TEXT]",
                        "ship_to": "[ADDRESS]", "payment_method": "UPI", "items": [{"product_id": 7, "name": "Mug"}]}
                    assert "Alice" not in text(r) and EMAIL not in text(r)
                    e = await mcp.call_tool("shop__fail", {})
                    assert e.is_error and "no order for [EMAIL]" in text(e)

                async with gateway_client(ops, key) as mcp:
                    listed = {t.name: t for t in (await mcp.list_tools()).tools}
                    assert listed["shop__get_order"].output_schema is not None
                    r = await mcp.call_tool("shop__get_order", {})
                    assert r.structured_content["customer"] == "Alice Example"
                    e = await mcp.call_tool("shop__fail", {})
                    assert EMAIL in text(e)  # protection off: the member sees it

                log = (await admin.get("/api/audit")).json()
                assert all("payload" not in row for row in log)
                by = {(row["gateway"], row["tool"]): row for row in reversed(log)}
                on = by[("support", "shop__get_order")]
                assert json.loads(on["pii"]) == {"PERSON": 1, "FREE_TEXT": 1, "ADDRESS": 1}
                full = (await admin.get(f"/api/audit/{on['id']}")).json()
                assert full["payload_masked_for_log"] is False
                assert json.loads(full["payload"])["structuredContent"]["customer"] == "[PERSON]"
                off = (await admin.get(f"/api/audit/{by[('ops', 'shop__get_order')]['id']}")).json()
                assert off["payload_masked_for_log"] is True and "Alice" not in off["payload"]
                for row in log:  # no raw PII anywhere, error column included
                    full = (await admin.get(f"/api/audit/{row['id']}")).json()
                    assert EMAIL not in json.dumps(full), full

                # The switch applies on the next call, no restart.
                r = await admin.put("/api/gateways/ops/pii", json={"enabled": True})
                assert r.status_code == 200 and r.json()["pii_protection"] is True
                async with gateway_client(ops, key) as mcp:
                    assert (await mcp.call_tool("shop__get_order", {})).structured_content["customer"] == "[PERSON]"
                assert (await admin.put("/api/gateways/ops/pii", json={"enabled": "yes"})).status_code == 400
                assert (await admin.put("/api/gateways/nope/pii", json={"enabled": True})).status_code == 404

    asyncio.run(run())


def test_a_filter_failure_fails_only_that_call(db_url, monkeypatch):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            with serve(create_app(edge_settings(db_url, "support"))) as support:
                await wait_tools(support, key, 3)

                def boom(*a):
                    raise RuntimeError("bug")

                monkeypatch.setattr(upstream_mod, "redact_result", boom)
                async with gateway_client(support, key) as mcp:
                    r = await mcp.call_tool("shop__get_order", {})
                assert r.is_error and text(r) == "Result withheld: the PII filter failed. An admin can check the gateway log."

                monkeypatch.undo()
                # A real catastrophic custom pattern, not a sleep: proves the regex
                # engine itself lets go of the event loop and stops at the deadline.
                r = await admin.post("/api/pii-rules", json={"name": "SLOW", "type": "pattern", "match": r"(\w|\w\w)+$"})
                assert r.status_code == 201, r.text

                async def health_while_stuck():
                    await asyncio.sleep(0.5)
                    started = time.monotonic()
                    async with httpx2.AsyncClient(base_url=support) as c:
                        assert (await c.get("/healthz")).status_code == 200
                    return time.monotonic() - started

                async def stuck_call():
                    async with gateway_client(support, key) as mcp:
                        return await mcp.call_tool("shop__slow_text", {})

                started = time.monotonic()
                r, health_s = await asyncio.gather(stuck_call(), health_while_stuck())
                assert r.is_error and "took too long" in text(r)
                assert time.monotonic() - started < 2.9  # withheld at the 2 s deadline, not after the regex
                assert health_s < 0.5  # the event loop stayed free

                log = (await admin.get("/api/audit")).json()
                assert [json.loads(row["pii"]) for row in log[:2]] == [{"FILTER_TIMEOUT": 1}, {"FILTER_ERROR": 1}]

    asyncio.run(run())


def test_an_echoed_credential_never_reaches_the_audit_log(db_url):
    token = "shop-5551234567-abcdefgh"  # 10 digits: PHONE must not get to it before the scrub

    def build_echo() -> MCPServer:
        server = MCPServer("echo")

        @server.tool()
        async def token_only(ctx: Context) -> str:
            """Echoes the bare bearer token, as a careless error message might."""
            return "bad token " + ctx.request_context.request.headers["authorization"].split()[-1]

        return server

    async def run():
        with serve(http_app(build_echo(), "Authorization", f"Bearer {token}")) as echo, serve(control(db_url)) as gw:
            admin = await setup_admin(gw)
            sid = (await admin.post("/api/servers", json={"name": "echo", "url": f"{echo}/mcp",
                                                          "auth": {"type": "bearer", "token": token}})).json()["id"]
            await add_gateway(admin, "ops", [sid])
            key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
            with serve(create_app(edge_settings(db_url, "ops"))) as ops:
                await wait_tools(ops, key, 1)
                async with gateway_client(ops, key) as mcp:
                    got = text(await mcp.call_tool("echo__token_only", {}))  # protection off
                    assert "abcdefgh" not in got and got == "bad token [CREDENTIAL]"
            row = (await admin.get("/api/audit")).json()[0]
            full = (await admin.get(f"/api/audit/{row['id']}")).json()
            assert "abcdefgh" not in full["payload"] and "bad token [CREDENTIAL]" in full["payload"]

    asyncio.run(run())


def test_pii_rules_api(db_url):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            await admin.post("/api/users", json={"username": "rahul", "password": "rahul-password-123"})
            rahul = httpx2.AsyncClient(base_url=gw, timeout=10)
            await rahul.post("/api/login", json={"username": "rahul", "password": "rahul-password-123"})
            assert (await rahul.get("/api/pii-rules")).status_code == 403
            assert (await rahul.post("/api/pii-rules", json={"name": "X1", "type": "key", "match": "x"})).status_code == 403

            new = lambda **b: admin.post("/api/pii-rules", json=b)
            assert (await new(name="AADHAAR", type="pattern", match=r"\b\d{4}\s?\d{4}\s?\d{4}\b")).status_code == 201
            assert (await new(name="AADHAAR", type="pattern", match=r"\b\d{4}\s?\d{4}\s?\d{4}\b")).status_code == 409
            r = await new(name="BAD", type="pattern", match="(")
            assert r.status_code == 400 and "regex" in r.json()["error"]
            assert (await new(name="LONG", type="pattern", match="a" * 201)).status_code == 400
            for empty in ("z*", "x?", r"\b"):  # matches the empty string: would wrap every character
                r = await new(name="EMPTY", type="pattern", match=empty)
                assert r.status_code == 400 and "empty" in r.json()["error"], empty
            assert (await new(name="lower", type="key", match="x")).status_code == 400
            assert (await new(name="X1", type="nope", match="x")).status_code == 400
            assert (await new(name="PAY", type="field", match="payment_method")).status_code == 400  # no server/tool
            assert (await new(name="PAY", type="field", match="bad path!", server_id=sid, tool="get_order")).status_code == 400
            assert (await new(name="PAY", type="field", match="x", server_id=999, tool="get_order")).status_code == 400
            pay = await new(name="REDACTED", type="field", match="payment_method", server_id=sid, tool="get_order")
            assert pay.status_code == 201
            assert (await new(name="REDACTED", type="field", match="payment_method", server_id=sid, tool="get_order")).status_code == 409
            assert (await new(name="REDACTED", type="field", match="gone_field", server_id=sid, tool="get_order")).status_code == 201

            listed = (await admin.get("/api/pii-rules")).json()
            assert {r["name"] for r in listed["rules"] if r["is_default"]} == {r.name for r in pii.DEFAULTS}
            assert [r["name"] for r in listed["rules"] if not r["is_default"]] == ["AADHAAR", "REDACTED", "REDACTED"]

            for _ in range(50):  # the control plane's health loop has to see the shop first
                fields = (await admin.get("/api/pii-rules/fields")).json()
                if fields and fields[0]["tools"]:
                    break
                await asyncio.sleep(0.1)
            tools = {t["tool"]: t for t in fields[0]["tools"]}
            by_path = {f["path"]: f for f in tools["get_order"]["fields"]}
            assert by_path["customer"]["covered_by"] == "PERSON"
            assert by_path["payment_method"]["rule"]["name"] == "REDACTED"
            assert by_path["items[].name"] == {"path": "items[].name", "type": "string", "description": None,
                                               "rule": None, "covered_by": None}
            assert [s["path"] for s in tools["get_order"]["stale"]] == ["gone_field"]

            # The new rules apply on the next call through a protected gateway.
            with serve(create_app(edge_settings(db_url, "support"))) as support:
                await wait_tools(support, key, 3)
                async with gateway_client(support, key) as mcp:
                    r = await mcp.call_tool("shop__get_order", {})
                assert r.structured_content["payment_method"] == "[REDACTED]"

            t = await admin.post("/api/pii-rules/test", json={"payload": {"x": "id 2345 6789 0123", "payment_method": "UPI"},
                                                             "server_id": sid, "tool": "get_order"})
            assert t.json() == {"payload": {"x": "id [AADHAAR]", "payment_method": "[REDACTED]"},
                                "counts": {"AADHAAR": 1, "REDACTED": 1}}
            assert (await admin.post("/api/pii-rules/test", json={})).status_code == 400
            before = len((await admin.get("/api/audit")).json())
            await admin.post("/api/pii-rules/test", json={"payload": "a@b.co"})
            assert len((await admin.get("/api/audit")).json()) == before  # stores nothing

            slow = (await new(name="SLOW", type="pattern", match=r"(\w|\w\w)+$")).json()
            r = await admin.post("/api/pii-rules/test", json={"payload": {"x": "a" * 60 + "!"}})
            assert r.status_code == 400 and "too long" in r.json()["error"]
            assert (await admin.delete(f"/api/pii-rules/{slow['id']}")).status_code == 200
            assert (await admin.delete(f"/api/pii-rules/{slow['id']}")).status_code == 404

            await admin.delete(f"/api/servers/{sid}")  # field rules go with their server
            rules = (await admin.get("/api/pii-rules")).json()["rules"]
            assert [r["name"] for r in rules if not r["is_default"]] == ["AADHAAR"]

            # Defaults are rules like any other: deleted, they stop masking and are not seeded back.
            email = next(r for r in rules if r["name"] == "EMAIL")
            assert (await admin.delete(f"/api/pii-rules/{email['id']}")).status_code == 200
            t = await admin.post("/api/pii-rules/test", json={"payload": "mail a@b.co, ssn 123-45-6789"})
            assert t.json()["payload"] == "mail a@b.co, ssn [SSN]"
            conn = store.connect(db_url)
            pii.seed_defaults(conn)
            assert "EMAIL" not in {r.name for r in pii.load_rules(conn)}
            conn.close()
            assert (await new(name="EMAIL", type="pattern", match=email["match"])).status_code == 201  # and come back by hand

    asyncio.run(run())
