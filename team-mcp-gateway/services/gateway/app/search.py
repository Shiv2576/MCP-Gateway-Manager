"""Search mode: a client that sends `X-Gateway-Tool-Mode: search` is listed two tools instead of
every allowed tool's schema, which is large (one store's woo-mcp alone is ~14k tokens):
gateway__search_tools finds the few it needs, and gateway__call_tool runs one through the normal
Upstreams.call_tool path, so policy, budget, confirmation, PII filtering and audit all apply."""

from __future__ import annotations

import json
import re

from mcp.types import CallToolResult, TextContent, Tool

HEADER = "x-gateway-tool-mode"
SEARCH, CALL = "gateway__search_tools", "gateway__call_tool"
DEFAULT_LIMIT, MAX_LIMIT = 5, 20

TOOLS = [
    Tool(
        name=SEARCH,
        description=(
            "[gateway] Find tools by what you want to do, e.g. 'low stock in shop2' or 'convert currency'. "
            "Returns the best matches with their full input schemas. Then run one with gateway__call_tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to do, in a few words"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                          "description": f"How many tools to return (default {DEFAULT_LIMIT})"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name=CALL,
        description=(
            "[gateway] Run a tool found with gateway__search_tools, by its full name <server>__<tool>, "
            "with arguments that match its input schema."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The full tool name, <server>__<tool>"},
                "arguments": {"type": "object", "description": "The tool's arguments"},
            },
            "required": ["name"],
        },
    ),
]


def wants_search(headers) -> bool:
    return (headers.get(HEADER) or "").strip().lower() == "search"


def listed(tools: list[Tool]) -> list[Tool]:
    """What a search-mode client is listed: the two search tools plus the gateway's own tools."""
    return TOOLS + [t for t in tools if t.name.startswith("gateway__")]


def _text(text: str, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=error)


def search(tools: list[Tool], arguments: dict | None) -> CallToolResult:
    """The best matches among the caller's allowed tools: query words in a tool's name count three
    times as much as in its description."""
    args = arguments or {}
    query, limit = args.get("query"), args.get("limit", DEFAULT_LIMIT)
    if not isinstance(query, str) or not query.strip():
        return _text("Pass {\"query\": \"what you want to do\"}.", error=True)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        return _text(f"limit must be a whole number from 1 to {MAX_LIMIT}.", error=True)
    words = set(re.findall(r"[a-z0-9]+", query.lower()))

    def score(t: Tool) -> int:
        name = set(re.findall(r"[a-z0-9]+", t.name.lower()))
        text = set(re.findall(r"[a-z0-9]+", f"{t.title or ''} {t.description or ''}".lower()))
        return 3 * len(words & name) + len(words & text)

    ranked = sorted((t for t in tools if not t.name.startswith("gateway__")), key=lambda t: (-score(t), t.name))
    hits = [t for t in ranked if score(t) > 0][:limit]
    if not hits:
        return _text(f"No tool matches '{query}'. Try other words.")
    return _text(json.dumps([
        {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in hits
    ]))


def unwrap(arguments: dict | None) -> tuple[str, dict] | CallToolResult:
    """The tool name and arguments a gateway__call_tool carries, or why they are refused."""
    args = arguments or {}
    name, inner = args.get("name"), args.get("arguments") or {}
    if not isinstance(name, str) or not name or not isinstance(inner, dict):
        return _text("Pass {\"name\": \"<server>__<tool>\", \"arguments\": {...}}.", error=True)
    if name in (SEARCH, CALL):
        return _text(f"{name} can't be run through {CALL}.", error=True)
    return name, inner
