"""PDF exclusion decisions protect the actual files sent to a model.

The PDF parser, image extraction, route choice and transcription checks are
real. Only model answers are scripted, using repository-fictional records.
"""

from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from tests.fixtures import example_universe as u
from tests.fixtures import pdfs
from tests.fixtures.records import record_answer
from yizkr.pipeline import textlayer
from yizkr.pipeline.transcribe import LlmVisionTranscriber, PdfPageTranscriber

SOURCES = {source.key: source for source in u.SOURCES}
TITLE = "Our Family from Germany. Compiled by Nicholas Lobig. Stupfield, 2011."
EXTRACT = SOURCES["birth-extract-1938"].text
JPEG_SHA1 = hashlib.sha1(pdfs.JPEG).hexdigest()
PERMITTED = pdfs.gray(160, 100, shade=43)
PERMITTED_PNG = textlayer.png(160, 100, 1, PERMITTED.data)
BROKEN = pdfs.Image(
    "/Width 100 /Height 100 /ColorSpace /DeviceGray /BitsPerComponent 8 "
    "/Filter /Fictional",
    b"no image stream PDFium can read",
)


class RecordingClient:
    model_identifier = "fictional-pdf-reader"

    def __init__(self):
        self.requests: list[list[tuple[str, bytes]]] = []

    def complete(self, prompt, image_paths=()):
        self.requests.append(
            [
                (Path(path).suffix, Path(path).read_bytes())
                for path in image_paths
            ]
        )
        return record_answer(EXTRACT)

    def sent(self) -> list[bytes]:
        return [data for request in self.requests for _suffix, data in request]


def source(tmp_path: Path, *pages: pdfs.Page) -> Path:
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(pdfs.pdf(list(pages)))
    return path


def typed() -> pdfs.Page:
    return pdfs.Page().text(pdfs.lines(TITLE))


def scan(image: pdfs.Image = PERMITTED) -> pdfs.Page:
    return pdfs.Page().image(image, 0, 0, pdfs.WIDTH, pdfs.HEIGHT)


def transcribe(path: Path, client: RecordingClient, excluded):
    """Exercise the paged route and its public whole-PDF fallback."""
    result = PdfPageTranscriber(lambda: client, excluded=excluded).transcribe(
        path
    )
    if result is None:
        result = LlmVisionTranscriber(client, excluded=excluded).transcribe(
            path
        )
    return result


def close(result) -> None:
    if result.reading is not None:
        result.reading.close()


@pytest.mark.parametrize("exclude_jpeg", [False, True])
def test_text_layer_keeps_permitted_text_and_only_sends_allowed_images(
    tmp_path, exclude_jpeg
):
    page = typed().image(pdfs.jpeg(), 72, 500, 128, 96)
    page.image(PERMITTED, 250, 500, 160, 100)
    path = source(tmp_path, page)
    client = RecordingClient()
    result = transcribe(
        path, client, lambda sha1: exclude_jpeg and sha1 == JPEG_SHA1
    )
    try:
        assert TITLE in result.text
        assert result.pages[0].provenance.method == "text-layer"
        expected = {PERMITTED_PNG}
        if not exclude_jpeg:
            expected.add(pdfs.JPEG)
        assert set(client.sent()) == expected
        assert {image.sha1 for image in result.reading.images} == {
            hashlib.sha1(data).hexdigest() for data in expected
        }
        if exclude_jpeg:
            assert JPEG_SHA1 not in result.text
    finally:
        close(result)


def test_allowed_mixed_pdf_sends_the_rendered_page(tmp_path):
    # The frame prevents the single-image shortcut, exercising rendering.
    vision = scan().frame(20, 20, 500, 700)
    path = source(tmp_path, typed(), vision)
    client = RecordingClient()
    result = transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    try:
        assert [page.provenance.method for page in result.pages] == [
            "text-layer",
            "vision",
        ]
        assert len(client.sent()) == 1
        assert client.sent()[0].startswith(b"\x89PNG\r\n\x1a\n")
        assert client.sent()[0] != path.read_bytes()
        assert EXTRACT in result.text
    finally:
        close(result)


def test_excluded_image_on_later_rendered_page_blocks_all_model_calls(
    tmp_path,
):
    earlier_allowed = scan().frame(20, 20, 500, 700)
    later_excluded = scan(pdfs.jpeg()).frame(20, 20, 500, 700)
    path = source(tmp_path, typed(), earlier_allowed, later_excluded)
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError) as refusal:
        transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    assert client.requests == []
    assert "3" in str(refusal.value)
    assert "exclu" in str(refusal.value).lower()


@pytest.mark.parametrize("nested", [False, True])
def test_excluded_image_blocks_all_vision_pdf_before_fallback(
    tmp_path, nested
):
    first = scan()
    last = pdfs.Page()
    place = last.form_image if nested else last.image
    place(pdfs.jpeg(), 0, 0, pdfs.WIDTH, pdfs.HEIGHT)
    path = source(tmp_path, first, last)
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    assert client.requests == []


def test_allowed_whole_pdf_is_rebuilt_without_unused_excluded_bytes(tmp_path):
    page = scan()
    # A resource named in the PDF but never painted can be omitted safely.
    # Giving the original PDF to the model would still disclose its bytes.
    page.images["Unused"] = pdfs.jpeg()
    path = source(tmp_path, page)
    original = path.read_bytes()
    assert pdfs.JPEG in original
    client = RecordingClient()
    result = transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    try:
        assert EXTRACT in result.text
        assert result.artifact.sha1 == hashlib.sha1(original).hexdigest()
        assert result.source_context is not None
        (reading,) = result.source_context.readings
        assert reading.state == "attributed"
        assert reading.owner == result.artifact
        assert reading.support.locator.position == 1
        assert result.text[reading.start : reading.end] == EXTRACT
        assert len(client.sent()) == 1
        submitted = client.sent()[0]
        assert submitted.startswith(b"%PDF-")
        assert submitted != original
        assert pdfs.JPEG not in submitted
        with pdfium.PdfDocument(submitted) as rendered:
            assert len(rendered) == 1
            submitted_page = rendered[0]
            text = submitted_page.get_textpage()
            try:
                assert text.get_text_range() == ""
            finally:
                text.close()
                submitted_page.close()
    finally:
        close(result)


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_direct_vision_cannot_bypass_exclusions_by_pdf_filename(
    tmp_path, suffix
):
    path = tmp_path / ("family-booklet-2011" + suffix)
    path.write_bytes(pdfs.pdf([scan(pdfs.jpeg())]))
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        LlmVisionTranscriber(
            client, excluded=lambda sha1: sha1 == JPEG_SHA1
        ).transcribe(path)
    assert client.requests == []


def test_decoration_exclusion_also_blocks_a_rendered_page(tmp_path):
    decoration = pdfs.gray(8, 8, shade=7)
    decoration_sha1 = hashlib.sha1(
        textlayer.png(8, 8, 1, decoration.data)
    ).hexdigest()
    page = scan().image(decoration, 30, 30, 8, 8)
    path = source(tmp_path, typed(), page)
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        transcribe(path, client, lambda sha1: sha1 == decoration_sha1)
    assert client.requests == []


@pytest.mark.parametrize("whole_pdf", [False, True])
def test_uninspectable_pdf_is_refused_before_a_model_call(tmp_path, whole_pdf):
    path = tmp_path / "family-booklet-2011.pdf"
    path.write_bytes(b"%PDF-1.7\nnot a readable PDF\n%%EOF")
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        if whole_pdf:
            LlmVisionTranscriber(
                client, excluded=lambda sha1: sha1 == JPEG_SHA1
            ).transcribe(path)
        else:
            transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    assert client.requests == []


def test_uninspectable_image_on_vision_page_is_not_sent(tmp_path):
    path = source(tmp_path, typed(), scan(BROKEN))
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    assert client.requests == []


def test_excluded_soft_mask_cannot_hide_behind_allowed_image_bytes(tmp_path):
    mask = pdfs.gray(160, 100, shade=19)
    mask_sha1 = hashlib.sha1(textlayer.png(160, 100, 1, mask.data)).hexdigest()
    image = pdfs.Image(PERMITTED.entries, PERMITTED.data, smask=mask)
    path = source(tmp_path, typed(), scan(image))
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError):
        transcribe(path, client, lambda sha1: sha1 == mask_sha1)
    assert client.requests == []


def test_safe_whole_pdf_preparation_is_repeatable(tmp_path):
    path = source(tmp_path, scan(), scan(pdfs.jpeg()))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    prepared = [
        textlayer.prepare_pdf_vision(path, directory, lambda sha1: False)
        for directory in (first, second)
    ]
    # Identical prepared bytes preserve compatible model-response reuse.
    assert prepared[0].read_bytes() == prepared[1].read_bytes()
    with pdfium.PdfDocument(prepared[0]) as document:
        assert len(document) == 2


def test_opaque_annotation_appearance_is_refused_before_vision(tmp_path):
    path = source(tmp_path, typed(), scan())
    # Give the stamp a real appearance stream. Ordinary page-object
    # enumeration cannot establish the eligibility of its resources.
    with pdfium.PdfDocument(path.read_bytes()) as document:
        page = document[1]
        annotation = pdfium_c.FPDFPage_CreateAnnot(
            page.raw, pdfium_c.FPDF_ANNOT_STAMP
        )
        assert annotation
        try:
            rect = pdfium_c.FS_RECTF(40, 80, 100, 40)
            assert pdfium_c.FPDFAnnot_SetRect(annotation, ctypes.byref(rect))
            commands = "q 0 g 0 0 60 40 re f Q".encode("utf-16-le") + b"\0\0"
            appearance = (
                ctypes.c_ushort * (len(commands) // 2)
            ).from_buffer_copy(commands)
            assert pdfium_c.FPDFAnnot_SetAP(
                annotation,
                pdfium_c.FPDF_ANNOT_APPEARANCEMODE_NORMAL,
                appearance,
            )
            document.save(path)
        finally:
            pdfium_c.FPDFPage_CloseAnnot(annotation)
            page.close()
    client = RecordingClient()
    with pytest.raises(textlayer.PdfExclusionError, match="appearance"):
        transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    assert client.requests == []


def test_whole_pdf_preparation_preserves_displayed_pixels_and_page_order(
    tmp_path,
):
    pages = []
    for rotation in (0, 90, 180, 270):
        page = pdfs.Page(width=120, height=80, rotate=rotation)
        page.rectangle(0, 0, 60, 40, gray=0.2)
        page.ops.append(b"q 1 0 0 rg 60 40 60 40 re f Q")
        pages.append(page)
    original = source(tmp_path, *pages)
    output = tmp_path / "prepared"
    output.mkdir()
    prepared = textlayer.prepare_pdf_vision(
        original, output, lambda sha1: False
    )

    with (
        pdfium.PdfDocument(original) as before,
        pdfium.PdfDocument(prepared) as after,
    ):
        assert len(after) == len(before)
        for number in range(len(before)):
            source_page, prepared_page = before[number], after[number]
            scale = textlayer.RENDER_LONG_EDGE / max(source_page.get_size())
            source_bitmap = source_page.render(
                scale=scale, force_bitmap_format=pdfium_c.FPDFBitmap_BGR
            )
            prepared_bitmap = prepared_page.render(
                scale=1, force_bitmap_format=pdfium_c.FPDFBitmap_BGR
            )
            try:
                assert (source_bitmap.width, source_bitmap.height) == (
                    prepared_bitmap.width,
                    prepared_bitmap.height,
                )
                assert bytes(source_bitmap.buffer) == bytes(
                    prepared_bitmap.buffer
                )
            finally:
                source_bitmap.close()
                prepared_bitmap.close()
                source_page.close()
                prepared_page.close()


@pytest.mark.parametrize("page_numbers", [[], [1, 3], [1, 2, 4]])
def test_saved_pdf_reading_needs_complete_physical_page_provenance(
    tmp_path, page_numbers
):
    from yizkr.pipeline.transcribe import Transcription, pdf_reading_failure

    path = source(tmp_path, typed(), typed(), typed())
    client = RecordingClient()
    result = transcribe(path, client, lambda sha1: sha1 == JPEG_SHA1)
    try:
        saved = result.model_dump(mode="json")
        saved["pages"] = [
            dict(saved["pages"][index], page=number)
            for index, number in enumerate(page_numbers)
        ]
        # These records pass the format schema; the source-aware reuse check
        # must still reject missing or out-of-range physical pages.
        reading = Transcription.model_validate(saved)
        assert (
            pdf_reading_failure(path, reading, lambda sha1: False) is not None
        )
        assert client.requests == []
    finally:
        close(result)
