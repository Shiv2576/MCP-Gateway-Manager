"""The PII filter on its own: no database, no network."""

from __future__ import annotations

import json
import time

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from app.pii import DEFAULTS, FILTER_TIMEOUT, MAX_SCAN, NON_TEXT, Rule, redact_result, redact_value, schema_fields

SHOP, GET = 3, "get_order"


def scan(value, rules=DEFAULTS, server_id=SHOP, tool=GET):
    out, counts = redact_value(value, rules, server_id, tool)
    return out, dict(counts)


def test_default_patterns_inside_free_text():
    out, counts = scan({"msg": "mail a@b.co, call +1 415 555 0100, card 4111 1111 1111 1111, "
                               "ssn 123-45-6789, ip 10.0.0.12, order 2042, total 149.00"})
    assert out == {"msg": "mail [EMAIL], call [PHONE], card [CREDIT_CARD], ssn [SSN], "
                          "ip [IP_ADDRESS], order 2042, total 149.00"}
    assert counts == {"EMAIL": 1, "PHONE": 1, "CREDIT_CARD": 1, "SSN": 1, "IP_ADDRESS": 1}


def test_no_false_positives():
    for text in ("4111 1111 1111 1112", "2042", "149.00", "2026-10-11T05:43:02Z", "2026-10-11 05:43:02", "v1.2",
                 "1760159400000", "order 10234567891", "202610110530", "11-10-2026 05:30"):
        assert scan({"x": text}) == ({"x": text}, {}), text


def test_indian_mobile_is_a_phone():
    for text in ("+91 98765 43210", "9876543210", "+919876543210", "(415) 555-0100"):
        assert scan({"x": text})[0] == {"x": "[PHONE]"}, text


def test_key_rules_at_any_depth_and_any_case():
    out, counts = scan({"Customer": "Alice", "order": {"billing": {"first_name": "A", "email": "a@b.co"}},
                        "customer_note": None, "id": 7})
    assert out == {"Customer": "[PERSON]", "order": {"billing": "[ADDRESS]"}, "customer_note": None, "id": 7}
    assert counts == {"PERSON": 1, "ADDRESS": 1}  # a null value is not counted


def test_custom_rules_run_before_defaults():
    aadhaar = Rule("AADHAAR", "pattern", r"\b\d{4}\s?\d{4}\s?\d{4}\b")
    loyalty = Rule("LOYALTY", "key", "loyalty_id")
    out, counts = scan({"name": "Gift card 2345 6789 0123", "loyalty_id": 99}, [aadhaar, loyalty, *DEFAULTS])
    assert out == {"name": "Gift card [AADHAAR]", "loyalty_id": "[LOYALTY]"}
    assert counts == {"AADHAAR": 1, "LOYALTY": 1}


def test_field_rules_touch_only_their_path_and_tool():
    rules = [Rule("REDACTED", "field", "orders[].items[].name", SHOP, "list_orders"),
             Rule("PAY", "field", "orders[].payment", SHOP, "list_orders"), *DEFAULTS]
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
    out, counts = redact_result(r, DEFAULTS, SHOP, GET)
    assert out.structured_content == {"id": 2042, "customer": "[PERSON]", "note": "mail [EMAIL]"}
    assert json.loads(out.content[0].text) == out.structured_content
    assert counts == {"PERSON": 1, "EMAIL": 1}  # the two copies count once
    assert r.structured_content["customer"] == "Alice"  # the input is not changed


def test_result_plain_text_and_non_text():
    r = CallToolResult(content=[TextContent(type="text", text="no order for bob@x.io"),
                                ImageContent(type="image", data="aGk=", mime_type="image/png")], is_error=True)
    out, counts = redact_result(r, DEFAULTS, None, "")
    assert [c.text for c in out.content] == ["no order for [EMAIL]", NON_TEXT]
    assert out.is_error and counts == {"EMAIL": 1, "NON_TEXT": 1}


def test_json_scalars_in_text():
    r = CallToolResult(content=[TextContent(type="text", text="2042"), TextContent(type="text", text='"a@b.co"')])
    out, _ = redact_result(r, DEFAULTS, None, "")
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
    assert time.monotonic() - started < FILTER_TIMEOUT + 1.0  # the pattern alone would run for hours


def test_defaults_are_fast_on_hostile_input():
    for hostile in (("1-" * 20_000) + "a@" * 20_000, " a@" + "a." * 50_000 + "1"):
        started = time.monotonic()
        scan({"x": hostile})
        assert time.monotonic() - started < 0.2, hostile[:20]
