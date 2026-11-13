"""Mixed-slide layout supplies native words, never a second image witness."""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.fixtures import slides
from tests.unit.test_chunking import ScriptedLlm, ndjson
from tests.unit.test_container_claims import CAPTION, caption
from tests.unit.test_run_provenance import fixed_capture  # noqa: F401
from yizkr.claims import ArtifactRef
from yizkr.pipeline.container_sources import (
    image_source_context,
    project_sources,
    source_context,
)
from yizkr.pipeline.extract import (
    ExtractionFailed,
    LlmClaimExtractor,
    placed_by,
)
from yizkr.pipeline.media_stages import PreparationRequest, prepare_container
from yizkr.pipeline.slide_layout import (
    assemble_native_layout,
    native_slide_blocks,
    parse_native_layout,
)
from yizkr.pipeline.source_context import bind_source_support
from yizkr.pipeline.transcribe import (
    DECK_ENGINE,
    PageProvenance,
    TranscribedPage,
    Transcription,
)

pytestmark = pytest.mark.usefixtures("fixed_capture")


def prepared_sources(tmp_path):
    """A native caption and a distinct inherited original picture."""
    source = tmp_path / "fictional.pptx"
    archive = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(slides.presentation())) as original,
        zipfile.ZipFile(archive, "w") as output,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "ppt/slides/slide3.xml":
                data = data.replace(b"Fictional slide 1", CAPTION.encode())
            output.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data)
    source.write_bytes(archive.getvalue())
    directory = tmp_path / "products"
    prepared = prepare_container(
        PreparationRequest(source, existing_directory=directory)
    )
    assert prepared is not None
    located = project_sources(
        prepared.inventory, directory, data=prepared.data
    )
    body = next(p for p in located.bodies if p.locator.position == 1)
    assert not directory.exists()
    return prepared, located, body, directory


def reading(sources, body):
    blocks = native_slide_blocks(sources.body_bytes[body.id], body.id)
    proof = parse_native_layout('{"rows":[["b1"]]}', blocks)
    text = assemble_native_layout(blocks, proof)
    notes = next(p for p in sources.notes if p.locator.position == 1)
    composite = text + f"\n[Slide 1 notes]\n{notes.text}"
    result = Transcription(
        artifact=sources.inventory.source,
        text=composite,
        engine=DECK_ENGINE,
        pages=[
            TranscribedPage(
                page=1,
                start=0,
                end=len(composite),
                body_end=len(text),
                images=list(sources.sources),
                status="transcribed",
                provenance=PageProvenance(
                    method="vision",
                    engine="fictional-layout",
                    native_layout=proof,
                ),
            )
        ],
    )
    result.source_context = source_context(result, sources)
    return result


def quote(text):
    return {
        "source": {
            "artifact": ArtifactRef.from_bytes(
                b"untrusted model owner"
            ).model_dump(),
            "page_number": "1",
            "transcription_excerpt": {"text": text},
        }
    }


def test_native_caption_and_image_keep_distinct_sources_after_roundtrip(
    tmp_path,
):
    _, sources, body, _ = prepared_sources(tmp_path)
    result = reading(sources, body)
    stored = Transcription.model_validate_json(result.model_dump_json())
    stored.source_context = source_context(stored, sources)
    owner = next(iter(sources.sources.values()))
    assert len(sources.sources) == 1
    assert placed_by(stored, stored.artifact) == {
        stored.artifact.sha1: {owner.owner.sha1}
    }
    proposed = caption(
        subject=owner.owner.model_dump(), source=stored.artifact, page="1"
    )
    batch = LlmClaimExtractor(ScriptedLlm([ndjson([proposed])])).extract(
        stored, stored.artifact
    )
    assert len(batch.claims) == 1
    claim = batch.claims[0]
    assert claim.subject == owner.owner
    assert claim.source.artifact == stored.artifact
    assert claim.source.support.part_id == body.id
    assert claim.source.support.locator.kind == "slide_body"
    accepted, rejected = bind_source_support(
        [quote(CAPTION), quote(slides.NOTES)], stored
    )
    assert not rejected
    assert [
        item["source"]["support"]["locator"]["kind"] for item in accepted
    ] == ["slide_body", "speaker_notes"]

    image_reading = Transcription(
        artifact=owner.owner,
        text="Fictional image reading",
        engine="fictional-image",
        pages=[
            TranscribedPage(
                page=1,
                start=0,
                end=len("Fictional image reading"),
                status="transcribed",
                provenance=PageProvenance(
                    method="vision", engine="fictional-image"
                ),
            )
        ],
    )
    image_reading.source_context = image_source_context(
        image_reading, [sources.support(p) for p in owner.part_ids]
    )
    accepted, rejected = bind_source_support(
        [quote(image_reading.text)], image_reading
    )
    assert not rejected
    assert accepted[0]["source"]["artifact"] == owner.owner.model_dump()
    assert (
        accepted[0]["source"]["support"]["locator"]["kind"]
        == "embedded_resource"
    )
    assert len(stored.source_context.placements) == 1
    assert stored.source_context.placements[0].owner == owner.owner


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "part",
        "bytes",
        "basis",
        "invented-word",
        "missing-bytes",
        "changed-bytes",
    ],
)
def test_unproved_native_body_stays_unsupported_but_notes_remain_located(
    tmp_path, damage
):
    _, sources, body, _ = prepared_sources(tmp_path)
    result = reading(sources, body)
    page = result.pages[0]
    proof = page.provenance.native_layout
    if damage == "missing":
        page.provenance.native_layout = None
    elif damage in {"part", "bytes", "basis"}:
        fields = {
            "part": {"part_id": "another-part"},
            "bytes": {"byte_object": ArtifactRef.from_bytes(b"another slide")},
            "basis": {"basis_sha256": "f" * 64},
        }
        page.provenance.native_layout = proof.model_copy(update=fields[damage])
    elif damage == "invented-word":
        extra = " image-only words"
        result.text = (
            result.text[: page.body_end] + extra + result.text[page.body_end :]
        )
        page.body_end += len(extra)
        page.end += len(extra)
    elif damage == "missing-bytes":
        sources.body_bytes.clear()
    else:
        sources.body_bytes[body.id] = b"another slide"
    result.source_context = source_context(result, sources)
    spans = result.source_context.readings
    assert spans[0].state == "unsupported"
    assert spans[0].owner is None
    assert spans[1].state == "attributed"
    accepted, rejected = bind_source_support(
        [quote(CAPTION), quote(slides.NOTES)], result
    )
    assert len(rejected) == 1
    assert len(accepted) == 1
    assert (
        accepted[0]["source"]["support"]["locator"]["kind"] == "speaker_notes"
    )

    owner = next(iter(sources.sources.values())).owner
    extractor = LlmClaimExtractor(
        ScriptedLlm(
            [
                ndjson(
                    [
                        caption(
                            subject=owner.model_dump(),
                            source=result.artifact,
                            page="1",
                        )
                    ]
                )
            ]
        )
    )
    with pytest.raises(ExtractionFailed):
        extractor.extract(result, result.artifact)
    assert any("source" in item for item in extractor.incomplete_reasons)


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "excluded-part", "excluded-source"]
)
def test_projection_never_trusts_unverified_or_excluded_native_bytes(
    tmp_path, damage
):
    prepared, sources, body, directory = prepared_sources(tmp_path)
    data = dict(prepared.data)
    excluded_sha = None
    if damage == "missing":
        data.pop(body.byte_object.sha1)
    elif damage == "corrupt":
        data[body.byte_object.sha1] = b"unverified bytes"
    elif damage == "excluded-part":
        excluded_sha = body.byte_object.sha1
    else:
        excluded_sha = sources.inventory.source.sha1
    projected = project_sources(
        prepared.inventory,
        directory,
        data=data,
        excluded=lambda sha, names: sha == excluded_sha,
    )
    assert body.id not in projected.body_bytes
    assert not directory.exists()


def test_slide_layout_proof_cannot_authorize_a_pdf_composite(tmp_path):
    _, sources, body, _ = prepared_sources(tmp_path)
    result = reading(sources, body)
    sources.inventory.container = "pdf"
    body.kind = "page_body"
    result.source_context = source_context(result, sources)
    assert result.source_context.readings[0].state == "unsupported"
    accepted, rejected = bind_source_support([quote(CAPTION)], result)
    assert not accepted
    assert len(rejected) == 1
