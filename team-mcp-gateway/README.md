# Team MCP Gateway on Kubernetes

WooCommerce hosting with a team MCP gateway. This scaffold contains the first
finished piece, the WooCommerce MCP server (layer 2), and the plan for the rest
in [ROADMAP.md](ROADMAP.md).

```
services/woo-mcp/     the MCP server: 6 tools over Streamable HTTP
  app/config.py       settings from environment variables
  app/woo.py          WooCommerce REST client, with OAuth 1.0a signing
  app/server.py       the six tools
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
  app/main.py         /mcp behind API keys, wiring
services/demo-mcp/    two small upstreams: notes and fx (both have a `list` tool)
charts/gateway/       Helm chart: Deployment (1 replica, SQLite on a PVC), Service, Ingress
charts/demo-mcp/      Helm chart, installed once per demo server
services/dashboard/   React + Vite dashboard, served by nginx (which forwards /api)
charts/dashboard/     Helm chart: nginx Deployment, Service, Ingress at dash.localhost
deploy/               local and prod values for every chart
```

## The six tools

| Tool                  | What it does                                                          |
| --------------------- | --------------------------------------------------------------------- |
| `list_products`       | Newest first, optional name search                                    |
| `create_product`      | Creates and publishes a simple product                                |
| `delete_product`      | Moves a product to the trash (restorable from wp-admin)               |
| `list_orders`         | Newest first; `on_date` = today, yesterday or a date; optional status |
| `get_order`           | One order with items, payment method and destination                  |
| `update_order_status` | Moves an order to a new status                                        |

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
docker build -t woo-mcp:0.1.0 services/woo-mcp
k3d image import woo-mcp:0.1.0 -c <your-cluster>

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
static credential, and hand out API keys. MCP clients use one URL,
`http://gateway.localhost/mcp`, with `Authorization: Bearer <api key>`.

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

Users are `admin` (manage servers, users, audit) or `member` (use tools, manage
their own keys). Browsers get an HttpOnly session cookie from `POST /api/login`.
API keys are shown once and only their hashes are stored. Every tool call writes
an audit row with the user, tool, server, result and duration.

| Endpoint | Who |
| --- | --- |
| `POST /api/login`, `POST /api/logout`, `GET /api/me` | anyone / logged in |
| `GET, POST /api/users` | admin |
| `GET, POST /api/keys`, `DELETE /api/keys/{id}` | own keys; admin sees all |
| `GET /api/servers` | logged in |
| `POST /api/servers`, `PATCH, DELETE /api/servers/{id}` | admin |
| `GET /api/audit?limit=100` | admin |

### Install (local k3d)

```
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=ADMIN_USERNAME=admin --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 12)"
kubectl -n mercora create secret generic notes-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic fx-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"

docker build -t gateway:0.1.0 services/gateway
docker build -t demo-mcp:0.1.0 services/demo-mcp
k3d image import gateway:0.1.0 demo-mcp:0.1.0 -c <your-cluster>

helm upgrade --install notes charts/demo-mcp -n mercora -f deploy/notes-values-local.yaml
helm upgrade --install fx    charts/demo-mcp -n mercora -f deploy/fx-values-local.yaml
helm upgrade --install team  charts/gateway  -n mercora -f deploy/gateway-values-local.yaml
```

Then log in and add the three servers. In-cluster URLs:
`http://woo-mcp-woo-mcp.mercora.svc.cluster.local:8000/mcp` (bearer = woo-mcp's
`MCP_AUTH_TOKEN`), `http://notes.mercora.svc.cluster.local:8000/mcp` (header
`X-API-Key`), `http://fx.mercora.svc.cluster.local:8000/mcp` (bearer). Each
token is in that server's Secret.

Gateway tests: `cd services/gateway && uv run pytest`.

## The dashboard

http://dash.localhost. Log in as `admin` (password in Secret `gateway-admin`).

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
docker build -t dashboard:0.1.0 services/dashboard
k3d image import dashboard:0.1.0 -c <your-cluster>
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml
```

Opening `/mcp` in a browser shows `a valid API key is required`. That is
expected: it is the endpoint for MCP clients, which send `Authorization: Bearer <key>`.
