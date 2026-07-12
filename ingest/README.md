# Ingestion pipeline

Pluggable steps that take an artifact from digitised file to reviewed,
source-referenced statements in the graph (R3, ADR-0003). Every step
reads and writes plain files, so a run can pause for review, be
inspected, and resume:

```
register ──► transcribe ──► extract ──► resolve ──► export-review
   │                                                     │
   │                                          (human fills decision column)
   ▼                                                     ▼
media + artifact/fragment items          apply-review ──► write
                                                            │
                                                            ▼
                                          statements, dual-referenced,
                                          normal rank (ADR-0003)
```

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m ingest --help
```

Connection settings come from `deploy/.env` (same variables as the
smoke test); the vocabulary comes from `model/manifest.yaml` resolved
against the live instance (run `model/bootstrap.py` first).

## Steps

- **register** — uploads media (idempotent by content), ensures the
  artifact item (`instance of`, `media file`) and optional page
  fragments (`part of` + page qualifier). Statement provenance:
  `stated in → the artifact itself` (see docs/MODELING.md, "Artifacts
  and registration"). Runs before extraction so claims can cite QIDs.
- **transcribe** — engines: `llm` (LLM-vision, ADR-0004), `textfile`
  (wrap an existing/manual transcription; keeps the pipeline fully
  local), `htr` (Transkribus-style platform adapter, stub for now).
- **extract** — engines: `llm` (prompts with the claim JSON Schema and
  the uncertainty rules; output is schema- and manifest-validated
  before it goes anywhere) or `file` (prepared claims JSON through the
  same validation).
- **resolve** — replaces `local` refs whose label exactly matches an
  existing item; anything fuzzier is left for the human reviewer, who
  sees `NEW:<local_id>` vs a QID in the CSV.
- **export-review / apply-review** — one CSV row per claim for
  OpenRefine or any spreadsheet (R4). Rows are matched back by
  `claim_id`, a content hash: sorting or filtering the sheet cannot
  mis-apply decisions, and edited claim content is detected loudly.
  **Only explicit `approve` decisions pass** — no decision means no
  write (no auto-confirmation, CLAUDE.md).
- **write** — mints the extraction-run item, creates items for
  unresolved subjects (reusing exact-label matches), writes each claim
  at normal rank with two references: `stated in → artifact/fragment`
  (with page/region/excerpt locator qualifiers) and `inferred from
  extraction run → run item`. Re-runs skip existing statements.

## The claim contract (`ingest/claims.py`)

Every step exchanges `ClaimBatch` JSON. The non-negotiable rules are
structural in the schema:

- `source` (artifact + fragment locators) is a **required** field —
  a referenceless claim cannot be represented (rule 1).
- Batches carry `extraction_run` metadata for the second, process
  reference (rule 2).
- There is **no rank field** — pipeline writes are always normal rank;
  preferring is a curator act on the wiki (rule 4).
- Time values carry explicit precision and calendar; ranges and
  "circa" are qualifiers, and inconsistent precision is rejected
  rather than silently narrowed or padded (rule 5).

Properties and vocabulary items are referenced by **manifest key**
(`spouse`, `circa` — see `model/manifest.yaml`), never by local P/Q id:
ids are per-instance and only the writer resolves them, at write time.

```sh
python3 claims.py --json-schema           # JSON Schema, used in LLM prompts
python3 claims.py --validate batch.json   # schema + manifest check a file
```

## Privacy (rule 7)

No step talks to any external service by default; the whole pipeline
runs with `textfile` + `file` engines. Choosing `--engine llm` sends
artifact content — which may describe living people — to the
configured provider; setting the provider's API key is the explicit
opt-in. There is no default key and no fallback.

## Tests

```sh
.venv/bin/python -m pytest   # offline: fakes for LLM and no instance needed
```

An end-to-end exercise against a live instance is documented by the
task-4 commit: register → textfile transcribe → file extract →
resolve → review (approve 2 / reject 1) → write → idempotent re-run.
