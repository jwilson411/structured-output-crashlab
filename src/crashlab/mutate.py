"""Deterministic, schema-directed mutation of a valid instance into SOC-01 cases.

Given a JSON Schema (Draft 2020-12) and a JSON instance that validates against
it, this module derives new case directories -- each one a standalone fixture
in the layout :mod:`crashlab.cases` loads.

The rules are deliberately narrow:

* Nothing is generated. Every mutation is directed by a keyword that is
  actually present in the schema (``required``, ``enum``, ``maxItems``, ...) or
  by a serialization trick (``duplicate_key``, ``truncation``). A mutation kind
  with no candidate location is reported as inapplicable, never fabricated.
* The source ``--schema`` and ``--json`` files are read, never written.
* ``expected.json`` is filled in by running :func:`crashlab.classify.classify_text`
  on the bytes that were just written, so a fixture cannot drift away from the
  classifier.
* Output is reproducible: for a fixed seed the same cases are written, in the
  same order, byte for byte.

Determinism
-----------

Mutation kinds are processed in sorted ID order. Within one kind the candidate
locations are sorted by JSON Pointer and exactly one is picked with
``random.Random(f"{seed}:{mutation_id}").choice(...)``. Seeding per kind rather
than once per run means selecting a subset with ``--mutations`` does not shift
the choices made for the other kinds.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from crashlab.cases import EXPECTED_FILE, META_FILE, OUTPUT_FILE, SCHEMA_FILE
from crashlab.classify import SCHEMA_INVALID, SCHEMA_VALID, SYNTAX_INVALID, classify_text

#: Written into every generated ``meta.json``.
GENERATOR = "crashlab-mutate"

#: Property name inserted by :data:`UNKNOWN_FIELD`, suffixed if it collides.
UNKNOWN_FIELD_NAME = "__crashlab_unknown"

ARRAY_BOUNDS = "array_bounds"
DUPLICATE_KEY = "duplicate_key"
ENUM_VIOLATION = "enum_violation"
MISSING_REQUIRED = "missing_required"
NUMERIC_BOUNDS = "numeric_bounds"
STRING_BOUNDS = "string_bounds"
TRUNCATION = "truncation"
UNKNOWN_FIELD = "unknown_field"
WRONG_PRIMITIVE = "wrong_primitive"

#: The complete, stable mutation vocabulary, in the order cases are written.
MUTATION_IDS: tuple[str, ...] = (
    ARRAY_BOUNDS,
    DUPLICATE_KEY,
    ENUM_VIOLATION,
    MISSING_REQUIRED,
    NUMERIC_BOUNDS,
    STRING_BOUNDS,
    TRUNCATION,
    UNKNOWN_FIELD,
    WRONG_PRIMITIVE,
)

_SLUG_CLEAN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_SLUG_DIRTY = re.compile(r"[^a-z0-9]+")
_SLUG_LIMIT = 48


class MutateError(Exception):
    """Raised on unusable inputs: bad paths, bad schema, or a non-conforming instance."""


# --------------------------------------------------------------------------
# JSON Pointer (RFC 6901)
# --------------------------------------------------------------------------


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def join_pointer(parent: str, token: Any) -> str:
    return f"{parent}/{escape_token(str(token))}"


def parse_pointer(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [unescape_token(token) for token in pointer.split("/")[1:]]


def _child(container: Any, token: str) -> Any:
    return container[int(token)] if isinstance(container, list) else container[token]


def _resolve_parent(document: Any, tokens: Sequence[str]) -> Any:
    node = document
    for token in tokens[:-1]:
        node = _child(node, token)
    return node


def value_at(document: Any, pointer: str) -> Any:
    node = document
    for token in parse_pointer(pointer):
        node = _child(node, token)
    return node


def _set_at(document: Any, pointer: str, value: Any) -> None:
    tokens = parse_pointer(pointer)
    parent = _resolve_parent(document, tokens)
    if isinstance(parent, list):
        parent[int(tokens[-1])] = value
    else:
        parent[tokens[-1]] = value


def _delete_at(document: Any, pointer: str) -> None:
    tokens = parse_pointer(pointer)
    parent = _resolve_parent(document, tokens)
    if isinstance(parent, list):
        del parent[int(tokens[-1])]
    else:
        del parent[tokens[-1]]


# --------------------------------------------------------------------------
# Tiny schema walk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One (subschema, value) pair reached from the document root."""

    pointer: str
    schema: Mapping[str, Any]
    value: Any


def iter_nodes(schema: Any, value: Any, pointer: str = "") -> Iterator[Node]:
    """Walk schema and instance together, in a deterministic order.

    Only the keywords this generator needs are followed: ``properties`` for
    objects and a single ``items`` subschema for arrays. ``$ref``, ``allOf``
    and friends are not resolved -- the walk simply stops there, which shows
    up as fewer candidates, never as a crash.
    """
    if not isinstance(schema, Mapping):
        return
    yield Node(pointer, schema, value)
    if "$ref" in schema:
        return

    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key in sorted(value):
                subschema = properties.get(key)
                if isinstance(subschema, Mapping):
                    yield from iter_nodes(subschema, value[key], join_pointer(pointer, key))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                yield from iter_nodes(items, item, join_pointer(pointer, index))


def iter_object_pointers(value: Any, pointer: str = "") -> Iterator[str]:
    """Walk the instance alone, yielding a pointer for every non-empty object."""
    if isinstance(value, dict):
        if value:
            yield pointer
        for key in sorted(value):
            yield from iter_object_pointers(value[key], join_pointer(pointer, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_object_pointers(item, join_pointer(pointer, index))


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _declared_type(schema: Mapping[str, Any]) -> str | None:
    """The single declared ``type``, or ``None`` for a union / absent type."""
    declared = schema.get("type")
    return declared if isinstance(declared, str) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    # ``True == 1`` in Python but not in JSON, so compare booleans by type too.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _in_enum(value: Any, enum: Sequence[Any]) -> bool:
    return any(_json_equal(value, member) for member in enum)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutation:
    """One applied mutation: the bytes to write plus its provenance."""

    mutation_id: str
    pointer: str
    raw: str
    before: Any
    after: Any
    description: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """A location a mutation could be applied to."""

    pointer: str
    build: Callable[[], Mutation]


def _tree_mutation(
    instance: Any,
    mutation_id: str,
    pointer: str,
    edit: Callable[[Any], None],
    before: Any,
    after: Any,
    description: str,
    extra: dict[str, Any] | None = None,
) -> Mutation:
    mutated = copy.deepcopy(instance)
    edit(mutated)
    return Mutation(
        mutation_id=mutation_id,
        pointer=pointer,
        raw=_compact(mutated) + "\n",
        before=before,
        after=after,
        description=description,
        extra=extra or {},
    )


def _missing_required(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        required = node.schema.get("required")
        if not isinstance(node.value, dict) or not isinstance(required, list):
            continue
        for key in sorted(k for k in required if isinstance(k, str) and k in node.value):
            pointer = join_pointer(node.pointer, key)
            candidates.append(
                Candidate(
                    pointer,
                    lambda pointer=pointer, key=key: _tree_mutation(
                        instance,
                        MISSING_REQUIRED,
                        pointer,
                        lambda doc, pointer=pointer: _delete_at(doc, pointer),
                        before=value_at(instance, pointer),
                        after=None,
                        description=f"Required property {key!r} removed.",
                        extra={"removed_property": key},
                    ),
                )
            )
    return candidates


def _unknown_field(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    closed: list[Candidate] = []
    open_: list[Candidate] = []
    for node in nodes:
        if not isinstance(node.value, dict):
            continue
        properties = node.schema.get("properties")
        if not isinstance(properties, Mapping) and _declared_type(node.schema) != "object":
            continue
        declared = set(properties) if isinstance(properties, Mapping) else set()
        name = UNKNOWN_FIELD_NAME
        suffix = 0
        while name in declared or name in node.value:
            suffix += 1
            name = f"{UNKNOWN_FIELD_NAME}_{suffix}"
        pointer = join_pointer(node.pointer, name)
        candidate = Candidate(
            pointer,
            lambda pointer=pointer, name=name: _tree_mutation(
                instance,
                UNKNOWN_FIELD,
                pointer,
                lambda doc, pointer=pointer: _set_at(doc, pointer, True),
                before=None,
                after=True,
                description=f"Undeclared property {name!r} inserted.",
                extra={"inserted_property": name},
            ),
        )
        (closed if node.schema.get("additionalProperties") is False else open_).append(candidate)
    # A closed object gives a schema_invalid case; only fall back to an open one
    # (whose classification may well be schema_valid) when there is no choice.
    return closed or open_


_PRIMITIVE_SWAP: dict[str, Any] = {
    "string": 0,
    "number": "x",
    "integer": "x",
    "boolean": 0,
    "null": "x",
}


def _wrong_primitive(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        declared = _declared_type(node.schema)
        if declared not in _PRIMITIVE_SWAP:
            continue
        actual = _json_type(node.value)
        if actual != declared and not (declared == "number" and actual == "integer"):
            continue
        replacement = _PRIMITIVE_SWAP[declared]
        pointer = node.pointer
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, replacement=replacement, declared=declared: _tree_mutation(
                    instance,
                    WRONG_PRIMITIVE,
                    pointer,
                    lambda doc, pointer=pointer: _set_at(doc, pointer, replacement),
                    before=value_at(instance, pointer),
                    after=replacement,
                    description=(
                        f"Value declared as {declared!r} replaced with "
                        f"{_json_type(replacement)!r}."
                    ),
                ),
            )
        )
    return candidates


def _enum_breaker(enum: Sequence[Any], value: Any) -> Any:
    kind = _json_type(value)
    if kind == "string":
        replacement = "__crashlab_not_in_enum"
        while _in_enum(replacement, enum):
            replacement += "_x"
        return replacement
    if kind in ("integer", "number"):
        numbers = [member for member in enum if _is_number(member)]
        replacement = max(numbers) + 1 if numbers else 1
        return int(replacement) if kind == "integer" and float(replacement).is_integer() else replacement
    if kind == "boolean":
        flipped = not value
        return None if _in_enum(flipped, enum) else flipped
    # null, arrays and objects have no obvious same-type escape hatch.
    return None


def _enum_violation(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        enum = node.schema.get("enum")
        if not isinstance(enum, list) or not _in_enum(node.value, enum):
            continue
        replacement = _enum_breaker(enum, node.value)
        if replacement is None:
            continue
        pointer = node.pointer
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, replacement=replacement, enum=enum: _tree_mutation(
                    instance,
                    ENUM_VIOLATION,
                    pointer,
                    lambda doc, pointer=pointer: _set_at(doc, pointer, replacement),
                    before=value_at(instance, pointer),
                    after=replacement,
                    description="Enum-constrained value replaced with a same-type non-member.",
                    extra={"enum": list(enum)},
                ),
            )
        )
    return candidates


def _numeric_breaker(schema: Mapping[str, Any]) -> tuple[Any, str] | None:
    for keyword, offset in (("maximum", 1), ("exclusiveMaximum", 0), ("minimum", -1), ("exclusiveMinimum", 0)):
        bound = schema.get(keyword)
        if _is_number(bound):
            return bound + offset, keyword
    return None


def _numeric_bounds(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        if _declared_type(node.schema) not in ("number", "integer") or not _is_number(node.value):
            continue
        broken = _numeric_breaker(node.schema)
        if broken is None:
            continue
        replacement, keyword = broken
        if _declared_type(node.schema) == "integer" and float(replacement).is_integer():
            replacement = int(replacement)
        if _json_equal(replacement, node.value):
            continue
        pointer = node.pointer
        limit = node.schema.get(keyword)
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, replacement=replacement, keyword=keyword, limit=limit: _tree_mutation(
                    instance,
                    NUMERIC_BOUNDS,
                    pointer,
                    lambda doc, pointer=pointer: _set_at(doc, pointer, replacement),
                    before=value_at(instance, pointer),
                    after=replacement,
                    description=f"Numeric value pushed past {keyword}.",
                    extra={"bound": keyword, "limit": limit},
                ),
            )
        )
    return candidates


def _string_breaker(schema: Mapping[str, Any]) -> tuple[str, str] | None:
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool) and max_length >= 0:
        return "a" * (max_length + 1), "maxLength"
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and not isinstance(min_length, bool) and min_length >= 1:
        return "a" * (min_length - 1), "minLength"
    return None


def _string_bounds(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        if _declared_type(node.schema) != "string" or not isinstance(node.value, str):
            continue
        broken = _string_breaker(node.schema)
        if broken is None:
            continue
        replacement, keyword = broken
        if replacement == node.value:
            continue
        pointer = node.pointer
        limit = node.schema.get(keyword)
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, replacement=replacement, keyword=keyword, limit=limit: _tree_mutation(
                    instance,
                    STRING_BOUNDS,
                    pointer,
                    lambda doc, pointer=pointer: _set_at(doc, pointer, replacement),
                    before=value_at(instance, pointer),
                    after=replacement,
                    description=f"String length pushed past {keyword}.",
                    extra={"bound": keyword, "limit": limit},
                ),
            )
        )
    return candidates


def _array_breaker(schema: Mapping[str, Any], value: list[Any]) -> tuple[list[Any], str] | None:
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 0:
        filler = copy.deepcopy(value[-1]) if value else None
        grown = list(value)
        while len(grown) <= max_items:
            grown.append(copy.deepcopy(filler))
        return grown, "maxItems"
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and not isinstance(min_items, bool) and min_items >= 1:
        return list(value)[: min_items - 1], "minItems"
    return None


def _array_bounds(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for node in nodes:
        if _declared_type(node.schema) != "array" or not isinstance(node.value, list):
            continue
        broken = _array_breaker(node.schema, node.value)
        if broken is None:
            continue
        replacement, keyword = broken
        if replacement == node.value:
            continue
        pointer = node.pointer
        limit = node.schema.get(keyword)
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, replacement=replacement, keyword=keyword, limit=limit: _tree_mutation(
                    instance,
                    ARRAY_BOUNDS,
                    pointer,
                    lambda doc, pointer=pointer: _set_at(doc, pointer, replacement),
                    before=value_at(instance, pointer),
                    after=replacement,
                    description=f"Array length pushed past {keyword}.",
                    extra={"bound": keyword, "limit": limit},
                ),
            )
        )
    return candidates


def _dumps_with_duplicate(value: Any, tokens: Sequence[str], key: str) -> str:
    """Compact-serialize ``value``, repeating ``key`` in the object at ``tokens``."""
    if not tokens:
        parts = []
        for name, member in value.items():
            entry = f"{_compact(name)}:{_compact(member)}"
            parts.append(entry)
            if name == key:
                parts.append(entry)
        return "{" + ",".join(parts) + "}"

    head, rest = tokens[0], tokens[1:]
    if isinstance(value, list):
        index = int(head)
        parts = [
            _dumps_with_duplicate(item, rest, key) if position == index else _compact(item)
            for position, item in enumerate(value)
        ]
        return "[" + ",".join(parts) + "]"
    parts = []
    for name, member in value.items():
        rendered = _dumps_with_duplicate(member, rest, key) if name == head else _compact(member)
        parts.append(f"{_compact(name)}:{rendered}")
    return "{" + ",".join(parts) + "}"


def _duplicate_key(instance: Any, nodes: Sequence[Node]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for pointer in iter_object_pointers(instance):
        target = value_at(instance, pointer)
        key = sorted(target)[0]
        candidates.append(
            Candidate(
                pointer,
                lambda pointer=pointer, key=key: Mutation(
                    mutation_id=DUPLICATE_KEY,
                    pointer=pointer,
                    raw=_dumps_with_duplicate(instance, parse_pointer(pointer), key) + "\n",
                    before=_compact(instance),
                    after=_dumps_with_duplicate(instance, parse_pointer(pointer), key),
                    description=(
                        f"Object serialized with the key {key!r} repeated; json.loads keeps "
                        "the last occurrence, so the duplicate is silent."
                    ),
                    extra={"duplicate_key": key},
                ),
            )
        )
    return candidates


def _truncation(instance: Any, schema: Mapping[str, Any]) -> list[Candidate]:
    original = _compact(instance)
    if len(original) < 2:
        return []

    middle = len(original) // 2
    offsets = [0]
    for step in range(1, len(original)):
        offsets.extend((-step, step))
    for offset in offsets:
        cut = middle + offset
        if not 1 <= cut < len(original):
            continue
        text = original[:cut]
        if classify_text(text, schema).status != SYNTAX_INVALID:
            continue
        return [
            Candidate(
                "",
                lambda text=text, cut=cut: Mutation(
                    mutation_id=TRUNCATION,
                    pointer="",
                    raw=text,
                    before=original,
                    after=text,
                    description="Compact serialization cut mid-value; nothing is completed.",
                    extra={"cut_index": cut, "original_length": len(original)},
                ),
            )
        ]
    return []


def candidates_for(
    mutation_id: str, schema: Mapping[str, Any], instance: Any, nodes: Sequence[Node]
) -> list[Candidate]:
    """Every location ``mutation_id`` applies to, sorted by JSON Pointer."""
    if mutation_id == TRUNCATION:
        found = _truncation(instance, schema)
    else:
        found = _CANDIDATE_BUILDERS[mutation_id](instance, nodes)
    return sorted(found, key=lambda candidate: candidate.pointer)


_CANDIDATE_BUILDERS: dict[str, Callable[[Any, Sequence[Node]], list[Candidate]]] = {
    ARRAY_BOUNDS: _array_bounds,
    DUPLICATE_KEY: _duplicate_key,
    ENUM_VIOLATION: _enum_violation,
    MISSING_REQUIRED: _missing_required,
    NUMERIC_BOUNDS: _numeric_bounds,
    STRING_BOUNDS: _string_bounds,
    UNKNOWN_FIELD: _unknown_field,
    WRONG_PRIMITIVE: _wrong_primitive,
}

#: Why a mutation kind found nothing to do. Reported, never worked around.
_INAPPLICABLE: dict[str, str] = {
    ARRAY_BOUNDS: "no array with minItems/maxItems",
    DUPLICATE_KEY: "instance has no object with at least one property",
    ENUM_VIOLATION: "schema has no enum with a same-type non-member",
    MISSING_REQUIRED: "no required property is present in the instance",
    NUMERIC_BOUNDS: "no number/integer with a minimum/maximum bound",
    STRING_BOUNDS: "no string with minLength/maxLength",
    TRUNCATION: "no cut of the serialized instance fails to parse",
    UNKNOWN_FIELD: "instance has no object to insert a property into",
    WRONG_PRIMITIVE: "no field declares a single primitive type",
}


# --------------------------------------------------------------------------
# Case writing
# --------------------------------------------------------------------------


def case_slug(pointer: str) -> str:
    """A stable, filesystem-safe slug for a JSON Pointer."""
    if not pointer:
        return "root"
    clean = "-".join(parse_pointer(pointer)).lower()
    if _SLUG_CLEAN.fullmatch(clean) and len(clean) <= _SLUG_LIMIT:
        return clean
    digest = hashlib.sha256(pointer.encode("utf-8")).hexdigest()[:6]
    dirty = _SLUG_DIRTY.sub("-", clean).strip("-")[:_SLUG_LIMIT].strip("-")
    return f"{dirty}-{digest}" if dirty else digest


def case_id_for(mutation_id: str, pointer: str) -> str:
    return f"mut-{mutation_id}-{case_slug(pointer)}"


@dataclass(frozen=True)
class WrittenCase:
    """One case directory this run created."""

    case_id: str
    mutation_id: str
    pointer: str
    status: str
    path: Path


@dataclass(frozen=True)
class SkippedMutation:
    """A selected mutation kind that produced no case, and why."""

    mutation_id: str
    reason: str


@dataclass(frozen=True)
class MutationRun:
    """The result of one ``crashlab mutate`` invocation."""

    seed: int
    out_dir: Path
    written: tuple[WrittenCase, ...]
    skipped: tuple[SkippedMutation, ...]


def _write_json(path: Path, payload: Any) -> None:
    path.write_bytes((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def write_case(
    out_dir: Path, mutation: Mutation, schema_bytes: bytes, schema: Mapping[str, Any], seed: int
) -> WrittenCase:
    """Write one standalone SOC-01 case directory. Never overwrites."""
    case_id = case_id_for(mutation.mutation_id, mutation.pointer)
    directory = out_dir / case_id
    try:
        directory.mkdir(parents=True)
    except FileExistsError as exc:
        raise FileExistsError(f"{directory}: case directory already exists") from exc

    result = classify_text(mutation.raw, schema)

    expected: dict[str, Any] = {"status": result.status}
    if result.status == SCHEMA_INVALID:
        seen: list[str] = []
        for pointer in result.pointers:
            if pointer not in seen:
                seen.append(pointer)
        expected["error_pointers"] = seen
    expected["description"] = mutation.description

    meta: dict[str, Any] = {
        "seed": seed,
        "mutation_id": mutation.mutation_id,
        "pointer": mutation.pointer,
        "before": mutation.before,
        "after": mutation.after,
        "generator": GENERATOR,
        "tags": ["generated", mutation.mutation_id],
    }
    meta.update(mutation.extra)

    (directory / SCHEMA_FILE).write_bytes(schema_bytes)
    (directory / OUTPUT_FILE).write_bytes(mutation.raw.encode("utf-8"))
    _write_json(directory / EXPECTED_FILE, expected)
    _write_json(directory / META_FILE, meta)

    return WrittenCase(
        case_id=case_id,
        mutation_id=mutation.mutation_id,
        pointer=mutation.pointer,
        status=result.status,
        path=directory,
    )


def select_mutations(requested: Sequence[str] | None) -> list[str]:
    """Normalize a ``--mutations`` selection into sorted, de-duplicated IDs."""
    if requested is None:
        return list(MUTATION_IDS)
    unknown = sorted({name for name in requested if name not in MUTATION_IDS})
    if unknown:
        raise MutateError(
            f"unknown mutation ID(s) {unknown}; known IDs: {list(MUTATION_IDS)}"
        )
    return sorted(set(requested))


def generate(
    schema: Mapping[str, Any],
    schema_bytes: bytes,
    instance: Any,
    seed: int,
    out_dir: Path,
    mutations: Sequence[str] | None = None,
    max_cases: int | None = None,
) -> MutationRun:
    """Mutate ``instance`` and write one case per applicable mutation kind."""
    selected = select_mutations(mutations)
    nodes = list(iter_nodes(schema, instance))

    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MutateError(f"{out_dir}: cannot create output directory ({exc})") from exc

    written: list[WrittenCase] = []
    skipped: list[SkippedMutation] = []

    for mutation_id in selected:
        found = candidates_for(mutation_id, schema, instance, nodes)
        if not found:
            skipped.append(SkippedMutation(mutation_id, _INAPPLICABLE[mutation_id]))
            continue
        if max_cases is not None and len(written) >= max_cases:
            skipped.append(SkippedMutation(mutation_id, f"--max-cases {max_cases} reached"))
            continue

        rng = random.Random(f"{seed}:{mutation_id}")
        chosen = rng.choice(found)
        try:
            written.append(write_case(out_dir, chosen.build(), schema_bytes, schema, seed))
        except FileExistsError as exc:
            skipped.append(SkippedMutation(mutation_id, f"refusing to overwrite {exc}"))
        except OSError as exc:
            raise MutateError(f"{out_dir}: cannot write case ({exc})") from exc

    return MutationRun(seed=seed, out_dir=out_dir, written=tuple(written), skipped=tuple(skipped))


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _read_json_file(path: Path, label: str) -> tuple[bytes, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MutateError(f"{path}: cannot read {label} ({exc})") from exc
    try:
        return data, json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise MutateError(f"{path}: {label} is not valid UTF-8 ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise MutateError(f"{path}: {label} is not valid JSON ({exc})") from exc


def load_inputs(schema_path: Path, json_path: Path) -> tuple[bytes, dict[str, Any], Any]:
    """Read and check the schema and the instance. Neither file is written."""
    schema_bytes, schema = _read_json_file(Path(schema_path), "schema")
    if not isinstance(schema, dict):
        raise MutateError(f"{schema_path}: schema must be a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise MutateError(f"{schema_path}: not a valid Draft 2020-12 schema ({exc.message})") from exc

    _, instance = _read_json_file(Path(json_path), "instance")
    result = classify_text(_compact(instance), schema)
    if result.status != SCHEMA_VALID:
        pointers = ", ".join(pointer or "<root>" for pointer in result.pointers)
        raise MutateError(
            f"{json_path}: instance does not validate against {schema_path}"
            + (f" (at {pointers})" if pointers else "")
        )
    return schema_bytes, schema, instance


def mutate_files(
    schema_path: Path,
    json_path: Path,
    seed: int,
    out_dir: Path,
    mutations: Sequence[str] | None = None,
    max_cases: int | None = None,
) -> MutationRun:
    """Load the inputs, then :func:`generate` cases from them."""
    schema_bytes, schema, instance = load_inputs(Path(schema_path), Path(json_path))
    return generate(
        schema=schema,
        schema_bytes=schema_bytes,
        instance=instance,
        seed=seed,
        out_dir=Path(out_dir),
        mutations=mutations,
        max_cases=max_cases,
    )


def render_run(run: MutationRun) -> str:
    """One line per written case, then one line per skipped mutation kind."""
    lines = [
        f"crashlab mutate -- seed {run.seed} -- {len(run.written)} case(s) into {run.out_dir}",
        "",
    ]
    id_width = max((len(case.case_id) for case in run.written), default=0)
    mutation_width = max(
        (len(item.mutation_id) for item in (*run.written, *run.skipped)), default=0
    )
    for case in run.written:
        pointer = case.pointer or "<root>"
        lines.append(
            f"WROTE  {case.case_id:<{id_width}}  {case.mutation_id:<{mutation_width}}"
            f"  {pointer}  ({case.status})"
        )
    for item in run.skipped:
        lines.append(f"SKIP   {item.mutation_id}: {item.reason}")

    lines.append("")
    lines.append(f"{len(run.written)} written, {len(run.skipped)} skipped")
    return "\n".join(lines)
