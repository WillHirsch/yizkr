"""Entity resolution step (W1 minimal): local refs -> existing QIDs.

Replaces `local` references whose label exactly matches an existing
item's English label with `qid` references. Deliberately conservative —
exact label match only; anything fuzzier needs the human reviewer, who
sees the resolution outcome in the review CSV (resolved QID or
"NEW:<local_id>") and can reject claims that would create duplicates.

A ref of an artifact kind (artifact, or a class the manifest makes a
`subclass_of` it), by the batch's entity-manifest record of its id when
there is one and otherwise by its own `instance_of`, is never resolved by
its label: an artifact is never matched by its title or label (ADR-0010),
and the writer decides what such a ref becomes (yizkr.pipeline.writer,
step 2). Nor is any other ref resolved to an artifact or a part of one
(`is_artifact_item` in yizkr.pipeline.writer): the labels a batch is
resolved against are the instance's item labels without those items'
(`resolvable_labels`), so such a ref stays local and the writer gives
it an item of its own (ADR-0010).

Unresolved local refs are left in place: the writer creates new items
for them at write time.
"""

from __future__ import annotations

from yizkr.claims import Claim, ClaimBatch, ItemValue, LocalRef, QidRef
from yizkr.pipeline.review import entity_records
from yizkr.pipeline.writer import artifact_kinds, is_artifact_item


def resolvable_labels(client) -> dict[str, str]:
    """The item labels of the instance `client` reads (`label_index`),
    label -> QID, that a ref is resolved against: every item's but an
    artifact's or a part of one's (is_artifact_item), which no ref is
    resolved to by its label (the module docstring)."""
    properties, items = client.vocabulary()
    _, labels = client.label_index()
    artifacts = {
        qid
        for qid, entity in client.iter_entities("labels|claims")
        if is_artifact_item(entity.get("claims") or {}, properties, items)
    }
    return {
        label: qid for label, qid in labels.items() if qid not in artifacts
    }


def _resolve_ref(ref, label_to_qid: dict[str, str], is_artifact):
    if (
        isinstance(ref, LocalRef)
        and ref.label
        and ref.label in label_to_qid
        and not is_artifact(ref)
    ):
        return QidRef(qid=label_to_qid[ref.label])
    return ref


def _resolve_claim(
    claim: Claim, label_to_qid: dict[str, str], is_artifact
) -> Claim:
    def resolved(ref):
        return _resolve_ref(ref, label_to_qid, is_artifact)

    updates: dict = {"subject": resolved(claim.subject)}
    if isinstance(claim.value, ItemValue):
        updates["value"] = ItemValue(value=resolved(claim.value.value))
    updates["qualifiers"] = [
        q.model_copy(
            update={"value": ItemValue(value=resolved(q.value.value))}
        )
        if isinstance(q.value, ItemValue)
        else q
        for q in claim.qualifiers
    ]
    # the source cites its artifact by content, which resolves only at
    # write time
    return claim.model_copy(update=updates)


def resolve_batch(
    batch: ClaimBatch, label_to_qid: dict[str, str]
) -> ClaimBatch:
    """Return a copy of the batch with exact-label local refs resolved,
    but those of an artifact kind (the module docstring)."""
    # a ref's kind is its record's, as the writer reads the record
    # (entity_records), and otherwise its own
    records = entity_records(batch)
    kinds = artifact_kinds()

    def is_artifact(ref: LocalRef) -> bool:
        record = records.get(ref.local_id)
        return (record.kind if record else ref.instance_of) in kinds

    return batch.model_copy(
        update={
            "claims": [
                _resolve_claim(c, label_to_qid, is_artifact)
                for c in batch.claims
            ]
        }
    )
