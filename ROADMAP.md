# Roadmap

Tasks are sized for a single focused Claude Code session. Definition
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
4. **Ingestion skeleton** – `ingest/` package with pluggable steps:
   artifact registrar (media upload + artifact/fragment items),
   transcription (LLM-vision first; HTR platform adapter stub),
   claim extractor (LLM → claim JSON), reviewer export (CSV for
   OpenRefine), and writer (claims → statements per ADR-0003).
5. **Face proposals (W1 minimal)** – detection script emitting
   candidate depicts rows into the same review CSV flow (ADR-0005).
6. **Query library** – SPARQL queries checked in and embedded on wiki
   pages: disagreement report (properties whose values differ across
   sources – the flagship R2 view), per-person timeline, places map,
   unreferenced-statement QC report.
7. **Backup/export** – scheduled RDF dump + media backup job, with a
   living-persons-excluded public export variant (empty for now).

## Phase W2 (IIIF tier) – later

Cantaloupe + OpenSeadragon/Mirador; W3C annotation store; migrate
fragment locators to canvas+xywh URIs; annotation-driven curation UI.

## Phase W3 (immich tier) – later

immich deployment; person–QID mapping table; sync bot writing depicts
statements; family onboarding notes.

## Frontend ladder – later

Static publish tier (scheduled SPARQL → static site with map browse,
TimelineJS feeds, galleries) → custom plural-display SPA (timeline
rendering coexisting date claims side by side, labeled by source).
