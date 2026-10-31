"""Verified neighbouring readings for interpreting one unchanged source.

Context supplies continuity, not additional evidence to cite as the target.
Its groups follow actual full-page image placements and stop at unavailable
or unsupported pages. Adjacency does not establish a shared historical source.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.claims import ArtifactRef, PlacementSupport, SourceSupport
from yizkr.pipeline.container_inventory import SourcePart
from yizkr.pipeline.container_sources import (
    ContainerSources,
    image_page_source,
)
from yizkr.pipeline.stages import canonical_json
from yizkr.pipeline.transcribe import Transcription, reading_digest


class _ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextPassage(_ContextModel):
    owner: ArtifactRef
    support: SourceSupport
    position: int = Field(ge=1)
    kind: Literal["image", "native"]
    text: str
    language: str


class ContextGap(_ContextModel):
    container: ArtifactRef
    position: int | None = Field(default=None, ge=1)
    part_id: str | None = None
    reason: str


class ContextGroup(_ContextModel):
    container: ArtifactRef
    targets: tuple[PlacementSupport, ...]
    passages: tuple[ContextPassage, ...]


class InterpretationContext(_ContextModel):
    target: ArtifactRef
    reading_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    groups: tuple[ContextGroup, ...] = ()
    gaps: tuple[ContextGap, ...] = ()

    @property
    def has_context(self) -> bool:
        return any(
            passage.owner != self.target and passage.text.strip()
            for group in self.groups
            for passage in group.passages
        )

    def validate_target(self, transcription: Transcription) -> None:
        if (
            transcription.artifact != self.target
            or reading_digest(transcription) != self.reading_sha256
        ):
            raise ValueError(
                "interpretation context belongs to another reading"
            )

    def prompt_block(self) -> str:
        return interpretation_block(self)


@dataclass(frozen=True)
class ContainerReadings:
    """Current admitted projection and products verified by the caller.

    An absent parent or image product is a gap, never a reason to read a stale
    file. This value carries no paths and the builder performs no I/O.
    """

    sources: ContainerSources
    parent: Transcription | None
    images: Mapping[str, Transcription]


def interpretation_block(context: InterpretationContext) -> str:
    """Render the same context and evidence boundary for every model stage."""
    if not context.has_context:
        return ""
    lines = [
        "Interpretation context only",
        f"The primary source remains sha1:{context.target.sha1}. "
        "Use the neighbouring readings below only to interpret continuations, "
        "names and references in that primary reading. Every claim must be "
        "supported by the primary reading and cite it. Do not extract claims "
        "supported only by a neighbour or quote a neighbour as primary "
        "evidence. "
        "Mention line numbers refer only to the numbered primary reading. "
        "Preparation statements and markers must also come from the primary "
        "reading, never from this context. "
        "A shared container or adjacent position does not establish that "
        "these "
        "are one historical source or the same person. Gaps end continuity; "
        "do not bridge them.",
    ]
    for index, group in enumerate(context.groups, 1):
        positions = sorted(
            {target.locator.position for target in group.targets}
        )
        lines.append(
            f"Context group {index}; container sha1:{group.container.sha1}; "
            "primary-source physical positions: "
            + ", ".join(map(str, positions))
        )
        for passage in group.passages:
            label = (
                "primary reading"
                if passage.owner == context.target
                else "interpretation only"
            )
            body = (
                "Primary source occurs here; use the supplied primary reading."
                if passage.owner == context.target
                else passage.text or "[verified empty reading]"
            )
            lines.append(
                f"Physical position {passage.position}; "
                f"owner sha1:{passage.owner.sha1}; {label}\n"
                f"{body}"
            )
    for gap in context.gaps:
        position = (
            f", physical position {gap.position}"
            if gap.position is not None
            else ""
        )
        lines.append(
            f"Context gap in sha1:{gap.container.sha1}{position}: {gap.reason}"
        )
    return "\n\n" + "\n\n".join(lines)


def _gap(sources, position, reason, part=None):
    return ContextGap(
        container=sources.inventory.source,
        position=position,
        part_id=part.id if part is not None else None,
        reason=reason,
    )


def _native_passage(sources, parent, page, body):
    context = parent.source_context
    digest = reading_digest(parent)
    if context is None or context.container != sources.inventory.source:
        return None
    expected = sources.support(body.id)
    spans = [
        span
        for span in context.readings
        if span.state == "attributed"
        and span.owner == sources.inventory.source
        and span.support.container == expected.container
        and span.support.part_id == expected.part_id
        and span.support.locator == expected.locator
        and span.support.placements == expected.placements
        and span.support.reading_sha256 == digest
        and page.start <= span.start <= span.end <= page.end
    ]
    bounds = {(span.start, span.end) for span in spans}
    if len(bounds) != 1:
        return None
    start, end = next(iter(bounds))
    # The body can end before speaker notes; those have a distinct source part.
    if start != page.start or (
        end != page.end
        and not any(
            note.locator.position == page.page and note.text
            for note in sources.notes
        )
    ):
        return None
    return ContextPassage(
        owner=sources.inventory.source,
        support=expected.model_copy(
            update={"reading_sha256": digest}, deep=True
        ),
        position=page.page,
        kind="native",
        text=parent.text[start:end],
        language=parent.language,
    )


def _image_passage(target, supplied, body):
    sources = supplied.sources
    found = image_page_source(sources, body)
    if found is None:
        return None, None, "page image has no verified source placement"
    source, part, placement = found
    if (
        part.eligibility.state != "allowed"
        or part.source_role != "independent"
        or part.ownership_issue
    ):
        return None, None, "page image is excluded or unsupported"
    primary = source.owner == target.artifact
    reading = target if primary else supplied.images.get(source.owner.sha1)
    if reading is None:
        return None, None, "page image reading is unavailable"
    item = source.current_input
    if (
        reading.artifact != source.owner
        or item is None
        or not primary
        and reading.reading_input is not None
        and reading.reading_input != item.representation
    ):
        return None, None, "page image reading does not match its source"
    if (
        reading.flag
        or reading.input_representation is not None
        or not reading.pages
        or any(page.status == "illegible" for page in reading.pages)
    ):
        return None, None, "page image reading is unsupported"
    support = sources.support(part.id, placement_ids=[placement.id])
    support.reading_sha256 = reading_digest(reading)
    text = "" if primary else reading.prompt_text()
    if not primary and all(page.status == "blank" for page in reading.pages):
        text = ""
    passage = ContextPassage(
        owner=source.owner,
        support=support.model_copy(deep=True),
        position=body.locator.position,
        kind="image",
        text=text,
        language=reading.language,
    )
    anchor = (
        PlacementSupport(
            id=placement.id, locator=placement.locator.model_copy()
        )
        if primary
        else None
    )
    return passage, anchor, None


def _container_context(target, supplied):
    sources, parent = supplied.sources, supplied.parent
    bodies: dict[int, list[SourcePart]] = defaultdict(list)
    for body in sources.bodies:
        if body.locator.position is not None:
            bodies[body.locator.position].append(body)
    anchors = [
        body
        for body in sources.bodies
        if (found := image_page_source(sources, body)) is not None
        and found[0].owner == target.artifact
    ]
    if not anchors:
        return (), ()
    if parent is None or parent.artifact != sources.inventory.source:
        reason = (
            "parent reading is unavailable"
            if parent is None
            else "parent reading belongs to another container"
        )
        return (), (_gap(sources, None, reason),)
    if not parent.pages:
        return (), (
            _gap(sources, None, "parent reading has no physical pages"),
        )
    pages = {page.page: page for page in parent.pages}
    groups, gaps = [], []
    passages, targets = [], []

    def finish():
        if targets:
            groups.append(
                ContextGroup(
                    container=sources.inventory.source,
                    targets=tuple(targets),
                    passages=tuple(passages),
                )
            )
        passages.clear()
        targets.clear()

    previous = 0
    for position in sorted(pages.keys() | bodies.keys()):
        if position != previous + 1:
            finish()
            gaps.append(
                _gap(sources, previous + 1, "physical page is unavailable")
            )
        previous = position
        candidates = bodies.get(position, [])
        page = pages.get(position)
        body = candidates[0] if len(candidates) == 1 else None
        method = page.provenance.method if page and page.provenance else None
        reason = None
        passage = anchor = None
        if page is None or not candidates:
            reason = "physical page is unavailable"
        elif body is None:
            reason = "physical page has ambiguous source bodies"
        elif body.eligibility.state != "allowed":
            reason = "page body is excluded or undetermined"
        elif page.status == "illegible":
            reason = "page reading is illegible"
        elif method == "image-source":
            passage, anchor, reason = _image_passage(target, supplied, body)
        elif method in {"text-layer", "slide-xml", "none"}:
            passage = _native_passage(sources, parent, page, body)
            if passage is None:
                reason = "native page reading has no verified source support"
        else:
            reason = (
                "page reading route is unsupported for interpretation context"
            )
        if reason:
            finish()
            gaps.append(_gap(sources, position, reason, body))
            continue
        passages.append(passage)
        if anchor is not None:
            targets.append(anchor)
    finish()
    return tuple(groups), tuple(gaps)


def build_interpretation_context(
    target: Transcription, containers: Sequence[ContainerReadings]
) -> InterpretationContext:
    """Build deterministic contiguous context from verified page readings.

    All placements of the target in one contiguous segment share a group.
    Conflicting observations of the same container produce a gap rather than
    choosing one by discovery order. Only the caller's current products are
    considered; this function neither reads files nor repairs them.
    """
    observations = defaultdict(dict)
    for supplied in containers:
        groups, gaps = _container_context(target, supplied)
        value = (
            [group.model_dump(mode="json") for group in groups],
            [gap.model_dump(mode="json") for gap in gaps],
        )
        observations[supplied.sources.inventory.source.sha1][
            canonical_json(value)
        ] = (
            groups,
            gaps,
        )
    groups, gaps = [], []
    for owner, versions in sorted(observations.items()):
        if len(versions) != 1:
            gaps.append(
                ContextGap(
                    container=ArtifactRef(sha1=owner),
                    reason="container has conflicting context observations",
                )
            )
            continue
        found_groups, found_gaps = next(iter(versions.values()))
        groups.extend(found_groups)
        gaps.extend(found_gaps)
    return InterpretationContext(
        target=target.artifact,
        reading_sha256=reading_digest(target),
        groups=tuple(groups),
        gaps=tuple(gaps),
    ).model_copy(deep=True)
