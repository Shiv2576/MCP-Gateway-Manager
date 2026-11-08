#!/usr/bin/env bash
# Upgrade existing stores to the charts in this checkout: each store-<name> namespace holds the
# `store` release (WordPress storefront + its MariaDB) and the `woo-mcp` release (the store's
# MCP server). The operator installs both when a store is created, and moves woo-mcp to a new
# image by itself, but never re-applies the store chart to existing stores: that touches
# customer data, so a person runs it, here, one store at a time.
#
#   scripts/upgrade-stores.sh --dry-run --all          what would change, per store; changes nothing
#   scripts/upgrade-stores.sh realstore                back up, then upgrade one store
#   scripts/upgrade-stores.sh --all                    every store, stopping at the first failure
#   options: --only store|woo-mcp   --no-backup   --backup-dir DIR (default ./store-backups)   --yes
#
# Per store, in order: preflight (releases healthy) -> backup (MariaDB dump + WordPress files)
# -> upgrade `store` -> upgrade `woo-mcp` -> check the pods. Each upgrade reuses that release's own
# values with the chart's new defaults (--reset-then-reuse-values), waits for readiness and rolls
# itself back if it doesn't get there (--atomic / --rollback-on-failure). A failure stops the run:
# the stores after it are left alone. Helm rolls back configuration, never data: if a WordPress or
# MariaDB upgrade migrated the database, restore the backup from this run.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
dry=0 backup=1 yes=0 only="" backup_dir=./store-backups stores=()
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) dry=1 ;;
    --no-backup) backup=0 ;;
    --backup-dir) backup_dir=$2; shift ;;
    --only) only=$2; shift ;;
    --yes) yes=1 ;;
    --all) mapfile -t stores < <(kubectl get ns -l mcpgateway.example.com/store \
             -o jsonpath='{range .items[*]}{.metadata.labels.mcpgateway\.example\.com/store}{"\n"}{end}') ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) stores+=("$1") ;;
  esac
  shift
done
[ ${#stores[@]} -gt 0 ] || { echo "name the stores, or --all" >&2; exit 2; }
case $only in ""|store|woo-mcp) ;; *) echo "--only store|woo-mcp" >&2; exit 2 ;; esac

# Helm 4 renamed --atomic; the operator image still runs Helm 3.
if helm version --short | grep -q '^v3'; then rollback=--atomic; else rollback=--rollback-on-failure; fi
[ -d "$root/charts/store/charts" ] || helm dependency build "$root/charts/store" >/dev/null

echo "cluster: $(kubectl config current-context)   stores: ${stores[*]}   $([ $dry = 1 ] && echo DRY RUN)"
if [ $dry = 0 ] && [ $yes = 0 ]; then
  read -r -p "Upgrade these stores on this cluster? [y/N] " ok
  [ "$ok" = y ] || exit 1
fi

releases() { case $only in store) echo store ;; woo-mcp) echo woo-mcp ;; *) echo store woo-mcp ;; esac; }
timeout_for() { [ "$1" = store ] && echo 15m || echo 5m; }

preflight() { # every release exists and isn't stuck mid-operation (pending-upgrade, failed)
  local ns=$1 r status
  for r in $(releases); do
    status=$(helm -n "$ns" status "$r" -o json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["info"]["status"])') ||
      { echo "  $r: no such release in $ns" >&2; return 1; }
    [ "$status" = deployed ] || { echo "  $r is '$status', not deployed: fix it first (helm -n $ns history $r)" >&2; return 1; }
  done
}

show_changes() { # a server-side dry run, then what it would change; kubectl diff masks Secret data
  local ns=$1 r=$2
  helm -n "$ns" upgrade "$r" "$root/charts/$r" --reset-then-reuse-values --dry-run=server 2>&1 |
    awk '/^MANIFEST:/{on=1; next} /^NOTES:/{on=0} on' >"$tmp/$r.yaml"
  if kubectl -n "$ns" diff -f "$tmp/$r.yaml" >"$tmp/$r.diff" 2>&1; then
    echo "  $r: no changes"
  else
    echo "  $r: changes:"; sed 's/^/    /' "$tmp/$r.diff"
  fi
}

backup_store() { # before the store chart, which owns the data
  local name=$1 ns=$2 dir pw
  dir="$backup_dir/$name/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dir"
  pw=$(kubectl -n "$ns" get secret store-mariadb -o jsonpath='{.data.mariadb-root-password}' | base64 -d)
  kubectl -n "$ns" exec store-mariadb-0 -c mariadb -- sh -c 'mariadb-dump -u root -p"$1" --all-databases --single-transaction' _ "$pw" >"$dir/db.sql"
  kubectl -n "$ns" exec deploy/store-wordpress -c wordpress -- tar czf - -C /bitnami/wordpress . >"$dir/wordpress.tgz"
  [ -s "$dir/db.sql" ] && [ -s "$dir/wordpress.tgz" ] || { echo "  backup is empty: stopping" >&2; return 1; }
  echo "  backup: $dir ($(du -sh "$dir" | cut -f1))"
}

upgrade() {
  local ns=$1 r=$2
  echo "  upgrading $r ..."
  helm -n "$ns" upgrade "$r" "$root/charts/$r" --reset-then-reuse-values --wait "$rollback" \
    --timeout "$(timeout_for "$r")" --description "upgrade-stores.sh: $r from $(git -C "$root" rev-parse --short HEAD)" >/dev/null
}

check() { # the storefront, its database and its MCP server all run the new revision
  local ns=$1
  kubectl -n "$ns" rollout status statefulset/store-mariadb --timeout=2m >/dev/null
  kubectl -n "$ns" rollout status deploy/store-wordpress --timeout=2m >/dev/null
  kubectl -n "$ns" rollout status deploy/woo-mcp-woo-mcp --timeout=2m >/dev/null
  echo "  pods ready: mariadb, wordpress, woo-mcp"
}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
done_=()
for name in "${stores[@]}"; do
  ns=store-$name
  echo "== $name ($ns)"
  if ! preflight "$ns"; then echo "stopped at $name (preflight). Upgraded: ${done_[*]:-none}"; exit 1; fi
  if [ $dry = 1 ]; then
    for r in $(releases); do show_changes "$ns" "$r"; done
    continue
  fi
  if [ $backup = 1 ] && [ "$only" != woo-mcp ]; then
    backup_store "$name" "$ns" || { echo "stopped at $name (backup). Upgraded: ${done_[*]:-none}"; exit 1; }
  fi
  for r in $(releases); do
    if ! upgrade "$ns" "$r"; then
      echo "stopped at $name: $r failed and Helm rolled it back (helm -n $ns history $r)."
      echo "Upgraded: ${done_[*]:-none}. Not touched: the stores after $name."
      exit 1
    fi
  done
  check "$ns" || { echo "stopped at $name (pods not ready). Upgraded: ${done_[*]:-none}"; exit 1; }
  done_+=("$name")
done
echo "== $([ $dry = 1 ] && echo 'dry run done' || echo "upgraded: ${done_[*]}")"
