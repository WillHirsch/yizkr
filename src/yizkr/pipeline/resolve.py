"""Entity resolution step (W1 minimal): local refs -> existing QIDs.

Replaces `local` references whose label exactly matches an existing
item's English label with `qid` references. Deliberately conservative —
exact label match only; anything fuzzier needs the human reviewer, who
sees the resolution outcome in the review CSV (resolved QID or
"NEW:<local_id>") and can reject claims that would create duplicates.

A ref's own label is matched in the form the instance keeps a label
in, as the writer sends and looks for a label (`_term_text` in
yizkr.pipeline.writer: trimmed, one line, cleaned up, in Unicode NFC,
cut to the characters the instance holds), against the labels the
instance serves, which are in that form. So a ref's own label given in
NFD, or with whitespace around it, or longer than the instance holds,
resolves to the item under that label in the instance's form, and the
review CSV shows that item, not NEW.
A label holding a character Python's Unicode tables do not know
(`unknown_characters`) resolves to nothing: the instance may keep it in
another form, and the writer matches no item by it either.

A ref on the artifact side (of an artifact kind, artifact or a class
the manifest makes a `subclass_of` it, or of artifact fragment;
`is_artifact_kind` in yizkr.pipeline.writer), by the batch's
entity-manifest record of its id when there is one and otherwise by its
own `instance_of`, is never resolved by its label: an artifact is never
matched by its title or label (ADR-0010), and the writer decides what
such a ref becomes (yizkr.pipeline.writer, step 2). Nor is any other
ref resolved to an artifact or a part of one (`is_artifact_item` in
yizkr.pipeline.writer): the labels a batch is resolved against are the
instance's item labels its refs carry without those items'
(`resolvable_labels`), so such a ref stays local and the writer gives it
an item of its own (ADR-0010).

What resolving a batch reads of the instance: its item labels once
(`label_index`), and the statements of just the items the batch's refs
name by label (`fetch_claims`), never every item's.

Unresolved local refs are left in place: the writer creates new items
for them at write time.
"""

from __future__ import annotations

from yizkr.claims import Claim, ClaimBatch, ItemValue, LocalRef, QidRef
from yizkr.pipeline.review import entity_records
from yizkr.pipeline.writer import (
    _term_text,
    is_artifact_item,
    is_artifact_kind,
    local_refs,
    unknown_characters,
)


def resolvable_labels(client, batch: ClaimBatch) -> dict[str, str]:
    """The item labels of the instance `client` reads (`label_index`),
    label -> QID, that the refs of `batch` are resolved against: of the
    labels its local refs not on the artifact side carry, in the form the
    instance keeps a label in (_ref_label), each the instance holds, but
    an artifact's or a part of one's (is_artifact_item), which no ref is
    resolved to by its label (the module docstring). The labels are read
    once, and the statements of only the items those labels name
    (`fetch_claims`)."""
    is_artifact = _artifact_side(batch)
    wanted = {
        label
        for claim in batch.claims
        for ref in local_refs(claim)
        if (label := _ref_label(ref)) and not is_artifact(ref)
    }
    _, labels = client.label_index()
    matched = {label: labels[label] for label in wanted if label in labels}
    if not matched:
        return {}
    properties, items = client.vocabulary()
    claims = client.fetch_claims(sorted(set(matched.values())))
    return {
        label: qid
        for label, qid in matched.items()
        if not is_artifact_item(claims.get(qid) or {}, properties, items)
    }


def _artifact_side(batch: ClaimBatch):
    """Is a local ref of `batch` on the artifact side (the module
    docstring)? A ref's kind is its record's, as the writer reads the
    record (entity_records), and otherwise its own."""
    records = entity_records(batch)

    def is_artifact(ref: LocalRef) -> bool:
        record = records.get(ref.local_id)
        return is_artifact_kind(record.kind if record else ref.instance_of)

    return is_artifact


def _ref_label(ref: LocalRef) -> str:
    """The label a local ref is resolved by: its own, in the form the
    instance keeps a label in (_term_text), as the writer looks for it.
    Empty when it has none the instance keeps, or holds a character
    Python's Unicode tables do not know (the module docstring)."""
    if not ref.label or unknown_characters(ref.label):
        return ""
    return _term_text(ref.label)


def _resolve_ref(ref, label_to_qid: dict[str, str], is_artifact):
    if isinstance(ref, LocalRef) and not is_artifact(ref):
        label = _ref_label(ref)
        if label and label in label_to_qid:
            return QidRef(qid=label_to_qid[label])
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
    but those on the artifact side (the module docstring). `label_to_qid`
    is keyed by labels as the instance serves them, and a ref is looked
    up by its label in that form (_ref_label)."""
    is_artifact = _artifact_side(batch)
    return batch.model_copy(
        update={
            "claims": [
                _resolve_claim(c, label_to_qid, is_artifact)
                for c in batch.claims
            ]
        }
    )
