"""The text-layer module (#176): how each page of a PDF is read, the
images placed within its pages, and the bytes the vision engine and the
record of derived media are given.

Every PDF is fictional, written by tests/fixtures/pdfs.py from the
universe's texts (docs/EXAMPLES.md).
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs
from yizkr.pipeline import textlayer
from yizkr.pipeline.textlayer import (
    JPEG_SUFFIX,
    NONE,
    PNG_SUFFIX,
    REDACTED,
    TEXT_LAYER,
    VISION,
    clean,
    garbled,
    png,
    read_pages,
    union_area,
    usable,
)

SOURCES = {source.key: source for source in u.SOURCES}
EXTRACT = SOURCES["birth-extract-1938"].text
EXCERPT = SOURCES["letter-excerpt-1939"].text
HISTORY = SOURCES["family-history-2008"].text
TITLE = "Our Family from Germany. Compiled by Nicholas Lobig. Stupfield, 2011."
# an image in a filter PDFium does not know, so it cannot decode it
BROKEN = pdfs.Image(
    "/Width 100 /Height 100 /ColorSpace /DeviceGray /BitsPerComponent 8 "
    "/Filter /Fictional",
    b"no image stream PDFium can read",
)
CAPTION = "Wastl to Klara, July 1939."
TRANSLATION = (
    "Translation: Flörburg, Brunnenring 35, 28 July 1939. Dear Pünktchen! "
    "Gustl finally has his visa. Hilde is sewing him shirts for the "
    "journey. Rahel now lives at Angerring 71."
)
STAMP = "Stadtarchiv Flörburg, Standesamt 1938"
# a page of 600 by 800 points, whose shares of its area are whole numbers
SIDE, TALL = 600, 800


def read(tmp_path: Path, *pages: pdfs.Page, **options):
    """`read_pages` of a PDF of `pages`, with the files it writes in a
    folder of their own; `font` names the PDF's plain font."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "document.pdf"
    font = options.pop("font", "Helvetica")
    source.write_bytes(pdfs.pdf(list(pages), font=font))
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return read_pages(source, out, **options)


def methods(result) -> list[str]:
    return [page.method for page in result.pages]


def written(tmp_path: Path) -> list[str]:
    return sorted(path.name for path in (tmp_path / "out").iterdir())


def typed(*paragraphs: str, **options) -> pdfs.Page:
    return pdfs.Page().text(pdfs.lines(*paragraphs), **options)


def scan(*, ocr: str | None = None) -> pdfs.Page:
    """A scanned page: one JPEG covering the page, with an OCR layer drawn
    invisibly over it when `ocr` is given."""
    page = pdfs.Page().image(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
    if ocr is not None:
        page.text(pdfs.lines(ocr), mode=pdfs.INVISIBLE)
    return page


def contents(data: bytes) -> textlayer._Contents:
    """What the first page of the PDF `data` draws, as `read_pages` reads
    it (under the lock PDFium is read under)."""
    with textlayer._LOCK:
        textlayer.fresh_start()
        document = pdfium.PdfDocument(data)
        try:
            page = document[0]
            try:
                return textlayer._contents(page)
            finally:
                page.close()
        finally:
            document.close()


class TestUsable:
    def test_text_is_usable(self):
        assert usable(TITLE)
        assert usable("3")

    @pytest.mark.parametrize(
        "text", ["", "   \n\t ", "... — ;", "\ue047\ue061\ue072\ue062"]
    )
    def test_no_letter_or_digit_is_not(self, text):
        assert not usable(text)

    def test_a_tenth_unreadable_is_the_most_it_takes(self):
        # nine letters and one replacement character: a tenth
        assert usable("Hutmacher\ufffd")
        assert not usable("Hutmache\ufffd\ufffd")
        # private-use, unassigned and control characters count alike;
        # whitespace counts for nothing
        assert not usable("Gustl \ue000\U000e0080")
        assert not usable("Rahel\x01\x02")
        assert usable("Rahel    \n\n\t  Angerring")

    def test_the_replacement_character_within_a_word_is_garbled(self):
        assert garbled("Fl\ufffdrburg")
        assert garbled("Brunnenring 35\ufffd")
        assert not garbled("Flörburg \ufffd Brunnenring 35")
        assert not garbled(TITLE)

    def test_code_units_become_characters(self):
        """A surrogate pair is one character; a lone surrogate, or a code
        beyond Unicode, is the replacement character; a NUL is a control
        character, never the mark of a character the page hides."""
        decode = textlayer._decode
        assert decode([0x47, 0xD83D, 0xDCDC, 0x6C]) == "G\U0001f4dcl"
        assert decode([0x47, 0xD83D, 0x6C]) == "G\ufffdl"
        assert decode([0xDCDC, 0x110000]) == "\ufffd\ufffd"
        assert decode([0x47, 0, 0x6C]) == "G\x01l"
        assert decode([0x47, textlayer._HIDDEN_CODE, 0x6C]) == "G\x00l"

    def test_clean_keeps_the_text_and_its_lines(self):
        raw = "\r\nFlörburg,  \r\n\r\n\r\n\x00Brunnenring 35\t\r\n\r\n"
        assert clean(raw) == "Flörburg,\n\nBrunnenring 35"


class TestPng:
    """An image's bytes are its identity (ADR-0008): the same pixels always
    give the same PNG, which the tests hold byte for byte."""

    GREY = bytes(range(0, 240, 20)) * 2  # 12 by 2, one byte a pixel

    def test_the_encoding_is_held_byte_for_byte(self):
        data = png(12, 2, 1, self.GREY)
        assert hashlib.sha1(data).hexdigest() == (
            "81fc97f44f1fad62c90731202bcf5aa0c618a6c2"
        )
        rgb = png(2, 2, 3, bytes(range(12)))
        assert hashlib.sha1(rgb).hexdigest() == (
            "d286b97893304f986b27d4b30266d3794cc9b001"
        )

    def test_a_png_holds_its_pixels(self):
        data = png(12, 2, 1, self.GREY)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        length, tag = struct.unpack(">I4s", data[8:16])
        width, height, depth, kind = struct.unpack(">IIBB", data[16:26])
        assert (tag, width, height, depth, kind) == (b"IHDR", 12, 2, 8, 0)
        at = 8 + 12 + length
        length, tag = struct.unpack(">I4s", data[at : at + 8])
        assert tag == b"IDAT"
        rows = zlib.decompress(data[at + 8 : at + 8 + length])
        assert rows == b"\x00" + self.GREY[:12] + b"\x00" + self.GREY[12:]
        assert data.endswith(b"IEND\xaeB`\x82")

    def test_pixels_that_do_not_fill_the_image_are_refused(self):
        with pytest.raises(ValueError):
            png(12, 3, 1, self.GREY)


class TestPages:
    def test_a_typed_page_reads_from_its_text_layer(self, tmp_path):
        result = read(tmp_path, typed(TITLE))
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == TITLE
        assert result.by_own_text

    def test_a_scan_with_an_ocr_layer_is_read_by_vision(self, tmp_path):
        """An OCR layer is drawn invisibly: it is a machine's reading, not
        the page's text, and the page is a scan."""
        result = read(tmp_path, scan(ocr=EXTRACT))
        assert methods(result) == [VISION]
        assert result.pages[0].text == ""
        # a PDF with no page read from its text layer is read whole, and
        # nothing is written for it
        assert not result.by_own_text
        assert written(tmp_path) == []

    def test_text_in_render_mode_7_is_no_part_of_the_text_layer(
        self, tmp_path
    ):
        page = typed(TITLE).text([STAMP], y=500, mode=pdfs.CLIPPING)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == TITLE

    def test_text_outside_the_page_is_no_part_of_the_text_layer(
        self, tmp_path
    ):
        page = typed(TITLE).text([STAMP], x=pdfs.WIDTH + 50, y=500)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == TITLE

    @pytest.mark.parametrize(
        "lines",
        [
            ["Flörburg, Brunnen-", "ring 35, den 28. Juli 1939."],
            ["S. Faxridge-", "van 't Nijks"],
        ],
        ids=["a word", "a double-barrelled name"],
    )
    def test_a_hyphen_at_a_line_end_stays(self, tmp_path, lines):
        """PDFium marks a hyphen that ends a line and drops the line break
        after it: the text layer gives both back, as the page prints them,
        and joins no word."""
        result = read(tmp_path, pdfs.Page().text(lines))
        assert result.pages[0].text == "\n".join(lines)

    def test_a_stamp_over_a_scan_leaves_it_a_scan(self, tmp_path):
        """Images cover the page, and more of its visible letters lie over
        them than beside them: a scan (as the owner decided)."""
        page = scan().text([STAMP], y=20)
        assert methods(read(tmp_path, page)) == [VISION]

    def test_typed_text_over_a_picture_that_fills_the_page_is_a_scan(
        self, tmp_path
    ):
        """However much of it there is, text laid over a picture that fills
        the page is read by vision, as the owner decided, and no image is
        read on its own."""
        page = pdfs.Page().image(pdfs.gray(300, 400), 0, 0, 595, 842)
        page.text(pdfs.lines(TRANSLATION, HISTORY))
        result = read(tmp_path, page)
        assert methods(result) == [VISION]
        assert result.images == {}

    def test_text_beside_a_large_image_reads_its_text_layer(self, tmp_path):
        """An excerpt that fills most of the page, with its caption beside
        it: a typed page, and the image a witness of its own, as the owner
        decided."""
        page = pdfs.Page(width=SIDE, height=TALL)
        page.image(pdfs.gray(300, 200), 0, 200, SIDE, 600)
        page.text([CAPTION], y=100)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == CAPTION
        assert len(result.images) == 1

    @pytest.mark.parametrize(
        "height, method", [(480, VISION), (479, TEXT_LAYER)]
    )
    def test_the_boundary_of_a_scan(self, tmp_path, height, method):
        """Images covering exactly SCAN_COVER of the page, with its text
        over them, make a scan; a point less does not."""
        page = pdfs.Page(width=SIDE, height=TALL)
        page.image(pdfs.gray(300, 200), 0, TALL - height, SIDE, height)
        page.text([STAMP], y=TALL - 40)
        assert methods(read(tmp_path, page)) == [method]

    @pytest.mark.parametrize(
        "beside, method", [("Angerring", TEXT_LAYER), ("Flörburg", VISION)]
    )
    def test_the_boundary_of_text_over_images(self, tmp_path, beside, method):
        """A covered page is a scan when more of its letters and digits lie
        over its images than beside them: as many beside, and it reads
        from its text layer."""
        page = pdfs.Page(width=SIDE, height=TALL)
        page.image(pdfs.gray(300, 200), 0, 200, SIDE, 600)
        page.text(["Angerring"], y=700)
        page.text([beside], y=100)
        assert methods(read(tmp_path, page)) == [method]

    def test_punctuation_does_not_count_beside_a_scan(self, tmp_path):
        """Only letters and digits are weighed, over the images and beside
        them: a row of dots beside a scan does not outweigh its stamp."""
        page = pdfs.Page(width=SIDE, height=TALL)
        page.image(pdfs.gray(300, 200), 0, 200, SIDE, 600)
        page.text(["Standesamt 1938"], y=700)
        page.text(["." * 70], y=100)
        assert methods(read(tmp_path, page)) == [VISION]

    def test_text_a_shading_paints_through_is_read_by_vision(self, tmp_path):
        """Text drawn as a clip (render mode 7) shows only as the shading
        painted through it: the text layer holds none of it, and the page
        is read from a rendering, never taken for one that draws nothing."""
        page = pdfs.Page().text(["Klara Wastl"], size=40, mode=pdfs.CLIPPING)
        page.shade(0, 0, pdfs.WIDTH, pdfs.HEIGHT)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]

    def test_a_garbage_text_layer_is_read_by_vision(self, tmp_path):
        """A font whose codes extract as private-use characters: the text
        layer is garbage, and the page is read from a rendering."""
        page = typed(HISTORY, font=pdfs.GARBLED)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].text == ""
        assert result.pages[1].vision.name == "page-2.png"

    def test_a_replacement_character_within_a_word_is_read_by_vision(
        self, tmp_path
    ):
        """One letter its font cannot map, in a text layer otherwise usable:
        the page is read by vision (as the owner decided)."""
        page = typed(TITLE).text(["Flörburg, Brunnenring 35"], y=500)
        garbling = page.text(["Flörburg"], y=480, font=pdfs.REPLACED)
        result = read(tmp_path, typed(TITLE), garbling)
        assert methods(result) == [TEXT_LAYER, VISION]

    def test_a_replacement_character_alone_leaves_a_text_layer(self, tmp_path):
        page = typed(TITLE, HISTORY).text(["\xf6"], y=100, font=pdfs.REPLACED)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text.endswith("\ufffd")

    def test_a_page_with_no_text_layer_is_read_by_vision(self, tmp_path):
        """A sole inset image represents the page, which still needs vision;
        blank margins do not create a second independent witness."""
        photo = pdfs.Page().image(pdfs.gray(200, 150), 100, 300, 400, 300)
        result = read(tmp_path, typed(TITLE), photo)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].images == []
        assert result.sources.sources == {}
        assert result.images == {}
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    def test_a_page_that_draws_only_paths_is_read_by_vision(self, tmp_path):
        """Text drawn as outlines is paths, and no text layer: the page is
        not empty, and only vision can read it."""
        drawing = pdfs.Page().rectangle(72, 400, 300, 200)
        result = read(tmp_path, typed(TITLE), drawing)
        assert methods(result) == [TEXT_LAYER, VISION]

    def test_a_page_that_draws_nothing_is_not_read(self, tmp_path):
        result = read(tmp_path, typed(TITLE), pdfs.Page())
        assert methods(result) == [TEXT_LAYER, NONE]
        assert result.pages[1].vision is None

    def test_a_page_whose_only_annotation_is_a_link_is_not_read(
        self, tmp_path
    ):
        linked = pdfs.Page().annotate("/Subtype /Link /Rect [72 400 300 440]")
        result = read(tmp_path, typed(TITLE), linked)
        assert methods(result) == [TEXT_LAYER, NONE]

    def test_a_page_that_shows_only_an_annotation_is_read(self, tmp_path):
        """By vision, whose rendering draws the annotation: none is left
        unread."""
        noted = pdfs.Page().annotate("/Subtype /Square /Rect [72 400 300 440]")
        result = read(tmp_path, typed(TITLE), noted)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].annotations == 0

    def test_a_pdf_pdfium_cannot_read_is_read_whole(self, tmp_path):
        source = tmp_path / "damaged.pdf"
        source.write_bytes(b"%PDF-1.4 not a PDF after all")
        assert read_pages(source, tmp_path) is None


class TestAnnotations:
    """Text a page shows in an annotation is no part of its text layer: a
    page that shows some is read by vision, as the owner decided. Other
    annotations are left unread, and the page counts them."""

    def test_a_free_text_annotation_sends_its_page_to_vision(self, tmp_path):
        page = typed(TITLE).annotate(
            "/Subtype /FreeText /Rect [72 500 300 540] "
            "/Contents (Rahel wohnt jetzt Angerring 11) /DA (/Helv 12 Tf 0 g)"
        )
        assert methods(read(tmp_path, typed(TITLE), page)) == [
            TEXT_LAYER,
            VISION,
        ]

    def test_a_form_field_holding_text_sends_its_page_to_vision(
        self, tmp_path
    ):
        page = typed(TITLE).field("name", "Gustav Löbig", (72, 500, 300, 540))
        assert methods(read(tmp_path, typed(TITLE), page)) == [
            TEXT_LAYER,
            VISION,
        ]

    @pytest.mark.parametrize(
        "flags", [1 << 17, 0], ids=["combo box", "list box"]
    )
    def test_a_choice_field_holding_text_sends_its_page_to_vision(
        self, tmp_path, flags
    ):
        page = typed(TITLE).field(
            "name", "Klara", (72, 500, 300, 540), kind="/Ch", flags=flags
        )
        assert methods(read(tmp_path, typed(TITLE), page)) == [
            TEXT_LAYER,
            VISION,
        ]

    @pytest.mark.parametrize(
        "annotate",
        [
            lambda page: page.field("name", "", (72, 500, 300, 540)),
            lambda page: page.field(
                "box", "/Yes", (72, 500, 90, 518), kind="/Btn"
            ),
            lambda page: page.annotate(
                "/Subtype /Stamp /Rect [72 500 300 540]"
            ),
        ],
        ids=["empty field", "check box", "stamp"],
    )
    def test_other_annotations_are_counted_and_left_unread(
        self, tmp_path, annotate
    ):
        page = annotate(typed(TITLE))
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].annotations == 1

    def test_a_link_is_not_counted(self, tmp_path):
        page = typed(TITLE).annotate("/Subtype /Link /Rect [72 500 300 540]")
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].annotations == 0


class TestHiddenText:
    """Text a page hides is never read, as the owner decided: a character
    drawn in a visible mode that an image, a filled path or an annotation
    painted after it covers sends its page to vision, which reads what the
    page shows."""

    PARAGRAPH = pdfs.lines(HISTORY)[:6]

    def hiding(self, cover) -> pdfs.Page:
        """A typed paragraph, then `cover` painted after it."""
        page = pdfs.Page().text(self.PARAGRAPH)
        cover(page)
        return page

    @pytest.mark.parametrize(
        "cover",
        [
            # a photograph cropped to below the paragraph, whose uncropped
            # extent reaches over it
            lambda page: page.image(
                pdfs.gray(300, 200),
                72,
                500,
                400,
                300,
                clip=(72, 500, 400, 100),
            ),
            # a stamp its soft mask shows nothing of
            lambda page: page.image(
                pdfs.transparent(pdfs.gray(200, 100)), 60, 690, 480, 100
            ),
            # a stencil mask
            lambda page: page.image(pdfs.stencil(200, 100), 60, 690, 480, 100),
            # a picture laid over the paragraph at 30% opacity
            lambda page: page.image(
                pdfs.gray(200, 100), 60, 690, 480, 100, alpha=0.3
            ),
            # a black box drawn over one line, to redact it
            lambda page: page.rectangle(60, 755, 480, 20, gray=0),
            # a black box a viewer draws over it, as an annotation
            lambda page: page.annotate(
                "/Subtype /Square /Rect [60 755 540 775] /IC [0 0 0]"
            ),
            # the same, its rectangle given by its other two corners
            lambda page: page.annotate(
                "/Subtype /Square /Rect [540 775 60 755] /IC [0 0 0]"
            ),
            # ink scribbled over it
            lambda page: page.annotate(
                "/Subtype /Ink /Rect [60 755 540 775] "
                "/InkList [[60 765 540 765]] /BS << /W 20 >>"
            ),
            # a redaction marked and not yet applied
            lambda page: page.annotate(
                "/Subtype /Redact /Rect [60 755 540 775]"
            ),
        ],
        ids=[
            "cropped photo",
            "transparent stamp",
            "stencil mask",
            "alpha overlay",
            "path redaction",
            "square annotation",
            "square annotation, corners reversed",
            "ink annotation",
            "redact annotation",
        ],
    )
    def test_its_page_is_read_by_vision(self, tmp_path, cover):
        result = read(tmp_path, typed(TITLE), self.hiding(cover))
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].text == ""
        # and its text layer, which no transcription is given, marks what
        # the page hides rather than leaving it out
        read_back = contents(pdfs.pdf([self.hiding(cover)]))
        assert read_back.text.hidden > 0
        assert REDACTED in read_back.text.text

    def test_a_filled_path_painted_before_the_text_hides_nothing(
        self, tmp_path
    ):
        """A shaded box behind a paragraph, as a table cell's is."""
        page = pdfs.Page().rectangle(60, 740, 480, 40, gray=0.9)
        page.text(self.PARAGRAPH)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == "\n".join(self.PARAGRAPH)

    @pytest.mark.parametrize(
        "markup", ["Highlight", "Underline", "Squiggly", "StrikeOut"]
    )
    def test_a_text_markup_leaves_the_text_in_view(self, tmp_path, markup):
        """A highlight, an underline, a squiggle or a strike-out marks the
        text it lies over and leaves it in view: the page reads from its
        text layer, and counts the annotation."""
        page = self.hiding(
            lambda page: page.annotate(
                f"/Subtype /{markup} /Rect [60 755 540 775] "
                "/QuadPoints [60 775 540 775 60 755 540 755]"
            )
        )
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == "\n".join(self.PARAGRAPH)
        assert result.pages[0].annotations == 1

    def test_a_thick_underline_painted_after_the_text_hides_nothing(
        self, tmp_path
    ):
        """It covers the foot of each letter, never its centre: a
        character is hidden only where its centre is covered."""
        page = pdfs.Page().text(["Klara Wastl"])
        page.rectangle(60, 765, 480, 6.5, gray=0)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == "Klara Wastl"

    def test_a_frame_drawn_round_the_text_hides_nothing(self, tmp_path):
        """A box stroked round a paragraph after it, as a table's borders
        are: its bounds hold the text, which it leaves in view."""
        page = pdfs.Page().text(self.PARAGRAPH).frame(60, 700, 480, 85)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == "\n".join(self.PARAGRAPH)

    def test_a_box_over_a_space_alone_hides_nothing(self, tmp_path):
        """A box painted over the space between two words, and over no
        letter: no text is hidden, and the space stays."""
        page = pdfs.Page().text(["Klara Wastl", "Angerring 11"])
        page.rectangle(95.5, 768, 2.5, 4, gray=0)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.pages[0].text == "Klara Wastl\nAngerring 11"

    def test_a_text_layer_marks_what_it_hides(self):
        """Were a text layer ever given for a page that hides text, each
        run of hidden characters would stand as REDACTED, never silently
        left out: the redacted line is one mark, the lines around it are
        the page's."""
        page = pdfs.Page().text(["Flörburg, Brunnenring 35", "Angerring 11"])
        page.rectangle(60, 766, 480, 14, gray=0)
        read_back = contents(pdfs.pdf([page]))
        assert read_back.text.hidden == len("Flörburg,Brunnenring35")
        assert read_back.text.text.split() == [REDACTED, "Angerring", "11"]


class TestWhatVisionReads:
    def test_a_scanned_page_gives_its_image_as_held(self, tmp_path):
        result = read(tmp_path, typed(TITLE), scan(ocr=EXTRACT))
        vision = result.pages[1].vision
        assert vision.name == "page-2.jpg"
        assert vision.read_bytes() == pdfs.JPEG

    def test_a_page_that_draws_more_is_rendered(self, tmp_path):
        page = scan().text([STAMP], y=20)
        result = read(tmp_path, typed(TITLE), page)
        vision = result.pages[1].vision
        assert vision.name == "page-2.png"
        data = vision.read_bytes()
        width, height = struct.unpack(">II", data[16:24])
        assert max(width, height) == textlayer.RENDER_LONG_EDGE

    def test_the_long_edge_of_a_rendering_is_held(self):
        """A change to it changes every rendered page's bytes, and so the
        key of every call that reads one: it is made on purpose."""
        assert textlayer.RENDER_LONG_EDGE == 1600

    @pytest.mark.parametrize(
        "change",
        [
            lambda page: setattr(page, "rotate", 90),
            lambda page: page.annotate(
                "/Subtype /Square /Rect [72 400 300 440]"
            ),
        ],
        ids=["rotated", "annotated"],
    )
    def test_a_rotated_or_annotated_scan_is_rendered(self, tmp_path, change):
        page = scan()
        change(page)
        result = read(tmp_path, typed(TITLE), page)
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    def test_a_scan_placed_through_a_form_is_rendered(self, tmp_path):
        """A form's own matrix and bounding box can turn or cut what it
        holds, which the image's placement within it does not show: the
        page is rendered, never given as the image the form holds."""
        page = pdfs.Page().form_image(
            pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT
        )
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    def test_a_mirrored_scan_is_rendered(self, tmp_path):
        page = pdfs.Page().image(
            pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT, mirrored=True
        )
        result = read(tmp_path, typed(TITLE), page)
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    @pytest.mark.parametrize(
        "height, suffix", [(720, JPEG_SUFFIX), (719, PNG_SUFFIX)]
    )
    def test_the_boundary_of_a_page_s_one_image(
        self, tmp_path, height, suffix
    ):
        """A page's one image covering exactly BACKGROUND_COVER of it is
        given as held; a point less, and the page is rendered."""
        page = pdfs.Page(width=SIDE, height=TALL)
        page.image(pdfs.jpeg(), 0, 0, SIDE, height)
        result = read(tmp_path, typed(TITLE), page)
        assert result.pages[1].vision.suffix == suffix

    def test_an_image_partly_off_the_page_counts_for_its_part_on_it(
        self, tmp_path
    ):
        """The page's one image, placed mostly beyond its edge: what falls
        on the page covers too little of it to stand for the page, which
        is rendered."""
        page = pdfs.Page().image(pdfs.jpeg(), 400, 0, 1000, pdfs.HEIGHT)
        result = read(tmp_path, typed(TITLE), page)
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    def test_a_scan_pdfium_cannot_decode_is_rendered(self, tmp_path):
        """A page's one image in a filter PDFium does not know: it cannot
        be given as held, and the page is rendered instead."""
        page = pdfs.Page().image(BROKEN, 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].vision.name == "page-2.png"

    def test_a_rendering_is_the_same_every_time(self, tmp_path):
        page = scan().text([STAMP], y=20)
        first = read(tmp_path / "first", typed(TITLE), page)
        second = read(tmp_path / "second", typed(TITLE), page)
        assert (
            first.pages[1].vision.read_bytes()
            == second.pages[1].vision.read_bytes()
        )

    def redacted(self, tmp_path: Path, font: str) -> bytes:
        """The rendering of a page of text in the non-embedded `font`, with
        a line redacted, so that vision reads it."""
        page = typed(TRANSLATION).rectangle(60, 755, 480, 20, gray=0)
        result = read(tmp_path / font, typed(TITLE), page, font=font)
        return result.pages[1].vision.read_bytes()

    def test_a_font_no_pdf_embeds_renders_alike_on_every_host(self, tmp_path):
        """PDFium draws a font the PDF does not embed with a built-in one,
        never with a font the host has installed: a family many hosts have
        renders as one none has, and the bytes are held."""
        installed = self.redacted(tmp_path, "Ubuntu")
        nowhere = self.redacted(tmp_path, "Fictional")
        assert installed == nowhere
        assert hashlib.sha1(installed).hexdigest() == (
            "91597b0d27e26af7aec5094ee5b310060438d2c8"
        )


class TestImages:
    def booklet_page(self, image: pdfs.Image) -> pdfs.Page:
        page = pdfs.Page().image(image, 72, 480, 300, 200)
        page.text(pdfs.lines(CAPTION, TRANSLATION), y=460)
        return page

    def test_an_image_placed_within_a_page_is_read_on_its_own(self, tmp_path):
        image = pdfs.gray(300, 120)
        result = read(tmp_path, typed(TITLE), self.booklet_page(image))
        assert methods(result) == [TEXT_LAYER, TEXT_LAYER]
        (placed,) = result.images.values()
        data = placed.path.read_bytes()
        assert placed.suffix == PNG_SUFFIX
        assert placed.sha1 == hashlib.sha1(data).hexdigest()
        assert placed.path.name == f"{placed.sha1}.png"
        assert placed.places == [(2, 1)]
        assert result.pages[1].images == [(1, placed.sha1)]
        # the page's text layer is its own text, the caption and the
        # translation, and nothing of the image
        assert result.pages[1].text == "\n".join(
            pdfs.lines(CAPTION, TRANSLATION)
        )

    def test_its_png_holds_its_pixels(self, tmp_path):
        image = pdfs.gray(300, 120)
        result = read(tmp_path, self.booklet_page(image))
        (placed,) = result.images.values()
        assert placed.path.read_bytes() == png(300, 120, 1, image.data)

    @pytest.mark.parametrize("flated", [False, True], ids=["as is", "flated"])
    def test_a_jpeg_is_kept_as_the_jpeg(self, tmp_path, flated):
        """A JPEG the PDF holds, even compressed again by a lossless
        filter, is kept as the JPEG file."""
        page = pdfs.Page().image(pdfs.jpeg(flated=flated), 72, 480, 256, 192)
        page.text(pdfs.lines(CAPTION), y=460)
        result = read(tmp_path, page)
        (placed,) = result.images.values()
        assert placed.suffix == JPEG_SUFFIX
        assert placed.path.read_bytes() == pdfs.JPEG

    def test_an_rgb_image_is_written_as_rgb(self, tmp_path):
        image = pdfs.rgb(150, 100)
        result = read(tmp_path, self.booklet_page(image))
        (placed,) = result.images.values()
        assert placed.path.read_bytes() == png(150, 100, 3, image.data)

    def test_one_image_placed_twice_is_one_image(self, tmp_path):
        # Complete original JPEG bytes prove shared media across these
        # distinct PDF image objects; equal decoded PNGs would not.
        image = pdfs.jpeg()
        twice = self.booklet_page(image).image(image, 72, 100, 150, 60)
        result = read(tmp_path, twice, typed(TITLE), self.booklet_page(image))
        (placed,) = result.images.values()
        assert placed.places == [(1, 1), (1, 2), (3, 1)]
        assert placed.pages == [1, 3]
        assert [page.images for page in result.pages] == [
            [(1, placed.sha1), (2, placed.sha1)],
            [],
            [(1, placed.sha1)],
        ]

    def test_decoration_is_not_read(self, tmp_path):
        """An image under MIN_IMAGE_SIDE pixels on a side, or under
        MIN_IMAGE_PIXELS in all, could hold no more than a word."""
        page = self.booklet_page(pdfs.gray(300, 120))
        page.image(pdfs.gray(20, 600, shade=1), 500, 100, 20, 300)
        page.image(pdfs.gray(99, 100, shade=2), 420, 50, 99, 100)
        kept = pdfs.gray(100, 100, shade=3)
        page.image(kept, 72, 50, 100, 100)
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert len(result.images) == 2
        assert [place for place, _sha1 in result.pages[0].images] == [1, 2]
        assert hashlib.sha1(png(100, 100, 1, kept.data)).hexdigest() in (
            result.images
        )

    @pytest.mark.parametrize("width, read_on_its_own", [(24, 1), (23, 0)])
    def test_the_boundary_of_decoration(
        self, tmp_path, width, read_on_its_own
    ):
        """An image MIN_IMAGE_SIDE pixels wide (and over MIN_IMAGE_PIXELS in
        all) is read; a pixel narrower, it is decoration."""
        page = typed(TITLE).image(pdfs.gray(width, 500), 400, 100, 20, 400)
        assert len(read(tmp_path, page).images) == read_on_its_own

    def test_an_image_in_a_form_is_placed_where_the_form_puts_it(
        self, tmp_path
    ):
        """A form XObject scaled over the whole page puts its image over
        the whole page, over the text painted before it: that page is read
        by vision, and its image is not read on its own."""
        within = typed(TITLE).form_image(
            pdfs.rgb(150, 100), 100, 300, 240, 160
        )
        covering = typed(TITLE).form_image(
            pdfs.rgb(150, 100), 0, 0, pdfs.WIDTH, pdfs.HEIGHT
        )
        assert len(read(tmp_path / "within", within).images) == 1
        assert read(tmp_path / "covering", covering).images == {}

    def test_an_image_the_never_ingest_list_names_is_left_out(self, tmp_path):
        image = pdfs.gray(300, 120)
        sha1 = hashlib.sha1(png(300, 120, 1, image.data)).hexdigest()
        result = read(
            tmp_path,
            self.booklet_page(image),
            excluded=lambda named: named == sha1,
        )
        assert result.images == {}
        assert result.pages[0].images == []
        assert written(tmp_path) == []

    def test_an_image_pdfium_cannot_decode_is_noted(self, tmp_path):
        page = self.booklet_page(BROKEN)
        result = read(tmp_path, page)
        assert result.images == {}
        assert result.pages[0].images == []
        assert any("PDFium" in reason for reason in result.notes)

    def test_inner_sources_are_read_independently_of_the_page_route(
        self, tmp_path
    ):
        page = scan().image(pdfs.gray(300, 120), 72, 480, 300, 200)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert len(result.images) == 1
        assert result.pages[1].images[0][1] in result.sources.sources


class TestUnionArea:
    def test_overlaps_count_once(self):
        assert union_area([(0, 0, 2, 2), (1, 1, 3, 3)]) == 7
        assert union_area([(0, 0, 1, 1), (2, 2, 3, 3)]) == 2
        assert union_area([]) == 0

    def test_strips_cover_a_page_together(self, tmp_path):
        """A scan stored as strips covers the page as one image does: a
        stamp over them leaves the page a scan."""
        page = pdfs.Page()
        for strip in range(4):
            page.image(
                pdfs.gray(100, 50, shade=strip),
                0,
                strip * pdfs.HEIGHT / 4,
                pdfs.WIDTH,
                pdfs.HEIGHT / 4,
            )
        page.text([STAMP], y=20)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].vision.suffix == PNG_SUFFIX
