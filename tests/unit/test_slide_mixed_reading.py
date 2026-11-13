"""Mixed vision arranges native text without borrowing image assertions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from tests.fixtures import slides
from tests.fixtures.records import record_answer
from tests.unit import test_slide_renders as render_tests
from tests.unit.test_media_stages import AuthoredReader
from tests.unit.test_slide_reading import request
from tests.unit.test_slide_reading_reuse import failure
from yizkr.claims import ArtifactRef
from yizkr.pipeline.media_stages import (
    PreparationRequest,
    prepare_container,
    read_media,
)
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.transcription_check import TranscriptionCheckFailed

backend = render_tests.backend


class MixedReader(AuthoredReader):
    def __init__(self, layout=None):
        super().__init__()
        self.layout = layout or json.dumps({"rows": [["b1"]]})

    def complete(self, prompt, image_paths=()):
        answer = super().complete(prompt, image_paths)
        return (
            self.layout
            if "Native text blocks in original XML order:" in prompt
            else answer
        )


def test_mixed_background_reads_native_content_and_image_as_distinct_sources(
    tmp_path, backend
):
    selected = request(tmp_path, positions=(1,))
    client = MixedReader()
    with read_media(selected, lambda: client) as result:
        reading = result.transcription
        page = reading.pages[0]
        assert reading.page_record(page).pages[0].text == "Fictional slide 1"
        assert slides.NOTES in reading.text
        assert len(result.products.images) == 1
        image = result.products.images[0]
        assert image.transcription.text not in reading.text
        assert image.sha1 == page.images[0]
        assert [
            place.locator.position
            for place in image.source_supports[0].placements
        ] == [1]
        assert len(client.inputs) == 2
        assert page.provenance.native_layout.rows == (("b1",),)
        assert (
            page.provenance.native_layout.byte_object
            != page.provenance.reading_input
        )
        assert all(
            span.state == "attributed"
            for span in reading.source_context.readings
        )
        notes = next(
            span
            for span in reading.source_context.readings
            if span.support.locator.kind == "speaker_notes"
        )
        assert reading.text[notes.start : notes.end] == slides.NOTES
        restored = Transcription.model_validate_json(reading.model_dump_json())
        assert (
            restored.pages[0].provenance.native_layout
            == page.provenance.native_layout
        )
        from yizkr.pipeline.slide_layout_reading import NATIVE_LAYOUT_PROMPT

        prompts = reading.provenance.stages[-1].prompt_hashes
        assert prompts[
            "pipeline.slide_layout_reading.NATIVE_LAYOUT_PROMPT"
        ] == (hashlib.sha256(NATIVE_LAYOUT_PROMPT.encode()).hexdigest())
        assert "pipeline.transcribe.TRANSCRIPTION_PROMPT" in prompts


@pytest.mark.parametrize(
    "layout",
    [
        '{"rows": [["image-text"]]}',
        '{"rows": [["b1", "b1"]]}',
        '{"rows": []}',
        '{"rows": [["b1"]], "text": "Invented image transcription"}',
        "I cannot identify these blocks.",
        record_answer("Fictional image text must not enter the native body"),
    ],
)
def test_ambiguous_or_unbounded_layout_fails_before_independent_image_read(
    tmp_path, backend, layout
):
    selected = request(tmp_path, positions=(1,))
    client = MixedReader(layout)
    with pytest.raises(
        TranscriptionCheckFailed, match="verified native text block"
    ) as failed:
        read_media(selected, lambda: client)
    assert not failed.value.check.passed
    assert len(client.inputs) == 1


def test_rejected_cached_layout_readmits_contributors_before_retry(
    tmp_path, backend
):
    selected = request(tmp_path, positions=(1,))
    blocked = set()
    selected = replace(selected, excluded=lambda sha1, names: sha1 in blocked)

    class CachedReader(MixedReader):
        def reject(self, answer, reason):
            from yizkr.pipeline.presentation import unpack_pptx

            content = unpack_pptx(selected.source)
            blocked.update(
                part.byte_object.sha1
                for part in content.inventory.parts
                if part.kind == "embedded_resource"
            )
            return True

    client = CachedReader('{"rows": []}')
    with pytest.raises(ValueError, match="excluded|contributing image"):
        read_media(selected, lambda: client)
    assert len(client.inputs) == 1


def test_rejected_cached_layout_can_be_replaced_once(tmp_path, backend):
    selected = request(tmp_path, positions=(1,))

    class CachedReader(MixedReader):
        def reject(self, answer, reason):
            assert answer == '{"rows": []}'
            self.layout = '{"rows": [["b1"]]}'
            return True

    client = CachedReader('{"rows": []}')
    with read_media(selected, lambda: client) as result:
        assert (
            result.transcription.page_record(result.transcription.pages[0])
            .pages[0]
            .text
            == "Fictional slide 1"
        )
    assert len(client.inputs) == 3


@pytest.mark.parametrize("damage", ["exclusion", "bytes"])
def test_native_prompt_input_is_rechecked_after_client_construction(
    tmp_path, backend, damage
):
    selected = request(tmp_path, positions=(1,))
    prepared = prepare_container(PreparationRequest(selected.source))
    content = prepared.presentation
    body = next(
        part
        for part in content.inventory.parts
        if part.kind == "slide_body" and part.locator.position == 1
    )
    blocked = set()
    selected = replace(
        selected,
        prepared=prepared,
        excluded=lambda sha1, names: sha1 in blocked,
    )
    client = MixedReader()

    def factory():
        if damage == "exclusion":
            blocked.add(body.byte_object.sha1)
        else:
            content.inventory_bytes[body.byte_object.sha1] = (
                b"changed native input"
            )
        return client

    with pytest.raises(ValueError, match="excluded|native input changed"):
        read_media(selected, factory)
    assert client.inputs == []


@pytest.mark.parametrize(
    "damage", ["missing", "basis", "part", "rows", "text"]
)
def test_persisted_mixed_reading_rechecks_native_attribution(
    tmp_path, backend, damage
):
    selected = request(tmp_path, positions=(1,))
    with read_media(selected, MixedReader) as result:
        stored = Transcription.model_validate_json(
            result.transcription.model_dump_json()
        )
    prepared = prepare_container(
        PreparationRequest(
            selected.source, existing_directory=selected.slide_vision.directory
        )
    )
    assert failure(selected, stored, prepared.presentation) is None
    page = stored.pages[0]
    proof = page.provenance.native_layout
    if damage == "missing":
        page.provenance.native_layout = None
    elif damage == "text":
        stored.text = stored.text.replace(
            "Fictional slide 1", "Imaginary text 1"
        )
    else:
        updates = {
            "basis": {"basis_sha256": "0" * 64},
            "part": {"byte_object": ArtifactRef.from_bytes(b"another slide")},
            "rows": {"rows": (("b2",),)},
        }
        page.provenance.native_layout = proof.model_copy(
            update=updates[damage]
        )
    assert failure(selected, stored, prepared.presentation) is not None
