"""Human review step: ClaimBatch <-> CSV for OpenRefine (R4).

`export_review_csv` renders one row per claim with human-readable
values, source locators, a machine-written `critique_notes` column, and
empty curator columns. Reviewers work in OpenRefine or any spreadsheet
and fill:

* `decision` — approve / reject (nothing passes without it);
* `correction` — a replacement value for an approved claim the machine
  misread: dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD` (never an
  ambiguous format), plain text for string values, a plain number for
  quantity values. The corrected claim is curator-attributed: it keeps
  the artifact reference but no extraction-run reference (ADR-0003 —
  process provenance is for machine writes).

The `critique_notes` column carries the extraction critic's concerns
(ADR-0013) — flagged mistypes, misapplied properties, role-aliases — so
the curator sees them at this gate. Each finding is on exactly one row:
its claim's, or for a finding about an entity, the row of a claim that
names it (`critique.attach`). A finding that failed its checks is shown
on the first row, marked unchecked (`critique.sheet_notes`).

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

No auto-confirmation: a claim with no decision is NOT approved —
human confirmation is part of the data flow (R3, ADR-0005). A curator
who has spot-checked a batch may bulk-accept it with `accept_all` (the
`accept-all` CLI step): the human choosing to accept the machine's
proposals wholesale, still an explicit decision written per row, not the
machine confirming itself. For a claims sheet that is `bulk-accept` (an
approval) on every row; for an equivalences sheet it *ratifies each
verdict* (same -> approve, distinct -> reject), never a blanket merge.

A `depicts` claim never takes that path, whether face detection or a
source's text proposed it: only a per-row human decision confirms one
(ADR-0005). `accept_all` takes any sheet with a column like `claim_id`,
by normalised name, for a claims sheet. It refuses a claims sheet with
any `depicts` row, or without the exactly named `claim_id` and
`property` columns that show them. `apply_review` refuses a
`bulk-accept` on any claim whose property in the batch is `depicts`,
whatever the sheet's cells say. It also refuses any sheet carrying an
equivalences sheet's columns. A claims sheet disguised well enough to
reach `accept_all`'s equivalences branch comes out with those columns,
so the `approve`s that branch writes are never applied. A per-row
`approve` passes as it does for any claim, and `apply_review` records it
on the approved claim (`Claim.row_approval`, its `approval_digest`: the
claim's id and the entity-manifest records its local refs resolve
through). The writer refuses a batch with any depicts claim that lacks
that record for the claim and those records as they now read, or whose
item would depend on another claim's ref (`unapproved_depicts`). So a
batch that never went through a review sheet, such as a raw detect-faces
batch, cannot write one, and an approved depicts claim that a later
edit to the claim or to those records retargets has to be decided
again. The record is not a signature: whoever hand-edits a batch file
can recompute it.

Sheets are read as UTF-8, with or without the byte-order mark some
spreadsheets write; `accept_all` refuses a column name that still holds
an invisible format character after that, such as a second mark.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
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
    VocabRef,
)
from yizkr.core.dates import UnsupportedDate, year_problem

# `bulk-accept` is an approval that also records HOW it was accepted (a
# whole-sheet accept-all, ADR-0023), so the write path can mark those edits
# for later reappraisal; it approves like any other token.
APPROVE = {"approve", "approved", "accept", "yes", "y", "bulk-accept"}
REJECT = {"reject", "rejected", "no", "n"}

# The property only a per-row human decision confirms (ADR-0005): the
# whole-sheet paths refuse it, and the writer writes it only with the
# record of that decision.
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
    """A value as a review row shows it: an item by its reference, a date
    as `YYYY[-MM[-DD]]` with its precision when it is not the plain one, a
    text by its text and language. Every date is Gregorian and of 1 CE or
    later (ADR-0034)."""
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


def export_review_csv(
    batch: ClaimBatch,
    path: Path,
    critique_notes: dict[int, str] | None = None,
) -> int:
    """Write the review CSV; returns the number of rows.

    `critique_notes` (ADR-0013): row index (the claim's in `batch`) ->
    the extraction critic's concerns on that row, rendered in the
    `critique_notes` column so the curator sees flagged mistypes /
    misapplied properties / role-aliases at this gate. There is no
    possibly-living warning and no `living` column: privacy is a runtime
    determination, never a review decision (ADR-0020).
    """
    critique_notes = critique_notes or {}
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row, claim in enumerate(batch.claims):
            writer.writerow(
                {
                    "claim_id": claim_id(claim),
                    "subject": _describe_ref(claim.subject),
                    "property": claim.property,
                    "value": describe_value(claim.value),
                    "qualifiers": "; ".join(
                        f"{q.property}={describe_value(q.value)}"
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
    quantity values. Item corrections are not expressible as text —
    reject the claim and fix identity through the equivalence flow
    instead.
    Raises ValueError with a reviewer-readable message otherwise, and
    UnsupportedDate (a ValueError) for a year before the common era
    (ADR-0034).
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


def _has_format_character(name: str) -> bool:
    """True if `name` holds an invisible format character (Unicode category
    Cf), such as a byte-order mark or a zero-width space."""
    return any(unicodedata.category(ch) == "Cf" for ch in name)


def apply_review(
    batch: ClaimBatch, csv_path: Path
) -> tuple[ClaimBatch, list[str]]:
    """(approved batch, warnings). Only explicit approvals pass.

    A `depicts` claim is confirmed only by a per-row human decision
    (ADR-0005). A `bulk-accept` on one raises ValueError naming the claim
    ids, before anything is returned. The property is read from the batch
    claim, never from the sheet, so no edit to the sheet's `property`
    cells or columns can hide one. A per-row `approve` on a `depicts`
    claim passes, and the approved claim carries the record of it: its
    `row_approval` is set to its `approval_digest` against `batch`, whose
    entity manifest the approved batch keeps. The writer requires that
    record of every depicts claim it writes (`unapproved_depicts`).

    A sheet with a column like an equivalences sheet's `entity_id` or
    `verdict` raises ValueError too. A claims sheet shaped like an
    equivalences sheet is malformed, and its `approve` decisions may be
    ratified machine verdicts rather than a human's.
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
        for row in reader:
            rows[row["claim_id"]] = row

    bulk_accepted_depicts = []
    for claim in batch.claims:
        if claim.property != DEPICTS:
            continue
        cid = claim_id(claim)
        decision = (rows.get(cid, {}).get("decision") or "").strip().lower()
        if decision == "bulk-accept":
            bulk_accepted_depicts.append(f"claim {cid}")
    if bulk_accepted_depicts:
        raise ValueError(
            f"{csv_path} has bulk-accept on {len(bulk_accepted_depicts)} "
            f"depicts claim(s) — a depicts claim takes a per-row human "
            f"decision (ADR-0005), never a whole-sheet accept; decide these "
            f"rows by hand: {', '.join(bulk_accepted_depicts)}"
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
                # a per-row approve: a bulk-accept on a depicts claim was
                # refused above, before anything was returned
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


@dataclass
class AcceptAllResult:
    """What `accept_all` did. `mode` is the CSV kind it detected; for the
    claims gate everything is `approved`, for the equivalences gate the
    counts break the machine's verdicts down (see `ratify_equivalence_
    verdicts`)."""

    mode: str  # "claims" | "equivalences"
    total: int
    approved: int
    rejected: int = 0
    uncertain: int = 0
    conflicted: int = 0


def _depicts_rows(rows: list[dict]) -> list[str]:
    """Names of the rows whose `property` reads `depicts`, ignoring case and
    surrounding space: `claim <claim_id>`, or `row <n>` for a row with no
    claim_id, counting the header as row 1 as a spreadsheet does."""
    names = []
    for n, row in enumerate(rows, start=2):
        if (row.get("property") or "").strip().lower() != DEPICTS:
            continue
        cid = (row.get("claim_id") or "").strip()
        names.append(f"claim {cid}" if cid else f"row {n}")
    return names


def accept_all(source_csv: Path, out_csv: Path) -> AcceptAllResult:
    """Copy a review CSV to `out_csv` with the `decision` column filled in,
    ratifying the machine wholesale. Only `decision` is touched — every
    content column (and the `claim_id` / `entity_id` the appliers match on)
    is preserved verbatim.

    Two gates, detected by the CSV's columns:

    * **claims** (`review.csv`): any sheet with a column like `claim_id`,
      compared ignoring case, surrounding space and invisible format
      characters, whatever else it has. Every row -> `bulk-accept` (an
      approval token that also records the whole-sheet disposition, so the
      write path can flag these for reappraisal — ADR-0023). Each
      extracted claim is a proposal to add, so accepting the sheet is
      approving them all.
    * **equivalences** (`equivalences.csv`: `entity_id` + `verdict` and
      nothing like `claim_id`): each row's `decision` is set from its
      machine `verdict` — accept the verdict, NOT a blanket "same"
      (`ratify_equivalence_verdicts`). A blanket approve would merge even
      the pairs the machine judged distinct; ratifying rejects those and
      mints the entity fresh.

    Ratified verdicts are written as `approve`, which a claims sheet's rows
    must never get from here (ADR-0005). The kind check reads the
    normalised names above, and a column name that still holds an
    invisible format character is refused outright (below). A claims
    sheet disguised past both, say by a no-break space, a look-alike or a
    full-width letter inside `claim_id`, does take the equivalences
    branch. Its output then carries the `entity_id` and `verdict` columns
    that `apply_review` refuses, so none of its `approve`s is applied.

    A curator's deliberate bulk-accept of a whole sheet — the human end of
    the "explicit approve" rule (R3), not auto-confirmation. The machine
    never runs it; the reviewed file it writes is what the gate consumes.
    It trades per-row scrutiny for speed on a trusted or spot-checked
    batch — not the default.

    `depicts` claims are outside that trade, whether face detection or a
    source's text proposed them: only a per-row human decision confirms
    one (ADR-0005). A claims sheet is refused whole with a ValueError,
    before anything is written (so `out_csv` is left as it was), when:

    * any row's `property` reads `depicts`, ignoring case and surrounding
      space; the error names those rows;
    * it lacks a `claim_id` column, or exactly one `property` column,
      named as export-review writes them, so its depicts rows cannot be
      found.

    A cell edited to hide a `depicts` claim gets past these checks but not
    past `apply_review`, which reads the property from the batch and
    refuses a claims sheet carrying an equivalences sheet's columns.

    Any sheet is refused the same way when a column name still holds an
    invisible format character after decoding (a second byte-order mark
    from re-saving, a pasted zero-width space), when a row has more cells
    than the header, or when it is neither gate (e.g. the reconcile-places
    sheet).
    """
    with open(source_csv, newline="", encoding=SHEET_ENCODING) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "decision" not in fieldnames:
            raise ValueError(
                f"{source_csv} is not a review CSV (no 'decision' column)"
            )
        rows = list(reader)

    invisible = [name for name in fieldnames if _has_format_character(name)]
    if invisible:
        raise ValueError(
            f"{source_csv} has column names with invisible format "
            f"characters: {', '.join(map(repr, invisible))} — a second "
            f"byte-order mark from re-saving, or a pasted zero-width space; "
            f"save the sheet without them before accepting it"
        )
    ragged = [str(n) for n, row in enumerate(rows, start=2) if None in row]
    if ragged:
        raise ValueError(
            f"{source_csv} has more cells than columns in row(s) "
            f"{', '.join(ragged)} (the header is row 1) — accept-all copies "
            f"a sheet whole or not at all"
        )

    if "claim_id" in {_header_key(name) for name in fieldnames}:
        if "claim_id" not in fieldnames:
            raise ValueError(
                f"{source_csv} has a column like 'claim_id' but none named "
                f"exactly 'claim_id' — accept-all takes it for a claims "
                f"sheet, whose columns must be named as export-review "
                f"writes them"
            )
        properties = fieldnames.count("property")
        if properties != 1:
            found = (
                f"{properties} 'property' columns"
                if properties
                else "no 'property' column"
            )
            raise ValueError(
                f"{source_csv} is a claims sheet (it has 'claim_id') with "
                f"{found} — accept-all needs exactly one, as export-review "
                f"writes it, to find the depicts rows only a per-row human "
                f"decision confirms (ADR-0005)"
            )
        depicts = _depicts_rows(rows)
        if depicts:
            raise ValueError(
                f"{source_csv} has {len(depicts)} depicts row(s) — "
                f"accept-all never confirms a depicts claim, which takes a "
                f"per-row human decision (ADR-0005); decide this sheet by "
                f"hand. Depicts rows: {', '.join(depicts)}"
            )
        for row in rows:
            row["decision"] = "bulk-accept"
        result = AcceptAllResult(
            mode="claims", total=len(rows), approved=len(rows)
        )
    elif "verdict" in fieldnames and "entity_id" in fieldnames:
        from yizkr.pipeline.reconcile import ratify_equivalence_verdicts

        counts = ratify_equivalence_verdicts(rows)
        result = AcceptAllResult(
            mode="equivalences", total=len(rows), **counts
        )
    elif "verdict" in fieldnames:
        raise ValueError(
            f"{source_csv} has a 'verdict' column but no 'entity_id' — "
            f"accept-all handles the claims and equivalences review CSVs "
            f"only, not this sheet (a reconcile-places CSV?)"
        )
    else:
        raise ValueError(
            f"{source_csv} has neither a 'claim_id' column nor 'entity_id' "
            f"+ 'verdict' columns — accept-all handles the claims and "
            f"equivalences review CSVs only"
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return result


def is_bulk_accepted(csv_path: Path) -> bool:
    """True iff a reviewed claims CSV is a pristine `accept-all` sheet — every
    decision is `bulk-accept` and no row carries a per-row human token. The
    write path uses this to mark the resulting edits as low-scrutiny for
    reappraisal (ADR-0023); a human who changed any decision demotes the whole
    sheet to an ordinary review. Missing/empty or non-claims CSVs -> False.
    """
    try:
        with open(csv_path, newline="", encoding=SHEET_ENCODING) as f:
            reader = csv.DictReader(f)
            if "decision" not in (reader.fieldnames or []):
                return False
            decisions = {
                row.get("decision", "").strip().lower()
                for row in reader
                if row.get("decision", "").strip()
            }
    except OSError:
        return False
    return decisions == {"bulk-accept"}
