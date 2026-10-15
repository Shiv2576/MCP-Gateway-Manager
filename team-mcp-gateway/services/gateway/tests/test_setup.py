"""First-run organization setup: once, with the setup token."""

from __future__ import annotations

import asyncio

import httpx2

from tests import KEY  # noqa: E402
from app.main import ConfigError, Settings, create_app
from tests.test_gateway import ADMIN, SETUP, serve

BODY = {"setup_token": SETUP, "org_name": "Acme", "username": ADMIN[0], "password": ADMIN[1]}


def test_control_plane_needs_a_setup_token_until_set_up(db_url):
    try:
        create_app(Settings(database_url=db_url, credentials_key=KEY))
    except ConfigError:
        return
    raise AssertionError("started with no way to set up")


def test_setup_once(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP))) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            assert (await c.get("/api/setup")).json() == {"needed": True, "org": None}
            r = await c.get("/api/users")
            assert r.status_code == 503 and "setup" in r.json()["error"]

            assert (await c.post("/api/setup", json={**BODY, "setup_token": "wrong"})).status_code == 403
            assert (await c.post("/api/setup", json={**BODY, "password": "short"})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "username": "Bad Name"})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "org_name": " "})).status_code == 400
            assert (await c.post("/api/setup", json={**BODY, "org_name": "x" * 81})).status_code == 400

            r = await c.post("/api/setup", json=BODY)
            assert r.status_code == 201 and r.json()["role"] == "admin"
            assert (await c.get("/api/me")).json()["username"] == ADMIN[0]  # logged in by the cookie
            assert (await c.get("/api/setup")).json() == {"needed": False, "org": "Acme"}
            r = await httpx2.AsyncClient(base_url=gw).post("/api/setup", json={**BODY, "username": "eve"})
            assert r.status_code == 409

    asyncio.run(run())
    create_app(Settings(database_url=db_url, credentials_key=KEY))  # set up: no token needed any more


def test_concurrent_setups_only_one_wins(db_url):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)
        with serve(create_app(settings)) as a, serve(create_app(settings)) as b:
            r1, r2 = await asyncio.gather(
                httpx2.AsyncClient(base_url=a, timeout=10).post("/api/setup", json=BODY),
                httpx2.AsyncClient(base_url=b, timeout=10).post("/api/setup", json={**BODY, "username": "root"}),
            )
            assert sorted((r1.status_code, r2.status_code)) == [201, 409]

    asyncio.run(run())


def test_wrong_setup_tokens_are_throttled(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP))) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            for _ in range(5):
                assert (await c.post("/api/setup", json={**BODY, "setup_token": "guess"})).status_code == 403
            assert (await c.post("/api/setup", json=BODY)).status_code == 429

    asyncio.run(run())


def test_setup_registers_the_store_server_first(db_url):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                            seed_server_url="http://127.0.0.1:9/mcp", seed_server_token="tok")
        app = create_app(settings)
        with serve(app) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            assert (await c.post("/api/setup", json=BODY)).status_code == 201
            assert (await c.post("/api/servers", json={"name": "aaa", "url": "http://127.0.0.1:9/mcp"})).status_code == 201
            names = [s["name"] for s in (await c.get("/api/servers")).json()]
            assert names == ["woo-mcp", "aaa"]  # seeded one first, though "aaa" sorts before it
            row = app.state.db.execute("SELECT * FROM servers WHERE name = 'woo-mcp'").fetchone()
            assert row["auth_header"] == "Authorization" and app.state.vault.open(row["auth_value"]) == "Bearer tok"

    asyncio.run(run())


class FakeStores:
    def __init__(self, items=None, fail=False):
        self.items, self.fail = items or [], fail

    async def list(self):
        if self.fail:
            raise RuntimeError("cluster unreachable")
        return self.items


def test_no_seed_when_stores_already_exist(db_url):
    async def run(stores):
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                            seed_server_url="http://127.0.0.1:9/mcp", seed_server_token="tok")
        app = create_app(settings)
        app.state.stores = stores
        with serve(app) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            assert (await c.post("/api/setup", json=BODY)).status_code == 201
            assert (await c.get("/api/servers")).json() == []

    asyncio.run(run(FakeStores([{"metadata": {"name": "shop1"}}])))


def test_no_seed_when_stores_cannot_be_listed(db_url):
    async def run():
        settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                            seed_server_url="http://127.0.0.1:9/mcp", seed_server_token="tok")
        app = create_app(settings)
        app.state.stores = FakeStores(fail=True)
        with serve(app) as gw:
            c = httpx2.AsyncClient(base_url=gw, timeout=10)
            assert (await c.post("/api/setup", json=BODY)).status_code == 201
            assert (await c.get("/api/servers")).json() == []

    asyncio.run(run())
