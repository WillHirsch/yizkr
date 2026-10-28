"""Persisted container readings are evidence, not self-authenticating input."""

import json

import pytest

from tests.fixtures import pdfs
from tests.unit.test_container_consumers import container_corpus
from tests.unit.test_llmcache import CachedRun
from tests.unit.test_orchestrate import quiet
from tests.unit.test_pdf_pages import (
    EXCERPT,
    LETTER_IMAGE,
    LETTER_SHA1,
    Reader,
    RefusingReader,
    booklet,
    document,
    folder,
    run,
    typed,
)
from yizkr.pipeline import transcribe
from yizkr.pipeline.container_inventory import CONTAINER_INVENTORY
from yizkr.pipeline.presentation import derived_transcription
from yizkr.pipeline.scratchwrite import _registration
from yizkr.pipeline.textlayer import png


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
    assert any(sha1 == LETTER_SHA1 for _, sha1 in reader.read)
    assert "Wastl" not in json.loads(path.read_text())["text"]


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
