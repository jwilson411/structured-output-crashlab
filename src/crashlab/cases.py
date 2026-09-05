"""Loading of versioned case directories.

A case directory holds the whole fixture::

    cases/v1/<case-id>/
        schema.json     # JSON Schema, Draft 2020-12
        output.txt      # the exact text under test, byte for byte
        expected.json   # asserted status (+ optional error pointers)
        meta.json       # optional free-form notes

The case ID is the directory name, which is what reports key on. Discovery is
recursive, so a root of ``cases``, ``cases/v1`` or a single case directory all
work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crashlab.classify import STATUSES

SCHEMA_FILE = "schema.json"
OUTPUT_FILE = "output.txt"
EXPECTED_FILE = "expected.json"
META_FILE = "meta.json"

_VERSION_DIR = re.compile(r"^v\d+$")
_EXPECTED_KEYS = frozenset({"status", "error_pointers", "description", "note"})


class CaseError(Exception):
    """Raised when a case directory is missing or malformed."""


@dataclass(frozen=True)
class Case:
    """One fixture: a schema, the raw text under test, and the assertion."""

    id: str
    path: Path
    schema: dict[str, Any]
    raw: str
    expected_status: str
    #: Pointers that must appear among the reported errors, or ``None`` when
    #: the fixture does not assert on pointers.
    expected_pointers: tuple[str, ...] | None = None
    version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> Any:
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise CaseError(f"{path}: cannot read ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise CaseError(f"{path}: not valid UTF-8 ({exc})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaseError(f"{path}: not valid JSON ({exc})") from exc


def _read_expected(path: Path) -> tuple[str, tuple[str, ...] | None]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise CaseError(f"{path}: must contain a JSON object")

    unknown = sorted(set(data) - _EXPECTED_KEYS)
    if unknown:
        raise CaseError(f"{path}: unknown key(s) {unknown}; allowed: {sorted(_EXPECTED_KEYS)}")

    status = data.get("status")
    if status not in STATUSES:
        raise CaseError(f"{path}: 'status' must be one of {list(STATUSES)}, got {status!r}")

    if "error_pointers" not in data:
        return status, None

    pointers = data["error_pointers"]
    if not isinstance(pointers, list) or not all(isinstance(p, str) for p in pointers):
        raise CaseError(f"{path}: 'error_pointers' must be a list of JSON Pointer strings")
    if pointers and status != "schema_invalid":
        raise CaseError(f"{path}: 'error_pointers' only applies to status 'schema_invalid'")
    return status, tuple(pointers)


def load_case(path: Path) -> Case:
    """Load a single case directory."""
    path = Path(path)
    for name in (SCHEMA_FILE, OUTPUT_FILE, EXPECTED_FILE):
        if not (path / name).is_file():
            raise CaseError(f"{path}: missing required file {name}")

    schema = _read_json(path / SCHEMA_FILE)
    if not isinstance(schema, (dict, bool)):
        raise CaseError(f"{path / SCHEMA_FILE}: must contain a JSON Schema object")

    # Bytes, not str: universal-newline translation would edit the fixture.
    try:
        raw = (path / OUTPUT_FILE).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaseError(f"{path / OUTPUT_FILE}: not valid UTF-8 ({exc})") from exc

    status, pointers = _read_expected(path / EXPECTED_FILE)

    metadata: dict[str, Any] = {}
    if (path / META_FILE).is_file():
        loaded = _read_json(path / META_FILE)
        if not isinstance(loaded, dict):
            raise CaseError(f"{path / META_FILE}: must contain a JSON object")
        metadata = loaded

    parent = path.parent.name
    return Case(
        id=path.name,
        path=path,
        schema=schema,
        raw=raw,
        expected_status=status,
        expected_pointers=pointers,
        version=parent if _VERSION_DIR.match(parent) else None,
        metadata=metadata,
    )


def discover_case_dirs(root: Path) -> list[Path]:
    """Find every case directory at or under ``root``, sorted by case ID."""
    root = Path(root)
    if not root.is_dir():
        raise CaseError(f"{root}: not a directory")

    found: list[Path] = []
    if (root / SCHEMA_FILE).is_file():
        found.append(root)
    else:
        for child in sorted(root.rglob("*")):
            if child.is_dir() and not child.name.startswith(".") and (child / SCHEMA_FILE).is_file():
                found.append(child)
    return sorted(found, key=lambda path: path.name)


def load_cases(root: Path) -> list[Case]:
    """Load every case at or under ``root``, sorted by case ID."""
    dirs = discover_case_dirs(root)
    if not dirs:
        raise CaseError(f"{root}: no case directories found (looked for */{SCHEMA_FILE})")

    cases = [load_case(path) for path in dirs]
    seen: dict[str, Path] = {}
    for case in cases:
        if case.id in seen:
            raise CaseError(f"duplicate case ID {case.id!r}: {seen[case.id]} and {case.path}")
        seen[case.id] = case.path
    return cases
