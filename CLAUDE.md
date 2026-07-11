# CLAUDE.md – standing instructions

## What this project is

A self-hosted knowledge system for digitised historical family records.
Core store: **self-hosted Wikibase Suite**. The system ingests artifacts
(photographs, certificates, letters, census pages...), extracts
assertions via an LLM pipeline with human review, links face regions in
photographs to person entities, and presents everything through
interactive timelines, maps, and galleries.

Read `docs/REQUIREMENTS.md` and the ADRs in `docs/adr/` before making
architectural choices. `docs/MODELING.md` defines the data conventions.

## The core principle: neutral coexistence (source-first)

This database records **what sources assert**, never bare truth.
Multiple contradictory assertions about the same property are a
*healthy resting state*, not an error, a conflict to resolve, or a
"pending" condition. Resolution (preferring one assertion) is an
optional overlay that may never happen. See ADR-0002.

Acceptance test for any feature: *a property can hold zero canonical
values and N source-attributed values, and everything still works.*

## Non-negotiable rules

1. **Never write a statement without a reference.** Every statement
   carries a reference to the artifact (and fragment locator) that
   asserts it. See ADR-0003.
2. **Pipeline writes carry dual provenance.** Machine-written
   statements also reference an extraction-run item (model, date,
   pipeline version).
3. **Never overwrite or delete a source-attributed statement to
   "correct" it.** Corrections and preferences are *new* assertions or
   curator rank changes – the original attributed claim remains.
4. **Ranks default to normal.** Preferred/deprecated ranks are optional
   curation. No feature may *require* a preferred value to function.
5. **Uncertainty is structural, never silent.** Use date precision,
   earliest/latest qualifiers, and sourcing-circumstances qualifiers.
   Never round, guess, or narrow what a source says.
6. **Frontends are plural-by-default.** Display all attributed values
   with source labels (e.g. "moved to France: 1929 per source A;
   1930–1935 per source B"). Never silently pick one value.
7. **Privacy first.** Data includes living people. Nothing public by
   default; no personal data to third-party services without explicit
   instruction; self-hosted components only unless told otherwise.

## Engineering conventions

- Infrastructure: Docker Compose (Wikibase Suite and companions).
- Pipeline and bots: Python 3.12+, `WikibaseIntegrator` for writes,
  Pydantic for claim schemas, pytest for tests.
- Reads: SPARQL against the bundled query service.
- Bulk human review: CSV artifacts consumable by OpenRefine.
- Idempotency: all bootstrap/import scripts must be safely re-runnable.
- Docs move with code: update ADRs/MODELING.md in the same PR as any
  behavior change; new architectural decisions get a new ADR.

## What NOT to do

- Do not introduce a second store for assertions (the graph is the
  system of record; other services may hold *media* or *proposals*).
- Do not build anything that assumes a single canonical value per
  property.
- Do not auto-confirm machine proposals (extraction or faces); human
  confirmation is part of the data flow.
