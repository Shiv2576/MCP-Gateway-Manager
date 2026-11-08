"""The SSRF guard on admin-added upstream URLs. No database needed."""

import asyncio
import socket

import httpx2
import pytest

from app import ssrf
from app.api import HTTPError, check_upstream_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://[fd00:ec2::254]/",                     # AWS metadata over IPv6
    "http://127.0.0.1:8000/api",
    "http://localhost/mcp",
    "http://2130706433/",                          # 127.0.0.1 as a decimal
    "http://[::1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://10.43.0.1:443/",                       # Kubernetes API Service
    "http://192.168.1.10/",
    "http://100.64.0.1/",                          # carrier-grade NAT
    "http://0.0.0.0:5432/",
    "http://team-postgres.mercora.svc.cluster.local:5432/",
    "http://woo-mcp-woo-mcp.mercora.svc.cluster.local:8000/mcp",  # not an operator namespace
    "http://woo-mcp-woo-mcp.store-shop1.svc.cluster.local:8000/mcp",  # a real store, but not the operator
    "http://mcp.mcp-github.svc.cluster.local:8080/mcp",
    "ftp://example.com/",
    "http://:80/",
    "http://example.com:99999/",
])
def test_refused(url):
    with pytest.raises(HTTPError) as e:
        check_upstream_url(url)
    assert e.value.status == 400


@pytest.mark.parametrize("url,operator", [
    ("http://8.8.8.8/mcp", False),
    ("http://8.8.8.8/mcp", True),
    ("http://woo-mcp-woo-mcp.store-shop1.svc.cluster.local:8000/mcp", True),
    ("http://mcp.mcp-github.svc.cluster.local:8080/mcp", True),
])
def test_allowed(url, operator):
    assert check_upstream_url(url, operator) == url


@pytest.mark.parametrize("address,ok", [
    ("8.8.8.8", True), ("2606:4700::1111", True), ("64:ff9b::808:808", True),  # NAT64 of 8.8.8.8
    ("10.0.0.5", False), ("100.64.0.1", False), ("::ffff:127.0.0.1", False),
    ("64:ff9b::a00:5", False),  # NAT64 of 10.0.0.5
    ("fe80::1%eth0", False),
])
def test_public(address, ok):
    assert ssrf.public(address) is ok


def test_rebinding_is_refused_on_connect(monkeypatch):
    """A name that was public when registered and now resolves to a private address."""
    from app.upstream import connect, describe_failure

    async def rebound(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(ssrf.anyio, "getaddrinfo", rebound)
    row = {"id": 1, "url": "http://rebind.example/mcp"}

    async def run():
        try:
            async with connect(row, 2, {}) as client:
                await client.list_tools()
        except BaseException as exc:  # the SDK wraps it in exception groups
            return describe_failure("evil", exc, 2)
        raise AssertionError("connected")

    msg = asyncio.run(run())
    assert "rebind.example resolves to 10.0.0.5" in msg, msg


def test_connects_to_the_checked_address(monkeypatch):
    seen = []

    async def connect_tcp(self, host, port, **kw):
        seen.append(host)
        raise ssrf.ConnectError("stop here")

    monkeypatch.setattr(ssrf.AnyIOBackend, "connect_tcp", connect_tcp)

    async def public_name(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(ssrf.anyio, "getaddrinfo", public_name)

    async def run():
        async with httpx2.AsyncClient(transport=ssrf.guarded_transport()) as http:
            with pytest.raises(httpx2.ConnectError):
                await http.get("http://dns.example/")
            with pytest.raises(httpx2.ConnectError):
                await http.get("http://woo.store-shop1.svc.cluster.local:8000/")  # cluster DNS: as named

    asyncio.run(run())
    assert seen == ["8.8.8.8", "woo.store-shop1.svc.cluster.local"]


@pytest.mark.parametrize("url", [
    "http://notes.mercora.svc.cluster.local:8000/mcp",   # not an operator namespace
    "http://169.254.169.254/latest/meta-data/",
])
def test_operator_gets_no_other_internal_url(url):
    with pytest.raises(HTTPError):
        check_upstream_url(url, operator=True)
