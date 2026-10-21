"""kopf handlers: Store resource -> namespace + Helm releases + gateway server.

Every step is safe to repeat, so after an operator restart kopf re-runs a
handler from the top and it converges. Run:
  kopf run -m store_operator.handlers --standalone --namespace <gateway namespace>
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

import kopf
import kubernetes
from kubernetes.client import (ApiClient, ApiException, AppsV1Api, BatchV1Api, CoreV1Api, CustomObjectsApi,
                               NetworkingV1Api, RbacAuthorizationV1Api)

from . import manifests as m
from .gateway import Gateway

NAMESPACE = os.environ.get("STORES_NAMESPACE", "mercora")
CHARTS = os.environ.get("CHARTS_DIR", "/charts")
MAX_ATTEMPTS = 3
EDGE_IMAGE_TAG = os.environ.get("EDGE_IMAGE_TAG", "0.6.1")
WORKLOADS_ROLE = os.environ.get("WORKLOADS_ROLE", f"{NAMESPACE}-store-operator-workloads")
gateway = Gateway(os.environ.get("GATEWAY_URL", "http://team-gateway:8000"), os.environ.get("GATEWAY_API_KEY", ""))


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    settings.posting.level = 30  # WARNING: keep Events for problems only


def _create(fn, *args) -> None:
    try:
        fn(*args)
    except ApiException as e:
        if e.status != 409:
            raise


def _bind_workloads(ns: str) -> None:
    """The operator's rights inside ns come only from this binding (charts/store-operator rbac.yaml)."""
    _create(RbacAuthorizationV1Api().create_namespaced_role_binding, ns, m.workloads_binding(NAMESPACE, WORKLOADS_ROLE))


def _owned(core: CoreV1Api, store: str) -> bool:
    ns = ApiClient().sanitize_for_serialization(core.read_namespace(m.namespace_name(store)))
    return m.owns_namespace(ns, store)


def _namespace_objects(store: str, owner_id: str, create_namespace: bool = True) -> None:
    ns = m.namespace_name(store)
    core, net = CoreV1Api(), NetworkingV1Api()
    if create_namespace:
        _create(core.create_namespace, m.namespace(store, owner_id))
    if not _owned(core, store):
        raise RuntimeError(f"namespace {ns} already exists and was not created for this store")
    _bind_workloads(ns)  # also gives stores made before this binding existed their rights back
    _create(core.create_namespaced_resource_quota, ns, m.resource_quota())
    _create(core.create_namespaced_limit_range, ns, m.limit_range())
    for policy in m.network_policies():
        _create(net.create_namespaced_network_policy, ns, policy)


def _helm(release: str, chart: str, ns: str, values: dict) -> None:
    # Values via a file: --set breaks on commas.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(values, f)
    cmd = ["helm", "upgrade", "--install", release, f"{CHARTS}/{chart}", "-n", ns, "-f", f.name, "--timeout", "5m"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode and "in progress" in r.stderr:
            # A helm killed mid-operation (operator restart) leaves the release
            # pending; roll back, or uninstall if it never had a good revision.
            if subprocess.run(["helm", "rollback", release, "-n", ns], capture_output=True).returncode:
                subprocess.run(["helm", "uninstall", release, "-n", ns], capture_output=True)
            r = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if r.returncode:
        raise RuntimeError(f"helm {release}: {r.stderr.strip()[-400:]}")


def _owner_stores(owner_id: str) -> list[dict]:
    return CustomObjectsApi().list_namespaced_custom_object(
        m.GROUP, m.VERSION, NAMESPACE, m.PLURAL, label_selector=f"{m.OWNER_LABEL}={owner_id}"
    )["items"]


@kopf.on.create(m.GROUP, m.VERSION, m.PLURAL)
def on_store_created(name, spec, labels, patch, retry, logger, **_):
    owner_id = labels[m.OWNER_LABEL]
    if m.over_cap(name, _owner_stores(owner_id)):
        patch.status.update(phase="Failed", message=f"store limit ({m.MAX_STORES}) reached")
        return
    ns = m.namespace_name(name)
    patch.status.update(phase="Provisioning", message="installing", url=m.store_url(name))
    try:
        _namespace_objects(name, owner_id)
        _create(CoreV1Api().create_namespaced_secret, ns, m.credentials_secret())
        # Read back, not the generated value: a retry keeps the Secret from the first attempt.
        secret = CoreV1Api().read_namespaced_secret("woo-mcp-credentials", ns)
        patch.status["adminPassword"] = base64.b64decode(secret.data["wordpress-password"]).decode()
        _helm("store", "store", ns, m.store_values(name, spec["template"]))
        _helm("woo-mcp", "woo-mcp", ns, m.woo_mcp_values(name, NAMESPACE))
    except Exception as e:
        if retry + 1 >= MAX_ATTEMPTS:
            patch.status.update(phase="Failed", message=str(e)[:500])
            return
        raise kopf.TemporaryError(str(e)[:500], delay=20)
    patch.status["message"] = "waiting for WordPress"
    logger.info(f"{name}: installed into {ns}")


def _deployment_ready(apps: AppsV1Api, ns: str, name: str) -> bool:
    try:
        return (apps.read_namespaced_deployment(name, ns).status.ready_replicas or 0) > 0
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _job_state(batch: BatchV1Api, ns: str, name: str) -> str:
    try:
        st = batch.read_namespaced_job(name, ns).status
    except ApiException as e:
        if e.status == 404:
            return "missing"
        raise
    if st.succeeded:
        return "succeeded"
    if any(c.type == "Failed" and c.status == "True" for c in st.conditions or []):
        return "failed"
    return "running"


def _public_ready(url: str) -> bool:
    """Every address the public name resolves to (each Tailscale Funnel relay) answers
    HTTPS for it. IPv4 only: the relays serve both families alike, and this box's IPv6 is flaky."""
    host = urlsplit(url).hostname
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
    except socket.gaierror:
        return False  # not in DNS yet
    tls = ssl.create_default_context()
    for addr in addrs:
        try:
            with socket.create_connection((addr, 443), timeout=10) as raw, \
                    tls.wrap_socket(raw, server_hostname=host) as s:
                s.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                status = s.recv(64).split(b" ", 2)[1]
            if int(status) >= 500:
                return False
        except (OSError, ValueError, IndexError):
            return False
    return bool(addrs)


@kopf.timer(m.GROUP, m.VERSION, m.PLURAL, interval=15)
def reconcile_store(name, meta, status, patch, logger, **_):
    if status.get("phase") != "Provisioning":
        return
    ns = m.namespace_name(name)
    apps = AppsV1Api()
    state, message = m.progress(
        _deployment_ready(apps, ns, "store-wordpress"),
        _job_state(BatchV1Api(), ns, "store-store-setup"),
        _deployment_ready(apps, ns, "woo-mcp-woo-mcp"),
    )
    if state == "ready" and m.TAILNET_DOMAIN:
        state, message = m.progress(True, "succeeded", True, _public_ready(m.store_url(name)))
    message = message.replace("<namespace>", ns)
    now = datetime.now(timezone.utc)
    if state == "waiting" and message == m.PUBLIC_WAIT and m.timed_out(meta["creationTimestamp"], now):
        # The store works; only its name is slow. Don't fail it for Tailscale.
        state, message = "ready", "public URL may need a few more minutes"
    if state == "failed":
        patch.status.update(phase="Failed", message=message)
    elif state == "ready":
        secret = CoreV1Api().read_namespaced_secret("woo-mcp-credentials", ns)
        token = base64.b64decode(secret.data["MCP_AUTH_TOKEN"]).decode()
        gateway.upsert_server(name, m.mcp_url(name), token, store=name)
        patch.status.update(phase="Ready", message=message, readyAt=now.isoformat())
        logger.info(f"{name}: ready and registered in the gateway")
    elif m.timed_out(meta["creationTimestamp"], now):
        patch.status.update(phase="Failed", message=f"timed out after {m.TIMEOUT_S // 60} min: {message}")
    else:
        patch.status["message"] = message


@kopf.timer(m.GROUP, m.VERSION, m.PLURAL, interval=60)
def repair_guardrails(name, meta, labels, status, **_):
    """Put back a quota, LimitRange or NetworkPolicy someone deleted by hand.
    Never recreates the namespace itself, and stays out of the way of a delete."""
    if status.get("phase") == "Ready" and not meta.get("deletionTimestamp"):
        _namespace_objects(name, labels[m.OWNER_LABEL], create_namespace=False)


@kopf.on.delete(m.GROUP, m.VERSION, m.PLURAL)
def on_store_deleted(name, logger, **_):
    gateway.delete_server(name)  # tools disappear first
    ns = m.namespace_name(name)
    core = CoreV1Api()
    try:
        if not _owned(core, name):
            logger.warning(f"{name}: {ns} was not created for this store; leaving it alone")
            return
        if core.read_namespace(ns).status.phase != "Terminating":
            core.delete_namespace(ns)  # takes the releases, PVCs and Secrets with it
    except ApiException as e:
        if e.status == 404:
            logger.info(f"{name}: torn down")
            return
        raise
    # Hold the finalizer until the namespace is gone, so the same name can be reused at once.
    raise kopf.TemporaryError(f"waiting for namespace {ns} to be deleted", delay=10)


# ------------------------------------------------------------------ gateways
# An McpGateway becomes the Helm release gw-<slug> (charts/gateway, edge mode)
# next to the control plane. No namespace or guardrails of its own.


@kopf.on.create(m.GROUP, m.VERSION, m.GATEWAY_PLURAL)
def on_gateway_created(name, spec, patch, retry, logger, **_):
    patch.status.update(phase="Provisioning", message="installing", url=m.edge_url(name))
    try:
        _helm(m.edge_release(name), "gateway", NAMESPACE, m.edge_values(name, spec, EDGE_IMAGE_TAG))
    except Exception as e:
        if retry + 1 >= MAX_ATTEMPTS:
            patch.status.update(phase="Failed", message=str(e)[:500])
            return
        raise kopf.TemporaryError(str(e)[:500], delay=20)
    patch.status["message"] = "waiting for the edge pods"
    logger.info(f"gateway {name}: installed {m.edge_release(name)}")


@kopf.timer(m.GROUP, m.VERSION, m.GATEWAY_PLURAL, interval=15)
def reconcile_gateway(name, meta, status, patch, logger, **_):
    if status.get("phase") != "Provisioning":
        return
    if _deployment_ready(AppsV1Api(), NAMESPACE, f"{m.edge_release(name)}-gateway"):
        patch.status.update(phase="Ready", message="")
        logger.info(f"gateway {name}: ready")
    elif m.timed_out(meta["creationTimestamp"], datetime.now(timezone.utc), m.EDGE_TIMEOUT_S):
        patch.status.update(phase="Failed", message=f"no ready edge pod after {m.EDGE_TIMEOUT_S // 60} min")


def _deployment_exists(apps: AppsV1Api, ns: str, name: str) -> bool:
    try:
        apps.read_namespaced_deployment(name, ns)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


@kopf.timer(m.GROUP, m.VERSION, m.GATEWAY_PLURAL, interval=60)
def repair_gateway(name, spec, meta, status, logger, **_):
    """Reinstall a Ready gateway's edge if its release was removed by hand.
    (Restarts need nothing: kopf runs a create handler that has not finished.)"""
    if status.get("phase") != "Ready" or meta.get("deletionTimestamp"):
        return
    if not _deployment_exists(AppsV1Api(), NAMESPACE, f"{m.edge_release(name)}-gateway"):
        _helm(m.edge_release(name), "gateway", NAMESPACE, m.edge_values(name, spec, EDGE_IMAGE_TAG))
        logger.warning(f"gateway {name}: edge was missing; reinstalled")


@kopf.on.delete(m.GROUP, m.VERSION, m.GATEWAY_PLURAL)
def on_gateway_deleted(name, logger, **_):
    r = subprocess.run(["helm", "uninstall", m.edge_release(name), "-n", NAMESPACE, "--wait"],
                       capture_output=True, text=True)
    if r.returncode and "not found" not in r.stderr:
        raise kopf.TemporaryError(f"helm uninstall: {r.stderr.strip()[-300:]}", delay=10)
    logger.info(f"gateway {name}: removed")


# ------------------------------------------------------------ hosted servers
# A HostedServer becomes namespace mcp-<name> with a Deployment and Service of
# the admin's image; once it is ready it is registered in the gateway.


def _hosted_owned(core: CoreV1Api, name: str) -> bool:
    ns = ApiClient().sanitize_for_serialization(core.read_namespace(m.hosted_namespace_name(name)))
    return m.owns_hosted_namespace(ns, name)


@kopf.on.create(m.GROUP, m.VERSION, m.HOSTED_PLURAL)
def on_hosted_created(name, spec, labels, patch, retry, logger, **_):
    ns = m.hosted_namespace_name(name)
    core, net, apps = CoreV1Api(), NetworkingV1Api(), AppsV1Api()
    patch.status.update(phase="Provisioning", message="deploying", url=m.hosted_url(name, spec),
                        limits=m.hosted_limits())
    try:
        _create(core.create_namespace, m.hosted_namespace(name, labels.get(m.OWNER_LABEL, "")))
        if not _hosted_owned(core, name):
            raise RuntimeError(f"namespace {ns} already exists and was not created for this server")
        _bind_workloads(ns)
        _create(core.create_namespaced_resource_quota, ns, m.hosted_quota())
        _create(core.create_namespaced_limit_range, ns, m.limit_range())
        for policy in m.hosted_network_policies(spec, NAMESPACE):
            _create(net.create_namespaced_network_policy, ns, policy)
        secret = m.hosted_secret(spec)
        if secret:
            _create(core.create_namespaced_secret, ns, secret)  # never replaced: the gateway holds the same value
        _create(apps.create_namespaced_deployment, ns, m.hosted_deployment(spec))
        _create(core.create_namespaced_service, ns, m.hosted_service(spec))
    except Exception as e:
        if retry + 1 >= MAX_ATTEMPTS:
            patch.status.update(phase="Failed", message=str(e)[:500])
            return
        raise kopf.TemporaryError(str(e)[:500], delay=10)
    patch.status["message"] = "waiting for the pod"
    logger.info(f"hosted {name}: deployed into {ns}")


def _waiting_reasons(core: CoreV1Api, ns: str) -> list[tuple[str, str]]:
    pods = core.list_namespaced_pod(ns, label_selector="app.kubernetes.io/name=hosted-mcp").items
    return [(cs.state.waiting.reason or "", cs.state.waiting.message or "")
            for p in pods for cs in (p.status.container_statuses or []) if cs.state and cs.state.waiting]


@kopf.timer(m.GROUP, m.VERSION, m.HOSTED_PLURAL, interval=5)
def reconcile_hosted(name, spec, meta, status, patch, logger, **_):
    if status.get("phase") != "Provisioning" or meta.get("deletionTimestamp"):
        return
    ns = m.hosted_namespace_name(name)
    core = CoreV1Api()
    state, message = m.hosted_progress(_deployment_ready(AppsV1Api(), ns, "mcp"), _waiting_reasons(core, ns))
    if state == "ready":
        token = None
        if spec.get("tokenEnv"):
            token = base64.b64decode(core.read_namespaced_secret(m.HOSTED_SECRET, ns).data["token"]).decode()
        gateway.upsert_server(name, m.hosted_url(name, spec), token, spec.get("authHeader") or "Authorization")
        patch.status.update(phase="Ready", message="")
        logger.info(f"hosted {name}: ready and registered in the gateway")
    elif m.timed_out(meta["creationTimestamp"], datetime.now(timezone.utc), m.HOSTED_TIMEOUT_S):
        patch.status.update(phase="Failed", message=f"not ready after {m.HOSTED_TIMEOUT_S // 60} min: {message}")
    else:
        patch.status["message"] = message


@kopf.on.delete(m.GROUP, m.VERSION, m.HOSTED_PLURAL)
def on_hosted_deleted(name, logger, **_):
    gateway.delete_server(name)  # tools, policies and grants go first (DB cascade)
    ns = m.hosted_namespace_name(name)
    core = CoreV1Api()
    try:
        if not _hosted_owned(core, name):
            logger.warning(f"hosted {name}: {ns} was not created for this server; leaving it alone")
            return
        if core.read_namespace(ns).status.phase != "Terminating":
            core.delete_namespace(ns)  # takes the Deployment, Service, Secret and policies with it
    except ApiException as e:
        if e.status == 404:
            logger.info(f"hosted {name}: removed")
            return
        raise
    raise kopf.TemporaryError(f"waiting for namespace {ns} to be deleted", delay=5)
