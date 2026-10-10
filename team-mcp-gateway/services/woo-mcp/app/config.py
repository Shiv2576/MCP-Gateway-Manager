"""Settings, read once from environment variables.

Nothing here has a secret default. In Kubernetes the three secret values come
from a Secret (see charts/woo-mcp); locally you export them in your shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. The process should not start."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


@dataclass(frozen=True)
class Settings:
    # Where to SEND requests: the in-cluster Service of WordPress.
    woo_internal_url: str
    # What WordPress believes its own address is (Settings > General > Site
    # Address). WooCommerce builds the OAuth signature base string from this,
    # not from the URL we actually dialled, so the two must be kept apart.
    woo_public_url: str
    woo_consumer_key: str
    woo_consumer_secret: str
    # "oauth1" signs every request (required by WooCommerce over plain HTTP).
    # "basic" sends the key pair as HTTP Basic (WooCommerce allows it over HTTPS only).
    woo_auth_mode: str
    woo_timeout_s: float
    # The one credential a caller (the gateway) must present.
    mcp_auth_token: str
    # Timezone used to turn "today" into a date range.
    store_timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        internal = _required("WOO_INTERNAL_URL").rstrip("/")
        public = os.environ.get("WOO_PUBLIC_URL", "").strip().rstrip("/") or internal
        for label, url in (("WOO_INTERNAL_URL", internal), ("WOO_PUBLIC_URL", public)):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ConfigError(f"{label} must be an http(s) URL, got {url!r}")

        mode = os.environ.get("WOO_AUTH_MODE", "auto").strip().lower()
        if mode == "auto":
            mode = "basic" if internal.startswith("https://") else "oauth1"
        if mode not in ("oauth1", "basic"):
            raise ConfigError("WOO_AUTH_MODE must be auto, oauth1 or basic")

        token = _required("MCP_AUTH_TOKEN")
        if len(token) < 24:
            raise ConfigError("MCP_AUTH_TOKEN must be at least 24 characters")

        return cls(
            woo_internal_url=internal,
            woo_public_url=public,
            woo_consumer_key=_required("WOO_CONSUMER_KEY"),
            woo_consumer_secret=_required("WOO_CONSUMER_SECRET"),
            woo_auth_mode=mode,
            woo_timeout_s=float(os.environ.get("WOO_TIMEOUT_S", "10")),
            mcp_auth_token=token,
            store_timezone=os.environ.get("STORE_TIMEZONE", "Asia/Kolkata"),
        )
