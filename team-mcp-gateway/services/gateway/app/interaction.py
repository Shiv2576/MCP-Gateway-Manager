"""Questions the gateway asks the user mid-call (MCP 2026-07-28 elicitation), without sessions.

A call that needs an answer returns InputRequiredResult; the client asks the user and
repeats the call with the answers and the echoed requestState, maybe on another replica.
The SDK's RequestStateBoundary (app/main.py) seals that state, binding it to the user, the
gateway and the call; the hash below is the gateway's own check on top. The state grants
nothing: the retry runs every check again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from .approvals import SUMMARY_MAX

DECLINED = "Cancelled by the user. Nothing was changed."


@dataclass(frozen=True)
class Ask:
    can_elicit: bool  # client declared form elicitation
    state: dict | None  # unsealed requestState, json-decoded; None on round 1
    answers: dict[str, ElicitResult]  # params.input_responses or {}


def from_request(ctx, params) -> Ask:
    caps = ctx.session.client_capabilities
    el = caps and caps.elicitation
    # An empty elicitation capability means form mode (spec); only 2026-07-28 can retry.
    can = bool(el and (el.form is not None or el.url is None)) and ctx.session.protocol_version in MODERN_PROTOCOL_VERSIONS
    return Ask(
        can_elicit=can,
        state=json.loads(params.request_state) if params.request_state else None,
        answers=params.input_responses or {},
    )


def call_hash(name: str, arguments: dict | None) -> str:
    canonical = json.dumps([name, arguments or {}], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _request(key: str, state: dict, message: str, field: dict) -> InputRequiredResult:
    schema = {"type": "object", "properties": {key: field}, "required": [key]}
    return InputRequiredResult(
        input_requests={key: ElicitRequest(params=ElicitRequestFormParams(message=message, requested_schema=schema))},
        request_state=json.dumps(state),
    )


def _call_text(name: str, title: str | None, arguments: dict) -> str:
    lines = [f"{k}: {json.dumps(v) if not isinstance(v, str) else v}" for k, v in arguments.items()]
    return "\n".join([title or name, *lines])


def confirm_request(name: str, title: str | None, arguments: dict) -> InputRequiredResult:
    return _request(
        "confirm", {"ask": "confirm", "call": call_hash(name, arguments)},
        f"Run this change?\n\n{_call_text(name, title, arguments)}",
        {"type": "boolean", "title": "Run it", "description": "Yes to run this call"},
    )


def reason_request(name: str, title: str | None, arguments: dict, why: str) -> InputRequiredResult:
    return _request(
        "reason", {"ask": "reason", "call": call_hash(name, arguments)},
        f"{why}\n\nThis is over your budget. Give a reason and the gateway will ask an admin to approve it."
        f"\n\n{_call_text(name, title, arguments)}",
        {"type": "string", "title": "Reason", "maxLength": SUMMARY_MAX},
    )


def accepted(ask: Ask, key: str) -> ElicitResult | None:
    """The user's answer to question `key` when they accepted, else None."""
    answer = ask.answers.get(key)
    return answer if isinstance(answer, ElicitResult) and answer.action == "accept" else None
