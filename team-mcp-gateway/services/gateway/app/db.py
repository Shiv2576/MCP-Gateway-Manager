"""PostgreSQL storage and the hashing used for passwords, sessions and API keys.

Every gateway pod (the control plane and each edge) uses the same database, so
a revoked key or a detached server stops working everywhere on the next request.
Only hashes of passwords, session tokens and API keys are stored, so a copy of
the database does not let anyone log in or call tools.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

IntegrityError = psycopg.IntegrityError
log = logging.getLogger("gateway.db")



class DatabaseUnavailable(RuntimeError):
    """Postgres could not be reached at start. The process should not start."""


def _pg(sql: str) -> str:
    # The SQL in this codebase uses "?" placeholders and never a literal "?" or "%".
    return sql.replace("?", "%s")


class Result:
    """Rows fetched up front, so the connection goes back to the pool at once."""

    def __init__(self, cur: psycopg.Cursor):
        self.rowcount = cur.rowcount
        self._rows = iter(cur.fetchall() if cur.description else [])

    # Consumed like a cursor: each row is returned once.
    def fetchone(self) -> dict | None:
        return next(self._rows, None)

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def __iter__(self):
        return self._rows


class _Conn:
    """One borrowed connection: what a transaction block works with."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def execute(self, sql: str, params=None) -> Result:
        return Result(self._conn.execute(_pg(sql), params))

    def executemany(self, sql: str, seq) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(_pg(sql), seq)


class Db:
    # shortcut: synchronous psycopg. The hot paths (/mcp auth, tools/list, tools/call,
    # approvals, readyz, logins, the health loop) call it from worker threads; the
    # dashboard's /api handlers still call it on the event loop. Move to
    # psycopg.AsyncConnection if those ever carry real load.
    def __init__(self, url: str, timeout: float):
        self.pool = ConnectionPool(
            url, min_size=1, max_size=10, timeout=timeout, open=True,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        try:
            self.pool.wait(timeout=timeout)
        except PoolTimeout:
            self.pool.close()
            raise DatabaseUnavailable(f"cannot reach the database within {timeout:g}s") from None

    def execute(self, sql: str, params=None) -> Result:
        with self.pool.connection() as conn:
            return _Conn(conn).execute(sql, params)

    def executemany(self, sql: str, seq) -> None:
        with self.pool.connection() as conn:
            _Conn(conn).executemany(sql, seq)

    @contextlib.contextmanager
    def transaction(self):
        with self.pool.connection() as conn, conn.transaction():
            yield _Conn(conn)

    def close(self) -> None:
        self.pool.close()


# ------------------------------------------------------------------ migrations
# app/migrations/NNNN_name.sql, applied in order, each once, recorded in schema_version.
# Rule for every new one: the PREVIOUS release's code must still work on the result, because a
# GitOps rollback (git revert) brings old code back but leaves the database as it is. So add
# tables and nullable columns; rename or drop only a release after nothing uses the old name.
# scripts/schema-compat.sh checks it in CI.

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
MIGRATION_LOCK = 72_031_910  # pg_advisory_lock key: one migrator at a time, across all pods


def migrations() -> list[tuple[int, str, str]]:
    """(version, name, sql), numbered 1, 2, 3 ... with no gaps or duplicates."""
    found = sorted((int(m[1]), m[2], p.read_text()) for p in MIGRATIONS_DIR.iterdir()
                   if (m := MIGRATION_FILE.match(p.name)))
    if [v for v, _, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError(f"migrations in {MIGRATIONS_DIR} must be numbered 0001, 0002, ... without gaps")
    return found


def migrate(db: Db) -> list[int]:
    """Apply the pending migrations, each in its own transaction; returns the versions applied.
    A database already ahead of this code (after a rollback to the previous release) is left
    alone: that's the case the N-1 rule makes safe."""
    applied = []
    with db.pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))  # waits while another pod migrates
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY,"
                         " name TEXT NOT NULL, applied_at DOUBLE PRECISION NOT NULL)")
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_version")}
            known = migrations()
            for version, name, sql in known:
                if version in done:
                    continue
                with conn.transaction():  # a failing migration leaves no trace and stops the start
                    conn.execute(sql)
                    conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (%s, %s, %s)",
                                 (version, name, time.time()))
                log.info("applied migration %04d_%s", version, name)
                applied.append(version)
            if done and max(done) > len(known):
                log.warning("database is at schema version %d, this code knows up to %d (a rollback?); "
                            "running on it as the N-1 rule allows", max(done), len(known))
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    return applied


def connect(url: str, *, create_schema: bool = True, timeout: float = 10.0) -> Db:
    """create_schema=False on edges: only the control plane migrates the schema."""
    db = Db(url, timeout)
    if create_schema:
        migrate(db)
    return db


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1
    )
    return hmac.compare_digest(actual.hex(), digest)


# Checked against when the username does not exist, so a wrong username takes
# as long as a wrong password and does not reveal which accounts exist.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def create_user(conn, username: str, password: str, role: str) -> int:
    return conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?) RETURNING id",
        (username, hash_password(password), role, time.time()),
    ).fetchone()["id"]


def allowed_tools(conn, user, gateway_slug: str) -> dict[int, set[str] | None]:
    """Server id -> allowed upstream tool names on this gateway; None = every tool.
    A member gets their teams' allows minus their own denies. Only servers attached
    to the gateway appear. Read on every request."""
    if user["role"] == "admin":
        rows = conn.execute(
            "SELECT gs.server_id FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id WHERE g.slug = ?",
            (gateway_slug,),
        )
        return {r["server_id"]: None for r in rows}
    out: dict[int, set[str] | None] = {}
    for r in conn.execute(
        "SELECT DISTINCT tt.server_id, tt.tool FROM team_members tm"
        " JOIN team_tools tt ON tt.team_id = tm.team_id"
        " JOIN gateways g ON g.id = tt.gateway_id"
        " JOIN gateway_servers gs ON gs.gateway_id = tt.gateway_id AND gs.server_id = tt.server_id"
        " WHERE tm.user_id = ? AND g.slug = ? AND NOT EXISTS (SELECT 1 FROM user_denies ud"
        "  WHERE ud.user_id = tm.user_id AND ud.gateway_id = tt.gateway_id"
        "  AND ud.server_id = tt.server_id AND ud.tool = tt.tool)",
        (user["id"], gateway_slug),
    ):
        out.setdefault(r["server_id"], set()).add(r["tool"])
    return out


def connection_identity(conn, gateway_slug: str, server_id: int) -> str | None:
    """'shared' or 'personal' for an attached server; None when it is not attached."""
    row = conn.execute(
        "SELECT gs.identity FROM gateway_servers gs JOIN gateways g ON g.id = gs.gateway_id"
        " WHERE g.slug = ? AND gs.server_id = ?",
        (gateway_slug, server_id),
    ).fetchone()
    return row and row["identity"]
