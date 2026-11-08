"""Credentials are sealed with a label naming their server (id and URL) and member, so database
write access alone can't move a stored secret to a row or address it wasn't entered for."""

from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from tests import KEY
from app import db as store
from app.credentials import Vault, upgrade_credentials
from app.main import Settings, create_app
from tests.test_gateway import SETUP, FakeGateways, serve, setup_admin

STORE = "https://realstore.example/mcp"
EVIL = "https://evil.example/mcp"


def test_a_value_opens_only_for_its_own_server_url_and_member():
    v = Vault(KEY)
    shared = v.seal("store-admin-token", server_id=1, url=STORE)
    assert v.open(shared, server_id=1, url=STORE) == "store-admin-token"
    assert v.open(shared, server_id=2, url=STORE) is None  # copied to another server's row
    assert v.open(shared, server_id=1, url=EVIL) is None  # left in place, URL pointed elsewhere
    alice = v.seal("alice-token", server_id=1, url=STORE, user_id=7)
    assert v.open(alice, server_id=1, url=STORE, user_id=7) == "alice-token"
    assert v.open(alice, server_id=1, url=STORE, user_id=8) is None  # copied into bob's row
    assert v.open(alice, server_id=1, url=STORE) is None  # passed off as the shared credential
    assert v.open(shared, server_id=1, url=STORE, user_id=7) is None  # and the other way round


def test_the_old_unlabeled_format_and_other_keys_are_refused():
    v = Vault(KEY)
    old = Fernet(KEY.encode()).encrypt(b"store-admin-token").decode()  # what the gateway wrote before
    assert v.open(old, server_id=1, url=STORE) is None  # only the startup upgrade reads it
    other = Vault(Fernet.generate_key().decode()).seal("x", server_id=1, url=STORE)
    assert v.open(other, server_id=1, url=STORE) is None
    assert v.open("v2:not-base64!!", server_id=1, url=STORE) is None
    assert v.open("v2:" + "A" * 8, server_id=1, url=STORE) is None  # too short to be a value


def test_startup_upgrade_reseals_old_values_and_keeps_a_backup(db_url):
    conn = store.connect(db_url)
    f = Fernet(KEY.encode())
    uid = store.create_user(conn, "alice", "alice-password-1", "member")
    s1 = conn.execute("INSERT INTO servers (name, url, auth_header, auth_value, created_at) VALUES ('shop', ?, 'Authorization', ?, 0)"
                      " RETURNING id", (STORE, f.encrypt(b"Bearer shop-token").decode())).fetchone()["id"]
    s2 = conn.execute("INSERT INTO servers (name, url, auth_header, auth_value, created_at) VALUES ('plain', ?, 'X-Key', 'plain-token', 0)"
                      " RETURNING id", (EVIL,)).fetchone()["id"]
    conn.execute("INSERT INTO user_credentials (user_id, server_id, secret, updated_at) VALUES (?, ?, ?, 0)",
                 (uid, s1, f.encrypt(b"alice-token").decode()))
    v = Vault(KEY)
    upgrade_credentials(conn, v)

    rows = {r["id"]: r["auth_value"] for r in conn.execute("SELECT id, auth_value FROM servers")}
    assert v.open(rows[s1], server_id=s1, url=STORE) == "Bearer shop-token"
    assert v.open(rows[s2], server_id=s2, url=EVIL) == "plain-token"  # plaintext from before encryption, sealed too
    secret = conn.execute("SELECT secret FROM user_credentials").fetchone()["secret"]
    assert v.open(secret, server_id=s1, url=STORE, user_id=uid) == "alice-token"
    backup = {(r["source"], r["server_id"], r["user_id"]): r["value"]
              for r in conn.execute("SELECT * FROM credential_backup_v1")}
    assert set(backup) == {("servers", s1, None), ("user_credentials", s1, uid)}  # the Fernet ones, for a rollback
    assert f.decrypt(backup[("servers", s1, None)].encode()) == b"Bearer shop-token"

    upgrade_credentials(conn, v)  # a restart: nothing left to do, nothing backed up twice
    assert conn.execute("SELECT count(*) AS n FROM credential_backup_v1").fetchone()["n"] == 2
    assert {r["id"]: r["auth_value"] for r in conn.execute("SELECT id, auth_value FROM servers")} == rows
    conn.close()


def test_changing_a_servers_url_needs_its_credential_again(db_url):
    async def run():
        with serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP),
                              gateways=FakeGateways())) as gw:
            admin = await setup_admin(gw)
            sid = (await admin.post("/api/servers", json={"name": "s", "url": "http://127.0.0.1:9/mcp",
                   "auth": {"type": "bearer", "token": "tok"}})).json()["id"]
            r = await admin.patch(f"/api/servers/{sid}", json={"url": "http://127.0.0.1:10/mcp"})
            assert r.status_code == 400 and "enter the server's credential again" in r.text
            r = await admin.patch(f"/api/servers/{sid}", json={"url": "http://127.0.0.1:10/mcp",
                                                               "auth": {"type": "bearer", "token": "tok"}})
            assert r.status_code == 200
            assert (await admin.patch(f"/api/servers/{sid}", json={"enabled": False})).status_code == 200  # same URL: kept

    asyncio.run(run())
