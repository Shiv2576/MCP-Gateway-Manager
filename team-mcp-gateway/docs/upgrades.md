# Upgrades and rollback

Every component is a Helm release, so every upgrade is the same four steps and
every rollback is one command. Local (k3d) and the VPS (k3s) differ only in how
the image reaches the cluster and which values file is passed.

## The releases

| What | Release | Namespace | Chart | Values |
|---|---|---|---|---|
| Gateway, control plane (API, dashboard backend, chat) | `team` | `mercora` | `charts/gateway` | `deploy/gateway-values-{local,prod}.yaml` |
| Gateway, edge (`/mcp`) | `gw-<slug>`, e.g. `gw-prod` | `mercora` | `charts/gateway` | installed by the operator; reuse its values |
| Dashboard | `team-ui` | `mercora` | `charts/dashboard` | `deploy/dashboard-values-{local,prod}.yaml` |
| Gateway database | `db` | `mercora` | `charts/postgres` | `deploy/postgres-values-{local,prod}.yaml` |
| Store operator | `store-operator` | `mercora` | `charts/store-operator` | `deploy/store-operator-values-{local,prod}.yaml` |
| A store (WordPress + MariaDB) | `store` | `store-<name>` | `charts/store` | installed by the operator; reuse its values |
| That store's WooCommerce MCP server | `woo-mcp` | `store-<name>` | `charts/woo-mcp` | installed by the operator; reuse its values |

The operator runs `helm upgrade --install` for a store and its woo-mcp only when
the Store is created. It never upgrades existing stores by itself, so a bad
chart cannot roll out to every store at once: existing stores are upgraded by
hand, one at a time, as below. The operator image bundles the store, woo-mcp and
gateway charts, so rebuild it whenever those charts change, or new stores and
new edges get the old version.

## Rules that make rollback safe

1. **A new image tag for every build.** Rollback returns to the old tag; if
   that tag was rebuilt in place, it now holds the new code and the rollback
   changes nothing. Never rebuild an existing tag.
2. **Keep old images until the new one has proven itself.** Rollback needs the
   old image on the node (k3d/k3s) or in the registry.
3. **Schema changes are additive.** The gateway migrates its database on
   startup with `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, so
   older code ignores what newer code added and a rollback is safe. Remove a
   table or column only in a later release, once no version that needs it is a
   rollback target. Helm rolls back configuration, never data.
4. **Back up state before upgrading it.** The store's MariaDB and WordPress
   files, and the gateway's Postgres, live on persistent volumes. A WordPress
   or WooCommerce upgrade may migrate the database, and `helm rollback` will not
   undo that. Back up first (below).
5. **Always `--wait --atomic`.** `--wait` waits for the new pods to pass their
   readiness probes; `--atomic` rolls back by itself if they do not in time.
6. **Label each revision.** The chart versions do not change between images, so
   pass `--description` to say which image a revision runs; `helm history` shows
   it.
7. **Roll back with Helm, not `kubectl rollout undo`.** The kubectl version
   changes pods behind Helm's back and leaves its history wrong.

## Upgrade: the four steps

### 1. Build with a new tag

```sh
docker build -t gateway:0.10.2 services/gateway
docker build -t woo-mcp:0.5.1 services/woo-mcp
docker build -t dashboard:0.9.8 services/dashboard
docker build -t store-operator:0.7.2 -f services/store-operator/Dockerfile .   # from the repo root: it bundles charts/
```

### 2. Put the image in the cluster

Local k3d has no registry; import into its nodes:

```sh
k3d image import -c store-check gateway:0.10.2
```

VPS k3s: no registry either. The laptop is arm64 and the VPS amd64, so build
an amd64 image straight into a tarball (a plain `docker save` of the local
image would be the wrong architecture), copy it, import it into k3s's
containerd:

```sh
docker buildx build --platform linux/amd64 -o type=docker,dest=/tmp/gateway.tar -t gateway:0.10.2 services/gateway
scp -P 20045 /tmp/gateway.tar root@VPS:/tmp/
ssh -p 20045 root@VPS 'k3s ctr images import /tmp/gateway.tar'
```

`scripts/deploy-vps.sh` does this for every image, then the `helm upgrade`s,
after showing what will change and asking (see `docs/deploy-vps.md` step 3). It
builds under the charts' default tags and restarts the pods, which breaks rule 1:
bump the chart's default tag before running it if that release must stay
roll-back-able.

### 3. Helm upgrade

**Gateway.** The control plane and every edge run the same image; upgrade both,
and move the operator's `edgeImageTag` so edges it creates later match.

```sh
helm -n mercora upgrade team charts/gateway -f deploy/gateway-values-local.yaml \
  --set image.tag=0.10.2 --wait --atomic --timeout 5m --description "gateway 0.10.2"

helm -n mercora get values gw-prod -o yaml > /tmp/gw-prod.yaml
helm -n mercora upgrade gw-prod charts/gateway -f /tmp/gw-prod.yaml \
  --set image.tag=0.10.2 --wait --atomic --timeout 5m --description "gateway 0.10.2"
```

Releases the operator installed are upgraded from their own current values
(`helm get values`), not from a file in `deploy/`.

**WooCommerce MCP server**, for each store:

```sh
helm -n store-shop1 get values woo-mcp -o yaml > /tmp/woo-mcp.yaml
helm -n store-shop1 upgrade woo-mcp charts/woo-mcp -f /tmp/woo-mcp.yaml \
  --set image.tag=0.5.1 --wait --atomic --timeout 5m --description "woo-mcp 0.5.1"
```

**Store** (WordPress + MariaDB), for each store, after a backup:

```sh
helm -n store-shop1 get values store -o yaml > /tmp/store.yaml
helm -n store-shop1 upgrade store charts/store -f /tmp/store.yaml \
  --wait --atomic --timeout 15m --description "store chart: <what changed>"
```

**Dashboard:**

```sh
helm -n mercora upgrade team-ui charts/dashboard -f deploy/dashboard-values-local.yaml \
  --set image.tag=0.9.8 --wait --atomic --timeout 5m --description "dashboard 0.9.8"
```

On the VPS, use the `-prod` values files.

### 4. What Kubernetes does, and how to check it

The gateway, edge, dashboard and woo-mcp Deployments use rolling updates:
Kubernetes starts a new pod beside the old one, sends it traffic only once its
readiness probe passes, then removes the old pod. A broken image never gets
traffic; the old pod keeps serving. The edge runs two replicas. The store
operator uses `Recreate` on purpose, so two operators never act at once.

```sh
kubectl -n mercora rollout status deploy/team-gateway
helm -n mercora history team
```

## Rollback

```sh
helm -n mercora history team              # find the last good revision
helm -n mercora rollback team 21 --wait   # re-applies revision 21, image tag included
```

A rollback is a new revision and goes through the same rolling update, so it is
also zero downtime. The same command works for every release in the table
above; add `-n store-<name>` for a store's `store` and `woo-mcp`.

If an upgrade with `--atomic` failed its readiness checks, Helm has already
rolled it back; `helm history` shows the failed revision and the rollback.

## Backups before a stateful upgrade

Store database (MariaDB):

```sh
PW=$(kubectl -n store-shop1 get secret store-mariadb -o jsonpath='{.data.mariadb-root-password}' | base64 -d)
kubectl -n store-shop1 exec store-mariadb-0 -c mariadb -- mariadb-dump -u root -p"$PW" --all-databases > shop1-db.sql
```

Store files (WordPress uploads, plugins):

```sh
kubectl -n store-shop1 exec deploy/store-wordpress -c wordpress -- tar czf - -C /bitnami/wordpress . > shop1-wordpress.tgz
```

Gateway database (Postgres):

```sh
kubectl -n mercora exec db-postgres-0 -- pg_dump -U postgres gateway > gateway-db.sql
```

Restore the matching backup only if the rollback has to undo a data migration;
a rollback of code alone needs none.
