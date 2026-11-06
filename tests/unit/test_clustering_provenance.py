"""Identity producer evidence is separate from clustering decisions."""

import hashlib
import json

from tests.unit.test_adjudication import Engine, context_of, gustav, truths
from tests.unit.test_clustering import stored
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline import adjudication, clustering


def test_deterministic_map_records_producer_and_reuses_it(tmp_path):
    docs = [gustav("one", dated=True)]
    documents = stored(tmp_path, docs)
    expected = clustering.cluster_corpus([doc.manifest for doc in docs])
    made = clustering.ensure_identity_map(
        tmp_path / "maps", documents, run_manifest={"run_id": "fictional-run"}
    )
    record = ProcessRecord.model_validate(made.identity_map["provenance"])
    assert record.metadata["identity_map_hash"] == expected["hash"]
    assert record.metadata["producer_run_id"] == "fictional-run"
    assert len(record.metadata["producer_run_manifest_sha256"]) == 64
    assert made.identity_map["mentions"] == expected["mentions"]
    assert made.identity_map["clusters"] == expected["clusters"]
    (stage,) = record.stages
    assert stage.process == "deterministic"
    assert stage.model is None and not stage.prompt_hashes
    assert stage.configuration["clustering"] == (
        clustering.load_config().model_dump(mode="json")
    )
    assert stage.code is not None and stage.dependency_hash
    assert stage.started_at <= stage.ended_at
    reused = clustering.ensure_identity_map(tmp_path / "maps", documents)
    assert reused.reused
    assert reused.identity_map["provenance"] == made.identity_map["provenance"]


def test_adjudication_records_actual_client_and_loaded_prompt(
    tmp_path, monkeypatch
):
    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    engine = Engine(truths(docs))
    engine.model = "fictional-observed-model"
    engine.model_identifier = "fictional-observed-engine"
    context = context_of(docs, tmp_path, engine)
    monkeypatch.setattr(
        context.run,
        "manifest",
        lambda: {"run_id": "fictional-run", "project_stage": "development"},
    )
    template = adjudication.PROMPT
    complete = engine.complete

    def answer(prompt, image_paths=()):
        result = complete(prompt, image_paths)
        monkeypatch.setattr(adjudication, "PROMPT", "changed after invocation")
        return result

    monkeypatch.setattr(engine, "complete", answer)
    made = adjudication.ensure_adjudicated(context)
    record = ProcessRecord.model_validate(made.identity_map["provenance"])
    (stage,) = record.stages
    assert stage.process == "mixed"
    assert stage.model == "fictional-observed-model"
    assert stage.model_identifier == "fictional-observed-engine"
    assert stage.model != stage.configuration["configured_client"]["model"]
    assert stage.prompt_hashes == {
        "pipeline.adjudication.PROMPT": hashlib.sha256(
            template.encode()
        ).hexdigest()
    }
    assert record.metadata["identity_map_hash"] == made.identity_map["hash"]
    assert record.metadata["producer_run_id"] == "fictional-run"
    assert made.record["provenance"] == made.identity_map["provenance"]
    monkeypatch.setattr(adjudication, "PROMPT", template)
    reused = adjudication.ensure_adjudicated(context)
    assert reused.reused and context.driver.clients == 1
    assert reused.record["provenance"] == made.record["provenance"]


def test_no_adjudication_calls_claim_no_model_or_prompt(tmp_path):
    docs = [gustav("one", dated=True)]
    context = context_of(docs, tmp_path, Engine(truths(docs)))
    made = adjudication.ensure_adjudicated(context)
    assert context.driver.clients == 0
    record = ProcessRecord.model_validate(made.identity_map["provenance"])
    (stage,) = record.stages
    assert stage.process == "deterministic"
    assert stage.model is None and not stage.prompt_hashes


def test_reusing_legacy_outputs_does_not_invent_producer_evidence(tmp_path):
    docs = [gustav("one", dated=True), gustav("two", dated=False)]
    context = context_of(docs, tmp_path, Engine(truths(docs)))
    made = adjudication.ensure_adjudicated(context)
    for filename, value in (
        (clustering.IDENTITY_MAP, made.identity_map),
        (adjudication.RECORD, made.record),
    ):
        value.pop("provenance")
        (context.directory / filename).write_text(json.dumps(value))
    reused = adjudication.ensure_adjudicated(context)
    assert reused.reused and context.driver.clients == 1
    assert "provenance" not in reused.record
    assert "provenance" not in reused.identity_map
