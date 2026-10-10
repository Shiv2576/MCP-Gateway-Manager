# Project context: Team MCP Gateway on Kubernetes

What has been built so far, how it fits together, what has been verified, and
what is still open. Last updated 2026-10-10.

No secret values are written here. Where a password or key lives is named
instead (Kubernetes Secrets in namespace `mercora`).

---

## 1. The goal

A WooCommerce store on Kubernetes, plus a team **MCP gateway**:

- Admins register upstream MCP servers (Streamable HTTP), each with an optional
  static credential (header or bearer token).
- The gateway is itself one MCP endpoint. Listing tools returns the tools of
  every enabled server. A call is routed to the right server.
- Two servers may expose a tool with the same name; a documented scheme handles it.
- A slow or dead upstream must not break listing or calls to other servers.
  Calls have timeouts and return clear errors.
- Upstream credentials stay on the server. Never sent to a browser or MCP client.
- At least three upstreams: the WooCommerce MCP server plus two others.
- A React dashboard where admins manage servers, users, keys and see the audit log.

Added on top during the work:
- a **chat agent** in the dashboard that uses the gateway tools as the logged-in user (4.6);
- **member access**: admins choose which servers each member may use (4.7);
- **multi-store provisioning**: admins create up to 3 WooCommerce stores from
  templates, each in its own namespace with its own woo-mcp, registered in the
  gateway automatically (4.8);
- **OAuth sign-in for Claude**: members add the gateway to claude.ai / Claude
  Desktop as a custom connector and sign in with their gateway login; Anthropic's
  servers then call `/mcp` with an OAuth token (4.9).

An earlier `customer` role (store customers ordering through the gateway) was
built and later **removed**; the gateway's migration deletes old customer users
and the `customer_tools` column. woo-mcp now has only the 6 store tools.

---

## 2. Architecture

```
 ┌──────────────────────────── k3d cluster "store-check" ─────────────────────────────────────┐
 │ namespace mercora (the platform)                                                         │
 │                                                                                          │
 │  Browser ── dash.localhost ──► team-ui-dashboard (nginx: React app, /api ──► gateway)     │
 │  Claude Code / Inspector ── gateway.localhost ──► team-gateway                           │
 │  claude.ai (Anthropic) ── https PUBLIC_URL (ngrok) ──┘ (Starlette + MCP SDK, SQLite PVC) │
 │                                 /mcp  one MCP endpoint (API key or OAuth token)          │
 │                                 /authorize /token /register /oauth/login  OAuth server   │
 │                                 /api  REST API (session cookie or API key)               │
 │                                 adds each upstream's credential                          │
 │                   ┌─────────────┬───────┴──────────┬──────────────────────┐              │
 │                   ▼             ▼                  ▼                      ▼              │
 │                 notes          fx         Store CRs (mercora) ──► store-operator (kopf)  │
 └───────────────────────────────────────────────────────│──────────────────│───────────────┘
              tools calls to each store's woo-mcp         │                  │ creates
 ┌─────────── namespace store-<name> (one per store) ─────▼──────────────────▼──────────────┐
 │  woo-mcp-woo-mcp (6 tools) ──► store-wordpress (WooCommerce) ── store-mariadb             │
 │  ResourceQuota, LimitRange, default-deny NetworkPolicies; Browser ── <name>.localhost     │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

`mercora` holds only the platform: gateway, dashboard, operator, notes, fx.
Every store lives in its own namespace `store-<name>` with its own woo-mcp,
which the gateway knows as server `<name>` (tools `<name>__list_orders`, …).
The gateway is the only MCP endpoint clients use, and the only place where
access is controlled and every call is recorded.

---

## 3. Repository layout

```
team-mcp-gateway/
  README.md                 user-facing docs (store, woo-mcp, gateway, collision scheme, dashboard, store provisioning)
  ROADMAP.md                the original phase plan (phases 0-7)
  context.md                this file
  docs/superpowers/         spec + plan for store provisioning
  services/
    woo-mcp/                WooCommerce MCP server (Python, MCP SDK 2.3)
      app/config.py         settings from env
      app/woo.py            WooCommerce REST client, OAuth 1.0a signing over plain HTTP
      app/server.py         the 6 tools
      app/main.py           /mcp behind a bearer token, /healthz, /readyz
      app/check.py          one-off auth check against a real store
      tests/fake_woo.py     in-memory fake WooCommerce that verifies OAuth signatures
      tests/tests_woo_mcp.py  end-to-end tests
    gateway/                the MCP gateway (Python, Starlette, MCP SDK 2.3, SQLite)
      app/db.py             schema, migrations, password/key hashing, member grants
      app/upstream.py       health loop, tool listing, grant filter, call_tool (route, timeout, audit), rate limit
      app/api.py            REST API (users, keys, servers, tools, audit)
      app/chat.py           chat agent (POST /api/chat)
      app/stores.py         store API (POST/GET/DELETE /api/stores)
      app/kube.py           minimal Kubernetes client for Store resources (ServiceAccount token)
      app/oauth.py          OAuth authorization server for /mcp (clients, codes, tokens, login page)
      app/main.py           settings, /mcp guard (API key or OAuth token), MCP handlers, app wiring
      tests/test_gateway.py, tests/test_stores.py, tests/test_oauth.py
    store-operator/         kopf operator: Store CR -> namespace + Helm releases + gateway server
      store_operator/manifests.py  everything it creates, as plain data (unit tested)
      store_operator/handlers.py   create / reconcile / repair / delete handlers
      store_operator/gateway.py    registers / removes the store's server in the gateway
      tests/                       manifests + gateway sync tests
    demo-mcp/               two small upstreams in one image: notes and fx
    dashboard/              React 19 + Vite 7, served by nginx
      src/App.jsx           shell, login, sidebar
      src/Servers.jsx       servers table, add/edit form, OAuth / API key status
      src/Tools.jsx         tool browser and "try a tool" runner (typed form from the input schema)
      src/Chat.jsx          chat agent UI
      src/Stores.jsx        store templates, create, status, delete
      src/Admin.jsx         API keys, users (server access), audit log
      nginx.conf.template   static files + /api proxy to the gateway
  charts/
    store/                  Bitnami WordPress + MariaDB wrapper, Ingress, setup Job (seed.php templates)
    woo-mcp/                Deployment, ClusterIP Service, NetworkPolicy (ingress: gateway only; egress: own WordPress + DNS)
    gateway/                Deployment (1 replica, Recreate), PVC (kept on uninstall), Service, Ingress, RBAC for Stores
    store-operator/         Store CRD, ClusterRole, Deployment, PriorityClass store-workload
    demo-mcp/               installed once per demo server (notes, fx); includes NetworkPolicy
    dashboard/              nginx Deployment, Service, Ingress
  deploy/                   local and prod values for every chart
```

---

## 4. Components in detail

### 4.1 Store (phase 1, now a template)

- `charts/store`: Bitnami WordPress + MariaDB, persistence on, Ingress per host.
- A setup Job installs WooCommerce, sets permalinks, enables cash on delivery,
  registers the woo-mcp API key from Secret `woo-mcp-credentials`, then seeds
  products and orders with `files/seed.php` (one `wp eval-file`, template from
  `setup.template`: `catalog-a`, `catalog-b` or `blank`).
- `setup.hook: true` (default, for a hand install) runs the Job as a Helm hook;
  the operator sets it `false` so the Job is a normal resource that dies with
  the namespace. Hook resources survive `helm uninstall`; that is what kept the
  old store's PVC in `Terminating` until its Job was deleted.
- The original single store (`mercora` release, store.localhost) has been
  uninstalled; stores are now created from the dashboard (4.8).

### 4.2 woo-mcp (phase 2)

Calls the WooCommerce REST API (`/wp-json/wc/v3`) with one store-wide
read/write key. Inside the cluster this is plain HTTP, so every request is
OAuth 1.0a signed (`app/woo.py`), against `WOO_PUBLIC_URL` (the site address),
while requests go to `WOO_INTERNAL_URL` (the Service). One woo-mcp per store.

| Tool | Does |
|---|---|
| `list_products` | newest first, optional name search, `limit` 1-50, `page` |
| `create_product` | creates and publishes a simple product (`price` as text, e.g. `499.50`) |
| `delete_product` | moves a product to the trash |
| `list_orders` | newest first; `on_date` today/yesterday/YYYY-MM-DD in the store timezone; status filter |
| `get_order` | one order with items, payment method, destination |
| `update_order_status` | moves an order to a new status |

Data minimization: orders never return customer email, phone or street
address; only name and city/state/country.

### 4.3 Gateway (phases 3 and 4)

**Storage: SQLite** at `/data/gateway.db` on a PVC (`helm.sh/resource-policy: keep`).
Tables:

| Table | Holds |
|---|---|
| `users` | username, scrypt password hash, role (`admin` / `member`) |
| `sessions` | sha256 of the session token, user, expiry (12 h) |
| `api_keys` | sha256 of the key, 12-char prefix, owner, `revoked_at` |
| `servers` | name, url, `auth_header`, `auth_value` (the credential), enabled |
| `user_servers` | which servers each member may use (see 4.7) |
| `audit_log` | time, user, tool, server, ok, duration, error text (never arguments or results) |
| `oauth_clients` | client_id, registration info (name, redirect URIs) |
| `oauth_codes` | sha256 of the login-page id, then of the auth code; client, user, PKCE params, expiry |
| `oauth_tokens` | sha256 of access / refresh token, user, client, scopes, resource, expiry |

`db._migrate` upgrades older databases in place (drops the old customer role
and `customer_tools` column, adds `user_servers` granting existing members every
existing server); this ran on the live database without loss.

**Bootstrap:** on first start with an empty `users` table, the gateway creates
the first admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD` (Secret `gateway-admin`).
After that the Secret is never read; logins are checked against the database.

**Upstream manager (`app/upstream.py`):**
- A background loop lists every enabled server's tools every 30 s, all in
  parallel, each under a 5 s timeout, and keeps them in memory with a status
  (`pending` / `ok` / `unreachable` + reason). Adding or editing a server
  triggers an immediate check.
- Tool listing reads only that saved copy, so it never waits on an upstream.
- A call opens a fresh client to the upstream under a 10 s hard timeout. Every
  failure becomes a normal MCP tool error with one sentence, e.g.
  `Upstream 'fx' did not answer within 10s.` or `Upstream 'fx' is unreachable (ConnectError).`
- One function, `call_tool(user, name, args)`, is the only call path: rate
  limit, grant check, enabled check, upstream call, audit row. `/mcp` and the
  dashboard's "try a tool" both use it.
- The enabled flag and the API key are read from the database on every request:
  disabling hides tools immediately; a revoked key fails on the next call.

**Collision scheme:** every tool is exposed as `<server>__<tool>`
(`basic1__list_orders`, `notes__list`, `fx__list`). Always prefixed, never only on
a clash, so names never change when servers are added. Server names are
`[a-z0-9-]`, never `_`, so the first `__` always splits correctly. Names cannot
be edited (clients depend on them). Tools whose full name breaks MCP's rules
(over 128 chars, bad characters) are hidden and counted in the server status.

**Credentials:** `{"type":"header","name":...,"value":...}` or
`{"type":"bearer","token":...}`. Only the gateway reads them, to add them to its
own outgoing requests. The API returns only `auth_header` and `has_credential`.
A PATCH without `auth` keeps the stored credential. Client headers are never
forwarded upstream.

**Authentication:**
- Browsers: `POST /api/login` → HttpOnly, SameSite=Strict session cookie
  (`Secure` when `cookieSecure: true`). Wrong username and wrong password take
  the same time.
- MCP clients and scripts: `Authorization: Bearer mcpg_...` API key, shown once,
  stored as a hash.
- Claude custom connector: OAuth access token on `/mcp` (4.9), only when `PUBLIC_URL` is set.
- Wrong-password limit: 5 failures per 15 min per username, on both the dashboard
  login and the OAuth login page (in memory, per replica).

**Roles:**

| Role | Can |
|---|---|
| admin | everything: servers, users, all keys, audit log, stores, every tool |
| member | only the servers an admin granted (4.7), own keys |

Rate limit: 60 tool calls per minute per user (`CALLS_PER_MINUTE`); every
call, including refused ones, is audited.

**REST API:**

| Endpoint | Who |
|---|---|
| `POST /api/login`, `POST /api/logout`, `GET /api/me` | anyone / logged in |
| `GET, POST /api/users` (role, `server_ids` for members), `PUT /api/users/{id}/servers`, `DELETE /api/users/{id}` | admin |
| `GET, POST /api/keys`, `DELETE /api/keys/{id}` | own keys; admin sees all |
| `GET /api/servers` | admin; members see only granted servers |
| `POST /api/servers`, `PATCH, DELETE /api/servers/{id}` | admin |
| `GET /api/tools`, `POST /api/tools/call` | logged in (filtered by grants, audited) |
| `POST /api/chat` | logged in (4.6) |
| `GET, POST /api/stores`, `DELETE /api/stores/{name}` | admin; delete only your own (4.8) |
| `GET /api/audit?limit=` | admin |
| `GET /api/oauth/connections`, `DELETE /api/oauth/connections/{user_id}/{client_id}` | own; admin sees all (4.9) |
| `GET /api/connect` | logged in: `{oauth, mcp_url}` for the dashboard |
| `/healthz`, `/readyz` | open |

**Settings (env):** `DATABASE_PATH`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`,
`CALL_TIMEOUT_S` (10), `LIST_TIMEOUT_S` (5), `REFRESH_INTERVAL_S` (30),
`CALLS_PER_MINUTE` (60), `COOKIE_SECURE`, `STORES_NAMESPACE` (turns the store API on;
set by the chart when `stores.enabled`), `OPENROUTER_API_KEY`, `LLM_MODEL`,
`PUBLIC_URL` (chart `publicUrl`; turns OAuth on), `OAUTH_REDIRECT_URIS` (chart `oauthRedirectUris`).

### 4.4 demo-mcp: notes and fx (phase 4)

One image, chosen by `DEMO_SERVER`:
- **notes**: `save_note`, `list`. Credential: `X-API-Key` header. Notes live in memory.
- **fx**: `convert` (INR → USD/EUR/GBP/AED/JPY from a fixed table), `list`. Credential: bearer token.

Both have a tool named `list` on purpose, to show the collision scheme
(`notes__list` vs `fx__list`). Both use different credential types, to show both.

### 4.5 Dashboard (phase 5)

React + Vite, served by nginx, which also forwards `/api` to the gateway so the
session cookie stays same-origin. Pages:

- **Servers**: stat tiles (servers, healthy, unreachable, tools), pills showing
  whether OAuth and API keys are enabled for `/mcp`, then a table: name/status,
  upstream auth type, tools, created, enabled switch, edit / delete icons (admin);
  expand to see tools. Refreshes every 3 s.
- **Tools**: every tool the user may use, grouped by server. Picking one builds
  a typed form from its input schema: text inputs with length/pattern checks,
  number inputs with min/max, a dropdown for enums (with "Any" when optional), a
  switch for booleans, a JSON box only for object/array fields. Required fields
  are marked, descriptions and limits shown, defaults pre-filled, empty optional
  fields not sent. Run, then see result and time. Raw schema under "Input schema".
  Also shows how to connect an MCP client.
- **Stores** (admin): pick a template, name the store, create; live phase and
  message, store URL when Ready, delete your own stores; `n/3` counter.
- **Chat**: talk to an LLM agent that uses the gateway tools as you (see 4.6).
- **API keys**: create (shown once, copy button), list, revoke; **Connected apps**
  lists OAuth sign-ins (app, user, last token) with Disconnect.
- **Users** (admin): add admin / member (with the servers a member may use),
  change a member's access, delete a user.
- **Audit log** (admin): last 200 calls, refreshes every 5 s.

### 4.6 Chat agent (phase 6)

An LLM in the dashboard that answers questions by calling gateway tools as the
logged-in user. Code: `services/gateway/app/chat.py` (backend, `POST /api/chat`)
and `services/dashboard/src/Chat.jsx` (UI, the **Chat** tab).

**How a request runs**

1. The browser sends the whole conversation (`{messages: [{role, content}]}`)
   with each request. Nothing is stored server-side; a page reload starts a new chat.
2. The gateway checks the session (`current_user`), validates the messages
   (roles `user`/`assistant` only, last one from the user, at most the last 20
   messages, each cut to 4000 chars) and adds a system prompt naming the user.
3. Every tool from `upstreams.list_tools(user)` (enabled and granted servers only, names
   `<server>__<tool>`) is offered to the model as an OpenAI-style function, with
   its input schema as `parameters`. Names the API rejects are skipped.
4. Loop, at most 6 rounds: call the model; if it asks for tool calls, run each
   through `upstreams.call_tool(user, name, args)` and feed the result back
   (structured content as JSON, else the text; 8000 chars max); if it answers
   in plain text, return it.
5. Response: `{reply, calls: [{name, arguments, ok, result}]}`. The UI shows each
   tool call as a collapsible ✓/✗ line (arguments and result, cut to 2000 chars)
   above the reply bubble.

**Features and guardrails**

- Tool calls go through the same `call_tool` path as `/mcp`: same routing,
  timeouts, per-user rate limit, and an audit log row under the user's name.
  The upstream credentials never reach the model or the browser.
- System prompt: the model must say what it will do and ask for confirmation
  before a tool that creates, changes or deletes something, unless the user
  already asked for exactly that.
- Bad tool arguments (not a JSON object) are returned to the model as a failed
  call instead of crashing the request.
- Model provider errors or timeouts (60 s) come back as HTTP 502 with a short
  reason; without a key the endpoint returns 503 "chat is not configured".
- `max_tokens: 1024` per model call, so OpenRouter does not reserve the model's
  full output limit against the account credit.
- nginx `proxy_read_timeout 180s` on `/api/`, since one chat request can run
  several model and tool rounds.

**Configuration**

| Setting | Where | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | optional key in the `gateway-admin` Secret; turns chat on | unset (chat off) |
| `LLM_MODEL` | chart value `llmModel` | `openai/gpt-4.1-mini` |
| `LLM_URL` | env var | `https://openrouter.ai/api/v1/chat/completions` (any OpenAI-compatible URL works) |

**Test**: `test_chat_agent_calls_tools_as_the_logged_in_user` in
`services/gateway/tests/test_gateway.py` runs a fake chat-completions server
that asks for `notes__save_note`, then checks the call ran and was audited as
the logged-in user, and that an unauthenticated request gets 401.

---

### 4.7 Member access (bonus, part 1)

- A new member sees **no** servers. Admins pick the servers a member may use
  (Members → Access, or when adding the member). Admins see every server.
- Stored in `user_servers` (user, server), cascading on user or server delete.
  Upgrading an old database granted every existing server to every existing member.
- Enforced in one place: `Upstreams.list_tools(user)` and `call_tool`. A call to
  a server the member was not granted answers "Unknown tool" (does not reveal it)
  and is audited. Applies to `/mcp`, the Tools page and the chat agent alike.
- API: `GET/POST /api/users` carry `server_ids`; `PUT /api/users/{id}/servers`;
  `DELETE /api/users/{id}` (not yourself; an admin's stores are deleted first).

### 4.8 Store provisioning (bonus, part 2)

Code: `services/gateway/app/stores.py` + `kube.py` (API), `services/store-operator/`
(kopf operator), `charts/store-operator/` (CRD, RBAC), `services/dashboard/src/Stores.jsx`.
Spec and plan: `docs/superpowers/specs/2026-10-10-store-provisioning-design.md`,
`docs/superpowers/plans/2026-10-10-store-provisioning.md`.

```
Stores tab ── POST /api/stores ──► gateway: checks admin, name, 3-per-admin cap ──► Store CR (mercora)
                                                                                     │ watched by
store-operator (kopf, ClusterRole) ◄────────────────────────────────────────────────┘
  1. namespace store-<name>: ResourceQuota, LimitRange, 6 NetworkPolicies,
     Secret woo-mcp-credentials (generated once, never replaced)
  2. helm upgrade --install store   charts/store   (template, plain setup Job, Bitnami policies off)
     helm upgrade --install woo-mcp charts/woo-mcp (ingress from mercora gateway only, egress to own WordPress only)
  3. every 15 s: WordPress ready + setup Job succeeded + woo-mcp ready
     → upsert gateway server <name> (bearer MCP_AUTH_TOKEN) → phase Ready
     timeout 15 min from creation → Failed with the step it waited on
  delete (finalizer): delete gateway server → delete namespace → wait until gone
```

- **Templates** (`charts/store/files/seed.php`, one `wp eval-file`, fixed seed):
  `catalog-a` 300 products in Electronics/Books/Home & Kitchen/Toys/Office + 12 orders;
  `catalog-b` 300 in Fashion/Sports/Beauty/Grocery/Garden + 7 orders; `blank` 3 products + 2 orders.
  Orders are dated the day the store is created.
- **Names**: store `shop1` → namespace `store-shop1`, URL `http://shop1.localhost`,
  gateway server `shop1`, tools `shop1__list_orders`…; 2–20 chars so tool names fit the chat agent.
- **Gateway RBAC**: only `stores` in its own namespace (get/list/create/delete).
  The operator holds the cluster rights (namespaces, Helm-installed kinds).
- **Operator → gateway**: admin API key in Secret `store-operator-gateway`.
- **Recovery**: every create step tolerates "already exists"; Helm releases stuck
  "pending" are rolled back (or uninstalled) and retried; Helm errors retry 3× then Failed.

**Verified live on k3d (2026-10-10):** `shop1` (catalog-a) and `shop2` (catalog-b)
created together, both Ready in ~3 min (300 products + 12 orders / 300 + 7).
Chat "compare today's orders in shop1 and shop2" called `shop1__list_orders` and
`shop2__list_orders` → 12 vs 7 orders. From shop1's woo-mcp: own WordPress
connects; shop2's WordPress and MariaDB are refused. Member `dave` granted only
shop1 sees only `shop1__*`; calling `shop2__list_orders` → "Unknown tool". 4th
store → 400. Operator pod killed while `shop4` was Provisioning → it still went
Ready, Woo key unchanged. Deleting shop1/shop2 removed their namespaces, PVs,
gateway servers and tools. Deleting admin `erin` removed her store `erin1`.

**Found live and fixed:** with three stores the single k3d node reached 99% of
its memory requests; a restarted operator stayed Pending, so deletes waited for
it. Store pods now use PriorityClass `store-workload` (-100, never preempts), so
the operator and gateway can always evict a store pod to start.

### 4.9 OAuth for Claude custom connectors (bonus, part 3)

claude.ai and Claude Desktop "custom connectors" connect from **Anthropic's
servers**, not from the laptop, and cannot send a static API-key header. They
follow the MCP authorization spec (OAuth 2.1 + PKCE + dynamic client
registration). The gateway now is its own authorization server for `/mcp`.

Code: `services/gateway/app/oauth.py` (the SDK's `OAuthAuthorizationServerProvider`
backed by SQLite), wiring in `app/main.py`, tests in `tests/test_oauth.py`.

```
Claude (Anthropic) ── POST /mcp, no token ──► 401 + WWW-Authenticate: resource_metadata=…
   │ GET /.well-known/oauth-protected-resource/mcp, /.well-known/oauth-authorization-server
   │ POST /register        (dynamic client registration; redirect URI must be allowed)
   │ GET  /authorize       (PKCE) ──► 302 to /oauth/login?id=…
   │        member types gateway username + password ──► 302 to claude.ai/api/mcp/auth_callback?code=…
   │ POST /token           (code + PKCE verifier) ──► access token (1 h) + refresh token (30 d)
   └ POST /mcp  Authorization: Bearer <access token> ──► same path as an API key
```

- **On only when `PUBLIC_URL` is set** (chart `publicUrl`). It is the issuer and
  the token audience (`<PUBLIC_URL>/mcp`); a token issued for another URL is refused.
  Without it the OAuth routes return 404 and `/mcp` takes API keys only.
- **Token = the user who signed in**: their server grants (4.7), rate limit and
  audit rows apply exactly as with an API key. Admin sign-in works too.
- **Stored hashed**: codes and tokens are sha256 in SQLite, never in plain text.
  Codes are single use and short-lived; refresh tokens rotate (each works once).
- **Redirect allow-list**: clients may register only Claude's callbacks
  (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`,
  override with `OAUTH_REDIRECT_URIS`) or loopback `http://localhost` / `127.0.0.1`
  (Claude Code, MCP Inspector). Anything else is refused at registration.
- **Revocation**: Dashboard → API keys → Connected apps → Disconnect deletes
  that user+client's tokens; the next call is 401 and Claude asks to sign in again.
  Deleting a user deletes their tokens. `/revoke` works for clients.
- **Login page** `/oauth/login`: plain server-rendered form, same password check
  and wrong-password limit as the dashboard.

**Exposing it (local test):** `/mcp` must be reachable on public HTTPS.

```
kubectl -n mercora port-forward svc/team-gateway 8000:8000
ngrok http 8000
helm upgrade team charts/gateway -n mercora -f deploy/gateway-values-local.yaml \
  --set publicUrl=https://<id>.ngrok-free.app --set image.tag=<current> --set stores.enabled=true
```

Then Claude → Settings → Connectors → Add custom connector → `https://<id>.ngrok-free.app/mcp`
→ Connect → gateway login page → sign in as a member.

**Verified live (2026-10-10):** Claude registered as client "Claude" with redirect
`https://claude.ai/api/mcp/auth_callback`; member `shop3-tester` signed in and
holds an access + refresh token; the metadata at
`http://gateway.localhost/.well-known/oauth-authorization-server` advertises the
ngrok issuer. The audit log shows `shop3__list_products` calls under `shop3-tester`.

## 5. What is running now (local)

Cluster `store-check` (k3d, port 80).

Namespace `mercora` (platform):

| Helm release | Chart | Address / Service | Image |
|---|---|---|---|
| `team` | charts/gateway | http://gateway.localhost (`team-gateway:8000`); public via ngrok (`publicUrl`) | `gateway:0.1.5` |
| `team-ui` | charts/dashboard | http://dash.localhost (`team-ui-dashboard`) | `dashboard:0.1.5` |
| `store-operator` | charts/store-operator | (no Service; liveness on :8080) | `store-operator:0.1.2` |
| `notes` | charts/demo-mcp | `notes:8000` (cluster only) | `demo-mcp:0.1.0` |
| `fx` | charts/demo-mcp | `fx:8000` (cluster only) | `demo-mcp:0.1.0` |

Stores (each in `store-<name>` with releases `store` and `woo-mcp`, image `woo-mcp:0.1.0`):

| Store | Template | URL | Phase |
|---|---|---|---|
| `shop3` | blank | http://shop3.localhost | Ready |

`team` values: `image.tag=0.1.5`, `stores.enabled=true`, `publicUrl=<ngrok URL>`.
A free ngrok URL changes each run: re-run the helm upgrade with the new URL and
reconnect in Claude. With ngrok stopped, Claude's connector fails; API keys still work.

**Secrets** (namespace `mercora`):

| Secret | Keys | Used by |
|---|---|---|
| `gateway-admin` | `ADMIN_USERNAME`, `ADMIN_PASSWORD`, optional `OPENROUTER_API_KEY` | first-admin bootstrap, chat |
| `notes-mcp-credentials` | `AUTH_TOKEN` | notes; the gateway's `notes` credential (`X-API-Key`) |
| `fx-mcp-credentials` | `AUTH_TOKEN` | fx; the gateway's `fx` credential (bearer) |
| `store-operator-gateway` | admin API key | operator registering store servers |

Per store, `store-<name>/woo-mcp-credentials` (`WOO_CONSUMER_KEY`,
`WOO_CONSUMER_SECRET`, `MCP_AUTH_TOKEN`), generated once by the operator.

**Registered upstreams in the gateway:** `notes`, `fx`, and one per store
(`shop3`, URL `http://woo-mcp-woo-mcp.store-<name>.svc.cluster.local:8000/mcp`,
bearer). The old `woo` server (the removed single store) was deleted.

**Gateway users:** `admin` (admin); members `test_user1`, `test_user2`,
`test_user3` (granted notes + fx) and `dave` (test member from the store
acceptance run, no grants left; can be deleted); `shop3-tester` (member used
for the Claude OAuth test).

**Claude Code connection (user scope):** `team-gateway` →
`http://gateway.localhost/mcp` with a member's key; it sees only that member's
granted servers. A direct `woo` connection (local woo-mcp) should be removed.

**Claude (claude.ai) connector:** `<ngrok URL>/mcp`, signed in by OAuth as
`shop3-tester`; listed under Dashboard → API keys → Connected apps.

### Rebuild and redeploy

```
cd team-mcp-gateway
docker build -t dashboard:0.1.2 services/dashboard     # or gateway, woo-mcp, demo-mcp, store-operator
k3d image import dashboard:0.1.2 -c store-check
helm upgrade team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --set image.tag=0.1.2
```

Bump the tag on every change (same tag + `IfNotPresent` keeps the old image).
Fresh install: see README, "The gateway → Install", "The dashboard" and "Store provisioning".

---

## 6. Verification

### Automated tests

| Suite | Command | Result |
|---|---|---|
| gateway | `cd services/gateway && uv run pytest` | 15 passed |
| woo-mcp | `cd services/woo-mcp && uv run pytest tests/tests_woo_mcp.py` | 8 passed |
| store-operator | `cd services/store-operator && uv run pytest` | 11 passed |
| dashboard | `cd services/dashboard && npx vite build` | builds |

Plain `pytest` in woo-mcp collects nothing, because the file is named
`tests_woo_mcp.py`; pass the path.

Gateway tests cover: login (wrong/right password, HttpOnly cookie), member vs
admin permissions, both credential kinds, prefixed listing, routing two `list`
tools, slow upstream timing out with a clear error while others keep working,
listing staying fast, dead upstream marked unreachable, disable hides tools,
edit keeps credential, remove, audit attribution, key revocation → 401,
credentials absent from API responses, dashboard tool endpoints, rate limit,
member grants (members see and call only granted servers; old members keep
their servers on upgrade), deleting users, the chat agent, the store API
(admin only, name rules, reserved names, 3-per-admin cap under concurrent
creates, off outside a cluster), refusing to start without a first admin,
in-place upgrade of an old database, and OAuth: the full flow (register, authorize,
login page, code + PKCE, token, `/mcp` call as that user), refused foreign
redirect URIs, wrong password on the login page, single-use refresh, Disconnect → 401,
`/api/connect`, everything off (404) without `PUBLIC_URL`, and the wrong-password
lock. Not tested: a token issued for another resource URL (the check is in
`oauth.user_for_oauth_token`).

Operator tests cover the generated manifests (quota, limits, policies, values,
priority class, namespace ownership, cap, progress, timeout) and the gateway
sync (create vs update of the store's server, delete).

Mutation checks were done: removing the call timeout and disabling the access
filter both made the tests fail, so the tests really check those paths.

### Live checks in the cluster

- (Original single store.) All three upstreams `ok`; 10 tools listed through `gateway.localhost/mcp`;
  notes, fx and woo calls succeed; no credential appears in `/api/servers`.
- fx scaled to 0: marked unreachable after 18 s, listing took 0.01 s, notes
  kept working, fx calls returned `Upstream 'fx' is unreachable (ConnectError).`
- Dashboard through `dash.localhost`: page loads, login, servers, tool run, audit row.
- Store provisioning: see the live run in 4.8.
- From Claude Code (member key, original store): created "Masala Chai Gift Box" (#19), deleted
  "Rolled Oats" (#20), both audited under `test_user2`.
- From claude.ai over ngrok: OAuth sign-in as `shop3-tester`, tools listed (4.9).

---

## 7. Problems found and how they were resolved

| Problem | Cause | Resolution |
|---|---|---|
| woo tools: `401 Consumer key is invalid` | Secret `woo-mcp-credentials` held a key the store no longer had; the working key was in `services/woo-mcp/.env` | user synced the Secret (auto mode blocks Secret writes) and restarted woo-mcp |
| `/mcp` in a browser: "a valid API key is required" | expected; `/mcp` is for MCP clients | dashboard built for people |
| Claude Code `ECONNREFUSED 127.0.0.1:8080` | wrong URL registered | re-register as `http://gateway.localhost/mcp` |
| Claude called `woo` instead of `team-gateway` | a direct woo-mcp connection still existed | remove the `woo` connection |
| Timeout errors read "unreachable (ReadTimeout)" | HTTP client timeout fired before ours | both timeouts now say "did not answer within Ns" |
| Operator: `ModuleNotFoundError store_operator`, then `No username set` | kopf's script misses /srv on sys.path; uid 10001 has no passwd entry | `PYTHONPATH=/srv`, `USER=store-operator` in the image |
| Store deletes hung in `Deleting` | node at 99% memory requests, restarted operator stayed Pending | PriorityClass `store-workload` on store pods |
| Old store PVC stuck `Terminating` after `helm uninstall` | Helm hook Job (not removed by uninstall) still used the PVC | delete the Job; operator installs set `setup.hook: false` |
| Gateway showed `woo` unreachable | its woo-mcp was uninstalled with the old store | server removed in the dashboard |
| Tools page needed raw JSON arguments | no types shown | typed form generated from the input schema |
| claude.ai connector impossible with API keys | Anthropic's servers call `/mcp` and need OAuth + public HTTPS | gateway became its own OAuth server; ngrok for the public URL (4.9) |
| `delete_product` reply says `publish` | real WooCommerce returns the product as it was before trashing | **open**: one-line fix in `services/woo-mcp/app/server.py` to report `trash` |

---

## 8. Known limits (deliberate shortcuts)

- The local k3d node (Docker Desktop memory) fits about two stores next to the
  rest of the stack; a third store's pods may wait for memory. Give Docker more
  memory for 3 stores per admin.
- Seeded orders are dated the day the store is created; on a later day "today" has none.
- The operator registers stores with an admin API key (Secret `store-operator-gateway`);
  deleting that admin or revoking the key stops registration.
- Store pods may be evicted when the node is full (lowest priority by design).
- The operator's ClusterRole has cluster-wide rights on Secrets, pods and
  Deployments (Helm needs them in each new namespace). Narrow it with
  per-namespace RoleBindings before prod.
- Store egress blocks the k3s pod/service CIDRs and cloud metadata; on another
  cluster the CIDRs in `store_operator/manifests.py` must change.

- Upstream credentials are stored in plain text in SQLite (encryption is on the hardening list).
- SQLite and the in-memory rate limit mean one gateway replica only.
- Login limit is per username, in memory (resets on restart; someone can lock a
  user out for 15 min by guessing). No SSRF check on server URLs.
- OAuth needs a stable public URL; the ngrok one is temporary and `publicUrl`
  must match it exactly. While ngrok runs, the whole gateway (incl. `/api`) is public.
- OAuth scopes are not used: a token gives the same access as the user's API key.
- No change-password.
- notes are kept in memory and vanish on restart.
- Chat history lives only in the browser tab; the reply is not streamed (the UI
  waits for the full answer); the "confirm before changes" rule is a prompt, not enforced in code.
- Access is per server, not per tool: a member granted a store can use all 6
  of its tools, including reading every order.
- Tools form: nested objects and multi-type fields fall back to a JSON box.

---

## 9. Client access: what works where

| Client | Connection made from | Works locally? |
|---|---|---|
| Claude Code | your laptop | yes (header with API key) |
| Claude Desktop local config via `npx mcp-remote ... --header` | your laptop | yes (not yet tried) |
| claude.ai / Desktop "custom connector" | Anthropic's servers | yes, via OAuth (4.9) while a public HTTPS URL (ngrok) points at the gateway |

The tunnel exposes `/api` too. For prod: a real domain with TLS, only `/mcp`,
`/authorize`, `/token`, `/register`, `/revoke`, `/oauth/login` and `/.well-known/*` public.

---

## 10. Next steps

1. Fix the `delete_product` status quirk (`services/woo-mcp/app/server.py`).
2. Rotate the API keys that were pasted into chat (dashboard → API keys).
3. Rebuild and redeploy the dashboard (typed Tools form).
4. Change-password. Optional: per-tool grants for members.
5. Done: ~~OAuth for Claude custom connectors (4.9)~~, ~~login rate limit~~, ~~chat agent (4.6)~~, ~~member access (4.7)~~, ~~multi-store provisioning (4.8)~~,
   ~~typed Tools form (4.5)~~, ~~customer role removed~~, ~~old single store cleaned up~~.
6. ROADMAP phase 7: VPS with a real domain and TLS, images in a registry,
   `values-prod` files, public `/mcp` only, private dashboard; record the demo.
7. Hardening: encrypt upstream credentials, SSRF check; persistent login limit.
