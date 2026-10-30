"""Context readiness and freshness across the ordinary pipeline boundaries."""

from datetime import UTC, datetime

import pytest

from tests.fixtures import pdfs
from tests.unit.test_orchestrate import approve_all_claims, make_batch
from tests.unit.test_pdf_exclusions import (
    EXTRACT,
    TITLE,
    RecordingClient,
    scan,
)
from yizkr.claims import ClaimBatch, ExtractionRunMetadata
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline import text_stages
from yizkr.pipeline.estimate import CHUNKS, Estimator, _keyed, _Made, _Tally
from yizkr.pipeline.llm import cached_client
from yizkr.pipeline.llmcache import CacheAsIs, EngineCache
from yizkr.pipeline.orchestrate import Orchestrator
from yizkr.pipeline.review import export_review_csv
from yizkr.pipeline.scratch import ScratchDriver


def inputs(tmp_path, *, standalone):
    folder = tmp_path / "sources"
    folder.mkdir()
    if standalone:
        (folder / "0-original.jpg").write_bytes(pdfs.JPEG)
    for number in (1, 2):
        page = pdfs.Page().text(pdfs.lines(TITLE))
        # Distinct containers, with the same original source at actual pages.
        pages = [page] * number + [scan(pdfs.jpeg())]
        (folder / f"{number}-booklet.pdf").write_bytes(pdfs.pdf(pages))
    return folder


def driver_for(tmp_path, folder, monkeypatch):
    driver = ScratchDriver(
        folder,
        tmp_path / "run",
        exclusions=ExclusionList(),
        until="extraction",
        log=lambda *_a, **_k: None,
    )
    client = RecordingClient()
    monkeypatch.setattr(driver, "_llm", lambda *_a: client)
    captured = []

    def extract(request, client=None, **kwargs):
        captured.append(request)
        batch = ClaimBatch(
            extraction_run=ExtractionRunMetadata(
                model_identifier="fictional",
                pipeline_version="fictional",
                started_at=datetime.now(UTC),
            )
        )
        return text_stages.ExtractionResult(
            "complete",
            batch,
            None,
            [],
            [],
            {},
            [],
            (),
            {},
        )

    monkeypatch.setattr(text_stages, "extract", extract)
    docs = driver.documents()
    driver._corpus_sha1s = {doc.sha1 for doc in docs}
    driver.rundir.start(
        documents=[
            {
                "sha1": doc.sha1,
                "kind": doc.kind,
                "stages": driver._planned(doc),
            }
            for doc in docs
        ]
    )
    pending = driver._stages(docs, driver._canaries(docs))
    return driver, docs, pending, captured


@pytest.mark.parametrize("standalone", [False, True])
def test_first_source_extraction_has_every_matching_container(
    tmp_path,
    monkeypatch,
    standalone,
):
    folder = inputs(tmp_path, standalone=standalone)
    driver, docs, pending, captured = driver_for(tmp_path, folder, monkeypatch)
    stop, left = driver._dispatch(pending)
    assert stop is None and left == []
    contexts = [request for request in captured if request.context is not None]
    assert len(contexts) == 1  # one owner, including a standalone duplicate
    (request,) = contexts
    assert request.transcription.record().pages[0].text == EXTRACT
    assert {group.container.sha1 for group in request.context.groups} == {
        doc.sha1 for doc in docs if doc.kind == "pdf"
    }
    assert request.context.has_context
    assert not request.context.gaps
    if standalone:
        assert request.transcription.source_context is None
    events = driver.rundir.events()
    extracted = next(
        i
        for i, event in enumerate(events)
        if event.get("subject") == request.artifact.sha1
        and event.get("stage") == "extraction"
        and event["event"] == "started"
    )
    for parent in driver._interpretation_parents[request.artifact.sha1]:
        assert (
            sum(
                event.get("subject") == parent.sha1
                and event.get("stage") == "transcription"
                and event["event"] == "done"
                for event in events
            )
            == 1
        )
        assert any(
            event.get("subject") == parent.sha1
            and event.get("stage") == "transcription"
            and event["event"] == "done"
            for event in events[:extracted]
        )


@pytest.mark.parametrize("action", ["pause", "abort"])
def test_parent_control_holds_dependent_source_without_spinning(
    tmp_path,
    monkeypatch,
    action,
):
    folder = inputs(tmp_path, standalone=True)
    driver, _docs, pending, captured = driver_for(
        tmp_path, folder, monkeypatch
    )
    driver.rundir.request(action, kind="pdf")
    waits = []

    def resume(stops, announced):
        waits.append(stops)
        assert not captured
        driver.rundir.request("resume", kind="pdf")

    monkeypatch.setattr(driver, "_await_resume", resume)
    stop, left = driver._dispatch(pending)
    if action == "pause":
        assert len(waits) == 1
        assert stop is None and not left
        assert len([request for request in captured if request.context]) == 1
    else:
        assert stop and "aborted" in stop
        assert left and not captured and not waits
    assert not any(
        event["event"] == "failed" for event in driver.rundir.events()
    )


def test_failed_parent_is_a_gap_even_when_a_stale_reading_exists(
    tmp_path,
    monkeypatch,
):
    folder = inputs(tmp_path, standalone=True)
    driver, docs, pending, captured = driver_for(tmp_path, folder, monkeypatch)
    parent = next(doc for doc in docs if doc.kind == "pdf")
    driver._prepare(parent)
    driver._transcription(parent)
    driver._views.clear()  # Current dispatch must not use the persisted view.
    transcribe = driver._step_transcribe

    def fail(doc):
        if doc.sha1 == parent.sha1:
            raise RuntimeError("fictional unavailable reading")
        return transcribe(doc)

    monkeypatch.setattr(driver, "_refresh_requested", lambda *_a: True)
    monkeypatch.setattr(driver, "_step_transcribe", fail)
    stop, left = driver._dispatch(pending)
    assert stop is None and not left
    (request,) = [request for request in captured if request.context]
    assert request.context.gaps
    assert parent.sha1 not in {
        group.container.sha1 for group in request.context.groups
    }
    assert parent.sha1 in {gap.container.sha1 for gap in request.context.gaps}


def test_context_appearance_change_and_removal_invalidate_both_views(
    tmp_path,
    monkeypatch,
):
    folder = inputs(tmp_path, standalone=True)
    driver, docs, pending, captured = driver_for(tmp_path, folder, monkeypatch)
    assert driver._dispatch(pending) == (None, [])
    target = next(doc for doc in docs if doc.kind == "media")
    reading = driver._transcription(target)
    original = target.transcription.read_bytes()
    context = driver._interpretation_contexts[target.sha1]
    target.critique.write_text("[]")
    target.derivations.record(target.critique)
    assert target.derivations.current(target.batch)
    assert target.derivations.current(target.critique)

    for changed in (
        context.model_copy(update={"groups": context.groups[:1]}),
        None,
        context,
    ):
        monkeypatch.setattr(
            driver, "_interpretation_context", lambda *_a, value=changed: value
        )
        driver._prepare_interpretation_context(target, reading)
        assert target.transcription.read_bytes() == original
        assert not target.derivations.current(target.batch)
        assert not target.derivations.current(target.critique)
        target.derivations.record(target.batch)
        target.derivations.record(target.critique)
    driver._prepare_interpretation_context(target, reading)
    assert target.derivations.current(target.batch)
    assert target.derivations.current(target.critique)


def test_ordinary_parallel_preparation_reads_each_parent_only_once(
    tmp_path,
    monkeypatch,
):
    folder = inputs(tmp_path, standalone=True)
    driver = Orchestrator(
        folder,
        tmp_path / "run",
        exclusions=ExclusionList(),
        extract_concurrency=2,
        log=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(driver, "_llm", lambda *_a: RecordingClient())
    docs = driver.documents()
    driver.rundir.start(documents=[])
    driver._prepare_context_containers()
    readings = dict(driver._interpretation_readings)
    assert len(readings) == 2
    # The normal parallel extraction workers ask their own transcription
    # again; the preparation phase must have completed publication already.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        actual = list(
            pool.map(
                driver._transcription,
                [doc for doc in docs if doc.kind == "pdf"],
            )
        )
    assert {id(value) for value in actual} == {
        id(value) for value in readings.values()
    }
    assert (
        sum(
            event["event"] == "done" and event.get("stage") == "transcription"
            for event in driver.rundir.events()
        )
        == 2
    )


def test_estimate_context_is_read_only_and_future_readings_are_unknown(
    tmp_path,
    monkeypatch,
):
    folder = inputs(tmp_path, standalone=True)
    driver, docs, pending, _captured = driver_for(
        tmp_path, folder, monkeypatch
    )
    client = RecordingClient()
    client.engine, client.model, client.effort = "fictional", "fictional", None
    client.config_hash = "fictional-context-reader"
    cache = EngineCache(tmp_path / "cache")
    monkeypatch.setattr(
        driver,
        "_llm",
        lambda role, _doc: cached_client(client, cache, role=role),
    )
    assert driver._dispatch(pending) == (None, [])
    original = next(doc for doc in docs if doc.kind == "media")
    reading = driver._transcription(original)
    estimator = Estimator(folder, driver.state_dir, exclusions=ExclusionList())
    planned = estimator.documents()
    target = next(doc for doc in planned if doc.sha1 == original.sha1)
    estimator._interpretation_readings = dict(driver._interpretation_readings)
    before = {
        path: path.read_bytes()
        for path in driver.state_dir.rglob("*")
        if path.is_file()
    }
    assert not estimator._estimate_interpretation_context(target, reading)
    assert (
        estimator._interpretation_contexts[target.sha1]
        == driver._interpretation_contexts[target.sha1]
    )
    assert (
        estimator._stands(target, target.batch, ClaimBatch.model_validate_json)
        is not None
    )

    # A fresh estimate reconstructs the same context from ordinary cache hits,
    # including image readings whose temporary preparation is then closed.
    fresh = Estimator(folder, tmp_path / "fresh", exclusions=ExclusionList())
    fresh.documents()
    fresh._as_is = CacheAsIs(tmp_path / "cache")
    monkeypatch.setattr(
        fresh,
        "_engine_for",
        lambda role: fresh._keyed.setdefault(role, _keyed(client)),
    )
    for parent in fresh._interpretation_parents[target.sha1]:
        fresh._tally = _Tally(fresh._asked)
        result = fresh._transcription(parent, True, _Made())
        assert result.view is not None and result.tally.hits > 0
        fresh._interpretation_readings[parent.sha1] = result.view
    assert not fresh._estimate_interpretation_context(target, reading)
    assert (
        fresh._interpretation_contexts[target.sha1].prompt_block()
        == driver._interpretation_contexts[target.sha1].prompt_block()
    )
    assert not fresh.state_dir.exists()
    parent = estimator._interpretation_parents[target.sha1][0]
    estimator._context_made[parent.sha1] = _Made()  # a future model reading
    roles = []
    monkeypatch.setattr(estimator, "_engine_for", roles.append)
    outcome = estimator._extraction(
        target, False, _Made(view=reading, changed=False)
    )
    assert outcome.estimated == CHUNKS and not outcome.reused
    assert outcome.tally.hits == 0
    assert roles == ["extract"]
    assert before == {
        path: path.read_bytes()
        for path in driver.state_dir.rglob("*")
        if path.is_file()
    }


def test_unchanged_claims_preserve_approval_after_context_refresh(
    tmp_path,
    monkeypatch,
):
    folder = inputs(tmp_path, standalone=True)
    driver, docs, pending, _captured = driver_for(
        tmp_path, folder, monkeypatch
    )
    assert driver._dispatch(pending) == (None, [])
    target = next(doc for doc in docs if doc.kind == "media")
    batch = make_batch()
    batch.entities[0].label = "Gustav"
    batch.claims[0].subject.label = "Gustav"
    batch.claims[0].value.year = 1919
    batch.claims[0].source.artifact = driver._transcription(target).artifact
    driver._update_batch_view(target, batch)
    target.derivations.record(target.batch)
    target.critique.write_text("[]")
    target.derivations.record(target.critique)
    export_review_csv(batch, target.review)
    target.derivations.record(target.review)
    approve_all_claims(target)
    target.approved.write_text(batch.model_dump_json())
    assert target.derivations.consents(target.review_reviewed)
    decision = target.review_reviewed.read_bytes()
    calls = []

    def extract(request, client=None, **kwargs):
        calls.append("extract")
        return text_stages.ExtractionResult(
            "complete",
            batch,
            None,
            [],
            [],
            {},
            [],
            (),
            {},
        )

    def critique(request, client):
        calls.append("critique")
        return text_stages.CritiqueResult([], "complete", [], [], {})

    monkeypatch.setattr(text_stages, "extract", extract)
    monkeypatch.setattr(text_stages, "critique", critique)
    context = driver._interpretation_contexts[target.sha1]
    # One corroborating placement disappears; primary evidence and proposed
    # values remain the same. Both model stages reconsider the input.
    smaller = context.model_copy(update={"groups": context.groups[:1]})
    monkeypatch.setattr(driver, "_interpretation_context", lambda *_a: smaller)
    driver._derive(target)
    driver._critique(target, batch)
    assert calls == ["extract", "critique"]
    assert target.review_reviewed.read_bytes() == decision
    assert target.derivations.consents(target.review_reviewed)
    assert target.approved.exists()
    driver._derive(target)
    driver._critique(target, batch)
    assert calls == ["extract", "critique"]
