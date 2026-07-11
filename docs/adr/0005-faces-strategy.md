# ADR-0005: Face-region strategy

Status: accepted

## Context

R5: with initial manual inputs, detect faces in photographs and link
regions to person entities, with provenance on both the region and the
identity link.

## Decision

- Proposals vs assertions: detection output is a *proposal* in a
  review queue; only human-confirmed identifications become depicts
  statements in the graph.
- W1: a script (InsightFace-class model) emits candidate rows
  (photo, bbox, suggested person, score) to the review CSV; confirmed
  rows are written as depicts + relative-position qualifier, with a
  reference to the detection run and the confirming curator.
- W3: immich replaces the script as the detection/clustering engine
  ("initial manual inputs" = naming clusters); the sync bot writes the
  same statement shape.
- W2: regions may instead be W3C annotations on IIIF canvases, with
  the depicts statement referencing the annotation URI – same
  confirmation rule.
- Portability goal: support exporting confirmed regions as XMP/MWG
  region metadata embedded in the image files, so identifications
  survive outside this system.

## Consequences

- One statement shape regardless of engine; engines are swappable.
- No machine-only identifications ever enter the graph.
