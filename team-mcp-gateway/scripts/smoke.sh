#!/usr/bin/env bash
# Smoke test: start a freshly built image and check it answers its health endpoint.
# CI runs it before pushing; it catches what the source-tree tests can't (a file
# missing from the image, a wrong CMD or port, a dependency only broken in the image).
#   scripts/smoke.sh <service> <image>    gateway needs Postgres on host port 5433
set -euo pipefail
name=$1 image=$2
db=postgresql://postgres:t@host.docker.internal:5433/postgres

case $name in
  store-operator)
    # kopf needs a cluster, so check what the image holds: its code imports, and the
    # bundled helm can render the charts it installs stores and edges from.
    exec docker run --rm --entrypoint sh "$image" -c \
      'python -c "import store_operator.handlers" && for c in store woo-mcp gateway; do helm template x /charts/$c >/dev/null || exit 1; done && echo ok'
    ;;
  gateway)
    port=8000 path=/readyz # readyz also proves it reached Postgres and created its schema
    env=(-e "DATABASE_URL=$db" -e SETUP_TOKEN=smoke-test-setup-token
         -e CREDENTIALS_KEY=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=) ;;
  woo-mcp)
    port=8000 path=/readyz # readyz never calls the store, so a dummy one is fine
    env=(-e WOO_INTERNAL_URL=http://store.invalid -e WOO_CONSUMER_KEY=ck -e WOO_CONSUMER_SECRET=cs
         -e MCP_AUTH_TOKEN=smoke-test-token-0123456789) ;;
  dashboard)
    port=8080 path=/healthz # nginx resolves its /api upstream at startup, so give it one
    env=(-e GATEWAY_URL=http://host.docker.internal:8000) ;;
  *) echo "unknown service $name" >&2; exit 2 ;;
esac

id=$(docker run -d --add-host=host.docker.internal:host-gateway -p "127.0.0.1::$port" "${env[@]}" "$image")
trap 'docker rm -f "$id" >/dev/null' EXIT
url=http://$(docker port "$id" "$port" | head -1)$path # a free host port, so nothing local clashes
for _ in $(seq 30); do
  curl -fs "$url" && echo && exit 0
  sleep 1
done
echo "$name did not answer $path in 30s" >&2
docker logs "$id" >&2
exit 1
