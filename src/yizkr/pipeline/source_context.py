"""Inventory-backed locations carried into extraction and its diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yizkr.claims import SOURCE_SUPPORT, ArtifactRef, SourceSupport
from yizkr.core.verbatim import Transcript

if TYPE_CHECKING:
    from yizkr.pipeline.transcribe import Transcription


class LocatedReading(BaseModel):
    """One attributed or explicitly unresolved span of the reading text."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    owner: ArtifactRef | None
    support: SourceSupport
    state: Literal["attributed", "unsupported"] = "attributed"
    reason: str | None = None

    @model_validator(mode="after")
    def located(self):
        if self.end < self.start:
            raise ValueError("a reading span must end after its start")
        if self.state == "attributed" and self.owner is None:
            raise ValueError("an attributed reading needs a source owner")
        if self.state == "unsupported" and not self.reason:
            raise ValueError("an unsupported reading needs a reason")
        return self


class PlacedSource(BaseModel):
    """One source part and all its known structural placement occurrences."""

    model_config = ConfigDict(extra="forbid")

    owner: ArtifactRef
    support: SourceSupport


class SourceContext(BaseModel):
    """Verified source observations projected from the current inventory."""

    model_config = ConfigDict(extra="forbid")

    container: ArtifactRef
    readings: list[LocatedReading] = Field(default_factory=list)
    placements: list[PlacedSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def _places(transcription: Transcription):
    context = getattr(transcription, "source_context", None)
    if context is not None:
        for source in context.placements:
            for placement in source.support.placements:
                position = placement.locator.position
                label = (
                    f"page/slide {position}, " if position is not None else ""
                )
                yield source.owner.sha1, label + f"placement {placement.id}"


def placement_lines(transcription: Transcription) -> list[str]:
    """Prompt locations use original occurrences, before reading filters."""
    return [
        f'  {{"kind": "artifact", "sha1": "{sha1}"}}: {location}'
        for sha1, location in _places(transcription)
    ]


def placed_owners(transcription: Transcription) -> frozenset[str]:
    return frozenset(sha1 for sha1, _location in _places(transcription))


def artifact_places(transcription: Transcription) -> dict[str, str]:
    locations: dict[str, list[str]] = {}
    for sha1, location in _places(transcription):
        if location not in locations.setdefault(sha1, []):
            locations[sha1].append(location)
    return {sha1: "; ".join(places) for sha1, places in locations.items()}


def _candidates(source: dict, transcription: Transcription):
    context = transcription.source_context
    candidates = [span for span in context.readings if span.end > span.start]
    page_number = source.get("page_number")
    if page_number is not None:
        if (
            not isinstance(page_number, str)
            or not page_number.strip().isdigit()
        ):
            return [], "source support needs one physical reading page"
        page_number = page_number.strip()
        pages = [
            page
            for page in transcription.pages or ()
            if str(page.page) == page_number
        ]
        if not pages:
            return [], "source support names no physical reading page"
        candidates = [
            span
            for span in candidates
            if any(
                page.start <= span.start and span.end <= page.end
                for page in pages
            )
        ]
    excerpt = source.get("transcription_excerpt")
    quote = excerpt.get("text") if isinstance(excerpt, dict) else None
    if isinstance(quote, str) and quote.strip():
        candidates = [
            span
            for span in candidates
            if Transcript(transcription.text[span.start : span.end]).spans(
                quote
            )
        ]
    if not candidates:
        return [], "supporting text cannot be attributed to a source part"
    if any(span.end > len(transcription.text) for span in candidates):
        return [], "source support lies outside its reading"
    from yizkr.pipeline.transcribe import reading_digest

    digest = reading_digest(transcription)
    if any(
        span.support.reading_sha256 is not None
        and span.support.reading_sha256 != digest
        for span in candidates
    ):
        return [], "source support names a different reading revision"
    unresolved = [span for span in candidates if span.state != "attributed"]
    if unresolved:
        return [], "; ".join(dict.fromkeys(span.reason for span in unresolved))
    owners = {span.owner.sha1 for span in candidates}
    if len(owners) != 1:
        return [], "supporting text has ambiguous source ownership"
    parts = {span.support.part_id for span in candidates}
    if len(parts) > 1 and any(
        span.support.locator.kind not in {"image", "embedded_resource"}
        for span in candidates
    ):
        return [], "supporting text occurs in more than one source part"
    return candidates, None


def bind_source_support(
    claims: list, transcription: Transcription | None
) -> tuple[list, list[dict]]:
    """Discard supplied provenance and bind only unambiguous located support.

    A transcription without source context retains the standalone compatibility
    path. It cannot acquire trusted support from model-provided fields.
    """
    accepted, rejects = [], []
    for raw in claims:
        if not isinstance(raw, dict) or not isinstance(
            raw.get("source"), dict
        ):
            accepted.append(
                raw
            )  # ordinary claim validation diagnoses its shape
            continue
        claim = {**raw, "source": dict(raw["source"])}
        claim["source"].pop("support", None)
        if (
            transcription is None
            or getattr(transcription, "source_context", None) is None
        ):
            accepted.append(claim)
            continue
        candidates, reason = _candidates(claim["source"], transcription)
        if reason is not None:
            rejects.append(
                {
                    "claim": claim,
                    "problems": [reason],
                    "reasons": [SOURCE_SUPPORT],
                }
            )
            continue
        # Equivalent observations of an image keep a real, deterministic
        # source-part citation; repeats do not become independent witnesses.
        span = min(
            candidates, key=lambda item: (item.support.part_id, item.start)
        )
        claim["source"]["artifact"] = span.owner.model_dump(mode="json")
        claim["source"]["support"] = span.support.model_dump(mode="json")
        accepted.append(claim)
    return accepted, rejects


def support_failure(rejects: list[dict]) -> str | None:
    if not rejects:
        return None
    reasons = "; ".join(
        dict.fromkeys(
            problem for item in rejects for problem in item["problems"]
        )
    )
    count = len(rejects)
    return f"source attribution is incomplete ({count} claims): {reasons}"
