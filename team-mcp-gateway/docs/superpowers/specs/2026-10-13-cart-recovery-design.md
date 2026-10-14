# Cart recovery offers: design

## Goal

A marketing member uses their own AI agent (Claude, ChatGPT, or the dashboard
chat; any MCP client connected to the gateway) to look at abandoned carts. The
store supplies the hard facts: the carts of the last 20 days with each
product's price, cost of goods (COGS) and the highest safe discount. The agent
does the analysis and proposes a discount per product. The proposal goes to the
admin's Alerts page; an admin reviews it and approves, and the
gateway then runs it by itself: each approved product goes on sale (a
WooCommerce sale price that ends by itself), so the cart owners see the lower
price in their cart and on the storefront.

**No AI runs on our side for this feature.** The tools compute the facts and
the limits, the gateway enforces approval, and the judgment comes from whatever
agent the member brings. The server side is identical for every client.

Built for the demo stores (deployed on a VPS): the carts are synthetic, seeded
at provisioning, plus any real account the tester adds by hand.

## Out of scope (v1)

Email or any other notification to customers, discounts for the cart owners
only (personal coupons; see "Why a sale price"), an A/B control group, a
recovery results report, any LLM call or prompt in the gateway for this
feature.

### Why a sale price, not coupons

A sale price is WooCommerce's own field: the product page and every cart show
the crossed-out price, and WooCommerce ends the sale on `date_on_sale_to` by
itself. Nothing else is needed in the store. The cost: the discount applies to
every buyer of the product, not only the people who abandoned it. Personal
coupons (restricted to the cart owners' emails, auto-applied by the store
plugin) are the upgrade when that matters.

## Flow

```
Member's agent (OAuth or API key) -> gateway
  -> woo__get_abandoned_carts(days=20)
       per-product facts + max_discount_pct; no customer data
  -> the agent analyses and proposes discounts <= max_discount_pct
  -> woo__create_cart_offers(items)        refused: needs approval
  -> gateway__request_approval(tool, arguments, summary=<agent's analysis>)
       card on Alerts: items table (verified at run time) + the agent's summary
       the agent tells the member "approval #N is pending" and polls
       gateway__wait_for_approval (or stops; the call runs on approval either way)
  -> admin approves on Alerts
       gateway runs the frozen call as the requester
       per product: sale price, from now until now + ends_in_days
  -> admin's card shows the outcome (new prices, end dates);
     the member's agent gets the same result from gateway__wait_for_approval
  -> a cart owner logs in, opens the cart: the sale price is already there
```

Two properties hold whatever the client:

- **The agent never sees customers.** It gets per-product counts only and can
  only say "product, percent, days".
- **The agent cannot skip approval.** The gateway refuses the write tool for
  every caller with a budget of 0, regardless of client.

## Pieces

### 1. Store plugin (`charts/store/files/cart-recovery.php`)

A must-use plugin, copied to `wp-content/mu-plugins/` by `setup-woo.sh`.

- **Cart timestamp.** On `woocommerce_cart_updated`, for a logged-in user,
  store user meta `_cart_updated_at` = now. WooCommerce already keeps the cart
  itself in user meta `_woocommerce_persistent_cart_1`.
- **Carts endpoint.** `GET /wp-json/wc/v3/cart-recovery/carts?days=N` returns
  `[{user_id, updated_at, items: [{product_id, quantity}]}]` (no names or
  emails) for users whose `_cart_updated_at` is within `days` and whose cart is
  not empty. Permission: `manage_woocommerce`, checked on the user behind the
  WooCommerce API key. The route is in the `wc/v3` namespace, so WooCommerce's
  key authentication applies (checked in WooCommerce 11.2's
  `WC_REST_Authentication::is_wc_namespace`), and woo-mcp calls it like any
  other path.
- **Refresh command.** `wp cart-recovery refresh` re-spreads the synthetic
  shoppers' `_cart_updated_at` over the last 30 days (each shopper's age comes
  from `cart_recovery_synthetic_age(n)`, the same function the seed uses), so the
  20-day window has data weeks after deploying. It only touches users with
  meta `_synthetic_shopper = 1`.

### 2. Seed data: Catalog B (`charts/store/files/seed.php`)

Catalog B is the cart recovery demo store (Catalog A and Blank are unchanged).
Only runs on an empty store, like the rest of `seed.php`. Built.

- **40 hand-picked products**, 8 in each of Fashion, Sports, Beauty, Grocery,
  Garden, each with a price and COGS in WooCommerce's own Cost of Goods Sold
  field (`set_cogs_value`; `setup-woo.sh` turns the feature on for every store,
  option `woocommerce_feature_cost_of_goods_sold_enabled`). They include cases the
  demo needs: `Fresh Deodorant 150ml` (249, COGS 110), the hero product;
  `Basmati Rice 5kg` (699, COGS 640), too thin to discount; `Mystery Garden
Box`, no COGS (refused by the write tool).
- **300 shoppers** (`shopper001@example.com` …, meta `_synthetic_shopper = 1`),
  each with a saved WooCommerce cart (user meta
  `_woocommerce_persistent_cart_1`, the same one WooCommerce writes for a
  logged-in visitor, so it shows up when they log in) and `_cart_updated_at`
  spread over the last 30 days (about 200 within 20 days).
  Exactly 80 carts hold the deodorant; every cart has 1–3 products, picked by a
  per-product popularity weight. The job log names a few deodorant holders.
- Password for all shoppers: `SHOPPER_PASSWORD` from the store's
  `woo-mcp-credentials` Secret (the operator generates it); random when unset.
- Fixed `mt_srand`, so every Catalog B store gets the same carts.

### 3. Read tool: `get_abandoned_carts`

```
get_abandoned_carts(days: int = 20 [1..90], limit: int = 20 [1..100]) -> AbandonedCarts
```

- Reads the plugin's carts endpoint, then the products in them: price, and
  `cogs` from WooCommerce's `cost_of_goods_sold.total_value` (null when unset).
- Per product: `product_id`, `name`, `regular_price`, `cogs`, `margin_pct`,
  `carts`, `units`, `cart_value`, `max_discount_pct`.
- `max_discount_pct = floor((1 - cogs * (1 + MIN_MARGIN) / price) * 100)`,
  capped at `MAX_DISCOUNT_PCT`; 0 when COGS is unknown or the margin is already
  thin.
- Sorted by `cart_value` descending, top `limit`. Totals: carts, customers,
  value.
- No customer ids, names or emails in the result: the agent does not need them.
- Read-only annotation. The math is in code; the agent ranks and explains.
- **Description written for an unknown agent:** says what each field means
  (`max_discount_pct` = "highest discount that keeps the price at least
  `MIN_MARGIN` above cost"), and that offers are made with
  `create_cart_offers`, which needs an admin's approval.

### 4. Write tool: `create_cart_offers`

```
create_cart_offers(items: [{product_id, name, regular_price, cogs,
                            discount_pct, sale_price, ends_in_days}]) -> BulkResult
```

- `name`, `regular_price`, `cogs` and `sale_price` are there so the approval
  card (which renders `items` as a table) shows the admin the real numbers.
  **They are checked against the store when the call runs, not trusted:** an
  item whose numbers do not match the store is refused, so whatever the admin
  approves either matches the store or is not applied.
- Per item, the tool re-reads the product and its carts at run time (that is,
  after approval) and refuses the item when:
  - the product is gone, or `name`, `regular_price` or `cogs` differ from the
    store;
  - the product has no COGS (no floor can be checked);
  - `sale_price` is not `regular_price * (1 - discount_pct / 100)` rounded to
    the store's decimals;
  - `discount_pct` is above the product's `max_discount_pct` now;
  - `ends_in_days` is outside 1..`MAX_OFFER_DAYS`;
  - no cart in the last `CART_WINDOW_DAYS` holds the product;
  - the product is already on sale (a re-run or a second approval does not
    stack discounts).
- Passing items are written in one `POST /products/batch`: `sale_price`,
  `date_on_sale_from` = now, `date_on_sale_to` = now + `ends_in_days`.
  WooCommerce ends the sale on its own; nothing restores prices.
- Returns the usual `BulkResult`; a passing item's entry carries the sale price
  and end date.
- **Description written for an unknown agent:** copy `name`, `regular_price`
  and `cogs` from `get_abandoned_carts`; every call needs an admin's approval;
  on refusal call `gateway__request_approval` with a `summary` of the analysis,
  then report the approval id to the user (polling `wait_for_approval` is
  optional; the call runs on approval either way).

Limits are woo-mcp settings (`config.py`), with defaults `MIN_MARGIN=0.10`,
`MAX_DISCOUNT_PCT=40`, `MAX_OFFER_DAYS=14`, `CART_WINDOW_DAYS=20`.

### 5. Gateway: "always needs approval" and the agent's summary

Small changes to the bulk-approvals feature
(`2026-10-13-bulk-approvals-design.md`); nothing specific to carts or to any
client:

- **Budget 0 = every call needs approval.** `team_budgets.max_items` allows 0
  (`CHECK (max_items >= 0)`). With 0, every call of that tool is refused with
  "This tool needs an admin's approval for every call; call
  gateway__request_approval with …", and `request_approval` always accepts it.
  Teams page: 0 is a valid value and shows as "approval for every call". Admins
  stay unbudgeted, as today.
- **`summary` on approval requests.** `gateway__request_approval` takes an
  optional `summary` (text up to 4000 characters): the agent's analysis and why
  it asks. Stored in `approvals.summary`. The card shows it below the items
  table, labelled "Written by the requester's agent, not verified". The admin
  approves the frozen arguments, not the summary.
- No changes to the dashboard chat: it is one more MCP client.

### 6. Access and credentials

- **Tool policy:** marketing team gets both tools; support gets only
  `get_abandoned_carts`. Budget 0 on `create_cart_offers` for marketing.
- **Shared or personal identity: both work.** The approved run goes out as the
  requester, so on a personal connection it uses the requester's own key.
  - _Shared_ (default): woo-mcp uses the store's admin key from the
    `woo-mcp-credentials` Secret (`WOO_CONSUMER_KEY`, `WOO_CONSUMER_SECRET`).
    Nothing for members to set up; WooCommerce records every change as made by
    the admin key's user.
  - _Personal_: each marketing member has a WordPress user with the **Shop
    manager** role (has `manage_woocommerce` and can edit products), creates a
    WooCommerce REST key with **Read/Write** permission (WooCommerce → Settings
    → Advanced → REST API), and pastes `ck_…:cs_…` (consumer key and consumer
    secret, both) on the dashboard's Credentials page. WooCommerce then records
    who changed each price. A member without a key gets a clear error, never
    the shared key.

### 7. Deployment (VPS)

- External agents connect over OAuth, which needs a stable public URL: use the
  VPS domain with HTTPS (k3s Traefik + Let's Encrypt). A tunnel such as ngrok
  only works with a fixed domain; a changing URL breaks the OAuth redirect and
  the connector address.
- Expose the gateway and the storefront. Keep WP admin off the public internet
  or behind extra auth: 300 seeded accounts share one password.

## Errors

- Product deleted or changed between proposal and approval: that item fails
  with the reason; the others still apply.
- Numbers on the card that do not match the store (agent copied them wrong):
  that item fails with the reason.
- Product without COGS: `max_discount_pct` 0 in the read tool; the write tool
  refuses it.
- No carts left for a product at run time (carts aged out or emptied): that
  item fails with the reason.
- Admin rejects: existing reject path (new requests for the tool locked for an
  hour); the member's agent gets the admin's note from
  `gateway__wait_for_approval`.
- 24 h without a decision: expired, as today.

## Testing

`services/woo-mcp/tests` with the existing `fake_woo.py`, extended with the
carts endpoint and `cost_of_goods_sold` on products:

- read tool: window filter, grouping and sums, `max_discount_pct` incl. missing
  COGS and thin margin, no customer data in the result.
- write tool: each refusal reason, including mismatched `name`, `regular_price`,
  `cogs` and `sale_price`; a passing item sends the right sale fields; a product
  already on sale is skipped; mixed batch returns per-item results.

Gateway tests (Postgres on 5433):

- budget 0 refuses a direct call from any client (API key and OAuth) and points
  at approval;
- `request_approval` accepts it and stores `summary`.

Manual, on the VPS:

1. Create a store with Catalog B (or run `wp cart-recovery refresh` on an
   older one). Get the shopper password:
   `kubectl -n store-<name> get secret woo-mcp-credentials -o jsonpath='{.data.SHOPPER_PASSWORD}' | base64 -d`
2. Optional: log in with a real account, add the deodorant to the cart, leave.
3. As a marketing member, connect Claude to the gateway over OAuth and ask it
   to check abandoned carts and propose discounts. Claude reads the facts,
   analyses, and requests approval; the card appears on the admin's Alerts.
4. As admin, approve on Alerts. The card shows the new prices and end dates;
   Claude, polling `wait_for_approval`, reports the same.
5. Log in as a shopper whose cart holds the deodorant: the cart shows the sale
   price. The product page shows the crossed-out price.
6. In WP admin, the product shows the sale and its end date; the Cost of Goods
   field shows the cost.

## Status

Built: store plugin, Catalog B seed, both woo-mcp tools, budget 0 and
`summary` in the gateway, Teams and Alerts pages. Tests pass (woo-mcp,
gateway, store-operator). Not yet run against a real store.
