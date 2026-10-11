# Gateways part 1 (Postgres, control/edge, McpGateway, org setup) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single gateway into one control plane plus any number of separately deployed gateway edges, all on one central Postgres, with a first-run organization setup.

**Architecture:** One image (`services/gateway`) runs as `GATEWAY_MODE=control` (the `/api` behind the dashboard) or `edge` (`/mcp` for the servers attached to one gateway). Admins create gateways in the dashboard; the control plane writes a row and an `McpGateway` resource, and the existing kopf operator installs the edge release `gw-<slug>` from `charts/gateway`. Every pod reads and writes the same Postgres.

**Tech Stack:** Python 3.12+, Starlette, MCP SDK 2.x, psycopg 3 + psycopg-pool, kopf, Helm, k3d/Traefik, React 19 + Vite.

**Spec:** `docs/superpowers/specs/2026-10-11-gateways-control-edge-design.md`

## Not in this plan (separate specs later)

- **Part 2: teams and tool-level policies** (team × gateway × tools). Until then, members are still granted access server by server (`user_servers`), and an edge serves a member the granted servers that are attached to its gateway.
- **Part 3: personal vs shared identity** per gateway-server connection. Until then, every upstream call uses the server's one stored (shared) credential.
- **SQLite is gone for good.** Postgres is the only database; the old SQLite file is not migrated (fresh install).

## Global Constraints

- **No git commits.** The user makes every commit. Where a task ends, stop and report; never run `git commit`.
- One organization per install; fresh install, no migration from the SQLite file.
- Custom resource: `McpGateway`, group `mcpgateway.example.com`, version `v1`, plural `mcpgateways`, short name `mgw`.
- Gateway slug: `^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$` (2-20 chars). Edge release `gw-<slug>`, host `<slug>.gw.localhost`, Deployment `gw-<slug>-gateway`.
- Edge default replicas: 2. Control plane: 1 replica.
- Setup token: `SETUP_TOKEN`, at least 16 characters, compared with `hmac.compare_digest`. Setup locks once the `org` row (id 1) exists.
- Every pod keeps the label `app.kubernetes.io/name: gateway` (upstream and Postgres NetworkPolicies allow it).
- Secrets are created by hand, never held in a chart: `gateway-db` (`POSTGRES_PASSWORD` for the superuser, read only by the postgres pod; `APP_PASSWORD` and `DATABASE_URL` for the non-superuser `gateway` role), `gateway-admin` (`SETUP_TOKEN`, optional `OPENROUTER_API_KEY`), `store-operator-gateway` (`GATEWAY_API_KEY`).
- Timestamps `DOUBLE PRECISION` (Postgres `REAL` is 4 bytes and would round epoch seconds).
- Image tags for this work: `gateway:0.2.0`, `store-operator:0.2.0`, `dashboard:0.2.0`.
- Match the surrounding code: plain functions, short docstrings that say why, `shortcut:` comments for known limits.

## Review Focus

1. **A user spreading calls over gateways and replicas**: the per-user limit must hold in total, not per pod. Pinned in Task 4 (`test_rate_limit_is_shared_by_every_pod`) and checked on the cluster in Task 8.
2. **The `McpGateway` create succeeds but its response is lost**: a retry must adopt the resource, not 409 forever; an unretried one must show as `Orphaned` and be deletable. Pinned in Task 3 (`test_gateway_create_recovers_from_a_lost_response`, `test_gateway_row_is_removed_when_the_cluster_refuses`).
3. **A client whose requests land on different replicas**: initialize on one, call on another must work (stateless). Pinned in Task 4 (`test_each_edge_serves_only_its_servers`).
4. **A hand-typed `tools/call` on an edge for a server the user is granted but that is not attached to this gateway**: refused, audited with the gateway, never forwarded; a foreign Host header with a valid key gets 421. Pinned in Task 4 (same test).
5. **Two pods redeeming one OAuth code at once**: exactly one succeeds. Pinned in Task 4 (`test_a_code_is_redeemed_once_even_concurrently`).

Also pinned: Postgres unreachable at start fails fast (Task 1), wrong setup tokens are throttled (Task 2), members get 403 on admin gateway endpoints (Task 3), the `gateway` database role is not a superuser (Task 6/8).

---

## File map

| File | Change | Responsibility |
|---|---|---|
| `services/gateway/app/db.py` | rewrite | Postgres pool wrapper `Db`, schema, hashing, users/grants helpers |
| `services/gateway/app/api.py` | modify | SQL ported; setup gate in `handler`; `session_response`; servers show `store`, `gateways`; `/api/connect` removed |
| `services/gateway/app/oauth.py` | modify | SQL ported |
| `services/gateway/app/upstream.py` | modify | type hints; `gateway_slug` filter; audit `gateway` |
| `services/gateway/app/setup.py` | create | `GET/POST /api/setup` |
| `services/gateway/app/gateways.py` | create | `/api/gateways` CRUD + attach |
| `services/gateway/app/kube.py` | modify | generic plural, `create_object`, `AlreadyExists`, `gateway_resource` |
| `services/gateway/app/stores.py` | modify | `AlreadyExists` rename |
| `services/gateway/app/main.py` | modify | Settings (mode, slug, database_url, setup_token); control vs edge app |
| `services/gateway/tests/conftest.py` | create | `db_url` fixture: a fresh database per test |
| `services/gateway/tests/test_db.py` | create | wrapper + fail-fast tests |
| `services/gateway/tests/test_setup.py` | create | setup tests |
| `services/gateway/tests/test_gateways.py` | create | gateways API tests |
| `services/gateway/tests/test_edge.py` | create | edge tests |
| `services/gateway/tests/test_gateway.py`, `test_oauth.py`, `test_stores.py` | modify | Postgres, setup helper, edges for `/mcp` |
| `services/store-operator/store_operator/{manifests,handlers,gateway}.py` | modify | McpGateway handlers, `edge_values`, `store` on registration |
| `services/store-operator/tests/test_manifests.py`, `test_gateway_sync.py` | modify | new unit tests |
| `services/store-operator/Dockerfile` | modify | ship `charts/gateway` |
| `charts/store-operator/crds/mcpgateway.yaml` | create | CRD |
| `charts/store-operator/templates/{rbac,deployment}.yaml`, `values.yaml` | modify | RBAC, `EDGE_IMAGE_TAG` |
| `charts/gateway/*` | modify | `mode`, `slug`, `replicas`, `dbSecret`; PVC removed |
| `charts/postgres/*` | create | StatefulSet, Service, NetworkPolicy |
| `deploy/*.yaml`, `README.md` | modify | values and install order |
| `services/dashboard/src/{App,Gateways,Servers,Tools}.jsx`, `styles.css` | modify/create | Setup screen, Gateways page, columns |

---

### Task 1: Postgres storage

Port the gateway from SQLite to Postgres without changing behaviour. The first admin still comes from `ADMIN_USERNAME`/`ADMIN_PASSWORD` in this task (Task 2 replaces it), so the existing tests keep their shape.

**Files:**
- Rewrite: `services/gateway/app/db.py`
- Modify: `services/gateway/app/api.py`, `app/oauth.py`, `app/upstream.py`, `app/main.py`, `app/stores.py` (type hints only), `services/gateway/pyproject.toml`
- Create: `services/gateway/tests/conftest.py`, `services/gateway/tests/test_db.py`
- Modify: `services/gateway/tests/test_gateway.py`, `tests/test_oauth.py`, `tests/test_stores.py`

**Interfaces:**
- Produces: `db.connect(url: str, *, create_schema: bool = True, timeout: float = 10.0) -> Db`; `Db.execute(sql, params=None) -> Result`; `Db.executemany(sql, seq)`; `Db.transaction()` context manager yielding an object with the same `execute`/`executemany`; `Db.close()`; `Result.fetchone() -> dict | None` (advances, like a cursor), `Result.fetchall() -> list[dict]` (the remaining rows), iteration, `Result.rowcount`; `db.IntegrityError`; `db.DatabaseUnavailable`. SQL keeps `?` placeholders. `Settings.database_url` replaces `Settings.database_path`. Test fixture `db_url: str`.

- [ ] **Step 1: Start a test Postgres and add the drivers**

```bash
docker run -d --name gw-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine
cd services/gateway
uv add 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'
export TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres
```

- [ ] **Step 2: Write the fixture** — `services/gateway/tests/conftest.py`

```python
"""A fresh Postgres database for every test that asks for `db_url`.

Start one with:
  docker run -d --name gw-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine
  export TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

BASE = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def db_url():
    if not BASE:
        pytest.skip("set TEST_DATABASE_URL (see tests/conftest.py)")
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(BASE, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    yield BASE.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(BASE, autocommit=True) as c:
        # FORCE: apps under test may still hold pooled connections.
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
```

- [ ] **Step 3: Write the failing wrapper tests** — `services/gateway/tests/test_db.py`

```python
"""The Postgres wrapper the rest of the gateway uses."""

from __future__ import annotations

import time

from app import db as store


def test_db_wrapper(db_url):
    db = store.connect(db_url)
    uid = store.create_user(db, "ann", "ann-password-123", "admin")
    assert db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone() == {"username": "ann"}
    assert db.execute("DELETE FROM sessions").rowcount == 0
    assert [r["username"] for r in db.execute("SELECT username FROM users")] == ["ann"]
    store.create_user(db, "bea", "bea-password-123", "member")
    rows = db.execute("SELECT username FROM users ORDER BY id")
    assert rows.fetchone()["username"] == "ann"  # advances like a cursor
    assert rows.fetchone()["username"] == "bea"
    assert rows.fetchone() is None

    try:
        with db.transaction() as tx:
            tx.execute("INSERT INTO servers (name, url, created_at) VALUES ('a', 'http://a', 0)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.execute("SELECT COUNT(*) AS n FROM servers").fetchone()["n"] == 0  # rolled back

    try:
        store.create_user(db, "ann", "other-password-1", "admin")
        raise AssertionError("duplicate username accepted")
    except store.IntegrityError:
        pass
    store.connect(db_url).close()  # a second start leaves the schema alone
    db.close()


def test_unreachable_database_fails_fast():
    started = time.monotonic()
    try:
        store.connect("postgresql://u:p@127.0.0.1:1/x", timeout=2)
        raise AssertionError("connected to nothing")
    except store.DatabaseUnavailable as e:
        assert "2s" in str(e)
    assert time.monotonic() - started < 6
```

- [ ] **Step 4: Run them to see them fail**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL (`connect()` takes a file path and returns a sqlite3 connection; `DatabaseUnavailable` does not exist).

- [ ] **Step 5: Rewrite `app/db.py`**

Replace the module docstring, imports, `SCHEMA`, `connect`, `_drop_customers`, `create_user`, `granted_server_ids` and `set_grants`. Keep `sha256`, `hash_password`, `verify_password`, `DUMMY_HASH` exactly as they are.

```python
"""PostgreSQL storage and the hashing used for passwords, sessions and API keys.

Every gateway pod (the control plane and each edge) uses the same database, so
a revoked key or a detached server stops working everywhere on the next request.
Only hashes of passwords, session tokens and API keys are stored, so a copy of
the database does not let anyone log in or call tools.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

IntegrityError = psycopg.IntegrityError

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    created_at    DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    revoked_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS servers (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    -- The static credential: one header sent on every upstream request.
    -- Never returned by the API; only the gateway reads it.
    auth_header TEXT,
    auth_value  TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    at          DOUBLE PRECISION NOT NULL,
    user_id     BIGINT,
    username    TEXT NOT NULL,
    tool        TEXT NOT NULL,
    server      TEXT,
    ok          INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
-- Which servers a member may use. Admins use every server and have no rows.
CREATE TABLE IF NOT EXISTS user_servers (
    user_id   BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    server_id BIGINT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, server_id)
);
-- OAuth for /mcp (see oauth.py). Clients register themselves.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
-- A sign-in in progress. code_hash is first the hash of the login page id,
-- then, once the user logs in, of the authorization code.
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id    BIGINT REFERENCES users(id) ON DELETE CASCADE,
    params     TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes     TEXT NOT NULL,
    resource   TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
"""


class DatabaseUnavailable(RuntimeError):
    """Postgres could not be reached at start. The process should not start."""


def _pg(sql: str) -> str:
    # The SQL in this codebase uses "?" placeholders and never a literal "?" or "%".
    return sql.replace("?", "%s")


class Result:
    """Rows fetched up front, so the connection goes back to the pool at once."""

    def __init__(self, cur: psycopg.Cursor):
        self.rowcount = cur.rowcount
        self._rows = iter(cur.fetchall() if cur.description else [])

    # Consumed like a cursor: each row is returned once.
    def fetchone(self) -> dict | None:
        return next(self._rows, None)

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def __iter__(self):
        return self._rows


class _Conn:
    """One borrowed connection: what a transaction block works with."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def execute(self, sql: str, params=None) -> Result:
        return Result(self._conn.execute(_pg(sql), params))

    def executemany(self, sql: str, seq) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(_pg(sql), seq)


class Db:
    # shortcut: synchronous queries inside async handlers; fine at in-cluster
    # latency for a demo, move to psycopg.AsyncConnection if p99 matters.
    def __init__(self, url: str, timeout: float):
        self.pool = ConnectionPool(
            url, min_size=1, max_size=10, timeout=timeout, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        try:
            self.pool.wait(timeout=timeout)
        except PoolTimeout:
            self.pool.close()
            raise DatabaseUnavailable(f"cannot reach the database within {timeout:g}s") from None

    def execute(self, sql: str, params=None) -> Result:
        with self.pool.connection() as conn:
            return _Conn(conn).execute(sql, params)

    def executemany(self, sql: str, seq) -> None:
        with self.pool.connection() as conn:
            _Conn(conn).executemany(sql, seq)

    @contextlib.contextmanager
    def transaction(self):
        with self.pool.connection() as conn, conn.transaction():
            yield _Conn(conn)

    def close(self) -> None:
        self.pool.close()


def connect(url: str, *, create_schema: bool = True, timeout: float = 10.0) -> Db:
    """create_schema=False on edges: only the control plane creates tables, so
    pods starting together never race on CREATE TABLE."""
    db = Db(url, timeout)
    if create_schema:
        db.execute(SCHEMA)
    return db
```

And at the end of the file (after `DUMMY_HASH`):

```python
def create_user(conn, username: str, password: str, role: str) -> int:
    return conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?) RETURNING id",
        (username, hash_password(password), role, time.time()),
    ).fetchone()["id"]


def granted_server_ids(conn, user) -> set[int] | None:
    """Server ids this user may use; None means all of them (admins)."""
    if user["role"] == "admin":
        return None
    rows = conn.execute("SELECT server_id FROM user_servers WHERE user_id = ?", (user["id"],))
    return {r["server_id"] for r in rows}


def set_grants(conn: Db, user_id: int, server_ids: list[int]) -> None:
    with conn.transaction() as tx:
        tx.execute("DELETE FROM user_servers WHERE user_id = ?", (user_id,))
        tx.executemany(
            "INSERT INTO user_servers (user_id, server_id) VALUES (?, ?)",
            [(user_id, s) for s in server_ids],
        )
```

`_drop_customers` and the grants backfill are deleted (fresh install).

- [ ] **Step 6: Run the wrapper tests**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Port the SQL in `api.py`**

- Remove `import sqlite3`. Replace every `sqlite3.Row` type hint with `dict` and `sqlite3.Connection` with `store.Db`.
- In `handler`: `except sqlite3.IntegrityError:` → `except store.IntegrityError:`.
- In `_server_ids`: `known = {r[0] for r in conn.execute("SELECT id FROM servers")}` → `known = {r["id"] for r in conn.execute("SELECT id FROM servers")}`.
- In `create_key`:

```python
    kid = request.app.state.db.execute(
        "INSERT INTO api_keys (user_id, name, key_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (user["id"], name, store.sha256(key), key[:12], time.time()),
    ).fetchone()["id"]
    # The only time the key is ever shown. Only its hash is kept.
    return JSONResponse({"id": kid, "name": name, "key": key}, status_code=201)
```

- In `create_server`:

```python
    sid = conn.execute(
        "INSERT INTO servers (name, url, auth_header, auth_value, enabled, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (name, url, header, value, 1 if data.get("enabled", True) else 0, time.time()),
    ).fetchone()["id"]
    request.app.state.upstreams.refresh_soon(sid)
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()
    return JSONResponse(_server(row, None), status_code=201)
```

- In `list_connections`, the SQLite JSON function and the loose GROUP BY:

```python
    rows = request.app.state.db.execute(
        "SELECT t.user_id, u.username, t.client_id, c.info::json ->> 'client_name' AS client_name,"
        " MAX(t.created_at) AS last_used"
        " FROM oauth_tokens t JOIN users u ON u.id = t.user_id JOIN oauth_clients c ON c.client_id = t.client_id"
        " WHERE t.kind = 'refresh' AND t.expires_at > ? AND (? OR t.user_id = ?)"
        " GROUP BY t.user_id, u.username, t.client_id, c.info ORDER BY last_used DESC",
        (time.time(), user["role"] == "admin", user["id"]),
    ).fetchall()
```

- [ ] **Step 8: Port `oauth.py`, `upstream.py`, `stores.py`, `main.py`**

- `oauth.py`: remove `import sqlite3`; `sqlite3.Connection` → `store.Db`, `sqlite3.Row` → `dict`. The SQL already works in Postgres (`rtrim` exists; `executemany` is on `Db`). Docstring of `Provider`: "backed by the gateway's database".
- `upstream.py`: remove `import sqlite3`; `row: sqlite3.Row` → `row: dict`, `db: sqlite3.Connection` → `db` (untyped, as `granted_server_ids` takes it), `user: sqlite3.Row` → `user: dict`.
- `stores.py`: no SQL changes.
- `main.py`: in `Settings`, replace `database_path: str = "/data/gateway.db"` with `database_url: str = field(default="", repr=False)`; in `from_env`, replace the `database_path=` line with `database_url=env("DATABASE_URL", ""),`. In `create_app`:

```python
    settings = settings or Settings.from_env()
    if not settings.database_url:
        raise ConfigError("DATABASE_URL is required")
    conn = store.connect(settings.database_url)
    if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
```

  and close the pool when the app stops, in `lifespan`:

```python
            finally:
                task.cancel()
                conn.close()
```

- [ ] **Step 9: Check no literal `?` or `%` hides in SQL**

Run: `grep -nE "'[^']*[?%][^']*'" app/*.py | grep -v "^app/oauth.py.*PAGE" ; grep -n "LIKE" app/*.py`
Expected: no SQL string with a literal `?`/`%` and no `LIKE` (the `PAGE` HTML and f-strings are not SQL).

- [ ] **Step 10: Move the tests to Postgres**

In `tests/test_gateway.py`, `tests/test_oauth.py`, `tests/test_stores.py`:
- Every test function parameter `tmp_path` → `db_url`.
- Every `database_path=str(tmp_path / "g.db")` → `database_url=db_url`.
- In `test_rate_limit_per_user`: `Upstreams(store.connect(str(tmp_path / "g.db")), ...)` → `Upstreams(store.connect(db_url), ...)`.
- Delete `test_customer_role_is_removed_from_an_old_database` and `test_grants_upgrade_keeps_members_on_their_servers` (SQLite upgrade code is gone).

- [ ] **Step 11: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS, every test (none skipped while `TEST_DATABASE_URL` is set).

- [ ] **Step 12: Stop point** — report "Task 1 done" with the test output. The user commits.

---

### Task 2: First-run organization setup

**Files:**
- Create: `services/gateway/app/setup.py`, `services/gateway/tests/test_setup.py`
- Modify: `services/gateway/app/db.py` (schema), `app/api.py`, `app/main.py`, `tests/test_gateway.py`, `tests/test_oauth.py`, `tests/test_stores.py`

**Interfaces:**
- Consumes: `db.connect`, `Db.transaction`, `db.create_user`, `db.IntegrityError` (Task 1).
- Produces: `Settings.setup_token: str`; `api.session_response(request, user, status=200) -> JSONResponse`; routes `GET/POST /api/setup`; every other `/api` route answers 503 `{"error": "setup needed: ..."}` until the org exists. Test helpers in `tests/test_gateway.py`: `SETUP: str`, `async setup_admin(gw: str) -> httpx2.AsyncClient` (logged in as `ADMIN`).

- [ ] **Step 1: Write the failing tests** — `services/gateway/tests/test_setup.py`

```python
"""First-run organization setup: once, with the setup token."""

from __future__ import annotations

import asyncio

import httpx2

from app.main import ConfigError, Settings, create_app
from tests.test_gateway import ADMIN, SETUP, serve

BODY = {"setup_token": SETUP, "org_name": "Acme", "username": ADMIN[0], "password": ADMIN[1]}


def test_control_plane_needs_a_setup_token_until_set_up(db_url):
    try:
        create_app(Settings(database_url=db_url))
    except ConfigError:
        return
    raise AssertionError("started with no way to set up")


def test_setup_once(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP))) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            assert (await c.get("/api/setup")).json() == {"needed": True, "org": None}
            r = await c.get("/api/users")
            assert r.status_code == 503 and "setup" in r.json()["error"]

            assert (await c.post("/api/setup", json={**BODY, "setup_token": "wrong"})).status_code == 403
            assert (await c.post("/api/setup", json={**BODY, "password": "short"})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "username": "Bad Name"})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "org_name": " "})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "org_name": "x" * 81})).status_code == 400

            r = await c.post("/api/setup", json=BODY)
            assert r.status_code == 201 and r.json()["role"] == "admin"
            assert (await c.get("/api/me")).json()["username"] == ADMIN[0]  # logged in by the cookie
            assert (await c.get("/api/setup")).json() == {"needed": False, "org": "Acme"}
            r = await httpx2.AsyncClient(base_url=gw).post("/api/setup", json={**BODY, "username": "eve"})
            assert r.status_code == 409

    asyncio.run(run())
    create_app(Settings(database_url=db_url))  # set up: no token needed any more


def test_concurrent_setups_only_one_wins(db_url):
    async def run():
        settings = Settings(database_url=db_url, setup_token=SETUP)
        with serve(create_app(settings)) as a, serve(create_app(settings)) as b:
            r1, r2 = await asyncio.gather(
                httpx2.AsyncClient(base_url=a, timeout=10).post("/api/setup", json=BODY),
                httpx2.AsyncClient(base_url=b, timeout=10).post("/api/setup", json={**BODY, "username": "root"}),
            )
            assert sorted((r1.status_code, r2.status_code)) == [201, 409]

    asyncio.run(run())


def test_wrong_setup_tokens_are_throttled(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP))) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            for _ in range(5):
                assert (await c.post("/api/setup", json={**BODY, "setup_token": "guess"})).status_code == 403
            assert (await c.post("/api/setup", json=BODY)).status_code == 429

    asyncio.run(run())
```

- [ ] **Step 2: Add the shared test helpers** — top of `tests/test_gateway.py`, under `FX_TOKEN`:

```python
SETUP = "setup-token-0123456789"


async def setup_admin(gw: str) -> httpx2.AsyncClient:
    """Run first-run setup; the client comes back logged in as ADMIN."""
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    r = await c.post("/api/setup", json={"setup_token": SETUP, "org_name": "Acme",
                                         "username": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 201, r.text
    return c
```

(It uses `httpx2`, already imported in that file. Define it after `ADMIN`.)

- [ ] **Step 3: Run the new tests to see them fail**

Run: `uv run pytest tests/test_setup.py -v`
Expected: FAIL (`Settings` has no `setup_token`; `/api/setup` is 404).

- [ ] **Step 4: Add the `org` table** — append to `SCHEMA` in `app/db.py`:

```sql
-- The one organization this install belongs to. Its row locks first-run setup.
CREATE TABLE IF NOT EXISTS org (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    name       TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
```

- [ ] **Step 5: Gate and session helper in `api.py`**

Replace `handler` with:

```python
def org_ready(request: Request) -> bool:
    state = request.app.state
    if not getattr(state, "org_ready", False):  # cached once true: setup never undoes itself
        state.org_ready = state.db.execute("SELECT 1 FROM org").fetchone() is not None
    return state.org_ready


def handler(fn):
    """Turn HTTPError into a JSON error response. Until first-run setup is done,
    only /api/setup answers."""

    async def wrapped(request: Request) -> Response:
        try:
            if not request.url.path.startswith("/api/setup") and not org_ready(request):
                raise HTTPError(503, "setup needed: open the dashboard to set up the organization")
            return await fn(request)
        except HTTPError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        except store.IntegrityError:
            return JSONResponse({"error": "already exists"}, status_code=409)

    return wrapped
```

Split `login` so setup can reuse the session part:

```python
def session_response(request: Request, user: dict, status: int = 200) -> JSONResponse:
    """Start a browser session for this user: the HttpOnly cookie and the user as JSON."""
    conn = request.app.state.db
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (store.sha256(token), user["id"], time.time() + SESSION_TTL_S),
    )
    response = JSONResponse(_user(user), status_code=status)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="strict",
        secure=request.app.state.settings.cookie_secure,
    )
    return response


@handler
async def login(request: Request) -> Response:
    data = await body(request)
    user = check_password(request, _str(data, "username"), _str(data, "password"))
    return session_response(request, user)
```

- [ ] **Step 6: Write `app/setup.py`**

```python
"""First-run setup: name the organization and create its first admin, once.

Needs the setup token from the gateway-admin Secret, so the first person to
reach a fresh install cannot take it over. The org row's fixed id makes two
racing setups (even on different pods) end with exactly one winner.
"""

from __future__ import annotations

import hmac
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import USERNAME, HTTPError, LoginThrottle, _str, body, handler, session_response

# Not a valid username, so it never shares a throttle bucket with a real login.
THROTTLE_KEY = "@setup"


@handler
async def status(request: Request) -> Response:
    row = request.app.state.db.execute("SELECT name FROM org").fetchone()
    return JSONResponse({"needed": row is None, "org": row["name"] if row else None})


@handler
async def setup(request: Request) -> Response:
    conn = request.app.state.db
    if conn.execute("SELECT 1 FROM org").fetchone():
        raise HTTPError(409, "already set up")
    throttle: LoginThrottle = request.app.state.login_throttle
    if throttle.blocked(THROTTLE_KEY):
        raise HTTPError(429, "too many wrong setup tokens; try again in 15 minutes")
    data = await body(request)
    token, expected = data.get("setup_token"), request.app.state.settings.setup_token
    if not (isinstance(token, str) and expected and hmac.compare_digest(token.encode(), expected.encode())):
        throttle.failed(THROTTLE_KEY)
        raise HTTPError(403, "wrong setup token")
    throttle.succeeded(THROTTLE_KEY)

    org_name, username, password = _str(data, "org_name"), _str(data, "username"), _str(data, "password")
    if len(org_name) > 80:
        raise HTTPError(400, "organization name: at most 80 characters")
    if not USERNAME.match(username):
        raise HTTPError(400, "username: 2-32 of a-z 0-9 . _ -")
    if len(password) < 10:
        raise HTTPError(400, "password must be at least 10 characters")
    try:
        with conn.transaction() as tx:
            tx.execute("INSERT INTO org (id, name, created_at) VALUES (1, ?, ?)", (org_name, time.time()))
            uid = store.create_user(tx, username, password, "admin")
    except store.IntegrityError:
        raise HTTPError(409, "already set up") from None
    request.app.state.org_ready = True
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return session_response(request, user, status=201)


routes = [
    Route("/api/setup", status, methods=["GET"]),
    Route("/api/setup", setup, methods=["POST"]),
]
```

- [ ] **Step 7: Settings and startup in `main.py`**

- `Settings`: delete `admin_username` and `admin_password` (and their comment); add after `database_url`:

```python
    # Needed until first-run setup is done; then unused.
    setup_token: str = field(default="", repr=False)
```

- `from_env`: delete the `admin_username=`/`admin_password=` lines; add `setup_token=env("SETUP_TOKEN", "").strip(),`.
- `create_app`: replace the whole "no users yet" block with:

```python
    if conn.execute("SELECT 1 FROM org").fetchone() is None and len(settings.setup_token) < 16:
        raise ConfigError("not set up yet: set SETUP_TOKEN (16+ chars) and finish setup in the dashboard")
```

- imports: `from . import api, chat, oauth, setup, stores as store_api`; routes: add `*setup.routes,` after `*api.routes,`.

- [ ] **Step 8: Move the existing tests to setup**

In `tests/test_gateway.py`, `tests/test_oauth.py`, `tests/test_stores.py`:
- In every `Settings(...)`: replace `admin_username=ADMIN[0], admin_password=ADMIN[1]` with `setup_token=SETUP` (import `SETUP`, `setup_admin` from `tests.test_gateway` where needed).
- `test_refuses_to_start_without_a_first_admin`: delete it (covered by `test_control_plane_needs_a_setup_token_until_set_up`).
- Where a test gets its admin with `admin = await login(gw, *ADMIN)`, use `admin = await setup_admin(gw)`.
- Where a test logs the admin in by hand (`test_the_whole_gateway`, `test_chat_agent_calls_tools_as_the_logged_in_user`, `test_members_see_only_granted_servers`, `test_admin_deletes_a_user`, `test_wrong_passwords_lock_the_username`), add `await setup_admin(gw)` as the first line inside the innermost `with serve(...)` block and keep the rest unchanged.

- [ ] **Step 9: Run the suite**

Run: `uv run pytest -v`
Expected: PASS, including the 4 new setup tests.

- [ ] **Step 10: Stop point** — report "Task 2 done". The user commits.

---

### Task 3: Gateways API on the control plane

**Files:**
- Create: `services/gateway/app/gateways.py`, `services/gateway/tests/test_gateways.py`
- Modify: `app/db.py` (schema), `app/kube.py`, `app/stores.py`, `app/api.py`, `app/main.py`, `tests/test_gateway.py`, `tests/test_stores.py`, `tests/test_oauth.py`

**Interfaces:**
- Consumes: Task 1 `Db`, Task 2 `setup_admin`, `SETUP`.
- Produces: tables `gateways(id, slug, name, public_url, created_at)`, `gateway_servers(gateway_id, server_id)`, column `servers.store`. `kube.Kube(namespace, plural="stores")` with `list(owner_id=None)`, `get(name)`, `create_object(obj) -> dict` (raises `kube.AlreadyExists`), `create(name, template, owner_id, owner)` (stores, unchanged signature), `delete(name)`. `kube.gateway_resource(slug, name, public_url) -> dict`. `create_app(settings=None, stores=None, gateways=None)`. `FakeGateways(fail=False, lose_response=False)`. Routes `GET/POST /api/gateways` (POST is safe to retry: 201 when it inserted the row, 200 when it finished an earlier attempt; resources without a row list as phase `Orphaned`), `DELETE /api/gateways/{slug}` (also removes orphans), `PUT /api/gateways/{slug}/servers`. Server JSON gains `store: str | None`, `gateways: list[str]`. `/api/connect` is removed. Test helpers in `tests/test_gateway.py`: `FakeGateways(fail=False)` and `async add_gateway(admin, slug, server_ids)`.

- [ ] **Step 1: Add the test fake and helper** — in `tests/test_gateway.py` after `setup_admin`:

```python
class FakeGateways:
    """In-memory stand-in for the McpGateway resources in the Kubernetes API."""

    def __init__(self, fail: bool = False, lose_response: bool = False):
        self.items: dict[str, dict] = {}
        self.fail = fail
        self.lose_response = lose_response  # create it, then fail as if the answer got lost

    async def list(self, owner_id=None):
        return list(self.items.values())

    async def get(self, name):
        return self.items.get(name)

    async def create_object(self, obj):
        from app.kube import AlreadyExists

        if self.fail:
            raise RuntimeError("the cluster said no")
        name = obj["metadata"]["name"]
        if name in self.items:
            raise AlreadyExists(name)
        self.items[name] = obj
        if self.lose_response:
            raise TimeoutError("read timed out")
        return obj

    async def delete(self, name):
        self.items.pop(name, None)


async def add_gateway(admin: httpx2.AsyncClient, slug: str, server_ids: list[int]) -> None:
    r = await admin.post("/api/gateways", json={"slug": slug, "name": slug.title()})
    assert r.status_code == 201, r.text
    r = await admin.put(f"/api/gateways/{slug}/servers", json={"server_ids": server_ids})
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Write the failing tests** — `services/gateway/tests/test_gateways.py`

```python
"""The /api/gateways endpoints, with an in-memory stand-in for the Kubernetes API."""

from __future__ import annotations

import asyncio

import httpx2

from app.kube import gateway_resource
from app.main import Settings, create_app
from tests.test_gateway import SETUP, FakeGateways, serve, setup_admin


async def member(gw):
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    assert (await c.post("/api/login", json={"username": "bob", "password": "bob-password-123"})).status_code == 200
    return c


def test_gateway_api(db_url):
    fake = FakeGateways()

    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            bob = await member(gw)
            notes = (await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            shop = (await admin.post("/api/servers", json={"name": "shop1", "url": "http://127.0.0.1:9/mcp", "store": "shop1"})).json()
            assert shop["store"] == "shop1" and shop["gateways"] == []

            assert (await bob.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 403
            for slug in ("P", "a", "-ab", "ab-", "a_b", "a" * 21):
                assert (await admin.post("/api/gateways", json={"slug": slug, "name": "x"})).status_code == 400, slug

            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 201, r.text
            g = r.json()
            assert (g["slug"], g["name"], g["phase"], g["servers"]) == ("prod", "Production", "Provisioning", [])
            assert (g["url"], g["mcp_url"]) == ("http://prod.gw.localhost", "http://prod.gw.localhost/mcp")
            assert fake.items["prod"]["kind"] == "McpGateway"
            assert fake.items["prod"]["spec"] == {"displayName": "Production", "publicUrl": ""}
            # The same request again is a safe retry; a different one is a conflict.
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 200
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Again"})).status_code == 409

            r = await admin.post("/api/gateways", json={"slug": "ext", "name": "External", "public_url": "https://gw.example.com/"})
            assert r.json()["mcp_url"] == "https://gw.example.com/mcp"
            assert (await admin.post("/api/gateways", json={"slug": "bad", "name": "x", "public_url": "ftp://x"})).status_code == 400

            assert (await bob.put("/api/gateways/prod/servers", json={"server_ids": [notes]})).status_code == 403
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [999]})).status_code == 400
            assert (await admin.put("/api/gateways/nope/servers", json={"server_ids": []})).status_code == 404
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [notes, shop["id"]]})).status_code == 200
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [notes]})).status_code == 200  # replaces

            gws = {g["slug"]: g for g in (await bob.get("/api/gateways")).json()}  # members can read
            assert gws["prod"]["servers"] == [{"id": notes, "name": "notes"}]
            servers = {s["name"]: s for s in (await admin.get("/api/servers")).json()}
            assert servers["notes"]["gateways"] == ["prod"] and servers["shop1"]["gateways"] == []

            assert (await bob.delete("/api/gateways/prod")).status_code == 403
            assert (await admin.delete("/api/gateways/nope")).status_code == 404
            assert (await admin.delete("/api/gateways/prod")).status_code == 202
            assert "prod" not in fake.items
            assert [g["slug"] for g in (await admin.get("/api/gateways")).json()] == ["ext"]

            # Removing a server removes its attachments.
            await admin.put("/api/gateways/ext/servers", json={"server_ids": [notes]})
            await admin.delete(f"/api/servers/{notes}")
            assert (await admin.get("/api/gateways")).json()[0]["servers"] == []

    asyncio.run(run())


def test_gateway_row_is_removed_when_the_cluster_refuses(db_url):
    fake = FakeGateways(fail=True)

    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 502 and "the cluster said no" in r.json()["error"]
            assert (await admin.get("/api/gateways")).json() == []  # no row left behind
            fake.fail = False
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 201

    asyncio.run(run())


def test_gateway_create_recovers_from_a_lost_response(db_url):
    fake = FakeGateways(lose_response=True)

    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 502 and "prod" in fake.items  # made, but we never heard back
            [g] = (await admin.get("/api/gateways")).json()
            assert (g["slug"], g["phase"]) == ("prod", "Orphaned")

            fake.lose_response = False
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 201 and r.json()["phase"] == "Provisioning"  # adopted
            [g] = (await admin.get("/api/gateways")).json()
            assert g["phase"] == "Provisioning"

            # Same name in the cluster with a different spec: not adopted.
            fake.items["other"] = gateway_resource("other", "Someone else", None)
            r = await admin.post("/api/gateways", json={"slug": "other", "name": "Mine"})
            assert r.status_code == 409
            assert (await admin.get("/api/gateways")).json()[-1]["phase"] == "Orphaned"

            # An orphan can be deleted.
            assert (await admin.delete("/api/gateways/other")).status_code == 202
            assert "other" not in fake.items

    asyncio.run(run())


def test_gateways_off_outside_a_cluster(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, setup_token=SETUP))) as gw:
            admin = await setup_admin(gw)
            r = await admin.get("/api/gateways")
            assert r.status_code == 503 and "not configured" in r.json()["error"]

    asyncio.run(run())
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/test_gateways.py -v`
Expected: FAIL (`create_app()` has no `gateways` argument).

- [ ] **Step 4: Schema** — in `app/db.py` `SCHEMA`, add a column to `servers` after `enabled`:

```sql
    -- Set when the store operator registered this server for a store.
    store       TEXT,
```

and append:

```sql
-- Gateways are separate edge deployments; each serves /mcp for its attached servers.
CREATE TABLE IF NOT EXISTS gateways (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    public_url TEXT,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_servers (
    gateway_id BIGINT NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
    server_id  BIGINT NOT NULL REFERENCES servers(id)  ON DELETE CASCADE,
    PRIMARY KEY (gateway_id, server_id)
);
```

- [ ] **Step 5: Generalize `app/kube.py`**

Replace the docstring, `StoreExists`, the `Kube.__init__` and `Kube.create`; add `gateway_resource` and `create_object`:

```python
"""Store and McpGateway custom resources in the Kubernetes API, with the pod's
ServiceAccount.

The control plane only creates, lists and deletes these objects; the operator
does the provisioning. Its Role allows nothing else.
"""
```

```python
class AlreadyExists(Exception):
    pass


def gateway_resource(slug: str, name: str, public_url: str | None) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "McpGateway",
        "metadata": {"name": slug},
        "spec": {"displayName": name, "publicUrl": public_url or ""},
    }


class Kube:
    def __init__(self, namespace: str, plural: str = "stores"):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.url = f"https://{host}:{port}/apis/{GROUP}/v1/namespaces/{namespace}/{plural}"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
```

```python
    async def create_object(self, obj: dict) -> dict:
        r = await self._request("POST", json=obj)
        if r.status_code == 409:
            raise AlreadyExists(obj["metadata"]["name"])
        r.raise_for_status()
        return r.json()

    async def create(self, name: str, template: str, owner_id: int, owner: str) -> dict:
        return await self.create_object(store_resource(name, template, owner_id, owner))
```

In `app/stores.py` and `tests/test_stores.py`: rename every `StoreExists` to `AlreadyExists`.

- [ ] **Step 6: Servers show `store` and `gateways`; `/api/connect` goes** — `app/api.py`

- `_server(row, status)` → `_server(row, status, gateways=())`, and add to the returned dict after `"created_at"`:

```python
        "store": row["store"],
        "gateways": list(gateways),
```

- `list_servers`:

```python
@handler
async def list_servers(request: Request) -> Response:
    user = current_user(request)
    ups: Upstreams = request.app.state.upstreams
    conn = request.app.state.db
    allowed = store.granted_server_ids(conn, user)
    on = collections.defaultdict(list)
    for r in conn.execute(
        "SELECT gs.server_id, g.slug FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id ORDER BY g.slug"
    ):
        on[r["server_id"]].append(r["slug"])
    rows = conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
    return JSONResponse([_server(r, ups.status.get(r["id"]), on[r["id"]])
                         for r in rows if allowed is None or r["id"] in allowed])
```

- `create_server`: after `header, value = _auth_fields(data)` add `store_name = _str(data, "store", required=False)`, and insert it:

```python
    sid = conn.execute(
        "INSERT INTO servers (name, url, auth_header, auth_value, enabled, store, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, url, header, value, 1 if data.get("enabled", True) else 0, store_name, time.time()),
    ).fetchone()["id"]
```

- Delete `connect_info` and its `Route("/api/connect", ...)` (each gateway now has its own URL; the dashboard reads them from `/api/gateways`).
- In `tests/test_oauth.py` delete the two `/api/connect` assertions (`assert (await admin.get("/api/connect")).json() == ...`).

- [ ] **Step 7: Write `app/gateways.py`**

```python
"""Gateways: separate edge deployments, each serving /mcp for the servers attached to it.

The control plane keeps the gateway's row (what it serves, read by the edge on
every request) and an McpGateway resource (that it runs); the store operator
installs the edge release gw-<slug> from that resource.
"""

from __future__ import annotations

import collections
import re
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import db as store
from .api import HTTPError, _server_ids, _str, _url, body, current_user, handler, require_admin
from .kube import AlreadyExists, gateway_resource

# 2-20 chars: it is the release name gw-<slug> and the host <slug>.gw.localhost.
SLUG = re.compile(r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$")


def _backend(request: Request):
    kube = request.app.state.gateways
    if kube is None:
        raise HTTPError(503, "gateway deployment is not configured")
    return kube


def _view(row: dict, servers: list[dict], cr: dict | None) -> dict:
    meta, status = (cr or {}).get("metadata") or {}, (cr or {}).get("status") or {}
    url = status.get("url") or f"http://{row['slug']}.gw.localhost"
    if meta.get("deletionTimestamp"):
        phase = "Deleting"
    else:
        phase = status.get("phase", "Provisioning") if cr else "Missing"
    return {
        "slug": row["slug"],
        "name": row["name"],
        "public_url": row["public_url"],
        "url": url,
        "mcp_url": (row["public_url"] or url) + "/mcp",
        "phase": phase,
        "message": status.get("message", ""),
        "servers": servers,
    }


def _orphan(cr: dict) -> dict:
    """An McpGateway with no row, e.g. its create response was lost and nobody retried."""
    spec = cr.get("spec") or {}
    row = {"slug": cr["metadata"]["name"], "name": spec.get("displayName", ""), "public_url": spec.get("publicUrl") or None}
    return {**_view(row, [], cr), "phase": "Orphaned", "message": "running without a gateway row: retry the create or delete it"}


def _by_slug(conn, slug: str) -> dict | None:
    return conn.execute("SELECT * FROM gateways WHERE slug = ?", (slug,)).fetchone()


def _row(request: Request) -> dict:
    row = _by_slug(request.app.state.db, request.path_params["slug"])
    if row is None:
        raise HTTPError(404, "no such gateway")
    return row


def _attached(conn) -> dict[int, list[dict]]:
    out = collections.defaultdict(list)
    for r in conn.execute(
        "SELECT gs.gateway_id, s.id, s.name FROM gateway_servers gs"
        " JOIN servers s ON s.id = gs.server_id ORDER BY s.name"
    ):
        out[r["gateway_id"]].append({"id": r["id"], "name": r["name"]})
    return out


@handler
async def list_gateways(request: Request) -> Response:
    current_user(request)  # members read it too: it tells them which URL to use
    crs = {c["metadata"]["name"]: c for c in await _backend(request).list()}
    conn = request.app.state.db
    attached = _attached(conn)
    rows = conn.execute("SELECT * FROM gateways ORDER BY created_at").fetchall()
    known = {g["slug"] for g in rows}
    return JSONResponse(
        [_view(g, attached[g["id"]], crs.get(g["slug"])) for g in rows]
        + [_orphan(cr) for slug, cr in sorted(crs.items()) if slug not in known]
    )


@handler
async def create_gateway(request: Request) -> Response:
    """Safe to retry. Whatever failed before (the cluster refused, the response
    was lost after the resource was made, a crash between the row and the
    resource), sending the same request again finishes the job."""
    require_admin(request)
    kube = _backend(request)
    data = await body(request)
    slug, name = _str(data, "slug"), _str(data, "name")
    if not SLUG.match(slug):
        raise HTTPError(400, "slug: 2-20 of a-z 0-9 and -, starting with a letter")
    public_url = _url(_str(data, "public_url")).rstrip("/") if data.get("public_url") else None
    conn = request.app.state.db
    taken = HTTPError(409, f"a gateway named '{slug}' already exists")

    row = _by_slug(conn, slug)
    inserted = row is None
    if inserted:
        try:
            row = conn.execute(
                "INSERT INTO gateways (slug, name, public_url, created_at) VALUES (?, ?, ?, ?) RETURNING *",
                (slug, name, public_url, time.time()),
            ).fetchone()
        except store.IntegrityError:  # a concurrent create of the same slug
            raise taken from None
    elif (row["name"], row["public_url"]) != (name, public_url):
        raise taken

    def undo():
        if inserted:
            conn.execute("DELETE FROM gateways WHERE id = ?", (row["id"],))

    want = gateway_resource(slug, name, public_url)
    try:
        cr = await kube.create_object(want)
    except AlreadyExists:
        cr = await kube.get(slug)
        if cr is None or cr.get("spec") != want["spec"]:
            undo()
            raise taken from None
        # An earlier attempt made it: adopt it.
    except Exception as e:  # no row without a resource, so a retry starts clean
        undo()
        raise HTTPError(502, f"could not create the gateway deployment: {e}"[:300]) from None
    return JSONResponse(_view(row, _attached(conn)[row["id"]], cr), status_code=201 if inserted else 200)


@handler
async def delete_gateway(request: Request) -> Response:
    """Also removes an orphaned McpGateway that has no row."""
    require_admin(request)
    kube = _backend(request)
    slug = request.path_params["slug"]
    conn = request.app.state.db
    row = _by_slug(conn, slug)
    if row is None and await kube.get(slug) is None:
        raise HTTPError(404, "no such gateway")
    await kube.delete(slug)  # the operator uninstalls the edge
    if row is not None:
        conn.execute("DELETE FROM gateways WHERE id = ?", (row["id"],))
    return JSONResponse({"ok": True}, status_code=202)


@handler
async def set_gateway_servers(request: Request) -> Response:
    """Replace the attached set. Edges read it on every request, so it applies at once."""
    require_admin(request)
    row = _row(request)
    conn = request.app.state.db
    ids = _server_ids(conn, await body(request))
    with conn.transaction() as tx:
        tx.execute("DELETE FROM gateway_servers WHERE gateway_id = ?", (row["id"],))
        tx.executemany(
            "INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?)", [(row["id"], s) for s in ids]
        )
    return JSONResponse({"ok": True, "server_ids": ids})


routes = [
    Route("/api/gateways", list_gateways, methods=["GET"]),
    Route("/api/gateways", create_gateway, methods=["POST"]),
    Route("/api/gateways/{slug}", delete_gateway, methods=["DELETE"]),
    Route("/api/gateways/{slug}/servers", set_gateway_servers, methods=["PUT"]),
]
```

- [ ] **Step 8: Wire it in `main.py`**

- import: `from . import api, chat, gateways as gateway_api, oauth, setup, stores as store_api`
- `def create_app(settings: Settings | None = None, stores=None, gateways=None) -> ASGIApp:`
- routes: add `*gateway_api.routes,` after `*store_api.routes,`.
- state, after `app.state.stores = ...`:

```python
    app.state.gateways = gateways if gateways is not None else (
        Kube(settings.stores_namespace, "mcpgateways") if settings.stores_namespace else None
    )
```

- [ ] **Step 9: Run the suite**

Run: `uv run pytest -v`
Expected: PASS, including the 4 new gateway tests.

- [ ] **Step 10: Stop point** — report "Task 3 done". The user commits.

---

### Task 4: Edge mode

**Files:**
- Create: `services/gateway/tests/test_edge.py`
- Modify: `app/db.py` (schema), `app/upstream.py`, `app/main.py`, `tests/test_gateway.py`, `tests/test_oauth.py`

**Interfaces:**
- Consumes: Task 3 tables, `FakeGateways`, `add_gateway`.
- Produces: `Settings.mode: str` (`"control"` | `"edge"`), `Settings.gateway_slug: str`; env `GATEWAY_MODE`, `GATEWAY_SLUG`. `Upstreams(db, *, call_timeout, list_timeout, refresh_interval, calls_per_minute=60, gateway_slug: str | None = None)`; its rate limit is per user across the organization, counted in table `rate_limits(user_id, minute, calls)`. Column `audit_log.gateway`. Edges reject Host headers outside `<slug>.gw.localhost`, the public URL's host, `localhost`, `127.0.0.1` with 421. Control serves `/api/*`, `/healthz`, `/readyz` and no `/mcp`; edge serves `/mcp`, OAuth (when `public_url`), `/healthz`, `/readyz` and no `/api`. Test helpers in `tests/test_gateway.py`: `edge_settings(db_url, slug, **kw) -> Settings`, `async wait_tools(url, key, n, timeout=5) -> set[str]`.

- [ ] **Step 1: Add the helpers** — `tests/test_gateway.py`, after `add_gateway`:

```python
def edge_settings(db_url: str, slug: str, **kw) -> Settings:
    return Settings(mode="edge", gateway_slug=slug, database_url=db_url, **{"refresh_interval_s": 0.5, **kw})


async def wait_tools(url: str, key: str, n: int, timeout: float = 5) -> set[str]:
    """An edge lists a newly attached server once its own health loop has checked it."""
    deadline = time.time() + timeout
    while True:
        async with gateway_client(url, key) as mcp:
            names = {t.name for t in (await mcp.list_tools()).tools}
        if len(names) >= n:
            return names
        assert time.time() < deadline, names
        await asyncio.sleep(0.1)
```

- [ ] **Step 2: Write the failing tests** — `services/gateway/tests/test_edge.py`

```python
"""Edges: separate gateway deployments, each serving only the servers attached to it."""

from __future__ import annotations

import asyncio
import time

import httpx2

from app import db as store
from app.main import ConfigError, Settings, create_app
from tests.test_gateway import (
    FX_TOKEN, NOTES_KEY, SETUP, FakeGateways, _free_port, add_gateway, edge_settings,
    gateway_client, http_app, serve, setup_admin, text, wait_tools,
)

from demo_mcp import build_fx, build_notes  # noqa: E402  (tests.test_gateway puts it on sys.path)


def test_mode_and_slug_are_checked(db_url):
    for s in (Settings(mode="edge", database_url=db_url), Settings(mode="nope", database_url=db_url, setup_token=SETUP)):
        try:
            create_app(s)
        except ConfigError:
            continue
        raise AssertionError(f"started with mode={s.mode!r} slug={s.gateway_slug!r}")


def test_each_edge_serves_only_its_servers(db_url):
    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(create_app(Settings(database_url=db_url, setup_token=SETUP, refresh_interval_s=0.5),
                             gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            fid = (await admin.post("/api/servers", json={"name": "fx", "url": f"{fx}/mcp",
                   "auth": {"type": "bearer", "token": FX_TOKEN}})).json()["id"]
            await add_gateway(admin, "prod", [fid])
            await add_gateway(admin, "dev", [nid])
            # bob may use both servers; the gateway he connects to decides what he gets.
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "server_ids": [nid, fid]})
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            with (
                serve(create_app(edge_settings(db_url, "prod"))) as prod,
                serve(create_app(edge_settings(db_url, "prod"))) as prod_b,  # a second replica of prod
                serve(create_app(edge_settings(db_url, "dev"))) as dev,
            ):
                assert await wait_tools(prod, key, 2) == {"fx__convert", "fx__list"}
                assert await wait_tools(prod_b, key, 2) == {"fx__convert", "fx__list"}
                assert await wait_tools(dev, key, 2) == {"notes__save_note", "notes__list"}

                # Stateless: initialize on one replica, call on the other. No session to stick to.
                h = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}
                async with httpx2.AsyncClient(timeout=10) as raw:
                    init = await raw.post(f"{prod}/mcp", headers=h, json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}}})
                    assert init.status_code == 200, init.text
                    assert "mcp-session-id" not in init.headers
                    version = init.json()["result"]["protocolVersion"]
                    r = await raw.post(f"{prod_b}/mcp", headers={**h, "MCP-Protocol-Version": version}, json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "fx__list", "arguments": {}}})
                    assert r.status_code == 200 and r.json()["result"]["isError"] is False, r.text

                    # A valid key is not enough with a foreign Host header.
                    r = await raw.post(f"{prod}/mcp", headers={**h, "Host": "evil.example"}, json={})
                    assert r.status_code == 421

                # A hand-typed call for a server not attached here: refused, audited, never forwarded.
                async with gateway_client(prod, key) as mcp:
                    r = await mcp.call_tool("notes__save_note", {"text": "leaked"})
                    assert r.is_error and "Unknown tool" in text(r)
                    assert not (await mcp.call_tool("fx__list", {})).is_error
                log = (await admin.get("/api/audit")).json()
                assert [(e["gateway"], e["tool"], e["ok"]) for e in log[:3]] == [
                    ("prod", "fx__list", 1), ("prod", "notes__save_note", 0), ("prod", "fx__list", 1)]
                async with gateway_client(dev, key) as mcp:
                    assert "leaked" not in text(await mcp.call_tool("notes__list", {}))

                # Detaching applies on the next request.
                await admin.put("/api/gateways/prod/servers", json={"server_ids": []})
                async with gateway_client(prod, key) as mcp:
                    assert (await mcp.list_tools()).tools == []

                # A deleted gateway's edge is no longer ready.
                assert (await httpx2.AsyncClient(base_url=prod).get("/readyz")).status_code == 200
                await admin.delete("/api/gateways/prod")
                assert (await httpx2.AsyncClient(base_url=prod).get("/readyz")).status_code == 503

    asyncio.run(run())


def test_rate_limit_is_shared_by_every_pod(db_url):
    from app.upstream import Upstreams

    def pod(**kw):
        return Upstreams(store.connect(db_url, create_schema=False), call_timeout=1, list_timeout=1,
                         refresh_interval=30, calls_per_minute=3, **kw)

    store.connect(db_url).close()  # schema
    control, prod, dev = pod(), pod(gateway_slug="prod"), pod(gateway_slug="dev")
    alice = {"id": 1, "username": "alice", "role": "admin"}

    async def run():
        for ups in (control, prod, dev):  # one call each: three pods, one budget
            assert "Unknown tool" in text(await ups.call_tool(alice, "x__y", {}))
        assert "Too many calls" in text(await prod.call_tool(alice, "x__y", {}))

    asyncio.run(run())


def test_planes_serve_only_their_own_routes(db_url):
    async def run():
        with (
            serve(create_app(Settings(database_url=db_url, setup_token=SETUP))) as ctl,
            serve(create_app(edge_settings(db_url, "main"))) as edge,
        ):
            await setup_admin(ctl)
            assert (await httpx2.AsyncClient(base_url=ctl).post("/mcp", json={})).status_code == 404
            assert (await httpx2.AsyncClient(base_url=edge).get("/api/me")).status_code == 404
            assert (await httpx2.AsyncClient(base_url=edge).get("/healthz")).status_code == 200

    asyncio.run(run())


def test_oauth_token_works_only_at_the_gateway_it_was_issued_for(db_url):
    conn = store.connect(db_url)
    uid = store.create_user(conn, "bob", "bob-password-123", "admin")
    conn.execute("INSERT INTO org (id, name, created_at) VALUES (1, 'Acme', 0)")
    conn.execute("INSERT INTO oauth_clients (client_id, info, created_at) VALUES ('c1', '{}', 0)")
    conn.execute("INSERT INTO gateways (slug, name, created_at) VALUES ('prod', 'Prod', 0), ('dev', 'Dev', 0)")
    p_port, d_port = _free_port(), _free_port()
    prod_url, dev_url = f"http://127.0.0.1:{p_port}", f"http://127.0.0.1:{d_port}"
    token = "mcpo_issued-for-dev"
    conn.execute(
        "INSERT INTO oauth_tokens (token_hash, kind, user_id, client_id, scopes, resource, created_at, expires_at)"
        " VALUES (?, 'access', ?, 'c1', '', ?, 0, ?)",
        (store.sha256(token), uid, f"{dev_url}/mcp", time.time() + 600),
    )

    async def run():
        with (
            serve(create_app(edge_settings(db_url, "prod", public_url=prod_url)), port=p_port),
            serve(create_app(edge_settings(db_url, "dev", public_url=dev_url)), port=d_port),
        ):
            r = await httpx2.AsyncClient().post(f"{prod_url}/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
            async with gateway_client(dev_url, token) as mcp:
                assert (await mcp.list_tools()).tools == []  # accepted; nothing attached

    asyncio.run(run())
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/test_edge.py -v`
Expected: FAIL (`Settings` has no `mode`).

- [ ] **Step 4: Audit column and rate-limit table** — in `app/db.py` `SCHEMA`, `audit_log`, after `server TEXT,`:

```sql
    gateway     TEXT,  -- the edge's slug; NULL for calls from the dashboard and chat
```

and append:

```sql
-- Tool calls per user per clock minute, shared by every pod (Upstreams._over_limit).
CREATE TABLE IF NOT EXISTS rate_limits (
    user_id BIGINT NOT NULL,
    minute  BIGINT NOT NULL,
    calls   INTEGER NOT NULL,
    PRIMARY KEY (user_id, minute)
);
```

- [ ] **Step 5: `Upstreams` filters by gateway** — `app/upstream.py`

- Constructor: add the keyword argument `gateway_slug: str | None = None` and `self.gateway_slug = gateway_slug`.
- Add under the constructor:

```python
    def _attached(self) -> set[int] | None:
        """Server ids attached to this edge's gateway, read on every request;
        None on the control plane, which serves every server."""
        if self.gateway_slug is None:
            return None
        rows = self.db.execute(
            "SELECT gs.server_id FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id WHERE g.slug = ?",
            (self.gateway_slug,),
        )
        return {r["server_id"] for r in rows}
```

- `refresh_all`, after fetching `rows`:

```python
        attached = self._attached()
        rows = [r for r in rows if attached is None or r["id"] in attached]
```

- `list_tools`: after `allowed = ...` add `attached = self._attached()`, and the skip becomes:

```python
            if (allowed is not None and row["id"] not in allowed) or (attached is not None and row["id"] not in attached):
                continue
```

- `call_tool`: after `allowed = ...` add `attached = self._attached()`; the unknown-tool branch becomes:

```python
        elif (
            not sep or not tool or row is None
            or (allowed is not None and row["id"] not in allowed)
            or (attached is not None and row["id"] not in attached)
        ):
```

- the audit insert gains the gateway:

```python
        self.db.execute(
            "INSERT INTO audit_log (at, user_id, username, tool, server, gateway, ok, duration_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                user["id"],
                user["username"],
                name,
                row["name"] if row else None,
                self.gateway_slug,
                0 if result.is_error else 1,
                int((time.monotonic() - started) * 1000),
                error,
            ),
        )
```

- Replace the in-memory rate limit with the shared counter. Delete `self._recent` from the constructor and `import collections` (nothing else uses it), and replace `_over_limit`:

```python
    def _over_limit(self, user_id: int) -> bool:
        """60 calls a minute per user across the whole organization: every pod,
        gateway and key counts against the same row."""
        # shortcut: fixed one-minute windows, so up to 2x the limit can land around a
        # minute boundary; switch to a sliding window if that matters.
        minute = int(time.time() // 60)
        calls = self.db.execute(
            "INSERT INTO rate_limits (user_id, minute, calls) VALUES (?, ?, 1)"
            " ON CONFLICT (user_id, minute) DO UPDATE SET calls = rate_limits.calls + 1 RETURNING calls",
            (user_id, minute),
        ).fetchone()["calls"]
        if calls == 1:  # this user's first call this minute: drop their old windows
            self.db.execute("DELETE FROM rate_limits WHERE user_id = ? AND minute < ?", (user_id, minute))
        return calls > self.calls_per_minute
```

- [ ] **Step 6: Control vs edge in `main.py`**

- `Settings`: add after `database_url`:

```python
    mode: str = "control"  # control: the /api behind the dashboard; edge: /mcp for one gateway
    gateway_slug: str = ""  # edge only: which gateway's attached servers to serve
```

- `from_env`: add `mode=env("GATEWAY_MODE", cls.mode).strip(),` and `gateway_slug=env("GATEWAY_SLUG", "").strip(),`.
- imports: add `from starlette.applications import Starlette`.
- Replace `create_app` from its first line down to `app.state.db = conn` with:

```python
def create_app(settings: Settings | None = None, stores=None, gateways=None) -> ASGIApp:
    settings = settings or Settings.from_env()
    if settings.mode not in ("control", "edge"):
        raise ConfigError("GATEWAY_MODE must be control or edge")
    edge = settings.mode == "edge"
    if edge and not settings.gateway_slug:
        raise ConfigError("an edge needs GATEWAY_SLUG")
    if not settings.database_url:
        raise ConfigError("DATABASE_URL is required")
    conn = store.connect(settings.database_url, create_schema=not edge)
    if not edge and conn.execute("SELECT 1 FROM org").fetchone() is None and len(settings.setup_token) < 16:
        raise ConfigError("not set up yet: set SETUP_TOKEN (16+ chars) and finish setup in the dashboard")

    upstreams = Upstreams(
        conn,
        call_timeout=settings.call_timeout_s,
        list_timeout=settings.list_timeout_s,
        refresh_interval=settings.refresh_interval_s,
        calls_per_minute=settings.calls_per_minute,
        gateway_slug=settings.gateway_slug if edge else None,
    )

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def readyz(_: Request) -> Response:
        # An edge is ready only while its gateway exists; both need the database.
        try:
            if edge:
                ok = conn.execute("SELECT 1 FROM gateways WHERE slug = ?", (settings.gateway_slug,)).fetchone() is not None
            else:
                ok = conn.execute("SELECT 1").fetchone() is not None
        except Exception:
            ok = False
        return JSONResponse({"status": "ok" if ok else "not ready"}, status_code=200 if ok else 503)

    health = [Route("/healthz", healthz, methods=["GET"]), Route("/readyz", readyz, methods=["GET"])]
    if edge:
        app = _edge_app(settings, conn, upstreams, health)
    else:
        app = Starlette(routes=[
            *health,
            *api.routes,
            *setup.routes,
            *store_api.routes,
            *gateway_api.routes,
            Route("/api/chat", chat.chat, methods=["POST"]),
        ])
    app.state.db = conn
```

  Keep everything after `app.state.db = conn` (state, lifespan), and change the last line to:

```python
    return ApiKeyGuard(app, conn, settings.public_url) if edge else app
```

- Move the MCP server and OAuth construction that used to sit in `create_app` into a new function above it:

```python
def _edge_app(settings: Settings, conn, upstreams: Upstreams, health: list[Route]):
    """/mcp for one gateway's attached servers, plus OAuth when PUBLIC_URL is set."""

    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=upstreams.list_tools(ctx.request.state.user))

    async def on_call_tool(ctx, params) -> CallToolResult:
        return await upstreams.call_tool(ctx.request.state.user, params.name, params.arguments)

    server = Server(
        "team-mcp-gateway",
        instructions=(
            "Tools from several MCP servers. Each tool is named <server>__<tool>; "
            "the part before the double underscore says which server runs it."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    auth = provider = None
    if settings.public_url:
        auth = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # ApiKeyGuard checks the token's resource itself.
            validate_token_resource=False,
        )
        provider = oauth.Provider(conn, settings.public_url, settings.oauth_redirect_uris)

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        # No MCP sessions are kept: any replica answers any request, no sticky routing.
        # The gateway needs no session features (no server-to-client requests, no resumption).
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=_transport_security(settings),
        # No token_verifier: the SDK serves the OAuth endpoints, ApiKeyGuard guards /mcp.
        auth=auth,
        auth_server_provider=provider,
        custom_starlette_routes=[*(oauth.routes if provider else []), *health],
    )


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection: only this edge's own hostnames. The SDK's
    "host:*" matches only a Host header with a port, so list both forms."""
    names = [f"{settings.gateway_slug}.gw.localhost", "localhost", "127.0.0.1"]  # Ingress; port-forward and tests
    if settings.public_url:
        names.append(urlsplit(settings.public_url).hostname)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h for n in names for h in (n, f"{n}:*")],
        allowed_origins=[o for n in names for o in (f"http://{n}", f"http://{n}:*")]
        + ([settings.public_url] if settings.public_url else []),
    )
```

  Imports for these: `from urllib.parse import urlsplit` and `from mcp.server.transport_security import TransportSecuritySettings`.

  Update the module docstring: `"""The gateway web app, in one of two modes: control (the /api behind the dashboard) or edge (/mcp for one gateway's servers)."""`

- [ ] **Step 7: Run the new tests**

Run: `uv run pytest tests/test_edge.py -v`
Expected: PASS (5 tests).

- [ ] **Step 8: Point the existing `/mcp` tests at edges** — `tests/test_gateway.py`

`test_the_whole_gateway`:
- `serve(create_app(settings)) as gw,` → `serve(create_app(settings, gateways=FakeGateways())) as gw,` then a new item after it: `serve(create_app(edge_settings(db_url, "main", call_timeout_s=1, list_timeout_s=1))) as edge,`
- Right before the `# One MCP endpoint` comment, insert:

```python
            await add_gateway(admin, "main", list(ids.values()))
            await wait_tools(edge, key, 5)
```

- `async with gateway_client(gw, key) as mcp:` → `async with gateway_client(edge, key) as mcp:`
- `f"{gw}/mcp",` (the revoked-key raw POST) → `f"{edge}/mcp",`

`test_members_see_only_granted_servers`:
- `serve(create_app(settings)) as gw,` → `serve(create_app(settings, gateways=FakeGateways())) as gw,` plus `serve(create_app(edge_settings(db_url, "main"))) as edge,`
- After the `for body in (...)` loop that fills `ids`, add `await add_gateway(admin, "main", list(ids.values()))`.
- `async with gateway_client(gw, key) as mcp:` → `await wait_tools(edge, key, 2)` on the line before, then `async with gateway_client(edge, key) as mcp:`

`test_admin_deletes_a_user`:
- `with serve(create_app(settings)) as gw:` → `with serve(create_app(settings)) as gw, serve(create_app(edge_settings(db_url, "main"))) as edge:`
- `r = await httpx2.AsyncClient(base_url=gw).post(` → `r = await httpx2.AsyncClient(base_url=edge).post(`

`test_rate_limit_per_user`: no change (`Upstreams` on the control plane, `gateway_slug=None`).

- [ ] **Step 9: Point the OAuth tests at an edge** — `tests/test_oauth.py`

Imports: `from tests.test_gateway import ADMIN, NOTES_KEY, SETUP, FakeGateways, _free_port, add_gateway, edge_settings, gateway_client, http_app, serve, setup_admin, wait_tools`

`test_oauth_flow` (`gw` stays the edge's public URL; the control plane becomes `ctl`):
- `settings = Settings(...)` → `settings = Settings(database_url=db_url, setup_token=SETUP, refresh_interval_s=0.5)`
- `serve(create_app(settings), port=port),` → two items:

```python
            serve(create_app(settings, gateways=FakeGateways())) as ctl,
            serve(create_app(edge_settings(db_url, "main", public_url=gw)), port=port),
```

- `admin = await setup_admin(gw)` → `admin = await setup_admin(ctl)`
- After `await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": [nid]})` add `await add_gateway(admin, "main", [nid])`
- The block

```python
            async with gateway_client(gw, tokens["access_token"]) as mcp:
                names = {t.name for t in (await mcp.list_tools()).tools}
            assert names == {"notes__save_note", "notes__list"}
```

  → `assert await wait_tools(gw, tokens["access_token"], 2) == {"notes__save_note", "notes__list"}`
- `bob = await login(gw, "bob", "bob-password-123")` → `bob = await login(ctl, "bob", "bob-password-123")`

`test_oauth_off_without_public_url`:

```python
def test_oauth_off_without_public_url(db_url):
    async def run():
        with (
            serve(create_app(Settings(database_url=db_url, setup_token=SETUP))),
            serve(create_app(edge_settings(db_url, "main"))) as gw,
        ):
            c = httpx2.AsyncClient(base_url=gw)
            assert (await c.post("/register", json={})).status_code == 404
            assert (await c.get("/oauth/login")).status_code == 404
            r = await c.post("/mcp", json={})
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"

    asyncio.run(run())
```

- [ ] **Step 10: Pin single-use OAuth codes across pods** — append to `tests/test_oauth.py` (the code under test is already atomic: `exchange_authorization_code` deletes by hash and checks the row count; this test proves it under real concurrency, two pools as two pods):

```python
def test_a_code_is_redeemed_once_even_concurrently(db_url):
    import threading
    import time
    import types

    from mcp.server.auth.provider import AuthorizationCode, TokenError

    from app import db as store, oauth

    conn = store.connect(db_url)
    uid = store.create_user(conn, "bob", "bob-password-123", "member")
    conn.execute("INSERT INTO oauth_clients (client_id, info, created_at) VALUES ('c1', '{}', 0)")
    code = "the-one-code"
    conn.execute(
        "INSERT INTO oauth_codes (code_hash, client_id, user_id, params, expires_at) VALUES (?, 'c1', ?, '{}', ?)",
        (store.sha256(code), uid, time.time() + 300),
    )
    grant = AuthorizationCode(
        code=code, scopes=[], expires_at=time.time() + 300, client_id="c1", code_challenge=CHALLENGE,
        redirect_uri=CALLBACK, redirect_uri_provided_explicitly=True, resource=None, subject=str(uid),
    )
    results, barrier = [], threading.Barrier(2)

    def redeem():
        provider = oauth.Provider(store.connect(db_url, create_schema=False), "http://gw", ())
        barrier.wait()  # both start together
        try:
            asyncio.run(provider.exchange_authorization_code(types.SimpleNamespace(client_id="c1"), grant))
            results.append("ok")
        except TokenError:
            results.append("refused")

    threads = [threading.Thread(target=redeem) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["ok", "refused"]
```

  If `AuthorizationCode` rejects a field, build it with the same fields `Provider.load_authorization_code` uses.

- [ ] **Step 11: Run the suite**

Run: `uv run pytest -v`
Expected: PASS, every test.

- [ ] **Step 12: Stop point** — report "Task 4 done". The user commits.

---

### Task 5: Operator deploys gateways

**Files:**
- Create: `charts/store-operator/crds/mcpgateway.yaml`
- Modify: `services/store-operator/store_operator/manifests.py`, `handlers.py`, `gateway.py`, `services/store-operator/tests/test_manifests.py`, `tests/test_gateway_sync.py`, `services/store-operator/Dockerfile`, `charts/store-operator/templates/rbac.yaml`, `templates/deployment.yaml`, `values.yaml`, `Chart.yaml`

**Interfaces:**
- Consumes: the `McpGateway` shape from Task 3 (`metadata.name` = slug, `spec.displayName`, `spec.publicUrl`); the chart values from Task 6 (`mode`, `slug`, `replicas`, `image.tag`, `ingress.host`, `ingress.extraHosts`, `publicUrl`, `stores.enabled`). Deployment name `gw-<slug>-gateway`.
- Produces: `manifests.GATEWAY_PLURAL = "mcpgateways"`, `edge_release(slug) -> str`, `edge_url(slug) -> str`, `edge_values(slug, spec, image_tag) -> dict` (adds `ingress.extraHosts` for a public URL), handlers `on_gateway_created`, `reconcile_gateway` (15s, Provisioning → Ready/Failed), `repair_gateway` (60s, reinstalls a Ready gateway whose Deployment is gone), `on_gateway_deleted`, `EDGE_TIMEOUT_S = 300`, `timed_out(created_iso, now, limit_s=TIMEOUT_S)`. Status `phase` Provisioning/Ready/Failed, `url`, `message`.

- [ ] **Step 1: Write the failing unit tests** — append to `services/store-operator/tests/test_manifests.py`:

```python
def test_edge_values():
    assert m.edge_release("prod") == "gw-prod"
    assert m.edge_url("prod") == "http://prod.gw.localhost"
    v = m.edge_values("prod", {"displayName": "Production", "publicUrl": ""}, "0.2.0")
    assert v == {
        "mode": "edge", "slug": "prod", "replicas": 2, "image": {"tag": "0.2.0"},
        "ingress": {"host": "prod.gw.localhost"}, "stores": {"enabled": False},
    }
    v = m.edge_values("ext", {"displayName": "External", "publicUrl": "https://gw.example.com"}, "0.2.0")
    assert v["publicUrl"] == "https://gw.example.com"
    # The public host gets its own Ingress rule; the local one stays.
    assert v["ingress"] == {"host": "ext.gw.localhost", "extraHosts": ["gw.example.com"]}


def test_edge_timeout_is_shorter():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    six_min_ago = (now - timedelta(minutes=6)).isoformat()
    assert m.timed_out(six_min_ago, now, m.EDGE_TIMEOUT_S)
    assert not m.timed_out(six_min_ago, now)  # stores get 15 minutes
```

and in `tests/test_gateway_sync.py`, after the `assert (s["name"], s["url"], ...)` line:

```python
        assert s["store"] == "shop1"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd services/store-operator && uv run pytest -v`
Expected: FAIL (`edge_release` missing; `store` not sent).

- [ ] **Step 3: `manifests.py`** — add after `PLURAL = "stores"`:

```python
GATEWAY_PLURAL = "mcpgateways"
EDGE_TIMEOUT_S = 5 * 60
```

append at the end:

```python
def edge_release(slug: str) -> str:
    return f"gw-{slug}"


def edge_url(slug: str) -> str:
    return f"http://{slug}.gw.localhost"


def edge_values(slug: str, spec: dict, image_tag: str) -> dict:
    """charts/gateway in edge mode: /mcp only, no Kubernetes rights, two replicas."""
    values = {
        "mode": "edge",
        "slug": slug,
        "replicas": 2,
        "image": {"tag": image_tag},
        "ingress": {"host": f"{slug}.gw.localhost"},
        "stores": {"enabled": False},
    }
    if spec.get("publicUrl"):
        values["publicUrl"] = spec["publicUrl"]
        # A tunnel or proxy in front sends the public Host header; route it too.
        values["ingress"]["extraHosts"] = [urlsplit(spec["publicUrl"]).hostname]
    return values
```

  with `from urllib.parse import urlsplit` at the top of `manifests.py`.

and change `timed_out`:

```python
def timed_out(created_iso: str, now: datetime, limit_s: int = TIMEOUT_S) -> bool:
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (now - created).total_seconds() > limit_s
```

- [ ] **Step 4: `gateway.py`** — in `upsert_server`, the POST branch:

```python
            self._call("POST", "/api/servers", {"name": name, "store": name, **body})
```

- [ ] **Step 5: `handlers.py`** — add under `MAX_ATTEMPTS = 3`:

```python
EDGE_IMAGE_TAG = os.environ.get("EDGE_IMAGE_TAG", "0.2.0")
```

append at the end:

```python
# ------------------------------------------------------------------ gateways
# An McpGateway becomes the Helm release gw-<slug> (charts/gateway, edge mode)
# next to the control plane. No namespace or guardrails of its own.


@kopf.on.create(m.GROUP, m.VERSION, m.GATEWAY_PLURAL)
def on_gateway_created(name, spec, patch, retry, logger, **_):
    patch.status.update(phase="Provisioning", message="installing", url=m.edge_url(name))
    try:
        _helm(m.edge_release(name), "gateway", NAMESPACE, m.edge_values(name, spec, EDGE_IMAGE_TAG))
    except Exception as e:
        if retry + 1 >= MAX_ATTEMPTS:
            patch.status.update(phase="Failed", message=str(e)[:500])
            return
        raise kopf.TemporaryError(str(e)[:500], delay=20)
    patch.status["message"] = "waiting for the edge pods"
    logger.info(f"gateway {name}: installed {m.edge_release(name)}")


@kopf.timer(m.GROUP, m.VERSION, m.GATEWAY_PLURAL, interval=15)
def reconcile_gateway(name, meta, status, patch, logger, **_):
    if status.get("phase") != "Provisioning":
        return
    if _deployment_ready(AppsV1Api(), NAMESPACE, f"{m.edge_release(name)}-gateway"):
        patch.status.update(phase="Ready", message="")
        logger.info(f"gateway {name}: ready")
    elif m.timed_out(meta["creationTimestamp"], datetime.now(timezone.utc), m.EDGE_TIMEOUT_S):
        patch.status.update(phase="Failed", message=f"no ready edge pod after {m.EDGE_TIMEOUT_S // 60} min")


def _deployment_exists(apps: AppsV1Api, ns: str, name: str) -> bool:
    try:
        apps.read_namespaced_deployment(name, ns)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


@kopf.timer(m.GROUP, m.VERSION, m.GATEWAY_PLURAL, interval=60)
def repair_gateway(name, spec, meta, status, logger, **_):
    """Reinstall a Ready gateway's edge if its release was removed by hand.
    (Restarts need nothing: kopf runs a create handler that has not finished.)"""
    if status.get("phase") != "Ready" or meta.get("deletionTimestamp"):
        return
    if not _deployment_exists(AppsV1Api(), NAMESPACE, f"{m.edge_release(name)}-gateway"):
        _helm(m.edge_release(name), "gateway", NAMESPACE, m.edge_values(name, spec, EDGE_IMAGE_TAG))
        logger.warning(f"gateway {name}: edge was missing; reinstalled")


@kopf.on.delete(m.GROUP, m.VERSION, m.GATEWAY_PLURAL)
def on_gateway_deleted(name, logger, **_):
    r = subprocess.run(["helm", "uninstall", m.edge_release(name), "-n", NAMESPACE, "--wait"],
                       capture_output=True, text=True)
    if r.returncode and "not found" not in r.stderr:
        raise kopf.TemporaryError(f"helm uninstall: {r.stderr.strip()[-300:]}", delay=10)
    logger.info(f"gateway {name}: removed")
```

- [ ] **Step 6: Run the operator tests**

Run: `uv run pytest -v`
Expected: PASS.

- [ ] **Step 7: CRD** — `charts/store-operator/crds/mcpgateway.yaml`

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: mcpgateways.mcpgateway.example.com
spec:
  group: mcpgateway.example.com
  scope: Namespaced
  # Not "Gateway": that kind belongs to the Kubernetes Gateway API.
  names: { plural: mcpgateways, singular: mcpgateway, kind: McpGateway, shortNames: [mgw] }
  versions:
    - name: v1
      served: true
      storage: true
      subresources: { status: {} }
      additionalPrinterColumns:
        - { name: Name, type: string, jsonPath: .spec.displayName }
        - { name: Phase, type: string, jsonPath: .status.phase }
        - { name: URL, type: string, jsonPath: .status.url }
        - { name: Message, type: string, jsonPath: .status.message }
        - { name: Age, type: date, jsonPath: .metadata.creationTimestamp }
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [displayName]
              properties:
                displayName: { type: string }
                publicUrl: { type: string }
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
```

- [ ] **Step 8: RBAC, env, image** —

`charts/store-operator/templates/rbac.yaml`, first rule:

```yaml
  - apiGroups: ["mcpgateway.example.com"]
    resources: ["stores", "stores/status", "mcpgateways", "mcpgateways/status"]
    verbs: ["get", "list", "watch", "patch", "update"]
```

`templates/deployment.yaml`, env list:

```yaml
            - { name: EDGE_IMAGE_TAG, value: {{ .Values.edgeImageTag | quote }} }
```

`values.yaml`: `tag: "0.2.0"` and add

```yaml
# Gateway image tag every edge (McpGateway) is installed with.
edgeImageTag: "0.2.0"
```

`Chart.yaml`: `version: 0.2.0`, `appVersion: "0.2.0"`, description: `Provisions WooCommerce stores and gateway edges from Store and McpGateway resources.`

`services/store-operator/Dockerfile`, after `COPY charts/woo-mcp /charts/woo-mcp`:

```dockerfile
COPY charts/gateway /charts/gateway
```

- [ ] **Step 9: Lint the chart**

Run: `helm lint charts/store-operator`
Expected: `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 10: Stop point** — report "Task 5 done". The user commits.

---

### Task 6: Charts and deploy values

**Files:**
- Create: `charts/postgres/Chart.yaml`, `values.yaml`, `templates/statefulset.yaml`, `templates/init-configmap.yaml`, `templates/service.yaml`, `templates/networkpolicy.yaml`, `deploy/postgres-values-local.yaml`
- Modify: `charts/gateway/values.yaml`, `templates/deployment.yaml`, `templates/rbac.yaml`, `templates/ingress.yaml`, `Chart.yaml`; delete `charts/gateway/templates/pvc.yaml`
- Modify: `deploy/gateway-values-local.yaml`, `deploy/gateway-values-prod.yaml`, `deploy/store-operator-values-local.yaml`, `charts/dashboard/values.yaml`, `README.md`

**Interfaces:**
- Consumes: env names from Tasks 1-4 (`DATABASE_URL`, `GATEWAY_MODE`, `GATEWAY_SLUG`, `SETUP_TOKEN`, `PUBLIC_URL`, `STORES_NAMESPACE`), values from Task 5 `edge_values`.
- Produces: Service `db-postgres:5432` (release `db`), Secret contract `gateway-db`, a non-superuser `gateway` role, Ingress rules for `ingress.host` plus `ingress.extraHosts`.

- [ ] **Step 1: `charts/postgres`**

`Chart.yaml`:

```yaml
apiVersion: v2
name: postgres
description: One PostgreSQL shared by the gateway control plane and every gateway edge.
type: application
version: 0.1.0
appVersion: "17"
```

`values.yaml`:

```yaml
image:
  repository: postgres
  tag: "17-alpine"
  pullPolicy: IfNotPresent
# Secret in this namespace with POSTGRES_PASSWORD (the postgres superuser; only
# this pod reads it), APP_PASSWORD (the gateway role) and DATABASE_URL (read by
# the gateway pods).
existingSecret: gateway-db
# Created by the init script as a plain LOGIN role owning this database: no superuser.
database: gateway
appUser: gateway
persistence:
  size: 2Gi
  storageClass: ""
resources:
  requests: { cpu: 50m, memory: 128Mi }
  limits: { memory: 512Mi }
```

`templates/statefulset.yaml`:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ .Release.Name }}-postgres
  labels: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
spec:
  serviceName: {{ .Release.Name }}-postgres
  replicas: 1
  selector:
    matchLabels: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
  template:
    metadata:
      labels: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
    spec:
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, runAsUser: 70, runAsGroup: 70, fsGroup: 70, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: postgres
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - { name: pg, containerPort: 5432 }
          env:
            # POSTGRES_USER stays the default "postgres": the image makes it a superuser,
            # and no gateway pod ever gets that password.
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef: { name: {{ required "existingSecret is required" .Values.existingSecret }}, key: POSTGRES_PASSWORD }
            - name: APP_PASSWORD
              valueFrom:
                secretKeyRef: { name: {{ .Values.existingSecret }}, key: APP_PASSWORD }
            # A subdirectory: the volume root can hold lost+found, which initdb refuses.
            - { name: PGDATA, value: /var/lib/postgresql/data/pgdata }
          readinessProbe:
            exec: { command: ["pg_isready", "-U", "postgres"] }
            periodSeconds: 5
          livenessProbe:
            exec: { command: ["pg_isready", "-U", "postgres"] }
            initialDelaySeconds: 30
            periodSeconds: 15
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: ["ALL"] } }
          volumeMounts:
            - { name: data, mountPath: /var/lib/postgresql/data }
            - { name: run, mountPath: /var/run/postgresql }
            - { name: init, mountPath: /docker-entrypoint-initdb.d, readOnly: true }
      volumes:
        - { name: run, emptyDir: {} }
        - name: init
          configMap: { name: {{ .Release.Name }}-postgres-init }
  volumeClaimTemplates:
    - metadata: { name: data }
      spec:
        accessModes: ["ReadWriteOnce"]
        {{- with .Values.persistence.storageClass }}
        storageClassName: {{ . | quote }}
        {{- end }}
        resources: { requests: { storage: {{ .Values.persistence.size }} } }
```

`templates/init-configmap.yaml` (the image runs it once, on an empty volume):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-postgres-init
data:
  01-app-role.sh: |
    #!/bin/sh
    # The gateway's own role: it owns its database (and so, on Postgres 15+, the
    # public schema it creates tables in) and nothing else. Not a superuser.
    set -e
    psql -v ON_ERROR_STOP=1 -U postgres -v app_pw="$APP_PASSWORD" <<'SQL'
    CREATE ROLE {{ .Values.appUser }} LOGIN PASSWORD :'app_pw';
    CREATE DATABASE {{ .Values.database }} OWNER {{ .Values.appUser }};
    REVOKE ALL ON DATABASE {{ .Values.database }} FROM PUBLIC;
    SQL
```

`templates/service.yaml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ .Release.Name }}-postgres
  labels: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
spec:
  type: ClusterIP
  selector: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
  ports:
    - { name: pg, port: 5432, targetPort: pg }
```

`templates/networkpolicy.yaml`:

```yaml
# Only gateway pods (control plane and every edge carry this label) may connect.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ .Release.Name }}-postgres
spec:
  podSelector:
    matchLabels: { app.kubernetes.io/name: postgres, app.kubernetes.io/instance: {{ .Release.Name }} }
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: { matchLabels: { app.kubernetes.io/name: gateway } }
      ports:
        - { protocol: TCP, port: 5432 }
```

`deploy/postgres-values-local.yaml`:

```yaml
# k3d on your laptop:
#   helm upgrade --install db charts/postgres -n mercora -f deploy/postgres-values-local.yaml
persistence:
  size: 1Gi
```

- [ ] **Step 2: `charts/gateway/values.yaml`** — replace the `existingSecret` block, `persistence` block and the image tag:

```yaml
image:
  repository: gateway
  tag: "0.2.0"
  pullPolicy: IfNotPresent

# control: the /api behind the dashboard (one replica). edge: /mcp for one
# gateway's attached servers; the store operator installs edges as gw-<slug>.
mode: control
# Edge only: which gateway this release serves.
slug: ""
replicas: 1

# Secret with DATABASE_URL (key) for the shared Postgres. Both modes.
dbSecret: gateway-db
# Control only. Secret with SETUP_TOKEN (first-run setup) and optionally
# OPENROUTER_API_KEY (dashboard chat agent). The chart never holds them.
existingSecret: gateway-admin
```

Delete `persistence:` and its keys. Keep everything else.

- [ ] **Step 3: `charts/gateway/templates/deployment.yaml`** — replace the whole file:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "gateway.name" . }}
  labels:
    {{- include "gateway.labels" . | nindent 4 }}
spec:
  # Stateless: everything lives in Postgres, so pods roll one at a time.
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      {{- include "gateway.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        # app.kubernetes.io/name: gateway is the label upstream and Postgres NetworkPolicies allow.
        {{- include "gateway.selectorLabels" . | nindent 8 }}
    spec:
      {{- if and (eq .Values.mode "control") .Values.stores.enabled }}
      serviceAccountName: {{ include "gateway.name" . }}
      automountServiceAccountToken: true
      {{- else }}
      automountServiceAccountToken: false
      {{- end }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: gateway
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: 8000
          env:
            - name: GATEWAY_MODE
              value: {{ .Values.mode | quote }}
            {{- if eq .Values.mode "edge" }}
            - name: GATEWAY_SLUG
              value: {{ required "slug is required for an edge" .Values.slug | quote }}
            {{- end }}
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.dbSecret }}
                  key: DATABASE_URL
            - name: CALL_TIMEOUT_S
              value: {{ .Values.callTimeoutSeconds | quote }}
            - name: LIST_TIMEOUT_S
              value: {{ .Values.listTimeoutSeconds | quote }}
            - name: REFRESH_INTERVAL_S
              value: {{ .Values.refreshIntervalSeconds | quote }}
            - name: COOKIE_SECURE
              value: {{ .Values.cookieSecure | quote }}
            - name: LLM_MODEL
              value: {{ .Values.llmModel | quote }}
            {{- with .Values.publicUrl }}
            - name: PUBLIC_URL
              value: {{ . | quote }}
            {{- end }}
            {{- with .Values.oauthRedirectUris }}
            - name: OAUTH_REDIRECT_URIS
              value: {{ . | quote }}
            {{- end }}
            {{- if and (eq .Values.mode "control") .Values.stores.enabled }}
            - name: STORES_NAMESPACE
              value: {{ .Release.Namespace }}
            {{- end }}
          {{- if eq .Values.mode "control" }}
          envFrom:
            - secretRef:
                name: {{ required "existingSecret is required" .Values.existingSecret }}
          {{- end }}
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 15
            timeoutSeconds: 2
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            initialDelaySeconds: 2
            periodSeconds: 5
            timeoutSeconds: 2
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```

- [ ] **Step 4: Gateway RBAC, Ingress, chart version, PVC** —

`charts/gateway/templates/ingress.yaml`: replace the `rules:` list with one rule per host, so an edge with a public URL also answers the tunnel's Host header:

```yaml
  rules:
    {{- range prepend (.Values.ingress.extraHosts | default list) .Values.ingress.host }}
    - host: {{ . | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "gateway.name" $ }}
                port:
                  name: http
    {{- end }}
```

and in `values.yaml` under `ingress:` add `extraHosts: []  # e.g. the host of publicUrl; TLS comes from what fronts it (ngrok, cert-manager)`.


`charts/gateway/templates/rbac.yaml`: first line → `{{- if and (eq .Values.mode "control") .Values.stores.enabled }}`; the comment → `# Only Store and McpGateway objects in this namespace. Provisioning rights stay with the operator.`; the resources line → `resources: ["stores", "mcpgateways"]`.

`charts/gateway/Chart.yaml`: `version: 0.2.0`, `appVersion: "0.2.0"`.

Delete `charts/gateway/templates/pvc.yaml`.

- [ ] **Step 5: Deploy values** —

`deploy/gateway-values-local.yaml`: unchanged except add `mode: control` under the comment line (explicit).

`deploy/gateway-values-prod.yaml`: delete the `persistence:` block; `tag: "0.2.0"`.

`deploy/store-operator-values-local.yaml`: add `edgeImageTag: "0.2.0"`.

`charts/dashboard/values.yaml`: `tag: "0.2.0"`.

- [ ] **Step 6: Lint and render**

```bash
helm lint charts/gateway -f deploy/gateway-values-local.yaml
helm lint charts/postgres -f deploy/postgres-values-local.yaml
helm template gw-prod charts/gateway --set mode=edge,slug=prod,replicas=2,ingress.host=prod.gw.localhost,stores.enabled=false | grep -E "GATEWAY_SLUG|replicas|automount|envFrom"
helm template gw-ext charts/gateway --set mode=edge,slug=ext,ingress.host=ext.gw.localhost,'ingress.extraHosts[0]=gw.example.com' | grep "host:"
helm template db charts/postgres | grep -E "name: POSTGRES_USER|APP_PASSWORD|CREATE ROLE"
```

Expected: both lint clean; the edge render shows `replicas: 2`, `automountServiceAccountToken: false`, a `GATEWAY_SLUG` env, and no `envFrom`; the ext render shows two `host:` lines; the postgres render shows `APP_PASSWORD` and `CREATE ROLE gateway LOGIN`, and no `name: POSTGRES_USER` env.

After installing on the cluster (Task 8), confirm the role: `kubectl -n mercora exec db-postgres-0 -- psql -U postgres -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = 'gateway'"` prints `f`.

- [ ] **Step 7: README** — replace the "The gateway" install text with this order (keep the existing feature sections; drop `ADMIN_USERNAME`/`ADMIN_PASSWORD` mentions, `/api/connect` from the endpoint table, and add `GET/POST /api/setup` and the four `/api/gateways` rows):

```
APP_PW=$(openssl rand -hex 16)
kubectl -n mercora create secret generic gateway-db \
  --from-literal=POSTGRES_PASSWORD=$(openssl rand -hex 16) \
  --from-literal=APP_PASSWORD=$APP_PW \
  --from-literal=DATABASE_URL=postgresql://gateway:$APP_PW@db-postgres:5432/gateway
kubectl -n mercora create secret generic gateway-admin \
  --from-literal=SETUP_TOKEN=$(openssl rand -hex 16)      # add OPENROUTER_API_KEY for chat

helm upgrade --install db charts/postgres -n mercora -f deploy/postgres-values-local.yaml --wait
docker build -t gateway:0.2.0 services/gateway && k3d image import gateway:0.2.0 -c <cluster>
helm upgrade --install team charts/gateway -n mercora -f deploy/gateway-values-local.yaml
# dashboard (team-ui) as before with tag 0.2.0; open http://dash.localhost and finish setup
# with the SETUP_TOKEN, then make an admin API key and:
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY=<key>
docker build -f services/store-operator/Dockerfile -t store-operator:0.2.0 . && k3d image import store-operator:0.2.0 -c <cluster>
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml
```

Plus one paragraph: "Gateways: create one on the Gateways page; the operator installs `gw-<slug>` with two edge pods at `http://<slug>.gw.localhost/mcp`. Attach servers to it; each edge serves only its attached servers. `kubectl -n mercora get mgw` shows their phase."

And a "Known limits" paragraph: "Postgres is one replica with no backups: it is the demo's single shared failure point (two edge replicas keep a gateway up, not the database). Production needs backups and an HA Postgres. Edges are stateless MCP (no sessions), so any replica answers any request; the rate limit is per user across the organization, in fixed one-minute windows."

- [ ] **Step 8: Stop point** — report "Task 6 done". The user commits.

---

### Task 7: Dashboard: setup, gateways, server columns

**Files:**
- Create: `services/dashboard/src/Gateways.jsx`
- Modify: `services/dashboard/src/App.jsx`, `src/Servers.jsx`, `src/Tools.jsx`, `src/styles.css`

**Interfaces:**
- Consumes: `GET/POST /api/setup`, `GET/POST /api/gateways`, `DELETE /api/gateways/{slug}`, `PUT /api/gateways/{slug}/servers`, server fields `store`, `gateways` (Tasks 2-3).

- [ ] **Step 1: Setup screen in `App.jsx`**

Move the lava panel out of `Login` into a shared component and add `Setup`, both below `Login`:

```jsx
function Lava() {
  return (
    <div className="lava" aria-hidden="true">
      {/* Goo filter: blur the blobs together, then sharpen alpha so they merge like wax. */}
      <svg width="0" height="0">
        <filter id="goo">
          <feGaussianBlur in="SourceGraphic" stdDeviation="18" />
          <feColorMatrix values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 40 -18" />
        </filter>
      </svg>
      <div className="blobs">
        {[...Array(7)].map((_, i) => <span key={i} />)}
      </div>
    </div>
  )
}

// First run only: the gateway answers nothing else until this is done.
function Setup({ onDone }) {
  const [form, setForm] = useState({ setup_token: '', org_name: '', username: '', password: '', confirm: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const field = (k) => ({ value: form[k], onChange: (e) => setForm({ ...form, [k]: e.target.value }) })

  async function submit(e) {
    e.preventDefault()
    if (form.password !== form.confirm) return setError('Passwords do not match')
    setError('')
    setBusy(true)
    try {
      const { confirm, ...body } = form
      onDone(await api('/setup', { method: 'POST', body }))
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <div className="login-side">
        <div className="brand">
          <span className="logo">◆</span> MCP Gateway
        </div>
        <form className="login-form" onSubmit={submit}>
          <div className="login-head">
            <h1>Set up your organization</h1>
            <p className="muted">Name your organization and create its first admin. This is done once.</p>
          </div>
          <label>
            Setup token
            <input autoFocus required autoComplete="off" {...field('setup_token')} />
          </label>
          <label>
            Organization name
            <input required maxLength={80} {...field('org_name')} />
          </label>
          <label>
            Admin username
            <input required autoComplete="username" {...field('username')} />
          </label>
          <label>
            Password
            <input type="password" required minLength={10} autoComplete="new-password" {...field('password')} />
          </label>
          <label>
            Confirm password
            <input type="password" required minLength={10} autoComplete="new-password" {...field('confirm')} />
          </label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary" disabled={busy}>{busy ? 'Setting up…' : 'Create organization'}</button>
        </form>
        <p className="muted small">
          The setup token is in the <code>gateway-admin</code> Secret, key <code>SETUP_TOKEN</code>.
        </p>
      </div>
      <Lava />
    </div>
  )
}
```

In `Login`, replace the whole `<div className="lava" ...>...</div>` block with `<Lava />`.

In `App`:

```jsx
  const [setup, setSetup] = useState(undefined) // { needed, org }
```

```jsx
  useEffect(() => {
    // A failed check is shown as an error, never taken to mean "already set up".
    api('/setup').then(setSetup).catch((e) => setSetup({ error: e.message }))
    api('/me').then(setUser).catch(() => setUser(null))
  }, [])

  if (user === undefined || setup === undefined) return null
  if (setup.error)
    return (
      <div className="login">
        <div className="login-side">
          <div className="login-form">
            <h1>Can't reach the gateway</h1>
            <p className="error" role="alert">{setup.error}</p>
            <button className="primary" onClick={() => location.reload()}>Retry</button>
          </div>
        </div>
        <Lava />
      </div>
    )
  if (setup.needed)
    return (
      <Setup
        onDone={(u) => {
          api('/setup').then(setSetup)
          setUser(u)
        }}
      />
    )
  if (!user) return <Login onLogin={setUser} />
```

Brand label: `<span className="label grow">{setup.org || 'MCP Gateway'}</span>`.

- [ ] **Step 2: Nav entry** — `App.jsx`: `import Gateways from './Gateways.jsx'`; in `PAGES` after servers: `{ id: 'gateways', label: 'Gateways', el: Gateways, group: 'Workspace' },`; in `ICONS`: `gateways: 'M12 2l9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 22V12',`.

- [ ] **Step 3: `src/Gateways.jsx`**

```jsx
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const DOT = { Ready: 'ok', Failed: 'unreachable', Missing: 'unreachable', Provisioning: 'pending', Deleting: 'pending' }
const EMPTY = { slug: '', name: '', public_url: '' }

export default function Gateways({ user }) {
  const isAdmin = user.role === 'admin'
  const [gateways, setGateways] = useState([])
  const [servers, setServers] = useState([])
  const [error, setError] = useState('')
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState(null) // slug whose servers are being picked
  const [picked, setPicked] = useState([])

  const load = useCallback(() => {
    api('/gateways').then(setGateways).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    if (isAdmin) api('/servers').then(setServers).catch(() => {})
    const t = setInterval(load, 3000) // phases move as the operator works
    return () => clearInterval(t)
  }, [load, isAdmin])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/gateways', { method: 'POST', body: { ...form, public_url: form.public_url || null } })
      setForm(EMPTY)
    })
  }
  const remove = (g) =>
    confirm(`Delete gateway ${g.name}? Clients using ${g.mcp_url} stop working.`) &&
    act(() => api(`/gateways/${g.slug}`, { method: 'DELETE' }))
  const edit = (g) => {
    setEditing(g.slug)
    setPicked(g.servers.map((s) => s.id))
  }
  const toggle = (id) => setPicked(picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id])
  const save = () =>
    act(async () => {
      await api(`/gateways/${editing}/servers`, { method: 'PUT', body: { server_ids: picked } })
      setEditing(null)
    })
  const copy = (text) => navigator.clipboard?.writeText(text).catch(() => {})

  return (
    <>
      <div className="toolbar">
        <h2>Gateways</h2>
        <span className="muted small">Each gateway is its own deployment with its own MCP URL.</span>
      </div>
      {isAdmin && (
        <form className="card row" onSubmit={create}>
          <input aria-label="Slug" placeholder="slug, e.g. prod" required value={form.slug}
            onChange={(e) => setForm({ ...form, slug: e.target.value.toLowerCase() })} />
          <input aria-label="Name" placeholder="name, e.g. Production" required value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="grow" aria-label="Public URL" placeholder="public https URL (optional; turns on OAuth)"
            value={form.public_url} onChange={(e) => setForm({ ...form, public_url: e.target.value })} />
          <button className="primary">Create gateway</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
      {gateways.length === 0 && <p className="muted">No gateways yet.</p>}
      {gateways.map((g) => (
        <div key={g.slug} className="card">
          <div className="row">
            <span className={`dot ${DOT[g.phase] ?? ''}`} />
            <div className="grow">
              <div className="title">
                {g.name} <span className="pill">{g.phase}</span>
                {g.public_url && <span className="pill ok">OAuth</span>}
              </div>
              <div className="muted mono small">
                {g.mcp_url}{' '}
                <button className="ghost tiny" onClick={() => copy(g.mcp_url)}>Copy</button>
              </div>
              {g.message && <div className={g.phase === 'Failed' ? 'error small' : 'muted small'}>{g.message}</div>}
              <div className="muted small">
                Servers: {g.servers.length ? g.servers.map((s) => s.name).join(', ') : 'none attached'}
              </div>
            </div>
            {isAdmin && (
              <>
                <button className="ghost" onClick={() => edit(g)}>Servers</button>
                <button className="ghost danger" onClick={() => remove(g)}>Delete</button>
              </>
            )}
          </div>
          {editing === g.slug && (
            <div className="attach">
              {servers.map((s) => (
                <label key={s.id} className="row">
                  <input type="checkbox" checked={picked.includes(s.id)} onChange={() => toggle(s.id)} />
                  {s.name} {s.store && <span className="pill">store</span>}
                </label>
              ))}
              {servers.length === 0 && <p className="muted small">No servers yet.</p>}
              <div className="row">
                <button className="primary" onClick={save}>Save</button>
                <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      ))}
    </>
  )
}
```

`styles.css`, append: `.attach { margin-top: 12px; display: grid; gap: 6px; }`

- [ ] **Step 4: Servers columns** — `Servers.jsx`

- Delete the `connect` state, its `useEffect`, and the `{connect && (...)}` pill block.
- In `<thead>` after the "Upstream auth" `<th>`: `<th>Store</th>` and `<th>Gateways</th>`.
- In each row after `<td>{authLabel(s)}</td>`:

```jsx
                  <td>
                    {s.store ? (
                      <a href={`http://${s.store}.localhost`} target="_blank" rel="noreferrer">{s.store}</a>
                    ) : '—'}
                  </td>
                  <td>{s.gateways.length ? s.gateways.join(', ') : '—'}</td>
```

- Both `colSpan={isAdmin ? 6 : 5}` → `colSpan={isAdmin ? 8 : 7}`.

- [ ] **Step 5: Connect panel uses gateway URLs** — `Tools.jsx`

Add near the other state: `const [gateways, setGateways] = useState([])` and `useEffect(() => { api('/gateways').then(setGateways).catch(() => {}) }, [])` (import `api` is already there). Replace the `<li>Point the client at ... </li>` item with:

```jsx
          <li>
            Point the client at a gateway's MCP URL
            {gateways.length === 0 ? ' (an admin creates gateways on the Gateways page).' : ':'}
            <ul>
              {gateways.map((g) => (
                <li key={g.slug}>
                  <code>{g.mcp_url}</code>{' '}
                  <button className="ghost tiny" onClick={() => copy(g.mcp_url)}>Copy</button>{' '}
                  <span className="muted small">{g.name}</span>
                </li>
              ))}
            </ul>
          </li>
```

- [ ] **Step 6: Build**

Run: `cd services/dashboard && npm run build`
Expected: build succeeds with no errors.

- [ ] **Step 7: Stop point** — report "Task 7 done". The user commits.

---

### Task 8: Fresh install on k3d and the demo check

Manual, on the cluster. Every destructive step below needs the user's yes first.

- [ ] **Step 1: Bring the cluster up** — `k3d cluster list`; `k3d cluster start <name>` if stopped.

- [ ] **Step 2: Clear the old install (ask the user first)**

```bash
kubectl -n mercora delete stores --all --wait      # operator tears each store down
helm -n mercora uninstall team
kubectl -n mercora delete pvc team-gateway          # the old SQLite volume (kept by policy)
kubectl -n mercora delete secret gateway-admin store-operator-gateway
```

- [ ] **Step 3: Secrets** — the user runs the `gateway-db` and `gateway-admin` commands from the README (auto mode blocks Secret writes).

- [ ] **Step 4: Install in order** — `db`, build/import `gateway:0.2.0`, `team`, build/import `dashboard:0.2.0` and upgrade `team-ui` then `kubectl -n mercora rollout restart deploy/team-ui-dashboard`, setup in the browser at http://dash.localhost, admin API key → `store-operator-gateway` (user), build/import `store-operator:0.2.0`, upgrade `store-operator` (the CRD in `crds/` is only installed on first install: run `kubectl apply -f charts/store-operator/crds/mcpgateway.yaml`).

- [ ] **Step 5: Demo check**

1. Setup: wrong token refused; right one logs in; reload shows the org name.
2. Create stores `shop1` and `shop2`; wait until Ready.
3. Gateways page: create `prod` and `dev`. `kubectl -n mercora get mgw` shows both `Ready`; `kubectl -n mercora get pods -l app.kubernetes.io/instance=gw-prod` shows 2 pods.
4. Attach `shop1` to `prod`; `shop2` and `notes` to `dev`.
5. Create member `bob` with all servers granted; make a key as bob.
6. ```bash
   curl -s http://prod.gw.localhost/mcp -H "Authorization: Bearer $KEY" -H 'Accept: application/json, text/event-stream' \
     -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq -r '.result.tools[].name'
   ```
   shows only `shop1__*`; the same on `dev.gw.localhost` shows only `shop2__*` and `notes__*`.
7. `tools/call` for `shop2__list_orders` on `prod` → error "Unknown tool"; the Audit log shows the row with gateway `prod`.
8. **Load spread across replicas:** make 20 `tools/list` calls to `prod`, then
   `kubectl -n mercora logs -l app.kubernetes.io/instance=gw-prod --prefix --since=2m | grep -c "POST /mcp"` per pod: both pods show requests.
9. **Rate limit across gateways:** as bob, 61 `tools/call` requests within one minute, alternating `prod` and `dev`:
   ```bash
   for i in $(seq 61); do gw=$([ $((i % 2)) = 0 ] && echo prod || echo dev)
     curl -s http://$gw.gw.localhost/mcp -H "Authorization: Bearer $KEY" -H 'Accept: application/json, text/event-stream' \
       -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"notes__list","arguments":{}}}' | grep -o 'Too many calls' ; done | wc -l
   ```
   (attach `notes` to `prod` first). Prints `1`: only the 61st was refused. If the minute rolls over mid-loop the count can be 0; run it again at the start of a minute.
10. **A pod goes away:** run a loop of `tools/list` calls to `prod` every 0.2s for 30s and, while it runs,
    `kubectl -n mercora delete pod <one gw-prod pod>`. Count non-200 answers: expect 0, at most the request in flight on that pod. The Deployment brings a new pod back.
11. **Database role:** the `rolsuper` check from Task 6 prints `f`.
12. Delete `dev` on the Gateways page; `kubectl -n mercora get pods` no longer lists `gw-dev` pods.

- [ ] **Step 6: Report** — the results of each check, pass or fail, with output for any failure.

---

## Self-review notes

- Spec §1.1 modes → Task 4; §1.2 Postgres → Tasks 1, 6; §1.3 schema → Tasks 2-4; §2 lifecycle → Tasks 3, 5; §3 request flow → Task 4; §4 setup → Task 2; §5 dashboard → Task 7; §6 charts → Task 6; §7 edge cases → Task 4 (`/readyz`), Task 1 (fail fast), Task 4 rate-limit comment; §8 testing → Tasks 1-5, 8.
- Spec §1.3 said `ALTER TABLE ... ADD COLUMN`; the plan puts `servers.store` and `audit_log.gateway` in `CREATE TABLE` instead, since the install is fresh.
- Review round (2026-10-11) folded in: shared per-user rate limit (Task 4), stateless across replicas tested (Task 4), Host allowlist (Task 4), non-superuser DB role (Task 6), retry-safe gateway create + orphans (Task 3), repair timer instead of a resume handler (Task 5), public-URL Ingress host (Tasks 5, 6), cursor-like `fetchone` (Task 1), concurrent OAuth redeem test (Task 4), setup error screen (Task 7), scaling checks (Task 8).
- `/api/connect` removal is not in the spec; it only described the single `/mcp`, which no longer exists on the control plane. Its UI use moves to the Gateways page.
