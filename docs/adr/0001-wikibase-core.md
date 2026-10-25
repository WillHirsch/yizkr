# ADR-0001: Self-hosted Wikibase Suite as the core knowledge store

Status: accepted

## Context

Six essential requirements (docs/REQUIREMENTS.md). A survey of candidate cores was performed: Gramps/Gramps Web (genealogy-native), OpenAtlas and ResearchSpace (CIDOC-CRM platforms), Omeka S + modules, custom RDF store (named graphs / RDF-star), custom claim nodes in a property graph (Neo4j-style), nodegoat (near-miss: strong on model, uncertainty and sources; no image-region annotation; hosted, not open source). No single system satisfies all six requirements; R2 (assertion-level provenance with coexisting contradictions) and R5 (face-region linking) disqualify most.

## Decision

Use **self-hosted Wikibase Suite** (MediaWiki + Wikibase, WDQS query service, Elasticsearch, via Docker) as the single system of record. Self-hosted rather than a hosted public instance because the archive contains living people's data.

## Rationale

- The Wikibase statement model (references + qualifiers + ranks) is the most battle-tested implementation of assertion-level provenance with natively coexisting contradictions (Wikidata scale; precedent: Enslaved.org for source-attributed historical persons).
- Ready-made accessible curation: statement editor, QuickStatements, OpenRefine reconciliation (R4).
- SPARQL endpoint and standard RDF export for longevity and for driving frontends (R6).
- Schema is extensible in-instance without redesign (R1).

## Consequences

- We operate a multi-service stack (acceptable; home-server ops experience exists).
- Public no-foreknowledge exploration requires a custom frontend layer (accepted as the one unavoidable build – true in every stack).
- Fragment/region anchoring is done by convention (ADR-0003) rather than a native mechanism.
- Faces need a companion engine (ADR-0005).
