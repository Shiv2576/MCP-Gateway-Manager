# Reference

Details behind the README: tools, gateway internals, API, OAuth and store provisioning.

## WooCommerce MCP server: the 23 tools

`services/woo-mcp`, one server per store. Write tools carry MCP `readOnlyHint: false`
(and `destructiveHint` for deletes and refunds); the gateway and chat agent rely on that.

| Group | Tools |
|---|---|
| Products | `list_products`, `create_product`, `delete_product` (to trash), `bulk_update_product_descriptions`, `bulk_update_product_prices` |
| Orders | `list_orders`, `get_order`, `update_order_status`, `bulk_update_orders`, `bulk_refund_orders` |
| Reports (read-only) | `get_sales_summary`, `get_refund_summary`, `get_best_selling_products`, `get_low_stock_products`, `audit_product_quality`, `get_customer_timeline` |
| Cart recovery | `get_abandoned_carts`, `create_cart_offers` |
| Inventory | `get_inventory_health`, `create_restock_plan`, `list_restock_orders`, `receive_stock`, `create_clearance_offers` |

Reports count days in the store's timezone and cap at 2,000 rows with `truncated: true` past that.
Bulk tools return one result per item, so a partial failure is visible.

## How woo-mcp talks to the store, and why

It calls the WooCommerce REST API (`/wp-json/wc/v3`) with a read/write API key.

- **Why not WP-CLI:** that needs a shell inside the WordPress pod, which means
  Kubernetes API rights for this server, or a second copy of WordPress and the
  database password here. REST needs only a network path and a key that can be
  revoked on its own.
- **The HTTP trap:** inside the cluster the call is plain HTTP, and WooCommerce
  accepts simple key-and-secret auth only over HTTPS. Over HTTP it requires an
  OAuth 1.0a signature on every request. `app/woo.py` signs each request.
- **Two URLs on purpose:** WooCommerce computes the signature from the site's
  own address, not the address the request arrived on. So `WOO_INTERNAL_URL` is
  where requests are sent (the Service) and `WOO_PUBLIC_URL` is the WordPress
  Site Address used for signing.

## Notes for building the gateway on this SDK (mcp 2.x)

- `FastMCP` is now `mcp.server.mcpserver.MCPServer`.
- Client with a credential header, exactly what the gateway needs per upstream:
  ```python
  http = httpx2.AsyncClient(headers={"Authorization": "Bearer ..."}, timeout=10)
  async with Client(streamable_http_client(url, http_client=http)) as c:
      tools = (await c.list_tools()).tools
      result = await c.call_tool("list_orders", {"on_date": "today"})
  ```
- `streamable_http_app(host=...)` defaults to `127.0.0.1`, which switches on a
  localhost-only Host-header allowlist. In a cluster pass `host="0.0.0.0"` or
  every request by Service name is rejected.
- `stateless_http=True` keeps no session, so several replicas work behind a
  plain Service.

## The gateway

Admins log in, add upstream MCP servers (Streamable HTTP) with an optional
static credential, and hand out API keys. One image runs in two modes: the
control plane (`GATEWAY_MODE=control`, the `/api` behind the dashboard) and any
number of gateway edges (`GATEWAY_MODE=edge`, `/mcp` for the servers attached
to one gateway). MCP clients use a gateway's URL,
`http://<slug>.gw.localhost/mcp`, with `Authorization: Bearer <api key>`, or
sign in through OAuth when that gateway has a public URL (see below). Every pod
uses one shared Postgres.

Gateways: create one on the Gateways page; the operator installs `gw-<slug>`
with two edge pods at `http://<slug>.gw.localhost/mcp`. Attach servers to it;
each edge serves only its attached servers. `kubectl -n mercora get mgw` shows
their phase.

### Tool names: the collision scheme

Every tool is shown as `<server>__<tool>`, where `<server>` is the name the
admin gave when adding the server: `woo__list_orders`, `notes__list`,
`fx__list`.

- Always prefixed, never only on a clash, so a tool's name never changes when
  another server is added later.
- Server names are `a-z 0-9 -` only, never `_`, so the first `__` always ends
  the server name and splitting a name is unambiguous.
- A server's name cannot be edited, because clients already use its tools' names.
  Remove the server and add it again to rename it.
- A tool whose full name would break MCP's rules (over 128 characters, bad
  characters) is hidden, and the server's status says how many were hidden.

### Slow and dead upstreams

- A background loop lists each enabled server's tools every 30s, all at once,
  each with a 5s timeout, and keeps the result with a status (`ok` /
  `unreachable` plus the reason). Tool listing reads only this saved copy, so it
  never waits on an upstream.
- A call goes straight to its upstream with a 10s timeout. Any failure comes
  back as a normal MCP tool error with one plain sentence, e.g.
  `Upstream 'fx' did not answer within 10s.`. Other servers are not affected.
- The server's enabled flag and the caller's API key are read from the database
  on every request: disabling hides tools at once, revoking stops the next call.

### Credentials

An upstream credential is a header (`{"type":"header","name":"X-API-Key","value":...}`)
or a bearer token (`{"type":"bearer","token":...}`). Only the gateway reads it,
to add it to its own outgoing requests. The API answers with `auth_header` and
`has_credential` only. An edit that leaves out `auth` keeps the stored one.
Headers from MCP clients are never forwarded upstream.

### Logins, keys, audit

Users are `admin` (manage servers, users, teams, audit; every tool on every
gateway) or `member` (use tools, manage their own keys). A member's tools on a
gateway are the union of their teams' allow-lists for it: one row per upstream
tool, no wildcards, so a tool an upstream adds later stays hidden until allowed.
No team, no tools. A detached server's tools are refused whatever the policies
say. The dashboard's Tools and Chat pages act through a chosen gateway and get
exactly its policy. A hidden tool's call answers "Unknown tool" and is audited. Browsers get an HttpOnly session cookie from `POST /api/login`.
API keys are shown once and only their hashes are stored. Every tool call writes
an audit row with the user, tool, server, result and duration.
Each user may make 20 tool calls per minute (`CALLS_PER_MINUTE`); refused
calls are audited too.

| Endpoint | Who |
| --- | --- |
| `GET /api/setup`, `POST /api/setup` | anyone; POST needs `SETUP_TOKEN`, once |
| `POST /api/login`, `POST /api/logout`, `GET /api/me` | anyone / logged in |
| `GET, POST /api/users` | admin |
| `GET, POST /api/keys`, `DELETE /api/keys/{id}` | own keys; admin sees all |
| `GET /api/servers` | logged in |
| `POST /api/servers`, `PATCH, DELETE /api/servers/{id}` | admin |
| `GET /api/audit?limit=100` | admin |
| `GET /api/teams` | admin: all; member: own |
| `POST /api/teams`, `DELETE /api/teams/{id}` | admin |
| `PUT /api/teams/{id}/members`, `PUT /api/teams/{id}/gateways/{slug}/tools` | admin |
| `GET /api/tools?gateway=`, `POST /api/tools/call` (`gateway` in the body) | logged in; that gateway's policy |
| `GET /api/gateways` | logged in |
| `POST /api/gateways` | admin (safe to retry) |
| `DELETE /api/gateways/{slug}` | admin |
| `PUT /api/gateways/{slug}/servers` | admin |
| `GET /api/oauth/connections`, `DELETE /api/oauth/connections/{user_id}/{client_id}` | own; admin sees all |

### OAuth (Claude custom connector)

Give a gateway a public https URL (the "public URL" field when creating it;
its edge gets `PUBLIC_URL`) and its `/mcp` also accepts OAuth tokens, so members can add the gateway to Claude as a
custom connector and sign in with their gateway username and password. API keys
keep working. The gateway is its own authorization server (`app/oauth.py`):
the MCP SDK serves `/register`, `/authorize`, `/token`, `/revoke` and the
`/.well-known/*` metadata; the gateway stores clients, codes and tokens (hashed)
in Postgres and serves the login page at `/oauth/login`.

- A token belongs to the user who signed in: their teams' tool policies, rate limit
  and audit rows apply as with an API key.
- Access tokens last 1 hour, refresh tokens 30 days and work once (rotated).
- Clients may register only Claude's callbacks (`OAUTH_REDIRECT_URIS`, default
  `https://claude.ai/api/mcp/auth_callback` and the `claude.com` one) or a
  loopback `http://localhost` URL (Claude Code, MCP Inspector).
- Dashboard → API keys → **Connected apps** lists signed-in apps; Disconnect
  ends access on the next call. Deleting a user deletes their tokens.

Claude connects from Anthropic's servers, so the URL must be reachable from
the internet. Trying it from k3d with ngrok:

```
ngrok http 80                        # the k3d ingress; copy the https://….ngrok-free.app URL
# Dashboard → Gateways → create one with public URL https://<id>.ngrok-free.app.
# Its edge Ingress also answers that host, so ngrok's Host header routes to it.
```

Then in Claude: Settings → Connectors → Add custom connector →
`https://<id>.ngrok-free.app/mcp`, sign in as a member, and ask for that
member's servers' tools. A free ngrok URL changes each run unless you use your
free static domain, and the gateway's public URL must match it. Stop ngrok when done: the
gateway is on the internet while it runs.


Known limits: Postgres is one replica with no backups: it is the demo's single
shared failure point (two edge replicas keep a gateway up, not the database).
Production needs backups and an HA Postgres. Edges are stateless MCP (no
sessions), so any replica answers any request; the rate limit is per user
across the organization, in fixed one-minute windows.

### Shared vs personal identity

Each server attached to a gateway is either **shared** (calls carry the
server's own credential, as set on the Servers page) or **personal** (each
member's call carries that member's own credential and nothing of the shared
one). To use personal identity, set the server's *personal credential header*
(for `woo-mcp`: `X-Woo-Credentials`), switch the attachment to Personal on the
Gateways page, and have each member add their own value under *My
credentials* (for `woo-mcp`: `<consumer_key>:<consumer_secret>` of a
WooCommerce REST key made while logged in as themselves). A member without
one gets an error; the shared credential is never used in its place. Values
are encrypted with `CREDENTIALS_KEY` and never shown again. Tool permissions
(teams minus per-member denies on the Members page) are checked first; the
identity only decides whose credential goes upstream.


## The dashboard

`services/dashboard` (React + Vite, served by nginx, which forwards `/api` to the
gateway). On a fresh install it asks for the `SETUP_TOKEN`, the organization name
and the first admin.

| Page | Who | What |
|---|---|---|
| Servers | all (edit: admin) | health dot and status every 3 s; add, edit, disable, remove; host a server from an image |
| Gateways | admin | create gateways (edges), attach servers, shared or personal identity, PII protection |
| Teams | all (edit: admin) | members, per-gateway tool allow-lists, write budgets, alert rules |
| Tools | all | every tool the chosen gateway gives you; run one with JSON arguments |
| Chat | all | the chat agent (README) |
| Stores | admin | create and delete WooCommerce stores |
| Alerts | all | team alerts and (admin) approval requests |
| Reports | admin | per-store report tools as tables |
| API keys | all | create (shown once) and revoke keys; connected OAuth apps |
| My credentials | all | own credentials for personal-identity servers |
| Members | admin | users, roles, per-member tool denies |
| PII Protection | admin | masking rules for tool results |
| Audit log | admin | every tool call: user, tool, server, result, duration |

Develop it against a running gateway: `cd services/dashboard && npm install && npm run dev`
(http://localhost:5173; Vite forwards `/api` to `http://gateway.localhost`).

## Store provisioning

Admins create and delete WooCommerce stores from the dashboard's **Stores** tab.
Each store gets its own namespace `store-<name>` (WordPress, MariaDB, PVCs,
Secrets, Ingress `http://<name>.localhost`, ResourceQuota, LimitRange,
NetworkPolicies) and its own woo-mcp, registered in the gateway as server
`<name>`, so its tools are `<name>__list_orders` and so on.

- The Stores tab lists each store with its status (Provisioning / Ready /
  Failed, with the reason), its URL once Ready, owner and created time,
  refreshing every 3 seconds. Several stores can provision at the same time;
  the operator handles each `Store` resource independently.
- Templates: `catalog-a` (40 everyday products, 90 days of sales history: the restock demo), `catalog-b` (300 products, other
  categories), `blank` (3 products). Each also seeds a few orders dated the day it is created.
- At most 3 stores per admin (10 on the VPS: `stores.maxPerAdmin` / `maxStores`); a store not Ready in 15 minutes is marked Failed with the reason.
- Deleting a store removes its gateway server first, then its namespace, which
  takes its PVCs (and their volumes), Secrets and Ingress with it.
  Deleting an admin deletes their stores.
- Isolation: each store namespace starts from a default-deny NetworkPolicy and
  allows only same-namespace paths: woo-mcp → its own WordPress, WordPress →
  its own MariaDB, plus DNS and Traefik. woo-mcp accepts traffic only from the
  gateway namespace and can send only to its own WordPress, so one store's MCP server
  cannot reach another store's WordPress or database.
- Recovery: every provisioning step is safe to repeat (namespace objects are
  created-if-missing, Helm runs `upgrade --install`, a release left pending by a
  killed Helm is rolled back, or uninstalled if it never succeeded, and then reinstalled). Failed steps are retried
  (3 attempts, 20 s apart); after the last
  attempt, or the 15-minute timeout, the store goes to Failed with the error.
  If the operator restarts mid-provisioning, kopf re-runs the unfinished
  handler and it converges or fails the same way.
- Members use only the tools their teams allow on a gateway (Teams page).

How it works: the gateway writes a `Store` resource; `services/store-operator`
(kopf) provisions it with Helm (`charts/store`, `charts/woo-mcp`) and registers
the server through the gateway API.


Watch: `kubectl -n mercora get st` (phase and current step), `kubectl -n mercora logs deploy/store-operator`,
`kubectl -n store-<name> logs job/store-store-setup`.
