"""Parse + schema validation, producing exactly one stable status ID.

Classification rules (deliberately unforgiving):

* The raw text is parsed with ``json.JSONDecoder().raw_decode`` starting at the
  first character that is not JSON whitespace (space, tab, CR, LF).
* Nothing is stripped, unwrapped, or extracted. Markdown fences and leading
  prose are parse failures, not something to repair.
* Only JSON whitespace may follow the decoded value; anything else is trailing
  content, even if the value itself validates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

#: JSON Schema dialect used for every case in this repository.
SCHEMA_DRAFT = "2020-12"

SCHEMA_VALID = "schema_valid"
SCHEMA_INVALID = "schema_invalid"
SYNTAX_INVALID = "syntax_invalid"
TRAILING_CONTENT = "trailing_content"

#: The complete, stable status vocabulary. A case is always exactly one of these.
STATUSES: tuple[str, ...] = (
    SCHEMA_VALID,
    SCHEMA_INVALID,
    SYNTAX_INVALID,
    TRAILING_CONTENT,
)

# RFC 8259 whitespace. Narrower than str.isspace(): a form feed or a non-break
# space around a value is extra content, not whitespace.
_JSON_WS = re.compile(r"[ \t\n\r]*")
_DETAIL_LIMIT = 72


class SchemaError(Exception):
    """Raised when a case ships a schema that is not valid Draft 2020-12."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


@dataclass(frozen=True)
class ValidationError:
    """A single schema violation, located by JSON Pointer (RFC 6901)."""

    message: str
    pointer: str

    def to_dict(self) -> dict[str, str]:
        return {"message": self.message, "pointer": self.pointer}


@dataclass(frozen=True)
class Classification:
    """The verdict for one raw output."""

    status: str
    errors: tuple[ValidationError, ...] = ()
    #: Human-readable context for a parse failure or trailing content. Never
    #: part of the JSON report, whose shape is stable.
    detail: str | None = None
    #: The decoded value, when the text parsed cleanly. ``None`` otherwise --
    #: note that ``null`` also decodes to ``None``, so check ``status`` first.
    value: Any = field(default=None, repr=False)

    @property
    def pointers(self) -> tuple[str, ...]:
        return tuple(error.pointer for error in self.errors)


def to_pointer(path: Iterable[Any]) -> str:
    """Render a ``jsonschema`` error path as an RFC 6901 JSON Pointer.

    The document root is the empty string; ``~`` and ``/`` inside object keys
    are escaped as ``~0`` and ``~1``.
    """
    tokens = []
    for part in path:
        token = str(part)
        if not isinstance(part, int):
            token = token.replace("~", "~0").replace("/", "~1")
        tokens.append("/" + token)
    return "".join(tokens)


def _detail(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > _DETAIL_LIMIT:
        collapsed = collapsed[:_DETAIL_LIMIT] + "..."
    return collapsed


def _validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:  # pragma: no cover - fixture hygiene
        raise SchemaError(str(exc)) from exc
    return Draft202012Validator(schema)


def _errors(validator: Draft202012Validator, value: Any) -> tuple[ValidationError, ...]:
    found = [
        ValidationError(message=error.message, pointer=to_pointer(error.absolute_path))
        for error in validator.iter_errors(value)
    ]
    # iter_errors order depends on keyword iteration; sort so reports are stable.
    found.sort(key=lambda error: (error.pointer, error.message))
    return tuple(found)


def classify_text(raw: str, schema: Mapping[str, Any]) -> Classification:
    """Classify raw output text against ``schema``.

    ``raw`` is used verbatim -- no stripping, no fence removal, no substring
    extraction.
    """
    validator = _validator(schema)
    start = _JSON_WS.match(raw, 0).end()

    try:
        value, end = _DECODER.raw_decode(raw, start)
    except json.JSONDecodeError as exc:
        return Classification(
            SYNTAX_INVALID,
            detail=f"{exc.msg} (line {exc.lineno} column {exc.colno})",
        )
    except ValueError as exc:
        return Classification(SYNTAX_INVALID, detail=str(exc))

    rest = raw[end:]
    if _JSON_WS.match(rest, 0).end() != len(rest):
        return Classification(
            TRAILING_CONTENT,
            detail=f"extra content after value: {_detail(rest)!r}",
            value=value,
        )

    errors = _errors(validator, value)
    if errors:
        return Classification(SCHEMA_INVALID, errors=errors, value=value)
    return Classification(SCHEMA_VALID, value=value)


def classify(case: Any) -> Classification:
    """Classify a :class:`crashlab.cases.Case`."""
    return classify_text(case.raw, case.schema)


def pointers_of(errors: Sequence[ValidationError]) -> tuple[str, ...]:
    return tuple(error.pointer for error in errors)
