# MCP-Gateway-Manager

## Local setup (k3d)

Needs Docker, k3d, kubectl, Helm and Python 3. Run from `team-mcp-gateway/` (`cd team-mcp-gateway`).

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

# 3. Images (tags = chart defaults)
helm dependency build charts/store
docker build -t gateway:0.10.3 services/gateway
docker build -t dashboard:0.9.8 services/dashboard
docker build -t woo-mcp:0.5.0 services/woo-mcp
docker build -t store-operator:0.7.2 -f services/store-operator/Dockerfile .
k3d image import -c store-check gateway:0.10.3 dashboard:0.9.8 woo-mcp:0.5.0 store-operator:0.7.2

# 4. Install
helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-local.yaml --wait
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-local.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --wait

# 5. First admin: open http://dash.localhost and enter this token, org name, admin login
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# 6. Operator: dashboard → API keys → create "store-operator", paste it here
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='mcpg_...'
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
troubleshooting (IPv6-only/LXC hosts included): [docs/deploy-vps.md](team-mcp-gateway/docs/deploy-vps.md).

**On the VPS** — k3s, Helm, cert-manager:

```sh
curl -sfL https://get.k3s.io | sh -
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc && . ~/.bashrc
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true --wait
```

**On your laptop**, in `team-mcp-gateway/` — set the domain, build amd64 images, copy everything:

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

helm upgrade --install db      charts/postgres  -n mercora -f deploy/postgres-values-prod.yaml --wait
helm upgrade --install team    charts/gateway   -n mercora -f deploy/gateway-values-prod.yaml --set seedServer.url= --wait
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-prod.yaml --wait

# First admin, right away (setup is public until done): open https://dash.$BASE
kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d; echo

# Dashboard → API keys → create "store-operator", then:
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='mcpg_...'
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-prod.yaml --wait

kubectl get pods -A; kubectl get certificate -A
```

Then in the dashboard: **Stores** → create a store (at `https://<name>.$BASE`) →
**Gateways** → create `prod` with public URL `https://prod.gw.$BASE` (turns on
OAuth for Claude) and attach the store.

Later updates, from the laptop: `scripts/deploy-vps.sh` (shows the changes, asks,
then builds, copies, imports and runs every `helm upgrade`; set `VPS`, `PORT`,
`KEY` for your server). Rollback: [docs/upgrades.md](team-mcp-gateway/docs/upgrades.md).

## Using the online store

Everything starts from the dashboard: `https://dash.2001-41d0-303-2774--94.sslip.io`
(locally `http://dash.localhost`). Log in with your gateway username and password.

### Create and open a store

1. **Stores** → enter a name, pick a template (**Catalog A**, **Catalog B** or **Blank**) → **Create store**.
2. Wait until its status is **Ready** (about 5–10 minutes).
3. Open the store with its URL on the **Stores** page. The WordPress admin login is shown there too.

### Place an order

1. Open the store from the **Stores** page.
2. Pick a product → **Add to cart** → **View cart** → **Proceed to checkout**.
3. Fill in name, address, email and phone; payment is **Cash on delivery**.
4. **Place order**. The order number is shown on the confirmation page.
5. Check it: **Tools** → pick the gateway → `<store>__list_orders` with `{"on_date": "today"}`.
   Move it on with `<store>__update_order_status` (`{"order_id": 123, "status": "completed"}`).

### Connect an external MCP client

1. **Gateways** → pick a gateway → **Copy** its MCP URL (if none exists, fill in a slug and name → **Create gateway**,
   then attach the store's server to it).
2. **API keys** → create a key; it is shown once (`mcpg_...`).
3. Add it to the client:

   ```sh
   claude mcp add --transport http shop <MCP URL> --header "Authorization: Bearer mcpg_..."
   ```

   For claude.ai or Claude Desktop: **Settings** → **Connectors** → **Add custom connector** →
   paste the MCP URL → **Connect** → sign in with your gateway username and password.
   **API keys** → **Connected apps** lists it; **Disconnect** ends its access.

The client sees only the tools your teams allow on that gateway, named `<store>__<tool>`.

### Use the chat agent

1. **Chat** → pick the gateway.
2. Ask in plain language: "What were today's orders in shop1?", "Which products are low on stock?",
   "Best sellers over the last 30 days".
3. Before any change (create a product, change an order, refund) it shows the exact call;
   nothing is written until you click **Confirm**.
