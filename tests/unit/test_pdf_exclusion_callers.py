"""Current PDF exclusions protect standalone calls and persisted run reuse."""

from __future__ import annotations

import json

import pytest

from tests.fixtures import pdfs
from tests.unit.test_corpus import env, run_cli  # noqa: F401
from tests.unit.test_pdf_exclusions import (
    EXTRACT,
    JPEG_SHA1,
    RecordingClient,
    scan,
    source,
)
from tests.unit.test_pdf_pages import (
    Reader,
    document,
    folder,
    renderer,  # noqa: F401
    run,
    transcription_of,
)
from yizkr.core.corpus import EXCLUDE_FILE_VAR, ExclusionList
from yizkr.pipeline import llm, transcribe
from yizkr.pipeline.estimate import Estimator, _Made
from yizkr.pipeline.orchestrate import FAILED
from yizkr.pipeline.transcribe import LlmVisionTranscriber

pytestmark = pytest.mark.usefixtures("env")


def policy(tmp_path, monkeypatch):
    path = tmp_path / "exclude.txt"
    path.write_text(JPEG_SHA1 + "\n")
    monkeypatch.setenv(EXCLUDE_FILE_VAR, str(path))


def no_client(*args, **kwargs):
    raise AssertionError("an excluded input acquired a model client")


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_standalone_vision_checks_before_acquiring_client(
    tmp_path, monkeypatch, suffix
):
    policy(tmp_path, monkeypatch)
    path = source(tmp_path, scan(pdfs.jpeg()))
    path = path.rename(path.with_suffix(suffix))
    monkeypatch.setattr(llm, "make_client", no_client)
    output = tmp_path / "transcription.json"
    with pytest.raises(SystemExit, match="excluded embedded image"):
        run_cli(
            "ingest",
            "transcribe",
            "--engine",
            "llm",
            "--file",
            str(path),
            "--out",
            str(output),
        )
    assert not output.exists()


def test_standalone_permitted_vision_sends_only_checked_representation(
    tmp_path, monkeypatch
):
    policy(tmp_path, monkeypatch)
    page = scan()
    page.images["Unused"] = pdfs.jpeg()
    path = source(tmp_path, page)
    client = RecordingClient()
    monkeypatch.setattr(llm, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(
        transcribe, "preflight_vision_transcription", lambda *a: None
    )
    output = tmp_path / "transcription.json"
    assert (
        run_cli(
            "ingest",
            "transcribe",
            "--engine",
            "llm",
            "--file",
            str(path),
            "--out",
            str(output),
        )
        == 0
    )
    assert len(client.sent()) == 1
    assert pdfs.JPEG not in client.sent()[0]
    saved = json.loads(output.read_text())
    assert saved["input_representation"] == "rendered-pdf-v1"


def test_standalone_extraction_refuses_legacy_pdf_before_client(
    tmp_path, monkeypatch
):
    policy(tmp_path, monkeypatch)
    path = source(tmp_path, scan())
    # Model the previous supported raw-PDF product, using a canned answer.
    legacy = LlmVisionTranscriber(RecordingClient()).transcribe(path)
    saved = tmp_path / "reading.json"
    saved.write_text(legacy.model_dump_json())
    output = tmp_path / "batch.json"
    monkeypatch.setattr(llm, "make_client", no_client)
    with pytest.raises(SystemExit, match="checked rendered representation"):
        run_cli(
            "ingest",
            "extract",
            "--engine",
            "llm",
            "--transcription",
            str(saved),
            "--media",
            str(path),
            "--out",
            str(output),
        )
    assert not output.exists()


def test_run_rechecks_saved_whole_pdf_against_current_exclusions(tmp_path):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan(pdfs.jpeg())]))
    reader = Reader({"birth-extract-1938.pdf": EXTRACT})
    first = run(tmp_path, corpus, reader)
    doc = document(first, "birth-extract-1938.pdf")
    old_reading = doc.transcription.read_bytes()
    old_batch = doc.batch.read_bytes()
    reader.read.clear()
    again = run(
        tmp_path,
        corpus,
        reader,
        exclusions=ExclusionList(sha1s=frozenset({JPEG_SHA1})),
    )
    refused = document(again, "birth-extract-1938.pdf")
    assert refused.state.status == FAILED
    assert "excluded embedded image" in str(refused.state.error)
    assert reader.read == []
    # Refusal does not destroy the existing proposals or source reading.
    assert doc.transcription.read_bytes() == old_reading
    assert doc.batch.read_bytes() == old_batch


def test_legacy_whole_pdf_is_reread_once_preserving_unchanged_batch(tmp_path):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan()]))
    reader = Reader({"birth-extract-1938.pdf": EXTRACT})
    first = run(tmp_path, corpus, reader)
    doc = document(first, "birth-extract-1938.pdf")
    legacy = transcription_of(doc)
    legacy.input_representation = None
    doc.transcription.write_text(legacy.model_dump_json(indent=2))
    before = doc.batch.read_bytes()
    reader.read.clear()
    refreshed = run(tmp_path, corpus, reader)
    current = document(refreshed, "birth-extract-1938.pdf")
    assert len(reader.read) == 1
    assert transcription_of(current).input_representation == "rendered-pdf-v1"
    assert current.batch.read_bytes() == before
    reader.read.clear()
    run(tmp_path, corpus, reader)
    assert reader.read == []


def test_estimate_reports_saved_pdf_exclusion_without_model_client(tmp_path):
    corpus = folder(tmp_path, birth_extract_1938=pdfs.pdf([scan(pdfs.jpeg())]))
    first = run(tmp_path, corpus, Reader({"birth-extract-1938.pdf": EXTRACT}))
    doc = document(first, "birth-extract-1938.pdf")
    before = doc.transcription.read_bytes()
    estimator = Estimator(
        corpus,
        tmp_path / "state",
        exclusions=ExclusionList(sha1s=frozenset({JPEG_SHA1})),
    )
    result = estimator._transcription(doc, False, _Made())
    assert "excluded embedded image" in result.fails
    assert not result.reused
    assert estimator._clients == {}
    assert doc.transcription.read_bytes() == before


@pytest.mark.parametrize("suffix", ["pdf", "png"])
def test_saved_pdf_correction_is_checked_before_reuse(tmp_path, suffix):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = corpus / f"birth-extract-1938.{suffix}"
    path.write_bytes(pdfs.pdf([scan(pdfs.jpeg())]))
    reader = Reader({"birth-extract-1938.pdf": EXTRACT})
    first = run(tmp_path, corpus, reader)
    doc = document(first, path.name)
    doc.correction.write_text(EXTRACT)
    corrected = run(tmp_path, corpus, reader)
    doc = document(corrected, path.name)
    assert transcription_of(doc).text == EXTRACT
    before = doc.transcription.read_bytes()
    reader.read.clear()
    again = run(
        tmp_path,
        corpus,
        reader,
        exclusions=ExclusionList(sha1s=frozenset({JPEG_SHA1})),
    )
    doc = document(again, path.name)
    assert doc.state.status == FAILED
    assert "excluded embedded image" in str(doc.state.error)
    assert reader.read == []
    assert doc.transcription.read_bytes() == before


def test_standalone_extraction_needs_media_for_embedded_exclusion_check(
    tmp_path, monkeypatch
):
    policy(tmp_path, monkeypatch)
    path = source(tmp_path, scan())
    reading = LlmVisionTranscriber(RecordingClient()).transcribe(path)
    saved = tmp_path / "reading.json"
    saved.write_text(reading.model_dump_json())
    monkeypatch.setattr(llm, "make_client", no_client)
    with pytest.raises(SystemExit, match="give --media"):
        run_cli(
            "ingest",
            "extract",
            "--engine",
            "llm",
            "--transcription",
            str(saved),
            "--out",
            str(tmp_path / "batch.json"),
        )
