"""A stand-in for WooCommerce, small enough to read in one sitting.

It keeps products and orders in memory and, like the real thing over plain
HTTP, refuses any request whose OAuth 1.0a signature does not check out. The
check is done by `oauthlib`, an independent implementation, so a pass here
means our hand-written signer agrees with a library other people rely on.

The base string is built from PUBLIC_ORIGIN, not from the URL the request
arrived on. That mirrors WooCommerce, which signs against the site's home URL.
"""

from __future__ import annotations

import time
from decimal import Decimal

from oauthlib.oauth1.rfc5849 import signature as oauth_sig
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

CONSUMER_KEY = "ck_" + "a" * 40
CONSUMER_SECRET = "cs_" + "b" * 40
PUBLIC_ORIGIN = "http://store.localhost"
# A second key, made by another WordPress user with Read permission only.
READ_KEY = "ck_" + "c" * 40
READ_SECRET = "cs_" + "d" * 40
KEYS = {CONSUMER_KEY: (CONSUMER_SECRET, "read_write"), READ_KEY: (READ_SECRET, "read")}


def build_fake_woo() -> Starlette:
    products: dict[int, dict] = {
        1: {
            "id": 1,
            "name": "Trail Mix",
            "price": "199",
            "status": "publish",
            "stock_status": "instock",
            "sku": "TM-1",
            "permalink": f"{PUBLIC_ORIGIN}/p/trail-mix",
        },
    }
    orders: dict[int, dict] = {}
    carts: list[dict] = []  # the store plugin's carts: {user_id, updated_at, items}
    refunds: dict[int, list[dict]] = {}  # order id -> its refunds, as /orders/{id}/refunds returns
    purchase_orders: dict[int, dict] = {}  # the restock plugin's purchase orders
    seen_nonces: set[str] = set()
    state = {"next_id": 100, "slow": False, "no_restock_plugin": False}
    used_keys: list[str] = []  # the key of every request that got in

    def authorised(request: Request) -> bool:
        params = dict(request.query_params)
        supplied = params.pop("oauth_signature", None)
        if not supplied or params.get("oauth_consumer_key") not in KEYS:
            return False
        if params.get("oauth_signature_method") != "HMAC-SHA256":
            return False
        if abs(time.time() - int(params.get("oauth_timestamp", "0"))) > 900:
            return False
        nonce = params.get("oauth_nonce", "")
        if not nonce or nonce in seen_nonces:
            return False
        seen_nonces.add(nonce)
        normalized = oauth_sig.normalize_parameters(list(params.items()))
        base = oauth_sig.signature_base_string(
            request.method, PUBLIC_ORIGIN + request.url.path, normalized
        )
        expected = oauth_sig.sign_hmac_sha256(base, KEYS[params["oauth_consumer_key"]][0], "")
        return oauth_sig.safe_string_equals(expected, supplied)

    def guard(handler):
        async def wrapped(request: Request):
            if not authorised(request):
                return JSONResponse(
                    {
                        "code": "woocommerce_rest_authentication_error",
                        "message": "Invalid signature - provided signature does not match.",
                    },
                    status_code=401,
                )
            key = request.query_params["oauth_consumer_key"]
            used_keys.append(key)
            if request.method != "GET" and KEYS[key][1] == "read":
                return JSONResponse(
                    {"code": "woocommerce_rest_cannot_edit", "message": "Sorry, you are not allowed to edit this resource."},
                    status_code=401,
                )
            if state["slow"]:
                import anyio

                await anyio.sleep(5)
            return await handler(request)

        return wrapped

    async def list_products(request: Request):
        q = (request.query_params.get("search") or "").lower()
        rows = [p for p in products.values() if q in p["name"].lower()]
        if request.query_params.get("include"):
            wanted = {int(i) for i in request.query_params["include"].split(",")}
            rows = [p for p in rows if p["id"] in wanted]
        rows.sort(key=lambda p: -p["id"])
        return JSONResponse(page_of(rows, request))

    async def create_product(request: Request):
        body = await request.json()
        pid = state["next_id"]
        state["next_id"] += 1
        products[pid] = {
            "id": pid,
            "name": body["name"],
            "price": body["regular_price"],
            "status": body.get("status", "draft"),
            "stock_status": "instock",
            "sku": body.get("sku", ""),
            "manage_stock": body.get("manage_stock", False),
            "stock_quantity": body.get("stock_quantity"),
            "permalink": f"{PUBLIC_ORIGIN}/p/{pid}",
        }
        return JSONResponse(products[pid], status_code=201)

    async def trash_product(request: Request):
        pid = int(request.path_params["pid"])
        if pid not in products:
            return JSONResponse(
                {"code": "woocommerce_rest_product_invalid_id", "message": "Invalid ID."},
                status_code=404,
            )
        products[pid]["status"] = "trash"
        return JSONResponse(products.pop(pid))

    async def list_orders(request: Request):
        qp = request.query_params
        rows = list(orders.values())
        if qp.get("status"):
            rows = [o for o in rows if o["status"] == qp["status"]]
        if qp.get("after"):
            assert qp.get("dates_are_gmt") == "true"
            rows = [
                o for o in rows if qp["after"] <= o["date_created_gmt"] < qp["before"]
            ]
        if qp.get("modified_after"):
            assert qp.get("dates_are_gmt") == "true"
            rows = [
                o for o in rows
                if qp["modified_after"] <= o.get("date_modified_gmt", o["date_created_gmt"])
            ]
        if qp.get("customer"):
            rows = [o for o in rows if o.get("customer_id") == int(qp["customer"])]
        if qp.get("search"):
            rows = [o for o in rows if qp["search"].lower() in str(o).lower()]
        rows.sort(key=lambda o: o["date_created_gmt"], reverse=True)
        return JSONResponse(page_of(rows, request))

    async def order_refunds(request: Request):
        oid = int(request.path_params["oid"])
        if request.method == "POST":
            body = await request.json()
            assert body.get("api_refund") is False  # cash on delivery: record only
            row = {"id": state["next_id"], "amount": body["amount"], "reason": body.get("reason", ""),
                   "date_created_gmt": "2026-10-13T10:00:00"}
            state["next_id"] += 1
            refunds.setdefault(oid, []).append(row)
            orders[oid].setdefault("refunds", []).append(
                {"id": row["id"], "reason": row["reason"], "total": f"-{body['amount']}"}
            )
            return JSONResponse(row, status_code=201)
        return JSONResponse(refunds.get(oid, []))

    def batch_of(table: dict[int, dict], code: str):
        # Like WooCommerce: one answer per item; a bad id gets an error, not a failed batch.
        async def handler(request: Request):
            out = []
            for row in (await request.json()).get("update", []):
                if row["id"] not in table:
                    out.append({"id": row["id"], "error": {"code": code, "message": "Invalid ID."}})
                    continue
                for k, v in row.items():
                    table[row["id"]][k] = {**table[row["id"]].get(k, {}), **v} if isinstance(v, dict) else v
                if row.get("sale_price"):
                    table[row["id"]]["on_sale"] = True
                out.append(table[row["id"]])
            return JSONResponse({"update": out})

        return handler

    async def one_order(request: Request):
        oid = int(request.path_params["oid"])
        if oid not in orders:
            return JSONResponse(
                {
                    "code": "woocommerce_rest_shop_order_invalid_id",
                    "message": "Invalid ID.",
                },
                status_code=404,
            )
        if request.method == "PUT":
            orders[oid]["status"] = (await request.json())["status"]
        return JSONResponse(orders[oid])

    async def list_carts(request: Request):
        since = time.time() - int(request.query_params.get("days", 20)) * 86400
        return JSONResponse([c for c in carts if c["updated_at"] >= since and c["items"]])

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

    def page_of(rows: list, request: Request) -> list:
        per_page = int(request.query_params.get("per_page", 10))
        page = int(request.query_params.get("page", 1))
        return rows[(page - 1) * per_page : page * per_page]

    app = Starlette(
        routes=[
            Route("/wp-json/wc/v3/products", guard(list_products), methods=["GET"]),
            Route("/wp-json/wc/v3/products", guard(create_product), methods=["POST"]),
            Route(
                "/wp-json/wc/v3/products/{pid:int}",
                guard(trash_product),
                methods=["DELETE"],
            ),
            Route("/wp-json/wc/v3/orders", guard(list_orders), methods=["GET"]),
            Route("/wp-json/wc/v3/cart-recovery/carts", guard(list_carts), methods=["GET"]),
            Route("/wp-json/wc/v3/restock/orders", guard(restock_route(list_pos)), methods=["GET"]),
            Route("/wp-json/wc/v3/restock/orders", guard(restock_route(create_po)), methods=["POST"]),
            Route("/wp-json/wc/v3/restock/orders/{po:int}/receive", guard(restock_route(receive_po)), methods=["POST"]),
            Route(
                "/wp-json/wc/v3/orders/{oid:int}",
                guard(one_order),
                methods=["GET", "PUT"],
            ),
            Route(
                "/wp-json/wc/v3/orders/{oid:int}/refunds",
                guard(order_refunds),
                methods=["GET", "POST"],
            ),
            Route(
                "/wp-json/wc/v3/products/batch",
                guard(batch_of(products, "woocommerce_rest_product_invalid_id")),
                methods=["POST"],
            ),
            Route(
                "/wp-json/wc/v3/orders/batch",
                guard(batch_of(orders, "woocommerce_rest_shop_order_invalid_id")),
                methods=["POST"],
            ),
        ]
    )
    app.state.orders = orders
    app.state.products = products
    app.state.carts = carts
    app.state.refunds = refunds
    app.state.purchase_orders = purchase_orders
    app.state.flags = state
    app.state.used_keys = used_keys
    return app


def make_order(
    oid: int, created_gmt: str, status: str = "processing", total: str = "499.00"
) -> dict:
    return {
        "id": oid,
        "number": str(oid),
        "status": status,
        "total": total,
        "currency": "INR",
        "date_created_gmt": created_gmt,
        "billing": {
            "first_name": "Priya",
            "last_name": "Sharma",
            "email": "priya@example.com",
        },
        "shipping": {"city": "Pune", "state": "MH", "country": "IN"},
        "payment_method_title": "Cash on delivery",
        "customer_note": "",
        "line_items": [
            {"product_id": 1, "name": "Trail Mix", "quantity": 2, "total": total}
        ],
    }
