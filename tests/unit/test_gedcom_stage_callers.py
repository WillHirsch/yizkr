"""Deterministic GEDCOM proposals share callers, reuse and review gates.

These checks use repository-fictional GEDCOM and the real adapter, drivers
and estimator. Model and cache entry points refuse access; no provider or
graph transport is substituted to make a deterministic run possible.
"""

import json

import pytest

from tests.fixtures.example_universe import GEDCOM_FIXTURE
from yizkr import cli
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import gedcom_stages, llm, scratch, stage_planning
from yizkr.pipeline.estimate import Estimator, StageCalls
from yizkr.pipeline.orchestrate import AWAITING_CLAIMS, FAILED, Orchestrator
from yizkr.pipeline.scratch import ScratchDriver


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    source = folder / "example-family.ged"
    source.write_bytes(GEDCOM_FIXTURE.read_bytes())
    return source


@pytest.fixture(autouse=True)
def no_model_infrastructure(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("GEDCOM-only work touched model or cache infrastructure")

    for name in ("make_client", "read_cache", "cache_holder"):
        monkeypatch.setattr(llm, name, refuse)
    monkeypatch.setattr(scratch, "claude_logged_in", refuse)
    monkeypatch.setattr(llm.ClaudeCodeClient, "available", refuse)
    for name in (
        "_llm",
        "_step_transcribe",
        "_step_extract",
        "_step_critique",
    ):
        monkeypatch.setattr(Orchestrator, name, refuse)


def make_driver(source, state, *, workflow="reviewed", **settings):
    options = {
        "exclusions": ExclusionList(),
        "log": lambda *_a, **_k: None,
        **settings,
    }
    if workflow == "reviewed":
        return Orchestrator(source.parent, state, **options)
    # A file cannot hold a model-response cache. Deterministic work has no
    # reason to open it, test its contents, or require provider credentials.
    cache = state.parent / "not-a-model-cache"
    cache.write_text("fictional cache-unavailable sentinel")
    options.update(
        until="gedcom", corpus_stages=(), cache_dir=cache, min_free=0
    )
    cls = Estimator if workflow == "estimate" else ScratchDriver
    return cls(source.parent, state, **options)


def saved_batch(driver):
    (doc,) = driver.documents()
    return doc, ClaimBatch.model_validate_json(doc.batch.read_text())


def estimate(source, state):
    result = make_driver(source, state, workflow="estimate").estimate()
    assert not result.preflight
    assert result.total().engine_calls == 0
    assert result.total().hits == 0
    return result.stages["gedcom"]


@pytest.mark.parametrize("workflow", ["reviewed", "scratch"])
def test_gedcom_callers_preserve_proposals_without_models_or_approval(
    source, tmp_path, workflow
):
    standalone_path = tmp_path / "standalone.json"
    assert (
        cli.main(
            [
                "ingest",
                "gedcom",
                "--file",
                str(source),
                "--out",
                str(standalone_path),
            ]
        )
        == 0
    )
    standalone = ClaimBatch.model_validate_json(standalone_path.read_text())
    driver = make_driver(source, tmp_path / workflow, workflow=workflow)
    assert driver.run() == (2 if workflow == "reviewed" else 0)
    doc, proposed = saved_batch(driver)
    assert proposed.claims == standalone.claims
    assert proposed.entities == standalone.entities
    assert proposed.extraction_run.model_identifier == "gedcom-adapter"
    assert proposed.claims
    assert all(
        claim.source.artifact == ArtifactRef.from_path(source)
        for claim in proposed.claims
    )
    assert any(
        claim.property == "date_of_birth"
        and getattr(claim.value, "year", None) == 1836
        and claim.value.precision == "year"
        for claim in proposed.claims
    )
    assert not doc.transcription.exists()
    assert not doc.critique.exists()
    assert not doc.approved.exists()
    assert not doc.write_result.exists()
    assert doc.state.detail["stage_results"]["gedcom"]["status"] == "complete"
    if workflow == "reviewed":
        assert doc.state.status == AWAITING_CLAIMS
        assert doc.review.exists()
        assert not doc.review_reviewed.exists()


def test_saved_gedcom_run_and_estimate_agree_on_reuse(source, tmp_path):
    state = tmp_path / "scratch"
    assert estimate(source, state) == StageCalls(made=1)
    first = make_driver(source, state, workflow="scratch")
    assert first.run() == 0
    doc, _ = saved_batch(first)
    batch = doc.batch.read_bytes()
    derivations = doc.derivations.path.read_bytes()
    assert estimate(source, state) == StageCalls(reused=1)
    assert doc.batch.read_bytes() == batch
    assert doc.derivations.path.read_bytes() == derivations

    repeated = make_driver(source, state, workflow="scratch")
    assert repeated.run() == 0
    doc, _ = saved_batch(repeated)
    assert doc.batch.read_bytes() == batch
    assert doc.derivations.path.read_bytes() == derivations
    assert (
        repeated._inspect_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "reuse"
    )


def changed_adapter_dependencies(monkeypatch):
    previous = stage_planning.processing_dependencies

    def changed(stage, **kwargs):
        result = previous(stage, **kwargs)
        if stage == "gedcom":
            result["resources"]["pipeline/gedcom.py"] = "changed-adapter"
        return result

    monkeypatch.setattr(stage_planning, "processing_dependencies", changed)


def test_adapter_dependency_change_invalidates_run_and_estimate(
    source, tmp_path, monkeypatch
):
    state = tmp_path / "scratch"
    first = make_driver(source, state, workflow="scratch")
    assert first.run() == 0
    doc, _ = saved_batch(first)
    before = doc.derivations.path.read_bytes()
    changed_adapter_dependencies(monkeypatch)

    assert estimate(source, state) == StageCalls(made=1)
    assert doc.derivations.path.read_bytes() == before
    changed = make_driver(source, state, workflow="scratch")
    inspection = changed._inspect_view(
        doc, doc.batch, ClaimBatch.model_validate_json
    )
    assert inspection.plan.action == "recompute"
    assert "processing.resources.pipeline/gedcom.py" in (
        inspection.plan.changed_dependencies
    )
    assert changed.run() == 0
    assert estimate(source, state) == StageCalls(reused=1)
    assert doc.derivations.path.read_bytes() != before


def test_model_configuration_does_not_invalidate_gedcom_proposals(
    source, tmp_path
):
    state = tmp_path / "reviewed"
    first = make_driver(source, state)
    assert first.run() == 2
    doc, _ = saved_batch(first)
    batch = doc.batch.read_bytes()
    derivations = doc.derivations.path.read_bytes()
    changed = make_driver(
        source,
        state,
        llm="anthropic",
        extract_model="claude-fictional-2",
        extract_effort="high",
        review_effort="low",
        chunk_concurrency=19,
        epochs={"extract": 3, "critique": 2},
    )
    assert (
        changed._inspect_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "reuse"
    )
    assert changed.run() == 2
    doc, _ = saved_batch(changed)
    assert doc.batch.read_bytes() == batch
    assert doc.derivations.path.read_bytes() == derivations
    assert doc.state.status == AWAITING_CLAIMS


def test_failed_new_generation_preserves_old_proposals_but_requires_retry(
    source, tmp_path, monkeypatch
):
    state = tmp_path / "reviewed"
    first = make_driver(source, state)
    assert first.run() == 2
    doc, _ = saved_batch(first)
    batch = doc.batch.read_bytes()
    review = doc.review.read_bytes()
    processing = json.loads(doc.derivations.path.read_text())["processing"]
    changed_adapter_dependencies(monkeypatch)

    def unavailable(request):
        raise OSError("fictional GEDCOM snapshot unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(gedcom_stages, "derive_gedcom", unavailable)
        failed = make_driver(source, state)
        assert failed.run() == 1
        doc, _ = saved_batch(failed)
        assert doc.state.status == FAILED
        assert (
            doc.state.detail["stage_results"]["gedcom"]["status"] == "failed"
        )
        assert doc.batch.read_bytes() == batch
        assert doc.review.read_bytes() == review
        assert json.loads(doc.derivations.path.read_text())["processing"] == (
            processing
        )
        assert (
            failed._inspect_view(
                doc, doc.batch, ClaimBatch.model_validate_json
            ).plan.action
            == "recompute"
        )

    retried = make_driver(source, state)
    assert retried.run() == 2
    doc, _ = saved_batch(retried)
    assert doc.state.status == AWAITING_CLAIMS
    assert doc.state.detail["stage_results"]["gedcom"]["status"] == "complete"
    assert doc.batch.read_bytes() == batch
    assert doc.review.read_bytes() == review
    assert (
        retried._inspect_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "reuse"
    )
    assert not doc.approved.exists()
    assert not doc.write_result.exists()


@pytest.mark.parametrize("alias", ["same-path", "symlink", "hardlink"])
def test_standalone_gedcom_refuses_output_alias_of_its_source(
    source, tmp_path, alias
):
    original = source.read_bytes()
    output = source
    if alias != "same-path":
        output = tmp_path / "alias.ged"
        if alias == "symlink":
            output.symlink_to(source)
        else:
            output.hardlink_to(source)
    with pytest.raises(ValueError, match="overlaps input"):
        cli.main(
            [
                "ingest",
                "gedcom",
                "--file",
                str(source),
                "--out",
                str(output),
            ]
        )
    assert source.read_bytes() == original
    assert output.read_bytes() == original


def test_standalone_failed_derivation_preserves_accepted_output(
    source, tmp_path
):
    output = tmp_path / "accepted.json"
    command = [
        "ingest",
        "gedcom",
        "--file",
        str(source),
        "--out",
        str(output),
    ]
    assert cli.main(command) == 0
    accepted = output.read_bytes()
    source.write_text("0 HEAD\nnot a GEDCOM record\n")
    with pytest.raises(ValueError, match="line 2"):
        cli.main(command)
    assert output.read_bytes() == accepted
    assert ClaimBatch.model_validate_json(accepted).claims

    source.write_bytes(GEDCOM_FIXTURE.read_bytes())
    assert cli.main(command) == 0
    assert (
        ClaimBatch.model_validate_json(output.read_bytes()).claims
        == ClaimBatch.model_validate_json(accepted).claims
    )
