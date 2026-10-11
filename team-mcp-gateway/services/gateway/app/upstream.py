"""Talking to upstream MCP servers, and the one function every tool call goes through.

Tool listing never touches the network. A background loop asks each enabled
server for its tools on a timer and keeps the answer in memory with a status,
so a slow or dead server can never slow down or break listing. Calls go to the
upstream directly, under a hard timeout, and a failure comes back as an
ordinary tool error with a plain sentence in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool

from .db import allowed_tools, connection_identity

log = logging.getLogger("gateway")

# Gateway tool names are "<server>__<tool>". Server names cannot contain "_",
# so the first "__" always ends the server name and the split is unambiguous.
SEP = "__"
# What MCP allows in a tool name.
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass
class Status:
    state: str = "pending"  # pending | ok | unreachable
    tools: list[Tool] = field(default_factory=list)
    error: str | None = None
    checked_at: float | None = None


def error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _root_cause(exc: BaseException) -> BaseException:
    """The SDK runs transports in task groups, which wrap errors in groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe_failure(name: str, exc: BaseException, timeout: float) -> str:
    exc = _root_cause(exc)
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return f"Upstream '{name}' did not answer within {timeout:g}s."
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"Upstream '{name}' refused the request: HTTP {exc.response.status_code}."
    if isinstance(exc, httpx2.TransportError):
        return f"Upstream '{name}' is unreachable ({type(exc).__name__})."
    return f"Upstream '{name}' failed: {type(exc).__name__}: {exc}"


def shared_headers(row: dict) -> dict:
    """The server's own credential: the health loop, and shared connections."""
    return {row["auth_header"]: row["auth_value"]} if row["auth_header"] else {}


@contextlib.asynccontextmanager
async def connect(row: dict, timeout: float, headers: dict):
    # The caller picks the credential. Headers from the gateway's own MCP
    # clients are never forwarded.
    async with httpx2.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=False
    ) as http:
        async with Client(
            streamable_http_client(row["url"], http_client=http), mode="auto"
        ) as client:
            yield client


class Upstreams:
    def __init__(
        self,
        db,
        *,
        call_timeout: float,
        list_timeout: float,
        refresh_interval: float,
        calls_per_minute: int = 60,
        gateway_slug: str | None = None,
        vault=None,
    ):
        self.db = db
        self.call_timeout = call_timeout
        self.list_timeout = list_timeout
        self.refresh_interval = refresh_interval
        self.calls_per_minute = calls_per_minute
        self.gateway_slug = gateway_slug
        self.vault = vault
        self.status: dict[int, Status] = {}
        self._tasks: set[asyncio.Task] = set()

    def _attached(self) -> set[int] | None:
        """Server ids attached to this edge's gateway, for its health loop;
        None on the control plane, which checks every server."""
        if self.gateway_slug is None:
            return None
        rows = self.db.execute(
            "SELECT gs.server_id FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id WHERE g.slug = ?",
            (self.gateway_slug,),
        )
        return {r["server_id"] for r in rows}

    # ------------------------------------------------------------ health loop

    async def refresh_one(self, row: dict) -> None:
        status = Status(checked_at=time.time())
        try:
            with anyio.fail_after(self.list_timeout):
                async with connect(row, self.list_timeout, shared_headers(row)) as client:
                    tools, cursor = [], None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools += page.tools
                        cursor = page.next_cursor
                        if not cursor:
                            break
            status.state = "ok"
            status.tools = [t for t in tools if TOOL_NAME.match(row["name"] + SEP + t.name)]
            if len(status.tools) < len(tools):
                status.error = f"{len(tools) - len(status.tools)} tool(s) hidden: name too long or invalid"
        except Exception as exc:  # any failure only marks this one server down
            status.state = "unreachable"
            status.error = describe_failure(row["name"], exc, self.list_timeout)
        self.status[row["id"]] = status

    async def refresh_all(self) -> None:
        rows = self.db.execute("SELECT * FROM servers WHERE enabled = 1").fetchall()
        attached = self._attached()
        rows = [r for r in rows if attached is None or r["id"] in attached]
        ids = {r["id"] for r in rows}
        for gone in self.status.keys() - ids:
            del self.status[gone]
        # Concurrently, each under its own timeout: one hung server costs
        # list_timeout once, not once per server.
        await asyncio.gather(*(self.refresh_one(r) for r in rows))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh_all()
            except Exception:
                log.exception("refresh failed")
            await asyncio.sleep(self.refresh_interval)

    def _over_limit(self, user_id: int) -> bool:
        """60 calls a minute per user across the whole organization: every pod,
        gateway and key counts against the same row."""
        # shortcut: fixed one-minute windows, so up to 2x the limit can land around a
        # minute boundary; switch to a sliding window if that matters.
        minute = int(time.time() // 60)
        calls = self.db.execute(
            "INSERT INTO rate_limits (user_id, minute, calls) VALUES (?, ?, 1)"
            " ON CONFLICT (user_id, minute) DO UPDATE SET calls = rate_limits.calls + 1 RETURNING calls",
            (user_id, minute),
        ).fetchone()["calls"]
        if calls == 1:  # this user's first call this minute: drop their old windows
            self.db.execute("DELETE FROM rate_limits WHERE user_id = ? AND minute < ?", (user_id, minute))
        return calls > self.calls_per_minute

    def refresh_soon(self, server_id: int) -> None:
        """After an admin adds or edits a server, check it now instead of at the next tick."""
        self.status.pop(server_id, None)
        row = self.db.execute(
            "SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)
        ).fetchone()
        if row:
            task = asyncio.create_task(self.refresh_one(row))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------- MCP facing

    def list_tools(self, user, gateway_slug: str) -> list[Tool]:
        # Enabled flag, attachments and team policies read from the database on
        # every request, so any change applies at once.
        rows = self.db.execute("SELECT id, name FROM servers WHERE enabled = 1").fetchall()
        allowed = allowed_tools(self.db, user, gateway_slug)
        out = []
        for row in rows:
            if row["id"] not in allowed:
                continue
            names = allowed[row["id"]]
            for tool in self.status.get(row["id"], Status()).tools:
                if names is not None and tool.name not in names:
                    continue
                out.append(
                    tool.model_copy(
                        update={
                            "name": row["name"] + SEP + tool.name,
                            "description": f"[{row['name']}] {tool.description or ''}".strip(),
                        }
                    )
                )
        return out

    async def call_tool(
        self, user: dict, gateway_slug: str, name: str, arguments: dict[str, Any] | None
    ) -> CallToolResult:
        """The one path for every tool call: route, check the gateway's policy,
        call with a timeout, audit."""
        server_name, sep, tool = name.partition(SEP)
        row = self.db.execute("SELECT * FROM servers WHERE name = ?", (server_name,)).fetchone()
        started = time.monotonic()
        identity = None
        allowed = allowed_tools(self.db, user, gateway_slug)
        names = allowed.get(row["id"], set()) if row else set()
        if self._over_limit(user["id"]):
            result = error_result(f"Too many calls: at most {self.calls_per_minute} per minute. Try again shortly.")
        elif not sep or not tool or row is None or row["id"] not in allowed or (names is not None and tool not in names):
            # The same answer as for a tool that does not exist: hidden stays hidden.
            result = error_result(f"Unknown tool '{name}'. Tools are named <server>__<tool>.")
        elif not row["enabled"]:
            result = error_result(f"Server '{server_name}' is disabled.")
        else:
            headers, identity, refusal = self._credential(user, gateway_slug, row)
            if refusal:
                result = error_result(refusal)
            else:
                try:
                    with anyio.fail_after(self.call_timeout):
                        async with connect(row, self.call_timeout, headers) as client:
                            result = await client.call_tool(tool, arguments or {})
                except Exception as exc:
                    result = error_result(describe_failure(server_name, exc, self.call_timeout))

        error = None
        if result.is_error:
            error = " ".join(c.text for c in result.content if isinstance(c, TextContent))[:500]
        self.db.execute(
            "INSERT INTO audit_log (at, user_id, username, tool, server, gateway, ok, duration_ms, error, identity)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                user["id"],
                user["username"],
                name,
                row["name"] if row else None,
                gateway_slug,
                0 if result.is_error else 1,
                int((time.monotonic() - started) * 1000),
                error,
                identity,
            ),
        )
        return result

    def _credential(self, user, gateway_slug: str, row: dict) -> tuple[dict, str | None, str | None]:
        """Headers for this call, the identity to audit, or why it cannot be made.
        A personal connection never falls back to the shared credential."""
        kind = connection_identity(self.db, gateway_slug, row["id"])
        if kind == "shared":
            return shared_headers(row), "shared", None
        stored = self.db.execute(
            "SELECT secret FROM user_credentials WHERE user_id = ? AND server_id = ?", (user["id"], row["id"])
        ).fetchone()
        # kind None: detached since the policy was read; refused like a missing credential.
        if kind is None or stored is None or not row["personal_header"]:
            return {}, None, (f"Server '{row['name']}' uses your personal credential on gateway '{gateway_slug}', "
                              "and you have not added one. Add it in the dashboard under My credentials.")
        value = self.vault.open(stored["secret"]) if self.vault else None
        if value is None:
            return {}, None, (f"Your saved credential for server '{row['name']}' cannot be read. "
                              "Enter it again in the dashboard under My credentials.")
        return {row["personal_header"]: value}, "personal", None
