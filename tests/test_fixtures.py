"""Every shipped fixture is a deterministic regression test.

If classification ever disagrees with a case's ``expected.json``, this file
fails -- which is what makes ``make test`` the guard on the status vocabulary.
"""

from __future__ import annotations

import pytest

from conftest import CASES_ROOT
from crashlab import STATUSES, load_cases
from crashlab.classify import SCHEMA_INVALID, SYNTAX_INVALID, TRAILING_CONTENT
from crashlab.report import evaluate, run_cases
from crashlab.classify import classify

ALL_CASES = load_cases(CASES_ROOT)
CASE_IDS = [case.id for case in ALL_CASES]


@pytest.mark.parametrize("case", ALL_CASES, ids=CASE_IDS)
def test_case_matches_expected(case):
    result = evaluate(case, classify(case))
    assert result.passed, result.reason


def test_fixture_suite_is_broad_enough():
    assert len(ALL_CASES) >= 12
    statuses = {evaluate(case, classify(case)).status for case in ALL_CASES}
    assert statuses == set(STATUSES), "every status ID should be exercised by a fixture"


def test_case_ids_are_unique_and_sorted():
    assert CASE_IDS == sorted(set(CASE_IDS))


def test_no_repair_rule_is_covered():
    by_id = {case.id: case for case in ALL_CASES}
    fenced = by_id["markdown-fenced-json"]
    prose = by_id["leading-prose-then-json"]

    assert fenced.raw.startswith("```json")
    assert classify(fenced).status == SYNTAX_INVALID
    assert prose.raw.startswith("Sure!")
    assert classify(prose).status == SYNTAX_INVALID


def test_expected_pointers_are_asserted_somewhere():
    asserted = {
        pointer
        for case in ALL_CASES
        for pointer in (case.expected_pointers or ())
    }
    assert "/items/1/qty" in asserted
    assert "/user/profile/age" in asserted
    assert "" in asserted  # document root


def test_truncation_and_trailing_are_distinct():
    by_id = {case.id: case for case in ALL_CASES}
    assert classify(by_id["truncated-object"]).status == SYNTAX_INVALID
    assert classify(by_id["trailing-text-after-value"]).status == TRAILING_CONTENT


def test_run_cases_preserves_order():
    results = run_cases(ALL_CASES)
    assert [result.id for result in results] == CASE_IDS
    assert all(result.passed for result in results)
    assert any(result.status == SCHEMA_INVALID for result in results)
