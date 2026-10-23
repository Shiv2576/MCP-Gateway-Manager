from types import SimpleNamespace as NS

from kubernetes.client.exceptions import ApiException

from store_operator import handlers


def run(monkeypatch, status, dep):
    class Apps:
        def read_namespaced_deployment(self, name, ns):
            if dep is None:
                raise ApiException(status=404)
            return dep

    monkeypatch.setattr(handlers, "AppsV1Api", Apps)
    patch = NS(status={})
    meta = {"creationTimestamp": "2099-01-01T00:00:00Z"}
    handlers.reconcile_gateway(name="prod", meta=meta, status=status, patch=patch, logger=NS(info=print))
    return patch.status


def edge(ready, want):
    return NS(status=NS(ready_replicas=ready), spec=NS(replicas=want))


def test_pods_written_only_on_change(monkeypatch):
    assert run(monkeypatch, {"phase": "Ready"}, edge(2, 2)) == {"pods": "2/2"}
    assert run(monkeypatch, {"phase": "Ready", "pods": "2/2"}, edge(2, 2)) == {}
    assert run(monkeypatch, {"phase": "Ready", "pods": "2/2"}, edge(None, 2)) == {"pods": "0/2"}
    assert run(monkeypatch, {"phase": "Ready", "pods": "2/2"}, None) == {"pods": ""}


def test_provisioning_turns_ready_with_one_pod(monkeypatch):
    assert run(monkeypatch, {"phase": "Provisioning"}, edge(1, 2)) == {"pods": "1/2", "phase": "Ready", "message": ""}
    assert run(monkeypatch, {"phase": "Provisioning", "pods": "0/2"}, edge(0, 2)) == {}
