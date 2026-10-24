"""The store operator authenticates with its ServiceAccount token (TokenReview), not an API key."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import httpx2

from tests import KEY  # noqa: E402
from app import kube
from app.main import Settings, create_app
from tests.test_gateway import SETUP, serve, setup_admin

OPERATOR_SA = "system:serviceaccount:mercora:store-operator"
TOKENS = {"op-token": OPERATOR_SA, "other-token": "system:serviceaccount:mercora:default"}


def test_operator_token_reaches_only_the_server_registry(db_url):
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP,
                        operator_service_account=OPERATOR_SA)

    async def run():
        with serve(create_app(settings, token_review=TOKENS.get)) as gw:
            await setup_admin(gw)

            def client(token):
                return httpx2.AsyncClient(base_url=gw, timeout=10, headers={"Authorization": f"Bearer {token}"})

            op = client("op-token")
            assert (await op.get("/api/servers")).status_code == 200
            assert (await op.delete("/api/servers/999")).status_code == 404  # past auth, no such server
            assert (await op.get("/api/users")).status_code == 401
            assert (await op.get("/api/me")).status_code == 401
            for token in ("other-token", "forged"):
                assert (await client(token).get("/api/servers")).status_code == 401

    asyncio.run(run())


def test_operator_auth_is_off_without_its_service_account(db_url):
    settings = Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP)

    async def run():
        with serve(create_app(settings)) as gw:
            await setup_admin(gw)
            r = await httpx2.AsyncClient(base_url=gw).get("/api/servers", headers={"Authorization": "Bearer op-token"})
            assert r.status_code == 401

    asyncio.run(run())


def test_token_review_checks_the_audience_and_caches(monkeypatch):
    calls = []

    def post(url, json, **_):
        calls.append(json["spec"])
        status = {"op": {"authenticated": True, "audiences": [kube.TOKEN_AUDIENCE], "user": {"username": OPERATOR_SA}},
                  "wrong-aud": {"authenticated": True, "audiences": ["https://kubernetes.default.svc"],
                                "user": {"username": OPERATOR_SA}},
                  "bad": {"authenticated": False}}[json["spec"]["token"]]
        return NS(raise_for_status=lambda: None, json=lambda: {"status": status})

    monkeypatch.setattr(kube.httpx2, "post", post)
    monkeypatch.setattr(kube, "_sa_token", lambda: "gateway-sa-token")
    review = kube.TokenReview.__new__(kube.TokenReview)
    review.url, review.tls, review.cache = "https://api/tokenreviews", None, {}

    assert review("op") == review("op") == OPERATOR_SA
    assert review("wrong-aud") is None
    assert review("bad") is None
    assert [c["token"] for c in calls] == ["op", "wrong-aud", "bad"]  # the second "op" came from the cache
    assert calls[0]["audiences"] == [kube.TOKEN_AUDIENCE]
