"""Human review step: ClaimBatch <-> CSV for OpenRefine (R4).

`export_review_csv` renders one row per claim with human-readable
values, source locators, a machine-written `warning` column, and empty
curator columns. Reviewers work in OpenRefine or any spreadsheet and
fill:

* `decision` — approve / reject (nothing passes without it);
* `correction` — a replacement value for an approved claim the machine
  misread (ROADMAP item 11): dates as `YYYY`, `YYYY-MM` or `YYYY-MM-DD`
  (never an ambiguous format), plain text for string values, a plain
  number for quantity values. The
  corrected claim is curator-attributed: it keeps the artifact
  reference but no extraction-run reference (ADR-0003 — process
  provenance is for machine writes);
* `living` — `yes` / `no` sets the `living person` privacy flag for the
  row's subject (ROADMAP item 10): a curated claim referenced to the
  artifact whose review prompted it. The flag drives the public-export
  exclusion (docs/MODELING.md).

The `warning` column flags subjects with no death data in the batch (or
the graph, when the caller supplies known deaths) and activity within
the living-person cutoff — the reviewer decides whether to set
`living`.

`apply_review` filters the ORIGINAL batch by the returned CSV: rows are
matched by `claim_id`, a content hash of the claim, so reordering,
sorting, or column shuffling in the review tool cannot mis-apply
decisions — and any edit to claim content invalidates the id loudly
instead of silently writing altered data. Corrections change the claim
only AFTER matching, through the `correction` column.

No auto-confirmation: a claim with no decision is NOT approved —
human confirmation is part of the data flow (R3, ADR-0005).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
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
    "warning",
    "decision",
    "correction",
    "living",
    "notes",
]

# mirror of the public-export policy (docs/MODELING.md): activity within
# this window and no death data means "possibly living"
LIVING_CUTOFF_YEARS = 100

_CORRECTION_DATE = re.compile(r"^(\d{1,4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def claim_id(claim: Claim) -> str:
    """Stable content hash identifying a claim across export/import."""
    canonical = json.dumps(
        claim.model_dump(mode="json", exclude_none=True, exclude_defaults=False),
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


# -- living-person warning (ROADMAP item 10) ------------------------------


def _subject_key(claim: Claim) -> str:
    return _describe_ref(claim.subject)


def _claim_years(claim: Claim) -> list[int]:
    years = [claim.value.year] if isinstance(claim.value, TimeValue) else []
    years += [
        qualifier.value.year
        for qualifier in claim.qualifiers
        if isinstance(qualifier.value, TimeValue)
    ]
    return years


def living_warnings(
    batch: ClaimBatch,
    known_deaths: set[str] | None = None,
    cutoff_year: int | None = None,
) -> dict[str, str]:
    """subject key -> warning, for human subjects with no death data and
    activity within the cutoff window.

    `known_deaths` lets the caller add graph knowledge (QIDs with a
    recorded death) so already-documented deaths don't warn spuriously.
    Offline callers may omit it; the warning is advisory either way.
    """
    if cutoff_year is None:
        cutoff_year = datetime.now(UTC).year - LIVING_CUTOFF_YEARS
    human_ids = {record.id for record in batch.entities if record.kind == "human"}

    def is_human_subject(claim: Claim) -> bool:
        subject = claim.subject
        if isinstance(subject, LocalRef):
            if subject.local_id in human_ids:
                return True
            return subject.instance_of == "human"
        # QID subjects: the batch cannot tell; treat person-shaped
        # properties as evidence enough for an advisory warning
        return claim.property in (
            "date_of_birth",
            "place_of_birth",
            "date_of_death",
            "place_of_death",
            "age_at_death",
            "father",
            "mother",
            "spouse",
            "residence",
        )

    subjects: dict[str, dict] = {}
    for claim in batch.claims:
        if not is_human_subject(claim):
            continue
        entry = subjects.setdefault(
            _subject_key(claim), {"death": False, "latest": None}
        )
        if claim.property in ("date_of_death", "place_of_death", "age_at_death"):
            entry["death"] = True
        for year in _claim_years(claim):
            if entry["latest"] is None or year > entry["latest"]:
                entry["latest"] = year

    warnings: dict[str, str] = {}
    for key, entry in subjects.items():
        if entry["death"]:
            continue
        if known_deaths and key in known_deaths:
            continue
        if entry["latest"] is not None and entry["latest"] >= cutoff_year:
            warnings[key] = (
                f"no death data and activity in {entry['latest']} — possibly "
                f"living; consider living=yes (excludes from public exports)"
            )
    return warnings


def export_review_csv(
    batch: ClaimBatch,
    path: Path,
    known_deaths: set[str] | None = None,
    cutoff_year: int | None = None,
) -> int:
    """Write the review CSV; returns the number of rows."""
    warnings = living_warnings(batch, known_deaths, cutoff_year)
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
                    "warning": warnings.get(_subject_key(claim), ""),
                    "decision": "",
                    "correction": "",
                    "living": "",
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
            amount = int(text) if re.fullmatch(r"[+-]?\d+", text) else float(text)
        except ValueError:
            raise ValueError(
                f"correction {text!r} is not a number (quantity values "
                f"take a plain number)"
            ) from None
        corrected = value.model_copy(update={"amount": amount})
    elif hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        corrected = value.model_copy(update={"value": text})
    else:
        raise ValueError(
            f"{type(value).__name__} cannot be corrected as text — reject "
            f"the claim instead (item identity belongs to the equivalence "
            f"review)"
        )
    return claim.model_copy(update={"value": corrected, "curated": True})


LIVING_VALUES = {"yes", "no"}


def _living_claim(template: Claim, flag: str) -> Claim:
    """A curated `living person` claim for the template claim's subject,
    referenced to the artifact whose review prompted it."""
    return Claim(
        subject=template.subject,
        property="living_person",
        value=ItemValue(value=VocabRef(key=flag)),
        source=template.source.model_copy(
            update={"page_number": None, "region": None, "transcription_excerpt": None}
        ),
        curated=True,
    )


def apply_review(batch: ClaimBatch, csv_path: Path) -> tuple[ClaimBatch, list[str]]:
    """(approved batch, warnings). Only explicit approvals pass."""
    rows: dict[str, dict] = {}
    warnings: list[str] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            rows[row["claim_id"]] = row

    approved: list[Claim] = []
    living_flags: dict[str, tuple[str, Claim]] = {}
    for claim in batch.claims:
        cid = claim_id(claim)
        row = rows.pop(cid, None)
        if row is None:
            warnings.append(f"claim {cid} has no row in the CSV: excluded")
            continue
        decision = row.get("decision", "").strip().lower()
        living = row.get("living", "").strip().lower()
        if living:
            if living not in LIVING_VALUES:
                warnings.append(
                    f"claim {cid}: living={living!r} not understood "
                    f"(yes/no): flag not set"
                )
            else:
                key = _subject_key(claim)
                previous = living_flags.get(key)
                if previous and previous[0] != living:
                    warnings.append(
                        f"subject {key}: conflicting living flags "
                        f"({previous[0]} vs {living}): flag not set"
                    )
                    living_flags[key] = ("", claim)
                elif not previous:
                    living_flags[key] = (living, claim)
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
    for flag, template in living_flags.values():
        if flag:
            approved.append(_living_claim(template, flag))
    return batch.model_copy(update={"claims": approved}), warnings
