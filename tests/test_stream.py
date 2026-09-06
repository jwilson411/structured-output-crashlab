"""Tests for the incremental JSON recognizer, its chunk plans and ``crashlab stream``.

Three layers:

* the boundary fixtures in ``tests/stream_fixtures/boundary.json``, each a fixed
  chunking of a fixed byte string, so a failure is reproducible byte for byte;
* bounded property tests -- every chunking of the same valid payload must reach
  the same final status and the same value;
* the CLI contract.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CASES_ROOT, REPO_ROOT
from crashlab.cases import load_cases
from crashlab.classify import SCHEMA_VALID, SYNTAX_INVALID, TRAILING_CONTENT, classify_text
from crashlab.cli import main
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
    chunk_plan,
    feed_all,
)

FIXTURE_FILE = Path(__file__).parent / "stream_fixtures" / "boundary.json"
ANY_SCHEMA: dict = {}


def _chunk_bytes(entry) -> bytes:
    if isinstance(entry, str):
        return entry.encode("utf-8")
    return bytes.fromhex(entry["hex"])


def _load_fixtures():
    doc = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    for fixture in doc["fixtures"]:
        fixture["chunk_bytes"] = [_chunk_bytes(entry) for entry in fixture["chunks"]]
    return doc["fixtures"]


FIXTURES = _load_fixtures()
FIXTURE_IDS = [fixture["id"] for fixture in FIXTURES]


# -- boundary fixtures ------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_boundary_fixture(fixture):
    parser = IncrementalJson(fixture["schema"])
    states = [parser.feed(chunk) for chunk in fixture["chunk_bytes"]]
    final = parser.finish()

    assert [state.status for state in states] == fixture["feed_states"]
    assert final.status == fixture["final_status"]
    assert final.soc1_status == fixture["soc1_status"]
    if "value" in fixture:
        assert final.value == fixture["value"]
    if "error_pointers" in fixture:
        assert list(final.classification.pointers) == fixture["error_pointers"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_boundary_fixture_agrees_with_soc1(fixture):
    """Whatever the chunking, the stream must land on the SOC-01 status of the same bytes."""
    data = b"".join(fixture["chunk_bytes"])
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        assert fixture["final_status"] == STREAM_INVALID_UTF8
        return
    assert classify_text(text, fixture["schema"]).status == fixture["soc1_status"]


def test_fixture_suite_is_broad_enough():
    assert len(FIXTURES) >= 15
    assert {fixture["covers"] for fixture in FIXTURES} == {
        "structural",
        "token",
        "encoding",
        "trailing",
    }
    assert {fixture["final_status"] for fixture in FIXTURES} == set(STREAM_STATES) - {
        STREAM_INCOMPLETE
    }


def test_fixture_ids_are_unique():
    assert len(set(FIXTURE_IDS)) == len(FIXTURE_IDS)


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_incomplete_never_carries_a_value(fixture):
    parser = IncrementalJson(fixture["schema"])
    for chunk in fixture["chunk_bytes"]:
        state = parser.feed(chunk)
        if state.status == STREAM_INCOMPLETE:
            assert state.value is None
            assert state.classification is None


# -- the recognizer directly ------------------------------------------------


def test_complete_is_not_emitted_before_the_value_closes():
    parser = IncrementalJson(ANY_SCHEMA)
    for byte in b'{"a": [1, 2], "b": {"c": "d"}':
        assert parser.feed(bytes([byte])).status == STREAM_INCOMPLETE
    assert parser.feed(b"}").status == STREAM_COMPLETE


def test_number_is_not_complete_until_a_delimiter_or_finish():
    parser = IncrementalJson(ANY_SCHEMA)
    assert parser.feed(b"1").status == STREAM_INCOMPLETE
    assert parser.feed(b"2").status == STREAM_INCOMPLETE
    final = parser.finish()
    assert final.status == STREAM_COMPLETE
    assert final.value == 12


def test_number_completes_on_a_following_whitespace_chunk():
    states, final = feed_all([b"12", b" "], ANY_SCHEMA)
    assert [state.status for state in states] == [STREAM_INCOMPLETE, STREAM_COMPLETE]
    assert final.value == 12


def test_finish_of_a_truncated_value_is_invalid_and_says_so():
    parser = IncrementalJson(ANY_SCHEMA)
    assert parser.feed(b'{"a": [1,').status == STREAM_INCOMPLETE
    final = parser.finish()
    assert final.status == STREAM_INVALID
    assert "truncated" in final.detail
    assert final.soc1_status == SYNTAX_INVALID


def test_finish_is_idempotent():
    parser = IncrementalJson(ANY_SCHEMA)
    parser.feed(b"[1]")
    assert parser.finish() == parser.finish()


def test_feed_after_finish_is_refused():
    parser = IncrementalJson(ANY_SCHEMA)
    parser.feed(b"[1]")
    parser.finish()
    with pytest.raises(StreamError):
        parser.feed(b" ")


def test_empty_chunks_change_nothing():
    plain, _ = feed_all([b'{"a"', b": 1}"], ANY_SCHEMA)
    padded, _ = feed_all([b"", b'{"a"', b"", b": 1}", b""], ANY_SCHEMA)
    assert [state.status for state in padded] == [
        STREAM_INCOMPLETE,
        STREAM_INCOMPLETE,
        STREAM_INCOMPLETE,
        STREAM_COMPLETE,
        STREAM_COMPLETE,
    ]
    assert [state.status for state in plain] == [STREAM_INCOMPLETE, STREAM_COMPLETE]


def test_trailing_detail_names_everything_after_the_value():
    _, final = feed_all([b'{"a": 1} ', b"and then ", b"some prose"], ANY_SCHEMA)
    assert final.status == STREAM_TRAILING
    assert final.detail == "extra content after value: 'and then some prose'"
    assert final.classification.status == TRAILING_CONTENT


def test_no_schema_means_no_classification_but_still_a_value():
    _, final = feed_all([b"[1, 2]"])
    assert final.status == STREAM_COMPLETE
    assert final.value == [1, 2]
    assert final.classification is None
    assert final.soc1_status == SYNTAX_INVALID  # documented projection when unclassified


@pytest.mark.parametrize(
    "text",
    ["NaN", "Infinity", "-Infinity", "{'a': 1}", "{a: 1}", '{"a": 1,}', "[1 2]", "01x", "+1"],
)
def test_python_and_prose_json_extensions_are_rejected(text):
    _, final = feed_all([text.encode("utf-8")], ANY_SCHEMA)
    assert final.status in (STREAM_INVALID, STREAM_TRAILING)
    assert final.soc1_status == classify_text(text, ANY_SCHEMA).status


def test_no_repair_of_a_markdown_fence():
    _, final = feed_all([b'```json\n{"a": 1}\n```'], ANY_SCHEMA)
    assert final.status == STREAM_INVALID
    assert final.soc1_status == SYNTAX_INVALID


# -- chunk plans ------------------------------------------------------------

PAYLOADS = [
    b"{}",
    b"[]",
    b'{"name": "ada", "tags": ["x", "y"], "n": -12.5e3, "ok": true, "z": null}',
    b'[1, [2, [3, [4]]], {"a": {"b": {"c": []}}}]',
    b'"caf\xc3\xa9 \xf0\x9f\x98\x80 \\" \\\\ \\u0041"',
    b"  -0.5e-2  ",
    b"true",
    b"null",
    b'{"deep": {"quote": "}{][,:", "esc": "a\\/b\\tc"}}',
]


@pytest.mark.parametrize("plan", CHUNK_PLANS)
@pytest.mark.parametrize("payload", PAYLOADS, ids=range(len(PAYLOADS)))
def test_chunk_plans_partition_the_bytes_exactly(plan, payload):
    chunks = chunk_plan(plan, payload, seed=3)
    assert b"".join(chunks) == payload


def test_one_byte_plan_is_one_byte_per_chunk():
    chunks = chunk_plan("one-byte", b'{"a": 1}')
    assert all(len(chunk) == 1 for chunk in chunks)
    assert len(chunks) == 8


def test_boundary_plan_splits_multibyte_sequences_and_emits_empty_chunks():
    chunks = chunk_plan("boundary-focused", "[1, é]".encode("utf-8"))
    assert b"" in chunks
    assert b"\xc3" in chunks and b"\xa9" in chunks
    assert b" " in chunks  # whitespace-only chunks really occur


def test_boundary_plan_splits_around_escapes():
    chunks = chunk_plan("boundary-focused", b'"a\\u0041b"')
    assert b"\\" in chunks  # the backslash is alone in its own chunk
    assert b"u" in chunks  # ... and \uXXXX is split after the 'u'


def test_plans_are_deterministic():
    for plan in CHUNK_PLANS:
        assert chunk_plan(plan, PAYLOADS[2], seed=7) == chunk_plan(plan, PAYLOADS[2], seed=7)


def test_seeded_plan_varies_with_the_seed_and_includes_empty_chunks():
    a = chunk_plan("seeded", PAYLOADS[2], seed=0)
    b = chunk_plan("seeded", PAYLOADS[2], seed=1)
    assert a != b
    assert b"" in a
    assert max(len(chunk) for chunk in a) > 1


def test_seeded_plan_defaults_to_seed_zero():
    assert chunk_plan("seeded", PAYLOADS[2]) == chunk_plan("seeded", PAYLOADS[2], seed=0)


def test_unknown_plan_is_an_error():
    with pytest.raises(StreamError):
        chunk_plan("every-other-byte", b"1")


@pytest.mark.parametrize("plan", CHUNK_PLANS)
def test_plans_handle_empty_input(plan):
    assert chunk_plan(plan, b"", seed=1) == []


# -- property tests ---------------------------------------------------------

SEEDS = range(21)

SCHEMAS: dict[bytes, dict] = {
    PAYLOADS[0]: {"type": "object"},
    PAYLOADS[1]: {"type": "array"},
    PAYLOADS[2]: {"type": "object", "required": ["name", "tags", "n", "ok", "z"]},
    PAYLOADS[3]: {"type": "array", "minItems": 3},
    PAYLOADS[4]: {"type": "string", "minLength": 1},
    PAYLOADS[5]: {"type": "number", "maximum": 0},
    PAYLOADS[6]: {"type": "boolean"},
    PAYLOADS[7]: {"type": "null"},
    PAYLOADS[8]: {"type": "object", "required": ["deep"]},
}


def _random_chunkings(payload: bytes, seed: int, count: int = 6) -> list[list[bytes]]:
    """Uniformly random cut sets over ``payload``, from stdlib ``random`` only."""
    rng = random.Random(seed)
    chunkings = []
    for _ in range(count):
        cuts = sorted({0, len(payload)} | {rng.randrange(len(payload) + 1) for _ in range(4)})
        chunks: list[bytes] = []
        for index, start in enumerate(cuts[:-1]):
            if rng.random() < 0.3:
                chunks.append(b"")  # a transport frame that carried no payload
            chunks.append(payload[start : cuts[index + 1]])
        chunkings.append(chunks)
    return chunkings


def _all_chunkings(payload: bytes) -> list[tuple[str, list[bytes]]]:
    plans: list[tuple[str, list[bytes]]] = [
        ("one-byte", chunk_plan("one-byte", payload)),
        ("boundary-focused", chunk_plan("boundary-focused", payload)),
        ("whole", [payload]),
    ]
    for seed in SEEDS:
        plans.append((f"seeded:{seed}", chunk_plan("seeded", payload, seed=seed)))
        for index, chunks in enumerate(_random_chunkings(payload, seed)):
            plans.append((f"random:{seed}:{index}", chunks))
    return plans


@pytest.mark.parametrize("payload", PAYLOADS, ids=range(len(PAYLOADS)))
def test_every_chunking_of_a_valid_payload_is_schema_valid(payload):
    schema = SCHEMAS[payload]
    expected = json.loads(payload)
    chunkings = _all_chunkings(payload)
    assert len(chunkings) >= 100

    for label, chunks in chunkings:
        assert b"".join(chunks) == payload, label
        states, final = feed_all(chunks, schema)
        assert final.status == STREAM_COMPLETE, (label, final.detail)
        assert final.value == expected, label
        assert final.classification.status == SCHEMA_VALID, (label, final.classification)
        # A value is never announced before it is structurally finished.
        for state in states:
            if state.status == STREAM_INCOMPLETE:
                assert state.value is None, label


@pytest.mark.parametrize("payload", PAYLOADS, ids=range(len(PAYLOADS)))
def test_chunk_boundaries_never_change_the_outcome(payload):
    schema = SCHEMAS[payload]
    outcomes = set()
    for _, chunks in _all_chunkings(payload):
        _, final = feed_all(chunks, schema)
        outcomes.add((final.status, final.classification.status, json.dumps(final.value)))
    assert len(outcomes) == 1


TRUNCATED = [b'{"a": 1', b'[1, 2', b'"unter', b"tru", b"-", b"", b"   "]


@pytest.mark.parametrize("payload", TRUNCATED, ids=range(len(TRUNCATED)))
def test_every_chunking_of_a_truncated_payload_is_invalid_at_finish(payload):
    for label, chunks in _all_chunkings(payload):
        states, final = feed_all(chunks, ANY_SCHEMA)
        assert all(state.status == STREAM_INCOMPLETE for state in states), label
        assert final.status == STREAM_INVALID, label
        assert final.soc1_status == SYNTAX_INVALID, label


# -- agreement with SOC-01 --------------------------------------------------

_ALPHABET = list('{}[],:"\\0123456789.eE+-truefalsnl \t\n\rxéu')


def test_recognizer_agrees_with_classify_text_on_random_token_soup():
    """The stream and the offline classifier must never disagree about the same bytes."""
    rng = random.Random(20260906)
    for index in range(2000):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randrange(13)))
        expected = classify_text(text, ANY_SCHEMA)
        plan = CHUNK_PLANS[index % len(CHUNK_PLANS)]
        chunks = chunk_plan(plan, text.encode("utf-8"), seed=index % 5)
        _, final = feed_all(chunks, ANY_SCHEMA)
        assert final.soc1_status == expected.status, (text, plan, final.status, final.detail)
        if expected.status in (SCHEMA_VALID, TRAILING_CONTENT):
            assert final.value == expected.value, text


# -- the CLI ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)


@pytest.mark.parametrize("plan", CHUNK_PLANS)
def test_cli_streams_a_case_by_path(plan, capsys):
    assert main(["stream", "cases/v1/enum-valid", "--chunk-plan", plan]) == 0
    out = capsys.readouterr().out
    assert f"plan {plan}" in out
    assert "soc1: schema_valid  (expected schema_valid)" in out
    assert out.rstrip().endswith("ok")


def test_cli_accepts_a_bare_case_id(capsys):
    assert main(["stream", "enum-valid", "--chunk-plan", "one-byte"]) == 0
    assert "stream enum-valid" in capsys.readouterr().out


@pytest.mark.parametrize("plan", CHUNK_PLANS)
def test_cli_matches_expected_json_for_every_shipped_case(plan, capsys):
    """Streaming a case must reach the same verdict `crashlab run` reaches offline."""
    for case in load_cases(CASES_ROOT):
        assert main(["stream", str(case.path), "--chunk-plan", plan]) == 0, case.id
        assert "FAILED" not in capsys.readouterr().out


def test_cli_json_report_shape(capsys):
    assert main(["stream", "enum-valid", "--chunk-plan", "seeded", "--seed", "5", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {
        "ok",
        "case",
        "plan",
        "seed",
        "chunks",
        "bytes",
        "feed_states",
        "final",
        "status",
        "expected",
    }
    assert report["ok"] is True
    assert report["seed"] == 5
    assert report["status"] == report["expected"] == "schema_valid"
    assert set(report["final"]) == {"status", "soc1_status", "detail"}
    assert len(report["feed_states"]) == report["chunks"]
    assert set(report["feed_states"]) <= set(STREAM_STATES)


def test_cli_json_seed_is_null_for_unseeded_plans(capsys):
    assert main(["stream", "enum-valid", "--chunk-plan", "one-byte", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["seed"] is None


def test_cli_reports_the_truncation_detail(capsys):
    assert main(["stream", "truncated-object", "--chunk-plan", "one-byte"]) == 0
    out = capsys.readouterr().out
    assert "final: invalid" in out
    assert "truncated:" in out
    assert "soc1: syntax_invalid" in out


def test_cli_exits_1_when_the_stream_disagrees_with_expected_json(tmp_path, capsys):
    case = tmp_path / "wrong-expectation"
    case.mkdir()
    (case / "schema.json").write_text('{"type": "object"}', encoding="utf-8")
    (case / "output.txt").write_text('{"a": 1}', encoding="utf-8")
    (case / "expected.json").write_text('{"status": "syntax_invalid"}', encoding="utf-8")

    assert main(["stream", str(case), "--chunk-plan", "one-byte"]) == 1
    out = capsys.readouterr().out
    assert "soc1: schema_valid  (expected syntax_invalid)" in out
    assert "FAILED: wrong-expectation" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["stream", "--chunk-plan", "one-byte"], "missing required argument(s): CASE"),
        (["stream", "enum-valid"], "missing required argument(s): --chunk-plan"),
        (["stream", "enum-valid", "--chunk-plan", "nope"], "unknown chunk plan 'nope'"),
        (["stream", "enum-valid", "--chunk-plan", "seeded", "--seed", "x"], "must be an integer"),
        (["stream", "no-such-case", "--chunk-plan", "one-byte"], "no such case directory"),
        (["stream", "cases/v1/nope", "--chunk-plan", "one-byte"], "not a case directory"),
    ],
)
def test_cli_bad_arguments_exit_1_not_2(argv, message, capsys):
    assert main(argv) == 1
    assert message in capsys.readouterr().err


def test_stream_subcommand_works_with_pythonpath_src():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "crashlab",
            "stream",
            "cases/v1/nested-object-valid",
            "--chunk-plan",
            "boundary-focused",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "schema_valid"
