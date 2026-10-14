"""End-to-end tests: a real MCP client -> this server over HTTP -> a fake store.

Run:  uv run python -m tests.test_woo_mcp        (from the woo-mcp folder)
The functions are also named test_* and take no fixtures, so pytest collects them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from oauthlib.oauth1.rfc5849 import signature as oauth_sig
from tests.fake_woo import (
    CONSUMER_KEY,
    CONSUMER_SECRET,
    PUBLIC_ORIGIN,
    READ_KEY,
    READ_SECRET,
    build_fake_woo,
    make_order,
)

from app.config import ConfigError, Settings
from app.main import create_app
from app.server import day_bounds_utc
from app.woo import sign_oauth1

TOKEN = "test-token-0123456789abcdefghij"
TZ = "Asia/Kolkata"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(app):
    """Run an ASGI app on a real local port in a background thread."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def settings_for(store_url: str, **overrides) -> Settings:
    base = Settings(
        woo_internal_url=store_url,
        woo_public_url=PUBLIC_ORIGIN,
        woo_consumer_key=CONSUMER_KEY,
        woo_consumer_secret=CONSUMER_SECRET,
        woo_auth_mode="oauth1",
        woo_timeout_s=1.0,
        mcp_auth_token=TOKEN,
        store_timezone=TZ,
    )
    return dataclasses.replace(base, **overrides)


@contextlib.asynccontextmanager
async def mcp_client(url: str, token: str = TOKEN, mode: str = "auto"):
    """Connect exactly as the gateway will: one URL plus a bearer header."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=10
    ) as http:
        async with Client(
            streamable_http_client(f"{url}/mcp", http_client=http), mode=mode
        ) as client:
            yield client


def _payload(result):
    assert not result.is_error, result.content
    return result.structured_content


# ---------------------------------------------------------------- pure logic


def test_signature_agrees_with_oauthlib():
    params = {"per_page": "10", "search": "trail mix & nuts", "status": "on-hold"}
    url = f"{PUBLIC_ORIGIN}/wp-json/wc/v3/orders"
    signed = sign_oauth1(
        "get",
        url,
        params,
        CONSUMER_KEY,
        CONSUMER_SECRET,
        nonce="abc123",
        timestamp=1_760_000_000,
    )
    supplied = signed.pop("oauth_signature")
    base = oauth_sig.signature_base_string(
        "GET", url, oauth_sig.normalize_parameters(list(signed.items()))
    )
    assert supplied == oauth_sig.sign_hmac_sha256(base, CONSUMER_SECRET, "")


def test_today_is_the_stores_day_not_utc():
    # 20:00 UTC on the 8th is already 01:30 on the 9th in India.
    now = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
    assert day_bounds_utc("today", TZ, now=now) == (
        "2026-10-08T18:30:00",
        "2026-10-09T18:30:00",
    )
    assert day_bounds_utc("yesterday", TZ, now=now)[1] == "2026-10-08T18:30:00"
    assert day_bounds_utc("2026-01-15", TZ)[0] == "2026-01-14T18:30:00"


def test_startup_refuses_missing_or_weak_token():
    import os

    good = {
        "WOO_INTERNAL_URL": "http://wp",
        "WOO_CONSUMER_KEY": "ck",
        "WOO_CONSUMER_SECRET": "cs",
    }
    saved = dict(os.environ)
    try:
        for bad_token in (None, "short"):
            os.environ.clear()
            os.environ.update(good)
            if bad_token:
                os.environ["MCP_AUTH_TOKEN"] = bad_token
            try:
                Settings.from_env()
            except ConfigError:
                continue
            raise AssertionError(f"accepted token {bad_token!r}")
        os.environ["MCP_AUTH_TOKEN"] = TOKEN
        assert Settings.from_env().woo_auth_mode == "oauth1"
        os.environ["WOO_INTERNAL_URL"] = "https://wp"
        assert Settings.from_env().woo_auth_mode == "basic"
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ------------------------------------------------------------- over the wire


def test_token_is_required_but_health_is_open():
    async def run():
        with (
            serve(build_fake_woo()) as store,
            serve(create_app(settings_for(store))) as url,
        ):
            async with httpx2.AsyncClient() as http:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                assert (await http.post(f"{url}/mcp", json=body)).status_code == 401
                wrong = {"Authorization": "Bearer " + "x" * len(TOKEN)}
                assert (
                    await http.post(f"{url}/mcp", json=body, headers=wrong)
                ).status_code == 401
                assert (await http.get(f"{url}/healthz")).status_code == 200
                assert (await http.get(f"{url}/readyz")).json()["status"] == "ready"

    asyncio.run(run())


def test_the_eighteen_tools_are_listed_in_both_protocol_eras():
    expected = {
        "list_products",
        "create_product",
        "delete_product",
        "list_orders",
        "get_order",
        "update_order_status",
        "get_sales_summary",
        "get_refund_summary",
        "get_best_selling_products",
        "get_low_stock_products",
        "audit_product_quality",
        "get_customer_timeline",
        "bulk_update_product_descriptions",
        "bulk_update_product_prices",
        "bulk_update_orders",
        "bulk_refund_orders",
        "get_abandoned_carts",
        "create_cart_offers",
    }

    async def run():
        with (
            serve(build_fake_woo()) as store,
            serve(create_app(settings_for(store))) as url,
        ):
            for mode in ("auto", "legacy"):
                async with mcp_client(url, mode=mode) as client:
                    tools = (await client.list_tools()).tools
                    assert {t.name for t in tools} == expected, mode
                    assert all(t.description and t.input_schema for t in tools)

    asyncio.run(run())


def test_demo_flow_create_product_then_todays_orders():
    fake = build_fake_woo()
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S"
    fake.state.orders[501] = make_order(501, now.strftime(fmt))
    fake.state.orders[400] = make_order(
        400, (now - timedelta(days=3)).strftime(fmt), status="completed"
    )

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                made = _payload(
                    await client.call_tool(
                        "create_product", {"name": "Demo Mug", "price": "349", "stock_quantity": 1}
                    )
                )
                assert made["name"] == "Demo Mug" and made["status"] == "publish"
                low = _payload(await client.call_tool("get_low_stock_products", {}))
                assert [p["id"] for p in low["products"]] == [made["id"]]
                no_stock = await client.call_tool("create_product", {"name": "Y", "price": "1"})
                assert no_stock.is_error

                found = _payload(
                    await client.call_tool("list_products", {"search": "mug"})
                )
                assert [p["id"] for p in found["products"]] == [made["id"]]

                gone = _payload(
                    await client.call_tool("delete_product", {"product_id": made["id"]})
                )
                assert gone["status"] == "trash"
                found = _payload(
                    await client.call_tool("list_products", {"search": "mug"})
                )
                assert found["count"] == 0
                missing = await client.call_tool("delete_product", {"product_id": 999})
                assert missing.is_error

                today = _payload(
                    await client.call_tool("list_orders", {"on_date": "today"})
                )
                assert [o["id"] for o in today["orders"]] == [501]
                everything = _payload(await client.call_tool("list_orders", {}))
                assert everything["count"] == 2

                order = _payload(await client.call_tool("get_order", {"order_id": 501}))
                assert order["customer"] == "Priya Sharma"
                assert order["payment_method"] == "Cash on delivery"
                assert "priya@example.com" not in str(order)  # email is not exposed

                done = _payload(
                    await client.call_tool(
                        "update_order_status", {"order_id": 501, "status": "completed"}
                    )
                )
                assert done["status"] == "completed"
                completed = _payload(
                    await client.call_tool("list_orders", {"status": "completed"})
                )
                assert {o["id"] for o in completed["orders"]} == {400, 501}

    asyncio.run(run())


def test_bad_input_and_missing_order_give_readable_errors():
    async def run():
        with (
            serve(build_fake_woo()) as store,
            serve(create_app(settings_for(store))) as url,
        ):
            async with mcp_client(url) as client:
                missing = await client.call_tool("get_order", {"order_id": 999})
                assert missing.is_error and "not found" in missing.content[0].text

                bad_price = await client.call_tool(
                    "create_product", {"name": "X", "price": "free", "stock_quantity": 1}
                )
                assert bad_price.is_error

                bad_status = await client.call_tool(
                    "update_order_status", {"order_id": 1, "status": "shipped"}
                )
                assert bad_status.is_error

                bad_date = await client.call_tool(
                    "list_orders", {"on_date": "last tuesday"}
                )
                assert bad_date.is_error and "on_date" in bad_date.content[0].text

    asyncio.run(run())


def test_store_problems_become_clear_errors_not_hangs():
    fake = build_fake_woo()

    async def run():
        # 1. Wrong API secret: the store answers 401.
        with (
            serve(fake) as store,
            serve(
                create_app(settings_for(store, woo_consumer_secret="cs_wrong"))
            ) as url,
        ):
            async with mcp_client(url) as client:
                r = await client.call_tool("list_products", {})
                assert (
                    r.is_error and "rejected this server's API key" in r.content[0].text
                )
                assert "cs_wrong" not in r.content[0].text

        # 2. Store is slow: we give up after woo_timeout_s (1s here), not 5s.
        fake.state.flags["slow"] = True
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                started = time.monotonic()
                r = await client.call_tool("list_products", {})
                assert r.is_error and "did not answer within 1s" in r.content[0].text
                assert time.monotonic() - started < 3

        # 3. Store is gone: nothing listens on that port.
        dead = f"http://127.0.0.1:{_free_port()}"
        with serve(create_app(settings_for(dead))) as url:
            async with mcp_client(url) as client:
                assert (
                    len((await client.list_tools()).tools) == 18
                )  # listing still works
                r = await client.call_tool("list_orders", {})
                assert r.is_error and "Could not reach the store" in r.content[0].text

    asyncio.run(run())


def test_personal_key_from_the_gateway():
    """A personal call carries X-Woo-Credentials instead of the bearer token, and the
    store sees that key, never the one in our env."""
    fake = build_fake_woo()
    fake.state.orders[7] = make_order(7, "2026-01-01T00:00:00")
    personal = {"X-Woo-Credentials": f"{READ_KEY}:{READ_SECRET}"}

    async def call(url, headers, tool, args):
        async with httpx2.AsyncClient(headers=headers, timeout=10) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
                return await client.call_tool(tool, args)

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            used = fake.state.used_keys
            # No bearer token needed, and the read key is the one used.
            assert not (await call(url, personal, "list_orders", {})).is_error
            assert used == [READ_KEY]
            # Read only: WooCommerce refuses the write.
            r = await call(url, personal, "update_order_status", {"order_id": 7, "status": "completed"})
            assert r.is_error and fake.state.orders[7]["status"] == "processing"
            # Bearer and personal together: still the personal key.
            used.clear()
            both = {**personal, "Authorization": f"Bearer {TOKEN}"}
            assert not (await call(url, both, "list_orders", {})).is_error
            assert used == [READ_KEY]

            async with httpx2.AsyncClient() as http:
                rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                assert (await http.post(f"{url}/mcp", json=rpc)).status_code == 401
                for bad in ("nocolon-secret", ":cs_x", "ck_x:"):
                    r = await http.post(f"{url}/mcp", json=rpc, headers={"X-Woo-Credentials": bad})
                    assert r.status_code == 400 and bad not in r.text

            # Overlapping calls with different keys never borrow each other's.
            used.clear()
            writer = {"X-Woo-Credentials": f"{CONSUMER_KEY}:{CONSUMER_SECRET}"}
            results = await asyncio.gather(*(
                call(url, h, "update_order_status", {"order_id": 7, "status": "on-hold"})
                for h in [personal, writer] * 10
            ))
            assert [r.is_error for r in results] == [True, False] * 10
            assert sorted(used) == sorted([READ_KEY, CONSUMER_KEY] * 10)

    asyncio.run(run())


def test_tools_advertise_output_schemas():
    """The gateway's PII Protection tab lists each tool's fields from these schemas."""

    async def run():
        with (
            serve(build_fake_woo()) as store,
            serve(create_app(settings_for(store))) as url,
        ):
            async with mcp_client(url) as client:
                tools = {t.name: t.output_schema for t in (await client.list_tools()).tools}
        props = lambda name: set(tools[name]["properties"])
        assert props("get_order") == {"id", "number", "status", "total", "currency", "created_utc", "customer",
                                      "items", "payment_method", "customer_note", "ship_to"}
        assert props("list_orders") == {"count", "orders"}
        assert props("list_products") == {"count", "products"}
        assert props("create_product") == props("delete_product") == {
            "id", "name", "price", "status", "stock_status", "sku", "url"}
        assert props("update_order_status") == props("get_order") - {"payment_method", "customer_note", "ship_to"}

    asyncio.run(run())


# ---------------------------------------------------------------- reports

def _order_at(oid, created, status="completed", total="100.00", product=(1, "Trail Mix", 1), **extra):
    o = make_order(oid, created, status=status, total=total)
    o["line_items"] = [{"product_id": product[0], "name": product[1], "quantity": product[2], "total": total}]
    o.update(extra)
    return o


def test_sales_refunds_and_best_sellers_follow_the_store_day():
    # Store is Asia/Kolkata: 1 Oct 2026 there is 2026-09-30T18:30 .. 2026-10-01T18:30 UTC.
    fake = build_fake_woo()
    o = fake.state.orders
    o[10] = _order_at(10, "2026-09-30T19:00:00", total="500.00", product=(1, "Trail Mix", 2))
    o[11] = _order_at(11, "2026-10-01T18:00:00", "processing", "300.00", (2, "Mug", 5))
    o[12] = _order_at(12, "2026-10-01T19:00:00", total="1000.00")  # 2 Oct in the store
    o[13] = _order_at(13, "2026-10-01T10:00:00", "cancelled", "999.00")  # not a sale
    o[14] = _order_at(14, "2026-10-01T11:00:00", "refunded", "200.00",
                      refunds=[{"id": 1, "total": "-200.00"}], date_modified_gmt="2026-10-05T00:00:00")
    # An August order, partly refunded on 1 Oct: counts for refunds, not for sales.
    o[15] = _order_at(15, "2026-08-01T00:00:00", refunds=[{"id": 2, "total": "-40.00"}],
                      date_modified_gmt="2026-10-01T05:00:00")
    fake.state.refunds[14] = [{"id": 1, "amount": "200.00", "reason": "", "date_created_gmt": "2026-10-05T00:00:00"}]
    fake.state.refunds[15] = [{"id": 2, "amount": "40.00", "reason": "damaged", "date_created_gmt": "2026-10-01T05:00:00"}]
    day = {"start": "2026-10-01", "end": "2026-10-01"}

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                sales = _payload(await client.call_tool("get_sales_summary", day))
                assert sales["order_count"] == 3  # 10, 11, 14
                assert (sales["gross_sales"], sales["refunds"], sales["net_sales"]) == ("1000.00", "200.00", "800.00")
                assert sales["average_order_value"] == "333.33" and not sales["truncated"]

                refunds = _payload(await client.call_tool("get_refund_summary", day))
                assert refunds["refund_count"] == 1 and refunds["refund_total"] == "40.00"
                assert refunds["refunds"][0]["order_id"] == 15 and refunds["refunds"][0]["reason"] == "damaged"
                week = _payload(await client.call_tool("get_refund_summary", {"start": "2026-10-01", "end": "2026-10-05"}))
                assert (week["refund_total"], week["orders_affected"]) == ("240.00", 2)

                by_units = _payload(await client.call_tool("get_best_selling_products", day))
                assert [(p["product_id"], p["units"]) for p in by_units["products"]] == [(2, 5), (1, 3)]
                by_rev = _payload(await client.call_tool("get_best_selling_products", {**day, "by": "revenue"}))
                assert [(p["product_id"], p["revenue"]) for p in by_rev["products"]] == [(1, "700.00"), (2, "300.00")]

                backwards = await client.call_tool("get_sales_summary", {"start": "2026-10-02", "end": "2026-10-01"})
                assert backwards.is_error and "start must be" in str(backwards.content)

    asyncio.run(run())


def test_low_stock_and_product_quality():
    fake = build_fake_woo()
    good = {"description": "Crunchy", "images": [{"src": "x"}], "categories": [{"slug": "snacks"}]}
    fake.state.products[2] = {**fake.state.products[1], "id": 2, "name": "Nuts", "manage_stock": True,
                              "stock_quantity": 2, **good}
    fake.state.products[3] = {**fake.state.products[1], "id": 3, "name": "Jar", "manage_stock": True,
                              "stock_quantity": 10, "price": "", "categories": [{"slug": "uncategorized"}]}
    # product 1 has no stock tracking, no description, image or category

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                low = _payload(await client.call_tool("get_low_stock_products", {}))
                assert [(p["id"], p["stock_quantity"]) for p in low["products"]] == [(2, 2)]
                low = _payload(await client.call_tool("get_low_stock_products", {"threshold": 11}))
                assert [p["id"] for p in low["products"]] == [2, 3]

                audit = _payload(await client.call_tool("audit_product_quality", {}))
                assert audit["checked"] == 3
                issues = {p["id"]: set(p["issues"]) for p in audit["products"]}
                assert issues == {
                    1: {"missing_description", "missing_image", "missing_category"},
                    3: {"missing_description", "missing_image", "missing_price", "missing_category"},
                }

    asyncio.run(run())


def test_customer_timeline_by_id_or_email():
    fake = build_fake_woo()
    o = fake.state.orders
    o[30] = _order_at(30, "2026-10-02T00:00:00", customer_id=7)
    o[31] = _order_at(31, "2026-10-01T00:00:00", "cancelled", customer_id=7)
    o[32] = _order_at(32, "2026-10-03T00:00:00", customer_id=0)  # guest, same email
    o[33] = _order_at(33, "2026-10-04T00:00:00", customer_id=9, customer_note="call priya@example.com")
    o[33]["billing"] = {"first_name": "Ravi", "last_name": "K", "email": "ravi@example.com"}

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                by_id = _payload(await client.call_tool("get_customer_timeline", {"customer_id": 7}))
                assert [x["id"] for x in by_id["orders"]] == [31, 30]  # oldest first
                assert by_id["total_spent"] == "100.00"  # the cancelled one does not count
                assert by_id["customer"] == "Priya Sharma"

                by_email = _payload(await client.call_tool("get_customer_timeline", {"email": "Priya@Example.com"}))
                assert [x["id"] for x in by_email["orders"]] == [31, 30, 32]  # not 33
                assert "priya@example.com" not in str(by_email)

                for args in ({}, {"customer_id": 7, "email": "priya@example.com"}):
                    assert (await client.call_tool("get_customer_timeline", args)).is_error

    asyncio.run(run())


def test_reports_say_when_they_stopped_early():
    import app.server as server_module

    fake = build_fake_woo()
    for pid in range(2, 102):
        fake.state.products[pid] = {**fake.state.products[1], "id": pid}
    saved = server_module.MAX_PAGES
    server_module.MAX_PAGES = 1  # 101 products, one page of 100

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                audit = _payload(await client.call_tool("audit_product_quality", {}))
                assert audit["checked"] == 100 and audit["truncated"]

    try:
        asyncio.run(run())
    finally:
        server_module.MAX_PAGES = saved


def test_bulk_tools_report_per_item():
    fake = build_fake_woo()
    fake.state.orders[501] = make_order(501, "2026-10-13T08:00:00", total="500.00")
    fake.state.orders[502] = make_order(502, "2026-10-13T09:00:00", total="100.00")

    async def run():
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                desc = _payload(await client.call_tool("bulk_update_product_descriptions", {"items": [
                    {"product_id": 1, "description": "Crunchy"},
                    {"product_id": 999, "short_description": "x"},
                ]}))
                assert (desc["ok"], desc["failed"]) == (1, 1)
                assert desc["results"][1] == {"id": 999, "ok": False, "error": "Invalid ID.", "detail": None}
                assert fake.state.products[1]["description"] == "Crunchy"

                prices = _payload(await client.call_tool("bulk_update_product_prices", {"items": [
                    {"product_id": 1, "regular_price": "249", "sale_price": "199.50"},
                ]}))
                assert prices["ok"] == 1 and fake.state.products[1]["sale_price"] == "199.50"

                orders = _payload(await client.call_tool("bulk_update_orders", {"items": [
                    {"order_id": 501, "status": "completed", "shipping": {"city": "Mumbai"}},
                    {"order_id": 404, "status": "completed"},
                ]}))
                assert [r["ok"] for r in orders["results"]] == [True, False]
                assert fake.state.orders[501]["status"] == "completed"
                assert fake.state.orders[501]["shipping"] == {"city": "Mumbai", "state": "MH", "country": "IN"}

                refunds = _payload(await client.call_tool("bulk_refund_orders", {"items": [
                    {"order_id": 501, "amount": "200", "reason": "damaged"},
                    {"order_id": 502, "amount": "100.01"},
                    {"order_id": 404, "amount": "1"},
                ]}))
                assert [r["ok"] for r in refunds["results"]] == [True, False, False]
                assert "only 100.00 left" in refunds["results"][1]["error"]
                assert fake.state.refunds[501][0]["amount"] == "200" and 502 not in fake.state.refunds
                again = _payload(await client.call_tool("bulk_refund_orders", {"items": [
                    {"order_id": 501, "amount": "300.01"},
                ]}))
                assert "only 300.00 left" in again["results"][0]["error"]

                # Refused outright: too many items, nothing to change, extra fields, duplicates.
                for tool, args in [
                    ("bulk_update_product_prices", {"items": [{"product_id": 1, "regular_price": "1"}] * 101}),
                    ("bulk_update_product_prices", {"items": []}),
                    ("bulk_update_product_descriptions", {"items": [{"product_id": 1}]}),
                    ("bulk_update_orders", {"items": [{"order_id": 501, "shipping": {"planet": "Mars"}}]}),
                    ("bulk_refund_orders", {"items": [{"order_id": 501, "amount": "-5"}]}),
                    ("bulk_refund_orders", {"items": [{"order_id": 502, "amount": "1"}] * 2}),
                ]:
                    assert (await client.call_tool(tool, args)).is_error, (tool, args)
                assert 502 not in fake.state.refunds

    asyncio.run(run())


def _cart_store():
    """Four products in carts: a hero, a thin margin, one without cost, one already on sale."""
    fake = build_fake_woo()
    now = time.time()

    def product(pid, name, price, cogs, **extra):
        fake.state.products[pid] = {"id": pid, "name": name, "regular_price": price, "price": price, "on_sale": False,
                                    "cost_of_goods_sold": {"total_value": cogs}, **extra}

    product(10, "Fresh Deodorant 150ml", "249", 110)
    product(11, "Basmati Rice 5kg", "699", 640)
    product(12, "Mystery Garden Box", "1499", 0)  # Woo reports an unset cost as 0
    product(13, "Linen Shirt", "1499", 690, on_sale=True, sale_price="1199")
    day = 86400
    fake.state.carts.extend([
        {"user_id": 1, "updated_at": now - 2 * day, "items": [{"product_id": 10, "quantity": 2}, {"product_id": 11, "quantity": 1}]},
        {"user_id": 2, "updated_at": now - 5 * day, "items": [{"product_id": 10, "quantity": 1}, {"product_id": 12, "quantity": 1}]},
        {"user_id": 3, "updated_at": now - 9 * day, "items": [{"product_id": 13, "quantity": 1}]},
        {"user_id": 4, "updated_at": now - 25 * day, "items": [{"product_id": 10, "quantity": 5}, {"product_id": 1, "quantity": 1}]},
    ])
    return fake


def test_abandoned_carts_give_facts_and_safe_limits():
    async def run():
        fake = _cart_store()
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                out = _payload(await client.call_tool("get_abandoned_carts", {"days": 20}))
                # The 25-day-old cart is outside the window: its 5 deodorants and Trail Mix do not count.
                assert (out["carts"], out["min_margin_pct"], out["truncated"]) == (3, 10.0, False)
                by = {p["product_id"]: p for p in out["products"]}
                assert set(by) == {10, 11, 12, 13}
                deo = by[10]
                assert (deo["carts"], deo["units"], deo["cart_value"], deo["cogs"]) == (2, 3, "747.00", "110.00")
                assert deo["max_discount_pct"] == 51 - 11  # floor((1 - 121/249) * 100) = 51, capped at 40
                assert deo["margin_pct"] == 55.8
                assert by[11]["max_discount_pct"] == 0  # 640 * 1.1 > 699: already thin
                assert (by[12]["cogs"], by[12]["max_discount_pct"], by[12]["margin_pct"]) == (None, 0, None)
                assert by[13]["on_sale"] is True
                assert [p["product_id"] for p in out["products"]] == [13, 12, 10, 11]  # by cart value
                assert "email" not in str(out) and "user_id" not in str(out)
                top = _payload(await client.call_tool("get_abandoned_carts", {"days": 30, "limit": 1}))
                assert top["carts"] == 4 and top["truncated"] and len(top["products"]) == 1

    asyncio.run(run())


def test_cart_offers_check_every_number_against_the_store():
    def offer(pid, name, price, cogs, pct, sale, days=7):
        return {"product_id": pid, "name": name, "regular_price": price, "cogs": cogs,
                "discount_pct": pct, "sale_price": sale, "ends_in_days": days}

    deo = ("Fresh Deodorant 150ml", "249", "110")

    async def run():
        fake = _cart_store()
        fake.state.products[14] = {"id": 14, "name": "Yoga Mat", "regular_price": "999", "on_sale": False,
                                   "cost_of_goods_sold": {"total_value": 380}}  # in no cart
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                cases = [
                    (offer(10, "Fresh Deo", "249", "110", 20, "199.20"), "name"),
                    (offer(10, deo[0], "199", "110", 20, "159.20"), "regular price is 249.00"),
                    (offer(10, *deo[:2], "100", 20, "199.20"), "cost of goods is 110.00"),
                    (offer(12, "Mystery Garden Box", "1499", "500", 10, "1349.10"), "no cost of goods"),
                    (offer(13, "Linen Shirt", "1499", "690", 10, "1349.10"), "already on sale"),
                    (offer(14, "Yoga Mat", "999", "380", 10, "899.10"), "no cart changed in the last 20 days"),
                    (offer(10, *deo, 20, "199.20", days=15), "ends_in_days must be 1 to 14"),
                    (offer(10, *deo, 50, "120.00"), "under the floor 121.00"),
                    (offer(10, *deo, 20, "249.00"), "below the regular price"),
                    (offer(10, *deo, 10, "199.20"), "is a 20.0% discount, not 10%"),
                    (offer(99, "Gone", "1", "1", 10, "0.90"), "product not found"),
                ]
                for item, why in cases:
                    res = _payload(await client.call_tool("create_cart_offers", {"items": [item]}))
                    assert res["failed"] == 1 and why in res["results"][0]["error"], (why, res)
                assert not fake.state.products[10]["on_sale"]

                # Over the store's 40% cap, though above the floor.
                fake.state.products[15] = {"id": 15, "name": "Serum", "regular_price": "800", "on_sale": False,
                                           "cost_of_goods_sold": {"total_value": 200}}
                fake.state.carts[0]["items"].append({"product_id": 15, "quantity": 1})
                res = _payload(await client.call_tool("create_cart_offers", {"items": [offer(15, "Serum", "800", "200", 50, "400.00")]}))
                assert "over the store's limit of 40%" in res["results"][0]["error"]

                # A mixed batch: the good item goes on sale, the bad one says why.
                res = _payload(await client.call_tool("create_cart_offers", {"items": [
                    offer(10, *deo, 20, "199.20", days=7), offer(11, "Basmati Rice 5kg", "699", "640", 5, "664.05"),
                ]}))
                assert [r["ok"] for r in res["results"]] == [True, False]
                assert res["results"][0]["detail"].startswith("on sale at 199.20 until ")
                assert "under the floor 704.00" in res["results"][1]["error"]
                p = fake.state.products[10]
                assert p["sale_price"] == "199.20" and p["on_sale"]
                end = datetime.fromisoformat(p["date_on_sale_to_gmt"]).replace(tzinfo=timezone.utc)
                assert timedelta(days=6, hours=23) < end - datetime.now(timezone.utc) <= timedelta(days=7)
                # Approved twice: the second run does not stack.
                res = _payload(await client.call_tool("create_cart_offers", {"items": [offer(10, *deo, 20, "199.20")]}))
                assert "already on sale" in res["results"][0]["error"]
                # Duplicates are refused outright.
                assert (await client.call_tool("create_cart_offers", {"items": [offer(10, *deo, 20, "199.20")] * 2})).is_error

    asyncio.run(run())


if __name__ == "__main__":
    import sys
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
