<h1 align="center">MCP-Gateway-Manager</h1>

<p align="center">
  <img src="MCP-Gateway.png" alt="MCP-Gateway-Manager architecture" width="800">
</p>

## Local setup (k3d)

Needs Docker, k3d, kubectl, Helm, Python 3. Run in `team-mcp-gateway/`.

```sh
k3d cluster create store-check -p "80:80@loadbalancer" && kubectl create namespace mercora

# Secrets (random, none in git); omit OPENROUTER_API_KEY to run without chat
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin --from-literal=SETUP_TOKEN=$(openssl rand -hex 16) \
  --from-literal=OPENROUTER_API_KEY=sk-or-...
kubectl -n mercora create secret generic gateway-credentials \
  --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"

# Images (tags = chart defaults)
helm dependency build charts/store
docker build -t gateway:0.10.4 services/gateway
docker build -t dashboard:0.9.8 services/dashboard
docker build -t woo-mcp:0.5.0 services/woo-mcp
docker build -t store-operator:0.7.5 -f services/store-operator/Dockerfile .
k3d image import -c store-check gateway:0.10.4 dashboard:0.9.8 woo-mcp:0.5.0 store-operator:0.7.5

helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-local.yaml --wait
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-local.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --wait

# First admin: open http://dash.localhost, enter this token, org name, admin login
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# Operator: dashboard → API keys → create "store-operator", paste it below
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='mcpg_...'
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml --wait
kubectl -n mercora get pods
```

Then: **Stores** → create `shop1` (`catalog-a`, Ready in ~5–10 min at http://shop1.localhost) →
**Gateways** → create `prod` → **Servers** → tick `shop1` → **Save** (MCP at http://prod.gw.localhost/mcp) → **Teams** → give members tools.

Tests:

```sh
(cd services/woo-mcp && uv run pytest -q tests/tests_woo_mcp.py)
(cd services/store-operator && uv run pytest -q)
docker run -d --name gw-test-pg -e POSTGRES_PASSWORD=t -p 5433:5432 postgres:17-alpine
(cd services/gateway && TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q)
```

## VPS setup (k3s)

One Ubuntu VPS (2 vCPU, 8 GB, x86_64), ports 22/80/443 open. Hostnames from sslip.io, certificates from
Let's Encrypt. IPv6-only/LXC hosts and troubleshooting: `docs/deploy-vps.md`.

**VPS** — k3s, Helm, cert-manager:

```sh
curl -sfL https://get.k3s.io | sh -
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc && . ~/.bashrc
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait
```

**Laptop** (in `team-mcp-gateway/`; needs Docker with buildx, Helm, rsync) — set domain, build amd64 images, copy:

```sh
VPS_IP=203.0.113.7; BASE=203-0-113-7.sslip.io     # IP with dashes + .sslip.io; on Linux use sed -i without ''
sed -i '' "s/2001-41d0-303-2774--94\.sslip\.io/$BASE/g" deploy/*-prod.yaml
sed -i '' "s/email: .*#/email: you@yourmail.com  #/" deploy/tls-prod.yaml      # Let's Encrypt notices; not example.*
sed -i '' '/^tailnetDomain:/d' deploy/gateway-values-prod.yaml                # Tailscale off (on: docs/deploy-vps.md)

helm dependency build charts/store && mkdir -p /tmp/img
b() { docker buildx build --platform linux/amd64 -o type=docker,dest=/tmp/img/$1.tar -t $1:$2 "${@:3}"; }
b gateway 0.10.4 services/gateway; b dashboard 0.9.8 services/dashboard; b woo-mcp 0.5.0 services/woo-mcp
b store-operator 0.7.5 -f services/store-operator/Dockerfile .

rsync -a --exclude .git --exclude node_modules --exclude .venv ./ root@$VPS_IP:/opt/team-mcp-gateway/
scp /tmp/img/*.tar root@$VPS_IP:/tmp/
ssh root@$VPS_IP 'for f in /tmp/*.tar; do k3s ctr images import $f; done'
```

**VPS** — TLS, Secrets, install:

```sh
cd /opt/team-mcp-gateway
kubectl apply -f deploy/tls-prod.yaml && kubectl -n kube-system get certificate -w   # wait for READY True (~2 min), Ctrl-C

kubectl create namespace mercora
# Same three Secrets as local: gateway-db, gateway-admin, gateway-credentials (copy those commands)

helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-prod.yaml --wait
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-prod.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-prod.yaml --wait

# First admin right away (setup is public until done): https://dash.$BASE
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# Dashboard → API keys → create "store-operator", paste it below
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='mcpg_...'
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-prod.yaml --wait
kubectl get pods -A; kubectl get certificate -A
```

Then: **Stores** → create a store (`https://<name>.$BASE`) → **Gateways** → create `prod` with public URL
`https://prod.gw.$BASE` (turns on OAuth for Claude), attach the store.

Updates from the laptop: `scripts/deploy-vps.sh` (shows changes, asks, then builds, copies, imports, runs every
`helm upgrade`; set `VPS`, `PORT`, `KEY`). Rollback: `docs/upgrades.md`.

## Using the online store

Dashboard: `https://dash.2001-41d0-303-2774--94.sslip.io` (local: `http://dash.localhost`). Log in with your gateway
username and password.

### Create and open a store

1. **Stores** → enter a name → pick **Catalog A**, **Catalog B** or **Blank** → **Create store**.
2. Wait for **Ready** (~5–10 min).
3. Open the store from its link on **Stores** (the WordPress admin login is shown there too).

### Place an order

1. In the store: pick a product → **Add to cart** → **View cart** → **Proceed to checkout**.
2. Fill in name, address, email, phone → **Cash on delivery** → **Place order**.
3. Back in the dashboard: **Tools** → pick the gateway → `<store>__list_orders` with `{"on_date": "today"}` → **Run tool**.
4. To complete it: `<store>__update_order_status` with `{"order_id": <id>, "status": "completed"}` → **Run tool**.

### Connect an external MCP client

1. **Gateways** → **Copy** the gateway's MCP URL. No gateway yet? Enter slug + name → **Create gateway** →
   **Servers** → tick the store → **Save**.
2. **API keys** → create a key and copy it (shown once, `mcpg_...`).
3. Claude Code:
   ```sh
   claude mcp add --transport http shop <MCP URL> --header "Authorization: Bearer mcpg_..."
   ```
   claude.ai / Claude Desktop: **Settings** → **Connectors** → **Add custom connector** → paste the MCP URL →
   **Connect** → sign in with your gateway username and password.
4. **API keys** → **Connected apps** lists connected clients; **Disconnect** revokes one.

The client sees only the tools its teams allow on that gateway, named `<store>__<tool>`.

### Use the chat agent

1. **Chat** → pick the gateway.
2. Ask in plain language: "Today's orders in shop1?", "Which products are low on stock?", "Best sellers over the
   last 30 days".
3. For any change (new product, order update, refund) it shows the exact call; nothing is written until you click
   **Confirm**.

## Design and trade-offs

| Choice                                       | Why                                                                                                                                                                                                                                                                                                              | Trade-off                                                                  |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Control plane + edges (one image, two modes) | Control runs the dashboard API, chat and provisioning; each gateway's edge pods serve only `/mcp`, so public traffic can't take down admin. Control can only create our custom resources; the operator does the real provisioning; edges have no Kubernetes token.                                               | More moving parts; each edge has its own 30 s tool cache.                  |
| Stateless edges and chat                     | No MCP sessions: each gateway runs identical replicas, and any replica of a gateway can serve any request for that gateway’s tools; it only accepts tools from the servers attached to that gateway. Restarts don’t break clients. Chat uses the same call_tool path as /mcp, so policy and audit are identical. | No server push (e.g. tools-changed notifications).                         |
| One central Postgres                         | Single source of truth (users, keys, policies, servers, encrypted credentials, audit); a revoked key or disabled server applies on the next request.                                                                                                                                                             | Single point of failure and bottleneck. Next: HA Postgres (CloudNativePG). |
| Write budgets + admin approval               | Per member and tool, per rolling hour; going over turns the call into an approval request instead of a plain failure.                                                                                                                                                                                            | A big job waits for an admin.                                              |

**Routing:** every 30 s each edge fetches `tools/list` from its enabled servers in parallel (5 s timeout each) and
caches it. Tools are named `<server>__<tool>`; `tools/list` is served from cache, filtered by the caller's policy.
`tools/call` finds the server from the name, re-checks permission, rate limit and budget, adds the upstream credential
server-side, and audits.

**Failures:** a dead upstream costs 5 s in the background loop, never on a request, and shows as Unreachable with the
reason. Each call has a 10 s timeout and fails alone. Permission is checked live on every call, so a stale cache can't
grant access. The audit row is written before the call, so a crash still leaves a trace.

**Threat model:**

- Shared credentials: a member never sees the key, but can do anything its allowed tools do with it, and upstream sees
  one identity; only the gateway audit says who. Echoed keys are scrubbed from results; personal identity fixes this.
- Client tokens are never forwarded upstream: a gateway token sent on would let any upstream replay it against the
  gateway or other servers, and the upstream would trust a token not meant for it.
- Untrusted upstreams: tool descriptions reach the model as written (only a `[server]` prefix), so a malicious one can
  steer it. Only admins add servers, tools are allow-listed one by one, and writes in chat need a click to Confirm.
- Member vs member: no access to another's keys or credentials, audit is admin-only, limits and budgets are per member.
  On a shared credential, though, one member's writes (prices, refunds) change the same store everyone uses.

**Production:** same charts, only `*-local.yaml` → `*-prod.yaml` values change.

- DNS: `*.localhost` → sslip.io (VPS) → a real domain.
- Ingress: Traefik both; prod adds cert-manager + Let's Encrypt and HTTP→HTTPS redirect.
- Storage: `local-path` (one node's disk) → replicated or cloud block storage, with backups.
- Secrets: created by hand, never in charts → k3s secrets encryption or Vault/KMS; rotate `CREDENTIALS_KEY` with MultiFernet.

<h2 align="center">Stand Out Features (Optional)</h2>

> [!IMPORTANT]
> **Live dashboard:** [https://dash.2001-41d0-303-2774--94.sslip.io/](https://dash.2001-41d0-303-2774--94.sslip.io/)
> **Username:** `admin` · **Password:** `admin@1234`

### 1. Production-like VPS deployment

- **Images + deploy:** the same Helm charts run on a k3s VPS. Images are built for amd64 into tarballs, copied with
  `scp`, loaded with `k3s ctr images import` (no registry), then installed with `helm upgrade --install`.
- **Helm values:** only `*-local.yaml` → `*-prod.yaml` changes: domain `*.localhost` → sslip.io hosts, Traefik ingress
  with HTTPS, `local-path` storage. Secrets are created by hand in the cluster with `kubectl create secret`, never in git.
- **TLS:** cert-manager gets a Let's Encrypt certificate through a `ClusterIssuer` (HTTP-01 via Traefik). A Traefik
  `TLSStore` makes it the default certificate for every host, and Traefik redirects HTTP to HTTPS.

### 2. OAuth server for Claude custom connector

_Description:_ explain PKCE challenge authorization code and acess + refresh token swap between claude and gateway services in short

**Video:** https://youtu.be/k6-_-3qR1D0

### 3. Tool and team provisioning

_Description:_ explain how we can define teams and add members with specific mcp tool calls , how we can also do individual memebr tool governence

### 4. Rate limit and write budget

_Description: explain why we introduced write budget over global rate limit , because while testing it agent autonomously started doing loop batching on write operation , which can be a really big issue for the stores.

### 5. PII guardrails

_Description:_

**Video:** https://youtu.be/dmNZf4ZWjEo

### 6. Store provisioning

_Description:_

**Video:** https://youtu.be/5LdM9Yth_N4

### 7. Cart recovery (targeted cart discounts)

_Description:_
**Video:** https://youtu.be/UCnPN6Uindg

### 8. Inventory optimization

_Description:_
**Video:** https://youtu.be/ytZ5Q8L_Afg

### 9. Observability metrics

Per-pod CPU usage and PHP-FPM worker saturation.

_Description:_
