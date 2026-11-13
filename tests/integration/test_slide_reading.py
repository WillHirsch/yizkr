"""Optional actual office rendering through the selected-slide reading API."""

import os
import shutil
from collections import defaultdict

import pytest

from tests.fixtures import slides
from tests.fixtures.document_processing import snapshot
from tests.fixtures.records import record_answer
from tests.unit import test_scratch_estimate as estimate_tests
from tests.unit.test_scratch import Corpus, Driver, fakes  # noqa: F401
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_inventory import read_inventory
from yizkr.pipeline.media_stages import MediaReadRequest, read_media
from yizkr.pipeline.slide_reading import (
    SlideVisionRequest,
    SlideVisionSelection,
)
from yizkr.pipeline.slide_renderer import SlideRenderSettings
from yizkr.pipeline.transcribe import Transcription, check_vision_page

engine = estimate_tests.engine


def test_real_render_selected_body_and_native_notes(tmp_path):
    executable = os.environ.get("YIZKR_TEST_LIBREOFFICE")
    if executable is None:
        pytest.skip("set YIZKR_TEST_LIBREOFFICE to a local office executable")
    source = tmp_path / "fictional.pptx"
    source.write_bytes(slides.native_presentation())
    seen = []

    class Reader:
        model_identifier = "fictional-slide-reader"

        def complete(self, prompt, image_paths=()):
            (path,) = image_paths
            data = path.read_bytes()
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            seen.append(ArtifactRef.from_bytes(data))
            return record_answer("Left panel | Right panel")

    request = MediaReadRequest(
        source,
        "paged",
        language="en",
        excluded=lambda *_: False,
        slide_vision=SlideVisionRequest(
            (1,),
            tmp_path / "renders",
            SlideRenderSettings(executable=executable, long_edge=640),
        ),
    )
    with read_media(request, Reader) as result:
        saved = Transcription.model_validate_json(
            result.transcription.model_dump_json()
        )
        assert result.products.images == []
        assert [p.provenance.method for p in saved.pages] == [
            "vision",
            "slide-xml",
            "slide-xml",
        ]
        page = saved.pages[0]
        assert page.provenance.reading_input == seen[0]
        assert saved.artifact == ArtifactRef.from_path(source)
        assert check_vision_page(saved, page).passed
        assert saved.page_record(page).pages[0].text == (
            "Left panel | Right panel"
        )
        located = saved.source_context.readings[:2]
        assert [saved.text[span.start : span.end] for span in located] == [
            "Left panel | Right panel",
            slides.NOTES,
        ]
        assert all(span.state == "attributed" for span in located)
        assert located[0].support.part_id != located[1].support.part_id
        assert saved.provenance.stages[0].configuration["slide_vision"][
            "positions"
        ] == [1]
    assert len(seen) == 1


@pytest.mark.parametrize("mode", ["explicit", "layout"])
def test_real_render_survives_driver_publication_and_cache_removal(
    tmp_path, engine, mode
):
    executable = os.environ.get("YIZKR_TEST_LIBREOFFICE")
    if executable is None:
        pytest.skip("set YIZKR_TEST_LIBREOFFICE to a local office executable")
    corpus = Corpus(tmp_path / "corpus")
    source = corpus.folder / "fictional.pptx"
    source.write_bytes(
        slides.native_presentation()
        if mode == "explicit"
        else slides.layout_presentation()
    )
    corpus.texts = defaultdict(
        lambda: (
            "Left panel | Right panel"
            if mode == "explicit"
            else "Klara | 1938\nGustav | 1939"
        )
    )
    engine.corpus = corpus
    engine.fresh = False
    state = tmp_path / "runs" / "selected"
    cache = tmp_path / "cache"
    options = {
        "cache_dir": cache,
        "until": "transcription",
        "slide_vision": {
            ArtifactRef.from_path(source).sha1: SlideVisionSelection(
                (1,) if mode == "explicit" else (),
                SlideRenderSettings(executable=executable, long_edge=640),
                mode=mode,
            )
        },
    }
    first = Driver(corpus.folder, state, **options)
    assert first.run() == 0
    (doc,) = first.documents()
    transcription = doc.transcription.read_bytes()
    reading = Transcription.model_validate_json(transcription)
    assert reading.pages[0].provenance.method == "vision"
    assert reading.pages[0].provenance.render_product_sha256
    if mode == "layout":
        plan = reading.provenance.metadata["dependencies"]["slide_routing"]
        assert plan["policy"] == "layout-v1" and plan["positions"] == [1]
    assert check_vision_page(reading, reading.pages[0]).passed
    body, notes = reading.source_context.readings[:2]
    assert body.support.part_id != notes.support.part_id
    assert reading.text[notes.start : notes.end] == slides.NOTES
    inventory = read_inventory(doc.dir)
    part = next(p for p in inventory.parts if p.id == body.support.part_id)
    assert part.reading.product == "transcription.json"
    assert reading.pages[0].provenance.reading_input in [
        representation.byte_object for representation in part.representations
    ]
    assert len(engine.calls) == 1
    shutil.rmtree(cache)

    def unexpected(*args):
        raise AssertionError(
            "durable reading unexpectedly called the provider"
        )

    engine.hook = unexpected
    before = snapshot(state, skip=())
    estimate = estimate_tests.Quiet(corpus.folder, state, **options).estimate()
    assert estimate.stages["transcription"].reused == 1
    assert snapshot(state, skip=()) == before
    assert not cache.exists()
    assert Driver(corpus.folder, state, **options).run() == 0
    assert doc.transcription.read_bytes() == transcription
    assert len(engine.calls) == 1
