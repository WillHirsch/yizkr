"""Fictional PDFs for the tests, written byte by byte.

A test builds the PDF it needs from pages: text drawn in the standard
Helvetica font (WinAnsiEncoding, so German letters extract as written),
or in a font of another name that no PDF embeds, drawn visibly or
invisibly (render mode 3, as an OCR layer is); text whose font maps its
codes to private-use characters (a text layer that extracts as garbage),
or one letter to the replacement character; images placed anywhere,
directly or inside a form XObject, clipped, see-through, mirrored, with a
soft mask or as a stencil mask; filled rectangles; and annotations and
form fields. The bytes depend on nothing but the arguments: no
timestamps, no document ID, and images stored uncompressed, as the
constant JPEG below, or that JPEG compressed again by zlib.

    from tests.fixtures import pdfs

    page = pdfs.Page()
    page.text(["Our Family from Germany"])
    page.image(pdfs.gray(200, 100), 100, 400, 300, 150)
    data = pdfs.pdf([page])

Every text a test writes comes from the fictional universe
(docs/EXAMPLES.md).
"""

from __future__ import annotations

import base64
import textwrap
import zlib
from dataclasses import dataclass, field

# A4, in points
WIDTH, HEIGHT = 595, 842

# A 128 by 96 grey gradient, JPEG-encoded once: an image the PDF holds as
# a whole JPEG file (DCTDecode)
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVl"
    "ZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/wAALCABgAIABAREA/8QAHwAA"
    "AQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
    "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF"
    "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
    "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEB"
    "AAA/AOcUVKoqVRUiipVFSqKlUVIoqVRUqipVFSKKlUVKoqVRVXWL3+z9OklU4kb5I/8AeP8A"
    "hyfwriVFSKKlUVKoqVRUiipVFSqKkUVKoqVRUqipFFSqKlUVyHiW9+16iYlP7u3yg92/i/w/"
    "Cs5RUqipFFSqKlUVKoqRRUqipVFSqKkUVKoqVRUqiqusXv8AZ+nSSqcSN8kf+8f8OT+FcMoq"
    "dRUqipVFSqKkUVKoqVRUiipVFSqKlUVIoqVRUqiuQ8S3v2vUTEp/d2+UHu38X+H4VmKKnUVI"
    "oqVRUqipVFSKKlUVKoqVRUiipVFSqKlUVV1i9/s/TpJVOJG+SP8A3j/hyfwrhlFSqKnUVKoq"
    "VRUiipVFSqKkUVKoqVRUqipFFSqKlUVyHiW9+16iYlP7u3yg92/i/wAPwrMUVIoqdRUqipVF"
    "SqKkUVKoqVRUqipFFSqKlUVKoqrrF7/Z+nSSqcSN8kf+8f8ADk/hXDKKlUVKoqdRUqipFFSq"
    "KlUVIoqVRUqipVFSKKlUVKorkPEt79r1ExKf3dvlB7t/F/h+FZiipFFSqKnUVKoqVRUiipVF"
    "SqKlUVIoqVRUqipVFVdYvf7P06SVTiRvkj/3j/hyfwrhlFSqKlUVIoqwoqRRUqipVFSKKlUV"
    "KoqVRUiipVFSqK5DxLe/a9RMSn93b5Qe7fxf4fhWYoqRRUqipVFTqKlUVIoqVRUqipVFSKKl"
    "UVKoqVRVXWL3+z9OklU4kb5I/wDeP+HJ/CuGUVKoqVRUiipVFTqKlUVKoqRRUqipVFSqKkUV"
    "KoqVRXIeJb37XqJiU/u7fKD3b+L/AA/CsxRUiipVFSqKlUV//9k="
)
JPEG_SIZE = (128, 96)

# the fonts every page can use: the PDF's plain font as written (F1),
# Helvetica whose codes extract as private-use characters (F2), as a font
# with a broken ToUnicode map gives them, and Helvetica whose "ö" alone
# extracts as the replacement character (F3)
_GARBLING_CMAP = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
    b"/CMapName /Garbled def /CMapType 2 def\n"
    b"1 begincodespacerange <00> <FF> endcodespacerange\n"
    b"1 beginbfrange <20> <FF> <E020> endbfrange\n"
    b"endcmap CMapName currentdict /CMap defineresource pop end end"
)
_REPLACING_CMAP = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
    b"/CMapName /Replacing def /CMapType 2 def\n"
    b"1 begincodespacerange <00> <FF> endcodespacerange\n"
    b"2 beginbfrange <20> <F5> <0020> <F7> <FF> <00F7> endbfrange\n"
    b"1 beginbfchar <F6> <FFFD> endbfchar\n"
    b"endcmap CMapName currentdict /CMap defineresource pop end end"
)
PLAIN = "F1"
GARBLED = "F2"
REPLACED = "F3"
INVISIBLE = 3
# render mode 7 adds the text to the clipping path and draws nothing
CLIPPING = 7


@dataclass(frozen=True)
class Image:
    """An image XObject: its dictionary's entries and its stream, and the
    image that is its soft mask, if it has one."""

    entries: str
    data: bytes
    smask: Image | None = None


def gray(width: int, height: int, *, shade: int = 0) -> Image:
    """A grey image of `width` by `height` pixels, stored uncompressed: a
    diagonal gradient, offset by `shade` so that two images of one size
    can hold different bytes."""
    data = bytes(
        (x * 7 + y * 3 + shade) % 256
        for y in range(height)
        for x in range(width)
    )
    return Image(
        f"/Width {width} /Height {height} /ColorSpace /DeviceGray "
        "/BitsPerComponent 8",
        data,
    )


def rgb(width: int, height: int) -> Image:
    """An RGB image of `width` by `height` pixels, stored uncompressed."""
    data = bytes(
        channel
        for y in range(height)
        for x in range(width)
        for channel in ((x * 11) % 256, (y * 13) % 256, (x + y) % 256)
    )
    return Image(
        f"/Width {width} /Height {height} /ColorSpace /DeviceRGB "
        "/BitsPerComponent 8",
        data,
    )


def jpeg(*, flated: bool = False) -> Image:
    """The constant JPEG, as the PDF holds it (DCTDecode); `flated`, that
    JPEG compressed again by zlib (FlateDecode, then DCTDecode)."""
    width, height = JPEG_SIZE
    filters = "[/FlateDecode /DCTDecode]" if flated else "/DCTDecode"
    return Image(
        f"/Width {width} /Height {height} /ColorSpace /DeviceGray "
        f"/BitsPerComponent 8 /Filter {filters}",
        zlib.compress(JPEG) if flated else JPEG,
    )


def transparent(image: Image) -> Image:
    """`image` with a soft mask that lets none of it show: a stamp that
    draws nothing."""
    width = int(image.entries.split("/Width ")[1].split()[0])
    height = int(image.entries.split("/Height ")[1].split()[0])
    mask = Image(
        f"/Width {width} /Height {height} /ColorSpace /DeviceGray "
        "/BitsPerComponent 8",
        bytes(width * height),
    )
    return Image(image.entries, image.data, smask=mask)


def stencil(width: int, height: int) -> Image:
    """A stencil mask of `width` by `height` pixels (one bit each), which
    paints the fill colour where its bits say, in stripes."""
    row = bytes(0b11110000 for _ in range((width + 7) // 8))
    return Image(
        f"/Width {width} /Height {height} /ImageMask true /BitsPerComponent 1",
        row * height,
    )


def lines(*paragraphs: str, width: int = 78) -> list[str]:
    """The paragraphs as the lines a page draws them in, wrapped to fit a
    page at the default size: each paragraph's lines, then the next's."""
    return [
        line
        for paragraph in paragraphs
        for line in textwrap.wrap(paragraph, width) or [""]
    ]


def _literal(text: str) -> bytes:
    escaped = (
        text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    )
    return b"(" + escaped.encode("cp1252") + b")"


@dataclass
class Page:
    """One page: the operators of its content, in the order they paint,
    and the images and forms they name."""

    width: float = WIDTH
    height: float = HEIGHT
    rotate: int = 0
    ops: list[bytes] = field(default_factory=list)
    images: dict[str, Image] = field(default_factory=dict)
    # form name -> (the image it holds, placed on the form's unit square)
    forms: dict[str, Image] = field(default_factory=dict)
    # graphics state name -> the opacity it sets
    alphas: dict[str, float] = field(default_factory=dict)
    # each annotation's dictionary; a form field's is a widget's
    annotations: list[str] = field(default_factory=list)
    # whether it paints the grey shading (`shade`)
    shaded: bool = False

    def text(
        self,
        lines: list[str],
        *,
        x: float = 72,
        y: float = 770,
        size: float = 10,
        mode: int = 0,
        font: str = PLAIN,
    ) -> Page:
        """Draw `lines` from (x, y) down, one line a size and a bit apart,
        in render `mode` (0 fills them; 3 draws nothing, as an OCR layer).
        A line drawn at the default size fits the page up to about 80
        characters (`lines` wraps paragraphs to fit)."""
        ops = [
            f"BT /{font} {size} Tf {mode} Tr {x} {y} Td {size + 2} TL".encode()
        ]
        ops += [_literal(line) + b" Tj T*" for line in lines]
        ops.append(b"ET")
        self.ops.append(b"\n".join(ops))
        return self

    def image(
        self,
        image: Image,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        clip: tuple[float, float, float, float] | None = None,
        alpha: float | None = None,
        mirrored: bool = False,
    ) -> Page:
        """Place `image` in the rectangle (x, y, w, h): only as much of it
        as the rectangle `clip` (x, y, w, h) shows, when it is given, as
        a cropped photograph is; at the opacity `alpha`; or turned upside
        down, `mirrored`."""
        name = f"Im{len(self.images) + 1}"
        self.images[name] = image
        ops = ["q"]
        if clip is not None:
            ops.append("{} {} {} {} re W n".format(*clip))
        if alpha is not None:
            state = f"GS{len(self.alphas) + 1}"
            self.alphas[state] = alpha
            ops.append(f"/{state} gs")
        if mirrored:
            ops.append(f"{w} 0 0 {-h} {x} {y + h} cm")
        else:
            ops.append(f"{w} 0 0 {h} {x} {y} cm")
        ops += [f"/{name} Do", "Q"]
        self.ops.append(" ".join(ops).encode())
        return self

    def form_image(
        self, image: Image, x: float, y: float, w: float, h: float
    ) -> Page:
        """Place `image` in the rectangle (x, y, w, h), inside a form
        XObject that the page draws scaled and moved there."""
        name = f"Fm{len(self.forms) + 1}"
        self.forms[name] = image
        self.ops.append(f"q {w} 0 0 {h} {x} {y} cm /{name} Do Q".encode())
        return self

    def rectangle(
        self, x: float, y: float, w: float, h: float, *, gray: float = 0.5
    ) -> Page:
        """Fill a rectangle (a path) in the grey `gray` (0 is black, as a
        box drawn over a line to redact it is)."""
        self.ops.append(f"q {gray} g {x} {y} {w} {h} re f Q".encode())
        return self

    def shade(self, x: float, y: float, w: float, h: float) -> Page:
        """Paint a shading, black to white from left to right, over the
        rectangle (x, y, w, h), within whatever clip is in force: text
        drawn as a clip (render mode 7) shows only where it lets the
        shading through."""
        self.shaded = True
        self.ops.append(f"q {x} {y} {w} {h} re W n /Sh1 sh Q".encode())
        return self

    def frame(self, x: float, y: float, w: float, h: float) -> Page:
        """Stroke a rectangle's outline in black (a path that fills
        nothing), as a table's borders are drawn."""
        self.ops.append(f"q 0 G 1 w {x} {y} {w} {h} re S Q".encode())
        return self

    def annotate(self, entries: str) -> Page:
        """Add an annotation, of the dictionary `entries` (a subtype and a
        rectangle, say)."""
        self.annotations.append(f"<< /Type /Annot {entries} >>")
        return self

    def field(
        self,
        name: str,
        value: str,
        rect: tuple[float, float, float, float],
        *,
        kind: str = "/Tx",
        flags: int = 0,
    ) -> Page:
        """Add a form field on the page: a text field (`kind` /Tx) holding
        `value`; a choice field (/Ch) whose one option, `value`, is
        chosen, a list box, or a combo box by its `flags`; or a check box
        (/Btn), whose value is a name such as /Yes."""
        shown = value if kind == "/Btn" else _literal(value).decode("cp1252")
        entries = f"/FT {kind} /T ({name}) /V {shown}"
        if kind == "/Ch":
            entries += f" /Opt [{shown}]"
        if flags:
            entries += f" /Ff {flags}"
        self.annotations.append(
            f"<< /Type /Annot /Subtype /Widget {entries} "
            f"/Rect [{' '.join(str(v) for v in rect)}] >>"
        )
        return self


def pdf(pages: list[Page], *, font: str = "Helvetica") -> bytes:
    """A PDF of `pages`, in order, whose plain font (F1) is the font of
    that name, not embedded: a standard one, as Helvetica is, or one a
    host may or may not have."""
    objects: list[bytes] = []

    def add(body: bytes | str) -> int:
        objects.append(body.encode() if isinstance(body, str) else body)
        return len(objects)

    def stream(entries: str, data: bytes) -> bytes:
        return (
            f"<< {entries} /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )

    def image_object(image: Image) -> int:
        entries = f"/Type /XObject /Subtype /Image {image.entries}"
        if image.smask is not None:
            entries += f" /SMask {image_object(image.smask)} 0 R"
        return add(stream(entries, image.data))

    catalog = add(b"")
    tree = add(b"")
    plain = add(
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} "
        "/Encoding /WinAnsiEncoding >>"
    )
    fonts = f"/{PLAIN} {plain} 0 R"
    for name, cmap in ((GARBLED, _GARBLING_CMAP), (REPLACED, _REPLACING_CMAP)):
        mapped = add(stream("", cmap))
        ref = add(
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            f"/Encoding /WinAnsiEncoding /ToUnicode {mapped} 0 R >>"
        )
        fonts += f" /{name} {ref} 0 R"
    kids, fields = [], []
    for page in pages:
        xobjects = {}
        for name, image in page.images.items():
            xobjects[name] = image_object(image)
        for name, image in page.forms.items():
            inner = image_object(image)
            xobjects[name] = add(
                stream(
                    "/Type /XObject /Subtype /Form /BBox [0 0 1 1] "
                    f"/Resources << /XObject << /Im {inner} 0 R >> >>",
                    b"q 1 0 0 1 0 0 cm /Im Do Q",
                )
            )
        named = " ".join(
            f"/{name} {ref} 0 R" for name, ref in xobjects.items()
        )
        states = " ".join(
            f"/{name} << /ca {alpha} /CA {alpha} >>"
            for name, alpha in page.alphas.items()
        )
        shading = (
            " /Shading << /Sh1 << /ShadingType 2 /ColorSpace /DeviceGray "
            f"/Coords [0 0 {page.width} 0] /Function << /FunctionType 2 "
            "/Domain [0 1] /C0 [0] /C1 [1] /N 1 >> >> >>"
            if page.shaded
            else ""
        )
        resources = (
            f"<< /Font << {fonts} >> /XObject << {named} >> "
            f"/ExtGState << {states} >>{shading} >>"
        )
        contents = add(stream("", b"\n".join(page.ops)))
        rotate = f" /Rotate {page.rotate}" if page.rotate else ""
        annotations = [add(entries) for entries in page.annotations]
        fields += [
            ref
            for ref, entries in zip(annotations, page.annotations, strict=True)
            if "/Subtype /Widget" in entries
        ]
        annots = (
            f" /Annots [{' '.join(f'{ref} 0 R' for ref in annotations)}]"
            if annotations
            else ""
        )
        kids.append(
            add(
                f"<< /Type /Page /Parent {tree} 0 R "
                f"/MediaBox [0 0 {page.width} {page.height}]{rotate} "
                f"/Resources {resources} /Contents {contents} 0 R{annots} >>"
            )
        )
    listed = " ".join(f"{ref} 0 R" for ref in fields)
    form = f" /AcroForm << /Fields [{listed}] >>" if fields else ""
    objects[catalog - 1] = (
        f"<< /Type /Catalog /Pages {tree} 0 R{form} >>".encode()
    )
    objects[tree - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] "
        f"/Count {len(kids)} >>"
    ).encode()
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
