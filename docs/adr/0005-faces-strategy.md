# ADR-0005: Confirmed face-region identification

Status: accepted. The [current contract](../CONTRACT.md) governs the confirmation boundary; the current CSV/write enforcement is described below.

## Policy

Face detection or manual marking identifies a region; matching proposes a person. A `depicts` assertion requires affirmative human confirmation of that region-to-person association. Several displayed associations may be confirmed together, but generic acceptance of a document or ingestion run must not silently confirm faces. Region creation, identity proposals and confirmation retain their respective provenance. A confidence score or successful validation is not approval.

Detection engines, photo services and annotation formats are replaceable means. Face automation is not a prerequisite for retaining useful text evidence. Rebuild runs never write `depicts` statements.

## Current workflow

`pipeline/faces.py` provides an optional local InsightFace detector, saved detections and a gallery of curator-enrolled embeddings. `detect-faces` produces detections and identity proposals; `enroll-face` adds a curated gallery example; `match-faces` can use saved detections without recomputing embeddings. Gallery enrollment proposes future matches; it does not confirm them.

A proposed claim has the photograph item as subject, `depicts` as property and the person as value, with a `relative position within image` qualifier (`x%,y%,w%,h%`). Its source identifies the photograph's bytes and region; a machine proposal also identifies the detection run. The shared review workflow records the human decision before a write. [MODELING.md](../MODELING.md#faces-and-depiction-see-adr-0005) distinguishes `depicts` from a source's `shows`, `caption` or associated `image` statements.

## Current enforcement and limits

- The CSV implementation requires an affirmative decision on each `depicts` row, such as `approve`. `accept-all` and `bulk-accept` cannot confirm one. An interface for affirmatively confirming several displayed associations is permitted by policy but is not implemented by those bulk commands.
- `apply_review` stores `row_approval`, a digest of the claim's review ID and each entity-manifest record its local references use. The writer checks the digest before reading or writing the instance, including during dry runs. Editing the claim or an involved manifest record invalidates this record of approval.
- Approval and writing use the same `entity_records` lookup. Conflicting references to a local ID without a manifest record also fail the approval check. Existing unresolved local references remain subject to the writer's identity-map requirements.
- The digest is review state carried in the batch, not an authenticated signature or a durable graph-revision binding. Confirmation is evidenced by the reviewed batch and edit history; the graph's source/run references alone do not identify every aspect of the human decision. A hand-edited batch can forge its contents.
- Durable identity, approval persistence and affected-change handling remain work under [#708](https://github.com/WillHirsch/yizkr/issues/708), [#130](https://github.com/WillHirsch/yizkr/issues/130) and [#142](https://github.com/WillHirsch/yizkr/issues/142). The existing gate stays until its replacement is implemented and verified.

IIIF annotations ([#166](https://github.com/WillHirsch/yizkr/issues/166)), a photo-service integration ([#167](https://github.com/WillHirsch/yizkr/issues/167)) and XMP/MWG export of confirmed regions ([#161](https://github.com/WillHirsch/yizkr/issues/161)) are optional follow-up capabilities. None changes the confirmation requirement.
