# ADR-0004: Phased architecture (W1 → W2/W3)

Status: accepted

## Context

Three viable Wikibase-based permutations were identified, differing on
the artifact/anchoring layer, the faces engine, and the frontend. They
are phases and recombinables rather than rivals; nothing in the first
phase is throwaway.

## Decision

- **W1 – wiki-native minimal (now):** artifacts as wiki media on the
  instance; regions as percentage-bbox qualifiers; ingestion via
  LLM extraction → review CSV (OpenRefine) → bot writes; exploration
  via SPARQL result embeds (map/timeline/graph) on wiki pages.
- **W2 – IIIF tier (when documentary anchoring precision dominates):**
  Cantaloupe image server; OpenSeadragon/Mirador viewers;
  Annotorious/Recogito-style W3C Web Annotations; statements reference
  IIIF canvas+region URIs; face/fragment anchoring unified in one
  annotation mechanism.
- **W3 – immich tier (when photo throughput and family participation
  dominate):** immich owns the photographic corpus (mobile upload,
  face detection/clustering, two-click naming); a sync bot maps immich
  persons – QIDs and writes depicts statements with bbox qualifiers.
  W2 and W3 coexist (immich for photos, IIIF for documents).
- **Frontend ladder:** SPARQL embeds → static publish tier (scheduled
  SPARQL exports rendered as a static site: map browse, timelines,
  galleries) → custom plural-display SPA over SPARQL (+ IIIF in W2).

## Consequences

- Immediate progress without committing to the heavier tiers.
- The statement conventions (ADR-0003) are the stable contract all
  tiers write to; tiers can be added or swapped independently.
