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
"""


def connect(path: str) -> sqlite3.Connection:
    # shortcut: one connection used from the event loop thread, so the gateway
    # runs as a single replica; move to Postgres before scaling out.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


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
