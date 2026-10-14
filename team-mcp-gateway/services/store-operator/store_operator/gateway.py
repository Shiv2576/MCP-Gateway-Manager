"""Registers store and hosted MCP servers in the gateway through its admin API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class Gateway:
    def __init__(self, base_url: str, api_key: str):
        self.base_url, self.api_key = base_url.rstrip("/"), api_key

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
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
