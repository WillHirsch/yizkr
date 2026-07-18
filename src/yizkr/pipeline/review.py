"""Human review step: ClaimBatch <-> CSV for OpenRefine (R4).

`export_review_csv` renders one row per claim with human-readable
values, source locators, a machine-written `critique_notes` column, and
empty curator columns. Reviewers work in OpenRefine or any spreadsheet
and fill:

* `decision` — approve / reject (nothing passes without it);
* `correction` — a replacement value for an approved claim the machine
  misread (ROADMAP item 11): dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD`
  (never an ambiguous format), plain text for string values, a plain
  number for quantity values. The
  corrected claim is curator-attributed: it keeps the artifact
  reference but no extraction-run reference (ADR-0003 — process
  provenance is for machine writes).

The `critique_notes` column carries the extraction critic's concerns
(ADR-0013) for the row's subject — flagged mistypes, misapplied
properties, role-aliases — so the curator sees them at this gate.

There is no `living` column. Whether a person is possibly living is
never a review decision: it is a runtime determination from the absence
of death evidence, made when a privacy-sensitive action runs (public
export, an internet-facing publish) — see ADR-0020 and
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

No auto-confirmation: a claim with no decision is NOT approved —
human confirmation is part of the data flow (R3, ADR-0005). A curator
who has spot-checked a batch may bulk-approve it with `accept_all` (the
`accept-all` CLI step): that is the human choosing to accept every row,
still an explicit `approve` on each, not the machine confirming itself.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from yizkr.claims import (
    Claim,
    ClaimBatch,
    ItemValue,
    LocalRef,
    MonolingualText,
    MonolingualValue,
    QidRef,
    QuantityValue,
    StringValue,
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
    "critique_notes",
    "decision",
    "correction",
    "notes",
]

_CORRECTION_DATE = re.compile(r"^(\d{1,4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def claim_id(claim: Claim) -> str:
    """Stable content hash identifying a claim across export/import."""
    canonical = json.dumps(
        claim.model_dump(
            mode="json", exclude_none=True, exclude_defaults=False
        ),
        sort_keys=True,
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
        suffix = (
            ""
            if value.precision in ("year", "month", "day")
            else f" ({value.precision})"
        )
        calendar = "" if value.calendar == "gregorian" else " [julian]"
        return f"{text}{suffix}{calendar}"
    if isinstance(value, MonolingualValue):
        return f"{value.value.text} [{value.value.language}]"
    if isinstance(value, QuantityValue):
        return f"{value.amount:g}"
    if hasattr(value, "latitude"):
        return f"{value.latitude}, {value.longitude}"
    return str(value.value)


def export_review_csv(
    batch: ClaimBatch,
    path: Path,
    critique_notes: dict[str, str] | None = None,
) -> int:
    """Write the review CSV; returns the number of rows.

    `critique_notes` (ADR-0013): entity local_id -> concern from the
    extraction critic, rendered in the `critique_notes` column so the
    curator sees flagged mistypes / misapplied properties / role-aliases
    at this gate. There is no possibly-living warning and no `living`
    column: privacy is a runtime determination, never a review decision
    (ADR-0020).
    """
    critique_notes = critique_notes or {}
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
                    "critique_notes": critique_notes.get(
                        getattr(claim.subject, "local_id", None) or "", ""
                    ),
                    "decision": "",
                    "correction": "",
                    "notes": "",
                }
            )
    return len(batch.claims)


# -- corrections (ROADMAP item 11) ----------------------------------------


def _parse_correction(claim: Claim, text: str) -> Claim:
    """A curator's replacement value -> corrected, curator-attributed claim.

    Supported: time values in unambiguous ISO forms (`1885`, `1885-08`,
    `1885-08-12`), plain text for string / external-id / monolingual
    values, and plain numbers for quantity values. Item corrections are
    not expressible as text — reject the
    claim and fix identity through the equivalence flow instead.
    Raises ValueError with a reviewer-readable message otherwise.
    """
    value = claim.value
    if isinstance(value, TimeValue):
        match = _CORRECTION_DATE.match(text)
        if not match:
            raise ValueError(
                f"correction {text!r} is not an unambiguous date "
                f"(use YYYY, YYYY-MM or YYYY-MM-DD)"
            )
        year, month, day = (
            int(part) if part is not None else None for part in match.groups()
        )
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


def apply_review(
    batch: ClaimBatch, csv_path: Path
) -> tuple[ClaimBatch, list[str]]:
    """(approved batch, warnings). Only explicit approvals pass."""
    rows: dict[str, dict] = {}
    warnings: list[str] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            rows[row["claim_id"]] = row

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
                    approved.append(_parse_correction(claim, correction))
                except ValueError as exc:
                    warnings.append(f"claim {cid}: {exc}: excluded")
            else:
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


def accept_all(source_csv: Path, out_csv: Path) -> int:
    """Copy a review CSV to `out_csv` with every row's decision set to
    `approve`; returns the row count. Only the `decision` column is
    touched — `claim_id` and every content column are preserved verbatim,
    so `apply_review` still matches each row by content hash.

    This is a curator's deliberate bulk-accept of a whole sheet — the
    human end of the "explicit approve" rule (R3), not auto-confirmation.
    The machine never runs it; the reviewed file it writes is what the
    claim gate consumes. It trades per-row scrutiny for speed, for a
    trusted or already spot-checked batch — not the default.
    """
    with open(source_csv, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "decision" not in fieldnames:
            raise ValueError(
                f"{source_csv} is not a review CSV (no 'decision' column)"
            )
        rows = list(reader)
    for row in rows:
        row["decision"] = "approve"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
