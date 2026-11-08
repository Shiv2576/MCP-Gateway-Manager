# Interactive MCP Without Sessions: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faster upstream calls plus write confirmation, over-budget reason, progress, cancellation and `list_changed` push on the stateless gateway edges, with no MCP sessions.

**Architecture:** The edges keep `stateless_http=True`. On protocol 2026-07-28 the gateway's `on_call_tool` may return `InputRequiredResult`. The SDK's `RequestStateBoundary` seals the state, so the retry can land on any replica. Progress and cancellation ride the call's own response stream (`json_response=False`). Push uses the SDK `ListenHandler` over a per-process bus fed by Postgres `LISTEN/NOTIFY`. Upstream calls reuse pooled HTTP clients and cached negotiation results instead of sessions.

**Tech Stack:** Python 3.12, Starlette, MCP Python SDK 2.x (low-level `Server`, `mcp_types`), httpx2, psycopg 3 (sync pool + one async `LISTEN` connection), pytest against Postgres on 5433, Helm.

**Spec:** `team-mcp-gateway/docs/superpowers/specs/2026-11-07-stateless-interactive-mcp-design.md`

## Global Constraints

- No MCP sessions, no Redis, no state that ties a request to a replica. Edges keep 2 replicas and `edgeWorkers: 2`.
- Never `git commit`: the user makes every commit. Each task ends with "leave for the user to commit".
- Clients that don't declare elicitation, don't send a progress token or don't send `subscriptions/listen` get exactly today's results (the existing tests pass unchanged, except the raw-JSON parsing noted in Task 2).
- Write confirmation is always on for capable clients (user decision 2026-11-08); there's no admin switch.
- "Write" = an upstream tool not marked read-only, using `Upstreams.read_only(server_id, tool)`. `gateway__*` built-ins are never confirmed.
- Audit error strings, exactly: `"confirmation asked"`, `"reason asked"`, `"declined by the user"`, `"cancelled by the client"`.
- Request state: `RequestStateSecurity(keys=[REQUEST_STATE_KEY], ttl=600.0, audience=<gateway slug>, bind_principal=<user id as str>)`.
- `NOTIFY` channel `gateway_events`; payload a gateway slug or `*`.
- Upstream client cache per process: at most 256 clients, idle clients closed after 300 s.
- Never retry a `tools/call` to an upstream.
- Run gateway tests with `TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q` from `services/gateway`.

## Review Focus

1. **A retry with changed arguments** (same sealed state, different `arguments`) must be refused, never run. Pinned in Task 5.
2. **A decline or an unanswered question must not charge the budget.** A round-1 audit row must have `items IS NULL`, so `_window` never counts it. Pinned in Task 5.
3. **A cancelled write must still count against the hourly budget.** Pinned in Task 4.
4. **Pooled clients must not mix credentials.** Two users with personal credentials on the same server get different clients. Pinned in Task 1.
5. **A Postgres restart must not kill push forever.** The bus reconnects and then sends one `list_changed`. Pinned in Task 7.

---

### Task 1: Pooled upstream clients and cached negotiation

**Files:**
- Create: `services/gateway/app/pool.py`
- Modify: `services/gateway/app/upstream.py`: `connect()` (around line 118), `refresh_one`, `call_tool`, `run_forever`
- Test: `services/gateway/tests/test_pool.py`

**Interfaces:**
- Produces: `class UpstreamPool` in `app/pool.py`:
  - `UpstreamPool(max_clients: int = 256, idle_s: float = 300.0)`
  - `pool.session(row: dict, headers: dict, timeout: float) -> AbstractAsyncContextManager[mcp.Client]`: an entered MCP `Client` over a pooled `httpx2.AsyncClient` built with `transport=guarded_transport()` and `follow_redirects=False`. The client is keyed by `(row["id"], row["url"], sha256 of sorted headers)`.
  - `async pool.aclose() -> None`
- Produces: `Upstreams.pool: UpstreamPool`. `Upstreams.run_forever`'s `finally` awaits `self.pool.aclose()`.
- `connect()` stays as a thin wrapper (`async with self.pool.session(...)`), so the call sites don't change shape.

- [ ] **Step 1: Write failing tests in `tests/test_pool.py`**
  - `test_one_connection_for_two_calls`: serve a demo notes upstream (`serve(http_app(build_notes(), ...))` from `tests.test_gateway`). Make two calls through one `UpstreamPool` with the same row and headers. Count connections with a wrapped `GuardedBackend.connect_tcp` (monkeypatch) and assert `== 1`.
  - `test_credentials_never_share_a_client`: same row, headers `{"X": "a"}` and `{"X": "b"}`. Assert two distinct underlying `httpx2.AsyncClient` objects (`len(pool._clients) == 2`).
  - `test_modern_upstream_is_not_probed_twice`: count `server/discover` POSTs at the upstream with a Starlette middleware around `http_app`. Two sessions mean exactly 1 discover.
  - `test_changed_url_negotiates_again`: same id, new URL means a new key and a new discover.
  - `test_idle_clients_are_closed`: `UpstreamPool(idle_s=0.1)`. After one call, sleep 0.3 s, make a call to a second server, and assert the first client is gone and `is_closed`.
  - `test_cap`: `max_clients=2` and three servers: the least recently used client is closed.

- [ ] **Step 2: Run them and confirm they fail.**
  Run: `... uv run pytest tests/test_pool.py -q`. Expected: ImportError (`app.pool`).

- [ ] **Step 3: Implement `UpstreamPool`**
  - Keep an `OrderedDict` of key → `(httpx2.AsyncClient, last_used)`, plus a dict `(row["id"], row["url"]) → DiscoverResult | "legacy"`.
  - **Negotiation:** a cached `DiscoverResult` gives `Client(..., mode=result.protocol_version or the SDK's modern mode, prior_discover=result)`; `"legacy"` gives `mode="legacy"`; no cache gives `mode="auto"`. After entering, store `client.session.discover_result` or, if `initialize_result` is set, `"legacy"`.
  - **Protocol errors:** on an `mcp.MCPError` with a protocol-version code, drop the cache entry so the next call negotiates afresh.
  - **Housekeeping:** evict idle clients on each `session()` call. No background task.

- [ ] **Step 4: Wire it in.**
  - `Upstreams.__init__` creates `self.pool`. `connect(row, timeout, headers)` becomes a method on `Upstreams`.
  - Update both callers in `upstream.py` (`refresh_one`, `call_tool`) and the test in `tests/test_ssrf.py::test_rebinding_is_refused_on_connect`, which imports `connect`. Give it a module-level `connect` that builds a throwaway `UpstreamPool`.

- [ ] **Step 5: Run the whole suite.**
  Run: `... uv run pytest -q`. Expected: all pass (122 + 6 new).

- [ ] **Step 6: Measure.** Time 10 sequential `tools/call`s to a notes upstream before (`git stash`) and after. Write both medians in the spec §8 under "Latency".

- [ ] **Step 7: Leave for the user to commit.**

### Task 2: Progress (stream responses, pass upstream progress through, woo-mcp reports it)

**Files:**
- Modify: `services/gateway/app/main.py`: `streamable_http_app(...)` (`json_response=True` → `False`) and `on_call_tool`
- Modify: `services/gateway/app/upstream.py`: `call_tool` gains keyword `progress: Callable[[float, float | None, str | None], Awaitable[None]] | None = None`, passed as `progress_callback` to the upstream `Client.call_tool`
- Modify: `services/woo-mcp/app/server.py`: `all_pages` gains `on_page: Callable[[int], Awaitable[None]] | None = None`; `get_inventory_health` takes the SDK `Context` and reports progress
- Modify: `services/gateway/tests/test_edge.py:65-79`: parse raw responses that may now be `text/event-stream`
- Test: `services/gateway/tests/test_interactive.py` (new; later tasks add to it), `services/woo-mcp/tests/` (existing test for `get_inventory_health`)

**Interfaces:**
- Produces: `Upstreams.call_tool(..., progress=...)`.
- Produces: in `tests/test_interactive.py`, a helper `build_steps() -> MCPServer` (an upstream with a read-only tool `steps(n: int)` that calls `ctx.report_progress(i, n, f"step {i}")` n times, and a write tool `slow_write()` that sleeps 5 s), and `modern_client(url, key, **client_kwargs)`, a `Client(..., mode="auto")` context manager like `gateway_client`. Later tasks reuse both.

- [ ] **Step 1: Write the failing test** `test_progress_reaches_the_client`: an edge with the steps upstream attached. `modern_client` calls `steps` with `progress_callback` collecting `(progress, total, message)`. Assert it equals `[(1, 3, "step 1"), (2, 3, "step 2"), (3, 3, "step 3")]` and that the result is not an error.
- [ ] **Step 2: Run it; it fails** (empty list).
- [ ] **Step 3: Implement.**
  - Set `json_response=False`, with a one-line comment: progress, keepalives and cancellation need the call's response stream.
  - `on_call_tool` passes `progress=ctx.session.report_progress`, which does nothing when no token was sent.
  - `call_tool` forwards it to the upstream.
- [ ] **Step 4: Fix `test_edge.py`.** The raw `initialize` and `tools/call` responses there are now SSE on the legacy path. Add a small `rpc_json(response) -> dict` helper that returns `response.json()` for JSON, or the `data:` line of the first `message` event for `text/event-stream`, and use it at lines 69–75.
- [ ] **Step 5: woo-mcp.**
  - `all_pages` awaits `on_page(page)` after each page.
  - `get_inventory_health(..., ctx: Context)` passes `on_page=lambda p: ctx.report_progress(p, None, f"fetched page {p}")` into the fetches that `inventory()` makes. Thread `on_page` through `inventory()`.
  - Add a woo-mcp test that a fake store with 3 pages produces 3 progress updates.
- [ ] **Step 6: Run both suites; everything passes.**
- [ ] **Step 7: Leave for the user to commit.**

### Task 3: `REQUEST_STATE_KEY` and the request-state boundary

**Files:**
- Modify: `services/gateway/app/main.py`: `Settings.request_state_key: str` (env `REQUEST_STATE_KEY`, `repr=False`). `create_app` raises `ConfigError` in edge mode when it is empty. `_edge_app` appends the `RequestStateBoundary` to `server.middleware`.
- Modify: `charts/gateway/templates/deployment.yaml` (edge only: env `REQUEST_STATE_KEY` from secret `{{ .Values.credentialsSecret }}`, key `REQUEST_STATE_KEY`) and `charts/gateway/values.yaml` (comment under `credentialsSecret`)
- Modify: `services/gateway/tests/test_gateway.py`: `edge_settings` passes `request_state_key=STATE_KEY` (new constant in `tests/__init__.py` next to `KEY`, 32 random bytes in base64)
- Modify: install docs where `gateway-credentials` is created (`docs/deploy-vps.md` and the local install section of `docs/reference.md`): add the key, generated with `openssl rand -base64 32`
- Test: `tests/test_edge.py`

**Interfaces:**
- Produces: an edge whose low-level `Server` has `RequestStateBoundary(RequestStateSecurity(keys=[settings.request_state_key], ttl=600.0, audience=settings.gateway_slug, bind_principal=lambda ctx: str(ctx.request.state.user["id"])), default_audience=settings.gateway_slug)` in `server.middleware`.

- [ ] **Step 1: Failing test** `test_edge_needs_a_request_state_key`: `create_app(edge_settings(db_url, "prod", request_state_key=""))` raises `ConfigError`.
- [ ] **Step 2: Run it; it fails.**
- [ ] **Step 3: Implement the setting, the check, the middleware and the chart.** `helm lint --strict charts/gateway` passes, and `helm template` with `mode=edge` shows `REQUEST_STATE_KEY` (and the control mode doesn't).
- [ ] **Step 4: Full suite passes.**
- [ ] **Step 5: Leave for the user to commit.** Tell the user the live `gateway-credentials` Secret needs `REQUEST_STATE_KEY` added before the new edge image rolls out, and that the operator image must be rebuilt, because it bundles the chart.

### Task 4: Cancellation

**Files:**
- Modify: `services/gateway/app/upstream.py`: `call_tool`; new constant `CANCELLED = "cancelled by the client"`
- Modify: `services/gateway/app/approvals.py:_window`: count rows whose error is `upstream.CANCELLED` like `NO_RESULT`
- Test: `tests/test_interactive.py`

- [ ] **Step 1: Failing tests.**
  - `test_closing_the_stream_cancels_the_call`: `modern_client` starts `slow_write` in a task and cancels the task after 0.5 s. Poll `/api/audit` until the row's `error == "cancelled by the client"` (timeout 5 s). Also assert the steps upstream saw the request end: an `asyncio.Event` set in `slow_write`'s `finally`.
  - `test_cancelled_write_still_counts_against_the_budget`: give a member a budget of 1 item an hour on `slow_write`. Cancel one call, then make a second call. The second is over budget.
- [ ] **Step 2: Run them; they fail** (the error stays `NO_RESULT`; the second call fits).
- [ ] **Step 3: Implement.** Around the upstream call, catch `anyio.get_cancelled_exc_class()`. Inside `with anyio.CancelScope(shield=True)`, run in a thread `UPDATE audit_log SET error = ?, duration_ms = ? WHERE id = ?` with `CANCELLED`, then re-raise. Update `_window`'s condition to `error IN (?, ?)` with `NO_RESULT, CANCELLED`.
- [ ] **Step 4: Full suite passes.**
- [ ] **Step 5: Leave for the user to commit.**

### Task 5: Elicitation: write confirmation

**Files:**
- Create: `services/gateway/app/interaction.py`
- Modify: `services/gateway/app/upstream.py`: `call_tool` gains keyword `ask: Ask | None = None` and may return `InputRequiredResult`
- Modify: `services/gateway/app/approvals.py`: new `def fits(ups, user, gateway_slug, row, tool, name, arguments) -> str | None`, the same check as `charge()` without recording `items`
- Modify: `services/gateway/app/main.py`: `on_call_tool` builds `Ask` and returns whatever `call_tool` returns
- Test: `tests/test_interactive.py`

**Interfaces:**
- Produces, in `app/interaction.py`:
  ```python
  @dataclass(frozen=True)
  class Ask:
      can_elicit: bool              # client declared form elicitation
      state: dict | None            # unsealed requestState, json-decoded; None on round 1
      answers: dict[str, ElicitResult]  # params.input_responses or {}

  def from_request(ctx, params) -> Ask
  def call_hash(name: str, arguments: dict | None) -> str   # sha256 of name + canonical JSON
  def confirm_request(name: str, title: str | None, arguments: dict) -> InputRequiredResult  # input key "confirm", state {"ask": "confirm", "call": call_hash(...)}
  def reason_request(name: str, title: str | None, arguments: dict, why: str) -> InputRequiredResult  # input key "reason", state {"ask": "reason", ...}; schema one string field "reason", maxLength approvals.SUMMARY_MAX
  ```
  `confirm_request` states the tool's title (or name) and its arguments as `key: value` lines, and uses a schema with one required boolean field `confirm`.
- Consumes: `Upstreams.read_only`, `approvals.fits`, `approvals.charge`.

- [ ] **Step 1: Failing tests** (each uses `modern_client(..., elicitation_callback=cb)`; `cb` returns `ElicitResult(action=..., content=...)` and counts calls):
  - `test_accept_runs_the_write_once`: callback accepts. The result is not an error, the callback ran once, the upstream ran once, and the audit holds two rows (`"confirmation asked"`, then ok=1). Exactly one row has `items` set.
  - `test_decline_runs_nothing_and_charges_nothing`: callback declines. The result text is `"Cancelled by the user. Nothing was changed."`, the upstream never ran, and the audit rows are `"confirmation asked"` and `"declined by the user"`, both with `items IS NULL`.
  - `test_read_only_tools_are_not_confirmed`: `steps` runs without the callback being called.
  - `test_retry_on_another_replica`: two edge replicas. Do round 1 on A by hand with `client.session.call_tool(..., allow_input_required=True)`, then the retry on B with `input_responses` and `request_state`. It runs.
  - `test_tampered_retry_is_refused`: the retry with different `arguments`, the retry by another user's key, the retry to gateway `dev`, and the retry after monkeypatching time past 600 s are each refused (an error result or `-32602`). The upstream never runs.
  - `test_clients_without_elicitation_are_unchanged`: `gateway_client` (no callback, so no capability) runs the write directly, with one audit row.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.** In `prepare()`, after the policy checks and before `charge()`, when `ask and ask.can_elicit` and the tool is a write and the call isn't an approved run:
  - **Round 1** (`ask.state is None`): run `approvals.fits(...)`. If it fits, set the audit error to `"confirmation asked"` and return `confirm_request(...)`. If it doesn't, Task 6 handles it; until then, today's path.
  - **Retry** (`ask.state["ask"] == "confirm"`): refuse unless `ask.state["call"] == call_hash(name, arguments)`.
    - **Declined** (action other than `accept`, or `confirm` not true): set the audit error to `"declined by the user"` and return the decline text.
    - **Accepted:** go on to `charge()` as today.
  The returned `InputRequiredResult` skips the upstream, PII and alert steps. `finish()` only writes the audit error.
- [ ] **Step 4: Full suite passes.**
- [ ] **Step 5: Leave for the user to commit.**

### Task 6: Elicitation: over-budget reason

**Files:**
- Modify: `services/gateway/app/upstream.py`: the over-budget branch of `prepare()`
- Modify: `services/gateway/app/approvals.py`: none expected (`request()` already takes `summary`)
- Test: `tests/test_interactive.py`

**Interfaces:**
- Consumes: `interaction.reason_request`, `approvals.fits`, `approvals.request(ups, user, slug, {"tool", "arguments", "summary"}, client)`.

- [ ] **Step 1: Failing tests:**
  - `test_over_budget_asks_for_a_reason_and_files_it`: a budget of 0, and a callback answering `{"reason": "restock for the sale"}`. The approval in `/api/approvals` has `summary == "restock for the sale"`, and the result text tells the client to call `gateway__wait_for_approval`.
  - `test_over_budget_decline_files_nothing`: no approval row exists; the result is the over-budget message.
  - `test_open_request_is_not_asked`: with an approval already pending for that tool, the callback is never called and the result names the open request.
  - `test_budget_used_up_between_rounds_asks_for_a_reason`: round 1 fits and asks for confirmation. Use the budget up with another call, then accept. The next round is a reason request, not a run.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.**
  - **Over budget** (from `fits()` in round 1, or from `charge()` on an accepted confirmation), when `ask.can_elicit`: if `approvals.request` would refuse at once (an open request, or a lock), return that answer straight away. Otherwise set the audit error to `"reason asked"` and return `reason_request(...)`.
  - **Retry with `ask.state["ask"] == "reason"`:** same hash check. If accepted, call `approvals.request(...)` with `summary` set to the reason and return its text. If declined, return the over-budget message.
  - To know in advance whether `request()` would refuse without filing, add `approvals.would_refuse(ups, user, slug, name) -> CallToolResult | None`, the open-request and lock checks factored out of `request()`.
- [ ] **Step 4: Full suite passes.**
- [ ] **Step 5: Leave for the user to commit.**

### Task 7: Push: `subscriptions/listen` over Postgres `LISTEN/NOTIFY`

**Files:**
- Create: `services/gateway/app/events.py`
- Modify: `services/gateway/app/main.py`: `_edge_app` passes `on_subscriptions_listen=ListenHandler(bus)`; the lifespan starts and stops the bus
- Modify: control-plane writers, each calling `events.notify(conn, slug_or_star)` after its change:
  - `app/gateways.py`: `set_gateway_servers`, `set_pii`, `delete_gateway` (slug)
  - `app/teams.py`: `create_team`, `delete_team`, `set_members`, `set_tools`, `set_denies` (the team's gateway slug)
  - `app/api.py`: server create, update and delete, and enabled toggle (`*`)
- Modify: `services/gateway/app/upstream.py`: `refresh_one` notifies its own slug when an edge's tool names for a server change from a previous good list
- Test: `tests/test_interactive.py`

**Interfaces:**
- Produces, in `app/events.py`:
  ```python
  CHANNEL = "gateway_events"
  def notify(conn, slug: str) -> None          # conn.execute("SELECT pg_notify(?, ?)", (CHANNEL, slug))
  class PostgresBus:                            # implements mcp.server.subscriptions.SubscriptionBus
      def __init__(self, database_url: str, slug: str, on_change: Callable[[], Awaitable[None]]) -> None
      async def publish(self, event) -> None
      def subscribe(self, listener) -> Callable[[], None]
      async def run(self) -> None               # LISTEN loop; reconnect with 1-30 s backoff
  ```
  `on_change` is `upstreams.refresh_all`, awaited before delivering `ToolsListChanged()`. The bus delivers one `ToolsListChanged()` after every (re)connect, and the listen handler's first frame after the acknowledgement is one too.

- [ ] **Step 1: Failing tests:**
  - `test_attach_pushes_to_a_listener_on_another_replica`: two edge replicas A and B; `modern_client` on B with `client.listen(tools_list_changed=True)`. The admin attaches a server on the control plane. Within 5 s the subscription yields an event, and `list_tools()` then includes the new server's tools.
  - `test_new_listener_gets_one_event_at_once`.
  - `test_bus_reconnects`: terminate the bus's backend with `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE 'LISTEN%'`. A later attach still reaches the listener.
  - `test_other_gateways_are_not_told`: a listener on gateway `dev` gets no event when `prod` changes, apart from the one at connect.
- [ ] **Step 2: Run them; they fail** (`ListenNotSupported` or a timeout).
- [ ] **Step 3: Implement `events.py`, the notify calls and the wiring.** Use `psycopg.AsyncConnection.connect(database_url, autocommit=True)`, then `LISTEN gateway_events`, then `async for n in conn.notifies()`. Act on `n.payload in (slug, "*")`.
- [ ] **Step 4: Full suite passes.**
- [ ] **Step 5: Leave for the user to commit.**

### Task 8: Docs and the manual test checklist

**Files:**
- Modify: `README.md` (project root): in the Stateless edges and chat row of the trade-offs table (line ~236), replace "No server push" with what's now supported and the fallback
- Modify: `team-mcp-gateway/docs/design.md` and `docs/reference.md`, where they say no server push or no sessions
- Modify: the spec §8: latency numbers from Task 1

- [ ] **Step 1:** Make the edits above. No links in the root README, per the user's README rule.
- [ ] **Step 2:** Run the full gateway and woo-mcp suites one last time, plus `helm lint --strict charts/gateway`.
- [ ] **Step 3:** Hand the user the manual checklist from spec §8 (steps 1–8) and the rollout notes: the Secret key, the operator image rebuild, and the manual `helm upgrade` of existing `gw-*` edges.
- [ ] **Step 4: Leave for the user to commit.**
