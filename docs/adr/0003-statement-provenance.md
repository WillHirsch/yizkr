# ADR-0003: Statement provenance conventions

Status: accepted
Amended by [ADR-0035](0035-mention-records.md) (owner decision
2026-09-29, #385): the extraction-run reference of a machine-written
statement may carry a judgement note, a category and one sentence on how
the extraction read the source, never used to rank or filter a value
(ADR-0035 §4).
Amended by #424 (2026-10-02): a page's locator may carry, beside the
physical page number, the page number printed on that page, exactly as
the transcription record gives it (`printed page number`, ADR-0037 §2).
Amended by #607 (2026-10-24, the owner's decision of 2026-10-23): a
citation is a **located passage**. A claim's `transcription_excerpt`, a
container's `evidence` in a place's mention record (ADR-0035 §2.2) and
the excerpt the place stage writes on a `part_of` link (ADR-0038 §8.5)
are each the words a reference quotes plus where they stand in the
source: their lines and, where the reference gives one, its page, found
by `core/verbatim.py` (`Transcript.spans`). A citation supports a
statement when the passage, read where it stands, conveys the fact the
statement records: its value and any time it carries. The passage itself
must carry what the statement adds, such as the container's name or the
value; its context says how to read it: which document or form, which
field, whose entry. So a form's field line conveys the containment of
the place the form gives it for, by its place in the form. It passes:
on the 1941 death certificate's line "Место смерти: город, селение д.
Кошновка; район Калдовский; область, край Чадская; республика РСФСР.",
"район Калдовский" stands in the district slot of a place of death whose
village is Koshnovka, and conveys that Koshnovka is in Kaldovsky
District; so does "District: Kaldovsky District" in the district field
of the same form rendered one field a line. It fails: a passage that
only names both places (a list of towns), and a bare "Fläckfeld" beside
"Flörburg, Kreis Peiltal", where "Kreis Peiltal" is about Flörburg. The
location the reference names counts: a claim that gives a page is read
at its occurrences on that page, where the transcription numbers it (an
image excerpt's claims cite the page of the container it was found on). Presence comes first: an excerpt the
transcription does not hold stands nowhere (the excerpt check of #580,
ADR-0023). The page the place stage writes on a link is that of the
evidence's first occurrence (`places.evidence_page`), an approximation
the principle does not require. The extraction prompts, the critic
(ADR-0013, ADR-0023) and the excerpt check follow this principle; the
critic applies it as one support test.

## Context

R2 requires each unit of knowledge to be traceable to the specific
part of the specific artifact asserting it. Machine ingestion (R3)
additionally requires process provenance so any statement can be
audited back to the pipeline run that produced it.

## Decision

1. Every statement carries a reference: stated in → artifact or
   artifact-fragment item, with locator qualifiers (page; region as a
   percentage bounding box, or a IIIF canvas+xywh URI from Phase W2),
   and optionally a short supporting transcription excerpt. Amended by
   #424 (2026-10-02): the page is the physical page, and the page number
   printed on it goes beside it as a `printed page number`, where the
   transcription record gives one.
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
