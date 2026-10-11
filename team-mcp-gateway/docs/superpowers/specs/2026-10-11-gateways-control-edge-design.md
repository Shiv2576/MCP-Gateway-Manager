# Gateways part 1: Postgres, control plane / edge split, McpGateway, org setup

Date: 2026-10-11. Status: draft for review.

Part 1 of 3. The platform becomes one organization that owns stores (already
built) and several **gateways**, each a separate deployment with its own URL
and its own set of attached MCP servers.

- Part 1 (this spec): the infrastructure. Postgres, one image in two modes,
  gateways as Kubernetes resources, first-run org setup.
- Part 2: teams and tool-level access policies (team × gateway × tools),
  replacing `user_servers`.
- Part 3: shared vs personal identity per gateway-server connection.

---

## 0. Agreed requirements

From the conversation:

- **One organization per install.** Not a multi-company SaaS. Stores stay the
  isolated unit underneath it (namespace, WordPress, woo-mcp, NetworkPolicy:
  unchanged).
- **A gateway is a separate deployment** (its own pods, Service, Ingress host),
  so each one scales on its own.
- **Organization registration** is a first-run setup in the dashboard: name the
  org, create the first admin.
- A working demo matters more than scale; anything cut is written down here.

Decided in this spec (not stated by the user):

- **Fresh install, no data migration** from the current SQLite file. Existing
  stores are deleted and created again; keys are issued again.
- The custom resource is **`McpGateway`** (`mcpgateways.mcpgateway.example.com`,
  short name `mgw`), not `Gateway`, which clashes with the Kubernetes Gateway API.
- **Setup needs a setup token** from a Secret, so the first person to reach a
  fresh install cannot take it over.
- Authorization in part 1 stays as today (admins see everything, members see
  their `user_servers` grants), **intersected with what is attached to the
  gateway**. Part 2 replaces the grants.

---

## 1. Architecture

```
 Browser ─► dash.localhost (nginx) ──/api──► CONTROL PLANE  (release "team",
                                             gateway image, MODE=control)
                                             · /api: setup, users, keys, servers,
                                               stores, gateways, audit, chat
                                             · health loop over ALL servers
                                               (Servers tab, chat)
                                                     │ creates McpGateway CRs
 MCP clients                                         ▼
   ├─► prod.gw.localhost/mcp ─► EDGE gw-prod    OPERATOR (existing kopf)
   └─► dev.gw.localhost/mcp  ─► EDGE gw-dev     · Store CR      (unchanged)
       (gateway image, MODE=edge,               · McpGateway CR → helm install
        GATEWAY_SLUG=<slug>, 2 replicas)          gw-<slug> from charts/gateway
       · /mcp, OAuth, /healthz                    in mercora, wait Ready
       · health loop over ATTACHED servers
                │                 all pods ─► PostgreSQL (release "db")
                ▼
   store-*/woo-mcp, notes, fx  (NetworkPolicy: pods labelled
                                app.kubernetes.io/name=gateway only)
```

### 1.1 One image, two modes

`services/gateway` builds one image. `GATEWAY_MODE` picks the routes:

| | control | edge |
|---|---|---|
| `/api/*` (all of `api.py`, `stores.py`, `gateways.py`, chat) | yes | no |
| `/mcp` | no | yes |
| OAuth endpoints and login page (`oauth.py`) | no | yes, when `PUBLIC_URL` is set |
| `/healthz`, `/readyz` | yes | yes |
| Health loop covers | every enabled server | enabled servers attached to `GATEWAY_SLUG` |
| Creates schema on start | yes | no (waits: `/readyz` fails until the gateway row exists) |
| Kubernetes access (ServiceAccount) | yes, Store + McpGateway objects only | none (`automountServiceAccountToken: false`) |
| Replicas | 1 | 2 by default (`replicas` value) |

`create_app` keeps one code path and switches on `settings.mode`. Edge
requires `GATEWAY_SLUG`; a missing slug is a `ConfigError` at start.

Edge `/mcp` behaviour is today's: `ApiKeyGuard`, `Upstreams.list_tools` and
`Upstreams.call_tool`, with one extra filter, the gateway's attachment (§3).

**Stateless, on purpose.** Edges run `stateless_http=True`: no MCP session is
kept, so any replica answers any request and no sticky routing is needed. This
is not "a session moves between pods"; there is no session. The gateway needs
none: every upstream call is request/response, with no server-to-client
requests and no stream resumption. Tested by initializing on one replica and
calling a tool on the other (§8).

**Host allowlist.** Edges pass `TransportSecuritySettings` (DNS-rebinding
protection on) allowing `<slug>.gw.localhost`, the public URL's host, and
`localhost`/`127.0.0.1` (port-forward, tests), each with and without `:*`
(the SDK's `host:*` only matches a Host header that has a port). Allowed
origins: the same hosts over `http://`, plus the public URL. Other Host
headers get 421. The API key or OAuth token is still checked first.

### 1.2 PostgreSQL

Why: every edge reads keys, attachments and grants and writes audit rows on
each request. One central database means no copy to sync per gateway, and a
revoke or detach applies on the very next request everywhere. SQLite is a
single-pod file and cannot be shared safely.

- A small chart `charts/postgres`: one `postgres:17-alpine` StatefulSet, a
  PVC, a ClusterIP Service, and a NetworkPolicy allowing only gateway pods
  (control and edge share the label). Release `db` in `mercora`.
- Credentials: Secret `gateway-db`, created by hand like the other Secrets:
  - `POSTGRES_PASSWORD`: the `postgres` superuser. Read only by the postgres pod.
  - `APP_PASSWORD` and `DATABASE_URL`
    (`postgresql://gateway:<app pw>@db-postgres:5432/gateway`): the `gateway`
    role. Control and edges read only the `DATABASE_URL` key.
- **No superuser for the gateway.** The official image makes `POSTGRES_USER` a
  superuser, so the chart leaves it at `postgres` and an init script
  (`/docker-entrypoint-initdb.d`, runs once on an empty volume) creates
  `gateway` as a plain `LOGIN` role, the database `gateway` owned by it, and
  revokes `PUBLIC` access. Owning the database gives it the `public` schema
  (Postgres 15+), enough to create its tables. A narrower role for edges only
  is left for later: edges must read the upstream credentials anyway.
- Single replica, no backups: Postgres is the one shared failure point of the
  demo. Documented in the README; production needs backups and an HA setup.
- `services/gateway` adds `psycopg[binary]` and `psycopg-pool`.

`db.py` keeps its shape so the 60-odd `conn.execute(...)` call sites barely change:

- `connect(url)` returns a small `Db` object wrapping a `psycopg_pool.ConnectionPool`
  (min 1, max 10), autocommit, `dict_row`.
- `Db.execute(sql, params)` borrows a connection, runs, returns a cursor whose
  rows are already fetched (`fetchone` / `fetchall` work as before).
- `Db.transaction()` is a context manager for the two multi-statement writes
  (`set_grants`, setup).
- Source changes, done once by hand: `?` → `%s`; `cur.lastrowid` →
  `INSERT ... RETURNING id`; `r[0]` → `r["col"]`; `INTEGER PRIMARY KEY` →
  `BIGINT GENERATED ALWAYS AS IDENTITY`; `REAL` → `DOUBLE PRECISION`;
  `sqlite3.Row` / `sqlite3.IntegrityError` → `dict` / `psycopg.errors.UniqueViolation`.
- The SQLite-only upgrade code (`_drop_customers`, the first-grants backfill)
  is deleted, with its tests: fresh install.
- shortcut: queries are synchronous inside async handlers; fine at in-cluster
  latency for a demo, move to `psycopg.AsyncConnection` if p99 matters.

### 1.3 Schema changes (on top of today's tables)

```sql
CREATE TABLE IF NOT EXISTS org (
    id         INTEGER PRIMARY KEY CHECK (id = 1),   -- at most one row
    name       TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS gateways (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,      -- ^[a-z][a-z0-9-]{1,19}$, the host and release name
    name       TEXT NOT NULL,             -- display name
    public_url TEXT,                      -- optional https URL; set, OAuth is on for this edge
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_servers (
    gateway_id BIGINT NOT NULL REFERENCES gateways(id) ON DELETE CASCADE,
    server_id  BIGINT NOT NULL REFERENCES servers(id)  ON DELETE CASCADE,
    PRIMARY KEY (gateway_id, server_id)
);
ALTER TABLE servers   ADD COLUMN IF NOT EXISTS store   TEXT;   -- set by the operator
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS gateway TEXT;   -- slug; NULL = control plane chat
```

`identity` on `gateway_servers` arrives in part 3, policies in part 2.

---

## 2. Gateway lifecycle

### 2.1 The McpGateway resource

```yaml
apiVersion: mcpgateway.example.com/v1
kind: McpGateway
metadata: { name: prod }          # = slug
spec:
  displayName: Production
  publicUrl: ""                    # optional
status: { phase: Provisioning | Ready | Failed, url: http://prod.gw.localhost, message: "" }
```

CRD lives in `charts/store-operator/crds/` next to `store.yaml`.

### 2.2 Create (dashboard → control → operator)

1. Admin fills in slug, name, optional public URL on the **Gateways** page.
2. `POST /api/gateways` (admin) is **safe to retry**, so any earlier failure
   (cluster rejected it, response lost after the CR was made, control plane
   crashed between row and CR) is fixed by sending the same request again:
   - row missing → insert it; row present with the same name and public URL →
     keep it; present with different values → 409.
   - create the CR; if it already exists with the same spec, adopt it (an
     earlier attempt made it); with a different spec → 409.
   - CR create fails otherwise → delete the row only if this request inserted
     it, return 502.
   - 201 when this request inserted the row, 200 when it finished an earlier one.
   `GET /api/gateways` also lists CRs that have no row (e.g. a lost response
   nobody retried) with phase `Orphaned`; `DELETE` removes them.
3. Operator `on.create`: `helm upgrade --install gw-<slug> /charts/gateway -n mercora`
   with values `mode: edge`, `slug`, `ingress.host: <slug>.gw.localhost`,
   `ingress.extraHosts: [<public URL host>]` when a public URL is set,
   `publicUrl`, and the edge image tag from operator settings. Status
   `Provisioning`. Same retry rule as stores (3 attempts, then `Failed`).
   TLS for a public URL comes from what fronts it (ngrok, or cert-manager in
   production); the chart does not manage certificates.
4. Operator timer (15s): deployment `gw-<slug>-gateway` has a ready replica →
   `Ready`; 5 minutes without → `Failed` with the reason.

### 2.3 Delete

`DELETE /api/gateways/{slug}` (admin): deletes the CR, then the row (its
`gateway_servers` cascade; clients of that URL get connection errors).
Operator `on.delete`: `helm uninstall gw-<slug>`, finalizer held until done.

### 2.4 Attach servers

`PUT /api/gateways/{slug}/servers` (admin) with `{"server_ids": [...]}`
replaces the set in one transaction. Edges read the attachment from the
database on every `tools/list` and `tools/call`, so it applies on the next
request; the edge's health loop picks up new servers on its next tick
(30s) and a newly attached server's tools show then.

`GET /api/gateways` (logged in): every gateway with phase and URL (from the
CRs), attached server ids and names. Members see it too, so they know which URL
to connect to.

### 2.5 Operator changes

- `handlers.py`: handlers for `mcpgateways` mirroring the store ones (create,
  progress timer, repair timer, delete). No namespace, quota or guardrails:
  edges live in `mercora` next to the control plane.
- Restarts: kopf records finished handlers in an annotation on the object, so
  an `McpGateway` created while the operator was down still gets its create
  handler on start, and a handler cut off mid-way is retried from the top
  (`helm upgrade --install` is idempotent). No resume handler is needed.
- Repair timer (60s): a `Ready` gateway whose Deployment no longer exists
  (release removed by hand) gets `helm upgrade --install` again.
- `manifests.py`: `edge_values(slug, spec, settings)` (unit tested).
- Dockerfile copies `charts/gateway` into `/charts/gateway`.
- RBAC: add `mcpgateways` and `mcpgateways/status` to the ClusterRole; it
  already covers deployments, services, ingresses and secrets.
- Store registration additionally sends `"store": name`, so the Servers tab
  can say which store a server belongs to.

---

## 3. Edge request flow

For a request to `prod.gw.localhost/mcp`:

1. `ApiKeyGuard` resolves the user from the API key, or from an OAuth token
   whose `resource` equals this edge's `<public_url>/mcp`. A token issued by
   `dev` therefore fails at `prod`. No user → 401.
2. `tools/list`: servers that are enabled **and** attached to `prod` **and**
   granted to the user (admins: all), each from the edge's health-loop cache.
3. `tools/call`: same three conditions checked again. Failing any → the
   existing "Unknown tool" error, audited with `gateway='prod'`, never
   forwarded.
4. Rate limit: **per user, across the whole organization**: 60 calls a
   minute in total, whichever gateways, replicas or keys they come through.
   One Postgres row per user per clock minute, incremented atomically
   (`INSERT ... ON CONFLICT DO UPDATE SET calls = calls + 1 RETURNING calls`).
   shortcut: fixed windows let up to 2 × the limit land around a minute
   boundary; a sliding window can replace it if that matters.
5. Call, timeout, audit row as today, plus `gateway`.

OAuth one-time credentials stay single-use across pods: redeeming a code or a
refresh token is one `DELETE ... WHERE token_hash = ?` whose row count must be
1, and the login step is `UPDATE ... WHERE user_id IS NULL`. Postgres lets only
one concurrent statement remove or claim the row. Tested with two concurrent
redemptions (§8).

`Upstreams` gets one constructor argument `gateway_slug: str | None`; `None`
(control plane) means all servers. The attachment query is one join added to
the existing server queries.

---

## 4. First-run organization setup

- The control plane no longer creates an admin from `ADMIN_USERNAME` /
  `ADMIN_PASSWORD`. Secret `gateway-admin` now holds `SETUP_TOKEN` (and
  optionally `OPENROUTER_API_KEY`).
- `GET /api/setup` (anyone) → `{"needed": true|false, "org": name|null}`.
- `POST /api/setup` (anyone) with `{setup_token, org_name, username, password}`:
  - wrong token → 403 (compared with `hmac.compare_digest`, counted by the
    existing login throttle);
  - in one transaction: insert `org` id 1, create the admin. An existing org
    makes the insert fail → 409 "already set up". Two racing requests: exactly
    one wins.
  - success → session cookie, as after login.
- Every other `/api` route returns 503 `{"error": "setup needed"}` until the
  org exists. Edges cannot exist before setup (creating one needs an admin).
- Dashboard: on load, `GET /api/setup`; `needed` → a Setup screen (setup
  token, organization name, admin username, password, confirm) instead of
  Login. The org name shows in the sidebar header. If `GET /api/setup` fails
  (control plane down), the dashboard shows that error with a Retry button,
  never the Login screen as if setup were done.

---

## 5. Dashboard

- **Setup** screen (§4).
- **Gateways** page (new, in the nav after Servers): table of gateways (name,
  URL, phase with message, attached servers). Admins: "New gateway" form,
  delete with confirm, an "Attached servers" checklist per gateway. Members:
  read-only, with a copy button for the MCP URL.
- **Servers** tab: a "Store" column linking to `http://<store>.localhost`
  when `store` is set; a "Gateways" column listing where each server is attached.

---

## 6. Charts and deploy

- `charts/gateway`: new values `mode` (`control` | `edge`), `slug`,
  `replicas`, `dbSecret` (default `gateway-db`). PVC and `DATABASE_PATH` go.
  Control: Recreate strategy no longer needed; `RollingUpdate`. ServiceAccount
  and Role only when `mode: control` and `stores.enabled`; the Role adds
  `mcpgateways` (get, list, create, delete). Pod label
  `app.kubernetes.io/name: gateway` stays in both modes (upstream
  NetworkPolicies allow it).
- `charts/postgres`: new (§1.2).
- `deploy/postgres-values-local.yaml`, and the operator gets
  `edgeImageTag` and `gatewayChart` settings.
- README: new install order: Secrets → `db` → control (`team`) → operator →
  dashboard → setup in the browser → admin API key into
  `store-operator-gateway` → create stores and gateways.

---

## 7. Errors and edge cases

- Postgres down: `/readyz` fails on every pod (it runs `SELECT 1`), so pods
  leave the Service; requests in flight get 503.
- Edge for a gateway row deleted while running: `/readyz` fails (row missing),
  `/mcp` lists nothing, until the operator removes the release.
- Server deleted while attached: cascade removes the attachment.
- Rate limit is shared through Postgres (§3), so replicas and gateways cannot
  multiply it.
- Login throttle is per control-plane pod; the control plane runs 1 replica.

---

## 8. Testing

Gateway tests run against a real Postgres: `TEST_DATABASE_URL` (for example
`docker run -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine`). A fixture
creates a fresh database per test and drops it after. Without the variable,
the tests are skipped with a message saying how to start one.

- Existing gateway, OAuth and store tests pass on Postgres (apart from the
  deleted SQLite migration tests).
- Setup: wrong token 403; first setup 201 and logs in; second 409; two
  concurrent setups → one 201, one 409; `/api/users` before setup → 503.
- Gateways API: create (fake Kube records the CR), duplicate slug 409, bad
  slug 400, member cannot create (403), attach replaces the set, delete
  removes row and CR.
- Edge: an edge app for `prod` lists only attached servers' tools; a call to an
  unattached server's tool is refused, audited with `gateway='prod'`, and the
  upstream receives nothing; an OAuth token minted for `dev`'s resource gets
  401 at `prod`.
- Gateways API retries: the cluster rejects → 502, no row left, retry 201;
  the CR is made but the response is lost → 502, the CR shows as `Orphaned`,
  retry adopts it (201); an orphan can be deleted.
- Replicas: two edge apps for the same gateway; `initialize` on one, then
  `tools/call` on the other succeeds, and no `Mcp-Session-Id` is issued.
- Rate limit: two `Upstreams` on the same database (as two pods) share one
  user's limit.
- Host allowlist: a valid key with `Host: evil.example` gets 421.
- OAuth: two concurrent redemptions of one code → exactly one succeeds.
- `Result.fetchone()` advances like a cursor.
- Operator: `edge_values` unit tests (with and without a public URL); handlers
  checked on the cluster.

**Demo check (k3d):** fresh install by the README; setup in the browser;
create stores `shop1`, `shop2`; create gateways `prod` (attach `shop1`) and
`dev` (attach `shop2`, `notes`); `kubectl -n mercora get mcpgateways` shows
both Ready; `tools/list` on `prod.gw.localhost/mcp` with a member's key shows only
`shop1__*`, and on `dev` only `shop2__*` and `notes__*`; a hand-made
`tools/call` for `shop2__list_orders` on `prod` is refused and audited;
`kubectl -n mercora get pods` shows two `gw-prod` pods. Scaling checks:
20 calls to `prod` are answered by both pods (both pods' logs show
requests); 61 calls in a minute split across `prod` and `dev` → the 61st is
refused; deleting one `gw-prod` pod while calls run leaves the calls working
(at most the request in flight on that pod fails).

---

## 9. Out of scope (later parts or not planned)

- Teams, policies, tool-level grants: part 2.
- Personal identity, `user_credentials`, woo-mcp per-request keys: part 3.
- Per-gateway NetworkPolicy (only gateway X may reach store Y): enforced by
  the attachment check instead.
- Resource-level rules (order #100), multi-company tenants, self-service
  signup, migrating the SQLite data.
