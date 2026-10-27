"""Refuse opaque PDF resources before their images reach a model."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

from tests.fixtures import pdfs
from tests.unit.test_pdf_exclusions import JPEG_SHA1, RecordingClient, scan
from yizkr.pipeline.textlayer import PdfExclusionError
from yizkr.pipeline.transcribe import LlmVisionTranscriber


def dictionary(**entries):
    return DictionaryObject(
        {NameObject("/" + key): value for key, value in entries.items()}
    )


def numbers(*values):
    return ArrayObject([NumberObject(value) for value in values])


def stream(data: bytes, **entries):
    result = DecodedStreamObject()
    result.set_data(data)
    result.update(dictionary(**entries))
    return result


def patterned(writer, *, nested=False, inherited=False):
    page = writer.pages[0]
    resources = page["/Resources"]
    image = resources["/XObject"].raw_get("/Im1")
    pattern = stream(
        b"q 128 0 0 96 0 0 cm /Im Do Q",
        Type=NameObject("/Pattern"),
        PatternType=NumberObject(1),
        PaintType=NumberObject(1),
        TilingType=NumberObject(1),
        BBox=numbers(0, 0, 128, 96),
        XStep=NumberObject(128),
        YStep=NumberObject(96),
        Resources=dictionary(XObject=dictionary(Im=image)),
    )
    patterned_resources = dictionary(
        Pattern=dictionary(P1=writer._add_object(pattern))
    )
    paint = b"/Pattern cs /P1 scn 0 0 595 842 re f"
    if nested:
        form = stream(
            paint,
            Type=NameObject("/XObject"),
            Subtype=NameObject("/Form"),
            BBox=numbers(0, 0, 595, 842),
            Resources=patterned_resources,
        )
        page[NameObject("/Resources")] = dictionary(
            XObject=dictionary(Fm=writer._add_object(form))
        )
        paint = b"/Fm Do"
    else:
        page[NameObject("/Resources")] = patterned_resources
    if inherited:
        page["/Parent"][NameObject("/Resources")] = page["/Resources"]
        del page["/Resources"]
    page[NameObject("/Contents")] = writer._add_object(stream(paint))


@pytest.mark.parametrize("location", ["page", "inherited", "form"])
def test_image_pattern_is_refused_even_when_pdfium_only_sees_a_path(
    tmp_path, location
):
    writer = PdfWriter(io.BytesIO(pdfs.pdf([scan(pdfs.jpeg())])))
    patterned(
        writer, nested=location == "form", inherited=location == "inherited"
    )
    path = tmp_path / "family-booklet-2011.pdf"
    writer.write(path)
    client = RecordingClient()
    with pytest.raises(PdfExclusionError, match="pattern"):
        LlmVisionTranscriber(
            client, excluded=lambda sha1: sha1 == JPEG_SHA1
        ).transcribe(path)
    assert client.requests == []


@pytest.mark.parametrize("indirect_subtype", [False, True])
def test_image_inside_type3_glyph_is_refused(tmp_path, indirect_subtype):
    writer = PdfWriter(io.BytesIO(pdfs.pdf([scan(pdfs.jpeg())])))
    page = writer.pages[0]
    image = page["/Resources"]["/XObject"].raw_get("/Im1")
    glyph = stream(b"1000 0 0 0 1000 1000 d1 q 1000 0 0 1000 0 0 cm /Im Do Q")
    font = dictionary(
        Type=NameObject("/Font"),
        Subtype=(
            writer._add_object(NameObject("/Type3"))
            if indirect_subtype
            else NameObject("/Type3")
        ),
        FontBBox=numbers(0, 0, 1000, 1000),
        FontMatrix=ArrayObject(
            [
                FloatObject(0.001),
                NumberObject(0),
                NumberObject(0),
                FloatObject(0.001),
                NumberObject(0),
                NumberObject(0),
            ]
        ),
        CharProcs=dictionary(A=writer._add_object(glyph)),
        Encoding=dictionary(
            Type=NameObject("/Encoding"),
            Differences=ArrayObject([NumberObject(65), NameObject("/A")]),
        ),
        FirstChar=NumberObject(65),
        LastChar=NumberObject(65),
        Widths=numbers(1000),
        Resources=dictionary(XObject=dictionary(Im=image)),
    )
    page[NameObject("/Resources")] = dictionary(
        Font=dictionary(F1=writer._add_object(font))
    )
    page[NameObject("/Contents")] = writer._add_object(
        stream(b"BT /F1 72 Tf 72 600 Td (A) Tj ET")
    )
    path = tmp_path / "family-booklet-2011.pdf"
    writer.write(path)
    client = RecordingClient()
    with pytest.raises(PdfExclusionError, match="Type3"):
        LlmVisionTranscriber(client, excluded=lambda _: False).transcribe(path)
    assert client.requests == []


def test_encrypted_pdf_cannot_fall_through_to_raw_upload(tmp_path):
    writer = PdfWriter(io.BytesIO(pdfs.pdf([scan()])))
    writer.encrypt("fictional-fixture-password")
    path = tmp_path / "family-booklet-2011.pdf"
    writer.write(path)
    client = RecordingClient()
    with pytest.raises(PdfExclusionError, match="cannot be prepared"):
        LlmVisionTranscriber(client, excluded=lambda _: False).transcribe(path)
    assert client.requests == []
