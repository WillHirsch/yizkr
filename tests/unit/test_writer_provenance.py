"""Process enrichment preserves durable writes and retained admission."""

import copy
import json
from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import mapped
from tests.unit.test_pipeline import MEDIA_SHA1, FakeWiki, _citing
from yizkr import cli
from yizkr.core import instance
from yizkr.core.exits import EXIT_REFUSED
from yizkr.core.provenance import (
    CodeProvenance,
    ProcessRecord,
    StageProvenance,
)
from yizkr.core.write_recovery import GENERATION_PAGE
from yizkr.pipeline import run_provenance, rundir
from yizkr.pipeline.writer import BatchWriter


@pytest.fixture
def current_code(monkeypatch):
    code = {
        "version": "fictional-pipeline",
        "commit": "b" * 40,
        "dirty": False,
    }
    monkeypatch.setattr(rundir, "code_version", lambda **kw: dict(code))
    monkeypatch.setattr(
        run_provenance, "code_version", lambda **kw: dict(code)
    )
    return code


def producer():
    code = CodeProvenance(
        version="original-pipeline", commit="a" * 40, dirty=False
    )
    started = datetime(2026, 7, 12, 9, tzinfo=UTC)
    ended = datetime(2026, 7, 12, 9, 1, tzinfo=UTC)
    return ProcessRecord(
        kind="producer",
        code=code,
        started_at=started,
        ended_at=ended,
        stages=(
            StageProvenance(
                stage="extract",
                process="model",
                model_identifier="fake-model",
                effort="low",
                code=code,
                started_at=started,
                ended_at=ended,
                prompt_hashes={"prompts/extract.md": "c" * 64},
            ),
        ),
        metadata={
            "dependencies": {"reading_sha256": "d" * 64},
            "processing": {"libraries": {"fictional-parser": "1.2"}},
        },
    )


def graph(wiki):
    return {
        qid: copy.deepcopy(item.get_json())
        for qid, item in wiki.wbi.item.store.items()
    }


def applications(directory):
    return [
        record
        for path in directory.glob("*.json")
        if (record := ProcessRecord.model_validate_json(path.read_text())).kind
        == "batch-write-context"
    ]


def test_enriched_provenance_keeps_creation_bindings_and_repeat_is_noop(
    tmp_path, current_code
):
    wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
    original = _citing(MEDIA_SHA1)
    identity = mapped(original)
    first = BatchWriter(wiki, record_dir=tmp_path).write_batch(
        original, identity=identity
    )
    original_pages = dict(wiki._recovery_pages)
    creations = copy.deepcopy(wiki.wbi.item.creation_history)
    enriched = original.model_copy(deep=True)
    enriched.extraction_run.provenance = producer()

    again = BatchWriter(wiki, record_dir=tmp_path).write_batch(
        enriched, identity=identity
    )
    assert again.run_qid == first.run_qid
    assert again.created_items == first.created_items
    assert again.written == 0
    assert wiki._recovery_pages == original_pages
    assert wiki.wbi.item.creation_history == creations
    assert len(applications(tmp_path)) == 2

    before, writes = graph(wiki), wiki.wbi.item.writes
    files = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    repeated = BatchWriter(wiki, record_dir=tmp_path).write_batch(
        enriched, identity=identity
    )
    assert repeated.run_qid == first.run_qid
    assert graph(wiki) == before
    assert wiki.wbi.item.writes == writes
    assert {
        path.name: path.read_bytes() for path in tmp_path.glob("*.json")
    } == files


def test_application_preserves_the_original_producer_record(
    tmp_path, current_code
):
    wiki = FakeWiki({MEDIA_SHA1: "Q23"})
    batch = _citing(MEDIA_SHA1)
    batch.extraction_run.provenance = original = producer()
    result = BatchWriter(wiki, record_dir=tmp_path).write_batch(
        batch, identity=mapped(batch)
    )
    (application,) = applications(tmp_path)
    assert application.code.commit == current_code["commit"]
    assert application.started_at is application.ended_at is None
    extraction = next(
        stage for stage in application.stages if stage.stage == "extract"
    )
    assert extraction == original.stages[0]
    preparation = next(
        stage
        for stage in application.stages
        if stage.stage == "write-preparation"
    )
    assert preparation.process == "deterministic"
    assert preparation.model is preparation.model_identifier is None
    assert preparation.started_at is preparation.ended_at is None
    assert len(application.producer_refs) == 1
    assert (
        run_provenance.read_record(tmp_path, application.producer_refs[0])
        == original
    )

    properties, _ = wiki.vocabulary()
    stage_statement = next(
        statement
        for statement in wiki.wbi.item.get(result.run_qid).claims.get(
            properties["run_stage"]
        )
        if statement.mainsnak.datavalue["value"] == "extract"
    )
    assert any(
        snak.datavalue["value"] == "a" * 40
        for snak in stage_statement.qualifiers.get(
            properties["software_commit"]
        )
    )


@pytest.mark.parametrize(
    "code",
    [
        {"commit": "b" * 40, "dirty": True},
        {"commit": None, "dirty": False},
        {"commit": "b" * 40, "dirty": None},
    ],
)
def test_retained_refusal_precedes_generation_reservation_and_graph_changes(
    tmp_path, monkeypatch, code
):
    wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
    # A never-initialized target and a host kept marker make any accidental
    # registry generation/reservation visible, before an entity can be written.
    wiki._recovery_pages.pop(GENERATION_PAGE)
    marker = instance.kept_path(wiki.config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("fictional retained target")
    monkeypatch.setattr(rundir, "code_version", lambda **kw: dict(code))
    before, pages = graph(wiki), dict(wiki._recovery_pages)
    batch = _citing(MEDIA_SHA1)
    with pytest.raises(ValueError, match="retained operation requires"):
        BatchWriter(wiki, record_dir=tmp_path).write_batch(
            batch, identity=mapped(batch)
        )
    assert graph(wiki) == before
    assert wiki._recovery_pages == pages
    assert wiki.wbi.item.creation_history == []
    assert wiki.wbi.item.writes == 0
    assert not list(tmp_path.glob("*.json"))


def test_cli_reports_retained_admission_as_refusal_without_mutation(
    tmp_path, monkeypatch, capsys
):
    wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
    marker = instance.kept_path(wiki.config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("fictional retained target")
    monkeypatch.setattr(
        rundir,
        "code_version",
        lambda **kw: {"commit": "b" * 40, "dirty": True},
    )
    monkeypatch.setattr(cli, "_client", lambda args: wiki)
    batch = _citing(MEDIA_SHA1)
    batch_path, identity_path = (
        tmp_path / "batch.json",
        tmp_path / "identity.json",
    )
    batch_path.write_text(batch.model_dump_json())
    identity_path.write_text(json.dumps(mapped(batch).data))
    pages = dict(wiki._recovery_pages)
    status = cli.main(
        [
            "ingest",
            "write",
            "--batch",
            str(batch_path),
            "--identity-map",
            str(identity_path),
        ]
    )
    assert status == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "verified clean software" in err
    assert "Traceback" not in err
    assert wiki._recovery_pages == pages
    assert wiki.wbi.item.writes == 0
