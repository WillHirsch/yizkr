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

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs
from yizkr.pipeline import textlayer
from yizkr.pipeline.textlayer import (
    BLANK,
    JPEG_SUFFIX,
    MIN_TEXT_OVER_IMAGES,
    PNG_SUFFIX,
    TEXT_LAYER,
    VISION,
    clean,
    png,
    read_pages,
    union_area,
    usable,
)

SOURCES = {source.key: source for source in u.SOURCES}
EXTRACT = SOURCES["birth-extract-1938"].text
EXCERPT = SOURCES["letter-excerpt-1939"].text
TITLE = "Our Family from Germany. Compiled by Nicholas Lobig. Stupfield, 2011."
CAPTION = "Wastl to Klara, July 1939."
TRANSLATION = (
    "Translation: Flörburg, Brunnenring 35, 28 July 1939. Dear Pünktchen! "
    "Gustl finally has his visa. Hilde is sewing him shirts for the "
    "journey. Rahel now lives at Angerring 71."
)


def read(tmp_path: Path, *pages: pdfs.Page, **options):
    """`read_pages` of a PDF of `pages`, with the files it writes in a
    folder of their own."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "document.pdf"
    source.write_bytes(pdfs.pdf(list(pages)))
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
            "74b016d9ba49ee74899ce28f2f03b363883cfb4d"
        )
        rgb = png(2, 2, 3, bytes(range(12)))
        assert hashlib.sha1(rgb).hexdigest() == (
            "6de4d6e82dc6cd947fef062da6ae210d5a4cc1c6"
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
        assert result.by_text_layer

    def test_a_scan_with_an_ocr_layer_is_read_by_vision(self, tmp_path):
        """An OCR layer is drawn invisibly: it is a machine's reading, not
        the page's text, and the page is a scan."""
        result = read(tmp_path, scan(ocr=EXTRACT))
        assert methods(result) == [VISION]
        assert result.pages[0].text == ""
        # a PDF with no page read from its text layer is read whole, and
        # nothing is written for it
        assert not result.by_text_layer
        assert written(tmp_path) == []

    def test_text_painted_over_by_a_scan_is_not_its_text_layer(self, tmp_path):
        """An OCR layer drawn in a visible mode, but under the scan that
        covers it, cannot be seen either."""
        page = typed(EXTRACT)
        page.image(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
        result = read(tmp_path, page)
        assert methods(result) == [VISION]

    def test_text_outside_the_page_is_not_its_text_layer(self, tmp_path):
        page = pdfs.Page()
        page.text(pdfs.lines(EXTRACT), x=pdfs.WIDTH + 50)
        page.image(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
        assert methods(read(tmp_path, page)) == [VISION]

    def test_a_stamp_over_a_scan_leaves_it_a_scan(self, tmp_path):
        """A covered page is read from its text layer only when it draws
        at least MIN_TEXT_OVER_IMAGES letters and digits visibly."""
        stamp = "Stadtarchiv Flörburg, Standesamt 1938"
        page = scan().text([stamp], y=20)
        assert methods(read(tmp_path, page)) == [VISION]

    def test_a_typed_page_over_a_background_reads_its_text_layer(
        self, tmp_path
    ):
        """A born-digital page over a picture that fills it: enough
        visible text makes it a text-layer page, and the picture is its
        background, not an image read on its own."""
        page = pdfs.Page().image(pdfs.gray(300, 400), 0, 0, 595, 842)
        page.text(pdfs.lines(TRANSLATION, SOURCES["family-history-2008"].text))
        result = read(tmp_path, page)
        assert methods(result) == [TEXT_LAYER]
        assert result.images == {}

    def test_the_boundary_of_text_over_images(self, tmp_path):
        """MIN_TEXT_OVER_IMAGES letters and digits over a covered page read
        from its text layer; one fewer does not."""
        words = "Hutmacher " * (MIN_TEXT_OVER_IMAGES // 9 + 1)

        def over_scan(letters: int) -> pdfs.Page:
            kept, count = [], 0
            for word in words.split():
                if count + len(word) > letters:
                    word = word[: letters - count]
                if word:
                    kept.append(word)
                    count += len(word)
            return scan().text(pdfs.lines(" ".join(kept)))

        at = read(tmp_path / "at", over_scan(MIN_TEXT_OVER_IMAGES))
        below = read(tmp_path / "below", over_scan(MIN_TEXT_OVER_IMAGES - 1))
        assert methods(at) == [TEXT_LAYER]
        assert methods(below) == [VISION]

    def test_a_garbled_text_layer_is_read_by_vision(self, tmp_path):
        """A font whose codes extract as private-use characters: the text
        layer is garbage, and the page is read from a rendering."""
        page = typed(SOURCES["family-history-2008"].text, font=pdfs.GARBLED)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].text == ""
        assert result.pages[1].vision.name == "page-2.png"

    def test_a_page_with_no_text_layer_is_read_by_vision(self, tmp_path):
        """A page that draws a picture and no text at all, with room
        around it: nothing on it can be read but by vision, which reads it
        whole, as the compilation's page."""
        photo = pdfs.Page().image(pdfs.gray(200, 150), 100, 300, 400, 300)
        result = read(tmp_path, typed(TITLE), photo)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].images == []
        assert result.images == {}

    def test_a_blank_page_is_not_read(self, tmp_path):
        result = read(tmp_path, typed(TITLE), pdfs.Page())
        assert methods(result) == [TEXT_LAYER, BLANK]
        assert result.pages[1].vision is None

    def test_a_pdf_pdfium_cannot_read_is_read_whole(self, tmp_path):
        source = tmp_path / "damaged.pdf"
        source.write_bytes(b"%PDF-1.4 not a PDF after all")
        assert read_pages(source, tmp_path) is None


class TestWhatVisionReads:
    def test_a_scanned_page_gives_its_image_as_held(self, tmp_path):
        result = read(tmp_path, typed(TITLE), scan(ocr=EXTRACT))
        vision = result.pages[1].vision
        assert vision.name == "page-2.jpg"
        assert vision.read_bytes() == pdfs.JPEG

    def test_a_page_that_draws_more_is_rendered(self, tmp_path):
        page = scan().text(["Stadtarchiv Flörburg"], y=20)
        result = read(tmp_path, typed(TITLE), page)
        vision = result.pages[1].vision
        assert vision.name == "page-2.png"
        data = vision.read_bytes()
        width, height = struct.unpack(">II", data[16:24])
        assert max(width, height) == textlayer.RENDER_LONG_EDGE

    def test_a_rotated_scan_is_rendered(self, tmp_path):
        page = scan()
        page.rotate = 90
        result = read(tmp_path, typed(TITLE), page)
        assert result.pages[1].vision.suffix == PNG_SUFFIX

    def test_a_rendering_is_the_same_every_time(self, tmp_path):
        page = scan().text(["Stadtarchiv Flörburg"], y=20)
        first = read(tmp_path / "first", typed(TITLE), page)
        second = read(tmp_path / "second", typed(TITLE), page)
        assert (
            first.pages[1].vision.read_bytes()
            == second.pages[1].vision.read_bytes()
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

    def test_a_jpeg_is_kept_as_the_pdf_holds_it(self, tmp_path):
        page = pdfs.Page().image(pdfs.jpeg(), 72, 480, 256, 192)
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
        image = pdfs.gray(300, 120)
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
        page.image(pdfs.gray(20, 400, shade=1), 400, 100, 20, 400)
        page.image(pdfs.gray(99, 100, shade=2), 400, 50, 99, 100)
        kept = pdfs.gray(100, 100, shade=3)
        page.image(kept, 72, 50, 100, 100)
        result = read(tmp_path, page)
        assert len(result.images) == 2
        assert [place for place, _sha1 in result.pages[0].images] == [1, 2]
        assert hashlib.sha1(png(100, 100, 1, kept.data)).hexdigest() in (
            result.images
        )

    def test_an_image_in_a_form_is_placed_where_the_form_puts_it(
        self, tmp_path
    ):
        """A form XObject scaled over the whole page puts its image over
        the whole page: the page's background, not an image within it."""
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

    def test_no_image_is_read_on_a_page_read_by_vision(self, tmp_path):
        page = scan().image(pdfs.gray(300, 120), 72, 480, 300, 200)
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.images == {}


class TestUnionArea:
    def test_overlaps_count_once(self):
        assert union_area([(0, 0, 2, 2), (1, 1, 3, 3)]) == 7
        assert union_area([(0, 0, 1, 1), (2, 2, 3, 3)]) == 2
        assert union_area([]) == 0

    def test_strips_cover_a_page_together(self, tmp_path):
        """A scan stored as strips covers the page as one image does."""
        page = pdfs.Page()
        for strip in range(4):
            page.image(
                pdfs.gray(100, 50, shade=strip),
                0,
                strip * pdfs.HEIGHT / 4,
                pdfs.WIDTH,
                pdfs.HEIGHT / 4,
            )
        result = read(tmp_path, typed(TITLE), page)
        assert methods(result) == [TEXT_LAYER, VISION]
        assert result.pages[1].vision.suffix == PNG_SUFFIX
