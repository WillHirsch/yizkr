"""Human review step: ClaimBatch <-> CSV for OpenRefine (R4).

`export_review_csv` renders one row per claim with human-readable
values (a date at the precision its source states, as the site shows it:
`sheet_value`), source locators, a machine-written `critique_notes` column, and
empty curator columns. Reviewers work in OpenRefine or any spreadsheet
and fill:

* `decision` — approve / reject (nothing passes without it);
* `correction` — a replacement value for an approved claim the machine
  misread: dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD` (never an
  ambiguous format), plain text for string values, a plain number for
  quantity values. The corrected claim is curator-attributed: it keeps
  the artifact reference but no extraction-run reference (ADR-0003 —
  process provenance is for machine writes).

The locators are the cited page (`page`, the physical page), the number
printed on that page where the transcription record gives one
(`printed_page`, empty otherwise; ADR-0037), the image region and the
excerpt. They are shown for checking and never read back: `apply_review`
matches rows by `claim_id`, reads only their `decision` and `correction`,
and takes each claim's source from the batch. An edited `page` or
`printed_page` cell is ignored, and the approved claim keeps the page and
printed page number its batch gives.

The `critique_notes` column carries the extraction critic's concerns
(ADR-0013) — flagged mistypes, misapplied properties, role-aliases — so
the curator sees them at this gate. Each finding is on exactly one row:
its claim's, or for a finding about an entity, the row of a claim that
names it (`critique.attach`). A finding that failed its checks is shown
on the first row, marked unchecked (`critique.sheet_notes`).
Source-scoped widowhood/death attribution concerns are added deterministically
on the relevant union-end rows, even when no model critique was run.

There is no `living` column. Whether a person is possibly living is
never a review decision: it is a runtime determination from the absence
of death evidence, made when a privacy-sensitive action runs (the
non-privileged export, an internet-facing publish) — see ADR-0020 and
`yizkr.core.privacy`. To record a person as deceased with no
particulars, a source asserting death (a burial place, a cause of
death, or an undated death event) is enough; nothing is asked of the
reviewer.

`apply_review` filters the ORIGINAL batch by the returned CSV: rows are
matched by `claim_id`, a content hash of the claim, so reordering,
sorting, or column shuffling in the review tool cannot mis-apply
decisions — and any edit to claim content invalidates the id loudly
instead of silently writing altered data. Corrections change the claim
only AFTER matching, through the `correction` column.

Each retained row needs an explicit human decision. A blank decision is
not approval. A `depicts` claim additionally carries a record bound to
its claim content and local entity records (`Claim.row_approval`). The
writer refuses missing or stale face approval before instance access.
The record is batch review state, not an authenticated curator signature.

`apply_review` rejects whole-sheet approval tokens, malformed claim IDs
and equivalences-sheet columns. It reads the claimed property and source
from the original batch, so edits to display cells cannot change what is
approved. Sheets are UTF-8, with or without a leading byte-order mark.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from yizkr.claims import (
    Claim,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    MonolingualText,
    MonolingualValue,
    QidRef,
    QuantityValue,
    StringValue,
    TimeValue,
    UnknownValue,
    VocabRef,
    widowhood_review_notes,
)
from yizkr.core.dates import UnsupportedDate, claim_time_text, year_problem

APPROVE = {"approve", "approved", "accept", "yes", "y"}
REJECT = {"reject", "rejected", "no", "n"}

# The writer requires a claim-bound record of explicit face approval.
DEPICTS = "depicts"

# Review sheets are read as UTF-8 with or without a byte-order mark, which
# spreadsheets such as Excel write at the start of a "CSV UTF-8" file.
SHEET_ENCODING = "utf-8-sig"

CSV_COLUMNS = [
    "claim_id",
    "subject",
    "property",
    "value",
    "qualifiers",
    "source_artifact",
    "page",
    "printed_page",
    "region",
    "excerpt",
    "confidence",
    "critique_notes",
    "decision",
    "correction",
    "notes",
]

# a leading minus is read, so that a year before the common era is refused
# as unsupported rather than as a malformed date
_CORRECTION_DATE = re.compile(r"^(-?\d{1,4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def claim_id(claim: Claim) -> str:
    """Stable content hash identifying a claim across export/import.

    The row approval `apply_review` records on a claim is not content: an
    approved claim keeps the id it had on its review sheet."""
    canonical = json.dumps(
        claim.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=False,
            exclude={"row_approval"},
        ),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


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
    """The row approval `apply_review` records on a depicts claim of
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
    their own row, for the item they would now depict, as `apply_review`
    records it (ADR-0005).

    A depicts claim is approved when its `row_approval` equals its
    `approval_digest` against the batch as it now reads. So a claim that
    never went through `apply_review`, such as a detect-faces proposal,
    fails, and so does one whose content, or the manifest record one of
    its local refs resolves through, changed after its approval.

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


def _describe_ref(ref) -> str:
    if isinstance(ref, QidRef):
        return ref.qid
    if isinstance(ref, VocabRef):
        return ref.key
    if isinstance(ref, LocalRef):
        label = f" ({ref.label})" if ref.label else ""
        return f"NEW:{ref.local_id}{label}"
    return str(ref)


def describe_value(value) -> str:
    """A value as the extraction critic lists it, and as a review row shows
    any value but a date (which the sheet shows through `sheet_value`):
    an item by its reference, a date
    as `YYYY[-MM[-DD]]` with its precision when it is not the plain one, a
    text by its text and language, an unknown value (ADR-0040 §2.2) as
    "unknown value". Every date is Gregorian and of 1 CE or later
    (ADR-0034)."""
    if isinstance(value, UnknownValue):
        return UNKNOWN_SHOWN
    if isinstance(value, ItemValue):
        return _describe_ref(value.value)
    if isinstance(value, TimeValue):
        parts = [str(value.year)]
        if value.month:
            parts.append(f"{value.month:02d}")
        if value.day:
            parts.append(f"{value.day:02d}")
        text = "-".join(parts)
        suffix = (
            ""
            if value.precision in ("year", "month", "day")
            else f" ({value.precision})"
        )
        return f"{text}{suffix}"
    if isinstance(value, MonolingualValue):
        return f"{value.value.text} [{value.value.language}]"
    if isinstance(value, QuantityValue):
        return f"{value.amount:g}"
    if hasattr(value, "latitude"):
        return f"{value.latitude}, {value.longitude}"
    return str(value.value)


# how a review row and the critic show an unknown value
UNKNOWN_SHOWN = "unknown value"


def sheet_value(value) -> str:
    """A value as the review sheet shows it: as `describe_value` does, but
    a date at the precision its source states, as the site renders it
    (`core.dates.claim_time_text`): a decade as "the 1830s", a century as
    "the 19th century", never as its first year."""
    if isinstance(value, TimeValue):
        return claim_time_text(value, "a date the review sheet shows")
    return describe_value(value)


def export_review_csv(
    batch: ClaimBatch,
    path: Path,
    critique_notes: dict[int, str] | None = None,
) -> int:
    """Write the review CSV; returns the number of rows.

    `critique_notes` (ADR-0013): row index (the claim's in `batch`) ->
    the extraction critic's concerns on that row, rendered in the
    `critique_notes` column so the curator sees flagged mistypes /
    misapplied properties / role-aliases at this gate. Deterministic
    widowhood/death concerns are appended without replacing those notes.
    There is no
    possibly-living warning and no `living` column: privacy is a runtime
    determination, never a review decision (ADR-0020).
    """
    supplied = critique_notes or {}
    concerns = widowhood_review_notes(batch)
    critique_notes = {
        row: "\n".join(filter(None, (supplied.get(row), concerns.get(row))))
        for row in supplied.keys() | concerns.keys()
    }
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row, claim in enumerate(batch.claims):
            writer.writerow(
                {
                    "claim_id": claim_id(claim),
                    "subject": _describe_ref(claim.subject),
                    "property": claim.property,
                    "value": sheet_value(claim.value),
                    "qualifiers": "; ".join(
                        f"{q.property}={sheet_value(q.value)}"
                        for q in claim.qualifiers
                    ),
                    "source_artifact": _describe_ref(claim.source.artifact),
                    "page": claim.source.page_number or "",
                    "printed_page": claim.source.printed_page_number or "",
                    "region": claim.source.region.as_wikibase_string()
                    if claim.source.region
                    else "",
                    "excerpt": claim.source.transcription_excerpt.text
                    if claim.source.transcription_excerpt
                    else "",
                    "confidence": claim.confidence
                    if claim.confidence is not None
                    else "",
                    "critique_notes": critique_notes.get(row, ""),
                    "decision": "",
                    "correction": "",
                    "notes": "",
                }
            )
    return len(batch.claims)


# -- corrections ----------------------------------------------------------


def _parse_correction(claim: Claim, text: str) -> Claim:
    """A curator's replacement value -> corrected, curator-attributed claim.

    Supported: time values in unambiguous ISO forms (`1919`, `1919-01`,
    `1919-01-07`), Gregorian and of 1 CE or later, plain text for
    string / external-id / monolingual values, and plain numbers for
    quantity values. A date of death of unknown value (ADR-0040 §2.2)
    takes a date as a time value does: the curator's correction states
    the date the source gives. Item corrections are not expressible as
    text — reject the claim and fix identity through the equivalence flow
    instead.
    Raises ValueError with a reviewer-readable message otherwise, and
    UnsupportedDate (a ValueError) for a year before the common era
    (ADR-0034).
    """
    value = claim.value
    if isinstance(value, TimeValue) or (
        isinstance(value, UnknownValue) and claim.property == "date_of_death"
    ):
        match = _CORRECTION_DATE.match(text)
        if not match:
            raise ValueError(
                f"correction {text!r} is not an unambiguous date "
                f"(use YYYY, YYYY-MM or YYYY-MM-DD)"
            )
        year, month, day = (
            int(part) if part is not None else None for part in match.groups()
        )
        problem = year_problem(year)
        if problem is not None:
            raise UnsupportedDate(f"correction {text!r}: {problem}")
        corrected = TimeValue(year=year, month=month, day=day)
    elif isinstance(value, StringValue):
        corrected = value.model_copy(update={"value": text})
    elif isinstance(value, MonolingualValue):
        corrected = MonolingualValue(
            value=MonolingualText(text=text, language=value.value.language)
        )
    elif isinstance(value, QuantityValue):
        try:
            amount = (
                int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)
            )
        except ValueError:
            raise ValueError(
                f"correction {text!r} is not a number (quantity values "
                f"take a plain number)"
            ) from None
        corrected = value.model_copy(update={"amount": amount})
    elif hasattr(value, "value") and isinstance(
        getattr(value, "value", None), str
    ):
        corrected = value.model_copy(update={"value": text})
    else:
        raise ValueError(
            f"{type(value).__name__} cannot be corrected as text — reject "
            f"the claim instead (item identity belongs to the equivalence "
            f"review)"
        )
    return claim.model_copy(update={"value": corrected, "curated": True})


def _header_key(name: str) -> str:
    """A column name as the sheet-kind checks compare it: invisible format
    characters and surrounding space removed, case folded."""
    visible = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    return visible.strip().casefold()


def apply_review(
    batch: ClaimBatch, csv_path: Path
) -> tuple[ClaimBatch, list[str]]:
    """(approved batch, warnings). Only explicit approvals pass.

    A `depicts` claim is confirmed only by a per-row human decision.
    Its approval digest binds the original claim and the entity-manifest
    records of its local references; the writer requires that record.
    Display cells never determine the claim's property or source.

    Whole-sheet approval tokens are unsupported. A sheet with columns
    from an equivalences review, or without an exact `claim_id` column,
    is refused before any approved batch is returned.
    """
    rows: dict[str, dict] = {}
    warnings: list[str] = []
    records = entity_records(batch)
    with open(csv_path, newline="", encoding=SHEET_ENCODING) as f:
        reader = csv.DictReader(f)
        foreign = [
            name
            for name in reader.fieldnames or []
            if _header_key(name) in ("entity_id", "verdict")
        ]
        if foreign:
            raise ValueError(
                f"{csv_path} has an equivalences sheet's column(s) "
                f"{', '.join(map(repr, foreign))} — a claims sheet shaped "
                f"like an equivalences sheet is malformed, and its approvals "
                f"may be ratified machine verdicts; decide a fresh "
                f"export-review sheet instead"
            )
        fieldnames = reader.fieldnames or []
        if "claim_id" not in fieldnames:
            if "claim_id" in {_header_key(name) for name in fieldnames}:
                raise ValueError(
                    f"{csv_path} has a column like 'claim_id' but none named "
                    f"exactly 'claim_id' — apply-review matches each row to "
                    f"its claim by that column, named as export-review "
                    f"writes it"
                )
            raise ValueError(
                f"{csv_path} has no 'claim_id' column — apply-review "
                f"matches each row to its claim by that column; apply a "
                f"sheet export-review wrote"
            )
        for row in reader:
            rows[row["claim_id"]] = row

    unsupported = [
        f"claim {cid}"
        for cid, row in rows.items()
        if (row.get("decision") or "").strip().lower() == "bulk-accept"
    ]
    if unsupported:
        raise ValueError(
            f"{csv_path} has unsupported bulk-accept decisions; use explicit "
            f"approve/reject decisions for {', '.join(unsupported)}"
        )

    approved: list[Claim] = []
    for claim in batch.claims:
        cid = claim_id(claim)
        row = rows.pop(cid, None)
        if row is None:
            warnings.append(f"claim {cid} has no row in the CSV: excluded")
            continue
        decision = row.get("decision", "").strip().lower()
        if decision in APPROVE:
            correction = row.get("correction", "").strip()
            if correction:
                try:
                    claim = _parse_correction(claim, correction)
                except ValueError as exc:
                    warnings.append(f"claim {cid}: {exc}: excluded")
                    continue
            if claim.property == DEPICTS:
                claim = claim.model_copy(
                    update={"row_approval": _approval_digest(claim, records)}
                )
            approved.append(claim)
        elif decision in REJECT or decision == "":
            if decision == "":
                warnings.append(f"claim {cid} has no decision: excluded")
        else:
            warnings.append(
                f"claim {cid} has unrecognised decision {decision!r}: excluded"
            )
    for stale in rows:
        warnings.append(
            f"CSV row {stale} matches no claim in the batch (content edited "
            f"or wrong batch): ignored"
        )
    return batch.model_copy(update={"claims": approved}), warnings
