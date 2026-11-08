"""Pooled upstream clients: one httpx2 client per (server, credential), plus a memory of how
each upstream negotiated. No MCP session outlives a call; only TCP/TLS connections and the
discover result are reused (design §5.5)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections import OrderedDict

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp_types import UNSUPPORTED_PROTOCOL_VERSION

from .ssrf import guarded_transport


class UpstreamPool:
    def __init__(self, max_clients: int = 256, idle_s: float = 300.0):
        self.max_clients, self.idle_s = max_clients, idle_s
        self._clients: OrderedDict[tuple, tuple[httpx2.AsyncClient, float]] = OrderedDict()
        # (id, url) -> (DiscoverResult, protocol version) for a modern upstream, or None for a legacy one
        self._busy: dict[tuple, int] = {}
        self._negotiated: dict[tuple, tuple | None] = {}

    async def _http(self, key: tuple, headers: dict) -> httpx2.AsyncClient:
        now = time.monotonic()
        # A client with a call in flight is never closed; the cap can be briefly exceeded instead.
        idle = [k for k in self._clients if not self._busy.get(k)]
        stale = [k for k in idle if now - self._clients[k][1] > self.idle_s]
        if key not in self._clients:
            live = [k for k in idle if k not in stale]
            stale += live[: max(0, len(live) - self.max_clients + 1)]
        # Pop before awaiting: a concurrent session() must not find the same key half-closed.
        closing = [self._clients.pop(k) for k in dict.fromkeys(stale) if k in self._clients]
        for k in stale:
            self._negotiated.pop(k[:2], None)  # an idle server renegotiates, so "legacy" cannot stick
        for http, _ in closing:
            await http.aclose()
        entry = self._clients.pop(key, None)
        # No httpx timeout: both callers bound the call with their own deadline (anyio.fail_after).
        http = entry[0] if entry else httpx2.AsyncClient(
            headers=headers, timeout=None, follow_redirects=False, transport=guarded_transport())
        self._clients[key] = (http, now)  # most recently used last
        return http

    @contextlib.asynccontextmanager
    async def session(self, row: dict, headers: dict, timeout: float):
        # The caller picks the credential. Headers from the gateway's own MCP clients are never forwarded.
        digest = hashlib.sha256(json.dumps(sorted(headers.items())).encode()).hexdigest()
        key = (row["id"], row["url"], digest)
        http = await self._http(key, headers)
        self._busy[key] = self._busy.get(key, 0) + 1  # no await since _http: nothing can evict it
        seen = self._negotiated.get(key[:2], "auto")
        if seen == "auto":
            kw = {"mode": "auto"}
        elif seen is None:
            kw = {"mode": "legacy"}
        else:
            kw = {"mode": seen[1], "prior_discover": seen[0]}
        transport = streamable_http_client(row["url"], http_client=http)
        try:
            async with Client(transport, **kw) as client:
                s = client.session
                if seen == "auto":
                    self._negotiated[key[:2]] = (s.discover_result, s.protocol_version) if s.initialize_result is None else None
                yield client
        except BaseException as exc:
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:  # the SDK wraps errors in groups
                exc = exc.exceptions[0]
            if isinstance(exc, MCPError) and exc.code == UNSUPPORTED_PROTOCOL_VERSION:
                self._negotiated.pop(key[:2], None)
            raise
        finally:
            self._busy[key] -= 1

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), OrderedDict()
        for http, _ in clients:
            await http.aclose()
