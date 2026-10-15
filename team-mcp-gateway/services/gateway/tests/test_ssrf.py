"""The SSRF guard on admin-added upstream URLs. No database needed."""

import pytest

from app.api import HTTPError, check_upstream_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://[fd00:ec2::254]/",                     # AWS metadata over IPv6
    "http://127.0.0.1:8000/api",
    "http://localhost/mcp",
    "http://2130706433/",                          # 127.0.0.1 as a decimal
    "http://[::1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://10.43.0.1:443/",                       # Kubernetes API Service
    "http://192.168.1.10/",
    "http://100.64.0.1/",                          # carrier-grade NAT
    "http://0.0.0.0:5432/",
    "http://team-postgres.mercora.svc.cluster.local:5432/",
    "http://woo-mcp-woo-mcp.mercora.svc.cluster.local:8000/mcp",  # not an operator namespace
    "ftp://example.com/",
    "http://:80/",
    "http://example.com:99999/",
])
def test_refused(url):
    with pytest.raises(HTTPError) as e:
        check_upstream_url(url)
    assert e.value.status == 400


@pytest.mark.parametrize("url,allow", [
    ("http://8.8.8.8/mcp", ()),
    ("http://woo-mcp-woo-mcp.store-shop1.svc.cluster.local:8000/mcp", ()),
    ("http://mcp.mcp-github.svc.cluster.local:8080/mcp", ()),
    ("http://notes.mercora.svc.cluster.local:8000/mcp", ("notes.mercora.svc.cluster.local",)),
])
def test_allowed(url, allow):
    assert check_upstream_url(url, allow) == url
