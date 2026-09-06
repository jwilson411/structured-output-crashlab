"""Command line interface: ``crashlab`` / ``python -m crashlab``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from crashlab.cases import CaseError, discover_case_dirs, load_case, load_cases
from crashlab.classify import SchemaError
from crashlab.minimize import MinimizeError, minimize_case, render_result, write_bundle
from crashlab.mutate import MUTATION_IDS, MutateError, mutate_files, render_run
from crashlab.report import render_human, render_json, run_cases
from crashlab.stream import CHUNK_PLANS, DEFAULT_SEED, StreamError, stream_case
from crashlab.stream import render_human as render_stream_human
from crashlab.stream import render_json as render_stream_json

DEFAULT_CASES_DIR = "cases"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crashlab",
        description=(
            "Offline structured-output conformance runner. Classifies raw outputs "
            "against a JSON Schema (Draft 2020-12). Never repairs, strips or "
            "extracts -- the bytes under test are classified as written."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help="classify every case in DIR and check it against expected.json",
        description="Classify every case in DIR and check it against expected.json.",
    )
    run.add_argument(
        "dir",
        nargs="?",
        default=DEFAULT_CASES_DIR,
        metavar="DIR",
        help=f"case root, a case directory or a tree of them (default: {DEFAULT_CASES_DIR})",
    )
    run.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="report format (default: human)",
    )
    run.add_argument(
        "--json",
        dest="json_shorthand",
        action="store_true",
        help="shorthand for --format json",
    )
    run.set_defaults(func=_cmd_run)

    mutate = subparsers.add_parser(
        "mutate",
        help="derive new cases by mutating a valid instance of a schema",
        description=(
            "Derive standalone cases from a schema and a valid instance of it. "
            "Deterministic given --seed; the source files are never modified and "
            "no model is called."
        ),
    )
    # Every required flag is checked by hand so that a bad invocation exits 1,
    # the documented failure code, rather than argparse's 2.
    mutate.add_argument("--schema", metavar="PATH", help="JSON Schema (Draft 2020-12)")
    mutate.add_argument("--json", dest="instance", metavar="PATH", help="a valid instance of --schema")
    mutate.add_argument("--seed", metavar="INT", help="integer seed; required, there is no default")
    mutate.add_argument("--out", metavar="DIR", help="output directory, created if missing")
    mutate.add_argument(
        "--mutations",
        metavar="ID,ID,...",
        help="comma-separated mutation IDs (default: all of " + ",".join(MUTATION_IDS) + ")",
    )
    mutate.add_argument("--max-cases", metavar="N", help="write at most N cases")
    mutate.set_defaults(func=_cmd_mutate)

    stream = subparsers.add_parser(
        "stream",
        help="feed a case through the incremental parser one chunk at a time",
        description=(
            "Stream a case's output.txt through the incremental JSON recognizer "
            "under a chunk plan, and check that the final SOC-01 status still "
            "matches expected.json. Chunk boundaries must not change the verdict."
        ),
    )
    # As with mutate, arguments are checked by hand so a bad invocation exits 1.
    stream.add_argument(
        "case",
        nargs="?",
        metavar="CASE",
        help="a case directory (cases/v1/enum-valid) or a case ID under ./cases",
    )
    stream.add_argument(
        "--chunk-plan",
        metavar="PLAN",
        help="one of " + ", ".join(CHUNK_PLANS) + "; required, there is no default",
    )
    stream.add_argument(
        "--seed",
        metavar="INT",
        help=f"seed for the 'seeded' plan (default: {DEFAULT_SEED})",
    )
    stream.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="report format (default: human)",
    )
    stream.set_defaults(func=_cmd_stream)

    minimize = subparsers.add_parser(
        "minimize",
        help="shrink a failing case to a local minimum and write an incident bundle",
        description=(
            "Shrink a failing case -- schema and output together -- while the SOC-01 "
            "classifier still reports the same failure class and one of the original "
            "JSON Pointers or error keywords. The result is a local minimum for the "
            "operator list, not the globally smallest failing case. The source case "
            "directory is only ever read."
        ),
    )
    # As with mutate and stream, arguments are checked by hand so a bad invocation exits 1.
    minimize.add_argument(
        "case",
        nargs="?",
        metavar="CASE",
        help="a case directory (cases/v1/required-missing) or a case ID under ./cases",
    )
    minimize.add_argument(
        "--out",
        metavar="DIR",
        help="bundle directory, created if missing; required, there is no default",
    )
    minimize.set_defaults(func=_cmd_minimize)
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    fmt = "json" if args.json_shorthand else args.format
    try:
        cases = load_cases(Path(args.dir))
        results = run_cases(cases)
    except (CaseError, SchemaError) as exc:
        print(f"crashlab: {exc}", file=sys.stderr)
        return 1

    if fmt == "json":
        print(render_json(results))
    else:
        print(render_human(results))
    return 0 if all(result.passed for result in results) else 1


def _int_arg(
    name: str,
    raw: str,
    minimum: int | None = None,
    error: type[Exception] = MutateError,
) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise error(f"{name} must be an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise error(f"{name} must be >= {minimum}, got {value}")
    return value


def _cmd_mutate(args: argparse.Namespace) -> int:
    try:
        missing = [
            flag
            for flag, value in (
                ("--schema", args.schema),
                ("--json", args.instance),
                ("--seed", args.seed),
                ("--out", args.out),
            )
            if value is None
        ]
        if missing:
            raise MutateError("missing required argument(s): " + ", ".join(missing))

        seed = _int_arg("--seed", args.seed)
        max_cases = None if args.max_cases is None else _int_arg("--max-cases", args.max_cases, 1)
        mutations = None
        if args.mutations is not None:
            mutations = [name.strip() for name in args.mutations.split(",") if name.strip()]
            if not mutations:
                raise MutateError("--mutations was empty")

        run = mutate_files(
            schema_path=Path(args.schema),
            json_path=Path(args.instance),
            seed=seed,
            out_dir=Path(args.out),
            mutations=mutations,
            max_cases=max_cases,
        )
    except (MutateError, SchemaError) as exc:
        print(f"crashlab: {exc}", file=sys.stderr)
        return 1

    print(render_run(run))
    return 0


def _resolve_case(reference: str) -> Path:
    """Accept a case directory path, or a bare case ID to look up under ./cases."""
    path = Path(reference)
    if path.is_dir():
        return path
    if not path.parts or len(path.parts) > 1:
        raise CaseError(f"{reference}: not a case directory")
    for candidate in discover_case_dirs(Path(DEFAULT_CASES_DIR)):
        if candidate.name == reference:
            return candidate
    raise CaseError(f"{reference}: no such case directory, and no case with that ID under ./cases")


def _cmd_stream(args: argparse.Namespace) -> int:
    try:
        missing = [
            flag
            for flag, value in (("CASE", args.case), ("--chunk-plan", args.chunk_plan))
            if value is None
        ]
        if missing:
            raise StreamError("missing required argument(s): " + ", ".join(missing))
        if args.chunk_plan not in CHUNK_PLANS:
            raise StreamError(
                f"unknown chunk plan {args.chunk_plan!r}; choose from {', '.join(CHUNK_PLANS)}"
            )

        seed = (
            DEFAULT_SEED
            if args.seed is None
            else _int_arg("--seed", args.seed, error=StreamError)
        )
        case = load_case(_resolve_case(args.case))
        run = stream_case(case, args.chunk_plan, seed)
    except (CaseError, SchemaError, StreamError) as exc:
        print(f"crashlab: {exc}", file=sys.stderr)
        return 1

    print(render_stream_json(run) if args.format == "json" else render_stream_human(run))
    return 0 if run.passed else 1


def _cmd_minimize(args: argparse.Namespace) -> int:
    try:
        missing = [
            flag for flag, value in (("CASE", args.case), ("--out", args.out)) if value is None
        ]
        if missing:
            raise MinimizeError("missing required argument(s): " + ", ".join(missing))

        case = load_case(_resolve_case(args.case))
        result = minimize_case(case)
        out_dir = write_bundle(result, Path(args.out), args.case)
    except (CaseError, SchemaError, MinimizeError) as exc:
        print(f"crashlab: {exc}", file=sys.stderr)
        return 1

    print(render_result(result, out_dir, args.case))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
