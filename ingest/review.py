"""Human review step: ClaimBatch <-> CSV for OpenRefine (R4).

`export_review_csv` renders one row per claim with human-readable
values, source locators, and an empty `decision` column. Reviewers work
in OpenRefine or any spreadsheet and fill `decision` with approve /
reject (and optionally `notes`).

`apply_review` filters the ORIGINAL batch by the returned CSV: rows are
matched by `claim_id`, a content hash of the claim, so reordering,
sorting, or column shuffling in the review tool cannot mis-apply
decisions — and any edit to claim content invalidates the id loudly
instead of silently writing altered data. Value corrections are made in
the batch JSON (or upstream), then re-exported; the CSV only decides.

No auto-confirmation: a claim with no decision is NOT approved
(CLAUDE.md, "What NOT to do").
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from ingest.claims import (
    Claim,
    ClaimBatch,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    TimeValue,
    VocabRef,
)

APPROVE = {"approve", "approved", "accept", "yes", "y"}
REJECT = {"reject", "rejected", "no", "n"}

CSV_COLUMNS = [
    "claim_id",
    "subject",
    "property",
    "value",
    "qualifiers",
    "source_artifact",
    "page",
    "region",
    "excerpt",
    "confidence",
    "decision",
    "notes",
]


def claim_id(claim: Claim) -> str:
    """Stable content hash identifying a claim across export/import."""
    canonical = json.dumps(
        claim.model_dump(mode="json", exclude_none=True), sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _describe_ref(ref) -> str:
    if isinstance(ref, QidRef):
        return ref.qid
    if isinstance(ref, VocabRef):
        return ref.key
    if isinstance(ref, LocalRef):
        label = f" ({ref.label})" if ref.label else ""
        return f"NEW:{ref.local_id}{label}"
    return str(ref)


def _describe_value(value) -> str:
    if isinstance(value, ItemValue):
        return _describe_ref(value.value)
    if isinstance(value, TimeValue):
        parts = [str(value.year)]
        if value.month:
            parts.append(f"{value.month:02d}")
        if value.day:
            parts.append(f"{value.day:02d}")
        text = "-".join(parts)
        suffix = "" if value.precision in ("year", "month", "day") else f" ({value.precision})"
        calendar = "" if value.calendar == "gregorian" else " [julian]"
        return f"{text}{suffix}{calendar}"
    if isinstance(value, MonolingualValue):
        return f"{value.value.text} [{value.value.language}]"
    if hasattr(value, "latitude"):
        return f"{value.latitude}, {value.longitude}"
    return str(value.value)


def export_review_csv(batch: ClaimBatch, path: Path) -> int:
    """Write the review CSV; returns the number of rows."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for claim in batch.claims:
            writer.writerow(
                {
                    "claim_id": claim_id(claim),
                    "subject": _describe_ref(claim.subject),
                    "property": claim.property,
                    "value": _describe_value(claim.value),
                    "qualifiers": "; ".join(
                        f"{q.property}={_describe_value(q.value)}"
                        for q in claim.qualifiers
                    ),
                    "source_artifact": _describe_ref(claim.source.artifact),
                    "page": claim.source.page_number or "",
                    "region": claim.source.region.as_wikibase_string()
                    if claim.source.region
                    else "",
                    "excerpt": claim.source.transcription_excerpt.text
                    if claim.source.transcription_excerpt
                    else "",
                    "confidence": claim.confidence
                    if claim.confidence is not None
                    else "",
                    "decision": "",
                    "notes": "",
                }
            )
    return len(batch.claims)


def apply_review(batch: ClaimBatch, csv_path: Path) -> tuple[ClaimBatch, list[str]]:
    """(approved batch, warnings). Only explicit approvals pass."""
    decisions: dict[str, str] = {}
    warnings: list[str] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            decisions[row["claim_id"]] = row.get("decision", "").strip().lower()

    approved: list[Claim] = []
    for claim in batch.claims:
        cid = claim_id(claim)
        decision = decisions.pop(cid, None)
        if decision is None:
            warnings.append(f"claim {cid} has no row in the CSV: excluded")
        elif decision in APPROVE:
            approved.append(claim)
        elif decision in REJECT or decision == "":
            if decision == "":
                warnings.append(f"claim {cid} has no decision: excluded")
        else:
            warnings.append(
                f"claim {cid} has unrecognised decision {decision!r}: excluded"
            )
    for stale in decisions:
        warnings.append(
            f"CSV row {stale} matches no claim in the batch (content edited "
            f"or wrong batch): ignored"
        )
    return batch.model_copy(update={"claims": approved}), warnings
