"""A small async client for the WooCommerce REST API (wc/v3).

Why REST and not WP-CLI: REST needs only a network path and a revocable,
scoped API key. WP-CLI would need a shell inside the WordPress pod (kubectl
exec, so Kubernetes API rights) or a second copy of the WordPress files and
database password in this pod. REST keeps this server a plain network client.

Authentication has one trap. Inside the cluster we talk to WordPress over
plain HTTP, and WooCommerce refuses simple key-and-secret (Basic) auth unless
the request is HTTPS. Over HTTP it expects every request to carry an OAuth 1.0a
signature. `sign_oauth1` below produces that signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from .config import Settings

# The caller's own WooCommerce key for this request (X-Woo-Credentials, see main.py).
# Set, it is used instead of the key in our env, so WooCommerce acts as that person.
PERSONAL_KEY: ContextVar[tuple[str, str] | None] = ContextVar("personal_key", default=None)

API_PREFIX = "/wp-json/wc/v3"


class WooError(Exception):
    """A failure talking to the store, with a message safe to show a caller."""


def _enc(value: str) -> str:
    """Percent-encode the way OAuth 1.0a requires (RFC 3986, nothing left 'safe')."""
    return quote(str(value), safe="-._~")


def sign_oauth1(
    method: str,
    base_url: str,
    params: dict[str, str],
    consumer_key: str,
    consumer_secret: str,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Return `params` plus the oauth_* parameters, including the signature.

    Steps, in plain words:
      1. Add four oauth fields (who I am, a random nonce, the time, the method).
      2. Sort every parameter by name and glue them into one string.
      3. Build the "base string": METHOD & url & that-parameter-string.
      4. HMAC-SHA256 the base string, keyed with "<consumer_secret>&".
    The server repeats the same steps and compares. The secret never travels.
    """
    signed = dict(params)
    signed.update(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce or secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": str(timestamp or int(time.time())),
        }
    )
    pairs = sorted((_enc(k), _enc(v)) for k, v in signed.items())
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base_string = "&".join([method.upper(), _enc(base_url), _enc(param_string)])
    key = f"{consumer_secret}&".encode()
    digest = hmac.new(key, base_string.encode(), hashlib.sha256).digest()
    signed["oauth_signature"] = base64.b64encode(digest).decode()
    return signed


class WooClient:
    def __init__(self, settings: Settings, http: httpx2.AsyncClient | None = None):
        self._s = settings
        self._http = http or httpx2.AsyncClient(timeout=settings.woo_timeout_s)
        public = urlsplit(settings.woo_public_url)
        internal = urlsplit(settings.woo_internal_url)
        # WooCommerce signs against "<scheme>://<site host><path>", where the
        # scheme is whatever THIS request arrived as. We arrive as http inside
        # the cluster even when the public site is https, so use our scheme
        # with the public host.
        self._sign_origin = (
            f"{internal.scheme}://{public.netloc}{public.path.rstrip('/')}"
        )
        # Send the public hostname as Host so WordPress sees its own name.
        self._host_header = public.netloc

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean = {k: str(v) for k, v in (params or {}).items() if v is not None}
        url = f"{self._s.woo_internal_url}{API_PREFIX}{path}"
        headers = {"Host": self._host_header, "Accept": "application/json"}
        auth = None
        key, secret = PERSONAL_KEY.get() or (
            self._s.woo_consumer_key,
            self._s.woo_consumer_secret,
        )
        if self._s.woo_auth_mode == "oauth1":
            clean = sign_oauth1(
                method, f"{self._sign_origin}{API_PREFIX}{path}", clean, key, secret
            )
        else:
            auth = httpx2.BasicAuth(key, secret)

        try:
            response = await self._http.request(
                method, url, params=clean, json=json, headers=headers, auth=auth
            )
        except httpx2.TimeoutException as exc:
            raise WooError(
                f"The store did not answer within {self._s.woo_timeout_s:g}s"
            ) from exc
        except httpx2.HTTPError as exc:
            raise WooError(f"Could not reach the store ({type(exc).__name__})") from exc

        if response.status_code >= 400:
            raise WooError(_describe_error(response))
        try:
            return response.json()
        except ValueError as exc:
            raise WooError("The store returned a response that is not JSON") from exc


def _describe_error(response: httpx2.Response) -> str:
    """Turn a WooCommerce error into one readable line, without leaking internals."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("code") or "")
    except ValueError:
        pass
    status = response.status_code
    if status in (401, 403):
        hint = "the store rejected this server's API key"
    elif status == 404:
        hint = "not found"
    elif status == 400:
        hint = "the store rejected the request"
    else:
        hint = "the store returned an error"
    return f"{hint} (HTTP {status}{': ' + detail[:200] if detail else ''})"
