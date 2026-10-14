"""The /api/hosted endpoints, with an in-memory stand-in for the Kubernetes API."""

from __future__ import annotations

import asyncio

from tests import KEY  # noqa: E402
from app.kube import AlreadyExists
from app.main import Settings, create_app
from tests.test_gateway import SETUP, serve, setup_admin
from tests.test_stores import FakeStores, login


class FakeHosted:
    def __init__(self):
        self.items: dict[str, dict] = {}

    async def list(self, owner_id=None):
        return list(self.items.values())

    async def get(self, name):
        return self.items.get(name)

    async def create_object(self, obj):
        name = obj["metadata"]["name"]
        if name in self.items:
            raise AlreadyExists(name)
        obj["metadata"]["creationTimestamp"] = "2026-10-14T10:00:00Z"
        self.items[name] = obj
        return obj

    async def delete(self, name):
        self.items.pop(name, None)


NOTES = {"name": "notes2", "image": "docker.io/me/demo-mcp:0.1.0", "port": 8000, "path": "/mcp",
         "env": {"DEMO_SERVER": "notes", "AUTH_HEADER": "X-API-Key"},
         "token_env": "AUTH_TOKEN", "auth_header": "X-API-Key"}


def test_hosted_api(db_url):
    fake, stores = FakeHosted(), FakeStores()
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings, stores=stores, hosted=fake)) as gw:
            admin = await setup_admin(gw)
            await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})
            bob = await login(gw, "bob", "bob-password-123")
            assert (await bob.get("/api/hosted")).status_code == 403
            assert (await bob.post("/api/hosted", json=NOTES)).status_code == 403

            bad = [
                {"name": "Bad"}, {"name": "gateway"}, {"image": "has space"}, {"image": ""},
                {"port": 0}, {"port": "80"}, {"path": "mcp"}, {"args": "http"},
                {"env": {"1X": "v"}}, {"token_env": "DEMO_SERVER"}, {"auth_header": "bad header"},
            ]
            for patch in bad:
                r = await admin.post("/api/hosted", json={**NOTES, **patch})
                assert r.status_code == 400, (patch, r.text)
            assert fake.items == {}

            r = await admin.post("/api/hosted", json=NOTES)
            assert r.status_code == 201, r.text
            v = r.json()
            assert (v["name"], v["phase"], v["owner"], v["token_env"]) == ("notes2", "Provisioning", "admin", "AUTH_TOKEN")
            assert v["env"] == ["AUTH_HEADER", "DEMO_SERVER"]  # names only
            spec = fake.items["notes2"]["spec"]
            assert spec["tokenEnv"] == "AUTH_TOKEN" and spec["authHeader"] == "X-API-Key"
            assert (await admin.post("/api/hosted", json=NOTES)).status_code == 409

            # No token: no tokenEnv/authHeader in the spec (the gateway sends no credential).
            r = await admin.post("/api/hosted", json={"name": "github", "image": "ghcr.io/github/github-mcp-server",
                                                      "args": ["http"], "port": 8082, "path": "/"})
            assert r.status_code == 201, r.text
            assert "tokenEnv" not in fake.items["github"]["spec"]

            # Names shared with servers and stores are taken.
            await admin.post("/api/servers", json={"name": "fx", "url": "http://127.0.0.1:9/mcp"})
            assert (await admin.post("/api/hosted", json={**NOTES, "name": "fx"})).status_code == 409
            await admin.post("/api/stores", json={"name": "shop1", "template": "blank"})
            assert (await admin.post("/api/hosted", json={**NOTES, "name": "shop1"})).status_code == 409

            assert [h["name"] for h in (await admin.get("/api/hosted")).json()] == ["notes2", "github"]
            assert (await admin.delete("/api/hosted/notes2")).status_code == 202
            assert (await admin.delete("/api/hosted/notes2")).status_code == 404
            assert list(fake.items) == ["github"]

    asyncio.run(run())


def test_hosted_unconfigured(db_url):
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings)) as gw:
            admin = await setup_admin(gw)
            assert (await admin.get("/api/hosted")).status_code == 503

    asyncio.run(run())
