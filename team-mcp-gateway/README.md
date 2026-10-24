# Team MCP Gateway on Kubernetes

WooCommerce hosting with a team MCP gateway. Admins create stores from a
dashboard; each store gets its own WooCommerce MCP server, and a team uses all
of them, plus any other MCP server, through one gateway with logins, per-team
tool policies, audit, write budgets and a chat agent.

| Doc | What |
|---|---|
| [docs/design.md](docs/design.md) | System design and tradeoffs: architecture, aggregation, routing, failures, production |
| [docs/threat-model.md](docs/threat-model.md) | Threat model |
| [docs/reference.md](docs/reference.md) | Tools, gateway internals, API endpoints, OAuth, store provisioning details |
| [docs/deploy-vps.md](docs/deploy-vps.md) | Production-like setup on a VPS, step by step |
| [docs/upgrades.md](docs/upgrades.md) | Upgrades, rollback and backups |
| [docs/cluster-architecture.md](docs/cluster-architecture.md) | Every object in the cluster, with diagrams |

## Repository

```
services/gateway/         gateway: /mcp (edges), /api for the dashboard, OAuth server, chat agent
  app/upstream.py         health loop, tool listing, call_tool (the one path for every call)
  app/chat.py             chat agent
  app/api.py, teams.py    logins, users, API keys, servers, teams and policies
  app/oauth.py            OAuth authorization server for Claude custom connectors
  app/approvals.py, alerts.py, pii.py   write budgets and approvals, team alerts, PII filter
services/dashboard/       React + Vite dashboard, served by nginx
services/woo-mcp/         WooCommerce MCP server: 23 tools over Streamable HTTP
services/store-operator/  kopf operator: stores, gateway edges, hosted MCP servers
services/demo-mcp/        two small MCP servers (notes, fx) for testing
charts/                   Helm: gateway, dashboard, postgres, store, woo-mcp, store-operator, demo-mcp
deploy/                   values files: *-local.yaml (k3d) and *-prod.yaml (VPS)
scripts/deploy-vps.sh     build, copy and upgrade everything on the VPS
```

## Local setup (k3d)

Needs Docker, k3d, kubectl, Helm and Python 3. Run from this folder.

```sh
# 1. Cluster
k3d cluster create store-check -p "80:80@loadbalancer"
kubectl create namespace mercora

# 2. Secrets (random values; none are in git)
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW \
  --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=SETUP_TOKEN=$(openssl rand -hex 16) \
  --from-literal=OPENROUTER_API_KEY=sk-or-...          # chat agent; omit to run without chat
kubectl -n mercora create secret generic gateway-credentials \
  --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
kubectl -n mercora create secret generic notes-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic fx-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"

# 3. Images (tags = chart defaults)
helm dependency build charts/store
docker build -t gateway:0.10.3 services/gateway
docker build -t dashboard:0.9.8 services/dashboard
docker build -t woo-mcp:0.5.0 services/woo-mcp
docker build -t demo-mcp:0.1.0 services/demo-mcp
docker build -t store-operator:0.7.2 -f services/store-operator/Dockerfile .
k3d image import -c store-check gateway:0.10.3 dashboard:0.9.8 woo-mcp:0.5.0 demo-mcp:0.1.0 store-operator:0.7.2

# 4. Install
helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-local.yaml --wait
helm upgrade --install notes   charts/demo-mcp  -n mercora -f deploy/notes-values-local.yaml
helm upgrade --install fx      charts/demo-mcp  -n mercora -f deploy/fx-values-local.yaml
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-local.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --wait

# 5. First admin: open http://dash.localhost and enter this token, org name, admin login
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# 6. Operator: proves itself to the gateway with its ServiceAccount token, no key to paste
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml --wait

# 7. Check
kubectl -n mercora get pods
```

Then in the dashboard: **Stores** → create `shop1` (`catalog-a`; Ready in ~5–10 min at
http://shop1.localhost) → **Gateways** → create `prod` and attach `shop1`
(MCP endpoint http://prod.gw.localhost/mcp) → **Teams** → give members tools.

Tests:

```sh
(cd services/woo-mcp && pip install -r requirements-dev.txt && python -m pytest -q tests/tests_woo_mcp.py)
(cd services/store-operator && uv run pytest -q)
docker run -d --name gw-test-pg -e POSTGRES_PASSWORD=t -p 5433:5432 postgres:17-alpine
(cd services/gateway && TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q)
```

## VPS setup (k3s)

One Ubuntu VPS (2 vCPU, 8 GB, x86_64), ports 22, 80 and 443 open. Names come from
[sslip.io](https://sslip.io), certificates from Let's Encrypt. Background and
troubleshooting (IPv6-only/LXC hosts included): [docs/deploy-vps.md](docs/deploy-vps.md).

**On the VPS** — k3s, Helm, cert-manager:

```sh
curl -sfL https://get.k3s.io | sh -
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc && . ~/.bashrc
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait
```

**On your laptop** — set the domain, build amd64 images, copy everything:

```sh
VPS_IP=203.0.113.7
BASE=203-0-113-7.sslip.io                              # the IP with dashes + .sslip.io
sed -i '' "s/2001-41d0-303-2774--94\.sslip\.io/$BASE/g" deploy/*-prod.yaml
sed -i '' "s/email: .*#/email: you@yourmail.com  #/" deploy/tls-prod.yaml   # Let's Encrypt notices

helm dependency build charts/store
mkdir -p /tmp/img
b() { docker buildx build --platform linux/amd64 -o type=docker,dest=/tmp/img/$1.tar -t $1:$2 "${@:3}"; }
b gateway        0.10.3 services/gateway
b dashboard      0.9.8  services/dashboard
b woo-mcp        0.5.0  services/woo-mcp
b demo-mcp       0.1.0  services/demo-mcp
b store-operator 0.7.2  -f services/store-operator/Dockerfile .

rsync -a --exclude .git --exclude node_modules --exclude .venv ./ root@$VPS_IP:/opt/team-mcp-gateway/
scp /tmp/img/*.tar root@$VPS_IP:/tmp/
ssh root@$VPS_IP 'for f in /tmp/*.tar; do k3s ctr images import $f; done'
```

**On the VPS** — TLS, Secrets, install:

```sh
cd /opt/team-mcp-gateway
kubectl apply -f deploy/tls-prod.yaml
kubectl -n kube-system get certificate -w             # READY True within ~2 min

kubectl create namespace mercora
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW \
  --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=SETUP_TOKEN=$(openssl rand -hex 16) \
  --from-literal=OPENROUTER_API_KEY=sk-or-...
kubectl -n mercora create secret generic gateway-credentials \
  --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
kubectl -n mercora create secret generic notes-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic fx-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"

helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-prod.yaml --wait
helm upgrade --install notes   charts/demo-mcp  -n mercora -f deploy/notes-values-local.yaml
helm upgrade --install fx      charts/demo-mcp  -n mercora -f deploy/fx-values-local.yaml
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-prod.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-prod.yaml --wait

# First admin, right away (setup is public until done): open https://dash.$BASE
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# Operator: proves itself to the gateway with its ServiceAccount token, no key to paste
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-prod.yaml --wait

kubectl get pods -A; kubectl get certificate -A
```

Then in the dashboard: **Stores** → create a store (at `https://<name>.$BASE`) →
**Gateways** → create `prod` with public URL `https://prod.gw.$BASE` (turns on
OAuth for Claude) and attach the store.

Later updates, from the laptop: `scripts/deploy-vps.sh` (shows the changes, asks,
then builds, copies, imports and runs every `helm upgrade`; set `VPS`, `PORT`,
`KEY` for your server). Rollback: [docs/upgrades.md](docs/upgrades.md).

## Using it

### Place an order

Open the store (http://shop1.localhost, or `https://shop1.<base>` on the VPS),
add a product to the cart, go to **Checkout**, fill in the address, choose
**Cash on delivery** (enabled by the store setup) and **Place order**. Then see
it through MCP: Tools page, or chat, `shop1__list_orders` with
`{"on_date": "today"}`. Orders can be moved on with `update_order_status`.

### Connect an external MCP client

Every gateway's endpoint is `http://<slug>.gw.localhost/mcp` (local) or
`https://<slug>.gw.<base>/mcp` (VPS), Streamable HTTP.

- **With an API key** (Claude Code, MCP Inspector, scripts): dashboard →
  **API keys** → create one (shown once), then

  ```sh
  claude mcp add --transport http shop http://prod.gw.localhost/mcp \
    --header "Authorization: Bearer mcpg_..."
  ```

- **With OAuth** (claude.ai / Claude Desktop custom connectors, which cannot
  send a header): create the gateway with a public https URL. Then in Claude →
  Settings → Connectors → **Add custom connector** → `https://<slug>.gw.<base>/mcp`,
  and sign in with your gateway username and password. Locally, expose port 80
  with ngrok first ([reference](docs/reference.md#oauth-claude-custom-connector)).

Either way the client sees only the tools that user's teams allow on that
gateway, named `<server>__<tool>`.

### Use the chat agent

Dashboard → **Chat**, pick a gateway, and ask in plain language: "what sold best
last week in shop1?", "which products are low on stock?". The agent calls
tools as you, through the chosen gateway, so it has exactly your tools,
rate limit and audit trail.

- Before any change (create a product, refund, update orders) it shows the
  exact call and waits for **Confirm**; nothing is written without it.
- With several stores it asks which one if you did not say.
- Off-topic requests and attempts to change its rules are refused.
- Needs `OPENROUTER_API_KEY` in Secret `gateway-admin`; the model is
  `llmModel` in the gateway chart (`openai/gpt-4.1-mini`).

## Source code

| Part | Where |
|---|---|
| Dashboard | `services/dashboard/src` |
| Backend and gateway | `services/gateway/app` |
| WooCommerce MCP server | `services/woo-mcp/app` |
| Chat agent | `services/gateway/app/chat.py`, UI in `services/dashboard/src/Chat.jsx` |
| Store operator | `services/store-operator/store_operator` |
| Helm charts | `charts/*`; values per environment in `deploy/*-local.yaml` and `deploy/*-prod.yaml` |

## Notes

- [System design and tradeoffs](docs/design.md)
- [Threat model](docs/threat-model.md)

## Stand-out items attempted

| Item | Where |
|---|---|
| Store provisioning from the dashboard: one namespace per store, own woo-mcp, quotas, NetworkPolicies, auto-registered in the gateway | `services/store-operator`, `services/gateway/app/stores.py`, `charts/store`, `charts/woo-mcp` |
| Several gateways, each a separate edge deployment with its own servers | `services/gateway/app/gateways.py`, operator `McpGateway` |
| OAuth 2.1 + PKCE so Claude custom connectors work | `services/gateway/app/oauth.py` |
| Personal identity: a member's own upstream credential instead of the shared one | `services/gateway/app/credentials.py`, Gateways and My credentials pages |
| Per-tool write budgets with admin approval for bigger jobs | `services/gateway/app/approvals.py`, Alerts page |
| Team alerts when a tool call succeeds | `services/gateway/app/alerts.py`, Teams and Alerts pages |
| PII masking on tool results, per gateway | `services/gateway/app/pii.py`, PII page |
| Hosted MCP servers: run any image as an upstream in its own namespace | `services/gateway/app/hosted.py`, operator `HostedServer` |
| Chat agent guardrails: signed single-use confirmation for writes, schema-checked arguments, injection check | `services/gateway/app/chat.py` |
| Store analytics, cart recovery and restock tools | `services/woo-mcp/app/server.py`, Reports page (admins) |
| Production-like deployment with TLS on k3s, plus upgrade and rollback runbook | `docs/deploy-vps.md`, `docs/upgrades.md`, `scripts/deploy-vps.sh` |
