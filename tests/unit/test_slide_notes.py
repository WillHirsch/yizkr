"""Vision-body checks and native speaker-note support remain separate."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tests.fixtures.records import record_page
from tests.unit.test_chunking import ScriptedLlm
from tests.unit.test_run_provenance import fixed_capture  # noqa: F401
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import (
    ContainerInventory,
    Eligibility,
    Placement,
    SourcePart,
    StructuralLocator,
    structural_id,
)
from yizkr.pipeline.container_sources import ContainerSources, source_context
from yizkr.pipeline.containers import ContainerPage, ContainerPages
from yizkr.pipeline.source_context import bind_source_support
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import (
    DeckTranscriber,
    Transcription,
    check_vision_page,
    reading_digest,
)

pytestmark = pytest.mark.usefixtures("fixed_capture")
BODY = "Wastl to Klara, July 1939."
NOTES = "Photograph from the family album"
SOURCE = ArtifactRef.from_bytes(b"fictional library-talk-2018 presentation")


def sources(*, notes=NOTES, composite=False):
    parts = []
    for kind, package, text in (
        ("slide_body", "ppt/slides/slide1.xml", BODY),
        ("speaker_notes", "ppt/notesSlides/notesSlide1.xml", notes),
    ):
        locator = StructuralLocator(
            kind=kind, position=1, package_part=package
        )
        parts.append(
            SourcePart(
                id=structural_id(SOURCE, locator),
                kind=kind,
                locator=locator,
                text=text,
                eligibility=Eligibility(state="allowed"),
            )
        )
    body, note = parts
    placements = []
    if composite:
        locator = StructuralLocator(
            kind="embedded_resource", package_part="ppt/media/letter.png"
        )
        image = SourcePart(
            id=structural_id(SOURCE, locator),
            kind="embedded_resource",
            locator=locator,
            source_role="independent",
        )
        parts.append(image)
        occurrence = StructuralLocator(
            kind="placement", position=1, path="/p:sld[1]/p:pic[1]"
        )
        placements.append(
            Placement(
                id=structural_id(SOURCE, occurrence),
                parent=body.id,
                child=image.id,
                locator=occurrence,
                order=1,
                occurrence="paint",
            )
        )
    return ContainerSources(
        ContainerInventory(
            source=SOURCE,
            container="presentation",
            parts=parts,
            placements=placements,
        ),
        bodies=[body],
        notes=[note] if notes else [],
    )


def read(tmp_path, *, body=BODY, notes=NOTES, composite=False, native=False):
    path = tmp_path / "slide.png"
    path.write_bytes(png(4, 4, 3, b"\xff" * 48))
    answer = json.dumps(
        {
            "document_class": "unclassified",
            "pages": [record_page(1, body, notes="A fold is visible.")],
        }
    )
    client = ScriptedLlm([answer])
    located = sources(notes=notes, composite=composite)
    native_text = BODY + (f"\n[Slide 1 notes]\n{notes}" if notes else "")
    page = ContainerPage(
        1,
        "slide-xml" if native else "vision",
        text=native_text if native else "",
        vision=None if native else path,
        speaker_notes="" if native else notes,
        reading_input=None if native else ArtifactRef.from_path(path),
        render_product_sha256=None if native else "a" * 64,
    )
    result = DeckTranscriber(lambda: client)._transcription(
        SOURCE, ContainerPages([page], sources=located)
    )
    return result, located


def claim(quote):
    return {
        "source": {
            "artifact": SOURCE.model_dump(),
            "page_number": "1",
            "transcription_excerpt": {"text": quote},
        }
    }


def test_vision_and_native_notes_keep_distinct_checked_support(tmp_path):
    reading, _ = read(tmp_path)
    stored = Transcription.model_validate_json(reading.model_dump_json())
    page = stored.pages[0]
    assert stored.artifact == SOURCE
    assert page.body_end == page.start + len(BODY)
    assert stored.page_record(page).pages[0].text == BODY
    assert page.notes == "A fold is visible."
    assert page.provenance.reading_input == ArtifactRef.from_path(
        tmp_path / "slide.png"
    )
    assert page.provenance.render_product_sha256 == "a" * 64
    assert check_vision_page(stored, page).passed
    assert reading_digest(stored) == reading_digest(reading)
    accepted, rejected = bind_source_support(
        [claim(BODY), claim(NOTES)], stored
    )
    assert rejected == []
    assert [c["source"]["support"]["locator"]["kind"] for c in accepted] == [
        "slide_body",
        "speaker_notes",
    ]
    assert all(
        c["source"]["artifact"] == SOURCE.model_dump() for c in accepted
    )


def test_native_notes_do_not_make_a_blank_vision_body_transcribed(tmp_path):
    reading, _ = read(tmp_path, body="")
    stored = Transcription.model_validate_json(reading.model_dump_json())
    page = stored.pages[0]
    assert page.body_end == page.start
    assert page.status == "blank"
    assert stored.page_record(page).pages[0].text == ""
    assert check_vision_page(stored, page).passed
    accepted, rejected = bind_source_support([claim(NOTES)], stored)
    assert rejected == []
    assert (
        accepted[0]["source"]["support"]["locator"]["kind"] == "speaker_notes"
    )
    # Corrupted status cannot borrow the native note's text to pass the
    # structural requirement that a transcribed vision body contains text.
    page.status = "transcribed"
    assert stored.page_record(page) is None
    assert not check_vision_page(stored, page).passed


def test_model_marker_text_cannot_move_the_native_notes_boundary(tmp_path):
    body = "Wastl to Klara\n[Slide 1 notes]\nJuly 1939."
    reading, _ = read(tmp_path, body=body)
    body_span, notes_span = reading.source_context.readings
    assert reading.text[body_span.start : body_span.end] == body
    assert reading.text[notes_span.start : notes_span.end] == NOTES
    assert reading.page_record(reading.pages[0]).pages[0].text == body


@pytest.mark.parametrize(
    "damage", ["note-text", "missing-boundary", "missing-note-part"]
)
def test_note_support_requires_current_exact_suffix_and_boundary(
    tmp_path, damage
):
    reading, located = read(tmp_path)
    if damage == "note-text":
        reading.text = reading.text.replace(NOTES, "x" * len(NOTES))
    elif damage == "missing-boundary":
        reading.pages[0].body_end = None
    else:
        located.notes = []
    reading.source_context = source_context(reading, located)
    assert all(
        span.state == "unsupported" for span in reading.source_context.readings
    )
    accepted, rejected = bind_source_support([claim(BODY)], reading)
    assert accepted == []
    assert rejected[0]["reasons"] == ["source_support"]


@pytest.mark.parametrize("outside", ["before", "after"])
def test_stored_body_boundary_must_lie_within_its_page(tmp_path, outside):
    reading, _ = read(tmp_path)
    payload = reading.model_dump()
    page = payload["pages"][0]
    page["body_end"] = (
        page["start"] - 1 if outside == "before" else page["end"] + 1
    )
    with pytest.raises(ValidationError, match="visual body must lie"):
        Transcription.model_validate(payload)


def test_ambiguous_composite_body_does_not_poison_located_native_notes(
    tmp_path,
):
    reading, _ = read(tmp_path, composite=True)
    body, notes = reading.source_context.readings
    assert body.state == "unsupported"
    assert body.owner is None
    assert notes.state == "attributed"
    assert notes.owner == SOURCE
    accepted, rejected = bind_source_support(
        [claim(BODY), claim(NOTES)], reading
    )
    assert len(rejected) == 1
    assert len(accepted) == 1
    assert (
        accepted[0]["source"]["support"]["locator"]["kind"] == "speaker_notes"
    )


@pytest.mark.parametrize("native,notes", [(True, NOTES), (False, "")])
def test_existing_unsplit_page_serialization_is_preserved(
    tmp_path, native, notes
):
    reading, _ = read(tmp_path, native=native, notes=notes)
    page = reading.model_dump()["pages"][0]
    assert "body_end" not in page
    if native:
        assert page["provenance"] == {
            "method": "slide-xml",
            "engine": "pptx-adapter",
            "annotations": 0,
        }
        assert reading.text == f"[Slide 1]\n{BODY}\n[Slide 1 notes]\n{NOTES}"
    else:
        assert reading.page_record(reading.pages[0]).pages[0].text == BODY
