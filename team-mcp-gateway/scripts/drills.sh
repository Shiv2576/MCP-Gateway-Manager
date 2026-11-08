#!/usr/bin/env bash
# Failure drills against a live cluster: the behaviour the README promises, checked end to end.
#   GATEWAY_KEY=gw_... scripts/drills.sh            (all drills)
#   scripts/drills.sh                               (RBAC and default-deny only; MCP drills need a key)
# Env: NS (mercora), STORE_NS (store-shop1), EDGE (http://prod.gw.localhost), WOO (shop1: the
# store's server name on the gateway), CALL_TIMEOUT (10, the edge's CALL_TIMEOUT_S).
# It scales the store's woo-mcp to 0 and removes its NetworkPolicy for a moment; both are put
# back on exit, even after a failure or Ctrl-C. Run it against a test cluster, not production.
set -uo pipefail
NS=${NS:-mercora} STORE_NS=${STORE_NS:-store-shop1} EDGE=${EDGE:-http://prod.gw.localhost}
WOO=${WOO:-shop1} CALL_TIMEOUT=${CALL_TIMEOUT:-10} KEY=${GATEWAY_KEY:-}
OPERATOR=system:serviceaccount:$NS:store-operator
WOO_DEPLOY=woo-mcp-woo-mcp WOO_POLICY=woo-mcp-woo-mcp
pass=0 fail=0 saved_policy=$(mktemp) replicas=""

ok() { echo "  PASS  $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $*"; fail=$((fail + 1)); }
check() { local what=$1; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
refused() { local what=$1; shift; if "$@" >/dev/null 2>&1; then bad "$what"; else ok "$what"; fi; }

restore() {
  if [ -s "$saved_policy" ]; then kubectl apply -f "$saved_policy" >/dev/null && echo "  (restored NetworkPolicy $WOO_POLICY)"; fi
  if [ -n "$replicas" ]; then kubectl -n "$STORE_NS" scale deploy "$WOO_DEPLOY" --replicas="$replicas" >/dev/null && echo "  (scaled $WOO_DEPLOY back to $replicas)"; fi
  rm -f "$saved_policy"
}
trap restore EXIT

# One stateless MCP request to the edge; prints the JSON-RPC result (JSON or event-stream body).
rpc() {
  curl -s --max-time "$((CALL_TIMEOUT + 10))" "$EDGE/mcp" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: 2025-06-18" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}" |
    python3 -c 'import sys,json
body=sys.stdin.read()
lines=[l[5:].strip() for l in body.splitlines() if l.startswith("data:")] or [body]
msg=next((json.loads(l) for l in lines if l and "\"id\"" in l), {})
print(json.dumps(msg.get("result", msg)))'
}
tools() { rpc tools/list '{}' | python3 -c 'import sys,json; print("\n".join(t["name"] for t in json.load(sys.stdin).get("tools",[])))'; }
call_ok() { rpc tools/call "{\"name\":\"$1\",\"arguments\":{}}" | python3 -c 'import sys,json; r=json.load(sys.stdin); sys.exit(0 if r.get("content") and not r.get("isError") else 1)'; }
call_error() { rpc tools/call "{\"name\":\"$1\",\"arguments\":{}}" | python3 -c 'import sys,json; r=json.load(sys.stdin); sys.exit(0 if r.get("isError") else 1)'; }
wait_for() { for _ in $(seq 60); do "$@" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }

echo "== 1. Operator RBAC refuses what is out of its scope"
check   "operator may manage pods in a store namespace (control)" kubectl auth can-i get pods -n "$STORE_NS" --as "$OPERATOR"
refused "operator may not create pods in kube-system"        test "$(kubectl auth can-i create pods -n kube-system --as "$OPERATOR")" = yes
refused "operator may not read Secrets in kube-system"       test "$(kubectl auth can-i get secrets -n kube-system --as "$OPERATOR")" = yes
refused "operator may not create ClusterRoleBindings"        test "$(kubectl auth can-i create clusterrolebindings --as "$OPERATOR" 2>/dev/null)" = yes
refused "operator may not delete a namespace it didn't create" kubectl delete namespace default --as "$OPERATOR" --dry-run=server

echo "== 2. Default-deny holds"
probe() { # namespace host port: exit 0 if a fresh, unlabeled pod can open the TCP port
  kubectl -n "$1" run "drill-$RANDOM" --rm -i --quiet --restart=Never --image=busybox:1.36 \
    --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":65534,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"p","image":"busybox:1.36","command":["nc","-z","-w","3","'"$2"'","'"$3"'"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' \
    >/dev/null 2>&1
}
# Controls: the probe pods do run and DNS is allowed, so a refusal below is the policy, not a broken probe.
check   "control: an unlabeled pod in $NS reaches cluster DNS"     probe "$NS" kube-dns.kube-system.svc.cluster.local 53
check   "control: an unlabeled pod in $STORE_NS reaches cluster DNS" probe "$STORE_NS" kube-dns.kube-system.svc.cluster.local 53
refused "an unlabeled pod in $NS can't reach Postgres"            probe "$NS" db-postgres 5432
refused "an unlabeled pod in $STORE_NS can't reach MariaDB"        probe "$STORE_NS" store-mariadb 3306
refused "a store pod can't reach the control plane API"            probe "$STORE_NS" "team-gateway.$NS.svc.cluster.local" 8000

if [ -z "$KEY" ]; then
  echo "== 3-4 skipped: set GATEWAY_KEY to an API key on gateway $(basename "$EDGE") to run the MCP drills"
else
  listed=$(tools)
  woo_tool=$(grep "^${WOO}__list_products$" <<<"$listed" | head -1)
  other_tool=$(grep -v -e "^${WOO}__" -e "^gateway__" <<<"$listed" | grep '__list$' | head -1)
  if [ -z "$woo_tool" ] || [ -z "$other_tool" ]; then
    bad "need ${WOO}__list_products and another server's __list tool on this gateway (got: $(tr '\n' ' ' <<<"$listed"))"
  else
    echo "== 3. Killing the store's woo-mcp leaves other servers working"
    replicas=$(kubectl -n "$STORE_NS" get deploy "$WOO_DEPLOY" -o jsonpath='{.spec.replicas}')
    check "before: $woo_tool works" call_ok "$woo_tool"
    kubectl -n "$STORE_NS" scale deploy "$WOO_DEPLOY" --replicas=0 >/dev/null
    kubectl -n "$STORE_NS" wait --for=delete pod -l app.kubernetes.io/name=woo-mcp --timeout=60s >/dev/null 2>&1
    check "while down: $other_tool still works" call_ok "$other_tool"
    check "while down: $woo_tool fails with an error result, not a hang" call_error "$woo_tool"
    kubectl -n "$STORE_NS" scale deploy "$WOO_DEPLOY" --replicas="$replicas" >/dev/null && replicas=""
    check "after restart: $woo_tool works again" wait_for call_ok "$woo_tool"

    echo "== 4. A call to an upstream that stops answering times out cleanly"
    kubectl -n "$STORE_NS" get netpol "$WOO_POLICY" -o json |
      python3 -c 'import sys,json; p=json.load(sys.stdin); m=p["metadata"]; [m.pop(k,None) for k in ("uid","resourceVersion","creationTimestamp","generation","managedFields")]; print(json.dumps(p))' >"$saved_policy"
    kubectl -n "$STORE_NS" delete netpol "$WOO_POLICY" >/dev/null  # default-deny now drops the gateway's packets
    start=$(date +%s)
    check "$woo_tool returns an error result" call_error "$woo_tool"
    took=$(( $(date +%s) - start ))
    if [ "$took" -le $((CALL_TIMEOUT + 5)) ]; then ok "it ended in ${took}s, within the ${CALL_TIMEOUT}s call timeout"; else bad "it took ${took}s (call timeout ${CALL_TIMEOUT}s)"; fi
    check "meanwhile $other_tool still works" call_ok "$other_tool"
    kubectl apply -f "$saved_policy" >/dev/null && : >"$saved_policy"
    check "policy restored: $woo_tool works again" wait_for call_ok "$woo_tool"
  fi
fi

echo "== $pass passed, $fail failed"
[ "$fail" -eq 0 ]
