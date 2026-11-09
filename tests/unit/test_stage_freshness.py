"""Generation records track inputs and preserve reusable products.

The production scratch stage dispatcher uses authored model answers. These
checks cover publication and freshness; durable consent has its own suites.
"""

import json

import pytest

from tests.fixtures.document_processing import (
    ScriptedDriver,
    completed_run,
    engine_steps,
    make_run,
    prepare_document,
)
from yizkr.claims import ClaimBatch
from yizkr.pipeline import stage_planning
from yizkr.pipeline.document_state import FAILED


def process(driver):
    """Dispatch document stages without changing the run's saved settings."""
    (doc,) = driver.documents()
    return prepare_document(driver, doc, until="critique")


@pytest.mark.parametrize(
    ("settings", "expected_calls"),
    [
        ({"extract_model": "claude-fictional-2"}, ["manifest", "chunk"]),
        ({"review_effort": "low"}, ["critique"]),
        ({"epochs": {"extract": 1}}, ["manifest", "chunk"]),
    ],
)
def test_processing_change_recomputes_without_replacing_identical_products(
    tmp_path, settings, expected_calls
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    batch = doc.batch.read_bytes()

    changed = make_run(tmp_path, **settings)
    assert process(changed) != FAILED
    assert engine_steps(changed.calls) == expected_calls
    (doc,) = changed.documents()
    assert doc.batch.read_bytes() == batch
    assert not list(doc.dir.glob("*.superseded*"))


def test_missing_processing_metadata_is_rebuilt_without_replacing_products(
    tmp_path,
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    saved = json.loads(doc.derivations.path.read_text())
    del saved["processing"]
    doc.derivations.path.write_text(json.dumps(saved))

    upgraded = make_run(tmp_path)
    plan = upgraded._inspect_view(
        doc, doc.batch, ClaimBatch.model_validate_json
    ).plan
    assert plan.action == "recompute"
    assert "missing-processing" in plan.reasons
    assert process(upgraded) != FAILED
    assert engine_steps(upgraded.calls) == ["manifest", "chunk", "critique"]
    (doc,) = upgraded.documents()
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
    assert process(failing) == FAILED
    (failed,) = failing.documents()
    assert failed.state.status == FAILED
    assert failed.batch.read_bytes() == batch
    plan = failing._inspect_view(
        failed, failed.batch, ClaimBatch.model_validate_json
    ).plan
    assert plan.action == "recompute"
    assert "processing.resources.pipeline/extract.py" in (
        plan.changed_dependencies
    )

    retry = make_run(tmp_path)
    assert process(retry) != FAILED
    assert engine_steps(retry.calls) == ["manifest", "chunk"]
    (retried,) = retry.documents()
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
    assert process(blocked) == FAILED
    assert not blocked.calls
    assert doc.batch.read_bytes() == batch
    assert doc.derivations.path.read_bytes() == recorded


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

    extract = ScriptedDriver._step_extract

    def changing_resource(self, doc, transcription):
        nonlocal observed
        result = extract(self, doc, transcription)
        observed = "changed-during-call"
        doc.pointer.write_text("Rahel was born in 1871.")
        return result

    monkeypatch.setattr(stage_planning, "processing_dependencies", processing)
    monkeypatch.setattr(ScriptedDriver, "_step_extract", changing_resource)
    driver = make_run(tmp_path)
    assert driver.run() == 0
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


def test_invalid_processing_section_rebuilds_without_replacing_products(
    tmp_path,
):
    first = completed_run(tmp_path)
    (doc,) = first.documents()
    contents = json.loads(doc.derivations.path.read_text())
    contents["processing"] = None
    doc.derivations.path.write_text(json.dumps(contents))
    before = doc.batch.read_bytes()
    assert process(make_run(tmp_path)) != FAILED
    assert doc.batch.read_bytes() == before
