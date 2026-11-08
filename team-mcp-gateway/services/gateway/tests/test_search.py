"""Search mode: two tools instead of every schema; the call still takes the normal path."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests import KEY
from app.main import Settings, create_app
from app.search import search as rank
from tests.test_gateway import (
    FX_TOKEN, NOTES_KEY, SETUP, FakeGateways, add_gateway, add_team, edge_settings, gateway_client,
    http_app, serve, setup_admin, text, wait_tools,
)

from demo_mcp import build_fx, build_notes  # noqa: E402


@contextlib.asynccontextmanager
async def search_client(url: str, key: str):
    headers = {"Authorization": f"Bearer {key}", "X-Gateway-Tool-Mode": "search"}
    async with httpx2.AsyncClient(headers=headers, timeout=10) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client:
            yield client


def test_search_mode(db_url):
    async def run():
        with (
            serve(http_app(build_notes(), "X-API-Key", NOTES_KEY)) as notes,
            serve(http_app(build_fx(), "Authorization", f"Bearer {FX_TOKEN}")) as fx,
            serve(create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP),
                             gateways=FakeGateways())) as gw,
        ):
            admin = await setup_admin(gw)
            nid = (await admin.post("/api/servers", json={"name": "notes", "url": f"{notes}/mcp",
                   "auth": {"type": "header", "name": "X-API-Key", "value": NOTES_KEY}})).json()["id"]
            fid = (await admin.post("/api/servers", json={"name": "fx", "url": f"{fx}/mcp",
                   "auth": {"type": "bearer", "token": FX_TOKEN}})).json()["id"]
            await add_gateway(admin, "prod", [nid, fid])
            bob_id = (await admin.post("/api/users", json={"username": "bob", "password": "bob-password-123"})).json()["id"]
            # bob may use fx entirely and notes' list, not notes' save_note.
            await add_team(admin, "t", [bob_id], "prod", [(fid, "convert"), (fid, "list"), (nid, "list")])
            bob = httpx2.AsyncClient(base_url=gw, timeout=10)
            await bob.post("/api/login", json={"username": "bob", "password": "bob-password-123"})
            key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]

            with serve(create_app(edge_settings(db_url, "prod"))) as edge:
                normal = await wait_tools(edge, key, 3)
                assert normal == {"fx__convert", "fx__list", "notes__list"}  # without the header: unchanged

                async with search_client(edge, key) as mcp:
                    names = {t.name for t in (await mcp.list_tools()).tools}
                    assert {"gateway__search_tools", "gateway__call_tool"} <= names
                    assert not names & normal  # no upstream schema is sent

                    hits = json.loads(text(await mcp.call_tool("gateway__search_tools", {"query": "convert currency"})))
                    assert hits[0]["name"] == "fx__convert" and hits[0]["input_schema"]["type"] == "object"
                    # Only allowed tools are found: save_note is hidden from bob.
                    found = text(await mcp.call_tool("gateway__search_tools", {"query": "save note", "limit": 20}))
                    assert "notes__save_note" not in found

                    r = await mcp.call_tool("gateway__call_tool", {"name": "fx__list", "arguments": {}})
                    assert not r.is_error, text(r)
                    r = await mcp.call_tool("gateway__call_tool", {"name": "notes__save_note", "arguments": {"text": "x"}})
                    assert r.is_error and "Unknown tool" in text(r)  # policy still applies
                    r = await mcp.call_tool("gateway__call_tool", {"name": "gateway__call_tool", "arguments": {}})
                    assert r.is_error

                # Audited under the real tool's name.
                log = (await admin.get("/api/audit")).json()
                assert [(e["tool"], e["ok"]) for e in log[:2]] == [("notes__save_note", 0), ("fx__list", 1)]

                async with gateway_client(edge, key) as mcp:  # a client without the header can call through it too
                    assert not (await mcp.call_tool("gateway__call_tool", {"name": "fx__list"})).is_error

    asyncio.run(run())


def test_search_ranks_names_above_descriptions_and_checks_input():
    from mcp.types import Tool

    tools = [
        Tool(name="a__other", description="convert things", input_schema={"type": "object"}),
        Tool(name="b__convert", description="money", input_schema={"type": "object"}),
        Tool(name="gateway__request_approval", description="convert", input_schema={"type": "object"}),
    ]
    hits = json.loads(text(rank(tools, {"query": "convert"})))
    assert [h["name"] for h in hits] == ["b__convert", "a__other"]  # gateway tools are never search hits
    assert "No tool matches" in text(rank(tools, {"query": "zebra"}))
    assert rank(tools, {"query": ""}).is_error
    assert rank(tools, {"query": "x", "limit": 0}).is_error
    assert rank(tools, {"query": "x", "limit": True}).is_error
