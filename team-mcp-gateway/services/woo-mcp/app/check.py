"""One-command check that this server can authenticate to your real store.

Run it before building anything on top:
"""

from __future__ import annotations

import asyncio
import sys

from .config import Settings
from .woo import WooClient, WooError


async def main() -> int:
    settings = Settings.from_env()
    woo = WooClient(settings)
    print(
        f"store={settings.woo_internal_url} signing-as={settings.woo_public_url} "
        f"auth={settings.woo_auth_mode}"
    )
    try:
        products = await woo.request("GET", "/products", params={"per_page": 1})
        orders = await woo.request("GET", "/orders", params={"per_page": 1})
    except WooError as exc:
        print(f"FAILED: {exc}")
        print(
            "If this says 'Invalid signature': WOO_PUBLIC_URL must equal the WordPress "
            "Site Address exactly (host and any sub-path)."
        )
        return 1
    finally:
        await woo.aclose()
    print(
        f"OK: read access works ({len(products)} product, {len(orders)} order returned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
