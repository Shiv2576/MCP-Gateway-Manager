"""kopf handlers: Store resource -> namespace + Helm releases + gateway server.

Every step is safe to repeat, so after an operator restart kopf re-runs a
handler from the top and it converges. Run:
  kopf run -m store_operator.handlers --standalone --namespace <gateway namespace>
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

import kopf
import kubernetes
from kubernetes.client import ApiClient, ApiException, AppsV1Api, BatchV1Api, CoreV1Api, CustomObjectsApi, NetworkingV1Api

from . import manifests as m
from .gateway import Gateway

NAMESPACE = os.environ.get("STORES_NAMESPACE", "mercora")
CHARTS = os.environ.get("CHARTS_DIR", "/charts")
MAX_ATTEMPTS = 3
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
    message = message.replace("<namespace>", ns)
    now = datetime.now(timezone.utc)
    if state == "failed":
        patch.status.update(phase="Failed", message=message)
    elif state == "ready":
        secret = CoreV1Api().read_namespaced_secret("woo-mcp-credentials", ns)
        token = base64.b64decode(secret.data["MCP_AUTH_TOKEN"]).decode()
        gateway.upsert_server(name, m.mcp_url(name), token)
        patch.status.update(phase="Ready", message="", readyAt=now.isoformat())
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
