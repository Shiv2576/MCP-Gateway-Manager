# Restock planning: design

## Goal

A finance member uses their own AI agent (Claude, ChatGPT, or the dashboard
chat; any MCP client connected to the gateway) to decide what to reorder and
what to clear. Inventory loses money in two ways:

- **Stockouts:** a bestseller runs out, and every visitor until it is restocked
  is a lost sale.
- **Overstock:** money sits on shelves as unsold stock (stock × cost) instead of
  in the bank.

The store supplies the hard facts per product: stock, sales speed, days until it
runs out, a suggested reorder quantity and its cost, and cash tied up. The member
asks, for example, "I have ₹50,000 for restocking this month, what should I
reorder?" The agent prioritises within the budget ("the deodorant runs out in 6
days and has the best margin; the chips can wait") and proposes a restock plan.
An admin approves it on Alerts; the gateway runs it, and the store records a
purchase order (PO). When the goods arrive, `receive_stock` adds them to stock
and closes the PO. For overstock, the agent proposes a clearance sale that stays
above cost; that also needs an admin's approval.

**No AI runs on our side for this feature**, as with cart recovery: the tools
compute facts and limits, the gateway enforces approval, the member's agent
brings the judgment.

Built for the demo stores: Catalog A is rebuilt as the restock demo store
(synthetic sales history). Catalog B stays the cart recovery store, unchanged.

## Out of scope (v1)

Suppliers, supplier lead times per product, sending POs to anyone, partial
cancellation of a PO, a money budget enforced by the gateway (the agent works
to the member's budget; the admin sees the plan total on the card), seasonality
or forecasting beyond "average units per day over a window", variable products
(checked at parent level only, like `get_low_stock_products`), customer
win-back (its own spec later: it needs customer data and a channel to reach
them).

## Flow

```
Finance member's agent -> gateway
  -> woo__get_inventory_health(days=30)
       one row per stock-tracked product: stock, on order, per_day, days_left,
       reorder_qty, reorder_cost, cash_tied_up, status, max_discount_pct
  -> the agent ranks within the member's budget
  -> woo__create_restock_plan(items)            refused: needs approval
  -> gateway__request_approval(tool, arguments, summary=<agent's analysis>)
  -> admin approves on Alerts
       gateway runs the frozen call as the requester
       store records PO #N, status "ordered"; on_order goes up
       team alert to store-operators (2026-10-15-team-alerts-design.md)

Store operator (team store-operators) -> gateway
  -> sees the alert on the Alerts tab, or via gateway__list_team_alerts
  -> goods arrive (in the demo the operator also plays the supplier)
  -> woo__receive_stock(po_id, items)           no approval; bounded by the PO
       stock goes up; PO "partially_received", then "received" when every line is in
  -> their agent marks the alert done (gateway__mark_team_alert_done)

Finance member's agent
  -> woo__list_restock_orders()                 what is on order, what came in

Overstock:
  -> get_inventory_health rows with status "overstock" and max_discount_pct
  -> woo__create_clearance_offers(items)        refused: needs approval
  -> request_approval -> admin approves -> sale price until now + ends_in_days
```

## Pieces

### 1. Store plugins on every store

`setup-woo.sh` copies the custom must-use plugins into `wp-content/mu-plugins/`
for **every** template (`catalog-a`, `catalog-b`, `blank`): `cart-recovery.php`
(already so) and the new `restock.php`. The setup Job runs after every install
and upgrade, so existing stores get the plugin on their next `helm upgrade`;
they get the demo data only when recreated.

**A store without the data answers successfully with empty results, never an
error:**

| Call | On a store without the data |
|---|---|
| `get_abandoned_carts` | `carts: 0, products: []` (already so) |
| `get_inventory_health` | `products: []`, `untracked: N` (N products do not track stock) |
| `list_restock_orders` | `orders: []` |
| `create_restock_plan`, `create_clearance_offers` | each item refused with its reason, e.g. "stock tracking is off for this product" |
| `receive_stock` | each item refused: "PO #N not found" |
| `wp restock refresh`, `wp cart-recovery refresh` | "0 … re-dated", success |

### 2. Store plugin: `charts/store/files/restock.php`

A must-use plugin, like `cart-recovery.php`. WooCommerce has no purchase orders;
this adds them.

- **PO storage.** A non-public custom post type `restock_po`. Post meta:
  `_status` (`ordered` | `partially_received` | `received`), `_lines`
  (`[{product_id, name, qty, unit_cost, received}]`), `_total` (Σ qty ×
  unit_cost), `_note`. Post author = the user behind the WooCommerce API key,
  so WP records who ordered. Post date = ordered at; `_received_at` when the
  last line comes in.
- **Purchase orders in WP admin.** The post type has `show_ui` on, under the
  WooCommerce menu, read-only (no create/edit capability): a list of POs with
  status, total and ordered date, so the demo can show the PO is real store
  data. Changes only go through the endpoints below.
- **Endpoints**, all under `wc/v3` so WooCommerce's API keys authenticate them
  (as for the carts endpoint), permission `manage_woocommerce`:
  - `GET /wc/v3/restock/orders?status=open|received` → `[{id, status,
    ordered_at, received_at, total, note, lines}]`, newest first. `open` =
    `ordered` or `partially_received` (goods still to come).
  - `POST /wc/v3/restock/orders` `{lines: [{product_id, name, qty, unit_cost}],
    note}` → the new PO. Only stores; woo-mcp has already checked the lines.
  - `POST /wc/v3/restock/orders/{id}/receive` `{items: [{product_id, qty}]}` →
    per item `{product_id, ok, error}` and the PO after. Per item: refuse when
    the product is not on the PO or `qty` > `qty − received` on that line; else
    `wc_update_product_stock($product, $qty, 'increase')` and add to
    `received`. After a receive, `_status` = `received` when every line is
    fully received, else `partially_received` once anything came in. The
    whole request holds a MySQL named lock (`GET_LOCK('restock_po_<id>', 5)`),
    so two receives of the same PO cannot both pass the check.
- **Refresh command.** `wp restock refresh` re-dates the synthetic history
  orders (meta `_synthetic_age`, seconds) to `now − age`, so the sales window
  has data weeks after deploying. Never touches stock or POs.

### 3. Seed data: Catalog A rebuilt (`charts/store/files/seed.php`)

The template id stays `catalog-a`, so the CRD, gateway, operator and their tests
do not change. The word-list generator and its 300 products are removed.

**40 hand-picked everyday products**, 8 in each of Personal Care, Snacks,
Beverages, Household, Festive. Each row: name, price, COGS (WooCommerce's Cost
of Goods Sold field; null = unknown), stock, units sold per day.

| Category | Product | Price | COGS | Stock | Per day | Role |
|---|---|---|---|---|---|---|
| Personal Care | Fresh Deodorant 150ml | 249 | 110 | 54 | 9 | **stockout hero**: 6 days left, 56% margin |
| | Herbal Shampoo 400ml | 399 | 170 | 30 | 2 | 15 days left, reorder suggested |
| | Mint Toothpaste 150g | 129 | 62 | 270 | 6 | |
| | Neem Soap 4-pack | 199 | 120 | 180 | 4 | |
| | Aloe Face Wash 100ml | 249 | 95 | 135 | 3 | |
| | Coconut Hair Oil 300ml | 189 | 105 | 150 | 3 | |
| | Sunscreen SPF 50 | 499 | 210 | 45 | 1 | |
| | Shaving Foam 200ml | 220 | 120 | 70 | 1.5 | |
| Snacks | Instant Noodles 12-pack | 168 | 150 | 40 | 8 | **thin-margin fast mover**: 5 days left, 11% margin |
| | Salted Peanuts 200g | 99 | 58 | 25 | 3 | 8 days left |
| | Masala Chips 52g | 20 | 11 | 400 | 5 | **"can wait"**: 80 days left |
| | Roasted Makhana 100g | 149 | 80 | 90 | 2 | |
| | Digestive Biscuits 250g | 60 | 42 | 270 | 6 | |
| | Dark Chocolate 70% | 249 | 130 | 50 | 1 | |
| | Trail Mix 200g | 299 | 170 | 45 | 1 | |
| | Banana Chips 200g | 110 | 60 | 90 | 2 | |
| Beverages | Green Tea 100 Bags | 349 | 120 | 0 | 4 | **already out of stock**: no sales in the last 3 days |
| | Filter Coffee 500g | 450 | 260 | 120 | 2.5 | |
| | Masala Chai 250g | 220 | 110 | 135 | 3 | |
| | Mango Juice 1L | 120 | 78 | 225 | 5 | |
| | Lemon Soda 6-pack | 180 | 110 | 135 | 3 | |
| | Coconut Water 1L | 150 | 95 | 90 | 2 | |
| | Hot Chocolate Mix 400g | 399 | 230 | 40 | 0.75 | |
| | Kombucha 330ml | 199 | 120 | 25 | 0.5 | |
| Household | Detergent Powder 1kg | 135 | 118 | 200 | 5 | thin margin, healthy stock |
| | Dishwash Gel 500ml | 110 | 62 | 180 | 4 | |
| | Floor Cleaner 1L | 199 | 105 | 80 | 2 | |
| | Garbage Bags x30 | 99 | 45 | 135 | 3 | |
| | Toilet Rolls 10-pack | 399 | 250 | 70 | 1.5 | |
| | Kitchen Towels 2-roll | 149 | 80 | 90 | 2 | |
| | Steel Scrub Pack | 49 | 18 | 160 | 4 | |
| | Mosquito Repellent Refill | 89 | 48 | 135 | 3 | |
| Festive | Diwali Gift Hamper | 1499 | 950 | 140 | 0.1 | **overstock hero**: ₹1,33,000 tied up |
| | Brass Diya Set | 899 | 420 | 60 | 0 | **dead stock**: no sales in 90 days |
| | Scented Candle Set | 599 | 260 | 80 | 0.5 | overstock: 160 days |
| | Dry Fruit Box 500g | 899 | 640 | 25 | 1 | 25 days left, reorder suggested |
| | Assorted Mystery Box | 499 | — | 30 | 1 | **no COGS**: plan and clearance refuse it |
| | Rangoli Colour Kit | 299 | 120 | 45 | 1 | |
| | Festive Torans | 249 | 90 | 40 | 1 | |
| | Gift Wrap Roll | 99 | 35 | 90 | 2 | |

Filler products hold 40–60 days of stock, so they need no reorder and the
agent's list stays short. With the defaults below, the suggested reorders cost
about **₹1,05,000** (deodorant ≈ 30,700, noodles ≈ 38,400, green tea ≈ 16,100,
dry fruit ≈ 7,700, shampoo ≈ 7,500, peanuts ≈ 5,000), so a ₹50,000 budget
forces a choice. That choice is the demo.

**Order history.** 90 days of orders, built from the table with a fixed
`mt_srand`, so every Catalog A store has the same history:

- Units are spaced evenly in time per product (unit k of a product at
  `k / per_day` days ago, random hour), so any window gives the stated rate.
  Green Tea's units stop 3 days ago.
- Units in time order are packed into orders of 1–12 units (same product
  merged into one line), dated at the order's newest unit. Status
  `completed`. About 3% extra orders are `cancelled` (not a sale, so the
  rates hold). Not `refunded`: the existing reports count refunded orders as
  sales.
- Guest customers `Demo Customer N` / `customerN@example.com`, never the
  Catalog B shoppers, so the PII masking demo still has names and emails.
- The history reaches today, so "today's orders" has data; Catalog A's
  separate "orders created now" count becomes 0.
- Each order gets meta `_synthetic_age` for `wp restock refresh`.
- **Order of steps:** products → history orders → then stock tracking on and
  stock quantities set. Otherwise WooCommerce subtracts the history from stock.
- **Size:** about 109 units a day, about 6.5 units an order, so about 1,500
  orders; under the reports' 2,000-order page limit even for a 90-day window.
  Seed time must be measured against the setup Job's 900 s deadline before
  this is final; if too slow, pack more units per order (fewer orders, same
  rates). Mail is off during the seed (`pre_wp_mail`), so completed orders
  send nothing.

### 4. Read tool: `get_inventory_health`

```
get_inventory_health(days: int = 30 [7..90], limit: int = 50 [1..100]) -> InventoryHealth
```

- Reads all products (paged, like `get_low_stock_products`), sales orders in
  the last `days` (paged `/orders`, `SALE_STATUSES` only), and open POs
  (`GET /restock/orders?status=open`).
- Rows only for products with `manage_stock` on; the rest are counted in
  `untracked`.
- Per product: `product_id`, `name`, `regular_price`, `cogs`, `margin_pct`,
  `stock`, `on_order` (Σ qty − received on open POs), `units_sold`,
  `per_day = units_sold / days`, `days_left = stock / per_day` (null when
  nothing sold; 0 when stock is 0),
  `reorder_qty = max(0, ceil(per_day × (LEAD_TIME_DAYS + COVER_DAYS)) − stock − on_order)`,
  `reorder_cost = reorder_qty × cogs` (null without COGS),
  `cash_tied_up = stock × cogs` (null without COGS), `max_discount_pct`
  (the cart-recovery function: highest discount keeping the price at least
  `MIN_MARGIN` above cost), and `status`:
  - `out_of_stock`: stock ≤ 0
  - `at_risk`: days_left < LEAD_TIME_DAYS (runs out before a reorder arrives)
  - `overstock`: stock > 0 and (nothing sold, or days_left > OVERSTOCK_DAYS)
  - `healthy`: the rest
- Sorted by `days_left` ascending (out of stock first, overstock last), top
  `limit`. Totals: `cash_tied_up`, `reorder_cost` (sum of rows with COGS),
  `untracked`, `truncated`.
- Description for the agent: what each field means; plans go through
  `create_restock_plan` and clearance through `create_clearance_offers`, both
  need an admin's approval.

### 5. Write tool: `create_restock_plan`

```
create_restock_plan(items: [{product_id, name, qty, unit_cost}], note: str = "") -> BulkResult
```

- `name` and `unit_cost` are on the card so the admin sees real numbers; they
  are **checked against the store when the call runs** (after approval).
- Duplicate product ids in one call: `ToolError`.
- Per item, refuse when: product not found; `name` differs; stock tracking is
  off; no COGS; `unit_cost` ≠ the store's COGS; the product is `overstock`
  now ("already N days of stock").
- Passing items become **one PO** (`POST /restock/orders`, lines with the
  store's name and COGS, `note`). Each passing item's result:
  `detail = "PO #N: qty × unit_cost"`. All refused: no PO.
- Budget 0 for the finance team: every call goes through
  `gateway__request_approval`, like `create_cart_offers`.

### 6. Write tool: `receive_stock`

```
receive_stock(po_id: int, items: [{product_id, qty}]) -> BulkResult
```

- No approval: it records that goods arrived and is bounded by a PO an admin
  already approved. It is still a write tool, so the member's default hourly
  item budget applies.
- Calls `POST /restock/orders/{po_id}/receive`; the plugin checks and adds
  stock (see 2). Unknown PO: every item refused "PO #N not found". PO already
  `received`: every item refused "PO #N is already fully received".
- Result per item: `detail = "stock now S (R of Q received)"`.

### 7. Read tool: `list_restock_orders`

```
list_restock_orders(status: "open" | "received" | None = None, limit: int = 20) -> RestockOrders
```

POs newest first: id, status (`ordered`, `partially_received`, `received`),
ordered_at, received_at, total, note, lines with qty, received, remaining,
unit_cost. `open` lists POs with goods still to come. Totals: count, value
on order.

### 8. Write tool: `create_clearance_offers`

```
create_clearance_offers(items: [{product_id, name, regular_price, cogs,
                                 discount_pct, sale_price, ends_in_days}]) -> BulkResult
```

Same input and checks as `create_cart_offers`, one swap: instead of "in a
recent cart", the product must be `overstock` now (computed as in 4, with the
default 30-day window). The shared checks (name, price, COGS match, floor,
max discount, `sale_price` arithmetic, already on sale, `ends_in_days`) move
into one function both tools call; no copy. Budget 0 for the finance team.

### 9. Settings and access

New woo-mcp settings (`config.py`): `LEAD_TIME_DAYS=7`, `COVER_DAYS=30`,
`OVERSTOCK_DAYS=120`. Clearance reuses `MIN_MARGIN`, `MAX_DISCOUNT_PCT`,
`MAX_OFFER_DAYS`.

No gateway code changes for the tools themselves. Tool names on a gateway are
`<server>__<tool>`, and a store's server is named after the store: on a store
`shop1` the tools are `shop1__get_inventory_health` and so on (`woo__` above is
the `woo-mcp` server's prefix). On the Teams page:

| Team | Tools | Approval / alerts |
|---|---|---|
| `finance` | `get_inventory_health`, `create_restock_plan`, `create_clearance_offers`, `list_restock_orders` | budget 0 on the plan and clearance tools |
| `store-operators` | `list_restock_orders`, `receive_stock` | alert rule: `create_restock_plan` succeeds |

The alert rule is set on the **`store-operators`** team's card: tick
"alert store-operators" next to `create_restock_plan`. The box sits beside
every write tool of every team and alerts the team whose card it is on, so a
tick on `finance` or on `receive_stock` sends no alert to the operators (both
happened in the first live test).

The alert itself is the generic team alerts feature
(`2026-10-15-team-alerts-design.md`). Only members see team alerts: an
admin's Alerts tab shows approvals only. There is no Mark done button; an
alert is done when the operator's agent calls `gateway__mark_team_alert_done`. Shared or personal identity both work,
as for cart recovery (a personal key needs Read/Write and a Shop manager user).

### 10. Other edits

- `services/dashboard/src/Stores.jsx`: Catalog A card text "40 everyday
  products with stock and 90 days of sales: restock planning demo".
- `charts/store/values.yaml` comment; `charts/store/files/setup-woo.sh` copies
  `restock.php`.
- `README.md`, `context.md`, `docs/deploy-vps.md`: lines that say Catalog A has
  300 products.
- Server `instructions` in `server.py` mention restock planning.

## Errors

- Product changed or deleted between proposal and approval: that item is
  refused with the reason; the others still go on the PO.
- Two receives of the same PO at once: the named lock serialises them; the
  second sees the updated `received` and is refused for any excess.
- Lock not acquired in 5 s: the receive request fails as a whole with "PO #N
  is busy, try again"; nothing changed.
- Admin rejects or 24 h pass: existing approval paths.
- Store without the plugin (an old store before its upgrade): the plugin
  endpoints 404; the tools return "this store has no restock plugin yet;
  upgrade the store". `get_inventory_health` then reports `on_order` as 0.

## Testing

`services/woo-mcp/tests` with `fake_woo.py`, extended with stock fields,
dated orders and the three restock endpoints:

- `get_inventory_health`: per_day, days_left, reorder_qty incl. on_order,
  each status, missing COGS, `untracked`, empty store → `products: []`.
- `create_restock_plan`: each refusal; passing items make one PO with the
  store's numbers; all refused → no PO; duplicate ids.
- `receive_stock`: over-receive refused, partial receive sets
  `partially_received`, PO closes when full, unknown PO, already received.
- `create_clearance_offers`: refused when not overstock; the shared checks
  still refuse as before; `create_cart_offers` tests unchanged and passing.
- `list_restock_orders`: status filter, empty store.

Seed, on a k3d store: time `wp eval-file seed.php` for Catalog A; then check
the deodorant reports per_day 9 and days_left 6, and the hamper reports
overstock.

Manual demo (VPS or k3d):

1. Create a Catalog A store.
2. Members page: a finance member and a store operator. Teams page: the two
   teams and the alert rule from 9.
3. As the finance member, ask Claude: "I have ₹50,000 for restocking this month.
   What should I reorder?" Claude reads inventory health, picks within budget
   (deodorant first; noodles thin; chips wait), requests approval.
4. Admin approves on Alerts; the card shows PO #N. Inventory health shows
   `on_order`; WP admin → WooCommerce → Purchase orders lists it.
5. As the store operator: the Alerts tab shows "PO #N ordered" with a badge.
   "200 deodorants arrived": `receive_stock` → stock up, PO
   `partially_received`. Then the rest → PO `received`. "Mark the alert done"
   → the agent calls `gateway__mark_team_alert_done`; the Alerts tab shows
   "done by <operator>".
6. As the finance member: "Show me my restock orders" → PO #N received.
7. "What's sitting on the shelves?": Claude proposes clearance on the hamper
   (≤ 30%, the floor at cost + 10%); admin approves; the storefront shows the
   sale price.
8. Run the same tools on a blank store: successful empty answers.

## Status

Built, tested and deployed on k3d `store-check` (2026-10-15/16).

**Deployed:** woo-mcp 0.5.0 (the woo-mcp chart default, used by new stores),
store-operator 0.7.0 (bundles the store chart with `restock.php` and the new
seed), gateway 0.10.0 (control plane and the `prod` edge), dashboard 0.9.3.
Existing stores get `restock.php` on their next setup run but keep their old
data; only Catalog A stores created now get the demo data.

**Tests:** woo-mcp 23/23, gateway 99/99; the dashboard builds.

**Seed, measured on a throwaway Catalog A store:** setup Job 5 min 05 s
against the 900 s deadline, so no repacking was needed. 1,566 history orders
(46 cancelled); 494 completed in the last 30 days, 20 today. Deodorant stock
54 at 9.03/day, noodles 8/day, green tea stock 0 at 3.6/day over 30 days (its
sales stop 3 days ago), hamper 140 at 0.1/day, diya 0. `wp restock refresh`
re-dated the 1,566 orders in 23 s.

**Plugin, on real WordPress:** create PO; over-receive refused;
`partially_received`, then `received`; the already-received and not-found
messages; the status filter; 401 when logged out, 400 on a bad status. The WP
admin Purchase orders list is read-only (no Add new, no edit).

**Live run through the gateway** (store `shop1`, gateway `prod`):
`get_inventory_health` ×3, `list_restock_orders` ×4 and `receive_stock` ×1
all succeeded. Six `create_restock_plan` calls: 3 refused for approval, and
approvals #3 and #4 executed. #4 created PO #1618 (deodorant 160 × 110, green
tea 85 × 120, peanuts 37 × 58) and alert #1. That alert went to `finance`, not
the operators: the rule had been ticked on the wrong team (see 9).

**Not yet run live:** an alert reaching the operators' team (needs the rule on
their card), `gateway__mark_team_alert_done` from an agent, and
`create_clearance_offers`.

**Beyond this design, also deployed:**
- Alerts tab: members see their teams' alerts, listed for good (no
  Open/Done/All filter, no Refresh; reloaded every 30 s), with no Mark done
  button. Done comes only from `gateway__mark_team_alert_done` and shows "Done
  by X". Admins see approvals only, and their badge counts pending approvals
  only. Members' badges count their teams' open alerts.
- Teams page: the alert checkbox reads "alert <team name>".
- Dashboard chat: members also get `gateway__list_team_alerts` and
  `gateway__mark_team_alert_done`.
- The cluster runs with NetworkPolicies on every component. The gateway may
  reach woo-mcp pods only in namespaces the store operator created, so store
  tools work as before (`docs/deploy-vps.md`).
- On `store-check` the operators' team is named `store-operator` (singular);
  the name is free, only the rule's placement matters.
