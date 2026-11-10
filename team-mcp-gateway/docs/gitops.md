# GitOps: GitHub Actions → GHCR → Argo CD

How code reaches the VPS. Replaces `scripts/deploy-vps.sh` (laptop build, tarball,
scp, `k3s ctr images import`, `helm upgrade`).

```
push to main ─▶ CI: tests + ─▶ CI: build the affected ─▶ CI: commit their SHA ─▶ Argo CD syncs
                chart checks    images, smoke-test,     into deploy/*-prod.yaml   (polls git, ~3 min)
                                push to GHCR
```

## The pieces

| Piece | Where | Does |
|---|---|---|
| CI | `.github/workflows/ci.yaml` | PRs: tests, `helm lint` every chart + render the Argo charts with prod values, build the affected images and smoke-test them (`scripts/smoke.sh`). `main`: also push them and bump their tags. |
| Registry | `ghcr.io/shiv2576/<service>:<commit SHA>` | One immutable tag per commit, kept for rollback. Public, so k3s pulls without a secret. |
| Desired state | `deploy/*-prod.yaml` `image:` blocks | CI's bot commit sets the tag; git history is the deploy history. |
| Argo CD | `deploy/argocd/` | `root.yaml` (app of apps) watches `apps/`: `team`, `team-ui`, `store-operator`. Auto-sync, prune, self-heal. |
| Store operator | `edgeImage*`, `wooMcpImage` in its values | Upgrades the `gw-*` edges and each store's woo-mcp when its image values change. |

**Selective builds.** The `changes` job maps the changed folders to images
(`services/<name>/**` → that image; `charts/store`, `charts/woo-mcp`, `charts/gateway`
→ store-operator too, as its image copies them). Only those are built, pushed and
bumped, so Argo restarts only their pods; every other service keeps its tag. A gateway
change also bumps `edgeImageTag` (edges run the gateway image) and a woo-mcp change
bumps `wooMcpImage.tag`; the operator rolls both out. Tests always run in full.

Tags therefore differ per service: each one is the last commit that changed it.
If a main run fails, its services are not bumped; re-run it, or use *Run workflow*
(builds and deploys all four) so prod catches up with `main`.

**Kept outside Argo, on purpose:**
- `db`: holds the data; an auto-synced, auto-pruned app is the wrong owner for it.
- `gw-*` and `store-*`: created at runtime by the store operator from McpGateway/Store
  resources, not declared in git. The operator is their controller; Argo is the operator's.
- `notes` and `fx` (demo MCP servers): plain Helm releases with the local `demo-mcp`
  image, installed by hand (`docs/deploy-vps.md`); not built by CI.
- Secrets (`gateway-db`, `gateway-admin`, `gateway-credentials`, the MCP credentials):
  made by hand on the VPS (`docs/deploy-vps.md`), never in git.

## One-time setup

**1. First CI run.** Push these changes to `main`, then Actions → ci → *Run workflow*
on `main`: a push only builds what it changed, but every image is still `pending-ci`,
so the first deploy builds all four. Watch `changes` → `test` → `images` → `deploy`.
The `deploy` job commits `deploy: <services> at <sha>`; run `git pull` before your
next push (true after every deploy).

**2. Make the packages public.** New GHCR packages start private. For each of
gateway, dashboard, woo-mcp, store-operator: github.com/Shiv2576 →
Packages → the package → Package settings → Change visibility → Public. Check from
the VPS (it is NAT/IPv6-only, so this also proves it can reach ghcr.io):

```
k3s crictl pull ghcr.io/shiv2576/dashboard:<sha>
```

**3. Install Argo CD** (on the VPS, `KUBECONFIG=/etc/rancher/k3s/k3s.yaml`):

```
kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.4/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server
```

**4. Hand the platform to Argo.** One apply; everything after comes from git.

```
kubectl apply -f /opt/team-mcp-gateway/deploy/argocd/root.yaml
kubectl -n argocd get applications          # all Synced / Healthy after a few minutes
kubectl get deploy -A -o wide | grep ghcr   # platform, then edges and woo-mcps within ~1 min
```

Argo takes over the existing resources of `team`, `team-ui` and `store-operator`
in place (same names), so there is no downtime beyond the image
rollout.

**5. UI** (optional), through an SSH tunnel:

```
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
kubectl -n argocd port-forward svc/argocd-server 8080:443   # then https://localhost:8080, user admin
```

**6. Stop using `scripts/deploy-vps.sh`.** Its `helm upgrade`s would fight Argo
(self-heal reverts them) and it points at local tags. Delete it once Argo has run a
few deploys cleanly.

## Every day

Merge to `main`. That's the deploy. `kubectl -n argocd get applications` or the UI
shows which commit each app runs.

## Rollback

Revert the bot's commit (or set the tag in `deploy/*-prod.yaml` to an older SHA) and
push. Argo deploys it; the old image is still in GHCR, so the old code really comes
back. (`argocd app rollback` refuses while auto-sync is on: git is the place to roll back.)

Not rolled back by either: database migrations, and CRDs (Argo applies new ones but
never deletes them). That's why the schema is versioned (`services/gateway/app/migrations/`,
recorded in `schema_version`) and every migration must leave a schema the previous release
still runs on: add tables and nullable columns, and rename or drop only a release later. CI's
`schema-compat` job proves it by running the previous release's test suite on the new schema
(`scripts/schema-compat.sh`), so a rollback finds a database it understands.

One data change is one-way: the release that binds credentials to their server re-seals every
stored credential in a format older code can't read, and keeps the old values in
`credential_backup_v1`. Rolling back past it, put them back before (or right after) the old
code starts, else its servers report "the stored credential cannot be read":

```sh
kubectl -n mercora exec -i db-postgres-0 -- psql -U postgres gateway <<'SQL'
UPDATE servers s SET auth_value = b.value FROM credential_backup_v1 b
  WHERE b.source = 'servers' AND b.server_id = s.id;
UPDATE user_credentials u SET secret = b.value FROM credential_backup_v1 b
  WHERE b.source = 'user_credentials' AND b.server_id = u.server_id AND b.user_id = u.user_id;
SQL
```

Credentials added or changed after the upgrade have no old copy: enter those again.

## Monitoring

`deploy/argocd/apps/monitoring.yaml` installs a Prometheus server (chart
`prometheus-community/prometheus` 29.47.0) in the `monitoring` namespace: no Grafana,
Alertmanager or exporters. Its data lives on a 20 Gi volume and is kept 365 days, capped at
15 GB, so the oldest data goes first and it never fills the disk the stores share. It scrapes
every 30 s:

- every gateway pod (control plane and edges), on port 9100;
- the kubelet's cAdvisor, for each store's CPU and memory.

The control gateway queries it (`PROMETHEUS_URL`) for the Reports tab's charts. Only that
gateway may reach Prometheus (NetworkPolicy `prometheus-server-ingress`).

After the first deploy, check on the VPS:

1. `kubectl -n monitoring get pods`: the server is Running.
2. `kubectl -n monitoring port-forward svc/prometheus-server 9090:80`, then open
   http://localhost:9090/targets: the gateway pods and `kubernetes-nodes-cadvisor` are up.
3. The Reports tab shows lines within about 2 minutes of traffic.

Stores made before this change keep an unused NetworkPolicy, `metrics-ingress` (the operator only
creates policies). It is harmless; `kubectl -n store-<name> delete networkpolicy metrics-ingress`
removes it.

## Notes

- `helm ls` / `helm history` on the VPS stop changing for Argo-managed releases:
  Argo renders the chart and applies it itself. Git history replaces Helm history.
- Argo CD's 7 pods set no resource requests, so they schedule even when the node's
  CPU is fully requested; they use roughly 0.1–0.2 vCPU and 0.5–1 GB RAM idle.
- Argo CD release: pinned to v3.5.4 above; bump deliberately.
