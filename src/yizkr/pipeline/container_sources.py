"""One source/placement view for container readers and downstream consumers.

Binding preparation observes original parts, current eligible representations
and verified prior owner bytes. Projection never allocates an owner or changes
the inventory. A reading input is deliberately separate from canonical owner
media: converting an image does not rename a previously bound source.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from yizkr.claims import ArtifactRef, PlacementSupport, SourceSupport
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    Eligibility,
    MediaOwnerBinding,
    Placement,
    SourcePart,
    _read_regular,
)

if TYPE_CHECKING:
    from yizkr.pipeline.presentation import DerivedMediaRecord


def _never(_sha1: str, _names: Sequence[str] = ()) -> bool:
    return False


@dataclass(frozen=True)
class PartInput:
    part_id: str
    placement_ids: tuple[str, ...]
    representation: ArtifactRef
    file: str
    media_type: str


@dataclass
class BoundSource:
    owner: ArtifactRef
    file: str
    media_type: str
    names: list[str] = field(default_factory=list)
    part_ids: list[str] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    inputs: list[PartInput] = field(default_factory=list)
    reading_path: str | None = None
    reading_digest: str | None = None
    limitations: list[str] = field(default_factory=list)
    readable: bool = False

    @property
    def current_input(self) -> PartInput | None:
        if len({item.representation.sha1 for item in self.inputs}) != 1:
            return None
        return min(
            self.inputs,
            key=lambda item: (
                item.file != self.file,
                item.file,
                item.part_id,
            ),
        )

    @property
    def slides(self) -> list[int]:
        return sorted(
            {
                p.locator.position
                for p in self.placements
                if p.locator.position is not None
            }
        )


@dataclass(frozen=True)
class UnresolvedSource:
    part_id: str
    reason: str


@dataclass
class ContainerSources:
    inventory: ContainerInventory
    sources: dict[str, BoundSource] = field(default_factory=dict)
    inputs: list[PartInput] = field(default_factory=list)
    bodies: list[SourcePart] = field(default_factory=list)
    notes: list[SourcePart] = field(default_factory=list)
    unresolved: list[UnresolvedSource] = field(default_factory=list)

    def support(
        self, part_id: str, *, placement_ids: Sequence[str] | None = None
    ) -> SourceSupport:
        part = next(
            part for part in self.inventory.parts if part.id == part_id
        )
        return SourceSupport(
            container=self.inventory.source,
            part_id=part.id,
            locator=part.locator,
            placements=[
                PlacementSupport(id=p.id, locator=p.locator)
                for p in self.inventory.placements
                if p.child == part.id
                and (placement_ids is None or p.id in placement_ids)
            ],
        )


def reading_file(owner: ArtifactRef | str) -> str:
    """Keep existing per-owner reading products; they are not inventory."""
    sha1 = owner.sha1 if isinstance(owner, ArtifactRef) else owner
    return f"derived-media/{sha1}.transcription.json"


def _payload(
    directory: Path,
    name: str,
    reference: ArtifactRef,
    data: Mapping[str, bytes] | None = None,
) -> bytes | None:
    value = data.get(reference.sha1) if data is not None else None
    if value is None:
        value = _read_regular(directory, name)
    if value is None or hashlib.sha1(value).hexdigest() != reference.sha1:
        return None
    return value


def _aliases(inventory: ContainerInventory) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for part in inventory.parts:
        if part.byte_object is not None and part.locator.package_part:
            result.setdefault(part.byte_object.sha1, set()).add(
                PurePosixPath(part.locator.package_part).name
            )
    return {key: sorted(names) for key, names in result.items()}


def _inputs(part: SourcePart, placements: list[Placement]) -> list[PartInput]:
    found = [
        PartInput(
            part.id,
            tuple(rep.placement_ids or [p.id for p in placements]),
            rep.byte_object,
            rep.file,
            rep.media_type,
        )
        for rep in part.representations
        if rep.method == "pdfium-image"
        and rep.file is not None
        and rep.media_type.startswith("image/")
        and rep.eligibility.state == "allowed"
    ]
    if (
        not found
        and part.file is not None
        and part.byte_object is not None
        and (part.media_type or "").startswith("image/")
    ):
        found.append(
            PartInput(
                part.id,
                tuple(p.id for p in placements),
                part.byte_object,
                part.file,
                part.media_type,
            )
        )
    return found


def _blocked(part: SourcePart, aliases: Sequence[str], excluded) -> bool:
    references = [
        part.byte_object,
        *(r.byte_object for r in part.representations),
    ]
    if part.owner_binding is not None:
        references.append(part.owner_binding.artifact)
    return any(
        excluded(ref.sha1, aliases) for ref in references if ref is not None
    )


def _original_input(part: SourcePart, item: PartInput) -> bool:
    """Original image bytes, including JPEG inside lossless PDF filters."""
    return (
        part.byte_object == item.representation
        and (part.media_type or "").startswith("image/")
    ) or any(
        rep.byte_object == item.representation
        and rep.method == "pdfium-image"
        and rep.media_type == "image/jpeg"
        for rep in part.representations
    )


def _image_part(part: SourcePart) -> bool:
    """A PDF image stream, or package media of an image type."""
    return part.locator.kind == "image" or (part.media_type or "").startswith(
        "image/"
    )


def _deny(part: SourcePart) -> None:
    part.eligibility = Eligibility(
        state="excluded", reason="configured source exclusion"
    )
    part.file = None
    if part.owner_binding is not None:
        part.owner_binding.file = None
    for representation in part.representations:
        representation.file = None
        representation.eligibility = part.eligibility.model_copy()


def _canonical_file(directory: Path, owner: ArtifactRef) -> str | None:
    for folder in ("container-parts", "representations", "derived-media"):
        base = directory / folder
        if base.is_symlink() or not base.is_dir():
            continue
        for path in sorted(base.glob(f"{owner.sha1}.*")):
            if path.name.endswith(".transcription.json"):
                continue
            relative = f"{folder}/{path.name}"
            if _payload(directory, relative, owner) is not None:
                return relative
    return None


def _recover_owner_file(directory, owner, part, placements, data):
    held = _canonical_file(directory, owner)
    if held is not None:
        return held
    return next(
        (
            item.file
            for item in _inputs(part, placements)
            if item.representation == owner
            and _payload(directory, item.file, owner, data) is not None
        ),
        None,
    )


def _surviving_bindings(inventory: ContainerInventory, directory: Path):
    """Recover explicit part bindings from located readings, never labels."""
    from yizkr.pipeline.source_context import SourceContext

    parts = {p.id: p for p in inventory.parts}
    placements = {p.id: p for p in inventory.placements}
    bindings: dict[str, set[str]] = {}
    aliases: set[str] = set()
    files = ["transcription.json"]
    folder = directory / "derived-media"
    if folder.is_dir() and not folder.is_symlink():
        files.extend(
            f"derived-media/{p.name}"
            for p in folder.glob("*.transcription.json")
        )
    for filename in files:
        payload = _read_regular(directory, filename)
        if payload is None:
            continue
        try:
            reading = json.loads(payload)
            context = (
                SourceContext.model_validate(reading["source_context"])
                if reading.get("source_context")
                else None
            )
            if (
                filename == "transcription.json"
                and ArtifactRef.model_validate(reading["artifact"])
                == inventory.source
            ):
                aliases.update(
                    ArtifactRef(sha1=sha1).sha1
                    for page in reading.get("pages") or ()
                    for sha1 in page.get("images", [])
                )
            if context is None or context.container != inventory.source:
                continue
            for observation in [*context.placements, *context.readings]:
                support = observation.support
                part = parts.get(support.part_id)
                owner = observation.owner
                if (
                    part is None
                    or part.kind != "embedded_resource"
                    or owner is None
                ):
                    continue
                if (
                    support.container != inventory.source
                    or support.locator != part.locator
                ):
                    continue
                if any(
                    p.id not in placements
                    or placements[p.id].child != part.id
                    or placements[p.id].locator != p.locator
                    for p in support.placements
                ):
                    continue
                bindings.setdefault(part.id, set()).add(owner.sha1)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return bindings, aliases


def prepare_sources(
    inventory: ContainerInventory,
    data: dict[str, bytes],
    directory: Path,
    *,
    previous: ContainerInventory | None = None,
    legacy: DerivedMediaRecord | None = None,
    excluded: Callable[[str, Sequence[str]], bool] = _never,
) -> ContainerInventory:
    """Prepare explicit media-owner bindings without publication or models.

    A previous record is structural evidence only until its canonical bytes
    are verified. A legacy record can establish a binding only by exact
    current input correspondence, never by page number or filtered ordinal.
    """
    result = inventory.model_copy(deep=True)
    aliases = _aliases(result)
    old = (
        {p.id: p for p in previous.parts}
        if previous is not None and previous.source == result.source
        else {}
    )
    placed = {
        part.id: [p for p in result.placements if p.child == part.id]
        for part in result.parts
    }
    legacy_media = (
        legacy.media
        if legacy is not None and legacy.source_sha1 == result.source.sha1
        else {}
    )
    recovered, prior_aliases = _surviving_bindings(result, directory)
    recovered_owners = {
        owner for owners in recovered.values() for owner in owners
    }
    known_aliases = set(legacy_media) | prior_aliases
    legacy_candidates: dict[str, list[str]] = {}
    for part in result.parts:
        for item in _inputs(part, placed[part.id]):
            if item.representation.sha1 in known_aliases:
                legacy_candidates.setdefault(
                    item.representation.sha1, []
                ).append(part.id)
    unmatched = known_aliases - set(legacy_candidates) - recovered_owners
    fresh = set(inventory._new_bindings)
    for part in result.parts:
        if part.kind != "embedded_resource":
            continue
        names = (
            aliases.get(part.byte_object.sha1, []) if part.byte_object else []
        )
        prior = old.get(part.id)
        if (
            prior is None or prior.owner_binding is None
        ) and part.id in recovered:
            owners = recovered[part.id]
            if len(owners) != 1:
                part.ownership_issue = (
                    "surviving readings disagree about the source owner"
                )
            else:
                owner = ArtifactRef(sha1=next(iter(owners)))
                name = _recover_owner_file(
                    directory, owner, part, placed[part.id], data
                )
                part.owner_binding = MediaOwnerBinding(
                    artifact=owner,
                    file=name,
                    media_type=(
                        mimetypes.guess_type(name)[0] if name else None
                    )
                    or "application/octet-stream",
                    basis="verified-legacy-reading",
                )
                if name is None:
                    part.ownership_issue = (
                        "canonical bytes of the recorded source owner "
                        "are unavailable"
                    )
                else:
                    data[owner.sha1] = _payload(directory, name, owner, data)
        if prior is not None and prior.owner_binding is not None:
            if prior.byte_object != part.byte_object:
                part.ownership_issue = (
                    "prior owner binding has different original bytes"
                )
            else:
                part.owner_binding = prior.owner_binding.model_copy(deep=True)
                binding = part.owner_binding
                if binding.file is None:
                    binding.file = _recover_owner_file(
                        directory,
                        binding.artifact,
                        part,
                        placed[part.id],
                        data,
                    )
                payload = (
                    _payload(directory, binding.file, binding.artifact, data)
                    if binding.file
                    else None
                )
                if payload is None:
                    part.ownership_issue = (
                        "canonical bytes of the prior owner binding "
                        "are missing or damaged"
                    )
                else:
                    data[binding.artifact.sha1] = payload
                    part.ownership_issue = None
                if prior.byte_object == part.byte_object:
                    if (
                        part.reading.state == "unsupported"
                        and part.reading.reason
                    ):
                        part.limitations.append(part.reading.reason)
                    part.reading = prior.reading.model_copy(deep=True)
        if _blocked(part, names, excluded):
            _deny(part)
            continue
        if (
            inventory.container == "presentation"
            and part.byte_object is not None
            and part.file is not None
        ):
            part.eligibility = Eligibility(state="allowed")
            # Relationship-only or unused resources have no invented visible
            # occurrence and do not acquire owners through this projection.
            if any(p.occurrence == "paint" for p in placed[part.id]):
                part.source_role = "independent"
            else:
                part.source_role = "auxiliary"
            if part.source_role == "independent":
                from yizkr.pipeline.containers import image_size, is_decoration

                payload = _payload(
                    directory, part.file, part.byte_object, data
                )
                size = image_size(payload) if payload is not None else None
                if size is not None and is_decoration(*size):
                    part.source_role = "auxiliary"
        if part.owner_binding is not None or part.ownership_issue is not None:
            continue
        inputs = _inputs(part, placed[part.id])
        legacy_match = any(
            item.representation.sha1 in known_aliases for item in inputs
        )
        if part.eligibility.state != "allowed" or (
            part.source_role != "independent" and not legacy_match
        ):
            continue
        distinct = {item.representation.sha1: item for item in inputs}
        if len(distinct) != 1:
            part.ownership_issue = (
                "independent source has no unique prepared "
                "reading representation"
            )
            continue
        item = next(iter(distinct.values()))
        payload = _payload(directory, item.file, item.representation, data)
        if payload is None:
            part.ownership_issue = (
                "prepared source bytes are missing or damaged"
            )
            continue
        medium = legacy_media.get(item.representation.sha1)
        if item.representation.sha1 in known_aliases:
            candidates = legacy_candidates[item.representation.sha1]
            if len(candidates) > 1 and not all(
                _original_input(
                    candidate,
                    next(
                        value
                        for value in _inputs(candidate, placed[candidate.id])
                        if value.representation == item.representation
                    ),
                )
                for candidate in result.parts
                if candidate.id in candidates
            ):
                part.ownership_issue = (
                    "legacy image owner matches multiple distinct source parts"
                )
                continue
            name = (
                medium.file
                if medium is not None
                else _canonical_file(directory, item.representation)
            )
            canonical = (
                _payload(directory, name, item.representation)
                if name
                else None
            )
            if canonical is None:
                part.ownership_issue = (
                    "legacy owner bytes are missing or damaged"
                )
                continue
            binding_file, basis = name, "verified-legacy-reading"
            payload = canonical
        elif unmatched:
            part.ownership_issue = (
                "legacy owner cannot be matched to current "
                "source representations"
            )
            continue
        else:
            binding_file = item.file
            basis = (
                "original-media"
                if _original_input(part, item)
                else "prepared-part"
            )
            fresh.add(part.id)
        part.owner_binding = MediaOwnerBinding(
            artifact=item.representation,
            file=binding_file,
            media_type=item.media_type,
            basis=basis,
        )
        data[item.representation.sha1] = payload
    result._new_bindings = fresh
    check_owner_aliases([result])
    return result


def check_owner_aliases(
    inventories: Sequence[ContainerInventory],
    *,
    original_sources: Collection[str] = (),
) -> None:
    """Refuse fresh derived aliases across distinct original source parts."""
    owners: dict[str, dict[str, tuple[ContainerInventory, SourcePart]]] = {}
    for inventory in inventories:
        for part in inventory.parts:
            binding = part.owner_binding
            if binding is not None:
                owners.setdefault(binding.artifact.sha1, {})[part.id] = (
                    inventory,
                    part,
                )
    for owner, sources in owners.items():
        shared = len(sources) > 1 and not all(
            part.owner_binding.basis == "original-media"
            for _, part in sources.values()
        )
        for inventory, part in sources.values():
            conflict = shared or (
                owner in original_sources
                and part.owner_binding.basis != "original-media"
            )
            if conflict and part.id in inventory._new_bindings:
                part.owner_binding = None
                part.ownership_issue = (
                    "equal prepared image bytes do not establish ownership "
                    "equivalence between distinct original sources"
                )


def project_sources(
    inventory: ContainerInventory,
    directory: Path,
    *,
    data: Mapping[str, bytes] | None = None,
    excluded: Callable[[str, Sequence[str]], bool] = _never,
) -> ContainerSources:
    """Project verified source bindings, inputs and native occurrences only."""
    result = ContainerSources(inventory)
    aliases = _aliases(inventory)
    conflicting_readings: set[str] = set()
    for part in inventory.parts:
        if part.kind in {"page_body", "slide_body"}:
            result.bodies.append(part)
        elif part.kind == "speaker_notes":
            result.notes.append(part)
        if part.kind != "embedded_resource":
            continue
        placements = [p for p in inventory.placements if p.child == part.id]
        names = (
            aliases.get(part.byte_object.sha1, []) if part.byte_object else []
        )
        if part.eligibility.state != "allowed" or _blocked(
            part, names, excluded
        ):
            continue
        if part.ownership_issue:
            result.unresolved.append(
                UnresolvedSource(part.id, part.ownership_issue)
            )
            continue
        binding = part.owner_binding
        if binding is None:
            if part.source_role in {"independent", "unresolved"}:
                result.unresolved.append(
                    UnresolvedSource(part.id, "source owner is unresolved")
                )
            continue
        if (
            binding.file is None
            or _payload(directory, binding.file, binding.artifact, data)
            is None
        ):
            result.unresolved.append(
                UnresolvedSource(
                    part.id, "canonical owner bytes are unavailable"
                )
            )
            continue
        inputs = []
        for item in _inputs(part, placements):
            if (
                _payload(directory, item.file, item.representation, data)
                is None
            ):
                result.unresolved.append(
                    UnresolvedSource(
                        part.id, "reading representation is unavailable"
                    )
                )
            else:
                inputs.append(item)
        source = result.sources.setdefault(
            binding.artifact.sha1,
            BoundSource(
                binding.artifact,
                binding.file,
                binding.media_type,
            ),
        )
        source.part_ids.append(part.id)
        source.placements.extend(placements)
        source.inputs.extend(inputs)
        source.names = sorted(
            set(source.names)
            | set(
                names
                or [
                    f"page {p.locator.position}, image {p.order}"
                    for p in placements
                ]
            )
        )
        result.inputs.extend(inputs)
        reading = part.reading
        if reading.state == "read" and reading.product is not None:
            payload = _read_regular(directory, reading.product)
            if (
                payload is not None
                and hashlib.sha256(payload).hexdigest()
                == reading.product_sha256
            ):
                if source.owner.sha1 in conflicting_readings:
                    pass
                elif source.reading_path is None:
                    source.reading_path, source.reading_digest = (
                        reading.product,
                        reading.product_sha256,
                    )
                elif source.reading_digest != reading.product_sha256:
                    source.reading_path = source.reading_digest = None
                    conflicting_readings.add(source.owner.sha1)
                    source.limitations.append(
                        "bound parts have distinct reading products"
                    )
        source.limitations.extend(part.limitations)
    from yizkr.pipeline.presentation import unread_reason

    for source in result.sources.values():
        item = source.current_input
        if item is not None:
            payload = _payload(directory, item.file, item.representation, data)
            source.readable = (
                any(p.occurrence == "paint" for p in source.placements)
                and payload is not None
                and unread_reason(PurePosixPath(item.file).suffix, payload)
                is None
            )
    return result


def image_source_context(
    transcription,
    supports: Sequence[SourceSupport],
    *,
    reason: str | None = None,
):
    """Locate a verified bound image reading in its original source parts.

    Callers establish owner/input correspondence before supplying support.
    An unlocated correction or a known mismatch carries explicit unsupported
    attribution rather than borrowing a container page's ownership.
    """
    from yizkr.pipeline.source_context import LocatedReading, SourceContext
    from yizkr.pipeline.transcribe import reading_digest

    if not supports or len({item.container.sha1 for item in supports}) != 1:
        raise ValueError("image support needs one original container")
    context = SourceContext(container=supports[0].container)
    digest = reading_digest(transcription)
    reason = reason or (
        "unlocated image reading cannot establish source-part attribution"
        if not transcription.pages
        else None
    )
    spans = (
        [(0, len(transcription.text))]
        if reason
        else [(page.start, page.end) for page in transcription.pages]
    )
    for support in supports:
        located_support = support.model_copy(update={"reading_sha256": digest})
        for start, end in spans:
            context.readings.append(
                LocatedReading(
                    start=start,
                    end=end,
                    owner=None if reason else transcription.artifact,
                    support=located_support,
                    state="unsupported" if reason else "attributed",
                    reason=reason,
                )
            )
    return context


def source_context(transcription, sources: ContainerSources):
    """Locate reading spans without guessing composite authorship."""
    from yizkr.pipeline.source_context import (
        LocatedReading,
        PlacedSource,
        SourceContext,
    )
    from yizkr.pipeline.transcribe import reading_digest

    digest = reading_digest(transcription)
    context = SourceContext(container=sources.inventory.source)
    parts = {part.id: part for part in sources.inventory.parts}
    for source in sources.sources.values():
        for part_id in source.part_ids:
            painted = [
                p.id
                for p in source.placements
                if p.child == part_id and p.occurrence == "paint"
            ]
            if not painted:
                continue
            context.placements.append(
                PlacedSource(
                    owner=source.owner,
                    support=sources.support(part_id, placement_ids=painted),
                )
            )
    bodies = {part.locator.position: part for part in sources.bodies}
    notes = {part.locator.position: part for part in sources.notes}

    def span(part, start, end, *, reason=None):
        support = sources.support(part.id)
        support.reading_sha256 = digest
        context.readings.append(
            LocatedReading(
                start=start,
                end=end,
                owner=None if reason else sources.inventory.source,
                support=support,
                state="unsupported" if reason else "attributed",
                reason=reason,
            )
        )

    for page in transcription.pages or ():
        part = bodies.get(page.page)
        if part is None:
            context.limitations.append(
                f"reading page {page.page} has no source body"
            )
            continue
        inner = [
            p
            for p in sources.inventory.placements
            if p.parent == part.id
            and parts[p.child].kind == "embedded_resource"
            and (
                parts[p.child].owner_binding is not None
                or parts[p.child].source_role in {"independent", "unresolved"}
            )
        ]
        annotations = [
            p
            for p in sources.inventory.placements
            if p.parent == part.id and parts[p.child].kind == "annotation"
        ]
        if page.provenance.method == "vision" and (inner or annotations):
            span(
                part,
                page.start,
                page.end,
                reason="page-wide vision cannot separate inner-source "
                "assertions from the container body",
            )
            continue
        text = transcription.text[page.start : page.end]
        note = notes.get(page.page)
        if note is None or not note.text:
            span(part, page.start, page.end)
            continue
        prefix = (part.text + "\n") if part.text else ""
        marker = f"[Slide {page.page} notes]\n"
        if text != prefix + marker + note.text:
            span(
                part,
                page.start,
                page.end,
                reason="slide body and speaker notes cannot be located "
                "in this reading",
            )
            continue
        span(part, page.start, page.start + len(part.text or ""))
        note_start = page.start + len(prefix) + len(marker)
        span(note, note_start, page.end)
    if not transcription.pages:
        container = next(
            (
                part
                for part in sources.inventory.parts
                if part.kind == "container_source"
            ),
            None,
        )
        if container is not None:
            span(
                container,
                0,
                len(transcription.text),
                reason="unlocated reading cannot establish "
                "source-part attribution",
            )
        else:
            context.limitations.append(
                "unlocated reading cannot establish source-part attribution"
            )
    context.limitations.extend(item.reason for item in sources.unresolved)
    return context


def materialize_images(
    sources: ContainerSources,
    data: Mapping[str, bytes],
    directory: Path,
    reading,
) -> None:
    """Choose independent inputs once, retaining every native occurrence."""
    from yizkr.pipeline.containers import PlacedImage
    from yizkr.pipeline.presentation import DECORATION, unread_reason

    pages = {page.number: page for page in reading.pages}
    prepared = {}
    owner_parts = {}
    noted_parts: set[str] = set()
    for source in sources.sources.values():
        if not any(p.occurrence == "paint" for p in source.placements):
            continue
        owner = source.owner.sha1
        item = source.current_input
        if item is None:
            reading.notes.append(
                f"source {owner[:12]} has no unique reading representation"
            )
            continue
        payload = _payload(directory, item.file, item.representation, data)
        if payload is None:
            reading.notes.append(
                f"source {owner[:12]} has no available reading bytes"
            )
            continue
        suffix = PurePosixPath(item.file).suffix.lower()
        reason = unread_reason(suffix, payload)
        if reason is not None:
            if reason != DECORATION:
                reading.notes.append(
                    f"image not read on its own: {owner[:12]}: {reason}"
                )
                noted_parts.update(source.part_ids)
            continue
        path = directory / f"{owner}{suffix}"
        path.write_bytes(payload)
        image = prepared[owner] = PlacedImage(owner, suffix, path)
        image.source_supports = [
            sources.support(part_id) for part_id in source.part_ids
        ]
        owner_parts.update({part_id: owner for part_id in source.part_ids})
    for placement in sources.inventory.placements:
        owner = owner_parts.get(placement.child)
        if owner is None or placement.occurrence != "paint":
            continue
        page = pages.get(placement.locator.position)
        if page is None:
            continue
        image = reading.images.setdefault(owner, prepared[owner])
        ordinal = len(page.images) + 1
        image.places.append((page.number, ordinal))
        page.images.append((ordinal, owner))
    # Other media (a film, a sound) keep their unsupported outcome in the
    # inventory; they are not images left unread.
    for part in sources.inventory.parts:
        if (
            part.kind == "embedded_resource"
            and _image_part(part)
            and part.id not in noted_parts
            and part.eligibility.state != "excluded"
            and part.reading.state in {"unsupported", "failed"}
            and part.reading.reason
        ):
            reading.notes.append(
                f"image not read on its own: {part.id[:12]}: "
                f"{part.reading.reason}"
            )
    reading.notes.extend(
        f"source {item.part_id[:12]}: {item.reason}"
        for item in sources.unresolved
    )
