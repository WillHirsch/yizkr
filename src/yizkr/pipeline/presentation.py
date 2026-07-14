"""PPTX adapter: compound slide decks -> slides, text, derived media
(ADR-0008).

A .pptx is a documented ZIP of XML parts, so unpacking is fully
deterministic — structure never rides the generative path (ADR-0006
context). This module extracts:

* slide order (from ``ppt/presentation.xml`` + its relationships —
  slide FILE numbers are not authoritative after reordering);
* per-slide text runs and speaker notes, in document order;
* embedded media, mapped to the slide(s) that reference them.

Registration of the derived media follows the deck-asserts-its-own-
composition convention: every statement on a derived item is
referenced ``stated in -> the presentation`` (MODELING.md; ADR-0003).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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
    skipped_media: dict[str, int] = field(default_factory=dict)  # suffix -> count


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    """rId -> resolved target path, for one part's .rels file."""
    rels_path = str(
        PurePosixPath(part).parent / "_rels" / (PurePosixPath(part).name + ".rels")
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
            raise ValueError(f"{path.name} is not a PPTX (no ppt/presentation.xml)")

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
                Slide(number=position, text=_text_runs(archive, part), notes=notes)
            )

        media: list[EmbeddedMedium] = []
        skipped: dict[str, int] = {}
        for target in sorted(media_slides):
            if target not in names:
                continue
            name = PurePosixPath(target).name
            medium = EmbeddedMedium(
                name=name, data=archive.read(target), slides=media_slides[target]
            )
            if medium.is_image:
                media.append(medium)
            else:
                suffix = PurePosixPath(name).suffix.lower() or "(none)"
                skipped[suffix] = skipped.get(suffix, 0) + 1

    return PresentationContent(slides=slides, media=media, skipped_media=skipped)


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


def register_derived_media(
    client,
    deck_qid: str,
    deck_title: str,
    content: PresentationContent,
    workdir: Path,
) -> dict[str, str]:
    """Upload each embedded image and mint its derived artifact item.

    Idempotent: media items are matched by their deck-prefixed label
    (labels are the system-wide idempotency key); wiki file names are
    deck-prefixed for uniqueness. Every statement is referenced
    `stated in -> the presentation` (ADR-0008). Returns
    {media name -> QID}.
    """
    from yizkr.pipeline.registrar import (
        _append,
        _existing_claim_targets,
        _item_claim,
        _string_claim,
    )

    properties, items = client.vocabulary()
    instance_of = properties["instance_of"]
    part_of = properties["part_of"]
    stated_in = properties["stated_in"]
    media_file = properties["media_file"]
    page_number = properties["page_number"]

    from wikibaseintegrator.datatypes import String

    derived: dict[str, str] = {}
    prefix = re.sub(r"[^A-Za-z0-9 _.-]+", "", deck_title).strip() or "presentation"
    workdir.mkdir(parents=True, exist_ok=True)
    for medium in content.media:
        label = f"{deck_title} — {medium.name}"
        existing = client.find_item_by_label(label)
        if existing:
            derived[medium.name] = existing
            continue
        local = workdir / f"{prefix} {medium.name}"
        local.write_bytes(medium.data)
        file_page = client.upload_file(local)
        item = client.wbi.item.new()
        item.labels.set("en", label)
        slide_list = ", ".join(str(number) for number in medium.slides)
        item.descriptions.set(
            "en", f"media embedded in the presentation (slide {slide_list})"
        )
        item = item.write(summary="ingest: derived media from presentation")
        kind = items["photograph"] if medium.is_image else items["artifact"]
        if kind not in _existing_claim_targets(item, instance_of):
            _append(item, _item_claim(instance_of, kind, stated_in, deck_qid))
        _append(item, _string_claim(media_file, file_page, stated_in, deck_qid))
        for slide in medium.slides:
            claim = _item_claim(part_of, deck_qid, stated_in, deck_qid)
            claim.qualifiers.add(String(prop_nr=page_number, value=str(slide)))
            _append(item, claim)
        item.write(summary="ingest: derived media statements")
        derived[medium.name] = item.id
    return derived
