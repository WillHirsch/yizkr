# Roadmap

Tasks are sized for a single focused working session. Definition
of done for every task: runnable, tested where applicable, docs
updated, and no path writes an unreferenced statement.

## Phase W1

1. **Infrastructure** – Docker Compose for Wikibase Suite (MediaWiki +
   Wikibase, WDQS, Elasticsearch) with an `.env.example`, a README
   runbook for home-server deployment, and a smoke-test script that
   verifies item creation and SPARQL round-trip.
   *Done — see `deploy/`.*
2. **Model bootstrap** – `model/manifest.yaml` declaring the core
   properties/classes from docs/MODELING.md, plus an idempotent Python
   script (WikibaseIntegrator) that creates/updates them and records
   the local–Wikidata property mapping on a wiki page.
   *Done — see `model/`.*
3. **Claim schema** – Pydantic models for the pipeline's claim JSON
   (entity refs, property, value, uncertainty fields, fragment
   locator, extraction-run metadata), with validation tests. This is
   the contract between extraction and writing.
   *Done — see `ingest/claims.py`.*
4. **Ingestion skeleton** – `ingest/` package with pluggable steps:
   artifact registrar (media upload + artifact/fragment items),
   transcription (LLM-vision first; HTR platform adapter stub),
   claim extractor (LLM → claim JSON), reviewer export (CSV for
   OpenRefine), and writer (claims → statements per ADR-0003).
   *Done — see `ingest/` (plus a minimal exact-label resolve step).*
5. **Face proposals (W1 minimal)** – detection script emitting
   candidate depicts rows into the same review CSV flow (ADR-0005).
   *Done — see `ingest/faces.py` (detect/enroll/match commands).*
6. **Query library** – SPARQL queries checked in and embedded on wiki
   pages: disagreement report (properties whose values differ across
   sources – the flagship R2 view), per-person timeline, places map,
   unreferenced-statement QC report.
   *Done — see `queries/` (plus an artifact-assertions inventory).*
7. **Backup/export** – scheduled RDF dump + media backup job, with a
   living-persons-excluded public export variant (empty for now).
   *Done — see `backup.py`; `yizkr backup` (database + media + RDF +
   conservative public export), cron runbook in deploy/README.md.*

### Hardening items from the first trial ingestion

8. **Chunked extraction** – dense documents (a 60-person family tree)
   overflow a single model response; continuation turns are reassembled
   and salvaged today, but per-unit extraction with batch merge is the
   structural fix. Specified in ADR-0006 (semantic segmentation →
   packed chunks → NDJSON emission with sentinel → deterministic merge;
   per-chunk state with resume, subsuming raw-dump re-parse).
   *Done — see `pipeline/chunking.py`; `ingest extract --strategy`.*
9. **Cross-document entity reconciliation** – exact-label resolve
   duplicates people across documents ("Samuel Genserowski" vs "Samuel
   Moses Genserowski"). Specified in ADR-0007: entity-first extraction
   (per-document alias-unified entity manifests, which also make claim
   ownership structural within chunks), then deterministic candidate
   blocking + LLM relational adjudication against a prefetched graph
   roster, emitting equivalence proposals for human review — approved
   matches resolve to existing items before write; a separate
   occasional pass proposes merges/`said to be the same as` for
   duplicates already in the graph.
   *Done — see `pipeline/reconcile.py`; `ingest reconcile`,
   `resolve --equivalences`, `reconcile-graph`.*
10. **Living-person policy at ingestion** – partially done: the entity
    manifest's `extract: false` flag (ADR-0007) keeps logistics-context
    people out of extraction. Still open: review CSV warns when a
    subject has no death data and recent activity, and the
    `living person` flag gets set as part of curation. Vocabulary
    session for the observed gaps: artifact types (genealogical
    report, address book), occupation, sibling, cause of death
    (quantity datatype support for ages/counts).
11. **Review corrections** – the CSV review flow is approve/reject
    only; ambiguous machine readings (date-format ambiguity) currently
    force rejection. Add an edit/correction column that produces a
    curator-attributed replacement claim.
12. **Travel-notes re-ingestion + duplicate cleanup** – re-extract the
    travel notes entity-first (the trial batch predates the `extract`
    flag and wrote duplicate people), apply the already-reviewed
    equivalences, and write — the first live exercise of the writer's
    alias-append path. Execute the four approved duplicate merges from
    `reconcile-graph` (Samuel, Rosa, Alex, Salomon) and review the
    place candidates.
13. **GEDCOM adapter** – structured genealogy exports must never ride
    the generative path (ADR-0006 context): a deterministic
    GEDCOM → ClaimBatch adapter feeding the existing validation/review
    pipeline via `ClaimFileExtractor`. Exact, complete, free,
    reproducible; the Ancestry family tree becomes its first fixture.
14. **Parallel chunk execution** – chunks are independent by design;
    v1 runs them sequentially. Add a concurrency flag (the 49-chunk
    travel-notes run made the wall-clock cost concrete). Same session:
    batch the reconciliation adjudication call — 369 pairs through one
    response is the same output-bound fragility ADR-0006 removed from
    extraction, one level up.
15. **Folder/batch orchestration** – `yizkr ingest run <folder>`:
    register → transcribe → extract → reconcile → resolve → pause for
    review → write, with per-document state (the chunk manifest
    already provides the progress unit) and safe re-runs.
16. **Second trial: the untested half** – a real photograph through
    the faces/depicts flow (never live-run), a handwritten certificate
    (HTR is a stub; triage the gap), and a German-language document.
    Gate further feature work on its findings, as the first trial
    gated this batch.

### Agreed working order

7 (backup) → 12 (re-ingestion + merges) → 13 (GEDCOM) → 14 (parallel
chunks + batched adjudication) → 15 (folder orchestration) → 16
(photo/certificate/German trial). Items 10 and 11 ride along with
whichever of these first touches the review flow. Deferred by explicit
ADR decision, revisit on trigger: MCP/agentic graph access (roster
outgrows a prompt, or interactive curation), slimmed per-chunk prompt
schema, automated `said to be the same as` writing (curator-only by
design), modeling guidance for collective manifest entities.

## Phase W2 (IIIF tier) – later

Cantaloupe + OpenSeadragon/Mirador; W3C annotation store; migrate
fragment locators to canvas+xywh URIs; annotation-driven curation UI.

## Phase W3 (immich tier) – later

immich deployment; person–QID mapping table; sync bot writing depicts
statements; family onboarding notes.

## Model extensions – later

Cross-domain coverage relations on artifacts (docs/MODELING.md,
"Artifact coverage"). First up, **georeferenced maps**: a `map`
artifact subclass plus bounding-box corner properties (NW/SE,
globe-coordinate) so map artifacts carry their geographic extent and
frontends can draw their outlines on interactive maps; the places-map
query grows an artifact-outline variant. Manifest + bootstrap +
query in one session; W2 later upgrades the flat bounding box to
IIIF Georeference (Allmaps-style) annotations. The same pattern —
reuse existing structured properties, reference the artifact itself
or the producing run — stays open for temporal and subject coverage
and other cross-domain relations.

## Frontend ladder – later

Static publish tier (scheduled SPARQL → static site with map browse
including georeferenced artifact outlines, TimelineJS feeds,
galleries) → custom plural-display SPA (timeline rendering coexisting
date claims side by side, labeled by source).
