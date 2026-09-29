"""Build the fixture corpus's documents (docs/EXAMPLES.md, "The fixture
corpus").

    uv run python -m tests.fixtures.corpus.build

writes every document into tests/fixtures/corpus/documents/, which holds
what it wrote last: the committed files are its output, and
tests/unit/test_fixture_corpus.py fails when they differ from what it
makes now. Each document's content comes from the example universe
(tests/fixtures/example_universe.py): the family history's text is its
source's; the deck's slides give its source's text; the two images are
stand-ins for the certificate and the postcard, whose texts the recorded
engine reads off them (tests/fixtures/corpus/answers.py); and the export is
a slice of the universe's GEDCOM rendering (tests/fixtures/example-family.ged)
with two traps of its own.

Nothing it writes depends on when or where it runs: the images are stored
without compression, so their bytes depend on no zlib, and the deck is a
ZIP archive of stored members with one fixed timestamp.
"""

from __future__ import annotations

import struct
import sys
import zipfile
import zlib
from pathlib import Path

from tests.fixtures import example_universe as u
from tests.fixtures.corpus import (
    CERTIFICATE,
    DECK,
    EXPORT,
    FOLDER,
    LETTER,
    NARRATIVE,
)

SOURCES = {source.key: source for source in u.SOURCES}

# -- the export ---------------------------------------------------------------

# The Lobig line of example-family.ged, as a second genealogy program would
# export it: its people and families, record for record, with two changes
# (the fixture corpus's traps). Sebastian's daughter Ursula carries no date,
# so her record and the widow's describe them alike: one name, one sex, no
# birth or death. And Nicholas's record holds a DEAT that states no death
# (no DATE, PLAC or Y), which such programs write for anyone.
EXPORT_TEXT = """\
0 HEAD
1 SOUR yizkr-example-universe
1 GEDC
2 VERS 5.5.1
1 CHAR UTF-8
0 @I3@ INDI
1 NAME Sebastian /Löbig/
2 TYPE birth
2 NICK Wastl
1 SEX M
1 BIRT
2 DATE 15 FEB 1862
2 PLAC Flörburg, Hesse, Germany
1 DEAT
2 DATE 1945
1 OCCU Hatter
1 FAMS @F2@
0 @I4@ INDI
1 NAME Hildegard /Laudler/
2 TYPE birth
2 NICK Hilde
1 NAME Hildegard /Löbig/
2 TYPE married
1 SEX F
1 BIRT
2 DATE 19 MAR 1876
2 PLAC Fläckfeld, Kreis Mockburg, Hesse, Germany
1 DEAT
2 DATE 1968
1 FAMS @F2@
0 @I10@ INDI
1 NAME Ursula /Löbig/
1 SEX F
1 FAMC @F2@
0 @I9@ INDI
1 NAME Klara /Löbig/
2 TYPE birth
2 NICK Pünktchen
1 NAME Clara /Lobig/
2 TYPE immigrant
1 SEX F
1 BIRT
2 DATE 1917
1 DEAT
2 DATE 2001
1 RESI
2 DATE 19 AUG 1938
2 PLAC 31 Juniper Street, Stupfield, Victoria, Australia
1 FAMC @F2@
0 @I8@ INDI
1 NAME Gustav /Löbig/
2 TYPE birth
2 NICK Gustl
1 NAME Gustave /Lobig/
2 TYPE immigrant
1 SEX M
1 BIRT
2 DATE 7 JAN 1919
2 PLAC Flörburg, Hesse, Germany
1 DEAT
2 DATE 1996
1 BURI
2 PLAC Stupfield, Victoria, Australia
1 OCCU Hatter
1 RESI
2 DATE 1952
2 PLAC 8 Hawthorn Road, Stupfield, Victoria, Australia
1 FAMC @F2@
1 FAMS @F4@
0 @I14@ INDI
1 NAME Florence /Faxridge/
2 TYPE birth
1 NAME Florence /Lobig/
2 TYPE married
1 SEX F
1 BIRT
2 DATE 1914
1 DEAT
2 DATE 1963
1 FAMS @F4@
0 @I15@ INDI
1 NAME Nicholas /Lobig/
1 SEX M
1 BIRT
2 DATE 1950
2 PLAC Stupfield, Victoria, Australia
1 DEAT
1 FAMC @F4@
0 @I16@ INDI
1 NAME Ursula /Löbig/
1 SEX F
1 RESI
2 DATE 1912
2 PLAC Angerring 57, Flörburg, Hesse, Germany
0 @F2@ FAM
1 HUSB @I3@
1 WIFE @I4@
1 CHIL @I10@
1 CHIL @I9@
1 CHIL @I8@
1 MARR
2 DATE 15 DEC 1905
2 PLAC Flörburg, Hesse, Germany
0 @F4@ FAM
1 HUSB @I8@
1 WIFE @I14@
1 CHIL @I15@
1 MARR
2 DATE 1942
0 TRLR
"""

# -- images -------------------------------------------------------------------

PAPER, INK = 0xF2, 0x40
# the width of a stand-in page, and the characters one of its bars stands for
PAGE_WIDTH = 64
LINE_CHARS = 48


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
        struct.pack(">I", len(data))
        + body
        + struct.pack(">I", zlib.crc32(body))
    )


def _stored(data: bytes) -> bytes:
    """A zlib stream that holds `data` in stored deflate blocks: the same
    bytes whichever zlib is at hand, since nothing is compressed."""
    blocks = [data[i : i + 0xFFFF] for i in range(0, len(data), 0xFFFF)] or [
        b""
    ]
    out = bytearray(b"\x78\x01")
    for index, block in enumerate(blocks):
        final = index == len(blocks) - 1
        out += bytes([1 if final else 0])
        out += struct.pack("<HH", len(block), len(block) ^ 0xFFFF)
        out += block
    out += struct.pack(">I", zlib.adler32(data))
    return bytes(out)


def stand_in(text: str, title: str) -> bytes:
    """A small grayscale PNG that stands in for a scan: a light page with a
    dark bar for every LINE_CHARS characters of `text`, and `title` in an
    iTXt chunk. It shows no text; the recorded engine reads the text off
    it by its bytes."""
    words = text.split()
    lines: list[int] = []
    current = 0
    for word in words:
        if current and current + 1 + len(word) > LINE_CHARS:
            lines.append(current)
            current = 0
        current += (1 if current else 0) + len(word)
    if current:
        lines.append(current)
    height = 8 + 5 * len(lines)
    rows = []
    for y in range(height):
        row = bytearray([PAPER] * PAGE_WIDTH)
        line, offset = divmod(y - 4, 5)
        if y >= 4 and line < len(lines) and offset < 2:
            length = max(1, lines[line] * (PAGE_WIDTH - 8) // LINE_CHARS)
            row[4 : 4 + length] = bytes([INK]) * length
        rows.append(b"\x00" + bytes(row))
    header = struct.pack(">IIBBBBB", PAGE_WIDTH, height, 8, 0, 0, 0, 0)
    caption = (
        b"Title\x00\x00\x00\x00\x00"
        + title.encode()
        + b" (a fictional stand-in, docs/EXAMPLES.md)"
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"iTXt", caption)
        + _chunk(b"IDAT", _stored(b"".join(rows)))
        + _chunk(b"IEND", b"")
    )


# -- the deck -----------------------------------------------------------------

# each slide as (its paragraphs, its speaker notes, whether the photograph
# is on it); slide_transcript gives the deck's source text from them
SLIDES = (
    (
        ("Where the Lobigs came from", "Nicholas Lobig · library talk, 2018"),
        (),
        False,
    ),
    (
        (
            "Wastl and Hilde",
            "Sebastian 'Wastl' Löbig 1862–1945 = Hildegard 'Hilde' "
            "Laudler 1876–1968",
            "Their children: Ursula, Klara 'Pünktchen' and Gustav 'Gustl'",
        ),
        ("Photograph from the family album",),
        True,
    ),
    (
        (
            "Stupfield",
            "Klara came first, Gustl in 1939.",
            "Gustave 'Gus' Lobig = Florence Faxridge, 1942",
            "Their son: Nicholas 1950",
        ),
        (),
        False,
    ),
)
PHOTOGRAPH = stand_in(
    "Sebastian Wastl Hildegard Hilde Ursula Klara Gustav",
    "Photograph from the family album",
)

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SLIDE = f"{_R}/slide"
_IMAGE = f"{_R}/image"
_NOTES = f"{_R}/notesSlide"
_DOCUMENT = f"{_R}/officeDocument"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_PML = "application/vnd.openxmlformats-officedocument.presentationml"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slide_xml(paragraphs: tuple[str, ...], root: str = "sld") -> str:
    body = "".join(
        f"<a:p><a:r><a:t>{_escape(paragraph)}</a:t></a:r></a:p>"
        for paragraph in paragraphs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:{root} xmlns:p="{_P}" xmlns:a="{_A}" xmlns:r="{_R}">'
        f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:{root}>"
    )


def _rels_xml(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>'
        for rid, kind, target in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_RELS}">{body}</Relationships>'
    )


def deck() -> bytes:
    """The talk's three slides as a minimal .pptx: the presentation part
    and its slide list, each slide's text runs, the second slide's
    speaker notes and its one picture."""
    numbers = range(1, len(SLIDES) + 1)
    overrides = [
        ("/ppt/presentation.xml", f"{_PML}.presentation.main+xml"),
        *((f"/ppt/slides/slide{n}.xml", f"{_PML}.slide+xml") for n in numbers),
        *(
            (f"/ppt/notesSlides/notesSlide{n}.xml", f"{_PML}.notesSlide+xml")
            for n, (_, notes, _) in zip(numbers, SLIDES, strict=True)
            if notes
        ),
    ]
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{_TYPES}">'
            '<Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            + "".join(
                f'<Override PartName="{name}" ContentType="{kind}"/>'
                for name, kind in overrides
            )
            + "</Types>"
        ),
        "_rels/.rels": _rels_xml(
            [("rId1", _DOCUMENT, "ppt/presentation.xml")]
        ),
        "ppt/presentation.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}"><p:sldIdLst>'
            + "".join(
                f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in numbers
            )
            + "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": _rels_xml(
            [(f"rId{n}", _SLIDE, f"slides/slide{n}.xml") for n in numbers]
        ),
    }
    for n, (paragraphs, notes, photograph) in zip(
        numbers, SLIDES, strict=True
    ):
        parts[f"ppt/slides/slide{n}.xml"] = _slide_xml(paragraphs)
        links = []
        if photograph:
            links.append(("rId2", _IMAGE, "../media/image1.png"))
        if notes:
            links.append(("rId3", _NOTES, f"../notesSlides/notesSlide{n}.xml"))
            parts[f"ppt/notesSlides/notesSlide{n}.xml"] = _slide_xml(
                notes, root="notes"
            )
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = _rels_xml(links)
    return _zip({**parts, "ppt/media/image1.png": PHOTOGRAPH})


def _zip(members: dict[str, str | bytes]) -> bytes:
    """A ZIP archive of `members`, in their order, each stored, with one
    fixed timestamp and mode."""
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            data = content.encode() if isinstance(content, str) else content
            archive.writestr(info, data)
    return buffer.getvalue()


# -- the corpus ---------------------------------------------------------------


def documents() -> dict[str, bytes]:
    """Every document of the corpus, by its file name, as the builder
    makes it."""
    return {
        NARRATIVE: (SOURCES["family-history-2008"].text + "\n").encode(),
        DECK: deck(),
        EXPORT: EXPORT_TEXT.encode(),
        CERTIFICATE: stand_in(
            SOURCES["naturalisation-1952"].text,
            SOURCES["naturalisation-1952"].title,
        ),
        LETTER: stand_in(
            SOURCES["postcard-1938"].text, SOURCES["postcard-1938"].title
        ),
    }


def build(folder: Path = FOLDER) -> list[Path]:
    """Write every document into `folder`; returns their paths."""
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in documents().items():
        path = folder / name
        path.write_bytes(content)
        written.append(path)
    return written


def main() -> int:
    for path in build():
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
