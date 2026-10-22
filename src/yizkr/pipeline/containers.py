"""Paged containers: a PDF or a deck read page by page or slide by slide,
each page with its own text and the images placed on it (ADR-0008).

A container holds text of its own and images it places. Both formats
take one path: a reader per format gives the container's pages in order
(`ContainerPages`), each with the method that reads it, its text where
the file holds it, the file the vision engine is to read where it does
not, and the images placed on it that are read on their own; and the
images themselves, each once by the SHA-1 of its bytes (`PlacedImage`).
The transcription (yizkr.pipeline.transcribe.PagedTranscriber) makes the
container's own transcription from them, page by page behind its page
markers, and reads each image by the vision engine on its own, as a
witness of its own: an excerpt (yizkr.pipeline.orchestrate.ExcerptRun).

The readers:

- a PDF's, `yizkr.pipeline.textlayer.read_pages`: a page from its text
  layer where it has a usable one, by vision otherwise, and the images
  placed within its text-layer pages;
- a deck's, `yizkr.pipeline.presentation.read_slides`: each slide's text
  and speaker notes from the PPTX XML (`SLIDE_XML`), with no model call,
  and the images the slide embeds.

**Which images are read on their own** is the same rule for both: an
image under `MIN_IMAGE_SIDE` pixels on a side, or under
`MIN_IMAGE_PIXELS` pixels in all, is decoration, and is not read; nor is
one the never-ingest list names by its SHA-1. A deck's image is read in
the form the deck holds it, so one in a format the vision engine does not
read (`VISION_SUFFIXES`), or whose size its header does not give
(`image_size`), is not read, and the reader notes it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# how a deck's slide is read: from the slide's XML, with no model call
SLIDE_XML = "slide-xml"

# below either, an image is decoration and is not read
MIN_IMAGE_SIDE = 24
MIN_IMAGE_PIXELS = 10_000

# the image formats the vision engine reads, by file suffix
VISION_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


@dataclass
class PlacedImage:
    """An image placed within a page and read on its own: its bytes, kept
    in a file named for their SHA-1, and each place it has, as (page, its
    place among that page's images read on their own, from 1)."""

    sha1: str
    suffix: str
    path: Path
    places: list[tuple[int, int]] = field(default_factory=list)

    @property
    def pages(self) -> list[int]:
        return sorted({page for page, _place in self.places})


@dataclass
class ContainerPage:
    """How one page or slide is read: its method, its own text (a page
    whose text the file holds), the file the vision engine reads (a vision
    page's), the images placed within it that are read on their own, as
    (place, SHA-1) in their order on the page, and how many annotations it
    shows that its text layer leaves unread (a PDF's text-layer page)."""

    number: int
    method: str
    text: str = ""
    vision: Path | None = None
    images: list[tuple[int, str]] = field(default_factory=list)
    annotations: int = 0


@dataclass
class ContainerPages:
    """A container read page by page: its pages in order, the images read
    on their own by SHA-1 in the order they are first placed, and `notes`
    on what could not be read: an image whose bytes could not be given,
    or that the vision engine cannot read."""

    pages: list[ContainerPage]
    images: dict[str, PlacedImage] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def by_own_text(self) -> bool:
        """Does any page read from the text the file holds: a PDF's text
        layer, a deck's slide XML? A PDF none of whose pages does is read
        whole by the vision engine."""
        return any(
            page.method not in ("vision", "none") for page in self.pages
        )


def is_decoration(width: int, height: int) -> bool:
    """Is an image of this size too small to hold legible text
    (MIN_IMAGE_SIDE, MIN_IMAGE_PIXELS)?"""
    return min(width, height) < MIN_IMAGE_SIDE or (
        width * height < MIN_IMAGE_PIXELS
    )


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels of a PNG, JPEG, GIF or WebP image, read
    from its header alone; None for any other bytes, or a header that
    gives no size."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_size(data)
        if data[:2] == b"\xff\xd8":
            return _jpeg_size(data)
    except struct.error:
        return None
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20:21] == b"\x2f":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


# the JPEG start-of-frame markers, which carry the image's size
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        (length,) = struct.unpack(">H", data[at + 2 : at + 4])
        if marker in _SOF:
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height
        at += 2 + length
    return None
