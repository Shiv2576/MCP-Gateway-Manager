# Deploy on a VPS (k3s + sslip.io + Let's Encrypt)

The same setup as the local k3d cluster `store-check`, on one VPS running k3s.
**The only difference is the domain:** local uses `*.localhost` over http; the
VPS uses `*.BASE` over https, reachable from the internet.

- **Names:** [sslip.io](https://sslip.io), a free public DNS service that
  resolves any name containing an IP to that IP. No domain to buy, no DNS to set up.
- **Certificates:** Let's Encrypt, through cert-manager, verified over HTTP
  (HTTP-01). HTTP-01 can't issue wildcards, so every host gets its own
  certificate. cert-manager creates them on its own for stores and edges.

Store provisioning works as on local: create a store from the dashboard
(`blank`, `catalog-a` or `catalog-b`, with the same setup and seed scripts),
and it comes up at `https://<name>.BASE` with its own certificate.

Sized for: 2 vCPU, 8 GB RAM, 80 GB disk, Ubuntu 24.04, x86_64, public IPv4.
The core (including the `mercora` store) uses about 3.5 GB; each store you
create adds about 1.5 GB.

Where stores and edges are served comes from three store-operator values,
`baseDomain`, `urlScheme` and `clusterIssuer` (the gateway chart takes the first
two). Local defaults are `localhost`, `http` and none, so k3d is unchanged.

## Names used below

| Placeholder | Meaning | Example |
|---|---|---|
| `VPS_IP` | the server's public IPv4 | `203.0.113.7` |
| `BASE` | the IP with dashes + `.sslip.io` | `203-0-113-7.sslip.io` |
| `SLUG` | your gateway's name (local uses `prod`) | `prod` |

| Local (k3d) | VPS |
|---|---|
| `http://dash.localhost` | `https://dash.BASE` (dashboard) |
| `http://gateway.localhost` | `https://gateway.BASE` (gateway API) |
| `http://prod.gw.localhost/mcp` | `https://prod.gw.BASE/mcp` (MCP endpoint for Claude) |
| `http://store.localhost` | `https://store.BASE` (the `mercora` store) |
| `http://<store>.localhost` | `https://<store>.BASE` (each store you create) |

These URLs stay the same across restarts and redeploys. They change only if
the VPS's IP changes (see *Known limits*).

## What runs

The same releases as `store-check`, in namespace `mercora`:

| Release | Chart | Image |
|---|---|---|
| `db` | postgres | `postgres:17-alpine` |
| `mercora` | store | Bitnami WordPress + MariaDB |
| `woo-mcp` | woo-mcp (the `mercora` store's MCP server) | `woo-mcp:0.5.0` |
| `team` | gateway (control plane) | `gateway:0.7.3` |
| `team-ui` | dashboard | `dashboard:0.7.3` |
| `store-operator` | store-operator | `store-operator:0.5.3` |
| `notes`, `fx` | demo-mcp | `demo-mcp:0.1.0` |
| `gw-<slug>` | gateway (edge), installed by the operator | `gateway:0.6.1` |
| `store-<name>` | store, installed by the operator | Bitnami WordPress + MariaDB, `woo-mcp:0.5.0` |

The tags are the chart defaults, not code versions: step 3 builds the current
code under them. `gateway:0.6.1` is the same build as `gateway:0.7.3`, so
edges run the control plane's code.

Only Helm values differ from local:

| Chart | Value | Local (k3d) | VPS |
|---|---|---|---|
| gateway | `ingress.host` | `gateway.localhost` | `gateway.BASE` |
| gateway | `cookieSecure` | `false` | `true` (https only) |
| gateway | `baseDomain` / `urlScheme` | `localhost` / `http` | `BASE` / `https` |
| dashboard | `ingress.host` | `dash.localhost` | `dash.BASE` |
| postgres | `persistence.size` | `1Gi` | `5Gi` |
| store (`mercora`) | `wordpress.ingress.hostname` / `wordpressScheme` | `store.localhost` / `http` | `store.BASE` / `https` |
| woo-mcp | `store.publicUrl` | `http://store.localhost` | `https://store.BASE` |
| store-operator | `baseDomain` / `urlScheme` / `clusterIssuer` | `localhost` / `http` / none | `BASE` / `https` / `letsencrypt` |
| notes, fx | — | same files (`*-local.yaml`) | same files |

Same on both: the gateway's `upstreamAllowHosts` (the in-cluster `woo-mcp`,
`notes` and `fx` Services) and `seedServer.url` (the `mercora` store's woo-mcp).

- **Storage:** both use the built-in `local-path` StorageClass. Volumes live on
  the VPS disk under `/var/lib/rancher/k3s/storage`.
- **Secrets:** same names and keys as local, with new random values made on
  the VPS. Nothing secret is in any values file. No users or data are copied;
  you run the dashboard's first-time setup again.
- **Ingress:** the same Traefik that k3s ships.
- **Network policies:** k3s enforces them out of the box (its embedded
  kube-router). Every component is deny-by-default in both directions: Postgres
  and the demo servers open no connections; the dashboard reaches only the
  gateway; the operator reaches only the gateway and the Kubernetes API; the
  gateway reaches Postgres, the listed MCP servers, store and hosted woo-mcp
  pods, and the internet, but never other pod/Service IPs or cloud metadata
  (`169.254.0.0/16`). The CIDRs in the charts (`clusterCidrs`) are k3s's
  defaults; change them only if you install k3s with other `--cluster-cidr` /
  `--service-cidr` values.
- **SSRF guard:** a server an admin adds must resolve to a public address,
  unless its host is in `upstreamAllowHosts`. Store and hosted servers the
  operator creates need no entry.
- **Operator rights:** it can create and delete only the `store-<name>` and
  `mcp-<name>` namespaces it labels, and gets workload rights only inside them
  (a RoleBinding per namespace). A ValidatingAdmissionPolicy enforces the
  "only its own namespaces" part; it needs Kubernetes 1.30+ (current k3s is).
- **Bitnami:** nothing to add. Its WordPress chart is bundled in `charts/store`
  (and copied into the operator image); only its images are pulled from Docker Hub.
- **Images:** built for amd64 on your laptop and imported into k3s, the same
  way `k3d image import` works locally.

## 1. Domain prefix

On your laptop, in `team-mcp-gateway`, replace the placeholder domain once
(macOS `sed`):

```
sed -i '' 's/example\.com/203-0-113-7.sslip.io/g' deploy/*-prod.yaml
```

This covers every VPS file: gateway, dashboard, store-operator, the `mercora`
store (`store-values-prod.yaml`), its woo-mcp (`values-prod.yaml`) and TLS.

Check that the names resolve (any prefix works):

```
dig +short dash.203-0-113-7.sslip.io        # prints VPS_IP
dig +short anything.203-0-113-7.sslip.io    # prints VPS_IP
```

## 2. Server software (on the VPS)

```
ssh root@VPS_IP
apt update && apt -y upgrade
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
ufw allow from 10.42.0.0/16 && ufw allow from 10.43.0.0/16   # k3s pod and service networks
ufw enable

curl -sfL https://get.k3s.io | sh -
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc && . ~/.bashrc
kubectl get nodes            # Ready; VERSION v1.30 or later

helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
  --set crds.enabled=true --wait
```

**Port 80 must stay open**, for the first certificate of every host and for
every renewal. Port 6443 (the Kubernetes API) stays closed to the internet; the
gateway and operator pods reach it through the `10.42.0.0/16` rule. Use it from
your laptop with `ssh -L 6443:127.0.0.1:6443 root@VPS_IP` if needed.

## 3. Code and images (on your laptop)

Build amd64 images into tarballs; this leaves your local arm64 images alone.
Tags are the chart defaults (see *What runs*).

```
cd team-mcp-gateway
helm dependency build charts/store     # the operator image copies charts/store
mkdir -p /tmp/img
b() { docker buildx build --platform linux/amd64 -o type=docker,dest=/tmp/img/$1.tar -t $1:$2 "${@:3}"; }
b gateway        0.7.3 services/gateway -t gateway:0.6.1
b dashboard      0.7.3 services/dashboard
b woo-mcp        0.5.0 services/woo-mcp
b demo-mcp       0.1.0 services/demo-mcp
b store-operator 0.5.3 -f services/store-operator/Dockerfile .

rsync -a --exclude node_modules --exclude .venv ./ root@VPS_IP:/opt/team-mcp-gateway/
scp /tmp/img/*.tar root@VPS_IP:/tmp/
ssh root@VPS_IP 'for f in /tmp/*.tar; do k3s ctr images import $f; done'
ssh root@VPS_IP 'k3s ctr images ls -q | grep -E "gateway|dashboard|operator|mcp"'   # gateway shows 0.7.3 and 0.6.1
```

`pullPolicy: IfNotPresent` (chart default) makes the pods use these imported
images. After rebuilding with the same tag, delete the pod so it restarts on
the new one.

**Later updates:** `scripts/deploy-vps.sh` does this step plus the app
`helm upgrade`s and pod restarts, after showing what changes and asking.
Override `VPS`, `PORT` or `KEY` in the environment for another server.

## 4. TLS (on the VPS)

Edit `deploy/tls-prod.yaml`:

- set your email (Let's Encrypt expiry notices; addresses at example.* are rejected),
- add `store.BASE` under `dnsNames`, beside `dash.BASE` and `gateway.BASE`.
  The `mercora` store's Ingress has no certificate of its own and uses this one.

The file holds:

- the `letsencrypt` ClusterIssuer (HTTP-01 through Traefik),
- one certificate for the fixed hosts, served as Traefik's default,
- the http→https redirect for every host.

Stores you create and gateway edges are not in it: each gets its own
certificate when the operator creates its Ingress.

```
cd /opt/team-mcp-gateway
kubectl apply -f deploy/tls-prod.yaml
kubectl -n kube-system get certificate wildcard -w     # READY True, usually within 2 minutes
```

If a certificate stays not ready: `kubectl describe challenge -A`. The usual
causes are port 80 blocked, a typo in a hostname, or a Let's Encrypt rate limit
(see *Known limits*). If the challenge never passes, remove the redirect
(`HelmChartConfig`) from the file, apply, and put it back once the
certificate is issued. cert-manager renews every certificate 30 days before expiry.

## 5. Namespace and Secrets

```
kubectl create namespace mercora
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW \
  --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=SETUP_TOKEN=$(openssl rand -hex 16)      # add OPENROUTER_API_KEY for chat
kubectl -n mercora create secret generic gateway-credentials \
  --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
kubectl -n mercora create secret generic woo-mcp-credentials \
  --from-literal=WOO_CONSUMER_KEY=ck_$(openssl rand -hex 20) \
  --from-literal=WOO_CONSUMER_SECRET=cs_$(openssl rand -hex 20) \
  --from-literal=MCP_AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic notes-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
kubectl -n mercora create secret generic fx-mcp-credentials --from-literal=AUTH_TOKEN="$(openssl rand -hex 24)"
```

`woo-mcp-credentials` is shared: the `mercora` store's setup Job registers the
key in WooCommerce, woo-mcp reads it, and the gateway reads `MCP_AUTH_TOKEN`
from it to register woo-mcp at first-run setup. Each operator-created store's
Secrets are made by the operator, as on local.

## 6. Install, one by one

```
helm upgrade --install db charts/postgres -n mercora -f deploy/postgres-values-prod.yaml --wait
helm upgrade --install mercora charts/store -n mercora -f deploy/store-values-prod.yaml --wait --timeout 15m
kubectl -n mercora logs job/mercora-store-setup        # ends with "store setup done"
helm upgrade --install woo-mcp charts/woo-mcp -n mercora -f deploy/values-prod.yaml
helm upgrade --install notes charts/demo-mcp -n mercora -f deploy/notes-values-local.yaml
helm upgrade --install fx    charts/demo-mcp -n mercora -f deploy/fx-values-local.yaml
helm upgrade --install team charts/gateway -n mercora -f deploy/gateway-values-prod.yaml
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-prod.yaml
```

Right away (the setup page is public until done): open `https://dash.BASE`,
enter the `SETUP_TOKEN`
(`kubectl -n mercora get secret gateway-admin -o jsonpath='{.data.SETUP_TOKEN}' | base64 -d`),
create the organization and first admin. Setup also registers the `mercora`
store's woo-mcp as server `woo-mcp`, so it is listed on first login.
Then API keys → create an admin key, and:

```
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY=<key>
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-prod.yaml
```

`deploy/store-operator-values-prod.yaml` is the local file plus `baseDomain`,
`urlScheme: https` and `clusterIssuer: letsencrypt`. The operator
chart also installs the Store, McpGateway and HostedServer CRDs; until it is
installed, those dashboard pages show errors.

Skipping the `mercora` store: leave out its two releases and the
`woo-mcp-credentials` Secret, and delete `seedServer` from
`deploy/gateway-values-prod.yaml`, or setup registers a server that does not exist.

## 7. Stores, gateway, Claude

**Stores** → create one (`blank`, `catalog-a` or `catalog-b`). The operator
installs it in namespace `store-<name>` (about 10 minutes), runs the setup and
seed scripts, and registers its woo-mcp as a server. Its storefront is at
`https://<name>.BASE` with its own certificate.

**Gateways** → create one named `SLUG` with **public URL exactly
`https://SLUG.gw.BASE`** (e.g. `https://prod.gw.203-0-113-7.sslip.io`). OAuth
redirect and issuer URLs are built from it, so http instead of https or a
different host breaks sign-in. The operator installs the edge `gw-SLUG`, gives
its Ingress that host and a certificate, and
turns on OAuth. Attach the store servers (`woo-mcp` and any you created) to it.

`notes` and `fx` run as on local but are not registered. Add them under
Servers if you want them (in-cluster URLs from the README, tokens in their
Secrets); their hosts are already in `upstreamAllowHosts`.

Claude → Settings → Connectors → Add custom connector →
`https://SLUG.gw.BASE/mcp`, sign in with a gateway login.

## 8. Check

```
curl -sI http://dash.BASE | head -3                    # 301/308 to https
curl -sI https://dash.BASE | head -1                   # 200
curl -s https://store.BASE/wp-json/ | head -c 80       # WordPress JSON (mercora store)
curl -s https://<store>.BASE/wp-json/ | head -c 80     # WordPress JSON (a created store)
echo | openssl s_client -connect VPS_IP:443 -servername SLUG.gw.BASE 2>/dev/null \
  | openssl x509 -noout -subject -issuer -enddate      # that host, Let's Encrypt, ~90 days out
kubectl get certificate -A                             # every host READY True
kubectl -n mercora get networkpolicy                   # db-postgres, team-gateway, team-ui-dashboard, store-operator, woo-mcp-woo-mcp, notes, fx
kubectl get validatingadmissionpolicy                  # mercora-store-operator-own-namespaces
kubectl get pods -A; kubectl -n mercora get mgw,stores
free -h                                                # memory headroom
```

In the dashboard: Tools → run `woo-mcp`'s `list_products`; once a created
store is Ready, run its `list_products` too. A catalog store returns its
seeded products.

## Optional: Claude Code on the VPS for debugging

Needs a Pro, Max, Team, Enterprise or Console account. It asks for 4 GB+ RAM,
so check `free -h` first.

```
curl -fsSL https://claude.ai/install.sh | bash
exec bash
cd /opt/team-mcp-gateway && claude
```

On a headless server, follow the login prompt and finish signing in from your
laptop's browser. `kubectl` works inside it because `KUBECONFIG` is in
`.bashrc`. This is the live cluster, so read each command before approving it,
especially `helm uninstall`, `kubectl delete`, and anything touching PVCs.

## NAT / IPv6-only / LXC servers

Some cheap VPSes share one IPv4 behind NAT (only SSH forwarded) and give each
server its own IPv6, often as an LXC container. Use the IPv6 as `BASE`
(`2001-41d0-...--94.sslip.io`, colons → dashes, `::` → `--`) and add four
things: k3s settings for LXC, legacy iptables (kube-router fails on nftables in LXC), a socat forwarder from IPv6 80/443 to Traefik,
and a CoreDNS override so pods resolve `*.BASE` to Traefik over IPv4. See
[vps-setup-log.md](vps-setup-log.md), which also records the live server's setup.
Clients without IPv6 can't reach such a server, Claude's connector included:
publish the gateway edge through Tailscale Funnel (same file) for IPv4.

## Known limits

- **URLs depend on the IP.** If the VPS's IP changes (usually only when the
  server is recreated), every URL changes: rerun the `sed`, reapply
  `tls-prod.yaml`, set the operator's `baseDomain`, recreate stores, the
  `mercora` one included (a store's site address is fixed when it is
  created), update the gateway's public URL, and re-add the connector in Claude.
- **sslip.io is a free third-party service.** If it is down, names stop
  resolving. Every store and gateway is one certificate; Let's Encrypt allows
  50 new certificates per registered domain per week. If issuance gets
  rate-limited, fall back to a free [DuckDNS](https://www.duckdns.org) name
  (`yourname.duckdns.org`, sub-subdomains resolve too) as `BASE` with the same
  steps; it also survives IP changes, since you update the IP in DuckDNS.
- Edges also keep their `<slug>.gw.localhost` host. The public URL host is
  added beside it, so this does no harm.
- One Postgres replica, no backups. Snapshot the VPS once the demo works.
- The `mercora` WordPress pods have no NetworkPolicy of their own, so the
  gateway's `namespaceDefaultDeny` stays off.
- `catalog-b` stores create shoppers with the password `test123`
  (`manifests.py`, `credentials_secret`). With public storefronts, anyone can
  log in as them. Fine for fake demo data; otherwise change it first.
- The dashboard is public: anyone with an admin login or API key can run any
  container through Hosted servers. Use a strong admin password.
