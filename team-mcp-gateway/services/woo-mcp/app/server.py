"""The MCP tools: six things an agent may do to the store, six reports, four bulk writes.

The first six are thin, typed wrappers over one WooCommerce REST call. The
reports page through orders or products and add them up here, so dates follow
the store's timezone (WooCommerce's own /reports endpoints do not). The tool
descriptions and parameter descriptions below are what the language model
reads when it decides which tool to call, so they are written for the model.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import anyio

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

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


class BulkResult(BaseModel):
    ok: int = Field(description="Items that succeeded")
    failed: int = Field(description="Items that failed")
    results: list[ItemResult]


def Items(model: type[BaseModel]) -> Any:
    return Annotated[list[model], Field(min_length=1, max_length=MAX_BULK, description=f"1 to {MAX_BULK} items")]


def _bulk(results: list[ItemResult]) -> BulkResult:
    ok = sum(r.ok for r in results)
    return BulkResult(ok=ok, failed=len(results) - ok, results=results)


# Statuses that count as a sale. "refunded" stays in, so its refund is subtracted
# from net sales instead of the order silently vanishing from gross.
SALE_STATUSES = {"processing", "completed", "on-hold", "refunded"}
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


def build_server(woo: WooClient, store_timezone: str) -> MCPServer:
    server = MCPServer(
        "woocommerce",
        title="WooCommerce store",
        instructions=(
            "Tools for one WooCommerce store: its products, its orders, and "
            "reports over them (sales, refunds, best sellers, stock, product quality, "
            "customer history). "
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
        """List products in the store, newest first. Use `search` to find one by name."""
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
        """Create a simple product with stock tracking on and publish it, so it appears
        on the storefront at once."""
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
        """List orders, newest first. For "what came in today", pass on_date='today'."""
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
        """Get one order with its line items, payment method and destination."""
        return _order(await call("GET", f"/orders/{order_id}"), full=True)

    @server.tool(title="Update order status", annotations=WRITE)
    async def update_order_status(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric id")],
        status: Annotated[OrderStatus, Field(description="The new status")],
    ) -> OrderSummary:
        """Move an order to a new status, for example 'processing' to 'completed'."""
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
        between two days. Counts processing, completed, on-hold and refunded orders."""
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
        and each refund with its reason."""
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
        Variable products are only checked at the parent level, not per variation."""
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
        ("Uncategorized" counts as missing)."""
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
        """Set the description and/or short description of up to 100 products in one call.
        Reports success or the error for each product."""
        return await batch("/products", *updates_of(items, "product_id"))

    @server.tool(title="Bulk update product prices", annotations=WRITE)
    async def bulk_update_product_prices(items: Items(PriceItem)) -> BulkResult:
        """Set the regular and/or sale price of up to 100 products in one call.
        An empty sale price is not allowed; omit it to leave the sale price as is."""
        return await batch("/products", *updates_of(items, "product_id"))

    @server.tool(title="Bulk update orders", annotations=WRITE)
    async def bulk_update_orders(items: Items(OrderUpdateItem)) -> BulkResult:
        """Change the status and/or shipping address of up to 100 orders in one call."""
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

    return server
