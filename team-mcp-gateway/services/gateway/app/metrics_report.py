"""GET /api/reports/metrics: the Reports tab's charts, read from Prometheus.

A fixed list of queries (nothing from the request reaches PromQL but the range), run by
the control plane, so the browser never talks to Prometheus. Per-user and per-tool
numbers stay in audit_log (GET /api/reports).
"""

from __future__ import annotations

import asyncio
import math
import time

import httpx2
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HTTPError, handler, require_admin

# range -> (seconds shown, step between points): about 60-180 points per line.
RANGES = {"1h": (3600, 60), "24h": (86400, 600), "7d": (604800, 3600), "30d": (2592000, 14400)}

_ERRORS = 'outcome=~"tool_error|timeout|unreachable"'
# A question (elicitation) isn't a call: the client sends the call again with its answer.
_CALLS = 'outcome!="asked"'
_STORE_SVC = 'service=~"store-.+-store-wordpress-.+"'  # Traefik's name for a store's Service
_BY_STORE_SVC = '"store", "$1", "service", "store-(.+)-store-wordpress-.+"'
_STORE_PODS = 'namespace=~"store-.+", container!="", container!="POD"'
_BY_STORE_NS = '"store", "$1", "namespace", "store-(.+)"'

# (id, group, title, unit, query with {w} for the rate window; PromQL's own braces stay as they are). Series are named by
# their `gateway` or `store` label.
CHARTS = [
    ("gw_calls", "Gateways", "Tool calls per minute", "/min",
     f"sum by (gateway) (rate(gateway_tool_calls_total{{{_CALLS}}}[{{w}}])) * 60"),
    # `or ... * 0`: an error counter exists only after a gateway's first error; a healthy one shows 0, not no line.
    ("gw_errors", "Gateways", "Error rate", "%",
     f"100 * (sum by (gateway) (rate(gateway_tool_calls_total{{{_ERRORS}}}[{{w}}]))"
     f" or sum by (gateway) (rate(gateway_tool_calls_total{{{_CALLS}}}[{{w}}])) * 0)"
     f" / sum by (gateway) (rate(gateway_tool_calls_total{{{_CALLS}}}[{{w}}]))"),
    ("gw_p95", "Gateways", "p95 latency", " ms",
     # Not built-ins: gateway__wait_for_approval long-polls ~25 s by design.
     '1000 * histogram_quantile(0.95, sum by (gateway, le) (rate(gateway_tool_call_seconds_bucket{server!="gateway"}[{w}])))'),
    ("st_requests", "Stores", "Storefront requests per minute", "/min",
     f"sum by (store) (label_replace(rate(traefik_service_requests_total{{{_STORE_SVC}}}[{{w}}]),"
     f" {_BY_STORE_SVC})) * 60"),
    ("st_5xx", "Stores", "Storefront 5xx per minute", "/min",
     f'(sum by (store) (label_replace(rate(traefik_service_requests_total{{{_STORE_SVC}, code=~"5.."}}[{{w}}]),'
     f" {_BY_STORE_SVC})) or sum by (store) (label_replace(rate(traefik_service_requests_total{{{_STORE_SVC}}}[{{w}}]),"
     f" {_BY_STORE_SVC})) * 0) * 60"),
    ("st_cpu", "Stores", "CPU", " m",
     f"sum by (store) (label_replace(rate(container_cpu_usage_seconds_total{{{_STORE_PODS}}}[{{w}}]),"
     f" {_BY_STORE_NS})) * 1000"),
    ("st_mem", "Stores", "Memory", " MiB",
     f"sum by (store) (label_replace(container_memory_working_set_bytes{{{_STORE_PODS}}},"
     f" {_BY_STORE_NS})) / 1048576"),
]


def _value(raw: str) -> float | None:
    v = float(raw)
    return round(v, 2) if math.isfinite(v) else None  # NaN: 0/0, e.g. an error rate with no calls


def _series(result: list) -> list[dict]:
    return [{"name": r["metric"].get("gateway") or r["metric"].get("store") or "",
             "points": [[int(t), _value(v)] for t, v in r["values"]]} for r in result]


@handler
async def report(request: Request) -> Response:
    require_admin(request)
    base = request.app.state.settings.prometheus_url
    if not base:
        raise HTTPError(503, "metrics are not configured (PROMETHEUS_URL)")
    name = request.query_params.get("range", "24h")
    if name not in RANGES:
        raise HTTPError(400, f"range must be one of {', '.join(RANGES)}")
    seconds, step = RANGES[name]
    end = int(time.time()) // step * step  # aligned, so every line shares the same timestamps
    window = f"{max(step, 120)}s"  # at least 4 scrapes (30 s apart) in every rate

    async def query(http, promql):
        r = await http.get(f"{base}/api/v1/query_range",
                           params={"query": promql.replace("{w}", window), "start": end - seconds, "end": end, "step": step})
        r.raise_for_status()
        return _series(r.json()["data"]["result"])

    try:
        async with httpx2.AsyncClient(timeout=10) as http:
            results = await asyncio.gather(*(query(http, c[4]) for c in CHARTS))
    except (httpx2.HTTPError, httpx2.InvalidURL, KeyError, TypeError, ValueError):  # a bad URL or answer too
        raise HTTPError(502, "metrics backend unreachable") from None
    charts = [{"id": i, "group": g, "title": t, "unit": u, "series": s}
              for (i, g, t, u, _), s in zip(CHARTS, results)]
    return JSONResponse({"range": name, "step": step, "charts": charts})


routes = [Route("/api/reports/metrics", report, methods=["GET"])]
