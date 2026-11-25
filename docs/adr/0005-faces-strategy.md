# ADR-0005: Confirmed face-region identification

Status: accepted. The [current contract](../CONTRACT.md) governs the confirmation boundary; what is implemented today, proposals without a confirmed write, is described below.

## Policy

Face detection or manual marking identifies a region; matching proposes a person. A `depicts` assertion requires affirmative human confirmation of that region-to-person association. Several displayed associations may be confirmed together, but generic acceptance of a document or ingestion run must not silently confirm faces. Region creation, identity proposals and confirmation retain their respective provenance. A confidence score or successful validation is not approval.

Detection engines, photo services and annotation formats are replaceable means. Face automation is not a prerequisite for retaining useful text evidence. Rebuild runs never write `depicts` statements.

## Current workflow

`pipeline/faces.py` provides an optional local InsightFace detector, saved detections and a gallery of curator-enrolled embeddings. `detect-faces` produces detections and identity proposals; `enroll-face` adds a curated gallery example; `match-faces` can use saved detections without recomputing embeddings. Gallery enrollment proposes future matches; it does not confirm them.

A proposed claim has the photograph item as subject, `depicts` as property and the person as value, with a `relative position within image` qualifier (`x%,y%,w%,h%`). Its source identifies the photograph's bytes and region; a machine proposal also identifies the detection run. No route confirms and writes a face-region proposal until faces work resumes ([#160](https://github.com/WillHirsch/yizkr/issues/160)): the proposals stay in their batch file. [MODELING.md](../MODELING.md#faces-and-depiction-see-adr-0005) distinguishes `depicts` from a source's `shows`, `caption` or associated `image` statements.

## Current enforcement and limits

- The map-bound writer writes a `depicts` claim only with `row_approval`, a digest of the claim's id and each entity-manifest record its local references use, checked before it reads or writes the instance, including during dry runs; conflicting references to a local ID without a manifest record also fail the check. Nothing records that approval, so the writer refuses every `depicts` claim. The digest is review state carried in a batch, not an authenticated signature or a durable graph-revision binding. The gate goes with the map-bound writer ([#893](https://github.com/WillHirsch/yizkr/issues/893)).
- The [assignment-aware claim route](../IDENTITY_REVIEW.md#apply-reviewed-historical-claims) records durable claim decisions and requires explicit `depicts_confirmed` approval of the displayed association, rechecked at application; a general approval cannot supply it. It also requires supported current source products, literal evidence and accepted assignments, so a face-detection batch, which names the photograph by QID, names an enrolled person by QID where the gallery has one (otherwise by a local ref) and locates its evidence by image region with no excerpt, does not enter it. Confirming a face-region proposal through claim decisions is the work of [#160](https://github.com/WillHirsch/yizkr/issues/160). General retained correction and merge/split remain under [#142](https://github.com/WillHirsch/yizkr/issues/142).

IIIF annotations ([#166](https://github.com/WillHirsch/yizkr/issues/166)), a photo-service integration ([#167](https://github.com/WillHirsch/yizkr/issues/167)) and XMP/MWG export of confirmed regions ([#161](https://github.com/WillHirsch/yizkr/issues/161)) are optional follow-up capabilities. None changes the confirmation requirement.
