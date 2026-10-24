"""Store, McpGateway and HostedServer custom resources in the Kubernetes API, with the pod's
ServiceAccount.

The control plane only creates, lists and deletes these objects; the operator
does the provisioning. Its Role allows nothing else.
"""

from __future__ import annotations

import os
import ssl
import time

import httpx2

GROUP = "mcpgateway.example.com"
OWNER_LABEL = f"{GROUP}/owner-id"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# Audience of the token the store operator sends (its chart projects one for it), so a
# token meant for this gateway is useless against the Kubernetes API, and vice versa.
TOKEN_AUDIENCE = "mcp-gateway"


class AlreadyExists(Exception):
    pass


def gateway_resource(slug: str, name: str, public_url: str | None) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "McpGateway",
        "metadata": {"name": slug},
        "spec": {"displayName": name, "publicUrl": public_url or ""},
    }


def store_resource(name: str, template: str, owner_id: int, owner: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "Store",
        "metadata": {"name": name, "labels": {OWNER_LABEL: str(owner_id)}},
        "spec": {"template": template, "owner": owner},
    }


def hosted_resource(name: str, spec: dict, owner_id: int, owner: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "HostedServer",
        "metadata": {"name": name, "labels": {OWNER_LABEL: str(owner_id)}},
        "spec": {**spec, "owner": owner},
    }


class Kube:
    def __init__(self, namespace: str, plural: str = "stores"):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.url = f"https://{host}:{port}/apis/{GROUP}/v1/namespaces/{namespace}/{plural}"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    async def _request(self, method: str, path: str = "", **kw) -> httpx2.Response:
        async with httpx2.AsyncClient(verify=self.tls, timeout=10) as http:
            return await http.request(method, self.url + path, headers={"Authorization": f"Bearer {_sa_token()}"}, **kw)

    async def list(self, owner_id: int | None = None) -> list[dict]:
        params = {"labelSelector": f"{OWNER_LABEL}={owner_id}"} if owner_id is not None else None
        r = await self._request("GET", params=params)
        r.raise_for_status()
        return r.json()["items"]

    async def get(self, name: str) -> dict | None:
        r = await self._request("GET", f"/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def create_object(self, obj: dict) -> dict:
        r = await self._request("POST", json=obj)
        if r.status_code == 409:
            raise AlreadyExists(obj["metadata"]["name"])
        r.raise_for_status()
        return r.json()

    async def create(self, name: str, template: str, owner_id: int, owner: str) -> dict:
        return await self.create_object(store_resource(name, template, owner_id, owner))

    async def delete(self, name: str) -> None:
        r = await self._request("DELETE", f"/{name}")
        if r.status_code != 404:
            r.raise_for_status()


def _sa_token() -> str:
    # Read on every call: the kubelet rotates the token file.
    with open(f"{SA_DIR}/token") as f:
        return f.read().strip()


class TokenReview:
    """Who a ServiceAccount token belongs to, asked of the API server (TokenReview).

    None unless the token is valid, unexpired, its pod still exists, and it was issued
    for TOKEN_AUDIENCE. Answers are cached for a minute, so one token costs one call.
    """

    TTL_S = 60

    def __init__(self):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.url = f"https://{host}:{port}/apis/authentication.k8s.io/v1/tokenreviews"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
        self.cache: dict[str, tuple[str | None, float]] = {}

    def __call__(self, token: str) -> str | None:
        now = time.monotonic()
        if (hit := self.cache.get(token)) and hit[1] > now:
            return hit[0]
        # shortcut: a blocking call on the event loop, at most once a minute per token and only
        # for /api/servers; make it async if anything but the operator ever uses it.
        r = httpx2.post(
            self.url,
            json={"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                  "spec": {"token": token, "audiences": [TOKEN_AUDIENCE]}},
            headers={"Authorization": f"Bearer {_sa_token()}"},
            verify=self.tls,
            timeout=5,
        )
        r.raise_for_status()
        status = r.json().get("status", {})
        ok = status.get("authenticated") and TOKEN_AUDIENCE in status.get("audiences", [])
        user = status["user"]["username"] if ok else None
        self.cache = {t: v for t, v in self.cache.items() if v[1] > now}
        self.cache[token] = (user, now + self.TTL_S)
        return user
