# ADR-0006: Semantically chunked claim extraction

Status: proposed (specified; implementation pending)

## Context

The first trial ingestion (see ROADMAP "Hardening items") showed that
LLM claim extraction is **output-bound**: claim JSON amplifies source
text roughly 16× (an 8 KB family tree became a 130 KB response), so a
dense document exceeds what a model emits in one turn. Responses then
span multiple assistant turns, and **continuation across turns is a
generative act, not a file transfer** — the model cannot guarantee
byte-exact resumption. Four consecutive runs failed at different layers
of that same fault (turn caps, wrapper-object contract, transport
dropping earlier turns, an inexact resume seam corrupting the enclosing
JSON), each losing ~10 minutes of paid work, because the blast radius
of any seam error was the whole document.

The salvage machinery added during the trial (stream reassembly, raw
dumps, loose-claim parsing, per-claim validation) reduces the blast
radius but does not remove the seam. The structural fix is to bound the
size of each independently valid unit of emission below the single-turn
ceiling and make failure per-unit rather than per-document.

Two sharpenings from the trial inform the design:

- Chunking must follow **semantic units** (a person entry, a narrative
  section), not character offsets: outline-style documents encode
  relations positionally (a child's parentage is the enclosing
  subtree), so an offset boundary mid-subtree destroys exactly the
  context extraction needs.
- The merge problem chunking creates is one the architecture already
  solves: labels are the system-wide idempotency key, and the resolve
  step and writer materialize/unify local refs **by label**.
  Cross-chunk identity is the same mechanism as cross-document
  identity, so chunked extraction composes with existing machinery.

Out of scope here, decided elsewhere: structured-export adapters
(GEDCOM and friends) are the *preferred* path for machine-generated
genealogy exports — this ADR covers the generative path for documents
without exploitable structure. Cross-document reconciliation beyond
exact labels is ROADMAP item 9.

## Decision

Extraction becomes a two-stage pipeline with per-chunk emission,
chunk-level state, and a deterministic merge.

### 1. Segmentation stage (one small LLM call)

Input: the transcription with numbered lines. Output: a
`DocumentSegmentation` (Pydantic, in `pipeline/chunking.py`):

    SegmentationUnit:
      id:         str            # stable within the run, e.g. "u07"
      label:      str            # e.g. "Lina Genserowski entry"
      kind:       str            # free text: "person entry", "narrative section", ...
      first_line: int            # 1-based, inclusive
      last_line:  int
      context:    str            # ancestor path / preamble the unit needs,
                                 # e.g. "child of Samuel Moses Genserowski
                                 # and Fanny Hirschfeld (generation 2)"
      extract:    bool           # False = deliberately skipped (logistics,
                                 # boilerplate) — recorded, not extracted

    DocumentSegmentation:
      units: list[SegmentationUnit]

Rules enforced on the response: line ranges in bounds; units
non-overlapping; gaps allowed. Non-assertive sections are marked
`extract: false` rather than omitted, so coverage is auditable — every
line of the document is either inside a unit or listed as unsegmented
in the run manifest.

Segmentation output is tiny (KBs) and single-turn-safe. If the call
fails or returns an invalid segmentation after one retry, fall back to
fixed-size line-boundary windows with empty context and a warning —
degraded (offset chunking), never fatal.

### 2. Packing

Units are greedy-packed in document order into chunks whose combined
text stays under an input budget (`--chunk-budget`, default 2,000
chars — with ~16× amplification that keeps expected output well under
one turn). A single unit larger than the budget becomes its own chunk;
the reassembly nets from the trial remain as backstop for that case.

### 3. Per-chunk extraction calls

Each chunk prompt carries: artifact QID and language; the deduplicated
`context` strings of its units; the unit texts (with line numbers); the
manifest vocabulary (as today); and the claim schema. Two contract
changes relative to the current prompt:

- **NDJSON emission**: one complete claim JSON object per line, no
  wrapper object, no prose. Seams between turns then fall *between*
  units of meaning, and the parser is trivial. (The single-shot
  strategy adopts NDJSON too.)
- **Terminal sentinel**: the final line must be
  `{"done": true, "claims_emitted": N}`. A missing sentinel or a count
  mismatch marks the chunk incomplete → retry. This converts silent
  truncation into a detected, retryable failure.

Entities defined outside the chunk (e.g. a parent named only in
`context`) are referenced as local refs with labels exactly as written;
identity is resolved downstream by label, not within the chunk.

### 4. Merge

Deterministic, no model involvement:

- **Local-id namespacing**: every local id is prefixed with its chunk
  id (`c03:p1`) so ids never collide across chunks. Unification of
  same-label refs is deliberately left to the existing resolve/write
  machinery — the same label-keyed mechanism that unifies entities
  across documents.
- **Dedupe**: claims identical under exact structural equality
  (subject, property, value, qualifiers, source) are collapsed to the
  first occurrence; anything less than identical coexists and flows to
  human review like any other claim.
- **Validation**: the existing per-claim salvage validation runs per
  chunk; rejects aggregate into the batch-level
  `<out>.rejected.json`.
- One `ClaimBatch` with **one extraction-run item** for the document —
  chunking is an implementation detail of the run, not a provenance
  boundary.

### 5. State, failure, and resume

All intermediate state lives beside the output (runtime state, so under
`var/` or wherever `--out` points):

    <out>.chunks/
      segmentation.json     # the accepted DocumentSegmentation
      manifest.json         # per-chunk status: pending/ok/failed(+reason)
      <chunk>.raw.txt       # verbatim engine response, written pre-parse
      <chunk>.ndjson        # parsed, validated claims
      <chunk>.rejects.json  # per-chunk validation rejects

- Chunks run sequentially in v1 (subscription rate limits); the design
  keeps chunks independent so parallel execution is a flag away later.
- Per-chunk retries (`--chunk-retries`, default 2). A chunk that still
  fails does not abort the run: the batch is written from the chunks
  that succeeded, failures are listed in `manifest.json` and on stderr,
  and the exit code is non-zero.
- `--resume` re-runs only chunks without valid parsed output. Because
  raw responses persist per chunk, resume re-parses existing raw text
  before re-invoking the model — a parser fix never costs a second
  model run (this subsumes the `--from-raw` need from the trial).

### 6. Completeness report

After merge, a `coverage.json` in the state dir lists: units with
`extract: true` that yielded zero claims, unsegmented line ranges, and
the diff between segmentation unit labels and distinct subject labels
in the batch. Advisory and curator-facing — no automatic re-extraction
loop in v1.

### 7. Strategy selection and CLI surface

    yizkr ingest extract ... --strategy auto|single|chunked
                             --chunk-budget CHARS   (default 2000)
                             --chunk-retries N      (default 2)
                             --resume

`auto` (the default) picks chunked when the transcription exceeds a
conservative threshold (default 4,000 chars). Density — the real
driver — is unknowable upfront, and chunking a document that would
have fit is merely a few extra calls, never incorrect. Engine-agnostic:
works with any `LlmClient`.

## Consequences

- Failure domain shrinks from the document to the chunk; a lost chunk
  costs seconds of model time, not ten minutes.
- More calls per document, each small; wall-clock may rise slightly,
  and prompt overhead (schema + vocabulary repeated per chunk) is
  accepted for now — a slimmed per-chunk schema is a later
  optimization.
- Every stage is inspectable and replayable from the state dir, which
  also gives folder-level orchestration (ROADMAP item 8) a natural
  unit of progress to track.
- Segmentation adds one cheap model call and one new failure mode, with
  a deterministic fallback.
- Unit tests script the whole flow with a fake client (segmentation →
  chunks → merge; malformed segmentation fallback; failed chunk +
  resume; sentinel enforcement; id prefixing; dedupe). One minimal
  live-marked test exercises a two-chunk extraction end-to-end.
