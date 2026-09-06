# structured-output-crashlab

Offline structured-output conformance runner. It takes raw, model-like text, checks it
against a JSON Schema, and files the result under exactly one of four stable status IDs.

No model is ever called. No output is ever repaired. Every case is a directory of files
on disk, so the whole suite is a deterministic, offline regression test.

```
$ make demo
structured-output-crashlab -- JSON Schema draft 2020-12 -- 17 case(s)

PASS  array-item-invalid           schema_invalid    (expected schema_invalid)
        /items/1/qty: '5' is not of type 'integer'
PASS  markdown-fenced-json         syntax_invalid    (expected syntax_invalid)
        Expecting value (line 1 column 1)
PASS  required-missing             schema_invalid    (expected schema_invalid)
        <root>: 'name' is a required property
PASS  trailing-text-after-value    trailing_content  (expected trailing_content)
        extra content after value: 'Let me know if you need anything else!'
...

17/17 passed
ok
```

## Why

"The JSON was bad" is not a bug report. It collapses four unrelated failures into one
word, so teams argue about anecdotes instead of counting occurrences.

A truncated response, a fenced code block, a valid object with the wrong enum member, and
a valid object with an apology stapled to the end each need a different fix — a token
budget, a prompt or API mode change, a schema or few-shot change, and a stop-sequence
change respectively. This runner gives those four failures stable names and turns any
sanitized example into a fixture you can keep.

## The four statuses

A case is always classified as exactly one of these. The IDs are stable; treat them as an
API.

| Status | Meaning |
| --- | --- |
| `schema_valid` | One JSON value, only optional whitespace around it, and the schema validates. |
| `schema_invalid` | One JSON value, only optional whitespace around it, but the schema rejects it. Reported with JSON Pointers. |
| `syntax_invalid` | Not parseable as JSON at all: truncation, markdown fences, leading prose, empty output. |
| `trailing_content` | A complete JSON value followed by non-whitespace text. The value may itself be perfectly valid. |

The `schema_invalid` / `syntax_invalid` split is the one that matters most in triage:
`schema_invalid` means the model produced JSON and got the *content* wrong, while
`syntax_invalid` means you never had a document to validate.

`trailing_content` is deliberately separate from both. A model that emits a correct object
and then says "Let me know if you need anything else!" has a formatting problem, not a
schema problem, and lumping it in with `syntax_invalid` hides that.

## The no-repair rule

The runner never tries to rescue an output. Specifically, it will **never**:

- strip Markdown fences (```` ```json ```` blocks are `syntax_invalid`),
- extract a JSON substring from surrounding prose,
- complete a truncated value, or
- re-parse with a lenient or "relaxed JSON" mode.

Parsing is `json.JSONDecoder().raw_decode` on the exact text of `output.txt`, starting at
the first character that is not JSON whitespace. Anything left over after the decoded
value is `trailing_content`.

This is a measurement tool, so repair would poison the measurement: a runner that strips
fences reports 100% conformance for a model that has never once returned bare JSON. Repair
belongs in your application code, where you can decide the policy, not in the thing telling
you how often you need it.

Two consequences worth knowing:

- **Whitespace around a value is fine.** Leading newlines or indentation before the value
  parse normally — that is legal JSON. Only RFC 8259 whitespace (space, tab, CR, LF)
  counts; a stray form feed is content.
- **Python's JSON extensions are rejected.** `NaN` and `Infinity` are `syntax_invalid`,
  because they are not JSON, even though `json.loads` accepts them by default.

`cases/v1/markdown-fenced-json` and `cases/v1/leading-prose-then-json` exist specifically
so this rule is a test, not a promise in a README.

## JSON Schema draft

**Draft 2020-12** (`https://json-schema.org/draft/2020-12/schema`), via the
[`jsonschema`](https://python-jsonschema.readthedocs.io/) library's `Draft202012Validator`.
Every `schema.json` in this repository declares that dialect in its `$schema` key, and each
schema is checked with `check_schema` before use, so a malformed fixture schema fails loudly
rather than silently passing everything.

Validation errors are located by **JSON Pointer** (RFC 6901): `/items/1/qty`,
`/user/profile/age`. The document root is the empty string `""`, which is where `required`
and `additionalProperties` errors land, since those are properties of the containing object
rather than of any one member. The human report prints the root pointer as `<root>` for
legibility; the JSON report emits `""`.

## Install and run

Requires Python 3.11+.

```bash
make install          # pip install -e ".[dev]"
make test             # pytest -- fails if any case disagrees with its expected.json
make demo             # crashlab run cases --format human
```

The CLI is available as `crashlab` and as `python -m crashlab`:

```bash
crashlab run                          # defaults to ./cases
crashlab run cases --format human     # one line per case (default)
crashlab run cases --format json      # machine-readable report
crashlab run cases --json             # shorthand for --format json
crashlab run cases/v1/enum-invalid    # a single case directory also works

crashlab mutate --schema s.json --json valid.json --seed 20260905 --out /tmp/generated
crashlab stream cases/v1/enum-valid --chunk-plan one-byte
```

Exit code is `0` when every case matches its `expected.json`, and `1` on any mismatch or
on a malformed case directory.

## Generating cases with `crashlab mutate`

Writing fixtures by hand gets tedious once a schema has more than a couple of constraints.
`crashlab mutate` takes a schema and one **valid** instance of it and writes new case
directories — ordinary SOC-01 cases, loadable by `crashlab run` like any other.

```bash
crashlab mutate --schema schema.json --json valid.json --seed 20260905 --out cases/generated
crashlab mutate --schema schema.json --json valid.json --seed 7 --out /tmp/out \
    --mutations missing_required,truncation --max-cases 2
```

Each mutation is directed by something the schema actually says:

| Mutation ID | What it breaks |
| --- | --- |
| `missing_required` | drops a property listed in `required` |
| `unknown_field` | inserts an undeclared property (prefers an `additionalProperties: false` object) |
| `wrong_primitive` | swaps a field's JSON primitive type |
| `enum_violation` | replaces an `enum` value with a same-type non-member |
| `numeric_bounds` | pushes a number past `minimum` / `maximum` / their exclusive forms |
| `string_bounds` | pushes a string past `minLength` / `maxLength` |
| `array_bounds` | pushes an array past `minItems` / `maxItems` |
| `duplicate_key` | serializes an object with a repeated key (raw text; `json.dumps` cannot) |
| `truncation` | cuts the compact serialization mid-value |

Four properties are the whole point of the subcommand:

- **Deterministic.** `--seed` is required — there is no default, so generation is always
  explicit. Mutation kinds run in sorted ID order, candidate locations are sorted by JSON
  Pointer, and one is chosen with `random.Random(f"{seed}:{mutation_id}")`. Same inputs and
  same seed means the same case IDs, in the same order, byte for byte. Seeding per mutation
  kind means narrowing `--mutations` does not shift the other kinds' choices.
- **Never mutates sources.** `--schema` and `--json` are read only, new directories are
  written under `--out`, and an existing case directory is never overwritten — it is
  reported as skipped instead.
- **Never fabricates.** A mutation kind with no candidate location is listed as skipped with
  a reason (`enum_violation: schema has no enum with a same-type non-member`). It does not
  invent a schema change or a fake field to have something to do.
- **No model, no repair.** Nothing is generated by an LLM and nothing is scored.
  `expected.json` is filled in by running the classifier over the bytes just written, so a
  generated fixture cannot disagree with `crashlab run`.

By default at most one case per applicable mutation kind is written; `--max-cases N` caps
the total. Every case carries its provenance in `meta.json`:

```json
{
  "seed": 20260905,
  "mutation_id": "numeric_bounds",
  "pointer": "/quantity",
  "before": 2,
  "after": 11,
  "generator": "crashlab-mutate",
  "tags": ["generated", "numeric_bounds"],
  "bound": "maximum",
  "limit": 10
}
```

Exit code is `0` when the requested work completed — including when some selected mutations
turned out to be inapplicable — and `1` on bad arguments, unreadable files, an invalid
schema, an instance that does not validate against it, or an I/O error.

The schema walk is deliberately small: `properties`, a single `items` subschema, `required`,
`additionalProperties`, `enum`, and the numeric/string/array bound keywords. `$ref` and the
combinator keywords are not resolved, which shows up as fewer candidates rather than as a
crash.

### JSON report shape

The shape is stable — parse it, diff it, store it.

```json
{
  "ok": true,
  "draft": "2020-12",
  "results": [
    {
      "id": "array-item-invalid",
      "status": "schema_invalid",
      "expected": "schema_invalid",
      "passed": true,
      "errors": [
        { "message": "'5' is not of type 'integer'", "pointer": "/items/1/qty" }
      ]
    },
    {
      "id": "required-missing",
      "status": "schema_invalid",
      "expected": "schema_invalid",
      "passed": true,
      "errors": [
        { "message": "'name' is a required property", "pointer": "" }
      ]
    }
  ]
}
```

## Streaming classification with `crashlab stream`

Everything above assumes you have the whole output. A streaming client does not: it gets
the value one transport frame at a time, and the frames fall wherever the transport put
them — in the middle of a key, between the two halves of `\"`, between the lead byte and
the continuation byte of `é`. Parsers that are correct on whole documents routinely get
these wrong, and the bug only shows up under load, when the chunk sizes change.

`crashlab.stream.IncrementalJson` recognizes one JSON value from arbitrary `bytes` chunks
and reports one of five states:

| State | Meaning |
| --- | --- |
| `complete` | One JSON value, structurally finished. |
| `incomplete` | A prefix of a JSON value. More bytes could still finish it. |
| `invalid` | The bytes cannot be a prefix of any JSON value. |
| `trailing-content` | A complete value followed by non-whitespace. |
| `invalid-utf8` | The bytes are not UTF-8, and no continuation byte can fix that. |

```python
from crashlab import IncrementalJson

parser = IncrementalJson({"type": "object", "required": ["name"]})
parser.feed(b'{"name": "ad').status      # 'incomplete'
parser.feed(b'a"}').status               # 'complete'
parser.finish().classification.status    # 'schema_valid'
```

Two rules do the load-bearing work:

- **Never complete early.** `{"a": 1` is `incomplete`, not a partial object to hand
  upward. A number is only finished once a byte arrives that could not extend it, or the
  stream ends — `1` and `12` are different values, and only the transport knows which one
  you have.
- **Never repair.** The [no-repair rule](#the-no-repair-rule) applies unchanged. Truncation
  is reported, not completed; trailing bytes are reported, not discarded. `finish()` on an
  unfinished value returns `invalid` with a `truncated: ...` detail, never a best-effort
  object.

Split multibyte UTF-8 is `incomplete`, not `invalid-utf8` — half of `é` is a legitimate
thing to see mid-stream. It becomes `invalid-utf8` only when a byte arrives that cannot
continue the sequence, or when the stream ends with a partial sequence still buffered.

A finished value goes through the SOC-01 classifier, so the streaming path and the offline
path cannot drift apart. The recognizer deliberately tracks the same grammar as
`json.JSONDecoder.raw_decode`, including its edges: `01` is the value `0` followed by
trailing `1`, and `1e` is the value `1` followed by trailing `e`. A property test asserts
that agreement over random token soup, and another asserts that no chunking of a valid
payload ever changes the final status or the decoded value.

### Chunk plans

The subcommand replays a case's `output.txt` through the recognizer under a chunk plan.
Every plan is deterministic — a failing stream is a fixture, not a flake — and the chunks
always concatenate back to the exact bytes, since a plan chooses boundaries and never edits
content.

| Plan | Boundaries |
| --- | --- |
| `one-byte` | every chunk is exactly one byte, so every multibyte sequence is split |
| `boundary-focused` | structural punctuation, both sides of a `\` escape, inside `\uXXXX`, each byte of a multibyte sequence, the edges of whitespace runs, with empty chunks interleaved |
| `seeded` | pseudo-random variable-length chunks, including empty ones, from `--seed` (default `0`) |

```bash
crashlab stream cases/v1/enum-valid --chunk-plan one-byte
crashlab stream enum-valid --chunk-plan boundary-focused      # a bare case ID works too
crashlab stream truncated-object --chunk-plan seeded --seed 5 --format json
```

```
$ crashlab stream cases/v1/enum-valid --chunk-plan one-byte
structured-output-crashlab -- stream enum-valid -- plan one-byte
22 chunk(s), 22 byte(s)

feed states: complete x2, incomplete x20
first complete after chunk 21/22
final: complete
soc1: schema_valid  (expected schema_valid)

ok
```

Exit code is `0` when the final state, projected onto the four SOC-01 statuses, matches the
case's `expected.json`, and `1` on a mismatch or a bad argument. `incomplete` and
`invalid-utf8` have no SOC-01 equivalent — the first is a truncated document, the second
never decodes to text — so both project onto `syntax_invalid`.

Out of scope, on purpose: there is no SSE or WebSocket client, no retry policy, no provider
adapter, and no partial-object business logic. Feed it bytes; it does not care where they
came from.

## Case layout

Cases live in versioned directories. The case ID is the directory name, and it is the key
reports are written against — renaming a directory renames the case everywhere.

```
cases/v1/<case-id>/
    schema.json     # JSON Schema, Draft 2020-12
    output.txt      # the exact text under test, byte for byte
    expected.json   # the asserted status (+ optional error pointers)
    meta.json       # optional free-form notes
```

## Adding a case

1. Create `cases/v1/<case-id>/`. Use a descriptive, kebab-case ID that names the failure —
   `array-item-invalid`, not `case-18`.

2. Write `schema.json`, the smallest schema that expresses the constraint.

3. Write `output.txt` containing the **exact** text under test. Do not prettify it, do not
   strip the fences you are trying to catch, and do not add or remove a trailing newline to
   make it look tidy — the bytes are the fixture. Sanitize before you save: fixtures should
   be tiny and synthetic, with no real model dumps and no personal data.

4. Write `expected.json`:

   ```json
   {
     "status": "schema_invalid",
     "error_pointers": ["/items/1/qty"],
     "description": "Second item fails; the pointer carries the array index."
   }
   ```

   - `status` (required) — one of the four status IDs.
   - `error_pointers` (optional) — pointers that must appear among the reported errors.
     Only meaningful for `schema_invalid`. The check is containment, not equality, so
     adding a pointer assertion pins the location you care about without making the fixture
     brittle against `jsonschema` reporting extra errors.
   - `description` / `note` (optional) — prose for whoever reads the fixture next.

5. Optionally add `meta.json` for free-form notes and tags. It is not validated.

6. Run `make test`. A new case that misclassifies fails the suite immediately.

Every fixture is loaded by `tests/test_fixtures.py`, so the suite grows automatically — there
is no list of cases to update.

## What this is not

Out of scope on purpose:

- **Calling models.** Nothing here has network access or takes an API key.
- **Repairing output.** No fence stripping, no substring extraction, no truncation fixing.
- **Generating types.** No Pydantic or dataclass code generation.
- **Scoring models.** It classifies the outputs you give it; it does not rank producers.
- **Talking to transports.** No SSE parser, no WebSocket client, no retries. `crashlab
  stream` takes bytes and nothing else.

## Layout

```
src/crashlab/
    classify.py    # parse + schema validation -> one status ID
    cases.py       # load versioned case directories
    report.py      # human + JSON reports
    mutate.py      # schema-directed mutation -> new cases
    stream.py      # incremental recognizer + chunk plans
    cli.py         # crashlab run / mutate / stream
cases/v1/          # 17 synthetic fixtures
tests/
    goldens/mutate/    # schema + instance + the seed-20260905 golden tree
    stream_fixtures/   # boundary chunkings for the incremental recognizer
```

## License

MIT. See [LICENSE](LICENSE).
