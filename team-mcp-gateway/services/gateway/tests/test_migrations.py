"""Versioned migrations: applied once, in order, under a lock; a failed one leaves no trace;
a database ahead of the code (after a rollback) is left alone."""

from __future__ import annotations

import threading

import pytest

from app import db as store


def versions(conn) -> list[int]:
    return [r["version"] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


def test_a_fresh_database_gets_every_migration_once(db_url):
    db = store.connect(db_url)
    assert versions(db) == list(range(1, len(store.migrations()) + 1))
    assert store.migrate(db) == []  # a restart applies nothing
    db.close()


def test_a_database_from_before_migrations_only_gets_the_baseline_recorded(db_url):
    db = store.connect(db_url)
    db.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES ('kept', 'x', 'admin', 0)")
    db.execute("DROP TABLE schema_version")  # as deployed before this change: the tables, no versions
    assert store.migrate(db)[0] == 1
    assert db.execute("SELECT username FROM users").fetchone()["username"] == "kept"
    db.close()


def test_a_failing_migration_is_rolled_back_and_not_recorded(db_url, tmp_path, monkeypatch):
    for f in store.MIGRATIONS_DIR.iterdir():
        (tmp_path / f.name).write_text(f.read_text())
    n = len(store.migrations())
    (tmp_path / f"{n + 1:04d}_good_then_bad.sql").write_text(
        "CREATE TABLE half_done (id INT); SELECT no_such_function();")
    monkeypatch.setattr(store, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(Exception):
        store.connect(db_url)
    db = store.Db(db_url, 10)
    assert versions(db)[-1] == n  # not recorded
    assert db.execute("SELECT to_regclass('half_done') AS t").fetchone()["t"] is None  # and nothing left behind
    db.close()


def test_a_database_ahead_of_the_code_is_left_alone(db_url):
    db = store.connect(db_url)
    db.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (999, 'from_the_next_release', 0)")
    assert store.migrate(db) == []  # no error: the previous release runs on the newer schema
    db.close()


def test_pods_starting_together_migrate_once(db_url):
    errors = []

    def start():
        try:
            store.connect(db_url).close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    db = store.Db(db_url, 10)
    assert versions(db) == list(range(1, len(store.migrations()) + 1))  # each exactly once (primary key)
    db.close()


def test_migration_files_are_numbered_without_gaps(tmp_path, monkeypatch):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    monkeypatch.setattr(store, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="without gaps"):
        store.migrations()
