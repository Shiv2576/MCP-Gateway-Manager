"""Everything the operator creates for a store, as plain data, plus the small
decisions it makes. No Kubernetes calls here, so it is all unit tested."""

from __future__ import annotations

import os
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
MAX_STORES = int(os.environ.get("MAX_STORES", "3"))  # per admin; same value as the gateway's
# PriorityClass from the store-operator chart: below everything else, so on a
# full node the operator and gateway can always evict a store pod to start.
STORE_PRIORITY = "store-workload"
TIMEOUT_S = 15 * 60
# k3d/k3s defaults. Store pods may reach the internet (plugin downloads), never these.
# 169.254.0.0/16 is cloud instance metadata (node credentials).
CLUSTER_CIDRS = ["10.42.0.0/16", "10.43.0.0/16", "169.254.0.0/16"]

# Where stores (<name>.BASE) and edges (<slug>.gw.BASE) are served. Local k3d:
# localhost over http. VPS: e.g. 203-0-113-7.sslip.io over https, each Ingress
# getting its own certificate from this cert-manager ClusterIssuer.
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "localhost")
SCHEME = os.environ.get("URL_SCHEME", "http")
CLUSTER_ISSUER = os.environ.get("CLUSTER_ISSUER", "")
# Set (e.g. tail1234.ts.net): stores are https://<name>-shop.<tailnet> through Tailscale
# Funnel instead of Traefik, so they answer over IPv4 and need no inbound IPv6.
TAILNET_DOMAIN = os.environ.get("TAILNET_DOMAIN", "").strip().strip(".")
TAILSCALE_NAMESPACE = "tailscale"

WORDPRESS = {"app.kubernetes.io/name": "wordpress"}
MARIADB = {"app.kubernetes.io/name": "mariadb"}
WOO_MCP = {"app.kubernetes.io/name": "woo-mcp"}


def namespace_name(store: str) -> str:
    return f"store-{store}"


def store_url(store: str) -> str:
    if TAILNET_DOMAIN:
        return f"https://{store}-shop.{TAILNET_DOMAIN}"
    return f"{SCHEME}://{store}.{BASE_DOMAIN}"


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


def workloads_binding(operator_namespace: str, role: str) -> dict:
    """Gives the operator its Helm/workload rights in one namespace it created, and nowhere else."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "store-operator"},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"kind": "ServiceAccount", "name": "store-operator", "namespace": operator_namespace}],
    }


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
    # The store's front door: Traefik, or the Tailscale Funnel proxy.
    front = ({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILSCALE_NAMESPACE}}}
             if TAILNET_DOMAIN else kube_system)
    internet = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": CLUSTER_CIDRS}}],
                "ports": _tcp(80) + _tcp(443)}
    to_db = {"to": [{"podSelector": {"matchLabels": MARIADB}}], "ports": _tcp(3306)}
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny"},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        _policy("allow-dns", {}, egress=[{"to": [kube_system], "ports": [{"protocol": "UDP", "port": 53}, *_tcp(53)]}]),
        _policy("wordpress-ingress", WORDPRESS, ingress=[{
            "from": [front,
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
        # cert-manager's HTTP-01 solver pod (clusterIssuer set): Traefik must reach it or the store's certificate never issues.
        _policy("acme-solver-ingress", {"acme.cert-manager.io/http01-solver": "true"},
                ingress=[{"from": [kube_system], "ports": _tcp(8089)}]),
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
            # Login for catalog-b's synthetic shoppers (seed.php), to see their carts.
            # shortcut: one known demo password, replace with a random one before a public demo.
            "SHOPPER_PASSWORD": "test123",
            # WordPress admin login; the Bitnami chart reads it via existingSecret.
            "wordpress-password": secrets.token_urlsafe(12),
        },
    }


def store_values(store: str, template: str) -> dict:
    values = {
        "setup": {"template": template, "hook": False},
        "wordpress": {
            "wordpressBlogName": store,
            "existingSecret": "woo-mcp-credentials",
            "priorityClassName": STORE_PRIORITY,
            "ingress": {"hostname": f"{store}.{BASE_DOMAIN}"},
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
    wordpress = values["wordpress"]
    if TAILNET_DOMAIN:
        # charts/store renders the Tailscale Ingress; WordPress still needs its public host.
        wordpress.update(wordpressScheme="https", wordpressHost=f"{store}-shop.{TAILNET_DOMAIN}",
                         ingress={"enabled": False})
        values["tailscale"] = {"hostname": f"{store}-shop"}
        return values
    if SCHEME == "https":
        # Bitnami writes WP_HOME = <scheme>://<hostname> on first install; it must equal store_url.
        wordpress["wordpressScheme"] = "https"
    if CLUSTER_ISSUER:
        # Bitnami adds tls: with secret <hostname>-tls; cert-manager fills it.
        wordpress["ingress"].update(tls=True, annotations={"cert-manager.io/cluster-issuer": CLUSTER_ISSUER})
    return values


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


PUBLIC_WAIT = "waiting for the public URL (Tailscale DNS and relays)"


def progress(wordpress_ready: bool, job: str, woo_ready: bool, public_ready: bool = True) -> tuple[str, str]:
    if job == "failed":
        # The handler fills in <namespace>.
        return "failed", "setup job failed: kubectl logs job/store-store-setup -n <namespace>"
    if not wordpress_ready:
        return "waiting", "waiting for WordPress"
    if job != "succeeded":
        return "waiting", "seeding products (setup job)"
    if not woo_ready:
        return "waiting", "waiting for woo-mcp"
    if not public_ready:
        # A Tailscale name resolves and reaches every Funnel relay minutes after the pods are up.
        # Ready (and the link) waits for it, so nobody's browser caches "name not found".
        return "waiting", PUBLIC_WAIT
    return "ready", ""


def timed_out(created_iso: str, now: datetime, limit_s: int = TIMEOUT_S) -> bool:
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    return (now - created).total_seconds() > limit_s


def edge_release(slug: str) -> str:
    return f"gw-{slug}"


def edge_url(slug: str) -> str:
    return f"{SCHEME}://{slug}.gw.{BASE_DOMAIN}"


def edge_values(slug: str, spec: dict, image_tag: str) -> dict:
    """charts/gateway in edge mode: /mcp only, no Kubernetes rights, two replicas."""
    values = {
        "mode": "edge",
        "slug": slug,
        "replicas": 2,
        "image": {"tag": image_tag},
        "ingress": {"host": f"{slug}.gw.{BASE_DOMAIN}"},
        "stores": {"enabled": False},
    }
    if (BASE_DOMAIN, SCHEME) != ("localhost", "http"):
        values.update(baseDomain=BASE_DOMAIN, urlScheme=SCHEME)
    if CLUSTER_ISSUER:
        values["ingress"]["clusterIssuer"] = CLUSTER_ISSUER
    if spec.get("publicUrl"):
        values["publicUrl"] = spec["publicUrl"]
        # A tunnel or proxy in front sends the public Host header; route it too.
        public_host = urlsplit(spec["publicUrl"]).hostname
        if public_host.startswith(f"{slug}-gw.") and public_host.endswith(".ts.net"):
            # The gateway's automatic Tailscale name: the Tailscale operator serves it, not Traefik.
            values["tailscale"] = {"hostname": f"{slug}-gw"}
        elif public_host != values["ingress"]["host"]:
            values["ingress"]["extraHosts"] = [public_host]
    return values


# ------------------------------------------------------------ hosted servers
# A HostedServer is any MCP image speaking streamable HTTP, run in its own
# namespace mcp-<name>: quota, deny-all NetworkPolicies, a Deployment and a
# Service "mcp". Only gateway pods may connect; it may reach DNS and the internet.

HOSTED_PLURAL = "hostedservers"
HOSTED_LABEL = f"{GROUP}/hosted"
HOSTED_APP = {"app.kubernetes.io/name": "hosted-mcp"}
HOSTED_SECRET = "hosted-mcp-token"
HOSTED_TIMEOUT_S = 5 * 60
HOSTED_RESOURCES = {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}}


def hosted_namespace_name(name: str) -> str:
    return f"mcp-{name}"


def hosted_url(name: str, spec: dict) -> str:
    return f"http://mcp.{hosted_namespace_name(name)}.svc.cluster.local:{spec['port']}{spec['path']}"


def hosted_namespace(name: str, owner_id: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": hosted_namespace_name(name), "labels": {OWNER_LABEL: owner_id, HOSTED_LABEL: name}},
    }


def owns_hosted_namespace(ns: dict, name: str) -> bool:
    return (ns.get("metadata", {}).get("labels") or {}).get(HOSTED_LABEL) == name


def hosted_quota() -> dict:
    # One pod 50m/128Mi, room for a rolling restart.
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "hosted-quota"},
        "spec": {"hard": {
            "requests.cpu": "500m", "requests.memory": "1Gi", "limits.memory": "2Gi",
            "persistentvolumeclaims": "0", "pods": "4", "services.loadbalancers": "0", "services.nodeports": "0",
        }},
    }


def hosted_network_policies(spec: dict, gateway_namespace: str) -> list[dict]:
    kube_system = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
    from_gateway = {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "gateway"}},  # control plane and edges
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": gateway_namespace}},
    }
    internet = {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": CLUSTER_CIDRS}}], "ports": _tcp(80) + _tcp(443)}
    return [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny"},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        _policy("allow-dns", {}, egress=[{"to": [kube_system], "ports": [{"protocol": "UDP", "port": 53}, *_tcp(53)]}]),
        _policy("gateway-ingress", HOSTED_APP, ingress=[{"from": [from_gateway], "ports": _tcp(spec["port"])}]),
        # Vendor servers call their own APIs (api.github.com, ...); never the cluster.
        _policy("internet-egress", HOSTED_APP, egress=[internet]),
    ]


def hosted_limits() -> dict:
    """What the namespace and container are held to, for the dashboard."""
    return {"quota": hosted_quota()["spec"]["hard"], "container": HOSTED_RESOURCES}


def hosted_secret(spec: dict) -> dict | None:
    """The token the gateway sends and the container checks; None when the server takes none."""
    if not spec.get("tokenEnv"):
        return None
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": HOSTED_SECRET},
            "stringData": {"token": secrets.token_urlsafe(32)}}


def hosted_deployment(spec: dict) -> dict:
    env = [{"name": k, "value": v} for k, v in sorted((spec.get("env") or {}).items())]
    if spec.get("tokenEnv"):
        env.append({"name": spec["tokenEnv"],
                    "valueFrom": {"secretKeyRef": {"name": HOSTED_SECRET, "key": "token"}}})
    container = {
        "name": "mcp",
        "image": spec["image"],
        "args": spec.get("args") or [],
        "env": env,
        "ports": [{"name": "mcp", "containerPort": spec["port"]}],
        # TCP, not HTTP: we know the port, not what each image answers on GET.
        "readinessProbe": {"tcpSocket": {"port": spec["port"]}, "periodSeconds": 5},
        "livenessProbe": {"tcpSocket": {"port": spec["port"]}, "initialDelaySeconds": 30, "periodSeconds": 20},
        "resources": HOSTED_RESOURCES,
        # Not runAsNonRoot: many published images run as root and would never start.
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    if not container["args"]:
        del container["args"]  # keep the image's own CMD
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "mcp", "labels": HOSTED_APP},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": HOSTED_APP},
            "template": {
                "metadata": {"labels": HOSTED_APP},
                "spec": {
                    "automountServiceAccountToken": False,  # third-party code gets no cluster API access
                    "enableServiceLinks": False,
                    "priorityClassName": STORE_PRIORITY,
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [container],
                },
            },
        },
    }


def hosted_service(spec: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "mcp", "labels": HOSTED_APP},
        "spec": {"selector": HOSTED_APP, "ports": [{"name": "mcp", "port": spec["port"], "targetPort": spec["port"]}]},
    }


# Waiting reasons that will not fix themselves: report them instead of a bare timeout.
STUCK = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CrashLoopBackOff", "CreateContainerConfigError"}


def hosted_progress(ready: bool, waiting: list[tuple[str, str]]) -> tuple[str, str]:
    """waiting: (reason, message) of each waiting container."""
    if ready:
        return "ready", ""
    for reason, message in waiting:
        if reason in STUCK:
            return "waiting", f"{reason}: {message}"[:300]
    return "waiting", "waiting for the pod"
