"""Cancellation reaches owned work and preserves pause/failure policy."""

import threading

import pytest

from tests.fixtures.records import record_answer
from tests.unit.test_pdf_exclusions import EXTRACT
from tests.unit.test_registrar import PORTRAIT, RESCAN
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.cancellation import current_cancellation
from yizkr.pipeline.rundir import RunDir, run_state
from yizkr.pipeline.scratch import ScratchDriver
from yizkr.pipeline.workers import WorkerShutdownTimeout

HANG_GUARD = 30


def sources(tmp_path, *, text=False):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "a-portrait.png").write_bytes(PORTRAIT)
    if text:
        (folder / "b-letter.txt").write_text(EXTRACT)
    else:
        (folder / "b-portrait.png").write_bytes(RESCAN)
    return folder


def scratch(tmp_path, monkeypatch, *, text=False):
    driver = ScratchDriver(
        sources(tmp_path, text=text),
        tmp_path / "run",
        cache_dir=tmp_path / "cache",
        exclusions=ExclusionList(),
        language="en",
        until="transcription",
        poll=0.01,
        log=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(driver, "_preflight_host", lambda: None)
    monkeypatch.setattr(driver, "_preflight_models", lambda _past: None)
    return driver


@pytest.mark.parametrize("scope", ["run", "kind:media"])
def test_abort_reaches_active_reading_and_resume_keeps_completed_views(
    tmp_path, monkeypatch, scope
):
    driver = scratch(tmp_path, monkeypatch, text=scope != "run")
    calls = []
    abort = True

    class Reader:
        model_identifier = "fictional-reader"

        def complete(self, prompt, image_paths=()):
            calls.append(image_paths[0].name)
            if abort:
                driver.rundir.request(
                    "abort", kind="media" if scope != "run" else None
                )
                signal = current_cancellation()
                assert signal is not None and signal.wait(HANG_GUARD)
                signal.check()
            return record_answer(EXTRACT)

    monkeypatch.setattr(driver, "_llm", lambda *_args: Reader())
    assert driver.run() == 2
    docs = {doc.source.name: doc for doc in driver.documents()}
    first = docs["a-portrait.png"]
    assert calls == ["a-portrait.png"]
    assert not first.transcription.exists()
    assert (
        first.state.detail["stage_results"]["transcription"]["status"]
        == "cancelled"
    )
    assert (
        run_state(driver.rundir)[(first.sha1, "transcription")].status
        == "cancelled"
    )
    completed = {}
    if scope != "run":
        letter = docs["b-letter.txt"]
        completed[letter.transcription] = letter.transcription.read_bytes()
        assert (
            run_state(driver.rundir)[(letter.sha1, "transcription")].status
            == "done"
        )
    abort = False
    assert driver.run() == 0
    assert all(
        path.read_bytes() == payload for path, payload in completed.items()
    )
    assert all(doc.transcription.is_file() for doc in driver.documents())
    assert driver.rundir.manifest()["invocations"][-1]["outcome"] == "finished"


def test_cancelled_reading_cannot_publish_a_late_result(tmp_path, monkeypatch):
    driver = scratch(tmp_path, monkeypatch)

    class Reader:
        model_identifier = "fictional-reader"

        def complete(self, prompt, image_paths=()):
            driver.cancellation.cancel("operator cancelled")
            return record_answer(EXTRACT)

    monkeypatch.setattr(driver, "_llm", lambda *_args: Reader())
    assert driver.run() == 2
    assert (
        driver.rundir.manifest()["invocations"][-1]["reason"] == "RunCancelled"
    )
    cancelled = [
        event
        for event in driver.rundir.events()
        if event.get("event") == "cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["reason"] == "RunCancelled"
    assert all(not doc.transcription.exists() for doc in driver.documents())
    assert not any(
        event.get("event") == "done" for event in driver.rundir.events()
    )


def test_uncooperative_worker_timeout_ends_invocation_without_further_dispatch(
    tmp_path, monkeypatch
):
    driver = scratch(tmp_path, monkeypatch)
    dispatched = []

    def timeout(doc, stage):
        dispatched.append((doc.sha1, stage))
        raise WorkerShutdownTimeout(1)

    monkeypatch.setattr(driver, "_run_stage_of", timeout)
    with pytest.raises(WorkerShutdownTimeout):
        driver.run()
    assert len(dispatched) == 1
    assert (
        RunDir(driver.state_dir).manifest()["invocations"][-1]["outcome"]
        == "failed"
    )


def test_pause_does_not_cancel_active_call_or_poison_resumed_work(
    tmp_path, monkeypatch
):
    driver = scratch(tmp_path, monkeypatch)
    calls = []
    observed_pause = threading.Event()
    original_control = RunDir.control

    def control(run):
        state = original_control(run)
        if state.scopes.get("kind:media") == "paused":
            observed_pause.set()
        return state

    monkeypatch.setattr(RunDir, "control", control)

    class Reader:
        model_identifier = "fictional-reader"

        def complete(self, prompt, image_paths=()):
            calls.append(image_paths[0].name)
            if len(calls) == 1:
                driver.rundir.request("pause", kind="media")
                signal = current_cancellation()
                assert signal is not None
                assert observed_pause.wait(HANG_GUARD)
                assert not signal.is_set()
            return record_answer(EXTRACT)

    monkeypatch.setattr(driver, "_llm", lambda *_args: Reader())
    monkeypatch.setattr(
        driver,
        "_sleep",
        lambda _seconds: driver.rundir.request("resume", kind="media"),
    )
    assert driver.run() == 0
    assert calls == ["a-portrait.png", "b-portrait.png"]
    assert not any(
        event.get("event") == "cancelled" for event in driver.rundir.events()
    )


def test_scoped_cancellation_cannot_return_as_complete(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from yizkr.pipeline.cancellation import RunCancelled

    driver = scratch(tmp_path, monkeypatch)
    doc = driver.documents()[0]

    def compute():
        driver._record_stage_result(
            doc,
            "extraction",
            SimpleNamespace(status="complete", dependencies={}, artifacts=()),
        )
        current_cancellation().cancel("stage:extraction aborted")
        return "late result"

    with pytest.raises(RunCancelled, match="stage:extraction aborted"):
        driver._run_stage(doc, "extraction", compute)
    assert not driver.cancellation.is_set()
    assert doc.state.detail["stage_results"]["extraction"] == {
        "status": "cancelled"
    }
    assert (
        run_state(driver.rundir)[(doc.sha1, "extraction")].status
        == "cancelled"
    )
    assert not any(
        event["event"] == "done" for event in driver.rundir.events()
    )
