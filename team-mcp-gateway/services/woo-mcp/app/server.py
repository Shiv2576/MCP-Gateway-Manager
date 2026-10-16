"""The MCP tools: six things an agent may do to the store, six reports, four bulk writes,
cart recovery (abandoned-cart facts, and sale offers that need an admin's approval), and
restock planning (stock facts, purchase orders, receiving stock, clearance offers).

The first six are thin, typed wrappers over one WooCommerce REST call. The
reports page through orders or products and add them up here, so dates follow
the store's timezone (WooCommerce's own /reports endpoints do not). The tool
descriptions and parameter descriptions below are what the language model
reads when it decides which tool to call, so they are written for the model.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import anyio

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .woo import WooClient, WooError

OrderStatus = Literal[
    "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"
]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=False
)


def day_bounds_utc(
    on_date: str, tz_name: str, *, now: datetime | None = None
) -> tuple[str, str]:
    """Turn "today", "yesterday" or YYYY-MM-DD into [start, end) in UTC.

    "Today" means the store's calendar day, not the server's: a shop in India
    starts its day at 18:30 UTC the evening before.
    """
    tz = ZoneInfo(tz_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    word = on_date.strip().lower()
    if word == "today":
        day = local_now.date()
    elif word == "yesterday":
        day = local_now.date() - timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(word)
        except ValueError:
            raise ToolError(
                "on_date must be 'today', 'yesterday' or a date like 2026-10-09"
            ) from None
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (
        start.astimezone(timezone.utc).strftime(fmt),
        end.astimezone(timezone.utc).strftime(fmt),
    )


# Typed results, so each tool advertises an outputSchema: the gateway's PII
# Protection tab lists these fields for an admin to pick what to mask.
class Product(BaseModel):
    id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name")
    price: str | None = Field(None, description="Current price in the store currency")
    status: str | None = Field(None, description="publish, draft, trash, ...")
    stock_status: str | None = Field(None, description="instock, outofstock or onbackorder")
    sku: str | None = Field(None, description="Stock code")
    url: str | None = Field(None, description="Storefront link")


class LineItem(BaseModel):
    product_id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name at the time of the order")
    quantity: int | None = Field(None, description="How many")
    total: str | None = Field(None, description="Line total in the order currency")


class OrderSummary(BaseModel):
    id: int | None = Field(None, description="Order id")
    number: str | None = Field(None, description="Order number shown to the customer")
    status: str | None = Field(None, description="Order status")
    total: str | None = Field(None, description="Order total")
    currency: str | None = Field(None, description="Currency code")
    created_utc: str | None = Field(None, description="When it was placed, UTC")
    customer: str | None = Field(None, description="Billing first and last name")
    items: list[LineItem] = Field(default_factory=list, description="Line items")


class Order(OrderSummary):
    payment_method: str | None = Field(None, description="Payment method title")
    customer_note: str | None = Field(None, description="Note the customer wrote at checkout")
    ship_to: str | None = Field(None, description="City, state and country shipped to")


class ProductList(BaseModel):
    count: int
    products: list[Product]


class OrderList(BaseModel):
    count: int
    orders: list[OrderSummary]


class LowStockProduct(Product):
    stock_quantity: int | None = Field(None, description="Units in stock")


class LowStockList(BaseModel):
    threshold: int
    count: int
    products: list[LowStockProduct]
    truncated: bool = Field(description="True if the store had more products than were checked")


class ProductIssues(BaseModel):
    id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name")
    issues: list[str] = Field(description="missing_description, missing_image, missing_price, missing_category")


class QualityAudit(BaseModel):
    checked: int = Field(description="Products checked")
    count: int = Field(description="Products with at least one issue")
    products: list[ProductIssues]
    truncated: bool = Field(description="True if the store had more products than were checked")


class SalesSummary(BaseModel):
    start_utc: str
    end_utc: str
    currency: str | None
    order_count: int
    gross_sales: str = Field(description="Sum of order totals")
    refunds: str = Field(description="Refunded on those orders, as a positive amount")
    net_sales: str = Field(description="gross_sales minus refunds")
    average_order_value: str = Field(description="gross_sales / order_count")
    truncated: bool = Field(description="True if there were more orders than were counted")


class RefundLine(BaseModel):
    order_id: int | None = Field(None, description="Order the refund belongs to")
    refund_id: int | None = Field(None, description="Refund id")
    amount: str | None = Field(None, description="Refunded amount, positive")
    reason: str | None = Field(None, description="Reason the admin typed")
    date_utc: str | None = Field(None, description="When the refund was made, UTC")


class RefundSummary(BaseModel):
    start_utc: str
    end_utc: str
    currency: str | None
    refund_count: int
    refund_total: str = Field(description="Sum of refunds, positive")
    orders_affected: int
    refunds: list[RefundLine]
    truncated: bool = Field(description="True if there were more orders than were checked")


class BestSeller(BaseModel):
    product_id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name")
    units: int = Field(description="Units sold")
    revenue: str = Field(description="Line totals, store currency")


class BestSellers(BaseModel):
    start_utc: str
    end_utc: str
    ranked_by: str
    products: list[BestSeller]
    truncated: bool = Field(description="True if there were more orders than were counted")


class TimelineOrder(BaseModel):
    id: int | None = Field(None, description="Order id")
    number: str | None = Field(None, description="Order number shown to the customer")
    status: str | None = Field(None, description="Order status")
    total: str | None = Field(None, description="Order total")
    created_utc: str | None = Field(None, description="When it was placed, UTC")


class CustomerTimeline(BaseModel):
    customer: str | None = Field(None, description="Billing first and last name")
    order_count: int
    total_spent: str = Field(description="Sum of totals of orders that counted as sales")
    orders: list[TimelineOrder] = Field(description="Oldest first")
    truncated: bool = Field(description="True if the customer had more orders than were listed")


# Bulk tools. One list argument named `items` (the gateway counts it for write
# budgets), one result per item; one bad item never fails the batch.
MAX_BULK = 100  # WooCommerce's own /batch limit
Money = Annotated[str, Field(pattern=r"^\d+(\.\d{1,2})?$", description="Amount, e.g. '499' or '499.50'")]


class _Item(BaseModel):
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _something_to_change(self):
        # Every field but the id is optional, and at least one must be set.
        if all(v is None for k, v in self if not k.endswith("_id")):
            raise ValueError("each item needs at least one field to change")
        return self


class DescriptionItem(_Item):
    product_id: int = Field(ge=1)
    description: str | None = Field(None, max_length=10000)
    short_description: str | None = Field(None, max_length=2000)


class PriceItem(_Item):
    product_id: int = Field(ge=1)
    regular_price: Money | None = None
    sale_price: Money | None = None


class ShippingAddress(BaseModel):
    model_config = {"extra": "forbid"}
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    address_1: str | None = None
    address_2: str | None = None
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    country: str | None = Field(None, description="Two-letter country code")
    phone: str | None = None


class OrderUpdateItem(_Item):
    order_id: int = Field(ge=1)
    status: OrderStatus | None = None
    shipping: ShippingAddress | None = Field(None, description="Only the address fields to change")


class RefundItem(BaseModel):
    model_config = {"extra": "forbid"}
    order_id: int = Field(ge=1)
    amount: Money
    reason: str | None = Field(None, max_length=500)


class ItemResult(BaseModel):
    id: int
    ok: bool
    error: str | None = None
    detail: str | None = None


class BulkResult(BaseModel):
    ok: int = Field(description="Items that succeeded")
    failed: int = Field(description="Items that failed")
    results: list[ItemResult]


# Cart recovery. The store plugin (charts/store/files/cart-recovery.php) lists carts;
# costs come from WooCommerce's own Cost of Goods Sold field.
class CartProduct(BaseModel):
    product_id: int
    name: str | None
    regular_price: str
    cogs: str | None = Field(description="Cost of goods per unit; null when the store has none")
    margin_pct: float | None = Field(description="(regular_price - cogs) / regular_price, in percent")
    on_sale: bool = Field(description="Already on sale; create_cart_offers will not stack a second discount")
    carts: int = Field(description="Abandoned carts holding it")
    units: int = Field(description="Units across those carts")
    cart_value: str = Field(description="regular_price x units")
    max_discount_pct: int = Field(
        description="Highest whole-percent discount that keeps the price at least min_margin_pct above cost, "
        "capped by the store's limit; 0 when cost is unknown or the margin is already thin"
    )


class AbandonedCarts(BaseModel):
    days: int
    carts: int = Field(description="Abandoned carts in the window")
    value: str = Field(description="Their total value at regular prices")
    min_margin_pct: float = Field(description="A sale price must stay at least this far above cost")
    products: list[CartProduct] = Field(description="Highest cart_value first")
    truncated: bool = Field(description="True if more products were in carts than were listed")


class OfferItem(BaseModel):
    model_config = {"extra": "forbid"}
    product_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200, description="Copied from get_abandoned_carts or get_inventory_health")
    regular_price: Money
    cogs: Money
    discount_pct: int = Field(ge=1, le=100, description="Whole percent off the regular price")
    sale_price: Money
    ends_in_days: int = Field(ge=1, le=90, description="The sale ends this many days from when it runs")


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


def Items(model: type[BaseModel]) -> Any:
    return Annotated[list[model], Field(min_length=1, max_length=MAX_BULK, description=f"1 to {MAX_BULK} items")]


def _bulk(results: list[ItemResult]) -> BulkResult:
    ok = sum(r.ok for r in results)
    return BulkResult(ok=ok, failed=len(results) - ok, results=results)


# Statuses that count as a sale. "refunded" stays in, so its refund is subtracted
# from net sales instead of the order silently vanishing from gross.
SALE_STATUSES = {"processing", "completed", "on-hold", "refunded"}
STOCK_WINDOW_DAYS = 30  # sales window behind the plan and clearance checks
PER_PAGE = 100
MAX_PAGES = 20  # shortcut: 2,000 rows per report, move to wc-analytics if stores outgrow it


def _money(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _product(p: dict[str, Any]) -> Product:
    return Product(
        id=p.get("id"),
        name=p.get("name"),
        price=p.get("price"),
        status=p.get("status"),
        stock_status=p.get("stock_status"),
        sku=p.get("sku") or None,
        url=p.get("permalink"),
    )


def _order(o: dict[str, Any], *, full: bool = False) -> OrderSummary:
    billing = o.get("billing") or {}
    out: dict[str, Any] = {
        "id": o.get("id"),
        "number": o.get("number"),
        "status": o.get("status"),
        "total": o.get("total"),
        "currency": o.get("currency"),
        "created_utc": o.get("date_created_gmt"),
        "customer": " ".join(
            x for x in (billing.get("first_name"), billing.get("last_name")) if x
        )
        or None,
        "items": [
            {
                "product_id": i.get("product_id"),
                "name": i.get("name"),
                "quantity": i.get("quantity"),
                "total": i.get("total"),
            }
            for i in (o.get("line_items") or [])
        ],
    }
    if full:
        shipping = o.get("shipping") or {}
        out["payment_method"] = o.get("payment_method_title")
        out["customer_note"] = o.get("customer_note") or None
        out["ship_to"] = (
            ", ".join(
                x
                for x in (
                    shipping.get("city"),
                    shipping.get("state"),
                    shipping.get("country"),
                )
                if x
            )
            or None
        )
    return Order(**out) if full else OrderSummary(**out)


def build_server(woo: WooClient, settings: Settings) -> MCPServer:
    store_timezone = settings.store_timezone
    min_margin = Decimal(str(settings.min_margin))
    server = MCPServer(
        "woocommerce",
        title="WooCommerce store",
        instructions=(
            "Tools for one WooCommerce store: its products, its orders, and "
            "reports over them (sales, refunds, best sellers, stock, product quality, "
            "customer history), cart recovery (abandoned carts and sale offers), and "
            "restock planning (inventory health, purchase orders, receiving stock, clearance sales). "
            "Prices and totals are strings in the store currency."
        ),
        version="0.1.0",
    )

    async def call(method: str, path: str, **kw: Any) -> Any:
        # WooError is a failure we anticipated (store down, bad id, bad key).
        # ToolError hands its message to the model instead of a bare crash.
        try:
            return await woo.request(method, path, **kw)
        except WooError as exc:
            raise ToolError(str(exc)) from exc

    async def all_pages(path: str, params: dict[str, Any]) -> tuple[list[Any], bool]:
        """Every row up to MAX_PAGES pages, and whether we stopped early."""
        rows: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            batch = await call(
                "GET", path, params={**params, "per_page": PER_PAGE, "page": page}
            )
            rows += batch
            if len(batch) < PER_PAGE:
                return rows, False
        return rows, True

    def date_range(start: str, end: str) -> tuple[str, str]:
        after = day_bounds_utc(start, store_timezone)[0]
        before = day_bounds_utc(end, store_timezone)[1]
        if after >= before:
            raise ToolError("start must be on or before end")
        return after, before

    async def sales_in(after: str, before: str) -> tuple[list[dict], bool]:
        rows, truncated = await all_pages(
            "/orders", {"after": after, "before": before, "dates_are_gmt": "true"}
        )
        return [o for o in rows if o.get("status") in SALE_STATUSES], truncated

    @server.tool(title="List products", annotations=READ)
    async def list_products(
        search: Annotated[
            str | None, Field(description="Words to match in the product name")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=50, description="How many to return")
        ] = 10,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1")] = 1,
    ) -> ProductList:
        """List products in the store, newest first, with price and stock status. Use `search` to find
        one by name (for example to get its id). For stock levels use get_low_stock_products or
        get_inventory_health; for products missing details use audit_product_quality."""
        rows = await call(
            "GET",
            "/products",
            params={
                "search": search,
                "per_page": limit,
                "page": page,
                "orderby": "date",
                "order": "desc",
            },
        )
        return ProductList(count=len(rows), products=[_product(p) for p in rows])

    @server.tool(title="Create product", annotations=WRITE)
    async def create_product(
        name: Annotated[
            str, Field(min_length=1, max_length=200, description="Product name")
        ],
        price: Annotated[
            str,
            Field(
                pattern=r"^\d+(\.\d{1,2})?$",
                description="Regular price as a number, e.g. '499' or '499.50'",
            ),
        ],
        stock_quantity: Annotated[
            int, Field(ge=0, description="Units in stock; the store tracks it and warns when low")
        ],
        description: Annotated[str | None, Field(max_length=2000)] = None,
        sku: Annotated[
            str | None,
            Field(max_length=64, description="Optional unique product code (an ID, not a quantity)"),
        ] = None,
    ) -> Product:
        """Create a new simple product with stock tracking on and publish it, so it appears
        on the storefront at once. Only for a product that does not exist yet: to change an existing
        product's price or description use bulk_update_product_prices or
        bulk_update_product_descriptions."""
        body: dict[str, Any] = {
            "name": name,
            "type": "simple",
            "status": "publish",
            "regular_price": price,
            "manage_stock": True,
            "stock_quantity": stock_quantity,
        }
        if description:
            body["description"] = description
        if sku:
            body["sku"] = sku
        return _product(await call("POST", "/products", json=body))

    @server.tool(
        title="Delete product",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=False
        ),
    )
    async def delete_product(
        product_id: Annotated[int, Field(ge=1, description="The product's numeric id")],
    ) -> Product:
        """Move a product to the trash, so it leaves the storefront. An admin can restore it."""
        return _product(await call("DELETE", f"/products/{product_id}"))

    @server.tool(title="List orders", annotations=READ)
    async def list_orders(
        on_date: Annotated[
            str | None,
            Field(
                description="Only orders placed on this day in the store's timezone: "
                "'today', 'yesterday', or YYYY-MM-DD. Omit for all dates."
            ),
        ] = None,
        status: Annotated[
            OrderStatus | None, Field(description="Only orders in this status")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=50, description="How many to return")
        ] = 20,
    ) -> OrderList:
        """List orders, newest first, with status and total. For "what came in today", pass
        on_date='today'. For one order's line items use get_order; for revenue or order counts over a
        period use get_sales_summary; for one customer's history use get_customer_timeline."""
        params: dict[str, Any] = {
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": status,
        }
        if on_date:
            after, before = day_bounds_utc(on_date, store_timezone)
            params.update({"after": after, "before": before, "dates_are_gmt": "true"})
        rows = await call("GET", "/orders", params=params)
        return OrderList(count=len(rows), orders=[_order(o) for o in rows])

    @server.tool(title="Get order", annotations=READ)
    async def get_order(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric id")],
    ) -> Order:
        """Get one order with its line items, payment method and destination. Needs the order id;
        if the user gives none, find it with list_orders or get_customer_timeline first."""
        return _order(await call("GET", f"/orders/{order_id}"), full=True)

    @server.tool(title="Update order status", annotations=WRITE)
    async def update_order_status(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric id")],
        status: Annotated[OrderStatus, Field(description="The new status")],
    ) -> OrderSummary:
        """Move one order to a new status, for example 'processing' to 'completed'. For several
        orders, or to change a shipping address, use bulk_update_orders; to give money back use
        bulk_refund_orders."""
        return _order(await call("PUT", f"/orders/{order_id}", json={"status": status}))

    @server.tool(title="Sales summary", annotations=READ)
    async def get_sales_summary(
        start: Annotated[
            str,
            Field(description="First day, in the store's timezone: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
        end: Annotated[
            str,
            Field(description="Last day, included: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
    ) -> SalesSummary:
        """Total sales, order count, refunds and average order value for orders placed
        between two days. Counts processing, completed, on-hold and refunded orders. Use for any
        "how much did we sell / how many orders" question; for a single day pass the same start and end."""
        after, before = date_range(start, end)
        orders, truncated = await sales_in(after, before)
        gross = sum((Decimal(o.get("total") or "0") for o in orders), Decimal(0))
        refunded = sum(
            (abs(Decimal(r.get("total") or "0")) for o in orders for r in o.get("refunds") or []),
            Decimal(0),
        )
        return SalesSummary(
            start_utc=after,
            end_utc=before,
            currency=orders[0].get("currency") if orders else None,
            order_count=len(orders),
            gross_sales=_money(gross),
            refunds=_money(refunded),
            net_sales=_money(gross - refunded),
            average_order_value=_money(gross / len(orders) if orders else Decimal(0)),
            truncated=truncated,
        )

    @server.tool(title="Refund summary", annotations=READ)
    async def get_refund_summary(
        start: Annotated[
            str,
            Field(description="First day, in the store's timezone: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
        end: Annotated[
            str,
            Field(description="Last day, included: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
    ) -> RefundSummary:
        """Refunds made between two days (by refund date, not order date): count, total
        and each refund with its reason. For sales net of refunds use get_sales_summary."""
        after, before = date_range(start, end)
        # A refund touches its order, so any order refunded in range was modified
        # since `after`, however old the order itself is.
        orders, truncated = await all_pages(
            "/orders", {"modified_after": after, "dates_are_gmt": "true"}
        )
        lines: list[RefundLine] = []
        for o in orders:
            if not o.get("refunds"):
                continue
            for r in await call("GET", f"/orders/{o['id']}/refunds"):
                when = r.get("date_created_gmt") or ""
                if after <= when < before:
                    lines.append(
                        RefundLine(
                            order_id=o.get("id"),
                            refund_id=r.get("id"),
                            amount=_money(abs(Decimal(r.get("amount") or "0"))),
                            reason=r.get("reason") or None,
                            date_utc=when,
                        )
                    )
        total = sum((Decimal(x.amount or "0") for x in lines), Decimal(0))
        return RefundSummary(
            start_utc=after,
            end_utc=before,
            currency=orders[0].get("currency") if orders else None,
            refund_count=len(lines),
            refund_total=_money(total),
            orders_affected=len({x.order_id for x in lines}),
            refunds=lines,
            truncated=truncated,
        )

    @server.tool(title="Best-selling products", annotations=READ)
    async def get_best_selling_products(
        start: Annotated[
            str,
            Field(description="First day, in the store's timezone: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
        end: Annotated[
            str,
            Field(description="Last day, included: 'today', 'yesterday' or YYYY-MM-DD"),
        ],
        by: Annotated[
            Literal["units", "revenue"], Field(description="Rank by units sold or by revenue")
        ] = "units",
        limit: Annotated[int, Field(ge=1, le=50, description="How many to return")] = 10,
    ) -> BestSellers:
        """Products ranked by units sold or revenue, over orders placed between two days."""
        after, before = date_range(start, end)
        orders, truncated = await sales_in(after, before)
        units: dict[Any, int] = defaultdict(int)
        revenue: dict[Any, Decimal] = defaultdict(Decimal)
        names: dict[Any, str | None] = {}
        for o in orders:
            for item in o.get("line_items") or []:
                pid = item.get("product_id")
                units[pid] += item.get("quantity") or 0
                revenue[pid] += Decimal(item.get("total") or "0")
                names.setdefault(pid, item.get("name"))
        key = units if by == "units" else revenue
        ranked = sorted(names, key=lambda pid: key[pid], reverse=True)[:limit]
        return BestSellers(
            start_utc=after,
            end_utc=before,
            ranked_by=by,
            products=[
                BestSeller(product_id=pid, name=names[pid], units=units[pid], revenue=_money(revenue[pid]))
                for pid in ranked
            ],
            truncated=truncated,
        )

    @server.tool(title="Low-stock products", annotations=READ)
    async def get_low_stock_products(
        threshold: Annotated[
            int, Field(ge=0, description="List products with fewer units in stock than this")
        ] = 5,
    ) -> LowStockList:
        """Products with stock tracking on and fewer than `threshold` units, lowest first.
        Variable products are only checked at the parent level, not per variation. A quick
        "what is running low" list; for days of stock left, reorder quantities or overstock use
        get_inventory_health."""
        rows, truncated = await all_pages("/products", {})
        low = [
            p
            for p in rows
            if p.get("manage_stock") and p.get("stock_quantity") is not None
            and p["stock_quantity"] < threshold
        ]
        low.sort(key=lambda p: p["stock_quantity"])
        return LowStockList(
            threshold=threshold,
            count=len(low),
            products=[
                LowStockProduct(**_product(p).model_dump(), stock_quantity=p["stock_quantity"])
                for p in low
            ],
            truncated=truncated,
        )

    @server.tool(title="Audit product quality", annotations=READ)
    async def audit_product_quality() -> QualityAudit:
        """Find products missing a description, an image, a price or a real category
        ("Uncategorized" counts as missing). Fill in descriptions with bulk_update_product_descriptions."""
        rows, truncated = await all_pages("/products", {})
        flagged: list[ProductIssues] = []
        for p in rows:
            issues = []
            if not (p.get("description") or p.get("short_description") or "").strip():
                issues.append("missing_description")
            if not p.get("images"):
                issues.append("missing_image")
            if not p.get("price"):
                issues.append("missing_price")
            if not [c for c in p.get("categories") or [] if c.get("slug") != "uncategorized"]:
                issues.append("missing_category")
            if issues:
                flagged.append(ProductIssues(id=p.get("id"), name=p.get("name"), issues=issues))
        return QualityAudit(checked=len(rows), count=len(flagged), products=flagged, truncated=truncated)

    @server.tool(title="Customer timeline", annotations=READ)
    async def get_customer_timeline(
        customer_id: Annotated[
            int | None, Field(ge=1, description="Registered customer's id")
        ] = None,
        email: Annotated[
            str | None,
            Field(max_length=200, description="Billing email; also finds guest orders"),
        ] = None,
    ) -> CustomerTimeline:
        """Every order of one customer with its status, oldest first. Pass customer_id
        or email, not both."""
        if (customer_id is None) == (not email):
            raise ToolError("Pass exactly one of customer_id or email")
        params = {"customer": customer_id} if customer_id else {"search": email}
        rows, truncated = await all_pages("/orders", params)
        if email:
            # search also matches names and addresses; keep only this billing email
            rows = [
                o for o in rows
                if ((o.get("billing") or {}).get("email") or "").lower() == email.lower()
            ]
        rows.sort(key=lambda o: o.get("date_created_gmt") or "")
        spent = sum(
            (Decimal(o.get("total") or "0") for o in rows if o.get("status") in SALE_STATUSES),
            Decimal(0),
        )
        return CustomerTimeline(
            customer=_order(rows[-1]).customer if rows else None,
            order_count=len(rows),
            total_spent=_money(spent),
            orders=[
                TimelineOrder(
                    id=o.get("id"),
                    number=o.get("number"),
                    status=o.get("status"),
                    total=o.get("total"),
                    created_utc=o.get("date_created_gmt"),
                )
                for o in rows
            ],
            truncated=truncated,
        )

    async def batch(path: str, ids: list[int], updates: list[dict]) -> BulkResult:
        """One POST <path>/batch; Woo answers per item, with an `error` on failures."""
        rows = (await call("POST", f"{path}/batch", json={"update": updates})).get("update") or []
        results = []
        for n, i in enumerate(ids):
            row = rows[n] if n < len(rows) else {"error": {"message": "no answer from the store"}}
            err = row.get("error")
            results.append(ItemResult(id=i, ok=not err, error=err and str(err.get("message"))))
        return _bulk(results)

    def updates_of(items: list[BaseModel], id_field: str) -> tuple[list[int], list[dict]]:
        ids = [getattr(it, id_field) for it in items]
        rows = [
            {"id": getattr(it, id_field), **it.model_dump(exclude={id_field}, exclude_none=True)}
            for it in items
        ]
        return ids, rows

    @server.tool(title="Bulk update product descriptions", annotations=WRITE)
    async def bulk_update_product_descriptions(items: Items(DescriptionItem)) -> BulkResult:
        """Set the description and/or short description of up to 100 products in one call
        (one product is fine too). Reports success or the error for each product."""
        return await batch("/products", *updates_of(items, "product_id"))

    @server.tool(title="Bulk update product prices", annotations=WRITE)
    async def bulk_update_product_prices(items: Items(PriceItem)) -> BulkResult:
        """Set the regular and/or sale price of up to 100 products in one call (one product is
        fine too). An empty sale price is not allowed; omit it to leave the sale price as is.
        To discount products in abandoned carts or overstocked ones, use create_cart_offers or
        create_clearance_offers instead: they keep the price above cost and end the sale by themselves."""
        return await batch("/products", *updates_of(items, "product_id"))

    @server.tool(title="Bulk update orders", annotations=WRITE)
    async def bulk_update_orders(items: Items(OrderUpdateItem)) -> BulkResult:
        """Change the status and/or shipping address of up to 100 orders in one call (one order is
        fine too). To give money back use bulk_refund_orders."""
        return await batch("/orders", *updates_of(items, "order_id"))

    @server.tool(
        title="Bulk refund orders",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    )
    async def bulk_refund_orders(items: Items(RefundItem)) -> BulkResult:
        """Record a refund of an explicit amount on up to 100 orders. Each amount must not be
        more than what is left to refund on that order. The store records the refund; no
        payment is reversed (cash on delivery). Each order may appear once per call."""
        ids = [it.order_id for it in items]
        if len(set(ids)) != len(ids):
            raise ToolError("Each order may appear only once per call")
        limit = anyio.Semaphore(5)

        async def one(it: RefundItem) -> ItemResult:
            async with limit:
                try:
                    order = await woo.request("GET", f"/orders/{it.order_id}")
                    refunded = sum(
                        (abs(Decimal(r.get("total") or "0")) for r in order.get("refunds") or []),
                        Decimal(0),
                    )
                    left = Decimal(order.get("total") or "0") - refunded
                    if Decimal(it.amount) > left:
                        return ItemResult(id=it.order_id, ok=False, error=f"only {_money(left)} left to refund")
                    body = {"amount": it.amount, "api_refund": False}
                    if it.reason:
                        body["reason"] = it.reason
                    await woo.request("POST", f"/orders/{it.order_id}/refunds", json=body)
                    return ItemResult(id=it.order_id, ok=True)
                except WooError as exc:
                    return ItemResult(id=it.order_id, ok=False, error=str(exc))

        results: list[ItemResult] = [None] * len(items)  # type: ignore[list-item]

        async def run(i: int, it: RefundItem) -> None:
            results[i] = await one(it)

        async with anyio.create_task_group() as tg:
            for i, it in enumerate(items):
                tg.start_soon(run, i, it)
        return _bulk(results)

    # ---------------------------------------------------------- cart recovery

    async def products_by_id(ids: set[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        ordered = sorted(ids)
        for i in range(0, len(ordered), PER_PAGE):
            chunk = ordered[i : i + PER_PAGE]
            rows = await call("GET", "/products", params={"include": ",".join(map(str, chunk)), "per_page": PER_PAGE})
            out.update({p["id"]: p for p in rows})
        return out

    def cogs_of(p: dict) -> Decimal | None:
        value = (p.get("cost_of_goods_sold") or {}).get("total_value")
        cost = Decimal(str(value)) if value not in (None, "") else None
        return cost if cost and cost > 0 else None  # Woo reports an unset cost as 0

    def floor_of(cost: Decimal) -> Decimal:
        return cost * (1 + min_margin)

    def max_discount(price: Decimal, cost: Decimal | None) -> int:
        if cost is None or price <= 0:
            return 0
        return max(0, min(math.floor((1 - floor_of(cost) / price) * 100), settings.max_discount_pct))

    @server.tool(title="Abandoned carts", annotations=READ)
    async def get_abandoned_carts(
        days: Annotated[int, Field(ge=1, le=90, description="Carts last changed within this many days")] = 20,
        limit: Annotated[int, Field(ge=1, le=100, description="How many products to return")] = 20,
    ) -> AbandonedCarts:
        """Products sitting in logged-in customers' abandoned carts, with the facts to decide a
        discount: price, cost of goods, margin, how many carts hold it, and max_discount_pct, the
        highest discount that keeps the price at least min_margin_pct above cost. No customer data.
        To discount products, call create_cart_offers; every offer needs an admin's approval."""
        carts = await call("GET", "/cart-recovery/carts", params={"days": days})
        n_carts: dict[int, int] = defaultdict(int)
        units: dict[int, int] = defaultdict(int)
        for cart in carts:
            for line in cart.get("items") or []:
                units[line["product_id"]] += line.get("quantity") or 0
            for pid in {line["product_id"] for line in cart.get("items") or []}:
                n_carts[pid] += 1
        products = await products_by_id(set(n_carts))
        rows = []
        for pid, p in products.items():
            price = Decimal(p.get("regular_price") or "0")
            cost = cogs_of(p)
            rows.append(CartProduct(
                product_id=pid,
                name=p.get("name"),
                regular_price=_money(price),
                cogs=cost and _money(cost),
                margin_pct=round(float((price - cost) / price * 100), 1) if cost and price else None,
                on_sale=bool(p.get("on_sale")),
                carts=n_carts[pid],
                units=units[pid],
                cart_value=_money(price * units[pid]),
                max_discount_pct=max_discount(price, cost),
            ))
        rows.sort(key=lambda r: Decimal(r.cart_value), reverse=True)
        return AbandonedCarts(
            days=days,
            carts=len(carts),
            value=_money(sum((Decimal(r.cart_value) for r in rows), Decimal(0))),
            min_margin_pct=float(min_margin * 100),
            products=rows[:limit],
            truncated=len(rows) > limit,
        )

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

    @server.tool(title="Create cart recovery offers", annotations=WRITE)
    async def create_cart_offers(items: Items(OfferItem)) -> BulkResult:
        """Put products from abandoned carts on sale for a few days; the sale ends by itself.
        Every call needs an admin's approval: when the gateway refuses it, call
        gateway__request_approval with the same arguments and a `summary` of your analysis,
        then tell the user the approval id (the offer runs when approved, whether or not you wait).
        Per item: copy product_id, name, regular_price and cogs from get_abandoned_carts; pick
        discount_pct <= max_discount_pct; sale_price = regular_price x (1 - discount_pct/100),
        rounded UP to 2 decimals. When it runs, every number is checked against the store and an
        item that does not match, is under the cost floor, is already on sale or is in no recent
        cart is refused with the reason; the others still go on sale."""
        window = settings.cart_window_days
        in_carts = {
            line["product_id"]
            for cart in await call("GET", "/cart-recovery/carts", params={"days": window})
            for line in cart.get("items") or []
        }
        return await put_on_sale(
            items, lambda it: None if it.product_id in in_carts else f"no cart changed in the last {window} days holds it"
        )


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

    return server
