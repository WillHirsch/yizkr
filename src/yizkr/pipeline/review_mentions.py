"""Source observations and semantic evidence for durable identity review.

The observation's artifact/local ID locates a current product, not a durable
identity. Literal, uniquely located evidence can establish correspondence
between readings. Missing or ambiguous evidence stays explicit; neither model
ordering nor a proposal fingerprint disambiguates it. Callers supply verified
readings and allocate/persist mention and decision identities elsewhere.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.review_records import (
    MentionObservation,
    OccurrenceAnchor,
    SourceScope,
    canonical_form,
    canonical_form_digest,
    canonical_form_json,
    sorted_by_canonical_form,
    support_scope,
)
from yizkr.pipeline.fingerprints import local_refs
from yizkr.pipeline.transcribe import Transcription, reading_digest

BASIS_VERSION = 1
OBSERVATION_VERSION = 1


def _literal_spans(text: str, literal: str) -> list[tuple[int, int]]:
    if not literal.strip():
        return []
    pattern = re.escape(literal)
    if literal[0].isalnum() or literal[0] == "_":
        pattern = r"(?<!\w)" + pattern
    if literal[-1].isalnum() or literal[-1] == "_":
        pattern += r"(?!\w)"
    return [
        (match.start(), match.end()) for match in re.finditer(pattern, text)
    ]


def _quote_range(text: str, numbers: list[int]) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    selected = sorted(set(numbers))
    if (
        not selected
        or selected[0] < 1
        or selected[-1] > len(lines)
        or selected != list(range(selected[0], selected[-1] + 1))
    ):
        return None
    start = sum(len(line) for line in lines[: selected[0] - 1])
    end = sum(len(line) for line in lines[: selected[-1]])
    # Newline terminators locate lines, but are not themselves quoted text.
    end = start + len(text[start:end].rstrip("\r\n\v\f\x85\u2028\u2029"))
    return (start, end) if end > start else None


def _scope(transcription: Transcription, start: int, end: int):
    context = transcription.source_context
    if context is not None:
        overlapping = [
            span
            for span in context.readings
            if span.start < end and start < span.end
        ]
        if any(span.state != "attributed" for span in overlapping):
            return None, "unsupported_source_scope"
        if any(span.owner != transcription.artifact for span in overlapping):
            return None, "ambiguous_or_missing_source_scope"
        if any(
            not 0 <= span.start <= span.end <= len(transcription.text)
            for span in overlapping
        ):
            return None, "invalid_source_range"
        digest = reading_digest(transcription)
        if any(
            span.support.reading_sha256 is not None
            and span.support.reading_sha256 != digest
            for span in overlapping
        ):
            return None, "reading_support_mismatch"
        candidates = [
            span
            for span in overlapping
            if span.start <= start
            and end <= span.end
            and span.owner == transcription.artifact
        ]
        # Equivalent duplicate observations can be consolidated, but different
        # native parts cannot be selected by their order in the context.
        unique = {
            canonical_form_json(
                {
                    "support": support_scope(span.support),
                    "start": span.start,
                    "end": span.end,
                }
            ): span
            for span in candidates
        }
        if len(unique) != 1:
            return None, "ambiguous_or_missing_source_scope"
        span = next(iter(unique.values()))
        if any(
            support_scope(other.support) != support_scope(span.support)
            for other in overlapping
        ):
            return None, "ambiguous_or_missing_source_scope"
        return (span.start, span.end, span.support, None), None
    if transcription.pages:
        previous_end = 0
        previous_page = 0
        for page in transcription.pages:
            if not (
                previous_end
                <= page.start
                <= page.end
                <= len(transcription.text)
                and previous_page < page.page
            ):
                return None, "invalid_page_scope"
            previous_end, previous_page = page.end, page.page
        pages = [
            page
            for page in transcription.pages
            if page.start <= start
            and end <= page.end
            and page.status == "transcribed"
        ]
        if len(pages) != 1:
            return None, "ambiguous_or_unread_page"
        page = pages[0]
        return (page.start, page.end, None, page.page), None
    return (0, len(transcription.text), None, None), None


def _anchor(record, transcription: Transcription):
    bounds = _quote_range(transcription.text, record.mention_lines)
    if bounds is None:
        return None, {}, "missing_or_noncontiguous_mention_lines"
    start, end = bounds
    scoped, problem = _scope(transcription, start, end)
    if problem:
        return None, {}, problem
    scope_start, scope_end, support, page = scoped
    quote = transcription.text[start:end]
    scope = SourceScope(
        owner=transcription.artifact.model_copy(deep=True),
        support=support.model_copy(deep=True) if support else None,
        physical_page=page,
    )
    context = {
        "source_scope": scope,
        "context_quote": quote,
        "context_start": start,
        "context_end": end,
    }
    # The same line text twice within the source scope is not a location.
    scope_text = transcription.text[scope_start:scope_end]
    first_quote = scope_text.find(quote)
    if first_quote < 0 or scope_text.find(quote, first_quote + 1) >= 0:
        return None, context, "repeated_source_quote"
    candidates = set()
    for form in (record.label, *record.aliases):
        matches = _literal_spans(quote, form)
        if len(matches) == 1:
            candidates.add(matches[0])
    if not candidates:
        return None, context, "missing_or_ambiguous_literal_mention"
    # A full name and a contained shorter alias describe the same literal
    # occurrence. Disjoint aliases and crossing spans are not consolidated.
    outer = [
        span
        for span in candidates
        if all(
            span[0] <= other[0] and other[1] <= span[1] for other in candidates
        )
    ]
    if len(outer) != 1:
        return None, context, "multiple_literal_occurrences"
    first, last = outer[0]
    return (
        OccurrenceAnchor(
            **scope.model_dump(),
            quote=quote,
            quote_start=start,
            quote_end=end,
            mention_start=first,
            mention_end=last,
            literal=quote[first:last],
        ),
        context,
        None,
    )


# Every limitation observe_mentions records, in the order its checks run: a
# mention with none of them is anchored.
OBSERVATION_LIMITATIONS = (
    "reading_unavailable",
    "native_mention_location_unavailable",
    "missing_or_noncontiguous_mention_lines",
    "unsupported_source_scope",
    "ambiguous_or_missing_source_scope",
    "invalid_source_range",
    "reading_support_mismatch",
    "invalid_page_scope",
    "ambiguous_or_unread_page",
    "repeated_source_quote",
    "missing_or_ambiguous_literal_mention",
    "multiple_literal_occurrences",
    "shared_source_occurrence",
)


def observe_mentions(
    artifact: str,
    batch: ClaimBatch,
    transcription: Transcription | None = None,
) -> list[MentionObservation]:
    """Observe current mentions without choosing identities or reading files.

    Whole-product identity includes local IDs, ordering and source context;
    process metadata does not affect it. It can locate an unchanged product,
    including an ambiguous observation, without pretending to resolve that
    observation across a different reading or model answer.
    """
    owner = ArtifactRef(sha1=artifact)
    if transcription is not None and transcription.artifact != owner:
        raise ValueError("mention reading belongs to another source")
    records = {record.id: record for record in batch.entities}
    if len(records) != len(batch.entities):
        raise ValueError("duplicate local IDs cannot identify observations")
    referenced = {ref.local_id for ref in local_refs(batch)}
    if records and not referenced.issubset(records):
        raise ValueError("local reference has no entity-manifest record")
    local_ids = (
        list(records)
        if records
        else list(dict.fromkeys(ref.local_id for ref in local_refs(batch)))
    )
    digest = reading_digest(transcription) if transcription else None
    product = canonical_form_digest(
        {
            "observation_version": OBSERVATION_VERSION,
            "artifact": owner.sha1,
            "batch": batch.model_dump(mode="json", exclude={"extraction_run"}),
            "reading": digest,
            "source_context": (
                transcription.source_context.model_dump(mode="json")
                if transcription and transcription.source_context
                else None
            ),
        }
    )
    observations = []
    for local_id in local_ids:
        context = {}
        if transcription is None:
            anchor, problem = None, "reading_unavailable"
        elif local_id not in records:
            anchor, problem = None, "native_mention_location_unavailable"
        else:
            anchor, context, problem = _anchor(
                records[local_id], transcription
            )
        observations.append(
            MentionObservation(
                artifact=owner.sha1,
                local_id=local_id,
                product_sha256=product,
                reading_sha256=digest,
                anchor=anchor,
                limitations=[problem] if problem else [],
                **context,
            )
        )
    shared = set()
    for index, left in enumerate(observations):
        a = left.anchor
        if a is None:
            continue
        for other, right in enumerate(observations[index + 1 :], index + 1):
            b = right.anchor
            if b is None or a.scope_key != b.scope_key:
                continue
            if (
                a.quote_start + a.mention_start < b.quote_start + b.mention_end
                and b.quote_start + b.mention_start
                < a.quote_start + a.mention_end
            ):
                shared.update((index, other))
    return [
        observation.model_copy(
            update={
                "anchor": None,
                "limitations": ["shared_source_occurrence"],
            }
        )
        if index in shared
        else observation
        for index, observation in enumerate(observations)
    ]


def _semantic(value: Any, stable_mentions: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        if value.get("kind") == "local":
            local_id = value["local_id"]
            if local_id not in stable_mentions:
                raise ValueError(
                    f"no stable correspondence for local ID {local_id!r}"
                )
            result = {"kind": "mention", "id": stable_mentions[local_id]}
            if value.get("instance_of") is not None:
                result["instance_of"] = value["instance_of"]
            return result
        result = {
            key: _semantic(item, stable_mentions)
            for key, item in value.items()
            if key != "reading_sha256"
        }
        for key in ("qualifiers", "placements"):
            if key in result:
                result[key] = sorted_by_canonical_form(result[key])
        return result
    if isinstance(value, list):
        return [_semantic(item, stable_mentions) for item in value]
    return canonical_form(value)


def _referenced_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        if value.get("kind") == "local":
            return {value["local_id"]}
        return set().union(*(_referenced_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_referenced_ids(item) for item in value))
    return set()


def canonical_basis(
    batch: ClaimBatch,
    local_id: str,
    stable_mentions: Mapping[str, str],
) -> dict:
    """Material identity evidence, separate from proposal and reading hashes.

    The caller supplies correspondence for every referenced local mention.
    Reading digests remain in observation provenance; unchanged located quotes
    can support the same material basis across a reading revision. Display
    labels, summaries, local IDs, process stamps and confidence ordering do not
    establish identity. Source aliases, structured interpretation, source
    ownership/locations, excerpts and uncertainty remain part of the basis.
    """
    records = {record.id: record for record in batch.entities}
    if len(records) != len(batch.entities):
        raise ValueError("duplicate local IDs cannot identify a review basis")
    record = records.get(local_id)
    if record is None and local_id not in {
        ref.local_id for ref in local_refs(batch)
    }:
        raise ValueError("review basis names no current mention")
    if local_id not in stable_mentions:
        raise ValueError(f"no stable correspondence for local ID {local_id!r}")
    material = (
        record.model_dump(
            mode="json",
            exclude={
                "id",
                "label",
                "summary",
                "mention_lines",
                "qid",
            },
        )
        if record
        else None
    )
    if material is not None:
        material["aliases"] = sorted(
            set(canonical_form(name) for name in material["aliases"])
        )
        # Structured name evidence lists express sets of interpreted forms;
        # their display order does not carry an additional identity assertion.
        structure = material.get("structure")
        if structure:
            for key, value in structure.items():
                if isinstance(value, list) and key != "within":
                    structure[key] = sorted_by_canonical_form(value)
    claims = []
    for claim in batch.claims:
        data = claim.model_dump(
            mode="json", exclude={"row_approval", "confidence"}
        )
        if local_id in _referenced_ids(data):
            claims.append(_semantic(data, stable_mentions))
    return canonical_form(
        {
            "basis_version": BASIS_VERSION,
            "mention": stable_mentions[local_id],
            "interpretation": material,
            "claims": sorted_by_canonical_form(claims),
        }
    )
