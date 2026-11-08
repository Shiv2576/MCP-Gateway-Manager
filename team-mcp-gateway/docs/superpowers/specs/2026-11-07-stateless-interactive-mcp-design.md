# Interactive MCP without sessions: elicitation, progress, cancellation and push on stateless edges

Date: 2026-11-07. Status: design, awaiting approval.

## 1. Summary

The gateway edges stay fully stateless: no `Mcp-Session-Id`, no session in any process's
memory, no sticky routing. They gain the four interactive features people usually add
sessions for, by using what MCP protocol version **2026-07-28** defines for exactly this:

| Feature | What the user sees | How it works without a session |
|---|---|---|
| **Elicitation: write confirmation** | Before a write runs, Claude shows "Create product 'Blue Mug' at $12?" with Accept / Decline | The call returns an *input required* result; the client asks the user and repeats the call with the answer and a sealed `requestState` that any replica can verify |
| **Elicitation: over-budget reason** | When a write is over budget, Claude asks "Why do you need this?" and the gateway files the approval request with that reason | Same mechanism |
| **Progress** | A slow call (e.g. `get_inventory_health`) shows "page 3 of 8" while it runs | Progress travels on the call's own response stream |
| **Cancellation** | Stopping a call in Claude stops it in the gateway and at the upstream | On 2026-07-28, cancelling is closing the response stream |
| **Push (`list_changed`)** | When an admin attaches a server or edits a team's tools, Claude's tool list updates without reconnecting | The client opens a `subscriptions/listen` request whose response is the event stream; Postgres `LISTEN/NOTIFY` tells every replica |

Clients that don't use these features (older protocol versions, and probably claude.ai custom
connectors) keep today's behavior exactly.

## 2. The decision, and why not sessions

### 2.1 What "stateless" means here

Every request is processed on its own. Everything that matters (API keys, OAuth tokens,
teams, tool policies, budgets, approvals, audit) lives in Postgres and is checked on every
request. Any replica of a gateway, and any uvicorn worker in it, can serve any request.

This is the base the project already has. The edges already speak 2026-07-28: an MCP client
in `auto` mode connects to an edge with `server/discover`, not `initialize`, and gets protocol
`2026-07-28` (checked on 2026-11-07 against a local edge). The SDK serves it through
`StreamableHTTPSessionManager` (`mcp/server/_streamable_http_modern.py`: "A 2026-07-28 request
is a self-contained POST: no `initialize` handshake, no `Mcp-Session-Id`"). Older clients get
the older protocol, also stateless (`stateless_http=True`).

### 2.2 The three ways to add interactive features

| | A. Sessions + affinity routing | B. Sessions in a shared store (Redis) | **C. Stateless 2026-07-28 (chosen)** |
|---|---|---|---|
| Where state lives | In the memory of the replica that created the session | In Redis; replicas load it per request | **Nowhere on the server between requests.** Elicitation state is sealed and carried by the client; push uses a broadcast, not a session |
| Routing | Session ID names its owner; other replicas forward to it | Any replica | **Any replica** |
| Pod restart or rollout | Sessions on that pod are lost; clients get 404 and start again | Survives | **Nothing to lose** |
| Several uvicorn workers per pod | Breaks forwarding (one port, random worker); back to 1 worker | Works | **Works** |
| New infrastructure | Pod-to-pod NetworkPolicy, shared HMAC key, forwarding code | Redis, plus session expiry and cleanup | **None.** Postgres `LISTEN/NOTIFY` and one shared key in an existing Secret |
| Revoking a key | Must also kill its sessions | Must also kill its sessions | **Applies on the next request**, as today |
| Elicitation waiting for the user | Holds a request open on one replica | The answer must be routed to the waiting replica | **No waiting:** the call ends; the answer comes back as a new self-contained call |
| Cancellation | A `notifications/cancelled` message must reach the replica running the call | Same | **Closing the stream cancels it** on the replica that has it |
| Works with old-protocol clients | Yes | Yes | **No:** they get today's behavior (fallback, §5) |
| Code size | Medium, with routing risk | Large | **Small:** SDK hooks plus gateway logic |

**Why C.** The gateway's scaling unit is the gateway: each team gets its own edge deployment,
scaled by replicas and workers. That only works if no request depends on which process serves
it. Restarts and GitOps rollouts are free because no process holds anything that matters.
Sessions (A or B) would trade both away, or add Redis, for features the protocol now provides
without them. The one cost of C is that old-protocol clients don't get the new features; they
keep the stateless behavior the gateway already has, so nothing gets worse.

### 2.3 What sessions are actually for (so the comparison is fair)

Sessions don't make calls faster. They give the server a channel back to the client and tie
later requests to earlier ones: push notifications, resource subscriptions, elicitation,
sampling, roots, cancellation, resumable streams, per-session settings. On 2026-07-28 the
protocol provides the parts this gateway needs (elicitation, progress, cancellation, push)
without sessions. Sampling, roots and resource subscriptions aren't needed by any tool here.

## 3. What the clients support (researched 2026-11-07)

| Client | Elicitation | Progress | Cancellation | Push (`list_changed`) | What it gets |
|---|---|---|---|---|---|
| **Claude Code** | Supported (form and URL modes; arrived in v2.1.76 per third-party coverage) | Handled; doesn't extend the call timeout | Asks the server to cancel when the user stops a task | Supported: re-fetches in interactive sessions; on the newer protocol over "a stream it holds open" | All four, **if** it uses 2026-07-28 with remote HTTP servers: the first thing the manual test checks |
| **claude.ai custom connectors** | Not documented | Not documented | Not documented | Not documented; reports say a manual refresh is needed | Docs say not yet supported: "Resource subscriptions, Sampling, Advanced or draft capabilities". Assume today's behavior |
| **Old-protocol clients** (≤ 2025-11-25) | Would need a session | – | – | Would need a session | Today's behavior |
| **Dashboard chat** | Has its own confirm-before-write already (`app/chat.py`) | – | – | – | Unchanged (not MCP to the gateway) |

Sources: https://claude.com/docs/connectors/building, https://code.claude.com/docs/en/mcp.

## 4. Goals, non-goals, anti-goals

**Goals**
- Write confirmation, over-budget reason, progress, cancellation and push working for a
  2026-07-28 client (Claude Code), on an edge running 2 replicas × 2 workers.
- Every other client behaves exactly as today.
- Every feature survives a request landing on a different replica, and a pod restart between
  the two rounds of an elicitation.
- Manually testable in Claude Code against the local k3d cluster and the VPS.
- Lower latency on every upstream call by reusing connections and remembering how each
  server negotiates (§5.5), without keeping MCP sessions.

**Non-goals**
- Passing an upstream server's own questions (its elicitations) through to Claude. That needs
  the upstream's sealed state nested in the gateway's own; no upstream asks questions today.
- MCP sessions of any kind, and Redis.
- Sampling, roots, resource subscriptions.
- Changes to the dashboard chat.
- Resumable event streams: push events carry no data and a new listen stream gets a fresh
  `list_changed`, so nothing needs replaying.

**Anti-goals** (a failure even if it works)
- Any state that ties a request to a replica, or any change that makes restarts or extra
  workers break something.
- An elicitation answer replayed for a different tool, different arguments, a different user
  or a different gateway.
- A user who declines a write being charged against their budget, or the write running anyway.
- Old clients or claude.ai behaving differently than today.
- Automated Claude Code runs (`-p`, CI) hanging on a question nobody answers. They time out on
  Claude Code's side (elicitation timeout) or answer through its `Elicitation` hook; the
  gateway must treat no answer as no call.

## 5. Design

All gateway changes are in `services/gateway`. Detecting a capable client: a request on
2026-07-28 carries the client's capabilities in its protocol metadata. A client gets
elicitation only if it declares elicitation (form mode) there; progress only if the request
carries a progress token; push only if it sends `subscriptions/listen`. Everything else takes
the fallback, which is today's behavior.

### 5.1 Elicitation

**Mechanism.** `on_call_tool` may return `InputRequiredResult` (`mcp/server/lowlevel/server.py:155`).
The SDK's `RequestStateBoundary` middleware seals the outgoing `requestState` and verifies the
echoed one on the retry, answering `-32602 "Invalid or expired requestState"` on any tampering
(`mcp/server/request_state.py`). The gateway installs it on its low-level `Server`:
- **Key:** one key shared by all replicas of all gateways, `REQUEST_STATE_KEY`, in the existing
  `gateway-credentials` Secret, which both modes already mount. `RequestStateSecurity` takes a
  list of keys, so rotation is "add new, keep old for a while".
- **Audience:** the gateway's slug, so state minted on gateway `prod` is refused on `dev`.
- **Principal binding:** the authenticated user's id, so one user's sealed state is useless to
  another.
- **Expiry:** about 10 minutes.

**What the sealed state contains:** what was asked (`confirm` or `reason`), and a hash of the
tool name and arguments. On the retry, the gateway checks the hash against the retried call's
tool and arguments; a mismatch is refused. The state carries no permission: the retry runs
every check again (key, team policy, rate limit, budget) as on any call.

**Flow 1: write confirmation**
1. A call arrives for a write tool: an upstream tool that is not marked read-only
   (`readOnlyHint`), the same rule the dashboard chat uses (`app/chat.py`, `_is_write`). The
   gateway's own `gateway__*` tools are never confirmed.
2. All checks pass (key, policy, rate limit, budget fits), and the client supports elicitation.
3. The gateway returns `InputRequiredResult` with a form elicitation: a message naming the tool
   (title if it has one) and its arguments in readable form, and Accept / Decline.
4. **Accept:** the client repeats the call with the answer and the sealed state. The gateway
   verifies, re-runs every check, then charges the budget and calls the upstream, exactly like
   a normal call.
5. **Decline or cancel:** the call returns "Cancelled by the user. Nothing was changed."
   Nothing is charged; nothing reaches the upstream.

**Flow 2: over-budget reason**
1. A write call is over the member's budget (today: `approvals.charge` returns the over-budget
   message and the gateway files an approval request at once, `app/upstream.py`), and the
   client supports elicitation.
2. Instead of filing at once, the gateway returns `InputRequiredResult` asking: "This is over
   your budget. Give a reason and the gateway will ask an admin to approve it." A text field,
   up to the existing summary limit (`SUMMARY_MAX`).
3. **Answer given:** on the retry the gateway files the approval request with that reason as
   its `summary` (the field `approvals.request` already takes), and returns today's message
   telling the client to call `gateway__wait_for_approval`.
4. **Decline:** nothing is filed; the call returns the over-budget message without filing.
5. If a request for that tool is already open, or the tool is locked after a rejection, the
   gateway says so straight away without asking, as today.

A call never asks the same question twice, and a call that went through the over-budget flow
isn't also asked for confirmation. When the admin approves, the gateway runs the call as today.

**Budget used up between the two rounds** (my call). The budget is checked in round 1 and
charged on the retry; another call by the same member may use it up in between. If the
accepted retry no longer fits, the gateway switches to Flow 2 and asks for a reason. That is a
second, different question, which the rule above allows.

**Fallback (client without elicitation):** writes run without confirmation, and over-budget
writes file the approval at once with no reason. That is today's behavior.

**Audit.** The first round (asking) doesn't reach the upstream and isn't charged. It is
recorded as an audit row with an error such as "confirmation asked" or "reason asked", so an
admin can see that questions were asked. A decline is recorded as "declined by the user". The
accepted retry is a normal audited call.

**Ordering constraint for the builder.** Today `prepare()` in `call_tool` inserts the audit row,
checks the rate limit, the policy, then charges the budget, in one step. Asking must happen
after the policy and budget checks and **before** charging; charging moves to the accepted retry.

### 5.2 Progress

- Turn off `json_response` in `streamable_http_app` (`app/main.py:229`), so a call's response
  can be an event stream. In the SDK's modern path, a call that finishes quickly still gets a
  plain JSON response; only a call that sends progress or runs long becomes a stream (with
  keepalive pings, which also help behind proxy idle timeouts).
- When the client's request carries a progress token, the gateway passes a `progress_callback`
  to the upstream `call_tool` (`Client.call_tool` takes one) and forwards each update to the
  client with the request context's `report_progress`. The upstream's own message text is
  forwarded as is.
- **woo-mcp reports progress** in `get_inventory_health` (one update per page it fetches), so
  there is something real to see in the manual test. Other long woo-mcp tools may follow the
  same pattern; the builder decides which.
- Progress doesn't extend any timeout (the gateway's `CALL_TIMEOUT_S`, or Claude Code's).

### 5.3 Cancellation

- On 2026-07-28, the client cancels by closing the call's response stream. The SDK then cancels
  the gateway's handler. Because the upstream call runs inside it, the upstream connection is
  closed too.
- The gateway catches the cancellation in `call_tool`, records the audit row as "cancelled by
  the client", and re-raises it. Today such a call stays as "no result recorded".
- The budget charge for a cancelled write is kept, because the upstream may already have done
  the write. The hourly budget window (`approvals._window`) today counts only rows that
  succeeded or are still waiting for a result, so it must also count "cancelled by the client"
  rows, or a cancelled write would quietly free its budget.
- No change for old-protocol clients: their `notifications/cancelled` has no session to land
  in, as today.

### 5.4 Push: `list_changed`

- **Serving.** The gateway's `Server` gets `on_subscriptions_listen=ListenHandler(bus)`
  (`mcp/server/subscriptions.py`). `ListenHandler` sends the acknowledgement, tags frames and
  filters by what the client asked for; the gateway supplies the bus.
- **The bus (new, per process).** It implements `SubscriptionBus`
  (`publish`/`subscribe`). It holds one dedicated Postgres connection (not from the pool) on
  `LISTEN gateway_events`. When a notification names this edge's gateway slug (or `*`), the bus
  delivers `ToolsListChanged()` to its local listeners. If the connection drops, it reconnects
  and delivers one `ToolsListChanged()`, since changes may have been missed.
- **Who sends `NOTIFY gateway_events, '<slug>'`** (the control plane, after the change commits):
  - servers attached to or detached from a gateway;
  - a team's tool policy changed, a team attached to a gateway, members added or removed;
  - the gateway's PII switch changed (it changes the tools' output schemas).

  And `NOTIFY gateway_events, '*'` when a server is added, edited, enabled, disabled or deleted.
  An edge's health loop sends `NOTIFY gateway_events, '<own slug>'` when a server's tool list
  changes between checks; several replicas noticing the same change send several notices,
  which is harmless.
- **Refresh before telling clients.** A replica may not yet know a newly attached or edited
  server's tools. On a notice, the edge first checks the servers concerned (the existing
  `refresh_soon`), then delivers `ToolsListChanged()`, so the client's re-fetch returns the
  complete list.
- **Filtering per user isn't needed.** The event carries no data. Each client re-fetches
  `tools/list` and gets its own filtered list, as on any request.
- **Authentication.** `subscriptions/listen` goes through `ApiKeyGuard` like every `/mcp`
  request. A listen stream for a revoked key stays open until it closes, but it only ever says
  "ask again", and the re-fetch is refused. The builder may cap stream lifetime (e.g. re-listen
  every 30 minutes) to bound this.
- **Connections.** Each listening client holds one open request. Traefik and Tailscale Funnel
  idle timeouts are handled by the SDK's keepalive pings. A new listen stream also gets one
  `ToolsListChanged()` immediately, so a client reconnecting after a restart never misses a
  change.

### 5.5 Faster upstream calls: reuse connections, remember how each server negotiates

Today every upstream call opens a new HTTP client and negotiates MCP from scratch, then tears
it down (`connect()` in `app/upstream.py`). Over the internet that's about 7 round trips for
an older-protocol server and 4 for a 2026-07-28 one, for 1 useful round trip. This section
removes most of that **without keeping any MCP session**.

- **Reuse the HTTP client per (server, credential).** Each process keeps a small cache of
  `httpx2.AsyncClient`s, keyed by server id, URL and a hash of the credential headers, each with
  the SSRF-guarded transport (`app/ssrf.py`). TCP and TLS connections then stay open between
  calls. Because the key includes the credential, one user's personal token is never sent on
  another user's call, and a changed URL or credential simply makes a new key.
- **Remember how each server negotiates.** Per (server id, URL), cache the upstream's
  `server/discover` result and pass it as `prior_discover` to the MCP `Client`
  (`mcp/client/client.py:341`), so a 2026-07-28 upstream goes straight to `tools/call`. For an
  older-protocol upstream, remember that the probe fails and connect with `mode="legacy"`,
  skipping the failed probe. If a call fails with a protocol error, forget the cached result and
  negotiate again on the next call.
- **Bounds.** Close a cached client after it has been idle for a few minutes, cap the number of
  cached clients per process (personal credentials mean one per user), and close them all on
  shutdown, the same way the health-check retries are cancelled.
- **No retries.** A broken pooled connection is replaced on the next call. A `tools/call` is
  never retried by the gateway, so a write can't run twice.
- **Cancellation still works.** Cancelling a call closes that call's request; the pooled
  client discards the connection it was using.

Expected result: about 1 round trip for a 2026-07-28 upstream and 4 for an older one, both
over a warm connection. Health checks use the same cache. The latency figures are estimates
until measured (§8).

### 5.6 What doesn't change

- `stateless_http=True` stays. One visible change for old-protocol clients: with
  `json_response` off, their POSTs are answered as an event stream rather than plain JSON. The
  MCP transport allows both, and real clients accept both (they send
  `Accept: application/json, text/event-stream`). Hand-written scripts that parse the body as
  JSON need to read the event instead, as `tests/test_edge.py` will.
- Upstream MCP: still no session kept; each call negotiates (or reuses the cached negotiation
  result) over a pooled connection, with the SSRF guard (§5.5).
- The three kinds of upstream servers (internet URL, store woo-mcp, hosted) all behave the
  same: these features sit between the client and the gateway.
- Dashboard chat, control plane `/api`, approvals admin flow.

## 6. Security summary

- Sealed `requestState`: encrypted and authenticated by the SDK, bound to the user (principal)
  and the gateway (audience), expiring, and checked against the retried tool and arguments.
  It grants nothing: every check re-runs on the retry.
- A declined or unanswered confirmation never reaches the upstream and never charges a budget.
- Push events carry no data, so they can't leak another team's tools.
- `REQUEST_STATE_KEY` is a new secret: generated at install, kept in `gateway-credentials`,
  never in the chart's values.

## 7. Configuration and rollout

- `gateway-credentials` gets `REQUEST_STATE_KEY` (random, 32 bytes, base64). The gateway refuses
  to start without it, like `CREDENTIALS_KEY`. The install docs and the VPS GitOps setup need
  the key added.
- No new Helm values are expected besides passing the key through. Edges keep 2 replicas and
  `edgeWorkers: 2`.
- New edge image via GitOps; existing `gw-*` edges need the manual helm upgrade, as for every
  gateway change. New woo-mcp image for progress.

## 8. Testing

**Automated** (gateway tests use the SDK `Client`, which supports `elicitation_callback`,
`progress_callback` and the input-required retry loop):
- Confirmation: accept runs the call and charges once; decline runs nothing and charges
  nothing; the retry served by a **different edge replica** than the first round succeeds.
- Tampering: changed arguments on the retry, state from another user, state from another
  gateway, expired state: all refused.
- Over-budget reason: the approval request is filed with the reason as summary; decline files
  nothing; an already-open request doesn't ask.
- Fallback: a client without elicitation gets exactly today's results (existing tests keep
  passing unchanged).
- Progress: a fake upstream sending progress; the client receives the updates in order.
- Cancellation: closing the stream mid-call marks the audit row "cancelled by the client" and
  the fake upstream sees its connection closed.
- Push: a listen stream on replica B receives `list_changed` after the control plane attaches a
  server; a new listen stream gets one at once; the bus reconnects after its Postgres
  connection is killed.
- Connection reuse: two calls to the same upstream open one TCP connection; a different
  credential gets a different client; a 2026-07-28 upstream sees no second `server/discover`;
  a changed server URL negotiates again; idle clients are closed.
- Latency: time 10 calls to exa (internet) and woo-mcp (in-cluster) before and after §5.5, and
  write the numbers in this document.
  Latency (local notes upstream, median of 10 sequential calls, loopback so no TLS/RTT):
  before (new httpx2 client + `Client(mode="auto")` per call) 3.3 ms, after (UpstreamPool) 1.4 ms.
  Internet and in-cluster figures still to measure.

**Manual** (the user, in Claude Code, against an edge with 2 replicas × 2 workers):
1. Ask Claude Code for a write. An audit row with error "confirmation asked" proves the client
   uses 2026-07-28 with elicitation (only such clients are asked). A plain result means an older
   client; stop there.
2. Ask Claude to create a product: a confirmation dialog appears. Decline: nothing created.
   Accept: product created, audit shows one call.
3. Exceed the budget: Claude asks for a reason; the admin sees the approval with that reason.
4. Run `get_inventory_health`: progress shows while it runs.
5. Start it again and stop it in Claude Code: the audit shows "cancelled by the client".
6. With Claude Code connected, attach a new server to the gateway in the dashboard: the new
   tools show up without reconnecting.
7. Delete one edge pod between steps: everything keeps working.
8. Repeat 2 and 3 in claude.ai: today's behavior, no errors.

**Known limits:**
- Right after a push, the client's re-fetch can land on a replica that hasn't finished
  refreshing a newly attached server, so it may briefly see the old list until the next change
  (step 6 may need a second re-fetch).
- Pooled clients share upstream cookies per credential.
- Each edge process holds one extra Postgres connection for LISTEN (4 per gateway).

## 9. Interview talking points

- "I kept the gateway stateless on purpose: my scaling unit is the per-team gateway, replicas
  and workers are interchangeable, and restarts are free."
- "The usual reason to add sessions is interactivity: asking the user, progress, cancel, push.
  MCP 2026-07-28 gives all four without sessions, and my gateway already speaks it. So I added
  them without giving anything up."
- "Elicitation is a round trip, not a wait: the call ends with 'input required' and a sealed
  state; the client asks the user and repeats the call to any replica. The state is encrypted,
  bound to the user and the gateway, and grants nothing: every check runs again."
- "Push is a broadcast: Postgres NOTIFY tells every replica, and each tells the clients whose
  listen streams it holds. Events carry no data, so there's nothing to filter or leak."
- "Old clients and claude.ai keep exactly the behavior they had, so nothing gets worse."
- "For latency I reuse the HTTP connection per server and credential and remember how each
  server negotiates, instead of keeping long-lived upstream sessions. That's most of the speed
  win with no session state to leak between users, and no retried tool calls."

## 10. Decisions

**The user decided**
- Build all four features: elicitation (both write confirmation and over-budget reason),
  progress, cancellation and push.
- Keep the gateway completely stateless; no sessions.
- Leave the dashboard chat as it is.
- Test manually in Claude Code and claude.ai.
- Include faster upstream calls (connection reuse plus remembered negotiation, §5.5).

**My calls** (the user can override)
- Passing upstream servers' own questions through is out of scope.
- "Write" means not marked read-only, the same rule as the chat.
- Postgres `LISTEN/NOTIFY` for push, not Redis.
- woo-mcp gets progress reporting so there's something to test.
- A cancelled write keeps its budget charge.
- The asking round is audited as "confirmation asked" / "reason asked".
- One `REQUEST_STATE_KEY` for all gateways, separated by audience.
- No on/off setting: every feature turns on only when the client declares support, so a client
  that doesn't gets exactly today's behavior. The user didn't answer whether to check Claude
  Code first; connection reuse (§5.5) ships either way.
- claude.ai is not singled out by client name: it gets the features once it declares them.
- Budget used up between the two rounds of a confirmation: ask for a reason (Flow 2).
- Cancelled writes count against the hourly budget window.
- An edge refreshes the changed servers before pushing `list_changed`.

**Left to the builder**
- The exact elicitation wording and form schema.
- Which other woo-mcp tools report progress.
- Whether listen streams get a maximum lifetime.
- Where in `call_tool` the asking step goes, within the ordering constraint in §5.1.
