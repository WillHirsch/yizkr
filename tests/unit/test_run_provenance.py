"""Portable producer facts and immutable publication."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from tests.unit.test_text_stages import ScriptedClient, birth_answer
from yizkr.claims import ExtractionRunMetadata
from yizkr.core.provenance import (
    CodeProvenance,
    ProcessRecord,
    StageProvenance,
)
from yizkr.pipeline import run_provenance as provenance
from yizkr.pipeline.gedcom_stages import GedcomRequest, derive_gedcom
from yizkr.pipeline.stages import sha256_text
from yizkr.pipeline.text_stages import (
    ExtractionRequest,
    TextReadRequest,
    extract,
    read_text,
)
from yizkr.pipeline.transcribe import Transcription, reading_digest

START = datetime(2026, 1, 1, 12, tzinfo=UTC)
END = datetime(2026, 1, 1, 12, 1, tzinfo=UTC)
CODE = {
    "version": "fixture-version",
    "commit": "1" * 40,
    "dirty": False,
    "diff_sha256": None,
}


@pytest.fixture
def fixed_capture(monkeypatch):
    monkeypatch.setattr(
        provenance, "code_version", lambda **_kwargs: dict(CODE)
    )
    monkeypatch.setattr(provenance, "now", lambda: START)


def producer():
    return ProcessRecord(
        kind="producer",
        started_at=START,
        ended_at=END,
        code=CodeProvenance(**CODE),
        stages=(
            StageProvenance(
                stage="extraction",
                process="model",
                model="fictional-model",
                started_at=START,
                ended_at=END,
                code=CodeProvenance(**CODE),
            ),
        ),
    )


def test_detail_selects_only_complete_explicit_stage_records():
    record = producer()
    detail = {
        "stage_results": {
            "critique": {
                "status": "complete",
                "dependencies": {"provenance": record.model_dump(mode="json")},
            },
            "legacy": {"status": "complete", "dependencies": {}},
            "failed": {
                "status": "incomplete",
                "dependencies": {"provenance": "not a completed record"},
            },
        }
    }
    assert provenance.records_from_detail(detail) == (record,)
    assert provenance.records_from_detail(None) == ()
    assert provenance.records_from_detail({}) == ()


def test_detail_refuses_malformed_present_provenance():
    with pytest.raises(ValueError):
        provenance.records_from_detail(
            {
                "stage_results": {
                    "critique": {
                        "status": "complete",
                        "dependencies": {"provenance": None},
                    }
                }
            }
        )


def test_immutable_record_round_trip_and_concurrent_identical_freeze(tmp_path):
    record = producer()
    with ThreadPoolExecutor(max_workers=2) as workers:
        refs = list(
            workers.map(
                lambda _: provenance.freeze_record(tmp_path, record), range(2)
            )
        )
    assert refs[0] == refs[1]
    assert refs[0].sha256 == provenance.record_digest(record)
    path = tmp_path / f"{refs[0].sha256}.json"
    assert path.read_bytes() == provenance.record_bytes(record)
    assert provenance.read_record(tmp_path, refs[0]) == record
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_record_is_neither_read_nor_overwritten(tmp_path):
    record = producer()
    reference = provenance.freeze_record(tmp_path, record)
    path = tmp_path / f"{reference.sha256}.json"
    path.write_text("corrupt fictional record")
    with pytest.raises(ValueError, match="does not match"):
        provenance.read_record(tmp_path, reference)
    with pytest.raises(ValueError, match="differs"):
        provenance.freeze_record(tmp_path, record)
    assert path.read_text() == "corrupt fictional record"
    assert list(tmp_path.iterdir()) == [path]


def test_failure_before_publication_leaves_no_readable_record(
    tmp_path, monkeypatch
):
    def unavailable(*args):
        raise OSError("fictional publication refusal")

    monkeypatch.setattr(provenance.os, "link", unavailable)
    with pytest.raises(OSError, match="publication"):
        provenance.freeze_record(tmp_path, producer())
    assert list(tmp_path.iterdir()) == []


def test_application_keeps_original_producer_without_adding_a_clock(
    fixed_capture, monkeypatch
):
    original = producer()
    monkeypatch.setattr(
        provenance,
        "code_version",
        lambda **_kwargs: dict(CODE, commit="2" * 40),
    )
    first = provenance.application_record(
        [original],
        kind="batch-write-context",
        stage="write-preparation",
        metadata={"batch": "fictional"},
    )
    monkeypatch.setattr(
        provenance,
        "now",
        lambda: pytest.fail("application snapshot read clock"),
    )
    again = provenance.application_record(
        [original],
        kind="batch-write-context",
        stage="write-preparation",
        metadata={"batch": "fictional"},
    )
    assert first == again
    assert first.stages[0] == original.stages[0]
    assert first.stages[-1].code.commit == "2" * 40
    assert first.started_at is first.ended_at is None
    assert not first.producer_refs


@pytest.mark.parametrize(
    "identifier,process",
    [
        ("gedcom-adapter", "deterministic"),
        ("manual", "manual"),
        ("fictional-model", "unknown"),
    ],
)
def test_legacy_metadata_never_invents_current_producer_facts(
    identifier, process, monkeypatch
):
    monkeypatch.setattr(
        provenance,
        "code_version",
        lambda **_kwargs: pytest.fail("legacy observer read current checkout"),
    )
    old = ExtractionRunMetadata(
        model_identifier=identifier,
        pipeline_version="legacy-fixture",
        software_commit="1234",
        started_at=START,
    )
    record = provenance.legacy_record(old)
    assert record.stages[0].process == process
    assert record.code.commit == "1234"
    assert record.code.dirty is None and record.ended_at is None
    assert record.metadata["legacy_producer_identifier"] == identifier
    assert record.stages[0].model_identifier == (
        identifier if process == "unknown" else None
    )
    assert record.limitations


def test_schema_and_semantic_reading_identity_exclude_provenance():
    batch = birth_batch("naturalisation-1952")
    assert "provenance" not in batch.extraction_run.model_dump()
    assert (
        "provenance"
        not in ExtractionRunMetadata.model_json_schema()["properties"]
    )
    text = source_case("naturalisation-1952").transcription()
    before = reading_digest(text)
    text.provenance = producer()
    assert reading_digest(text) == before
    assert (
        Transcription.model_validate_json(text.model_dump_json()).provenance
        == text.provenance
    )
    assert "provenance" not in Transcription.model_json_schema()["properties"]


def test_capture_records_observed_client_and_resource_facts(
    fixed_capture, monkeypatch
):
    capture = provenance.Capture("places", "model")
    monkeypatch.setattr(provenance, "now", lambda: END)
    client = ScriptedClient()
    client.model = "fictional-model"
    client.effort = "high"
    client.config_hash = "a" * 64
    client.cli_version = "fixture-cli"
    prompt = sha256_text("fictional prompt")
    record = capture.finish(
        {"configuration": {"mode": "fixture"}},
        client=client,
        processing={
            "resources": {
                "pipeline/prompts/places.md": prompt,
                "model/manifest.yaml": "b" * 64,
            }
        },
    )
    stage = record.stages[0]
    assert stage.model == client.model
    assert stage.model_identifier == client.model_identifier
    assert (
        stage.config_hash == client.config_hash
        and stage.cli_version == "fixture-cli"
    )
    assert stage.prompt_hashes == {"pipeline/prompts/places.md": prompt}
    assert stage.started_at == START and stage.ended_at == END
    assert record.manifest_hash == "b" * 64
    assert client.prompts == []


def test_real_text_and_model_stages_carry_their_original_producers(
    tmp_path, fixed_capture
):
    case = source_case("naturalisation-1952")
    path = tmp_path / "naturalisation-1952.txt"
    path.write_text(case.text)
    reading = read_text(
        TextReadRequest(path, path, language="en")
    ).transcription
    assert reading.provenance.stages[0].process == "deterministic"
    assert reading.provenance.stages[0].model_identifier is None
    client = ScriptedClient(birth_answer())
    result = extract(
        ExtractionRequest(reading, reading.artifact, strategy="single"), client
    )
    record = result.require_complete().extraction_run.provenance
    assert record.stages[0] == reading.provenance.stages[0]
    assert record.stages[-1].model_identifier == client.model_identifier
    assert record.stages[-1].prompt_hashes
    assert result.provenance == record


def test_cached_observed_client_retains_known_engine_metadata(tmp_path):
    from yizkr.pipeline.llm import cached_client, observed_client, open_cache

    engine = ScriptedClient()
    engine.model = "fictional-model"
    engine.effort = "high"
    engine.config_hash = "a" * 64
    engine.cli_version = "fixture-cli"
    cache = open_cache(tmp_path / "responses")
    try:
        client = cached_client(
            observed_client(
                engine, role="extract", events=lambda *_a, **_kw: None
            ),
            cache,
            role="extract",
        )
        assert provenance.client_metadata(client) == {
            "model": engine.model,
            "model_identifier": engine.model_identifier,
            "effort": engine.effort,
            "config_hash": engine.config_hash,
            "cli_version": engine.cli_version,
        }
        assert engine.prompts == []
    finally:
        cache.close()


def test_imported_batch_keeps_legacy_or_supplied_producer(
    tmp_path, fixed_capture
):
    reading = source_case("naturalisation-1952").transcription()
    path = tmp_path / "claims.json"
    batch = birth_batch("naturalisation-1952")
    for record in (None, producer()):
        batch.extraction_run.provenance = record
        path.write_text(batch.model_dump_json())
        result = extract(
            ExtractionRequest(
                reading, reading.artifact, strategy="file", claims_file=path
            )
        )
        assert result.require_complete().extraction_run.provenance == record


def test_gedcom_producer_is_deterministic_and_never_a_model(fixed_capture):
    source = Path(__file__).parents[1] / "fixtures" / "example-family.ged"
    result = derive_gedcom(GedcomRequest(source))
    record = result.batch.extraction_run.provenance
    assert len(record.stages) == 1
    assert record.stages[0].stage == "gedcom"
    assert record.stages[0].process == "deterministic"
    assert record.stages[0].model is record.stages[0].model_identifier is None
    assert record.stages[0].prompt_hashes == {}
