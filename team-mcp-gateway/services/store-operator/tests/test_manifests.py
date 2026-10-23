from datetime import datetime, timedelta, timezone

from store_operator import manifests as m


def cr(name, created, owner="1"):
    return {"metadata": {"name": name, "creationTimestamp": created, "labels": {m.OWNER_LABEL: owner}}}


def test_names_and_urls():
    assert m.namespace_name("shop1") == "store-shop1"
    assert m.store_url("shop1") == "http://shop1.localhost"
    assert m.mcp_url("shop1") == "http://woo-mcp-woo-mcp.store-shop1.svc.cluster.local:8000/mcp"
    ns = m.namespace("shop1", "7")
    assert ns["metadata"]["labels"][m.OWNER_LABEL] == "7"


def test_credentials_secret_is_only_built_once_per_call():
    a, b = m.credentials_secret(), m.credentials_secret()
    data = a["stringData"]
    assert data["WOO_CONSUMER_KEY"].startswith("ck_") and len(data["WOO_CONSUMER_KEY"]) == 43
    assert data["WOO_CONSUMER_SECRET"].startswith("cs_") and len(data["WOO_CONSUMER_SECRET"]) == 43
    assert len(data["MCP_AUTH_TOKEN"]) >= 32
    assert data["SHOPPER_PASSWORD"] == "test123"
    assert len(data["wordpress-password"]) >= 16
    assert a["metadata"]["name"] == "woo-mcp-credentials"
    assert data != b["stringData"]  # fresh values: the caller must only create, never replace


def test_network_policies_isolate_the_store():
    pols = {p["metadata"]["name"]: p for p in m.network_policies()}
    assert set(pols) == {"default-deny", "allow-dns", "wordpress-ingress", "wordpress-egress", "setup-egress", "mariadb-ingress",
                         "acme-solver-ingress"}
    assert pols["default-deny"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    acme = pols["acme-solver-ingress"]["spec"]
    assert acme["podSelector"] == {"matchLabels": {"acme.cert-manager.io/http01-solver": "true"}}
    assert acme["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8089}]
    db_from = pols["mariadb-ingress"]["spec"]["ingress"][0]["from"]
    # Only pods in this namespace (no namespaceSelector) may reach MariaDB.
    assert all("namespaceSelector" not in f for f in db_from)


def test_helm_values():
    v = m.store_values("shop1", "catalog-a")
    assert v["setup"] == {"template": "catalog-a", "hook": False}
    assert v["wordpress"]["ingress"]["hostname"] == "shop1.localhost"
    assert v["wordpress"]["networkPolicy"]["enabled"] is False
    assert v["wordpress"]["mariadb"]["networkPolicy"]["enabled"] is False
    w = m.woo_mcp_values("shop1", "mercora")
    assert w["store"]["internalUrl"] == "http://store-wordpress.store-shop1.svc.cluster.local"
    assert w["store"]["publicUrl"] == "http://shop1.localhost"
    assert w["networkPolicy"]["allowFromNamespace"] == "mercora"
    assert m.image_ref(w["image"]) == "woo-mcp:0.5.0"


def test_over_cap_orders_by_creation_time():
    stores = [cr("d", "2026-10-10T10:00:03Z"), cr("a", "2026-10-10T10:00:00Z"),
              cr("c", "2026-10-10T10:00:02Z"), cr("b", "2026-10-10T10:00:01Z")]
    assert [m.over_cap(n, stores) for n in "abcd"] == [False, False, False, True]


def test_progress():
    assert m.progress(False, "missing", False) == ("waiting", "waiting for WordPress")
    assert m.progress(True, "running", False) == ("waiting", "seeding products (setup job)")
    assert m.progress(True, "succeeded", False) == ("waiting", "waiting for woo-mcp")
    assert m.progress(True, "succeeded", True) == ("ready", "")
    assert m.progress(True, "succeeded", True, public_ready=False) == ("waiting", m.PUBLIC_WAIT)
    state, msg = m.progress(True, "failed", True)
    assert state == "failed" and "kubectl logs job/store-store-setup" in msg


def test_timeout():
    now = datetime(2026, 10, 10, 10, 15, 1, tzinfo=timezone.utc)
    assert m.timed_out("2026-10-10T10:00:00Z", now)
    assert not m.timed_out("2026-10-10T10:00:00Z", now - timedelta(seconds=2))


def test_store_pods_yield_to_the_platform():
    # On a full node the operator must be able to evict a store pod to start,
    # or deletes stall with no operator running.
    v = m.store_values("shop1", "blank")
    assert v["wordpress"]["priorityClassName"] == m.STORE_PRIORITY
    assert v["wordpress"]["mariadb"]["primary"]["priorityClassName"] == m.STORE_PRIORITY
    assert m.woo_mcp_values("shop1", "mercora")["priorityClassName"] == m.STORE_PRIORITY


def test_only_our_own_namespace_counts_as_ours():
    assert m.owns_namespace(m.namespace("shop1", "7"), "shop1")
    assert not m.owns_namespace({"metadata": {"name": "store-shop1", "labels": {}}}, "shop1")
    assert not m.owns_namespace({"metadata": {"name": "store-shop1"}}, "shop1")
    assert not m.owns_namespace(m.namespace("other", "7"), "shop1")


def test_store_pods_cannot_reach_cloud_metadata():
    for p in m.network_policies():
        for rule in p["spec"].get("egress", []):
            for to in rule["to"]:
                if "ipBlock" in to:
                    assert "169.254.0.0/16" in to["ipBlock"]["except"]


def test_edge_values():
    assert m.edge_release("prod") == "gw-prod"
    assert m.edge_url("prod") == "http://prod.gw.localhost"
    v = m.edge_values("prod", {"displayName": "Production", "publicUrl": ""}, "0.2.0")
    assert v == {
        "mode": "edge", "slug": "prod", "replicas": 2, "image": {"repository": "gateway", "tag": "0.2.0"},
        "ingress": {"host": "prod.gw.localhost"}, "stores": {"enabled": False},
    }
    v = m.edge_values("ts", {"publicUrl": "https://ts-gw.tail1234.ts.net"}, "0.2.0")
    assert v["tailscale"] == {"hostname": "ts-gw"} and "extraHosts" not in v["ingress"]
    # A hand-made Funnel name (another machine) still goes through Traefik.
    v = m.edge_values("prod", {"publicUrl": "https://mcp-gateway.tail1234.ts.net"}, "0.2.0")
    assert "tailscale" not in v and v["ingress"]["extraHosts"] == ["mcp-gateway.tail1234.ts.net"]
    v = m.edge_values("ext", {"displayName": "External", "publicUrl": "https://gw.example.com"}, "0.2.0")
    assert v["publicUrl"] == "https://gw.example.com"
    # The public host gets its own Ingress rule; the local one stays.
    assert v["ingress"] == {"host": "ext.gw.localhost", "extraHosts": ["gw.example.com"]}


def test_vps_base_domain(monkeypatch):
    monkeypatch.setattr(m, "BASE_DOMAIN", "203-0-113-7.sslip.io")
    monkeypatch.setattr(m, "SCHEME", "https")
    monkeypatch.setattr(m, "CLUSTER_ISSUER", "letsencrypt")
    issuer = {"cert-manager.io/cluster-issuer": "letsencrypt"}
    assert m.store_url("shop1") == "https://shop1.203-0-113-7.sslip.io"
    wp = m.store_values("shop1", "blank")["wordpress"]
    assert wp["wordpressScheme"] == "https"
    assert wp["ingress"] == {"hostname": "shop1.203-0-113-7.sslip.io", "tls": True, "annotations": issuer}
    assert m.woo_mcp_values("shop1", "mercora")["store"]["publicUrl"] == "https://shop1.203-0-113-7.sslip.io"
    assert m.edge_url("prod") == "https://prod.gw.203-0-113-7.sslip.io"
    # The public URL is the edge's own host: no duplicate Ingress rule.
    v = m.edge_values("prod", {"publicUrl": "https://prod.gw.203-0-113-7.sslip.io"}, "0.6.1")
    assert v["ingress"] == {"host": "prod.gw.203-0-113-7.sslip.io", "clusterIssuer": "letsencrypt"}
    assert (v["baseDomain"], v["urlScheme"]) == ("203-0-113-7.sslip.io", "https")


def test_tailnet_serves_stores_through_tailscale(monkeypatch):
    monkeypatch.setattr(m, "BASE_DOMAIN", "203-0-113-7.sslip.io")
    monkeypatch.setattr(m, "SCHEME", "https")
    monkeypatch.setattr(m, "CLUSTER_ISSUER", "letsencrypt")
    monkeypatch.setattr(m, "TAILNET_DOMAIN", "tail1234.ts.net")
    assert m.store_url("shop1") == "https://shop1-shop.tail1234.ts.net"
    v = m.store_values("shop1", "blank")
    assert v["tailscale"] == {"hostname": "shop1-shop"}
    wp = v["wordpress"]
    # No Traefik Ingress or certificate; WordPress builds its links from the Tailscale host.
    assert wp["ingress"] == {"enabled": False}
    assert (wp["wordpressHost"], wp["wordpressScheme"]) == ("shop1-shop.tail1234.ts.net", "https")
    assert m.woo_mcp_values("shop1", "mercora")["store"]["publicUrl"] == "https://shop1-shop.tail1234.ts.net"
    wp_in = next(p for p in m.network_policies() if p["metadata"]["name"] == "wordpress-ingress")
    sources = wp_in["spec"]["ingress"][0]["from"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tailscale"}}} in sources
    assert not any("kube-system" in str(s) for s in sources)
    # Edges keep their own rules.
    assert m.edge_url("prod") == "https://prod.gw.203-0-113-7.sslip.io"


def test_local_values_have_no_vps_keys():
    wp = m.store_values("shop1", "blank")["wordpress"]
    assert "wordpressScheme" not in wp and wp["ingress"] == {"hostname": "shop1.localhost"}
    assert "baseDomain" not in m.edge_values("prod", {}, "0.6.1")


def test_edge_timeout_is_shorter():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    six_min_ago = (now - timedelta(minutes=6)).isoformat()
    assert m.timed_out(six_min_ago, now, m.EDGE_TIMEOUT_S)
    assert not m.timed_out(six_min_ago, now)  # stores get 15 minutes


SPEC = {"image": "me/demo-mcp:0.1.0", "args": [], "port": 8000, "path": "/mcp",
        "env": {"DEMO_SERVER": "notes"}, "tokenEnv": "AUTH_TOKEN", "authHeader": "X-API-Key"}


def test_hosted_names_and_url():
    assert m.hosted_namespace_name("notes") == "mcp-notes"
    assert m.hosted_url("notes", SPEC) == "http://mcp.mcp-notes.svc.cluster.local:8000/mcp"
    ns = m.hosted_namespace("notes", "7")
    assert m.owns_hosted_namespace(ns, "notes") and not m.owns_hosted_namespace(ns, "fx")
    assert not m.owns_hosted_namespace(m.namespace("notes", "7"), "notes")  # a store namespace is not ours


def test_hosted_deployment_and_secret():
    secret = m.hosted_secret(SPEC)
    assert len(secret["stringData"]["token"]) >= 32 and m.hosted_secret(SPEC) != secret
    assert m.hosted_secret({**SPEC, "tokenEnv": None}) is None

    pod = m.hosted_deployment(SPEC)["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert c["image"] == "me/demo-mcp:0.1.0" and "args" not in c  # image CMD kept
    assert {"name": "DEMO_SERVER", "value": "notes"} in c["env"]
    assert {"name": "AUTH_TOKEN", "valueFrom": {"secretKeyRef": {"name": m.HOSTED_SECRET, "key": "token"}}} in c["env"]
    assert c["readinessProbe"]["tcpSocket"]["port"] == 8000
    assert c["livenessProbe"]["tcpSocket"]["port"] == 8000
    gh = m.hosted_deployment({"image": "ghcr.io/github/github-mcp-server", "args": ["http"], "port": 8082, "path": "/"})
    gc = gh["spec"]["template"]["spec"]["containers"][0]
    assert gc["args"] == ["http"] and gc["env"] == []
    assert m.hosted_service(SPEC)["spec"]["ports"][0]["port"] == 8000
    lim = m.hosted_limits()
    assert lim["container"] == c["resources"] and lim["quota"]["pods"] == "4"


def test_hosted_network_policies():
    pols = {p["metadata"]["name"]: p["spec"] for p in m.hosted_network_policies(SPEC, "mercora")}
    assert pols["default-deny"]["policyTypes"] == ["Ingress", "Egress"]
    src = pols["gateway-ingress"]["ingress"][0]
    assert src["ports"] == [{"protocol": "TCP", "port": 8000}]
    assert src["from"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "mercora"}
    assert src["from"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "gateway"}
    block = pols["internet-egress"]["egress"][0]["to"][0]["ipBlock"]
    assert "10.43.0.0/16" in block["except"] and "169.254.0.0/16" in block["except"]


def test_hosted_progress():
    assert m.hosted_progress(True, []) == ("ready", "")
    assert m.hosted_progress(False, [("ContainerCreating", "")]) == ("waiting", "waiting for the pod")
    state, msg = m.hosted_progress(False, [("ImagePullBackOff", "Back-off pulling image x")])
    assert state == "waiting" and msg.startswith("ImagePullBackOff")


def test_unschedulable():
    now = datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
    msg = "0/1 nodes are available: 1 Insufficient cpu. preemption: 0/1 nodes are available."
    assert m.unschedulable([], now) == ""
    assert m.unschedulable([("wp-0", msg, now - timedelta(seconds=10))], now) == ""  # inside the grace
    got = m.unschedulable([("wp-0", msg, now - timedelta(minutes=2))], now)
    assert got.startswith("not enough cpu left in the cluster (wp-0: 0/1 nodes")
    other = m.unschedulable([("wp-0", "0/1 nodes are available: 1 node(s) had untolerated taint", now - timedelta(minutes=2))], now)
    assert other.startswith("no node can run it")
