"""The depicts row-approval gate of the map-bound writer (ADR-0005).

A `depicts` claim is written only with a record of a curator's approval
on its own row (`Claim.row_approval`): a digest of its claim content and
of the entity-manifest records its local refs resolve through
(`approval_digest`). The writer refuses a batch holding a depicts claim
without that record, or one whose claim or records changed after it
(`unapproved_depicts`), before it reads or writes the instance, dry runs
included. The record is batch review state, not an authenticated curator
signature. Nothing in the package records one, so the writer refuses
every depicts claim: a face-region proposal has no confirmed write until
faces work resumes.

`entity_records` is the one lookup of a batch's entity manifest that the
writer and the gate both read.
"""

from __future__ import annotations

import hashlib
import json

from yizkr.claims import (
    Claim,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
)

# re-exported: the adjudication stage imports how a claim's unknown value
# is shown from here, and its code digest reads it here
from yizkr.pipeline.claim_naming import UNKNOWN_SHOWN as UNKNOWN_SHOWN
from yizkr.pipeline.claim_naming import claim_id

# The writer requires a claim-bound record of explicit face approval.
DEPICTS = "depicts"


def _local_refs(claim: Claim) -> list[LocalRef]:
    """The claim's local refs: its subject, then its value and its
    qualifiers' values when they are items."""
    refs = [claim.subject]
    if isinstance(claim.value, ItemValue):
        refs.append(claim.value.value)
    refs.extend(
        qualifier.value.value
        for qualifier in claim.qualifiers
        if isinstance(qualifier.value, ItemValue)
    )
    return [ref for ref in refs if isinstance(ref, LocalRef)]


def entity_records(batch: ClaimBatch) -> dict[str, EntityRecord]:
    """The batch's entity manifest by id: the record each local ref of that
    id resolves through (ADR-0007). When two records share an id, the last
    one counts: the other resolves no local ref, and the writer adds no
    alias or kind from it. It is still part of the batch file, which the
    extraction-run item's fingerprint covers whole.

    The writer reads the manifest through this lookup alone, and so does
    `approval_digest`: the records a depicts claim's approval binds are the
    records its local refs are written through (ADR-0005)."""
    return {record.id: record for record in batch.entities}


def _approval_digest(claim: Claim, records: dict[str, EntityRecord]) -> str:
    resolved = {}
    for ref in _local_refs(claim):
        record = records.get(ref.local_id)
        resolved[ref.local_id] = (
            record.model_dump(mode="json") if record else None
        )
    canonical = json.dumps(
        {"claim_id": claim_id(claim), "entities": resolved}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def approval_digest(claim: Claim, batch: ClaimBatch) -> str:
    """The row approval the writer requires on a depicts claim of
    `batch`: a digest of the claim's id and of the entity-manifest record
    each of its local refs resolves through, or of that ref having none.

    The writer resolves a local ref through the manifest record that
    `entity_records` gives for its id, when there is one (ADR-0007), so the
    claim's own content does not fix the item it names. The digest covers
    both, reading the records through that same lookup: editing the claim,
    or adding, editing or removing a record one of its local refs resolves
    through, changes it."""
    return _approval_digest(claim, entity_records(batch))


def unapproved_depicts(batch: ClaimBatch) -> list[str]:
    """Ids of the batch's `depicts` claims that no curator approved on
    their own row, for the item they would now depict (ADR-0005).

    A depicts claim is approved when its `row_approval` equals its
    `approval_digest` against the batch as it now reads. So a claim that
    carries no approval, such as a detect-faces proposal, fails, and so
    does one whose content, or the manifest record one of its local refs
    resolves through, changed after its approval.

    A local ref without a manifest record is resolved by the writer from
    the first ref in the batch to its local id, which may be another
    claim's. So a depicts claim also fails when another ref in the batch
    to one of those local ids differs from its own (its subject, its value
    or a qualifier's value) in any field, its label, description or kind:
    the item it would name, and what the writer writes of that item, would
    then depend on the batch's order, not on the row the curator
    approved."""
    records = entity_records(batch)
    unrecorded: dict[str, list[LocalRef]] = {}
    for claim in batch.claims:
        for ref in _local_refs(claim):
            if ref.local_id not in records:
                unrecorded.setdefault(ref.local_id, []).append(ref)
    unapproved = []
    for claim in batch.claims:
        if claim.property != DEPICTS:
            continue
        ambiguous = any(
            other != ref
            for ref in _local_refs(claim)
            for other in unrecorded.get(ref.local_id, [])
        )
        digest = _approval_digest(claim, records)
        if ambiguous or claim.row_approval != digest:
            unapproved.append(claim_id(claim))
    return unapproved
