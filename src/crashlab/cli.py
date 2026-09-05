"""Command line interface: ``crashlab`` / ``python -m crashlab``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from crashlab.cases import CaseError, load_cases
from crashlab.classify import SchemaError
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
