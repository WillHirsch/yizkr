"""Entity resolution step (W1 minimal): local refs -> existing QIDs.

Replaces the `local` references to an entity the writer would find an
existing item for by its label with `qid` references to that item.
Deliberately conservative: exact label match only; anything fuzzier
needs the human reviewer, who sees the resolution outcome in the review
CSV (resolved QID or "NEW:<local_id>") and can reject claims that would
create duplicates. The CSV so shows the item the write will reuse.

An entity is matched by the label and the side rules the writer finds
its item by (yizkr.pipeline.writer, step 2), through the helpers the
writer itself places an entity with:

* its label is its manifest record's when the batch has one for its
  local id, and otherwise its first ref's, with its kind
  (`entity_terms`), in the form the instance keeps a label in, as the
  writer sends and looks for a label (`_term_text`: trimmed, one line,
  cleaned up, in Unicode NFC, cut to the characters the instance
  holds), against the labels the instance serves, which are in that
  form. So a GEDCOM person, whose ref carries no label of its own and
  whose name is on its record, resolves by that name; and a label given
  in NFD, or with whitespace around it, or longer than the instance
  holds, resolves to the item under it in the instance's form;
* the item under that label is its item when it is no artifact nor a
  part of one (`is_artifact_item`); when it is one, the item under the
  entity's tagged label (`tagged_label`, the label with ENTITY_TAG at its
  end) is its item, when that one is not (`reused_item`; ADR-0010). An
  entity the writer would make an item for, or refuse, stays local.

An entity whose label, its record's or any of its refs' when it has no
record, holds a character Python's Unicode tables do not know
(`unknown_characters`) resolves to nothing: the instance may keep it in
another form, and the writer matches no item by it either. Nor does an
entity an approved equivalence names: its refs already name its item by
QID (yizkr.pipeline.reconcile).

An entity on the artifact side (of an artifact kind, artifact or a class
the manifest makes a `subclass_of` it, or of artifact fragment;
`is_artifact_kind` in yizkr.pipeline.writer) is never resolved by its
label: an artifact is never matched by its title or label (ADR-0010),
and the writer decides what such a ref becomes (yizkr.pipeline.writer,
step 2).

What resolving a batch reads of the instance: its item labels once
(`label_index`), and the statements of just the items under the labels,
plain or tagged, of the batch's entities (`fetch_claims`), never every
item's.

Unresolved local refs are left in place: the writer creates new items
for them at write time.
"""

from __future__ import annotations

from yizkr.claims import Claim, ClaimBatch, ItemValue, LocalRef, QidRef
from yizkr.pipeline.review import entity_records
from yizkr.pipeline.writer import (
    entity_terms,
    is_artifact_item,
    is_artifact_kind,
    local_refs,
    reused_item,
    tagged_label,
    unknown_characters,
)


def resolvable_labels(client, batch: ClaimBatch) -> dict[str, str]:
    """The item each label of an entity of `batch` resolves to, label ->
    QID, read from the instance `client` reads: for each entity resolved
    by its label (_resolvable), the item the writer would reuse for it
    (reused_item), under the label or the tagged label. The labels are
    read once (`label_index`), and the statements of only the items under
    those labels (`fetch_claims`)."""
    entities = _resolvable(batch).values()
    _, held = client.label_index()
    found = {
        held[candidate]
        for label, kind in entities
        for candidate in (label, tagged_label(label, kind))
        if candidate in held
    }
    if not found:
        return {}
    properties, items = client.vocabulary()
    claims = client.fetch_claims(sorted(found))

    def artifact_item(qid: str) -> bool:
        return is_artifact_item(claims.get(qid) or {}, properties, items)

    resolved: dict[str, str] = {}
    for label, kind in entities:
        qid = reused_item(label, kind, held, artifact_item)
        if qid:
            resolved[label] = qid
    return resolved


def _resolvable(batch: ClaimBatch) -> dict[str, tuple[str, str | None]]:
    """The entities of `batch` resolved by their label, local id ->
    (label, kind), as the writer reads them (entity_terms): all but those
    on the artifact side and those whose label holds a character Python's
    Unicode tables do not know (the module docstring)."""
    records = entity_records(batch)
    unknown = {
        record.id
        for record in records.values()
        if unknown_characters(record.label)
    }
    unknown.update(
        ref.local_id
        for claim in batch.claims
        for ref in local_refs(claim)
        if ref.local_id not in records and unknown_characters(ref.label or "")
    )
    return {
        local_id: (label, kind)
        for local_id, (label, kind) in entity_terms(batch, records).items()
        if not is_artifact_kind(kind) and local_id not in unknown
    }


def _resolve_ref(ref, label_to_qid: dict[str, str], entities):
    if isinstance(ref, LocalRef) and ref.local_id in entities:
        label, _ = entities[ref.local_id]
        if label in label_to_qid:
            return QidRef(qid=label_to_qid[label])
    return ref


def _resolve_claim(
    claim: Claim, label_to_qid: dict[str, str], entities
) -> Claim:
    def resolved(ref):
        return _resolve_ref(ref, label_to_qid, entities)

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
    """Return a copy of the batch with the local refs to each entity
    resolved by its label (_resolvable) replaced by the item
    `label_to_qid` gives that label, as resolvable_labels reads it. It is
    keyed by labels as the instance serves them, and an entity is looked
    up by its label in that form, its record's or its first ref's
    (entity_terms)."""
    entities = _resolvable(batch)
    return batch.model_copy(
        update={
            "claims": [
                _resolve_claim(c, label_to_qid, entities) for c in batch.claims
            ]
        }
    )
