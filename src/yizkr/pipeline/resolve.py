"""Entity resolution step (W1 minimal): local refs -> existing QIDs.

Replaces `local` references whose label exactly matches an existing
item's English label with `qid` references. Deliberately conservative —
exact label match only; anything fuzzier needs the human reviewer, who
sees the resolution outcome in the review CSV (resolved QID or
"NEW:<local_id>") and can reject claims that would create duplicates.

Unresolved local refs are left in place: the writer creates new items
for them at write time.
"""

from __future__ import annotations

from yizkr.claims import Claim, ClaimBatch, ItemValue, LocalRef, QidRef


def _resolve_ref(ref, label_to_qid: dict[str, str]):
    if isinstance(ref, LocalRef) and ref.label and ref.label in label_to_qid:
        return QidRef(qid=label_to_qid[ref.label])
    return ref


def _resolve_claim(claim: Claim, label_to_qid: dict[str, str]) -> Claim:
    updates: dict = {"subject": _resolve_ref(claim.subject, label_to_qid)}
    if isinstance(claim.value, ItemValue):
        updates["value"] = ItemValue(
            value=_resolve_ref(claim.value.value, label_to_qid)
        )
    updates["qualifiers"] = [
        q.model_copy(
            update={"value": ItemValue(value=_resolve_ref(q.value.value, label_to_qid))}
        )
        if isinstance(q.value, ItemValue)
        else q
        for q in claim.qualifiers
    ]
    updates["source"] = claim.source.model_copy(
        update={"artifact": _resolve_ref(claim.source.artifact, label_to_qid)}
    )
    return claim.model_copy(update=updates)


def resolve_batch(batch: ClaimBatch, label_to_qid: dict[str, str]) -> ClaimBatch:
    """Return a copy of the batch with exact-label local refs resolved."""
    return batch.model_copy(
        update={"claims": [_resolve_claim(c, label_to_qid) for c in batch.claims]}
    )
