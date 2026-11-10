"""/metrics: every tool call counted by gateway, server and outcome, with its duration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx2
from prometheus_client import REGISTRY

from tests import KEY
from app import metrics
from app.main import Settings, create_app
from tests.test_gateway import (
    NOTES_KEY, SETUP, FakeGateways, _free_port, add_gateway, add_team, build_shop, http_app, serve, setup_admin,
)
from tests.test_stores import login


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0


def test_calls_are_counted_by_outcome_and_unknown_servers_stay_unknown(db_url):
    async def run():
        with serve(http_app(build_shop([]), "X-API-Key", NOTES_KEY)) as shop:
            settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5)
            with serve(create_app(settings, gateways=FakeGateways())) as gw:
                admin = await setup_admin(gw)
                shop_id = (await admin.post("/api/servers", json={"name": "shop", "url": f"{shop}/mcp",
                           "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
                await add_gateway(admin, "main", [shop_id])
                bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
                await add_team(admin, "shop", [bob_id], "main", [(shop_id, "orders")])
                bob = await login(gw, "bob", "bob-password-123")
                deadline = time.time() + 5
                while not (await bob.get("/api/tools?gateway=main")).json():
                    assert time.time() < deadline
                    await asyncio.sleep(0.1)

                ok = dict(gateway="main", server="shop", outcome="ok")
                before, seen = sample("gateway_tool_calls_total", **ok), sample(
                    "gateway_tool_call_seconds_count", gateway="main", server="shop")
                r = await bob.post("/api/tools/call", json={"gateway": "main", "name": "shop__orders", "arguments": {}})
                assert r.status_code == 200
                assert sample("gateway_tool_calls_total", **ok) == before + 1
                assert sample("gateway_tool_call_seconds_count", gateway="main", server="shop") == seen + 1

                unknown = dict(gateway="main", server="_unknown", outcome="denied")
                before = sample("gateway_tool_calls_total", **unknown)
                await bob.post("/api/tools/call", json={"gateway": "main", "name": "nosuch__thing", "arguments": {}})
                assert sample("gateway_tool_calls_total", **unknown) == before + 1
                # The requested name never becomes a label: a client can't create series at will. And
                # "_unknown" can't be a real server's name (a-z 0-9 -), so the two never merge.
                assert sample("gateway_tool_calls_total", gateway="main", server="nosuch", outcome="denied") == 0

    asyncio.run(run())


def test_serve_exposes_text_and_a_second_bind_is_refused():
    port = _free_port()
    assert metrics.serve(port) is True
    assert "gateway_tool_calls" in httpx2.get(f"http://127.0.0.1:{port}/metrics").text
    assert metrics.serve(port) is False  # another worker already serves it


def test_multiprocess_counts_add_up_across_processes(tmp_path):
    # An edge runs two uvicorn workers; a scrape must report both, whichever one answers.
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "m")}
    here = os.path.dirname(os.path.dirname(__file__))
    record = "from app import metrics; metrics.record_call('main', 'shop', 'ok', 0.2)"
    for _ in range(2):
        subprocess.run([sys.executable, "-c", record], env=env, cwd=here, check=True)
    out = subprocess.run([sys.executable, "-c",
                          "from prometheus_client import generate_latest; from app import metrics;"
                          " print(generate_latest(metrics.registry()).decode())"],
                         env=env, cwd=here, check=True, capture_output=True, text=True).stdout
    assert 'gateway_tool_calls_total{gateway="main",outcome="ok",server="shop"} 2.0' in out


def test_a_question_counts_as_asked_but_sets_no_latency():
    # An elicitation is answered by a second call; timing the question would drag p95 down.
    calls = dict(gateway="q", server="shop", outcome="asked")
    before, seen = sample("gateway_tool_calls_total", **calls), sample("gateway_tool_call_seconds_count", gateway="q", server="shop")
    metrics.record_call("q", "shop", "asked", 0.001)
    assert sample("gateway_tool_calls_total", **calls) == before + 1
    assert sample("gateway_tool_call_seconds_count", gateway="q", server="shop") == seen
