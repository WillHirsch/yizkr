# ADR-0006: Semantically chunked claim extraction

Status: accepted (implemented in `pipeline/chunking.py`; validated by
re-extracting the first trial's family tree chunked and diffing against
the reviewed single-shot batch — 99% of facts identical, the remainder
explained: richer symmetric spouse claims, and an ambiguous date
correctly downgraded to year precision instead of silently guessed).
Amended by #87 (2026-09-27): chunk state lives in the stage store (§5).

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
degraded (offset chunking), never fatal. On this fallback, chunk calls
use context mode 2 (§3), never mode 1: forking a conversation whose
segmentation turn was rejected would plant known-bad output in every
chunk's context.

### 2. Packing

Units are greedy-packed in document order into chunks whose combined
*assigned-unit* text stays under a budget (`--chunk-budget`, default
2,000 chars). The budget bounds the text a chunk call must emit claims
for — and therefore its expected output (~16× amplification keeps it
well under one turn) — not the context the call sees, which is the
full document (§3). A single unit larger than the budget becomes its
own chunk; the reassembly nets from the trial remain as backstop for
that case.

### 3. Per-chunk extraction calls

The constraint chunking addresses is **output** size; input size was
never the problem (the failing document was 8 KB). Chunk calls
therefore keep the *full document* in context and bound only what each
call is asked to emit. Context is supplied in one of three modes, best
available first:

1. **Forked conversation (default).** The segmentation call opens a
   conversation whose first turn contains the entire transcription;
   each chunk call is an independent *fork* of that conversation with
   the follow-up "emit claims for unit(s) X only". Every fork sees the
   full document **and the segmentation turn itself** — the complete
   unit roster with canonical labels, which directly improves the
   label-keyed merge (a fork says "Rosa Genserowski" where an isolated
   call would say "Rosa"). Because all forks share a byte-identical
   prefix, prompt caching prices the document at cache-read rates after
   the first call: full-context accuracy at roughly compressed-context
   cost.
   Mechanics per engine — `claude-code`: run segmentation with an
   explicit `--session-id`, then each chunk as
   `claude -p --resume <sid> --fork-session` (verified: forks branch
   from the same prefix without seeing each other's turns; the explicit
   session id is required because a nested CLI otherwise *inherits the
   parent's session id from the environment*). `anthropic`: replay the
   same messages array (document turn, segmentation response) with a
   `cache_control` breakpoint after the segmentation response — a
   stateless fork.
2. **Full document inline.** Engines without conversation support
   resend the transcription in each independent call. Functionally
   identical to forking (identical prefixes may still hit server-side
   caches); just no cache guarantee.
3. **Compressed context (degraded / very-large-document mode).** Only
   the chunk's unit texts plus the segmentation's `context` strings,
   as a fallback when the transcription itself is too large to carry
   per call (long-context attention degradation, cost). This is why
   `SegmentationUnit.context` exists even though mode 1 does not need
   it — segmentation produces it for free and it keeps mode 3 viable.

Each chunk prompt additionally carries: artifact QID and language, the
manifest vocabulary (as today), the claim schema, and a hard
instruction to emit claims **only** for the assigned units — with the
full document visible, drift into neighbouring units is possible;
merge dedupe and validation bound the damage.

Two contract changes relative to the current prompt:

- **NDJSON emission**: one complete claim JSON object per line, no
  wrapper object, no prose. Seams between turns then fall *between*
  units of meaning, and the parser is trivial. (The single-shot
  strategy adopts NDJSON too.)
- **Terminal sentinel**: the final line must be
  `{"done": true, "claims_emitted": N}`. Its *presence* proves the
  response was not cut off: a **missing** sentinel is silent truncation,
  so the chunk is marked incomplete and retried. The self-reported `N`
  is advisory only — models routinely miscount a dense chunk by one or
  more, so a count mismatch keeps every parsed claim and merely warns.
  Rejecting a complete, valid response over an off-by-one self-count
  would discard the whole chunk's extraction.

Entities defined outside the chunk's assigned units (e.g. a parent
named elsewhere in the document) are referenced as local refs with
labels exactly as written; identity is resolved downstream by label,
not within the chunk.

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

Amended by #87 (2026-09-27): the entity manifest (or segmentation), every
chunk and the merged batch are now stages in the stage store
(docs/STAGE_STORE.md), each stored under a key built from the
transcription's content, the prompt templates, the manifest and the model,
with every raw response beside its parsed output. The sidecar files below
are gone: per-chunk status is the store's records and the run's events,
failures go to the stage's append-only `failures.jsonl` (reason, attempts,
and the raw response of a truncation), and coverage, rejects and gaps are
extras of the batch record. `--resume` reuses records whose key matches; a
failed chunk's logged response is re-parsed before the model is called
again, and a parser version bump re-parses stored raw responses. A
recorded fallback is stored only when the engine answered: an outage
fails the stage, to be retried. Sidecar state and batches made before the
store are never read as stage outputs: they carry no key, and archived
outputs enter the store only through the import in #104, under keys of
their own (ADR-0025). `yizkr ingest run` holds a document whose directory
holds such outputs, with its files and reviews untouched, until a run
opts in with `--rederive-unrecorded` (docs/STAGE_STORE.md). The layout
this section first described:

All intermediate state lives beside the output (runtime state, so under
`var/` or wherever `--out` points):

    <out>.chunks/
      segmentation.json     # the accepted DocumentSegmentation
      manifest.json         # per-chunk status: pending/ok/failed(+reason);
                            # segmentation conversation/session id for forking
      <chunk>.raw.txt       # verbatim engine response, written pre-parse
      <chunk>.ndjson        # parsed, validated claims
      <chunk>.fail.ndjson   # append-only failure log (reason + last raw);
                            # survives a re-run that recovers the chunk
      <chunk>.rejects.json  # per-chunk validation rejects

If a `--resume` finds the recorded segmentation session no longer
available for forking, remaining chunks drop to context mode 2 (full
document inline) — resumption never requires re-running segmentation.

- Chunks run sequentially in v1 (subscription rate limits); the design
  keeps chunks independent so parallel execution is a flag away later.
- Per-chunk retries (`--chunk-retries`, default 2). A chunk that still
  fails does not abort the run: the batch is written from the chunks
  that succeeded, failures are listed in `manifest.json` and on stderr,
  and the exit code is non-zero. A later recovering `--resume` flips that
  chunk's `manifest.json` status back to `ok` and overwrites its
  `raw.txt`; the append-only `<chunk>.fail.ndjson` sidecar is never
  rewritten, so the original failure reason (and any truncated response)
  survives the recovery that hides it everywhere else.
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
  optimization. Forking makes the *document* portion of that overhead
  cache-priced.
- The `LlmClient` protocol grows an optional conversation capability
  (open a conversation, fork it with a follow-up); engines that lack it
  transparently fall back to context mode 2. Independently of this ADR,
  the `claude-code` engine must isolate child sessions (explicit
  `--session-id`, scrub inherited session environment) — discovered
  while verifying fork feasibility: a nested CLI silently adopts its
  parent's session id.
- Every stage is inspectable and replayable from the state dir, which
  also gives folder-level orchestration (ROADMAP item 8) a natural
  unit of progress to track.
- Segmentation adds one cheap model call and one new failure mode, with
  a deterministic fallback.
- Unit tests script the whole flow with a fake client (segmentation →
  chunks → merge; malformed segmentation fallback; failed chunk +
  resume; sentinel enforcement; id prefixing; dedupe). One minimal
  live-marked test exercises a two-chunk extraction end-to-end.
