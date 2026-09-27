# ADR-0005: Face-region strategy

Status: accepted
Amended by #226 (2026-09-27): the write step enforces the confirmation
rule instead of trusting its input (see Amendments).

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

## Amendments

Amended by #226 (2026-09-27): **the write step enforces the
confirmation rule instead of trusting its input.** The review gates
(#24) keep a whole-sheet accept from confirming a depicts claim, but
`yizkr ingest write --batch` wrote whatever batch it was given, a raw
detect-faces batch included. Now:

- `apply_review` records each per-row approval of a depicts claim on the
  approved claim itself: the claim contract's `row_approval` holds the
  claim's review-sheet id (`claim_id`, the content hash the sheet's rows
  are matched by). A bulk-accept never records one, since `apply_review`
  refuses it on a depicts claim.
- The writer (`BatchWriter.write_batch`, behind both `ingest write` and
  the orchestrator's write step) refuses a batch holding any depicts
  claim whose `row_approval` is not its own id as it now reads. It
  names those claims by id and refuses before it reads from or writes to
  the instance, in a dry run too. So no batch that a review sheet did not
  decide can write a depicts statement, whether face detection or a
  source's text proposed it, and an approved claim edited afterwards has
  to be decided again.
- The record lives in the batch because the batch file is all
  `ingest write` receives. It is review state, not extraction output:
  the JSON Schema that extraction prompts carry leaves it out, and a
  claim without one serialises as before. So prompts, LLM cache keys
  and claim ids are unchanged, and so is the content hash of every
  batch without an approved depicts claim. The record says that a
  per-row decision was made and applied; it does not authenticate the
  curator, and whoever edits a batch file by hand can forge it, as they
  can any content of that file.
- A batch without depicts claims is not affected. Scratch runs never
  write depicts: ADR-0025 has their write stage (#200) leave depicts
  claims out and count them, so the check never fires there. A depicts
  claim that reaches the writer by any unreviewed path is refused, not
  written.
