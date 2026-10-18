# System design and tradeoffs

## Architecture choices

```
                    MCP clients (Claude, Claude Code, Inspector)        browser
                         │ Bearer API key or OAuth token                  │ HttpOnly session cookie
                         ▼                                                ▼
  mercora   ┌── gw-<slug> edge ×2 ──┐   ┌── team (control plane) ──┐   team-ui (nginx, /api → team)
  namespace │ /mcp for one gateway's │   │ /api, setup, chat agent, │
            │ attached servers       │   │ approvals, health loop   │
            └───────────┬────────────┘   └────────────┬─────────────┘
                        └───── one Postgres (db) ─────┘        store-operator (kopf)
                        │ per-server credential, added by the gateway │ watches Store, McpGateway,
                        ▼                                             │ HostedServer resources
  store-<name>   woo-mcp ── OAuth1-signed REST ──► WordPress + MariaDB ◄┘ helm install per store
  mcp-<name>     hosted MCP server (image chosen by an admin)
  internet       remote MCP servers (Exa, Microsoft Learn, ...)
```

- **One gateway image, two modes.** The control plane serves the dashboard API,
  the chat agent and approvals. Each *gateway* an admin creates is a separate
  edge release (`gw-<slug>`, two replicas) serving `/mcp` for only the servers
  attached to it. A misbehaving client on one gateway cannot starve another,
  and each can have its own public URL and OAuth.
- **Postgres is the only shared state.** Edges are stateless MCP
  (`stateless_http`), so any replica answers any request, and policies, keys and
  enabled flags are read per request: a revoke or a policy change applies on the
  next call with no cache to invalidate.
- **An operator, not the API, touches Kubernetes.** The gateway only writes
  `Store` / `McpGateway` / `HostedServer` resources (RBAC: those resources in
  `mercora`). The kopf operator holds the wider rights and does the Helm
  installs, so the internet-facing pod cannot create namespaces or read Secrets.
- **One namespace per store**, with default-deny NetworkPolicies, a
  ResourceQuota and a LimitRange. The store's woo-mcp accepts traffic only from
  gateway pods and can reach only its own WordPress.
- **woo-mcp uses the WooCommerce REST API**, not WP-CLI: it needs a network path
  and a revocable key, not a shell in the WordPress pod. In-cluster HTTP forces
  OAuth 1.0a signing, which also keeps the secret off the wire.
- **Helm for everything**, the same charts locally and on the VPS; only values
  files differ (`deploy/*-local.yaml`, `deploy/*-prod.yaml`).

Tradeoffs taken on purpose: one Postgres replica (simple, but a single point
of failure); a per-user fixed one-minute rate-limit window (simple, allows a
burst at the boundary); stores are upgraded by hand, one at a time, so a bad
chart cannot roll out to every store at once ([upgrades.md](upgrades.md)).

## Tool aggregation

- A background loop asks every enabled, attached server for its tools every
  30 s, all at once, each under a 5 s timeout, and keeps the list in memory with
  a status (`ok` / `unreachable` + reason). `tools/list` only reads this copy,
  so it never waits on an upstream.
- Every tool is exposed as `<server>__<tool>` (`shop1__list_orders`,
  `notes__list`, `fx__list`): always prefixed, never only on a clash, so a name
  never changes when another server is added. Server names cannot contain `_`,
  so the first `__` always ends the server name. The description is prefixed
  with `[store <name>]` or `[<server>]` so a model can tell identical store
  tools apart.
- The list a caller gets is filtered: servers attached to that gateway, the
  union of the caller's teams' allow-lists (one row per tool, no wildcards, so
  a tool an upstream adds later stays hidden), minus per-member denies.

## Routing

`tools/call shop1__list_orders` → split at the first `__` → server `shop1` →
check it is enabled and attached to this gateway → check the caller's policy
(a hidden tool answers "Unknown tool", audited) → rate limit (20 calls/min per
user) → write budget for write tools (over it: refused and an approval request
filed) → add that server's credential (shared, or the caller's own for
personal identity) → call with a 10 s timeout → PII filter if the gateway has
it on → audit row (user, tool, server, gateway, result, duration) → team
alert rules. Every entry point, `/mcp`, the Tools page, the chat agent and
approved runs, goes through this one function (`Upstreams.call_tool`).

## Failure handling

- **Dead or slow upstream:** marked `unreachable` with the reason on the
  Servers page; its tools drop out of `tools/list` until the next good check.
  A call that fails or times out returns a normal MCP tool error with one plain
  sentence (`Upstream 'fx' did not answer within 10s.`); other servers are not
  affected.
- **Bad credential key:** if `CREDENTIALS_KEY` changes, stored credentials fail
  to decrypt and the server shows "an admin must enter it again" instead of
  sending garbage.
- **PII filter error or timeout:** the result is withheld, never sent unmasked.
- **Pods:** readiness and liveness probes on every Deployment; rolling updates
  with `helm upgrade --wait --atomic`, which rolls back on its own if new pods
  never get ready. Two edge replicas per gateway.
- **Store provisioning:** every step is idempotent; failed steps retry three
  times; a store not Ready in 15 minutes goes `Failed` with the step it waited
  on. If the operator restarts mid-way, kopf re-runs the unfinished handler.

## What changes for production

| | Local (k3d) | Production (k3s on a VPS, [deploy-vps.md](deploy-vps.md)) |
|---|---|---|
| DNS | `*.localhost` resolves to 127.0.0.1 | `*.<ip>.sslip.io`, or a real domain with a wildcard record |
| Ingress | Traefik, HTTP on port 80 | Traefik with cert-manager: Let's Encrypt per host over HTTP-01, HTTP redirects to HTTPS, `cookieSecure: true` |
| Storage class | `local-path` (hostPath on the node) | `local-path` on the single node, sized in values (Postgres 5 Gi). A multi-node cluster needs a replicated class (Longhorn, a cloud disk) and backups |
| Secrets | Kubernetes Secrets made by hand per cluster | Same, with fresh random values per environment; never in git, charts or images. Next steps: encryption at rest (`--secrets-encryption`) and an external store (Sealed Secrets or External Secrets) |
| Images | `k3d image import` | built for amd64, imported into k3s containerd (`scripts/deploy-vps.sh`); a registry in a real setup |
| Values | `deploy/*-values-local.yaml` | `deploy/*-values-prod.yaml` (domain, TLS, store limit) |

Still single-node: Postgres has one replica and no automated backups
([upgrades.md](upgrades.md) has the manual backup commands). A real production
setup needs an HA Postgres, scheduled backups, a registry and more than one node.
