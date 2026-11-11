"""Persisted container readings are evidence, not self-authenticating input."""

import json
from datetime import timedelta

import pytest

from tests.fixtures import pdfs
from tests.fixtures.document_processing import quiet
from tests.unit.test_container_consumers import container_corpus
from tests.unit.test_llmcache import CachedRun
from tests.unit.test_pdf_pages import (
    EXCERPT,
    LETTER_IMAGE,
    LETTER_SHA1,
    Reader,
    RefusingReader,
    booklet,
    document,
    excerpt_of,
    folder,
    run,
    transcription_of,
    typed,
)
from tests.unit.test_run_provenance import START, fixed_capture  # noqa: F401
from yizkr.pipeline import media_stages, transcribe
from yizkr.pipeline import run_provenance as provenance
from yizkr.pipeline.container_inventory import CONTAINER_INVENTORY
from yizkr.pipeline.document_state import BATCHED
from yizkr.pipeline.presentation import derived_transcription
from yizkr.pipeline.scratchwrite import _registration
from yizkr.pipeline.textlayer import png
from yizkr.pipeline.transcribe import reading_digest

pytestmark = pytest.mark.usefixtures("fixed_capture")


@pytest.fixture(autouse=True)
def renderer(monkeypatch):
    """No poppler tool runs: a scripted reader reads every PDF here, as
    in test_pdf_pages.py, whose `renderer` fixture this mirrors."""
    monkeypatch.setattr(transcribe, "require_pdf_renderer", lambda *a: None)


@pytest.mark.parametrize("kind", ["pdf", "pptx"])
@pytest.mark.parametrize("missing_inventory", [False, True])
def test_edited_image_reading_is_regenerated_before_reuse(
    tmp_path, kind, missing_inventory
):
    corpus, name = container_corpus(tmp_path, kind)
    original = document(
        run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT})), name
    )
    path = derived_transcription(original.dir, LETTER_SHA1)
    value = json.loads(path.read_text())
    assert "Gustl" in value["text"]
    value["text"] = value["text"].replace("Gustl", "Wastl")
    path.write_text(json.dumps(value))
    if missing_inventory:
        (original.dir / CONTAINER_INVENTORY).unlink()
    reader = Reader({LETTER_SHA1: EXCERPT})
    restored = document(run(tmp_path, corpus, reader), name)
    assert restored.state.error is None
    # Invalid persisted evidence is regenerated from the intact response
    # cache; it never becomes authoritative merely because it exists.
    assert reader.read == []
    assert "Wastl" not in json.loads(path.read_text())["text"]


def test_pptx_reading_recovery_survives_backward_wall_clock(
    tmp_path, monkeypatch
):
    corpus, name = container_corpus(tmp_path, "pptx")
    first = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    original = document(first, name)
    original_reading = transcription_of(original)
    excerpt = excerpt_of(first, LETTER_SHA1)
    source_bytes = excerpt.source.read_bytes()
    retained = {
        path: path.read_bytes()
        for path in (original.batch, excerpt.transcription, excerpt.batch)
    }
    path = derived_transcription(original.dir, LETTER_SHA1)
    image_reading = json.loads(path.read_text())
    assert "Gustl" in image_reading["text"]
    path.write_text(
        json.dumps(
            image_reading
            | {"text": image_reading["text"].replace("Gustl", "Wastl")}
        )
    )

    observed_end = START - timedelta(microseconds=1)
    real_transcribe = media_stages.DeckTranscriber.transcribe

    def read_during_clock_adjustment(reader, source):
        reading = real_transcribe(reader, source)
        # The container capture started before this actual reading. Move
        # only its finish observation backwards; later captures stay ordered.
        monkeypatch.setattr(provenance, "now", lambda: observed_end)
        return reading

    monkeypatch.setattr(
        media_stages.DeckTranscriber,
        "transcribe",
        read_during_clock_adjustment,
    )
    reader = RefusingReader({})
    second = run(tmp_path, corpus, reader)
    restored = document(second, name)
    assert restored.state.error is None
    assert restored.state.status == BATCHED
    assert reader.read == []
    # Repair uses the intact response cache and preserves the image producer,
    # source identity, source locations and existing derived products.
    assert json.loads(path.read_text()) == image_reading
    restored_excerpt = excerpt_of(second, LETTER_SHA1)
    assert restored_excerpt.dir == excerpt.dir
    assert restored_excerpt.source.read_bytes() == source_bytes
    assert {path: path.read_bytes() for path in retained} == retained
    reading = transcription_of(restored)
    assert reading.artifact == original_reading.artifact
    assert reading.source_context == original_reading.source_context
    assert reading_digest(reading) == reading_digest(original_reading)
    record = reading.provenance
    assert record is not None
    stage = record.stages[-1]
    assert record.started_at == stage.started_at == START
    assert record.ended_at is stage.ended_at is None
    assert stage.model_identifier == reader.model_identifier
    assert record.metadata["clock_observation"] == {
        "observed_ended_at": observed_end.isoformat()
    }
    limitation = (
        "transcription: observed wall-clock finish "
        f"{observed_end.isoformat()} "
        f"precedes start {START.isoformat()}; ended_at is unknown"
    )
    assert record.limitations == stage.limitations == (limitation,)

    persisted = restored.transcription.read_bytes()
    monkeypatch.setattr(
        media_stages.DeckTranscriber,
        "transcribe",
        lambda *_args: pytest.fail("the repaired reading must be reusable"),
    )
    reused = document(run(tmp_path, corpus, RefusingReader({})), name)
    assert reused.state.error is None
    assert reused.state.status == BATCHED
    assert reused.transcription.read_bytes() == persisted


@pytest.mark.parametrize("other", ["container", "standalone"])
def test_discovery_refuses_equal_decodings_from_distinct_originals(
    tmp_path, other
):
    corpus = folder(tmp_path, family_booklet_2011=booklet())
    if other == "container":
        page = typed(EXCERPT).image(LETTER_IMAGE, 72, 520, 300, 200)
        (corpus / "letter-compilation.pdf").write_bytes(pdfs.pdf([page]))
    else:
        (corpus / "letter-excerpt-1939.png").write_bytes(
            png(300, 120, 1, LETTER_IMAGE.data)
        )
    driver = CachedRun(
        corpus, tmp_path / "state", engine=RefusingReader({}), log=quiet
    )
    docs = driver.documents()
    containers = [doc for doc in docs if doc.source.suffix == ".pdf"]
    for doc in containers:
        sources = driver._container_sources(doc)
        assert LETTER_SHA1 not in sources.sources
        assert any(
            "equal prepared image bytes" in u.reason
            for u in sources.unresolved
        )
    assert not (tmp_path / "state").exists()


def test_registration_context_tracks_bindings_and_placements_not_readings(
    tmp_path,
):
    corpus, name = container_corpus(tmp_path, "pptx")
    driver = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    doc = document(driver, name)
    projection = driver._container_sources(doc)
    before = _registration(doc, projection)
    source = projection.sources[LETTER_SHA1]
    source.reading_path = None
    source.reading_digest = None
    assert _registration(doc, projection) == before
    part = next(
        p for p in projection.inventory.parts if p.id in source.part_ids
    )
    part.owner_binding.basis = "prepared-part"
    assert _registration(doc, projection) != before
    part.owner_binding.basis = "original-media"
    source.placements = source.placements[1:]
    assert _registration(doc, projection) != before


def test_registration_metadata_recovery_uses_current_preparation(tmp_path):
    corpus, name = container_corpus(tmp_path, "pptx")
    driver = run(tmp_path, corpus, Reader({LETTER_SHA1: EXCERPT}))
    doc = document(driver, name)
    before = _registration(doc, driver._container_sources(doc))
    (doc.dir / CONTAINER_INVENTORY).unlink()
    rebuilt = CachedRun(
        corpus, tmp_path / "state", engine=RefusingReader({}), log=quiet
    )
    current = document(rebuilt, name)
    assert (
        _registration(current, rebuilt._container_sources(current)) == before
    )
    assert not (doc.dir / CONTAINER_INVENTORY).exists()
