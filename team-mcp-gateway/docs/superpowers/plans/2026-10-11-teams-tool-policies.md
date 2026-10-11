# Gateways part 2 (teams and tool-level policies) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-member server grants with teams and an allow-list of tools per team and gateway, enforced the same way on edges and in the dashboard.

**Architecture:** Three tables (`teams`, `team_members`, `team_tools`) and one function `db.allowed_tools(conn, user, gateway_slug)`. `Upstreams.list_tools/call_tool` take the gateway per call and filter through it; edges pass their slug, the dashboard endpoints pass the gateway the user picked. Admins manage teams through `/api/teams` (new `app/teams.py`) and a Teams page.

**Tech Stack:** Python 3.12+, Starlette, MCP SDK 2.x, psycopg 3, React 19 + Vite.

**Spec:** `docs/superpowers/specs/2026-10-11-teams-tool-policies-design.md`

## Global Constraints

- **No git commits.** The user makes every commit.
- Allow-list only; a policy row names one upstream tool (`team_tools.tool`, 1-128 chars) on one server on one gateway. No wildcard.
- Team name 1-40 characters, unique.
- Admin: every tool on servers attached to the gateway. Member: union of their teams' rows for that gateway whose server is still attached. No team: nothing.
- Hidden tool call: `Unknown tool '<name>'. Tools are named <server>__<tool>.`, audited `ok=0` with the gateway, never forwarded.
- Dashboard `GET /api/tools?gateway=`, `POST /api/tools/call {gateway}`, `POST /api/chat {gateway}`: missing 400, unknown 404.
- `user_servers` dropped (`DROP TABLE IF EXISTS`), with `PUT /api/users/{id}/servers` and `server_ids`.
- Image tags for this work: `gateway:0.3.0`, `dashboard:0.3.0`; operator `edgeImageTag: "0.3.0"`.

## Review Focus

1. **A policy row outlives its attachment**: server detached then re-attached. Expected: while detached nothing; re-attached, the old rows apply again (they were never deleted). Pinned in Task 1 `test_allowed_tools`.
2. **A tool the upstream does not offer** (stale row): grants nothing, listing does not invent it. Pinned in Task 2 `test_edge_enforces_team_policies` (row for `notes:gone`).
3. **A member in two teams with overlapping tools**: no duplicates in the list. Pinned in Task 1 (`DISTINCT`) and Task 2.
4. **Dashboard call without a gateway**: 400, nothing called or audited. Pinned in Task 2 `test_dashboard_needs_a_gateway`.
5. **Policy PUT naming a server attached to another gateway only**: 400. Pinned in Task 1 `test_teams_api`.

Ruling carried from the spec: `allowed_tools` does not filter on `enabled`; `Upstreams` already hides disabled servers' tools and answers a call with "Server 'x' is disabled." (existing behavior and test), which the spec asks to keep.

---

### Task 1: Schema, `allowed_tools`, teams API

**Files:**
- Modify: `services/gateway/app/db.py`, `services/gateway/app/main.py`
- Create: `services/gateway/app/teams.py`, `services/gateway/tests/test_teams.py`
- Modify: `services/gateway/tests/test_gateway.py` (helper `add_team`)

**Interfaces:**
- Produces: `db.allowed_tools(conn, user: dict, gateway_slug: str) -> dict[int, set[str] | None]`; routes `GET/POST /api/teams`, `DELETE /api/teams/{id}`, `PUT /api/teams/{id}/members {user_ids}`, `PUT /api/teams/{id}/gateways/{slug}/tools {tools: [{server_id, tool}]}`; test helper `async add_team(admin, name, user_ids, gateway, tools: list[tuple[int, str]]) -> int`.

- [ ] **Step 1: Failing tests** — `tests/test_teams.py` with `test_allowed_tools` (SQL fixtures: two gateways, two servers, two teams, a member in both, an admin; asserts admin `{sid: None}` for attached only, member union, no-team member `{}`, detached server drops out and returns when re-attached, unknown gateway `{}`) and `test_teams_api` (member 403 on every write; name rules 400; duplicate 409; members PUT with unknown user 400; tools PUT for unattached server 400, unknown gateway 404, bad tool 400; member GET sees only own teams; deleting the team removes rows).
- [ ] **Step 2: Run** `uv run pytest tests/test_teams.py -q` → FAIL (no `allowed_tools`, `/api/teams` 404).
- [ ] **Step 3: Schema** — append to `SCHEMA` the three tables of spec §1; `DROP TABLE IF EXISTS user_servers` comes in Task 2.
- [ ] **Step 4: `db.allowed_tools`**

```python
def allowed_tools(conn, user, gateway_slug: str) -> dict[int, set[str] | None]:
    """Server id -> allowed upstream tool names on this gateway; None = every tool.
    Only servers attached to the gateway appear. Read on every request."""
    if user["role"] == "admin":
        rows = conn.execute(
            "SELECT gs.server_id FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id WHERE g.slug = ?",
            (gateway_slug,),
        )
        return {r["server_id"]: None for r in rows}
    out: dict[int, set[str] | None] = {}
    for r in conn.execute(
        "SELECT DISTINCT tt.server_id, tt.tool FROM team_members tm"
        " JOIN team_tools tt ON tt.team_id = tm.team_id"
        " JOIN gateways g ON g.id = tt.gateway_id"
        " JOIN gateway_servers gs ON gs.gateway_id = tt.gateway_id AND gs.server_id = tt.server_id"
        " WHERE tm.user_id = ? AND g.slug = ?",
        (user["id"], gateway_slug),
    ):
        out.setdefault(r["server_id"], set()).add(r["tool"])
    return out
```

- [ ] **Step 5: `app/teams.py`** — handlers as in spec §3 using `api.handler`, `require_admin`, `current_user`, `body`, `_str`; views built from three queries (teams, members, tools joined to gateways/servers). Wire `*teams.routes` into the control app in `main.py`.
- [ ] **Step 6: Helper `add_team`** in `tests/test_gateway.py`.
- [ ] **Step 7: Run** `uv run pytest -q` → all pass.

### Task 2: Enforce policies everywhere; drop grants

**Files:**
- Modify: `app/upstream.py`, `app/main.py`, `app/api.py`, `app/chat.py`, `app/db.py`
- Modify: `tests/test_gateway.py`, `tests/test_edge.py`, `tests/test_oauth.py`, `tests/test_teams.py`

**Interfaces:**
- Consumes: `db.allowed_tools`, `add_team` (Task 1).
- Produces: `Upstreams.list_tools(user, gateway_slug) -> list[Tool]`, `Upstreams.call_tool(user, gateway_slug, name, arguments)`; `Upstreams(gateway_slug=...)` now only limits the health loop. Dashboard endpoints take `gateway`. `GET /api/users` rows: `id, username, role, teams: [name]`.

- [ ] **Step 1: Failing tests** in `tests/test_teams.py`: `test_edge_enforces_team_policies` (edge `main`, notes + fx attached; bob in team A with `fx: list`, carol in team B with `notes: list, save_note` and a stale `notes: gone`; bob lists only `fx__list`, calls `fx__convert` → Unknown tool, audited `ok=0` gateway `main`, notes upstream never sees it; adding `fx: convert` to A works on the next call; detaching fx empties bob's list; carol's list has no `notes__gone`); `test_dashboard_needs_a_gateway` (400 without, 404 unknown, and member's `/api/tools?gateway=main` equals the edge's list); `/api/servers` for a member lists only servers with a policy.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: `Upstreams`** — `list_tools(self, user, gateway_slug)` filters rows by `allowed_tools` (server key present; tool in set or set is None); `call_tool(self, user, gateway_slug, name, arguments)` refuses when the server is not a key or the tool not in its set; audit `gateway` = `gateway_slug`. `_attached()` stays for `refresh_all` only. `main.py` edge handlers pass `settings.gateway_slug`.
- [ ] **Step 4: API** — `_gateway_param(request, slug)` (400/404); `list_tools`/`call_tool` and `chat` pass it; `list_servers` member filter via `team_tools` joined to `gateway_servers`; `list_users` adds `teams`; `create_user` drops `server_ids`; `set_user_servers` route deleted; `db.granted_server_ids` and `db.set_grants` deleted; `DROP TABLE IF EXISTS user_servers` appended to `SCHEMA`.
- [ ] **Step 5: Move existing tests** off `server_ids` onto `add_team` (whole gateway, chat, edges, OAuth); replace `test_members_see_only_granted_servers` with the teams version; rate-limit tests pass a gateway.
- [ ] **Step 6: Run** `uv run pytest -q` → all pass.

### Task 3: Dashboard

**Files:**
- Create: `services/dashboard/src/Teams.jsx`
- Modify: `src/App.jsx`, `src/Admin.jsx` (Members: Access out, Teams column in), `src/Tools.jsx`, `src/Chat.jsx`, `src/styles.css`

- [ ] **Step 1:** Teams page per spec §4 (admin edits; member read-only view of own teams and tools).
- [ ] **Step 2:** Shared `GatewayPicker` (in `Teams.jsx`, exported) used by Tools and Chat; choice kept in `localStorage` key `gateway`; requests carry it.
- [ ] **Step 3:** `npm run build` → succeeds.

### Task 4: Ship to the cluster

- [ ] Bump `charts/gateway` values tag and `charts/dashboard` tag to `0.3.0`, `deploy/store-operator-values-local.yaml` and chart `edgeImageTag` to `0.3.0`.
- [ ] Build and import `gateway:0.3.0`, `dashboard:0.3.0`; `helm upgrade` `team`, `team-ui`, `store-operator`; existing `gw-*` edges (if any) re-installed with `helm upgrade --reuse-values --set image.tag=0.3.0`.
- [ ] Check: `user_servers` gone, `teams` present; `/api/teams` answers; dashboard serves the new bundle.
