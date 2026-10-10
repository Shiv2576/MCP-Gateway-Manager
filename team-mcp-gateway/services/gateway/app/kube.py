"""Store custom resources in the Kubernetes API, with the pod's ServiceAccount.

The gateway only creates, lists and deletes Store objects; the store operator
does the provisioning. Its Role allows nothing else.
"""

from __future__ import annotations

import os
import ssl

import httpx2

GROUP = "mcpgateway.example.com"
OWNER_LABEL = f"{GROUP}/owner-id"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class StoreExists(Exception):
    pass


def store_resource(name: str, template: str, owner_id: int, owner: str) -> dict:
    return {
        "apiVersion": f"{GROUP}/v1",
        "kind": "Store",
        "metadata": {"name": name, "labels": {OWNER_LABEL: str(owner_id)}},
        "spec": {"template": template, "owner": owner},
    }


class Kube:
    def __init__(self, namespace: str):
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
        self.url = f"https://{host}:{port}/apis/{GROUP}/v1/namespaces/{namespace}/stores"
        self.tls = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    async def _request(self, method: str, path: str = "", **kw) -> httpx2.Response:
        # Read on every call: the kubelet rotates the token file.
        with open(f"{SA_DIR}/token") as f:
            token = f.read().strip()
        async with httpx2.AsyncClient(verify=self.tls, timeout=10) as http:
            return await http.request(method, self.url + path, headers={"Authorization": f"Bearer {token}"}, **kw)

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

    async def create(self, name: str, template: str, owner_id: int, owner: str) -> dict:
        r = await self._request("POST", json=store_resource(name, template, owner_id, owner))
        if r.status_code == 409:
            raise StoreExists(name)
        r.raise_for_status()
        return r.json()

    async def delete(self, name: str) -> None:
        r = await self._request("DELETE", f"/{name}")
        if r.status_code != 404:
            r.raise_for_status()
