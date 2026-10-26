"""Generation changes preserve unchanged evidence and human decisions.

The existing fictional scripted runner exercises real reading, extraction,
critique and review transitions. Its graph transport is a stand-in; these
checks make no claim about model quality or durable graph recovery.
"""

import json

import pytest

from tests.unit.test_orchestrate import (
    ScriptedOrchestrator,
    completed_run,
    engine_steps,
    make_run,
    wiki_steps,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline import stage_planning
from yizkr.pipeline.orchestrate import FAILED


def decisions(doc):
    return {
        path.name: path.read_bytes()
        for path in (doc.equivalences_reviewed, doc.review_reviewed)
    }


@pytest.mark.parametrize(
    ("settings", "expected_calls"),
    [
        ({"extract_model": "claude-fictional-2"}, ["manifest", "chunk"]),
        ({"review_effort": "low"}, ["critique"]),
        ({"epochs": {"extract": 1}}, ["manifest", "chunk"]),
    ],
)
def test_configuration_change_recomputes_without_revoking_same_decisions(
    tmp_path, settings, expected_calls
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    before = decisions(doc)
    batch = doc.batch.read_bytes()

    changed = make_run(tmp_path, **settings)
    assert changed.run() == 0
    assert engine_steps(changed.calls) == expected_calls
    assert wiki_steps(changed.calls) == []
    (doc,) = changed.documents()
    assert decisions(doc) == before
    assert doc.batch.read_bytes() == batch
    assert not list(doc.dir.glob("*.superseded*"))


def test_legacy_processing_metadata_is_rebuilt_without_discarding_review(
    tmp_path,
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    before = decisions(doc)
    saved = json.loads(doc.derivations.path.read_text())
    del saved["processing"]
    doc.derivations.path.write_text(json.dumps(saved))

    upgraded = make_run(tmp_path)
    plan = upgraded._inspect_view(
        doc, doc.batch, ClaimBatch.model_validate_json
    ).plan
    assert plan.action == "recompute"
    assert "missing-processing" in plan.reasons
    assert upgraded.run() == 0
    assert engine_steps(upgraded.calls) == ["manifest", "chunk", "critique"]
    assert wiki_steps(upgraded.calls) == []
    (doc,) = upgraded.documents()
    assert decisions(doc) == before
    assert set(json.loads(doc.derivations.path.read_text())["processing"]) == {
        "transcription.json",
        "batch.json",
        "critique.json",
    }


def test_failed_new_generation_does_not_mark_previous_batch_current(
    tmp_path, monkeypatch
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    before = decisions(doc)
    batch = doc.batch.read_bytes()
    previous = stage_planning.processing_dependencies

    def new_processing(stage, **kwargs):
        result = previous(stage, **kwargs)
        if stage == "extraction":
            result["resources"]["pipeline/extract.py"] = "new-parser"
        return result

    monkeypatch.setattr(
        stage_planning, "processing_dependencies", new_processing
    )
    failing = make_run(tmp_path, fail_step="manifest")
    assert failing.run() == 1
    (failed,) = failing.documents()
    assert failed.state.status == FAILED
    assert failed.batch.read_bytes() == batch
    assert decisions(failed) == before
    assert wiki_steps(failing.calls) == []
    plan = failing._inspect_view(
        failed, failed.batch, ClaimBatch.model_validate_json
    ).plan
    assert plan.action == "recompute"
    assert "processing.resources.pipeline/extract.py" in (
        plan.changed_dependencies
    )

    retry = make_run(tmp_path)
    assert retry.run() == 0
    assert engine_steps(retry.calls) == ["manifest", "chunk"]
    assert wiki_steps(retry.calls) == []
    (retried,) = retry.documents()
    assert decisions(retried) == before
    assert (
        retry._inspect_view(
            retried, retried.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "reuse"
    )


def test_uninspectable_processing_blocks_before_dispatch(
    tmp_path, monkeypatch
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    before = decisions(doc)
    batch = doc.batch.read_bytes()
    recorded = doc.derivations.path.read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError("a required processing resource cannot be read")

    monkeypatch.setattr(stage_planning, "processing_dependencies", unavailable)
    blocked = make_run(tmp_path)
    assert (
        blocked._inspect_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "blocked"
    )
    assert blocked.run() == 1
    assert not blocked.calls
    assert doc.batch.read_bytes() == batch
    assert doc.derivations.path.read_bytes() == recorded
    assert decisions(doc) == before


def test_generation_keeps_dependencies_observed_before_computation(
    tmp_path, monkeypatch
):
    observed = "before-call"
    dependencies = stage_planning.processing_dependencies

    def processing(stage, **kwargs):
        result = dependencies(stage, **kwargs)
        if stage == "extraction":
            result["resources"]["pipeline/extract.py"] = observed
        return result

    extract = ScriptedOrchestrator._step_extract

    def changing_resource(self, doc, transcription):
        nonlocal observed
        result = extract(self, doc, transcription)
        observed = "changed-during-call"
        doc.pointer.write_text("Rahel was born in 1871.")
        return result

    monkeypatch.setattr(stage_planning, "processing_dependencies", processing)
    monkeypatch.setattr(
        ScriptedOrchestrator, "_step_extract", changing_resource
    )
    driver = make_run(tmp_path)
    assert driver.run() == 2
    (doc,) = driver.documents()
    stored = json.loads(doc.derivations.path.read_text())["processing"]
    assert stored["batch.json"]["resources"]["pipeline/extract.py"] == (
        "before-call"
    )
    assert (
        json.loads(doc.derivations.path.read_text())["derived"]["batch.json"][
            "preparation.pointer.txt"
        ]
        is None
    )
    assert (
        driver._inspect_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        ).plan.action
        == "recompute"
    )


def test_invalid_processing_section_rebuilds_with_review_preserved(tmp_path):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    before = decisions(doc)
    contents = json.loads(doc.derivations.path.read_text())
    contents["processing"] = None
    doc.derivations.path.write_text(json.dumps(contents))
    assert make_run(tmp_path).run() == 0
    assert decisions(doc) == before
