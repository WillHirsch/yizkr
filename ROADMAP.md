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
10. **Living-person policy at ingestion** – contemporary documents
    yield living-person claims with no flagging. Vocabulary-by-omission
    (no email/phone properties) is deliberate policy; add an
    ingestion-time counterpart: review CSV warns when a subject has no
    death data and recent activity, and the `living person` flag gets
    set as part of curation. Vocabulary session for the observed gaps:
    occupation, sibling, cause of death (quantity datatype support for
    ages/counts).
11. **Review corrections** – the CSV review flow is approve/reject
    only; ambiguous machine readings (date-format ambiguity) currently
    force rejection. Add an edit/correction column that produces a
    curator-attributed replacement claim.

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
