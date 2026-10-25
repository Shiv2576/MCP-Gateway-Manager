# Project context: Team MCP Gateway on Kubernetes

What has been built, how it fits together, what has been verified, and what is
still open. Last updated 2026-10-25 (at commit 393d42b).

No secret values are written here. Where a password or key lives is named
instead (Kubernetes Secrets in namespace `mercora`).

Deeper docs, kept current alongside this file:

| Doc | Covers |
|---|---|
| `../README.md` (repo root) | setup (k3d, VPS), using the store, design trade-offs, stand-out features |
| `docs/reference.md` | the 23 woo-mcp tools, gateway internals, REST API, OAuth, identity, dashboard pages, store provisioning |
| `docs/design.md` | architecture choices, aggregation, routing, failure handling, local vs prod |
| `docs/threat-model.md` | who is trusted, what each control stops |
| `docs/cluster-architecture.md` | namespace/object map, traffic and control-loop flows |
| `docs/gitops.md` | CI → GHCR → Argo CD runbook |
| `docs/deploy-vps.md`, `docs/vps-setup-log.md` | VPS install, LXC/IPv6 quirks, everything done on the box |
| `docs/upgrades.md` | backups, rollback |
| `docs/production-values.md` | local vs prod Helm values |

---

## 1. The goal

A WooCommerce store on Kubernetes, plus a team **MCP gateway**:

- Admins register upstream MCP servers (Streamable HTTP), each with an optional
  static credential (header or bearer token).
- A gateway is one MCP endpoint. Listing tools returns the tools of every
  attached, enabled server the caller may use. A call is routed to the right server.
- Two servers may expose a tool with the same name; a documented scheme handles it.
- A slow or dead upstream must not break listing or calls to other servers.
  Calls have timeouts and return clear errors.
- Upstream credentials stay on the server. Never sent to a browser or MCP client.
- At least three upstreams: the WooCommerce MCP server plus two others.
- A React dashboard where admins manage servers, users, keys and see the audit log.

Built on top (each covered below): control plane + per-gateway edges, Postgres,
teams with per-tool policies, per-member denies, shared vs personal upstream
identity, write budgets + admin approvals, PII masking, team alerts, chat agent
with guardrails, store provisioning operator, hosted MCP servers from an image,
OAuth for Claude custom connectors, Reports + live WordPress metrics, network
hardening, a production VPS with TLS, and a GitOps pipeline.

Removed along the way: the `customer` role, per-member server grants
(`user_servers`, replaced by teams), SQLite (replaced by Postgres), the
hand-installed single `mercora` store, the operator's admin API key (replaced
by a Kubernetes JWT), `scripts/deploy-vps.sh` as the deploy path (replaced by Argo CD).

---

## 2. Architecture

```
 ┌────────────────────────────── cluster (k3d local / k3s VPS) ───────────────────────────────┐
 │ namespace mercora (platform, default-deny NetworkPolicies)                                   │
 │                                                                                              │
 │  Browser ─ dash.<base> ─► team-ui (nginx: React app, /api ─► team-gateway)                   │
 │                           team-gateway  GATEWAY_MODE=control: /api, chat, setup, stores      │
 │                              │   writes CRs: Store (st), McpGateway (mgw), HostedServer (hmcp)│
 │                              ▼                                                               │
 │                           store-operator (kopf) ── Helm installs ──┐                         │
 │                                                                    │                         │
 │  MCP clients ─ <slug>.gw.<base>/mcp ─► gw-<slug> (2 edge pods, GATEWAY_MODE=edge, /mcp only) │
 │  claude.ai (OAuth) ──────────────────┘     serves only servers attached to that gateway      │
 │                                                                                              │
 │  db-postgres (one shared Postgres for control + every edge)                                  │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
        │ tool calls (credential added server-side)                │ creates
        ▼                                                          ▼
 store-<name>: woo-mcp (23 tools) ─► WordPress/WooCommerce ─► MariaDB   (quota, limits, default-deny)
 mcp-<name>:   hosted MCP server from an image (HostedServer)
 internet:     exa (https://mcp.exa.ai/mcp), mslearn (https://learn.microsoft.com/api/mcp), no auth
```

- One gateway image, two modes. **Control** (`team`, `gateway.<base>`) runs the
  dashboard API, chat, setup and provisioning and can only create the custom
  resources. **Edges** (`gw-<slug>`, made by the operator from an `McpGateway`)
  serve only `/mcp`, have no Kubernetes token, so public MCP traffic can't take
  down admin.
- Edges are stateless MCP (`stateless_http`), so any replica answers any request.
  Policy, keys, rate limits and audit all live in Postgres, so a change applies
  on the next call on every pod.
- Every store lives in `store-<name>` with its own woo-mcp, known to the gateway
  as server `<name>` (tools `<name>__list_orders`, …).

---

## 3. Repository layout

```
.github/workflows/ci.yaml   CI: path-filtered tests, build, smoke test, push to GHCR, bump prod tags
README.md                   (repo root) the user-facing README
team-mcp-gateway/
  context.md                this file
  ROADMAP.md                the original phase plan (phases 0-7)
  docs/                     see the table at the top; docs/superpowers/ holds specs + plans
  scripts/smoke.sh          image smoke test used by CI
  scripts/deploy-vps.sh     old manual deploy; do not use since GitOps went live
  services/
    woo-mcp/                WooCommerce MCP server, 23 tools (app/server.py, app/woo.py OAuth 1.0a)
    gateway/app/
      main.py               settings, mode (control/edge), /mcp guard (API key, OAuth, operator JWT), wiring
      db.py                 Postgres schema + migrations, hashing
      upstream.py           health loop, tool cache, policy filter, call_tool (the only call path)
      api.py                users, keys, servers, tools, audit, reports
      setup.py              first-run setup (SETUP_TOKEN → org + first admin)
      teams.py              teams, members, per-gateway tool policies, budgets, alert rules, denies
      approvals.py          write budgets, approval requests, gateway__request/wait_for_approval
      alerts.py             team alerts, gateway__list_team_alerts / mark_team_alert_done
      pii.py, pii_api.py    PII masking of tool results, rules API
      credentials.py        Fernet encryption of upstream + personal credentials, SSRF check
      gateways.py           McpGateway CRs, attach servers, shared/personal identity
      hosted.py             HostedServer CRs (MCP server from an image)
      stores.py, kube.py    Store CRs, store limit, live metrics, TokenReview client
      chat.py               chat agent (POST /api/chat) with guardrails
      oauth.py              OAuth authorization server for /mcp
    store-operator/         kopf: Store / McpGateway / HostedServer → namespaces + Helm releases
      store_operator/{handlers,manifests,gateway}.py
    demo-mcp/               notes + fx demo upstreams (still deployable, not registered)
    dashboard/src/          React 19 + Vite: one .jsx per page (see 4.10)
  charts/                   gateway, dashboard, postgres, store, woo-mcp, store-operator (CRDs), demo-mcp
  deploy/                   *-values-local.yaml, *-values-prod.yaml, tls-prod.yaml, argocd/
```

---

## 4. Components

### 4.1 Store (`charts/store`)

Bitnami WordPress + MariaDB, Ingress per host, a setup Job (WooCommerce,
permalinks, cash on delivery, API key from Secret `woo-mcp-credentials`, seed
data via `files/seed.php`), and a `restock.php` mu-plugin (purchase orders).
Templates: `catalog-a` (40 products with cost and stock + 90 days of sales
history, ~1,500 orders: the restock demo, setup ~5 min), `catalog-b` (300
products, 7 orders), `blank` (3 products, 2 orders). Seed orders are dated the
day the store is created. An apache-exporter sidecar exposes busy workers on
`store-wordpress-metrics:9150` (the dashboard no longer shows it, see 4.11).

### 4.2 woo-mcp (`services/woo-mcp`)

23 tools (full list: `docs/reference.md`): products, orders, bulk updates and
refunds, read-only reports, cart recovery (`get_abandoned_carts`,
`create_cart_offers`), inventory (`get_inventory_health`, `create_restock_plan`,
`list_restock_orders`, `receive_stock`, `create_clearance_offers`). Write tools
carry `readOnlyHint: false`; the gateway's budgets and the chat's confirm step
rely on it.

Calls `/wp-json/wc/v3` over plain in-cluster HTTP, so every request is OAuth
1.0a signed against `WOO_PUBLIC_URL` while sent to `WOO_INTERNAL_URL`. Orders
never return customer email, phone or street address. Personal identity: a
`X-Woo-Credentials: <key>:<secret>` header makes it act as that member's own
WooCommerce key.

### 4.3 Gateway core

**Storage: one Postgres** (`charts/postgres`, release `db`, Service
`db-postgres`, 5 Gi on the VPS). Tables: `org`, `users`, `sessions`,
`api_keys`, `servers`, `gateways`, `gateway_servers`, `teams`,
`team_members`, `team_tools`, `team_budgets`, `team_alert_rules`,
`team_alerts`, `user_denies`, `user_credentials`, `approvals`, `rate_limits`,
`audit_log`, `pii_rules`, `pii_seeded`, `oauth_clients`, `oauth_codes`,
`oauth_tokens`.

**Setup:** on an empty database the dashboard asks for `SETUP_TOKEN` (Secret
`gateway-admin`), the org name and the first admin; `POST /api/setup` works once.
It is public until done, so finish it right after install.

**Upstream manager (`upstream.py`):** every 30 s each pod lists its servers'
tools in parallel (5 s timeout each) and caches them with a status. Listing
reads only the cache. A call opens a fresh client under `CALL_TIMEOUT_S`
(chart `callTimeoutSeconds`, 30 s in prod since `get_inventory_health` hit 10 s).
`call_tool(user, gateway, name, args)` is the only call path, used by `/mcp`,
the Tools page and chat: policy → rate limit → budget → enabled/attached check
→ PII filter on the result → audit row (written before the call).

**Collision scheme:** every tool is `<server>__<tool>`, always prefixed; server
names are `[a-z0-9-]` so the first `__` splits; names can't be edited; tools
whose full name breaks MCP rules are hidden and counted. The gateway's own tools
use the reserved prefix `gateway__`.

**Credentials:** header or bearer, encrypted with Fernet (`CREDENTIALS_KEY`,
Secret `gateway-credentials`), decrypted only in memory at call time, never
returned (API shows `auth_header`, `has_credential`). Client headers are never
forwarded upstream; echoed keys are scrubbed from results.

**SSRF:** a human admin's server URL must resolve to public addresses only.
Only the operator may register in-cluster servers (its own `*.store-*` /
`*.mcp-*.svc.cluster.local`), and only it may delete them; edits check the URL
only when it changes. An egress NetworkPolicy backs it up.

**Auth:** browser session cookie (HttpOnly, SameSite=Strict, `Secure` in prod);
API keys `mcpg_…` (shown once, hashed); OAuth tokens on edges with a public URL
(4.9); the operator's ServiceAccount JWT on `/api/servers` only (4.8).
Wrong-password limit 5 per 15 min per username.

**Rate limit:** 20 calls/min per user, in Postgres (`rate_limits`), so it holds
across all pods; refused calls are audited.

REST API: `docs/reference.md` has the main table. Added since: `/api/approvals`
(+ approve/reject/count), `/api/alerts` (+ done/count), `/api/pii-rules`
(+ fields/test), `/api/gateways/{slug}/pii`, `/api/gateways/{slug}/servers/{id}/identity`,
`/api/me/credentials`, `/api/hosted`, `/api/reports`, `/api/stores/limit`,
`/api/stores/metrics`, `/api/teams/{id}/gateways/{slug}/{budgets,alert-rules}`,
`/api/users/{id}/gateways/{slug}/denies`, `/api/audit/{id}`.

### 4.4 Gateways (control + edges)

Gateways page → create `<slug>` (optional public URL) → control writes an
`McpGateway` CR → operator installs Helm release `gw-<slug>` (2 edge pods,
Ingress `<slug>.gw.<base>`, its own Let's Encrypt cert in prod). Attach servers
per gateway; each attachment is **shared** (server's credential) or
**personal** (each member's own, from My credentials; no fallback to shared).
`kubectl -n mercora get mgw` shows phase and pods ("2/2", shown as a pill).
The operator upgrades edges itself when its `edgeImageTag` drifts.

### 4.5 Teams, policies, budgets, approvals

- Members start with **no tools**. A member's tools on a gateway = union of their
  teams' allow-lists (one row per upstream tool, no wildcards, so new upstream
  tools stay hidden) minus per-member denies.
- **Write budgets** per team, gateway and tool, per rolling hour (items and an
  optional money cap for refunds); a member gets the strictest of their teams,
  default 20 items/hour on unbudgeted write tools. Read-only tools are not budgeted.
- Over budget, the call becomes an **approval request** (one open per
  member+tool+gateway); admins approve/reject on Alerts. Agents can use
  `gateway__request_approval` / `gateway__wait_for_approval`.
- Open risk the user accepted: a direct call that fits once budget frees can
  run, then the approved request runs too (double write).

### 4.6 Team alerts

Alert rules per team, gateway and tool (Teams page checkbox "alert <team>"):
a call to that tool files an alert for the team. Members see their teams'
alerts on Alerts; admins see only approvals. Agents read and close them with
`gateway__list_team_alerts` / `gateway__mark_team_alert_done`; the dashboard
also has Mark done. If "no alert arrived", check `team_alert_rules` first.

### 4.7 PII protection

One filter over every tool result, on/off per gateway. Rule types: regex
patterns, JSON keys masked at any depth, and single fields ticked per tool.
Defaults seeded once (`pii_seeded`); admins add/delete rules and test them.

### 4.8 Store provisioning and the operator

Stores tab → `POST /api/stores` (admin, name rules, per-admin cap:
`stores.maxPerAdmin` / operator `maxStores`, 5 in prod) → `Store` CR →
operator: namespace `store-<name>` (ResourceQuota, LimitRange, default-deny
NetworkPolicies, generated `woo-mcp-credentials`) → Helm `store` + `woo-mcp`
→ wait for WordPress, setup Job, woo-mcp → register server `<name>` → Ready.
15-minute timeout → Failed with the step. Deletes: gateway server first, then
namespace. Store pods use PriorityClass `store-workload` (-100) so platform
pods can always schedule. Steps are idempotent; stuck Helm releases are rolled
back or uninstalled and retried. Details: `docs/reference.md`.

**HostedServer** (`hmcp`): admins run an MCP server from a container image;
the operator installs it in `mcp-<name>` and registers it.

**Operator RBAC:** narrow; per-namespace RoleBinding to
`mercora-store-operator-workloads` plus ValidatingAdmissionPolicy
`mercora-store-operator-own-namespaces`. Control plane can only create
Store/McpGateway/HostedServer in `mercora`. Edges have no token.

**Operator → gateway auth (since 2026-10-24):** a projected ServiceAccount
token (audience `mcp-gateway`, 1 h, rotated by kubelet, read per call). The
gateway checks it with a TokenReview (60 s cache) and accepts only
`OPERATOR_SERVICE_ACCOUNT` = `system:serviceaccount:mercora:store-operator`,
and only on `/api/servers`; everything else 401. Not a `users` row. Secret
`store-operator-gateway` is no longer read. Code: `kube.py`, `api.py`
(`_operator`), `store_operator/gateway.py`; tests `test_operator_auth.py`,
`test_gateway_sync.py`. Shortcut: the TokenReview call is synchronous on the
event loop (fine at once per token per minute).

### 4.9 OAuth for Claude custom connectors

A gateway created with a public https URL gets `PUBLIC_URL` on its edges and
its `/mcp` also accepts OAuth tokens (OAuth 2.1 + PKCE + dynamic client
registration; `/register`, `/authorize`, `/token`, `/revoke`, `/.well-known/*`,
login page `/oauth/login`). Tokens belong to the signed-in user (their policy,
rate limit, audit), stored hashed; access 1 h, refresh 30 d single-use and
rotated. Redirects limited to Claude's callbacks and loopback. Revoke:
API keys → Connected apps → Disconnect. In prod each gateway has a public URL
by default (`https://<slug>.gw.<base>`), so no ngrok.

### 4.10 Dashboard (`services/dashboard`)

React 19 + Vite, nginx serves it and proxies `/api` (180 s read timeout for
chat). Pages: Servers, Gateways, Stores (admin), Tools, Chat, Alerts, Reports
(admin), API keys (+ Connected apps), My credentials, Members (admin), Teams,
PII Protection (admin), Audit log (admin, expandable result rows, Server
column). Confirm dialogs use `src/confirm.jsx` `ask()`, not `window.confirm`.
`PageBoundary` in `App.jsx` keeps one page's crash from blanking the app.

### 4.11 Reports and live metrics

**Reports** (admin): per-store report tools as tables, plus (since d177923)
a **WordPress metrics** section: one card per Ready store with live CPU
(millicores) and memory (MiB) from metrics-server, polled from
`GET /api/stores/metrics` every 5 s, last 5 minutes kept in the browser only.
The separate "WordPress metrics" tab and the Apache busy-workers graph were
removed; the backend still returns `busy_workers` and the store chart still
runs the exporter sidecar (unused by the UI now).

### 4.12 Chat agent (`chat.py`, `Chat.jsx`)

The browser sends the whole conversation each time (nothing stored). The agent
gets the chosen gateway's tools as functions and an account summary (user,
role, teams, tools, write flags, limits, stores), at most 6 rounds, all calls
through `call_tool`. Guardrails: a guard model (`LLM_GUARD_MODEL`, runs in
parallel with the first agent call) hard-refuses only `injection`; off-topic is
refused by the main model's own rules. Arguments are schema-checked. Write
tools never run from the model: the reply carries a `pending` call with an HMAC
token; only Confirm runs it, once (Cancel spends it). Results are wrapped in
`<tool_result>` as data. Models via OpenRouter (`OPENROUTER_API_KEY` in
`gateway-admin`; `LLM_MODEL` default `openai/gpt-4.1-mini`). Used confirm tokens
are in process memory: needs a table before the control plane runs >1 replica.

### 4.13 Network and pod hardening

- `mercora` is default-deny (chart `namespaceDefaultDeny`); every release has
  ingress + egress policies; `acme-solver-ingress` lets cert-manager solvers in.
- Store namespaces: woo-mcp reachable only from the gateway, MariaDB only from
  WordPress, egress blocks pod/service CIDRs and cloud metadata (CIDRs are k3s
  defaults in `store_operator/manifests.py`).
- Every pod: non-root, read-only root fs, all capabilities dropped, seccomp,
  readiness + liveness probes, requests + limits.

---

## 5. Environments and what is running

### 5.1 VPS (production-like)

Ubuntu LXC container, 2 vCPU / 8 GB, k3s. Public over **IPv6 only**; base
domain `2001-41d0-303-2774--94.sslip.io` (dashboard
`https://dash.<base>`, also `https://dash.tailff0a64.ts.net` via Tailscale
Funnel for IPv4). TLS: cert-manager + Let's Encrypt HTTP-01, one cert for
dash/gateway, one per store/edge Ingress, HTTP → HTTPS redirect
(`deploy/tls-prod.yaml`). LXC quirks (kmsg, legacy iptables, socat IPv6
forwarders, coredns mapping) are in `docs/vps-setup-log.md`; inbound IPv6
drops some new connects, so retry before diagnosing an outage.

**Deploys are GitOps (since 2026-10-24):** merge to `main` → CI
(`.github/workflows/ci.yaml`) picks changed services by path, runs tests and
lint, builds, smoke-tests each image (gateway must pass `/readyz`), pushes
`ghcr.io/shiv2576/<svc>:<sha>`, commits the new tag into `deploy/*-prod.yaml`
(`deploy: … at <sha>` commits) → Argo CD v3.5.4 root app `platform` syncs
`team`, `team-ui`, `store-operator`. `db` and operator-made `gw-*` / `store-*`
releases are not Argo apps; the operator moves its own releases to new tags.
Do **not** run `scripts/deploy-vps.sh` any more.

Current prod tags: gateway + store-operator + edges `20f717c`, dashboard
`d177923`, woo-mcp `30c026d`. Last recorded objects (2026-10-23): edge
`gw-realgw`, store `store-realstore`, servers `exa`, `mslearn`, the store's.

**Secrets encrypted at rest (2026-10-25):** k3s `secrets-encryption`, provider
secretbox (XSalsa20-Poly1305), key `secretboxkey-2026-10-25T09:11:42Z`, 94/94
Secrets verified in `state.db`. Rotate with `k3s secrets-encrypt rotate-keys`.
Node config, not managed by Argo CD. A key copy is kept off the box by the user.

### 5.2 Local k3d `store-check` (port 80, `*.localhost`)

Same charts with `deploy/*-values-local.yaml`; images built locally and
`k3d image import`ed (chart default tags: gateway 0.10.5, dashboard 0.9.9,
store-operator 0.7.7, woo-mcp 0.5.0). Upstreams: a store (`shop1`), `exa`,
`mslearn`. Dashboard image tags get reused, so after an import run
`kubectl -n mercora rollout restart deploy/team-ui-dashboard`. Helm never
upgrades `crds/`: apply new CRDs by hand.

### 5.3 Secrets (namespace `mercora`, made by hand, never in git)

| Secret | Keys | Used by |
|---|---|---|
| `gateway-db` | `POSTGRES_PASSWORD`, `APP_PASSWORD`, `DATABASE_URL` | Postgres, every gateway pod |
| `gateway-admin` | `SETUP_TOKEN`, optional `OPENROUTER_API_KEY` | first-run setup, chat (patch, never recreate) |
| `gateway-credentials` | `CREDENTIALS_KEY` (Fernet) | credential encryption; gateway won't start without it |

Per store: `store-<name>/woo-mcp-credentials` (`WOO_CONSUMER_KEY`,
`WOO_CONSUMER_SECRET`, `MCP_AUTH_TOKEN`), generated once by the operator.
`store-operator-gateway` is obsolete (4.8).

---

## 6. Verification

### Automated tests

| Suite | Command | Tests |
|---|---|---|
| gateway | `docker run -d --name gw-test-pg -e POSTGRES_PASSWORD=t -p 5433:5432 postgres:17-alpine`, then `cd services/gateway && TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q` | 87 |
| woo-mcp | `cd services/woo-mcp && uv run pytest -q tests/tests_woo_mcp.py` (plain `pytest` collects nothing) | 23 |
| store-operator | `cd services/store-operator && uv run pytest -q` | 25 |
| dashboard | `cd services/dashboard && npx vite build` | builds |

CI runs the same on every push to `main` for the changed services.
Gateway suites: alerts, approvals, db, edge, gateway, gateways, hosted,
identity, oauth, operator_auth, pii, pii_gateway, reports, setup, ssrf, stores,
teams. Operator suites: manifests, gateway sync, gateway pods, image drift.
Earlier mutation checks (removing the call timeout, disabling the access
filter) made the tests fail, so those paths are really covered.

### Live checks (highlights)

- Slow/dead upstream: marked unreachable, listing stays instant, other servers keep working.
- Stores: two created in parallel, both Ready; cross-store traffic refused by NetworkPolicy;
  operator killed mid-provisioning still converged; deletes clean up namespace, PVs, server.
- Claude custom connector over OAuth signed in as a member; calls audited under that member.
- Operator JWT (VPS, 2026-10-24): TokenReview with audience `mcp-gateway` authenticated;
  same token for the API server's audience rejected; operator `GET /api/servers` → 200.
- Secrets encryption (VPS, 2026-10-25): all Secrets encrypted, pods Running, `/readyz` 200.

---

## 7. Problems found and how they were resolved

| Problem | Cause | Resolution |
|---|---|---|
| woo tools `401 Consumer key is invalid` | Secret held a key the store no longer had (setup Job re-run rotates it) | sync `woo-mcp-credentials`, restart woo-mcp |
| Operator `ModuleNotFoundError`, `No username set` | kopf misses /srv on sys.path; uid has no passwd entry | `PYTHONPATH=/srv`, `USER=store-operator` in the image |
| Store deletes hung | node full, restarted operator Pending | PriorityClass `store-workload` on store pods |
| Old store PVC stuck `Terminating` | Helm hook Job survives uninstall | operator sets `setup.hook: false` |
| claude.ai connector impossible with API keys | Anthropic's servers need OAuth + public HTTPS | gateway became its own OAuth server |
| Black screen on tab switch | Chat effect returned `scrollIntoView`'s Promise as cleanup | fixed + `PageBoundary` |
| Guard model refused "how many tools do I have?" | small model too strict | guard hard-refuses only injection |
| `get_inventory_health` timed out | 10 s call timeout too short on a real store | `callTimeoutSeconds` 30 |
| Stores unreliable over IPv6 / Tailscale names slow | provider IPv6 drops; new ts.net names take 5–10 min | stores on sslip.io; dashboard on both |
| New stores Failed "Unrecognized images" | GHCR change put the woo-mcp image into the Bitnami wordpress block | fixed in `manifests.py` + test (f0bf043) |
| Busy-workers graph confusing | — | removed from UI; metrics moved into Reports |

---

## 8. Known limits (deliberate shortcuts)

- Postgres is one replica, no automated backups (manual: `docs/upgrades.md`):
  the single shared failure point. Next: HA Postgres (CloudNativePG) + scheduled backups.
- Single node; `local-path` storage. Store pods are evicted first when full.
- Each edge has its own 30 s tool cache; no server push (tools-changed).
- Chat confirm tokens in process memory (control plane must stay 1 replica);
  chat history only in the browser; replies not streamed.
- Approval double-write risk (4.5).
- Shared credential: upstream sees one identity; only the gateway audit says who.
  Personal identity fixes this per server.
- Tool descriptions from upstreams reach the model as written (only a `[server]` prefix).
- Login limit per username, in memory (resets on restart; can lock a user out 15 min).
- OAuth scopes not used: a token = the user's full access.
- No change-password.
- Store egress CIDRs are k3s defaults; change them on another cluster.
- TokenReview is synchronous on the event loop.
- The Secrets are hand-made per cluster; no Sealed/External Secrets yet.

---

## 9. Next steps

1. User: delete Secret `store-operator-gateway` on the VPS and revoke the old
   "store-operator" API key (no longer used).
2. Root README local/VPS setup still tells you to create `store-operator-gateway`
   with an API key and lists 0.10.4/0.9.8/0.7.5 tags; update to the JWT flow and current tags.
3. Drop the unused apache-exporter sidecar and `busy_workers` from the backend,
   or show it again.
4. `delete_product` still returns the product as it was before trashing (status `publish`);
   one-line fix in `services/woo-mcp/app/server.py` to report `trash`.
5. HA Postgres + backups; move chat confirm tokens into Postgres.
6. Change-password; persistent login limit.
7. External secrets store (Sealed Secrets / External Secrets); own domain +
   Cloudflare Tunnel for instant IPv4 (declined for now).
