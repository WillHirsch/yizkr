"""PPTX adapter: compound slide decks -> slides, text, derived media
(ADR-0008).

A .pptx is a documented ZIP of XML parts, so unpacking is fully
deterministic — structure never rides the generative path (ADR-0006
context). This module extracts:

* slide order (from ``ppt/presentation.xml`` + its relationships —
  slide FILE numbers are not authoritative after reordering);
* per-slide text runs and speaker notes, in document order;
* embedded media, mapped to the slide(s) that reference them.

**A deck is a paged container** (yizkr.pipeline.containers):
`read_slides` gives its slides as pages, each read from its XML, with its
text (`slide_text`) and the images it embeds that are read on their own,
in the order the slide paints them. Its transcription marks each such
image below the slide's marker by its SHA-1 (``[Slide 2, image 1:
sha1:…]``), reads it by the vision engine on its own and keeps that
reading beside its bytes, so the image is an excerpt, a document of its
own, as an image a PDF places is (yizkr.pipeline.transcribe.
DeckTranscriber; ADR-0008).

Before the write, a deck's derived media are only recorded, in its
document's directory (`record_derived_media`): each distinct image by
the SHA-1 of its bytes, with the deck it is part of and the slides it is
on, and its bytes kept beside the record. Nothing is read from the wiki
or written to it then (ADR-0025 §2).

The record is any container's: a PDF read page by page records the images
placed within its pages in it too (`keep_derived_media`, called by
yizkr.pipeline.orchestrate), each named for its places, "page 6, image
1", with the pages it is on where a deck's image has its slides; and
beside each image's bytes, a PDF's or a deck's, its own transcription
(`derived_transcription`), which names the image by its SHA-1.

The write registers a container's derived media from that record
(`Registrar.register_derived_media` in yizkr.pipeline.registrar), after
the container itself: every statement a container contributes to a
derived item is referenced ``stated in -> that container`` (MODELING.md;
ADR-0003). Derived items are keyed on content (ADR-0010), so an image
shared by several decks or PDFs is one item that is ``part of`` each of
them. A PDF's or a deck's image with a transcription of its own is also a
document of its own, whose claims cite the image
(yizkr.pipeline.orchestrate, ExcerptRun; ADR-0008).
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.pipeline.containers import (
    SLIDE_XML,
    VISION_SUFFIXES,
    ContainerPage,
    ContainerPages,
    PlacedImage,
    image_size,
    is_decoration,
)
from yizkr.pipeline.stages import staging_path

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}
# why an image is not read on its own when it is too small to hold
# legible text (unread_reason)
DECORATION = "decoration"


@dataclass
class Slide:
    number: int  # 1-based position in the presentation order
    text: str
    notes: str = ""
    # the archive paths of the media it uses, each once, in the order the
    # slide's XML refers to them (its pictures in paint order), then any
    # its relationships name that its XML does not refer to
    media: list[str] = field(default_factory=list)


@dataclass
class EmbeddedMedium:
    name: str  # basename inside the archive, e.g. image1.jpeg
    data: bytes
    slides: list[int] = field(default_factory=list)

    @property
    def is_image(self) -> bool:
        return PurePosixPath(self.name).suffix.lower() in IMAGE_SUFFIXES


@dataclass
class PresentationContent:
    slides: list[Slide]
    media: list[EmbeddedMedium]
    skipped_media: dict[str, int] = field(
        default_factory=dict
    )  # suffix -> count


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    """rId -> resolved target path, for one part's .rels file."""
    rels_path = str(
        PurePosixPath(part).parent
        / "_rels"
        / (PurePosixPath(part).name + ".rels")
    )
    if rels_path not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rels_path))
    base = PurePosixPath(part).parent
    targets = {}
    for rel in root.iter(f"{REL_NS}Relationship"):
        target = rel.get("Target", "")
        if rel.get("TargetMode") == "External":
            continue
        resolved = (base / target).as_posix()
        # normalize ../ components
        parts: list[str] = []
        for piece in resolved.split("/"):
            if piece == "..":
                if parts:
                    parts.pop()
            elif piece not in (".", ""):
                parts.append(piece)
        targets[rel.get("Id", "")] = "/".join(parts)
    return targets


def _text_runs(archive: zipfile.ZipFile, part: str) -> str:
    """Every a:t run in document order; paragraphs joined by newlines."""
    root = ET.fromstring(archive.read(part))
    lines: list[str] = []
    for paragraph in root.iter(f"{A_NS}p"):
        line = "".join(t.text or "" for t in paragraph.iter(f"{A_NS}t"))
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def _slide_media(
    archive: zipfile.ZipFile, part: str, rels: dict[str, str]
) -> list[str]:
    """The archive paths of the media a slide uses, each once: those its
    XML refers to (`r:embed`, `r:link` or `r:id` on any element) in
    document order, which is the order the slide paints them in, then
    those only its relationships name, in their order there."""
    root = ET.fromstring(archive.read(part))
    order: list[str] = []
    for element in root.iter():
        for attribute in ("embed", "link", "id"):
            target = rels.get(element.get(f"{R_NS}{attribute}") or "")
            if target and target.startswith("ppt/media/"):
                order.append(target)
    order += [t for t in rels.values() if t.startswith("ppt/media/")]
    return list(dict.fromkeys(order))


def unpack_pptx(path: Path) -> PresentationContent:
    """Deterministically unpack a .pptx into slides + embedded media."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "ppt/presentation.xml" not in names:
            raise ValueError(
                f"{path.name} is not a PPTX (no ppt/presentation.xml)"
            )

        # authoritative slide order: sldIdLst r:ids -> relationship targets
        presentation_rels = _relationships(archive, "ppt/presentation.xml")
        root = ET.fromstring(archive.read("ppt/presentation.xml"))
        slide_parts = [
            presentation_rels[sld.get(f"{R_NS}id")]
            for sld in root.iter(f"{P_NS}sldId")
            if sld.get(f"{R_NS}id") in presentation_rels
        ]
        if not slide_parts:  # tolerate decks with no sldIdLst (rare)
            slide_parts = sorted(
                name
                for name in names
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )

        slides: list[Slide] = []
        media_slides: dict[str, list[int]] = {}
        for position, part in enumerate(slide_parts, start=1):
            rels = _relationships(archive, part)
            notes = ""
            for target in rels.values():
                if "notesSlides/" in target and target in names:
                    notes = _text_runs(archive, target)
                if target.startswith("ppt/media/"):
                    media_slides.setdefault(target, [])
                    if position not in media_slides[target]:
                        media_slides[target].append(position)
            slides.append(
                Slide(
                    number=position,
                    text=_text_runs(archive, part),
                    notes=notes,
                    media=_slide_media(archive, part, rels),
                )
            )

        media: list[EmbeddedMedium] = []
        skipped: dict[str, int] = {}
        for target in sorted(media_slides):
            if target not in names:
                continue
            name = PurePosixPath(target).name
            medium = EmbeddedMedium(
                name=name,
                data=archive.read(target),
                slides=media_slides[target],
            )
            if medium.is_image:
                media.append(medium)
            else:
                suffix = PurePosixPath(name).suffix.lower() or "(none)"
                skipped[suffix] = skipped.get(suffix, 0) + 1

    return PresentationContent(
        slides=slides, media=media, skipped_media=skipped
    )


def slide_text(slide: Slide) -> str:
    """A slide's own text, as its page of the deck's transcription holds
    it below the slide's `[Slide N]` marker: its text runs, then its
    speaker notes behind a `[Slide N notes]` marker. The markers give the
    extraction the slide scope, so every claim's `page_number` locator
    points into the right slide (R2)."""
    lines = [slide.text] if slide.text else []
    if slide.notes:
        lines += [f"[Slide {slide.number} notes]", slide.notes]
    return "\n".join(lines)


def slide_transcript(content: PresentationContent) -> str:
    """The deck's text slide by slide, each slide behind its `[Slide N]`
    marker (`slide_text`): its transcription's text when it reads no
    image on its own, which marks each image it reads below the marker of
    the slide that embeds it (yizkr.pipeline.transcribe.DeckTranscriber)."""
    blocks = []
    for slide in content.slides:
        body = slide_text(slide)
        blocks.append(
            f"[Slide {slide.number}]" + (f"\n{body}" if body else "")
        )
    return "\n".join(blocks)


def unread_reason(suffix: str, data: bytes) -> str | None:
    """Why a deck's image is not read on its own, or None when it is
    (yizkr.pipeline.containers): `DECORATION` for an image too small to
    hold legible text; otherwise a note, for an image in a format the
    vision engine does not read, or whose size its header does not give.
    `suffix` is the one its file in the record takes."""
    if suffix.lower() not in VISION_SUFFIXES:
        return (
            f"a {suffix.lower() or 'suffix-less'} image, a format the "
            "vision engine does not read"
        )
    size = image_size(data)
    if size is None:
        return "an image whose size its header does not give"
    if is_decoration(*size):
        return DECORATION
    return None


def read_slides(
    path: Path,
    directory: Path,
    *,
    excluded: Callable[[str], bool] = lambda sha1: False,
) -> ContainerPages:
    """Read the deck at `path` slide by slide, as a paged container
    (yizkr.pipeline.containers): each slide a page read from its XML
    (`SLIDE_XML`), its text `slide_text`, with the images it embeds that
    are read on their own, in the order it paints them, each written into
    `directory` once, as ``<sha1><suffix>``. Each image is known by the
    SHA-1 of its bytes and takes the suffix its file in the record of the
    deck's derived media takes (`record_derived_media`). An image the
    never-ingest list names (`excluded`), or that `unread_reason` gives a
    reason for, is not read; the reader notes each of the latter but
    decoration, once."""
    return slide_pages(unpack_pptx(path), directory, excluded=excluded)


def slide_pages(
    content: PresentationContent,
    directory: Path,
    *,
    excluded: Callable[[str], bool] = lambda sha1: False,
) -> ContainerPages:
    """`read_slides` of a deck already unpacked."""
    media: dict[str, tuple[str, bytes]] = {}
    names: dict[str, list[str]] = {}
    for medium in content.media:
        sha1 = hashlib.sha1(medium.data).hexdigest()
        names.setdefault(sha1, []).append(medium.name)
        media[medium.name] = (sha1, medium.data)
    suffix = {
        sha1: PurePosixPath(min(found)).suffix.lower()
        for sha1, found in names.items()
    }
    read = ContainerPages([])
    noted: set[str] = set()
    for slide in content.slides:
        page = ContainerPage(slide.number, SLIDE_XML, slide_text(slide))
        read.pages.append(page)
        for target in slide.media:
            name = PurePosixPath(target).name
            if name not in media:
                continue
            sha1, data = media[name]
            if excluded(sha1):
                continue
            reason = unread_reason(suffix[sha1], data)
            if reason is not None:
                if reason != DECORATION and sha1 not in noted:
                    noted.add(sha1)
                    read.notes.append(
                        f"slide {slide.number}, {name} (sha1 {sha1[:12]}): "
                        f"{reason}, so it is not read"
                    )
                continue
            if any(known == sha1 for _place, known in page.images):
                continue
            image = read.images.get(sha1)
            if image is None:
                path = directory / f"{sha1}{suffix[sha1]}"
                path.write_bytes(data)
                image = read.images[sha1] = PlacedImage(
                    sha1, suffix[sha1], path
                )
            place = len(page.images) + 1
            image.places.append((slide.number, place))
            page.images.append((place, sha1))
    return read


def read_on_their_own(
    record: DerivedMediaRecord,
    directory: Path,
    *,
    excluded: Callable[[str], bool] = lambda sha1: False,
) -> set[str]:
    """The SHA-1s of the images of a deck's record kept in `directory`
    (`read_derived_media`) that its transcription reads on their own, as
    `read_slides` chooses them: each the never-ingest list does not name
    and `unread_reason` gives no reason for."""
    return {
        sha1
        for sha1, medium in record.media.items()
        if not excluded(sha1)
        and unread_reason(
            PurePosixPath(medium.file).suffix,
            (directory / medium.file).read_bytes(),
        )
        is None
    }


# The record of a container's derived media in its document's directory,
# and the folder beside it that keeps their bytes and, for a PDF's images,
# their transcriptions.
DERIVED_MEDIA = "derived-media.json"
DERIVED_MEDIA_DIR = "derived-media"
DERIVED_TRANSCRIPTION = ".transcription.json"

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
# a medium's file: its SHA-1 and its suffix, in DERIVED_MEDIA_DIR
_MEDIUM_FILE = (
    rf"^{re.escape(DERIVED_MEDIA_DIR)}/[0-9a-f]{{40}}(\.[a-z0-9]+)?$"
)


class DerivedMedium(BaseModel):
    """One distinct medium a container embeds: its bytes, wherever the
    container uses them."""

    model_config = ConfigDict(extra="forbid")

    # its names: inside a deck's archive (image1.jpeg), or its places in a
    # PDF ("page 6, image 1"); the same bytes under several names are one
    # medium
    names: list[str]
    # every slide (of a deck) or page (of a PDF) it is on, by its 1-based
    # position: the page number of its `part of` the container (ADR-0008)
    slides: list[int]
    # the file that keeps its bytes, relative to the record's directory:
    # always in DERIVED_MEDIA_DIR, so a record never names a file outside
    # it for the write to upload
    file: str = Field(pattern=_MEDIUM_FILE)


class DerivedMediaRecord(BaseModel):
    """A container's derived media, recorded before the write for
    registration at write time: each distinct medium by the SHA-1 of its
    bytes (ADR-0010), part of the container (a deck, or a PDF read page by
    page) that the SHA-1 of the container's own bytes names. Nothing in it
    depends on an instance."""

    model_config = ConfigDict(extra="forbid")

    # the container, the artifact the media are part of
    source_sha1: Sha1
    # what kind of container it is: a deck, whose media are on slides, or
    # a PDF, whose images are placed within its pages
    container: Literal["presentation", "pdf"] = "presentation"
    # the container's title, which each medium's label begins with:
    # "<title> — <media name>" (ADR-0008)
    title: str
    # media SHA-1 -> the medium
    media: dict[Sha1, DerivedMedium] = {}


def record_derived_media(
    content: PresentationContent,
    *,
    source_sha1: str,
    title: str,
    directory: Path,
) -> DerivedMediaRecord:
    """Record the media a deck embeds, reading nothing from the wiki and
    writing nothing to it (ADR-0025 §2).

    It records what `content.media` holds: `unpack_pptx` puts the images
    there, and counts the other media in `content.skipped_media`. Each
    distinct medium is keyed by the SHA-1 of its bytes, with every name
    it has in the archive and every slide it is on. Its bytes are kept as
    ``<sha1><suffix>`` in `DERIVED_MEDIA_DIR` under `directory`, and the
    record as `DERIVED_MEDIA` there. Each file is written in one step, so
    a reader sees it whole or not at all, and one that already holds what
    it would be written with is left untouched. Returns the record."""
    names: dict[str, set[str]] = {}
    slides: dict[str, set[int]] = {}
    data: dict[str, bytes] = {}
    for medium in content.media:
        sha1 = hashlib.sha1(medium.data).hexdigest()
        names.setdefault(sha1, set()).add(medium.name)
        slides.setdefault(sha1, set()).update(medium.slides)
        data.setdefault(sha1, medium.data)
    record = DerivedMediaRecord(
        source_sha1=source_sha1,
        title=title,
        media={
            sha1: DerivedMedium(
                names=sorted(names[sha1]),
                slides=sorted(slides[sha1]),
                file=f"{DERIVED_MEDIA_DIR}/{sha1}"
                + PurePosixPath(min(names[sha1])).suffix.lower(),
            )
            for sha1 in sorted(data)
        },
    )
    keep_derived_media(record, data, directory)
    return record


def keep_derived_media(
    record: DerivedMediaRecord,
    data: dict[str, bytes | Path],
    directory: Path,
) -> None:
    """Keep a container's derived media in `directory`: each medium's
    bytes (`data`, by its SHA-1: the bytes, or a file that holds them) in
    the file the record names, then the record as `DERIVED_MEDIA`. The
    bytes go first, so a record never names a missing file. Each file is
    written in one step, and one that holds what it would be written with
    already is left untouched (`read_derived_media` reads them back)."""
    for sha1, medium in record.media.items():
        held = data[sha1]
        _keep(
            directory / medium.file,
            held.read_bytes() if isinstance(held, Path) else held,
        )
    _keep(
        directory / DERIVED_MEDIA,
        record.model_dump_json(indent=2).encode("utf-8"),
    )


def derived_transcription(directory: Path, sha1: str) -> Path:
    """Where the transcription of a PDF's image is kept, beside its bytes:
    ``derived-media/<sha1>.transcription.json``."""
    return directory / DERIVED_MEDIA_DIR / f"{sha1}{DERIVED_TRANSCRIPTION}"


def read_derived_media(directory: Path) -> DerivedMediaRecord | None:
    """The record of a container's derived media kept in `directory`
    (keep_derived_media), when it is there whole and so is the file of
    every medium it names: a regular file, not a symlink, named for the
    medium's SHA-1 and holding the bytes that SHA-1 names. None
    otherwise, so a record whose files were lost, damaged, swapped or
    replaced by a link is made again."""
    try:
        record = DerivedMediaRecord.model_validate_json(
            (directory / DERIVED_MEDIA).read_bytes()
        )
    except (OSError, ValueError):
        return None
    for key, medium in record.media.items():
        data = _read_regular(directory / medium.file)
        if (
            PurePosixPath(medium.file).stem != key
            or data is None
            or hashlib.sha1(data).hexdigest() != key
        ):
            return None
    return record


def _read_regular(path: Path) -> bytes | None:
    """The bytes of the regular file at `path`, never read through a
    symlink; None when there is no such file or it cannot be read."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _keep(path: Path, data: bytes) -> None:
    """Keep `data` at `path`, written in one step, unless the regular file
    there holds those bytes already. A symlink there is replaced, never
    written through."""
    if _read_regular(path) == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(path)
    try:
        with open(staging, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
