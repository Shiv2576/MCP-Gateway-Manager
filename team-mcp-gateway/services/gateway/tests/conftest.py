"""A fresh Postgres database for every test that asks for `db_url`.

Start one with:
  docker run -d --name gw-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine
  export TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

BASE = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def db_url():
    if not BASE:
        pytest.skip("set TEST_DATABASE_URL (see tests/conftest.py)")
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(BASE, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    yield BASE.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(BASE, autocommit=True) as c:
        # FORCE: apps under test may still hold pooled connections.
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def _loopback_upstreams(monkeypatch):
    """Fake upstreams listen on 127.0.0.1, which the SSRF guard refuses outside tests."""
    from app import api
    check = api.check_upstream_url
    monkeypatch.setattr(api, "check_upstream_url", lambda url, allow=(): check(url, (*allow, "127.0.0.1")))
