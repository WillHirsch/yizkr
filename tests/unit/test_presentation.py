"""Offline tests for the PPTX adapter (ADR-0008). Fixture deck is
assembled in-test with zipfile — the repo carries no personal data."""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from tests.fixtures import pdfs
from tests.unit.test_run_provenance import fixed_capture  # noqa: F401
from yizkr.pipeline.container_inventory import (
    CONTAINER_INVENTORY,
    read_inventory,
)
from yizkr.pipeline.presentation import unpack_pptx
from yizkr.pipeline.rundir import DONE, run_state

pytestmark = pytest.mark.usefixtures("fixed_capture")

CT = "[Content_Types].xml"
# every member of a built deck carries this one timestamp, so the same
# arguments make the same bytes whenever the deck is built (#479)
DECK_TIME = (2020, 1, 1, 0, 0, 0)


def slide_xml(*paragraphs: str) -> str:
    body = "".join(
        "<a:p>"
        + "".join(f"<a:r><a:t>{run}</a:t></a:r>" for run in runs)
        + "</a:p>"
        for runs in paragraphs
    )
    return (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
    )


def rels_xml(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>'
        for rid, kind, target in entries
    )
    return (
        "<Relationships xmlns="
        '"http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{body}</Relationships>"
    )


IMAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
SLIDE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
NOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def build_deck(path, reorder=False):
    """Two slides; slide FILE 1 has notes, and both relate to one image
    that neither paints; slide FILE 2 also relates to a film. The deck's
    sldIdLst can present the files in reversed order."""
    order = (
        [("rId2", "slides/slide2.xml"), ("rId1", "slides/slide1.xml")]
        if reorder
        else [("rId1", "slides/slide1.xml"), ("rId2", "slides/slide2.xml")]
    )
    presentation = (
        "<p:presentation xmlns:p="
        '"http://schemas.openxmlformats.org/presentationml/2006/main"'
        " xmlns:r="
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<p:sldIdLst>"
        + "".join(
            f'<p:sldId id="25{i}" r:id="{rid}"/>'
            for i, (rid, _) in enumerate(order)
        )
        + "</p:sldIdLst></p:presentation>"
    )

    def put(archive, name, data):
        archive.writestr(zipfile.ZipInfo(name, date_time=DECK_TIME), data)

    with zipfile.ZipFile(path, "w") as archive:
        put(archive, CT, "<Types/>")
        put(archive, "ppt/presentation.xml", presentation)
        put(
            archive,
            "ppt/_rels/presentation.xml.rels",
            rels_xml([(rid, SLIDE_REL, target) for rid, target in order]),
        )
        put(
            archive,
            "ppt/slides/slide1.xml",
            slide_xml(["The Löbig family", " in Flörburg"], ["Married 1905"]),
        )
        put(
            archive,
            "ppt/slides/_rels/slide1.xml.rels",
            rels_xml(
                [
                    ("rId7", IMAGE_REL, "../media/image1.jpeg"),
                    ("rId8", NOTES_REL, "../notesSlides/notesSlide1.xml"),
                ]
            ),
        )
        put(
            archive,
            "ppt/slides/slide2.xml",
            slide_xml(["Gustav's certificate"]),
        )
        put(
            archive,
            "ppt/slides/_rels/slide2.xml.rels",
            rels_xml(
                [
                    ("rId7", IMAGE_REL, "../media/image1.jpeg"),
                    ("rId9", IMAGE_REL, "../media/clip1.mp4"),
                ]
            ),
        )
        put(
            archive,
            "ppt/notesSlides/notesSlide1.xml",
            slide_xml(["Scanned from the family album"]),
        )
        put(archive, "ppt/media/image1.jpeg", pdfs.JPEG)
        put(archive, "ppt/media/clip1.mp4", b"fakemp4")
    return path


def native(content):
    """Each slide's (position, part, text, speaker notes), as the deck's
    inventory records them."""
    notes = {
        part.locator.position: part.text
        for part in content.inventory.parts
        if part.kind == "speaker_notes"
    }
    return [
        (
            part.locator.position,
            part.locator.package_part,
            part.text,
            notes.get(part.locator.position, ""),
        )
        for part in content.inventory.parts
        if part.kind == "slide_body"
    ]


FIRST_FILE = (
    "ppt/slides/slide1.xml",
    "The Löbig family in Flörburg\nMarried 1905",
    "Scanned from the family album",
)
SECOND_FILE = ("ppt/slides/slide2.xml", "Gustav's certificate", "")


class TestUnpack:
    def test_slides_text_and_notes(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        assert native(content) == [(1, *FIRST_FILE), (2, *SECOND_FILE)]
        assert content.slide_count == 2

    def test_slide_order_follows_presentation_not_filenames(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx", reorder=True))
        # sldIdLst puts file 2 first: it becomes slide NUMBER 1
        assert native(content) == [(1, *SECOND_FILE), (2, *FIRST_FILE)]
        assert content.slide_count == 2

    def test_an_image_two_slides_relate_to_is_one_part(self, tmp_path):
        """Both slides relate to the image, and neither paints it: one
        part, its bytes kept, and no occurrence on either slide."""
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        (image,) = [
            part
            for part in content.inventory.parts
            if part.locator.package_part == "ppt/media/image1.jpeg"
        ]
        assert image.kind == "embedded_resource"
        assert content.inventory_bytes[image.byte_object.sha1] == pdfs.JPEG
        for slide in ("ppt/slides/slide1.xml", "ppt/slides/slide2.xml"):
            assert any(slide in limit for limit in image.limitations)
        assert content.inventory.placements == []

    def test_non_image_media_are_parts_no_reader_reads(self, tmp_path):
        content = unpack_pptx(build_deck(tmp_path / "deck.pptx"))
        (film,) = [
            part
            for part in content.inventory.parts
            if part.locator.package_part == "ppt/media/clip1.mp4"
        ]
        assert film.kind == "embedded_resource"
        assert content.inventory_bytes[film.byte_object.sha1] == b"fakemp4"
        assert film.reading.state == "unsupported"

    def test_non_pptx_raises(self, tmp_path):
        bogus = tmp_path / "bogus.pptx"
        with zipfile.ZipFile(bogus, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("hello.txt", date_time=DECK_TIME), "not a deck"
            )
        with pytest.raises(ValueError, match="not a PPTX"):
            unpack_pptx(bogus)


class TestTranscript:
    def test_slide_markers_scope_the_text(self, tmp_path):
        from yizkr.pipeline.transcribe import DeckTranscriber

        path = build_deck(tmp_path / "deck.pptx")
        image = hashlib.sha1(pdfs.JPEG).hexdigest()

        def engine():
            raise AssertionError("the deck's text needs no engine")

        transcription = DeckTranscriber(
            engine, excluded=lambda sha1, names=(): sha1 == image
        ).transcribe(path)
        transcription.reading.close()
        transcript = transcription.text
        assert transcript.index("[Slide 1]") < transcript.index("Married 1905")
        assert (
            transcript.index("Married 1905")
            < transcript.index("[Slide 1 notes]")
            < transcript.index("Scanned from the family album")
            < transcript.index("[Slide 2]")
            < transcript.index("Gustav's certificate")
        )


class TestScratchProcessing:
    def test_pptx_routes_through_adapter(self, tmp_path):
        from tests.fixtures.document_processing import ScriptedDriver
        from tests.unit.test_pdf_pages import Reader

        folder = tmp_path / "docs"
        folder.mkdir()
        build_deck(folder / "Family Deck.pptx")

        image_sha1 = hashlib.sha1(pdfs.JPEG).hexdigest()
        reader = Reader({image_sha1: ""})

        class ScriptedDeck(ScriptedDriver):
            def _llm(self, role, doc):
                return (
                    reader if role == "transcribe" else super()._llm(role, doc)
                )

            def _step_extract(self, doc, transcription):
                self.calls.append("extract")
                assert transcription.engine == "pptx-adapter"
                assert "[Slide 2]" in transcription.text
                from tests.fixtures.document_processing import make_batch

                return make_batch()

        lines: list[str] = []
        orchestrator = ScriptedDeck(
            folder,
            tmp_path / "state",
            log=lambda line, **kwargs: lines.append(line),
        )
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert (
            run_state(orchestrator.rundir)[(doc.sha1, "transcription")].status
            == DONE
        )
        # Processing stops before graph writing and registration.
        assert "register:Family Deck.pptx" not in orchestrator.calls
        assert doc.state.artifact_qid is None
        # its media are inventoried in the document's directory (#128), by
        # their names in the archive and the SHA-1s of their bytes
        inventory = read_inventory(doc.dir)
        assert inventory.source.sha1 == doc.sha1
        assert {
            part.locator.package_part: part.byte_object.sha1
            for part in inventory.parts
            if part.kind == "embedded_resource"
        } == {
            "ppt/media/image1.jpeg": image_sha1,
            "ppt/media/clip1.mp4": hashlib.sha1(b"fakemp4").hexdigest(),
        }
        # a re-run finds the inventory whole, and leaves it as it stands
        record = doc.dir / CONTAINER_INVENTORY
        kept = (record.stat().st_ino, record.read_bytes())
        orchestrator.run()
        assert (record.stat().st_ino, record.read_bytes()) == kept
        assert reader.read == [(f"{image_sha1}.jpeg", image_sha1)]
