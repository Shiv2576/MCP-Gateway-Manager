"""Push for `subscriptions/listen`: tool-list changes reach every edge replica over Postgres LISTEN/NOTIFY.

The control plane (and an edge's health loop) sends NOTIFY gateway_events '<slug>' or '*'
after a change. Each edge process holds one LISTEN connection; on a notice for its own
gateway it refreshes its tool lists, then tells its open listen streams to re-fetch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import psycopg
from mcp.server.subscriptions import ServerEvent, ToolsListChanged

log = logging.getLogger("gateway")

CHANNEL = "gateway_events"


def notify(conn, slug: str) -> None:
    """Tell the edges of gateway `slug` ('*': every gateway) that their tools may have changed."""
    conn.execute("SELECT pg_notify(?, ?)", (CHANNEL, slug))


class PostgresBus:
    """A SubscriptionBus for one edge process, fed by LISTEN gateway_events."""

    def __init__(self, database_url: str, slug: str, on_change: Callable[[], Awaitable[None]]) -> None:
        self._url = database_url
        self._slug = slug
        self._on_change = on_change
        self._listeners: dict[object, Callable[[ServerEvent], None]] = {}

    async def publish(self, event: ServerEvent) -> None:
        for listener in list(self._listeners.values()):
            try:
                listener(event)
            except Exception:  # one bad listener must not starve the others
                log.exception("listen stream listener raised")

    def subscribe(self, listener: Callable[[ServerEvent], None]) -> Callable[[], None]:
        token = object()
        self._listeners[token] = listener
        # A new stream re-fetches at once, so a client re-listening after a drop misses nothing.
        # ListenHandler buffers it until its acknowledgement has gone out.
        listener(ToolsListChanged())
        return lambda: self._listeners.pop(token, None)

    async def _changed(self) -> None:
        try:
            await self._on_change()  # so the clients' re-fetch sees the new tools
        except Exception:
            log.exception("refresh before a tools list_changed failed")
        await self.publish(ToolsListChanged())

    async def _deliver(self, pending: asyncio.Event) -> None:
        # Notices that arrive during a refresh collapse into one more refresh, not one each.
        while True:
            await pending.wait()
            pending.clear()
            await self._changed()

    async def run(self) -> None:
        """LISTEN until cancelled, reconnecting with 1-30 s backoff."""
        delay = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    delay = 1.0
                    pending = asyncio.Event()
                    pending.set()  # notices sent while not listening are lost
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._deliver(pending))
                        async for n in conn.notifies():
                            if n.payload in (self._slug, "*"):
                                pending.set()
            except Exception as exc:  # also the TaskGroup's ExceptionGroup
                log.warning("LISTEN %s lost (%s); reconnecting in %gs", CHANNEL, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
