"""The /api/gateways endpoints, with an in-memory stand-in for the Kubernetes API."""

from __future__ import annotations

import asyncio

import httpx2

from tests import KEY  # noqa: E402
from app.kube import gateway_resource
from app.main import Settings, create_app
from tests.test_gateway import SETUP, FakeGateways, serve, setup_admin


async def member(gw):
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    assert (await c.post("/api/login", json={"username": "bob", "password": "bob-password-123"})).status_code == 200
    return c


def test_gateway_api(db_url):
    fake = FakeGateways()

    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            bob = await member(gw)
            notes = (await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})).json()["id"]
            shop = (await admin.post("/api/servers", json={"name": "shop1", "url": "http://127.0.0.1:9/mcp", "store": "shop1"})).json()
            assert shop["store"] == "shop1" and shop["gateways"] == []

            assert (await bob.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 403
            for slug in ("P", "a", "-ab", "ab-", "a_b", "a" * 21):
                assert (await admin.post("/api/gateways", json={"slug": slug, "name": "x"})).status_code == 400, slug

            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 201, r.text
            g = r.json()
            assert (g["slug"], g["name"], g["phase"], g["servers"]) == ("prod", "Production", "Provisioning", [])
            assert (g["url"], g["mcp_url"]) == ("http://prod.gw.localhost", "http://prod.gw.localhost/mcp")
            assert fake.items["prod"]["kind"] == "McpGateway"
            assert g["pods"] == ""  # no edge yet
            fake.items["prod"]["status"] = {"phase": "Ready", "pods": "2/2"}  # what the operator writes
            assert (await admin.get("/api/gateways")).json()[0]["pods"] == "2/2"
            assert fake.items["prod"]["spec"] == {"displayName": "Production", "publicUrl": ""}
            # The same request again is a safe retry; a different one is a conflict.
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 200
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Again"})).status_code == 409

            r = await admin.post("/api/gateways", json={"slug": "ext", "name": "External", "public_url": "https://gw.example.com/"})
            assert r.json()["mcp_url"] == "https://gw.example.com/mcp"
            assert (await admin.post("/api/gateways", json={"slug": "bad", "name": "x", "public_url": "ftp://x"})).status_code == 400

            assert (await bob.put("/api/gateways/prod/servers", json={"server_ids": [notes]})).status_code == 403
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [999]})).status_code == 400
            assert (await admin.put("/api/gateways/nope/servers", json={"server_ids": []})).status_code == 404
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [notes, shop["id"]]})).status_code == 200
            assert (await admin.put("/api/gateways/prod/servers", json={"server_ids": [notes]})).status_code == 200  # replaces

            gws = {g["slug"]: g for g in (await bob.get("/api/gateways")).json()}  # members can read
            assert gws["prod"]["servers"] == [{"id": notes, "name": "notes", "identity": "shared"}]
            servers = {s["name"]: s for s in (await admin.get("/api/servers")).json()}
            assert servers["notes"]["gateways"] == ["prod"] and servers["shop1"]["gateways"] == []

            assert (await bob.delete("/api/gateways/prod")).status_code == 403
            assert (await admin.delete("/api/gateways/nope")).status_code == 404
            assert (await admin.delete("/api/gateways/prod")).status_code == 202
            assert "prod" not in fake.items
            assert [g["slug"] for g in (await admin.get("/api/gateways")).json()] == ["ext"]

            # Removing a server removes its attachments.
            await admin.put("/api/gateways/ext/servers", json={"server_ids": [notes]})
            await admin.delete(f"/api/servers/{notes}")
            assert (await admin.get("/api/gateways")).json()[0]["servers"] == []

    asyncio.run(run())


def test_gateway_row_is_removed_when_the_cluster_refuses(db_url):
    fake = FakeGateways(fail=True)

    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 502 and "the cluster said no" in r.json()["error"]
            assert (await admin.get("/api/gateways")).json() == []  # no row left behind
            fake.fail = False
            assert (await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})).status_code == 201

    asyncio.run(run())


def test_gateway_create_recovers_from_a_lost_response(db_url):
    fake = FakeGateways(lose_response=True)

    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP), gateways=fake)) as gw:
            admin = await setup_admin(gw)
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 502 and "prod" in fake.items  # made, but we never heard back
            [g] = (await admin.get("/api/gateways")).json()
            assert (g["slug"], g["phase"]) == ("prod", "Orphaned")

            fake.lose_response = False
            r = await admin.post("/api/gateways", json={"slug": "prod", "name": "Production"})
            assert r.status_code == 201 and r.json()["phase"] == "Provisioning"  # adopted
            [g] = (await admin.get("/api/gateways")).json()
            assert g["phase"] == "Provisioning"

            # Same name in the cluster with a different spec: not adopted.
            fake.items["other"] = gateway_resource("other", "Someone else", None)
            r = await admin.post("/api/gateways", json={"slug": "other", "name": "Mine"})
            assert r.status_code == 409
            assert (await admin.get("/api/gateways")).json()[-1]["phase"] == "Orphaned"

            # An orphan can be deleted.
            assert (await admin.delete("/api/gateways/other")).status_code == 202
            assert "other" not in fake.items

    asyncio.run(run())


def test_gateways_off_outside_a_cluster(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP))) as gw:
            admin = await setup_admin(gw)
            r = await admin.get("/api/gateways")
            assert r.status_code == 503 and "not configured" in r.json()["error"]

    asyncio.run(run())


def test_tailnet_gives_every_new_gateway_a_public_url(db_url):
    fake = FakeGateways()

    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, tailnet_domain="tail1234.ts.net")
        with serve(create_app(settings, gateways=fake)) as gw:
            admin = await setup_admin(gw)
            r = await admin.post("/api/gateways", json={"slug": "ts", "name": "Tailscale"})
            assert r.json()["mcp_url"] == "https://ts-gw.tail1234.ts.net/mcp"
            assert fake.items["ts"]["spec"]["publicUrl"] == "https://ts-gw.tail1234.ts.net"
            # A typed public URL still wins; a retry of the first request is still safe.
            r = await admin.post("/api/gateways", json={"slug": "ext", "name": "Ext", "public_url": "https://gw.example.com"})
            assert r.json()["mcp_url"] == "https://gw.example.com/mcp"
            assert (await admin.post("/api/gateways", json={"slug": "ts", "name": "Tailscale"})).status_code == 200

    asyncio.run(run())
