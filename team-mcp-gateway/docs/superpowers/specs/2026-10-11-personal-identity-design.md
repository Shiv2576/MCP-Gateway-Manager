# Gateways part 3: per-user denies and shared vs personal upstream identity

Part 3 of the gateways work (part 1: `2026-10-11-gateways-control-edge-design.md`,
part 2: `2026-10-11-teams-tool-policies-design.md`). It adds two things:

- **Per-user deny overrides:** an admin can take a tool away from one member
  even though one of their teams allows it.
- **Personal identity:** each gateway-server connection is either *shared*
  (today: the server's one credential) or *personal* (the member's own
  credential), so the upstream sees, logs and limits the real person.

Masking PII with Presidio and admin-approved reveals are later, separate specs.

---

## 0. Agreed requirements

From the conversation:

1. **Effective tool permission** is the union of the member's team grants minus
   their per-user denies, checked on every call (not only in the listing).
   Denies only take away; extra access means another team.
2. **Personal identity selects a member's upstream credential only after tool
   authorization succeeds.** Identity never grants a tool.
3. **A personal connection never uses the shared credential, silently or
   otherwise.** A tool call on a personal connection sends the member's
   credential and nothing of the shared one. Missing credential: the call
   fails. No fallback of any kind.
4. **A pasted credential is not proof of identity.** The gateway records who
   set which credential and when; checking that a key really belongs to that
   person is a later feature (section 10).
5. **A credential that cannot be decrypted is a distinct error** from a missing
   one, and also fails closed.
6. **Removing a server from a gateway deletes the policy rows scoped to that
   attachment** (team allows and user denies). Re-attaching starts empty. This
   replaces part 2's rule that rows survived a detach.
7. **Credentials never appear in logs, audit rows, error messages or API
   responses,** and one request's credential cannot reach another concurrent
   request.
8. **Upstream authorization, upstream audit logging and record-level access
   are separate capabilities** of each upstream. The identity setting does not
   guarantee them.
9. Shared or personal is set **per gateway-server connection**: the same server
   can be shared on `prod` and personal on `dev`.

Decided in this spec (not stated by the user):

- Denies are per gateway, like team policies, and apply to members only.
  Admins keep every tool.
- A personal credential belongs to a member and a server, not a gateway.
- Only the member enters, replaces or deletes their own credential. Nobody
  reads it back, admins included; admins see whether one is set and when.
- Personal credentials are encrypted at rest (Fernet, key from a Secret).
  Shared server credentials stay as they are.
- **Tool listing and the health loop keep using the server's shared
  credential.** They are the gateway checking the server, not a member acting;
  no member's tool call ever goes out on it (requirement 3). A member with no
  credential sees the tools, and the call says what is missing.
- Admins follow the same identity rule: on a personal connection they need
  their own credential.
- There is no "disabled user" today; deleting a user is the way to cut access,
  and it removes their credentials, keys and tokens.

---

## 1. Data model

```sql
-- Which credential a connection uses.
ALTER TABLE gateway_servers ADD COLUMN IF NOT EXISTS identity TEXT NOT NULL DEFAULT 'shared'
    CHECK (identity IN ('shared', 'personal'));

-- Per-user denies, scoped to an attachment: detaching deletes them.
CREATE TABLE IF NOT EXISTS user_denies (
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gateway_id BIGINT NOT NULL,
    server_id  BIGINT NOT NULL,
    tool       TEXT NOT NULL,
    PRIMARY KEY (user_id, gateway_id, server_id, tool),
    FOREIGN KEY (gateway_id, server_id)
        REFERENCES gateway_servers (gateway_id, server_id) ON DELETE CASCADE
);

-- The header a personal credential goes in, e.g. X-Woo-Credentials or Authorization.
ALTER TABLE servers ADD COLUMN IF NOT EXISTS personal_header TEXT;

-- One credential per member per server, encrypted (Fernet token).
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id    BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    server_id  BIGINT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    secret     TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, server_id)
);

-- Whose credential a call used: 'shared' or 'personal'. NULL when nothing was sent.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS identity TEXT;
```

**`team_tools` becomes attachment-scoped too.** The schema step first
deletes rows whose `(gateway_id, server_id)` is not attached, then adds
`FOREIGN KEY (gateway_id, server_id) REFERENCES gateway_servers ON DELETE
CASCADE` (inside a `DO` block that skips it when the constraint already
exists, since Postgres has no `ADD CONSTRAINT IF NOT EXISTS`).

**`PUT /api/gateways/{slug}/servers` stops deleting and re-inserting every
row.** It keeps its body (`{server_ids}`), but now deletes only the servers
that were removed (which cascades their policy rows) and inserts the new ones
as `shared`. Servers that stay attached keep their identity and policies.

## 2. The access rule

`db.allowed_tools(conn, user, gateway_slug)` keeps its signature and return
type. For a member, it subtracts their `user_denies` rows for that gateway:

- the server maps to the set of tools left;
- a server whose set is empty is left out, so it looks the same as having no
  policy at all.

Admins are unchanged: `{server_id: None}`. Denied tools are hidden from the
listing and refused on a call with the existing `Unknown tool` message,
audited `ok=0`, never forwarded.

## 3. Picking the credential

A new function `db.connection_identity(conn, gateway_slug, server_id) -> str`
returns `'shared'` or `'personal'`. `Upstreams.call_tool` reads it after the
permission check and before connecting:

| Connection | Member's stored credential | Headers sent upstream | Audit |
|---|---|---|---|
| shared | (ignored) | the server's `auth_header: auth_value`, as today | `identity=shared` |
| personal | decrypts | **only** `personal_header: <value>`; the shared header is not sent | `identity=personal` |
| personal | none | nothing: the call does not happen | `identity=NULL`, `ok=0` |
| personal | cannot be decrypted | nothing: the call does not happen | `identity=NULL`, `ok=0` |

The errors the member sees:

- missing: `Server 'woo' uses your personal credential on gateway 'prod', and
  you have not added one. Add it in the dashboard under My credentials.`
- unreadable: `Your saved credential for server 'woo' cannot be read. Enter it
  again in the dashboard under My credentials.`

`connect(row, timeout, headers)` takes the headers to send, chosen by the
caller; the health loop passes the shared one. A credential is decrypted inside
`call_tool` just before connecting, never logged, never put in an audit row or
an error message. Every call already builds its own HTTP client, so two
concurrent calls never share headers.

**Rules on `personal_header`:**

- a valid header name, and not one the transport owns: `Host`,
  `Content-Length`, `Content-Type`, `Accept`, `Connection`,
  `Transfer-Encoding`, or any `Mcp-*` header (400);
- a connection can be set to `personal` only when the server has one (400);
- clearing it while any connection to the server is personal: 400.

The personal header may have the same name as the shared one (for example
both `Authorization`). That is allowed, and it changes nothing: on a personal
connection only the personal header is sent anyway.

## 4. Encryption key

- New setting `CREDENTIALS_KEY` (a Fernet key: 32 url-safe base64 bytes),
  required in both `control` and `edge` modes. Missing or malformed: the
  process refuses to start with a clear `ConfigError`.
- It comes from a user-made Secret `gateway-credentials`
  (`CREDENTIALS_KEY`). `charts/gateway` gains `credentialsSecret`, and the
  operator's `edge_values` passes it to every edge.
- `cryptography` (already in `uv.lock` through another dependency) becomes a
  direct dependency.
- Key rotation is out of scope. Changing the key makes every stored value
  unreadable (section 3's second error), and members enter theirs again.

## 5. API (control plane)

| Endpoint | Who | Notes |
|---|---|---|
| `PUT /api/gateways/{slug}/servers/{id}/identity` `{identity}` | admin | `shared` or `personal`. Server not attached: 404. `personal` without `personal_header`: 400 |
| `GET /api/gateways` | logged in | each attached server gains `identity` |
| `POST /api/servers`, `PATCH /api/servers/{id}` | admin | optional `personal_header`: a header name per section 3, or `null` to clear it |
| `GET /api/servers` | logged in | rows gain `personal_header` |
| `GET /api/users` | admin | rows gain `denies: {<gateway slug>: [{server_id, server, tool}]}` and `credentials: [{server, updated_at}]` (never values) |
| `PUT /api/users/{id}/gateways/{slug}/denies` `{tools: [{server_id, tool}]}` | admin | replaces this user's denies on this gateway. Server must be attached (400), tool 1-128 chars, target user an admin: 400 |
| `GET /api/me/credentials` | logged in | `[{server_id, server, personal_header, set, updated_at, gateways: [slug]}]`: every server the member has an allowed tool on through a personal connection, plus any server they already stored a credential for |
| `PUT /api/me/credentials/{server_id}` `{value}` | logged in | 1-4096 characters, one line, else 400; the error never echoes the value. Server has no `personal_header`: 400. Returns `{set: true, updated_at}` |
| `DELETE /api/me/credentials/{server_id}` | logged in | |

**Credential changes are audited** in `audit_log`: `tool` is
`credential:set`, `credential:replace` or `credential:delete`, with the user
and server, `ok=1`, and no value. They show up on the existing Audit page.

## 6. `woo-mcp`: reading the person

Today `woo-mcp` accepts one bearer token and signs every WooCommerce request
with one consumer key from its env. Requirement 3 means a personal call
carries no shared token, so the guard changes:

- **A request passes the guard with either** the shared bearer token (the
  gateway's health loop and shared connections, signed with the env key as
  today) **or** an `X-Woo-Credentials: <consumer_key>:<consumer_secret>`
  header (personal calls). Someone who can reach the pod and holds a valid
  WooCommerce key could already call WooCommerce directly with it, so this
  opens nothing new; the NetworkPolicy still limits who can reach the pod.
- With `X-Woo-Credentials`, every WooCommerce request for that MCP request is
  signed with that key and **never** the env key, even if a bearer token is
  also present. Set in a `contextvars.ContextVar` by a small middleware and
  reset when the request ends.
- Malformed header (no colon, an empty part): HTTP 400, value not echoed.
- A key WooCommerce rejects: WooCommerce's 401 comes back as the tool error,
  as any WooCommerce error does today.

The member makes their own WooCommerce REST key (WooCommerce → Settings →
Advanced → REST API, for their own WordPress user, Read or Read/Write) and
pastes `key:secret` in the dashboard. Admins set the `woo` server's
`personal_header` to `X-Woo-Credentials`.

What this does and does not give (requirement 8): WooCommerce enforces the
key's Read vs Read/Write permission and its WordPress user's capabilities.
What WooCommerce logs is not assumed: section 8 checks it on the cluster.
WooCommerce has no rule like "this user may see only these orders";
record-level limits need the upstream to support them.

## 7. Dashboard

- **Servers form:** a "Personal credential header" field (empty = this server
  cannot be used with personal identity).
- **Gateways page:** each attached server shows Shared or Personal, with a
  toggle (disabled with a hint when the server has no personal header).
  Detaching a server warns that its team policies and denies on this gateway
  are deleted.
- **Members page:** a Denies editor per member per gateway, listing the tools
  the member's teams give them there, each with a "deny" checkbox; and the
  servers the member has a credential for, with when it was last set.
- **My credentials page** (Settings group, every user): one row per server
  from `GET /api/me/credentials` with the gateways that need it, its header
  name, Set / Not set and last updated, and Add, Replace and Remove. A
  password-style input, never pre-filled.
- **Tools and Chat:** no change. A missing or unreadable credential shows up
  as the tool error from section 3.

## 8. Testing

Gateway tests (Postgres, as in parts 1-2). The upstream is a demo server that
records the headers each call arrived with.

Access rule:

- a deny removes one tool from a member's union, from the list and the call;
- denying every allowed tool on a server: the member can neither list nor call
  any of its tools;
- a deny on another gateway changes nothing; admins unaffected; denies API is
  admin-only, admin target 400, unattached server 400;
- detach then re-attach: team allows **and** user denies are gone, the member
  sees nothing until granted again (replaces part 2's survive-detach test);
- re-PUT of attachments with the server still in the list keeps its identity,
  allows and denies;
- schema upgrade with an orphaned `team_tools` row: the row is deleted and
  the foreign key added; running the schema twice is fine.

Identity:

- shared connection: the server credential is sent, audited `shared`;
- personal with a credential: **only** the personal header is sent (the shared
  header is absent), audited `personal`; same when both headers have the same
  name;
- personal without a credential: the missing error, `ok=0`, upstream never
  called;
- personal with an undecryptable value (written with another key): the
  unreadable error, `ok=0`, upstream never called, no shared fallback;
- two members call a personal connection concurrently (`asyncio.gather`):
  each call carries only its own member's value;
- a denied tool on a personal connection: `Unknown tool`, no credential
  decrypted, upstream never called;
- deleting a member: their API key and OAuth token stop working and their
  credentials are gone.

Credentials and config:

- a member sets, replaces and deletes their own; the value never comes back
  from any endpoint; the stored column is not the plaintext; each change is
  audited without the value;
- invalid input (empty, too long, a newline): 400, and the value appears in
  neither the response, the audit log nor the captured log output;
- `personal_header` rules from section 3 (reserved names 400, personal toggle
  without one 400, clearing while in use 400);
- startup without or with a malformed `CREDENTIALS_KEY`: `ConfigError`.

`woo-mcp` tests: `X-Woo-Credentials` without a bearer token passes the guard
and signs with the supplied key; with both, still the supplied key; neither:
401; malformed: 400 without echoing; two concurrent requests with different
keys each sign with their own.

**On the cluster, by hand, two separate checks** with two WooCommerce keys
made by two WordPress users (alice Read/Write, bob Read only):

1. Permissions: bob's update call fails in WooCommerce and alice's works,
   while both read.
2. Upstream audit: write down what WooCommerce actually records for each
   action (order notes, author fields, any logs), reads included, so nobody
   assumes it logs what it does not.

## 9. Rollout

- Images: `gateway:0.4.0`, `dashboard:0.4.0`, `woo-mcp` next tag; operator
  `edgeImageTag: "0.4.0"`.
- The user creates the Secret `gateway-credentials` before upgrading (the
  gateway will not start without it).
- Existing connections become `shared`, so nothing changes until an admin
  switches one to personal. Any `team_tools` row left over from a detached
  server is deleted by the upgrade.

## 10. Out of scope

- Allow overrides (another team covers it).
- Proving that a credential belongs to its member (a per-upstream "whoami"
  check, e.g. in `woo-mcp`).
- OAuth sign-in to upstreams for personal credentials (pasted values only).
- Key rotation; admins setting or reading members' credentials.
- A "disable user" switch (deleting the user is the way today).
- PII masking (Presidio) and approved reveals: their own specs.
- Record-level rules in the gateway.

---
---

# Appendix: manual end-to-end test cases

Run these on the `store-check` cluster (namespace `mercora`) after rolling out
0.4.0. They are for a person at a terminal and a browser; they do not replace
the automated tests in section 8.

## A. Setup

1. **Secret first.** Create the key and the Secret (the value never needs to
   leave your terminal):

   ```sh
   kubectl -n mercora create secret generic gateway-credentials \
     --from-literal=CREDENTIALS_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
   ```

2. Upgrade `team`, `team-ui`, `store-operator` and the `woo-mcp` image as in
   section 9.
3. A store with `woo-mcp` is provisioned and registered as server `woo`.
4. Gateways: `prod` and `dev` (Gateways page), both with `woo`, `notes` and
   `fx` attached. Wait for `kubectl -n mercora get mgw` to show both ready.
5. Users: admin, plus members `rahul` and `shivang`. Team `Development` with
   both members, allowed on `prod`: `woo: list_orders, get_order,
   update_order_status`, `notes: list`.
6. One API key per member (API keys page, signed in as each). In the shell:

   ```sh
   RAHUL=<rahul's key>; SHIVANG=<shivang's key>
   mcp() {  # mcp <key> <gateway> <json-rpc method> <params json>
     curl -s "http://$2.gw.localhost/mcp" \
       -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
       -H 'Accept: application/json, text/event-stream' \
       -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$3\",\"params\":$4}"
   }
   tools() { mcp "$1" "$2" tools/list '{}' | grep -o '"name":"[a-z_]*__[a-z_]*"'; }
   call()  { local args=${4:-'{}'}; mcp "$1" "$2" tools/call "{\"name\":\"$3\",\"arguments\":$args}"; }
   psql_gw() { kubectl -n mercora exec -i db-postgres-0 -- psql -U postgres -d gateway -c "$1"; }
   ```

7. Two WooCommerce REST keys, each made **while logged in to WordPress as a
   different user**: alice (shop manager) with Read/Write, bob (shop manager)
   with Read. Note each `ck_...:cs_...` pair; you paste them in step D.

## B. Per-user denies

| # | Do | Expect |
|---|---|---|
| B1 | `tools $RAHUL prod` and `tools $SHIVANG prod` | Both list `woo__list_orders`, `woo__get_order`, `woo__update_order_status`, `notes__list` |
| B2 | Members page → rahul → Denies on `prod` → tick `woo: update_order_status` → Save | Saved; rahul's row shows the deny |
| B3 | `tools $RAHUL prod` | `woo__update_order_status` gone; the rest still there |
| B4 | `tools $SHIVANG prod` | Unchanged: still has `woo__update_order_status` |
| B5 | `call $RAHUL prod woo__update_order_status '{"order_id":1,"status":"completed"}'` | `Unknown tool 'woo__update_order_status'...`; nothing changes in WooCommerce |
| B6 | Audit page | A row for rahul, `woo__update_order_status`, gateway `prod`, failed |
| B7 | `tools $RAHUL dev` | No woo tools at all (Development has no policy on `dev`); the deny on `prod` did not leak to `dev` |
| B8 | Deny rahul all three woo tools on `prod`; `tools $RAHUL prod` and `call $RAHUL prod woo__list_orders` | No `woo__` tools listed; the call is `Unknown tool` |
| B9 | Members page → try to add a deny for the admin | Refused (400 shown) |
| B10 | Clear rahul's denies again | B1's list is back for rahul |

## C. Detach deletes policies

| # | Do | Expect |
|---|---|---|
| C1 | Deny rahul `woo: get_order` on `prod` (keep the team allows) | Saved |
| C2 | Gateways page → `prod` → detach `woo` | Warning that its team policies and denies on `prod` are deleted; confirm |
| C3 | `psql_gw "SELECT count(*) FROM team_tools tt JOIN servers s ON s.id=tt.server_id WHERE s.name='woo'"` and the same for `user_denies` | `0` for `prod` rows (rows for other gateways, if any, remain) |
| C4 | Re-attach `woo` to `prod`; `tools $SHIVANG prod` | No `woo__` tools: nothing came back |
| C5 | Teams page → Development → `prod` | No woo tools ticked; re-tick them and save; `tools $SHIVANG prod` shows them again |
| C6 | Gateways page → `prod` → change only `fx` (detach it), save | `woo` keeps its policies and its identity setting |

## D. Personal identity

| # | Do | Expect |
|---|---|---|
| D1 | Servers page → `woo` → Personal credential header `Host` → save | Refused (400): reserved header |
| D2 | Gateways page → `prod` → set `woo` to Personal while it has no personal header | Toggle disabled with a hint (or 400) |
| D3 | Servers page → `woo` → header `X-Woo-Credentials` → save; Gateways → `prod` → `woo` Personal | Saved; `dev` still shows `woo` Shared |
| D4 | `call $RAHUL prod woo__list_orders` (rahul has no credential yet) | Error: `Server 'woo' uses your personal credential on gateway 'prod', and you have not added one...` |
| D5 | Audit page | That call failed, identity empty |
| D6 | `kubectl -n <store ns> logs deploy/<woo-mcp>` around D4 | No request arrived from the gateway for that call |
| D7 | Sign in as rahul → My credentials | `woo` listed, gateway `prod`, header `X-Woo-Credentials`, Not set |
| D8 | Paste bob's (Read) `ck_...:cs_...` → Save | Set, with a time; the input clears and never shows the value again; reload: still never shown |
| D9 | Sign in as shivang → My credentials → paste alice's (Read/Write) pair | Set |
| D10 | `call $RAHUL prod woo__list_orders` and `call $SHIVANG prod woo__list_orders` | Both succeed |
| D11 | `call $RAHUL prod woo__update_order_status '{"order_id":<id>,"status":"on-hold"}'` (clear rahul's deny first if B is still active) | Fails with WooCommerce's permission error: bob's key is Read only. Order unchanged |
| D12 | Same call as shivang | Succeeds: alice's key can write. Order status changed |
| D13 | Audit page | D10-D12 show identity `personal` |
| D14 | `call $RAHUL dev woo__list_orders` after granting Development woo on `dev` | Succeeds with **identity `shared`** in the audit: `dev` is still shared |

## E. No fallback, ever

| # | Do | Expect |
|---|---|---|
| E1 | rahul → My credentials → Remove `woo`; `call $RAHUL prod woo__list_orders` | The missing-credential error; no request reaches `woo-mcp` |
| E2 | Paste `garbage` (no colon) as rahul's credential; call again | Error from `woo-mcp` (400 malformed) as the tool error; **not** a successful call with the store's own key |
| E3 | Paste a well-formed but revoked/unknown `ck_x:cs_y`; call | WooCommerce 401 as the tool error; not a success |
| E4 | Unreadable credential: change the key and restart (`kubectl -n mercora create secret generic gateway-credentials --from-literal=CREDENTIALS_KEY=<new key> --dry-run=client -o yaml \| kubectl apply -f -`, then `kubectl -n mercora rollout restart deploy/team-gateway deploy/gw-prod-gateway`); `call $SHIVANG prod woo__list_orders` | `Your saved credential for server 'woo' cannot be read. Enter it again...`; the call does not happen. Re-enter alice's pair and it works again |
| E5 | Delete the `gateway-credentials` Secret and restart one edge | The pod crash-loops with a clear `CREDENTIALS_KEY` error in its log, instead of starting without it. Recreate the Secret afterwards |

## F. Secrets stay secret

| # | Do | Expect |
|---|---|---|
| F1 | `psql_gw "SELECT user_id, server_id, left(secret, 20) FROM user_credentials"` | Fernet tokens (`gAAAAA...`), not `ck_...` |
| F2 | `kubectl -n mercora logs deploy/team-gateway` and `deploy/gw-prod-gateway` piped through `grep -c 'ck_\|cs_'` | `0` |
| F3 | Audit page | `credential:set` / `credential:replace` / `credential:delete` rows for rahul and shivang, with the server, and no value anywhere |
| F4 | Browser dev tools → Network → `GET /api/me/credentials` and `GET /api/users` (as admin) | `set`, `updated_at`, server names; no value field |
| F5 | My credentials → paste a value with a newline (or 5000 characters) | 400; the error text does not contain what you pasted |

## G. Concurrency

| # | Do | Expect |
|---|---|---|
| G1 | Run 20 calls from each member at once: `for i in $(seq 20); do call $RAHUL prod woo__update_order_status '{"order_id":<id>,"status":"on-hold"}' & call $SHIVANG prod woo__update_order_status '{"order_id":<id>,"status":"processing"}' & done; wait` | Every rahul call fails with the Read-only permission error; every shivang call succeeds. Any rahul success means a credential leaked between requests |
| G2 | WooCommerce order notes for that order (section H) | Every change attributed to alice, never bob |

## H. What WooCommerce actually records (requirement 8)

Write down what you find; do not assume.

| # | Check | Record |
|---|---|---|
| H1 | Order edit screen → Order notes after D12 / G1 | Is the status change noted, and is the author alice? |
| H2 | WooCommerce → Status → Logs | Anything for the REST calls? Reads too, or only writes? |
| H3 | Users → alice / bob → REST API keys | "Last access" updated for each key after their calls? |
| H4 | The gateway's Audit page for the same window | Compare: the gateway knows rahul/shivang; WooCommerce knows bob/alice |

## I. Deleting a member

| # | Do | Expect |
|---|---|---|
| I1 | Admin deletes rahul | Done |
| I2 | `tools $RAHUL prod` | 401: his key no longer works |
| I3 | A Claude client signed in as rahul by OAuth to `prod` | Next request asks to sign in again |
| I4 | `psql_gw "SELECT count(*) FROM user_credentials WHERE user_id=<rahul id>"` and the same for `user_denies` | `0` |
