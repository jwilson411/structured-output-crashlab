"""Tests for the reports and the ``crashlab`` CLI contract."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import CASES_ROOT, REPO_ROOT
from crashlab import load_cases
from crashlab.cli import main
from crashlab.report import build_report, render_human, run_cases
from test_cases import make_case


@pytest.fixture(scope="module")
def report():
    return build_report(run_cases(load_cases(CASES_ROOT)))


def test_json_report_shape(report):
    assert set(report) == {"ok", "draft", "results"}
    assert report["ok"] is True
    assert report["draft"] == "2020-12"
    assert len(report["results"]) >= 12
    for entry in report["results"]:
        assert set(entry) == {"id", "status", "expected", "passed", "errors"}
        assert entry["passed"] is True
        assert entry["status"] == entry["expected"]
        for error in entry["errors"]:
            assert set(error) == {"message", "pointer"}
            assert error["pointer"] == "" or error["pointer"].startswith("/")


def test_json_report_includes_pointers_for_invalid_cases(report):
    by_id = {entry["id"]: entry for entry in report["results"]}
    assert [error["pointer"] for error in by_id["array-item-invalid"]["errors"]] == ["/items/1/qty"]
    assert by_id["enum-valid"]["errors"] == []


def test_human_report_lines(report):
    text = render_human(run_cases(load_cases(CASES_ROOT)))
    assert "PASS  array-item-invalid" in text
    assert "(expected schema_invalid)" in text
    assert "/items/1/qty" in text
    assert text.rstrip().endswith("ok")


def test_cli_human_ok(capsys):
    assert main(["run", str(CASES_ROOT)]) == 0
    out = capsys.readouterr().out
    assert "JSON Schema draft 2020-12" in out
    assert "FAIL" not in out


def test_cli_json_flag_and_format_agree(capsys):
    assert main(["run", str(CASES_ROOT), "--json"]) == 0
    first = capsys.readouterr().out
    assert main(["run", str(CASES_ROOT), "--format", "json"]) == 0
    second = capsys.readouterr().out
    assert first == second
    assert json.loads(first)["ok"] is True


def test_cli_exits_1_on_mismatch(tmp_path, capsys):
    make_case(tmp_path, "wrong-expectation", expected={"status": "syntax_invalid"})
    assert main(["run", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL  wrong-expectation" in out
    assert "expected status syntax_invalid, got schema_valid" in out


def test_cli_exits_1_on_broken_case(tmp_path, capsys):
    directory = make_case(tmp_path, "broken")
    (directory / "expected.json").unlink()
    assert main(["run", str(tmp_path)]) == 1
    assert "missing required file expected.json" in capsys.readouterr().err


def test_cli_default_dir_is_cases(capsys):
    assert main(["run"]) == 0  # run from the repo root; see the cwd fixture below
    assert "case(s)" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)


def test_python_dash_m_entry_point():
    proc = subprocess.run(
        [sys.executable, "-m", "crashlab", "run", "cases", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
