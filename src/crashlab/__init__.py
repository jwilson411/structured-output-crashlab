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
from crashlab.minimize import (
    OPERATOR_IDS,
    MinimizeError,
    MinimizeResult,
    Reduction,
    minimize_case,
    write_bundle,
)
from crashlab.mutate import (
    MUTATION_IDS,
    MutateError,
    MutationRun,
    generate,
    mutate_files,
)
from crashlab.report import CaseResult, build_report, render_human, run_cases
from crashlab.stream import (
    CHUNK_PLANS,
    STREAM_COMPLETE,
    STREAM_INCOMPLETE,
    STREAM_INVALID,
    STREAM_INVALID_UTF8,
    STREAM_STATES,
    STREAM_TRAILING,
    IncrementalJson,
    StreamError,
    StreamRun,
    StreamState,
    chunk_plan,
    feed_all,
    stream_case,
)

__version__ = "0.1.0"

__all__ = [
    "CHUNK_PLANS",
    "MUTATION_IDS",
    "OPERATOR_IDS",
    "SCHEMA_DRAFT",
    "SCHEMA_INVALID",
    "SCHEMA_VALID",
    "STATUSES",
    "STREAM_COMPLETE",
    "STREAM_INCOMPLETE",
    "STREAM_INVALID",
    "STREAM_INVALID_UTF8",
    "STREAM_STATES",
    "STREAM_TRAILING",
    "SYNTAX_INVALID",
    "TRAILING_CONTENT",
    "Case",
    "CaseError",
    "CaseResult",
    "Classification",
    "IncrementalJson",
    "MinimizeError",
    "MinimizeResult",
    "MutateError",
    "MutationRun",
    "Reduction",
    "SchemaError",
    "StreamError",
    "StreamRun",
    "StreamState",
    "__version__",
    "build_report",
    "chunk_plan",
    "classify",
    "classify_text",
    "feed_all",
    "generate",
    "load_cases",
    "minimize_case",
    "mutate_files",
    "render_human",
    "run_cases",
    "stream_case",
    "write_bundle",
]
