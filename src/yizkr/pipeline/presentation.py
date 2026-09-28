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

Registration of the derived media (`register_derived_media`) follows the
deck-asserts-its-own-composition convention: every statement a deck
contributes to a derived item is referenced ``stated in -> that
presentation`` (MODELING.md; ADR-0003). Derived items are keyed on
content (ADR-0010), so an image shared by several decks is one item that
is ``part of`` each of them.
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated

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


# The record of a deck's derived media in its document's directory, and
# the folder beside it that keeps their bytes.
DERIVED_MEDIA = "derived-media.json"
DERIVED_MEDIA_DIR = "derived-media"

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
# a medium's file: its SHA-1 and its suffix, in DERIVED_MEDIA_DIR
_MEDIUM_FILE = (
    rf"^{re.escape(DERIVED_MEDIA_DIR)}/[0-9a-f]{{40}}(\.[a-z0-9]+)?$"
)


class DerivedMedium(BaseModel):
    """One distinct medium a deck embeds: its bytes, wherever the deck
    uses them."""

    model_config = ConfigDict(extra="forbid")

    # its names inside the deck's archive (image1.jpeg): the same bytes
    # under several names are one medium
    names: list[str]
    # every slide it is on, by its 1-based position in presentation
    # order: the page number of its `part of` the deck (ADR-0008)
    slides: list[int]
    # the file that keeps its bytes, relative to the record's directory:
    # always in DERIVED_MEDIA_DIR, so a record never names a file outside
    # it for the write to upload
    file: str = Field(pattern=_MEDIUM_FILE)


class DerivedMediaRecord(BaseModel):
    """A deck's derived media, recorded before the write for registration
    at write time: each distinct medium by the SHA-1 of its bytes
    (ADR-0010), part of the deck that the SHA-1 of the deck's own bytes
    names. Nothing in it depends on an instance."""

    model_config = ConfigDict(extra="forbid")

    # the deck, the artifact the media are part of
    source_sha1: Sha1
    # the deck's title, which each medium's label begins with:
    # "<deck title> — <media name>" (ADR-0008)
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
    # the bytes before the record, so a record never names a missing file
    for sha1, medium in record.media.items():
        _keep(directory / medium.file, data[sha1])
    _keep(
        directory / DERIVED_MEDIA,
        record.model_dump_json(indent=2).encode("utf-8"),
    )
    return record


def read_derived_media(directory: Path) -> DerivedMediaRecord | None:
    """The record of a deck's derived media kept in `directory`
    (record_derived_media), when it is there whole and every file of
    bytes it names is too; None otherwise."""
    try:
        record = DerivedMediaRecord.model_validate_json(
            (directory / DERIVED_MEDIA).read_bytes()
        )
    except (OSError, ValueError):
        return None
    if not all((directory / m.file).is_file() for m in record.media.values()):
        return None
    return record


def _keep(path: Path, data: bytes) -> None:
    """Keep `data` at `path`, written in one step, unless the file there
    holds those bytes already."""
    if path.is_file() and path.read_bytes() == data:
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


def register_derived_media(
    client,
    deck_qid: str,
    deck_title: str,
    content: PresentationContent,
    workdir: Path,
) -> dict[str, str]:
    """Ensure each embedded image has its derived artifact item and file.

    Keyed on content (ADR-0010): a medium is matched by the SHA-1 of its
    bytes first, so the same image embedded in several decks (or also
    ingested on its own) is ONE artifact item, and its bytes are uploaded
    only when none of the item's wiki files holds them yet. An item
    without a hash is matched by its deck-prefixed label, and adopted
    and backfilled when its wiki file holds the same bytes; a
    same-label item with DIFFERENT bytes is different content, so the new
    item gets a content-tagged label. The upload never overwrites a wiki
    file (`WikiClient.upload_file`): bytes whose file name already holds
    other bytes go under a content-tagged file name, so every `media
    file` holds the bytes its item's `media SHA-1` names.

    Each deck's assertions are then ensured additively (ADR-0008): one
    `part of -> deck` per slide the medium is on, slide-qualified, plus
    `instance of`, `media file` and `media SHA-1`, each referenced
    `stated in -> this deck`. A statement that another deck (or the
    artifact itself) already made gains this deck's reference instead
    of a duplicate; nothing existing is removed or rewritten (CLAUDE.md
    rule 3). Idempotent on re-run. Returns {media name -> QID}.
    """
    from wikibaseintegrator.datatypes import String

    from yizkr.pipeline.registrar import (
        _ensure_referenced,
        _item_claim,
        _locate_by_content,
        _string_claim,
        _takes_alias,
    )

    derived: dict[str, str] = {}
    if not content.media:
        return derived
    properties, items = client.vocabulary()
    instance_of = properties["instance_of"]
    part_of = properties["part_of"]
    stated_in = properties["stated_in"]
    media_file = properties["media_file"]
    media_sha1 = properties["media_sha1"]
    page_number = properties["page_number"]

    # one lag-free enumeration per deck, kept current as items are minted
    by_hash = dict(client.string_statement_index(media_sha1))
    prefix = (
        re.sub(r"[^A-Za-z0-9 _.-]+", "", deck_title).strip() or "presentation"
    )
    workdir.mkdir(parents=True, exist_ok=True)
    for medium in content.media:
        content_sha1 = hashlib.sha1(medium.data).hexdigest()
        label = f"{deck_title} — {medium.name}"
        item, held, label_taken = _locate_by_content(
            client,
            by_hash.get(content_sha1),
            label,
            content_sha1,
            media_file=media_file,
            media_sha1=media_sha1,
        )
        if label_taken:
            # same deck title and media name, different bytes (a deck
            # re-exported with a changed image): a distinct artifact, so
            # its label must differ
            label = f"{label} [{content_sha1[:8]}]"

        # the bytes are uploaded only if no file of the item holds them
        # already, so a medium reused by another deck adds no second blob
        if not held:
            local = workdir / f"{prefix} {medium.name}"
            local.write_bytes(medium.data)
            held = [client.upload_file(local)]

        if item is None:
            item = client.wbi.item.new()
            item.labels.set("en", label)
            slide_list = ", ".join(str(number) for number in medium.slides)
            item.descriptions.set(
                "en",
                f"media embedded in the presentation (slide {slide_list})",
            )
            changed = True
        else:
            # another deck's name for the same bytes becomes an alias, as
            # the registrar keeps an alternate filename, unless the name
            # belongs to other content
            changed = _takes_alias(client, item, label, content_sha1)
            if changed:
                item.aliases.set("en", label)

        kind = items["photograph"] if medium.is_image else items["artifact"]
        statements = [
            _item_claim(instance_of, kind, stated_in, deck_qid),
            *(
                _string_claim(media_file, page, stated_in, deck_qid)
                for page in held
            ),
            _string_claim(media_sha1, content_sha1, stated_in, deck_qid),
        ]
        for slide in medium.slides:
            claim = _item_claim(part_of, deck_qid, stated_in, deck_qid)
            claim.qualifiers.add(String(prop_nr=page_number, value=str(slide)))
            statements.append(claim)
        for claim in statements:
            outcome = _ensure_referenced(item, claim, stated_in, deck_qid)
            changed = changed or outcome != "present"
        if changed:
            item = item.write(
                summary="ingest: derived media from presentation"
            )
        by_hash[content_sha1] = item.id
        derived[medium.name] = item.id
    return derived
