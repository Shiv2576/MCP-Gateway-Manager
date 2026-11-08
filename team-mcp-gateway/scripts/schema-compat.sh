#!/usr/bin/env bash
# N-1 check: the previous release's gateway must still work on the schema this code migrates to.
# A GitOps rollback (git revert) brings the old code back but leaves the database as the new code
# left it, so a migration that renames, drops or adds a NOT NULL column would break the rollback.
#
#   TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres scripts/schema-compat.sh [PREV_REF]
#
# PREV_REF (default origin/main) is the release a rollback would go back to. How: the NEW code
# migrates template1, the database Postgres copies for every CREATE DATABASE; then the OLD code's
# whole test suite runs, so each test database it creates already has the new schema. Needs a
# throwaway Postgres (the test one): template1 is put back to empty at the end.
set -euo pipefail
ref=${1:-origin/main}
base=${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a throwaway Postgres}
template=${base%/*}/template1
root=$(cd "$(dirname "$0")/.." && pwd) # team-mcp-gateway
new=$root/services/gateway
old=$(mktemp -d)

reset_template() {
  (cd "$new" && uv run --frozen python - "$template" <<'EOF'
import sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as c:
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.execute("GRANT ALL ON SCHEMA public TO public")
EOF
  )
}
trap 'reset_template; rm -rf "$old"' EXIT

echo "== previous release: $ref ($(git -C "$root" rev-parse --short "$ref"))"
git -C "$(git -C "$root" rev-parse --show-toplevel)" archive "$ref:team-mcp-gateway" | tar -x -C "$old"

echo "== new code migrates template1"
(cd "$new" && uv run --frozen python -c "import sys; from app import db; db.connect(sys.argv[1]).close()" "$template")

echo "== previous release's tests, on the new schema"
(cd "$old/services/gateway" && TEST_DATABASE_URL=$base uv run --frozen pytest -q -p no:cacheprovider)
echo "== ok: $ref runs on this schema"
