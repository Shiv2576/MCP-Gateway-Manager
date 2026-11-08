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


def test_old_audit_rows_get_an_outcome_once(db_url):
    from app.upstream import NO_RESULT

    db = store.connect(db_url)
    db.execute("ALTER TABLE audit_log DROP COLUMN outcome")  # as before the column existed
    old = [
        (1, None, "ok"),
        (0, "Too many calls: at most 2 per minute. Try again shortly.", "rate_limited"),
        (0, "Upstream 'x' did not answer within 1s.", "timeout"),
        (0, "Upstream 'x' is unreachable (ConnectError).", "unreachable"),
        (0, "Unknown tool 'a'. Tools are named <server>__<tool>.", "denied"),
        (0, "declined by the user", "denied"),
        (0, "note needs an admin's approval: over the hourly budget of 20 items/hour.", "over_budget"),
        (0, NO_RESULT, None),
        (0, "order 7 not found", "tool_error"),
    ]
    db.executemany("INSERT INTO audit_log (at, username, tool, ok, duration_ms, error) VALUES (0, 'u', 't', ?, 0, ?)",
                   [(ok, err) for ok, err, _ in old])
    store.connect(db_url).close()
    got = [r["outcome"] for r in db.execute("SELECT outcome FROM audit_log ORDER BY id")]
    assert got == [want for *_, want in old]
    db.execute("UPDATE audit_log SET outcome = 'ok' WHERE outcome IS NULL")  # a later start must not reclassify
    store.connect(db_url).close()
    assert [r["outcome"] for r in db.execute("SELECT outcome FROM audit_log ORDER BY id")][-2] == "ok"
    db.close()
