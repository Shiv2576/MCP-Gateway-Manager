"""GET /api/reports/metrics: the Reports tab's charts, from a fixed set of Prometheus queries."""

from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests import KEY
from app.main import Settings, create_app
from tests.test_gateway import SETUP, FakeGateways, serve, setup_admin
from tests.test_stores import login

IDS = ["gw_calls", "gw_p95", "st_cpu", "st_mem"]


def fake_prometheus(seen: list, answer):
    async def query_range(request):
        seen.append(dict(request.query_params))
        return JSONResponse({"status": "success",
                             "data": {"resultType": "matrix", "result": answer(request.query_params["query"])}})
    return Starlette(routes=[Route("/api/v1/query_range", query_range)])


def per_gateway(query: str):
    if "gateway_" in query:
        return [{"metric": {"gateway": "main"}, "values": [[1000, "1.5"], [1600, "NaN"]]}]
    return []  # no stores yet


def run_with(db_url, prometheus_url, check):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, prometheus_url=prometheus_url)
        with serve(create_app(settings, gateways=FakeGateways())) as gw:
            await check(gw, await setup_admin(gw))
    asyncio.run(run())


def test_admin_only_and_needs_configuration(db_url):
    async def check(gw, admin):
        await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
        bob = await login(gw, "bob", "bob-password-123")
        assert (await bob.get("/api/reports/metrics")).status_code == 403
        r = await admin.get("/api/reports/metrics")
        assert r.status_code == 503 and "not configured" in r.json()["error"]
    run_with(db_url, "", check)


def test_returns_every_chart_in_order_from_aligned_queries(db_url):
    seen = []
    with serve(fake_prometheus(seen, per_gateway)) as prom:
        async def check(gw, admin):
            r = await admin.get("/api/reports/metrics")
            assert r.status_code == 200
            body = r.json()
            assert body["range"] == "24h" and body["step"] == 600
            assert [c["id"] for c in body["charts"]] == IDS
            assert {c["group"] for c in body["charts"]} == {"Gateways", "Stores"}
            calls = body["charts"][0]
            assert calls["series"] == [{"name": "main", "points": [[1000, 1.5], [1600, None]]}]  # NaN: no traffic
            assert body["charts"][2]["series"] == []  # no stores: an empty chart, not an error
            assert len(seen) == 4
            for q in seen:
                assert q["step"] == "600" and int(q["end"]) % 600 == 0
                assert int(q["end"]) - int(q["start"]) == 86400
            assert any("histogram_quantile(0.95" in q["query"] for q in seen)
        run_with(db_url, prom, check)


def test_range_default_and_validation(db_url):
    seen = []
    with serve(fake_prometheus(seen, per_gateway)) as prom:
        async def check(gw, admin):
            r = await admin.get("/api/reports/metrics?range=1h")
            assert r.status_code == 200 and r.json()["step"] == 60
            assert all(int(q["end"]) - int(q["start"]) == 3600 for q in seen)
            assert (await admin.get("/api/reports/metrics?range=2h")).status_code == 400
        run_with(db_url, prom, check)


def test_prometheus_down_is_502(db_url):
    async def check(gw, admin):
        r = await admin.get("/api/reports/metrics")
        assert r.status_code == 502 and "unreachable" in r.json()["error"]
    run_with(db_url, "http://127.0.0.1:9", check)  # nothing listens


def test_p95_skips_builtins():
    from app.metrics_report import CHARTS
    q = {c[0]: c[4] for c in CHARTS}
    # gateway__wait_for_approval long-polls ~25 s: built-ins must not set the gateway's p95.
    assert 'server!="gateway"' in q["gw_p95"]


async def is_502(gw, admin):
    r = await admin.get("/api/reports/metrics")
    assert r.status_code == 502 and "unreachable" in r.json()["error"]


def test_malformed_url_is_502_not_500(db_url):
    run_with(db_url, "http://[not-a-host", is_502)


def test_null_result_is_502_not_500(db_url):
    with serve(fake_prometheus([], lambda q: None)) as prom:  # "result": null
        run_with(db_url, prom, is_502)


def test_questions_are_not_counted_as_calls():
    from app.metrics_report import CHARTS
    q = {c[0]: c[4] for c in CHARTS}
    # An asked call is retried with the answer: counting both would double it.
    assert 'outcome!="asked"' in q["gw_calls"]
