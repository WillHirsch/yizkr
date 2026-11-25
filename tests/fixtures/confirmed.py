"""Batches whose entities a curator confirmed as existing items, in the
state the map-bound writer and the claim subject rules still accept.

A confirmed entity's manifest record carries its item's QID
(`EntityRecord.qid`); every local ref to it, as a claim's subject, value
or a qualifier's value, names that QID; and a claim whose subject it
replaces records the local subject it named (`Claim.confirmed_from`),
with the entity's kind: its manifest record's, else the ref's own. The
writer attaches such an entity to its item directly and appends the
record's aliases there (ADR-0007), and the subject rules tell a confirmed
mention or record's artifact from a QID named outright by that record
(ADR-0039 §10.1(c)).

No command of the package makes this state. These tests build it to keep
the writer's and the subject rules' handling of it covered while they
read it: the map-bound writer until #893, the claim fields until #894.
"""

from __future__ import annotations

from collections.abc import Mapping

from yizkr.claims import Claim, ClaimBatch, ItemValue, LocalRef, QidRef


def confirmed(batch: ClaimBatch, qids: Mapping[str, str]) -> ClaimBatch:
    """`batch` with each local id that `qids` names confirmed as the item
    it gives, as the module docstring describes."""
    kinds = {entity.id: entity.kind for entity in batch.entities}

    def named(ref):
        if isinstance(ref, LocalRef) and ref.local_id in qids:
            return QidRef(qid=qids[ref.local_id])
        return ref

    def item(value):
        if isinstance(value, ItemValue):
            return ItemValue(value=named(value.value))
        return value

    def claim_of(claim: Claim) -> Claim:
        update: dict = {
            "subject": named(claim.subject),
            "value": item(claim.value),
            "qualifiers": [
                qualifier.model_copy(update={"value": item(qualifier.value)})
                for qualifier in claim.qualifiers
            ],
        }
        subject = claim.subject
        if isinstance(subject, LocalRef) and subject.local_id in qids:
            kind = kinds.get(subject.local_id, subject.instance_of)
            update["confirmed_from"] = subject.model_copy(
                update={"instance_of": kind}
            )
        return claim.model_copy(update=update)

    return batch.model_copy(
        update={
            "claims": [claim_of(claim) for claim in batch.claims],
            "entities": [
                entity.model_copy(update={"qid": qids[entity.id]})
                if entity.id in qids
                else entity
                for entity in batch.entities
            ],
        }
    )
