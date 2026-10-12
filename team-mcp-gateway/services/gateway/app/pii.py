"""PII protection: one filter over every tool result, on gateways that turn it on.

Three kinds of rule. A pattern is a regex run over every string value; a key is
a JSON key whose whole value is replaced, at any depth; a field is one path in
one tool's result, ticked in the dashboard. Built-in rules live here, custom
rules in the pii_rules table. Everything but load_rules is pure, so it can run
in a worker thread under a time limit (see Upstreams.call_tool).
"""

from __future__ import annotations

import collections
import functools
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import regex
from mcp.types import CallToolResult, TextContent

FILTER_TIMEOUT = 2.0  # seconds; past it the call's result is withheld
MAX_SCAN = 100_000  # characters of one string the patterns see; the rest is cut off
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
FIELD_PATH = re.compile(r"^[A-Za-z0-9_$-]+(\[\])?(\.[A-Za-z0-9_$-]+(\[\])?)*$")
NON_TEXT = "[non-text content removed by PII protection]"


@dataclass(frozen=True)
class Rule:
    name: str
    type: str  # pattern | key | field
    match: str
    server_id: int | None = None  # field rules only
    tool: str | None = None  # field rules only
    check: Callable[[str], bool] | None = None  # built-in patterns: a match must also pass this
    note: str = ""  # shown in the dashboard next to a built-in


def _luhn(text: str) -> bool:
    total = 0
    for i, d in enumerate(int(c) for c in reversed(text) if c.isdigit()):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _phone(text: str) -> bool:
    # "2026-10-11 05" and "11-10-2026 05" are dates, not phones.
    if re.match(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}", text):
        return False
    digits = sum(c.isdigit() for c in text)
    if text.isdigit():  # a bare run: only a 10-digit mobile; longer ones are ids and timestamps
        return digits == 10
    return 10 <= digits <= 15  # E.164 numbers have at most 15 digits


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# Order matters: a card or an IP address must not be taken for a phone number.
BUILTIN = [
    # Starts only at the beginning of a run and never backtracks (++): without that,
    # "1-1-1-…a@" or "a@a.a.a.…1" is rescanned from every character (quadratic).
    Rule("EMAIL", "pattern", r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@(?:[A-Za-z0-9-]++\.)++[A-Za-z]{2,}+"),
    Rule("CREDIT_CARD", "pattern", r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)", check=_luhn, note="must pass the Luhn check"),
    Rule("SSN", "pattern", r"\b\d{3}-\d{2}-\d{4}\b"),
    Rule("IP_ADDRESS", "pattern", rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\d.])"),
    Rule("PHONE", "pattern", r"(?<![\w+])\+?\(?\d[\d ().-]{7,}\d(?!\w)", check=_phone,
         note="10-15 digits, not a date"),
    *(Rule("PERSON", "key", k) for k in ("customer", "customer_name", "first_name", "last_name", "full_name",
                                          "billing_name", "shipping_name")),
    *(Rule("ADDRESS", "key", k) for k in ("address", "address_1", "address_2", "street", "postcode", "zip",
                                           "ship_to", "billing", "shipping")),
    Rule("FREE_TEXT", "key", "customer_note"),
]
BUILTIN_NAMES = {r.name for r in BUILTIN}


def load_rules(conn) -> list[Rule]:
    """Custom rules first: an admin's specific pattern beats a broad built-in
    (AADHAAR before PHONE). Read on every call, so a change applies at once."""
    rows = conn.execute("SELECT name, type, match, server_id, tool FROM pii_rules ORDER BY id").fetchall()
    return [Rule(**r) for r in rows] + BUILTIN


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> regex.Pattern:
    # regex, not re: re holds the GIL for a whole match, so one catastrophic
    # pattern would freeze every other call on the pod; regex takes a timeout.
    return regex.compile(pattern)


def key_map(rules) -> dict[str, str]:
    """Lower-cased key -> rule name; the first rule for a key wins."""
    keys: dict[str, str] = {}
    for r in rules:
        if r.type == "key":
            keys.setdefault(r.match.lower(), r.name)
    return keys


def _patterns(rules) -> list[tuple[str, regex.Pattern, Callable | None]]:
    return [(r.name, _compile(r.match), r.check) for r in rules if r.type == "pattern"]


def _parts(path: str) -> list[tuple[str, bool]]:
    """"orders[].customer" -> [("orders", True), ("customer", False)]; True = each list element."""
    return [(p.removesuffix("[]"), p.endswith("[]")) for p in path.split(".")]


def covered_by(path: str, keys: dict[str, str]) -> str | None:
    """The key rule that already masks this field (or one of its parents)."""
    for key, _ in _parts(path):
        if key.lower() in keys:
            return keys[key.lower()]
    return None


def _scan(text: str, patterns, counts, deadline: float) -> str:
    """Raises TimeoutError once the deadline passes."""
    if len(text) > MAX_SCAN:
        text = text[:MAX_SCAN] + "[TRUNCATED]"
    for name, rx, check in patterns:
        def sub(m, name=name, check=check) -> str:
            # An empty match (a rule like "z*") would wrap every character: skip it.
            if not m.group() or (check and not check(m.group())):
                return m.group()
            counts[name] += 1
            return f"[{name}]"
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("PII filter deadline passed")
        text = rx.sub(sub, text, concurrent=True, timeout=left)
    return text


def _walk(value: Any, keys, patterns, counts, deadline: float) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = keys.get(k.lower()) if isinstance(k, str) else None
            if name and v is not None:
                counts[name] += 1
                out[k] = f"[{name}]"
            else:
                out[k] = _walk(v, keys, patterns, counts, deadline)
        return out
    if isinstance(value, list):
        return [_walk(v, keys, patterns, counts, deadline) for v in value]
    if isinstance(value, str):
        return _scan(value, patterns, counts, deadline)
    return value  # numbers, booleans, null


def _mask_path(value: Any, parts, placeholder: str, counts, name: str) -> Any:
    """Replace one ticked path. A missing key or a value of another shape: nothing to do."""
    (key, many), rest = parts[0], parts[1:]
    if not isinstance(value, dict) or value.get(key) is None:
        return value

    def one(v):
        if v is None:
            return v
        if not rest:
            counts[name] += 1
            return placeholder
        return _mask_path(v, rest, placeholder, counts, name)

    out = dict(value)
    if not many:
        out[key] = one(out[key])
    elif isinstance(out[key], list):
        out[key] = [one(v) for v in out[key]]
    return out


def redact_value(value: Any, rules, server_id: int | None, tool: str | None, counts=None, deadline=None):
    """A filtered copy of one JSON value and the matches per rule name.
    Order: this tool's field rules, then key rules, then patterns.
    Raises TimeoutError past the deadline (default: FILTER_TIMEOUT from now)."""
    counts = collections.Counter() if counts is None else counts
    deadline = deadline or time.monotonic() + FILTER_TIMEOUT
    for r in rules:
        if r.type == "field" and r.server_id == server_id and r.tool == tool:
            value = _mask_path(value, _parts(r.match), f"[{r.name}]", counts, r.name)
    return _walk(value, key_map(rules), _patterns(rules), counts, deadline), counts


def redact_result(result: CallToolResult, rules, server_id: int | None, tool: str | None):
    """A filtered copy of a tool result and the matches per rule name.
    One FILTER_TIMEOUT for the whole result; past it, TimeoutError."""
    deadline = time.monotonic() + FILTER_TIMEOUT
    counts, text_counts = collections.Counter(), collections.Counter()
    structured = result.structured_content
    if structured is not None:
        structured, _ = redact_value(structured, rules, server_id, tool, counts, deadline)
    content = []
    for c in result.content:
        if not isinstance(c, TextContent):  # cannot be scanned: dropped, not passed through
            counts["NON_TEXT"] += 1
            content.append(TextContent(type="text", text=NON_TEXT))
            continue
        try:
            parsed = json.loads(c.text)
        except ValueError:
            text = _scan(c.text, _patterns(rules), text_counts, deadline)
        else:
            text = json.dumps(redact_value(parsed, rules, server_id, tool, text_counts, deadline)[0],
                              indent=2, ensure_ascii=False)
        content.append(TextContent(type="text", text=text))
    # The SDK sends a structured result twice (data, and the same JSON as text):
    # the larger count per rule, not the sum.
    return result.model_copy(update={"content": content, "structured_content": structured}), dict(counts | text_counts)


def schema_fields(output_schema: dict | None) -> list[dict]:
    """Every leaf path in a tool's output schema: [{path, type, description}].
    Follows $ref into $defs, array items ("items[]"), and nullable anyOf."""
    if not output_schema:
        return []
    defs = output_schema.get("$defs", {})
    out: list[dict] = []

    def resolve(s: dict) -> dict:
        for _ in range(10):  # $ref chains; bounded so a cycle cannot hang
            if "$ref" not in s:
                break
            s = defs.get(s["$ref"].rsplit("/", 1)[-1], {})
        branches = [b for b in s.get("anyOf", []) if b.get("type") != "null"]
        if len(branches) == 1:  # Optional[X]
            inner = resolve(branches[0])
            return {**inner, "description": s.get("description", inner.get("description"))}
        return s

    def walk(s: dict, path: str, depth: int) -> None:
        s = resolve(s)
        if depth > 8:
            return
        if s.get("type") == "object" and s.get("properties"):
            for k, sub in s["properties"].items():
                walk(sub, f"{path}.{k}" if path else k, depth + 1)
        elif s.get("type") == "array":
            walk(s.get("items", {}), f"{path}[]", depth + 1)
        elif path:
            out.append({"path": path, "type": s.get("type", "any"), "description": s.get("description")})

    walk(output_schema, "", 0)
    return out
