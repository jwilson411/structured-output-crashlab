"""Tests for versioned case-directory loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crashlab.cases import CaseError, load_case, load_cases

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"status": {"enum": ["shipped"]}},
    "required": ["status"],
}


def make_case(
    root: Path,
    case_id: str,
    *,
    output: str = '{"status": "shipped"}\n',
    expected: dict | None = None,
    meta: dict | None = None,
    version: str = "v1",
) -> Path:
    directory = root / version / case_id
    directory.mkdir(parents=True)
    (directory / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (directory / "output.txt").write_bytes(output.encode("utf-8"))
    (directory / "expected.json").write_text(
        json.dumps(expected or {"status": "schema_valid"}), encoding="utf-8"
    )
    if meta is not None:
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory


def test_case_id_is_the_directory_name(tmp_path):
    make_case(tmp_path, "enum-valid", meta={"tags": ["enums"]})
    (case,) = load_cases(tmp_path)
    assert case.id == "enum-valid"
    assert case.version == "v1"
    assert case.metadata == {"tags": ["enums"]}


def test_metadata_file_is_optional(tmp_path):
    make_case(tmp_path, "no-meta")
    (case,) = load_cases(tmp_path)
    assert case.metadata == {}


def test_loads_from_root_version_dir_or_single_case(tmp_path):
    directory = make_case(tmp_path, "one")
    assert [case.id for case in load_cases(tmp_path)] == ["one"]
    assert [case.id for case in load_cases(tmp_path / "v1")] == ["one"]
    assert [case.id for case in load_cases(directory)] == ["one"]


def test_cases_are_sorted_by_id(tmp_path):
    for case_id in ("zebra", "alpha", "middle"):
        make_case(tmp_path, case_id)
    assert [case.id for case in load_cases(tmp_path)] == ["alpha", "middle", "zebra"]


def test_raw_output_is_not_normalised(tmp_path):
    directory = make_case(tmp_path, "crlf", output='{"status": "shipped"}\r\n')
    case = load_case(directory)
    assert case.raw == '{"status": "shipped"}\r\n'


def test_duplicate_case_ids_rejected(tmp_path):
    make_case(tmp_path, "dupe", version="v1")
    make_case(tmp_path, "dupe", version="v2")
    with pytest.raises(CaseError, match="duplicate case ID"):
        load_cases(tmp_path)


def test_missing_required_file_rejected(tmp_path):
    directory = make_case(tmp_path, "broken")
    (directory / "output.txt").unlink()
    with pytest.raises(CaseError, match="missing required file output.txt"):
        load_case(directory)


def test_unknown_status_rejected(tmp_path):
    directory = make_case(tmp_path, "bad-status", expected={"status": "mostly_fine"})
    with pytest.raises(CaseError, match="'status' must be one of"):
        load_case(directory)


def test_unknown_expected_key_rejected(tmp_path):
    directory = make_case(
        tmp_path, "typo", expected={"status": "schema_valid", "expected_status": "schema_valid"}
    )
    with pytest.raises(CaseError, match="unknown key"):
        load_case(directory)


def test_pointers_only_apply_to_schema_invalid(tmp_path):
    directory = make_case(
        tmp_path, "bad-pointers", expected={"status": "schema_valid", "error_pointers": ["/status"]}
    )
    with pytest.raises(CaseError, match="only applies to status 'schema_invalid'"):
        load_case(directory)


def test_empty_root_rejected(tmp_path):
    with pytest.raises(CaseError, match="no case directories found"):
        load_cases(tmp_path)


def test_missing_root_rejected(tmp_path):
    with pytest.raises(CaseError, match="not a directory"):
        load_cases(tmp_path / "nope")
