"""A PDF read page by page: each page's text layer, the images placed
within it, and what the vision engine is given of a page it reads.

A born-digital PDF carries its text in its pages: a text layer read from
the file, deterministically and without a model call, is the page's text
exactly as it was typed. A scanned page carries only a picture of its
text, and the vision engine reads it. `read_pages` tells the two apart
page by page, and the transcription reads a PDF page by page whenever one
of its pages reads from its text layer or its inventory identifies an
independently readable image source
(yizkr.pipeline.transcribe.PdfPageTranscriber, one of the paged
containers of yizkr.pipeline.containers). Otherwise it is read whole by
the vision engine. With exclusion enforcement,
that call receives a deterministic PDF made only from checked page
bitmaps, without the original package's unused resources. Its response
cache key identifies those actual outbound bytes (ADR-0032).

**The text layer** of a page is the text its content draws visibly, in
the order PDFium extracts it. Text drawn in an invisible render mode (3,
as an OCR layer over a scan is, or 7), and text that falls outside the
page, is no part of it: neither can be seen, and an OCR layer is a
machine's reading, not the page's text. A hyphen that PDFium finds at
the end of a line stays, with the line break after it. A text layer is
**usable** when it holds a letter or a digit and at most
`MAX_UNREADABLE_SHARE` of its characters, whitespace aside, are ones no
text is written in: the replacement character, private-use and
unassigned code points, lone surrogates and control characters, which is
what a font without a usable Unicode map extracts as. It is **garbled**
when the replacement character stands within a word, next to a letter or
a digit, where a letter the font could not map should be.

**Text a page hides** is never read from its text layer. A character
drawn in a visible mode that something painted after it covers, its
centre within the other's bounds, cannot be seen as the text layer holds
it: an image or a filled path (a box drawn over a line to redact it, a
scan laid over an OCR layer drawn visibly, or one that only seems to
cover it, an image clipped to less than its bounds, or see-through), or
an annotation, which is painted over the page's content (a box or ink a
viewer draws over a line, a stamp, a redaction marked and not yet
applied). Its page is read by vision, which reads what the page shows. A
text markup (a highlight, an underline, a squiggle or a strike-out)
leaves the text it marks in view. Were a text layer ever given with such
characters left out, each run of them would stand as ``[redacted]``
(`REDACTED`), never silently dropped.

**What it does not see** (known limits, which ADR-0008 records): text
hidden in other ways is read from the text layer as though it showed.
That is text under a thick stroked line (a marker drawn over it and
flattened into the page), text a clip path cuts out of view, text drawn
in the colour behind it (white on a white page), and text drawn at a fill
opacity of 0.

**How each page is read** (its method, which the transcription records):

- ``none``: the page draws nothing and shows no annotation (a link shows
  nothing), so there is nothing to read and no call is made;
- ``vision``, for a page that shows text in an annotation: a free-text
  annotation, or a form field whose value is text;
- ``vision``, for a page that hides text it draws visibly (above);
- ``vision``, for a scanned page: images cover at least `SCAN_COVER` of
  it together, and more of the letters and digits it draws visibly lie
  over an image than beside one (a stamp, a page number or a header over
  a scan, or text laid over a picture that fills the page);
- ``text-layer``: its text layer is usable, and not garbled;
- ``vision`` otherwise: its text layer is empty, garbage or garbled, so
  what it draws can only be read from a picture of it.

**Independent image sources** are selected from the common inventory before
page route selection (ADR-0008): an original that a
compilation embeds beside its translation is its writer's assertion, not
the compiler's. An image under `MIN_IMAGE_SIDE` pixels on a side, or
under `MIN_IMAGE_PIXELS` pixels in all (yizkr.pipeline.containers, the
rule a deck's images are read by too), is taken for decoration: it could
hold no more than a word of legible text, and it is not read. Each image
has a verified canonical media binding (ADR-0010), so repeated placements
of that source are read once. Equal newly decoded pixels alone do not
establish a shared witness. Full-page scans remain page representations
without automatically acquiring independent owners. An image the
never-ingest list names by its SHA-1 is neither read nor kept. Before
any model input is produced, every vision page is checked for excluded
images, including nested and decorative images. A prohibited image or
an uninspectable representation refuses vision for the document. Opaque
annotation appearances, widget drawings, pattern resources, masked images
and Type3 fonts are refused because a partial object walk cannot
establish their eligibility. A vision reading that combines the body with
an inner source or annotation carries unsupported composite attribution;
it cannot assign their assertions to the compiler.
Annotations a text-layer page shows that cover none of its text (a stamp
in a margin, a check box, a highlight) are not read, and the page counts
them.

**The image bytes used by the compatibility reader** are the image as the
PDF holds it where that is a whole JPEG file (a DCTDecode stream, after
any lossless filters). Other images are decoded by PDFium and written as
PNG by `png`, whose output depends only on the pixels and fixed-level
zlib deflate. These stable reading hashes remain the exclusion and excerpt
keys. Before route selection, `pdf_inventory` separately records exact
encoded source streams, native dictionary context and every paint path;
decoded images and page renderings are derived representations of those
parts. Reading hashes do not replace original source identities.
**What the vision engine reads of a page** is the
page's one image, as held, where the page draws nothing but one upright
image that covers at least `BACKGROUND_COVER` of it and shows no
annotation, as a scanned page does; otherwise a rendering of the whole
page, `RENDER_LONG_EDGE` pixels on its long edge, written as PNG, with
its annotations and form fields drawn.

**Fonts, and a fresh start.** PDFium draws a font a PDF does not embed
with one of its own built-in fonts, never with one the host has
installed, and each PDF is read by PDFium started afresh for it, since
how PDFium draws such a font can depend on what it has drawn before. So a
page renders to the same bytes, and its characters take the same places,
on every host and in every run, and the calls that read its pages keep
their keys in the LLM cache: they depend only on the PDF and on the
PDFium build that pyproject.toml pins.

**The library** is pypdfium2 (Apache-2.0 or BSD-3-Clause), the Python
binding of PDFium (BSD-3-Clause), Chromium's PDF engine, which its wheels
bundle: it reads the file offline, in this process, and nothing leaves
the machine. PDFium is not thread-safe, so every call into it is made
under one lock, and every page, form environment and bitmap it hands out
is closed before the lock is released.
"""

from __future__ import annotations

import ctypes
import re
import struct
import threading
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pypdfium2 as pdfium
import pypdfium2._library_scope as pdfium_library
import pypdfium2.raw as pdfium_c
from pypdf.generic import DictionaryObject

from yizkr.pipeline.containers import (
    ContainerPage,
    ContainerPages,
    is_decoration,
)

if TYPE_CHECKING:
    from yizkr.pipeline.pdf_inventory import PdfInventory

# how a page is read
TEXT_LAYER = "text-layer"
VISION = "vision"
NONE = "none"
# the text-layer method, as a transcription's provenance names it: VERSION
# goes up whenever what the method reads off a page changes
METHOD = "pdf-text-layer"
VERSION = 1
LIBRARY = f"pypdfium2 {pdfium.PYPDFIUM_INFO}, pdfium {pdfium.PDFIUM_INFO}"

# the share of a page that images must cover, together, for the page to be
# read as a scan when its text lies over them
SCAN_COVER = 0.6
# the share of a page its one image must cover to be given to the vision
# engine as held, rather than the page rendered
BACKGROUND_COVER = 0.9
# the largest share of a usable text layer's characters, whitespace aside,
# that may be unreadable
MAX_UNREADABLE_SHARE = 0.1
# the long edge, in pixels, of a page rendered for the vision engine
RENDER_LONG_EDGE = 1600
# zlib's level for PNG: fixed, since an image's bytes are its identity
PNG_LEVEL = 6
# what stands for a run of characters a page hides, were a text layer
# ever given without them
REDACTED = "[redacted]"

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
# the annotations that show nothing of their own on the page
_UNSHOWN = frozenset({pdfium_c.FPDF_ANNOT_LINK, pdfium_c.FPDF_ANNOT_POPUP})
# the annotations that mark text up and leave it in view
_MARKUPS = frozenset(
    {
        pdfium_c.FPDF_ANNOT_HIGHLIGHT,
        pdfium_c.FPDF_ANNOT_UNDERLINE,
        pdfium_c.FPDF_ANNOT_SQUIGGLY,
        pdfium_c.FPDF_ANNOT_STRIKEOUT,
    }
)
# the form fields whose value is text
_TEXT_FIELDS = frozenset(
    {
        pdfium_c.FPDF_FORMFIELD_TEXTFIELD,
        pdfium_c.FPDF_FORMFIELD_COMBOBOX,
        pdfium_c.FPDF_FORMFIELD_LISTBOX,
    }
)
# the replacement character next to a letter or a digit
_GARBLED = re.compile(r"[^\W_]\ufffd|\ufffd[^\W_]")
# what marks a character a page hides while its text is put together (a
# code PDFium never gives, which `_decode` makes NUL): a run of them, with
# any whitespace between, is one REDACTED
_HIDDEN_CODE = -1
_HIDDEN_RUN = re.compile(r"\x00(?:[\s\x00]*\x00)?")

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


def garbled(text: str) -> bool:
    """Whether the replacement character stands within a word of `text`,
    next to a letter or a digit: a letter its font could not map."""
    return _GARBLED.search(text) is not None


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


def _bitmap_pixels(bitmap: Any) -> tuple[int, int, int, bytes]:
    """A PDFium bitmap as tightly packed grey, RGB or RGBA pixels."""
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
    return width, height, channels, bytes(pixels)


def bitmap_png(bitmap: Any) -> bytes:
    """A PDFium bitmap as deterministic PNG, retaining its alpha channel."""
    return png(*_bitmap_pixels(bitmap))


class PdfExclusionError(ValueError):
    """The requested PDF vision route cannot establish eligible content."""


def is_pdf_input(path: Path) -> bool:
    """Recognize PDF content even when its filename has an image suffix."""
    if path.suffix.lower() == ".pdf":
        return True
    with path.open("rb") as handle:
        return b"%PDF-" in handle.read(1024)


_MAX_FORM_DEPTH = 64


def _pdf_value(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _pdf_dictionary(value: Any) -> DictionaryObject:
    if value is None:
        return DictionaryObject()
    value = _pdf_value(value)
    if not isinstance(value, DictionaryObject):
        raise PdfExclusionError("PDF resource dictionary cannot be inspected")
    return value


def _check_resources(resources: Any, active: set[int], depth: int = 0):
    """Refuse image-bearing constructs outside the page-object reader.

    This inspects parsed resource dictionaries, including inherited page
    resources and nested forms. It does not decode patterns or appearances,
    and creates no inventory or alternate source identity.
    """
    resources = _pdf_dictionary(resources)
    key = id(resources)
    if key in active or depth >= _MAX_FORM_DEPTH:
        raise PdfExclusionError("PDF resource nesting cannot be inspected")
    active.add(key)
    try:
        if resources.get("/Pattern"):
            raise PdfExclusionError(
                "PDF pattern resources cannot be checked for image exclusions"
            )
        for value in _pdf_dictionary(resources.get("/Font")).values():
            font = _pdf_dictionary(value)
            if _pdf_value(font.get("/Subtype")) == "/Type3":
                raise PdfExclusionError(
                    "PDF Type3 font drawings cannot be checked for image "
                    "exclusions"
                )
        for value in _pdf_dictionary(resources.get("/ExtGState")).values():
            state = _pdf_dictionary(value)
            if _pdf_value(state.get("/SMask", "/None")) != "/None":
                raise PdfExclusionError(
                    "PDF graphics-state mask cannot be checked for exclusions"
                )
        for value in _pdf_dictionary(resources.get("/XObject")).values():
            obj = _pdf_dictionary(value)
            kind = _pdf_value(obj.get("/Subtype"))
            if kind == "/Image":
                if any(
                    name in obj for name in ("/Mask", "/SMask")
                ) or _pdf_value(obj.get("/SMaskInData", 0)):
                    raise PdfExclusionError(
                        "PDF image mask cannot be checked for exclusions"
                    )
            elif kind == "/Form":
                if "/Ref" in obj:
                    raise PdfExclusionError(
                        "PDF external form cannot be checked for exclusions"
                    )
                _check_resources(
                    obj.get("/Resources", resources), active, depth + 1
                )
            else:
                raise PdfExclusionError(
                    "PDF XObject type cannot be checked for image exclusions"
                )
    finally:
        active.remove(key)


def check_pdf_vision(
    path: Path,
    excluded: Callable[[str], bool],
    *,
    pages: set[int] | None = None,
    inventory: PdfInventory | None = None,
) -> int:
    """Admit selected page representations from the source-bound inventory."""
    from yizkr.pipeline.pdf_inventory import inventory_pdf

    source_bytes = path.read_bytes()
    plan = inventory or inventory_pdf(source_bytes, excluded)
    plan.verify(source_bytes)
    plan.apply_exclusions(excluded)
    plan.guard(pages)
    return plan.page_count


def _raster_pdf(pages: list[tuple[int, int, bytes]]) -> bytes:
    """A deterministic PDF of already compressed RGB page pixels."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    children = []
    for width, height, data in pages:
        number = len(objects) + 1
        children.append(f"{number} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Resources << /XObject << /Im0 {number + 1} 0 R >> >> "
            f"/Contents {number + 2} 0 R >>".encode()
        )
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {width} "
            f"/Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 "
            f"/Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )
        commands = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q\n".encode()
        objects.append(
            f"<< /Length {len(commands)} >>\nstream\n".encode()
            + commands
            + b"endstream"
        )
    objects[1] = (
        f"<< /Type /Pages /Count {len(pages)} "
        f"/Kids [{' '.join(children)}] >>".encode()
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def prepare_pdf_vision(
    path: Path,
    directory: Path,
    excluded: Callable[[str], bool],
    *,
    inventory: PdfInventory | None = None,
) -> Path:
    """Make a checked bitmap-only PDF, without original unused resources."""
    from yizkr.pipeline.pdf_inventory import inventory_pdf

    with _LOCK:
        fresh_start()
        try:
            source_bytes = path.read_bytes()
            document = pdfium.PdfDocument(source_bytes)
            try:
                document.init_forms()
                if len(document) < 1:
                    raise PdfExclusionError("PDF has no inspectable pages")
                plan = inventory or inventory_pdf(
                    source_bytes, excluded, document=document
                )
                plan.verify(source_bytes)
                plan.apply_exclusions(excluded)
                plan.guard()
                pages = []
                for index in range(len(document)):
                    page = document[index]
                    try:
                        width, height = page.get_size()
                        bitmap = page.render(
                            scale=RENDER_LONG_EDGE / max(width, height, 1),
                            force_bitmap_format=pdfium_c.FPDFBitmap_BGR,
                            draw_annots=True,
                        )
                        try:
                            w, h, _channels, pixels = _bitmap_pixels(bitmap)
                            pages.append(
                                (w, h, zlib.compress(pixels, PNG_LEVEL))
                            )
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                document.close()
        except (pdfium.PdfiumError, ValueError) as error:
            if isinstance(error, PdfExclusionError):
                raise
            raise PdfExclusionError(
                "PDF cannot be prepared for embedded-image exclusions"
            ) from error
    output = directory / path.with_suffix(".pdf").name
    data = _raster_pdf(pages)
    output.write_bytes(data)
    source = next(
        part
        for part in plan.inventory.parts
        if part.kind == "container_source"
    )
    plan.representation(source, data, ".pdf", "rendered-pdf-v1")
    return output


def read_pages(
    path: Path,
    directory: Path,
    *,
    excluded: Callable[[str], bool] | None = None,
    inventory: PdfInventory | None = None,
) -> ContainerPages | None:
    """Read the PDF at `path` page by page (the module docstring says
    how): each page's method and text layer, and the files the vision
    engine is to read, written into `directory`: each image read on its
    own (``<sha1><suffix>``) and, when any page reads from its text layer
    or an image is read on its own, each vision page's
    (``page-<n><suffix>``). An image read on its own whose SHA-1 the
    never-ingest list names (`excluded`) is neither written nor listed.
    When no page reads from its text layer and no image is read on its
    own, no page file is written, and the caller can prepare a checked
    whole-document representation.
    With an exclusion callback, all vision pages are checked before any
    output; unsafe or unreadable content raises PdfExclusionError. Without
    a callback, an unreadable PDF retains the legacy None result."""
    from yizkr.pipeline.pdf_inventory import inventory_pdf

    with _LOCK:
        fresh_start()
        try:
            source_bytes = path.read_bytes()
            document = pdfium.PdfDocument(source_bytes)
        except pdfium.PdfiumError as error:
            if excluded is not None:
                raise PdfExclusionError(
                    "PDF cannot be inspected for embedded-image exclusions"
                ) from error
            return None
        try:
            document.init_forms()
            plan = inventory or inventory_pdf(
                source_bytes, excluded or (lambda _: False), document=document
            )
            plan.verify(source_bytes)
            from yizkr.pipeline.container_sources import prepare_sources

            plan.replace_inventory(
                prepare_sources(
                    plan.inventory,
                    plan.data,
                    directory,
                    excluded=lambda sha1, _names: bool(
                        excluded and excluded(sha1)
                    ),
                )
            )
            if excluded is not None:
                plan.apply_exclusions(excluded)
            return _read(document, directory, excluded, plan)
        except pdfium.PdfiumError as error:
            if excluded is not None:
                raise PdfExclusionError(
                    "PDF cannot be inspected for embedded-image exclusions"
                ) from error
            return None
        finally:
            # its form environment with it
            document.close()


def fresh_start() -> None:
    """Start PDFium afresh, drawing a font a PDF does not embed with a
    built-in one, whatever fonts the host has (the module docstring says
    why). Called under the lock, with nothing PDFium handed out still
    open."""
    pdfium_library.destroy_lib()
    pdfium_library.init_lib()
    pdfium_c.FPDF_SetSystemFontInfo(None)


@dataclass
class _Layout:
    """What one page holds, as `_layout` reads it: its method and text
    layer, the index of the page's one image when it draws nothing else
    and covers the page, and the annotations it shows that its text layer
    leaves unread."""

    number: int
    method: str
    text: str
    lone_image: int | None
    annotations: int


def _read(
    document: Any,
    directory: Path,
    excluded: Callable[[str], bool] | None,
    plan: PdfInventory,
) -> ContainerPages:
    """`read_pages` of an open document: every page's layout first, and
    only then the files: each image read on its own and, when a page
    reads from its text layer or an image is read on its own, the vision
    pages."""
    layouts = []
    for index in range(len(document)):
        page = document[index]
        try:
            layouts.append(_layout(page, index + 1, document.formenv))
        finally:
            page.close()
    read = ContainerPages(
        [
            ContainerPage(
                layout.number,
                layout.method,
                layout.text,
                annotations=layout.annotations,
            )
            for layout in layouts
        ],
        inventory=plan.inventory,
        inventory_bytes=plan.data,
    )
    if excluded is not None:
        plan.guard(
            {layout.number for layout in layouts if layout.method == VISION}
        )
    from yizkr.pipeline.container_sources import (
        materialize_images,
        project_sources,
    )

    read.sources = project_sources(
        plan.inventory,
        directory,
        data=plan.data,
        excluded=lambda sha1, _names: bool(excluded and excluded(sha1)),
    )
    materialize_images(read.sources, plan.data, directory, read)
    if not read.by_own_text and not read.images:
        return read
    for layout, page in zip(layouts, read.pages, strict=True):
        if page.method == VISION:
            page.vision = _write_vision(document, layout, directory, plan)
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


def _inventory_image_ordinals(page: Any) -> dict[int, int]:
    """Bind legacy visible objects to the full inventory on this open page.

    Native page-object pointers are stable for the page's lifetime only.
    They are neither durable source identities nor persisted locators.
    """
    images = (
        obj
        for obj in page.get_objects(max_depth=_MAX_FORM_DEPTH)
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
    )
    return {
        _address(obj.raw): ordinal
        for ordinal, obj in enumerate(images, start=1)
    }


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
    """Characters from PDFium's code units: a surrogate pair joined, a
    lone surrogate or a code beyond Unicode as the replacement character,
    a NUL as another control character, and _HIDDEN_CODE, which marks a
    character the page hides, as NUL."""
    chars: list[str] = []
    index = 0
    while index < len(codes):
        code = codes[index]
        if code in (_HIDDEN_CODE, 0):
            chars.append("\x00" if code == _HIDDEN_CODE else "\x01")
            index += 1
            continue
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


@dataclass
class _Text:
    """What a page draws of its text layer: its text, the text objects
    that draw a character that can be seen, how many characters drawn in
    a visible mode something painted after them hides, and how many of the
    letters and digits that can be seen lie over an image painted before
    them, and how many beside."""

    text: str
    drawing: set[int]
    hidden: int
    over: int
    beside: int


def _characters(
    textpage: Any,
    page_box: Rect,
    orders: dict[int, int],
    hiders: dict[int, Rect],
    images: dict[int, Rect],
) -> _Text:
    """The page's text layer (the module docstring says what it holds): a
    character drawn in a visible mode, whose centre falls on the page, is
    part of it, unless something painted after its object (`hiders`: an
    image, a filled path or an annotation) covers its centre, when it is
    hidden and stands in the text as REDACTED. Characters PDFium adds
    between words and lines are kept, and a hyphen it finds at a line's
    end is given back with its line break."""
    raw = textpage.raw
    box = [ctypes.c_double() for _ in range(4)]
    # for each text object: None when its render mode draws nothing, else
    # the hiders painted after it and the images painted before it
    seen: dict[int, tuple[list[Rect], list[Rect]] | None] = {}
    drawing: set[int] = set()
    hidden = over = beside = 0
    codes: list[int] = []
    for index in range(pdfium_c.FPDFText_CountChars(raw)):
        code = pdfium_c.FPDFText_GetUnicode(raw, index)
        handle = pdfium_c.FPDFText_GetTextObject(raw, index)
        if handle:
            key = _address(handle)
            if key not in seen:
                mode = pdfium_c.FPDFTextObj_GetTextRenderMode(handle)
                painted = orders.get(key, -1)
                seen[key] = (
                    None
                    if mode in _INVISIBLE_MODES
                    else (
                        [rect for at, rect in hiders.items() if at > painted],
                        [rect for at, rect in images.items() if at < painted],
                    )
                )
            entry = seen[key]
            if entry is None:
                continue
            later, under = entry
            pdfium_c.FPDFText_GetCharBox(raw, index, *box)
            left, right, bottom, top = (value.value for value in box)
            x, y = (left + right) / 2, (bottom + top) / 2
            if not _inside(x, y, page_box):
                continue
            if any(_inside(x, y, rect) for rect in later):
                # a space hides nothing, and stays as it is
                if code > 0x10FFFF or not chr(code).isspace():
                    hidden += 1
                    code = _HIDDEN_CODE
                codes.append(code)
                continue
            drawing.add(key)
            if code <= 0x10FFFF and chr(code).isalnum():
                if any(_inside(x, y, rect) for rect in under):
                    over += 1
                else:
                    beside += 1
        codes.append(code)
    text = _decode(codes).replace("\x02", "-\n")
    text = _HIDDEN_RUN.sub(REDACTED, text)
    return _Text(text, drawing, hidden, over, beside)


def _filled(obj: Any) -> bool:
    """Does a path object fill the area it bounds?"""
    fill, stroke = ctypes.c_int(), ctypes.c_int()
    if not pdfium_c.FPDFPath_GetDrawMode(obj.raw, fill, stroke):
        return False
    return fill.value != pdfium_c.FPDF_FILLMODE_NONE


def _draws(obj: Any, on_page: bool, drawing: set[int]) -> bool:
    """Does the object draw something on the page? An image does where it
    falls on the page, a text object where it draws a character that can
    be seen, and a path or a shading always: PDFium keeps a path as an
    object only when it fills or strokes. A form draws through the
    objects it holds."""
    kind = obj.type
    if kind == pdfium_c.FPDF_PAGEOBJ_IMAGE:
        return on_page
    if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
        return _address(obj.raw) in drawing
    return kind in (pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_SHADING)


def _decoration(obj: Any) -> bool:
    """Is an image too small to hold legible text (MIN_IMAGE_SIDE,
    MIN_IMAGE_PIXELS)?"""
    return is_decoration(*obj.get_px_size())


def _upright(obj: Any) -> bool:
    """Is an object placed without rotation or mirroring?"""
    a, b, c, d, _e, _f = obj.get_matrix().get()
    return b == 0 and c == 0 and a > 0 and d > 0


def _field_text(formenv: Any, annot: Any) -> str:
    """The value of the form field an annotation is a widget of, when the
    field's value is text; "" otherwise."""
    if not formenv:
        return ""
    kind = pdfium_c.FPDFAnnot_GetFormFieldType(formenv, annot)
    if kind not in _TEXT_FIELDS:
        return ""
    size = pdfium_c.FPDFAnnot_GetFormFieldValue(formenv, annot, None, 0)
    if size <= 2:
        return ""
    buffer = (ctypes.c_ushort * (size // 2))()
    pdfium_c.FPDFAnnot_GetFormFieldValue(formenv, annot, buffer, size)
    return bytes(buffer).decode("utf-16-le", errors="replace").rstrip("\x00")


@dataclass
class _Annotations:
    """What a page's annotations show (`_annotations`): whether one shows
    text, how many others there are, which its text layer leaves unread,
    and the rectangle of each that can cover the page's text."""

    shows_text: bool = False
    others: int = 0
    covers: list[Rect] = field(default_factory=list)


def _annotations(page: Any, formenv: Any) -> _Annotations:
    """What the page's annotations show. A free-text annotation, and a
    form field whose value is text, show text; a link and a pop-up show
    nothing of their own, and are not counted. Every annotation that shows
    something is painted over the page's content, and covers the text
    under its rectangle, except a text markup (a highlight, an underline,
    a squiggle or a strike-out), which leaves the text it marks in view."""
    shown = _Annotations()
    rect = pdfium_c.FS_RECTF()
    for index in range(pdfium_c.FPDFPage_GetAnnotCount(page.raw)):
        annot = pdfium_c.FPDFPage_GetAnnot(page.raw, index)
        if not annot:
            continue
        try:
            kind = pdfium_c.FPDFAnnot_GetSubtype(annot)
            if kind in _UNSHOWN:
                continue
            if kind == pdfium_c.FPDF_ANNOT_FREETEXT or (
                kind == pdfium_c.FPDF_ANNOT_WIDGET
                and _field_text(formenv, annot).strip()
            ):
                shown.shows_text = True
            else:
                shown.others += 1
            if kind not in _MARKUPS and pdfium_c.FPDFAnnot_GetRect(
                annot, rect
            ):
                # a rectangle may name any two opposite corners
                shown.covers.append(
                    (
                        min(rect.left, rect.right),
                        min(rect.bottom, rect.top),
                        max(rect.left, rect.right),
                        max(rect.bottom, rect.top),
                    )
                )
        finally:
            pdfium_c.FPDFPage_CloseAnnot(annot)
    return shown


@dataclass
class _Contents:
    """What one page draws: its box and area, its objects in paint order
    with their bounds, its images on the page by index (their bounds
    clipped to it), its text layer (`_characters`) and its annotations."""

    box: Rect
    area: float
    objects: list[tuple[Any, Rect]]
    images: dict[int, Rect]
    text: _Text
    annotations: _Annotations


def _contents(page: Any, formenv: Any = None) -> _Contents:
    """What `page` draws (`_Contents`), read under the lock. An image or a
    filled path hides the text an earlier object draws, and an annotation,
    painted over every object, the text of any that lies under it."""
    box = page.get_cropbox()
    objects = _objects(page)
    orders = {_address(obj.raw): at for at, (obj, _rect) in enumerate(objects)}
    images = {
        at: clipped
        for at, (obj, rect) in enumerate(objects)
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE
        and (clipped := _clip(rect, box)) is not None
    }
    hiders = dict(images)
    for at, (obj, rect) in enumerate(objects):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH and _filled(obj):
            clipped = _clip(rect, box)
            if clipped is not None:
                hiders[at] = clipped
    annotations = _annotations(page, formenv)
    for at, rect in enumerate(annotations.covers, start=len(objects)):
        hiders[at] = rect
    textpage = page.get_textpage()
    try:
        text = _characters(textpage, box, orders, hiders, images)
    finally:
        textpage.close()
    return _Contents(box, _area(box), objects, images, text, annotations)


def _layout(page: Any, number: int, formenv: Any) -> _Layout:
    """Read one page and decide how it is read (the module docstring says
    how)."""
    contents = _contents(page, formenv)
    area, objects, images = contents.area, contents.objects, contents.images
    drawn_text = contents.text
    visible = drawn_text.text
    cover = union_area(list(images.values())) / area if area > 0 else 0.0
    drawn = [
        at
        for at, (obj, _rect) in enumerate(objects)
        if _draws(obj, at in images, drawn_text.drawing)
    ]
    annotations = contents.annotations
    shows_text, others = annotations.shows_text, annotations.others
    shows = shows_text or others > 0
    if not drawn and not shows:
        method = NONE
    elif (
        # text an annotation shows, text the page hides, or a scan
        shows_text
        or drawn_text.hidden
        or (cover >= SCAN_COVER and drawn_text.over > drawn_text.beside)
    ):
        method = VISION
    elif usable(visible) and not garbled(visible):
        method = TEXT_LAYER
    else:
        method = VISION
    lone = None
    if method == VISION and not shows and len(drawn) == 1:
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
    reads_text = method == TEXT_LAYER
    return _Layout(
        number,
        method,
        clean(visible) if reads_text else "",
        lone,
        others if reads_text else 0,
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


def _write_vision(
    document: Any, layout: _Layout, directory: Path, plan: PdfInventory
) -> Path:
    """Write what the vision engine reads of a vision page: its one
    covering image as held, when it draws nothing else and PDFium can
    give its bytes, or else a rendering of the page as PNG."""
    page = document[layout.number - 1]
    try:
        held = None
        method = "pdfium-page-render"
        if layout.lone_image is not None:
            objects = _objects(page)
            ordinal = _inventory_image_ordinals(page).get(
                _address(objects[layout.lone_image][0].raw)
            )
            prepared = plan.image_representations.get((layout.number, ordinal))
            try:
                if prepared is not None and prepared.file is not None:
                    held = (
                        plan.data[prepared.byte_object.sha1],
                        JPEG_SUFFIX
                        if prepared.media_type == "image/jpeg"
                        else PNG_SUFFIX,
                    )
                else:
                    held = image_bytes(objects[layout.lone_image][0])
                method = "pdf-page-image"
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
    part = plan.page_parts.get(layout.number)
    if part is not None:
        plan.representation(part, data, suffix, method)
    return path
