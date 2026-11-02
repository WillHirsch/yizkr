"""Process evidence does not become source meaning or human consent."""

import pytest

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from tests.unit.test_orchestrate import (
    approve_all_claims,
    approve_all_equivalences,
    make_run,
)
from tests.unit.test_run_provenance import producer
from tests.unit.test_scratch_places import PlaceRun
from tests.unit.test_text_stages import ScriptedClient, birth_answer
from yizkr.claims import ClaimBatch
from yizkr.core.provenance import ProcessRecord
from yizkr.pipeline import clustering, places, scratchwrite
from yizkr.pipeline.llm import cached_client, open_cache
from yizkr.pipeline.review_mentions import canonical_basis, observe_mentions
from yizkr.pipeline.text_stages import (
    CritiqueRequest,
    ExtractionRequest,
    critique,
    extract,
)


def test_provenance_preserves_clustering_and_durable_review_basis():
    reading = source_case("naturalisation-1952").transcription()
    original = birth_batch("naturalisation-1952")
    enriched = original.model_copy(deep=True)
    enriched.extraction_run.provenance = producer()
    reread = reading.model_copy(update={"provenance": producer()})
    owner = reading.artifact.sha1
    config = clustering.load_config()
    assert clustering.inputs_key(
        [clustering.Manifest(owner, original)], config
    ) == clustering.inputs_key([clustering.Manifest(owner, enriched)], config)
    before = observe_mentions(owner, original, reading)
    after = observe_mentions(owner, enriched, reread)
    assert before == after
    stable = {
        observation.local_id: str(index)
        for index, observation in enumerate(before)
    }
    for local_id in stable:
        assert canonical_basis(original, local_id, stable) == canonical_basis(
            enriched, local_id, stable
        )


def test_provenance_preserves_actual_extraction_and_critique_cache_keys(
    tmp_path,
):
    reading = source_case("naturalisation-1952").transcription()
    enriched = reading.model_copy(update={"provenance": producer()})
    engine = ScriptedClient(
        birth_answer(), '{"done": true, "findings_emitted": 0}'
    )
    engine.model = "fictional-model"
    engine.config_hash = "a" * 64
    cache = open_cache(tmp_path / "responses")
    try:
        extractor = cached_client(engine, cache, role="extract")
        critic = cached_client(engine, cache, role="critique")
        first = extract(
            ExtractionRequest(reading, reading.artifact, strategy="single"),
            extractor,
        ).require_complete()
        assert (
            critique(CritiqueRequest(first, reading), critic).status
            == "complete"
        )
        second = extract(
            ExtractionRequest(enriched, enriched.artifact, strategy="single"),
            extractor,
        ).require_complete()
        assert (
            second.extraction_run.provenance != first.extraction_run.provenance
        )
        assert (
            critique(CritiqueRequest(second, enriched), critic).status
            == "complete"
        )
        # Both second-stage requests must hit their genuine cache entry. The
        # scripted engine has no third answer and cannot hide another call.
        assert len(engine.prompts) == 2
    finally:
        cache.close()


def test_identical_batch_with_new_process_evidence_keeps_existing_approvals(
    tmp_path, monkeypatch
):
    driver = make_run(tmp_path)
    driver.run()
    (doc,) = driver.documents()
    approve_all_equivalences(doc)
    driver.run()
    (doc,) = driver.documents()
    approve_all_claims(doc)
    driver.run()
    (doc,) = driver.documents()
    kept = {
        path: path.read_bytes()
        for path in (
            doc.batch,
            doc.equivalences_reviewed,
            doc.review_reviewed,
            doc.approved,
        )
    }
    assert doc.derivations.consents(doc.equivalences_reviewed)
    assert doc.derivations.consents(doc.review_reviewed)
    proposed = ClaimBatch.model_validate_json(doc.batch.read_text())
    proposed.extraction_run.provenance = producer()
    doc.derivations.forget(doc.batch)
    calls = []
    monkeypatch.setattr(
        driver,
        "_step_batch",
        lambda _doc: calls.append("recompute") or proposed,
    )
    assert driver._derive(doc) == proposed
    assert calls == ["recompute"]
    assert {path: path.read_bytes() for path in kept} == kept
    assert doc.derivations.consents(doc.equivalences_reviewed)
    assert doc.derivations.consents(doc.review_reviewed)
    assert doc.derivations.current(doc.batch)
    assert doc.derivations.current(doc.review)


@pytest.mark.write_side
def test_saved_place_plan_keeps_original_producer_after_client_change(
    tmp_path, monkeypatch
):
    run = PlaceRun(tmp_path, monkeypatch)
    run.past_the_barrier(until="write")
    real = places.ensure_places_run_item

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("fictional application interruption")

    monkeypatch.setattr(places, "ensure_places_run_item", interrupted)
    with pytest.raises(RuntimeError, match="application interruption"):
        run.stage("places")
    planned = run.record("places")
    original = ProcessRecord.model_validate(planned["producer_origin"])
    assert original.stages[-1].model_identifier == run.engine.model_identifier
    run.engine.model_identifier = "another-available-client"
    run.engine.cli_version = "another-cli-version"
    run.engine.config_hash = "b" * 64
    monkeypatch.setattr(places, "ensure_places_run_item", real)
    monkeypatch.setattr(
        scratchwrite,
        "place_client",
        lambda *_args: pytest.fail("saved plan must not borrow a new client"),
    )
    run.stage("places")
    completed = run.record("places")
    assert completed["planned"] == planned["planned"]
    assert completed["producer_origin"] == planned["producer_origin"]
    assert len(run.engine.prompts) == 1
    properties, _ = run.wiki.vocabulary()
    models = [
        qualifier["datavalue"]["value"]
        for statement in run.wiki.entities[completed["run_qid"]]["claims"][
            properties["run_stage"]
        ]
        for qualifier in statement.get("qualifiers", {}).get(
            properties["model_identifier"], []
        )
    ]
    assert models == [original.stages[-1].model_identifier]
