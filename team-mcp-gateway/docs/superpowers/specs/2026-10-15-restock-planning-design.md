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
  -> goods arrive (in the demo: the member says so)
  -> woo__receive_stock(po_id, items)           no approval; bounded by the PO
       stock goes up; PO becomes "received" when every line is in
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
  `_status` (`ordered` | `received`), `_lines`
  (`[{product_id, name, qty, unit_cost, received}]`), `_total` (Σ qty ×
  unit_cost), `_note`. Post author = the user behind the WooCommerce API key,
  so WP records who ordered. Post date = ordered at; `_received_at` when the
  last line comes in.
- **Endpoints**, all under `wc/v3` so WooCommerce's API keys authenticate them
  (as for the carts endpoint), permission `manage_woocommerce`:
  - `GET /wc/v3/restock/orders?status=ordered|received` → `[{id, status,
    ordered_at, received_at, total, note, lines}]`, newest first.
  - `POST /wc/v3/restock/orders` `{lines: [{product_id, name, qty, unit_cost}],
    note}` → the new PO. Only stores; woo-mcp has already checked the lines.
  - `POST /wc/v3/restock/orders/{id}/receive` `{items: [{product_id, qty}]}` →
    per item `{product_id, ok, error}` and the PO after. Per item: refuse when
    the product is not on the PO or `qty` > `qty − received` on that line; else
    `wc_update_product_stock($product, $qty, 'increase')` and add to
    `received`. When every line is fully received, `_status = received`. The
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
- Units on the same day are packed into orders of 1–4 lines, 1–3 units a line.
  Status `completed`. About 3% extra orders are `refunded` or `cancelled`
  (not counted as sales, so the rates hold); the refund report gets data.
- Guest customers `Demo Customer N` / `customerN@example.com`, never the
  Catalog B shoppers, so the PII masking demo still has names and emails.
- The history reaches today, so "today's orders" has data; Catalog A's
  separate "orders created now" count becomes 0.
- Each order gets meta `_synthetic_age` for `wp restock refresh`.
- **Order of steps:** products → history orders → then stock tracking on and
  stock quantities set. Otherwise WooCommerce subtracts the history from stock.
- **Size:** about 109 units a day, about 5 units an order, so about 2,000 orders. Seed time must be
  measured against the provisioning timeout before this is final; if too slow,
  put more units on each line (fewer orders, same rates).

### 4. Read tool: `get_inventory_health`

```
get_inventory_health(days: int = 30 [7..90], limit: int = 50 [1..100]) -> InventoryHealth
```

- Reads all products (paged, like `get_low_stock_products`), sales orders in
  the last `days` (paged `/orders`, `SALE_STATUSES` only), and open POs
  (`GET /restock/orders?status=ordered`).
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
list_restock_orders(status: "ordered" | "received" | None = None, limit: int = 20) -> RestockOrders
```

POs newest first: id, status, ordered_at, received_at, total, note, lines with
qty, received, unit_cost. Totals: count, value on order.

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

No gateway code changes. On the Teams page: a finance team gets the five
tools; budget 0 on `create_restock_plan` and `create_clearance_offers`.
Shared or personal identity both work, as for cart recovery (a personal key
needs Read/Write and a Shop manager user).

### 10. Other edits

- `services/dashboard/src/Stores.jsx`: Catalog A card text "40 everyday
  products, 90 days of sales history".
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
- `receive_stock`: over-receive refused, partial receive, PO closes when full,
  unknown PO, already received.
- `create_clearance_offers`: refused when not overstock; the shared checks
  still refuse as before; `create_cart_offers` tests unchanged and passing.
- `list_restock_orders`: status filter, empty store.

Seed, on a k3d store: time `wp eval-file seed.php` for Catalog A; then check
the deodorant reports per_day 9 and days_left 6, and the hamper reports
overstock.

Manual demo (VPS or k3d):

1. Create a Catalog A store.
2. Teams page: finance team with the five tools, budget 0 on the plan and
   clearance tools.
3. As a finance member, ask Claude: "I have ₹50,000 for restocking this month.
   What should I reorder?" Claude reads inventory health, picks within budget
   (deodorant first; noodles thin; chips wait), requests approval.
4. Admin approves on Alerts; the card shows PO #N. `list_restock_orders`
   shows it as ordered; inventory health shows `on_order`.
5. "The deodorant delivery arrived": Claude calls `receive_stock`; stock goes
   up; the PO becomes received.
6. "What's sitting on the shelves?": Claude proposes clearance on the hamper
   (≤ 30%, the floor at cost + 10%); admin approves; the storefront shows the
   sale price.
7. Run the same tools on a blank store: successful empty answers.

## Status

Design only. Nothing built.
