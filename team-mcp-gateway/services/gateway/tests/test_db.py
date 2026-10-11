"""The Postgres wrapper the rest of the gateway uses."""

from __future__ import annotations

import time

from app import db as store


def test_db_wrapper(db_url):
    db = store.connect(db_url)
    uid = store.create_user(db, "ann", "ann-password-123", "admin")
    assert db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone() == {"username": "ann"}
    assert db.execute("DELETE FROM sessions").rowcount == 0
    assert [r["username"] for r in db.execute("SELECT username FROM users")] == ["ann"]
    store.create_user(db, "bea", "bea-password-123", "member")
    rows = db.execute("SELECT username FROM users ORDER BY id")
    assert rows.fetchone()["username"] == "ann"  # advances like a cursor
    assert rows.fetchone()["username"] == "bea"
    assert rows.fetchone() is None

    try:
        with db.transaction() as tx:
            tx.execute("INSERT INTO servers (name, url, created_at) VALUES ('a', 'http://a', 0)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.execute("SELECT COUNT(*) AS n FROM servers").fetchone()["n"] == 0  # rolled back

    try:
        store.create_user(db, "ann", "other-password-1", "admin")
        raise AssertionError("duplicate username accepted")
    except store.IntegrityError:
        pass
    store.connect(db_url).close()  # a second start leaves the schema alone
    db.close()


def test_unreachable_database_fails_fast():
    started = time.monotonic()
    try:
        store.connect("postgresql://u:p@127.0.0.1:1/x", timeout=2)
        raise AssertionError("connected to nothing")
    except store.DatabaseUnavailable as e:
        assert "2s" in str(e)
    assert time.monotonic() - started < 6
