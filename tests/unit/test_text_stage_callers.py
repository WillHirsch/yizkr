"""Equivalent text inputs exercise CLI, reviewed and scratch ingestion.

Only the model and host preflight are substituted. Fixed fictional answers
exercise the production readers, extraction, critique, persistence and review
gates; they provide no evidence about model interpretation quality.
"""

import json

import pytest

from tests.fixtures.semantic_acceptance import source_case
from tests.unit.test_text_stages import ScriptedClient, birth_answer
from yizkr import cli
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import llm
from yizkr.pipeline.llm import PassPaused
from yizkr.pipeline.orchestrate import AWAITING_CLAIMS, Orchestrator
from yizkr.pipeline.scratch import ScratchDriver
from yizkr.pipeline.transcribe import Transcription


def extraction_client():
    return ScriptedClient(
        json.dumps(
            {
                "entities": [
                    {
                        "id": "gustav",
                        "label": "Gustave Lobig",
                        "kind": "human",
                        "mention_lines": [1],
                    }
                ],
                "preparation": None,
            }
        ),
        birth_answer(),
    )


@pytest.mark.parametrize("workflow", ["reviewed", "scratch"])
@pytest.mark.parametrize("finished_critique", [True, False])
def test_text_callers_preserve_the_same_proposals_and_advisory_critique(
    tmp_path, monkeypatch, workflow, finished_critique
):
    case = source_case("naturalisation-1952")
    folder = tmp_path / "sources"
    folder.mkdir()
    media = folder / "naturalisation-1952.txt"
    media.write_text(case.text, encoding="utf-8")
    reading_path = tmp_path / "standalone-transcription.json"
    assert (
        cli.main(
            [
                "ingest",
                "transcribe",
                "--file",
                str(media),
                "--text-file",
                str(media),
                "--language",
                "en",
                "--out",
                str(reading_path),
            ]
        )
        == 0
    )
    monkeypatch.setattr(
        llm, "make_client", lambda *_a, **_k: extraction_client()
    )
    standalone_path = tmp_path / "standalone-batch.json"
    assert (
        cli.main(
            [
                "ingest",
                "extract",
                "--engine",
                "llm",
                "--strategy",
                "chunked",
                "--transcription",
                str(reading_path),
                "--out",
                str(standalone_path),
            ]
        )
        == 0
    )
    standalone = ClaimBatch.model_validate_json(standalone_path.read_text())

    critic_answer = '{"done":true}' if finished_critique else "unfinished"
    monkeypatch.setattr(
        Orchestrator,
        "_llm",
        lambda self, role, doc: (
            extraction_client()
            if role == "extract"
            else ScriptedClient(critic_answer)
        ),
    )
    state = tmp_path / workflow
    options = {
        "language": "en",
        "exclusions": ExclusionList(),
        "log": lambda *_a, **_k: None,
    }
    if workflow == "scratch":
        # The script owns no provider or deployed service to preflight.
        monkeypatch.setattr(
            ScratchDriver, "_preflight_host", lambda self: None
        )
        monkeypatch.setattr(
            ScratchDriver, "_preflight_models", lambda self, past: None
        )
        driver = ScratchDriver(
            folder,
            state,
            until="critique",
            corpus_stages=(),
            cache_dir=tmp_path / "cache",
            **options,
        )
        assert driver.run() == 0
    else:
        driver = Orchestrator(folder, state, **options)
        assert driver.run() == 2

    (doc,) = driver.documents()
    run_batch = ClaimBatch.model_validate_json(doc.batch.read_text())
    assert run_batch.claims == standalone.claims
    assert run_batch.entities == standalone.entities
    (claim,) = run_batch.claims
    assert claim.property == "date_of_birth"
    assert (claim.value.year, claim.value.month, claim.value.day) == (
        1919,
        7,
        1,
    )
    assert claim.source.artifact == ArtifactRef.from_path(media)
    assert claim.source.transcription_excerpt.text in case.text
    standalone_reading = Transcription.model_validate_json(
        reading_path.read_text()
    )
    run_reading = Transcription.model_validate_json(
        doc.transcription.read_text()
    )
    assert run_reading == standalone_reading
    assert json.loads(doc.critique.read_text()) == []
    outcomes = doc.state.detail["stage_results"]
    assert outcomes["extraction"]["status"] == "complete"
    assert outcomes["critique"]["status"] == (
        "complete" if finished_critique else "incomplete"
    )
    if not finished_critique:
        assert doc.state.detail["attention"]["critique"]
    assert not doc.approved.exists()
    assert not doc.write_result.exists()
    if workflow == "reviewed":
        assert doc.state.status == AWAITING_CLAIMS
        assert doc.review.exists()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RuntimeError("engine unavailable"), "failed"),
        (PassPaused("provider paused"), "paused"),
        (KeyboardInterrupt(), "cancelled"),
    ],
)
def test_failed_refresh_does_not_report_the_previous_attempt_complete(
    tmp_path, monkeypatch, error, status
):
    case = source_case("naturalisation-1952")
    folder = tmp_path / "sources"
    folder.mkdir()
    media = folder / "naturalisation-1952.txt"
    media.write_text(case.text)
    driver = Orchestrator(
        folder,
        tmp_path / "state",
        language="en",
        exclusions=ExclusionList(),
        log=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(driver, "_llm", lambda *_a: extraction_client())
    (doc,) = driver.documents()
    reading = driver._step_transcribe(doc)
    completed = driver._step_extract(doc, reading)
    assert completed.claims
    assert (
        doc.state.detail["stage_results"]["extraction"]["status"] == "complete"
    )

    monkeypatch.setattr(driver, "_llm", lambda *_a: ScriptedClient(error))
    with pytest.raises(type(error)):
        driver._step_extract(doc, reading)
    outcome = doc.state.detail["stage_results"]["extraction"]
    assert outcome["status"] == status
    assert "artifacts" not in outcome
