# Write budgets with explicit admin approval

Four bulk tools on woo-mcp, a rolling-hour **write budget** per member and tool
in the gateway, and an **explicit** approval path for jobs bigger than the
budget: the agent either waits for budget to free up, or calls
`gateway__request_approval` and an admin approves the whole job from the
dashboard's Alerts page. Everything is decided by plain code; no LLM is involved
in permissions, budgets or approvals.

---

## 0. Agreed requirements

From the conversation:

- **Four bulk tools.** Marketing: bulk update product descriptions, bulk update
  product prices. Support: bulk refund orders, bulk update orders (status and
  shipping address).
- **General rate limit: 20 calls per minute per user**, all tools, read and
  write (already changed in `main.py` / `upstream.py`).
  `gateway__wait_for_approval` does not count against it.
- **Rolling-hour write budget**, counted in items (business impact), not calls,
  per user, gateway and tool. Batching (19 + 19) and looping single-item tools
  cannot get around it.
- **Over budget never runs and never creates an approval by itself.** The
  refusal tells the agent how much budget is left and when it frees up, and that
  it can call `gateway__request_approval`.
- **Explicit approval.** `gateway__request_approval` stores the exact operation;
  it appears under Alerts → Pending; the agent waits with
  `gateway__wait_for_approval`. Execution on approval does not depend on the
  agent still polling.
- **Approved jobs do not use the hourly budget.**
- **Rejection lock.** A rejection blocks new approval _requests_ for that user,
  gateway and tool for one hour. The normal budget keeps working during the lock.
- **No LLM** anywhere in this feature.

Decided in this spec (not stated by the user):

- Budgets are set per **team, gateway and tool** on the Teams page, next to the
  team's tool policy. A tool with no budget for any of the caller's teams is not
  budgeted (tool policy still applies).
- A member in several teams with a budget on the same tool gets the **smallest**
  budget (the strictest wins).
- Admins are not budgeted, like they are not limited by tool policies.
- Refunds can also have a money budget (`max_refund_total`, in the store's
  currency); a refund call must fit both budgets.
- `gateway__request_approval` is refused when the operation fits the remaining
  budget ("run it directly"): approvals are for exceptions only.
- Approved runs get a longer timeout than normal calls (bulk jobs of 100 items
  take longer than 10 s).

## 1. Rules

| Situation                                                                              | Behavior                                                                                                                                                                                                    |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tool has no budget for any of the caller's teams, or caller is an admin                | Runs as today                                                                                                                                                                                               |
| Used in the last 60 min + this call ≤ budget                                           | Runs; the items count against the budget if it succeeds                                                                                                                                                     |
| Used + this call > budget                                                              | Refused, nothing created: "Budget: 3 of 20 items left for bulk_update_product_descriptions; 19 free up at 14:32. Wait and retry, or call gateway__request_approval with {tool, arguments} to ask an admin." |
| Single-item write tool with a budget                                                   | Each successful call counts as 1 item                                                                                                                                                                       |
| Refund call over the money budget, under the item budget                               | Refused like any over-budget call                                                                                                                                                                           |
| Failed or refused calls                                                                | Use no budget                                                                                                                                                                                               |
| `request_approval` for an operation that fits the remaining budget                     | Refused: "This fits your budget; call the tool directly."                                                                                                                                                   |
| `request_approval` while one is pending or running for the same user, gateway and tool | Refused, pointing at that id                                                                                                                                                                                |
| `request_approval` within 1 h of a rejection for the same user, gateway and tool       | Refused: "Approval #42 was rejected; new requests for this tool are locked until 14:32. Your hourly budget still works."                                                                                    |
| Admin approves                                                                         | Runs immediately with the frozen arguments as the requester; uses no budget                                                                                                                                 |
| Admin rejects                                                                          | Agent's next wait gets "rejected" with the note; 1 h request lock starts                                                                                                                                    |
| 24 h with no decision                                                                  | Expired; no lock                                                                                                                                                                                            |
| Admin tries to approve their own request                                               | Refused (403)                                                                                                                                                                                               |
| A bulk call with more than 100 items                                                   | Refused by woo-mcp                                                                                                                                                                                          |

### Rolling window

The budget looks back exactly 60 minutes from the moment of the call. Each
successful call's items free up 60 minutes after that call, one call at a time:

```
10:00  19 items  → used 19
10:30   1 item   → used 20 (allowed: 20 ≤ 20)
10:40   1 item   → refused, 0 left; next free-up 11:00 (19 items)
11:00            → the 19 from 10:00 drop out; 19 left
11:30            → the 1 from 10:30 drops out; 20 left
```

The refusal's "free up" time is the earliest moment enough items have dropped
out for _this_ call to fit (walking the window's rows oldest first), or, if the
call is bigger than the whole budget, the time the oldest row drops out with a
note that it can never fit without approval.

## 2. The bulk tools (services/woo-mcp)

Every bulk tool takes one list argument named `items` (1 to 100 entries) and
returns one result per item: `{id, ok, error?}`. One bad item never fails the
batch.

| Tool                               | `items` entry                                                                         | WooCommerce call                                       |
| ---------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `bulk_update_product_descriptions` | `{product_id, description?, short_description?}` (at least one)                       | `POST /products/batch`                                 |
| `bulk_update_product_prices`       | `{product_id, regular_price?, sale_price?}` (at least one; decimal strings)           | `POST /products/batch`                                 |
| `bulk_update_orders`               | `{order_id, status?, shipping?}` (at least one; `shipping` uses Woo's address fields) | `POST /orders/batch`                                   |
| `bulk_refund_orders`               | `{order_id, amount, reason?}`                                                         | `POST /orders/{id}/refunds`, one per item, 5 at a time |

- `bulk_refund_orders` requires an explicit `amount` so the gateway can sum the
  money without reading orders. woo-mcp checks each amount against the order's
  remaining refundable total and fails that item if it is more.
- Refunds use `api_refund: false`: the store is cash on delivery, so WooCommerce
  records the refund; no money moves.

## 3. Gateway (services/gateway)

### Tables (db.py)

```sql
-- A team's write budget for one tool on one gateway. Lives and dies with the attachment.
CREATE TABLE IF NOT EXISTS team_budgets (
    team_id          BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    gateway_id       BIGINT NOT NULL,
    server_id        BIGINT NOT NULL,
    tool             TEXT NOT NULL,
    max_items        INTEGER CHECK (max_items > 0),                -- per rolling hour
    max_refund_total NUMERIC(12, 2) CHECK (max_refund_total > 0),  -- per rolling hour, refunds only
    PRIMARY KEY (team_id, gateway_id, server_id, tool),
    FOREIGN KEY (gateway_id, server_id) REFERENCES gateway_servers (gateway_id, server_id) ON DELETE CASCADE,
    CHECK (max_items IS NOT NULL OR max_refund_total IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS approvals (
    id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN
                       ('pending', 'running', 'executed', 'failed', 'rejected', 'expired')),
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gateway          TEXT NOT NULL,    -- slug, as in audit_log
    tool             TEXT NOT NULL,    -- full <server>__<tool> name
    arguments        TEXT NOT NULL,    -- JSON, frozen at request time
    items            INTEGER NOT NULL,
    refund_total     NUMERIC(12, 2),
    budget           TEXT NOT NULL,    -- the budget it exceeded, e.g. "20 items/hour", for the card
    client           TEXT,             -- MCP client name from initialize, when known
    requested_at     DOUBLE PRECISION NOT NULL,
    decided_by       BIGINT REFERENCES users(id) ON DELETE SET NULL,
    decided_at       DOUBLE PRECISION,
    finished_at      DOUBLE PRECISION,
    note             TEXT,
    result           TEXT,             -- the result as the requester would get it; cleared on delivery or after 24 h
    audit_id         BIGINT            -- the audit row of the run
);

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS items INTEGER;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS refund_total NUMERIC(12, 2);
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS approval_id BIGINT;
```

Constants in a new `approvals.py`: `WINDOW_SECONDS = 3600`,
`LOCK_SECONDS = 3600`, `EXPIRE_SECONDS = 86400`.

### The budget check (upstream.py, `call_tool`)

Runs after the rate limit, policy and enabled checks, before `_credential` and
the upstream call. Skipped for admins and for approved runs.

1. Load the caller's budgets for this gateway, server and tool (their teams'
   `team_budgets` rows; smallest `max_items` and smallest `max_refund_total`).
   None: no check.
2. `items = len(arguments["items"])` when `arguments` has an `items` list, else
   `1`. For `bulk_refund_orders`, `refund_total = sum(item["amount"])`; a
   missing or non-numeric amount is refused.
3. Used: `SUM(items)`, `SUM(refund_total)` over `audit_log` rows with the same
   user, gateway and tool, `ok = 1`, `approval_id IS NULL`,
   `at > now - WINDOW_SECONDS`.
4. `used + this > budget` for either limit → refused with the message in
   section 1. Otherwise the call runs.

Every call to a budgeted tool records `items` and `refund_total` on its audit
row, whether it runs or is refused (only `ok = 1` rows count).

### `gateway__request_approval` (built-in tool)

Input: `{"tool": "<server>__<tool>", "arguments": {...}}`.

1. Same policy check as a call to that tool: a tool the caller cannot use is
   "unknown tool". The tool must be budgeted for the caller; otherwise "this tool
   needs no approval; call it directly".
2. Count `items` / `refund_total` as above. Fits the remaining budget → refused,
   "call it directly".
3. Expire stale rows (below). A `pending` or `running` approval for the same
   user, gateway and tool → refused with its id.
4. A `rejected` one with `decided_at > now - LOCK_SECONDS` → refused with the
   unlock time.
5. Insert a `pending` row and return (not an error):

   > Approval #42 requested: bulk_update_product_descriptions on 60 items
   > (budget 20 items/hour). Nothing has changed yet. Call
   > gateway__wait_for_approval with {"id": 42} to wait for the decision.

Counts against the 20/minute rate limit like any call. Audited with the
approval id.

### `gateway__wait_for_approval` (built-in tool)

Input: `{"id": integer}`.

- Only the requester can wait on an approval; any other id answers "unknown
  approval", like a hidden tool.
- Checks the row every 2 s for up to 60 s, sending MCP progress notifications
  between checks when the client asked for them. If the SDK's `on_call_tool`
  cannot send progress, it checks without them and the hold drops to 25 s.
- Returns:
  - `executed` / `failed`: the stored result, exactly as the original call
    would have returned it; then `result` is cleared.
  - `rejected`: an error result with the admin's note and the lock end time.
  - `expired`: an error result.
  - still `pending` / `running`: "Approval #42 still pending; call
    gateway__wait_for_approval again."
- Not counted by the rate limit. Audited with the approval id and the status
  returned, never the payload.

Both built-in tools are listed on every edge for every member, named
`gateway__…` and routed in `call_tool` before the `<server>__<tool>` lookup.
Creating or renaming a server to `gateway` is refused, so the names cannot clash.

### Approve and reject (control pod, `approvals.py`)

| Route                                        | Who                      | Does                                                |
| -------------------------------------------- | ------------------------ | --------------------------------------------------- |
| `GET /api/approvals?status=pending\|history` | admin                    | List, newest first, with the first 20 items of each |
| `GET /api/approvals/count`                   | admin                    | Pending count, for the nav badge                    |
| `POST /api/approvals/{id}/approve {note?}`   | admin, not the requester | Run, then store the result                          |
| `POST /api/approvals/{id}/reject {note?}`    | admin, not the requester | Mark rejected                                       |

Approve:

1. Claim the row:
   `UPDATE approvals SET status='running', decided_by=?, decided_at=? WHERE id=? AND status='pending' RETURNING *`.
   No row → 409, already decided or expired. One click, one run, whatever the
   number of admins or pods.
2. Run `Upstreams.call_tool` as the requester, on the request's gateway, with
   the frozen arguments, `approval_id` set, and timeout
   `APPROVAL_CALL_TIMEOUT_S` (default 120). The approval skips the budget check
   and the rate limit; everything else applies: the requester's current policy
   (they may have lost access), personal credentials, PII filtering, audit.
3. Store the result as returned, set `finished_at`; status `executed`, or
   `failed` if `is_error`.

Reject: `UPDATE … SET status='rejected' … WHERE id=? AND status='pending'`;
no row → 409.

Housekeeping, done on every list, request and wait (no background job):

- `pending` older than `EXPIRE_SECONDS` → `expired`.
- `running` older than `2 × APPROVAL_CALL_TIMEOUT_S` → `failed` with the result
  "The run did not finish (the gateway restarted?). Its outcome is unknown:
  check the store before asking again."
- `result` of `executed` / `failed` rows with `finished_at` older than
  `EXPIRE_SECONDS` → set to NULL. The stored result is the only copy of a call's
  output kept outside the audit log's masked payload, so it lives at most 24 h.

### Budgets API (teams.py)

`PUT /api/teams/{id}/gateways/{slug}/budgets`, admin only, replaces the team's
budgets on one gateway, like `set_tools`:

```json
{
  "budgets": [
    {
      "server_id": 3,
      "tool": "bulk_update_product_descriptions",
      "max_items": 50
    },
    {
      "server_id": 3,
      "tool": "bulk_refund_orders",
      "max_items": 20,
      "max_refund_total": 10000
    }
  ]
}
```

Validated: the server is attached to the gateway, limits are positive whole
numbers (money: up to 2 decimals), at least one limit per row,
`max_refund_total` only on `bulk_refund_orders`. `GET /api/teams` includes each
team's budgets.

## 4. Dashboard (services/dashboard/src)

- **Teams page:** next to each allowed tool in a team's policy, an optional
  "items / hour" field (and "money / hour" on `bulk_refund_orders`). Empty =
  not budgeted.
- **New `Alerts.jsx`**, admins only, nav badge polling the pending count every
  10 s:
  - **Pending:** explicitly requested approvals only. Each card: requester,
    team, client, tool, item count, refund total, the budget it exceeded, a
    table of the first 20 items, a note field, Approve / Reject. After Approve
    the card shows the outcome.
  - **History:** executed, failed, rejected and expired requests, with who
    decided and the outcome.

Over-budget calls that were never submitted never appear in Alerts.

## 5. Tests

woo-mcp (`tests/fake_woo.py` gains `/products/batch`, `/orders/batch`,
`/orders/{id}/refunds`):

- each tool updates the fake store and reports per item
- a missing product or order fails only that item
- a refund amount over the remaining refundable total fails that item
- more than 100 items, or an item with no fields to change, is refused

Gateway (edge settings in these tests raise `calls_per_minute` so the rate
limit does not interfere):

- under budget runs; 19 + 1 runs; the next item is refused with left/free-up
  info; nothing is created in `approvals`
- 19 + 19: the second call is refused
- rolling window: rows older than 60 min stop counting (audit `at` set back)
- single-item tool: 21 calls under a 20/hour budget, the 21st is refused
- failed calls use no budget
- refund money budget refuses while under the item budget
- several teams: the smallest budget applies; admins and unbudgeted tools are
  not checked
- `request_approval`: fits budget → refused; over budget → pending row; second
  request while pending → refused; unknown/hidden tool → unknown
- approve runs the frozen arguments once and the result reaches `wait`;
  approved items do not count against the budget afterwards
- approve twice → 409; approving your own request → 403
- reject: `wait` gets the note; new requests refused for 1 h, then allowed;
  direct calls within budget still run; other tools and members unaffected
- expiry after 24 h: `wait` gets expired; no lock
- a `running` row past 2 × timeout becomes failed with the unknown-outcome text
- results are cleared on delivery and 24 h after finishing
- approve re-checks policy: a requester removed from the team gets a refusal stored
- `wait` on another user's id → unknown approval
- a server named `gateway` cannot be created
- budgets API: rejects detached servers, zero/negative limits, a money limit
  on a non-refund tool

## 6. Not in v1

- Before/after preview of current values on the approval card
- Slack or email notifications
- Multi-step approval chains
- Budgets on anything other than item count and refund total
- A per-call maximum below the hourly budget (the budget already caps a single
  call; woo-mcp caps at 100)
- Re-reading the store before running an approved request: an order that
  changed since the request shows up as a failed item in the result

## 7. Risks

- An approved run uses the store's state at approval time, not request time.
- Refunds are records only (cash on delivery); a real payment gateway would
  need `api_refund: true` and its own review.
- A normal (unapproved) bulk call still has the 10 s call timeout: 100 refunds
  at 5 at a time may not finish in it. Budgets of 20–50 items keep direct calls
  well inside it; bigger jobs go through approval with the 120 s timeout.
- Budgets are per member: two members of a team each get the full budget.
- Calls that go around the gateway (wp-admin, a raw WooCommerce key) are not
  budgeted.

## Manual Test.

1. The budget can't be dodged

As bob:

1. Run bulk_update_product_descriptions on 19 products, then on 1. Both should run.
2. Run it on 1 more. Expect a refusal like "0 of 20 items left; 19 free up at HH:MM UTC… call gateway__request_approval".
3. Call update_order_status 4 times. The 4th should be refused. This shows looping a single-item tool doesn't escape the budget.
4. Ask Claude to send 8 bulk calls of 5 items in parallel. In psql:
   SELECT ok, items, error FROM audit_log WHERE tool LIKE '%descriptions' ORDER BY id DESC LIMIT 10;
   No more than 20 items should have ok=1 in the hour.
5. Check SELECT * FROM approvals;. It should be empty: a refusal never creates an approval.

6. Approve end to end, and it runs exactly once

7. As bob, ask Claude to update descriptions on 60 products.
8. Expect a refusal, then Claude calls gateway__request_approval and gets back "Approval #N requested… Nothing has changed yet".
9. Check the store: none of the 60 products has changed.
10. As admin, open Alerts. The badge should show 1. The card should show bob, his team, 60 items, the 20 items/hour budget and the first 20 items.
11. Open the card in two browser tabs and click Approve in both. One tab shows "executed" and the other "already decided or expired" (409).
12. Check the store: all 60 products changed once. Claude's wait_for_approval returns the result.
13. Check retention: SELECT result FROM approvals WHERE id=N; should be NULL now that the result was delivered.
14. Check budget accounting: bob can still run 20 items directly, because approved items don't use his hourly budget.

15. Access control

16. Members can't reach admin routes. With bob's API key, each of these should return 403:
    curl -H "Authorization: Bearer <bob key>" http://dash.localhost/api/approvals
    curl -X POST -H "Authorization: Bearer <bob key>" -H 'Content-Type: application/json' \
    -d '{}' http://dash.localhost/api/approvals/1/approve
    curl -X PUT -H "Authorization: Bearer <bob key>" -H 'Content-Type: application/json' \
    -d '{"budgets":[]}' http://dash.localhost/api/teams/1/gateways/<slug>/budgets
17. Nobody can read someone else's approval. As carol, call gateway__wait_for_approval with bob's id. Expect "Unknown approval".
18. Nobody can approve their own request. Run:
    UPDATE approvals SET user_id=<admin id>, status='pending' WHERE id=<some id>;
    Then try to approve it as admin. Expect 403.
19. Policy is checked again at approval time.
    - Have bob request a 60-item job.
    - Remove bob from the team.
    - Approve the request.
    - Expect status failed with "Unknown tool", and no store change.
20. A hidden tool stays hidden. Have bob call request_approval for a tool his team doesn't allow. Expect "Unknown tool", so the tool's existence isn't revealed.

21. Rejection lock and expiry

22. Bob requests 30 items. As admin, reject with the note "split it up".
23. Bob's wait returns an error that includes "split it up" and a "locked until HH:MM UTC" time.
24. While the lock is on:
    - Bob requests the same tool again. Refused with "Approval #N was rejected…".
    - Bob runs 5 items directly. Runs, because the budget still works.
    - Carol requests 30 items. Allowed, because the lock is per user.
25. Shorten the lock and check it lifts:
    UPDATE approvals SET decided_at = decided_at - 3601 WHERE id=N;
    Bob's next request should be accepted.
26. Simulate expiry:
    UPDATE approvals SET requested_at = requested_at - 86401 WHERE status='pending';
    The wait returns "expired", and a new request is allowed because expiry doesn't lock.

27. Refunds stay safe

Create three cash-on-delivery orders of 500 each first.

1. As carol, call bulk_refund_orders with [600 on order A]. Refused: 600 is more than order A's 500 total. Nothing should be recorded.
2. Refund 400 on A. Runs. In wp-admin, check that order A shows a 400 refund recorded and no payment was reversed.
3. Refund [200 on A, 300 on B]. Item A fails with "only 100.00 left to refund"; item B succeeds.
4. Refund [400 on B, 400 on C]. Refused by the money budget, because it would take the hour's total to 1500 against a limit of 1000, even though only 2 items are involved.
5. Send each of these. All should be refused before anything reaches the store:
   - amount "-5"
   - amount "abc"
   - the same order twice in one call
   - 101 items

   To confirm, check that audit_log has no row with ok=1 for these calls.

---
