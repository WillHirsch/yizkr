"""Optional actual office rendering through the selected-slide reading API."""

import os

import pytest

from tests.fixtures import slides
from tests.fixtures.records import record_answer
from yizkr.claims import ArtifactRef
from yizkr.pipeline.media_stages import MediaReadRequest, read_media
from yizkr.pipeline.slide_reading import SlideVisionRequest
from yizkr.pipeline.slide_renderer import SlideRenderSettings
from yizkr.pipeline.transcribe import Transcription, check_vision_page


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
