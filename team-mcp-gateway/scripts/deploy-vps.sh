#!/usr/bin/env bash
# Make the VPS run this working tree: show what changes, ask, then build amd64
# images under the chart-default tags, sync the repo, import the images into k3s,
# helm-upgrade the app releases and restart every pod that runs one of our images.
# Leaves db, the mercora store (its setup Job would re-run and rotate the woo key)
# and the WordPress releases of operator stores alone. See docs/deploy-vps.md step 3.
set -euo pipefail
cd "$(dirname "$0")/.."

VPS=${VPS:-root@147.135.130.116}
PORT=${PORT:-20045}
KEY=${KEY:-$HOME/.ssh/id_ed25519_work}
DEST=/opt/team-mcp-gateway
SSH=(ssh -i "$KEY" -p "$PORT" -o ConnectTimeout=15 "$VPS")
RSYNC=(rsync -a --delete --exclude .git --exclude node_modules --exclude .venv
       --exclude __pycache__ --exclude .pytest_cache -e "ssh -i $KEY -p $PORT")

tag() { awk -F'"' '/^  tag:/ {print $2; exit}' "charts/$1/values.yaml"; }
EDGE=$(awk -F'"' '/^edgeImageTag:/ {print $2}' deploy/store-operator-values-prod.yaml)

echo "== Local changes not in git (deployed too) =="
git status --short .
echo
echo "== Files that change on the VPS ($DEST) =="
"${RSYNC[@]}" -n --itemize-changes ./ "$VPS:$DEST/" | grep -v '^\.d' || echo "(none)"
echo
echo "== Images to build (amd64) =="
echo "gateway:$(tag gateway) + gateway:$EDGE (edges), dashboard:$(tag dashboard), woo-mcp:$(tag woo-mcp),"
echo "demo-mcp:$(tag demo-mcp), store-operator:$(tag store-operator)"
echo
echo "== Then on the VPS =="
echo "helm upgrade woo-mcp notes fx team team-ui store-operator (mercora), every gw-* edge,"
echo "woo-mcp in every store-* namespace; restart all pods on those images."
echo
read -rp "Deploy to $VPS? [y/N] " ok
[[ $ok == [yY] ]] || { echo "Cancelled, nothing changed."; exit 1; }

IMG=$(mktemp -d)
trap 'rm -rf "$IMG"' EXIT
helm dependency build charts/store >/dev/null   # the operator image copies charts/store
b() { docker buildx build --platform linux/amd64 -o "type=docker,dest=$IMG/$1.tar" -t "$1:$2" "${@:3}"; }
b gateway        "$(tag gateway)"        services/gateway -t "gateway:$EDGE"
b dashboard      "$(tag dashboard)"      services/dashboard
b woo-mcp        "$(tag woo-mcp)"        services/woo-mcp
b demo-mcp       "$(tag demo-mcp)"       services/demo-mcp
b store-operator "$(tag store-operator)" -f services/store-operator/Dockerfile .

"${RSYNC[@]}" ./ "$VPS:$DEST/"
"${SSH[@]}" 'rm -rf /tmp/deploy-img && mkdir /tmp/deploy-img'
scp -i "$KEY" -P "$PORT" "$IMG"/*.tar "$VPS:/tmp/deploy-img/"

"${SSH[@]}" bash -s <<'EOF'
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd /opt/team-mcp-gateway
for f in /tmp/deploy-img/*.tar; do k3s ctr images import "$f"; done
rm -rf /tmp/deploy-img

n=mercora
helm upgrade --install woo-mcp        charts/woo-mcp        -n $n -f deploy/values-prod.yaml
helm upgrade --install notes          charts/demo-mcp       -n $n -f deploy/notes-values-local.yaml
helm upgrade --install fx             charts/demo-mcp       -n $n -f deploy/fx-values-local.yaml
helm upgrade --install team           charts/gateway        -n $n -f deploy/gateway-values-prod.yaml
helm upgrade --install team-ui        charts/dashboard      -n $n -f deploy/dashboard-values-prod.yaml
helm upgrade --install store-operator charts/store-operator -n $n -f deploy/store-operator-values-prod.yaml
# Operator-installed releases: new chart defaults, values the operator set.
for r in $(helm ls -n $n -q | grep '^gw-' || true); do
  helm upgrade "$r" charts/gateway -n $n --reset-then-reuse-values
done
for ns in $(kubectl get ns -o name | sed -n 's|^namespace/\(store-.*\)|\1|p'); do
  helm status woo-mcp -n "$ns" >/dev/null 2>&1 && helm upgrade woo-mcp charts/woo-mcp -n "$ns" --reset-then-reuse-values
done

# Same tags as before, so pods whose spec didn't change still run the old image.
kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.template.spec.containers[0].image}{"\n"}{end}' \
  | awk '$3 ~ /^(gateway|dashboard|woo-mcp|demo-mcp|store-operator):/ {print $1, $2}' > /tmp/deploy-restart
while read -r ns d; do kubectl -n "$ns" rollout restart "deploy/$d"; done < /tmp/deploy-restart
while read -r ns d; do kubectl -n "$ns" rollout status "deploy/$d" --timeout 5m; done < /tmp/deploy-restart
rm /tmp/deploy-restart
echo "VPS updated."
EOF
