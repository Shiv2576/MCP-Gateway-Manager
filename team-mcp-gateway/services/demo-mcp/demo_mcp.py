"""Two small MCP servers, so the gateway has more than one upstream.

  DEMO_SERVER=notes  save_note, list        (credential: an X-API-Key header)
  DEMO_SERVER=fx     convert, list          (credential: a bearer token)

Both have a tool called `list` on purpose, to show the gateway's collision
scheme: clients see notes__list and fx__list.

Run with:  uvicorn demo_mcp:create_app --factory --host 0.0.0.0 --port 8000
Settings:  DEMO_SERVER, AUTH_HEADER (default Authorization), AUTH_TOKEN (24+ chars)
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Units of each currency per 1 INR. Fixed on purpose: a demo, not a quote.
RATES_PER_INR = {"USD": 0.012, "EUR": 0.011, "GBP": 0.0094, "AED": 0.044, "JPY": 1.78}


def build_notes() -> MCPServer:
    server = MCPServer("notes", instructions="A shared notepad for the team.")
    # shortcut: notes live in memory and vanish on restart; add a volume if they must last.
    notes: list[dict[str, Any]] = []

    @server.tool(title="Save note")
    async def save_note(
        text: Annotated[str, Field(min_length=1, max_length=2000)],
    ) -> dict[str, Any]:
        """Save a short note for the team."""
        note = {"id": len(notes) + 1, "text": text, "at": datetime.now(timezone.utc).isoformat()}
        notes.append(note)
        return note

    @server.tool(name="list", title="List notes")
    async def list_notes() -> dict[str, Any]:
        """List every saved note, oldest first."""
        return {"count": len(notes), "notes": notes}

    return server


def build_fx() -> MCPServer:
    server = MCPServer("fx", instructions="Converts Indian rupees using a fixed rate table.")

    @server.tool(title="Convert INR")
    async def convert(
        amount_inr: Annotated[float, Field(ge=0, description="Amount in Indian rupees")],
        to: Annotated[str, Field(description="Currency code, e.g. USD")],
    ) -> dict[str, Any]:
        """Convert an amount in INR to another currency."""
        rate = RATES_PER_INR.get(to.upper())
        if rate is None:
            raise ToolError(f"Unknown currency {to!r}. Known: {', '.join(RATES_PER_INR)}")
        return {"amount_inr": amount_inr, "currency": to.upper(), "amount": round(amount_inr * rate, 2), "rate": rate}

    @server.tool(name="list", title="List currencies")
    async def list_currencies() -> dict[str, Any]:
        """List the currencies and their rate per 1 INR."""
        return {"rates_per_inr": RATES_PER_INR}

    return server


class TokenGuard:
    """Only callers holding the credential (the gateway) get in."""

    def __init__(self, app: ASGIApp, header: str, expected: str):
        self._app, self._header, self._expected = app, header.lower().encode(), expected.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in ("/healthz", "/readyz"):
            supplied = dict(scope["headers"]).get(self._header, b"")
            if not hmac.compare_digest(supplied, self._expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def create_app() -> ASGIApp:
    kind = os.environ.get("DEMO_SERVER", "")
    builders = {"notes": build_notes, "fx": build_fx}
    if kind not in builders:
        raise RuntimeError("DEMO_SERVER must be notes or fx")
    token = os.environ.get("AUTH_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("AUTH_TOKEN must be at least 24 characters")
    header = os.environ.get("AUTH_HEADER", "Authorization")
    expected = f"Bearer {token}" if header.lower() == "authorization" else token

    server = builders[kind]()

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> Response:
        return JSONResponse({"status": "ready"})

    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )
    return TokenGuard(app, header, expected)
