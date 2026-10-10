"""OAuth on /mcp: the browser-login flow a Claude custom connector runs, end to end."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx2

from app.main import Settings, create_app
from tests.test_gateway import ADMIN, NOTES_KEY, _free_port, gateway_client, http_app, serve
from tests.test_stores import login

from demo_mcp import build_fx, build_notes  # noqa: E402  (tests.test_gateway puts it on sys.path)

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def test_oauth_flow(tmp_path):
    port = _free_port()
    gw = f"http://127.0.0.1:{port}"
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1],
                        refresh_interval_s=0.5, public_url=gw)

    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx())) as fx,
            serve(create_app(settings), port=port),
        ):
            admin = await login(gw, *ADMIN)
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            await admin.post("/api/servers", json={"name": "fx", "url": f"{fx}/mcp"})
            await admin.put(f"/api/users/{bob_id}/servers", json={"server_ids": [nid]})
            c = httpx2.AsyncClient(base_url=gw, timeout=10)

            # Discovery: 401 points at the resource metadata, which points at the issuer.
            r = await c.post("/mcp", json={})
            assert r.status_code == 401
            assert f'resource_metadata="{gw}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
            prm = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert prm["resource"] == f"{gw}/mcp" and [u.rstrip("/") for u in prm["authorization_servers"]] == [gw]
            meta = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert meta["registration_endpoint"] == f"{gw}/register"

            # Registration: only allowed redirect URIs.
            reg = {"client_name": "Claude", "grant_types": ["authorization_code", "refresh_token"],
                   "response_types": ["code"]}
            r = await c.post("/register", json={**reg, "redirect_uris": ["https://evil.example/cb"]})
            assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
            r = await c.post("/register", json={**reg, "redirect_uris": [CALLBACK]})
            assert r.status_code == 201, r.text
            client = r.json()
            creds = {"client_id": client["client_id"], "client_secret": client["client_secret"]}

            # Authorize -> login page.
            r = await c.get("/authorize", params={
                "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
                "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "st8", "resource": f"{gw}/mcp"})
            assert r.status_code == 302, r.text
            login_url = r.headers["location"]
            assert login_url.startswith(f"{gw}/oauth/login?id=")
            login_id = parse_qs(urlsplit(login_url).query)["id"][0]
            page = await c.get(login_url)
            assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
            assert page.headers["x-frame-options"] == "DENY"

            form = {"id": login_id, "username": "bob"}
            r = await c.post("/oauth/login", data={**form, "password": "wrong-password"})
            assert r.status_code == 401 and "Wrong username or password" in r.text
            r = await c.post("/oauth/login", data={**form, "password": "bob-password-123"})
            assert r.status_code == 303
            back = urlsplit(r.headers["location"])
            assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
            q = parse_qs(back.query)
            assert q["state"] == ["st8"]
            code = q["code"][0]
            # The login page id is spent.
            assert (await c.get(login_url)).status_code == 400

            # Token exchange: PKCE checked, code works once.
            grant = {**creds, "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                     "resource": f"{gw}/mcp"}
            assert (await c.post("/token", data={**grant, "code_verifier": "x" * 64})).status_code == 400
            r = await c.post("/token", data={**grant, "code_verifier": VERIFIER})
            assert r.status_code == 200, r.text
            tokens = r.json()
            assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
            assert (await c.post("/token", data={**grant, "code_verifier": VERIFIER})).status_code == 400

            # The token is bob's: only his granted server.
            async with gateway_client(gw, tokens["access_token"]) as mcp:
                names = {t.name for t in (await mcp.list_tools()).tools}
            assert names == {"notes__save_note", "notes__list"}

            # Refresh rotates.
            refresh = {**creds, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            r = await c.post("/token", data=refresh)
            assert r.status_code == 200, r.text
            new = r.json()
            assert (await c.post("/token", data=refresh)).status_code == 400

            assert (await admin.get("/api/connect")).json() == {"oauth": True, "mcp_url": f"{gw}/mcp"}

            # Dashboard: bob sees his connection; the admin sees everyone's.
            bob = await login(gw, "bob", "bob-password-123")
            conns = (await bob.get("/api/oauth/connections")).json()
            assert [(x["username"], x["client_name"]) for x in conns] == [("bob", "Claude")]
            assert len((await admin.get("/api/oauth/connections")).json()) == 1

            # API keys still work next to OAuth.
            key = (await bob.post("/api/keys", json={"name": "cli"})).json()["key"]
            async with gateway_client(gw, key) as mcp:
                assert len((await mcp.list_tools()).tools) == 2

            # Disconnect ends access on the next call.
            r = await bob.delete(f"/api/oauth/connections/{bob_id}/{client['client_id']}")
            assert r.status_code == 200
            bearer = {"Authorization": f"Bearer {new['access_token']}"}
            assert (await c.post("/mcp", json={}, headers=bearer)).status_code == 401
            assert (await c.post("/token", data={**refresh, "refresh_token": new["refresh_token"]})).status_code == 400

    asyncio.run(run())


def test_oauth_off_without_public_url(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings)) as gw:
            c = httpx2.AsyncClient(base_url=gw)
            assert (await c.post("/register", json={})).status_code == 404
            assert (await c.get("/oauth/login")).status_code == 404
            r = await c.post("/mcp", json={})
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
            admin = await login(gw, *ADMIN)
            assert (await admin.get("/api/connect")).json() == {"oauth": False, "mcp_url": None}

    asyncio.run(run())


def test_wrong_passwords_lock_the_username(tmp_path):
    settings = Settings(database_path=str(tmp_path / "g.db"), admin_username=ADMIN[0], admin_password=ADMIN[1])

    async def run():
        with serve(create_app(settings)) as gw:
            c = httpx2.AsyncClient(base_url=gw)
            for _ in range(5):
                r = await c.post("/api/login", json={"username": "admin", "password": "wrong-password"})
                assert r.status_code == 401
            # Locked now, even with the right password; other usernames are not.
            r = await c.post("/api/login", json={"username": "ADMIN", "password": ADMIN[1]})
            assert r.status_code == 429
            r = await c.post("/api/login", json={"username": "someone", "password": "wrong-password"})
            assert r.status_code == 401

    asyncio.run(run())
