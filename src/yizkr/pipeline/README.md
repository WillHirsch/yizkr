# Ingestion pipeline (`yizkr.pipeline`)

Takes an artifact from a digitised file to reviewed, source-referenced
statements in the graph (R3, ADR-0003). Every step reads and writes plain
files, so a run pauses for review, can be inspected, and resumes.

Whole-corpus scratch runs (`yizkr scratch run`, ADR-0025) have a runbook
of their own, [docs/runbooks/scratch-run.md](../../../docs/runbooks/scratch-run.md),
and a live monitor, [docs/MONITOR.md](../../../docs/MONITOR.md).

## The ingest loop: one command, two gates, repeat

`yizkr ingest run <folder>` is the whole workflow. It advances every
document in the folder as far as it can, then **stops at a human review
gate** and prints what to do. Fill in the gate's CSV, run the *same command
again*, and it picks up where it paused — repeat until every document
reports `written`.

```mermaid
flowchart TD
    run(["yizkr ingest run &lt;folder&gt;"]) --> register

    subgraph auto ["per document, automatically"]
        register[register] --> transcribe[transcribe] --> extract[extract] --> reconcile[reconcile]
        extract -. advise .-> critique["critique + gaps<br/>(flag likely errors)"]
    end

    reconcile --> gate1{{"🚦 GATE 1 · identity<br/>fill equivalences.csv<br/>→ equivalences.reviewed.csv"}}
    gate1 --> resolve[resolve] --> export[export-review]
    export --> gate2{{"🚦 GATE 2 · claims<br/>fill review.csv<br/>→ review.reviewed.csv"}}
    gate2 --> write[write] --> outcome[("statements in the graph<br/>dual-referenced, normal rank")]

    classDef gate fill:#fde68a,stroke:#b45309,color:#1f2937;
    classDef store fill:#dbeafe,stroke:#1e40af,color:#1f2937;
    class gate1,gate2 gate
    class outcome store
```

The two amber hexagons are the human gates — the run pauses there until you
save the reviewed CSV, then continues on the next `ingest run`.

**The two gates**

- **Gate 1 — identity** (`awaiting equivalences review`): *is each person or
  place in this document the same as one already in the graph?* Fill the
  `decision` column of `<doc>/equivalences.csv` and save it as
  `equivalences.reviewed.csv`. A document with nothing to match skips this
  gate, and in `ingest run` no document has anything to match: its
  reconcile step reads no roster from the graph (see **reconcile** below).
  An `equivalences.csv` with rows from an earlier run is still reviewed
  here.
- **Gate 2 — claims** (`awaiting claim review`): *are these extracted facts
  correct?* Fill `<doc>/review.csv` and save as `review.reviewed.csv`. The
  `critique_notes` column surfaces the extraction critic's concerns
  (ADR-0013). There is no living-status decision: privacy is a runtime
  determination, never curation (ADR-0020).

**What keeps it safe.** A gate opens only when its `*.reviewed.csv` exists
*and* reviews the CSV as it is now: a reviewed file is bound, by content
hash, to the CSV it was first seen with, so editing the source CSV re-arms
the gate. A batch whose content changed moves both reviewed files aside as
`*.reviewed.superseded-<n>.csv` (kept, never deleted), and so does a
re-exported CSV whose rows changed, so decisions about the old rows cannot
open gates for the new ones. No reviewed file, nothing writes; identity is
never auto-confirmed and no claim reaches the graph without an explicit
`approve` (R3, ADR-0007).

**Reading a run.** Each document prints a status — `awaiting equivalences
review` (Gate 1), `awaiting claim review` (Gate 2), `written` (done),
`paused` (the run's control file held one of its stages), or `failed`
(errored; re-run to retry, unless the model's answers caused it: see
below). The exit code summarises the batch: `0` all written, `2`
something awaits you, `1` something failed. Re-running is always safe:
finished work is skipped, and a corrected upstream file re-does only what
depends on it (make-style, by content hash; modification times are never
read).

A transcription that fails the transcription check (below), such as a
tool-failure message or a refusal instead of the page's text, fails its
document the same way: the reason is recorded as the `error` in the
document's `state.json` and in the run's events, and no transcription is
kept. The check runs on every vision and text-file transcription a run
makes, whether its answer comes from the engine or from the LLM cache,
and again on every one it reuses (below), so a stricter check applies to
every transcription, the stored ones included. A stored transcription
it now fails is moved aside as `transcription.superseded-<n>.json`, with
a `set-aside` event, and made again in the same run. An answer the cache
kept that fails it is rejected in the cache and asked for once more in
the same run, as the engine would have been asked without the cache; an
answer of the engine's that fails it fails the document, and the next
run asks again. Slide text from a deck and a curator's correction are
not checked.

An extraction that ends with no claims because the model's answers could
not be used fails its document too (ADR-0006 §5): a chunk failed, the
chunks' answers held JSON objects with content the parser cannot read
as claims (claims wrapped in one object or carried in the sentinel, say,
or written with other keys; an object that holds nothing at any depth,
such as `{}` or `{"claims": [{}]}`, counts for nothing), or validation
rejected every claim. The `error` leads with that outcome and names
every cause, then any fallback tier taken, with each attempt's reason. A
chunk that failed because its engine call failed, or its answer was cut
off, is asked again on the next run. A failure the model's answers
caused is not cured by re-running: the LLM cache replays the same
answers, at no engine cost, and they fail the same way (so a parser fix
does reach them). To ask the model again, run with `--epoch extract=N`
(a new epoch), which re-asks only the documents without a current batch,
such as a failed one, or with `--refresh extract`, which extracts every
document again. An extraction whose accepted answers say the document
holds no claims, on either tier, is no failure: its batch is kept, and
its `extraction` stage needs attention, with the reason.

State for each document lives in `var/runs/<folder>/<doc>/`: its views
(`transcription.json`, `batch.json`, `critique.json`, and the extractor's
coverage, rejects and gaps under `extraction/`) and the files the later
steps derive from them. A view persists and is reused while the inputs it
was made from are unchanged (the transcription is made from the source,
or a curator's correction; the batch from the transcription's content;
the critique from the batch's), so a re-run asks the engine nothing for a
document that has not changed, with or without a cache. A change to a
prompt, a model or the code does not make a view stale by itself (the
transcription check aside, above): `--refresh ROLE` makes a role's views
again, in place, and a scratch run starts from fresh state directories.
The model calls a view is made with go through the LLM cache
(`var/llm-cache`, `--cache`; ADR-0032): a call made before with the same
key (built from what the call sends: its prompt
text, the files it reads, and the engine, model, effort and configuration)
is answered from the cache, while parsing, merging and the transcription
check run whenever the view is made. So a fix to them costs no model call,
and a change to a prompt, a model or anything else a call sends asks the
engine again for exactly the calls it changes. `--epoch ROLE=N` samples a
role afresh under new keys (with `--refresh`, for views that stand), and
`--no-cache` runs without the cache: views are reused all the same, and a
view made again calls the engine for everything. To correct a
transcription, save the corrected text as `transcription.corrected.txt`
there. `var/runs/<folder>/` is also the run directory, with an event for
every stage started, done, reused or failed and every answer the cache
gives or cannot give; `yizkr stages state var/runs/<folder>` sums them up.
Every document directory the run makes has a `derivations.json` from its
first run. A directory without one (from before the records, or one that
lost its record), one whose views have lost their `state.json`, and one
whose document's source bytes changed (a different artifact, ADR-0010),
is moved aside whole as `.<doc>.superseded-<n>`, with nothing in it
deleted, and the document is ingested afresh in a new one. See
[docs/LLM_CACHE.md](../../../docs/LLM_CACHE.md) for the views, keys,
rejection, forks, the switches, the run directory and these set-aside
directories. Anomaly checks watch every run's stage outputs and raise
alerts in its run directory, and a fail-fast one pauses a slice through
its control file; their thresholds are in `anomalies.yaml` here, and
`$YIZKR_ANOMALIES` names a file of an operator's own over them
([docs/ANOMALIES.md](../../../docs/ANOMALIES.md)).
GEDCOM (`.ged`) files take a deterministic adapter in place of
transcribe/extract; the gates, resolve, and write are identical.

```sh
uv sync --dev            # from the repo root
uv run yizkr ingest --help
```

Connection settings come from `deploy/.env` (same variables as the
smoke test); the vocabulary comes from `model/manifest.yaml` resolved
against the live instance (run `model/bootstrap.py` first).

## Ingestion host prerequisites

The machine that runs `ingest run` needs, besides the stack in `deploy/`:

- **Python 3.11+ and uv.** `uv sync --dev` from the repo root installs
  the package.
- **An LLM engine** for vision transcription and extraction: the Claude
  Code CLI, logged in (`claude-code`), or `uv sync --extra llm` with
  `ANTHROPIC_API_KEY` set (`anthropic`). See
  [LLM providers](#llm-providers---llm).
- **The PDF renderer, from poppler-utils.** Vision transcription of a PDF
  renders its pages with `pdftoppm`, and the transcription check counts
  them with `pdfinfo`. Install it with
  `sudo apt-get install poppler-utils` (Debian/Ubuntu) or
  `brew install poppler` (macOS), and check it with `pdftoppm -v`.
  Without it, `ingest run` refuses to start when the folder holds a
  PDF, with that instruction, before any document is touched: it checks
  once per run, since a PDF needs the renderer even when its
  transcription comes from the LLM cache, and `pdfinfo` even when its
  transcription is a view the run reuses and checks again. Images need
  no renderer, and neither does a PDF read page by page (below), which
  gives the engine images, but whether a PDF is read whole is known only
  once its pages are read, so the check covers every PDF.
- **Nothing to install for PDF text layers.** A PDF's pages are read with
  pypdfium2 (Apache-2.0 or BSD-3-Clause), a dependency in
  `pyproject.toml` whose wheels bundle PDFium, Chromium's PDF engine
  (BSD-3-Clause): offline, in the pipeline's own process.

## The corpus and the never-ingest list

The corpus lives outside the repository, in the private folder named by
`$YIZKR_CORPUS_DIR`, the default folder for `ingest run`.
`$YIZKR_EXCLUDE_FILE` names the never-ingest list. Both are set in
`deploy/.env` or the environment; the setup and the list's format are in
[deploy/README.md](../../../deploy/README.md#the-corpus-and-the-never-ingest-list).

- **Fail closed.** `ingest run`, like any later production or extract-all
  mode, refuses to start without a list; an empty file is an explicit,
  allowed list. A configured list that is missing or unreadable, or that
  holds a line which would silently match nothing, is an error.
- **One shared check.** Every entry point that reads source documents asks
  `yizkr.core.corpus.is_excluded()` before it uses a document's content.
  `ingest run` drops a listed document at discovery, so it gets no
  artifact, no transcription and no state. The single-file steps
  (`register`, `transcribe`, `extract`, `gedcom`, `detect-faces`) run
  without a list, but refuse a listed file once one is configured. For
  `extract`, that is the media its transcription names: by its SHA-1,
  and by name and hash through `--media`, the media file, which must be
  the one the transcription names. A transcription keeps no file name,
  so with a list that names files by pattern, `extract` refuses to run
  without `--media`. New entry points must
  call it too: write-time registration (#103), transcription import
  (#104), the run driver (#118) and faces (#160).

## The stages, individually

The stages below are what `ingest run` orchestrates for you. Each is also a
standalone `yizkr ingest <step>` command (see `--help`) for debugging or
re-running one step in isolation — but for a normal ingest you only ever run
`ingest run` and fill the two gates above.

- **register** — uploads media (idempotent by content; a wiki file is
  never overwritten or stored twice, ADR-0010), ensures the
  artifact item (`instance of`, `media file`, `media SHA-1`) and optional
  page fragments (`part of` + page qualifier). Statement provenance:
  `stated in → the artifact itself` (see docs/MODELING.md, "Artifacts
  and registration"). Claims cite an artifact by the SHA-1 of its media,
  never by QID, and the write resolves that key through the `media
  SHA-1` statement registration writes, so a batch written to another
  instance, or after a reset, finds its artifacts there once they are
  registered. Registration and resolve (below) are the steps before the
  write that use the wiki: registration until write-time registration
  (#103) takes its place, and resolve's label match until the
  identity-map writer (#109).
- **derived media** (decks) — records the images a deck embeds in the
  deck's state directory, not on the wiki: `derived-media.json` keys each
  distinct image by the SHA-1 of its bytes (ADR-0010), with its names in
  the deck, the slides it is on, and the deck's own SHA-1 and title, and
  `derived-media/` keeps its bytes, in a file named for that SHA-1. The
  record stands while it is there whole, made for the deck's bytes as
  they are now, and each image's file is a regular file (not a symlink)
  named for the image's SHA-1 and holding those bytes; otherwise it is
  made again, and only the files that are not what they should be are
  written. Registering and uploading the images (ADR-0008) is left to
  write-time registration (#103). Media other than images are counted
  and reported, not recorded. A PDF read page by page (below) records
  the images placed within its pages in the same record, by its
  transcription stage, each named for its places ("page 6, image 1")
  with the pages it is on; they are not registered, since no claim cites
  them yet.
- **transcribe** — engines: `llm` (LLM-vision, ADR-0004), `textfile`
  (wrap an existing/manual transcription; keeps the pipeline fully
  local), `htr` (a Transkribus-style platform adapter, dropped as LLM
  vision is sufficient for handwriting: a stub that raises; #171). In
  `ingest run` and `scratch run`, a PDF with a page whose text layer is
  usable is read **page by page** (#176, `yizkr.pipeline.textlayer`):
  such a page from its text layer, deterministically, without a model
  call (the method `pdf-text-layer`, version 1); a scanned page (images
  cover most of it and it draws little text visibly, since an OCR layer
  is drawn invisibly), or one whose text layer is empty or garbage, by a
  vision call of its own; a blank page by nothing; and each image placed
  within a text-layer page by a vision call of its own, as a witness of
  its own (#178, ADR-0008), unless it is too small to hold text or the
  never-ingest list names its SHA-1. The PDF's transcription holds its
  own text, page by page behind `[Page N]` markers, with each image
  marked where it sits by its SHA-1, never what was read from it; its
  `pages` record each page's method, and each image's transcription is
  kept beside the image in `derived-media/`. A PDF with no such page,
  a scan, is read whole, by the one call it has always been read with,
  so the LLM cache and the transcription seed answer it as before. The
  standalone `ingest transcribe --engine llm` reads a PDF whole.
- **transcription check** — runs on the output of the `llm` and
  `textfile` engines (in `ingest run`, the vision and text-file methods)
  before anything uses it, in `ingest run` and `ingest transcribe`
  alike; slide text and a curator's correction are not checked. It is
  `yizkr.pipeline.transcription_check`, a step with its own version,
  apart from the transcription's. It runs on every transcription a run
  makes, from the engine or the LLM cache, and on every transcription
  view a run reuses, so a changed check applies to every transcription
  without asking the engine again for one it passes. A stored view it
  now fails is moved aside and made again, and a cached answer it fails
  is rejected in the cache and asked for once more (docs/LLM_CACHE.md).
  It fails the document when the output
  matches a tool-failure or refusal signature (the PDF renderer missing,
  a CLI or API error, "I can't transcribe this document"), or when a
  document of two or more pages yields fewer than 3 letters or digits per
  page. Editorial marks such as `[illegible]` don't count, and a single
  image has no floor, since a photograph may carry no text at all. A
  note that a printed email or letter could also hold ("I couldn't open
  the PDF", "I can't transcribe this", "unable to comply with your
  request") counts only in the engine's own voice: opening the output, a
  page section (after a "Page 2" or "[Page 2]" heading) or a line
  labelled "Note:", "NB:", "Error:" or "Warning:", or naming the page it
  skipped, and not about the correspondents' own attachment, writing or
  request ("the attachment you sent", "the old handwriting on the back of
  the photograph", "your request for copies of the register"). A page
  named by number or place as the subject or object of a failed read
  ("Page 2 could not be read", "I couldn't read the remaining pages")
  fails wherever it stands; other wordings count only where a note
  opens. The reason is recorded and the failed output is never passed
  on. A PDF read page by page is checked where the vision engine read
  it, each page and each image apart, as a single image: a page that
  fails fails the document, while an image that fails is left without
  a transcription of its own, which the stage's attention reports. Text
  read from a text layer is the document's own and is never checked.
- **extract** — engines: `llm` (prompts with the claim JSON Schema and
  the uncertainty rules; output is schema- and manifest-validated
  before it goes anywhere) or `file` (prepared claims JSON through the
  same validation). The `llm` engine sets every claim's source artifact
  itself, to the key its transcription names, whatever the model wrote
  there: the SHA-1 of the media transcribed (`ingest transcribe
  --file`, with the `textfile` engine too, never the text file; in
  `ingest run`, the source file's bytes). The prompts name the artifact
  by that key, never by QID, and ask each claim to cite it, so neither
  a transcription nor a prompt changes with the instance or a reset. A
  prepared file's claims cite their artifacts themselves. The GEDCOM
  adapter (`ingest gedcom`) cites the export's own bytes.
- **reconcile** — in `ingest run`, proposes nothing. A proposal matches
  one of the document's entities to an entity of a roster, and a stage
  before the write reads nothing from the wiki (ADR-0025 §2), so no roster
  of the graph, nor its place containment, reaches the step. It writes an
  `equivalences.csv` with no rows, and **Gate 1** has nothing to review.
  Matching against a kept graph's roster comes back as an explicit, keyed
  input (#130); across a scratch run's corpus, identity is corpus
  clustering's (#105). The standalone `yizkr ingest reconcile` step still
  reads the graph: it blocks a batch's entities against a roster of the
  whole graph (shared kind + name token), then an LLM adjudicates each
  candidate as same / uncertain / distinct. It passes the blocking
  neither the batch's claims nor the place containment, so the
  impossibility filter (ADR-0017) prunes next to nothing there. Only your
  `approve` rows of its CSV take effect, because a wrong merge corrupts
  every downstream attribution (identity is never auto-confirmed,
  ADR-0007).
- **resolve** — applies the approved equivalences, then replaces remaining
  `local` refs whose label exactly matches an existing item; anything
  fuzzier is left for the reviewer, who sees `NEW:<local_id>` vs a QID in
  the claim CSV.
- **export-review / apply-review** — one CSV row per claim for
  OpenRefine or any spreadsheet (R4). Rows are matched back by
  `claim_id`, a content hash: sorting or filtering the sheet cannot
  mis-apply decisions, and edited claim content is detected loudly.
  **Only explicit `approve` decisions pass** — no decision means no
  write; human confirmation is part of the data flow (R3).
- **accept-all** — writes a `*.reviewed.csv` from a gate CSV with the
  `decision` column pre-filled, so a curator can flip the few outliers
  instead of typing a decision on every row. It writes `<input-stem>`
  `.reviewed.csv` beside the input (so `equivalences.csv` →
  `equivalences.reviewed.csv`, `review.csv` → `review.reviewed.csv`) and
  detects the gate by the CSV's columns:
  - **claims** (`review.csv`): every row → `approve`.
  - **equivalences** (`equivalences.csv`): each row is set from its
    machine `verdict` — *ratify the verdict*, not a blanket approve:
    `same → approve`, `distinct → reject`, `uncertain → blank` (left for
    the human). An entity the machine calls `same` against two items is a
    contradiction it can't ratify, so those rows are left blank and
    reported. Distinct-judged pairs are therefore never silently merged.

  It is a deliberate human bulk-accept — each decision is written
  explicitly, per row, and the machine never runs it on its own — not
  auto-confirmation. Refuses to overwrite an existing reviewed file
  without `--force`.
- **write** — resolves each cited artifact's media SHA-1 to its artifact
  item (refusing the whole batch, before any write, if some media has
  no item), mints the extraction-run item, creates items for
  unresolved subjects (reusing exact-label matches), writes each claim
  at normal rank with two references: `stated in → artifact/fragment`
  (with page/region/excerpt locator qualifiers) and `inferred from
  extraction run → run item`. Re-runs skip existing statements.

## Resolving entities across artifacts

In `ingest run`, reconcile matches a document's entities against nothing
(above), so no Gate 1 row ties an entity to an item already in the graph.
**Resolve** still does, by exact label: it replaces a `local` ref whose
label exactly matches an existing item's, so a document meets the
artifacts written before it under the same names only. By default the
loop takes every document to the gates in one pass, before any is
written, so siblings in one folder run are invisible to each other, and
two artifacts naming one person land as two items. A scratch run leaves
identity to corpus clustering (#105), and #119 removes this per-document
path; until then, two mechanisms remain:

- **`yizkr reconcile-graph`** — a separate, curator-run pass that proposes
  duplicate *pairs among items already in the graph* (across every artifact
  and prior run). You review the proposals like an equivalences CSV;
  approved pairs are executed by you — a wiki item merge, or a referenced
  `said to be the same as` statement (identity of two existing items is a
  contestable judgement, ADR-0007). This is how "the Gustav from the birth
  register" and "the Gustav from the naturalisation" become one item under
  the default flow: run it **after** a folder ingest to consolidate
  cross-artifact duplicates.

- **`yizkr ingest run --serial`** (ADR-0019) — processes artifacts one at a
  time, most-connective first (a greedy overlap of their entity names, so a
  family tree or GEDCOM leads), each **fully written before the next
  reconciles**. A later artifact's "Gustav Löbig" then resolves to the
  item an earlier artifact wrote under that label. A variant name
  ("Gustl") is not matched at Gate 1, since reconcile reads no roster, so
  `reconcile-graph` stays the way to consolidate such pairs, as well as
  pre-existing dups, cross-run dups, and within-artifact dups the
  extraction manifest missed (serial is a *cross*-artifact fix).

  ```mermaid
  flowchart LR
      x["extract EVERY<br/>artifact"] --> s["sequence<br/>(hubs first)"]
      s --> a1["artifact 1<br/>· claim review · write"]
      a1 --> a2["artifact 2<br/>· claim review · write"]
      a2 --> a3["artifact 3 …<br/>each sees the prior"]
  ```

  The trade is rhythm: because each artifact must be written before the
  next reconciles, a serial run advances **one artifact per pass** — clear
  its claim review, re-run, next artifact: about one re-run per artifact,
  where the default takes every document to its claim review in one pass
  and writes them all in the next. In return each artifact resolves
  against what the earlier ones wrote. **Advancing the process =
  re-running `ingest run`** after you save a reviewed CSV; a future
  frontend (or a `--watch` mode) will drive those re-runs for you.

  Serial extracts every artifact up front (the sequencer needs all the
  manifests). That phase is parallelisable: `--extract-concurrency N`
  transcribes/extracts N artifacts at once. It multiplies with
  `--chunk-concurrency` (N × chunks concurrent LLM calls), so keep the
  product within your engine's limits — modest on the subscription
  `claude-code` engine (rolling usage windows), higher on `anthropic`
  (API rate limits). Wiki writes stay serialised, so only the LLM-heavy
  extraction overlaps. Default 1.

## Face proposals (`ingest/faces.py`, ADR-0005)

A face proposal is an ordinary claim — `depicts` on the photograph,
person as value, `relative position within image` qualifier, the
photograph as its own source — so proposals ride the same review flow
above, and only human-confirmed identifications reach the graph, with
dual provenance (photograph + region; detection run). The claim's
subject is the photograph's item (`--artifact`), and its source is the
photograph's bytes, by SHA-1 (`--file`), which the detections file
keeps as `photo_sha1`.

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
  a referenceless claim cannot be represented (ADR-0003).
- The source artifact is named by **content**, the SHA-1 of its media
  (`{"kind": "artifact", "sha1": ...}`), never by QID: the same batch
  holds on any instance and after a reset, and the writer resolves the
  key to the artifact item at write time (ADR-0010).
- Batches carry `extraction_run` metadata for the second, process
  reference (ADR-0003).
- There is **no rank field** — pipeline writes are always normal rank;
  preferring is a curator act on the wiki (ADR-0002).
- Time values carry explicit precision and calendar; ranges and
  "circa" are qualifiers, and inconsistent precision is rejected
  rather than silently narrowed or padded (docs/MODELING.md). A
  century is given by its first year as Wikibase counts centuries (the
  19th as 1801), and a century given by any other year is refused.

Properties and vocabulary items are referenced by **manifest key**
(`spouse`, `circa` — see `model/manifest.yaml`), never by local P/Q id:
ids are per-instance and only the writer resolves them, at write time.

```sh
uv run yizkr claims --json-schema           # JSON Schema, used in LLM prompts
uv run yizkr claims --validate batch.json   # schema + manifest check a file
```

## Privacy

No step talks to any external service by default; the whole pipeline
runs with `textfile` + `file` engines. Choosing `--engine llm` sends
artifact content — which may describe living people — to the
configured provider. There is no default credential and no fallback;
each provider has its own explicit opt-in (below).

## LLM providers (`--llm`)

Two Anthropic-backed providers, differing in how usage is billed:

- **`anthropic`** — the Anthropic API via the `anthropic` package
  (`uv sync --extra llm`). Token-based billing; setting
  `ANTHROPIC_API_KEY` is the opt-in.
- **`claude-code`** — the Claude Code CLI in headless mode
  (`claude -p`), using the CLI's own login, so a Claude Pro/Max
  **subscription** covers usage instead of per-token billing. Install
  the CLI and log in once; being logged in is the opt-in. The engine
  restricts the CLI to the Read tool only: `--tools Read` makes it the
  one tool the model is offered and `--allowedTools Read` the one tool
  approved, so images and PDFs are read from disk for vision and nothing
  can be executed or written. A call whose stream reports offering the
  model any other tool pauses the pass (`CliToolsMismatch`) and its
  output is discarded. Note subscriptions have usage windows (five
  hours, and weekly) rather than API rate limits. A call a limit refuses
  never fails a document: the engine asks again once the limit lifts, or
  pauses the pass (`UsageLimitReached`) with the time it can go on, and it
  paces its calls, from what the CLI itself reports of each window, to
  leave a reserve of it to development sessions
  (`$YIZKR_CLAUDE_RESERVE`, `$YIZKR_CLAUDE_MAX_WAIT`; docs/LLM_CACHE.md,
  Subscription limits) — best-effort, since the CLI reports a window's
  use only once one of its own warning thresholds is passed, so the
  reserve gives no protection below the first of those.

  A pass runs on one **pinned** CLI version (#133). The first client
  pins the installed version; every later client is checked when it
  starts, and every call against the version its stream reports. A
  mismatch (an update landed mid-pass, say from another Claude Code
  session's auto-update) raises `CliVersionMismatch`, which pauses the
  pass: it unwinds past per-document error handling, no further call is
  made, the call's output is discarded and completed outputs are kept.
  Run the pass again once the version is settled. The engine sets
  `DISABLE_AUTOUPDATER=1` for its own calls; disable auto-update in your
  interactive sessions too while a long pass runs.

  Calls are **isolated** from your own Claude Code configuration:
  `--setting-sources ""` (no user, project or local settings, so no
  hooks, permission rules, plugins or CLAUDE.md), `--strict-mcp-config`
  (no MCP servers, claude.ai connectors included),
  `--disable-slash-commands` (no skills), `--settings
  '{"autoCompactEnabled":true}'` (see below), and a child environment
  built from an **allowlist**. A call inherits only `PATH`; `HOME`,
  `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN` (the config
  directory, which holds the login, the global config and the session
  store); `TMPDIR`; and the proxy and certificate variables
  (`HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` in upper or lower case;
  `NODE_EXTRA_CA_CERTS`, `NODE_USE_SYSTEM_CA`, `SSL_CERT_FILE` and
  `SSL_CERT_DIR`). Nothing else passes from the pipeline's environment:
  not a stray API key, which would switch billing; not what a Claude
  Code session that launches the pass exports to it; and not the dozens
  of variables the CLI reads that change a call, such as the thinking
  switches (`MAX_THINKING_TOKENS`), the prompt-caching and cache-TTL
  switches (`DISABLE_PROMPT_CACHING*`, `ENABLE_PROMPT_CACHING_1H`,
  `FORCE_PROMPT_CACHING_5M`, which change the cost forked extraction
  relies on), tool search (`ENABLE_TOOL_SEARCH`) and compaction
  (`DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`), nor any a later CLI
  version adds. `SHELL` and the locale stay behind too: the CLI names
  the shell in the model's context and formats some numbers there with
  its runtime's default locale. Empty variables are left out, as unset.
  The CLI would take `CLAUDE_CONFIG_DIR` raw, without expanding `~` and
  reading a relative value against its own working directory, so a
  client normalises it once when it starts (`~` is your home directory,
  and a relative value is taken from the pipeline's working directory)
  and passes the absolute path on. A relative `HOME` is refused
  (`CliHomeRelative`).
  On top, the engine sets `DISABLE_AUTOUPDATER`, `DISABLE_GROWTHBOOK`
  (no server-side feature flags), `CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000`
  (see below), `CLAUDE_CODE_DISABLE_AUTO_MEMORY`,
  `DISABLE_ERROR_REPORTING` and `DISABLE_TELEMETRY`. Calls run in
  `<tmp>/yizkr-claude-code`.

  The config directory still reaches a call. Its **global config**
  (`.config.json` in the config directory if that exists, else
  `.claude.json` in `$CLAUDE_CONFIG_DIR`, or in your home directory when
  that is unset) has an `env` map that the CLI copies into its own
  environment at every start, past both `--setting-sources` and the
  allowlist. So a client refuses to start, and the pass pauses before
  any further call, while that map is not empty (`CliConfigEnvironment`);
  the error names the file and the variables, never their values. Keep
  such variables in your shell, or in the `env` block of your user
  settings (`settings.json` in the config directory), which pipeline
  calls do not load. The CLI also falls back to the global config for a
  few settings; the one that changes a call, `autoCompactEnabled`, is
  pinned on by the `--settings` argument, which outranks that fallback.
  The **login** in the config directory decides the account and how it
  is billed. The global config also caches the **account data** the
  server sends at every start, which your own sessions refresh too. It
  can change the system-prompt text, the output-token budget, the
  default model and the auto-compact windows, and no local setting keeps
  it out. The engine pins what it can: `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
  takes the budget out of its reach (64000, the Claude 5 default, capped
  at the model's ceiling in the CLI's built-in model catalog, so the
  budget follows the CLI version and the model alone); `--tools Read`
  keeps out the tools it can add; and an explicit `--model` wins over
  its default model. The rest is outside the pipeline's control.

  Machine-managed settings under `/etc/claude-code` always apply to the
  CLI: they are machine policy, outside the pipeline's control.

  The client's `effective_config` lists what the engine sets for a call:
  the flags (the pinned settings among them), the working directory, the
  variables it sets and the names of the variables passed, never their
  values. `config_hash` fingerprints it for the run record and for the
  LLM cache's keys (ADR-0032). It follows the working directory, and so
  `TMPDIR`, but not the login, the server's account data, managed
  settings or the machine: a run with another `TMPDIR` asks the engine
  again, and a change the server makes is not seen by the cache.

  **Transcripts.** The CLI stores each session under
  `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/`); sessions
  stay persistent because forks resume from there. Once a session is
  finished with, the engine moves its transcript into
  `$YIZKR_CLAUDE_TRANSCRIPTS` (a directory, normally in the run record)
  or, when that is unset or `delete`, deletes it: a one-shot call's or a
  fork's when the call returns, a conversation's when it is closed or
  its client is garbage-collected, or at process exit. A process killed
  outright can leave some behind, in the project directory the CLI
  names after the working directory (`-tmp-yizkr-claude-code` when the
  temporary directory is `/tmp`).

Pick the model for either provider with `$YIZKR_LLM_MODEL`.

## Tests

`make test` runs the offline suite (fakes for LLM and detector, no
instance needed). `make test-live` runs the encoded end-to-end flows
against the running stack — including the overwrite regression that two
values on one property must coexist.
