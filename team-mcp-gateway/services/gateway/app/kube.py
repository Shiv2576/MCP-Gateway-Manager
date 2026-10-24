"""Store, McpGateway and HostedServer custom resources in the Kubernetes API, with the pod's
ServiceAccount.

The control plane only creates, lists and deletes these objects; the operator
does the provisioning. Its Role allows nothing else.
"""

from __future__ import annotations

import asyncio
import os
import re
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


_CPU = {"n": 1e-6, "u": 1e-3, "m": 1, "": 1000}  # to millicores
_MEM = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "": 1 / 2**20}  # to MiB


def quantity(value: str, units: dict) -> float:
    """A Kubernetes quantity ("12345678n", "250m", "204800Ki") in the unit `units` maps to 1."""
    number, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", value).groups()
    return float(number) * units[unit]


def apache_workers(text: str) -> tuple[int, int] | None:
    """(busy, total slots) from apache-exporter's Prometheus text, or None if Apache is down."""
    found = dict(re.findall(r'^apache_workers\{state="(busy|idle)"\} ([0-9.e+]+)$', text, re.M))
    slots = re.findall(r"^apache_scoreboard\{[^}]*\} ([0-9.e+]+)$", text, re.M)
    if "busy" not in found or not slots:
        return None
    return int(float(found["busy"])), int(sum(float(n) for n in slots))


class StoreMetrics:
    """Live numbers for one store's WordPress pod: CPU and memory from metrics-server
    (what `kubectl top` shows), Apache's busy workers from its exporter sidecar. Nothing is kept."""

    def __init__(self):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.api = f"https://{host}:{port}"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    async def _usage(self, ns: str) -> tuple[float, float] | None:
        async with httpx2.AsyncClient(verify=self.tls, timeout=5) as http:
            r = await http.get(f"{self.api}/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods",
                               params={"labelSelector": "app.kubernetes.io/name=wordpress"},
                               headers={"Authorization": f"Bearer {_sa_token()}"})
        r.raise_for_status()
        containers = [c for pod in r.json()["items"] for c in pod["containers"] if c["name"] == "wordpress"]
        if not containers:
            return None
        return (sum(quantity(c["usage"]["cpu"], _CPU) for c in containers),
                sum(quantity(c["usage"]["memory"], _MEM) for c in containers))

    async def _workers(self, ns: str) -> tuple[int, int] | None:
        async with httpx2.AsyncClient(timeout=5) as http:
            r = await http.get(f"http://store-wordpress-metrics.{ns}.svc.cluster.local:9150/metrics")
        r.raise_for_status()
        return apache_workers(r.text)

    async def get(self, store: str) -> dict:
        ns = f"store-{store}"  # the operator's namespace for a store
        usage, workers = await asyncio.gather(self._usage(ns), self._workers(ns), return_exceptions=True)
        usage = None if isinstance(usage, Exception) else usage
        workers = None if isinstance(workers, Exception) else workers
        return {
            "cpu_millicores": round(usage[0], 1) if usage else None,
            "memory_mib": round(usage[1], 1) if usage else None,
            "busy_workers": workers[0] if workers else None,
            "max_workers": workers[1] if workers else None,
        }
