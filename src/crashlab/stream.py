"""Incremental JSON recognition over arbitrary byte chunks.

A streaming client sees a JSON value one transport frame at a time, and the
frames fall wherever the transport put them: in the middle of a key, between
the two halves of ``\\"``, between the lead byte and the continuation byte of
``é``. This module recognizes one JSON value under those conditions and reports
which of five states the stream is in::

    complete          one JSON value, structurally finished
    incomplete        a prefix of a JSON value; more bytes could finish it
    invalid           the bytes cannot be a prefix of any JSON value
    trailing-content  a complete value followed by non-whitespace
    invalid-utf8      the bytes are not UTF-8, and no continuation can fix it

It is transport-neutral on purpose: there is no SSE parser, no WebSocket
client, and no retry policy here. Feed it ``bytes``; it does not care where
they came from.

Two rules do the load-bearing work:

* **Never complete early.** ``{"a": 1`` is ``incomplete``, not a partial object
  to hand upward, and a number is only finished once a byte arrives that could
  not extend it (or the stream ends) -- ``1`` and ``12`` are different values,
  and only the transport knows which one you have.
* **Never repair.** Truncation is reported, not completed. Trailing bytes are
  reported, not discarded. Nothing is stripped, unwrapped, or re-parsed
  leniently. A finished value is handed to :func:`crashlab.classify.classify_text`,
  the SOC-01 classifier, so the streaming path and the offline path agree.

The recognizer tracks the same grammar as ``json.JSONDecoder.raw_decode``,
including its quirks, so the two never disagree about where a value ends:
``01`` is the value ``0`` followed by trailing ``1``, and ``1e`` is the value
``1`` followed by trailing ``e``.
"""

from __future__ import annotations

import codecs
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from crashlab.classify import SYNTAX_INVALID, Classification, classify_text

STREAM_COMPLETE = "complete"
STREAM_INCOMPLETE = "incomplete"
STREAM_INVALID = "invalid"
STREAM_TRAILING = "trailing-content"
STREAM_INVALID_UTF8 = "invalid-utf8"

#: The complete, stable stream status vocabulary.
STREAM_STATES: tuple[str, ...] = (
    STREAM_COMPLETE,
    STREAM_INCOMPLETE,
    STREAM_INVALID,
    STREAM_TRAILING,
    STREAM_INVALID_UTF8,
)

#: Chunk plan IDs accepted by :func:`chunk_plan`.
CHUNK_PLANS: tuple[str, ...] = ("one-byte", "boundary-focused", "seeded")

DEFAULT_SEED = 0

# RFC 8259 whitespace, matching crashlab.classify. A form feed is content.
_WS = " \t\n\r"
_DIGITS = "0123456789"
_HEX = "0123456789abcdefABCDEF"
_STRUCTURAL = b'{}[],:"'

# Parser modes. The stack holds "object"/"array" for the enclosing containers.
_M_VALUE = "value"  # expecting the first byte of a value
_M_ARRAY_FIRST = "array-first"  # after '[': a value or ']'
_M_OBJECT_FIRST = "object-first"  # after '{': a key or '}'
_M_OBJECT_KEY = "object-key"  # after ',' in an object: a key
_M_COLON = "colon"  # after a key: ':'
_M_OBJECT_NEXT = "object-next"  # after a member: ',' or '}'
_M_ARRAY_NEXT = "array-next"  # after an item: ',' or ']'
_M_STRING = "string"
_M_ESCAPE = "escape"
_M_UNICODE = "unicode"  # inside \uXXXX, collecting hex digits
_M_NUMBER = "number"
_M_LITERAL = "literal"  # inside true / false / null
_M_DONE = "done"  # a value is finished; only whitespace may follow

_ESCAPES = '"\\/bfnrtu'
_LITERALS = {"t": "true", "f": "false", "n": "null"}

# Number sub-states, mirroring json.decoder.NUMBER_RE. A state in _NUM_TERMINAL
# is one where the token scanned so far is already a complete JSON number.
_NUM_MINUS = "minus"
_NUM_ZERO = "zero"
_NUM_INT = "int"
_NUM_POINT = "point"
_NUM_FRAC = "frac"
_NUM_EXP = "exp"
_NUM_EXP_SIGN = "exp-sign"
_NUM_EXP_DIGITS = "exp-digits"
_NUM_TERMINAL = frozenset({_NUM_ZERO, _NUM_INT, _NUM_FRAC, _NUM_EXP_DIGITS})

_DETAIL_LIMIT = 72


class StreamError(Exception):
    """Raised for a bad chunk plan or an unusable streaming request."""


@dataclass(frozen=True)
class StreamState:
    """The recognizer's verdict after one ``feed`` or after ``finish``."""

    status: str
    #: The decoded JSON value, set only once a full value has been recognized
    #: (``complete`` or ``trailing-content``). Never set while ``incomplete``.
    #: ``None`` is also how ``null`` decodes, so check ``status`` first.
    value: Any = field(default=None, repr=False)
    #: The SOC-01 verdict for the bytes seen so far. Present for every terminal
    #: state except ``invalid-utf8``, which never reaches the schema validator.
    classification: Classification | None = None
    detail: str | None = None

    @property
    def soc1_status(self) -> str:
        """This state projected onto the four SOC-01 status IDs.

        ``incomplete`` and ``invalid-utf8`` have no SOC-01 equivalent -- the
        first is a truncated document and the second never decodes to text --
        so both project onto ``syntax_invalid``.
        """
        if self.classification is not None:
            return self.classification.status
        return SYNTAX_INVALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "soc1_status": self.soc1_status,
            "detail": self.detail,
        }


def _detail(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > _DETAIL_LIMIT:
        collapsed = collapsed[:_DETAIL_LIMIT] + "..."
    return collapsed


class IncrementalJson:
    """A pushdown recognizer for exactly one JSON value, fed in byte chunks.

    ``schema`` is optional; when given, a finished value is classified with
    :func:`crashlab.classify.classify_text` so a stream and an offline run of
    the same bytes produce the same SOC-01 status.
    """

    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self._schema = schema
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._text: list[str] = []
        self._length = 0  # == len("".join(self._text)); avoids repeated joins
        self._cursor = 0
        self._pushback: list[tuple[str, int]] = []

        self._status = STREAM_INCOMPLETE
        self._detail: str | None = None
        self._mode = _M_VALUE
        self._stack: list[str] = []
        self._string_is_key = False
        self._unicode_left = 0
        self._literal = ""
        self._literal_index = 0
        self._num_state = _NUM_MINUS
        self._num_start = 0
        self._num_valid = 0  # length of the longest valid number prefix
        self._value_start: int | None = None
        self._trailing_start: int | None = None
        self._value_text: str | None = None
        self._value: Any = None
        self._has_value = False
        self._finished = False
        self._bytes_fed = 0

    # -- public API ---------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def text(self) -> str:
        """Everything decoded so far. Exactly the bytes fed, never repaired."""
        if len(self._text) > 1:
            self._text = ["".join(self._text)]
        return self._text[0] if self._text else ""

    def feed(self, chunk: bytes) -> StreamState:
        """Consume one chunk of bytes and report the state after it."""
        if self._finished:
            raise StreamError("feed() after finish()")
        self._bytes_fed += len(chunk)
        if self._status != STREAM_INVALID_UTF8:
            try:
                decoded = self._decoder.decode(bytes(chunk))
            except UnicodeDecodeError as exc:
                self._fail_utf8(exc)
            else:
                self._append(decoded)
                self._consume()
        return self.state()

    def finish(self) -> StreamState:
        """Close the stream and report the final state.

        A value that is still unfinished when the stream ends is ``invalid``,
        not ``incomplete``: ``incomplete`` is a claim that more bytes could
        still arrive, and at ``finish`` that claim is false. The detail says it
        was truncated, and the SOC-01 projection is ``syntax_invalid``.
        """
        if self._finished:
            return self.state()

        if self._status != STREAM_INVALID_UTF8:
            try:
                self._append(self._decoder.decode(b"", final=True))
            except UnicodeDecodeError as exc:
                self._fail_utf8(exc)
            else:
                self._consume()
                if self._mode == _M_NUMBER:
                    self._close_number(None, self._length)
                    self._consume()
                if self._status == STREAM_INCOMPLETE:
                    self._status = STREAM_INVALID
                    self._detail = self._truncation_detail()

        self._finished = True
        return self.state()

    def state(self) -> StreamState:
        """The current verdict, without ending the stream."""
        if self._status == STREAM_INVALID_UTF8:
            return StreamState(self._status, detail=self._detail)
        if self._status == STREAM_INCOMPLETE:
            return StreamState(self._status, detail=self._detail)

        classification = None
        if self._schema is not None:
            classification = classify_text(self.text, self._schema)
        detail = self._detail
        if self._status == STREAM_TRAILING and self._trailing_start is not None:
            # Recomputed here, not when the first stray byte arrived, so the
            # detail names everything that followed the value, not just the
            # part that happened to be in that chunk.
            extra = self.text[self._trailing_start :]
            detail = f"extra content after value: {_detail(extra)!r}"
        return StreamState(
            self._status,
            value=self._value if self._has_value else None,
            classification=classification,
            detail=detail,
        )

    # -- byte / character plumbing -----------------------------------------

    def _append(self, decoded: str) -> None:
        if decoded:
            self._text.append(decoded)
            self._length += len(decoded)

    def _fail_utf8(self, exc: UnicodeDecodeError) -> None:
        self._status = STREAM_INVALID_UTF8
        self._detail = f"not valid UTF-8: {exc.reason}"
        self._pushback.clear()

    def _fail(self, detail: str) -> None:
        self._status = STREAM_INVALID
        self._detail = detail
        self._pushback.clear()
        self._cursor = self._length

    def _consume(self) -> None:
        """Run the state machine over every character not yet parsed."""
        if self._status in (STREAM_INVALID, STREAM_INVALID_UTF8, STREAM_TRAILING):
            self._cursor = self._length
            return

        text = self.text
        while self._pushback or self._cursor < self._length:
            if self._pushback:
                char, position = self._pushback.pop(0)
            else:
                position = self._cursor
                char = text[position]
                self._cursor += 1
            self._step(char, position)
            if self._status in (STREAM_INVALID, STREAM_TRAILING):
                self._pushback.clear()
                self._cursor = self._length
                return

    def _unexpected(self, char: str, position: int) -> None:
        self._fail(f"unexpected character {char!r} at position {position}")

    # -- the state machine --------------------------------------------------

    def _step(self, char: str, position: int) -> None:
        mode = self._mode

        if mode == _M_STRING:
            self._step_string(char, position)
        elif mode == _M_ESCAPE:
            self._step_escape(char, position)
        elif mode == _M_UNICODE:
            self._step_unicode(char, position)
        elif mode == _M_NUMBER:
            self._step_number(char, position)
        elif mode == _M_LITERAL:
            self._step_literal(char, position)
        elif char in _WS:
            return  # whitespace is legal between every token, and after the value
        elif mode == _M_VALUE:
            self._start_value(char, position)
        elif mode == _M_ARRAY_FIRST:
            if char == "]":
                self._close_container(position)
            else:
                self._start_value(char, position)
        elif mode == _M_OBJECT_FIRST or mode == _M_OBJECT_KEY:
            if char == '"':
                self._start_string(is_key=True)
            elif char == "}" and mode == _M_OBJECT_FIRST:
                self._close_container(position)
            else:
                self._unexpected(char, position)
        elif mode == _M_COLON:
            if char == ":":
                self._mode = _M_VALUE
            else:
                self._unexpected(char, position)
        elif mode == _M_OBJECT_NEXT:
            if char == ",":
                self._mode = _M_OBJECT_KEY
            elif char == "}":
                self._close_container(position)
            else:
                self._unexpected(char, position)
        elif mode == _M_ARRAY_NEXT:
            if char == ",":
                self._mode = _M_VALUE
            elif char == "]":
                self._close_container(position)
            else:
                self._unexpected(char, position)
        elif mode == _M_DONE:
            # A second value is never parsed. Extra bytes are reported as-is.
            self._status = STREAM_TRAILING
            self._trailing_start = position
        else:  # pragma: no cover - every mode is handled above
            raise AssertionError(f"unhandled mode {mode!r}")

    def _start_value(self, char: str, position: int) -> None:
        if self._value_start is None:
            self._value_start = position
        if char == "{":
            self._stack.append("object")
            self._mode = _M_OBJECT_FIRST
        elif char == "[":
            self._stack.append("array")
            self._mode = _M_ARRAY_FIRST
        elif char == '"':
            self._start_string(is_key=False)
        elif char == "-" or char in _DIGITS:
            self._mode = _M_NUMBER
            self._num_start = position
            if char == "-":
                self._num_state = _NUM_MINUS
                self._num_valid = 0
            else:
                self._num_state = _NUM_ZERO if char == "0" else _NUM_INT
                self._num_valid = 1
        elif char in _LITERALS:
            self._mode = _M_LITERAL
            self._literal = _LITERALS[char]
            self._literal_index = 1
        else:
            self._unexpected(char, position)

    def _start_string(self, *, is_key: bool) -> None:
        self._mode = _M_STRING
        self._string_is_key = is_key

    def _step_string(self, char: str, position: int) -> None:
        if char == '"':
            if self._string_is_key:
                self._mode = _M_COLON
            else:
                self._close_value(position + 1)
        elif char == "\\":
            self._mode = _M_ESCAPE
        elif char < " ":
            # json.decoder scans strings in strict mode: a raw control
            # character inside a string is a syntax error, not content.
            self._fail(f"unescaped control character {char!r} at position {position}")

    def _step_escape(self, char: str, position: int) -> None:
        if char not in _ESCAPES:
            escape = "\\" + char
            self._fail(f"invalid escape {escape!r} at position {position}")
        elif char == "u":
            self._mode = _M_UNICODE
            self._unicode_left = 4
        else:
            self._mode = _M_STRING

    def _step_unicode(self, char: str, position: int) -> None:
        if char not in _HEX:
            self._fail(f"invalid \\u escape: {char!r} at position {position} is not hex")
            return
        self._unicode_left -= 1
        if self._unicode_left == 0:
            # A surrogate pair is simply two of these in a row; the second
            # \uXXXX re-enters here through the ordinary escape path.
            self._mode = _M_STRING

    def _step_literal(self, char: str, position: int) -> None:
        expected = self._literal[self._literal_index]
        if char != expected:
            self._fail(
                f"invalid literal at position {position}: "
                f"expected {expected!r} of {self._literal!r}, got {char!r}"
            )
            return
        self._literal_index += 1
        if self._literal_index == len(self._literal):
            self._close_value(position + 1)

    # -- numbers ------------------------------------------------------------

    def _step_number(self, char: str, position: int) -> None:
        state = self._num_state
        nxt: str | None = None
        if state == _NUM_MINUS:
            if char == "0":
                nxt = _NUM_ZERO
            elif char in _DIGITS:
                nxt = _NUM_INT
        elif state == _NUM_ZERO:
            if char == ".":
                nxt = _NUM_POINT
            elif char in "eE":
                nxt = _NUM_EXP
        elif state == _NUM_INT:
            if char in _DIGITS:
                nxt = _NUM_INT
            elif char == ".":
                nxt = _NUM_POINT
            elif char in "eE":
                nxt = _NUM_EXP
        elif state == _NUM_POINT:
            if char in _DIGITS:
                nxt = _NUM_FRAC
        elif state == _NUM_FRAC:
            if char in _DIGITS:
                nxt = _NUM_FRAC
            elif char in "eE":
                nxt = _NUM_EXP
        elif state == _NUM_EXP:
            if char in "+-":
                nxt = _NUM_EXP_SIGN
            elif char in _DIGITS:
                nxt = _NUM_EXP_DIGITS
        elif state in (_NUM_EXP_SIGN, _NUM_EXP_DIGITS):
            if char in _DIGITS:
                nxt = _NUM_EXP_DIGITS

        if nxt is None:
            self._close_number(char, position)
            return

        self._num_state = nxt
        if nxt in _NUM_TERMINAL:
            self._num_valid = position - self._num_start + 1

    def _close_number(self, char: str | None, position: int) -> None:
        """Finish the number token, pushing back whatever it could not use.

        ``json.JSONDecoder`` matches the longest prefix that is a valid number
        and leaves the rest for the enclosing context, so ``1e`` is the value
        ``1`` with a stray ``e`` after it, and ``01`` is ``0`` with a stray
        ``1``. Doing the same here keeps the stream and SOC-01 in agreement.
        """
        if self._num_valid == 0:
            self._fail(f"invalid number literal at position {self._num_start}")
            return

        end = self._num_start + self._num_valid
        text = self.text
        pushback = [(text[index], index) for index in range(end, position)]
        if char is not None:
            pushback.append((char, position))
        self._close_value(end)
        if self._status not in (STREAM_INVALID, STREAM_INVALID_UTF8):
            self._pushback[:0] = pushback

    # -- value completion ---------------------------------------------------

    def _close_container(self, position: int) -> None:
        self._stack.pop()
        self._close_value(position + 1)

    def _close_value(self, end: int) -> None:
        """One value ended at character index ``end`` (exclusive)."""
        if self._stack:
            self._mode = _M_OBJECT_NEXT if self._stack[-1] == "object" else _M_ARRAY_NEXT
            return

        self._mode = _M_DONE
        start = 0 if self._value_start is None else self._value_start
        self._value_text = self.text[start:end]
        try:
            self._value = json.loads(self._value_text)
        except ValueError as exc:  # pragma: no cover - the grammar above forbids it
            self._fail(f"recognized value did not decode: {exc}")
            return
        self._has_value = True
        self._status = STREAM_COMPLETE
        self._detail = None

    def _truncation_detail(self) -> str:
        if self._value_start is None:
            seen = self.text
            what = "whitespace only" if seen else "no bytes"
            return f"truncated: no JSON value ({what})"
        if self._mode in (_M_STRING, _M_ESCAPE, _M_UNICODE):
            return "truncated: unterminated string"
        if self._mode == _M_LITERAL:
            return f"truncated: incomplete literal {self._literal!r}"
        if self._mode == _M_NUMBER:
            return "truncated: incomplete number"
        if self._stack:
            open_containers = "".join("{" if kind == "object" else "[" for kind in self._stack)
            return f"truncated: {len(self._stack)} unclosed container(s) {open_containers}"
        return "truncated: incomplete JSON value"


# -- chunk plans ------------------------------------------------------------


def chunk_plan(name: str, data: bytes, seed: int | None = None) -> list[bytes]:
    """Split ``data`` into chunks according to plan ``name``.

    Every plan is deterministic: the same name, bytes and seed always produce
    the same list, so a failing stream test is a fixture, not a flake. The
    concatenation of the result is always exactly ``data`` -- a plan chooses
    boundaries, it never edits bytes.
    """
    if name not in CHUNK_PLANS:
        raise StreamError(f"unknown chunk plan {name!r}; choose from {', '.join(CHUNK_PLANS)}")
    data = bytes(data)
    if name == "one-byte":
        return [data[index : index + 1] for index in range(len(data))]
    if name == "boundary-focused":
        return _boundary_chunks(data)
    return _seeded_chunks(data, DEFAULT_SEED if seed is None else seed)


def _boundary_chunks(data: bytes) -> list[bytes]:
    """Cut at every boundary a hand-written streaming parser tends to get wrong.

    Structural punctuation, both sides of a backslash escape, inside ``\\uXXXX``,
    every byte of a multibyte UTF-8 sequence, and both edges of a whitespace run
    (so whitespace-only chunks actually occur). Empty chunks are interleaved.
    """
    cuts = {0, len(data)}

    def cut(index: int) -> None:
        if 0 <= index <= len(data):
            cuts.add(index)

    for index, byte in enumerate(data):
        if byte in _STRUCTURAL or byte in b" \t\n\r":
            cut(index)
            cut(index + 1)
        elif byte == 0x5C:  # backslash: split before it, after it, and mid-\uXXXX
            cut(index)
            cut(index + 1)
            cut(index + 2)
            cut(index + 4)
        elif byte >= 0x80:  # ASCII/multibyte transitions, and inside the sequence
            cut(index)
            cut(index + 1)

    ordered = sorted(cuts)
    chunks: list[bytes] = []
    for position, start in enumerate(ordered[:-1]):
        if position % 3 == 0:
            chunks.append(b"")
        chunks.append(data[start : ordered[position + 1]])
    return chunks


def _seeded_chunks(data: bytes, seed: int) -> list[bytes]:
    """Pseudo-random variable-length chunks, including empty ones."""
    rng = random.Random(f"crashlab-stream:{seed}")
    chunks: list[bytes] = []
    position = 0
    while position < len(data):
        size = rng.randint(0, 5)
        chunks.append(data[position : position + size])
        position += size
    return chunks


# -- running a stream -------------------------------------------------------


def feed_all(
    chunks: Iterable[bytes], schema: Mapping[str, Any] | None = None
) -> tuple[tuple[StreamState, ...], StreamState]:
    """Feed every chunk, then ``finish``. Returns the per-feed states and the final one."""
    parser = IncrementalJson(schema)
    states = tuple(parser.feed(chunk) for chunk in chunks)
    return states, parser.finish()


@dataclass(frozen=True)
class StreamRun:
    """One case streamed under one chunk plan."""

    case_id: str
    plan: str
    seed: int | None
    chunks: tuple[bytes, ...]
    states: tuple[StreamState, ...]
    final: StreamState
    expected: str | None = None

    @property
    def status(self) -> str:
        """The final state projected onto the four SOC-01 status IDs."""
        return self.final.soc1_status

    @property
    def passed(self) -> bool:
        return self.expected is None or self.status == self.expected

    @property
    def byte_count(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.passed,
            "case": self.case_id,
            "plan": self.plan,
            "seed": self.seed,
            "chunks": len(self.chunks),
            "bytes": self.byte_count,
            "feed_states": [state.status for state in self.states],
            "final": self.final.to_dict(),
            "status": self.status,
            "expected": self.expected,
        }


def stream_case(case: Any, plan: str, seed: int | None = None) -> StreamRun:
    """Stream a :class:`crashlab.cases.Case`'s ``output.txt`` under ``plan``.

    The case text is streamed as the UTF-8 bytes it is stored as, and the
    case's own schema is used, so the run's SOC-01 status is directly
    comparable with the case's ``expected.json``.
    """
    data = case.raw.encode("utf-8")
    chunks = chunk_plan(plan, data, seed)
    states, final = feed_all(chunks, case.schema)
    return StreamRun(
        case_id=case.id,
        plan=plan,
        seed=seed if plan == "seeded" else None,
        chunks=tuple(chunks),
        states=states,
        final=final,
        expected=case.expected_status,
    )


def render_json(run: StreamRun) -> str:
    return json.dumps(run.to_dict(), indent=2, sort_keys=False)


def _tally(states: Sequence[StreamState]) -> str:
    counts: dict[str, int] = {}
    for state in states:
        counts[state.status] = counts.get(state.status, 0) + 1
    ordered = [name for name in STREAM_STATES if name in counts]
    return ", ".join(f"{name} x{counts[name]}" for name in ordered) or "(no chunks)"


def render_human(run: StreamRun) -> str:
    """A compact report: what the feeds saw, then the final verdict."""
    seed = f" -- seed {run.seed}" if run.seed is not None else ""
    lines = [
        f"structured-output-crashlab -- stream {run.case_id} -- plan {run.plan}{seed}",
        f"{len(run.chunks)} chunk(s), {run.byte_count} byte(s)",
        "",
        f"feed states: {_tally(run.states)}",
    ]

    first_complete = next(
        (index for index, state in enumerate(run.states) if state.status == STREAM_COMPLETE),
        None,
    )
    if first_complete is not None:
        lines.append(f"first complete after chunk {first_complete + 1}/{len(run.chunks)}")

    lines.append(f"final: {run.final.status}")
    if run.final.detail:
        lines.append(f"        {run.final.detail}")
    if run.final.classification is not None:
        for error in run.final.classification.errors:
            lines.append(f"        {error.pointer or '<root>'}: {error.message}")

    expected = "" if run.expected is None else f"  (expected {run.expected})"
    lines.append(f"soc1: {run.status}{expected}")
    lines.append("")
    lines.append("ok" if run.passed else f"FAILED: {run.case_id} under plan {run.plan}")
    return "\n".join(lines)


__all__ = [
    "CHUNK_PLANS",
    "DEFAULT_SEED",
    "STREAM_COMPLETE",
    "STREAM_INCOMPLETE",
    "STREAM_INVALID",
    "STREAM_INVALID_UTF8",
    "STREAM_STATES",
    "STREAM_TRAILING",
    "IncrementalJson",
    "StreamError",
    "StreamRun",
    "StreamState",
    "chunk_plan",
    "feed_all",
    "render_human",
    "render_json",
    "stream_case",
]
