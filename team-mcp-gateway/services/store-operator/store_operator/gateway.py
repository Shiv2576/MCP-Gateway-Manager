"""Registers store and hosted MCP servers in the gateway through its admin API.

Authenticates with this pod's projected ServiceAccount token (audience mcp-gateway); the
gateway checks it with a TokenReview. No API key to create, paste or rotate.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class Gateway:
    def __init__(self, base_url: str, token_file: str):
        self.base_url, self.token_file = base_url.rstrip("/"), token_file

    def _call(self, method: str, path: str, body: dict | None = None):
        # Read on every call: the kubelet replaces the token before it expires (1h).
        with open(self.token_file) as f:
            token = f.read().strip()
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"gateway {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from None

    def _find(self, name: str) -> dict | None:
        return next((s for s in self._call("GET", "/api/servers") if s["name"] == name), None)

    def upsert_server(self, name: str, url: str, token: str | None,
                      header: str = "Authorization", store: str | None = None) -> None:
        if token is None:
            auth = {"type": "none"}
        elif header.lower() == "authorization":
            auth = {"type": "bearer", "token": token}
        else:
            auth = {"type": "header", "name": header, "value": token}
        body = {"url": url, "auth": auth, "enabled": True}
        existing = self._find(name)
        if existing:
            self._call("PATCH", f"/api/servers/{existing['id']}", body)
        else:
            self._call("POST", "/api/servers", {"name": name, "store": store, **body})

    def delete_server(self, name: str) -> None:
        existing = self._find(name)
        if existing:
            self._call("DELETE", f"/api/servers/{existing['id']}")
