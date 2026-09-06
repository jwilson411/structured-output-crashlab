"""structured-output-crashlab: offline structured-output conformance runner.

Classifies raw, model-like text against a JSON Schema (Draft 2020-12) into one
of four stable status IDs. Nothing here calls a model, and nothing here repairs
output: the bytes under test are classified exactly as written.
"""

from crashlab.cases import Case, CaseError, load_cases
from crashlab.classify import (
    SCHEMA_DRAFT,
    SCHEMA_INVALID,
    SCHEMA_VALID,
    STATUSES,
    SYNTAX_INVALID,
    TRAILING_CONTENT,
    Classification,
    SchemaError,
    classify,
    classify_text,
)
from crashlab.mutate import (
    MUTATION_IDS,
    MutateError,
    MutationRun,
    generate,
    mutate_files,
)
from crashlab.report import CaseResult, build_report, render_human, run_cases

__version__ = "0.1.0"

__all__ = [
    "MUTATION_IDS",
    "SCHEMA_DRAFT",
    "SCHEMA_INVALID",
    "SCHEMA_VALID",
    "STATUSES",
    "SYNTAX_INVALID",
    "TRAILING_CONTENT",
    "Case",
    "CaseError",
    "CaseResult",
    "Classification",
    "MutateError",
    "MutationRun",
    "SchemaError",
    "__version__",
    "build_report",
    "classify",
    "classify_text",
    "generate",
    "load_cases",
    "mutate_files",
    "render_human",
    "run_cases",
]
