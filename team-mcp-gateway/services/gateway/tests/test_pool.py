"""UpstreamPool: connection reuse, per-credential clients, remembered negotiation, bounds."""

from __future__ import annotations

import asyncio

from starlette.middleware.base import BaseHTTPMiddleware

import json

from mcp import MCPError
from mcp_types import UNSUPPORTED_PROTOCOL_VERSION

from app import ssrf
from app.pool import UpstreamPool
from tests.test_gateway import build_notes, http_app, serve


class Counting(BaseHTTPMiddleware):
    discovers = 0

    async def dispatch(self, request, call_next):
        if request.method == "POST" and b"server/discover" in await request.body():
            type(self).discovers += 1
        return await call_next(request)


def notes_app():
    app = http_app(build_notes())
    app.add_middleware(Counting)
    return app


async def call(pool, url, headers=None, id=1):
    async with pool.session({"id": id, "url": url + "/mcp"}, headers or {}, 5) as c:
        return await c.list_tools()


def test_one_connection_for_two_calls(monkeypatch):
    opened = []
    real = ssrf.GuardedBackend.connect_tcp

    async def counted(self, *a, **kw):
        opened.append(a)
        return await real(self, *a, **kw)

    monkeypatch.setattr(ssrf.GuardedBackend, "connect_tcp", counted)

    async def run(url):
        pool = UpstreamPool()
        await call(pool, url)
        await call(pool, url)
        await pool.aclose()

    with serve(notes_app()) as url:
        asyncio.run(run(url))
    assert len(opened) == 1


def test_credentials_never_share_a_client():
    async def run(url):
        pool = UpstreamPool()
        await call(pool, url, {"X": "a"})
        await call(pool, url, {"X": "b"})
        n = len(pool._clients)
        await pool.aclose()
        return n

    with serve(notes_app()) as url:
        assert asyncio.run(run(url)) == 2


def test_modern_upstream_is_not_probed_twice():
    Counting.discovers = 0

    async def run(url):
        pool = UpstreamPool()
        await call(pool, url)
        await call(pool, url)
        await pool.aclose()

    with serve(notes_app()) as url:
        asyncio.run(run(url))
    assert Counting.discovers == 1


def test_changed_url_negotiates_again():
    Counting.discovers = 0

    async def run(a, b):
        pool = UpstreamPool()
        await call(pool, a)
        await call(pool, b)  # same id, new URL
        await pool.aclose()

    with serve(notes_app()) as a, serve(notes_app()) as b:
        asyncio.run(run(a, b))
    assert Counting.discovers == 2


def test_idle_clients_are_closed():
    async def run(a, b):
        pool = UpstreamPool(idle_s=0.1)
        await call(pool, a, id=1)
        first = next(iter(pool._clients.values()))[0]
        await asyncio.sleep(0.3)
        await call(pool, b, id=2)
        gone = len(pool._clients) == 1 and first.is_closed and (1, a + "/mcp") not in pool._negotiated
        await pool.aclose()
        return gone

    with serve(notes_app()) as a, serve(notes_app()) as b:
        assert asyncio.run(run(a, b))


def test_cap():
    async def run(urls):
        pool = UpstreamPool(max_clients=2)
        await call(pool, urls[0], id=1)
        first = next(iter(pool._clients.values()))[0]
        await call(pool, urls[1], id=2)
        await call(pool, urls[2], id=3)
        ok = len(pool._clients) == 2 and first.is_closed
        await pool.aclose()
        return ok

    with serve(notes_app()) as a, serve(notes_app()) as b, serve(notes_app()) as c:
        assert asyncio.run(run([a, b, c]))


def test_concurrent_sessions_with_stale_clients():
    async def run(url):
        pool = UpstreamPool(idle_s=0.1)
        await call(pool, url)
        await asyncio.sleep(0.3)  # the client is stale; six calls arrive at once
        await asyncio.gather(*(call(pool, url) for _ in range(6)))
        await pool.aclose()

    with serve(notes_app()) as url:
        asyncio.run(run(url))


def test_protocol_error_forgets_the_negotiation():
    Counting.discovers = 0

    async def run(url):
        pool = UpstreamPool()
        await call(pool, url)
        try:
            async with pool.session({"id": 1, "url": url + "/mcp"}, {}, 5):
                raise MCPError(UNSUPPORTED_PROTOCOL_VERSION, "gone")
        except BaseException as exc:
            while isinstance(exc, BaseExceptionGroup):
                exc = exc.exceptions[0]
            assert isinstance(exc, MCPError)
        assert not pool._negotiated
        await call(pool, url)
        await pool.aclose()

    with serve(notes_app()) as url:
        asyncio.run(run(url))
    assert Counting.discovers == 2


class RejectDiscover:
    """A legacy-only upstream: it does not know server/discover."""

    probes = 0

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        if b"server/discover" in body:
            type(self).probes += 1
            msg = json.loads(body)
            out = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": out})
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def test_legacy_upstream_is_not_probed_twice():
    RejectDiscover.probes = 0

    async def run(url):
        pool = UpstreamPool()
        await call(pool, url)
        assert pool._negotiated[(1, url + "/mcp")] is None
        await call(pool, url)
        await pool.aclose()

    with serve(RejectDiscover(http_app(build_notes()))) as url:
        asyncio.run(run(url))
    assert RejectDiscover.probes == 1
