"""Tests for ``crashlab minimize``: the reducer, the bundle, and the CLI contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import CASES_ROOT, REPO_ROOT
from crashlab.cases import load_case
from crashlab.classify import classify_text
from crashlab.cli import main
from crashlab.minimize import CASE_DIR, MinimizeError, minimize_case, write_bundle

DRAFT = "https://json-schema.org/draft/2020-12/schema"

#: A large schema whose only actual failure is a missing nested required field.
NESTED_SCHEMA = {
    "$schema": DRAFT,
    "title": "support ticket",
    "type": "object",
    "$defs": {
        "Address": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
        },
        "Tag": {"type": "string", "maxLength": 40},
        "Unused": {"type": "object", "properties": {"nope": {"type": "boolean"}}},
    },
    "properties": {
        "user": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                    "required": ["name", "age"],
                }
            },
            "required": ["profile"],
        },
        "address": {"$ref": "#/$defs/Address"},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/Tag"}},
        "note": {"type": "string"},
        "priority": {"enum": ["low", "high"]},
    },
    "required": ["user"],
}

NESTED_OUTPUT = json.dumps(
    {
        "user": {"profile": {"name": "ada"}},
        "address": {"city": "springfield", "zip": "11111"},
        "tags": ["alpha", "beta", "gamma"],
        "note": "an irrelevant note",
        "priority": "low",
    }
)

#: Extra properties, unused ``$defs``, and one wrong enum member.
ENUM_SCHEMA = {
    "$schema": DRAFT,
    "title": "order status",
    "type": "object",
    "$defs": {
        "Priority": {"enum": ["low", "high"]},
        "Unused": {"type": "object", "properties": {"nope": {"type": "boolean"}}},
    },
    "properties": {
        "status": {"enum": ["pending", "shipped", "cancelled"]},
        "priority": {"$ref": "#/$defs/Priority"},
        "note": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status"],
}

ENUM_OUTPUT = json.dumps(
    {"status": "shippd", "priority": "low", "note": "an irrelevant note", "tags": ["a", "b"]}
)


def write_case_dir(directory: Path, schema: object, output: str, status: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.json").write_bytes((json.dumps(schema, indent=2) + "\n").encode("utf-8"))
    (directory / "output.txt").write_bytes(output.encode("utf-8"))
    (directory / "expected.json").write_bytes(
        (json.dumps({"status": status}, indent=2) + "\n").encode("utf-8")
    )
    return directory


def checksums(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def bundle_bytes(out_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(out_dir)): path.read_bytes()
        for path in sorted(out_dir.rglob("*"))
        if path.is_file()
    }


def minimize_dir(source: Path, out_dir: Path):
    result = minimize_case(load_case(source))
    write_bundle(result, out_dir, str(source))
    return result


def reduction_log(out_dir: Path) -> list[dict[str, object]]:
    text = (out_dir / "reduction.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


# --------------------------------------------------------------------------
# The reducer
# --------------------------------------------------------------------------


def test_nested_required_field_minimizes_and_still_fails(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    before = checksums(source)
    out_dir = tmp_path / "bundle"

    result = minimize_dir(source, out_dir)

    assert checksums(source) == before  # the source case is only ever read
    assert result.status == "schema_invalid"
    assert "/user/profile" in result.original_pointers
    assert "/user/profile" in result.minimized_pointers
    assert result.total_after < result.total_before

    schema = json.loads((out_dir / CASE_DIR / "schema.json").read_text(encoding="utf-8"))
    assert "$defs" not in schema  # every definition became unreferenced
    assert set(schema["properties"]) == {"user"}  # unused siblings are gone

    minimized = load_case(out_dir / CASE_DIR)
    assert json.loads(minimized.raw) == {"user": {"profile": {}}}

    again = classify_text(minimized.raw, minimized.schema)
    assert again.status == "schema_invalid"
    assert "/user/profile" in again.pointers
    assert any("age" in error.message for error in again.errors)
    assert main(["run", str(out_dir / CASE_DIR)]) == 0


def test_enum_failure_keeps_the_enum_violation(tmp_path):
    source = write_case_dir(tmp_path / "enum-wrong", ENUM_SCHEMA, ENUM_OUTPUT, "schema_invalid")
    out_dir = tmp_path / "bundle"

    result = minimize_dir(source, out_dir)

    assert result.status == "schema_invalid"
    assert result.minimized_pointers == ("/status",)

    schema = json.loads((out_dir / CASE_DIR / "schema.json").read_text(encoding="utf-8"))
    assert "$defs" not in schema
    assert set(schema["properties"]) == {"status"}
    assert "enum" in schema["properties"]["status"]

    minimized = load_case(out_dir / CASE_DIR)
    again = classify_text(minimized.raw, minimized.schema)
    assert again.status == "schema_invalid"
    assert "/status" in again.pointers
    assert any("is not one of" in error.message for error in again.errors)
    assert main(["run", str(out_dir / CASE_DIR)]) == 0


def test_trailing_content_shrinks_the_suffix_only(tmp_path):
    source = CASES_ROOT / "v1" / "trailing-text-after-value"
    before = checksums(source)
    out_dir = tmp_path / "bundle"

    result = minimize_dir(source, out_dir)

    assert checksums(source) == before  # the repo fixture is untouched
    assert result.status == "trailing_content"
    assert result.output_after < result.output_before
    assert any(entry.op == "trim_trailing_text" for entry in result.reductions)

    minimized = load_case(out_dir / CASE_DIR)
    assert minimized.raw.startswith("{")  # the JSON value is never stripped
    assert minimized.raw.rstrip()[-1] not in "}]"  # a suffix still follows it
    assert classify_text(minimized.raw, minimized.schema).status == "trailing_content"
    assert main(["run", str(out_dir / CASE_DIR)]) == 0


@pytest.mark.parametrize("case_id", ["truncated-object", "markdown-fenced-json"])
def test_syntax_invalid_is_never_repaired(tmp_path, case_id):
    source = CASES_ROOT / "v1" / case_id
    before = checksums(source)
    out_dir = tmp_path / case_id

    result = minimize_dir(source, out_dir)

    assert checksums(source) == before
    assert result.status == "syntax_invalid"
    assert result.schema_after < result.schema_before  # unused schema shrank

    minimized = load_case(out_dir / CASE_DIR)
    with pytest.raises(json.JSONDecodeError):
        json.loads(minimized.raw)
    assert classify_text(minimized.raw, minimized.schema).status == "syntax_invalid"
    if case_id == "markdown-fenced-json":
        assert minimized.raw.startswith("```")  # the fence is the failure, so it stays
    assert main(["run", str(out_dir / CASE_DIR)]) == 0


def test_local_minimum_is_stable_under_re_minimization(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    first = minimize_dir(source, tmp_path / "one")
    again = minimize_case(load_case(tmp_path / "one" / CASE_DIR))
    assert again.reductions == ()  # already at a local minimum
    assert again.total_after == first.total_after


# --------------------------------------------------------------------------
# The bundle
# --------------------------------------------------------------------------


def test_bundle_layout_and_counts(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    out_dir = tmp_path / "bundle"
    result = minimize_dir(source, out_dir)

    assert set(bundle_bytes(out_dir)) == {
        "INCIDENT.md",
        "bytes.json",
        "reduction.jsonl",
        f"{CASE_DIR}/schema.json",
        f"{CASE_DIR}/output.txt",
        f"{CASE_DIR}/expected.json",
        f"{CASE_DIR}/meta.json",
    }

    counts = json.loads((out_dir / "bytes.json").read_text(encoding="utf-8"))
    assert counts == {
        "schema_before": result.schema_before,
        "schema_after": result.schema_after,
        "output_before": result.output_before,
        "output_after": result.output_after,
        "total_before": result.total_before,
        "total_after": result.total_after,
    }
    assert counts["schema_after"] == len((out_dir / CASE_DIR / "schema.json").read_bytes())
    assert counts["output_after"] == len((out_dir / CASE_DIR / "output.txt").read_bytes())

    meta = json.loads((out_dir / CASE_DIR / "meta.json").read_text(encoding="utf-8"))
    assert meta == {
        "generator": "crashlab-minimize",
        "local_minimum": True,
        "minimized_status": "schema_invalid",
        "original_status": "schema_invalid",
        "source_id": "nested-required",
    }

    expected = json.loads((out_dir / CASE_DIR / "expected.json").read_text(encoding="utf-8"))
    assert expected["status"] == "schema_invalid"
    assert expected["error_pointers"] == list(result.minimized_pointers)

    log = reduction_log(out_dir)
    assert log and len(log) == len(result.reductions)
    assert log[0]["before_bytes"] == counts["total_before"]
    assert log[-1]["after_bytes"] == counts["total_after"]
    for line, entry in zip(log, result.reductions):
        assert set(line) == {"op", "target", "before_bytes", "after_bytes"}
        assert line["op"] == entry.op and line["target"] == entry.target
        assert line["after_bytes"] < line["before_bytes"]
    for previous, current in zip(log, log[1:]):
        assert previous["after_bytes"] == current["before_bytes"]  # application order


def test_incident_markdown_is_factual(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    out_dir = tmp_path / "bundle"
    result = minimize_dir(source, out_dir)
    text = (out_dir / "INCIDENT.md").read_text(encoding="utf-8")

    assert text.startswith("# Incident bundle\n")
    assert "**local minimum**" in text
    assert "- Source case: `nested-required`" in text
    assert "- Failure class: `schema_invalid`" in text
    assert "- JSON pointers (original → minimized): `/user/profile` → `/user/profile`" in text
    assert (
        f"- Bytes: schema {result.schema_before} → {result.schema_after}, "
        f"output {result.output_before} → {result.output_after}, "
        f"total {result.total_before} → {result.total_after}"
    ) in text
    assert f"- Operators applied: {len(result.reductions)}" in text
    assert "## Reproduction" in text
    assert f"crashlab minimize {source} --out {out_dir}" in text
    assert f"crashlab run {out_dir / CASE_DIR}" in text
    assert "## Reduction log" in text
    assert "See `reduction.jsonl`." in text


def test_identical_inputs_produce_byte_identical_bundles(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    out_dir = tmp_path / "bundle"

    minimize_dir(source, out_dir)
    first = bundle_bytes(out_dir)
    minimize_dir(source, out_dir)
    assert bundle_bytes(out_dir) == first

    # A different bundle directory only changes the paths quoted in INCIDENT.md.
    other = tmp_path / "elsewhere"
    minimize_dir(source, other)
    second = bundle_bytes(other)
    assert {name: data for name, data in second.items() if name != "INCIDENT.md"} == {
        name: data for name, data in first.items() if name != "INCIDENT.md"
    }


def test_write_bundle_refuses_the_source_directory(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    result = minimize_case(load_case(source))
    with pytest.raises(MinimizeError):
        write_bundle(result, source, str(source))


def test_source_case_files_are_not_written(tmp_path):
    source = write_case_dir(
        tmp_path / "nested-required", NESTED_SCHEMA, NESTED_OUTPUT, "schema_invalid"
    )
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(source.iterdir())
    }
    minimize_dir(source, tmp_path / "bundle")
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(source.iterdir())
    }
    assert after == before


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)


def test_cli_missing_arguments_exit_1(tmp_path, capsys):
    assert main(["minimize"]) == 1
    assert "missing required argument(s): CASE, --out" in capsys.readouterr().err

    assert main(["minimize", "enum-invalid"]) == 1
    assert "missing required argument(s): --out" in capsys.readouterr().err

    assert main(["minimize", "--out", str(tmp_path / "bundle")]) == 1
    assert "missing required argument(s): CASE" in capsys.readouterr().err


def test_cli_rejects_a_valid_case_and_a_bad_case(tmp_path, capsys):
    assert main(["minimize", "nested-object-valid", "--out", str(tmp_path / "a")]) == 1
    assert "schema_valid" in capsys.readouterr().err

    assert main(["minimize", "no-such-case", "--out", str(tmp_path / "b")]) == 1
    assert capsys.readouterr().err.startswith("crashlab: ")


def test_cli_success_writes_the_bundle(tmp_path, capsys):
    out_dir = tmp_path / "bundle"
    assert main(["minimize", "enum-invalid", "--out", str(out_dir)]) == 0
    out = capsys.readouterr().out

    assert "crashlab minimize -- enum-invalid -- schema_invalid" in out
    assert "local minimum" in out
    assert f"repro: crashlab minimize enum-invalid --out {out_dir}" in out
    assert set(bundle_bytes(out_dir)) >= {"INCIDENT.md", "bytes.json", "reduction.jsonl"}
    assert main(["run", str(out_dir / CASE_DIR)]) == 0
