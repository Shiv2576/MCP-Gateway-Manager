"""Prometheus metrics: every tool call by gateway, server and outcome, with its duration.

Served on its own port (METRICS_PORT), never behind the Ingress. An edge runs several
uvicorn workers: with PROMETHEUS_MULTIPROC_DIR set (the chart sets it), each worker writes
its numbers to files there and whichever worker serves the port reports the sum.
Labels never carry raw request input: `server` is a known server's name, UNKNOWN or
"gateway" (built-ins); per-user and per-tool history lives in audit_log instead.
A confirmed write counts twice, "asked" then its outcome; the Reports queries leave "asked" out.
"""

from __future__ import annotations

import os

# Before prometheus_client is imported: it picks file-backed values when this is set.
if _dir := os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    os.makedirs(_dir, exist_ok=True)

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram, start_http_server  # noqa: E402
from prometheus_client import multiprocess  # noqa: E402

CALLS = Counter("gateway_tool_calls", "Tool calls by gateway, server and outcome.", ["gateway", "server", "outcome"])
DURATION = Histogram("gateway_tool_call_seconds", "Tool call duration.", ["gateway", "server"],
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))


# `server` for a call naming no known server: never a real name (those are a-z 0-9 -).
UNKNOWN = "_unknown"


def record_call(gateway: str, server: str, outcome: str, seconds: float) -> None:
    CALLS.labels(gateway, server, outcome).inc()
    if outcome != "asked":  # a question returns at once; its answer comes as the next call
        DURATION.labels(gateway, server).observe(seconds)


def registry() -> CollectorRegistry:
    """What a scrape reports: every worker's numbers when multiprocess, else this process's."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return REGISTRY
    reg = CollectorRegistry()
    multiprocess.MultiProcessCollector(reg)
    return reg


def serve(port: int) -> bool:
    """Serve /metrics on `port`; False when another worker of this pod already does."""
    try:
        start_http_server(port, registry=registry())
    except OSError:
        return False
    return True
