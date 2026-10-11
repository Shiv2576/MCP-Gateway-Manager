"""Everything the operator creates for a store, as plain data, plus the small
decisions it makes. No Kubernetes calls here, so it is all unit tested."""

from __future__ import annotations

import secrets
from datetime import datetime
from urllib.parse import urlsplit

GROUP = "mcpgateway.example.com"
VERSION = "v1"
PLURAL = "stores"
GATEWAY_PLURAL = "mcpgateways"
EDGE_TIMEOUT_S = 5 * 60
OWNER_LABEL = f"{GROUP}/owner-id"
STORE_LABEL = f"{GROUP}/store"
SETUP_LABEL = {f"{GROUP}/role": "store-setup"}
MAX_STORES = 3
# PriorityClass from the store-operator chart: below everything else, so on a
# full node the operator and gateway can always evict a store pod to start.
STORE_PRIORITY = "store-workload"
TIMEOUT_S = 15 * 60
# k3d/k3s defaults. Store pods may reach the internet (plugin downloads), never these.
# 169.254.0.0/16 is cloud instance metadata (node credentials).
CLUSTER_CIDRS = ["10.42.0.0/16", "10.43.0.0/16", "169.254.0.0/16"]

WORDPRESS = {"app.kubernetes.io/name": "wordpress"}
MARIADB = {"app.kubernetes.io/name": "mariadb"}
WOO_MCP = {"app.kubernetes.io/name": "woo-mcp"}


def namespace_name(store: str) -> str:
    return f"store-{store}"


def store_url(store: str) -> str:
    return f"http://{store}.localhost"


def mcp_url(store: str) -> str:
    return f"http://woo-mcp-woo-mcp.{namespace_name(store)}.svc.cluster.local:8000/mcp"


def namespace(store: str, owner_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace_name(store), "labels": {OWNER_LABEL: owner_id, STORE_LABEL: store}},
    }


def owns_namespace(ns: dict, store: str) -> bool:
    """Only a namespace this operator made for this store may be used or deleted."""
    return (ns.get("metadata", {}).get("labels") or {}).get(STORE_LABEL) == store


def resource_quota() -> dict:
    # WordPress 250m/512Mi, MariaDB 100m/256Mi, setup Job 100m/256Mi, woo-mcp 50m/96Mi, plus rollout slack.
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "store-quota"},
        "spec": {"hard": {
            "requests.cpu": "1", "requests.memory": "2Gi", "limits.memory": "3Gi",
            "requests.storage": "3Gi", "persistentvolumeclaims": "2", "pods": "10",
        }},
    }


def limit_range() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "store-limits"},
        "spec": {"limits": [{
            "type": "Container",
            "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
            "default": {"memory": "512Mi"},
            "max": {"memory": "1Gi"},
        }]},
    }


def _policy(name: str, selector: dict, ingress=None, egress=None) -> dict:
    spec: dict = {"podSelector": {"matchLabels": selector} if selector else {}, "policyTypes": []}
    if ingress is not None:
        spec["policyTypes"].append("Ingress")
        spec["ingress"] = ingress
    if egress is not None:
        spec["policyTypes"].append("Egress")
        spec["egress"] = egress
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name}, "spec": spec}


def _tcp(port: int) -> list[dict]:
    return [{"protocol": "TCP", "port": port}]


def network_policies() -> list[dict]:
    """Deny everything, then allow only the paths a store needs. woo-mcp's own
    ingress (gateway only) and egress (WordPress only) come with its chart."""
    kube_system = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
    internet = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": CLUSTER_CIDRS}}],
                "ports": _tcp(80) + _tcp(443)}
    to_db = {"to": [{"podSelector": {"matchLabels": MARIADB}}], "ports": _tcp(3306)}
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny"},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        _policy("allow-dns", {}, egress=[{"to": [kube_system], "ports": [{"protocol": "UDP", "port": 53}, *_tcp(53)]}]),
        _policy("wordpress-ingress", WORDPRESS, ingress=[{
            "from": [kube_system,  # Traefik
                     {"podSelector": {"matchLabels": WOO_MCP}},
                     {"podSelector": {"matchLabels": SETUP_LABEL}}],
            "ports": _tcp(8080),
        }]),
        _policy("wordpress-egress", WORDPRESS, egress=[to_db, internet]),
        _policy("setup-egress", SETUP_LABEL, egress=[
            to_db, {"to": [{"podSelector": {"matchLabels": WORDPRESS}}], "ports": _tcp(8080)}, internet,
        ]),
        _policy("mariadb-ingress", MARIADB, ingress=[{
            "from": [{"podSelector": {"matchLabels": WORDPRESS}}, {"podSelector": {"matchLabels": SETUP_LABEL}}],
            "ports": _tcp(3306),
        }]),
    ]


def credentials_secret() -> dict:
    """New random values each call. Create it only when absent: replacing it
    would leave WordPress and woo-mcp with different keys."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "woo-mcp-credentials"},
        "stringData": {
            "WOO_CONSUMER_KEY": "ck_" + secrets.token_hex(20),
            "WOO_CONSUMER_SECRET": "cs_" + secrets.token_hex(20),
            "MCP_AUTH_TOKEN": secrets.token_urlsafe(32),
        },
    }


def store_values(store: str, template: str) -> dict:
    return {
        "setup": {"template": template, "hook": False},
        "wordpress": {
            "wordpressBlogName": store,
            "priorityClassName": STORE_PRIORITY,
            "ingress": {"hostname": f"{store}.localhost"},
            # Bitnami's policies allow all ingress; allows add up, so they would undo ours.
            "networkPolicy": {"enabled": False},
            "persistence": {"size": "1Gi"},
            "mariadb": {
                "networkPolicy": {"enabled": False},
                "primary": {
                    "priorityClassName": STORE_PRIORITY,
                    "persistence": {"size": "1Gi"},
                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
                },
            },
        },
    }


def woo_mcp_values(store: str, gateway_namespace: str) -> dict:
    return {
        "store": {
            "internalUrl": f"http://store-wordpress.{namespace_name(store)}.svc.cluster.local",
            "publicUrl": store_url(store),
        },
        "existingSecret": "woo-mcp-credentials",
        "priorityClassName": STORE_PRIORITY,
        "networkPolicy": {"enabled": True, "allowFromNamespace": gateway_namespace},
    }


def over_cap(store: str, owner_stores: list[dict]) -> bool:
    """True if this store is the owner's 4th or later, oldest first. Catches two
    creates that both passed the gateway's check at the same moment."""
    order = sorted(owner_stores, key=lambda s: (s["metadata"]["creationTimestamp"], s["metadata"]["name"]))
    names = [s["metadata"]["name"] for s in order]
    return store in names and names.index(store) >= MAX_STORES


def progress(wordpress_ready: bool, job: str, woo_ready: bool) -> tuple[str, str]:
    if job == "failed":
        # The handler fills in <namespace>.
        return "failed", "setup job failed: kubectl logs job/store-store-setup -n <namespace>"
    if not wordpress_ready:
        return "waiting", "waiting for WordPress"
    if job != "succeeded":
        return "waiting", "seeding products (setup job)"
    if not woo_ready:
        return "waiting", "waiting for woo-mcp"
    return "ready", ""


def timed_out(created_iso: str, now: datetime, limit_s: int = TIMEOUT_S) -> bool:
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (now - created).total_seconds() > limit_s


def edge_release(slug: str) -> str:
    return f"gw-{slug}"


def edge_url(slug: str) -> str:
    return f"http://{slug}.gw.localhost"


def edge_values(slug: str, spec: dict, image_tag: str) -> dict:
    """charts/gateway in edge mode: /mcp only, no Kubernetes rights, two replicas."""
    values = {
        "mode": "edge",
        "slug": slug,
        "replicas": 2,
        "image": {"tag": image_tag},
        "ingress": {"host": f"{slug}.gw.localhost"},
        "stores": {"enabled": False},
    }
    if spec.get("publicUrl"):
        values["publicUrl"] = spec["publicUrl"]
        # A tunnel or proxy in front sends the public Host header; route it too.
        values["ingress"]["extraHosts"] = [urlsplit(spec["publicUrl"]).hostname]
    return values
