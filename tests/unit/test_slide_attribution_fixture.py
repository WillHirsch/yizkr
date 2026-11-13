"""The real-graph fixture reaches attribution through production stages."""

import pytest

from tests.fixtures.slide_attribution import (
    CAPTION,
    IMAGE_TEXT,
    NATIVE,
    NOTES,
    SELECTED,
    prepare_case,
)
from yizkr.claims import QidRef
from yizkr.pipeline.extract import ExtractionFailed
from yizkr.pipeline.writer import check_batch


def test_fixture_keeps_native_visual_notes_and_image_claims_separate(
    tmp_path, monkeypatch
):
    person, address = QidRef(qid="Q101"), QidRef(qid="Q102")
    with prepare_case(
        tmp_path, monkeypatch, person=person, address=address
    ) as case:
        pages = case.transcription.pages
        assert [p.provenance.method for p in pages] == [
            "vision",
            "vision",
            "slide-xml",
        ]
        assert pages[0].provenance.native_layout is not None
        assert pages[1].provenance.native_layout is None
        assert pages[0].images == [case.image_owner.sha1] * 2
        assert IMAGE_TEXT not in case.transcription.text
        assert IMAGE_TEXT in case.image_transcription.text
        assert set(case.sources.sources) == {case.image_owner.sha1}
        expected = {
            CAPTION: ("slide_body", 1),
            SELECTED: ("slide_body", 2),
            NATIVE: ("slide_body", 3),
            NOTES: ("speaker_notes", 1),
        }
        for claim in case.parent_batch.claims:
            assert claim.subject == case.image_owner
            assert claim.source.artifact == case.transcription.artifact
            support = claim.source.support
            assert (
                support.locator.kind,
                support.locator.position,
            ) == expected[claim.source.transcription_excerpt.text]
        assert len(case.parent_batch.claims) == 4
        (claim,) = case.image_batch.claims
        assert claim.property == "residence"
        assert claim.source.artifact == case.image_owner
        assert claim.source.support.locator.kind == "embedded_resource"
        assert claim.subject == person
        assert claim.value.value == address
        assert check_batch(case.image_batch, None)[3] == ""
        assert case.parent_batch.extraction_run.provenance is not None
        assert case.image_batch.extraction_run.provenance is not None
        for batch in (case.parent_batch, case.image_batch):
            assert [
                stage.stage for stage in batch.extraction_run.provenance.stages
            ] == ["transcription", "extraction"]
        assert case.model_calls == [
            "image",
            "mixed-layout",
            "selected-vision",
            "image",
        ]
        assert len(case.render_calls) == 1

        calls = list(case.model_calls)
        with pytest.raises(ValueError, match="excluded|eligible|unresolved"):
            case.refuse_excluded()
        assert case.model_calls == calls
        assert len(case.render_calls) == 1
        with pytest.raises(ExtractionFailed):
            case.refuse_ambiguous()
