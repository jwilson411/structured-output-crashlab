"""Human and JSON reports over classified cases.

The JSON shape is part of the contract; treat it as an API, not as debug
output. The human shape is for eyeballs and may gain detail lines.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from crashlab.cases import Case
from crashlab.classify import (
    SCHEMA_DRAFT,
    SCHEMA_INVALID,
    Classification,
    ValidationError,
    classify,
)

_STATUS_WIDTH = 16  # len("trailing_content")


@dataclass(frozen=True)
class CaseResult:
    """One classified case, compared against its ``expected.json``."""

    id: str
    status: str
    expected: str
    passed: bool
    errors: tuple[ValidationError, ...] = ()
    detail: str | None = None
    #: Why the case failed, or ``None`` when it passed.
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "expected": self.expected,
            "passed": self.passed,
            "errors": [error.to_dict() for error in self.errors],
        }


def evaluate(case: Case, result: Classification) -> CaseResult:
    """Compare a classification against the case's assertions."""
    reason: str | None = None
    if result.status != case.expected_status:
        reason = f"expected status {case.expected_status}, got {result.status}"
    elif case.expected_pointers is not None:
        actual = set(result.pointers)
        missing = [pointer for pointer in case.expected_pointers if pointer not in actual]
        if missing:
            reason = "missing expected error pointer(s): " + ", ".join(
                repr(pointer) for pointer in missing
            )

    return CaseResult(
        id=case.id,
        status=result.status,
        expected=case.expected_status,
        passed=reason is None,
        errors=result.errors,
        detail=result.detail,
        reason=reason,
    )


def run_cases(cases: Sequence[Case]) -> list[CaseResult]:
    """Classify and evaluate every case, in the order given."""
    return [evaluate(case, classify(case)) for case in cases]


def build_report(results: Sequence[CaseResult]) -> dict[str, Any]:
    """Build the stable JSON report."""
    return {
        "ok": all(result.passed for result in results),
        "draft": SCHEMA_DRAFT,
        "results": [result.to_dict() for result in results],
    }


def render_json(results: Sequence[CaseResult]) -> str:
    return json.dumps(build_report(results), indent=2, sort_keys=False)


def render_human(results: Sequence[CaseResult]) -> str:
    """Render the one-line-per-case report."""
    total = len(results)
    failed = [result for result in results if not result.passed]
    id_width = max((len(result.id) for result in results), default=0)

    lines = [f"structured-output-crashlab -- JSON Schema draft {SCHEMA_DRAFT} -- {total} case(s)", ""]
    for result in results:
        verdict = "PASS" if result.passed else "FAIL"
        lines.append(
            f"{verdict}  {result.id:<{id_width}}  {result.status:<{_STATUS_WIDTH}}"
            f"  (expected {result.expected})"
        )
        if not result.passed and result.reason:
            lines.append(f"        reason: {result.reason}")
        if not result.passed or result.status == SCHEMA_INVALID:
            for error in result.errors:
                pointer = error.pointer or "<root>"
                lines.append(f"        {pointer}: {error.message}")
        if result.detail:
            lines.append(f"        {result.detail}")

    lines.append("")
    lines.append(f"{total - len(failed)}/{total} passed")
    lines.append("ok" if not failed else "FAILED: " + ", ".join(result.id for result in failed))
    return "\n".join(lines)
