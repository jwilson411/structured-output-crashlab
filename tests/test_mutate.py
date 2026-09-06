"""Tests for ``crashlab mutate``: the CLI contract, applicability, determinism."""

from __future__ import annotations

import filecmp
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from crashlab import load_cases
from crashlab.cli import main
from crashlab.mutate import MUTATION_IDS
from crashlab.report import run_cases

GOLDEN_ROOT = REPO_ROOT / "tests" / "goldens" / "mutate"
GOLDEN_SCHEMA = GOLDEN_ROOT / "schema.json"
GOLDEN_INSTANCE = GOLDEN_ROOT / "instance.json"
GOLDEN_SEED = 20260905
GOLDEN_TREE = GOLDEN_ROOT / f"seed-{GOLDEN_SEED}"


def mutate(out: Path, *extra: str, schema: Path = GOLDEN_SCHEMA, instance: Path = GOLDEN_INSTANCE,
           seed: int | str | None = GOLDEN_SEED) -> int:
    argv = ["mutate", "--schema", str(schema), "--json", str(instance), "--out", str(out)]
    if seed is not None:
        argv += ["--seed", str(seed)]
    return main([*argv, *extra])


def case_ids(out: Path) -> list[str]:
    return sorted(path.name for path in out.iterdir() if path.is_dir())


def tree_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def generated(tmp_path):
    out = tmp_path / "out"
    assert mutate(out) == 0
    return out


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


def test_mutate_writes_cases_and_reports(tmp_path, capsys):
    out = tmp_path / "out"
    assert mutate(out) == 0
    report = capsys.readouterr().out
    assert f"seed {GOLDEN_SEED}" in report
    for case_id in case_ids(out):
        assert case_id in report
    assert "WROTE" in report


def test_missing_seed_exits_1(tmp_path, capsys):
    assert mutate(tmp_path / "out", seed=None) == 1
    assert "--seed" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_non_integer_seed_exits_1(tmp_path, capsys):
    assert mutate(tmp_path / "out", seed="later") == 1
    assert "must be an integer" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [["--max-cases", "0"], ["--max-cases", "x"], ["--mutations", "nope"]])
def test_bad_arguments_exit_1(tmp_path, capsys, flags):
    assert mutate(tmp_path / "out", *flags) == 1
    assert capsys.readouterr().err.startswith("crashlab:")


def test_unreadable_input_exits_1(tmp_path, capsys):
    assert mutate(tmp_path / "out", schema=tmp_path / "nope.json") == 1
    assert "cannot read schema" in capsys.readouterr().err


def test_instance_that_does_not_validate_exits_1(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"id": "A-1", "status": "nope"}), encoding="utf-8")
    assert mutate(tmp_path / "out", instance=bad) == 1
    assert "does not validate" in capsys.readouterr().err


def test_invalid_schema_exits_1(tmp_path, capsys):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": 7}), encoding="utf-8")
    assert mutate(tmp_path / "out", schema=schema) == 1
    assert "not a valid Draft 2020-12 schema" in capsys.readouterr().err


def test_sources_are_never_modified(tmp_path):
    before = {path: path.read_bytes() for path in (GOLDEN_SCHEMA, GOLDEN_INSTANCE)}
    assert mutate(tmp_path / "out") == 0
    assert {path: path.read_bytes() for path in before} == before


def test_refuses_to_overwrite_an_existing_case(tmp_path, capsys):
    out = tmp_path / "out"
    assert mutate(out) == 0
    kept = {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()}

    assert mutate(out) == 0
    report = capsys.readouterr().out
    assert report.count("SKIP") == len(MUTATION_IDS)
    assert "refusing to overwrite" in report
    assert {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()} == kept


def test_python_dash_m_entry_point(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable, "-m", "crashlab", "mutate",
            "--schema", str(GOLDEN_SCHEMA), "--json", str(GOLDEN_INSTANCE),
            "--seed", str(GOLDEN_SEED), "--out", str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert case_ids(out) == case_ids(GOLDEN_TREE)


# --------------------------------------------------------------------------
# Mutation coverage and applicability
# --------------------------------------------------------------------------


def test_every_mutation_kind_is_produced_for_the_rich_fixture(generated, capsys):
    produced = {
        json.loads((generated / case_id / "meta.json").read_text())["mutation_id"]
        for case_id in case_ids(generated)
    }
    assert produced == set(MUTATION_IDS)


def test_inapplicable_mutations_are_skipped_not_fabricated(tmp_path, capsys):
    schema = tmp_path / "plain-schema.json"
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    instance = tmp_path / "plain.json"
    instance.write_text(json.dumps({"name": "Ada"}), encoding="utf-8")

    out = tmp_path / "out"
    assert mutate(out, schema=schema, instance=instance) == 0
    report = capsys.readouterr().out

    assert "SKIP   enum_violation: schema has no enum" in report
    assert "SKIP   numeric_bounds:" in report
    assert "SKIP   string_bounds:" in report
    assert "SKIP   array_bounds:" in report
    assert "SKIP   missing_required:" in report
    assert not [case_id for case_id in case_ids(out) if "enum_violation" in case_id]


def test_mutations_flag_selects_only_those_kinds(tmp_path):
    out = tmp_path / "out"
    assert mutate(out, "--mutations", "missing_required,truncation") == 0
    assert [case_id.split("-")[1] for case_id in case_ids(out)] == ["missing_required", "truncation"]


def test_mutations_flag_does_not_shift_the_other_choices(tmp_path, generated):
    out = tmp_path / "subset"
    assert mutate(out, "--mutations", "enum_violation,truncation") == 0
    for case_id in case_ids(out):
        assert (out / case_id / "output.txt").read_bytes() == (
            generated / case_id / "output.txt"
        ).read_bytes()


def test_max_cases_caps_written_cases(tmp_path, capsys):
    out = tmp_path / "out"
    assert mutate(out, "--max-cases", "2") == 0
    assert len(case_ids(out)) == 2
    assert "--max-cases 2 reached" in capsys.readouterr().out


def test_max_cases_is_a_prefix_of_the_full_run(tmp_path, generated):
    out = tmp_path / "capped"
    assert mutate(out, "--max-cases", "2") == 0
    assert case_ids(out) == case_ids(generated)[:2]


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_two_runs_are_byte_for_byte_identical(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert mutate(first) == 0
    assert mutate(second) == 0
    assert case_ids(first) == case_ids(second)
    assert tree_files(first) == tree_files(second)


def test_run_matches_the_checked_in_golden_tree(generated):
    assert case_ids(generated) == case_ids(GOLDEN_TREE)
    assert tree_files(generated) == tree_files(GOLDEN_TREE)
    # filecmp is byte-exact too, and catches a file present on only one side.
    match, mismatch, errors = filecmp.cmpfiles(
        generated, GOLDEN_TREE, list(tree_files(GOLDEN_TREE)), shallow=False
    )
    assert (mismatch, errors) == ([], [])
    assert len(match) == len(tree_files(GOLDEN_TREE))


def test_a_different_seed_can_pick_different_candidates(tmp_path, generated):
    out = tmp_path / "other-seed"
    assert mutate(out, seed=1) == 0
    assert case_ids(out) != case_ids(generated)


# --------------------------------------------------------------------------
# Generated cases are real SOC-01 cases
# --------------------------------------------------------------------------


def test_generated_cases_load_and_pass_the_runner(generated, capsys):
    cases = load_cases(generated)
    assert len(cases) == len(MUTATION_IDS)
    assert all(result.passed for result in run_cases(cases))

    assert main(["run", str(generated)]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_generated_case_shape(generated):
    schema_bytes = GOLDEN_SCHEMA.read_bytes()
    for case_id in case_ids(generated):
        directory = generated / case_id
        assert (directory / "schema.json").read_bytes() == schema_bytes
        meta = json.loads((directory / "meta.json").read_text())
        assert meta["seed"] == GOLDEN_SEED
        assert meta["generator"] == "crashlab-mutate"
        assert meta["tags"] == ["generated", meta["mutation_id"]]
        assert meta["pointer"] == "" or meta["pointer"].startswith("/")
        assert "before" in meta and "after" in meta
        json.dumps(meta)  # provenance must stay JSON-serializable

        expected = json.loads((directory / "expected.json").read_text())
        assert expected["description"]
        if expected["status"] == "schema_invalid":
            assert expected["error_pointers"]


def test_duplicate_key_output_repeats_a_key_verbatim(generated):
    directory = next(path for path in generated.iterdir() if "duplicate_key" in path.name)
    raw = (directory / "output.txt").read_text()
    key = json.loads((directory / "meta.json").read_text())["duplicate_key"]
    assert raw.count(f'"{key}":') == 2
    # Still parses: json.loads keeps the last occurrence, which is the point.
    json.loads(raw)


def test_truncation_is_a_prefix_and_fails_to_parse(generated):
    directory = generated / "mut-truncation-root"
    raw = (directory / "output.txt").read_text()
    meta = json.loads((directory / "meta.json").read_text())
    original = meta["before"]
    assert original.startswith(raw)
    assert 0 < len(raw) < len(original)
    assert raw == original[: meta["cut_index"]]
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert json.loads((directory / "expected.json").read_text())["status"] == "syntax_invalid"
