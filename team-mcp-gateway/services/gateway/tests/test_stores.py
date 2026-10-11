"""The /api/stores endpoints, with an in-memory stand-in for the Kubernetes API."""

from __future__ import annotations

import asyncio

import httpx2

from tests import KEY  # noqa: E402
from app.kube import OWNER_LABEL, AlreadyExists, store_resource
from app.main import Settings, create_app
from tests.test_gateway import SETUP, serve, setup_admin


class FakeStores:
    def __init__(self):
        self.items: dict[str, dict] = {}

    async def list(self, owner_id=None):
        return [c for c in self.items.values()
                if owner_id is None or c["metadata"]["labels"][OWNER_LABEL] == str(owner_id)]

    async def get(self, name):
        return self.items.get(name)

    async def create(self, name, template, owner_id, owner):
        if name in self.items:
            raise AlreadyExists(name)
        cr = store_resource(name, template, owner_id, owner)
        cr["metadata"]["creationTimestamp"] = f"2026-10-10T10:00:0{len(self.items)}Z"
        self.items[name] = cr
        return cr

    async def delete(self, name):
        self.items.pop(name, None)


async def login(gw, username, password):
    c = httpx2.AsyncClient(base_url=gw, timeout=10)
    assert (await c.post("/api/login", json={"username": username, "password": password})).status_code == 200
    return c


def test_store_api(db_url):
    fake = FakeStores()
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings, stores=fake)) as gw:
            admin = await setup_admin(gw)
            carol_id = (await admin.post("/api/users", json={"username": "carol", "password": "carol-password-1", "role": "admin"})).json()["id"]
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            carol = await login(gw, "carol", "carol-password-1")
            bob = await login(gw, "bob", "bob-password-123")

            assert (await bob.get("/api/stores")).status_code == 403
            for name in ("A", "x", "shop_1", "a" * 21, "-ab", "ab-"):
                r = await admin.post("/api/stores", json={"name": name, "template": "blank"})
                assert r.status_code == 400, name
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "huge"})).status_code == 400

            r = await admin.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})
            assert r.status_code == 201, r.text
            s = r.json()
            assert (s["name"], s["template"], s["owner"], s["phase"]) == ("shop1", "catalog-a", "admin", "Provisioning")
            assert s["url"] == "http://shop1.localhost" and s["created"] > 0
            assert fake.items["shop1"]["metadata"]["labels"][OWNER_LABEL] == str(s["owner_id"])

            # A retried create returns the same store; a different one is a conflict.
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})).status_code == 200
            assert (await admin.post("/api/stores", json={"name": "shop1", "template": "blank"})).status_code == 409
            assert (await carol.post("/api/stores", json={"name": "shop1", "template": "catalog-a"})).status_code == 409

            # A store name cannot shadow a registered server (it becomes the tool prefix).
            await admin.post("/api/servers", json={"name": "notes", "url": "http://127.0.0.1:9/mcp"})
            assert (await admin.post("/api/stores", json={"name": "notes", "template": "blank"})).status_code == 409

            for name in ("shop2", "shop3"):
                assert (await admin.post("/api/stores", json={"name": name, "template": "blank"})).status_code == 201
            r = await admin.post("/api/stores", json={"name": "shop4", "template": "blank"})
            assert r.status_code == 400 and "3" in r.json()["error"]
            assert (await carol.post("/api/stores", json={"name": "shop9", "template": "catalog-b"})).status_code == 201

            assert {s["name"] for s in (await carol.get("/api/stores")).json()} == {"shop1", "shop2", "shop3", "shop9"}
            assert (await carol.delete("/api/stores/shop1")).status_code == 404
            assert (await admin.delete("/api/stores/shop1")).status_code == 202
            assert "shop1" not in fake.items

            # Deleting an admin deletes the stores they own.
            assert (await admin.delete(f"/api/users/{carol_id}")).status_code == 200
            assert set(fake.items) == {"shop2", "shop3"}

    asyncio.run(run())


def test_stores_off_outside_a_cluster(db_url):
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings)) as gw:
            admin = await setup_admin(gw)
            r = await admin.get("/api/stores")
            assert r.status_code == 503 and "not configured" in r.json()["error"]
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123", "role": "admin"})).json()["id"]
            assert (await admin.delete(f"/api/users/{bob_id}")).status_code == 200  # no stores to delete

    asyncio.run(run())


class SlowStores(FakeStores):
    async def create(self, *args):
        await asyncio.sleep(0.2)  # widen the gap between the cap check and the create
        return await super().create(*args)


def test_store_cap_holds_under_concurrent_creates(db_url):
    fake = SlowStores()
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings, stores=fake)) as gw:
            admin = await setup_admin(gw)
            for name in ("one", "two"):
                assert (await admin.post("/api/stores", json={"name": name, "template": "blank"})).status_code == 201
            r1, r2 = await asyncio.gather(
                admin.post("/api/stores", json={"name": "zeta", "template": "blank"}),
                admin.post("/api/stores", json={"name": "alpha", "template": "blank"}),
            )
            assert sorted((r1.status_code, r2.status_code)) == [201, 400]
            assert len(fake.items) == 3

    asyncio.run(run())


def test_store_names_cannot_take_platform_hostnames(db_url):
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings, stores=FakeStores())) as gw:
            admin = await setup_admin(gw)
            for name in ("gateway", "dash", "store"):
                r = await admin.post("/api/stores", json={"name": name, "template": "blank"})
                assert r.status_code == 400 and "reserved" in r.json()["error"], name

    asyncio.run(run())
