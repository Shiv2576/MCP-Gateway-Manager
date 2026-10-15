# Team MCP Gateway on Kubernetes

WooCommerce hosting with a team MCP gateway. This scaffold contains the first
finished piece, the WooCommerce MCP server (layer 2), and the plan for the rest
in [ROADMAP.md](ROADMAP.md).

```
services/woo-mcp/     the MCP server: 12 tools over Streamable HTTP
  app/config.py       settings from environment variables
  app/woo.py          WooCommerce REST client, with OAuth 1.0a signing
  app/server.py       the tools: six actions, six reports
  app/main.py         web app: /mcp behind a bearer token, /healthz, /readyz
  app/check.py        one command to test auth against your real store
  tests/              a fake store plus end-to-end tests
charts/store/         the store: Bitnami WordPress + MariaDB, Ingress, and a setup Job
  files/setup-woo.sh  run by the Job: WooCommerce, permalinks, cash on delivery, products
  files/register-woo-key.php  creates the store API key from a Secret
charts/woo-mcp/       Helm chart: Deployment, ClusterIP Service, NetworkPolicy
services/gateway/     the gateway: one MCP endpoint, logins, API keys, audit log
  app/upstream.py     health loop, tool listing, call_tool (route, timeout, audit)
  app/api.py          admin REST API
  app/main.py         /mcp behind API keys or OAuth tokens, wiring
  app/oauth.py        OAuth authorization server for /mcp and its login page
services/demo-mcp/    two small upstreams: notes and fx (both have a `list` tool)
charts/gateway/       Helm chart: Deployment (1 replica, SQLite on a PVC), Service, Ingress
charts/demo-mcp/      Helm chart, installed once per demo server
services/dashboard/   React + Vite dashboard, served by nginx (which forwards /api)
charts/dashboard/     Helm chart: nginx Deployment, Service, Ingress at dash.localhost
deploy/               local and prod values for every chart
```

## The tools

| Tool                  | What it does                                                          |
| --------------------- | --------------------------------------------------------------------- |
| `list_products`       | Newest first, optional name search                                    |
| `create_product`      | Creates and publishes a simple product                                |
| `delete_product`      | Moves a product to the trash (restorable from wp-admin)               |
| `list_orders`         | Newest first; `on_date` = today, yesterday or a date; optional status |
| `get_order`           | One order with items, payment method and destination                  |
| `update_order_status` | Moves an order to a new status                                        |

Reports (read-only, days in the store's timezone, capped at 2,000 rows with `truncated: true` past that):

| Tool                        | What it does                                                                 |
| --------------------------- | ---------------------------------------------------------------------------- |
| `get_sales_summary`         | Gross, refunds, net, order count, average order value (processing, completed, on-hold, refunded) |
| `get_refund_summary`        | Refunds by refund date, with order and reason                               |
| `get_best_selling_products` | Ranked by units or revenue                                                   |
| `get_low_stock_products`    | Stock-tracked products under a threshold (parent level, not variations)      |
| `audit_product_quality`     | Missing description, image, price or category ("Uncategorized" counts)       |
| `get_customer_timeline`     | One customer's orders, oldest first, by customer id or billing email         |

Give teams access per tool in the dashboard's team policies (e.g. Finance gets the sales and refund summaries).

## How it talks to the store, and why

It calls the WooCommerce REST API (`/wp-json/wc/v3`) with a read/write API key.

- **Why not WP-CLI:** that needs a shell inside the WordPress pod, which means
  Kubernetes API rights for this server, or a second copy of WordPress and the
  database password here. REST needs only a network path and a key that can be
  revoked on its own.
- **The HTTP trap:** inside the cluster the call is plain HTTP, and WooCommerce
  accepts simple key-and-secret auth only over HTTPS. Over HTTP it requires an
  OAuth 1.0a signature on every request. `app/woo.py` signs each request.
- **Two URLs on purpose:** WooCommerce computes the signature from the site's
  own address, not the address the request arrived on. So `
` is
  where requests are sent (the Service) and `WOO_PUBLIC_URL` is the WordPress
  Site Address used for signing.

## Run the tests

```
cd services/woo-mcp
pip install -r requirements-dev.txt
python -m tests.test_woo_mcp
```

They start a fake store and this server on real local ports and drive them
with the SDK's own MCP client.

## Check against your real store

See the header of `services/woo-mcp/app/check.py`. Do this first.

## Build and install (local k3d)

Store first. The Secret holds the API key the setup Job registers in WooCommerce,
and woo-mcp later reads the same Secret, so there is only one copy of the key.

```
k3d cluster create mercora -p "80:80@loadbalancer"
kubectl create namespace mercora
kubectl -n mercora create secret generic woo-mcp-credentials \
  --from-literal=WOO_CONSUMER_KEY=ck_$(openssl rand -hex 20) \
  --from-literal=WOO_CONSUMER_SECRET=cs_$(openssl rand -hex 20) \
  --from-literal=MCP_AUTH_TOKEN="$(openssl rand -hex 24)"

helm dependency build charts/store
helm upgrade --install mercora charts/store -n mercora \
  -f deploy/store-values-local.yaml --wait --timeout 15m
kubectl -n mercora logs job/mercora-store-setup    # ends with "store setup done"
```

Open http://store.localhost. Admin is `admin`; the password is in the
`mercora-wordpress` Secret under `wordpress-password`.

Then the MCP server:

```
docker build -t woo-mcp:0.3.0 services/woo-mcp
k3d image import woo-mcp:0.3.0 -c <your-cluster>

helm lint charts/woo-mcp -f deploy/values-local.yaml
helm upgrade --install woo-mcp charts/woo-mcp -n mercora -f deploy/values-local.yaml
```

Production uses the same chart with `-f deploy/values-prod.yaml`.

## What has and has not been verified

Verified here: all eight tests pass against MCP SDK 2.2.0 (tool listing in both
protocol eras, the create-product and today's-orders flow, token rejection,
timeouts, dead store, and the signer agreeing with `oauthlib`).

Not verified here, because the build sandbox had no Docker daemon, no Helm and
no WooCommerce: the image build, `helm lint`/install, signing against a real
WooCommerce, and `register-woo-key.php`. Phase 0 and Phase 2 of the roadmap
cover exactly these.

One thing to watch in production: if WordPress redirects plain-HTTP requests
to HTTPS, in-cluster calls will be redirected too. If that happens, set
`store.authMode: basic` and point `internalUrl` at the HTTPS address.

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

### Install (local k3d)

```
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW \
  --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=SETUP_TOKEN=$(openssl rand -hex 16)      # add OPENROUTER_API_KEY for chat
# Encrypts members' own upstream credentials (personal identity). Changing it later
# makes every stored one unreadable: members enter theirs again.
kubectl -n mercora create secret generic gateway-credentials \
  --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
kubectl -n mercora create secret generic notes-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic fx-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"

helm upgrade --install db charts/postgres -n mercora -f deploy/postgres-values-local.yaml --wait
docker build -t gateway:0.5.0 services/gateway && k3d image import gateway:0.5.0 -c <cluster>
docker build -t demo-mcp:0.1.0 services/demo-mcp && k3d image import demo-mcp:0.1.0 -c <cluster>
helm upgrade --install notes charts/demo-mcp -n mercora -f deploy/notes-values-local.yaml
helm upgrade --install fx    charts/demo-mcp -n mercora -f deploy/fx-values-local.yaml
helm upgrade --install team charts/gateway -n mercora -f deploy/gateway-values-local.yaml
# dashboard (team-ui) as before with tag 0.5.0; open http://dash.localhost and finish setup
# with the SETUP_TOKEN, then make an admin API key and:
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY=<key>
docker build -f services/store-operator/Dockerfile -t store-operator:0.3.1 . && k3d image import store-operator:0.3.1 -c <cluster>
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml
```

Known limits: Postgres is one replica with no backups: it is the demo's single
shared failure point (two edge replicas keep a gateway up, not the database).
Production needs backups and an HA Postgres. Edges are stateless MCP (no
sessions), so any replica answers any request; the rate limit is per user
across the organization, in fixed one-minute windows.

Then log in and add the three servers. In-cluster URLs:
`http://woo-mcp-woo-mcp.mercora.svc.cluster.local:8000/mcp` (bearer = woo-mcp's
`MCP_AUTH_TOKEN`), `http://notes.mercora.svc.cluster.local:8000/mcp` (header
`X-API-Key`), `http://fx.mercora.svc.cluster.local:8000/mcp` (bearer). Each
token is in that server's Secret.

Gateway tests: `cd services/gateway && uv run pytest`.

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

http://dash.localhost. On a fresh install it asks for the `SETUP_TOKEN` (Secret
`gateway-admin`), the organization name and the first admin; then log in.

- **Servers:** health dot and status, refreshed every 3s; add, edit, disable or
  remove a server; expand a server to see its tools.
- **Tools:** every tool the gateway exposes. Pick one, edit the JSON
  arguments, and run it. Runs are audited under your user.
- **API keys:** create a key (shown once) for an MCP client, or revoke one.
- **Members** and **Audit log:** admins only.

Develop it locally against the cluster's gateway: `cd services/dashboard && npm
install && npm run dev`, then open http://localhost:5173. Vite forwards `/api`
to `http://gateway.localhost`.

Build and install:

```
docker build -t dashboard:0.5.0 services/dashboard
k3d image import dashboard:0.5.0 -c <your-cluster>
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml
```

Opening `/mcp` in a browser shows `a valid API key or OAuth token is required`. That is
expected: it is the endpoint for MCP clients, which send `Authorization: Bearer <key or token>`.

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
- At most 3 stores per admin; a store not Ready in 15 minutes is marked Failed with the reason.
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

Install (once; from this folder):

```bash
docker build -f services/store-operator/Dockerfile -t store-operator:0.3.1 .
k3d image import store-operator:0.3.1 -c store-check
# An admin API key for the operator: dashboard → API keys → "store-operator".
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='<key>'
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml
helm upgrade --install team charts/gateway -n mercora -f deploy/gateway-values-local.yaml   # stores.enabled: true
```

Watch: `kubectl -n mercora get st` (phase and current step), `kubectl -n mercora logs deploy/store-operator`,
`kubectl -n store-<name> logs job/store-store-setup`.
