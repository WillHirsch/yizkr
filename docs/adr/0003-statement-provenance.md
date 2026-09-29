# ADR-0003: Statement provenance conventions

Status: accepted
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): the extraction-run reference of a machine-written
statement may carry a judgement note, a category and one sentence on how
the extraction read the source, never used to rank or filter a value
(ADR-0035 §4).

## Context

R2 requires each unit of knowledge to be traceable to the specific
part of the specific artifact asserting it. Machine ingestion (R3)
additionally requires process provenance so any statement can be
audited back to the pipeline run that produced it.

## Decision

1. Every statement carries a reference: stated in → artifact or
   artifact-fragment item, with locator qualifiers (page; region as a
   percentage bounding box, or a IIIF canvas+xywh URI from Phase W2),
   and optionally a short supporting transcription excerpt.
2. Machine-written statements carry a second reference: inferred from
   extraction run → an extraction-run item recording model
   name+version, date, prompt/pipeline version, and software commit.
3. Property constraints flag any statement lacking a reference; the
   disagreement/qc SPARQL reports treat unreferenced statements as
   defects.
4. Bots therefore create artifact, fragment, and extraction-run items
   *before* writing claims, in idempotent batches.

## Consequences

- Slightly heavier write path; fully auditable graph.
- Human verification is one click: statement → reference → fragment →
  image region/page.
- Source provenance and process provenance never conflated.
