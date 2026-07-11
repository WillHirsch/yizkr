# Requirements

These six requirements are all **essential**. A tool or design that
cannot satisfy one of them elegantly is disqualified; the correct
response to a gap is to build at a lower level, not to soften the
requirement.

## R1 – Extensible, expressive knowledge graph

People, points in time and time periods, places, and artifacts as
first-class nodes, with typed relations between them. The schema must
be extensible without redesign (new entity types and properties can be
minted as needed).

## R2 – Assertion-level provenance, uncertainty, and contradiction

Every unit of knowledge is traceable to the *specific part* of the
specific artifact that asserts it. The model represents uncertain and
doubtful assertions as stated by their sources.

**Clarified stance (see ADR-0002):** the core requirement is to *see*
disagreements, not to resolve them. The system must hold a neutral
collection of potentially contradictory assertions – e.g. "person X
moved to France in 1929 per source A, in 1930–1935 per source B, and
married there in 1933 per the marriage record" – so a reader can weigh
the evidence even when no curator or algorithm has expressed a view.
Resolution mechanisms are a bonus feature, never a dependency.

## R3 – Automated LLM ingestion, first-class

Artifacts are ingested as-is and passed through an automated pipeline
(transcription → structured claim extraction → entity resolution →
review → write), with every written statement carrying both source and
process provenance.

## R4 – Accessible manual curation

Manual curation is facilitated in an accessible way alongside
automation, including for non-technical contributors.

## R5 – Face-region-to-person linking

With initial manual inputs, the system detects faces in photographs and
links those *regions* to person entities. The region and the identity
link both carry provenance (who/what proposed, who confirmed).

## R6 – Interactive public exploration without foreknowledge

Interactive presentation that requires no knowledge of the underlying
model or curation methods: graphical timelines, per-person image
galleries, and interactive maps of places in context. Displays are
plural-by-default (contradictory attributed values shown side by side,
labeled by source).
