"""Updating a hosted server in place: roll out, keep serving, roll back with the reason."""

from types import SimpleNamespace as NS

from store_operator import handlers
from store_operator import manifests as m

OLD = {"image": "me/mcp:1", "port": 8000, "path": "/mcp", "env": {"A": "1"}}
NEW = {**OLD, "image": "me/mcp:2", "env": {"A": "1", "B": "2"}}
LOG = NS(info=lambda *a: None, warning=lambda *a: None)


class Patch:
    def __init__(self):
        self.status = {}


def fake_cluster(monkeypatch, deployment=None, waiting=()):
    replaced = []

    class Apps:
        def replace_namespaced_deployment(self, name, ns, body):
            replaced.append(body["spec"]["template"]["spec"]["containers"][0])

        def read_namespaced_deployment(self, name, ns):
            return deployment

    monkeypatch.setattr(handlers, "AppsV1Api", Apps)
    monkeypatch.setattr(handlers, "CoreV1Api", lambda: None)
    monkeypatch.setattr(handlers, "_waiting_reasons", lambda core, ns, since=None: list(waiting))
    return replaced


def dep(generation=2, observed=2, updated=1, available=1, total=1):
    return NS(metadata=NS(generation=generation), spec=NS(replicas=1),
              status=NS(observed_generation=observed, updated_replicas=updated, available_replicas=available, replicas=total))


def updating(**kw):
    return {"phase": "Updating", "serving": m.serving(OLD), "updateStarted": handlers._now(), **kw}


def test_a_ready_server_is_rolled_out_and_remembers_what_ran(monkeypatch):
    replaced = fake_cluster(monkeypatch)
    patch = Patch()
    handlers.on_hosted_updated(name="notes", spec=NEW, old=OLD, status={"phase": "Ready"}, meta={}, patch=patch, logger=LOG)
    assert replaced[0]["image"] == "me/mcp:2"
    assert patch.status["phase"] == "Updating" and patch.status["serving"] == {"image": "me/mcp:1", "args": [], "env": {"A": "1"}}


def test_a_second_edit_mid_rollout_keeps_the_last_working_version(monkeypatch):
    fake_cluster(monkeypatch)
    patch = Patch()
    third = {**NEW, "image": "me/mcp:3"}
    handlers.on_hosted_updated(name="notes", spec=third, old=NEW, status=updating(), meta={}, patch=patch, logger=LOG)
    assert patch.status["serving"]["image"] == "me/mcp:1"  # not me/mcp:2, which never came up


def test_finished_rollout_is_ready(monkeypatch):
    replaced = fake_cluster(monkeypatch, dep())
    patch = Patch()
    handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, status=updating(), patch=patch, logger=LOG)
    assert patch.status["phase"] == "Ready" and patch.status["serving"] is None and replaced == []


def test_still_rolling_out_waits(monkeypatch):
    for d in (dep(observed=1), dep(updated=0), dep(total=2)):  # not seen yet / not updated / old pod still there
        replaced = fake_cluster(monkeypatch, d)
        patch = Patch()
        handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, status=updating(), patch=patch, logger=LOG)
        assert "phase" not in patch.status and replaced == []


def test_a_stuck_new_pod_is_rolled_back_with_the_reason(monkeypatch):
    replaced = fake_cluster(monkeypatch, dep(total=2, updated=1, available=1), waiting=[("ImagePullBackOff", "not found")])
    patch = Patch()
    handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, status=updating(), patch=patch, logger=LOG)
    assert replaced[0]["image"] == "me/mcp:1"
    assert {e["name"] for e in replaced[0]["env"]} == {"A"}  # B, added by the failed update, is gone
    assert patch.status["phase"] == "Ready"
    assert "update to me/mcp:2 failed (ImagePullBackOff: not found); still serving me/mcp:1" == patch.status["message"]
    assert patch.status["serving"]["image"] == "me/mcp:1"  # still the target for the next rollback


def test_after_a_rollback_the_next_failed_update_restores_the_working_version(monkeypatch):
    fake_cluster(monkeypatch)
    patch = Patch()
    after_rollback = {"phase": "Ready", "serving": m.serving(OLD)}  # spec says me/mcp:2, which failed
    handlers.on_hosted_updated(name="notes", spec={**NEW, "image": "me/mcp:3"}, old=NEW, status=after_rollback,
                               meta={}, patch=patch, logger=LOG)
    assert patch.status["serving"]["image"] == "me/mcp:1"


def test_only_pods_created_since_the_update_count(monkeypatch):
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc)
    crashing = NS(state=NS(waiting=NS(reason="CrashLoopBackOff", message="old pod")))
    pods = [NS(metadata=NS(creation_timestamp=started - timedelta(hours=1)), status=NS(container_statuses=[crashing]))]
    core = NS(list_namespaced_pod=lambda ns, label_selector: NS(items=pods))
    assert handlers._waiting_reasons(core, "mcp-x") == [("CrashLoopBackOff", "old pod")]
    assert handlers._waiting_reasons(core, "mcp-x", since=started) == []  # the old crashing pod doesn't fail the fix


def test_a_missing_deployment_fails_instead_of_erroring_forever(monkeypatch):
    from kubernetes.client.exceptions import ApiException

    fake_cluster(monkeypatch)

    class Gone:
        def read_namespaced_deployment(self, name, ns):
            raise ApiException(status=404)

    monkeypatch.setattr(handlers, "AppsV1Api", Gone)
    patch = Patch()
    handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, status=updating(), patch=patch, logger=LOG)
    assert patch.status["phase"] == "Failed"


def test_a_rollout_that_never_finishes_is_rolled_back(monkeypatch):
    replaced = fake_cluster(monkeypatch, dep(updated=0))
    patch = Patch()
    handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, patch=patch, logger=LOG,
                              status=updating(updateStarted="2020-01-01T00:00:00+00:00"))
    assert replaced[0]["image"] == "me/mcp:1" and "not ready after 5 min" in patch.status["message"]


def test_the_deployment_keeps_the_old_pod_until_the_new_one_is_ready():
    s = m.hosted_deployment({**OLD, "args": []})["spec"]["strategy"]
    assert s == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}}


def test_an_update_the_api_refuses_shows_as_pending_and_is_retried(monkeypatch):
    import kopf
    import pytest
    from kubernetes.client.exceptions import ApiException

    written = []

    class Apps:
        def replace_namespaced_deployment(self, name, ns, body):
            raise ApiException(status=422, reason="Unprocessable Entity")

    class Custom:
        def patch_namespaced_custom_object_status(self, group, version, ns, plural, name, body):
            written.append(body["status"])

    monkeypatch.setattr(handlers, "AppsV1Api", Apps)
    monkeypatch.setattr(handlers, "CustomObjectsApi", Custom)
    with pytest.raises(kopf.TemporaryError):
        handlers.on_hosted_updated(name="notes", spec=NEW, old=OLD, status={"phase": "Ready"}, meta={},
                                   patch=Patch(), logger=LOG)
    s = written[0]
    assert s["phase"] == "Updating" and s["updateApplied"] is False and s["serving"]["image"] == "me/mcp:1"
    assert "422 Unprocessable Entity" in s["message"]


def test_a_pending_update_is_not_mistaken_for_a_finished_one(monkeypatch):
    replaced = fake_cluster(monkeypatch, dep())  # the old spec's rollout is complete
    patch = Patch()
    handlers.reconcile_hosted(name="notes", spec=NEW, meta={}, status=updating(updateApplied=False), patch=patch, logger=LOG)
    assert patch.status == {} and replaced == []  # stays Updating until the update is applied
