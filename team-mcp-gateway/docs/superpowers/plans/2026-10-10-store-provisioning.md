# Store Provisioning + Member Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admins create/delete WooCommerce stores (3 templates) from the dashboard; each store gets its own namespace and woo-mcp, auto-registered in the gateway; members only see servers an admin granted them.

**Architecture:** Gateway gains per-member server grants (enforced in `Upstreams.list_tools/call_tool`) and a `/api/stores` API that only writes `Store` custom resources. A new kopf operator (`services/store-operator`) turns each Store CR into a namespace with quota/LimitRange/NetworkPolicies/Secrets plus two Helm releases (`charts/store`, `charts/woo-mcp`), registers the woo-mcp in the gateway when Ready, and tears it all down on delete.

**Tech Stack:** Python 3.12+ (Starlette, httpx2, SQLite; kopf + kubernetes client), Helm 3 charts, PHP (wp-cli `eval-file`), React 19 + Vite, k3d/k3s.

**Spec:** `docs/superpowers/specs/2026-10-10-store-provisioning-design.md`

## Global Constraints

- All paths below are relative to `team-mcp-gateway/`.
- Store name regex: `^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$` (2–20 chars). Templates: exactly `catalog-a`, `catalog-b`, `blank`.
- Max **3 stores per admin**. Provisioning timeout **15 min** (900 s), measured from CR `creationTimestamp`.
- CRD: group `mcpgateway.example.com`, version `v1`, plural `stores`, kind `Store`, namespaced in the gateway's namespace (`mercora`).
- Owner label: `mcpgateway.example.com/owner-id` = owner's numeric user id as string.
- Store namespace: `store-<name>`. Helm releases inside it: `store` (charts/store) and `woo-mcp` (charts/woo-mcp).
  Resulting names: WordPress Service `store-wordpress`, setup Job `store-store-setup`, woo-mcp Deployment/Service `woo-mcp-woo-mcp`.
- Gateway server name = store name; MCP URL `http://woo-mcp-woo-mcp.store-<name>.svc.cluster.local:8000/mcp`; credential = bearer `MCP_AUTH_TOKEN`.
- Store public URL: `http://<name>.localhost`.
- Secret `woo-mcp-credentials` in each store namespace (keys `WOO_CONSUMER_KEY` = `ck_`+40 hex, `WOO_CONSUMER_SECRET` = `cs_`+40 hex, `MCP_AUTH_TOKEN`) is **created only if absent**.
- Members see no servers by default; grants are per whole server. Admins see all.
- No new dependency in the gateway. Operator deps: `kopf`, `kubernetes` only (gateway calls use stdlib `urllib`).
- **Git:** the repo has no commits yet. Do not commit unless the user asks; each task ends with a *Checkpoint* where a commit would go.
- Gateway tests: `cd services/gateway && uv run pytest`. Operator tests: `cd services/store-operator && uv run pytest`.

## Review Focus

1. A member's existing API key after upgrade: must keep working with the same tools (migration grants existing servers) → test in Task 1.
2. A member calling a tool of a server not granted (by guessing the name) through `/mcp` and through `/api/chat`: must get "Unknown tool", and it must be audited → Task 2 test covers `/mcp`; chat uses the same `call_tool`.
3. Operator restarted after the namespace exists but before Helm ran: re-run must not regenerate the Woo key (else woo-mcp and WordPress disagree) → Task 7 test `test_credentials_secret_is_only_built_once_per_call` + `create_if_absent` behaviour in Task 8.
4. Deleting a store whose namespace is still Terminating, then re-creating the same name: must not fail half-created → finalizer waits for namespace gone (Task 8); gateway returns 409 while the old CR exists (Task 4 test).
5. Two POSTs for a 4th store at once: operator must fail the later one with "store limit (3) reached" → Task 7 test `test_over_cap_orders_by_creation_time`.

---

## File map

| File | Responsibility |
|---|---|
| `services/gateway/app/db.py` | `user_servers` table, upgrade grant, `granted_server_ids`, `set_grants` |
| `services/gateway/app/upstream.py` | filter `list_tools(user)` / `call_tool` by grants |
| `services/gateway/app/api.py` | users API: `server_ids`, `PUT /users/{id}/servers`, `DELETE /users/{id}`; `/api/servers` filtered |
| `services/gateway/app/kube.py` (new) | Store CR builder + async Kubernetes client for Store CRs |
| `services/gateway/app/stores.py` (new) | `/api/stores` handlers |
| `services/gateway/app/main.py` | pass user to `list_tools`, `stores_namespace` setting, `create_app(settings, stores=None)`, routes |
| `services/gateway/app/chat.py` | pass user to `list_tools` |
| `services/gateway/tests/test_gateway.py` | grant updates to existing tests; grants tests |
| `services/gateway/tests/test_stores.py` (new) | stores API tests with a fake backend |
| `charts/gateway/...` | ServiceAccount, Role, env when `stores.enabled` |
| `charts/store/...` | `setup.template`, `setup.hook`, `files/seed.php`, setup label |
| `charts/woo-mcp/templates/networkpolicy.yaml` | egress policy |
| `services/store-operator/` (new) | `store_operator/manifests.py` (pure), `gateway.py` (HTTP), `handlers.py` (kopf), tests, Dockerfile |
| `charts/store-operator/` (new) | CRD, RBAC, Deployment |
| `services/dashboard/src/Admin.jsx` | Members: grants + delete |
| `services/dashboard/src/Stores.jsx` (new), `App.jsx`, `styles.css` | Stores tab |
| `README.md`, `context.md` | install + acceptance steps |

---

### Task 1: Grants table and upgrade migration

**Files:**
- Modify: `services/gateway/app/db.py`
- Test: `services/gateway/tests/test_gateway.py` (append)

**Interfaces:**
- Produces: `db.granted_server_ids(conn, user) -> set[int] | None` (None = every server, for admins); `db.set_grants(conn, user_id: int, server_ids: list[int]) -> None`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_gateway.py`)

```python
def test_grants_upgrade_keeps_members_on_their_servers(tmp_path):
    from app import db as store

    path = str(tmp_path / "g.db")
    conn = store.connect(path)
    store.create_user(conn, "root", "root-password-123", "admin")
    bob = store.create_user(conn, "bob", "bob-password-123", "member")
    conn.execute("INSERT INTO servers (name, url, created_at) VALUES ('notes', 'http://n', 0), ('fx', 'http://f', 0)")
    conn.execute("DROP TABLE user_servers")  # a database from before grants
    conn.close()

    conn = store.connect(path)
    granted = conn.execute(
        "SELECT u.username, s.name FROM user_servers g JOIN users u ON u.id = g.user_id"
        " JOIN servers s ON s.id = g.server_id ORDER BY s.name"
    ).fetchall()
    assert [tuple(r) for r in granted] == [("bob", "fx"), ("bob", "notes")]

    root = conn.execute("SELECT * FROM users WHERE username = 'root'").fetchone()
    member = conn.execute("SELECT * FROM users WHERE id = ?", (bob,)).fetchone()
    assert store.granted_server_ids(conn, root) is None
    assert len(store.granted_server_ids(conn, member)) == 2

    store.set_grants(conn, bob, [])
    conn.close()
    conn = store.connect(path)  # a later start does not grant again
    assert store.granted_server_ids(conn, member) == set()
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `cd services/gateway && uv run pytest tests/test_gateway.py::test_grants_upgrade_keeps_members_on_their_servers -v`
Expected: FAIL, `no such table: user_servers`.

- [ ] **Step 3: Implement** in `app/db.py`

Append to the `SCHEMA` string (before the closing `"""`):

```sql
-- Which servers a member may use. Admins use every server and have no rows.
CREATE TABLE IF NOT EXISTS user_servers (
    user_id   INTEGER NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, server_id)
);
```

Replace the body of `connect` after `_drop_customers(conn)`:

```python
    _drop_customers(conn)
    had_grants = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'user_servers'").fetchone()
    conn.executescript(SCHEMA)
    if not had_grants:
        # Upgrade from before grants: members keep every server they could use.
        conn.execute(
            "INSERT INTO user_servers (user_id, server_id)"
            " SELECT u.id, s.id FROM users u, servers s WHERE u.role = 'member'"
        )
    return conn
```

Add after `create_user`:

```python
def granted_server_ids(conn: sqlite3.Connection, user) -> set[int] | None:
    """Server ids this user may use; None means all of them (admins)."""
    if user["role"] == "admin":
        return None
    rows = conn.execute("SELECT server_id FROM user_servers WHERE user_id = ?", (user["id"],))
    return {r[0] for r in rows}


def set_grants(conn: sqlite3.Connection, user_id: int, server_ids: list[int]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM user_servers WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO user_servers (user_id, server_id) VALUES (?, ?)",
            [(user_id, s) for s in server_ids],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
```

- [ ] **Step 4: Run the test, expect PASS**; then run the whole suite: `uv run pytest` (all pass; nothing reads grants yet).

- [ ] **Step 5: Checkpoint**

---

### Task 2: Enforce grants + users API for grants

**Files:**
- Modify: `services/gateway/app/upstream.py` (`list_tools`, `call_tool`)
- Modify: `services/gateway/app/main.py` (`on_list_tools`)
- Modify: `services/gateway/app/chat.py` (tool list)
- Modify: `services/gateway/app/api.py` (`list_servers`, `list_tools`, `list_users`, `create_user`, new `set_user_servers`, routes)
- Test: `services/gateway/tests/test_gateway.py`

**Interfaces:**
- Consumes: `db.granted_server_ids`, `db.set_grants` (Task 1).
- Produces: `Upstreams.list_tools(user) -> list[Tool]`; `GET /api/users` rows have `server_ids: list[int]`; `POST /api/users` accepts `server_ids`; `PUT /api/users/{id}/servers` body `{server_ids}` → `{ok, server_ids}`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_members_see_only_granted_servers(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0],
                        admin_password=ADMIN[1], refresh_interval_s=0.5)

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(create_app(settings)) as gw,
        ):
            admin = httpx2.AsyncClient(base_url=gw, timeout=10)
            await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})
            ids = {}
            for body in (
                {"name": "notes", "url": f"{notes}/mcp", "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}},
                {"name": "fx", "url": f"{fx}/mcp", "auth": {"type": "bearer", "token": FX_TOKEN}},
            ):
                ids[body["name"]] = (await admin.post("/api/servers", json=body)).json()["id"]

            bad = await admin.post("/api/users", json={"username": "eve", "password": "eve-password-123", "server_ids": [999]})
            assert bad.status_code == 400
            r = await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "server_ids": [ids["notes"]]})
            assert r.status_code == 201 and r.json()["server_ids"] == [ids["notes"]]
            bob_id = r.json()["id"]
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["server_ids"] == [ids["notes"]] and users["admin"]["server_ids"] == []

            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
            deadline = time.time() + 5
            while len((await admin.get("/api/tools")).json()) < 4:
                assert time.time() < deadline
                await asyncio.sleep(0.1)

            assert {t["name"] for t in (await bob.get("/api/tools")).json()} == {"notes__save_note", "notes__list"}
            assert [s["name"] for s in (await bob.get("/api/servers")).json()] == ["notes"]
            async with gateway_client(gw, key) as mcp:
                assert {t.name for t in (await mcp.list_tools()).tools} == {"notes__save_note", "notes__list"}
                r = await mcp.call_tool("fx__list", {})
                assert r.is_error and "Unknown tool" in text(r)
            log = (await admin.get("/api/audit")).json()
            assert (log[0]["username"], log[0]["tool"], log[0]["ok"]) == ("bob", "fx__list", 0)

            assert (await bob.put(f"/api/users/{bob_id}/servers", json={"server_ids": []})).status_code == 403
            admin_id = users["admin"]["id"]
            assert (await admin.put(f"/api/users/{admin_id}/servers", json={"server_ids": []})).status_code == 400
            r = await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": [ids["fx"]]})
            assert r.status_code == 200
            assert {t["name"] for t in (await bob.get("/api/tools")).json()} == {"fx__convert", "fx__list"}

            # Removing a server removes its grants.
            await admin.delete(f"/api/servers/{ids['fx']}")
            users = {u["username"]: u for u in (await admin.get("/api/users")).json()}
            assert users["bob"]["server_ids"] == []

    asyncio.run(run())
```

- [ ] **Step 2: Run it, expect FAIL** (`uv run pytest tests/test_gateway.py::test_members_see_only_granted_servers -v`): bob sees fx tools / `server_ids` KeyError.

- [ ] **Step 3: Implement**

`app/upstream.py`: add import `from .db import granted_server_ids`. Replace `list_tools`:

```python
    def list_tools(self, user) -> list[Tool]:
        # Enabled flag and grants read from the database on every request, so
        # disabling a server or changing a member's access applies at once.
        rows = self.db.execute("SELECT id, name FROM servers WHERE enabled = 1").fetchall()
        allowed = granted_server_ids(self.db, user)
        out = []
        for row in rows:
            if allowed is not None and row["id"] not in allowed:
                continue
            for tool in self.status.get(row["id"], Status()).tools:
                out.append(
                    tool.model_copy(
                        update={
                            "name": row["name"] + SEP + tool.name,
                            "description": f"[{row['name']}] {tool.description or ''}".strip(),
                        }
                    )
                )
        return out
```

In `call_tool`, change the unknown-tool branch so a server the member is not granted looks unknown:

```python
        allowed = granted_server_ids(self.db, user)
        if self._over_limit(user["id"]):
            result = error_result(f"Too many calls: at most {self.calls_per_minute} per minute. Try again shortly.")
        elif not sep or not tool or row is None or (allowed is not None and row["id"] not in allowed):
            result = error_result(f"Unknown tool '{name}'. Tools are named <server>__<tool>.")
```

`app/main.py` `on_list_tools`:

```python
    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=upstreams.list_tools(ctx.request.state.user))
```

`app/chat.py`: `for t in upstreams.list_tools(user)`.

`app/api.py`: add `import collections`. In `list_tools`: `user = current_user(request)` and `request.app.state.upstreams.list_tools(user)`. In `list_servers`:

```python
@handler
async def list_servers(request: Request) -> Response:
    user = current_user(request)
    ups: Upstreams = request.app.state.upstreams
    conn = request.app.state.db
    allowed = store.granted_server_ids(conn, user)
    rows = conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
    return JSONResponse([_server(r, ups.status.get(r["id"])) for r in rows if allowed is None or r["id"] in allowed])
```

Users section:

```python
def _server_ids(conn: sqlite3.Connection, data: dict) -> list[int]:
    ids = data.get("server_ids", [])
    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
        raise HTTPError(400, "server_ids must be a list of server ids")
    known = {r[0] for r in conn.execute("SELECT id FROM servers")}
    if not set(ids) <= known:
        raise HTTPError(400, "server_ids contains an unknown server")
    return sorted(set(ids))


@handler
async def list_users(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    grants = collections.defaultdict(list)
    for g in conn.execute("SELECT user_id, server_id FROM user_servers ORDER BY server_id"):
        grants[g["user_id"]].append(g["server_id"])
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return JSONResponse([{**_user(u), "server_ids": grants[u["id"]]} for u in rows])
```

In `create_user`, after the role check: `server_ids = _server_ids(request.app.state.db, data) if role == "member" else []`; after `store.create_user(...)`: `store.set_grants(request.app.state.db, uid, server_ids)`; return `{"id": uid, "username": username, "role": role, "server_ids": server_ids}`.

New handler:

```python
@handler
async def set_user_servers(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    user = conn.execute("SELECT * FROM users WHERE id = ?", (request.path_params["id"],)).fetchone()
    if user is None:
        raise HTTPError(404, "no such user")
    if user["role"] == "admin":
        raise HTTPError(400, "admins use every server")
    server_ids = _server_ids(conn, await body(request))
    store.set_grants(conn, user["id"], server_ids)
    return JSONResponse({"ok": True, "server_ids": server_ids})
```

Route: `Route("/api/users/{id:int}/servers", set_user_servers, methods=["PUT"]),`

- [ ] **Step 4: Fix the tests that relied on "everyone sees everything"**

In `test_the_whole_gateway`, keep bob's id at creation (`bob_id = r.json()["id"]` right after `assert r.status_code == 201`) and, right after the loop that adds the four servers, add:

```python
            r = await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": list(ids.values())})
            assert r.status_code == 200
```

In `test_chat_agent_calls_tools_as_the_logged_in_user`: keep the notes id (`notes_id = (await admin.post("/api/servers", json={...})).json()["id"]`) and create bob with `"server_ids": [notes_id]`.

In `test_rate_limit_per_user`: the user dicts become `{"id": 1, "username": "alice", "role": "admin"}` and `{"id": 2, "username": "bob", "role": "admin"}`.

- [ ] **Step 5: Run the whole suite**: `uv run pytest -v` → all PASS.

- [ ] **Step 6: Checkpoint**

---

### Task 3: Delete a user

**Files:**
- Modify: `services/gateway/app/api.py`
- Test: `services/gateway/tests/test_gateway.py` (append)

**Interfaces:**
- Produces: `DELETE /api/users/{id}` → `{ok: true}`. Task 4 adds a call to `stores.delete_all_for(request, user_id)` inside it; in this task the handler has no store step.

- [ ] **Step 1: Failing test**

```python
def test_admin_deletes_a_user(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings)) as gw:
            admin = httpx2.AsyncClient(base_url=gw, timeout=10)
            me = (await admin.post("/api/login", json={"username": ADMIN[0], "password": ADMIN[1]})).json()
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            assert (await bob.delete(f"/api/users/{me['id']}")).status_code == 403
            assert (await admin.delete(f"/api/users/{me['id']}")).status_code == 400
            assert (await admin.delete("/api/users/999")).status_code == 404
            assert (await admin.delete(f"/api/users/{bob_id}")).status_code == 200

            assert (await bob.get("/api/me")).status_code == 401
            r = await httpx2.AsyncClient(base_url=gw).post(
                "/mcp", headers={"Authorization": f"Bearer {key}"}, json={})
            assert r.status_code == 401
            assert [u["username"] for u in (await admin.get("/api/users")).json()] == ["admin"]

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect FAIL** (405 Method Not Allowed).

- [ ] **Step 3: Implement** in `api.py`

```python
@handler
async def delete_user(request: Request) -> Response:
    """Keys, sessions and grants go with the user; audit rows keep the username.
    The caller is an admin and cannot delete themself, so an admin always remains."""
    me = require_admin(request)
    conn = request.app.state.db
    user = conn.execute("SELECT * FROM users WHERE id = ?", (request.path_params["id"],)).fetchone()
    if user is None:
        raise HTTPError(404, "no such user")
    if user["id"] == me["id"]:
        raise HTTPError(400, "you cannot delete yourself")
    conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    return JSONResponse({"ok": True})
```

Route: `Route("/api/users/{id:int}", delete_user, methods=["DELETE"]),`

- [ ] **Step 4: Run whole suite, PASS.**
- [ ] **Step 5: Checkpoint**

---

### Task 4: Store API in the gateway

**Files:**
- Create: `services/gateway/app/kube.py`
- Create: `services/gateway/app/stores.py`
- Modify: `services/gateway/app/main.py` (setting, `create_app(settings, stores=None)`, routes)
- Modify: `services/gateway/app/api.py` (`delete_user` deletes owned stores)
- Test: `services/gateway/tests/test_stores.py`

**Interfaces:**
- Produces (`app/kube.py`): `GROUP = "mcpgateway.example.com"`, `OWNER_LABEL = "mcpgateway.example.com/owner-id"`, `class StoreExists(Exception)`, `store_resource(name, template, owner_id: int, owner: str) -> dict`, `class Kube(namespace)` with async `list(owner_id: int | None = None) -> list[dict]`, `get(name) -> dict | None`, `create(name, template, owner_id, owner) -> dict` (raises `StoreExists` on 409), `delete(name) -> None` (404 ignored).
- Produces (`app/stores.py`): `MAX_STORES = 3`, `TEMPLATES`, handlers `list_stores`, `create_store`, `delete_store`, `async delete_all_for(request, user_id: int) -> None`, `routes`.
- Store JSON view: `{name, template, owner, owner_id, phase, message, url, created}`; `created` is epoch seconds; `phase` is `status.phase` or `"Provisioning"`, or `"Deleting"` when `deletionTimestamp` is set.

- [ ] **Step 1: Failing tests** (`tests/test_stores.py`)

```python
"""The /api/stores endpoints, with an in-memory stand-in for the Kubernetes API."""

from __future__ import annotations

import asyncio

import httpx2

from app.kube import OWNER_LABEL, StoreExists, store_resource
from app.main import Settings, create_app
from tests.test_gateway import ADMIN, serve


class FakeStores:
    def __init__(self):
        self.items: dict[str, dict] = {}

    async def list(self, owner_id=None):
        return [c for c in self.items.values()
                if owner_id is None or c["metadata"]["labels"][OWNER_LABEL] == str(owner_id)]

    async def get(self, name):
        return self.items.get(name)

    async def create(self, name, template, owner_id, owner):
        if name in self.items:
            raise StoreExists(name)
        cr = store_resource(name, template, owner_id, owner)
        cr["metadata"]["creationTimestamp"] = f"2026-10-10T10:00:0{len(self.items)}Z"
        self.items[name] = cr
        return cr

    async def delete(self, name):
        self.items.pop(name, None)


async def login(gw, username, password):
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    assert (await c.post("/api/login", json={"username": username, "password": password})).status_code == 200
    return c


def test_store_api(tmp_path):
    fake = FakeStores()
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings, stores=fake)) as gw:
            admin = await login(gw, *ADMIN)
            carol_id = (await admin.post("/api/users", json={"username": "carol", "password": "carol-password-1", "role": "admin"})).json()["id"]
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            carol = await login(gw, "carol", "carol-password-1")
            bob = await login(gw, "bob", "bob-password-123")

            assert (await bob.get("/api/stores")).status_code == 403
            for name in ("A", "x", "shop_1", "a" * 21, "-ab", "ab-"):
                r = await admin.post("/api/stores", json={"name": name, "template": "blank"})
                assert r.status_code == 400, name
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "huge"})).status_code == 400

            r = await admin.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})
            assert r.status_code == 201, r.text
            s = r.json()
            assert (s["name"], s["template"], s["owner"], s["phase"]) == ("shop1", "catalog-a", "admin", "Provisioning")
            assert s["url"] == "http://shop1.localhost" and s["created"] > 0
            assert fake.items["shop1"]["metadata"]["labels"][OWNER_LABEL] == str(s["owner_id"])

            # A retried create returns the same store; a different one is a conflict.
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})).status_code == 200
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "blank"})).status_code == 409
            assert (await carol.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})).status_code == 409

            # A store name cannot shadow a registered server (it becomes the tool prefix).
            await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})
            assert (await admin.post("/api/stores", json={"name": "notes", "template": "blank"})).status_code == 409

            for name in ("shop2", "shop3"):
                assert (await admin.post("/api/stores", json={"name": name, "template": "blank"})).status_code == 201
            r = await admin.post("/api/stores", json={"name": "shop4", "template": "blank"})
            assert r.status_code == 400 and "3" in r.json()["error"]
            assert (await carol.post("/api/stores", json={"name": "shop9", "template": "catalog-b"})).status_code == 201

            assert {s["name"] for s in (await carol.get("/api/stores")).json()} == {"shop1", "shop2", "shop3", "shop9"}
            assert (await carol.delete("/api/stores/shop1")).status_code == 404
            assert (await admin.delete("/api/stores/shop1")).status_code == 202
            assert "shop1" not in fake.items

            # Deleting an admin deletes the stores they own.
            assert (await admin.delete(f"/api/users/{carol_id}")).status_code == 200
            assert set(fake.items) == {"shop2", "shop3"}

    asyncio.run(run())


def test_stores_off_outside_a_cluster(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings)) as gw:
            admin = await login(gw, *ADMIN)
            r = await admin.get("/api/stores")
            assert r.status_code == 503 and "not configured" in r.json()["error"]
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "role": "admin"})).json()["id"]
            assert (await admin.delete(f"/api/users/{bob_id}")).status_code == 200  # no stores to delete

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect FAIL** (`uv run pytest tests/test_stores.py -v`): `ModuleNotFoundError: app.kube`.

- [ ] **Step 3: Implement `app/kube.py`**

```python
"""Store custom resources in the Kubernetes API, with the pod's ServiceAccount.

The gateway only creates, lists and deletes Store objects; the store operator
does the provisioning. Its Role allows nothing else.
"""

from __future__ import annotations

import os
import ssl

import httpx2

GROUP = "mcpgateway.example.com"
OWNER_LABEL = f"{GROUP}/owner-id"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class StoreExists(Exception):
    pass


def store_resource(name: str, template: str, owner_id: int, owner: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "Store",
        "metadata": {"name": name, "labels": {OWNER_LABEL: str(owner_id)}},
        "spec": {"template": template, "owner": owner},
    }


class Kube:
    def __init__(self, namespace: str):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.url = f"https://{host}:{port}/apis/{GROUP}/v1/namespaces/{namespace}/stores"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    async def _request(self, method: str, path: str = "", **kw) -> httpx2.Response:
        # Read on every call: the kubelet rotates the token file.
        with open(f"{SA_DIR}/token") as f:
            token = f.read().strip()
        async with httpx2.AsyncClient(verify=self.tls, timeout=10) as http:
            return await http.request(method, self.url + path, headers={"Authorization": f"Bearer {token}"}, **kw)

    async def list(self, owner_id: int | None = None) -> list[dict]:
        params = {"labelSelector": f"{OWNER_LABEL}={owner_id}"} if owner_id is not None else None
        r = await self._request("GET", params=params)
        r.raise_for_status()
        return r.json()["items"]

    async def get(self, name: str) -> dict | None:
        r = await self._request("GET", f"/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def create(self, name: str, template: str, owner_id: int, owner: str) -> dict:
        r = await self._request("POST", json=store_resource(name, template, owner_id, owner))
        if r.status_code == 409:
            raise StoreExists(name)
        r.raise_for_status()
        return r.json()

    async def delete(self, name: str) -> None:
        r = await self._request("DELETE", f"/{name}")
        if r.status_code != 404:
            r.raise_for_status()
```

- [ ] **Step 4: Implement `app/stores.py`**

```python
"""Admins create and delete WooCommerce stores. This only writes Store
resources; the store operator provisions them and registers each store's MCP
server in the gateway when it is ready."""

from __future__ import annotations

import re
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api import HTTPError, _str, body, handler, require_admin
from .kube import OWNER_LABEL, StoreExists

MAX_STORES = 3
TEMPLATES = ("catalog-a", "catalog-b", "blank")
# 2-20 chars: "<name>__<tool>" must stay within the 64 chars the chat agent accepts.
STORE_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])$")


def _backend(request: Request):
    stores = request.app.state.stores
    if stores is None:
        raise HTTPError(503, "store provisioning is not configured")
    return stores


def _view(cr: dict) -> dict:
    meta, status = cr["metadata"], cr.get("status") or {}
    created = meta.get("creationTimestamp")
    return {
        "name": meta["name"],
        "template": cr["spec"]["template"],
        "owner": cr["spec"].get("owner", ""),
        "owner_id": int(meta["labels"][OWNER_LABEL]),
        "phase": "Deleting" if meta.get("deletionTimestamp") else status.get("phase", "Provisioning"),
        "message": status.get("message", ""),
        "url": status.get("url") or f"http://{meta['name']}.localhost",
        "created": datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None,
    }


@handler
async def list_stores(request: Request) -> Response:
    require_admin(request)
    items = await _backend(request).list()
    return JSONResponse(sorted((_view(c) for c in items), key=lambda s: s["created"] or 0))


@handler
async def create_store(request: Request) -> Response:
    """Safe to retry: the same name, owner and template returns the existing store."""
    me = require_admin(request)
    stores = _backend(request)
    data = await body(request)
    name, template = _str(data, "name"), _str(data, "template")
    if not STORE_NAME.match(name):
        raise HTTPError(400, "name: 2-20 of a-z 0-9 and -, starting with a letter")
    if template not in TEMPLATES:
        raise HTTPError(400, f"template must be one of {', '.join(TEMPLATES)}")
    existing = await stores.get(name)
    if existing is not None:
        view = _view(existing)
        if view["owner_id"] == me["id"] and view["template"] == template:
            return JSONResponse(view)
        raise HTTPError(409, f"a store named '{name}' already exists")
    if request.app.state.db.execute("SELECT 1 FROM servers WHERE name = ?", (name,)).fetchone():
        raise HTTPError(409, f"'{name}' is already a server name")
    if len(await stores.list(owner_id=me["id"])) >= MAX_STORES:
        raise HTTPError(400, f"store limit reached: at most {MAX_STORES} stores per admin")
    try:
        cr = await stores.create(name, template, me["id"], me["username"])
    except StoreExists:
        raise HTTPError(409, f"a store named '{name}' already exists") from None
    return JSONResponse(_view(cr), status_code=201)


@handler
async def delete_store(request: Request) -> Response:
    me = require_admin(request)
    stores = _backend(request)
    name = request.path_params["name"]
    cr = await stores.get(name)
    if cr is None or _view(cr)["owner_id"] != me["id"]:
        raise HTTPError(404, "no such store")
    await stores.delete(name)
    return JSONResponse({"ok": True}, status_code=202)


async def delete_all_for(request: Request, user_id: int) -> None:
    stores = request.app.state.stores
    if stores is None:
        return
    for cr in await stores.list(owner_id=user_id):
        await stores.delete(cr["metadata"]["name"])


routes = [
    Route("/api/stores", list_stores, methods=["GET"]),
    Route("/api/stores", create_store, methods=["POST"]),
    Route("/api/stores/{name}", delete_store, methods=["DELETE"]),
]
```

- [ ] **Step 5: Wire it up**

`app/main.py`:
- import: `from . import api, chat, stores` and `from .kube import Kube`.
- `Settings`: add field `stores_namespace: str = ""` (comment: `# Namespace holding Store resources; empty turns store provisioning off.`) and in `from_env`: `stores_namespace=env("STORES_NAMESPACE", ""),`.
- signature: `def create_app(settings: Settings | None = None, stores=None) -> ASGIApp:`
- routes: add `*stores.routes,` after `*api.routes,`.
- state: `app.state.stores = stores if stores is not None else (Kube(settings.stores_namespace) if settings.stores_namespace else None)`

`app/api.py` `delete_user`: before the `DELETE FROM users`, add

```python
    if user["role"] == "admin":
        from .stores import delete_all_for  # stores imports api

        await delete_all_for(request, user["id"])
```

- [ ] **Step 6: Run all gateway tests**: `uv run pytest -v` → PASS.
- [ ] **Step 7: Checkpoint**

---

### Task 5: Gateway chart: ServiceAccount, Role, env

**Files:**
- Create: `charts/gateway/templates/rbac.yaml`
- Modify: `charts/gateway/templates/deployment.yaml`, `charts/gateway/values.yaml`, `deploy/gateway-values-local.yaml`

- [ ] **Step 1: `values.yaml`** append:

```yaml
# Store provisioning: the gateway may create/list/delete Store resources in its
# own namespace. Needs the store-operator chart (it installs the CRD).
stores:
  enabled: false
```

`deploy/gateway-values-local.yaml` append:

```yaml
stores:
  enabled: true
```

- [ ] **Step 2: `templates/rbac.yaml`**

```yaml
{{- if .Values.stores.enabled }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "gateway.name" . }}
  labels:
    {{- include "gateway.labels" . | nindent 4 }}
---
# Only Store objects in this namespace. Provisioning rights stay with the operator.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "gateway.name" . }}-stores
rules:
  - apiGroups: ["mcpgateway.example.com"]
    resources: ["stores"]
    verbs: ["get", "list", "create", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "gateway.name" . }}-stores
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ include "gateway.name" . }}-stores
subjects:
  - kind: ServiceAccount
    name: {{ include "gateway.name" . }}
    namespace: {{ .Release.Namespace }}
{{- end }}
```

- [ ] **Step 3: `templates/deployment.yaml`**: replace `automountServiceAccountToken: false` with

```yaml
      {{- if .Values.stores.enabled }}
      serviceAccountName: {{ include "gateway.name" . }}
      automountServiceAccountToken: true
      {{- else }}
      automountServiceAccountToken: false
      {{- end }}
```

and add to `env:`

```yaml
            {{- if .Values.stores.enabled }}
            - name: STORES_NAMESPACE
              value: {{ .Release.Namespace }}
            {{- end }}
```

- [ ] **Step 4: Verify render**

Run: `helm template team charts/gateway -n mercora -f deploy/gateway-values-local.yaml | grep -E "kind: (Role|RoleBinding|ServiceAccount)|STORES_NAMESPACE|automountServiceAccountToken"`
Expected: the three kinds, `STORES_NAMESPACE`, `automountServiceAccountToken: true`.
Run: `helm template team charts/gateway -n mercora | grep -c "kind: Role"` → `0`.

- [ ] **Step 5: Checkpoint**

---

### Task 6: Store chart: templates, seeding, plain-Job mode; woo-mcp egress

**Files:**
- Create: `charts/store/files/seed.php`
- Modify: `charts/store/files/setup-woo.sh`, `charts/store/templates/setup-job.yaml`, `charts/store/values.yaml`
- Modify: `charts/woo-mcp/templates/networkpolicy.yaml`

**Interfaces:**
- Produces: store chart values `setup.template` (`catalog-a|catalog-b|blank`, default `blank`), `setup.hook` (bool, default `true`); setup Job pod label `mcpgateway.example.com/role: store-setup`.

- [ ] **Step 1: `values.yaml`**: under `setup:` add

```yaml
  # catalog-a | catalog-b: 300 generated products (files/seed.php); blank: the list below.
  template: blank
  # true: the setup Job is a Helm hook (helm waits for it). The store operator
  # sets false so helm returns at once and the operator watches the Job instead.
  hook: true
```

and change the `products:` comment to `# The "blank" template's products. Seeded only when the store has no products yet.`

- [ ] **Step 2: `files/seed.php`**

```php
<?php
/**
 * Seeds an empty store in one WordPress boot (one wp-cli call per product
 * takes 1-2 s each; 300 of them would not fit the provisioning timeout).
 *
 *   TEMPLATE=catalog-a|catalog-b|blank wp eval-file seed.php
 *
 * catalog-a / catalog-b: 300 products, 60 in each of 5 categories, generated
 * from word lists with a fixed seed, so every store of a template is identical.
 * blank: the products in /setup/products ("name|price" lines).
 * Then a few orders created now, so "today's orders" has data.
 * The caller runs this only when the store has no products.
 */

$catalogs = array(
	'catalog-a' => array(
		'seed'       => 101,
		'orders'     => 12,
		'categories' => array(
			'Electronics'    => array( array( 'Wireless', 'Smart', 'Portable', 'Compact', 'Pro', 'USB-C' ), array( 'Headphones', 'Speaker', 'Charger', 'Power Bank', 'Keyboard', 'Mouse', 'Webcam' ), 499, 9999 ),
			'Books'          => array( array( 'Illustrated', 'Pocket', 'Collected', 'Annotated', 'Classic', 'Modern' ), array( 'Poems', 'Cookbook', 'Atlas', 'Novel', 'Field Guide', 'Short Stories' ), 199, 1499 ),
			'Home & Kitchen' => array( array( 'Steel', 'Ceramic', 'Bamboo', 'Cast Iron', 'Glass', 'Copper' ), array( 'Pan', 'Kettle', 'Bowl Set', 'Knife', 'Storage Jar', 'Tray' ), 249, 4999 ),
			'Toys'           => array( array( 'Wooden', 'Magnetic', 'Plush', 'Remote Control', 'Puzzle', 'Musical' ), array( 'Train', 'Blocks', 'Bear', 'Car', 'Robot', 'Kite' ), 149, 2999 ),
			'Office'         => array( array( 'Ergonomic', 'Recycled', 'Leather', 'Minimal', 'Magnetic', 'Desk' ), array( 'Notebook', 'Pen Set', 'Organizer', 'Lamp', 'Whiteboard', 'Stapler' ), 99, 3999 ),
		),
	),
	'catalog-b' => array(
		'seed'       => 202,
		'orders'     => 7,
		'categories' => array(
			'Fashion' => array( array( 'Linen', 'Cotton', 'Denim', 'Silk', 'Wool', 'Handloom' ), array( 'Shirt', 'Kurta', 'Scarf', 'Jacket', 'Dress', 'Trousers' ), 499, 5999 ),
			'Sports'  => array( array( 'Pro', 'Training', 'Lightweight', 'Grip', 'All-Weather', 'Junior' ), array( 'Cricket Bat', 'Football', 'Yoga Mat', 'Racket', 'Dumbbell', 'Skipping Rope' ), 299, 7999 ),
			'Beauty'  => array( array( 'Herbal', 'Rose', 'Aloe', 'Charcoal', 'Saffron', 'Vitamin C' ), array( 'Face Wash', 'Serum', 'Lip Balm', 'Shampoo', 'Body Lotion', 'Soap' ), 99, 1999 ),
			'Grocery' => array( array( 'Organic', 'Roasted', 'Cold-Pressed', 'Stone-Ground', 'Wild', 'Single-Origin' ), array( 'Basmati Rice', 'Almonds', 'Honey', 'Coffee Beans', 'Olive Oil', 'Green Tea' ), 99, 1499 ),
			'Garden'  => array( array( 'Terracotta', 'Hanging', 'Self-Watering', 'Steel', 'Coir', 'Solar' ), array( 'Planter', 'Hose', 'Pruner', 'Seed Kit', 'Lantern', 'Trowel' ), 149, 3499 ),
		),
	),
);

function seed_product( $name, $price, $category_id ) {
	$product = new WC_Product_Simple();
	$product->set_name( $name );
	$product->set_regular_price( (string) $price );
	$product->set_status( 'publish' );
	if ( $category_id ) {
		$product->set_category_ids( array( $category_id ) );
	}
	return $product->save();
}

$template = getenv( 'TEMPLATE' ) ?: 'blank';
$ids      = array();

if ( 'blank' === $template ) {
	mt_srand( 303 );
	$orders = 2;
	foreach ( file( '/setup/products', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES ) as $line ) {
		list( $name, $price ) = array_map( 'trim', explode( '|', $line ) );
		$ids[] = seed_product( $name, $price, 0 );
	}
} elseif ( isset( $catalogs[ $template ] ) ) {
	$catalog = $catalogs[ $template ];
	mt_srand( $catalog['seed'] );
	$orders = $catalog['orders'];
	foreach ( $catalog['categories'] as $category => $words ) {
		list( $adjectives, $nouns, $min, $max ) = $words;
		$term = term_exists( $category, 'product_cat' ) ?: wp_insert_term( $category, 'product_cat' );
		$code = strtoupper( substr( preg_replace( '/[^A-Za-z]/', '', $category ), 0, 3 ) );
		for ( $i = 1; $i <= 60; $i++ ) {
			$name  = $adjectives[ mt_rand( 0, count( $adjectives ) - 1 ) ] . ' ' . $nouns[ mt_rand( 0, count( $nouns ) - 1 ) ] . sprintf( ' %s-%02d', $code, $i );
			$ids[] = seed_product( $name, mt_rand( $min, $max ), (int) $term['term_id'] );
		}
	}
} else {
	WP_CLI::error( "Unknown TEMPLATE '$template': use catalog-a, catalog-b or blank." );
}

for ( $n = 1; $n <= $orders; $n++ ) {
	$order = wc_create_order();
	for ( $k = mt_rand( 1, 3 ); $k > 0; $k-- ) {
		$order->add_product( wc_get_product( $ids[ mt_rand( 0, count( $ids ) - 1 ) ] ), mt_rand( 1, 2 ) );
	}
	$order->set_address( array( 'first_name' => 'Demo', 'last_name' => "Customer $n", 'email' => "customer$n@example.com" ), 'billing' );
	$order->calculate_totals();
	$order->update_status( 'processing' );
}

WP_CLI::success( count( $ids ) . " products, $orders orders ($template)" );
```

- [ ] **Step 3: `files/setup-woo.sh`**: replace the `if [ "$(wp wc product list ...)" = 0 ]; then ... fi` block with

```bash
if [ "$(wp wc product list --user=1 --format=count)" = 0 ]; then
  wp eval-file /setup/seed.php
fi
```

- [ ] **Step 4: `templates/setup-job.yaml`**
- ConfigMap `metadata`: wrap the annotations:

```yaml
metadata:
  name: {{ .Release.Name }}-store-setup
  {{- if .Values.setup.hook }}
  annotations:
    helm.sh/hook: post-install,post-upgrade
    helm.sh/hook-weight: "0"
    helm.sh/hook-delete-policy: before-hook-creation
  {{- end }}
```

- ConfigMap `data`: add

```yaml
  seed.php: |-
{{ .Files.Get "files/seed.php" | indent 4 }}
```

- Job `metadata`: same `{{- if .Values.setup.hook }}` wrap around its three annotations (keep the comment line inside the `if`).
- Job pod `labels`: add `mcpgateway.example.com/role: store-setup` (comment: `# Store namespaces' NetworkPolicies admit the setup Job by this label.`).
- Job container `env`: add

```yaml
            - name: TEMPLATE
              value: {{ .Values.setup.template | quote }}
```

- [ ] **Step 5: `charts/woo-mcp/templates/networkpolicy.yaml`**: change `policyTypes` to `[Ingress, Egress]` and append after the ingress rules:

```yaml
  # Outbound: only this namespace's WordPress, plus DNS. woo-mcp never needs
  # anything else, so it cannot reach another store.
  egress:
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: wordpress
      ports:
        - protocol: TCP
          port: 8080
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
```

- [ ] **Step 6: Verify render**

Run: `helm template store charts/store --set setup.hook=false --set setup.template=catalog-a | grep -E "helm.sh/hook|TEMPLATE|store-setup|seed.php"`
Expected: no `helm.sh/hook` lines; `TEMPLATE` with `catalog-a`; `seed.php` key present; role label present.
Run: `helm template mercora charts/store -f deploy/store-values-local.yaml | grep -c "helm.sh/hook:"` → `2` (unchanged for the current store).
Run: `helm template woo-mcp charts/woo-mcp | grep -A2 "policyTypes"` → Ingress and Egress.

- [ ] **Step 7: Checkpoint**

(seed.php is exercised live in Task 11; php is not installed locally.)

---

### Task 7: Operator: pure manifests and decisions

**Files:**
- Create: `services/store-operator/pyproject.toml`, `services/store-operator/.python-version` (`3.12`), `services/store-operator/store_operator/__init__.py` (empty), `services/store-operator/store_operator/manifests.py`, `services/store-operator/tests/__init__.py` (empty), `services/store-operator/tests/test_manifests.py`

**Interfaces:**
- Produces (`store_operator/manifests.py`): constants `GROUP`, `VERSION="v1"`, `PLURAL="stores"`, `OWNER_LABEL`, `MAX_STORES=3`, `TIMEOUT_S=900`; functions
  `namespace_name(store) -> str`, `store_url(store) -> str`, `mcp_url(store) -> str`,
  `namespace(store, owner_id: str) -> dict`, `resource_quota() -> dict`, `limit_range() -> dict`, `network_policies() -> list[dict]`, `credentials_secret() -> dict`,
  `store_values(store, template) -> dict`, `woo_mcp_values(store, gateway_namespace) -> dict`,
  `over_cap(store, owner_stores: list[dict]) -> bool`, `progress(wordpress_ready: bool, job: str, woo_ready: bool) -> tuple[str, str]` where `job` ∈ `missing|running|succeeded|failed` and the result state ∈ `ready|waiting|failed`, `timed_out(created_iso: str, now: datetime) -> bool`.

- [ ] **Step 1: `pyproject.toml`**

```toml
[project]
name = "store-operator"
version = "0.1.0"
description = "Turns Store resources into WooCommerce stores with their own MCP server, registered in the gateway."
requires-python = ">=3.12"
dependencies = [
    "kopf>=1.37",
    "kubernetes>=31",
]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

Run: `cd services/store-operator && uv lock && uv sync`

- [ ] **Step 2: Failing tests** (`tests/test_manifests.py`)

```python
from datetime import datetime, timedelta, timezone

from store_operator import manifests as m


def cr(name, created, owner="1"):
    return {"metadata": {"name": name, "creationTimestamp": created, "labels": {m.OWNER_LABEL: owner}}}


def test_names_and_urls():
    assert m.namespace_name("shop1") == "store-shop1"
    assert m.store_url("shop1") == "http://shop1.localhost"
    assert m.mcp_url("shop1") == "http://woo-mcp-woo-mcp.store-shop1.svc.cluster.local:8000/mcp"
    ns = m.namespace("shop1", "7")
    assert ns["metadata"]["labels"][m.OWNER_LABEL] == "7"


def test_credentials_secret_is_only_built_once_per_call():
    a, b = m.credentials_secret(), m.credentials_secret()
    data = a["stringData"]
    assert data["WOO_CONSUMER_KEY"].startswith("ck_") and len(data["WOO_CONSUMER_KEY"]) == 43
    assert data["WOO_CONSUMER_SECRET"].startswith("cs_") and len(data["WOO_CONSUMER_SECRET"]) == 43
    assert len(data["MCP_AUTH_TOKEN"]) >= 32
    assert a["metadata"]["name"] == "woo-mcp-credentials"
    assert data != b["stringData"]  # fresh values: the caller must only create, never replace


def test_network_policies_isolate_the_store():
    pols = {p["metadata"]["name"]: p for p in m.network_policies()}
    assert set(pols) == {"default-deny", "allow-dns", "wordpress-ingress", "wordpress-egress", "setup-egress", "mariadb-ingress"}
    assert pols["default-deny"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    db_from = pols["mariadb-ingress"]["spec"]["ingress"][0]["from"]
    # Only pods in this namespace (no namespaceSelector) may reach MariaDB.
    assert all("namespaceSelector" not in f for f in db_from)


def test_helm_values():
    v = m.store_values("shop1", "catalog-a")
    assert v["setup"] == {"template": "catalog-a", "hook": False}
    assert v["wordpress"]["ingress"]["hostname"] == "shop1.localhost"
    assert v["wordpress"]["networkPolicy"]["enabled"] is False
    assert v["wordpress"]["mariadb"]["networkPolicy"]["enabled"] is False
    w = m.woo_mcp_values("shop1", "mercora")
    assert w["store"]["internalUrl"] == "http://store-wordpress.store-shop1.svc.cluster.local"
    assert w["store"]["publicUrl"] == "http://shop1.localhost"
    assert w["networkPolicy"]["allowFromNamespace"] == "mercora"


def test_over_cap_orders_by_creation_time():
    stores = [cr("d", "2026-10-10T10:00:03Z"), cr("a", "2026-10-10T10:00:00Z"),
              cr("c", "2026-10-10T10:00:02Z"), cr("b", "2026-10-10T10:00:01Z")]
    assert [m.over_cap(n, stores) for n in "abcd"] == [False, False, False, True]


def test_progress():
    assert m.progress(False, "missing", False) == ("waiting", "waiting for WordPress")
    assert m.progress(True, "running", False) == ("waiting", "seeding products (setup job)")
    assert m.progress(True, "succeeded", False) == ("waiting", "waiting for woo-mcp")
    assert m.progress(True, "succeeded", True) == ("ready", "")
    state, msg = m.progress(True, "failed", True)
    assert state == "failed" and "kubectl logs job/store-store-setup" in msg


def test_timeout():
    now = datetime(2026, 10, 10, 10, 15, 1, tzinfo=timezone.utc)
    assert m.timed_out("2026-10-10T10:00:00Z", now)
    assert not m.timed_out("2026-10-10T10:00:00Z", now - timedelta(seconds=2))
```

- [ ] **Step 3: Run, expect FAIL** (`uv run pytest -v`): import error.

- [ ] **Step 4: Implement `store_operator/manifests.py`**

```python
"""Everything the operator creates for a store, as plain data, plus the small
decisions it makes. No Kubernetes calls here, so it is all unit tested."""

from __future__ import annotations

import secrets
from datetime import datetime

GROUP = "mcpgateway.example.com"
VERSION = "v1"
PLURAL = "stores"
OWNER_LABEL = f"{GROUP}/owner-id"
STORE_LABEL = f"{GROUP}/store"
SETUP_LABEL = {f"{GROUP}/role": "store-setup"}
MAX_STORES = 3
TIMEOUT_S = 15 * 60
# k3d/k3s defaults. Store pods may reach the internet (plugin downloads), never these.
CLUSTER_CIDRS = ["10.42.0.0/16", "10.43.0.0/16"]

WORDPRESS = {"app.kubernetes.io/name": "wordpress"}
MARIADB = {"app.kubernetes.io/name": "mariadb"}
WOO_MCP = {"app.kubernetes.io/name": "woo-mcp"}


def namespace_name(store: str) -> str:
    return f"store-{store}"


def store_url(store: str) -> str:
    return f"http://{store}.localhost"


def mcp_url(store: str) -> str:
    return f"http://woo-mcp-woo-mcp.{namespace_name(store)}.svc.cluster.local:8000/mcp"


def namespace(store: str, owner_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace_name(store), "labels": {OWNER_LABEL: owner_id, STORE_LABEL: store}},
    }


def resource_quota() -> dict:
    # WordPress 250m/512Mi, MariaDB 100m/256Mi, setup Job 100m/256Mi, woo-mcp 50m/96Mi, plus rollout slack.
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "store-quota"},
        "spec": {"hard": {
            "requests.cpu": "1", "requests.memory": "2Gi", "limits.memory": "3Gi",
            "requests.storage": "3Gi", "persistentvolumeclaims": "2", "pods": "10",
        }},
    }


def limit_range() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "store-limits"},
        "spec": {"limits": [{
            "type": "Container",
            "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
            "default": {"memory": "512Mi"},
            "max": {"memory": "1Gi"},
        }]},
    }


def _policy(name: str, selector: dict, ingress=None, egress=None) -> dict:
    spec: dict = {"podSelector": {"matchLabels": selector} if selector else {}, "policyTypes": []}
    if ingress is not None:
        spec["policyTypes"].append("Ingress")
        spec["ingress"] = ingress
    if egress is not None:
        spec["policyTypes"].append("Egress")
        spec["egress"] = egress
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name}, "spec": spec}


def _tcp(port: int) -> list[dict]:
    return [{"protocol": "TCP", "port": port}]


def network_policies() -> list[dict]:
    """Deny everything, then allow only the paths a store needs. woo-mcp's own
    ingress (gateway only) and egress (WordPress only) come with its chart."""
    kube_system = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
    internet = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": CLUSTER_CIDRS}}],
                "ports": _tcp(80) + _tcp(443)}
    to_db = {"to": [{"podSelector": {"matchLabels": MARIADB}}], "ports": _tcp(3306)}
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny"},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        _policy("allow-dns", {}, egress=[{"to": [kube_system], "ports": [{"protocol": "UDP", "port": 53}, *_tcp(53)]}]),
        _policy("wordpress-ingress", WORDPRESS, ingress=[{
            "from": [kube_system,  # Traefik
                     {"podSelector": {"matchLabels": WOO_MCP}},
                     {"podSelector": {"matchLabels": SETUP_LABEL}}],
            "ports": _tcp(8080),
        }]),
        _policy("wordpress-egress", WORDPRESS, egress=[to_db, internet]),
        _policy("setup-egress", SETUP_LABEL, egress=[
            to_db, {"to": [{"podSelector": {"matchLabels": WORDPRESS}}], "ports": _tcp(8080)}, internet,
        ]),
        _policy("mariadb-ingress", MARIADB, ingress=[{
            "from": [{"podSelector": {"matchLabels": WORDPRESS}}, {"podSelector": {"matchLabels": SETUP_LABEL}}],
            "ports": _tcp(3306),
        }]),
    ]


def credentials_secret() -> dict:
    """New random values each call. Create it only when absent: replacing it
    would leave WordPress and woo-mcp with different keys."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "woo-mcp-credentials"},
        "stringData": {
            "WOO_CONSUMER_KEY": "ck_" + secrets.token_hex(20),
            "WOO_CONSUMER_SECRET": "cs_" + secrets.token_hex(20),
            "MCP_AUTH_TOKEN": secrets.token_urlsafe(32),
        },
    }


def store_values(store: str, template: str) -> dict:
    return {
        "setup": {"template": template, "hook": False},
        "wordpress": {
            "wordpressBlogName": store,
            "ingress": {"hostname": f"{store}.localhost"},
            # Bitnami's policies allow all ingress; allows add up, so they would undo ours.
            "networkPolicy": {"enabled": False},
            "persistence": {"size": "1Gi"},
            "mariadb": {
                "networkPolicy": {"enabled": False},
                "primary": {
                    "persistence": {"size": "1Gi"},
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
                },
            },
        },
    }


def woo_mcp_values(store: str, gateway_namespace: str) -> dict:
    return {
        "store": {
            "internalUrl": f"http://store-wordpress.{namespace_name(store)}.svc.cluster.local",
            "publicUrl": store_url(store),
        },
        "existingSecret": "woo-mcp-credentials",
        "networkPolicy": {"enabled": True, "allowFromNamespace": gateway_namespace},
    }


def over_cap(store: str, owner_stores: list[dict]) -> bool:
    """True if this store is the owner's 4th or later, oldest first. Catches two
    creates that both passed the gateway's check at the same moment."""
    order = sorted(owner_stores, key=lambda s: (s["metadata"]["creationTimestamp"], s["metadata"]["name"]))
    names = [s["metadata"]["name"] for s in order]
    return store in names and names.index(store) >= MAX_STORES


def progress(wordpress_ready: bool, job: str, woo_ready: bool) -> tuple[str, str]:
    if job == "failed":
        # The handler fills in <namespace>.
        return "failed", "setup job failed: kubectl logs job/store-store-setup -n <namespace>"
    if not wordpress_ready:
        return "waiting", "waiting for WordPress"
    if job != "succeeded":
        return "waiting", "seeding products (setup job)"
    if not woo_ready:
        return "waiting", "waiting for woo-mcp"
    return "ready", ""


def timed_out(created_iso: str, now: datetime) -> bool:
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (now - created).total_seconds() > TIMEOUT_S
```

- [ ] **Step 5: Run, expect PASS**: `uv run pytest -v`.
- [ ] **Step 6: Checkpoint**

---

### Task 8: Operator: gateway client and kopf handlers

**Files:**
- Create: `services/store-operator/store_operator/gateway.py`, `services/store-operator/store_operator/handlers.py`
- Test: `services/store-operator/tests/test_gateway_sync.py`

**Interfaces:**
- Consumes: everything from `manifests.py` (Task 7). Gateway API: `GET/POST /api/servers`, `PATCH/DELETE /api/servers/{id}` with bearer admin API key.
- Produces: `Gateway(base_url, api_key)` with `upsert_server(name, url, token) -> None`, `delete_server(name) -> None`; kopf module `store_operator.handlers` (run with `kopf run -m store_operator.handlers`).
- Env: `GATEWAY_URL`, `GATEWAY_API_KEY`, `STORES_NAMESPACE`, `CHARTS_DIR` (default `/charts`).

- [ ] **Step 1: Failing test** (`tests/test_gateway_sync.py`)

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from store_operator.gateway import Gateway

KEY = "admin-key"


def fake_gateway():
    servers, calls = {}, []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _handle(self):
            calls.append((self.command, self.path))
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._reply(401, {"error": "no"})
            if self.command == "GET" and self.path == "/api/servers":
                return self._reply(200, [{"id": i, "name": s["name"]} for i, s in servers.items()])
            if self.command == "POST" and self.path == "/api/servers":
                sid = len(servers) + 1
                servers[sid] = self._body()
                return self._reply(201, {"id": sid})
            sid = int(self.path.rsplit("/", 1)[1])
            if self.command == "PATCH":
                servers[sid].update(self._body())
                return self._reply(200, {"id": sid})
            if self.command == "DELETE":
                servers.pop(sid)
                return self._reply(200, {"ok": True})

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, servers, calls


def test_upsert_and_delete_are_idempotent():
    httpd, servers, calls = fake_gateway()
    gw = Gateway(f"http://127.0.0.1:{httpd.server_port}", KEY)
    try:
        gw.upsert_server("shop1", "http://a/mcp", "t1")
        gw.upsert_server("shop1", "http://b/mcp", "t2")  # retry: updates, no duplicate
        assert len(servers) == 1
        s = servers[1]
        assert (s["name"], s["url"], s["auth"], s["enabled"]) == ("shop1", "http://b/mcp", {"type": "bearer", "token": "t2"}, True)
        assert ("PATCH", "/api/servers/1") in calls

        gw.delete_server("shop1")
        gw.delete_server("shop1")  # already gone: fine
        assert servers == {}

        bad = Gateway(f"http://127.0.0.1:{httpd.server_port}", "wrong")
        try:
            bad.delete_server("x")
            raise AssertionError("expected an error")
        except RuntimeError as e:
            assert "401" in str(e)
    finally:
        httpd.shutdown()
```

- [ ] **Step 2: Run, expect FAIL** (import error).

- [ ] **Step 3: Implement `store_operator/gateway.py`**

```python
"""Registers store MCP servers in the gateway through its admin API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class Gateway:
    def __init__(self, base_url: str, api_key: str):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"gateway {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from None

    def _find(self, name: str) -> dict | None:
        return next((s for s in self._call("GET", "/api/servers") if s["name"] == name), None)

    def upsert_server(self, name: str, url: str, token: str) -> None:
        body = {"url": url, "auth": {"type": "bearer", "token": token}, "enabled": True}
        existing = self._find(name)
        if existing:
            self._call("PATCH", f"/api/servers/{existing['id']}", body)
        else:
            self._call("POST", "/api/servers", {"name": name, **body})

    def delete_server(self, name: str) -> None:
        existing = self._find(name)
        if existing:
            self._call("DELETE", f"/api/servers/{existing['id']}")
```

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Implement `store_operator/handlers.py`** (not unit tested: it is Kubernetes glue; Task 11 exercises it live)

```python
"""kopf handlers: Store resource -> namespace + Helm releases + gateway server.

Every step is safe to repeat, so after an operator restart kopf re-runs a
handler from the top and it converges. Run:
  kopf run -m store_operator.handlers --standalone --namespace <gateway namespace>
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

import kopf
import kubernetes
from kubernetes.client import ApiException, AppsV1Api, BatchV1Api, CoreV1Api, CustomObjectsApi, NetworkingV1Api

from . import manifests as m
from .gateway import Gateway

NAMESPACE = os.environ.get("STORES_NAMESPACE", "mercora")
CHARTS = os.environ.get("CHARTS_DIR", "/charts")
MAX_ATTEMPTS = 3
gateway = Gateway(os.environ.get("GATEWAY_URL", "http://team-gateway:8000"), os.environ.get("GATEWAY_API_KEY", ""))


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    settings.posting.level = 30  # WARNING: keep Events for problems only


def _create(fn, *args) -> None:
    try:
        fn(*args)
    except ApiException as e:
        if e.status != 409:
            raise


def _namespace_objects(store: str, owner_id: str) -> None:
    ns = m.namespace_name(store)
    core, net = CoreV1Api(), NetworkingV1Api()
    _create(core.create_namespace, m.namespace(store, owner_id))
    _create(core.create_namespaced_resource_quota, ns, m.resource_quota())
    _create(core.create_namespaced_limit_range, ns, m.limit_range())
    for policy in m.network_policies():
        _create(net.create_namespaced_network_policy, ns, policy)


def _helm(release: str, chart: str, ns: str, values: dict) -> None:
    # Values via a file: --set breaks on commas.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(values, f)
    cmd = ["helm", "upgrade", "--install", release, f"{CHARTS}/{chart}", "-n", ns, "-f", f.name, "--timeout", "5m"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode and "in progress" in r.stderr:
            # A helm killed mid-operation (operator restart) leaves the release
            # pending; roll back, or uninstall if it never had a good revision.
            if subprocess.run(["helm", "rollback", release, "-n", ns], capture_output=True).returncode:
                subprocess.run(["helm", "uninstall", release, "-n", ns], capture_output=True)
            r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if r.returncode:
        raise RuntimeError(f"helm {release}: {r.stderr.strip()[-400:]}")


def _owner_stores(owner_id: str) -> list[dict]:
    return CustomObjectsApi().list_namespaced_custom_object(
        m.GROUP, m.VERSION, NAMESPACE, m.PLURAL, label_selector=f"{m.OWNER_LABEL}={owner_id}"
    )["items"]


@kopf.on.create(m.GROUP, m.VERSION, m.PLURAL)
def on_store_created(name, spec, labels, patch, retry, logger, **_):
    owner_id = labels[m.OWNER_LABEL]
    if m.over_cap(name, _owner_stores(owner_id)):
        patch.status.update(phase="Failed", message=f"store limit ({m.MAX_STORES}) reached")
        return
    ns = m.namespace_name(name)
    patch.status.update(phase="Provisioning", message="installing", url=m.store_url(name))
    try:
        _namespace_objects(name, owner_id)
        _create(CoreV1Api().create_namespaced_secret, ns, m.credentials_secret())
        _helm("store", "store", ns, m.store_values(name, spec["template"]))
        _helm("woo-mcp", "woo-mcp", ns, m.woo_mcp_values(name, NAMESPACE))
    except Exception as e:
        if retry + 1 >= MAX_ATTEMPTS:
            patch.status.update(phase="Failed", message=str(e)[:500])
            return
        raise kopf.TemporaryError(str(e)[:500], delay=20)
    patch.status["message"] = "waiting for WordPress"
    logger.info(f"{name}: installed into {ns}")


def _deployment_ready(apps: AppsV1Api, ns: str, name: str) -> bool:
    try:
        return (apps.read_namespaced_deployment(name, ns).status.ready_replicas or 0) > 0
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _job_state(batch: BatchV1Api, ns: str, name: str) -> str:
    try:
        st = batch.read_namespaced_job(name, ns).status
    except ApiException as e:
        if e.status == 404:
            return "missing"
        raise
    if st.succeeded:
        return "succeeded"
    if any(c.type == "Failed" and c.status == "True" for c in st.conditions or []):
        return "failed"
    return "running"


@kopf.timer(m.GROUP, m.VERSION, m.PLURAL, interval=15)
def reconcile_store(name, meta, status, patch, logger, **_):
    if status.get("phase") != "Provisioning":
        return
    ns = m.namespace_name(name)
    apps = AppsV1Api()
    state, message = m.progress(
        _deployment_ready(apps, ns, "store-wordpress"),
        _job_state(BatchV1Api(), ns, "store-store-setup"),
        _deployment_ready(apps, ns, "woo-mcp-woo-mcp"),
    )
    message = message.replace("<namespace>", ns)
    now = datetime.now(timezone.utc)
    if state == "failed":
        patch.status.update(phase="Failed", message=message)
    elif state == "ready":
        secret = CoreV1Api().read_namespaced_secret("woo-mcp-credentials", ns)
        token = base64.b64decode(secret.data["MCP_AUTH_TOKEN"]).decode()
        gateway.upsert_server(name, m.mcp_url(name), token)
        patch.status.update(phase="Ready", message="", readyAt=now.isoformat())
        logger.info(f"{name}: ready and registered in the gateway")
    elif m.timed_out(meta["creationTimestamp"], now):
        patch.status.update(phase="Failed", message=f"timed out after {m.TIMEOUT_S // 60} min: {message}")
    else:
        patch.status["message"] = message


@kopf.timer(m.GROUP, m.VERSION, m.PLURAL, interval=60)
def repair_guardrails(name, labels, status, **_):
    """Put back a quota, LimitRange or NetworkPolicy someone deleted by hand."""
    if status.get("phase") == "Ready":
        _namespace_objects(name, labels[m.OWNER_LABEL])


@kopf.on.delete(m.GROUP, m.VERSION, m.PLURAL)
def on_store_deleted(name, logger, **_):
    gateway.delete_server(name)  # tools disappear first
    ns = m.namespace_name(name)
    core = CoreV1Api()
    try:
        if core.read_namespace(ns).status.phase != "Terminating":
            core.delete_namespace(ns)  # takes the releases, PVCs and Secrets with it
    except ApiException as e:
        if e.status == 404:
            logger.info(f"{name}: torn down")
            return
        raise
    # Hold the finalizer until the namespace is gone, so the same name can be reused at once.
    raise kopf.TemporaryError(f"waiting for namespace {ns} to be deleted", delay=10)
```

- [ ] **Step 6: Import check**: `uv run python -c "import store_operator.handlers"` → no output, exit 0.
- [ ] **Step 7: Run tests**: `uv run pytest -v` → PASS.
- [ ] **Step 8: Checkpoint**

---

### Task 9: Operator image and chart

**Files:**
- Create: `services/store-operator/Dockerfile`, `services/store-operator/.dockerignore`
- Create: `charts/store-operator/Chart.yaml`, `charts/store-operator/values.yaml`, `charts/store-operator/crds/store.yaml`, `charts/store-operator/templates/rbac.yaml`, `charts/store-operator/templates/deployment.yaml`
- Create: `deploy/store-operator-values-local.yaml`

- [ ] **Step 1: `services/store-operator/Dockerfile`** (build context = `team-mcp-gateway/`, so it can copy the charts)

```dockerfile
# Build from team-mcp-gateway/:
#   docker build -f services/store-operator/Dockerfile -t store-operator:0.1.0 .
FROM alpine/helm:3.16.3 AS helm

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY --from=helm /usr/bin/helm /usr/bin/helm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    HOME=/tmp CHARTS_DIR=/charts
WORKDIR /srv
COPY services/store-operator/pyproject.toml services/store-operator/uv.lock ./
RUN uv sync --frozen --no-dev
COPY services/store-operator/store_operator ./store_operator
# The charts every store is installed from (Bitnami WordPress is vendored in charts/store/charts).
COPY charts/store /charts/store
COPY charts/woo-mcp /charts/woo-mcp
ENV PATH="/srv/.venv/bin:$PATH"

USER 10001:10001
CMD ["sh", "-c", "kopf run -m store_operator.handlers --standalone --namespace \"$STORES_NAMESPACE\" --liveness=http://0.0.0.0:8080/healthz"]
```

`.dockerignore`: `.venv`, `__pycache__`, `.pytest_cache`, `tests`.

- [ ] **Step 2: `charts/store-operator/Chart.yaml`**

```yaml
apiVersion: v2
name: store-operator
description: Provisions WooCommerce stores from Store resources and registers their MCP servers in the gateway.
type: application
version: 0.1.0
appVersion: "0.1.0"
```

`values.yaml`:

```yaml
image:
  repository: store-operator
  tag: "0.1.0"
  pullPolicy: IfNotPresent
# The gateway's Service inside this namespace.
gatewayUrl: http://team-gateway:8000
# Secret in this namespace with GATEWAY_API_KEY: an admin API key from the dashboard.
existingSecret: store-operator-gateway
resources:
  requests: { cpu: 50m, memory: 128Mi }
  limits: { memory: 512Mi }
```

- [ ] **Step 3: `crds/store.yaml`**

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: stores.mcpgateway.example.com
spec:
  group: mcpgateway.example.com
  scope: Namespaced
  names: { plural: stores, singular: store, kind: Store, shortNames: [st] }
  versions:
    - name: v1
      served: true
      storage: true
      subresources: { status: {} }
      additionalPrinterColumns:
        - { name: Template, type: string, jsonPath: .spec.template }
        - { name: Owner, type: string, jsonPath: .spec.owner }
        - { name: Phase, type: string, jsonPath: .status.phase }
        - { name: Message, type: string, jsonPath: .status.message }
        - { name: Age, type: date, jsonPath: .metadata.creationTimestamp }
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [template]
              properties:
                template: { type: string, enum: [catalog-a, catalog-b, blank] }
                owner: { type: string }
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
```

- [ ] **Step 4: `templates/rbac.yaml`**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: store-operator
---
# Broad on purpose: Helm installs WordPress, MariaDB and woo-mcp into a new
# namespace per store. Only this pod holds these rights; the gateway can only
# create Store objects.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ .Release.Namespace }}-store-operator
rules:
  - apiGroups: ["mcpgateway.example.com"]
    resources: ["stores", "stores/status"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: ["apiextensions.k8s.io"]
    resources: ["customresourcedefinitions"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
  - apiGroups: [""]
    resources: ["resourcequotas", "limitranges", "secrets", "configmaps", "services", "persistentvolumeclaims", "serviceaccounts", "pods"]
    verbs: ["*"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["*"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["*"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["networkpolicies", "ingresses"]
    verbs: ["*"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["*"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ .Release.Namespace }}-store-operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {{ .Release.Namespace }}-store-operator
subjects:
  - kind: ServiceAccount
    name: store-operator
    namespace: {{ .Release.Namespace }}
```

- [ ] **Step 5: `templates/deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: store-operator
  labels: { app.kubernetes.io/name: store-operator }
spec:
  replicas: 1   # kopf runs --standalone: exactly one operator
  strategy: { type: Recreate }
  selector:
    matchLabels: { app.kubernetes.io/name: store-operator }
  template:
    metadata:
      labels: { app.kubernetes.io/name: store-operator }
    spec:
      serviceAccountName: store-operator
      securityContext: { runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: operator
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - { name: STORES_NAMESPACE, value: {{ .Release.Namespace | quote }} }
            - { name: GATEWAY_URL, value: {{ .Values.gatewayUrl | quote }} }
          envFrom:
            - secretRef:
                name: {{ required "existingSecret is required" .Values.existingSecret }}
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
            initialDelaySeconds: 20
            periodSeconds: 30
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: tmp, mountPath: /tmp }
      volumes:
        - { name: tmp, emptyDir: {} }
```

`deploy/store-operator-values-local.yaml`:

```yaml
# k3d on your laptop:
#   helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml
gatewayUrl: http://team-gateway:8000
```

- [ ] **Step 6: Verify**

Run: `helm lint charts/store-operator` → `0 chart(s) failed`.
Run: `docker build -f services/store-operator/Dockerfile -t store-operator:0.1.0 .` (from `team-mcp-gateway/`) → success.
Run: `docker run --rm store-operator:0.1.0 sh -c "helm version --short && ls /charts && python -c 'import store_operator.handlers'"` → helm v3.16.3, `store woo-mcp`, no error.

- [ ] **Step 7: Checkpoint**

---

### Task 10: Dashboard: Members access + delete, Stores tab

**Files:**
- Modify: `services/dashboard/src/Admin.jsx` (`Users`)
- Create: `services/dashboard/src/Stores.jsx`
- Modify: `services/dashboard/src/App.jsx` (PAGES), `services/dashboard/src/styles.css`

**Interfaces:**
- Consumes: `GET/POST /api/users` (`server_ids`), `PUT /api/users/{id}/servers`, `DELETE /api/users/{id}`, `GET /api/servers`, `GET/POST /api/stores`, `DELETE /api/stores/{name}`; `/api/me` gives `user.id`.

- [ ] **Step 1: Replace `Users` in `Admin.jsx`**

```jsx
function ServerPicks({ servers, value, onChange }) {
  if (!servers.length) return <span className="muted small">No servers yet</span>
  return (
    <div className="picks">
      {servers.map((s) => (
        <label key={s.id} className="pick">
          <input
            type="checkbox"
            checked={value.includes(s.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, s.id] : value.filter((id) => id !== s.id))}
          />
          <span className="mono small">{s.name}</span>
        </label>
      ))}
    </div>
  )
}

export function Users({ user }) {
  const [users, load, error, setError] = useLoad('/users')
  const [servers] = useLoad('/servers')
  const blank = { username: '', password: '', role: 'member', server_ids: [] }
  const [f, setF] = useState(blank)
  const [editing, setEditing] = useState(null) // { id, server_ids }

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/users', { method: 'POST', body: f })
      setF(blank)
    })
  }

  const saveAccess = () =>
    act(async () => {
      await api(`/users/${editing.id}/servers`, { method: 'PUT', body: { server_ids: editing.server_ids } })
      setEditing(null)
    })

  const remove = (u) => {
    const extra = u.role === 'admin' ? ' Their stores and everything in them will be deleted too.' : ''
    if (!confirm(`Delete ${u.username}? Their API keys stop working at once.${extra}`)) return
    act(() => api(`/users/${u.id}`, { method: 'DELETE' }))
  }

  const name = (id) => servers.find((s) => s.id === id)?.name ?? `#${id}`

  return (
    <>
      <h2>Members</h2>
      <form className="card" onSubmit={create}>
        <div className="row">
          <input placeholder="username" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required />
          <input type="password" placeholder="password (10+ chars)" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required />
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
          <button className="primary">Add member</button>
        </div>
        {f.role === 'member' && (
          <div className="access">
            <span className="muted small">Can use:</span>
            <ServerPicks servers={servers} value={f.server_ids} onChange={(ids) => setF({ ...f, server_ids: ids })} />
          </div>
        )}
      </form>
      {error && <p className="error">{error}</p>}
      <table className="card">
        <thead>
          <tr>
            <th>Username</th>
            <th>Role</th>
            <th>Access</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>
                <span className="pill">{u.role}</span>
              </td>
              <td>
                {u.role === 'admin' ? (
                  <span className="muted small">all servers</span>
                ) : editing?.id === u.id ? (
                  <div className="row">
                    <ServerPicks servers={servers} value={editing.server_ids} onChange={(ids) => setEditing({ ...editing, server_ids: ids })} />
                    <button className="primary" onClick={saveAccess}>Save</button>
                    <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                ) : (
                  <span className="mono small">{u.server_ids.map(name).join(', ') || 'none'}</span>
                )}
              </td>
              <td className="actions">
                {u.role === 'member' && editing?.id !== u.id && (
                  <button className="ghost" onClick={() => setEditing({ id: u.id, server_ids: u.server_ids })}>Access</button>
                )}
                {u.id !== user.id && (
                  <button className="ghost danger" onClick={() => remove(u)}>Delete</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
```

- [ ] **Step 2: Create `Stores.jsx`**

```jsx
import { useCallback, useEffect, useState } from 'react'
import { api, when } from './api.js'

const TEMPLATES = [
  { id: 'catalog-a', title: 'Catalog A', text: '300 products: electronics, books, home & kitchen, toys, office' },
  { id: 'catalog-b', title: 'Catalog B', text: '300 products: fashion, sports, beauty, grocery, garden' },
  { id: 'blank', title: 'Blank', text: '3 basic products' },
]
const MAX = 3
const DOT = { Ready: 'ok', Failed: 'unreachable', Provisioning: 'pending', Deleting: 'pending' }

export default function Stores({ user }) {
  const [stores, setStores] = useState([])
  const [error, setError] = useState('')
  const [form, setForm] = useState({ name: '', template: 'catalog-a' })

  const load = useCallback(() => {
    api('/stores').then(setStores).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 3000) // phases move as the operator works
    return () => clearInterval(t)
  }, [load])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const mine = stores.filter((s) => s.owner_id === user.id).length
  const create = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/stores', { method: 'POST', body: form })
      setForm({ ...form, name: '' })
    })
  }
  const remove = (s) => {
    if (!confirm(`Delete store ${s.name}? Its data, volumes and MCP tools are removed.`)) return
    act(() => api(`/stores/${s.name}`, { method: 'DELETE' }))
  }

  return (
    <>
      <div className="toolbar">
        <h2>Stores</h2>
        <span className="muted small">{mine}/{MAX} of your stores used</span>
      </div>
      <form className="card" onSubmit={create}>
        <div className="templates">
          {TEMPLATES.map((t) => (
            <label key={t.id} className={form.template === t.id ? 'template active' : 'template'}>
              <input type="radio" name="template" value={t.id} checked={form.template === t.id} onChange={() => setForm({ ...form, template: t.id })} />
              <b>{t.title}</b>
              <span className="muted small">{t.text}</span>
            </label>
          ))}
        </div>
        <div className="row">
          <input
            className="grow"
            aria-label="Store name"
            placeholder="store name, e.g. shop1 (tools become shop1__list_orders)"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value.toLowerCase() })}
            required
          />
          <button className="primary" disabled={mine >= MAX}>Create store</button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {stores.length === 0 && <p className="muted">No stores yet.</p>}
      {stores.map((s) => (
        <div key={s.name} className="card server row">
          <span className={`dot ${DOT[s.phase] ?? ''}`} />
          <div className="grow">
            <div className="title">
              {s.name} <span className="pill">{s.phase}</span> <span className="pill">{s.template}</span>
            </div>
            <div className="muted small">
              {s.owner} · created {when(s.created)}
              {s.phase === 'Ready' && (
                <>
                  {' · '}
                  <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a>
                </>
              )}
            </div>
            {s.message && <div className={s.phase === 'Failed' ? 'error small' : 'muted small'}>{s.message}</div>}
          </div>
          {s.owner_id === user.id && s.phase !== 'Deleting' && (
            <button className="ghost danger" onClick={() => remove(s)}>Delete</button>
          )}
        </div>
      ))}
    </>
  )
}
```

- [ ] **Step 3: `App.jsx`**: `import Stores from './Stores.jsx'` and add `{ id: 'stores', label: 'Stores', el: Stores, admin: true },` after the `servers` entry. Change the Members entry to pass the user (it already gets `user` via `<Page user={user} />`; no change needed).

- [ ] **Step 4: `styles.css`** append:

```css
.access { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
.picks { display: flex; gap: 10px; flex-wrap: wrap; }
.pick { display: inline-flex; gap: 4px; align-items: center; }
.templates { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-bottom: 12px; }
.template { display: flex; flex-direction: column; gap: 4px; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; cursor: pointer; }
.template input { position: absolute; opacity: 0; }
.template.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.template:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
```

- [ ] **Step 5: Verify build**: `cd services/dashboard && npm run build` → success, no errors.
- [ ] **Step 6: Checkpoint**

---

### Task 11: Deploy to k3d and run the acceptance test

**Files:**
- Modify: `README.md` (new section "Store provisioning"), `context.md` (new section 4.7 + update Next steps)

No new code: this task deploys and proves it. Record outputs in `context.md`. If a step fails, fix the owning task's code and re-run from that step.

- [ ] **Step 1: Build and import images** (from `team-mcp-gateway/`)

```bash
docker build -t gateway:0.1.1 services/gateway
docker build -f services/store-operator/Dockerfile -t store-operator:0.1.0 .
docker build -t team-ui-dashboard:0.1.1 services/dashboard   # use the dashboard chart's image name from charts/dashboard/values.yaml
k3d image import gateway:0.1.1 store-operator:0.1.0 team-ui-dashboard:0.1.1 -c store-check
```

(Check exact image repository names in `charts/gateway/values.yaml` and `charts/dashboard/values.yaml`; pass `--set image.tag=0.1.1` below.)

- [ ] **Step 2: Install the operator chart (CRD first), then upgrade gateway and dashboard**

```bash
# One-time: an admin API key for the operator (dashboard → API keys → "store-operator").
kubectl -n mercora create secret generic store-operator-gateway --from-literal=GATEWAY_API_KEY='<paste key>'
helm upgrade --install store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml
helm upgrade --install team charts/gateway -n mercora -f deploy/gateway-values-local.yaml --set image.tag=0.1.1
helm upgrade --install team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --set image.tag=0.1.1
kubectl -n mercora rollout status deploy/store-operator deploy/team-gateway
```

Expected: both rolled out; `kubectl get crd stores.mcpgateway.example.com` exists; existing members still see their tools (log in as a member in the dashboard → Tools).

- [ ] **Step 3: Create two stores at once** (dashboard → Stores: `shop1` Catalog A, then `shop2` Catalog B immediately)

Watch: `kubectl -n mercora get st -w` → both go `Provisioning` → `Ready` within 15 min.
Check: `kubectl get ns store-shop1 store-shop2`; `kubectl -n store-shop1 get resourcequota,limitrange,networkpolicy,pvc,secret`.
Check: dashboard → Servers shows `shop1`, `shop2` healthy with 10 tools each.
Check: `kubectl -n store-shop1 logs job/store-store-setup | tail -3` → `Success: 300 products, 12 orders (catalog-a)`.

- [ ] **Step 4: Chat comparison**: Chat tab: "Compare today's orders in shop1 and shop2." → the reply shows `✓ shop1__list_orders` and `✓ shop2__list_orders`, 12 vs 7 orders.

- [ ] **Step 5: Isolation**

```bash
P=$(kubectl -n store-shop1 get pod -l app.kubernetes.io/name=woo-mcp -o name)
kubectl -n store-shop1 exec $P -- python -c "import urllib.request;print(urllib.request.urlopen('http://store-wordpress.store-shop1.svc.cluster.local',timeout=5).status)"   # 200
kubectl -n store-shop1 exec $P -- python -c "import urllib.request;urllib.request.urlopen('http://store-wordpress.store-shop2.svc.cluster.local',timeout=5)"  # timeout error
kubectl -n store-shop1 exec $P -- python -c "import socket;socket.create_connection(('store-mariadb.store-shop2.svc.cluster.local',3306),timeout=5)"  # timeout error
```

- [ ] **Step 6: Member access**: Members → add `dave` with only `shop1`; log in as dave → Tools shows only `shop1__*`; Chat "list shop2 orders" → agent has no shop2 tools.

- [ ] **Step 7: Recovery**: create `shop3` (Blank); while `Provisioning`, `kubectl -n mercora delete pod -l app.kubernetes.io/name=store-operator`; it still ends `Ready` (or `Failed` with a reason). Confirm `woo-mcp-credentials` was not regenerated: woo-mcp `list_products` works (3 products).

- [ ] **Step 8: Guardrails**: try a 4th store → error "store limit reached: at most 3 stores per admin".

- [ ] **Step 9: Delete**: delete `shop2` in the dashboard. Then:

```bash
kubectl get ns store-shop2            # NotFound (after Terminating)
kubectl get pvc,secret -n store-shop2 # NotFound
kubectl -n mercora get st shop2       # NotFound
```

Dashboard → Tools: no `shop2__*`; Chat "list shop2 orders" → no such tools.

- [ ] **Step 10: Delete an admin**: create admin `erin`, log in as erin, create store `erin1` (Blank), wait Ready; as the first admin delete erin → `store-erin1` namespace and `erin1` server gone.

- [ ] **Step 11: Docs**: README "Store provisioning" section: build/import images, the one-time `store-operator-gateway` Secret, install commands, how to watch (`kubectl -n mercora get st`). `context.md`: add section 4.7 "Store provisioning" (components, flow, guardrails, isolation) and 4.8 "Member access"; add the operator gateway-key and per-day seeded orders to Known limits; mark the bonus done in Next steps.

- [ ] **Step 12: Checkpoint**
