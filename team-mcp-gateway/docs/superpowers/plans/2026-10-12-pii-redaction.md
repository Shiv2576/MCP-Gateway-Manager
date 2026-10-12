# PII Protection Part 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-gateway "PII protection" switch that masks PII in every tool result
at the gateway's single choke point (`Upstreams.call_tool`), with an org-wide rule
library, per-tool field rules picked from each tool's output schema, and an audit
log that stores the payload the member received (never raw PII).

**Architecture:** `app/pii.py` holds the rules and a pure filter. `call_tool`
runs the filter in a worker thread under a 2 s limit and fails only that call
when the filter throws or times out. The filter runs after the upstream answers
and before the audit insert. `app/pii_api.py` serves the PII Protection tab.
woo-mcp returns Pydantic models so it advertises `outputSchema`. The dashboard
gains a PII Protection page, a switch on each gateway, and payloads in the audit
log.

**Tech Stack:** Python 3.12, mcp 2.x SDK, Starlette, psycopg 3 / Postgres, anyio
4 (`to_thread.run_sync(..., abandon_on_cancel=True)`), Pydantic 2, React (Vite),
Helm on k3d.

**Spec:** `docs/superpowers/specs/2026-10-11-pii-redaction-design.md`

## Global Constraints

- Patterns use the `regex` package (a drop-in for `re` that adds a per-call `timeout=` and releases the GIL with `concurrent=True`); no Presidio, no other new dependencies.
- Filter time limit `FILTER_TIMEOUT = 2.0` s; strings scanned up to 100 KB (`MAX_SCAN = 100_000`), then `[TRUNCATED]`.
- Custom regex at most 200 characters, compiled on save; key match 1-64 chars; rule name `^[A-Z][A-Z0-9_]{1,39}$`.
- Placeholders are `[<NAME>]`; field rules default to `REDACTED`.
- Audit payload cap 64 KB (`65_536` chars), then `…[truncated, N bytes]`.
- Withheld messages, verbatim: `Result withheld: the PII filter failed. An admin can check the gateway log.` and `Result withheld: the PII filter took too long. An admin can check the PII rules.`
- Non-text content becomes `[non-text content removed by PII protection]`.
- Protection is off by default and applies to admins too.
- Never `git commit`: the user makes all commits. "Checkpoint" steps mean: stop, run the tests, and leave the change for the user.
- Rules and the gateway flag are read from the database on every call (no caches beyond compiled regexes).

## Spec amendments (found while planning; written back into the spec in Task 1)

1. **A protected gateway lists tools without `outputSchema`.** MCP clients check
   `structuredContent` against the schema, so a masked `integer` or a key rule
   that turns an object into a string would make the client reject the result.
   The dashboard still reads schemas from the health cache.
2. **Custom patterns run before built-in ones.** An admin's specific pattern
   should win over a broad built-in: otherwise PHONE (10-15 digits) would take
   `2345 6789 0123` before AADHAAR sees it.
3. **PHONE allows 10-15 digits and never matches text that starts like a date
   (`2026-10-11`).** Without the upper limit, a 16-digit number that fails the
   Luhn check would be masked as PHONE.
4. **`/api/pii-rules/test` runs under the same thread and time limit.** A slow
   pattern answers 400 instead of hanging the control plane.
6. **Patterns run on the `regex` package, not `re`.** Verified while planning:
   `re` holds the GIL during a match, so a catastrophic custom regex in a worker
   thread still froze the event loop (1 tick in 10 s, and the 2 s timer fired
   only after the regex ended). `regex` with `concurrent=True, timeout=…` stopped
   at its deadline with the loop ticking. The whole filter shares one 2 s
   deadline; `anyio.fail_after` stays as a backstop. `regex` also defuses the
   textbook `(a+)+$`, so tests use `(\w|\w\w)+$` on 60 `a`s and a `!`, which
   is genuinely slow (over 3 s).
5. **Manual tests 1 and 7:** `ship_to` is a built-in ADDRESS key, so test 1
   expects `[ADDRESS]`, and test 7 ticks `payment_method` instead of `ship_to`.

## Review Focus

- A Luhn-failing 16-digit reference or an ISO timestamp in a payload: must stay unmasked (no PHONE or CARD false positive).
- An upstream text result that is not JSON, or JSON that is not an object (`"2042"`, a list): plain-text scan or walk, never a crash that withholds every call.
- `null` values under a sensitive key or ticked path: stay `null` and are not counted.
- A protected gateway's error results (upstream echoes an email in its error): masked in the reply *and* in `audit_log.error`; with protection off, the audit `error` column comes from the masked copy.
- A tool name with no server (`x__y`, unknown tool): the filter still runs with `server_id=None` and never raises.

---

## File map

| File | Change |
|---|---|
| `services/gateway/app/pii.py` | **new**: rules, `redact_value`, `redact_result`, `schema_fields`, `covered_by`, `load_rules` |
| `services/gateway/tests/test_pii.py` | **new**: unit tests (no DB) |
| `services/gateway/app/db.py` | schema: `gateways.pii_protection`, `pii_rules`, audit columns |
| `services/gateway/app/upstream.py` | filter in `call_tool`, schema strip in `list_tools`, audit columns |
| `services/gateway/app/pii_api.py` | **new**: `/api/pii-rules*` |
| `services/gateway/app/gateways.py` | `pii_protection` on create and view; `PUT /api/gateways/{slug}/pii` |
| `services/gateway/app/api.py` | `/api/audit` without payload; `GET /api/audit/{id}` |
| `services/gateway/app/main.py` | mount `pii_api.routes` |
| `services/gateway/tests/test_pii_gateway.py` | **new**: end-to-end and API tests (Postgres) |
| `services/woo-mcp/app/server.py` | Pydantic result models |
| `services/woo-mcp/tests/tests_woo_mcp.py` | output schema test |
| `services/dashboard/src/Pii.jsx` | **new**: PII Protection page |
| `services/dashboard/src/App.jsx` | page entry and icon |
| `services/dashboard/src/Gateways.jsx` | switch in the create form and on each card |
| `services/dashboard/src/Admin.jsx` | audit PII chips and payload viewer |
| `charts/*/values.yaml`, `deploy/store-operator-values-local.yaml`, `services/store-operator/store_operator/handlers.py`, `README.md` | image tags: gateway and dashboard 0.5.0, woo-mcp 0.3.0, store-operator 0.3.1 |

---

### Task 1: The filter (`app/pii.py`) and spec amendments

**Files:**
- Create: `services/gateway/app/pii.py`
- Create: `services/gateway/tests/test_pii.py`
- Modify: `services/gateway/pyproject.toml`, `uv.lock` (via `uv add`)
- Modify: `docs/superpowers/specs/2026-10-11-pii-redaction-design.md` (amendments 1-6)

**Interfaces:**
- Produces:
  - `Rule(name, type, match, server_id=None, tool=None, check=None, note="")`, a frozen dataclass
  - `BUILTIN: list[Rule]`, `BUILTIN_NAMES: set[str]`, `NAME`, `FIELD_PATH`, `FILTER_TIMEOUT`, `MAX_SCAN`, `NON_TEXT`
  - `load_rules(conn) -> list[Rule]`: custom rules first, then built-ins
  - `redact_value(value, rules, server_id, tool, counts=None, deadline=None) -> tuple[Any, Counter]`; raises `TimeoutError` past the deadline (default: now + `FILTER_TIMEOUT`)
  - `redact_result(result: CallToolResult, rules, server_id, tool) -> tuple[CallToolResult, dict[str, int]]`; raises `TimeoutError` after `FILTER_TIMEOUT`
  - `schema_fields(output_schema: dict | None) -> list[dict]`, giving `[{path, type, description}]`
  - `key_map(rules) -> dict[str, str]` and `covered_by(path, keys) -> str | None`

- [ ] **Step 1: Write the failing tests** in `services/gateway/tests/test_pii.py`:

```python
"""The PII filter on its own: no database, no network."""

from __future__ import annotations

import json
import time

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from app.pii import BUILTIN, FILTER_TIMEOUT, MAX_SCAN, NON_TEXT, Rule, redact_result, redact_value, schema_fields

SHOP, GET = 3, "get_order"


def scan(value, rules=BUILTIN, server_id=SHOP, tool=GET):
    out, counts = redact_value(value, rules, server_id, tool)
    return out, dict(counts)


def test_builtin_patterns_inside_free_text():
    out, counts = scan({"msg": "mail a@b.co, call +1 415 555 0100, card 4111 1111 1111 1111, "
                               "ssn 123-45-6789, ip 10.0.0.12, order 2042, total 149.00"})
    assert out == {"msg": "mail [EMAIL], call [PHONE], card [CREDIT_CARD], ssn [SSN], "
                          "ip [IP_ADDRESS], order 2042, total 149.00"}
    assert counts == {"EMAIL": 1, "PHONE": 1, "CREDIT_CARD": 1, "SSN": 1, "IP_ADDRESS": 1}


def test_no_false_positives():
    for text in ("4111 1111 1111 1112", "2042", "149.00", "2026-10-11T05:43:02Z", "2026-10-11 05:43:02", "v1.2"):
        assert scan({"x": text}) == ({"x": text}, {}), text


def test_indian_mobile_is_a_phone():
    assert scan({"x": "+91 98765 43210"})[0] == {"x": "[PHONE]"}


def test_key_rules_at_any_depth_and_any_case():
    out, counts = scan({"Customer": "Alice", "order": {"billing": {"first_name": "A", "email": "a@b.co"}},
                        "customer_note": None, "id": 7})
    assert out == {"Customer": "[PERSON]", "order": {"billing": "[ADDRESS]"}, "customer_note": None, "id": 7}
    assert counts == {"PERSON": 1, "ADDRESS": 1}  # a null value is not counted


def test_custom_rules_run_before_builtins():
    aadhaar = Rule("AADHAAR", "pattern", r"\b\d{4}\s?\d{4}\s?\d{4}\b")
    loyalty = Rule("LOYALTY", "key", "loyalty_id")
    out, counts = scan({"name": "Gift card 2345 6789 0123", "loyalty_id": 99}, [aadhaar, loyalty, *BUILTIN])
    assert out == {"name": "Gift card [AADHAAR]", "loyalty_id": "[LOYALTY]"}
    assert counts == {"AADHAAR": 1, "LOYALTY": 1}


def test_field_rules_touch_only_their_path_and_tool():
    rules = [Rule("REDACTED", "field", "orders[].items[].name", SHOP, "list_orders"),
             Rule("PAY", "field", "orders[].payment", SHOP, "list_orders"), *BUILTIN]
    value = {"orders": [{"items": [{"name": "Mug"}, {"name": None}], "payment": "UPI"}], "name": "keep"}
    out, counts = scan(value, rules, SHOP, "list_orders")
    assert out == {"orders": [{"items": [{"name": "[REDACTED]"}, {"name": None}], "payment": "[PAY]"}], "name": "keep"}
    assert counts == {"REDACTED": 1, "PAY": 1}
    assert scan(value, rules, SHOP, "list_products")[0] == value  # another tool
    assert scan(value, rules, 99, "list_orders")[0] == value  # another server
    assert scan({"other": 1}, rules, SHOP, "list_orders")[0] == {"other": 1}  # path missing


def test_long_strings_are_cut():
    out, _ = scan({"x": "a" * (MAX_SCAN + 5)})
    assert out["x"] == "a" * MAX_SCAN + "[TRUNCATED]"


def test_result_structured_and_text_copies():
    data = {"id": 2042, "customer": "Alice", "note": "mail alice@x.io"}
    r = CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structured_content=data)
    out, counts = redact_result(r, BUILTIN, SHOP, GET)
    assert out.structured_content == {"id": 2042, "customer": "[PERSON]", "note": "mail [EMAIL]"}
    assert json.loads(out.content[0].text) == out.structured_content
    assert counts == {"PERSON": 1, "EMAIL": 1}  # the two copies count once
    assert r.structured_content["customer"] == "Alice"  # the input is not changed


def test_result_plain_text_and_non_text():
    r = CallToolResult(content=[TextContent(type="text", text="no order for bob@x.io"),
                                ImageContent(type="image", data="aGk=", mime_type="image/png")], is_error=True)
    out, counts = redact_result(r, BUILTIN, None, "")
    assert [c.text for c in out.content] == ["no order for [EMAIL]", NON_TEXT]
    assert out.is_error and counts == {"EMAIL": 1, "NON_TEXT": 1}


def test_json_scalars_in_text():
    r = CallToolResult(content=[TextContent(type="text", text="2042"), TextContent(type="text", text='"a@b.co"')])
    out, _ = redact_result(r, BUILTIN, None, "")
    assert [c.text for c in out.content] == ["2042", '"[EMAIL]"']


def test_schema_fields_follow_refs_arrays_and_nullables():
    schema = {
        "$defs": {"LineItem": {"type": "object", "properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}},
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "customer": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Buyer's name"},
            "items": {"type": "array", "items": {"$ref": "#/$defs/LineItem"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    assert schema_fields(schema) == [
        {"path": "id", "type": "integer", "description": None},
        {"path": "customer", "type": "string", "description": "Buyer's name"},
        {"path": "items[].name", "type": "string", "description": None},
        {"path": "tags[]", "type": "string", "description": None},
    ]
    assert schema_fields(None) == []
    assert schema_fields({"type": "object", "additionalProperties": True}) == []


def test_a_slow_custom_pattern_times_out():
    slow = Rule("SLOW", "pattern", r"(\w|\w\w)+$")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        redact_value({"x": "a" * 60 + "!"}, [slow], None, None)
    assert time.monotonic() - started < FILTER_TIMEOUT + 0.5


def test_builtins_are_fast_on_hostile_input():
    started = time.monotonic()
    scan({"x": ("1-" * 20_000) + "a@" * 20_000})
    assert time.monotonic() - started < 1.0
```

- [ ] **Step 1b: Add the dependency**

Run: `cd services/gateway && uv add 'regex>=2024.11.6'`
Expected: `pyproject.toml` lists `regex` and `uv.lock` is updated. Add a comment line above it in `dependencies`: `# Patterns with a timeout that release the GIL: a slow custom regex fails one call, not the pod.`

- [ ] **Step 2: Run, expect an import failure**

Run: `cd services/gateway && uv run pytest tests/test_pii.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.pii'`

- [ ] **Step 3: Write `services/gateway/app/pii.py`**

```python
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
    # E.164 numbers have at most 15 digits; "2026-10-11 05" is a date, not a phone.
    return 10 <= sum(c.isdigit() for c in text) <= 15 and not re.match(r"\d{4}-\d{2}-\d{2}", text)


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

# Order matters: a card or an IP address must not be taken for a phone number.
BUILTIN = [
    Rule("EMAIL", "pattern", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
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
            if check and not check(m.group()):
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
```

- [ ] **Step 4: Run the tests, expect a pass**

Run: `cd services/gateway && uv run pytest tests/test_pii.py -q`
Expected: all pass. If `test_no_false_positives` fails on one input, fix the regex or its check, not the test.

- [ ] **Step 5: Write amendments 1-5 (from "Spec amendments" above) into the spec**

Make these edits in `docs/superpowers/specs/2026-10-11-pii-redaction-design.md`:
- **§0 and §4:** patterns run on the `regex` package with one shared 2 s deadline (amendment 6). Replace the "Python's `re` cannot be interrupted … stuck thread keeps burning CPU" shortcut bullets with: "`regex` releases the GIL and stops at the deadline, so the thread ends too; `fail_after` is a backstop".
- **§10 test 9:** the SLOW match becomes `(\w|\w\w)+$` and the product name becomes 60 `a`s and a `!`.
- **§2 "Order of application":** custom patterns run first, then built-ins.
- **§2 PHONE row:** "10-15 digits, not text starting like a date".
- **§4:** add a bullet. A protected gateway lists tools without `output_schema`, because MCP clients check `structuredContent` against it.
- **§6 `/test` row:** it runs under the same thread and time limit; a timeout answers 400.
- **§10 test 1:** `ship_to` is `[ADDRESS]`.
- **§10 test 7:** tick `payment_method` (placeholder `REDACTED`) instead of `ship_to`. The expected result is `payment_method: "[REDACTED]"`.

- [ ] **Step 6: Checkpoint.** `uv run pytest tests/test_pii.py -q` is green. Leave the files uncommitted.

---

### Task 2: Schema, `call_tool` integration, and the gateway end-to-end test

**Files:**
- Modify: `services/gateway/app/db.py` (SCHEMA, at the end of the string)
- Modify: `services/gateway/app/upstream.py`
- Modify: `services/gateway/app/gateways.py` (`pii_protection` on create and view, `PUT …/pii`)
- Create: `services/gateway/tests/test_pii_gateway.py`

**Interfaces:**
- Consumes: `load_rules`, `redact_result`, `FILTER_TIMEOUT` from Task 1.
- Produces:
  - `audit_log` columns `pii TEXT`, `payload TEXT`, `payload_masked_for_log BOOLEAN`
  - `gateways.pii_protection BOOLEAN`
  - the `pii_rules` table
  - `POST /api/gateways` accepts `pii_protection: bool`; `PUT /api/gateways/{slug}/pii {enabled: bool}` returns `{ok, pii_protection}`
  - `GET /api/gateways` items gain `pii_protection`
  - `Upstreams._protected(slug) -> bool`
  - a module-level name `redact_result` in `app.upstream`, which tests monkeypatch

- [ ] **Step 1: Write the failing end-to-end test** `services/gateway/tests/test_pii_gateway.py`:

```python
"""PII protection end to end: MCP client -> edge -> a shop upstream with real PII."""

from __future__ import annotations

import asyncio
import json
import time

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

import app.upstream as upstream_mod
from app.main import Settings, create_app
from tests import KEY
from tests.test_gateway import (
    SETUP, FakeGateways, add_gateway, edge_settings, gateway_client, http_app, serve, setup_admin, text, wait_tools,
)

EMAIL = "alice.backup@gmail.com"


class Item(BaseModel):
    product_id: int
    name: str | None = None


class Order(BaseModel):
    id: int
    total: str
    customer: str | None = None
    customer_note: str | None = None
    ship_to: str | None = None
    payment_method: str | None = None
    items: list[Item]


ORDER = Order(id=2042, total="149.00", customer="Alice Example", customer_note=f"mail {EMAIL} or +91 98765 43210",
              ship_to="Pune, MH, IN", payment_method="UPI", items=[Item(product_id=7, name="Mug")])


def build_shop() -> MCPServer:
    server = MCPServer("shop")

    @server.tool()
    async def get_order() -> Order:
        """One order, typed, so the tool advertises an output schema."""
        return ORDER

    @server.tool()
    async def fail() -> str:
        """An upstream error that echoes PII."""
        raise ToolError(f"no order for {EMAIL}")

    @server.tool()
    async def slow_text() -> str:
        """Text that a slow custom pattern takes seconds on."""
        return "a" * 60 + "!"

    return server


async def shop_setup(gw: str, shop: str):
    admin = await setup_admin(gw)
    sid = (await admin.post("/api/servers", json={"name": "shop", "url": f"{shop}/mcp"})).json()["id"]
    r = await admin.post("/api/gateways", json={"slug": "support", "name": "Support", "pii_protection": True})
    assert r.status_code == 201 and r.json()["pii_protection"] is True, r.text
    await admin.put("/api/gateways/support/servers", json={"server_ids": [sid]})
    await add_gateway(admin, "ops", [sid])
    key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
    return admin, sid, key


def control(db_url):
    return create_app(Settings(database_url=db_url, credentials_key=KEY, setup_token=SETUP, refresh_interval_s=0.5),
                      gateways=FakeGateways())


def test_protection_masks_per_gateway_and_audits_the_payload(db_url):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            with serve(create_app(edge_settings(db_url, "support"))) as support, \
                 serve(create_app(edge_settings(db_url, "ops"))) as ops:
                await wait_tools(support, key, 3)
                await wait_tools(ops, key, 3)

                async with gateway_client(support, key) as mcp:
                    listed = {t.name: t for t in (await mcp.list_tools()).tools}
                    assert listed["shop__get_order"].output_schema is None  # amendment 1
                    r = await mcp.call_tool("shop__get_order", {})
                    assert r.structured_content == {
                        "id": 2042, "total": "149.00", "customer": "[PERSON]", "customer_note": "[FREE_TEXT]",
                        "ship_to": "[ADDRESS]", "payment_method": "UPI", "items": [{"product_id": 7, "name": "Mug"}]}
                    assert "Alice" not in text(r) and EMAIL not in text(r)
                    e = await mcp.call_tool("shop__fail", {})
                    assert e.is_error and "no order for [EMAIL]" in text(e)

                async with gateway_client(ops, key) as mcp:
                    listed = {t.name: t for t in (await mcp.list_tools()).tools}
                    assert listed["shop__get_order"].output_schema is not None
                    r = await mcp.call_tool("shop__get_order", {})
                    assert r.structured_content["customer"] == "Alice Example"
                    e = await mcp.call_tool("shop__fail", {})
                    assert EMAIL in text(e)  # protection off: the member sees it

                log = (await admin.get("/api/audit")).json()
                assert all("payload" not in row for row in log)
                by = {(row["gateway"], row["tool"]): row for row in reversed(log)}
                on = by[("support", "shop__get_order")]
                assert json.loads(on["pii"]) == {"PERSON": 1, "FREE_TEXT": 1, "ADDRESS": 1}
                full = (await admin.get(f"/api/audit/{on['id']}")).json()
                assert full["payload_masked_for_log"] is False
                assert json.loads(full["payload"])["structuredContent"]["customer"] == "[PERSON]"
                off = (await admin.get(f"/api/audit/{by[('ops', 'shop__get_order')]['id']}")).json()
                assert off["payload_masked_for_log"] is True and "Alice" not in off["payload"]
                for row in log:  # no raw PII anywhere, error column included
                    full = (await admin.get(f"/api/audit/{row['id']}")).json()
                    assert EMAIL not in json.dumps(full), full

                # The switch applies on the next call, no restart.
                r = await admin.put("/api/gateways/ops/pii", json={"enabled": True})
                assert r.status_code == 200 and r.json()["pii_protection"] is True
                async with gateway_client(ops, key) as mcp:
                    assert (await mcp.call_tool("shop__get_order", {})).structured_content["customer"] == "[PERSON]"
                assert (await admin.put("/api/gateways/ops/pii", json={"enabled": "yes"})).status_code == 400
                assert (await admin.put("/api/gateways/nope/pii", json={"enabled": True})).status_code == 404

    asyncio.run(run())


def test_a_filter_failure_fails_only_that_call(db_url, monkeypatch):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            with serve(create_app(edge_settings(db_url, "support"))) as support:
                await wait_tools(support, key, 3)

                def boom(*a):
                    raise RuntimeError("bug")

                monkeypatch.setattr(upstream_mod, "redact_result", boom)
                async with gateway_client(support, key) as mcp:
                    r = await mcp.call_tool("shop__get_order", {})
                assert r.is_error and text(r) == "Result withheld: the PII filter failed. An admin can check the gateway log."

                monkeypatch.undo()
                # A real catastrophic custom pattern, not a sleep: proves the regex
                # engine itself lets go of the event loop and stops at the deadline.
                r = await admin.post("/api/pii-rules", json={"name": "SLOW", "type": "pattern", "match": r"(\w|\w\w)+$"})
                assert r.status_code == 201, r.text

                async def health_while_stuck():
                    await asyncio.sleep(0.5)
                    started = time.monotonic()
                    async with httpx2.AsyncClient(base_url=support) as c:
                        assert (await c.get("/healthz")).status_code == 200
                    return time.monotonic() - started

                async def stuck_call():
                    async with gateway_client(support, key) as mcp:
                        return await mcp.call_tool("shop__slow_text", {})

                started = time.monotonic()
                r, health_s = await asyncio.gather(stuck_call(), health_while_stuck())
                assert r.is_error and "took too long" in text(r)
                assert time.monotonic() - started < 2.9  # withheld at the 2 s deadline, not after the regex
                assert health_s < 0.5  # the event loop stayed free

                log = (await admin.get("/api/audit")).json()
                assert [json.loads(row["pii"]) for row in log[:2]] == [{"FILTER_TIMEOUT": 1}, {"FILTER_ERROR": 1}]

    asyncio.run(run())
```

- [ ] **Step 2: Run it, expect failures**

Run: `cd services/gateway && TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest tests/test_pii_gateway.py -q`
Expected: FAIL (`pii_protection` missing from the create response; 404 on `/api/audit/{id}`).
If the test Postgres is not running: `docker start gw-test-pg || docker run -d --name gw-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=t postgres:17-alpine`.

- [ ] **Step 3: Schema**: append to `SCHEMA` in `app/db.py`, before the closing `"""`:

```sql
-- PII protection (pii.py): a switch per gateway, the org's custom rules, and the
-- audit copy of what the caller received.
ALTER TABLE gateways ADD COLUMN IF NOT EXISTS pii_protection BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS pii_rules (
    id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name       TEXT NOT NULL,  -- also the placeholder: [NAME]
    type       TEXT NOT NULL CHECK (type IN ('pattern', 'key', 'field')),
    match      TEXT NOT NULL,  -- regex, JSON key, or field path like orders[].customer
    server_id  BIGINT REFERENCES servers(id) ON DELETE CASCADE,  -- field rules only
    tool       TEXT,                                             -- field rules only
    created_at DOUBLE PRECISION NOT NULL,
    CHECK ((type = 'field') = (server_id IS NOT NULL AND tool IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS pii_rules_field ON pii_rules (server_id, tool, match) WHERE type = 'field';
CREATE UNIQUE INDEX IF NOT EXISTS pii_rules_name ON pii_rules (name) WHERE type <> 'field';
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS pii TEXT;      -- JSON counts per rule, never values
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS payload TEXT;  -- the result as returned, filtered
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS payload_masked_for_log BOOLEAN NOT NULL DEFAULT false;
```

- [ ] **Step 4: `app/upstream.py`.** Add imports, constants, helpers, and the changes to `list_tools` and `call_tool`.

Imports: add `import json` and `from .pii import FILTER_TIMEOUT, load_rules, redact_result`.

Below `error_result`:

```python
WITHHELD = {
    "FILTER_ERROR": "Result withheld: the PII filter failed. An admin can check the gateway log.",
    "FILTER_TIMEOUT": "Result withheld: the PII filter took too long. An admin can check the PII rules.",
}
PAYLOAD_CAP = 65_536  # characters of a result kept in the audit log


def _payload(result: CallToolResult | None) -> str | None:
    if result is None:
        return None
    s = result.model_dump_json(by_alias=True, exclude_none=True)
    return s if len(s) <= PAYLOAD_CAP else s[:PAYLOAD_CAP] + f"…[truncated, {len(s)} bytes]"
```

Methods on `Upstreams` (next to `_over_limit`):

```python
    def _protected(self, gateway_slug: str) -> bool:
        row = self.db.execute("SELECT pii_protection FROM gateways WHERE slug = ?", (gateway_slug,)).fetchone()
        return bool(row and row["pii_protection"])

    async def _redact(self, result: CallToolResult, server_id: int | None, tool: str, name: str):
        """The filtered result and its counts, or None and why. Only this call fails."""
        rules = load_rules(self.db)
        try:
            with anyio.fail_after(FILTER_TIMEOUT):
                # A worker thread, so the event loop keeps serving other calls. The
                # filter stops itself at the same deadline (regex timeout); this limit
                # is the backstop, and abandon_on_cancel lets it fire without waiting.
                return await anyio.to_thread.run_sync(
                    redact_result, result, rules, server_id, tool, abandon_on_cancel=True)
        except TimeoutError:
            log.error("PII filter timed out on %s", name)
            return None, {"FILTER_TIMEOUT": 1}
        except Exception:
            log.exception("PII filter failed on %s", name)
            return None, {"FILTER_ERROR": 1}
```

In `list_tools`, read `protected = self._protected(gateway_slug)` once before the loop. Then extend the `model_copy` update with:

```python
                            # MCP clients check structuredContent against it; masked values may not fit.
                            "output_schema": None if protected else tool.output_schema,
```

In `call_tool`, replace the block from `error = None` through the `INSERT` with:

```python
        filtered, pii = await self._redact(result, row and row["id"], tool, name)
        if self._protected(gateway_slug):
            result = filtered or error_result(WITHHELD[next(iter(pii))])
            logged, masked_for_log = result, False
        else:  # the caller gets the raw result; the log still keeps only a masked copy
            logged, masked_for_log = filtered, True

        error = None
        if result.is_error:
            error = (" ".join(c.text for c in logged.content if isinstance(c, TextContent))[:500]
                     if logged else "error text not kept: the PII filter failed")
        self.db.execute(
            "INSERT INTO audit_log (at, user_id, username, tool, server, gateway, ok, duration_ms, error, identity,"
            " pii, payload, payload_masked_for_log) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                user["id"],
                user["username"],
                name,
                row["name"] if row else None,
                gateway_slug,
                0 if result.is_error else 1,
                int((time.monotonic() - started) * 1000),
                error,
                identity,
                json.dumps(pii) if pii else None,
                _payload(logged),
                masked_for_log,
            ),
        )
        return result
```

Also update the `call_tool` docstring to: `"""The one path for every tool call: route, check the gateway's policy, call with a timeout, filter PII, audit."""`

- [ ] **Step 5: `app/gateways.py`**

In `_view`, add `"pii_protection": bool(row.get("pii_protection")),` after `"public_url"`. (`_orphan` rows have no such key; `.get` gives False.)

In `create_gateway`, after `public_url = …`:

```python
    pii = data.get("pii_protection", False)
    if not isinstance(pii, bool):
        raise HTTPError(400, "pii_protection must be true or false")
```

and change the INSERT to:

```python
                "INSERT INTO gateways (slug, name, public_url, pii_protection, created_at) VALUES (?, ?, ?, ?, ?) RETURNING *",
                (slug, name, public_url, pii, time.time()),
```

New handler and route:

```python
@handler
async def set_pii(request: Request) -> Response:
    """PII protection on or off. Edges read it on every call, so it applies at once."""
    require_admin(request)
    row = _row(request)
    enabled = (await body(request)).get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPError(400, "enabled must be true or false")
    request.app.state.db.execute("UPDATE gateways SET pii_protection = ? WHERE id = ?", (enabled, row["id"]))
    return JSONResponse({"ok": True, "pii_protection": enabled})
```

`Route("/api/gateways/{slug}/pii", set_pii, methods=["PUT"]),`

- [ ] **Step 6: `app/api.py`: the audit list without payload, and one row with it**

```python
AUDIT_LIST = ("id, at, user_id, username, tool, server, gateway, ok, duration_ms, error, identity,"
              " pii, payload_masked_for_log")


@handler
async def audit(request: Request) -> Response:
    require_admin(request)
    try:
        limit = min(int(request.query_params.get("limit", 100)), 1000)
    except ValueError:
        raise HTTPError(400, "limit must be a number") from None
    # Without payload: up to 64 KB each, fetched one row at a time by audit_entry.
    rows = request.app.state.db.execute(
        f"SELECT {AUDIT_LIST} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return JSONResponse([dict(r) for r in rows])


@handler
async def audit_entry(request: Request) -> Response:
    require_admin(request)
    row = request.app.state.db.execute(
        "SELECT * FROM audit_log WHERE id = ?", (request.path_params["id"],)
    ).fetchone()
    if row is None:
        raise HTTPError(404, "no such audit entry")
    return JSONResponse(dict(row))
```

`Route("/api/audit/{id:int}", audit_entry, methods=["GET"]),`

- [ ] **Step 7: Run the new test and the whole suite**

Run: `cd services/gateway && TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:5433/postgres uv run pytest -q`
Expected: all pass, including the existing audit assertions in `test_edge.py` and `test_gateway.py`, which read only the listed columns.

- [ ] **Step 8: Checkpoint.** Leave the files uncommitted.

---

### Task 3: The PII Protection API (`app/pii_api.py`)

**Files:**
- Create: `services/gateway/app/pii_api.py`
- Modify: `services/gateway/app/main.py` (import, and add `*pii_api.routes` to the control routes)
- Modify: `services/gateway/tests/test_pii_gateway.py` (append a test)

**Interfaces:**
- Consumes: `pii.BUILTIN, BUILTIN_NAMES, NAME, FIELD_PATH, FILTER_TIMEOUT, load_rules, key_map, covered_by, schema_fields, redact_value`; `upstream.Status`; `request.app.state.upstreams.status`.
- Produces (used by the dashboard in Task 5):
  - `GET /api/pii-rules` returns `{builtin: [{name, type, match, note}], custom: [{id, name, type, match, server_id, tool}]}`
  - `POST /api/pii-rules` takes `{name, type, match, server_id?, tool?}` and returns 201 with the rule
  - `DELETE /api/pii-rules/{id}` returns `{ok}`
  - `GET /api/pii-rules/fields` returns `[{server_id, server, tools: [{tool, has_schema, fields: [{path, type, description, rule: {id, name} | null, covered_by}], stale: [{path, id, name}]}]}]`
  - `POST /api/pii-rules/test` takes `{payload, server_id?, tool?}` and returns `{payload, counts}`

- [ ] **Step 1: Append the failing API test** to `tests/test_pii_gateway.py`:

```python
def test_pii_rules_api(db_url):
    async def run():
        with serve(http_app(build_shop())) as shop, serve(control(db_url)) as gw:
            admin, sid, key = await shop_setup(gw, shop)
            await admin.post("/api/users", json={"username": "rahul", "password": "rahul-password-123"})
            rahul = httpx2.AsyncClient(base_url=gw, timeout=10)
            await rahul.post("/api/login", json={"username": "rahul", "password": "rahul-password-123"})
            assert (await rahul.get("/api/pii-rules")).status_code == 403
            assert (await rahul.post("/api/pii-rules", json={"name": "X1", "type": "key", "match": "x"})).status_code == 403

            new = lambda **b: admin.post("/api/pii-rules", json=b)
            assert (await new(name="AADHAAR", type="pattern", match=r"\b\d{4}\s?\d{4}\s?\d{4}\b")).status_code == 201
            assert (await new(name="AADHAAR", type="key", match="aadhaar")).status_code == 409
            r = await new(name="BAD", type="pattern", match="(")
            assert r.status_code == 400 and "regex" in r.json()["error"]
            assert (await new(name="LONG", type="pattern", match="a" * 201)).status_code == 400
            assert (await new(name="EMAIL", type="key", match="mail")).status_code == 400  # a built-in's name
            assert (await new(name="lower", type="key", match="x")).status_code == 400
            assert (await new(name="X1", type="nope", match="x")).status_code == 400
            assert (await new(name="PAY", type="field", match="payment_method")).status_code == 400  # no server/tool
            assert (await new(name="PAY", type="field", match="bad path!", server_id=sid, tool="get_order")).status_code == 400
            assert (await new(name="PAY", type="field", match="x", server_id=999, tool="get_order")).status_code == 400
            pay = await new(name="REDACTED", type="field", match="payment_method", server_id=sid, tool="get_order")
            assert pay.status_code == 201
            assert (await new(name="REDACTED", type="field", match="payment_method", server_id=sid, tool="get_order")).status_code == 409
            assert (await new(name="REDACTED", type="field", match="gone_field", server_id=sid, tool="get_order")).status_code == 201

            listed = (await admin.get("/api/pii-rules")).json()
            assert {r["name"] for r in listed["builtin"]} >= {"EMAIL", "PHONE", "PERSON", "ADDRESS", "FREE_TEXT"}
            assert [r["name"] for r in listed["custom"]] == ["AADHAAR", "REDACTED", "REDACTED"]

            for _ in range(50):  # the control plane's health loop has to see the shop first
                fields = (await admin.get("/api/pii-rules/fields")).json()
                if fields and fields[0]["tools"]:
                    break
                await asyncio.sleep(0.1)
            tools = {t["tool"]: t for t in fields[0]["tools"]}
            by_path = {f["path"]: f for f in tools["get_order"]["fields"]}
            assert by_path["customer"]["covered_by"] == "PERSON"
            assert by_path["payment_method"]["rule"]["name"] == "REDACTED"
            assert by_path["items[].name"] == {"path": "items[].name", "type": "string", "description": None,
                                               "rule": None, "covered_by": None}
            assert [s["path"] for s in tools["get_order"]["stale"]] == ["gone_field"]

            # The new rules apply on the next call through a protected gateway.
            with serve(create_app(edge_settings(db_url, "support"))) as support:
                await wait_tools(support, key, 3)
                async with gateway_client(support, key) as mcp:
                    r = await mcp.call_tool("shop__get_order", {})
                assert r.structured_content["payment_method"] == "[REDACTED]"

            t = await admin.post("/api/pii-rules/test", json={"payload": {"x": "id 2345 6789 0123", "payment_method": "UPI"},
                                                             "server_id": sid, "tool": "get_order"})
            assert t.json() == {"payload": {"x": "id [AADHAAR]", "payment_method": "[REDACTED]"},
                                "counts": {"AADHAAR": 1, "REDACTED": 1}}
            assert (await admin.post("/api/pii-rules/test", json={})).status_code == 400
            before = len((await admin.get("/api/audit")).json())
            await admin.post("/api/pii-rules/test", json={"payload": "a@b.co"})
            assert len((await admin.get("/api/audit")).json()) == before  # stores nothing

            slow = (await new(name="SLOW", type="pattern", match=r"(\w|\w\w)+$")).json()
            r = await admin.post("/api/pii-rules/test", json={"payload": {"x": "a" * 60 + "!"}})
            assert r.status_code == 400 and "too long" in r.json()["error"]
            assert (await admin.delete(f"/api/pii-rules/{slow['id']}")).status_code == 200
            assert (await admin.delete(f"/api/pii-rules/{slow['id']}")).status_code == 404

            await admin.delete(f"/api/servers/{sid}")  # field rules go with their server
            assert [r["name"] for r in (await admin.get("/api/pii-rules")).json()["custom"]] == ["AADHAAR"]

    asyncio.run(run())
```

- [ ] **Step 2: Run it, expect a failure**

Run: `TEST_DATABASE_URL=… uv run pytest tests/test_pii_gateway.py::test_pii_rules_api -q`
Expected: FAIL, 404 on `/api/pii-rules`.

- [ ] **Step 3: Write `app/pii_api.py`**

```python
"""The PII Protection tab: the rule library, each tool's fields, and a test box. Admins only."""

from __future__ import annotations

import time

import anyio
import regex
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import pii
from .api import HTTPError, _str, body, handler, require_admin
from .upstream import Status


def _view(r: dict) -> dict:
    return {k: r[k] for k in ("id", "name", "type", "match", "server_id", "tool")}


@handler
async def list_rules(request: Request) -> Response:
    require_admin(request)
    rows = request.app.state.db.execute("SELECT * FROM pii_rules ORDER BY id").fetchall()
    return JSONResponse({
        "builtin": [{"name": r.name, "type": r.type, "match": r.match, "note": r.note} for r in pii.BUILTIN],
        "custom": [_view(r) for r in rows],
    })


@handler
async def create_rule(request: Request) -> Response:
    require_admin(request)
    conn = request.app.state.db
    data = await body(request)
    name, kind, match = _str(data, "name"), _str(data, "type"), _str(data, "match")
    if not pii.NAME.match(name):
        raise HTTPError(400, "name: 2-40 of A-Z 0-9 and _, starting with a letter")
    server_id = tool = None
    if kind == "pattern":
        if len(match) > 200:
            raise HTTPError(400, "match: a regex of at most 200 characters")
        try:
            regex.compile(match)  # the engine the filter uses
        except regex.error as e:
            raise HTTPError(400, f"match is not a valid regex: {e}") from None
    elif kind == "key":
        if len(match) > 64:
            raise HTTPError(400, "match: a key of at most 64 characters")
    elif kind == "field":
        server_id, tool = data.get("server_id"), _str(data, "tool")
        if not isinstance(server_id, int) or isinstance(server_id, bool) or len(tool) > 128:
            raise HTTPError(400, "a field rule needs server_id and tool")
        if not pii.FIELD_PATH.match(match):
            raise HTTPError(400, "match must be a field path like orders[].customer")
        if conn.execute("SELECT 1 FROM servers WHERE id = ?", (server_id,)).fetchone() is None:
            raise HTTPError(400, "no such server")
    else:
        raise HTTPError(400, "type must be pattern, key or field")
    if kind != "field" and name in pii.BUILTIN_NAMES:
        raise HTTPError(400, f"{name} is a built-in rule; pick another name")
    row = conn.execute(  # a duplicate is a 409 from @handler
        "INSERT INTO pii_rules (name, type, match, server_id, tool, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (name, kind, match, server_id, tool, time.time()),
    ).fetchone()
    return JSONResponse(_view(row), status_code=201)


@handler
async def delete_rule(request: Request) -> Response:
    require_admin(request)
    gone = request.app.state.db.execute(
        "DELETE FROM pii_rules WHERE id = ? RETURNING id", (request.path_params["id"],)
    ).fetchone()
    if gone is None:
        raise HTTPError(404, "no such rule")
    return JSONResponse({"ok": True})


@handler
async def fields(request: Request) -> Response:
    """Every field each enabled server's tools return, from the output schemas the
    health loop already cached; never calls an upstream."""
    require_admin(request)
    conn = request.app.state.db
    keys = pii.key_map(pii.load_rules(conn))
    status = request.app.state.upstreams.status
    out = []
    for s in conn.execute("SELECT id, name FROM servers WHERE enabled = 1 ORDER BY name").fetchall():
        ticked: dict[str, dict[str, dict]] = {}
        for r in conn.execute(
            "SELECT id, name, tool, match FROM pii_rules WHERE type = 'field' AND server_id = ?", (s["id"],)
        ):
            ticked.setdefault(r["tool"], {})[r["match"]] = {"id": r["id"], "name": r["name"]}
        tools = []
        for t in sorted(status.get(s["id"], Status()).tools, key=lambda t: t.name):
            found = pii.schema_fields(t.output_schema)
            mine = ticked.get(t.name, {})
            paths = {f["path"] for f in found}
            tools.append({
                "tool": t.name,
                "has_schema": bool(found),
                "fields": [{**f, "rule": mine.get(f["path"]), "covered_by": pii.covered_by(f["path"], keys)}
                           for f in found],
                "stale": [{"path": p, **r} for p, r in mine.items() if p not in paths],
            })
        out.append({"server_id": s["id"], "server": s["name"], "tools": tools})
    return JSONResponse(out)


@handler
async def test_rules(request: Request) -> Response:
    """Run the rules over a pasted payload. Nothing is stored or audited."""
    require_admin(request)
    data = await body(request)
    if "payload" not in data:
        raise HTTPError(400, "'payload' is required")
    rules = pii.load_rules(request.app.state.db)
    try:
        with anyio.fail_after(pii.FILTER_TIMEOUT):
            value, counts = await anyio.to_thread.run_sync(
                pii.redact_value, data["payload"], rules, data.get("server_id"), data.get("tool"),
                abandon_on_cancel=True)
    except TimeoutError:
        raise HTTPError(400, "the PII filter took too long on this payload: check the custom patterns") from None
    return JSONResponse({"payload": value, "counts": dict(counts)})


routes = [
    Route("/api/pii-rules", list_rules, methods=["GET"]),
    Route("/api/pii-rules", create_rule, methods=["POST"]),
    Route("/api/pii-rules/fields", fields, methods=["GET"]),
    Route("/api/pii-rules/test", test_rules, methods=["POST"]),
    Route("/api/pii-rules/{id:int}", delete_rule, methods=["DELETE"]),
]
```

- [ ] **Step 4: Mount the routes** in `app/main.py`:
  - Add `pii_api` to the `from . import …` line.
  - Add `*pii_api.routes,` after `*credentials.routes,`.

- [ ] **Step 5: Run the whole gateway suite**

Run: `TEST_DATABASE_URL=… uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Checkpoint.**

---

### Task 4: woo-mcp advertises output schemas

**Files:**
- Modify: `services/woo-mcp/app/server.py` (`_product`, `_order`, the tool return types)
- Modify: `services/woo-mcp/tests/tests_woo_mcp.py` (one new test)

**Interfaces:**
- Produces: tools whose `outputSchema` lists the fields `Product`, `OrderSummary`, `Order`, `ProductList` and `OrderList`, with the same JSON values as before.

- [ ] **Step 1: Read the existing tests.** Look at how `tests_woo_mcp.py` builds a client against the app (its `serve` helper and the fake store), then add this test using the same helpers. The test body's client setup copies the existing `get_order` test in that file:

```python
def test_tools_advertise_output_schemas():
    async def run():
        async with _client() as mcp:  # use the file's existing client helper; rename to match it
            tools = {t.name: t.output_schema for t in (await mcp.list_tools()).tools}
        props = lambda name: set(tools[name]["properties"])
        assert props("get_order") == {"id", "number", "status", "total", "currency", "created_utc", "customer",
                                      "items", "payment_method", "customer_note", "ship_to"}
        assert props("list_orders") == {"count", "orders"}
        assert props("list_products") == {"count", "products"}
        assert props("create_product") == props("delete_product") == {
            "id", "name", "price", "status", "stock_status", "sku", "url"}
        assert props("update_order_status") == props("get_order") - {"payment_method", "customer_note", "ship_to"}
    asyncio.run(run())
```

- [ ] **Step 2: Run it, expect a failure**

Run: `cd services/woo-mcp && uv run pytest tests/tests_woo_mcp.py -q -k output_schemas` (if pytest does not collect the `tests_` file, run `uv run python -m pytest tests/tests_woo_mcp.py -q -k output_schemas`)
Expected: FAIL, KeyError `'properties'` (the schema is a bare object).

- [ ] **Step 3: Models.** In `app/server.py`, add `from pydantic import BaseModel, Field` (replacing the `Field`-only import), and add above `_product`:

```python
class Product(BaseModel):
    id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name")
    price: str | None = Field(None, description="Current price in the store currency")
    status: str | None = Field(None, description="publish, draft, trash, ...")
    stock_status: str | None = Field(None, description="instock, outofstock or onbackorder")
    sku: str | None = Field(None, description="Stock code")
    url: str | None = Field(None, description="Storefront link")


class LineItem(BaseModel):
    product_id: int | None = Field(None, description="Product id")
    name: str | None = Field(None, description="Product name at the time of the order")
    quantity: int | None = Field(None, description="How many")
    total: str | None = Field(None, description="Line total in the order currency")


class OrderSummary(BaseModel):
    id: int | None = Field(None, description="Order id")
    number: str | None = Field(None, description="Order number shown to the customer")
    status: str | None = Field(None, description="Order status")
    total: str | None = Field(None, description="Order total")
    currency: str | None = Field(None, description="Currency code")
    created_utc: str | None = Field(None, description="When it was placed, UTC")
    customer: str | None = Field(None, description="Billing first and last name")
    items: list[LineItem] = Field(default_factory=list, description="Line items")


class Order(OrderSummary):
    payment_method: str | None = Field(None, description="Payment method title")
    customer_note: str | None = Field(None, description="Note the customer wrote at checkout")
    ship_to: str | None = Field(None, description="City, state and country shipped to")


class ProductList(BaseModel):
    count: int
    products: list[Product]


class OrderList(BaseModel):
    count: int
    orders: list[OrderSummary]
```

Make `_product` return `Product(**{…same dict…})`. Make `_order` build the same dict and return `Order(**out) if full else OrderSummary(**out)`. The return annotations become `-> Product` and `-> OrderSummary | Order`.

Change the tool return types and returns:
- `list_products`: `-> ProductList`, `return ProductList(count=len(rows), products=[_product(p) for p in rows])`
- `create_product`, `delete_product`: `-> Product`
- `list_orders`: `-> OrderList`, `return OrderList(count=len(rows), orders=[_order(o) for o in rows])`
- `get_order`: `-> Order`
- `update_order_status`: `-> OrderSummary`

Woo returns `number` as a string and `id` as an int. If an existing test asserts on a field whose type differs, keep the model loose (`str | int | None`) rather than changing the values.

- [ ] **Step 4: Run the whole woo-mcp suite**

Run: `cd services/woo-mcp && uv run pytest -q tests/tests_woo_mcp.py` (or `uv run python -m tests.tests_woo_mcp`)
Expected: all pass, the old tests unchanged.

- [ ] **Step 5: Checkpoint.**

---

### Task 5: Dashboard

**Files:**
- Create: `services/dashboard/src/Pii.jsx`
- Modify: `services/dashboard/src/App.jsx`, `Gateways.jsx`, `Admin.jsx`

**Interfaces:**
- Consumes: the endpoints from Tasks 2 and 3.

- [ ] **Step 1: `App.jsx`**:
  - `import Pii from './Pii.jsx'`.
  - `ICONS.shield = 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'`.
  - Add `{ id: 'pii', label: 'PII Protection', el: Pii, admin: true, group: 'Settings' },` to `PAGES` before `audit`.
  - Check how the sidebar picks icons (`<Icon name={p.id} />` or a map) and make `pii` resolve to `shield`, e.g. `ICONS.pii = ICONS.shield`.

- [ ] **Step 2: `Gateways.jsx`**:
  - `EMPTY` gains `pii_protection: false`.
  - The create form gets a checkbox: `<label className="row small"><input type="checkbox" checked={form.pii_protection} onChange={(e) => setForm({ ...form, pii_protection: e.target.checked })} /> PII protection</label>`.
  - The card title gets `{g.pii_protection && <span className="pill ok">PII protected</span>}`.
  - Admins also get a toggle in the card's row, next to the Servers button: `<label className="row small"><input type="checkbox" checked={g.pii_protection} onChange={(e) => act(() => api(`/gateways/${g.slug}/pii`, { method: 'PUT', body: { enabled: e.target.checked } }))} /> PII protection</label>`.

- [ ] **Step 3: `Pii.jsx`**

```jsx
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

const PLACEHOLDERS = ['REDACTED', 'PERSON', 'ADDRESS', 'FREE_TEXT', 'EMAIL', 'PHONE']
const EMPTY = { name: '', type: 'pattern', match: '' }

// The global redaction schema, the fields each tool returns, and a test box.
export default function Pii() {
  const [rules, setRules] = useState({ builtin: [], custom: [] })
  const [servers, setServers] = useState([])
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const [open, setOpen] = useState({}) // "server/tool" -> expanded
  const [pick, setPick] = useState({}) // "server/tool/path" -> placeholder for a new tick
  const [test, setTest] = useState({ target: '', payload: '{\n  "customer": "Alice",\n  "note": "mail a@b.co"\n}' })
  const [tested, setTested] = useState(null)

  const load = useCallback(() => {
    api('/pii-rules').then(setRules).catch((e) => setError(e.message))
    api('/pii-rules/fields').then(setServers).catch((e) => setError(e.message))
  }, [])
  useEffect(load, [load])

  async function act(fn) {
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const add = (e) => {
    e.preventDefault()
    act(async () => {
      await api('/pii-rules', { method: 'POST', body: form })
      setForm(EMPTY)
    })
  }
  const remove = (id) => act(() => api(`/pii-rules/${id}`, { method: 'DELETE' }))
  const tick = (s, t, f, on) =>
    act(() =>
      on
        ? api('/pii-rules', { method: 'POST', body: { name: pick[`${s.server_id}/${t.tool}/${f.path}`] || 'REDACTED', type: 'field', match: f.path, server_id: s.server_id, tool: t.tool } })
        : api(`/pii-rules/${f.rule.id}`, { method: 'DELETE' }),
    )

  async function runTest() {
    setError('')
    setTested(null)
    let payload
    try {
      payload = JSON.parse(test.payload)
    } catch {
      return setError('Test payload is not valid JSON')
    }
    const [server_id, tool] = test.target ? test.target.split('/') : []
    try {
      setTested(await api('/pii-rules/test', { method: 'POST', body: { payload, server_id: server_id ? Number(server_id) : null, tool: tool || null } }))
    } catch (e) {
      setError(e.message)
    }
  }

  const global = [...rules.builtin.map((r) => ({ ...r, builtin: true })), ...rules.custom.filter((r) => r.type !== 'field')]

  return (
    <>
      <div className="toolbar">
        <h2>PII Protection</h2>
        <span className="muted small">Applies to every gateway with PII protection on. Changes apply on the next call.</span>
      </div>
      {error && <p className="error" role="alert">{error}</p>}

      <h3>Global redaction schema</h3>
      <table className="card">
        <thead>
          <tr><th>Name</th><th>Type</th><th>Match</th><th>Becomes</th><th /></tr>
        </thead>
        <tbody>
          {global.map((r, i) => (
            <tr key={r.id ?? `b${i}`}>
              <td>{r.name} {r.builtin && <span className="pill">built-in</span>}</td>
              <td>{r.type}</td>
              <td className="mono small">{r.match}{r.note && <span className="muted"> ({r.note})</span>}</td>
              <td className="mono">[{r.name}]</td>
              <td>{!r.builtin && <button className="ghost danger tiny" onClick={() => remove(r.id)}>Delete</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="card row" onSubmit={add}>
        <input aria-label="Rule name" placeholder="NAME, e.g. AADHAAR" required value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value.toUpperCase() })} />
        <select aria-label="Rule type" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
          <option value="pattern">pattern (regex on every value)</option>
          <option value="key">key (JSON key, any depth)</option>
        </select>
        <input className="grow mono" aria-label="Match" required value={form.match}
          placeholder={form.type === 'pattern' ? 'regex, e.g. \\b\\d{4}\\s?\\d{4}\\s?\\d{4}\\b' : 'key, e.g. loyalty_id'}
          onChange={(e) => setForm({ ...form, match: e.target.value })} />
        <button className="primary">Add rule</button>
      </form>

      <h3>Fields by server and tool</h3>
      {servers.length === 0 && <p className="muted">No enabled servers.</p>}
      {servers.map((s) => (
        <div key={s.server_id} className="card">
          <div className="title">{s.server}</div>
          {s.tools.length === 0 && <p className="muted small">No tools yet: the server has not answered a health check.</p>}
          {s.tools.map((t) => {
            const k = `${s.server_id}/${t.tool}`
            return (
              <div key={k}>
                <button className="ghost" aria-expanded={!!open[k]} onClick={() => setOpen({ ...open, [k]: !open[k] })}>
                  {open[k] ? '▾' : '▸'} <span className="mono">{t.tool}</span>{' '}
                  <span className="muted small">{t.fields.filter((f) => f.rule || f.covered_by).length}/{t.fields.length} masked</span>
                </button>
                {open[k] && (
                  <div className="attach">
                    {!t.has_schema && (
                      <p className="muted small">This tool doesn't describe its output. Global rules still apply; add a key rule above for any other field.</p>
                    )}
                    {t.fields.map((f) => {
                      const pk = `${k}/${f.path}`
                      return (
                        <label key={f.path} className="row small">
                          <input type="checkbox" checked={!!(f.rule || f.covered_by)} disabled={!!f.covered_by}
                            onChange={(e) => tick(s, t, f, e.target.checked)} />
                          <span className="mono">{f.path}</span>
                          <span className="muted">{f.type}</span>
                          {f.covered_by && <span className="pill">covered by {f.covered_by}</span>}
                          {f.rule && <span className="pill ok">[{f.rule.name}]</span>}
                          {!f.rule && !f.covered_by && (
                            <select aria-label={`Placeholder for ${f.path}`} value={pick[pk] || 'REDACTED'}
                              onChange={(e) => setPick({ ...pick, [pk]: e.target.value })}>
                              {PLACEHOLDERS.map((p) => <option key={p}>{p}</option>)}
                            </select>
                          )}
                          {f.description && <span className="muted">{f.description}</span>}
                        </label>
                      )
                    })}
                    {t.stale.map((r) => (
                      <div key={r.id} className="row small">
                        <s className="mono">{r.path}</s> <span className="muted">no longer in schema</span>
                        <button className="ghost danger tiny" onClick={() => remove(r.id)}>Remove</button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ))}

      <h3>Test</h3>
      <div className="card">
        <div className="row">
          <select aria-label="Apply field rules of" value={test.target} onChange={(e) => setTest({ ...test, target: e.target.value })}>
            <option value="">Global rules only</option>
            {servers.flatMap((s) => s.tools.map((t) => (
              <option key={`${s.server_id}/${t.tool}`} value={`${s.server_id}/${t.tool}`}>{s.server} → {t.tool}</option>
            )))}
          </select>
          <button className="primary" onClick={runTest}>Run</button>
        </div>
        <textarea className="mono" rows={8} aria-label="Payload JSON" value={test.payload}
          onChange={(e) => setTest({ ...test, payload: e.target.value })} style={{ width: '100%' }} />
        {tested && (
          <>
            <div className="row small">
              {Object.entries(tested.counts).map(([n, c]) => <span key={n} className="pill">{n} {c}</span>)}
              {Object.keys(tested.counts).length === 0 && <span className="muted">Nothing matched.</span>}
            </div>
            <pre className="mono small">{JSON.stringify(tested.payload, null, 2)}</pre>
          </>
        )}
      </div>
    </>
  )
}
```

- [ ] **Step 4: `Admin.jsx` Audit**:
  - Add a `PII` column header after `Result`.
  - In each row, add `<td>{r.pii ? Object.entries(JSON.parse(r.pii)).map(([n, c]) => <span key={n} className="pill">{n} {c}</span>) : <span className="muted">—</span>}{r.payload_masked_for_log && <span className="pill bad" title="Member saw unmasked data; log copy masked">unmasked</span>}</td>`.
  - Clicking a row (`onClick={() => show(r.id)}`, with `style={{ cursor: 'pointer' }}`) loads `/audit/{id}` into `const [entry, setEntry] = useState(null)`. Below the table, render:

```jsx
      {entry && (
        <div className="card">
          <div className="row">
            <div className="title grow">Payload #{entry.id}: {entry.tool}</div>
            <button className="ghost" onClick={() => setEntry(null)}>Close</button>
          </div>
          {entry.payload_masked_for_log && <p className="muted small">Member saw unmasked data; log copy masked.</p>}
          <pre className="mono small">{entry.payload ? JSON.stringify(JSON.parse(entry.payload), null, 2) : 'Not kept (the PII filter failed).'}</pre>
        </div>
      )}
```

with `const show = (id) => api(`/audit/${id}`).then(setEntry).catch(() => {})`. Payloads cut off by the 64 KB cap are not valid JSON, so wrap the `JSON.parse` in a small `pretty(s)` helper that falls back to the raw string.

- [ ] **Step 5: Build**

Run: `cd services/dashboard && npm run build`
Expected: the build succeeds with no errors.

- [ ] **Step 6: Checkpoint.**

---

### Task 6: Deploy to the `store-check` cluster with Helm

**Files:**
- Modify: `charts/gateway/values.yaml` (`tag: "0.5.0"`), `charts/dashboard/values.yaml` (`tag: "0.5.0"`), `charts/woo-mcp/values.yaml` (`tag: "0.3.0"`), `charts/store-operator/values.yaml` (`tag: "0.3.1"`, `edgeImageTag: "0.5.0"`), `deploy/store-operator-values-local.yaml` (`edgeImageTag: "0.5.0"`), `services/store-operator/store_operator/handlers.py` (the `EDGE_IMAGE_TAG` default becomes `"0.5.0"`), `deploy/gateway-values-prod.yaml` (`tag: "0.5.0"`), and the build commands in `README.md`.

- [ ] **Step 1: Bump the tags** in the files listed above. Then check that nothing still points at the old tags: `grep -rn '0\.4\.0' charts deploy services/store-operator | grep -v '^charts/store/'` should show only unrelated chart versions.

- [ ] **Step 2: Build and import the images**

```bash
docker build -t gateway:0.5.0 services/gateway
docker build -t dashboard:0.5.0 services/dashboard
docker build -t woo-mcp:0.3.0 services/woo-mcp
docker build -f services/store-operator/Dockerfile -t store-operator:0.3.1 .
k3d image import gateway:0.5.0 dashboard:0.5.0 woo-mcp:0.3.0 store-operator:0.3.1 -c store-check
```

- [ ] **Step 3: Helm upgrades** (namespace `mercora`; the store's woo-mcp is in `store-shop1`)

```bash
helm upgrade team charts/gateway -n mercora -f deploy/gateway-values-local.yaml --wait   # control plane: creates the new tables
helm upgrade gw-prod charts/gateway -n mercora --reuse-values --set image.tag=0.5.0 --wait  # the existing edge (the operator does not upgrade it)
helm upgrade team-ui charts/dashboard -n mercora -f deploy/dashboard-values-local.yaml --wait
helm upgrade store-operator charts/store-operator -n mercora -f deploy/store-operator-values-local.yaml --wait
helm upgrade woo-mcp charts/woo-mcp -n store-shop1 --reuse-values --set image.tag=0.3.0 --wait
```

`team` goes first so the schema exists before the edge's new code reads `pii_protection`.

- [ ] **Step 4: Smoke check**

```bash
kubectl get pods -n mercora; kubectl get pods -n store-shop1
kubectl get deploy -n mercora -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].image}{"\n"}{end}'
kubectl logs -n mercora deploy/team-gateway --tail=20
```

Expected: every pod Running with 0 new restarts, the images at 0.5.0 / 0.3.0 / 0.3.1, and no tracebacks in the logs. In the dashboard (http://localhost or the dashboard ingress host), the **PII Protection** tab lists `shop1` → `get_order` with its fields, `customer` is shown as covered by PERSON, and a gateway shows the PII protection switch.

- [ ] **Step 5: Hand over** spec §10's ten manual tests for the user to run.
