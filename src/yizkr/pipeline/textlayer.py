"""A PDF read page by page: each page's text layer, the images placed
within it, and what the vision engine is given of a page it reads.

A born-digital PDF carries its text in its pages: a text layer read from
the file, deterministically and without a model call, is the page's text
exactly as it was typed. A scanned page carries only a picture of its
text, and the vision engine reads it. `read_pages` tells the two apart
page by page, and the transcription reads a PDF page by page whenever one
of its pages reads from its text layer
(yizkr.pipeline.transcribe.PdfPageTranscriber). A PDF none of whose pages
does is read whole by the vision engine, with the call it has always been
read with, so its transcription, and every answer the LLM cache or the
transcription seed holds for it, stays as it was (ADR-0032).

**The text layer** of a page is the text its content draws visibly, in
the order PDFium extracts it. Text drawn in an invisible render mode, as
an OCR layer over a scan is, text that falls outside the page, and text
that an image painted after it covers, is not part of it: none of it can
be seen on the page, and an OCR layer is a machine's reading, not the
page's text. A text layer is **usable** when it holds a letter or a digit
and at most `MAX_UNREADABLE_SHARE` of its characters, whitespace aside,
are ones no text is written in: the replacement character, private-use
and unassigned code points, lone surrogates and control characters, which
is what a font without a usable Unicode map extracts as.

**How each page is read** (its method, which the transcription records):

- ``blank``: the page draws nothing and has no annotation, so there is
  nothing to read and no call is made;
- ``vision``, for a scanned page: images cover at least `SCAN_COVER` of
  it together, and it draws fewer than `MIN_TEXT_OVER_IMAGES` letters and
  digits visibly (a stamp, a page number or a header over a scan);
- ``text-layer``: its text layer is usable;
- ``vision`` otherwise: its text layer is empty or garbage, so what it
  draws can only be read from a picture of it.

**Images placed within a page** that reads from its text layer are read
on their own, each as a witness of its own (ADR-0008): an original that a
compilation embeds beside its translation is its writer's assertion, not
the compiler's. An image is placed within a page when it covers less than
`BACKGROUND_COVER` of the page on its own; one that covers more is the
page's background, or the page itself. An image under `MIN_IMAGE_SIDE`
pixels on a side, or under `MIN_IMAGE_PIXELS` pixels in all, is taken for
decoration: it could hold no more than a word of legible text, and it is
not read. Each image is known by the SHA-1 of its bytes (ADR-0010), so one
placed on several pages, or twice on one, is one image, read once. A page
read by vision is read whole, whatever images it holds, as the
compilation's own page (ADR-0008, the third question its compilation
amendment leaves open): nothing is derived from it.

**The bytes of an image** are the image as the PDF holds it where that is
a whole JPEG file (a DCTDecode stream, after any lossless filters). Any
other image is decoded by PDFium and written out as PNG by `png`, whose
output depends only on the pixels and on zlib's deflate at a fixed level,
so every run finds the same SHA-1 for the same image; a test holds the
encoding byte for byte. **What the vision engine reads of a page** is the
page's one image, as held, where the page draws nothing but one unrotated
image that covers it, as a scanned page does; otherwise a rendering of the
whole page, `RENDER_LONG_EDGE` pixels on its long edge, written as PNG.
Both depend only on the PDF and on the PDFium build that uv.lock pins, so
the calls that read them keep their keys in the LLM cache from run to run.

**Text a page holds only in annotations**, such as a filled form field,
is not part of its text layer; a page that draws nothing else is read by
vision, whose rendering shows its annotations.

**The library** is pypdfium2 (Apache-2.0 or BSD-3-Clause), the Python
binding of PDFium (BSD-3-Clause), Chromium's PDF engine, which its wheels
bundle: it reads the file offline, in this process, and nothing leaves
the machine. PDFium is not thread-safe, so every call into it is made
under one lock, and every page and bitmap it hands out is closed before
the lock is released.
"""

from __future__ import annotations

import ctypes
import hashlib
import struct
import threading
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

# how a page is read
TEXT_LAYER = "text-layer"
VISION = "vision"
BLANK = "blank"
# the text-layer method, as a transcription's provenance names it: VERSION
# goes up whenever what the method reads off a page changes
METHOD = "pdf-text-layer"
VERSION = 1
LIBRARY = f"pypdfium2 {pdfium.PYPDFIUM_INFO}, pdfium {pdfium.PDFIUM_INFO}"

# the share of a page that images must cover, together, for the page to be
# read as a scan
SCAN_COVER = 0.6
# the share of a page one image must cover to be its background (or the
# page itself) rather than an image placed within it
BACKGROUND_COVER = 0.9
# the fewest letters and digits a page covered by images must draw visibly
# to be read from its text layer
MIN_TEXT_OVER_IMAGES = 200
# the largest share of a usable text layer's characters, whitespace aside,
# that may be unreadable
MAX_UNREADABLE_SHARE = 0.1
# below either, an image is decoration and is not read
MIN_IMAGE_SIDE = 24
MIN_IMAGE_PIXELS = 10_000
# the long edge, in pixels, of a page rendered for the vision engine
RENDER_LONG_EDGE = 1600
# zlib's level for PNG: fixed, since an image's bytes are its identity
PNG_LEVEL = 9

JPEG_SUFFIX = ".jpg"
PNG_SUFFIX = ".png"

# every call into PDFium is made under this lock
_LOCK = threading.Lock()

_INVISIBLE_MODES = frozenset(
    {pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE, pdfium_c.FPDF_TEXTRENDERMODE_CLIP}
)
_UNREADABLE_CATEGORIES = frozenset({"Co", "Cn", "Cs", "Cc"})
# bytes per pixel of the PDFium bitmaps an image or a page comes as
_BITMAP_BYTES = {
    pdfium_c.FPDFBitmap_Gray: 1,
    pdfium_c.FPDFBitmap_BGR: 3,
    pdfium_c.FPDFBitmap_BGRx: 4,
    pdfium_c.FPDFBitmap_BGRA: 4,
}

Rect = tuple[float, float, float, float]  # left, bottom, right, top


def unreadable(ch: str) -> bool:
    """A character no text is written in: the replacement character, a
    private-use or unassigned code point, a lone surrogate, or a control
    character other than whitespace."""
    return ch == "\ufffd" or (
        unicodedata.category(ch) in _UNREADABLE_CATEGORIES and not ch.isspace()
    )


def usable(text: str) -> bool:
    """Whether a page's text layer is text to transcribe: it holds a
    letter or a digit, and at most MAX_UNREADABLE_SHARE of its characters,
    whitespace aside, are unreadable."""
    chars = [ch for ch in text if not ch.isspace()]
    if not any(ch.isalnum() for ch in chars):
        return False
    return sum(map(unreadable, chars)) <= MAX_UNREADABLE_SHARE * len(chars)


def clean(text: str) -> str:
    """A text layer as a transcription holds it: its line breaks as
    newlines, control characters other than tabs dropped, trailing spaces
    trimmed, at most one blank line in a row, and none at either end."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )
    kept: list[str] = []
    for line in (line.rstrip() for line in text.split("\n")):
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip("\n")


def png(width: int, height: int, channels: int, pixels: bytes) -> bytes:
    """A PNG of `pixels`: rows from the top, each `width` pixels of
    `channels` bytes (1 grey, 3 RGB, 4 RGBA), eight bits each. The same
    pixels always give the same bytes: no row filter, no metadata, zlib at
    PNG_LEVEL."""
    kind = {1: 0, 3: 2, 4: 6}[channels]
    row = width * channels
    if width < 1 or height < 1 or len(pixels) != row * height:
        raise ValueError("the pixels do not fill the image")
    raw = b"".join(
        b"\x00" + pixels[top : top + row] for top in range(0, len(pixels), row)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, kind, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, PNG_LEVEL))
        + chunk(b"IEND", b"")
    )


def bitmap_png(bitmap: Any) -> bytes:
    """A PDFium bitmap (grey, BGR, BGRx or BGRA) as PNG: grey stays grey,
    and the rest become RGB, or RGBA where the bitmap has alpha."""
    source = _BITMAP_BYTES.get(bitmap.format)
    if source is None:
        raise ValueError(
            f"no PNG is written of a bitmap of format {bitmap.format}"
        )
    width, height, stride = bitmap.width, bitmap.height, bitmap.stride
    buffer = bytes(bitmap.buffer)
    alpha = bitmap.format == pdfium_c.FPDFBitmap_BGRA
    channels = 1 if source == 1 else 4 if alpha else 3
    pixels = bytearray()
    for y in range(height):
        row = buffer[y * stride : y * stride + width * source]
        if source == 1:
            pixels += row
            continue
        out = bytearray(width * channels)
        out[0::channels] = row[2::source]
        out[1::channels] = row[1::source]
        out[2::channels] = row[0::source]
        if alpha:
            out[3::channels] = row[3::source]
        pixels += out
    return png(width, height, channels, bytes(pixels))


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
class PdfPage:
    """How one page is read: its method, its text layer (a text-layer
    page's), the file the vision engine reads (a vision page's), and the
    images placed within it that are read on their own, as (place, SHA-1)
    in their order on the page."""

    number: int
    method: str
    text: str = ""
    vision: Path | None = None
    images: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class PdfPages:
    """A PDF read page by page (`read_pages`): its pages in order, the
    images read on their own by SHA-1 in the order they are first placed,
    and `notes` on what could not be read: an image whose bytes PDFium
    could not give."""

    pages: list[PdfPage]
    images: dict[str, PlacedImage] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def by_text_layer(self) -> bool:
        """Does any page read from its text layer? A PDF none of whose
        pages does is read whole by the vision engine."""
        return any(page.method == TEXT_LAYER for page in self.pages)


def read_pages(
    path: Path,
    directory: Path,
    *,
    excluded: Callable[[str], bool] = lambda sha1: False,
) -> PdfPages | None:
    """Read the PDF at `path` page by page (the module docstring says
    how): each page's method and text layer and, when any page reads from
    its text layer, the files the vision engine is to read, written into
    `directory`: each vision page's (``page-<n><suffix>``) and each image
    read on its own (``<sha1><suffix>``). An image whose SHA-1 the
    never-ingest list names (`excluded`) is neither written nor listed.
    When no page reads from its text layer, nothing is written, and the
    caller reads the PDF whole. None when PDFium cannot open or read the
    file (one that is no PDF, or one locked by a password): the caller
    reads it whole."""
    with _LOCK:
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError:
            return None
        try:
            return _read(document, directory, excluded)
        except pdfium.PdfiumError:
            return None
        finally:
            document.close()


@dataclass
class _Layout:
    """What one page holds, as `_layout` reads it: its method and text
    layer, the images read on their own (their indexes among the page's
    objects, in paint order), and the index of the page's one image when
    it draws nothing else and covers the page."""

    number: int
    method: str
    text: str
    images: list[int]
    lone_image: int | None


def _read(
    document: Any, directory: Path, excluded: Callable[[str], bool]
) -> PdfPages:
    """`read_pages` of an open document: every page's layout first, and
    only then, when a page reads from its text layer, the files."""
    layouts = []
    for index in range(len(document)):
        page = document[index]
        try:
            layouts.append(_layout(page, index + 1))
        finally:
            page.close()
    read = PdfPages(
        [
            PdfPage(layout.number, layout.method, layout.text)
            for layout in layouts
        ]
    )
    if not read.by_text_layer:
        return read
    for layout, page in zip(layouts, read.pages, strict=True):
        if page.method == VISION:
            page.vision = _write_vision(document, layout, directory)
        elif page.method == TEXT_LAYER:
            _write_images(document, layout, page, read, directory, excluded)
    return read


def _objects(page: Any) -> list[tuple[Any, Rect]]:
    """Every object on the page, in the order it is painted, with its
    bounds on the page: an object inside a form XObject is carried to the
    page by the matrix of each form it is in."""
    placed = []
    for obj in page.get_objects():
        left, bottom, right, top = obj.get_bounds()
        container = obj.container
        while container is not None:
            left, bottom, right, top = container.get_matrix().on_rect(
                left, bottom, right, top
            )
            container = container.container
        placed.append((obj, (left, bottom, right, top)))
    return placed


def _clip(rect: Rect, box: Rect) -> Rect | None:
    left, bottom = max(rect[0], box[0]), max(rect[1], box[1])
    right, top = min(rect[2], box[2]), min(rect[3], box[3])
    if right <= left or top <= bottom:
        return None
    return (left, bottom, right, top)


def _area(rect: Rect) -> float:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def union_area(rects: list[Rect]) -> float:
    """The area the rectangles cover together, overlaps counted once."""
    xs = sorted({x for rect in rects for x in (rect[0], rect[2])})
    total = 0.0
    for x0, x1 in zip(xs, xs[1:], strict=False):
        spans = sorted(
            (rect[1], rect[3])
            for rect in rects
            if rect[0] <= x0 and rect[2] >= x1
        )
        covered, low, high = 0.0, 0.0, None
        for bottom, top in spans:
            if high is None or bottom > high:
                if high is not None:
                    covered += high - low
                low, high = bottom, top
            else:
                high = max(high, top)
        if high is not None:
            covered += high - low
        total += covered * (x1 - x0)
    return total


def _address(handle: Any) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


def _decode(codes: list[int]) -> str:
    """Characters from PDFium's code units: a surrogate pair joined, and
    a lone surrogate or a code beyond Unicode as the replacement
    character."""
    chars: list[str] = []
    index = 0
    while index < len(codes):
        code = codes[index]
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(codes):
            low = codes[index + 1]
            if 0xDC00 <= low <= 0xDFFF:
                chars.append(
                    chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
                )
                index += 2
                continue
        if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            chars.append("\ufffd")
        else:
            chars.append(chr(code))
        index += 1
    return "".join(chars)


def _inside(x: float, y: float, rect: Rect) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _visible_text(
    textpage: Any,
    page_box: Rect,
    orders: dict[int, int],
    images: dict[int, Rect],
) -> tuple[str, set[int]]:
    """The text the page draws visibly, and the text objects that draw
    it. A character is visible when its object draws in a visible render
    mode, its centre falls on the page, and no image painted after that
    object covers its centre. Characters PDFium adds between words and
    lines are kept."""
    raw = textpage.raw
    box = [ctypes.c_double() for _ in range(4)]
    # for each text object: None when its render mode draws nothing, else
    # the images painted after it
    over: dict[int, list[Rect] | None] = {}
    drawing: set[int] = set()
    codes: list[int] = []
    for index in range(pdfium_c.FPDFText_CountChars(raw)):
        handle = pdfium_c.FPDFText_GetTextObject(raw, index)
        if handle:
            key = _address(handle)
            if key not in over:
                mode = pdfium_c.FPDFTextObj_GetTextRenderMode(handle)
                painted = orders.get(key, -1)
                over[key] = (
                    None
                    if mode in _INVISIBLE_MODES
                    else [rect for at, rect in images.items() if at > painted]
                )
            later = over[key]
            if later is None:
                continue
            pdfium_c.FPDFText_GetCharBox(raw, index, *box)
            left, right, bottom, top = (value.value for value in box)
            x, y = (left + right) / 2, (bottom + top) / 2
            if not _inside(x, y, page_box) or any(
                _inside(x, y, rect) for rect in later
            ):
                continue
            drawing.add(key)
        codes.append(pdfium_c.FPDFText_GetUnicode(raw, index))
    return _decode(codes), drawing


def _draws(obj: Any, on_page: bool, drawing: set[int]) -> bool:
    """Does the object draw something on the page? An image does where it
    falls on the page, a text object where it draws a visible character,
    and a path or a shading always: PDFium keeps a path as an object only
    when it fills or strokes. A form draws through the objects it
    holds."""
    kind = obj.type
    if kind == pdfium_c.FPDF_PAGEOBJ_IMAGE:
        return on_page
    if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
        return _address(obj.raw) in drawing
    return kind in (pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_SHADING)


def _decoration(obj: Any) -> bool:
    """Is an image too small to hold legible text (MIN_IMAGE_SIDE,
    MIN_IMAGE_PIXELS)?"""
    width, height = obj.get_px_size()
    return (
        min(width, height) < MIN_IMAGE_SIDE
        or width * height < MIN_IMAGE_PIXELS
    )


def _upright(obj: Any) -> bool:
    """Is an object placed without rotation or mirroring?"""
    a, b, c, d, _e, _f = obj.get_matrix().get()
    return b == 0 and c == 0 and a > 0 and d > 0


def _layout(page: Any, number: int) -> _Layout:
    """Read one page and decide how it is read (the module docstring says
    how)."""
    box = page.get_cropbox()
    area = _area(box)
    objects = _objects(page)
    orders = {_address(obj.raw): at for at, (obj, _rect) in enumerate(objects)}
    images = {
        at: clipped
        for at, (obj, rect) in enumerate(objects)
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
        and (clipped := _clip(rect, box)) is not None
    }
    textpage = page.get_textpage()
    try:
        visible, drawing = _visible_text(textpage, box, orders, images)
    finally:
        textpage.close()
    text = clean(visible)
    letters = sum(ch.isalnum() for ch in text)
    cover = union_area(list(images.values())) / area if area > 0 else 0.0
    drawn = [
        at
        for at, (obj, _rect) in enumerate(objects)
        if _draws(obj, at in images, drawing)
    ]
    annotated = pdfium_c.FPDFPage_GetAnnotCount(page.raw) > 0
    if not drawn and not annotated:
        method = BLANK
    elif cover >= SCAN_COVER and letters < MIN_TEXT_OVER_IMAGES:
        method = VISION
    elif usable(visible):
        method = TEXT_LAYER
    else:
        method = VISION
    placed = []
    if method == TEXT_LAYER:
        placed = [
            at
            for at, rect in images.items()
            if _area(rect) < BACKGROUND_COVER * area
            and not _decoration(objects[at][0])
        ]
    lone = None
    if method == VISION and not annotated and len(drawn) == 1:
        at = drawn[0]
        obj = objects[at][0]
        if (
            at in images
            and obj.container is None
            and _area(images[at]) >= BACKGROUND_COVER * area
            and _upright(obj)
            and page.get_rotation() == 0
        ):
            lone = at
    return _Layout(
        number, method, text if method == TEXT_LAYER else "", placed, lone
    )


def image_bytes(obj: Any) -> tuple[bytes, str]:
    """An image object's bytes and their suffix: the JPEG file the PDF
    holds, when its only filter beyond lossless ones is DCTDecode;
    otherwise its pixels as PDFium decodes them, as PNG (`png`)."""
    if obj.get_filters(skip_simple=True) == ["DCTDecode"]:
        return bytes(obj.get_data(decode_simple=True)), JPEG_SUFFIX
    bitmap = obj.get_bitmap(render=False)
    try:
        return bitmap_png(bitmap), PNG_SUFFIX
    finally:
        bitmap.close()


def _write_vision(document: Any, layout: _Layout, directory: Path) -> Path:
    """Write what the vision engine reads of a vision page: its one
    covering image as held, when it draws nothing else and PDFium can
    give its bytes, or else a rendering of the page as PNG."""
    page = document[layout.number - 1]
    try:
        held = None
        if layout.lone_image is not None:
            obj = _objects(page)[layout.lone_image][0]
            try:
                held = image_bytes(obj)
            except (pdfium.PdfiumError, ValueError):
                held = None
        if held is None:
            width, height = page.get_size()
            bitmap = page.render(
                scale=RENDER_LONG_EDGE / max(width, height, 1)
            )
            try:
                held = bitmap_png(bitmap), PNG_SUFFIX
            finally:
                bitmap.close()
        data, suffix = held
    finally:
        page.close()
    path = directory / f"page-{layout.number}{suffix}"
    path.write_bytes(data)
    return path


def _write_images(
    document: Any,
    layout: _Layout,
    page: PdfPage,
    read: PdfPages,
    directory: Path,
    excluded: Callable[[str], bool],
) -> None:
    """Write each image placed within a text-layer page that is read on
    its own, once per SHA-1, and note its place on the page."""
    pdf_page = document[layout.number - 1]
    try:
        objects = _objects(pdf_page)
        for place, at in enumerate(layout.images, start=1):
            try:
                data, suffix = image_bytes(objects[at][0])
            except (pdfium.PdfiumError, ValueError):
                read.notes.append(
                    f"page {layout.number}, image {place}: PDFium could not "
                    "give its bytes, so it is not read"
                )
                continue
            sha1 = hashlib.sha1(data).hexdigest()
            if excluded(sha1):
                continue
            image = read.images.get(sha1)
            if image is None:
                path = directory / f"{sha1}{suffix}"
                path.write_bytes(data)
                image = read.images[sha1] = PlacedImage(sha1, suffix, path)
            image.places.append((layout.number, place))
            page.images.append((place, sha1))
    finally:
        pdf_page.close()
