# ADR-0005: Face-region strategy

Status: accepted  
Amended by #226 (2026-09-27): the write step enforces the confirmation rule instead of trusting its input (see Amendments).  
Amended by #258 (2026-09-28): the writer and the approval record read the entity manifest through one lookup (see Amendments).

## Context

R5: with initial manual inputs, detect faces in photographs and link regions to person entities, with provenance on both the region and the identity link.

## Decision

- Proposals vs assertions: detection output is a _proposal_ in a review queue; only human-confirmed identifications become depicts statements in the graph.
- W1: a script (InsightFace-class model) emits candidate rows (photo, bbox, suggested person, score) to the review CSV; confirmed rows are written as depicts + relative-position qualifier, with a reference to the detection run and the confirming curator.
- W3: immich replaces the script as the detection/clustering engine ("initial manual inputs" = naming clusters); the sync bot writes the same statement shape (#167).
- W2: regions may instead be W3C annotations on IIIF canvases, with the depicts statement referencing the annotation URI – same confirmation rule (#166).
- Portability goal: support exporting confirmed regions as XMP/MWG region metadata embedded in the image files, so identifications survive outside this system (#161).

## Consequences

- One statement shape regardless of engine; engines are swappable.
- No machine-only identifications ever enter the graph.

## Amendments

Amended by #226 (2026-09-27): **the write step enforces the confirmation rule instead of trusting its input.** The review gates (#24) keep a whole-sheet accept from confirming a depicts claim, but `yizkr ingest write --batch` wrote whatever batch it was given, a raw detect-faces batch included. Now:

- `apply_review` records each per-row approval of a depicts claim on the approved claim itself: the claim contract's `row_approval` holds a digest (`approval_digest`) of the claim's review-sheet id (`claim_id`, the content hash the sheet's rows are matched by) and of the batch's entity-manifest record for each of the claim's local refs, or of that ref having none. The writer resolves a local ref through its manifest record when there is one (ADR-0007): the record's `qid`, or else its label, names the item. So the claim's content alone does not fix whom it depicts, and the digest binds both. A bulk-accept never records one, since `apply_review` refuses it on a depicts claim.
- The writer (`BatchWriter.write_batch`, behind both `ingest write` and the orchestrator's write step) refuses a batch holding any depicts claim whose `row_approval` is not its digest against the batch as it now reads. It also refuses one with a local ref that has no manifest record when another ref in the batch to the same local id differs from it: the writer makes such an id into an item from the first ref it meets, so the item depicted would depend on another claim and the batch's order. It names those claims by id and refuses before it reads from or writes to the instance, in a dry run too. So no batch that a review sheet did not decide can write a depicts statement, whether face detection or a source's text proposed it. An approved claim has to be decided again when it is edited afterwards, or when a manifest record one of its local refs resolves through is added, removed or changed in any field, such as a `qid` set or a `label` changed.
- The record lives in the batch because the batch file is all `ingest write` receives. It is review state, not extraction output: the JSON Schema that extraction prompts carry leaves it out, and a claim without one serialises as before. So prompts, LLM cache keys and claim ids are unchanged, and so is the content hash of every batch without an approved depicts claim. The record says that a per-row decision was made and applied, for the claim and records as they then read; it is not a signature and does not authenticate the curator. Whoever edits a batch file by hand can recompute it, as they can forge any content of that file. Nor does it pin the instance: a local ref whose record gives no `qid`, or that has no record, resolves to the item that carries its label when the batch is written, or to a new one.
- A batch without depicts claims is not affected. Scratch runs never write depicts: ADR-0025 has their write stage (#200) leave depicts claims out and count them, so the check never fires there. A depicts claim that reaches the writer by any unreviewed path is refused, not written.

Amended by #258 (2026-09-28): **one lookup of the entity manifest.** The writer and `approval_digest` read a batch's entity-manifest records through one function, `entity_records` in yizkr.pipeline.review. When two records share an id, which manifest validation refuses but a hand-edited batch file can hold, the last one counts for both: the approval binds the record the writer resolves the local ref through, and the other record leaves the approval as it is, resolves no local ref, and adds no alias or kind. It is still part of the batch file, which the extraction-run item's fingerprint covers whole, so editing it gives the batch another run item. A change to how the lookup picks a record, such as letting the first win or refusing a duplicate id, changes both at once. The rule for a local ref without a record compares whole refs: another ref to its id that differs only in its description or kind voids the approval as one that differs in its label does, and a ref that is the value of one of the claim's qualifiers is held to the same rule.
