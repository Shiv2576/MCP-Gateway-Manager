"""Chat agent: an LLM that calls gateway tools as the logged-in user.

Every tool call goes through Upstreams.call_tool with the session's user and the
chosen gateway, so it gets that gateway's policy and is rate limited and audited
exactly like a call from that gateway's /mcp.
Nothing is stored: the browser sends the conversation with each request.
"""

from __future__ import annotations

import json
import re

import httpx2
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .api import HTTPError, body, current_user, gateway_param, handler

MAX_ROUNDS = 6
MAX_MESSAGES = 20
MAX_CHARS = 4000
# Function names the OpenAI-style API accepts; other tools are left out.
FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _messages(data: dict) -> list[dict]:
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise HTTPError(400, "'messages' must be a non-empty list")
    out = []
    for m in msgs[-MAX_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            raise HTTPError(400, "each message needs role user|assistant and string content")
        out.append({"role": m["role"], "content": m["content"][:MAX_CHARS]})
    if out[-1]["role"] != "user":
        raise HTTPError(400, "the last message must be from the user")
    return out


def _result_text(result) -> str:
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def _complete(settings, messages: list[dict], tools: list[dict]) -> dict:
    # Without a cap OpenRouter reserves the model's full output limit against your credit.
    payload = {"model": settings.llm_model, "messages": messages, "max_tokens": 1024}
    if tools:
        payload["tools"] = tools
    try:
        async with httpx2.AsyncClient(timeout=60) as http:
            r = await http.post(
                settings.llm_url,
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json=payload,
            )
    except httpx2.HTTPError as exc:
        raise HTTPError(502, f"model provider unreachable ({type(exc).__name__})") from None
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("choices"):
        detail = str((data.get("error") or {}).get("message", ""))[:200]
        raise HTTPError(502, f"model provider error: HTTP {r.status_code} {detail}".strip())
    return data["choices"][0]["message"]


@handler
async def chat(request: Request) -> Response:
    user = current_user(request)
    data = await body(request)
    gateway = gateway_param(request, data.get("gateway"))
    settings = request.app.state.settings
    if not settings.openrouter_api_key:
        raise HTTPError(503, "chat is not configured: OPENROUTER_API_KEY is not set")
    messages = [
        {
            "role": "system",
            "content": (
                f"You are the assistant in a team MCP gateway dashboard, acting as user '{user['username']}'. "
                "Use the tools to answer. Tool names are <server>__<tool>. "
                "Before calling a tool that creates, changes or deletes something, say what you will do "
                "and ask the user to confirm, unless they already asked for exactly that."
            ),
        },
        *_messages(data),
    ]
    upstreams = request.app.state.upstreams
    tools = [
        {"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
        for t in upstreams.list_tools(user, gateway)
        if FUNCTION_NAME.match(t.name)
    ]

    calls = []
    for _ in range(MAX_ROUNDS):
        reply = await _complete(settings, messages, tools)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            return JSONResponse({"reply": reply.get("content") or "", "calls": calls})
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                ok, text, args = False, "arguments were not a JSON object", {}
            else:
                result = await upstreams.call_tool(user, gateway, name, args)
                ok, text = not result.is_error, _result_text(result)
            text = text[:8000]
            calls.append({"name": name, "arguments": args, "ok": ok, "result": text[:2000]})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
    return JSONResponse({"reply": f"Stopped after {MAX_ROUNDS} rounds of tool calls.", "calls": calls})
