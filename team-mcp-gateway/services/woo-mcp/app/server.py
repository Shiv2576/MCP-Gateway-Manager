"""The MCP tools: six things an agent may do to the store.

Each tool is a thin, typed wrapper over one WooCommerce REST call. The tool
descriptions and parameter descriptions below are what the language model
reads when it decides which tool to call, so they are written for the model.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

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
            "Tools for one WooCommerce store: its products and its orders. "
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
        description: Annotated[str | None, Field(max_length=2000)] = None,
        sku: Annotated[
            str | None, Field(max_length=64, description="Optional unique stock code")
        ] = None,
    ) -> Product:
        """Create a simple product and publish it, so it appears on the storefront at once."""
        body: dict[str, Any] = {
            "name": name,
            "type": "simple",
            "status": "publish",
            "regular_price": price,
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

    return server
