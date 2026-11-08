"""The dashboard chat as server-sent events: each tool call as it starts and ends, then the reply."""

from __future__ import annotations

import asyncio
import json
import time

import httpx2

from tests import KEY
from app.main import Settings, create_app
from tests.test_gateway import (
    ADMIN, NOTES_KEY, SETUP, FakeGateways, add_gateway, add_team, build_shop, fake_llm, http_app, serve, setup_admin,
)


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_chat_streams_each_step_and_the_json_reply_is_unchanged(db_url):
    seen, refunds = [], []

    async def run():
        with serve(http_app(build_shop(refunds), "X-API-Key", NOTES_KEY)) as shop, serve(fake_llm(seen)) as llm:
            settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5,
                                openrouter_api_key="test-llm-key", llm_url=f"{llm}/v1/chat/completions")
            with serve(create_app(settings, gateways=FakeGateways())) as gw:
                await setup_admin(gw)
                admin = httpx2.AsyncClient(base_url=gw, timeout=10)
                await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
                shop_id = (await admin.post("/api/servers", json={"name": "shop", "url": f"{shop}/mcp",
                           "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
                await add_gateway(admin, "main", [shop_id])
                bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
                await add_team(admin, "shop", [bob_id], "main", [(shop_id, "orders"), (shop_id, "refund")])
                bob = httpx2.AsyncClient(base_url=gw, timeout=10)
                await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
                deadline = time.time() + 5
                while not (await bob.get("/api/tools?gateway=main")).json():
                    assert time.time() < deadline
                    await asyncio.sleep(0.1)
                ask = {"gateway": "main", "messages": [{"role": "user", "content": "show my orders"}]}

                r = await bob.post("/api/chat", json=ask, headers={"Accept": "text/event-stream"})
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
                assert r.headers["x-accel-buffering"] == "no"
                got = events(r.text)
                assert [k for k, _ in got] == ["call_start", "call", "done"]
                assert got[0][1] == {"name": "shop__orders", "arguments": {}}
                assert got[1][1]["name"] == "shop__orders" and got[1][1]["ok"] is True
                # The fixture's order note carries an injection asking for a refund: the write is held
                # for Confirm (pending), never run, and the stream ends there.
                assert got[2][1]["calls"] == [got[1][1]] and got[2][1]["pending"]["name"] == "shop__refund"
                assert refunds == []

                # A write is held for Confirm: the stream ends with it pending, nothing has run.
                r = await bob.post("/api/chat", headers={"Accept": "text/event-stream"},
                                   json={**ask, "messages": [{"role": "user", "content": "refund order 7"}]})
                done = events(r.text)[-1]
                assert done[0] == "done" and done[1]["pending"]["name"] == "shop__refund" and refunds == []

                # Without the header: one JSON reply, exactly as before.
                r = await bob.post("/api/chat", json=ask)
                assert r.headers["content-type"].startswith("application/json")
                assert r.json()["calls"][0]["name"] == "shop__orders" and r.json()["pending"]["name"] == "shop__refund"

    asyncio.run(run())


def test_a_model_failure_mid_stream_is_an_error_event(db_url):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                            openrouter_api_key="k", llm_url="http://127.0.0.1:9/v1/chat/completions")  # nothing listens
        with serve(create_app(settings, gateways=FakeGateways())) as gw:
            await setup_admin(gw)
            admin = httpx2.AsyncClient(base_url=gw, timeout=10)
            await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
            await add_gateway(admin, "main", [])
            ask = {"gateway": "main", "messages": [{"role": "user", "content": "hi"}]}
            r = await admin.post("/api/chat", json=ask, headers={"Accept": "text/event-stream"})
            kind, data = events(r.text)[-1]
            assert kind == "error" and "model provider unreachable" in data["error"]
            assert (await admin.post("/api/chat", json=ask)).status_code == 502  # the JSON path: as before

    asyncio.run(run())
