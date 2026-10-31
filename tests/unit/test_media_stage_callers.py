"""Driver publication and failure boundaries for shared media operations."""

from pathlib import Path

import pytest

from tests.fixtures import pdfs
from tests.fixtures.records import record_answer
from tests.unit.test_pdf_exclusions import EXTRACT, RecordingClient, scan
from tests.unit.test_pdf_pages import EXCERPT
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import media_stages, orchestrate
from yizkr.pipeline.llm import PassPaused
from yizkr.pipeline.orchestrate import Orchestrator


def driver_for(tmp_path, monkeypatch, *, image=False, client=None):
    folder = tmp_path / "sources"
    folder.mkdir(exist_ok=True)
    source = folder / (
        "birth-extract-1938.jpg" if image else "family-booklet-2011.pdf"
    )
    source.write_bytes(pdfs.JPEG if image else pdfs.pdf([scan(pdfs.jpeg())]))
    driver = Orchestrator(
        folder,
        tmp_path / "state",
        exclusions=ExclusionList(),
        language="en",
        log=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        driver, "_llm", lambda *_a: client or RecordingClient()
    )
    (doc,) = driver.documents()
    driver._prepare_document(doc)
    return driver, doc


@pytest.mark.parametrize("publication", ["images", "transcription"])
def test_media_products_close_when_publication_fails(
    tmp_path, monkeypatch, publication
):
    driver, doc = driver_for(tmp_path, monkeypatch)
    operation = media_stages.read_media
    results = []

    def read(*args, **kwargs):
        result = operation(*args, **kwargs)
        assert result.transcription.reading is None
        assert result.products is not None
        assert Path(result.products.folder.name).is_dir()
        results.append(result)
        return result

    monkeypatch.setattr(media_stages, "read_media", read)

    def fail(*_args, **_kwargs):
        raise OSError("fictional publication failure")

    if publication == "images":
        monkeypatch.setattr(driver, "_keep_reading", fail)
    else:
        write = orchestrate._write_view

        def write_view(path, value):
            if path == doc.transcription:
                fail()
            return write(path, value)

        monkeypatch.setattr(orchestrate, "_write_view", write_view)
    with pytest.raises(OSError, match="fictional publication failure"):
        driver._step_transcribe(doc)
    (result,) = results
    assert not Path(result.products.folder.name).exists()
    assert not doc.transcription.exists()
    assert doc.state.detail["stage_results"]["transcription"] == {
        "status": "failed"
    }
    assert not any(
        event["event"] == "done" and event.get("stage") == "transcription"
        for event in driver.rundir.events()
    )


def test_cleanup_failure_does_not_report_a_completed_media_stage(
    tmp_path, monkeypatch
):
    driver, doc = driver_for(tmp_path, monkeypatch)
    operation = media_stages.read_media
    folders = []

    def read(*args, **kwargs):
        result = operation(*args, **kwargs)
        folders.append(Path(result.products.folder.name))
        close = result.close

        def fail_cleanup():
            close()
            raise OSError("fictional cleanup failure")

        monkeypatch.setattr(result, "close", fail_cleanup)
        return result

    monkeypatch.setattr(media_stages, "read_media", read)
    with pytest.raises(OSError, match="fictional cleanup failure"):
        driver._step_transcribe(doc)
    # Publication has happened, but the failed stage is not recorded complete.
    assert doc.transcription.is_file()
    assert all(not folder.exists() for folder in folders)
    assert doc.state.detail["stage_results"]["transcription"] == {
        "status": "failed"
    }
    assert not any(
        event["event"] == "done" and event.get("stage") == "transcription"
        for event in driver.rundir.events()
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RuntimeError("engine unavailable"), "failed"),
        (PassPaused("provider paused"), "paused"),
        (KeyboardInterrupt(), "cancelled"),
    ],
)
def test_failed_media_refresh_replaces_prior_success_status(
    tmp_path, monkeypatch, error, status
):
    driver, doc = driver_for(tmp_path, monkeypatch, image=True)
    driver._step_transcribe(doc)
    original = doc.transcription.read_bytes()
    assert (
        doc.state.detail["stage_results"]["transcription"]["status"]
        == "complete"
    )

    def fail(*_args):
        raise error

    monkeypatch.setattr(driver, "_llm", fail)
    with pytest.raises(type(error)):
        driver._step_transcribe(doc)
    assert doc.state.detail["stage_results"]["transcription"] == {
        "status": status
    }
    assert doc.transcription.read_bytes() == original


def test_excerpt_reuse_follows_verified_parent_reading_not_just_image_bytes(
    tmp_path, monkeypatch
):
    driver, parent = driver_for(tmp_path, monkeypatch)
    driver._step_transcribe(parent)
    (excerpt,) = driver._excerpts_of(parent, ())
    driver._prepare_document(excerpt)
    first = driver._transcription(excerpt)
    assert EXTRACT in first.text
    excerpt.save()
    original_owner = excerpt.sha1

    class ChangedReading(RecordingClient):
        def complete(self, prompt, image_paths=()):
            return record_answer(EXCERPT)

    resumed, parent = driver_for(
        tmp_path, monkeypatch, client=ChangedReading()
    )
    resumed._step_transcribe(parent)
    (excerpt,) = resumed._excerpts_of(parent, ())
    resumed._prepare_document(excerpt)
    current = resumed._transcription(excerpt)
    assert excerpt.sha1 == original_owner
    assert EXCERPT in current.text and EXTRACT not in current.text
    assert current.source_context != first.source_context
    assert any(
        event["event"] == "started"
        and event.get("stage") == "transcription"
        and event.get("subject") == original_owner
        for event in resumed.rundir.events()
    )
