"""SSRF guard for upstream MCP servers.

check_upstream_url (api.py) refuses a private address when a URL is registered; GuardedBackend
checks again on every connection, after DNS, and connects to the address it checked, so a name
that later resolves (rebinds) to a private address is still refused. The gateway's egress
NetworkPolicy is the network-level backstop."""

from __future__ import annotations

import ipaddress
import re
import socket

import anyio
import httpx2
from httpcore2 import AnyIOBackend, ConnectError

# The only cluster-internal upstreams, and only the store operator may register them:
# MCP servers it runs in its own locked-down namespaces (store-<name>, mcp-<name>).
OPERATOR_HOST = re.compile(r"^[a-z0-9-]+\.(?:store|mcp)-[a-z0-9-]+\.svc\.cluster\.local$")
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class Blocked(Exception):
    """A connection refused because the host resolved to a non-public address."""


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in NAT64:  # NAT64 forwards to the IPv4 address in the low 32 bits
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global


class GuardedBackend(AnyIOBackend):
    def exempt(self, host: str) -> bool:
        # Cluster DNS answers these names, not the internet, so they cannot be rebound; and
        # check_upstream_url lets only the operator (and first-run setup's seed) register them.
        return host.rstrip(".").endswith(".svc.cluster.local")

    def allowed(self, address: str) -> bool:
        return public(address)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        kw = {"timeout": timeout, "local_address": local_address, "socket_options": socket_options}
        if self.exempt(host):
            return await super().connect_tcp(host, port, **kw)
        try:
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConnectError(f"cannot resolve {host}: {exc}") from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if bad := next((a for a in addresses if not self.allowed(a)), None):
            raise Blocked(f"{host} resolves to {bad}, a private or internal address; refused")
        error: Exception = ConnectError(f"no address for {host}")
        for address in addresses:  # the checked address itself: no second lookup to rebind
            try:
                return await super().connect_tcp(address, port, **kw)
            except ConnectError as exc:
                error = exc
        raise error


def guarded_transport() -> httpx2.AsyncHTTPTransport:
    transport = httpx2.AsyncHTTPTransport()
    # shortcut: httpx2 takes no network_backend argument, so this sets the pool's private
    # attribute (httpx2 is pinned in uv.lock; test_ssrf fails if it moves). TLS still checks
    # the certificate against the hostname: httpcore2 takes SNI from the URL, not the address.
    transport._pool._network_backend = GuardedBackend()
    return transport
