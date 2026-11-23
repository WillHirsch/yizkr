"""How a claim and its values are named in text.

`claim_id` is the content hash a claim is known by: extraction's excerpt
audit, a review row and the writer's refusals name a claim by it.
`describe_value` and `describe_ref` show a value and a ref as the
extraction critic lists them and a review row shows them
(yizkr.pipeline.review), and UNKNOWN_SHOWN is how both show an unknown
value.

The module depends on the claim schema alone, so the stages before
review (chunking, critique) name claims and values without the review
sheet's code.
"""

from __future__ import annotations

import hashlib
import json

from yizkr.claims import (
    Claim,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    TimeValue,
    UnknownValue,
    VocabRef,
)


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


def describe_ref(ref) -> str:
    """A ref as a review row and the extraction critic show it: an item
    by its QID, a vocabulary item by its key, a local entity as
    NEW:<local id> with its label in brackets, and any other ref, such as
    an artifact's, as str gives it."""
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
    any value but a date (which the sheet shows through
    `review.sheet_value`):
    an item by its reference, a date
    as `YYYY[-MM[-DD]]` with its precision when it is not the plain one, a
    text by its text and language, an unknown value (ADR-0040 §2.2) as
    "unknown value". Every date is Gregorian and of 1 CE or later
    (ADR-0034)."""
    if isinstance(value, UnknownValue):
        return UNKNOWN_SHOWN
    if isinstance(value, ItemValue):
        return describe_ref(value.value)
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
