# Gateways part 3 (per-user denies, shared vs personal identity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-user deny overrides on top of team allows, and a per gateway-server identity setting under which a member's own encrypted credential (and never the shared one) goes upstream.

**Architecture:** Schema additions in `db.py` (`user_denies`, `user_credentials`, `gateway_servers.identity`, `servers.personal_header`, `audit_log.identity`, attachment-scoped foreign keys). `allowed_tools` subtracts denies in SQL. `Upstreams.call_tool` picks headers per call through a new `Vault` (Fernet) in `app/credentials.py`, which also serves `/api/me/credentials`. `woo-mcp` reads `X-Woo-Credentials` into a ContextVar used to sign WooCommerce requests.

**Tech Stack:** Python 3.12, Starlette, MCP SDK 2.x, psycopg 3, `cryptography` (Fernet), React 19 + Vite, Helm.

**Spec:** `docs/superpowers/specs/2026-10-11-personal-identity-design.md`

## Global Constraints

- **No git commits.** The user makes every commit. (Plan steps say "checkpoint" instead.)
- `effective = union(team allows) − user denies`; denies per gateway, members only (admin target 400).
- A personal call sends **only** `personal_header: <value>`; the shared header is never sent. Missing or undecryptable credential: the call does not happen, `ok=0`, audit `identity=NULL`.
- Errors, verbatim: missing `Server '<server>' uses your personal credential on gateway '<slug>', and you have not added one. Add it in the dashboard under My credentials.`; unreadable `Your saved credential for server '<server>' cannot be read. Enter it again in the dashboard under My credentials.`
- Reserved personal header names (case-insensitive): `host`, `content-length`, `content-type`, `accept`, `connection`, `transfer-encoding`, any starting `mcp-`.
- Credential value: 1-4096 characters, no `\r` or `\n`; never echoed, logged or audited.
- Credential audit rows: `tool` = `credential:set` / `credential:replace` / `credential:delete`, `server` = name, `ok=1`, `duration_ms=0`, `gateway` and `identity` NULL.
- `CREDENTIALS_KEY` (Fernet key) required in both modes; missing or malformed → `ConfigError`. Secret `gateway-credentials`, chart value `credentialsSecret`.
- Detaching a server deletes its `team_tools` and `user_denies` rows on that gateway (FK cascade); re-PUT of attachments keeps rows of servers that stay.
- `woo-mcp`: `X-Woo-Credentials: <key>:<secret>` passes the guard without the bearer token and is used instead of the env key; malformed → 400 `{"error": "X-Woo-Credentials must be <consumer_key>:<consumer_secret>"}`.
- Tags: `gateway:0.4.0`, `dashboard:0.4.0`, `woo-mcp:0.2.0`, operator `edgeImageTag: "0.4.0"`.

## Review Focus

1. **Identity row for a server that is not attached** (race: detached between permission check and identity lookup). Expected: treated as not allowed, no call. `connection_identity` returns `None` → `Unknown tool`. Pinned in Task 2 `test_personal_identity` (unit call on detached server).
2. **Personal connection on a server whose `personal_header` got cleared directly in the DB.** Expected: fails closed with the missing-credential error, not a crash or shared fallback. Pinned in Task 2.
3. **Credentials of a server the member can no longer reach** (team removed). Expected: still listed in `GET /api/me/credentials` (they stored one) so they can delete it. Pinned in Task 3.
4. **Header value with leading/trailing spaces** (copy-paste). Expected: stripped before storing, so `ck:cs ` works. Pinned in Task 3.
5. **woo-mcp request with both bearer and `X-Woo-Credentials`.** Expected: the personal key signs, never the env key. Pinned in Task 4.

---

### Task 1: Schema, denies, attachment-scoped policies

**Files:**
- Modify: `services/gateway/app/db.py`, `app/gateways.py`, `app/teams.py`, `app/api.py` (`list_users`)
- Test: `services/gateway/tests/test_teams.py`

**Interfaces:**
- Produces: `db.allowed_tools` (same signature, minus denies); `db.connection_identity(conn, gateway_slug: str, server_id: int) -> str | None`; route `PUT /api/users/{id}/gateways/{slug}/denies {tools}`; `GET /api/users` rows gain `denies: {slug: [{server_id, server, tool}]}`; `GET /api/gateways` servers gain `identity`; route `PUT /api/gateways/{slug}/servers/{id}/identity {identity}` (validation of `personal_header` arrives in Task 3: until then `personal` is accepted only when `servers.personal_header` is set); test helper `add_denies(admin, user_id, gateway, tools)`.

- [ ] **Step 1: Failing tests** in `tests/test_teams.py`:
  - rewrite the tail of `test_allowed_tools`: after detaching `s1` from `prod`, `SELECT count(*) FROM team_tools WHERE gateway_id=prod AND server_id=s1` is 0; re-attach → `allowed_tools(ann, "prod")` has no `s1`.
  - new `test_user_denies` (SQL fixture like `test_allowed_tools`): ann allowed `list_orders, get_order` on `s1/prod`; deny `get_order` → `{s1: {"list_orders"}}`; deny both → `s1` absent; a deny on `dev` changes nothing on `prod`; admin unaffected; detaching removes the deny rows.
  - new `test_denies_api` (HTTP, like `test_teams_api`): member 403; admin target 400; unattached server 400; unknown gateway 404; bad tool 400; PUT replaces; `GET /api/users` shows `denies`; re-PUT `/api/gateways/prod/servers` with the same ids keeps `team_tools`, `user_denies` and `identity='personal'` (set by SQL), dropping one server deletes only its rows.
  - new `test_schema_drops_orphaned_team_tools`: create DB with `store.connect`, `ALTER TABLE team_tools DROP CONSTRAINT team_tools_attachment`, insert an orphan row, `store.connect` again → row gone, constraint back; connect a third time is fine.
- [ ] **Step 2: Run** `uv run pytest tests/test_teams.py -q` → FAIL.
- [ ] **Step 3: Schema** — append to `SCHEMA`:

```sql
-- Which credential a connection uses (part 3).
ALTER TABLE gateway_servers ADD COLUMN IF NOT EXISTS identity TEXT NOT NULL DEFAULT 'shared'
    CHECK (identity IN ('shared', 'personal'));
ALTER TABLE servers ADD COLUMN IF NOT EXISTS personal_header TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS identity TEXT;
-- Policies belong to an attachment: detaching a server deletes them.
DELETE FROM team_tools tt WHERE NOT EXISTS (SELECT 1 FROM gateway_servers gs
    WHERE gs.gateway_id = tt.gateway_id AND gs.server_id = tt.server_id);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_tools_attachment') THEN
        ALTER TABLE team_tools ADD CONSTRAINT team_tools_attachment FOREIGN KEY (gateway_id, server_id)
            REFERENCES gateway_servers (gateway_id, server_id) ON DELETE CASCADE;
    END IF;
END $$;
-- Per-user denies, subtracted from the union of the member's teams' allows.
CREATE TABLE IF NOT EXISTS user_denies (
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gateway_id BIGINT NOT NULL,
    server_id  BIGINT NOT NULL,
    tool       TEXT NOT NULL,
    PRIMARY KEY (user_id, gateway_id, server_id, tool),
    FOREIGN KEY (gateway_id, server_id) REFERENCES gateway_servers (gateway_id, server_id) ON DELETE CASCADE
);
-- A member's own credential for a server, encrypted (Fernet token, see credentials.py).
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id    BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    server_id  BIGINT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    secret     TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, server_id)
);
```

- [ ] **Step 4: `allowed_tools` / `connection_identity`** — member query gains `AND NOT EXISTS (SELECT 1 FROM user_denies ud WHERE ud.user_id = tm.user_id AND ud.gateway_id = tt.gateway_id AND ud.server_id = tt.server_id AND ud.tool = tt.tool)`; add

```python
def connection_identity(conn, gateway_slug: str, server_id: int) -> str | None:
    """'shared' or 'personal' for an attached server; None when it is not attached."""
    row = conn.execute(
        "SELECT gs.identity FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id"
        " WHERE g.slug = ? AND gs.server_id = ?",
        (gateway_slug, server_id),
    ).fetchone()
    return row and row["identity"]
```

- [ ] **Step 5: `set_gateway_servers`** keeps attached rows:

```python
    with conn.transaction() as tx:
        # Only removed servers go (their policies cascade); the rest keep identity and policies.
        tx.execute("DELETE FROM gateway_servers WHERE gateway_id = ? AND NOT (server_id = ANY(?))", (row["id"], ids))
        tx.executemany("INSERT INTO gateway_servers (gateway_id, server_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                       [(row["id"], s) for s in ids])
```

  `_attached` selects `gs.identity` too and puts it in each server dict. New handler `set_identity` (admin; 404 unknown gateway or server not attached; 400 unless `identity in ("shared","personal")`; 400 `personal` when `servers.personal_header` is NULL) → `{"ok": True, "identity": ...}`.
- [ ] **Step 6: Denies API** in `app/teams.py` (`set_denies`, same body validation as `set_tools`, factored into `_tool_rows(conn, gateway_id, data) -> set[tuple[int, str]]` shared by both), route `PUT /api/users/{id:int}/gateways/{slug}/denies`; `api.list_users` adds `denies` (one query joined to gateways/servers). Helper `add_denies` in `tests/test_gateway.py`.
- [ ] **Step 7: Run** `uv run pytest -q` → all pass. Checkpoint.

### Task 2: Vault, identity on calls, `CREDENTIALS_KEY`

**Files:**
- Create: `services/gateway/app/credentials.py`
- Modify: `app/upstream.py`, `app/main.py`, `pyproject.toml` (`cryptography` direct), every test that builds `Settings(... database_url=db_url ...)` (add `credentials_key=KEY`), `tests/__init__.py` (`KEY`)
- Test: new `tests/test_identity.py`

**Interfaces:**
- Consumes: `connection_identity`, schema (Task 1).
- Produces: `credentials.Vault(key: str)` with `seal(value: str) -> str`, `open(token: str) -> str | None` (None when it cannot be decrypted); `Settings.credentials_key`; `Upstreams(..., vault: Vault | None = None)`; `connect(row, timeout, headers: dict)`; `upstream.shared_headers(row) -> dict`; `app.state.vault`; `tests.KEY`, `tests.test_identity.build_echo()` (tool `whoami` returns `"<X-Person>|<X-API-Key>"` from the request headers).

- [ ] **Step 1: Failing tests** `tests/test_identity.py` (echo upstream behind `http_app(build_echo())`, no guard; credentials inserted with `Vault(KEY).seal` by SQL until Task 3's API exists):
  - `test_config_needs_credentials_key`: `create_app(Settings(database_url=db_url, setup_token=SETUP))` and with `credentials_key="nope"` raise `ConfigError`.
  - `test_personal_identity` on an edge `main`, server `echo` with shared auth header `X-API-Key: shared-secret` and `personal_header='X-Person'`, bob and carol in a team allowed `whoami`:
    - shared connection: bob's call returns `None|shared-secret`; last audit row `identity='shared'`;
    - set `identity='personal'`: bob (no credential) gets the missing error exactly, audit `ok=0, identity NULL`, upstream counter unchanged;
    - bob's credential sealed with another key: the unreadable error, upstream never called;
    - bob `bob-key`, carol `carol-key`: 20 concurrent calls each (`asyncio.gather`) return `bob-key|None` / `carol-key|None` only;
    - `personal_header='X-API-Key'` (same name as shared): bob's call returns `None|bob-key`;
    - `personal_header` set to NULL by SQL: missing error, no call;
    - a deny on `whoami` for bob: `Unknown tool`, no call.
  The echo server counts calls in a list the test reads.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: `app/credentials.py`** (Vault part):

```python
class Vault:
    """Encrypts members' upstream credentials at rest (Fernet: AES-128-CBC + HMAC)."""

    def __init__(self, key: str):
        self._f = Fernet(key.encode())  # ValueError/TypeError when malformed

    def seal(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def open(self, token: str) -> str | None:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            return None
```

- [ ] **Step 4: `main.py`** — `credentials_key: str = field(default="", repr=False)` read from `CREDENTIALS_KEY`; in `create_app` before connecting: `try: vault = Vault(settings.credentials_key) except (ValueError, TypeError): raise ConfigError("CREDENTIALS_KEY must be a Fernet key (32 url-safe base64 bytes)")`; pass `vault=vault` to `Upstreams`; `app.state.vault = vault`.
- [ ] **Step 5: `upstream.py`**:

```python
def shared_headers(row: dict) -> dict:
    return {row["auth_header"]: row["auth_value"]} if row["auth_header"] else {}

@contextlib.asynccontextmanager
async def connect(row: dict, timeout: float, headers: dict):
    # The caller picks the credential. Headers from the gateway's own MCP
    # clients are never forwarded.
    ...
```

  `refresh_one` passes `shared_headers(row)`. In `call_tool`, after the disabled check:

```python
        else:
            headers, identity, refusal = self._credential(user, gateway_slug, row)
            if refusal:
                result = error_result(refusal)
            else:
                ... connect(row, self.call_timeout, headers) ...
```

```python
    def _credential(self, user, gateway_slug, row) -> tuple[dict, str | None, str | None]:
        """Headers for this call, the identity to audit, or why it cannot be made.
        A personal connection never falls back to the shared credential."""
        kind = connection_identity(self.db, gateway_slug, row["id"])
        if kind == "shared":
            return shared_headers(row), "shared", None
        stored = self.db.execute(
            "SELECT secret FROM user_credentials WHERE user_id = ? AND server_id = ?", (user["id"], row["id"])
        ).fetchone()
        if kind is None or stored is None or not row["personal_header"]:
            return {}, None, (f"Server '{row['name']}' uses your personal credential on gateway '{gateway_slug}', "
                              "and you have not added one. Add it in the dashboard under My credentials.")
        value = self.vault.open(stored["secret"]) if self.vault else None
        if value is None:
            return {}, None, (f"Your saved credential for server '{row['name']}' cannot be read. "
                              "Enter it again in the dashboard under My credentials.")
        return {row["personal_header"]: value}, "personal", None
```

  `identity` defaults to `None` before the branches and goes into the audit insert. Review Focus 1: `kind is None` (detached after the policy read) is refused the same way.
- [ ] **Step 6: Tests' `Settings`** — `tests/__init__.py`: `KEY = Fernet.generate_key().decode()`; in every test module: `from tests import KEY` and `database_url=db_url` → `database_url=db_url, credentials_key=KEY` (including `edge_settings`). `test_setup`'s "missing token" checks keep passing because the key check runs first only when the key is bad — keep order: mode, slug, DATABASE_URL, key, then setup token.
- [ ] **Step 7: Run** `uv run pytest -q` → all pass. Checkpoint.

### Task 3: Credentials API, `personal_header`, credential audit

**Files:**
- Modify: `app/credentials.py` (routes), `app/api.py` (`_server`, `create_server`, `update_server`, `list_users`), `app/gateways.py` (`set_identity` uses the same rule), `app/main.py` (routes)
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `Vault`, `app.state.vault` (Task 2).
- Produces: `GET /api/me/credentials`, `PUT/DELETE /api/me/credentials/{server_id:int}`; `personal_header` on server create/PATCH/list; `GET /api/users` rows gain `credentials: [{server, updated_at}]`; `api.personal_header(value) -> str | None`.

- [ ] **Step 1: Failing tests** `test_credentials_api`:
  - `personal_header`: `Host`, `content-type`, `Mcp-Session-Id`, `bad header` → 400; `X-Woo-Credentials` OK; GET `/api/servers` shows it; `null` clears it; clearing while a connection is personal → 400; identity `personal` on a server without one → 400.
  - bob: `GET /api/me/credentials` lists the personal server (gateways `["main"]`, `set: false`); PUT `""`, 4097 chars, `"a\nb"` → 400 and the response text does not contain the value; PUT `"  bob-key "` → 200 `{set: true, updated_at}`; stored `secret` starts with `gAAAAA` and `Vault(KEY).open` gives `bob-key`; PUT again → replace; DELETE → gone; DELETE again 404; server without `personal_header` → 400; unknown server 404.
  - audit rows `credential:set`, `credential:replace`, `credential:delete` for bob with the server name; no row or response contains `bob-key`; `caplog` text has no `bob-key`.
  - admin `GET /api/users` bob row `credentials == [{"server": "echo", "updated_at": ...}]`, no value key anywhere in the JSON.
  - after removing bob from the team, `GET /api/me/credentials` still lists the server (he has a credential) with `gateways: []`.
  - the end-to-end call from Task 2 now works with credentials set through the API.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: `api.personal_header`**

```python
RESERVED_HEADERS = {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding"}

def personal_header(value) -> str | None:
    """The header a member's own credential goes in; None = personal identity off."""
    if value is None:
        return None
    if not isinstance(value, str) or not HEADER_NAME.match(value):
        raise HTTPError(400, "personal_header is not a valid header name")
    if value.lower() in RESERVED_HEADERS or value.lower().startswith("mcp-"):
        raise HTTPError(400, f"personal_header cannot be {value}: the transport sets it")
    return value
```

  create: `personal_header(data.get("personal_header"))` into the INSERT; PATCH: when `"personal_header" in data`, validate, and if the new value is `None` while `SELECT 1 FROM gateway_servers WHERE server_id = ? AND identity = 'personal'` finds a row → 400 `"a gateway uses this server with personal identity; switch it to shared first"`. `_server` adds `"personal_header"`.
- [ ] **Step 4: Routes in `credentials.py`**:
  - `list_mine`: servers where the member has an allowed tool through a personal connection (admins: every personal connection), plus servers with a stored row; each `{server_id, server, personal_header, set, updated_at, gateways}`; gateways = slugs of personal connections the member reaches. For members reuse `allowed_tools` per gateway slug of personal connections.
  - `put_mine`: value = `data.get("value")`, must be `str`; `value.strip()`; 1-4096, no `\r\n` else 400 `"value: 1-4096 characters on one line"`; server 404 / no `personal_header` 400; upsert `INSERT ... ON CONFLICT (user_id, server_id) DO UPDATE SET secret = EXCLUDED.secret, updated_at = EXCLUDED.updated_at RETURNING (xmax = 0) AS inserted` → audit `credential:set` or `credential:replace`.
  - `delete_mine`: 404 when no row; audit `credential:delete`.
  - `_audit(conn, user, tool, server_name)` inserts the row per Global Constraints.
- [ ] **Step 5: `list_users`** adds `credentials`.
- [ ] **Step 6: Run** `uv run pytest -q` → all pass. Checkpoint.

### Task 4: `woo-mcp` personal key

**Files:**
- Modify: `services/woo-mcp/app/main.py`, `app/woo.py`, `tests/fake_woo.py`, `tests/tests_woo_mcp.py`, `charts/woo-mcp/values.yaml` (tag `0.2.0`)

**Interfaces:**
- Produces: `woo.PERSONAL_KEY: ContextVar[tuple[str, str] | None]`; fake store `KEYS = {key: (secret, "read_write" | "read")}`, `READ_KEY/READ_SECRET`, `app.state.used_keys: list[str]`.

- [ ] **Step 1: Failing tests** `test_personal_key`: list_orders with only `X-Woo-Credentials: READ_KEY:READ_SECRET` (no bearer) succeeds and the fake store saw `READ_KEY`; `update_order_status` with the read key → tool error (fake returns 401 `woocommerce_rest_cannot_edit` for non-GET with a read key); bearer + personal header → `READ_KEY` used; neither → 401; `X-Woo-Credentials: nocolon` → 400 without echoing; 10 concurrent calls with each key: `used_keys` per call matches its caller (count per key equals calls made with it).
- [ ] **Step 2: Run** `uv run python -m pytest tests -q` → FAIL.
- [ ] **Step 3: Fake store** — `authorised` looks the key up in `KEYS` and signs with its secret, appends to `used_keys`, and the guard returns 401 `{"code": "woocommerce_rest_cannot_edit", ...}` for non-GET with a `"read"` key.
- [ ] **Step 4: woo-mcp** — `PERSONAL_KEY = ContextVar("personal_key", default=None)` in `woo.py`; `request()` uses `PERSONAL_KEY.get() or (settings key, secret)` for both auth modes. Guard: `x-woo-credentials` present → validate, set the ContextVar, call the app, reset; else the bearer check as today.
- [ ] **Step 5: Run** → all pass. Checkpoint.

### Task 5: Dashboard

**Files:**
- Create: `services/dashboard/src/Credentials.jsx`
- Modify: `src/App.jsx` (page `credentials`, label "My credentials", Settings group, icon `keys`-like), `src/Servers.jsx` (Personal credential header field), `src/Gateways.jsx` (Shared/Personal toggle per attached server, detach warning), `src/Admin.jsx` (Members: Denies editor per gateway, credentials column)

- [ ] **Step 1:** My credentials page per spec §7 (PasswordInput, never pre-filled; Add/Replace/Remove; shows header, gateways, Set/Not set, updated).
- [ ] **Step 2:** Servers form field `personal_header` (empty → `null`).
- [ ] **Step 3:** Gateways: identity select per attached server calling `PUT .../servers/{id}/identity`, disabled with hint when the server has no `personal_header`; confirm text on detach.
- [ ] **Step 4:** Members: per member, a Denies editor: pick gateway, checkboxes over tools the member's teams allow there (from `/api/teams` policies), save via `PUT /api/users/{id}/gateways/{slug}/denies`; credentials listed by server name and date.
- [ ] **Step 5:** `npm run build` → succeeds.

### Task 6: Charts, images, docs

**Files:**
- Modify: `charts/gateway/values.yaml` (`credentialsSecret: gateway-credentials`, tag `0.4.0`), `charts/gateway/templates/deployment.yaml` (env `CREDENTIALS_KEY` from `credentialsSecret`, both modes), `charts/dashboard/values.yaml` (tag `0.4.0`), `charts/store-operator/values.yaml` + `deploy/store-operator-values-local.yaml` + `handlers.py` default (`edgeImageTag` `0.4.0`), `README.md` (Secret, personal identity), memory file.

Ruling carried from the spec: edges use the chart default `credentialsSecret`, so `edge_values` needs no change.

- [ ] **Step 1:** Edit charts; `helm template` both modes shows `CREDENTIALS_KEY` from `gateway-credentials`.
- [ ] **Step 2:** Operator tests: `uv run pytest -q` in `services/store-operator` → pass.
- [ ] **Step 3:** Hand the user the rollout commands (Secret creation is theirs); build/import/upgrade after they create it.
