"""SQLite storage and the hashing used for passwords, sessions and API keys.

Only hashes of passwords, session tokens and API keys are stored, so a copy of
the database file does not let anyone log in or call tools.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS servers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    -- The static credential: one header sent on every upstream request.
    -- Never returned by the API; only the gateway reads it.
    auth_header TEXT,
    auth_value  TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    at          REAL NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL,
    tool        TEXT NOT NULL,
    server      TEXT,
    ok          INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
-- Which servers a member may use. Admins use every server and have no rows.
CREATE TABLE IF NOT EXISTS user_servers (
    user_id   INTEGER NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, server_id)
);
-- OAuth for /mcp (see oauth.py). Clients register themselves.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- A sign-in in progress. code_hash is first the hash of the login page id,
-- then, once the user logs in, of the authorization code.
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    params     TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id  TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    scopes     TEXT NOT NULL,
    resource   TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    # shortcut: one connection used from the event loop thread, so the gateway
    # runs as a single replica; move to Postgres before scaling out.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _drop_customers(conn)
    had_grants = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'user_servers'").fetchone()
    conn.executescript(SCHEMA)
    if not had_grants:
        # Upgrade from before grants: members keep every server they could use.
        conn.execute(
            "INSERT INTO user_servers (user_id, server_id)"
            " SELECT u.id, s.id FROM users u, servers s WHERE u.role = 'member'"
        )
    return conn


def _drop_customers(conn: sqlite3.Connection) -> None:
    """An earlier version had a 'customer' role. Delete those users (their keys
    and sessions cascade; audit rows keep the username) and the columns it added."""
    users = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()
    if users and "'customer'" in users["sql"]:
        conn.execute("DELETE FROM users WHERE role = 'customer'")
        # SQLite cannot change a CHECK constraint in place: copy into a new table.
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            BEGIN;
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
                created_at REAL NOT NULL
            );
            INSERT INTO users_new SELECT id, username, password_hash, role, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            COMMIT;
            PRAGMA foreign_keys = ON;
            """
        )
    if "customer_tools" in {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}:
        conn.execute("ALTER TABLE servers DROP COLUMN customer_tools")


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


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), role, time.time()),
    )
    return cur.lastrowid


def granted_server_ids(conn: sqlite3.Connection, user) -> set[int] | None:
    """Server ids this user may use; None means all of them (admins)."""
    if user["role"] == "admin":
        return None
    rows = conn.execute("SELECT server_id FROM user_servers WHERE user_id = ?", (user["id"],))
    return {r[0] for r in rows}


def set_grants(conn: sqlite3.Connection, user_id: int, server_ids: list[int]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM user_servers WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO user_servers (user_id, server_id) VALUES (?, ?)",
            [(user_id, s) for s in server_ids],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
