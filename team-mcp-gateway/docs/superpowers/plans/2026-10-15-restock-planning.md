# Restock Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five woo-mcp tools (inventory health, restock plan, receive stock, purchase orders, clearance offers), a store mu-plugin for purchase orders on every store, Catalog A rebuilt as the restock demo store with 90 days of synthetic sales, and generic team alerts in the gateway (rules, alerts, one Alerts tab for everyone, two built-in agent tools).

**Architecture:** Same shape as cart recovery. The store mu-plugin `restock.php` adds purchase orders (a private post type, listed read-only in WP admin) and REST routes under `wc/v3`. woo-mcp computes stock facts from products and orders, checks every write against the store, and talks to the plugin. Approval comes from the existing gateway budget-0 flow. Team alerts hook into `Upstreams.call_tool` (the one path for every call, on edges and approved runs) and are stored in Postgres, which control plane and edges share.

**Tech Stack:** Python 3.12+, MCP SDK 2.x (`MCPServer`), pydantic, Starlette fake store for tests; PHP mu-plugin + wp-cli seed; gateway on Postgres; React dashboard; Helm charts on k3d.

**Specs:** `docs/superpowers/specs/2026-10-15-restock-planning-design.md`, `docs/superpowers/specs/2026-10-15-team-alerts-design.md`

## Global Constraints

- Never `git commit`: the user makes all commits. Each task ends with "leave for the user to commit".
- Defaults: `LEAD_TIME_DAYS=7`, `COVER_DAYS=30`, `OVERSTOCK_DAYS=120`; stock window for plan/clearance checks 30 days.
- Status rules: `out_of_stock` stock ≤ 0; `overstock` stock > 0 and (nothing sold or days_left > OVERSTOCK_DAYS); `at_risk` days_left < LEAD_TIME_DAYS; else `healthy`.
- `reorder_qty = max(0, ceil(per_day × (LEAD_TIME_DAYS + COVER_DAYS)) − stock − on_order)`.
- A store without the data answers successfully with empty results, never an error.
- `cart-recovery.php` and `restock.php` are copied to `wp-content/mu-plugins/` on every template.
- Template id stays `catalog-a`; CRD, gateway, operator code unchanged.
- No customer data in any restock tool result.
- woo-mcp tests: `uv run python -m tests.tests_woo_mcp` from `services/woo-mcp` (pytest also collects them).
- PO statuses: `ordered` → `partially_received` (something came in) → `received` (every line in). Filter `open` = `ordered` or `partially_received`.
- Gateway tests need Postgres: `docker run -d --name gw-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine` (once), then from `services/gateway`: `TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q`.
- Team alerts never fail the call that fired them; alert content comes from the PII-masked copy of the result.
- One Alerts tab for everyone: admins see approvals + all teams' alerts; members see their current teams' alerts.

## Review Focus

- A store from before the plugin (route missing, WordPress 404 "No route was found"): `get_inventory_health` still works with `on_order` 0; PO tools say "This store has no restock plugin yet; upgrade the store". Test in Task 1.
- Receiving twice / receiving more than ordered across two calls: the second call sees the first's `received`. Test in Task 3.
- A product with stock tracking on but no COGS: listed with null costs, excluded from totals, refused by plan and clearance. Tests in Tasks 1, 2.
- Plan item whose `unit_cost` is written differently but equal ("120" vs "120.00"): accepted, stored as "120.00". Test in Task 2.
- `create_cart_offers` behaviour unchanged after the shared-check refactor: the existing cart offer test must pass untouched. Task 4.
- A partly delivered PO must still count as on order (`status=open`, not `ordered`), or inventory health would suggest reordering goods already on the way. Test in Task 3.
- A rule on a team that does not itself have the tool (store-operators alerted on finance's `create_restock_plan`): must fire. Test in Task 7.
- The masked result may lose `structured_content`; if so the alert must still read sensibly (text fallback), never crash the call. Test in Task 7 (email in a detail must not reach the alert).

---

### Task 1: Inventory health tool (+ settings, models, fake store)

**Files:**
- Modify: `services/woo-mcp/app/config.py`
- Modify: `services/woo-mcp/app/server.py`
- Modify: `services/woo-mcp/tests/fake_woo.py`
- Test: `services/woo-mcp/tests/tests_woo_mcp.py`

**Interfaces:**
- Produces (inside `build_server`): `restock(method, path, **kw)` (raises `NoRestockPlugin`), `inventory(days) -> (dict[int, dict], list[StockRow], int, bool)`, `stock_row(p, units, on_order, days) -> StockRow`; module-level `STOCK_WINDOW_DAYS = 30`, `NoRestockPlugin(ToolError)`, models `StockRow`, `InventoryHealth`, `PlanItem`, `ReceiveItem`, `PoLine`, `PurchaseOrder`, `RestockOrders`.
- Fake store produces: `app.state.purchase_orders: dict[int, dict]`, flag `app.state.flags["no_restock_plugin"]`; routes `GET/POST /wp-json/wc/v3/restock/orders`, `POST /wp-json/wc/v3/restock/orders/{po}/receive`.

- [ ] **Step 1: Settings.** In `config.py`, after `cart_window_days`:

```python
    # Restock planning: how stock facts are computed.
    lead_time_days: int = 7  # a reorder takes this long to arrive
    cover_days: int = 30  # a reorder should last this long after it arrives
    overstock_days: int = 120  # more days of stock than this is overstock
```

and in `from_env`, after `cart_window_days=...`:

```python
            lead_time_days=int(os.environ.get("LEAD_TIME_DAYS", cls.lead_time_days)),
            cover_days=int(os.environ.get("COVER_DAYS", cls.cover_days)),
            overstock_days=int(os.environ.get("OVERSTOCK_DAYS", cls.overstock_days)),
```

- [ ] **Step 2: Fake store.** In `tests/fake_woo.py` add `from decimal import Decimal` to the imports. In `build_fake_woo`, after `refunds: ...`:

```python
    purchase_orders: dict[int, dict] = {}  # the restock plugin's purchase orders
```

change `state = {"next_id": 100, "slow": False}` to `state = {"next_id": 100, "slow": False, "no_restock_plugin": False}`, and before `def page_of` add:

```python
    def restock_route(handler):
        # A store from before restock.php: WordPress answers an unknown route like this.
        async def wrapped(request: Request):
            if state["no_restock_plugin"]:
                return JSONResponse({"code": "rest_no_route", "message": "No route was found matching the URL and request method."},
                                    status_code=404)
            return await handler(request)

        return wrapped

    async def list_pos(request: Request):
        status = request.query_params.get("status")  # open = ordered or partially_received
        rows = [po for po in purchase_orders.values()
                if not status or (po["status"] != "received" if status == "open" else po["status"] == status)]
        return JSONResponse(sorted(rows, key=lambda po: -po["id"]))

    async def create_po(request: Request):
        body = await request.json()
        po_id = len(purchase_orders) + 1
        lines = [{**line, "received": 0} for line in body["lines"]]
        total = sum(Decimal(line["unit_cost"]) * line["qty"] for line in lines)
        purchase_orders[po_id] = {"id": po_id, "status": "ordered", "ordered_at": "2026-10-15T10:00:00", "received_at": None,
                                  "total": f"{total:.2f}", "note": body.get("note", ""), "lines": lines}
        return JSONResponse(purchase_orders[po_id], status_code=201)

    async def receive_po(request: Request):
        # Same rules and messages as restock_receive() in charts/store/files/restock.php.
        po_id = int(request.path_params["po"])
        po = purchase_orders.get(po_id)
        results = []
        for it in (await request.json())["items"]:
            line = next((l for l in po["lines"] if l["product_id"] == it["product_id"]), None) if po else None
            if po is None:
                why = f"PO #{po_id} not found"
            elif po["status"] == "received":
                why = f"PO #{po_id} is already fully received"
            elif line is None:
                why = f"product {it['product_id']} is not on PO #{po_id}"
            elif it["qty"] > line["qty"] - line["received"]:
                why = (f"only {line['qty'] - line['received']} left to receive on PO #{po_id} "
                       f"({line['qty']} ordered, {line['received']} received)")
            else:
                line["received"] += it["qty"]
                p = products[it["product_id"]]
                p["stock_quantity"] = (p.get("stock_quantity") or 0) + it["qty"]
                results.append({"product_id": it["product_id"], "ok": True, "stock": p["stock_quantity"],
                                "received": line["received"], "qty": line["qty"]})
                continue
            results.append({"product_id": it["product_id"], "ok": False, "error": why})
        if po and po["status"] != "received":
            if all(l["received"] == l["qty"] for l in po["lines"]):
                po["status"], po["received_at"] = "received", "2026-10-15T12:00:00"
            elif any(l["received"] for l in po["lines"]):
                po["status"] = "partially_received"
        return JSONResponse({"results": results, "order": po})
```

Add routes (after the carts route):

```python
            Route("/wp-json/wc/v3/restock/orders", guard(restock_route(list_pos)), methods=["GET"]),
            Route("/wp-json/wc/v3/restock/orders", guard(restock_route(create_po)), methods=["POST"]),
            Route("/wp-json/wc/v3/restock/orders/{po:int}/receive", guard(restock_route(receive_po)), methods=["POST"]),
```

and `app.state.purchase_orders = purchase_orders` next to the other `app.state` lines.

- [ ] **Step 3: Write the failing tests.** In `tests/tests_woo_mcp.py`, before `if __name__ == "__main__":` add:

```python
def _stock_store():
    """The restock heroes, with 30 days of sales. Trail Mix (id 1) tracks no stock."""
    fake = build_fake_woo()
    now = datetime.now(timezone.utc)

    def product(pid, name, price, cogs, stock):
        fake.state.products[pid] = {"id": pid, "name": name, "regular_price": price, "price": price, "on_sale": False,
                                    "manage_stock": True, "stock_quantity": stock, "cost_of_goods_sold": {"total_value": cogs}}

    def sold(oid, days_ago, lines, status="completed"):
        o = make_order(oid, (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S"), status=status)
        o["line_items"] = [{"product_id": pid, "name": "", "quantity": q, "total": "0"} for pid, q in lines]
        fake.state.orders[oid] = o

    product(20, "Fresh Deodorant 150ml", "249", 110, 54)
    product(21, "Instant Noodles 12-pack", "168", 150, 40)
    product(22, "Masala Chips 52g", "20", 11, 400)
    product(23, "Diwali Gift Hamper", "1499", 950, 140)
    product(24, "Brass Diya Set", "899", 420, 60)
    product(25, "Green Tea 100 Bags", "349", 120, 0)
    product(26, "Assorted Mystery Box", "499", 0, 30)  # Woo reports an unset cost as 0
    # Last 30 days: deodorant 270 (9/day), noodles 240, chips 150, hamper 3, green tea 108, mystery box 30.
    sold(500, 1, [(20, 100), (21, 120), (22, 50), (25, 60), (26, 10)])
    sold(501, 10, [(20, 100), (21, 120), (22, 50), (23, 3), (25, 48), (26, 20)])
    sold(502, 20, [(20, 70), (22, 50)])
    sold(503, 2, [(20, 500)], status="cancelled")  # not a sale
    sold(504, 40, [(21, 999)])  # outside the window
    return fake


def test_inventory_health_gives_stock_facts():
    async def run():
        fake = _stock_store()
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                out = _payload(await client.call_tool("get_inventory_health", {"days": 30}))
                assert (out["untracked"], out["truncated"], out["lead_time_days"], out["cover_days"]) == (1, False, 7, 30)
                by = {r["product_id"]: r for r in out["products"]}
                deo = by[20]
                assert (deo["per_day"], deo["days_left"], deo["status"], deo["reorder_qty"], deo["reorder_cost"]) == (
                    9.0, 6.0, "at_risk", 279, "30690.00")
                assert (deo["cash_tied_up"], deo["max_discount_pct"], deo["margin_pct"], deo["on_order"]) == ("5940.00", 40, 55.8, 0)
                assert (by[21]["days_left"], by[21]["status"], by[21]["reorder_qty"]) == (5.0, "at_risk", 256)
                assert (by[25]["per_day"], by[25]["days_left"], by[25]["status"], by[25]["reorder_qty"]) == (3.6, 0.0, "out_of_stock", 134)
                assert (by[22]["days_left"], by[22]["status"], by[22]["reorder_qty"]) == (80.0, "healthy", 0)
                assert (by[23]["days_left"], by[23]["status"], by[23]["cash_tied_up"], by[23]["max_discount_pct"]) == (
                    1400.0, "overstock", "133000.00", 30)
                assert (by[24]["days_left"], by[24]["status"], by[24]["units_sold"]) == (None, "overstock", 0)
                assert (by[26]["reorder_qty"], by[26]["reorder_cost"], by[26]["cash_tied_up"]) == (7, None, None)
                assert [r["product_id"] for r in out["products"]] == [25, 21, 20, 26, 22, 23, 24]
                assert (out["reorder_cost"], out["cash_tied_up"]) == ("85170.00", "174540.00")
                assert "email" not in str(out) and "Priya" not in str(out)
                top = _payload(await client.call_tool("get_inventory_health", {"limit": 2}))
                assert len(top["products"]) == 2 and top["truncated"]

    asyncio.run(run())


def test_inventory_health_on_a_store_without_the_data():
    async def run():
        fake = build_fake_woo()  # one product, no stock tracking, no purchase orders
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                out = _payload(await client.call_tool("get_inventory_health", {}))
                assert (out["products"], out["untracked"], out["reorder_cost"], out["cash_tied_up"]) == ([], 1, "0.00", "0.00")
                # A store from before the plugin: the facts still work, with nothing on order.
                fake.state.flags["no_restock_plugin"] = True
                assert _payload(await client.call_tool("get_inventory_health", {}))["products"] == []

    asyncio.run(run())
```

- [ ] **Step 4: Run, expect failure.** `cd services/woo-mcp && uv run pytest tests/tests_woo_mcp.py -k inventory_health -q` → FAIL (unknown tool `get_inventory_health`).

- [ ] **Step 5: Models.** In `server.py`, after the `OfferItem` class (before `def Items`), add:

```python
# Restock planning. Stock and costs are WooCommerce's own fields; purchase orders
# come from the store plugin (charts/store/files/restock.php).
class StockRow(BaseModel):
    product_id: int
    name: str | None
    regular_price: str
    cogs: str | None = Field(description="Cost of goods per unit; null when the store has none")
    margin_pct: float | None
    stock: int = Field(description="Units in stock now")
    on_order: int = Field(description="Units on open purchase orders, not yet received")
    units_sold: int = Field(description="Units sold in the window")
    per_day: float = Field(description="units_sold / days")
    days_left: float | None = Field(description="stock / per_day; null when nothing sold")
    reorder_qty: int = Field(description="Units to order now to last lead_time_days + cover_days, after stock and on_order")
    reorder_cost: str | None = Field(description="reorder_qty x cogs")
    cash_tied_up: str | None = Field(description="stock x cogs")
    max_discount_pct: int = Field(description="Highest clearance discount that keeps the price at least min_margin_pct above cost")
    status: Literal["out_of_stock", "at_risk", "healthy", "overstock"]


class InventoryHealth(BaseModel):
    days: int
    lead_time_days: int
    cover_days: int
    overstock_days: int
    cash_tied_up: str = Field(description="Sum over all stock-tracked products with a cost")
    reorder_cost: str = Field(description="Cost of every suggested reorder that has a cost")
    untracked: int = Field(description="Products that do not track stock, so are not listed")
    products: list[StockRow]
    truncated: bool


class PlanItem(BaseModel):
    model_config = {"extra": "forbid"}
    product_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200, description="Copied from get_inventory_health")
    qty: int = Field(ge=1, le=100_000, description="Units to order")
    unit_cost: Money = Field(description="The product's cogs, copied from get_inventory_health")


class ReceiveItem(BaseModel):
    model_config = {"extra": "forbid"}
    product_id: int = Field(ge=1)
    qty: int = Field(ge=1, le=100_000, description="Units that arrived")


class PoLine(BaseModel):
    product_id: int
    name: str
    qty: int = Field(description="Units ordered")
    unit_cost: str
    received: int
    remaining: int = Field(description="qty - received: still to come")


class PurchaseOrder(BaseModel):
    id: int
    status: Literal["ordered", "partially_received", "received"]
    ordered_at: str = Field(description="UTC")
    received_at: str | None = Field(None, description="UTC, when the last line came in")
    total: str
    note: str = ""
    lines: list[PoLine]


class RestockOrders(BaseModel):
    count: int
    on_order_value: str = Field(description="Cost of units ordered and not yet received, over the listed orders")
    orders: list[PurchaseOrder]


class NoRestockPlugin(ToolError):
    """The store was set up before charts/store/files/restock.php existed."""
```

Change the `OfferItem.name` description to `"Copied from get_abandoned_carts or get_inventory_health"`. Next to `SALE_STATUSES` add:

```python
STOCK_WINDOW_DAYS = 30  # sales window behind the plan and clearance checks
```

- [ ] **Step 6: Helpers and tool.** In `build_server`, after `create_cart_offers` and before `return server`:

```python
    # ---------------------------------------------------------- restock planning

    async def restock(method: str, path: str, **kw: Any) -> Any:
        """A call to the store plugin; a store from before the plugin gets a clear message."""
        try:
            return await woo.request(method, f"/restock{path}", **kw)
        except WooError as exc:
            if "No route was found" in str(exc):  # WordPress's 404 for a route nobody registered
                raise NoRestockPlugin("This store has no restock plugin yet; upgrade the store") from exc
            raise ToolError(str(exc)) from exc

    def stock_row(p: dict, units: int, on_order: int, days: int) -> StockRow:
        stock = p.get("stock_quantity") or 0
        price = Decimal(p.get("regular_price") or "0")
        cost = cogs_of(p)
        per_day = Decimal(units) / days
        days_left = None if not per_day else (Decimal(0) if stock <= 0 else stock / per_day)
        need = int((per_day * (settings.lead_time_days + settings.cover_days)).to_integral_value(ROUND_CEILING))
        reorder = max(0, need - stock - on_order)
        if stock <= 0:
            status = "out_of_stock"
        elif days_left is None or days_left > settings.overstock_days:
            status = "overstock"
        elif days_left < settings.lead_time_days:
            status = "at_risk"
        else:
            status = "healthy"
        return StockRow(
            product_id=p["id"],
            name=p.get("name"),
            regular_price=_money(price),
            cogs=cost and _money(cost),
            margin_pct=round(float((price - cost) / price * 100), 1) if cost and price else None,
            stock=stock,
            on_order=on_order,
            units_sold=units,
            per_day=round(float(per_day), 2),
            days_left=None if days_left is None else round(float(days_left), 1),
            reorder_qty=reorder,
            reorder_cost=cost and _money(cost * reorder),
            cash_tied_up=cost and _money(cost * max(stock, 0)),
            max_discount_pct=max_discount(price, cost),
            status=status,
        )

    async def inventory(days: int) -> tuple[dict[int, dict], list[StockRow], int, bool]:
        """Every product by id, a row per stock-tracked one (soonest to run out first),
        how many products track no stock, and whether a page limit cut the data."""
        products, cut_products = await all_pages("/products", {})
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S"
        orders, cut_orders = await sales_in((now - timedelta(days=days)).strftime(fmt), now.strftime(fmt))
        try:
            open_pos = await restock("GET", "/orders", params={"status": "open"})  # partly delivered still counts
        except NoRestockPlugin:
            open_pos = []
        sold: dict[Any, int] = defaultdict(int)
        for o in orders:
            for item in o.get("line_items") or []:
                sold[item.get("product_id")] += item.get("quantity") or 0
        on_order: dict[int, int] = defaultdict(int)
        for po in open_pos:
            for line in po.get("lines") or []:
                on_order[line["product_id"]] += line["qty"] - line["received"]
        rows = [stock_row(p, sold[p["id"]], on_order[p["id"]], days) for p in products if p.get("manage_stock")]
        rows.sort(key=lambda r: (r.status != "out_of_stock", math.inf if r.days_left is None else r.days_left))
        return {p["id"]: p for p in products}, rows, len(products) - len(rows), cut_products or cut_orders

    @server.tool(title="Inventory health", annotations=READ)
    async def get_inventory_health(
        days: Annotated[int, Field(ge=7, le=90, description="Sales window: per_day is units sold in the last this many days / days")] = STOCK_WINDOW_DAYS,
        limit: Annotated[int, Field(ge=1, le=100, description="How many products to return")] = 50,
    ) -> InventoryHealth:
        """Stock facts per product that tracks stock, soonest to run out first: stock, units on order,
        sales per_day, days_left, a suggested reorder_qty (enough for lead_time_days + cover_days of sales,
        after stock and on_order) with its reorder_cost, cash_tied_up (stock x cost of goods),
        max_discount_pct for clearance, and status: out_of_stock; at_risk (runs out before a reorder
        could arrive); healthy; overstock (nothing sold, or more than overstock_days of stock).
        Reorders go through create_restock_plan, clearance sales through create_clearance_offers;
        both need an admin's approval. No customer data."""
        _, rows, untracked, truncated = await inventory(days)
        priced = [r for r in rows if r.cogs]
        return InventoryHealth(
            days=days,
            lead_time_days=settings.lead_time_days,
            cover_days=settings.cover_days,
            overstock_days=settings.overstock_days,
            cash_tied_up=_money(sum((Decimal(r.cash_tied_up) for r in priced), Decimal(0))),
            reorder_cost=_money(sum((Decimal(r.reorder_cost) for r in priced), Decimal(0))),
            untracked=untracked,
            products=rows[:limit],
            truncated=truncated or len(rows) > limit,
        )
```

- [ ] **Step 7: Run, expect pass.** `uv run pytest tests/tests_woo_mcp.py -k inventory_health -q` → 2 passed.

- [ ] **Step 8: Leave for the user to commit.**

---

### Task 2: create_restock_plan and list_restock_orders

**Files:** Modify `services/woo-mcp/app/server.py`; Test `services/woo-mcp/tests/tests_woo_mcp.py`

**Interfaces:**
- Consumes: `restock`, `inventory`, `NoRestockPlugin`, `PlanItem`, `PurchaseOrder`, `RestockOrders`, `STOCK_WINDOW_DAYS`, `_bulk`, `ItemResult`, `Items` (Task 1 and existing code).
- Produces: tools `create_restock_plan(items, note="") -> BulkResult`, `list_restock_orders(status=None, limit=20) -> RestockOrders`. Plugin POST body `{"note": str, "lines": [{"product_id", "name", "qty", "unit_cost"}]}`.

- [ ] **Step 1: Failing tests.**

```python
def test_restock_plan_checks_the_store_and_makes_one_purchase_order():
    def item(pid, name, qty, cost):
        return {"product_id": pid, "name": name, "qty": qty, "unit_cost": cost}

    deo, tea = item(20, "Fresh Deodorant 150ml", 279, "110"), item(25, "Green Tea 100 Bags", 134, "120.00")

    async def run():
        fake = _stock_store()
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                cases = [
                    (item(99, "Gone", 1, "1"), "product not found"),
                    (item(20, "Deo", 1, "110"), "name"),
                    (item(1, "Trail Mix", 1, "100"), "stock tracking is off"),
                    (item(26, "Assorted Mystery Box", 7, "100"), "no cost of goods"),
                    (item(20, "Fresh Deodorant 150ml", 1, "100"), "cost of goods is 110.00, not 100"),
                    (item(23, "Diwali Gift Hamper", 10, "950"), "already overstocked: 1400 days of stock"),
                    (item(24, "Brass Diya Set", 10, "420"), "nothing sold in the last 30 days"),
                ]
                for it, why in cases:
                    res = _payload(await client.call_tool("create_restock_plan", {"items": [it]}))
                    assert res["failed"] == 1 and why in res["results"][0]["error"], (why, res)
                assert not fake.state.purchase_orders  # all refused: no purchase order
                assert (await client.call_tool("create_restock_plan", {"items": [deo, deo]})).is_error

                res = _payload(await client.call_tool("create_restock_plan", {
                    "items": [deo, tea, item(24, "Brass Diya Set", 1, "420")], "note": "₹50k budget"}))
                assert [r["ok"] for r in res["results"]] == [True, True, False]
                assert res["results"][0]["detail"] == "PO #1: 279 x 110.00"
                po = fake.state.purchase_orders[1]
                assert (po["status"], po["total"], po["note"]) == ("ordered", "46770.00", "₹50k budget")
                assert [(l["product_id"], l["qty"], l["unit_cost"]) for l in po["lines"]] == [(20, 279, "110.00"), (25, 134, "120.00")]

                health = {r["product_id"]: r for r in _payload(await client.call_tool("get_inventory_health", {}))["products"]}
                assert (health[20]["on_order"], health[20]["reorder_qty"]) == (279, 0)
                listed = _payload(await client.call_tool("list_restock_orders", {"status": "open"}))
                assert (listed["count"], listed["on_order_value"], listed["orders"][0]["id"]) == (1, "46770.00", 1)
                assert listed["orders"][0]["lines"][0]["remaining"] == 279
                assert _payload(await client.call_tool("list_restock_orders", {"status": "received"}))["orders"] == []

    asyncio.run(run())


def test_purchase_orders_on_a_store_without_the_data():
    async def run():
        fake = build_fake_woo()
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                assert _payload(await client.call_tool("list_restock_orders", {}))["orders"] == []
                plan = _payload(await client.call_tool("create_restock_plan", {
                    "items": [{"product_id": 1, "name": "Trail Mix", "qty": 5, "unit_cost": "100"}]}))
                assert "stock tracking is off" in plan["results"][0]["error"]
                fake.state.flags["no_restock_plugin"] = True
                res = await client.call_tool("list_restock_orders", {})
                assert res.is_error and "no restock plugin yet" in str(res.content)

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect failure.** `uv run pytest tests/tests_woo_mcp.py -k "restock_plan or purchase_orders" -q` → FAIL (unknown tool).

- [ ] **Step 3: Implement** after `get_inventory_health`:

```python
    @server.tool(title="Create restock plan", annotations=WRITE)
    async def create_restock_plan(
        items: Items(PlanItem),
        note: Annotated[str, Field(max_length=500, description="Why, in a sentence; stored on the purchase order")] = "",
    ) -> BulkResult:
        """Order stock: the passing items become one purchase order (status "ordered") in the store.
        Stock does not change until the goods arrive (receive_stock). Every call needs an admin's
        approval: when the gateway refuses it, call gateway__request_approval with the same arguments
        and a `summary` of your analysis (the budget, what you chose and why), then tell the user the
        approval id. Per item: copy product_id, name and cogs (as unit_cost) from get_inventory_health;
        qty is usually its reorder_qty. When it runs, an item whose name or cost does not match the
        store, that has no cost of goods or no stock tracking, or that is overstocked is refused with
        the reason; the others are still ordered."""
        ids = [it.product_id for it in items]
        if len(set(ids)) != len(ids):
            raise ToolError("Each product may appear only once per call")
        by_id, rows, _, _ = await inventory(STOCK_WINDOW_DAYS)
        row_of = {r.product_id: r for r in rows}

        def refusal(it: PlanItem) -> str | None:
            p, r = by_id.get(it.product_id), row_of.get(it.product_id)
            if p is None:
                return "product not found"
            if p.get("name") != it.name:
                return f"the store's name is {p.get('name')!r}, not {it.name!r}"
            if r is None:
                return "stock tracking is off for this product"
            if r.cogs is None:
                return "the product has no cost of goods in the store"
            if Decimal(r.cogs) != Decimal(it.unit_cost):
                return f"the store's cost of goods is {r.cogs}, not {it.unit_cost}"
            if r.status == "overstock":
                if r.days_left is None:
                    return f"already overstocked: nothing sold in the last {STOCK_WINDOW_DAYS} days"
                return f"already overstocked: {r.days_left:g} days of stock"
            return None

        results = {it.product_id: ItemResult(id=it.product_id, ok=False, error=why) for it in items if (why := refusal(it))}
        passing = [it for it in items if it.product_id not in results]
        if passing:
            po = await restock("POST", "/orders", json={
                "note": note,
                "lines": [{"product_id": it.product_id, "name": it.name, "qty": it.qty,
                           "unit_cost": _money(Decimal(it.unit_cost))} for it in passing],
            })
            for it in passing:
                results[it.product_id] = ItemResult(
                    id=it.product_id, ok=True, detail=f"PO #{po['id']}: {it.qty} x {_money(Decimal(it.unit_cost))}")
        return _bulk([results[i] for i in ids])

    @server.tool(title="Purchase orders", annotations=READ)
    async def list_restock_orders(
        status: Annotated[Literal["open", "received"] | None,
                          Field(description="open: goods still to come (ordered or partially_received); "
                                            "received: complete; omit for both")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="How many to return, newest first")] = 20,
    ) -> RestockOrders:
        """Purchase orders made by create_restock_plan, newest first, with status (ordered,
        partially_received, received), each line's units ordered, received and remaining, and the
        cost of what is still on order."""
        rows = [
            PurchaseOrder(**{**po, "lines": [{**l, "remaining": l["qty"] - l["received"]} for l in po["lines"]]})
            for po in (await restock("GET", "/orders", params={"status": status}))[:limit]
        ]
        waiting = sum((Decimal(l.unit_cost) * (l.qty - l.received) for po in rows for l in po.lines), Decimal(0))
        return RestockOrders(count=len(rows), on_order_value=_money(waiting), orders=rows)
```

- [ ] **Step 4: Run, expect pass.** Same command → 2 passed.

- [ ] **Step 5: Leave for the user to commit.**

---

### Task 3: receive_stock

**Files:** Modify `services/woo-mcp/app/server.py`; Test `services/woo-mcp/tests/tests_woo_mcp.py`

**Interfaces:**
- Consumes: `restock`, `ReceiveItem`, `_bulk`, `ItemResult`.
- Produces: tool `receive_stock(po_id, items) -> BulkResult`. Plugin answer `{"results": [{"product_id", "ok", "error"?, "stock"?, "received"?, "qty"?}], "order": PurchaseOrder | null}`.

- [ ] **Step 1: Failing test.**

```python
def test_receive_stock_is_bounded_by_the_purchase_order():
    async def run():
        fake = _stock_store()
        fake.state.purchase_orders[1] = {
            "id": 1, "status": "ordered", "ordered_at": "2026-10-15T10:00:00", "received_at": None, "total": "46770.00", "note": "",
            "lines": [{"product_id": 20, "name": "Fresh Deodorant 150ml", "qty": 279, "unit_cost": "110.00", "received": 0},
                      {"product_id": 25, "name": "Green Tea 100 Bags", "qty": 134, "unit_cost": "120.00", "received": 0}]}
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                async def receive(po_id, *lines):
                    return _payload(await client.call_tool("receive_stock", {
                        "po_id": po_id, "items": [{"product_id": p, "qty": q} for p, q in lines]}))

                res = await receive(1, (20, 300), (21, 1))
                assert [r["error"] for r in res["results"]] == [
                    "only 279 left to receive on PO #1 (279 ordered, 0 received)", "product 21 is not on PO #1"]
                assert fake.state.products[20]["stock_quantity"] == 54

                res = await receive(1, (20, 200))
                assert res["results"][0]["detail"] == "stock now 254 (200 of 279 received)"
                assert fake.state.purchase_orders[1]["status"] == "partially_received"
                listed = _payload(await client.call_tool("list_restock_orders", {"status": "open"}))
                assert listed["on_order_value"] == "24770.00"  # 79 x 110 + 134 x 120
                assert [l["remaining"] for l in listed["orders"][0]["lines"]] == [79, 134]
                # Partly delivered still counts as on order: no second reorder suggested for it.
                health = {r["product_id"]: r for r in _payload(await client.call_tool("get_inventory_health", {}))["products"]}
                assert health[20]["on_order"] == 79
                # The second delivery sees the first: 80 more would be one too many.
                assert "only 79 left" in (await receive(1, (20, 80)))["results"][0]["error"]

                res = await receive(1, (20, 79), (25, 134))
                assert res["ok"] == 2
                po = fake.state.purchase_orders[1]
                assert (po["status"], fake.state.products[20]["stock_quantity"], fake.state.products[25]["stock_quantity"]) == (
                    "received", 333, 134)
                assert (await receive(1, (20, 1)))["results"][0]["error"] == "PO #1 is already fully received"
                assert (await receive(9, (20, 1)))["results"][0]["error"] == "PO #9 not found"
                done = _payload(await client.call_tool("list_restock_orders", {"status": "received"}))
                assert (done["count"], done["on_order_value"]) == (1, "0.00")

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect failure.** `uv run pytest tests/tests_woo_mcp.py -k receive_stock -q` → FAIL.

- [ ] **Step 3: Implement** after `list_restock_orders`:

```python
    @server.tool(title="Receive stock", annotations=WRITE)
    async def receive_stock(
        po_id: Annotated[int, Field(ge=1, description="Purchase order id, from create_restock_plan or list_restock_orders")],
        items: Items(ReceiveItem),
    ) -> BulkResult:
        """Record goods that arrived on a purchase order: adds each item's qty to the product's stock.
        No approval needed, but an item is refused when it is not on the order or when qty is more
        than ordered minus already received. Partial deliveries are fine: the order becomes
        "partially_received", then "received" when every line is in."""
        out = await restock("POST", f"/orders/{po_id}/receive", json={"items": [it.model_dump() for it in items]})
        return _bulk([
            ItemResult(id=r["product_id"], ok=True, detail=f"stock now {r['stock']} ({r['received']} of {r['qty']} received)")
            if r["ok"] else ItemResult(id=r["product_id"], ok=False, error=r["error"])
            for r in out["results"]
        ])
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Leave for the user to commit.**

---

### Task 4: Clearance offers, shared offer checks, tool list

**Files:** Modify `services/woo-mcp/app/server.py`; Test `services/woo-mcp/tests/tests_woo_mcp.py`

**Interfaces:**
- Consumes: `inventory`, `STOCK_WINDOW_DAYS`, existing `products_by_id`, `cogs_of`, `floor_of`, `batch`, `_bulk`.
- Produces: `offer_refusal(it: OfferItem, p: dict | None) -> str | None`, `put_on_sale(items, not_eligible: Callable[[OfferItem], str | None]) -> BulkResult`, tool `create_clearance_offers(items) -> BulkResult`.

- [ ] **Step 1: Failing tests.** Rename `test_the_eighteen_tools_are_listed_in_both_protocol_eras` to `test_the_twenty_three_tools_are_listed_in_both_protocol_eras` and add to `expected`:

```python
        "get_inventory_health",
        "create_restock_plan",
        "receive_stock",
        "list_restock_orders",
        "create_clearance_offers",
```

Add:

```python
def test_clearance_offers_only_for_overstock():
    def offer(pid, name, price, cogs, pct, sale, days=7):
        return {"product_id": pid, "name": name, "regular_price": price, "cogs": cogs,
                "discount_pct": pct, "sale_price": sale, "ends_in_days": days}

    hamper = (23, "Diwali Gift Hamper", "1499", "950")

    async def run():
        fake = _stock_store()
        with serve(fake) as store, serve(create_app(settings_for(store))) as url:
            async with mcp_client(url) as client:
                for item, why in [
                    (offer(20, "Fresh Deodorant 150ml", "249", "110", 20, "199.20"), "not overstocked: it is at risk"),
                    (offer(*hamper, 35, "974.35"), "under the floor 1045.00"),
                    (offer(*hamper, 25, "1124.25", days=15), "ends_in_days must be 1 to 14"),
                ]:
                    res = _payload(await client.call_tool("create_clearance_offers", {"items": [item]}))
                    assert res["failed"] == 1 and why in res["results"][0]["error"], (why, res)
                res = _payload(await client.call_tool("create_clearance_offers", {"items": [
                    offer(*hamper, 25, "1124.25", days=14), offer(24, "Brass Diya Set", "899", "420", 40, "539.40")]}))
                assert res["ok"] == 2 and res["results"][0]["detail"].startswith("on sale at 1124.25 until ")
                assert fake.state.products[23]["sale_price"] == "1124.25" and fake.state.products[24]["on_sale"]
                again = _payload(await client.call_tool("create_clearance_offers", {"items": [offer(*hamper, 25, "1124.25")]}))
                assert "already on sale" in again["results"][0]["error"]

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect failure.** `uv run pytest tests/tests_woo_mcp.py -k "clearance or twenty_three" -q` → FAIL.

- [ ] **Step 3: Refactor the cart offer checks.** In `build_server`, replace the whole body of `create_cart_offers` (from `ids = [it.product_id for it in items]` to `return _bulk([results[i] for i in ids])`) with:

```python
        window = settings.cart_window_days
        in_carts = {
            line["product_id"]
            for cart in await call("GET", "/cart-recovery/carts", params={"days": window})
            for line in cart.get("items") or []
        }
        return await put_on_sale(
            items, lambda it: None if it.product_id in in_carts else f"no cart changed in the last {window} days holds it"
        )
```

and define, just above `@server.tool(title="Create cart recovery offers", ...)`:

```python
    def offer_refusal(it: OfferItem, p: dict | None) -> str | None:
        """The checks every sale offer passes: its numbers match the store and the price stays above the floor."""
        if p is None:
            return "product not found"
        price, cost, sale = Decimal(p.get("regular_price") or "0"), cogs_of(p), Decimal(it.sale_price)
        if p.get("name") != it.name:
            return f"the store's name is {p.get('name')!r}, not {it.name!r}"
        if price != Decimal(it.regular_price):
            return f"the store's regular price is {_money(price)}, not {it.regular_price}"
        if cost is None:
            return "the product has no cost of goods in the store, so no price floor can be checked"
        if cost != Decimal(it.cogs):
            return f"the store's cost of goods is {_money(cost)}, not {it.cogs}"
        if p.get("on_sale"):
            return "already on sale; discounts are not stacked"
        if it.ends_in_days > settings.max_offer_days:
            return f"ends_in_days must be 1 to {settings.max_offer_days}"
        if sale >= price:
            return "sale_price must be below the regular price"
        floor = floor_of(cost)
        if sale < floor:
            return f"sale_price is under the floor {floor.quantize(Decimal('0.01'), ROUND_CEILING)} (cost + {min_margin * 100:g}%)"
        actual = (1 - sale / price) * 100
        if actual > settings.max_discount_pct:
            return f"a {actual:.1f}% discount is over the store's limit of {settings.max_discount_pct}%"
        if abs(actual - it.discount_pct) > 1:
            return f"sale_price {it.sale_price} is a {actual:.1f}% discount, not {it.discount_pct}%"
        return None

    async def put_on_sale(items: list[OfferItem], not_eligible: Callable[[OfferItem], str | None]) -> BulkResult:
        """Each item that passes offer_refusal and not_eligible goes on sale until now + ends_in_days; the rest say why."""
        ids = [it.product_id for it in items]
        if len(set(ids)) != len(ids):
            raise ToolError("Each product may appear only once per call")
        store = await products_by_id(set(ids))
        results: dict[int, ItemResult] = {}
        passing = []
        for it in items:
            why = offer_refusal(it, store.get(it.product_id)) or not_eligible(it)
            if why:
                results[it.product_id] = ItemResult(id=it.product_id, ok=False, error=why)
            else:
                passing.append(it)
        if passing:
            now = datetime.now(timezone.utc)
            ends = {it.product_id: (now + timedelta(days=it.ends_in_days)).strftime("%Y-%m-%dT%H:%M:%S") for it in passing}
            sales = {it.product_id: it.sale_price for it in passing}
            done = await batch(
                "/products",
                [it.product_id for it in passing],
                # No start date: the sale starts at once. WooCommerce ends it on its own.
                [{"id": it.product_id, "sale_price": it.sale_price, "date_on_sale_to_gmt": ends[it.product_id]}
                 for it in passing],
            )
            for r in done.results:
                if r.ok:
                    r.detail = f"on sale at {sales[r.id]} until {ends[r.id]} UTC"
                results[r.id] = r
        return _bulk([results[i] for i in ids])
```

Add `from collections.abc import Callable` to the imports. The old order put the cart check before `ends_in_days`; it now runs last, which only changes which reason wins when an item has two problems.

- [ ] **Step 4: Run the existing cart tests, expect pass unchanged.** `uv run pytest tests/tests_woo_mcp.py -k "cart" -q` → passed.

- [ ] **Step 5: Clearance tool.** After `receive_stock`:

```python
    @server.tool(title="Create clearance offers", annotations=WRITE)
    async def create_clearance_offers(items: Items(OfferItem)) -> BulkResult:
        """Put overstocked products on sale for a few days to turn stock back into cash; the sale ends
        by itself. Every call needs an admin's approval: when the gateway refuses it, call
        gateway__request_approval with the same arguments and a `summary` of your analysis, then tell
        the user the approval id. Per item: copy product_id, name, regular_price and cogs from
        get_inventory_health (rows with status overstock); pick discount_pct <= max_discount_pct;
        sale_price = regular_price x (1 - discount_pct/100), rounded UP to 2 decimals. When it runs,
        every number is checked against the store, and an item that does not match, is under the cost
        floor, is already on sale or is not overstocked now is refused with the reason; the others
        still go on sale."""
        _, rows, _, _ = await inventory(STOCK_WINDOW_DAYS)
        row_of = {r.product_id: r for r in rows}

        def not_overstocked(it: OfferItem) -> str | None:
            r = row_of.get(it.product_id)
            if r is None:
                return "stock tracking is off for this product"
            if r.status != "overstock":
                return f"not overstocked: it is {r.status.replace('_', ' ')}"
            return None

        return await put_on_sale(items, not_overstocked)
```

Update the module docstring's first lines and the server `instructions` to mention restock planning:

```python
"""The MCP tools: six things an agent may do to the store, six reports, four bulk writes,
cart recovery (abandoned-cart facts, and sale offers that need an admin's approval), and
restock planning (stock facts, purchase orders, receiving stock, clearance offers).
```

```python
            "customer history), cart recovery (abandoned carts and sale offers), and "
            "restock planning (inventory health, purchase orders, receiving stock, clearance sales). "
```

- [ ] **Step 6: Full suite.** `uv run python -m tests.tests_woo_mcp` → every test PASS.

- [ ] **Step 7: Leave for the user to commit.**

---

### Task 5: Store plugin `restock.php`, shipped to every store

**Files:**
- Create: `charts/store/files/restock.php`
- Modify: `charts/store/files/setup-woo.sh`, `charts/store/templates/setup-job.yaml`

**Interfaces:**
- Produces: routes and JSON shapes the fake store in Task 1 mirrors (same messages), `wp restock refresh`, post type `restock_po`.

- [ ] **Step 1: Write the plugin.**

```php
<?php
/**
 * Plugin Name: Restock
 * Description: Purchase orders for woo-mcp's restock tools, and a refresh command for the synthetic sales history.
 *
 * Must-use plugin (copied to wp-content/mu-plugins on every store by setup-woo.sh).
 * WooCommerce has no purchase orders; this keeps them as a private post type,
 * listed read-only under WooCommerce > Purchase orders, and serves them over the
 * WooCommerce REST API (wc/v3, so WooCommerce's API keys authenticate it):
 *
 *   GET  /wp-json/wc/v3/restock/orders?status=open|received   (open: ordered or partially_received)
 *   POST /wp-json/wc/v3/restock/orders               {lines: [{product_id, name, qty, unit_cost}], note}
 *   POST /wp-json/wc/v3/restock/orders/<id>/receive  {items: [{product_id, qty}]}
 *
 * woo-mcp checks a plan against the store before it creates an order. Receiving
 * is checked here, under a lock, because it changes stock.
 *
 *   wp restock refresh
 *     re-dates the synthetic history orders (seed.php) so the sales window has data again.
 */

defined( 'ABSPATH' ) || exit;

const RESTOCK_PO = 'restock_po';

add_action(
	'init',
	function () {
		register_post_type(
			RESTOCK_PO,
			array(
				'label'        => 'Purchase orders',
				'public'       => false,
				'show_ui'      => true,
				'show_in_menu' => 'woocommerce',
				'supports'     => array( 'title' ),
				'map_meta_cap' => true,
				'capabilities' => array(
					'create_posts' => 'do_not_allow', // no "Add new": orders come from the API only
					'edit_posts'   => 'manage_woocommerce', // who sees the list
				),
			)
		);
	}
);

// Read-only in WP admin: nobody edits, trashes or deletes a purchase order by hand.
add_filter(
	'map_meta_cap',
	function ( $caps, $cap, $user_id, $args ) {
		if ( in_array( $cap, array( 'edit_post', 'delete_post' ), true ) && ! empty( $args[0] ) && RESTOCK_PO === get_post_type( $args[0] ) ) {
			return array( 'do_not_allow' );
		}
		return $caps;
	},
	10,
	4
);

add_filter(
	'manage_' . RESTOCK_PO . '_posts_columns',
	fn() => array(
		'title'     => 'Order',
		'po_status' => 'Status',
		'po_total'  => 'Total',
		'date'      => 'Ordered',
	)
);
add_action(
	'manage_' . RESTOCK_PO . '_posts_custom_column',
	function ( $column, $post_id ) {
		if ( 'po_status' === $column ) {
			echo esc_html( get_post_meta( $post_id, '_status', true ) );
		} elseif ( 'po_total' === $column ) {
			echo wp_kses_post( wc_price( get_post_meta( $post_id, '_total', true ) ) );
		}
	},
	10,
	2
);

function restock_po_json( WP_Post $post ): array {
	$id = $post->ID;
	return array(
		'id'          => $id,
		'status'      => get_post_meta( $id, '_status', true ),
		'ordered_at'  => get_post_time( 'Y-m-d\TH:i:s', true, $post ),
		'received_at' => get_post_meta( $id, '_received_at', true ) ?: null,
		'total'       => get_post_meta( $id, '_total', true ),
		'note'        => get_post_meta( $id, '_note', true ),
		'lines'       => get_post_meta( $id, '_lines', true ) ?: array(),
	);
}

function restock_create( WP_REST_Request $request ) {
	$lines = array();
	$total = 0.0;
	foreach ( $request['lines'] as $line ) {
		$lines[] = array(
			'product_id' => (int) $line['product_id'],
			'name'       => sanitize_text_field( $line['name'] ),
			'qty'        => (int) $line['qty'],
			'unit_cost'  => $line['unit_cost'],
			'received'   => 0,
		);
		$total += $line['qty'] * (float) $line['unit_cost'];
	}
	$id = wp_insert_post(
		array(
			'post_type'   => RESTOCK_PO,
			'post_status' => 'publish',
			'post_title'  => 'Purchase order',
			'post_author' => get_current_user_id(), // the WooCommerce key's user: who ordered
			'meta_input'  => array(
				'_status' => 'ordered',
				'_lines'  => $lines,
				'_total'  => number_format( $total, 2, '.', '' ),
				'_note'   => sanitize_text_field( $request['note'] ),
			),
		),
		true
	);
	if ( is_wp_error( $id ) ) {
		return $id;
	}
	wp_update_post( array( 'ID' => $id, 'post_title' => "PO #$id" ) );
	return new WP_REST_Response( restock_po_json( get_post( $id ) ), 201 );
}

/** Adds arrived goods to stock. One receive per order at a time, so the check and the write cannot interleave. */
function restock_receive( WP_REST_Request $request ) {
	global $wpdb;
	$id   = (int) $request['id'];
	$lock = "restock_po_$id";
	if ( ! $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 5)', $lock ) ) ) {
		return new WP_Error( 'restock_po_busy', "PO #$id is busy, try again", array( 'status' => 409 ) );
	}
	try {
		wp_cache_delete( $id, 'post_meta' ); // read what the previous lock holder wrote
		$post    = get_post( $id );
		$found   = $post && RESTOCK_PO === $post->post_type;
		$status  = $found ? get_post_meta( $id, '_status', true ) : '';
		$lines   = $found ? ( get_post_meta( $id, '_lines', true ) ?: array() ) : array();
		$results = array();
		foreach ( $request['items'] as $item ) {
			$pid  = (int) $item['product_id'];
			$qty  = (int) $item['qty'];
			$fail = fn( $why ) => array( 'product_id' => $pid, 'ok' => false, 'error' => $why );
			$i    = array_search( $pid, array_column( $lines, 'product_id' ), true );
			if ( ! $found ) {
				$results[] = $fail( "PO #$id not found" );
				continue;
			}
			if ( 'received' === $status ) {
				$results[] = $fail( "PO #$id is already fully received" );
				continue;
			}
			if ( false === $i ) {
				$results[] = $fail( "product $pid is not on PO #$id" );
				continue;
			}
			$left = $lines[ $i ]['qty'] - $lines[ $i ]['received'];
			if ( $qty > $left ) {
				$results[] = $fail( "only $left left to receive on PO #$id ({$lines[ $i ]['qty']} ordered, {$lines[ $i ]['received']} received)" );
				continue;
			}
			$product = wc_get_product( $pid );
			if ( ! $product || ! $product->managing_stock() ) {
				$results[] = $fail( $product ? 'stock tracking is off for this product' : 'product not found' );
				continue;
			}
			// shortcut: stock and the order's lines are two writes; a crash between them lets the same units be received again. Move both into one DB transaction if receiving ever runs unattended.
			$stock                    = wc_update_product_stock( $product, $qty, 'increase' );
			$lines[ $i ]['received'] += $qty;
			$results[]                = array( 'product_id' => $pid, 'ok' => true, 'stock' => (int) $stock, 'received' => $lines[ $i ]['received'], 'qty' => $lines[ $i ]['qty'] );
		}
		if ( $found && 'received' !== $status ) {
			update_post_meta( $id, '_lines', $lines );
			if ( ! array_filter( $lines, fn( $l ) => $l['received'] < $l['qty'] ) ) {
				update_post_meta( $id, '_status', 'received' );
				update_post_meta( $id, '_received_at', gmdate( 'Y-m-d\TH:i:s' ) );
			} elseif ( array_filter( $lines, fn( $l ) => $l['received'] > 0 ) ) {
				update_post_meta( $id, '_status', 'partially_received' );
			}
		}
		return array(
			'results' => $results,
			'order'   => $found ? restock_po_json( get_post( $id ) ) : null,
		);
	} finally {
		$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
	}
}

add_action(
	'rest_api_init',
	function () {
		$can = fn() => current_user_can( 'manage_woocommerce' );
		register_rest_route(
			'wc/v3',
			'/restock/orders',
			array(
				array(
					'methods'             => 'GET',
					'permission_callback' => $can,
					'args'                => array(
						'status' => array(
							'type' => 'string',
							'enum' => array( 'open', 'received' ),
						),
					),
					'callback'            => function ( WP_REST_Request $request ) {
						$query = array(
							'post_type'   => RESTOCK_PO,
							'post_status' => 'publish',
							'numberposts' => -1,
							'orderby'     => 'ID',
							'order'       => 'DESC',
						);
						if ( $request['status'] ) {
							$query['meta_query'] = array(
								array(
									'key'     => '_status',
									'value'   => 'open' === $request['status'] ? array( 'ordered', 'partially_received' ) : array( 'received' ),
									'compare' => 'IN',
								),
							);
						}
						return array_map( 'restock_po_json', get_posts( $query ) );
					},
				),
				array(
					'methods'             => 'POST',
					'permission_callback' => $can,
					'args'                => array(
						'lines' => array(
							'type'     => 'array',
							'required' => true,
							'minItems' => 1,
							'items'    => array(
								'type'       => 'object',
								'properties' => array(
									'product_id' => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
									'name'       => array( 'type' => 'string', 'required' => true ),
									'qty'        => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
									'unit_cost'  => array( 'type' => 'string', 'pattern' => '^\d+(\.\d{1,2})?$', 'required' => true ),
								),
							),
						),
						'note'  => array(
							'type'    => 'string',
							'default' => '',
						),
					),
					'callback'            => 'restock_create',
				),
			)
		);
		register_rest_route(
			'wc/v3',
			'/restock/orders/(?P<id>\d+)/receive',
			array(
				'methods'             => 'POST',
				'permission_callback' => $can,
				'args'                => array(
					'items' => array(
						'type'     => 'array',
						'required' => true,
						'minItems' => 1,
						'items'    => array(
							'type'       => 'object',
							'properties' => array(
								'product_id' => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
								'qty'        => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
							),
						),
					),
				),
				'callback'            => 'restock_receive',
			)
		);
	}
);

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	WP_CLI::add_command(
		'restock refresh',
		function () {
			$ids = wc_get_orders(
				array(
					'limit'      => -1,
					'return'     => 'ids',
					'meta_query' => array( array( 'key' => '_synthetic_age', 'compare' => 'EXISTS' ) ),
				)
			);
			foreach ( $ids as $id ) {
				$order = wc_get_order( $id );
				$at    = time() - (int) $order->get_meta( '_synthetic_age' );
				$order->set_date_created( $at );
				if ( $order->get_date_paid() ) {
					$order->set_date_paid( $at );
				}
				if ( $order->get_date_completed() ) {
					$order->set_date_completed( $at );
				}
				$order->save();
			}
			WP_CLI::success( count( $ids ) . ' synthetic orders re-dated' );
		}
	);
}
```

- [ ] **Step 2: Ship it on every store.** In `setup-woo.sh`, replace the two lines

```bash
# Abandoned carts for woo-mcp (see the file). Before seeding, which uses it.
mkdir -p /app/wp-content/mu-plugins
cp /setup/cart-recovery.php /app/wp-content/mu-plugins/cart-recovery.php
```

with

```bash
# Our must-use plugins, on every store whatever the template (see each file):
# abandoned carts and purchase orders for woo-mcp. Before seeding, which uses them.
mkdir -p /app/wp-content/mu-plugins
cp /setup/cart-recovery.php /setup/restock.php /app/wp-content/mu-plugins/
```

In `setup-job.yaml`, after the `cart-recovery.php` entry:

```yaml
  restock.php: |-
{{ .Files.Get "files/restock.php" | indent 4 }}
```

- [ ] **Step 3: Lint and render.**

```bash
docker run --rm -v "$PWD/charts/store/files:/f:ro" php:8.3-cli php -l /f/restock.php
helm template t charts/store | grep -c "restock.php"
```

Expected: `No syntax errors detected`; count ≥ 2 (ConfigMap key + copy line).

- [ ] **Step 4: Leave for the user to commit.**

---

### Task 6: Catalog A rebuilt + seed timing on a throwaway store

**Files:**
- Modify: `charts/store/files/seed.php`
- Modify: `charts/store/values.yaml:12`, `services/dashboard/src/Stores.jsx:5`, `README.md:369`, `context.md:399,411`

**Interfaces:**
- Consumes: `restock.php` (Task 5) for `wp restock refresh`, meta `_synthetic_age`.

- [ ] **Step 1: Seed docblock.** Replace the `catalog-a:` paragraph in the header comment with:

```php
 * catalog-a: the restock planning demo. 40 everyday products with a cost of
 * goods and tracked stock, and 90 days of sales (about 1,500 guest orders)
 * made from each product's units-per-day rate, so the restock tools see a
 * stockout hero (Fresh Deodorant 150ml, 6 days left), a thin-margin fast mover,
 * a sold-out product, overstock and dead stock. History orders carry
 * _synthetic_age for `wp restock refresh` (mu-plugin restock.php). Same fixed
 * seed, same history in every store.
```

and change the first line's "300 of them" to "hundreds of them", and "Then a few orders created now" to "Then a few orders created now (catalog-a: none, its history reaches today)".

- [ ] **Step 2: Catalog A data.** Replace the whole `'catalog-a' => array( ... ),` entry with:

```php
	// Restock planning demo: name, price, cost of goods (null: unknown), stock, units sold per day,
	// and, for a sold-out product, how many days ago its sales stopped.
	'catalog-a' => array(
		'seed'           => 101,
		'orders'         => 0, // the history reaches today
		'history_days'   => 90,
		'stock_products' => array(
			'Personal Care' => array(
				array( 'Fresh Deodorant 150ml', 249, 110, 54, 9 ), array( 'Herbal Shampoo 400ml', 399, 170, 30, 2 ),
				array( 'Mint Toothpaste 150g', 129, 62, 270, 6 ), array( 'Neem Soap 4-pack', 199, 120, 180, 4 ),
				array( 'Aloe Face Wash 100ml', 249, 95, 135, 3 ), array( 'Coconut Hair Oil 300ml', 189, 105, 150, 3 ),
				array( 'Sunscreen SPF 50', 499, 210, 45, 1 ), array( 'Shaving Foam 200ml', 220, 120, 70, 1.5 ),
			),
			'Snacks'        => array(
				array( 'Instant Noodles 12-pack', 168, 150, 40, 8 ), array( 'Salted Peanuts 200g', 99, 58, 25, 3 ),
				array( 'Masala Chips 52g', 20, 11, 400, 5 ), array( 'Roasted Makhana 100g', 149, 80, 90, 2 ),
				array( 'Digestive Biscuits 250g', 60, 42, 270, 6 ), array( 'Dark Chocolate 70%', 249, 130, 50, 1 ),
				array( 'Trail Mix 200g', 299, 170, 45, 1 ), array( 'Banana Chips 200g', 110, 60, 90, 2 ),
			),
			'Beverages'     => array(
				array( 'Green Tea 100 Bags', 349, 120, 0, 4, 3 ), array( 'Filter Coffee 500g', 450, 260, 120, 2.5 ),
				array( 'Masala Chai 250g', 220, 110, 135, 3 ), array( 'Mango Juice 1L', 120, 78, 225, 5 ),
				array( 'Lemon Soda 6-pack', 180, 110, 135, 3 ), array( 'Coconut Water 1L', 150, 95, 90, 2 ),
				array( 'Hot Chocolate Mix 400g', 399, 230, 40, 0.75 ), array( 'Kombucha 330ml', 199, 120, 25, 0.5 ),
			),
			'Household'     => array(
				array( 'Detergent Powder 1kg', 135, 118, 200, 5 ), array( 'Dishwash Gel 500ml', 110, 62, 180, 4 ),
				array( 'Floor Cleaner 1L', 199, 105, 80, 2 ), array( 'Garbage Bags x30', 99, 45, 135, 3 ),
				array( 'Toilet Rolls 10-pack', 399, 250, 70, 1.5 ), array( 'Kitchen Towels 2-roll', 149, 80, 90, 2 ),
				array( 'Steel Scrub Pack', 49, 18, 160, 4 ), array( 'Mosquito Repellent Refill', 89, 48, 135, 3 ),
			),
			'Festive'       => array(
				array( 'Diwali Gift Hamper', 1499, 950, 140, 0.1 ), array( 'Brass Diya Set', 899, 420, 60, 0 ),
				array( 'Scented Candle Set', 599, 260, 80, 0.5 ), array( 'Dry Fruit Box 500g', 899, 640, 25, 1 ),
				array( 'Assorted Mystery Box', 499, null, 30, 1 ), array( 'Rangoli Colour Kit', 299, 120, 45, 1 ),
				array( 'Festive Torans', 249, 90, 40, 1 ), array( 'Gift Wrap Roll', 99, 35, 90, 2 ),
			),
		),
	),
```

- [ ] **Step 3: Products and history.** Add `$stocked = array();` after `$weights  = array();`. Delete the `foreach ( $catalog['categories'] ?? array() ... ) { ... }` loop (only the old Catalog A used it) and put in its place:

```php
	foreach ( $catalog['stock_products'] ?? array() as $category => $rows ) {
		$term = term_exists( $category, 'product_cat' ) ?: wp_insert_term( $category, 'product_cat' );
		foreach ( $rows as $row ) {
			list( $name, $price, $cogs, $stock, $per_day ) = $row;
			$id             = seed_product( $name, $price, (int) $term['term_id'], $cogs );
			$ids[]          = $id;
			$stocked[ $id ] = array( $stock, $per_day, $row[5] ?? 0 );
		}
	}
```

After `function seed_product(...) {...}` add:

```php
function seed_history_order( $n, $products, $lines, $age, $status ) {
	$at    = time() - $age;
	$order = wc_create_order();
	foreach ( $lines as $id => $qty ) {
		$order->add_product( $products[ $id ], $qty );
	}
	$order->set_address( array( 'first_name' => 'Demo', 'last_name' => "Customer $n", 'email' => "customer$n@example.com" ), 'billing' );
	$order->calculate_totals();
	$order->set_status( $status );
	$order->set_date_created( $at );
	if ( 'completed' === $status ) {
		$order->set_date_paid( $at );
		$order->set_date_completed( $at );
	}
	$order->update_meta_data( '_synthetic_age', $age ); // wp restock refresh re-dates from this
	$order->save();
}
```

After the "orders created now" `for` loop, add:

```php
// Sales history: unit k of a product sold k / per_day days ago (counted from when it sold
// out, if it did), so any window gives the stated rate. Units close in time share an order.
if ( $stocked ) {
	add_filter( 'pre_wp_mail', '__return_false' ); // no "order completed" mail to example.com
	mt_srand( $catalog['seed'] + 2 );
	$horizon = $catalog['history_days'] * DAY_IN_SECONDS;
	$units   = array();
	foreach ( $stocked as $id => list( , $per_day, $stopped ) ) {
		for ( $k = 0; $per_day > 0 && ( $age = (int) ( ( $stopped + $k / $per_day ) * DAY_IN_SECONDS ) ) < $horizon; $k++ ) {
			$units[] = array( $age, $id );
		}
	}
	sort( $units );
	$products = array();
	foreach ( array_keys( $stocked ) as $id ) {
		$products[ $id ] = wc_get_product( $id );
	}
	$n = 0;
	for ( $i = 0; $i < count( $units ); $i += $size ) {
		$size  = mt_rand( 1, 12 );
		$lines = array_count_values( array_column( array_slice( $units, $i, $size ), 1 ) );
		seed_history_order( ++$n, $products, $lines, $units[ $i ][0], 'completed' );
		if ( mt_rand( 1, 100 ) <= 3 ) { // now and then a cancelled order: not a sale, so the rates hold
			seed_history_order( ++$n, $products, $lines, $units[ $i ][0], 'cancelled' );
		}
	}
	// Stock last: with tracking on, WooCommerce would have taken the history's sales out of it.
	foreach ( $stocked as $id => list( $stock ) ) {
		$products[ $id ]->set_manage_stock( true );
		$products[ $id ]->set_stock_quantity( $stock );
		$products[ $id ]->save();
	}
	WP_CLI::log( "$n history orders over {$catalog['history_days']} days" );
}
```

Lint: `docker run --rm -v "$PWD/charts/store/files:/f:ro" php:8.3-cli php -l /f/seed.php` → no syntax errors.

- [ ] **Step 4: Throwaway store, time the seed.** In cluster `store-check` (never recreate it), a separate namespace:

```bash
kubectl create namespace restock-check
kubectl -n restock-check create secret generic woo-mcp-credentials \
  --from-literal=WOO_CONSUMER_KEY=ck_$(openssl rand -hex 20) --from-literal=WOO_CONSUMER_SECRET=cs_$(openssl rand -hex 20)
time helm install restock-check charts/store -n restock-check \
  --set setup.template=catalog-a --set wordpress.ingress.hostname=restock-check.localhost --wait --timeout 20m
kubectl -n restock-check logs job/restock-check-store-setup | tail -5
```

Expected: the log shows `N history orders over 90 days` with N ≈ 1,500 and `store setup done`; the Job finishes well inside its 900 s deadline. Note the Job's duration (`kubectl -n restock-check get job -o wide`). If over ~600 s, change `mt_rand( 1, 12 )` to `mt_rand( 4, 16 )` (fewer orders, same rates) and repeat after `helm uninstall` + deleting the namespace's PVCs.

- [ ] **Step 5: Check the numbers in the store.**

```bash
POD=$(kubectl -n restock-check get pod -l app.kubernetes.io/name=wordpress -o name | head -1)
kubectl -n restock-check exec $POD -- wp --path=/opt/bitnami/wordpress eval '
  foreach (["Fresh Deodorant 150ml","Green Tea 100 Bags","Diwali Gift Hamper"] as $n) {
    $p = wc_get_product(get_page_by_title($n, OBJECT, "product")->ID);
    echo $n, " stock=", $p->get_stock_quantity(), " cogs=", $p->get_cogs_value(), "\n"; }
  echo count(wc_get_orders(["limit"=>-1,"return"=>"ids","date_created"=>">".(time()-30*DAY_IN_SECONDS),"status"=>"completed"])), " completed orders in 30 days\n";'
kubectl -n restock-check exec $POD -- wp --path=/opt/bitnami/wordpress restock refresh
```

Expected: deodorant stock 54 cogs 110, green tea stock 0, hamper stock 140; about 500 completed orders in 30 days; refresh prints `… synthetic orders re-dated`.

- [ ] **Step 6: Remove the throwaway store.** `helm uninstall restock-check -n restock-check && kubectl delete namespace restock-check`.

- [ ] **Step 7: Text.**
  - `charts/store/values.yaml` line 12 → `# catalog-a: restock demo (40 products, 90 days of sales); catalog-b: cart recovery demo (40 products, 300 shoppers); blank: the list below.`
  - `Stores.jsx` line 5 → `{ id: 'catalog-a', title: 'Catalog A', text: '40 everyday products with stock and 90 days of sales: restock planning demo' },`
  - `README.md:369`, `context.md:399` and `context.md:411`: replace Catalog A's "300 products …" with "40 everyday products, 90 days of sales history (restock demo)"; keep the rest of each line.

- [ ] **Step 8: Leave for the user to commit.**

---

### Task 7: Team alerts: tables, rules API, trigger

**Files:**
- Modify: `services/gateway/app/db.py` (SCHEMA, before its closing `"""`)
- Modify: `services/gateway/app/teams.py`
- Create: `services/gateway/app/alerts.py`
- Modify: `services/gateway/app/upstream.py` (end of `call_tool`)
- Test: `services/gateway/tests/test_alerts.py`

**Interfaces:**
- Produces: tables `team_alert_rules`, `team_alerts`; `PUT /api/teams/{id}/gateways/{slug}/alert-rules` body `{"rules": [{"server_id", "tool"}]}`; team views gain `alert_rules: {slug: [{server_id, server, tool}]}`; `alerts.fire(conn, *, user, gateway_slug, server_id, tool, title, arguments, result, approval_id, audit_id) -> None`; `alerts._lines(result, arguments) -> list[str] | None`.
- Test helper produced: `AlertWorld` in `tests/test_alerts.py` (control plane `w.gw`, edge `w.edge`, `w.admin`, members `w.priya` (finance), `w.ravi` (store-operators), `w.omar` (support), their ids `w.priya_id` …, API clients `w.api(key)`, `w.sid`, team ids `w.finance`, `w.ops`, `w.support`, `w.call(key, name, args)`, `w.alerts()` rows).

- [ ] **Step 1: Failing tests.** Create `services/gateway/tests/test_alerts.py`:

```python
"""Team alerts: a rule on a team, a successful call by anyone, one alert the team sees."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from tests import KEY
from app import db as store
from app.main import Settings, create_app
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, add_team, edge_settings, gateway_client, http_app, serve, setup_admin, text,
    wait_tools,
)
from tests.test_identity import member

PLAN, RECEIVE, CRASH = "woo__create_restock_plan", "woo__receive_stock", "woo__crash"


class Result(BaseModel):
    id: int
    ok: bool
    error: str | None = None
    detail: str | None = None


class Bulk(BaseModel):
    ok: int
    failed: int
    results: list[Result]


def build_woo() -> MCPServer:
    """create_restock_plan answers like woo-mcp's bulk tools; an item with "bad" is refused."""
    server = MCPServer("woo")

    @server.tool(name="create_restock_plan", title="Create restock plan")
    async def plan(items: list[dict]) -> Bulk:
        results = [Result(id=i["product_id"], ok=False, error="refused") if i.get("bad") else
                   Result(id=i["product_id"], ok=True, detail=f"PO #7: {i['qty']} x 110.00 {i.get('note', '')}".strip())
                   for i in items]
        return Bulk(ok=sum(r.ok for r in results), failed=sum(not r.ok for r in results), results=results)

    @server.tool(name="receive_stock")
    async def receive(po_id: int) -> str:
        return f"received PO #{po_id}"

    @server.tool(name="crash")
    async def crash() -> str:
        raise ValueError("store down")

    return server


def plan(*items) -> dict:
    return {"items": [{"product_id": pid, "name": name, "qty": qty, **extra} for pid, name, qty, extra in items]}


class AlertWorld:
    """Control plane, the woo upstream and one edge. finance (priya) may plan; store-operators
    (ravi) and support (omar) may only receive. store-operators is alerted when a plan succeeds."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    async def __aenter__(self):
        self._stack = contextlib.ExitStack()
        woo = self._stack.enter_context(serve(http_app(build_woo())))
        self.gw = self._stack.enter_context(serve(create_app(
            Settings(database_url=self.db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
            gateways=FakeGateways())))
        self.admin = await setup_admin(self.gw)
        self.sid = (await self.admin.post("/api/servers", json={"name": "woo", "url": f"{woo}/mcp"})).json()["id"]
        await add_gateway(self.admin, "main", [self.sid])
        self.priya_id, self.priya = await member(self.admin, self.gw, "priya")
        self.ravi_id, self.ravi = await member(self.admin, self.gw, "ravi")
        self.omar_id, self.omar = await member(self.admin, self.gw, "omar")
        self.finance = await add_team(self.admin, "finance", [self.priya_id], "main",
                                      [(self.sid, "create_restock_plan"), (self.sid, "crash")])
        self.ops = await add_team(self.admin, "store-operators", [self.ravi_id], "main", [(self.sid, "receive_stock")])
        self.support = await add_team(self.admin, "support", [self.omar_id], "main", [(self.sid, "receive_stock")])
        r = await self.admin.put(f"/api/teams/{self.ops}/gateways/main/alert-rules", json={"rules": [
            {"server_id": self.sid, "tool": "create_restock_plan"}, {"server_id": self.sid, "tool": "crash"}]})
        assert r.status_code == 200, r.text
        self.edge = self._stack.enter_context(serve(create_app(edge_settings(self.db_url, "main", calls_per_minute=1000))))
        self.conn = store.connect(self.db_url, create_schema=False)
        await wait_tools(self.edge, self.priya, 2)
        return self

    async def __aexit__(self, *exc):
        self.conn.close()
        self._stack.close()

    def api(self, key: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(base_url=self.gw, headers={"Authorization": f"Bearer {key}"}, timeout=10)

    async def call(self, key: str, name: str, args: dict):
        async with gateway_client(self.edge, key) as mcp:
            r = await mcp.call_tool(name, args)
            return r.is_error, text(r)

    def alerts(self) -> list[dict]:
        return self.conn.execute("SELECT * FROM team_alerts ORDER BY id").fetchall()


def test_a_rule_turns_a_successful_call_into_one_team_alert(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            # store-operators cannot call the planning tool, yet hears when finance's call succeeds.
            err, _ = await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {}), (21, "Noodles", 5, {"bad": True})))
            assert not err
            [a] = w.alerts()
            assert (a["team_id"], a["gateway"], a["tool"], a["title"], a["requested_by"], a["approval_id"], a["done_at"]) == (
                w.ops, "main", PLAN, "Create restock plan", w.priya_id, None, None)
            assert json.loads(a["lines"]) == ["Fresh Deodorant 150ml (#20): PO #7: 279 x 110.00", "1 item refused"]

            assert not (await w.call(w.priya, PLAN, plan((21, "Noodles", 5, {"bad": True}))))[0]  # every item refused
            assert (await w.call(w.priya, CRASH, {}))[0]  # an error result
            assert not (await w.call(w.ravi, RECEIVE, {"po_id": 7}))[0]  # no rule for this tool
            assert len(w.alerts()) == 1

            # Content comes from the masked copy: an email in a detail never reaches the team.
            await w.call(w.priya, PLAN, plan((22, "Tea", 1, {"note": "call priya@example.com"})))
            assert "priya@example.com" not in w.alerts()[-1]["lines"]

    asyncio.run(run())


def test_an_approved_run_alerts_with_its_approval(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            r = await w.admin.put(f"/api/teams/{w.finance}/gateways/main/budgets",
                                  json={"budgets": [{"server_id": w.sid, "tool": "create_restock_plan", "max_items": 0}]})
            assert r.status_code == 200, r.text
            err, msg = await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            assert err and "Approval #1 requested" in msg, msg
            assert w.alerts() == []
            assert (await w.admin.post("/api/approvals/1/approve", json={})).status_code == 200
            [a] = w.alerts()
            assert (a["approval_id"], a["requested_by"]) == (1, w.priya_id)

    asyncio.run(run())


def test_alert_rules_api(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            other = (await w.admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            r = await w.admin.put(f"/api/teams/{w.ops}/gateways/main/alert-rules",
                                  json={"rules": [{"server_id": other, "tool": "x"}]})
            assert r.status_code == 400 and "not attached" in r.text
            async with w.api(w.ravi) as ravi:
                assert (await ravi.put(f"/api/teams/{w.ops}/gateways/main/alert-rules", json={"rules": []})).status_code == 403
            teams = {t["name"]: t for t in (await w.admin.get("/api/teams")).json()}
            assert [r["tool"] for r in teams["store-operators"]["alert_rules"]["main"]] == ["crash", "create_restock_plan"]
            # Detaching the server deletes its rules, like team tools.
            assert (await w.admin.put("/api/gateways/main/servers", json={"server_ids": []})).status_code == 200
            assert w.conn.execute("SELECT COUNT(*) AS n FROM team_alert_rules").fetchone()["n"] == 0

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect failure.** From `services/gateway`: `TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest tests/test_alerts.py -q` → FAIL (404 on alert-rules).

- [ ] **Step 3: Tables.** In `db.py`, append inside SCHEMA, just before its closing `"""`:

```sql
-- Team alerts: which successful tool calls alert which team. Like team_tools, a rule
-- belongs to an attachment, and any attached tool may alert any team (the team that
-- acts is often not the one that calls).
CREATE TABLE IF NOT EXISTS team_alert_rules (
    team_id    BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    gateway_id BIGINT NOT NULL,
    server_id  BIGINT NOT NULL,
    tool       TEXT NOT NULL,
    PRIMARY KEY (team_id, gateway_id, server_id, tool),
    FOREIGN KEY (gateway_id, server_id) REFERENCES gateway_servers (gateway_id, server_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS team_alerts (
    id           BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    team_id      BIGINT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    gateway      TEXT NOT NULL,               -- slug, as in audit_log
    tool         TEXT NOT NULL,               -- full <server>__<tool> name
    title        TEXT NOT NULL,
    lines        TEXT NOT NULL,               -- JSON list of strings
    requested_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    approval_id  BIGINT,                      -- set when the call was an approved run
    audit_id     BIGINT NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    done_by      BIGINT REFERENCES users(id) ON DELETE SET NULL,
    done_at      DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS team_alerts_open ON team_alerts (team_id, done_at);
```

- [ ] **Step 4: Rules API.** In `teams.py`, give `_tool_rows` a field name (existing callers unchanged):

```python
async def _tool_rows(request: Request, gateway_id: int, field: str = "tools") -> list[tuple[int, str]]:
    """The body's {<field>: [{server_id, tool}]}, each server attached to this gateway.
    Tool names are not checked against the upstream, which may be down."""
    tools = (await body(request)).get(field, [])
    if not isinstance(tools, list) or not all(
        isinstance(t, dict) and type(t.get("server_id")) is int
        and isinstance(t.get("tool"), str) and 0 < len(t["tool"]) <= TOOL_MAX
        for t in tools
    ):
        raise HTTPError(400, f"{field} must be a list of {{server_id, tool}} with tool 1-{TOOL_MAX} characters")
    attached = {r["server_id"] for r in request.app.state.db.execute(
        "SELECT server_id FROM gateway_servers WHERE gateway_id = ?", (gateway_id,))}
    if not {t["server_id"] for t in tools} <= attached:
        raise HTTPError(400, f"{field} names a server that is not attached to this gateway")
    return sorted({(t["server_id"], t["tool"]) for t in tools})
```

After `set_tools` add:

```python
@handler
async def set_alert_rules(request: Request) -> Response:
    """Replace which tools on one gateway alert this team when a call of them succeeds."""
    require_admin(request)
    team = _team(request)
    gateway = _gateway(request)
    rows = await _tool_rows(request, gateway["id"], "rules")
    with request.app.state.db.transaction() as tx:
        tx.execute("DELETE FROM team_alert_rules WHERE team_id = ? AND gateway_id = ?", (team["id"], gateway["id"]))
        tx.executemany("INSERT INTO team_alert_rules (team_id, gateway_id, server_id, tool) VALUES (?, ?, ?, ?)",
                       [(team["id"], gateway["id"], s, t) for s, t in rows])
    return JSONResponse({"ok": True, "count": len(rows)})
```

Route, after the `/tools` route:

```python
    Route("/api/teams/{id:int}/gateways/{slug}/alert-rules", set_alert_rules, methods=["PUT"]),
```

In `_views`: add `alert_rules = collections.defaultdict(lambda: collections.defaultdict(list))` next to `budgets`, inside `if ids:` add

```python
        for r in conn.execute(
            "SELECT ar.team_id, g.slug, s.id AS server_id, s.name AS server, ar.tool FROM team_alert_rules ar"
            " JOIN gateways g ON g.id = ar.gateway_id JOIN servers s ON s.id = ar.server_id"
            " WHERE ar.team_id = ANY(?) ORDER BY g.slug, s.name, ar.tool",
            (ids,),
        ):
            alert_rules[r["team_id"]][r["slug"]].append({"server_id": r["server_id"], "server": r["server"], "tool": r["tool"]})
```

and add `"alert_rules": alert_rules[t["id"]]` to each returned team dict.

- [ ] **Step 5: The trigger.** Create `services/gateway/app/alerts.py`:

```python
"""Team alerts: tell a team when a tool call it must act on has succeeded.

An admin sets rules per team on the Teams page ("alert this team when <tool> succeeds
on <gateway>"). Upstreams.call_tool calls fire() after every successful upstream call,
on edges and approved runs alike; each matching team gets one alert, open until a
member marks it done. The content is the PII-masked copy of the result.
"""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.types import CallToolResult, TextContent

MAX_LINES = 50


def _lines(result: CallToolResult, arguments: dict) -> list[str] | None:
    """What happened, one line per item that succeeded; None when nothing did."""
    data = result.structured_content
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = [i for i in arguments.get("items") or [] if isinstance(i, dict)]

        def name_of(item_id: Any) -> str | None:
            # The call's own item with that id (id, product_id, order_id, ...) names it.
            for i in items:
                if any(v == item_id for k, v in i.items() if k == "id" or k.endswith("_id")):
                    return i.get("name")
            return None

        ok = [r for r in data["results"] if isinstance(r, dict) and r.get("ok")]
        if not ok:
            return None
        lines = []
        for r in ok[:MAX_LINES]:
            name = name_of(r.get("id"))
            lines.append(f"{name + ' ' if name else ''}(#{r.get('id')}): {r.get('detail') or 'done'}")
        if len(ok) > MAX_LINES:
            lines.append(f"…and {len(ok) - MAX_LINES} more")
        refused = len(data["results"]) - len(ok)
        if refused:
            lines.append(f"{refused} item{'s' if refused != 1 else ''} refused")
        return lines
    words = " ".join(c.text for c in result.content if isinstance(c, TextContent)).strip()
    return [words[:500] or "done"]


def fire(conn, *, user: dict, gateway_slug: str, server_id: int, tool: str, title: str, arguments: dict,
         result: CallToolResult, approval_id: int | None, audit_id: int) -> None:
    """One alert per team with a rule for this tool on this gateway. `result` is the masked copy."""
    teams = [r["team_id"] for r in conn.execute(
        "SELECT r.team_id FROM team_alert_rules r JOIN gateways g ON g.id = r.gateway_id"
        " WHERE g.slug = ? AND r.server_id = ? AND r.tool = ?",
        (gateway_slug, server_id, tool),
    )]
    if not teams:
        return
    lines = _lines(result, arguments)
    if lines is None:
        return
    name = f"{conn.execute('SELECT name FROM servers WHERE id = ?', (server_id,)).fetchone()['name']}__{tool}"
    now = time.time()
    with conn.transaction() as tx:
        tx.executemany(
            "INSERT INTO team_alerts (team_id, gateway, tool, title, lines, requested_by, approval_id, audit_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(t, gateway_slug, name, title, json.dumps(lines), user["id"], approval_id, audit_id, now) for t in teams],
        )
```

- [ ] **Step 6: Hook it into `call_tool`.** In `upstream.py`, at the end of `call_tool`, between the audit `UPDATE` and `return result`:

```python
        if not result.is_error and logged is not None and row is not None:
            from . import alerts  # here, like approvals: avoids an import cycle through api

            listed = next((t for t in self.status.get(row["id"], Status()).tools if t.name == tool), None)
            try:
                alerts.fire(self.db, user=user, gateway_slug=gateway_slug, server_id=row["id"], tool=tool,
                            title=(listed and (listed.title or (listed.annotations and listed.annotations.title))) or tool,
                            arguments=arguments or {}, result=logged, approval_id=approval_id, audit_id=audit_id)
            except Exception:
                log.exception("team alert for %s failed", name)  # the caller gets their result either way
```

- [ ] **Step 7: Run, expect pass.** Same command → 3 passed. If the masked copy drops `structured_content` (lines come back as one JSON text line), make `_redact`'s masked copy keep masked `structured_content`, or build lines from the masked text's JSON (`json.loads` of the text when it parses to a dict with `results`); the test's exact lines decide.

- [ ] **Step 8: Whole gateway suite.** `TEST_DATABASE_URL=… uv run pytest -q` → all pass.

- [ ] **Step 9: Leave for the user to commit.**

---

### Task 8: Team alerts: API and the agents' built-in tools

**Files:**
- Modify: `services/gateway/app/alerts.py`, `services/gateway/app/approvals.py` (`builtin`), `services/gateway/app/upstream.py` (`list_tools`), `services/gateway/app/main.py` (routes)
- Test: `services/gateway/tests/test_alerts.py`

**Interfaces:**
- Consumes: `AlertWorld` (Task 7), `HTTPError`, `current_user`, `handler` from `app.api`, `error_result` from `app.upstream`.
- Produces: `GET /api/alerts?status=open|done|all`, `GET /api/alerts/count` → `{"open": n}`, `POST /api/alerts/{id}/done`; alert JSON `{id, team, gateway, tool, title, lines, requested_by, approved_by, created_at, done_by, done_at}`; MCP tools `gateway__list_team_alerts`, `gateway__mark_team_alert_done`; `alerts.TOOLS`, `alerts.builtin(conn, user, tool, arguments) -> CallToolResult | None`, `alerts.routes`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_alerts.py`:

```python
def test_members_see_their_teams_alerts_and_mark_them_done(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            async with w.api(w.ravi) as ravi, w.api(w.omar) as omar, w.api(w.priya) as priya:
                [a] = (await ravi.get("/api/alerts")).json()
                assert (a["team"], a["title"], a["requested_by"], a["approved_by"], a["done_by"]) == (
                    "store-operators", "Create restock plan", "priya", None, None)
                assert (await ravi.get("/api/alerts/count")).json() == {"open": 1}
                assert (await omar.get("/api/alerts")).json() == []  # another team
                assert (await priya.get("/api/alerts")).json() == []  # the caller's own team has no rule
                assert len((await w.admin.get("/api/alerts")).json()) == 1  # admins see every team's

                assert (await omar.post(f"/api/alerts/{a['id']}/done")).status_code == 404
                r = await ravi.post(f"/api/alerts/{a['id']}/done")
                assert r.status_code == 200 and r.json()["done_by"] == "ravi"
                r = await w.admin.post(f"/api/alerts/{a['id']}/done")
                assert r.status_code == 409 and "already done by ravi" in r.text
                assert (await ravi.get("/api/alerts")).json() == []  # open is the default
                assert [x["done_by"] for x in (await ravi.get("/api/alerts?status=done")).json()] == ["ravi"]
                assert (await ravi.get("/api/alerts?status=nope")).status_code == 400

                # Leaving the team hides its alerts.
                assert (await w.admin.put(f"/api/teams/{w.ops}/members", json={"user_ids": []})).status_code == 200
                assert (await ravi.get("/api/alerts?status=all")).json() == []

    asyncio.run(run())


def test_agents_list_and_mark_alerts_through_mcp(db_url):
    async def run():
        async with AlertWorld(db_url) as w:
            await w.call(w.priya, PLAN, plan((20, "Fresh Deodorant 150ml", 279, {})))
            async with gateway_client(w.edge, w.ravi) as mcp:
                names = {t.name for t in (await mcp.list_tools()).tools}
                assert {"gateway__list_team_alerts", "gateway__mark_team_alert_done"} <= names
                r = await mcp.call_tool("gateway__list_team_alerts", {})
                [a] = r.structured_content["alerts"]
                assert a["lines"] == ["Fresh Deodorant 150ml (#20): PO #7: 279 x 110.00"]
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert not r.is_error and f"Alert #{a['id']} marked done" in text(r)
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert r.is_error and "already done by ravi" in text(r)
                assert (await mcp.call_tool("gateway__list_team_alerts", {})).structured_content["alerts"] == []
            async with gateway_client(w.edge, w.omar) as mcp:
                r = await mcp.call_tool("gateway__mark_team_alert_done", {"alert_id": a["id"]})
                assert r.is_error and "no such alert" in text(r)

    asyncio.run(run())
```

- [ ] **Step 2: Run, expect failure** (404 on `/api/alerts`).

- [ ] **Step 3: Implement.** Append to `alerts.py` (and add imports `from mcp.types import Tool, ToolAnnotations`, `from starlette.requests import Request`, `from starlette.responses import JSONResponse, Response`, `from starlette.routing import Route`, `from .api import HTTPError, current_user, handler`, `from .upstream import error_result`):

```python
STATUSES = ("open", "done", "all")
VIEW = (
    "SELECT a.*, t.name AS team, r.username AS requested_by_name, d.username AS done_by_name,"
    " au.username AS approved_by_name FROM team_alerts a JOIN teams t ON t.id = a.team_id"
    " LEFT JOIN users r ON r.id = a.requested_by LEFT JOIN users d ON d.id = a.done_by"
    " LEFT JOIN approvals ap ON ap.id = a.approval_id LEFT JOIN users au ON au.id = ap.decided_by"
)


def _visible(user: dict) -> tuple[str, tuple]:
    """Admins see every team's alerts; members, their current teams'."""
    if user["role"] == "admin":
        return "TRUE", ()
    return "a.team_id IN (SELECT team_id FROM team_members WHERE user_id = ?)", (user["id"],)


def _view(r: dict) -> dict:
    return {
        "id": r["id"], "team": r["team"], "gateway": r["gateway"], "tool": r["tool"], "title": r["title"],
        "lines": json.loads(r["lines"]), "requested_by": r["requested_by_name"], "approved_by": r["approved_by_name"],
        "created_at": r["created_at"], "done_by": r["done_by_name"], "done_at": r["done_at"],
    }


def query(conn, user: dict, status: str) -> list[dict]:
    where, args = _visible(user)
    if status != "all":
        where += " AND a.done_at IS NULL" if status == "open" else " AND a.done_at IS NOT NULL"
    return [_view(r) for r in conn.execute(f"{VIEW} WHERE {where} ORDER BY a.id DESC LIMIT 200", args)]


def mark_done(conn, user: dict, alert_id: int) -> dict:
    where, args = _visible(user)
    if conn.execute(f"{VIEW} WHERE a.id = ? AND {where}", (alert_id, *args)).fetchone() is None:
        raise HTTPError(404, "no such alert")
    # The claim: one of two clicks wins; the other is told who did it.
    won = conn.execute("UPDATE team_alerts SET done_by = ?, done_at = ? WHERE id = ? AND done_at IS NULL RETURNING id",
                       (user["id"], time.time(), alert_id)).fetchone()
    row = conn.execute(f"{VIEW} WHERE a.id = ?", (alert_id,)).fetchone()
    if won is None:
        raise HTTPError(409, f"already done by {row['done_by_name'] or 'a removed user'}")
    return _view(row)


@handler
async def list_alerts(request: Request) -> Response:
    status = request.query_params.get("status", "open")
    if status not in STATUSES:
        raise HTTPError(400, "status must be open, done or all")
    return JSONResponse(query(request.app.state.db, current_user(request), status))


@handler
async def count_alerts(request: Request) -> Response:
    where, args = _visible(current_user(request))
    n = request.app.state.db.execute(
        f"SELECT COUNT(*) AS n FROM team_alerts a WHERE a.done_at IS NULL AND {where}", args).fetchone()["n"]
    return JSONResponse({"open": n})


@handler
async def done_alert(request: Request) -> Response:
    return JSONResponse(mark_done(request.app.state.db, current_user(request), request.path_params["id"]))


routes = [
    Route("/api/alerts", list_alerts, methods=["GET"]),
    Route("/api/alerts/count", count_alerts, methods=["GET"]),
    Route("/api/alerts/{id:int}/done", done_alert, methods=["POST"]),
]

TOOLS = [
    Tool(
        name="gateway__list_team_alerts",
        description=(
            "[gateway] Alerts for your teams: what a teammate's or another team's tool call did that your "
            "team should act on (for example a purchase order whose goods you will receive). Open alerts "
            "wait for someone to act; after handling one, mark it with gateway__mark_team_alert_done."
        ),
        input_schema={"type": "object", "properties": {
            "status": {"type": "string", "enum": list(STATUSES), "description": "open (default), done or all"}}},
        annotations=ToolAnnotations(read_only_hint=True),
    ),
    Tool(
        name="gateway__mark_team_alert_done",
        description="[gateway] Mark one of your teams' alerts done once it is handled; your teammates see who did it.",
        input_schema={"type": "object", "properties": {"alert_id": {"type": "integer"}}, "required": ["alert_id"]},
    ),
]


def builtin(conn, user: dict, tool: str, arguments: dict) -> CallToolResult | None:
    """A gateway__ alerts call, or None when the tool is not one of these."""
    if tool == "list_team_alerts":
        status = arguments.get("status", "open")
        if status not in STATUSES:
            return error_result("status must be open, done or all")
        rows = query(conn, user, status)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(rows))], structured_content={"alerts": rows})
    if tool == "mark_team_alert_done":
        alert_id = arguments.get("alert_id")
        if type(alert_id) is not int:
            return error_result("alert_id must be a whole number")
        try:
            mark_done(conn, user, alert_id)
        except HTTPError as exc:
            return error_result(exc.message)
        return CallToolResult(content=[TextContent(type="text", text=f"Alert #{alert_id} marked done.")])
    return None
```

In `approvals.builtin`, before the final `return upstream.error_result(...)`:

```python
    from . import alerts

    if (result := alerts.builtin(ups.db, user, tool, arguments)) is not None:
        return result, None, "bad input" if result.is_error else None
```

In `upstream.list_tools`, replace the last line:

```python
        from . import alerts, approvals  # here, not at the top: approvals imports api, which imports this module

        # Every member gets them, budget or not: MCP clients keep the list they got at connect
        # time, so tying it to budgets left agents without the tool when an admin added one later.
        builtins = (approvals.TOOLS if user["role"] != "admin" else []) + alerts.TOOLS
        return out + (builtins if out else [])
```

(and drop the old `from . import approvals` line above it). In `main.py`, import `alerts` with the other app modules and add `*alerts.routes,` after `*approvals.routes,`.

- [ ] **Step 4: Run, expect pass**, then the whole gateway suite. Any existing test that asserted an admin's exact tool list now also sees the two alert tools: update its expected set.

- [ ] **Step 5: Leave for the user to commit.**

---

### Task 9: Dashboard: one Alerts tab for everyone, alert rules on Teams

**Files:** Modify `services/dashboard/src/App.jsx`, `services/dashboard/src/Alerts.jsx`, `services/dashboard/src/Teams.jsx`

**Interfaces:** Consumes the Task 7–8 API. Pages receive `user` (App renders `<Page user={user} />`).

- [ ] **Step 1: App.jsx.** In `PAGES`, drop `admin: true` from the `alerts` entry. Replace the approvals-count effect:

```js
  useEffect(() => {
    if (!user) return
    // Open team alerts for everyone; admins add the approvals waiting for them.
    const poll = () => Promise.all([
      api('/alerts/count'),
      user.role === 'admin' ? api('/approvals/count') : Promise.resolve({ pending: 0 }),
    ]).then(([a, p]) => setPending(a.open + p.pending)).catch(() => {})
    poll()
    const t = setInterval(poll, 10000)
    return () => clearInterval(t)
  }, [user])
```

and change the `pending` state comment to `// open team alerts (+ approvals waiting, for admins), for the Alerts badge`.

- [ ] **Step 2: Alerts.jsx.** Rename `export default function Alerts()` to `function Approvals()` and its `<h2>Alerts</h2>` to `<h2>Approvals</h2>`. Add:

```jsx
// Alerts for teams: what someone's tool call did that a team should act on.
function TeamAlerts({ user }) {
  const [status, setStatus] = useState('open')
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')

  const load = useCallback(() => {
    api(`/alerts?status=${status}`).then(setRows).catch((e) => setError(e.message))
  }, [status])
  useEffect(load, [load])

  async function done(id) {
    setError('')
    try {
      await api(`/alerts/${id}/done`, { method: 'POST' })
    } catch (e) {
      setError(e.message) // e.g. a teammate was first: the reload shows who
    }
    load()
  }

  return (
    <>
      <div className="toolbar">
        <div>
          <h2>Team alerts</h2>
          <div className="muted small">
            {user.role === 'admin' ? "Every team's alerts." : 'Alerts for your teams.'} An admin chooses on the Teams
            page which tool calls alert which team.
          </div>
        </div>
      </div>
      <div className="row">
        {['open', 'done', 'all'].map((s) => (
          <button key={s} className={s === status ? 'primary' : 'ghost'} aria-pressed={s === status} onClick={() => setStatus(s)}>
            {s[0].toUpperCase() + s.slice(1)}
          </button>
        ))}
        <button className="ghost" onClick={load}>Refresh</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {rows.length === 0 && <p className="muted">{status === 'open' ? 'Nothing to act on.' : 'No alerts yet.'}</p>}
      {rows.map((a) => (
        <div key={a.id} className="card">
          <div className="row">
            <b>{a.title}</b>
            <span className="pill">{a.team}</span>
            {a.done_at && <span className="pill ok">done</span>}
          </div>
          <div className="muted small">
            <span className="mono">{a.tool}</span> on <span className="mono">{a.gateway}</span> · {when(a.created_at)}
          </div>
          <ul className="small">{a.lines.map((l, i) => <li key={i} className="mono">{l}</li>)}</ul>
          <div className="muted small">
            requested by {a.requested_by || 'a removed user'}{a.approved_by && <> · approved by {a.approved_by}</>}
          </div>
          {a.done_at ? (
            <div className="small">Done by <b>{a.done_by || 'a removed user'}</b>, {when(a.done_at)}</div>
          ) : (
            <div className="row end"><button className="primary" onClick={() => done(a.id)}>Mark done</button></div>
          )}
        </div>
      ))}
    </>
  )
}

export default function Alerts({ user }) {
  return (
    <>
      {user.role === 'admin' && <Approvals />}
      <TeamAlerts user={user} />
    </>
  )
}
```

- [ ] **Step 3: Teams.jsx.** In `TeamView`, after the tools map:

```jsx
      {Object.entries(team.alert_rules || {}).map(([slug, rules]) => (
        <div key={`alerts-${slug}`} className="small">
          Alerted on <b>{slug}</b> when these succeed:{' '}
          <span className="mono">{rules.map((r) => `${r.server}__${r.tool}`).join(', ')}</span>
        </div>
      ))}
```

In `TeamEditor`, after `const [picks, setPicks] = …`:

```js
  // Alert rules, per gateway: any attached tool, allowed to this team or not (the team that acts
  // is often not the one that calls).
  const savedAlerts = Object.fromEntries(
    gateways.map((g) => [g.slug, new Set((team.alert_rules?.[g.slug] || []).map((t) => key(t.server_id, t.tool)))]),
  )
  const [alertPicks, setAlertPicks] = useState(() => Object.fromEntries(gateways.map((g) => [g.slug, new Set(savedAlerts[g.slug])])))
  const toggleAlert = (slug, k) => {
    const next = new Set(alertPicks[slug])
    next.has(k) ? next.delete(k) : next.add(k)
    setAlertPicks({ ...alertPicks, [slug]: next })
  }
```

In `save()`, after the budgets loop:

```js
      for (const g of gateways) {
        if (same(alertPicks[g.slug], savedAlerts[g.slug])) continue
        const rules = [...alertPicks[g.slug]].map((k) => {
          const i = k.indexOf(':')
          return { server_id: Number(k.slice(0, i)), tool: k.slice(i + 1) }
        })
        await api(`/teams/${team.id}/gateways/${g.slug}/alert-rules`, { method: 'PUT', body: { rules } })
      }
```

In the per-tool `<span key={t} className="pick">`, after the budget inputs block:

```jsx
                        {!readOnly.has(k) && (
                          <label className="pick" title="Alert this team when anyone's call of this tool succeeds">
                            <input type="checkbox" checked={alertPicks[g.slug].has(k)} onChange={() => toggleAlert(g.slug, k)} />
                            <span className="muted small">alert team</span>
                          </label>
                        )}
```

- [ ] **Step 4: Build.** `cd services/dashboard && npm run build` → no errors.

- [ ] **Step 5: Leave for the user to commit.**

---

### Task 10: Ship to store-check and run the demo by hand

**Files:** Modify `charts/woo-mcp/values.yaml` (`image.tag`)

- [ ] **Step 1: Versions in use.** `helm -n mercora list`, and the image of each deployment (`kubectl -n mercora get deploy -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image`). Pick next tags: operator `OP`, gateway `GW` (after 0.9.0), dashboard `UI` (after 0.8.6). woo-mcp goes to `0.5.0`.

- [ ] **Step 2: Build and import.** From `team-mcp-gateway/` (check each Dockerfile's header for its build context):

```bash
sed -i '' 's/tag: "0.4.0"/tag: "0.5.0"/' charts/woo-mcp/values.yaml
docker build -t woo-mcp:0.5.0 services/woo-mcp
docker build -f services/store-operator/Dockerfile -t store-operator:$OP .   # bundles the store + woo-mcp charts
docker build -t gateway:$GW services/gateway
docker build -t dashboard:$UI services/dashboard
k3d image import woo-mcp:0.5.0 store-operator:$OP gateway:$GW dashboard:$UI -c store-check
```

- [ ] **Step 3: Upgrade.** (Auto mode may block `helm upgrade`; then the user runs these with `!`.) Use the values files, not `--reuse-values`:

```bash
helm -n mercora upgrade team charts/gateway -f deploy/gateway-values-local.yaml --set image.tag=$GW
helm -n mercora get values gw-prod -o yaml > <scratchpad>/gw-prod.yaml
helm -n mercora upgrade gw-prod charts/gateway -f <scratchpad>/gw-prod.yaml --set image.tag=$GW   # the one edge
helm -n mercora upgrade team-ui charts/dashboard -f deploy/dashboard-values-local.yaml --set image.tag=$UI
kubectl -n mercora rollout restart deploy/team-ui-dashboard
helm -n mercora upgrade store-operator charts/store-operator -f deploy/store-operator-values-local.yaml \
  --set image.tag=$OP --set edgeImageTag=$GW
```

Check: `curl -s http://gateway.localhost/readyz`, `curl -s http://prod.gw.localhost/readyz` → ok; the dashboard shows Alerts to a member.

- [ ] **Step 4: Demo store.** Dashboard → Stores → Catalog A → `shop-a`; wait for Ready (setup Job log shows the history count). Attach its woo-mcp server to the `prod` gateway.

- [ ] **Step 5: People and teams.** Members: `priya` (finance), `ravi` (store operator). Teams: `finance` with `get_inventory_health`, `create_restock_plan`, `create_clearance_offers`, `list_restock_orders`, budget 0 on the plan and clearance tools; `store-operators` with `list_restock_orders`, `receive_stock`, and "alert team" ticked on `create_restock_plan`.

- [ ] **Step 6: Demo** (restock spec, Testing → Manual demo, steps 3–8):
  1. priya: "I have ₹50,000 for restocking this month. What should I reorder?" → plan within budget → approval requested.
  2. admin approves on Alerts → PO #1; WP admin → WooCommerce → Purchase orders lists it.
  3. ravi: Alerts tab shows "Create restock plan" for store-operators with a badge; via agent `gateway__list_team_alerts` too.
  4. ravi: "200 deodorants arrived on PO #1" → `receive_stock` → PO `partially_received`; then the rest → `received`; Mark done → priya's view shows "done by ravi" (admin's too).
  5. priya: "Show me my restock orders" → PO #1 received.
  6. Clearance on the hamper → approve → sale price on the storefront.
  7. Same tools on a blank store → successful empty answers.

- [ ] **Step 7: Report** what passed and what did not, with outputs. Update the `gateway-deployment` memory with the new tags. Leave for the user to commit.
