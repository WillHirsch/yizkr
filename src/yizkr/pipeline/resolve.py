"""Entity resolution step (W1 minimal): local refs -> existing QIDs.

Replaces the `local` references to an entity the writer would find an
existing item for by its label with `qid` references to that item.
Deliberately conservative: exact label match only; anything fuzzier
needs the human reviewer, who sees the resolution outcome in the review
CSV (resolved QID or "NEW:<local_id>") and can reject claims that would
create duplicates. The CSV so shows the item the write will reuse, but
for an entity without a record whose item is under its tagged label and
that has no kind or is in a batch citing more than one artifact (below):
it stays local, as NEW, and the writer finds that item itself.

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

Nor is a manifest record from the second on under one label, as the
son of a father of one name is, when one document names both: the
manifest gives each entity one record (ADR-0007), so the writer makes
such a record an item of its own, found again by its description, never
the item its label names (`homonym_ordinals` in yizkr.pipeline.writer).

An entity on the artifact side (of an artifact kind, artifact or a class
the manifest makes a `subclass_of` it, or of artifact fragment;
`is_artifact_kind` in yizkr.pipeline.writer) is never resolved by its
label: an artifact is never matched by its title or label (ADR-0010),
and the writer decides what such a ref becomes (yizkr.pipeline.writer,
step 2).

What resolving a batch reads of the instance: its item labels once
(`label_index`), and the statements of just the items under the labels,
plain or tagged, of the batch's entities (`fetch_claims`), never every
item's. A label two or more items hold (homonyms, such as two people of
one name) resolves to the numerically earliest of them, the item the
writer finds under it too (`ItemLabels` in yizkr.core.wiki).

An entity's label and kind are read as the writer's plan reads them,
past every claim the writer refuses before its plan (`pre_plan_refusals`:
for its own text, a misplaced document subject, or naming an entity
whose label holds such a character, `unnamed_entities`), so an entity
named only in such claims stays local. The writer refuses more claims
while it plans (with an entity it may place under neither label);
resolve does not see those, so where the first ref to a local id without
a record is in such a claim and a later ref gives another label or kind,
resolve may read the entity by that first ref's label and kind.

An entity without a manifest record that resolves to the item under its
tagged label is given a record in the resolved batch, its first ref's
label and kind (`_tagged_records`): the writer then pairs it with that
item after the claims, as it pairs any record (yizkr.pipeline.writer,
step 5), and gives the item its label as an alias and its kind, as the
local refs would have. One with no kind stays local, for the writer to
find that item itself, as no record can be made without a kind. So does
any such entity of a batch whose claims the writer writes cite more than
one artifact: a record's kind is cited to the artifact of the first claim
that mentions the item, where a ref's kind is cited to the artifact of
the first claim of each local id, so a record would cite the kind once
where the local refs of two documents cite it to each.

Unresolved local refs are left in place: the writer creates new items
for them at write time.
"""

from __future__ import annotations

from yizkr.claims import (
    Claim,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    QidRef,
)
from yizkr.pipeline.review import entity_records
from yizkr.pipeline.writer import (
    entity_terms,
    homonym_ordinals,
    is_artifact_item,
    is_artifact_kind,
    pre_plan_refusals,
    reused_item,
    tagged_label,
    unnamed_entities,
)


def resolvable_labels(client, batch: ClaimBatch) -> dict[str, str]:
    """The item each label of an entity of `batch` resolves to, label ->
    QID, read from the instance `client` reads: for each entity resolved
    by its label (_resolvable), the item the writer would reuse for it
    (reused_item), under the label or the tagged label. An item reused
    under the tagged label is given under that label too, which names it
    as the writer finds it there, and tells resolve_batch it is tagged.
    The labels are read once (`label_index`), and the statements of only
    the items under those labels (`fetch_claims`). A label two or more
    items hold, as two people of one name do, names the earliest of them,
    as it does for the writer (ItemLabels)."""
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
            if held.get(label) != qid:
                resolved[tagged_label(label, kind)] = qid
    return resolved


def _resolvable(batch: ClaimBatch) -> dict[str, tuple[str, str | None]]:
    """The entities of `batch` resolved by their label, local id ->
    (label, kind), as the writer reads them (entity_terms) past the claims
    it refuses before its plan (pre_plan_refusals), so no entity whose
    label holds a character Python's Unicode tables do not know is among
    them: all but those on the artifact side, and a manifest record from
    the second on under one label, which the writer makes an item of its
    own (homonym_ordinals; the module docstring)."""
    records = entity_records(batch)
    refused = pre_plan_refusals(batch, unnamed_entities(batch, records))
    ordinals = homonym_ordinals(records)
    return {
        local_id: (label, kind)
        for local_id, (label, kind) in entity_terms(
            batch, records, refused
        ).items()
        if not is_artifact_kind(kind) and ordinals.get(local_id, 1) == 1
    }


def _tagged_records(
    batch: ClaimBatch, label_to_qid: dict[str, str], entities
) -> tuple[list[EntityRecord], set[str]]:
    """For each entity of `entities` (_resolvable) without a manifest
    record that `label_to_qid` resolves to the item under its tagged label
    (resolvable_labels gives that label the same item): a record of its
    label and kind, so the writer pairs it with the item (the module
    docstring); and the local ids of those with no kind, or of a batch
    whose claims the writer writes, all but those it refuses before its
    plan (pre_plan_refusals), cite more than one artifact, which stay
    local."""
    records = entity_records(batch)
    refused = pre_plan_refusals(batch, unnamed_entities(batch, records))
    cited = {
        claim.source.artifact.sha1
        for index, claim in enumerate(batch.claims)
        if index not in refused
    }
    several = len(cited) > 1
    made: list[EntityRecord] = []
    kept: set[str] = set()
    for local_id, (label, kind) in entities.items():
        qid = label_to_qid.get(label)
        if (
            local_id in records
            or qid is None
            or label_to_qid.get(tagged_label(label, kind)) != qid
        ):
            continue
        if kind is None or several:
            kept.add(local_id)
        else:
            made.append(EntityRecord(id=local_id, label=label, kind=kind))
    return made, kept


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
    (entity_terms). An entity without a record resolved to the item under
    its tagged label gains a record, or stays local when it has no kind or
    its batch's claims cite more than one artifact (_tagged_records)."""
    entities = _resolvable(batch)
    made, kept = _tagged_records(batch, label_to_qid, entities)
    for local_id in kept:
        del entities[local_id]
    return batch.model_copy(
        update={
            "claims": [
                _resolve_claim(c, label_to_qid, entities) for c in batch.claims
            ],
            "entities": [*batch.entities, *made],
        }
    )
