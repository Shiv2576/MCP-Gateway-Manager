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
    assert a["metadata"]["name"] == "woo-mcp-credentials"
    assert data != b["stringData"]  # fresh values: the caller must only create, never replace


def test_network_policies_isolate_the_store():
    pols = {p["metadata"]["name"]: p for p in m.network_policies()}
    assert set(pols) == {"default-deny", "allow-dns", "wordpress-ingress", "wordpress-egress", "setup-egress", "mariadb-ingress"}
    assert pols["default-deny"]["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
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


def test_over_cap_orders_by_creation_time():
    stores = [cr("d", "2026-10-10T10:00:03Z"), cr("a", "2026-10-10T10:00:00Z"),
              cr("c", "2026-10-10T10:00:02Z"), cr("b", "2026-10-10T10:00:01Z")]
    assert [m.over_cap(n, stores) for n in "abcd"] == [False, False, False, True]


def test_progress():
    assert m.progress(False, "missing", False) == ("waiting", "waiting for WordPress")
    assert m.progress(True, "running", False) == ("waiting", "seeding products (setup job)")
    assert m.progress(True, "succeeded", False) == ("waiting", "waiting for woo-mcp")
    assert m.progress(True, "succeeded", True) == ("ready", "")
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
        "mode": "edge", "slug": "prod", "replicas": 2, "image": {"tag": "0.2.0"},
        "ingress": {"host": "prod.gw.localhost"}, "stores": {"enabled": False},
    }
    v = m.edge_values("ext", {"displayName": "External", "publicUrl": "https://gw.example.com"}, "0.2.0")
    assert v["publicUrl"] == "https://gw.example.com"
    # The public host gets its own Ingress rule; the local one stays.
    assert v["ingress"] == {"host": "ext.gw.localhost", "extraHosts": ["gw.example.com"]}


def test_edge_timeout_is_shorter():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    six_min_ago = (now - timedelta(minutes=6)).isoformat()
    assert m.timed_out(six_min_ago, now, m.EDGE_TIMEOUT_S)
    assert not m.timed_out(six_min_ago, now)  # stores get 15 minutes
