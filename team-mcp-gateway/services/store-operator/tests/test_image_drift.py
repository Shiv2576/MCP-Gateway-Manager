from types import SimpleNamespace as NS

from kubernetes.client.exceptions import ApiException

from store_operator import handlers


def repair(monkeypatch, image):
    """Run repair_gateway with the edge deployment on `image` (None: no deployment); return helm calls."""
    class Apps:
        def read_namespaced_deployment(self, name, ns):
            if image is None:
                raise ApiException(status=404)
            return NS(spec=NS(template=NS(spec=NS(containers=[NS(image=image)]))))

    calls = []
    monkeypatch.setattr(handlers, "AppsV1Api", Apps)
    monkeypatch.setattr(handlers, "_helm", lambda *a: calls.append(a[0]))
    monkeypatch.setattr(handlers, "EDGE_IMAGE_TAG", "abc123")
    handlers.repair_gateway(name="prod", spec={}, meta={}, status={"phase": "Ready"}, logger=NS(warning=print))
    return calls


def test_edge_on_current_image_is_left_alone(monkeypatch):
    assert repair(monkeypatch, "gateway:abc123") == []


def test_edge_on_old_image_or_missing_is_upgraded(monkeypatch):
    assert repair(monkeypatch, "gateway:0.10.5") == ["gw-prod"]
    assert repair(monkeypatch, None) == ["gw-prod"]
