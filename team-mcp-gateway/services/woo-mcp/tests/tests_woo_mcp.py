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


def test_the_six_tools_are_listed_in_both_protocol_eras():
    expected = {
        "list_products",
        "create_product",
        "delete_product",
        "list_orders",
        "get_order",
        "update_order_status",
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
                        "create_product", {"name": "Demo Mug", "price": "349"}
                    )
                )
                assert made["name"] == "Demo Mug" and made["status"] == "publish"

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
                    "create_product", {"name": "X", "price": "free"}
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
                    len((await client.list_tools()).tools) == 6
                )  # listing still works
                r = await client.call_tool("list_orders", {})
                assert r.is_error and "Could not reach the store" in r.content[0].text

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
