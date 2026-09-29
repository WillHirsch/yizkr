"""PPTX adapter: compound slide decks -> slides, text, derived media
(ADR-0008).

A .pptx is a documented ZIP of XML parts, so unpacking is fully
deterministic — structure never rides the generative path (ADR-0006
context). This module extracts:

* slide order (from ``ppt/presentation.xml`` + its relationships —
  slide FILE numbers are not authoritative after reordering);
* per-slide text runs and speaker notes, in document order;
* embedded media, mapped to the slide(s) that reference them.

Before the write, a deck's derived media are only recorded, in its
document's directory (`record_derived_media`): each distinct image by
the SHA-1 of its bytes, with the deck it is part of and the slides it is
on, and its bytes kept beside the record. Nothing is read from the wiki
or written to it then (ADR-0025 §2).

The record is any container's: a PDF read page by page records the images
placed within its pages in it too (`keep_derived_media`, called by
yizkr.pipeline.orchestrate), each named for its places, "page 6, image
1", with the pages it is on where a deck's image has its slides; and
beside each image's bytes, its own transcription
(`derived_transcription`), which names the image by its SHA-1.

The write registers a container's derived media from that record
(`Registrar.register_derived_media` in yizkr.pipeline.registrar), after
the container itself: every statement a container contributes to a
derived item is referenced ``stated in -> that container`` (MODELING.md;
ADR-0003). Derived items are keyed on content (ADR-0010), so an image
shared by several decks or PDFs is one item that is ``part of`` each of
them. A PDF's image with a transcription of its own is also a document of
its own, whose claims cite the image (yizkr.pipeline.orchestrate,
ExcerptRun; ADR-0008).
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from yizkr.pipeline.stages import staging_path

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}


@dataclass
class Slide:
    number: int  # 1-based position in the presentation order
    text: str
    notes: str = ""


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


def slide_transcript(content: PresentationContent) -> str:
    """Slide-by-slide plain text with locator markers.

    The extraction prompt carries the claim schema's `page_number`
    locator; the `[Slide N]` markers give the model the slide scope so
    every claim's reference points into the right fragment (R2).
    """
    blocks: list[str] = []
    for slide in content.slides:
        blocks.append(f"[Slide {slide.number}]")
        if slide.text:
            blocks.append(slide.text)
        if slide.notes:
            blocks.append(f"[Slide {slide.number} notes]")
            blocks.append(slide.notes)
    return "\n".join(blocks)


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
