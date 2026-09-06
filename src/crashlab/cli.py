"""Command line interface: ``crashlab`` / ``python -m crashlab``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from crashlab.cases import CaseError, load_cases
from crashlab.classify import SchemaError
from crashlab.mutate import MUTATION_IDS, MutateError, mutate_files, render_run
from crashlab.report import render_human, render_json, run_cases

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


def _int_arg(name: str, raw: str, minimum: int | None = None) -> int:
    try:
        value = int(raw, 10)
    except ValueError:
        raise MutateError(f"{name} must be an integer, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise MutateError(f"{name} must be >= {minimum}, got {value}")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
