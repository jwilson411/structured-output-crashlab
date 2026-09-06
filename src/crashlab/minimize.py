"""Deterministic, schema-aware shrinking of a failing case to a local minimum.

Given a case that fails SOC-01 classification, this module reduces the schema
and the output *together* until a full pass of the operator list accepts
nothing, then writes a portable incident bundle.

The rules are deliberately narrow:

* **The classifier is the oracle.** Every candidate pair is re-run through
  :func:`crashlab.classify.classify_text`. A candidate is accepted only if the
  status is unchanged, it does not introduce additional errors, and it keeps at
  least one of the original JSON Pointers or one of the original error keywords.
* **Greedy and local.** Operators run in a fixed order; within an operator,
  candidates are enumerated in a fixed order and the first accepted one wins.
  The result is a *local minimum* for this operator list -- not a proof that no
  smaller failing case exists.
* **Terminating.** A candidate is only considered if it is strictly smaller
  than the current pair, so the search cannot cycle.
* **Read only at the source.** The case directory is never written; everything
  lands under the bundle directory.
* **No repair.** The no-repair rule from SOC-01 holds: a truncated value is
  never completed, fences are never stripped to rescue the JSON inside them,
  and the JSON value under a trailing suffix is never removed.

This is not general-purpose delta debugging: there is no hierarchical ddmin over
byte windows, only the schema-directed operators below.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from crashlab.cases import EXPECTED_FILE, META_FILE, OUTPUT_FILE, SCHEMA_FILE, Case
from crashlab.classify import (
    SCHEMA_INVALID,
    SCHEMA_VALID,
    SYNTAX_INVALID,
    TRAILING_CONTENT,
    Classification,
    classify_text,
)
from crashlab.mutate import escape_token, join_pointer, parse_pointer, value_at

#: Written into the bundle's ``meta.json``.
GENERATOR = "crashlab-minimize"

CASE_DIR = "case"
REDUCTION_FILE = "reduction.jsonl"
INCIDENT_FILE = "INCIDENT.md"
BYTES_FILE = "bytes.json"

DROP_UNUSED_PROPERTY = "drop_unused_property"
SHRINK_ARRAY = "shrink_array"
SHRINK_STRING = "shrink_string"
SIMPLIFY_NESTED_OBJECT = "simplify_nested_object"
DROP_UNUSED_DEFS = "drop_unused_defs"
TRIM_TRAILING_TEXT = "trim_trailing_text"
TRIM_SYNTAX_PADDING = "trim_syntax_padding"

#: Keys that hold a subschema container of named definitions.
_DEFS_KEYS = ("$defs", "definitions")

#: Substrings of ``jsonschema`` messages that name the *kind* of violation.
_PHRASES: tuple[str, ...] = (
    "additional properties are not allowed",
    "does not match",
    "has non-unique elements",
    "is greater than",
    "is less than",
    "is not a multiple of",
    "is not of type",
    "is not one of",
    "is not valid under any of the given schemas",
    "is too long",
    "is too short",
    "required property",
    "was expected",
)

_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_JSON_WS = re.compile(r"[ \t\n\r]*")
_FENCE_TAG = re.compile(r"(?m)^([ \t]*`{3,})([A-Za-z0-9_.+-]+)[ \t]*$")
_RUNS = re.compile(r"\s+|\S+")

#: Quoted tokens longer than this are prose, not an identifier worth pinning.
_KEYWORD_LIMIT = 64


class MinimizeError(Exception):
    """Raised on a case that cannot be minimized, or a bundle that cannot be written."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


# Mirrors the classifier's decoder: ``NaN`` and friends are not JSON here either.
_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


# --------------------------------------------------------------------------
# Stable serialization
# --------------------------------------------------------------------------


def compact(value: Any) -> str:
    """The single stable encoder used for every minimized instance."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_body(schema: Any) -> bytes:
    """The exact bytes written to ``schema.json``; byte counts are taken from these."""
    return (json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _decode_output(raw: str) -> tuple[Any, str] | None:
    """Split ``raw`` into its leading JSON value and the text that follows it."""
    start = _JSON_WS.match(raw, 0).end()
    try:
        value, end = _DECODER.raw_decode(raw, start)
    except (json.JSONDecodeError, ValueError):
        return None
    return value, raw[end:]


# --------------------------------------------------------------------------
# What the failure is, and what must survive
# --------------------------------------------------------------------------


def failure_keywords(result: Classification, raw: str) -> tuple[str, ...]:
    """Stable tokens identifying this failure, for the preservation rule.

    For ``schema_invalid`` these are the ``jsonschema`` phrases plus the quoted
    tokens (property names, values, types) of the messages. For the two textual
    statuses they are the words of the parse error or of the trailing suffix
    itself -- the classifier's own boilerplate prefix is not distinctive.
    """
    texts: list[str] = []
    if result.status == SCHEMA_INVALID:
        texts = [error.message for error in result.errors]
    elif result.status == TRAILING_CONTENT:
        decoded = _decode_output(raw)
        if decoded is not None and decoded[1].strip():
            texts = [decoded[1]]
        elif result.detail:
            texts = [result.detail]
    elif result.detail:
        texts = [result.detail]

    found: set[str] = set()
    for text in texts:
        lowered = text.lower()
        found.update(phrase for phrase in _PHRASES if phrase in lowered)
        for match in _QUOTED.finditer(text):
            token = match.group(1) if match.group(1) is not None else match.group(2)
            if token and len(token) <= _KEYWORD_LIMIT:
                found.add(token)
        if result.status != SCHEMA_INVALID:
            found.update(match.group(0).lower() for match in _WORD.finditer(text))
    return tuple(sorted(found))


@dataclass(frozen=True)
class _Target:
    """The failure that every candidate has to keep reproducing."""

    status: str
    pointers: tuple[str, ...]
    keywords: tuple[str, ...]
    error_count: int

    def preserved(self, result: Classification | None, raw: str) -> bool:
        if result is None or result.status != self.status:
            return False
        # Shrinking must not introduce a second failure to look at.
        if len(result.errors) > self.error_count:
            return False
        if not self.pointers and not self.keywords:
            return True
        if set(result.pointers) & set(self.pointers):
            return True
        return bool(set(failure_keywords(result, raw)) & set(self.keywords))


@dataclass
class _State:
    """One candidate pair: a schema and the exact output text."""

    schema: Any
    raw: str

    def __post_init__(self) -> None:
        self.schema_bytes = schema_body(self.schema)
        self.output_bytes = self.raw.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.schema_bytes) + len(self.output_bytes)


@dataclass(frozen=True)
class _Candidate:
    op: str
    target: str
    state: _State


# --------------------------------------------------------------------------
# Walking schema and instance together
# --------------------------------------------------------------------------


def _at_path(schema: Any, path: Sequence[str]) -> Any:
    node = schema
    for key in path:
        node = node[int(key)] if isinstance(node, list) else node[key]
    return node


def _schema_pointer(path: Sequence[str]) -> str:
    return "".join("/" + escape_token(key) for key in path)


def _deref(schema: Any, node: Any, path: tuple[str, ...] | None) -> tuple[Any, tuple[str, ...] | None]:
    """Follow local ``$ref`` chains. Anything else stops the walk."""
    seen: set[str] = set()
    while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen or not ref.startswith("#/"):
            return None, None
        seen.add(ref)
        target: Any = schema
        walked: list[str] = []
        for token in parse_pointer(ref[1:]):
            if not isinstance(target, Mapping) or token not in target:
                return None, None
            target = target[token]
            walked.append(token)
        node, path = target, tuple(walked)
    return node, path


@dataclass(frozen=True)
class _Join:
    """One instance location, with the schema location that governs it."""

    pointer: str
    value: Any
    #: Where the governing subschema lives, after following ``$ref``.
    schema_path: tuple[str, ...] | None
    #: Where it was declared, before following ``$ref`` -- i.e. the
    #: ``.../properties/<key>`` slot this member occupies.
    owner_path: tuple[str, ...] | None = None


def _join(schema: Any, value: Any) -> list[_Join]:
    """Walk the instance, carrying the schema location for as long as it is known.

    Only ``properties``, a single ``items`` subschema and local ``$ref`` are
    followed; anywhere else the schema side simply becomes ``None``, which shows
    up as fewer candidates rather than as a crash.
    """
    found: list[_Join] = []

    def walk(pointer: str, node: Any, path: tuple[str, ...] | None, current: Any) -> None:
        owner = path
        node, path = _deref(schema, node, path)
        found.append(
            _Join(pointer, current, path if isinstance(node, Mapping) else None, owner)
        )
        properties = node.get("properties") if isinstance(node, Mapping) else None
        items = node.get("items") if isinstance(node, Mapping) else None
        if isinstance(current, dict):
            for key in sorted(current):
                sub = properties.get(key) if isinstance(properties, Mapping) else None
                sub_path = (*path, "properties", key) if sub is not None and path is not None else None
                walk(join_pointer(pointer, key), sub, sub_path, current[key])
        elif isinstance(current, list):
            sub_path = (*path, "items") if isinstance(items, Mapping) and path is not None else None
            for index, item in enumerate(current):
                walk(join_pointer(pointer, index), items, sub_path, item)

    walk("", schema, (), value)
    return found


def _iter_schema_nodes(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every dict in the schema document, parents first, keys sorted."""
    if isinstance(node, Mapping):
        yield path, node
        for key in sorted(node):
            yield from _iter_schema_nodes(node[key], (*path, key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_schema_nodes(item, (*path, str(index)))


def _drop_property(schema: Any, path: Sequence[str], key: str) -> bool:
    """Remove ``key`` from the ``properties`` (and ``required``) at ``path``."""
    try:
        node = _at_path(schema, path)
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(node, dict):
        return False
    properties = node.get("properties")
    if not isinstance(properties, dict) or key not in properties:
        return False
    del properties[key]
    if not properties:
        del node["properties"]
    required = node.get("required")
    if isinstance(required, list) and key in required:
        required.remove(key)
        if not required:
            del node["required"]
    return True


def _drop_instance_key(value: Any, pointer: str, key: str) -> Any:
    reduced = copy.deepcopy(value)
    del value_at(reduced, pointer)[key]
    return reduced


def _replace_at(value: Any, pointer: str, replacement: Any) -> Any:
    """A copy of ``value`` with the member at ``pointer`` replaced."""
    if not pointer:
        return replacement
    reduced = copy.deepcopy(value)
    token = parse_pointer(pointer)[-1]
    parent = value_at(reduced, pointer.rsplit("/", 1)[0])
    if isinstance(parent, list):
        parent[int(token)] = replacement
    else:
        parent[token] = replacement
    return reduced


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------


def _instance(state: _State, target: _Target) -> tuple[Any, str] | None:
    """The decoded instance and the suffix to keep, or ``None`` if unparseable.

    For ``schema_invalid`` the suffix is dropped: whitespace around the value is
    not part of the failure. For ``trailing_content`` it is the trailing text,
    which only :func:`_trim_trailing_text` may touch.
    """
    if target.status not in (SCHEMA_INVALID, TRAILING_CONTENT):
        return None
    decoded = _decode_output(state.raw)
    if decoded is None:
        return None
    value, trailing = decoded
    return value, (trailing if target.status == TRAILING_CONTENT else "")


def _pair(op: str, name: str, schema: Any, value: Any, trailing: str) -> _Candidate:
    return _Candidate(op, name, _State(schema, compact(value) + trailing))


def _drop_unused_property(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Drop a property the failure does not need, from instance and schema together."""
    decoded = _instance(state, target)
    backed: set[tuple[str, ...]] = set()

    if decoded is not None:
        value, trailing = decoded
        joins = _join(state.schema, value)
        backed = {join.owner_path for join in joins if join.owner_path is not None}
        for join in sorted(joins, key=lambda item: item.pointer):
            if not isinstance(join.value, dict):
                continue
            for key in sorted(join.value):
                schema = copy.deepcopy(state.schema)
                if join.schema_path is not None:
                    _drop_property(schema, join.schema_path, key)
                yield _pair(
                    DROP_UNUSED_PROPERTY,
                    join_pointer(join.pointer, key),
                    schema,
                    _drop_instance_key(value, join.pointer, key),
                    trailing,
                )

    for path, node in _iter_schema_nodes(state.schema):
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            continue
        for key in sorted(properties):
            if (*path, "properties", key) in backed:
                continue  # covered above, together with its instance member
            schema = copy.deepcopy(state.schema)
            if _drop_property(schema, path, key):
                yield _Candidate(
                    DROP_UNUSED_PROPERTY,
                    _schema_pointer((*path, "properties", key)),
                    _State(schema, state.raw),
                )


def _shrink_array(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Drop one array item at a time, highest index first."""
    decoded = _instance(state, target)
    if decoded is None:
        return
    value, trailing = decoded
    for join in sorted(_join(state.schema, value), key=lambda item: item.pointer):
        if not isinstance(join.value, list):
            continue
        for index in range(len(join.value) - 1, -1, -1):
            reduced = copy.deepcopy(value)
            del value_at(reduced, join.pointer)[index]
            yield _pair(
                SHRINK_ARRAY,
                join_pointer(join.pointer, index),
                copy.deepcopy(state.schema),
                reduced,
                trailing,
            )


def _shorter(text: str) -> list[str]:
    """Halve first, then trim a single character. Never invents characters."""
    if not text:
        return []
    candidates = [text[: len(text) // 2], text[:-1]]
    return [candidate for candidate in candidates if candidate != text]


def _shrink_string(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Shorten instance strings, then unrelated ``enum`` / ``const`` strings."""
    decoded = _instance(state, target)
    if decoded is not None:
        value, trailing = decoded
        for join in sorted(_join(state.schema, value), key=lambda item: item.pointer):
            if not isinstance(join.value, str):
                continue
            for shorter in _shorter(join.value):
                yield _pair(
                    SHRINK_STRING,
                    join.pointer,
                    copy.deepcopy(state.schema),
                    _replace_at(value, join.pointer, shorter),
                    trailing,
                )

    for path, node in _iter_schema_nodes(state.schema):
        for keyword in ("const", "enum"):
            holder = node.get(keyword)
            members = holder if isinstance(holder, list) else [holder]
            for index, member in enumerate(members):
                if not isinstance(member, str):
                    continue
                for shorter in _shorter(member):
                    schema = copy.deepcopy(state.schema)
                    if isinstance(holder, list):
                        _at_path(schema, path)[keyword][index] = shorter
                        name = _schema_pointer((*path, keyword, str(index)))
                    else:
                        _at_path(schema, path)[keyword] = shorter
                        name = _schema_pointer((*path, keyword))
                    yield _Candidate(SHRINK_STRING, name, _State(schema, state.raw))


def _simplify_nested_object(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Empty out a nested object, or drop its optional keys."""
    decoded = _instance(state, target)
    if decoded is None:
        return
    value, trailing = decoded
    for join in sorted(_join(state.schema, value), key=lambda item: item.pointer):
        if not join.pointer or not isinstance(join.value, dict) or not join.value:
            continue
        prefix = join.pointer + "/"
        if any(p == join.pointer or p.startswith(prefix) for p in target.pointers):
            continue  # on a preserved pointer path: leave it alone

        yield _pair(
            SIMPLIFY_NESTED_OBJECT,
            join.pointer,
            copy.deepcopy(state.schema),
            _replace_at(value, join.pointer, {}),
            trailing,
        )

        required: Sequence[Any] = ()
        if join.schema_path is not None:
            node = _at_path(state.schema, join.schema_path)
            if isinstance(node, Mapping) and isinstance(node.get("required"), list):
                required = node["required"]
        for key in sorted(join.value):
            if key in required:
                continue
            schema = copy.deepcopy(state.schema)
            if join.schema_path is not None:
                _drop_property(schema, join.schema_path, key)
            yield _pair(
                SIMPLIFY_NESTED_OBJECT,
                join_pointer(join.pointer, key),
                schema,
                _drop_instance_key(value, join.pointer, key),
                trailing,
            )


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for key in sorted(node):
            yield from _refs(node[key])
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _drop_unused_defs(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Remove a ``$defs`` / ``definitions`` entry no remaining ``$ref`` names."""
    referenced = set(_refs(state.schema))
    for path, node in _iter_schema_nodes(state.schema):
        for container in _DEFS_KEYS:
            entries = node.get(container)
            if not isinstance(entries, Mapping):
                continue
            for name in sorted(entries):
                pointer = "#" + _schema_pointer((*path, container, name))
                if any(ref == pointer or ref.startswith(pointer + "/") for ref in referenced):
                    continue
                schema = copy.deepcopy(state.schema)
                holder = _at_path(schema, path)
                del holder[container][name]
                if not holder[container]:
                    del holder[container]
                yield _Candidate(
                    DROP_UNUSED_DEFS,
                    _schema_pointer((*path, container, name)).lstrip("/"),
                    _State(schema, state.raw),
                )


def _trim_trailing_text(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Shorten the suffix after a complete value: whole runs first, then characters."""
    if target.status != TRAILING_CONTENT:
        return
    decoded = _decode_output(state.raw)
    if decoded is None or not decoded[1]:
        return
    trailing = decoded[1]
    head = state.raw[: len(state.raw) - len(trailing)]
    runs = _RUNS.findall(trailing)
    shorter = ["".join(runs[:-1])] if len(runs) > 1 else []
    shorter.append(trailing[:-1])
    for suffix in shorter:
        if suffix != trailing:
            yield _Candidate(TRIM_TRAILING_TEXT, "output", _State(state.schema, head + suffix))


def _body_span(raw: str) -> tuple[int, int]:
    """The span from the first ``{``/``[`` to the last ``}``/``]``: never touched."""
    starts = [index for index in (raw.find("{"), raw.find("[")) if index >= 0]
    if not starts:
        return len(raw), len(raw)
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]")) + 1
    return start, max(end, start)


def _trim_syntax_padding(state: _State, target: _Target) -> Iterator[_Candidate]:
    """Drop padding around an unparseable body: fence language tags, then lines.

    The body itself is never edited, so this cannot repair the output into valid
    JSON -- if a candidate happened to parse, the status would change and the
    classifier would reject it anyway.
    """
    if target.status != SYNTAX_INVALID:
        return
    raw = state.raw
    match = _FENCE_TAG.search(raw)
    if match:
        yield _Candidate(
            TRIM_SYNTAX_PADDING,
            "output",
            _State(state.schema, raw[: match.start(2)] + raw[match.end(2) :]),
        )

    start, end = _body_span(raw)
    if start == end:
        return
    leading = raw[:start].splitlines(keepends=True)
    if leading:
        yield _Candidate(
            TRIM_SYNTAX_PADDING, "output", _State(state.schema, "".join(leading[1:]) + raw[start:])
        )
    trailing = raw[end:].splitlines(keepends=True)
    if trailing:
        yield _Candidate(
            TRIM_SYNTAX_PADDING, "output", _State(state.schema, raw[:end] + "".join(trailing[:-1]))
        )


_Operator = Callable[[_State, _Target], Iterator[_Candidate]]

#: The fixed operator order. One full pass with no acceptance is the local minimum.
_OPERATORS: tuple[tuple[str, _Operator], ...] = (
    (DROP_UNUSED_PROPERTY, _drop_unused_property),
    (SHRINK_ARRAY, _shrink_array),
    (SHRINK_STRING, _shrink_string),
    (SIMPLIFY_NESTED_OBJECT, _simplify_nested_object),
    (DROP_UNUSED_DEFS, _drop_unused_defs),
    (TRIM_TRAILING_TEXT, _trim_trailing_text),
    (TRIM_SYNTAX_PADDING, _trim_syntax_padding),
)

OPERATOR_IDS: tuple[str, ...] = tuple(op for op, _ in _OPERATORS)


# --------------------------------------------------------------------------
# The search
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reduction:
    """One accepted reduction, in application order."""

    op: str
    target: str
    before_bytes: int
    after_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "target": self.target,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
        }


@dataclass(frozen=True)
class MinimizeResult:
    """A minimized case, its provenance, and how it got there."""

    case_id: str
    source: Path
    status: str
    original_pointers: tuple[str, ...]
    minimized_pointers: tuple[str, ...]
    schema: Any
    raw: str
    reductions: tuple[Reduction, ...]
    passes: int
    schema_before: int
    schema_after: int
    output_before: int
    output_after: int
    #: Always true: the search stops at a local minimum for :data:`OPERATOR_IDS`.
    local_minimum: bool = field(default=True)

    @property
    def total_before(self) -> int:
        return self.schema_before + self.output_before

    @property
    def total_after(self) -> int:
        return self.schema_after + self.output_after


def _schema_ok(schema: Any) -> bool:
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError:
        return False
    return True


def _safe_classify(raw: str, schema: Any) -> Classification | None:
    try:
        return classify_text(raw, schema)
    except Exception:  # noqa: BLE001 - a candidate the validator cannot run is rejected
        return None


def _first_accepted(
    candidates: Iterator[_Candidate], state: _State, target: _Target
) -> _Candidate | None:
    for candidate in candidates:
        if candidate.state.size >= state.size:
            continue  # strictly smaller only, so the search terminates
        if not _schema_ok(candidate.state.schema):
            continue
        result = _safe_classify(candidate.state.raw, candidate.state.schema)
        if target.preserved(result, candidate.state.raw):
            return candidate
    return None


def minimize_case(case: Case) -> MinimizeResult:
    """Shrink ``case`` to a local minimum that still fails the same way.

    ``case`` is not modified: the search runs on in-memory copies.
    """
    original = classify_text(case.raw, case.schema)
    if original.status == SCHEMA_VALID:
        raise MinimizeError(
            f"{case.path}: status is schema_valid, so there is no failure to minimize"
        )

    seen: list[str] = []
    for pointer in original.pointers:
        if pointer not in seen:
            seen.append(pointer)
    target = _Target(
        status=original.status,
        pointers=tuple(seen),
        keywords=failure_keywords(original, case.raw),
        error_count=len(original.errors),
    )

    state = _State(copy.deepcopy(case.schema), case.raw)
    before = state
    reductions: list[Reduction] = []
    passes = 0

    while True:
        passes += 1
        accepted = 0
        for _, operator in _OPERATORS:
            while True:
                found = _first_accepted(operator(state, target), state, target)
                if found is None:
                    break
                reductions.append(
                    Reduction(found.op, found.target, state.size, found.state.size)
                )
                state = found.state
                accepted += 1
        if accepted == 0:
            break  # a full pass of the operator list changed nothing

    final = classify_text(state.raw, state.schema)
    minimized: list[str] = []
    for pointer in final.pointers:
        if pointer not in minimized:
            minimized.append(pointer)

    return MinimizeResult(
        case_id=case.id,
        source=case.path,
        status=final.status,
        original_pointers=target.pointers,
        minimized_pointers=tuple(minimized),
        schema=state.schema,
        raw=state.raw,
        reductions=tuple(reductions),
        passes=passes,
        schema_before=len(before.schema_bytes),
        schema_after=len(state.schema_bytes),
        output_before=len(before.output_bytes),
        output_after=len(state.output_bytes),
    )


# --------------------------------------------------------------------------
# The bundle
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_bytes(text.encode("utf-8"))


def _show_pointer(pointer: str) -> str:
    return pointer or "<root>"


def _show_pointers(pointers: Sequence[str]) -> str:
    return ", ".join(f"`{_show_pointer(pointer)}`" for pointer in pointers) if pointers else "(none)"


def repro_commands(result: MinimizeResult, out_dir: Path, source: str) -> tuple[str, str]:
    """The two commands that reproduce the bundle and re-run the minimized case."""
    return (
        f"crashlab run {out_dir / CASE_DIR}",
        f"crashlab minimize {source} --out {out_dir}",
    )


def render_incident(result: MinimizeResult, out_dir: Path, source: str) -> str:
    run_command, minimize_command = repro_commands(result, out_dir, source)
    pointers = (
        f"{_show_pointers(result.original_pointers)} → {_show_pointers(result.minimized_pointers)}"
    )
    return "\n".join(
        [
            "# Incident bundle",
            "",
            "This is a **local minimum**, not a proof of the globally smallest failing case.",
            "",
            f"- Source case: `{result.case_id}` (`{result.source}`)",
            f"- Failure class: `{result.status}`",
            f"- JSON pointers (original → minimized): {pointers}",
            f"- Bytes: schema {result.schema_before} → {result.schema_after}, "
            f"output {result.output_before} → {result.output_after}, "
            f"total {result.total_before} → {result.total_after}",
            f"- Operators applied: {len(result.reductions)}",
            "",
            "## Reproduction",
            "",
            "```",
            f"PYTHONPATH=src python3 -m {run_command}",
            f"PYTHONPATH=src python3 -m {minimize_command}",
            "```",
            "",
            "## Reduction log",
            "",
            "See `reduction.jsonl`.",
            "",
        ]
    )


def bytes_report(result: MinimizeResult) -> dict[str, int]:
    return {
        "output_after": result.output_after,
        "output_before": result.output_before,
        "schema_after": result.schema_after,
        "schema_before": result.schema_before,
        "total_after": result.total_after,
        "total_before": result.total_before,
    }


def write_bundle(result: MinimizeResult, out_dir: Path, source: str) -> Path:
    """Write the incident bundle. The source case directory is never touched."""
    out_dir = Path(out_dir)
    case_dir = out_dir / CASE_DIR
    source_dir = Path(result.source)
    try:
        resolved_source = source_dir.resolve()
        if out_dir.resolve() == resolved_source or case_dir.resolve() == resolved_source:
            raise MinimizeError(f"{out_dir}: refusing to write into the source case directory")
        case_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MinimizeError(f"{out_dir}: cannot create bundle directory ({exc})") from exc

    expected: dict[str, Any] = {"status": result.status}
    if result.minimized_pointers:
        expected["error_pointers"] = list(result.minimized_pointers)
    expected["description"] = f"Minimized from case {result.case_id} by {GENERATOR}."

    meta = {
        "generator": GENERATOR,
        "local_minimum": result.local_minimum,
        "minimized_status": result.status,
        "original_status": result.status,
        "source_id": result.case_id,
    }

    lines = [
        json.dumps(reduction.to_dict(), ensure_ascii=False, sort_keys=True)
        for reduction in result.reductions
    ]
    try:
        (case_dir / SCHEMA_FILE).write_bytes(schema_body(result.schema))
        (case_dir / OUTPUT_FILE).write_bytes(result.raw.encode("utf-8"))
        _write_json(case_dir / EXPECTED_FILE, expected)
        _write_json(case_dir / META_FILE, meta)
        (out_dir / REDUCTION_FILE).write_bytes(
            "".join(line + "\n" for line in lines).encode("utf-8")
        )
        _write_json(out_dir / BYTES_FILE, bytes_report(result))
        (out_dir / INCIDENT_FILE).write_bytes(
            render_incident(result, out_dir, source).encode("utf-8")
        )
    except OSError as exc:
        raise MinimizeError(f"{out_dir}: cannot write bundle ({exc})") from exc
    return out_dir


def render_result(result: MinimizeResult, out_dir: Path, source: str) -> str:
    """The human summary printed by ``crashlab minimize``."""
    run_command, minimize_command = repro_commands(result, out_dir, source)
    applied: list[str] = []
    for reduction in result.reductions:
        if reduction.op not in applied:
            applied.append(reduction.op)
    return "\n".join(
        [
            f"crashlab minimize -- {result.case_id} -- {result.status}",
            "",
            f"schema  {result.schema_before} -> {result.schema_after} bytes",
            f"output  {result.output_before} -> {result.output_after} bytes",
            f"total   {result.total_before} -> {result.total_after} bytes",
            f"pointers: {_show_pointers(result.original_pointers)}"
            f" -> {_show_pointers(result.minimized_pointers)}",
            f"{len(result.reductions)} reduction(s) over {result.passes} pass(es)"
            + (f" -- {', '.join(applied)}" if applied else ""),
            "",
            f"wrote {out_dir / CASE_DIR}, {out_dir / REDUCTION_FILE},"
            f" {out_dir / BYTES_FILE}, {out_dir / INCIDENT_FILE}",
            f"repro: {minimize_command}",
            f"       {run_command}",
            "",
            "local minimum for this operator list, not the globally smallest failing case",
        ]
    )
