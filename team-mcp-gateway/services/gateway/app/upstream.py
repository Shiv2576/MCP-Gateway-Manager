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
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
from mcp.types import CallToolResult, InputRequiredResult, TextContent, Tool

from . import events
from .db import allowed_tools, connection_identity
from .pii import FILTER_TIMEOUT, load_rules, redact_result
from .pool import UpstreamPool
from .ssrf import Blocked

if TYPE_CHECKING:
    from .interaction import Ask

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
    ok_at: float | None = None  # last successful check
    failures: int = 0  # failed checks in a row


def error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


WITHHELD = {
    "FILTER_ERROR": "Result withheld: the PII filter failed. An admin can check the gateway log.",
    "FILTER_TIMEOUT": "Result withheld: the PII filter took too long. An admin can check the PII rules.",
}
CANCELLED = "cancelled by the client"
NO_RESULT = "no result recorded: still running, or the gateway stopped mid-call"
PAYLOAD_CAP = 65_536  # characters of a result kept in the audit log
STALE_TOOLS_S = 300  # a down server keeps listing its last good tools this long, then drops them


def _scrub(result: CallToolResult, headers: dict) -> CallToolResult:
    """The result with the credentials this call sent replaced by [CREDENTIAL], so an
    upstream that echoes its key cannot put it in the log. Done before the PII filter,
    which could otherwise mask part of a key (its digits) and leave the rest; the bare
    token too, for "Bearer <token>". Longest first so the whole value wins."""
    secrets = {p for v in headers.values() for p in (v, v.split()[-1] if v.split() else v) if len(p) >= 8}
    if not secrets:
        return result
    s = result.model_dump_json(by_alias=True)
    for secret in sorted(secrets, key=len, reverse=True):
        s = s.replace(json.dumps(secret)[1:-1], "[CREDENTIAL]")  # as it appears inside JSON strings
    return CallToolResult.model_validate_json(s)


def _payload(result: CallToolResult | None) -> str | None:
    if result is None:
        return None
    s = result.model_dump_json(by_alias=True, exclude_none=True)
    return s if len(s) <= PAYLOAD_CAP else s[:PAYLOAD_CAP] + f"…[truncated, {len(s)} bytes]"


def _root_cause(exc: BaseException) -> BaseException:
    """The SDK runs transports in task groups, which wrap errors in groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe_failure(name: str, exc: BaseException, timeout: float) -> str:
    exc = _root_cause(exc)
    if isinstance(exc, Blocked):
        return f"Upstream '{name}': {exc}."
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return f"Upstream '{name}' did not answer within {timeout:g}s."
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"Upstream '{name}' refused the request: HTTP {exc.response.status_code}."
    if isinstance(exc, httpx2.TransportError):
        return f"Upstream '{name}' is unreachable ({type(exc).__name__})."
    return f"Upstream '{name}' failed: {type(exc).__name__}: {exc}"


UNREADABLE_SHARED = "the stored credential cannot be read (CREDENTIALS_KEY changed?); an admin must enter it again"


def shared_headers(row: dict, vault) -> dict | None:
    """The server's own credential: the health loop, and shared connections.
    None when it is stored but cannot be decrypted."""
    if not row["auth_header"]:
        return {}
    value = vault.open(row["auth_value"]) if vault and row["auth_value"] else None
    return None if value is None else {row["auth_header"]: value}


@contextlib.asynccontextmanager
async def connect(row: dict, timeout: float, headers: dict):
    """One-off connection over a throwaway pool (tests); Upstreams.connect uses the shared one."""
    pool = UpstreamPool()
    try:
        async with pool.session(row, headers, timeout) as client:
            yield client
    finally:
        await pool.aclose()


class Upstreams:
    def __init__(
        self,
        db,
        *,
        call_timeout: float,
        list_timeout: float,
        refresh_interval: float,
        calls_per_minute: int = 20,
        default_items_per_hour: int = 20,
        gateway_slug: str | None = None,
        vault=None,
        approval_timeout: float = 120,
    ):
        self.db = db
        self.call_timeout = call_timeout
        self.list_timeout = list_timeout
        self.refresh_interval = refresh_interval
        self.calls_per_minute = calls_per_minute
        self.default_items_per_hour = default_items_per_hour
        self.gateway_slug = gateway_slug
        self.vault = vault
        self.approval_timeout = approval_timeout
        self.pool = UpstreamPool()
        self.status: dict[int, Status] = {}
        self._tasks: set[asyncio.Task] = set()
        self._retrying: set[int] = set()  # servers with a recheck already scheduled

    def connect(self, row: dict, timeout: float, headers: dict):
        return self.pool.session(row, headers, timeout)

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

    async def refresh_one(self, row: dict, notify: bool = True) -> None:
        prev = self.status.get(row["id"])
        status = Status(checked_at=time.time())
        headers = shared_headers(row, self.vault)
        if headers is None:
            status.state, status.error = "unreachable", f"Server '{row['name']}': {UNREADABLE_SHARED}."
        else:
            try:
                with anyio.fail_after(self.list_timeout):
                    async with self.connect(row, self.list_timeout, headers) as client:
                        tools, cursor = [], None
                        while True:
                            page = await client.list_tools(cursor=cursor)
                            tools += page.tools
                            cursor = page.next_cursor
                            if not cursor:
                                break
                status.state, status.ok_at = "ok", status.checked_at
                status.tools = [t for t in tools if TOOL_NAME.match(row["name"] + SEP + t.name)]
                if len(status.tools) < len(tools):
                    status.error = f"{len(tools) - len(status.tools)} tool(s) hidden: name too long or invalid"
            except Exception as exc:  # any failure only marks this one server down
                status.state = "unreachable"
                status.error = describe_failure(row["name"], exc, self.list_timeout)
        if status.state != "ok":
            self._failed(row["id"], status, prev)
        self.status[row["id"]] = status
        # notify=False after a notice: a refresh caused by a notice must not send one, or a
        # flapping upstream would echo notices between processes without end.
        if (notify and self.gateway_slug and prev and prev.state == status.state == "ok"
                and {t.name for t in prev.tools} != {t.name for t in status.tools}):
            try:  # the server's tools changed between two good checks: tell this gateway's listeners
                await anyio.to_thread.run_sync(events.notify, self.db, self.gateway_slug)
            except Exception:
                log.exception("could not notify %s", self.gateway_slug)

    def _failed(self, server_id: int, status: Status, prev: Status | None) -> None:
        status.failures = (prev.failures if prev else 0) + 1
        status.ok_at = prev.ok_at if prev else None
        # Keep the last good list through a blip: clients keep the list they got at
        # connect, so a tool hidden now stays hidden for their whole session.
        if prev and status.ok_at and status.checked_at - status.ok_at < STALE_TOOLS_S:
            status.tools = prev.tools
        # Recheck sooner than the next tick: 0.5-1s, 1-2s, 4-8s ... with jitter so
        # every pod and worker does not retry at once. The tick takes over past it.
        delay = 2 ** (status.failures - 1) * random.uniform(0.5, 1.0)
        if delay < self.refresh_interval and server_id not in self._retrying:
            self._retrying.add(server_id)
            task = asyncio.create_task(self._retry(server_id, delay))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _retry(self, server_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._retrying.discard(server_id)
        if server_id not in self.status:  # deleted, detached or edited meanwhile
            return
        row = await anyio.to_thread.run_sync(self._enabled_row, server_id)
        if row:
            await self.refresh_one(row)

    def _enabled_row(self, server_id: int) -> dict | None:
        return self.db.execute("SELECT * FROM servers WHERE id = ? AND enabled = 1", (server_id,)).fetchone()

    def _servers_to_check(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM servers WHERE enabled = 1").fetchall()
        attached = self._attached()
        return [r for r in rows if attached is None or r["id"] in attached]

    async def refresh_all(self, notify: bool = True) -> None:
        rows = await anyio.to_thread.run_sync(self._servers_to_check)
        ids = {r["id"] for r in rows}
        for gone in self.status.keys() - ids:
            del self.status[gone]
        # Concurrently, each under its own timeout: one hung server costs
        # list_timeout once, not once per server.
        await asyncio.gather(*(self.refresh_one(r, notify) for r in rows))

    async def run_forever(self) -> None:
        try:
            while True:
                try:
                    await self.refresh_all()
                except Exception:
                    log.exception("refresh failed")
                await asyncio.sleep(self.refresh_interval)
        finally:  # shutdown: no retry may outlive the database connection
            for task in list(self._tasks):
                task.cancel()
            await self.pool.aclose()

    def read_only(self, server_id: int, tool: str) -> bool:
        """The upstream marks this tool read-only (MCP readOnlyHint). Unknown or not yet
        listed tools count as writes, so a budget never lapses while a server is down."""
        return any(t.name == tool and t.annotations is not None and t.annotations.read_only_hint
                   for t in self.status.get(server_id, Status()).tools)

    def _over_limit(self, user_id: int) -> bool:
        """calls_per_minute calls a minute per user across the whole organization: every pod,
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

    def _protected(self, gateway_slug: str) -> bool:
        row = self.db.execute("SELECT pii_protection FROM gateways WHERE slug = ?", (gateway_slug,)).fetchone()
        return bool(row and row["pii_protection"])

    async def _redact(self, result: CallToolResult, server_id: int | None, tool: str, name: str):
        """The filtered result and its counts, or None and why. Only this call fails."""
        # Outside the filter's deadline: a slow database is not a slow filter.
        rules = await anyio.to_thread.run_sync(load_rules, self.db)
        try:
            with anyio.fail_after(FILTER_TIMEOUT):
                # A worker thread, so the event loop keeps serving other calls. The
                # filter stops itself at the same deadline (regex timeout); this limit
                # is the backstop, and abandon_on_cancel lets it fire without waiting.
                return await anyio.to_thread.run_sync(
                    redact_result, result, rules, server_id, tool, abandon_on_cancel=True)
        except TimeoutError:
            log.error("PII filter timed out on %s", name)
            return None, {"FILTER_TIMEOUT": 1}
        except Exception:
            log.exception("PII filter failed on %s", name)
            return None, {"FILTER_ERROR": 1}

    def refresh_soon(self, server_id: int) -> None:
        """After an admin adds or edits a server, check it now instead of at the next tick."""
        self.status.pop(server_id, None)
        row = self._enabled_row(server_id)
        if row:
            task = asyncio.create_task(self.refresh_one(row))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------- MCP facing

    def list_tools(self, user, gateway_slug: str) -> list[Tool]:
        # Enabled flag, attachments and team policies read from the database on
        # every request, so any change applies at once.
        rows = self.db.execute("SELECT id, name, store FROM servers WHERE enabled = 1").fetchall()
        allowed = allowed_tools(self.db, user, gateway_slug)
        protected = self._protected(gateway_slug)
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
                            # Several stores expose the same tools; the prefix says which one this acts on.
                            "description": (f"[store {row['store']}] " if row["store"] else f"[{row['name']}] ")
                            + (tool.description or ""),
                            # MCP clients check structuredContent against it; masked values may not fit.
                            "output_schema": None if protected else tool.output_schema,
                        }
                    )
                )
        from . import alerts, approvals  # here, not at the top: approvals imports api, which imports this module

        # Every member gets them, budget or not: MCP clients keep the list they got at connect
        # time, so tying it to budgets left agents without the tool when an admin added one later.
        builtins = (approvals.TOOLS if user["role"] != "admin" else []) + alerts.TOOLS
        return out + (builtins if out else [])

    async def call_tool(
        self, user: dict, gateway_slug: str, name: str, arguments: dict[str, Any] | None, *,
        approval_id: int | None = None, timeout: float | None = None, client: str | None = None,
        progress: Callable[[float, float | None, str | None], Awaitable[None]] | None = None,
        ask: Ask | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """The one path for every tool call: route, check the gateway's policy and the
        caller's write budget, call with a timeout, filter PII, audit. An approved run
        (approval_id set) skips the rate limit and the budget; nothing else. With `ask`
        from a client that can answer, a write is confirmed with the user first."""
        from . import approvals

        server_name, sep, tool = name.partition(SEP)
        if server_name == approvals.BUILTIN:
            return await self._builtin(user, gateway_slug, name, tool, arguments or {}, client)
        timeout = timeout or self.call_timeout
        started = time.monotonic()

        # The database work before and after the upstream call runs in a worker thread
        # (psycopg here is synchronous), so a slow query never stalls the event loop
        # and with it every other request on this pod.
        def prepare():
            row = self.db.execute("SELECT * FROM servers WHERE name = ?", (server_name,)).fetchone()
            identity, headers, result, note = None, {}, None, None
            # Written before the upstream is called, so a pod that dies mid-call still
            # leaves a row; the UPDATE below replaces this with the outcome.
            audit_id = self.db.execute(
                "INSERT INTO audit_log (at, user_id, username, tool, server, gateway, ok, duration_ms, error, approval_id)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?) RETURNING id",
                (time.time(), user["id"], user["username"], name, row["name"] if row else None, gateway_slug, NO_RESULT,
                 approval_id),
            ).fetchone()["id"]
            allowed = allowed_tools(self.db, user, gateway_slug)
            names = allowed.get(row["id"], set()) if row else set()
            if approval_id is None and self._over_limit(user["id"]):
                result = error_result(f"Too many calls: at most {self.calls_per_minute} per minute. Try again shortly.")
            elif not sep or not tool or row is None or row["id"] not in allowed or (names is not None and tool not in names):
                # The same answer as for a tool that does not exist: hidden stays hidden.
                result = error_result(f"Unknown tool '{name}'. Tools are named <server>__<tool>.")
            elif not row["enabled"]:
                result = error_result(f"Server '{server_name}' is disabled.")
            # After the policy checks, before charging: asking charges nothing.
            elif approval_id is None and ask and (asked := self._confirm(ask, user, gateway_slug, row, tool, name,
                                                                          arguments or {}, client)):
                result, note = asked
            elif approval_id is None and (over := approvals.charge(
                    self, user, gateway_slug, row, tool, name, arguments or {}, audit_id)):
                if ask and (asked := self._reason(ask, user, gateway_slug, row, tool, name, arguments or {}, over)):
                    result, note = asked  # budget used up since the confirmation: ask for a reason
                else:
                    # Over budget: ask an admin right away. request() keeps one open request per member
                    # and tool (and locks the tool for an hour after a rejection), so retries cannot spam.
                    filed, _, code = approvals.request(self, user, gateway_slug,
                                                       {"tool": name, "arguments": arguments or {}}, client)
                    result = error_result(over if code == "bad input" else f"{over} {filed.content[0].text}")
            else:
                headers, identity, refusal = self._credential(user, gateway_slug, row)
                if refusal:
                    result = error_result(refusal)
            return row, audit_id, headers, identity, result, note

        row, audit_id, headers, identity, result, note = await anyio.to_thread.run_sync(prepare)
        if isinstance(result, InputRequiredResult):  # a question: no upstream, PII or alert step
            await anyio.to_thread.run_sync(lambda: self.db.execute(
                "UPDATE audit_log SET error = ?, duration_ms = ? WHERE id = ?",
                (note, int((time.monotonic() - started) * 1000), audit_id)))
            return result
        if result is None:
            try:
                with anyio.fail_after(timeout):
                    async with self.connect(row, timeout, headers) as upstream:
                        result = await upstream.call_tool(tool, arguments or {}, progress_callback=progress)
            except Exception as exc:
                result = error_result(describe_failure(server_name, exc, timeout))
            except anyio.get_cancelled_exc_class():
                # The client closed the call's stream. The upstream connection is already closed
                # by the unwinding; the audit row must not stay NO_RESULT, and it keeps counting
                # against the budget (approvals._window).
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda: self.db.execute(
                        "UPDATE audit_log SET error = ?, duration_ms = ? WHERE id = ?",
                        (CANCELLED, int((time.monotonic() - started) * 1000), audit_id)))
                raise

        result = _scrub(result, headers)  # for the caller too, whatever the PII setting
        filtered, pii = await self._redact(result, row and row["id"], tool, name)
        if await anyio.to_thread.run_sync(self._protected, gateway_slug):
            result = filtered or error_result(WITHHELD[next(iter(pii))])
            logged, masked_for_log = result, False
        else:  # the caller gets the unmasked result; the log still keeps only a masked copy
            logged, masked_for_log = filtered, True

        # The arguments go through the same filter, wrapped as a result; always masked in the log.
        masked_args, _ = await self._redact(
            CallToolResult(content=[TextContent(type="text", text=json.dumps(arguments or {}))]),
            row and row["id"], tool, name)
        error = note
        if result.is_error and note is None:
            error = (" ".join(c.text for c in logged.content if isinstance(c, TextContent))[:500]
                     if logged else "error text not kept: the PII filter failed")
        duration_ms = int((time.monotonic() - started) * 1000)

        def finish():
            self.db.execute(
                "UPDATE audit_log SET ok = ?, duration_ms = ?, error = ?, identity = ?,"
                " pii = ?, payload = ?, payload_masked_for_log = ?, args = ? WHERE id = ?",
                (
                    0 if result.is_error else 1,
                    duration_ms,
                    error,
                    identity,
                    json.dumps(pii) if pii else None,
                    _payload(logged),
                    masked_for_log,
                    masked_args.content[0].text[:PAYLOAD_CAP] if masked_args else None,
                    audit_id,
                ),
            )
            if not result.is_error and logged is not None and row is not None:
                from . import alerts  # here, like approvals: avoids an import cycle through api

                try:
                    alerts.fire(self.db, user=user, gateway_slug=gateway_slug, server_id=row["id"], tool=tool,
                                title=self._title(row["id"], tool) or tool,
                                arguments=arguments or {}, result=logged, approval_id=approval_id, audit_id=audit_id)
                except Exception:
                    log.exception("team alert for %s failed", name)  # the caller gets their result either way

        await anyio.to_thread.run_sync(finish)
        return result

    def _title(self, server_id: int, tool: str) -> str | None:
        listed = next((t for t in self.status.get(server_id, Status()).tools if t.name == tool), None)
        return listed and (listed.title or (listed.annotations and listed.annotations.title))

    def _confirm(self, ask: Ask, user, gateway_slug: str, row: dict, tool: str, name: str,
                 arguments: dict, client: str | None = None
                 ) -> tuple[CallToolResult | InputRequiredResult, str | None] | None:
        """Write confirmation, or a reason when over budget: (what to answer, its audit error),
        or None to go on and charge."""
        from . import approvals, interaction

        if not ask.can_elicit or self.read_only(row["id"], tool):
            return None
        if ask.state is None:
            over = approvals.fits(self, user, gateway_slug, row, tool, name, arguments)
            if over is None:
                return interaction.confirm_request(name, self._title(row["id"], tool), arguments), "confirmation asked"
            return self._reason(ask, user, gateway_slug, row, tool, name, arguments, over)
        kind = ask.state.get("ask")
        if kind not in ("confirm", "reason") or ask.state.get("call") != interaction.call_hash(name, arguments):
            return error_result("This answer was given for a different call. Call the tool again."), None
        if kind == "reason":
            answer = interaction.accepted(ask, "reason")
            if answer is None or not answer.content:
                over = approvals.fits(self, user, gateway_slug, row, tool, name, arguments)
                return error_result(over or interaction.DECLINED), "declined by the user"
            filed, _, _ = approvals.request(self, user, gateway_slug, {
                "tool": name, "arguments": arguments, "summary": answer.content.get("reason")}, client)
            return error_result(filed.content[0].text), None  # an error, like today's over-budget answer
        answer = interaction.accepted(ask, "confirm")
        if answer is None or not answer.content or answer.content.get("confirm") is not True:
            return error_result(interaction.DECLINED), "declined by the user"
        return None  # accepted: charged and run like any call

    def _reason(self, ask: Ask, user, gateway_slug: str, row: dict, tool: str, name: str, arguments: dict,
                over: str) -> tuple[CallToolResult | InputRequiredResult, str | None] | None:
        """Over budget, a client that can answer: ask why, unless filing would be refused anyway.
        None for today's path (no question)."""
        from . import approvals, interaction

        if not ask.can_elicit or isinstance(approvals.measure(tool, arguments), str):
            return None  # a call that cannot be counted cannot be filed either
        if refused := approvals.would_refuse(self, user, gateway_slug, name):
            return error_result(f"{over} {refused.content[0].text}"), None
        return interaction.reason_request(name, self._title(row["id"], tool), arguments, over), "reason asked"

    async def _builtin(self, user, gateway_slug: str, name: str, tool: str, arguments: dict, client):
        """gateway__request_approval and gateway__wait_for_approval. Waiting is not
        rate limited; neither audits a tool's payload."""
        from . import approvals

        started = time.monotonic()

        def start():  # in a worker thread, like call_tool's database work
            audit_id = self.db.execute(
                "INSERT INTO audit_log (at, user_id, username, tool, server, gateway, ok, duration_ms, error)"
                " VALUES (?, ?, ?, ?, NULL, ?, 0, 0, ?) RETURNING id",
                (time.time(), user["id"], user["username"], name, gateway_slug, NO_RESULT),
            ).fetchone()["id"]
            return audit_id, tool != "wait_for_approval" and self._over_limit(user["id"])

        audit_id, limited = await anyio.to_thread.run_sync(start)
        if limited:
            result, aid, error = error_result(
                f"Too many calls: at most {self.calls_per_minute} per minute. Try again shortly."), None, "rate limited"
        else:
            result, aid, error = await approvals.builtin(self, user, gateway_slug, tool, arguments, client)
        duration_ms = int((time.monotonic() - started) * 1000)
        await anyio.to_thread.run_sync(lambda: self.db.execute(
            "UPDATE audit_log SET ok = ?, duration_ms = ?, error = ?, approval_id = ? WHERE id = ?",
            (0 if result.is_error else 1, duration_ms,
             error or ("result not logged" if result.is_error else None), aid, audit_id),
        ))
        return result

    def _credential(self, user, gateway_slug: str, row: dict) -> tuple[dict, str | None, str | None]:
        """Headers for this call, the identity to audit, or why it cannot be made.
        A personal connection never falls back to the shared credential."""
        kind = connection_identity(self.db, gateway_slug, row["id"])
        if kind == "shared":
            headers = shared_headers(row, self.vault)
            if headers is None:
                return {}, None, f"Server '{row['name']}': {UNREADABLE_SHARED}."
            return headers, "shared", None
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
