# Roadmap: three days to a passing demo

Each phase ends with a check taken from the assignment's definition of done.
Do not start a phase until the previous check passes. Helm chart, probes,
limits and Secrets are written with each service, never "at the end".

## Day 1: store and the WooCommerce MCP server

### Phase 0. Clean start (1 hour)

- New repo from this scaffold. Keep Alice and the Mercora plugin on a branch of
  the old repo; they are not part of the submission.
- Make an API key by hand in wp-admin (WooCommerce > Settings > Advanced > REST
  API, read/write) and run `python -m app.check` against your current store.
- **Check:** it prints `OK: read access works`. This proves the HTTP signing
  against real WooCommerce, which the tests here could only prove against a fake.

### Phase 1. Store chart (3 to 4 hours)

- `charts/store`: a wrapper chart with the Bitnami WordPress chart as a
  dependency. Persistence on for WordPress and MariaDB. Ingress host from values.
- Remove the hostPath plugin mount and every `127.0.0.1` / `host.k3d.internal`.
- Turn `infra/setup-woo.sh` into a Helm post-install Job: install WooCommerce,
  set permalinks, enable cash on delivery, seed a few products, and run
  `scripts/register-woo-key.php` with the key taken from a Secret.
- Local domains: `store.localhost` and `dash.localhost` through the Ingress.
- **Check:** on a fresh cluster, `helm install` then: add to cart, check out with
  cash on delivery, see the order in wp-admin. Delete the WordPress pod; the
  order is still there.

### Phase 2. WooCommerce MCP server in the cluster (1 to 2 hours)

- Already written and tested in `services/woo-mcp`. Build, import into k3d,
  install `charts/woo-mcp`.
- **Check:** MCP Inspector over `kubectl port-forward` with the token: call
  `create_product`, see it on the storefront; place an order by hand, call
  `list_orders` with `on_date=today`. From a throwaway pod without the gateway
  label, `curl` to the Service times out (NetworkPolicy works).

## Day 2: the gateway

### Phase 3. Gateway core, no UI (5 to 6 hours)

- `services/gateway`: FastAPI, Postgres, the MCP SDK.
- Tables: `users` (role), `api_keys` (hash, revoked_at), `servers` (url,
  encrypted credential, enabled), `audit_log`.
- Upstream manager: one background loop per server that connects, lists tools
  and stores them with a status. Tool listing reads this cache, so a dead
  server can never slow it down.
- `/mcp`: lists every enabled server's tools as `server__tool`; routes a call by
  the prefix; per-call timeout; clear error text.
- One function, `call_tool(user, name, args)`, does the permission check, the
  upstream call and the audit row. The MCP endpoint and (later) the chat both
  use it. Key and enabled flag are read from the database on every request.
- **Check:** Inspector with only the gateway URL and user B's key lists the
  Woo tools and answers the orders question. Revoke the key: next call is 401.
  Every call has an audit row with the right user.

### Phase 4. Two more servers and failure isolation (2 to 3 hours)

- Two tiny servers you write: `notes` (save and list notes) and `fx` (convert
  INR to another currency from a fixed table). Give both a tool called `list`
  to demonstrate the name-collision scheme.
- **Check:** kill the `fx` pod: Woo and notes still work, status shows
  Unreachable within one health interval. Disable `notes`: its tools vanish.
  One prompt uses two servers ("total of today's orders in USD").

## Day 3: dashboard, chat, production

### Phase 5. Dashboard (4 to 5 hours)

- React + Vite, served by nginx in its own pod, behind `dash.localhost`.
- Pages: login, servers (status, tools, add/edit/disable/remove), members and
  keys (key shown once), audit log.
- **Check:** add a server in the UI; its tools appear in Inspector without
  touching the client. The network tab never shows an upstream credential.

### Phase 6. Chat agent (2 to 3 hours)

- Backend loop: question + tool list to the LLM; run each requested tool
  through `call_tool` as the logged-in user; feed results back; stop when the
  model answers. The UI lists the tools it called.
- **Check:** as user A, "create a product called X for 499" then "what orders
  came in today?". Both appear in the audit log under user A.

### Phase 7. VPS, docs, recording (3 hours)

- k3s on a VPS with `values-prod.yaml`; push images to a registry.
- README: local domains, how the MCP server talks to the store and why, the
  collision scheme, what changed between local and prod values.
- Record the full definition-of-done script with two users.

## If time runs short, cut in this order

1. Dashboard polish (plain tables are fine).
2. Edit-server form (add, disable and remove cover the demo).
3. TLS on the VPS.
   Never cut: audit attribution, revocation, failure isolation, Helm values split.

## After the core: stand-out items, cheapest first

1. **Tool permissions by role.** One filter in `call_tool` and in tool listing:
   members read, admins write. About two hours.
2. **Security hardening.** Deny-by-default NetworkPolicies, non-root (done for
   woo-mcp), encrypted upstream credentials, SSRF check on added URLs.
3. **VPS with TLS** via cert-manager.
4. Multi-store bonus: only with a full spare day.
