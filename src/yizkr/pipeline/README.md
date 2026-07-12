# Ingestion pipeline (`yizkr.pipeline`)

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
uv sync --dev            # from the repo root
uv run yizkr ingest --help
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

## Face proposals (`ingest/faces.py`, ADR-0005)

A face proposal is an ordinary claim — `depicts` on the photograph,
person as value, `relative position within image` qualifier, the
photograph as its own source — so proposals ride the same review flow
above, and only human-confirmed identifications reach the graph, with
dual provenance (photograph + region; detection run).

The curator loop ("initial manual inputs", R5):

```sh
yizkr ingest detect-faces --file photo.jpg --artifact Q29 \
    --gallery gallery.json --detections det.json --out faces-batch.json
yizkr ingest enroll-face --detections det.json --face 0 \
    --label "A. Person" --qid Q26 --gallery gallery.json
yizkr ingest match-faces --detections det.json \
    --gallery gallery.json --out faces-batch.json
# then: export-review -> decide -> apply-review -> write
```

Unmatched faces produce no claim (an unidentified face has no person
to depict); they wait in the detections file for enrolment. Detection
uses an InsightFace-class model running **locally** — installing the
optional dependencies (`uv sync --extra faces`) never sends images
anywhere. If the model-pack auto-download is blocked in your network,
fetch `buffalo_l` manually into `~/.insightface/models/buffalo_l/`.

## The claim contract (`yizkr/claims.py`)

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
uv run yizkr claims --json-schema           # JSON Schema, used in LLM prompts
uv run yizkr claims --validate batch.json   # schema + manifest check a file
```

## Privacy (rule 7)

No step talks to any external service by default; the whole pipeline
runs with `textfile` + `file` engines. Choosing `--engine llm` sends
artifact content — which may describe living people — to the
configured provider; setting the provider's API key is the explicit
opt-in. There is no default key and no fallback.

## Tests

`make test` runs the offline suite (fakes for LLM and detector, no
instance needed). `make test-live` runs the encoded end-to-end flows
against the running stack — including the rule-3 regression that two
values on one property must coexist.
