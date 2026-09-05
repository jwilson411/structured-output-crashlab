"""Unit tests for the four-status classifier."""

from __future__ import annotations

import pytest

from crashlab.classify import (
    SCHEMA_INVALID,
    SCHEMA_VALID,
    STATUSES,
    SYNTAX_INVALID,
    TRAILING_CONTENT,
    SchemaError,
    classify_text,
    to_pointer,
)

STATUS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"status": {"enum": ["pending", "shipped"]}},
    "required": ["status"],
}
ANY_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"status": "shipped"}', SCHEMA_VALID),
        ('   \n\t{"status": "shipped"}\n\n  ', SCHEMA_VALID),
        ('{"status": "Shipped"}', SCHEMA_INVALID),
        ("{}", SCHEMA_INVALID),
        ('{"status": ', SYNTAX_INVALID),
        ("", SYNTAX_INVALID),
        ("   \n  ", SYNTAX_INVALID),
        ('```json\n{"status": "shipped"}\n```', SYNTAX_INVALID),
        ('Here you go:\n{"status": "shipped"}', SYNTAX_INVALID),
        ('{"status": "shipped"} thanks!', TRAILING_CONTENT),
        ('{"status": "shipped"}\n```', TRAILING_CONTENT),
        ('{"status": "shipped"}{"status": "pending"}', TRAILING_CONTENT),
    ],
)
def test_status_ids(raw, expected):
    result = classify_text(raw, STATUS_SCHEMA)
    assert result.status == expected
    assert result.status in STATUSES


def test_fences_are_never_stripped():
    """The whole point: a fenced block is a parse failure, not a valid value."""
    fenced = '```json\n{"status": "shipped"}\n```\n'
    assert classify_text(fenced, STATUS_SCHEMA).status == SYNTAX_INVALID


def test_json_substring_is_never_extracted():
    prose = 'The answer is {"status": "shipped"} -- hope that helps.'
    assert classify_text(prose, STATUS_SCHEMA).status == SYNTAX_INVALID


def test_trailing_content_wins_over_schema_validity():
    """A value that would validate is still trailing_content if text follows."""
    result = classify_text('{"status": "shipped"}\nDone.', STATUS_SCHEMA)
    assert result.status == TRAILING_CONTENT
    assert result.errors == ()
    assert "Done." in result.detail


def test_only_rfc8259_whitespace_is_whitespace():
    # A form feed is whitespace to str.strip() but not to JSON.
    assert classify_text('\f{"status": "shipped"}', STATUS_SCHEMA).status == SYNTAX_INVALID
    assert classify_text('{"status": "shipped"}\f', STATUS_SCHEMA).status == TRAILING_CONTENT


def test_python_json_extensions_are_rejected():
    assert classify_text("NaN", ANY_SCHEMA).status == SYNTAX_INVALID
    assert classify_text("Infinity", ANY_SCHEMA).status == SYNTAX_INVALID


def test_scalar_and_null_documents_are_values():
    assert classify_text("null", ANY_SCHEMA).status == SCHEMA_VALID
    assert classify_text("42", ANY_SCHEMA).status == SCHEMA_VALID


def test_pointers_are_rfc6901():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"qty": {"type": "integer"}},
                },
            }
        },
    }
    result = classify_text('{"items": [{"qty": 1}, {"qty": "2"}]}', schema)
    assert result.status == SCHEMA_INVALID
    assert result.pointers == ("/items/1/qty",)


def test_root_errors_use_the_empty_pointer():
    result = classify_text('{"status": "shipped", "extra": 1}', {**STATUS_SCHEMA, "additionalProperties": False})
    assert result.status == SCHEMA_INVALID
    assert result.pointers == ("",)


def test_pointer_escaping():
    assert to_pointer([]) == ""
    assert to_pointer(["a/b"]) == "/a~1b"
    assert to_pointer(["a~b"]) == "/a~0b"
    assert to_pointer(["items", 0, "x"]) == "/items/0/x"


def test_errors_are_sorted_deterministically():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    first = classify_text('{"a": "x", "b": "y"}', schema)
    second = classify_text('{"b": "y", "a": "x"}', schema)
    assert first.pointers == ("/a", "/b") == second.pointers


def test_invalid_schema_raises():
    with pytest.raises(SchemaError):
        classify_text("{}", {"type": "not-a-type"})
