"""Cancellation preserves finished work and remains visible to operators."""

import hashlib
import threading

import pytest

from tests.fixtures.example_universe import SOURCES
from tests.unit.test_llmcache import (
    Engine,
    Refusing,
    WaitCountingLock,
    client,
    live_entries,
)
from tests.unit.test_registrar import PORTRAIT, FakeWiki
from tests.unit.test_registrar import TestStepRegister as RegisterScenario
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
    check_cancelled,
)
from yizkr.pipeline.llm import OutputLimitReached, PassPaused
from yizkr.pipeline.monitor import RunMonitor, render_terminal
from yizkr.pipeline.report import build, render_markdown
from yizkr.pipeline.rundir import CANCELLED, RunDir, run_state

HANG_GUARD = 30


def test_child_pause_stops_siblings_but_parent_can_resume():
    parent = Cancellation()
    child = Cancellation(parent)
    stopped = threading.Event()
    child.add_callback(stopped.set)
    pause = PassPaused("subscription unavailable")
    child.cancel(exception=pause)
    assert stopped.wait(HANG_GUARD)
    with pytest.raises(PassPaused) as caught:
        child.check()
    assert caught.value is pause
    child.cancel("later cancellation cannot replace the pause")
    with pytest.raises(PassPaused):
        child.check()
    child.close()
    with cancellation_scope(Cancellation(parent)):
        check_cancelled()
    parent.check()


def test_parent_stop_reaches_existing_and_late_children():
    parent = Cancellation()
    child = Cancellation(parent)
    parent.cancel("operator abort")
    for signal in (parent, child, Cancellation(parent)):
        with pytest.raises(RunCancelled, match="operator abort"):
            signal.check()
    late = threading.Event()
    child.add_callback(late.set)
    assert late.wait(HANG_GUARD)


def test_finished_scope_does_not_receive_later_parent_stop():
    parent = Cancellation()
    child = Cancellation(parent)
    notified = []
    unregister = child.add_callback(lambda: notified.append(True))
    unregister()
    child.close()
    parent.cancel()
    child.check()
    assert notified == []


def test_cancelled_cache_dispatch_keeps_completed_answer(tmp_path):
    caching = client(tmp_path, Engine())
    answer = caching.complete("Read the fictional letter.")
    before = live_entries(tmp_path)
    signal = Cancellation()
    signal.cancel()
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        caching.complete("Read the fictional letter.")
    assert live_entries(tmp_path) == before
    assert (
        client(tmp_path, Refusing()).complete("Read the fictional letter.")
        == answer
    )


def test_late_answer_from_stopped_work_is_not_cached(tmp_path):
    signal = Cancellation()

    def answer(*_args):
        signal.cancel()
        return "A late unfinished reading."

    caching = client(tmp_path, Engine(answer))
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        caching.complete("Read the fictional letter.")
    assert live_entries(tmp_path) == []


def test_cancelled_output_limit_does_not_publish_a_give_up(tmp_path):
    signal = Cancellation()

    def answer(*_args):
        signal.cancel()
        raise OutputLimitReached()

    caching = client(tmp_path, Engine(answer)).attempt(0)
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        caching.complete("Read the fictional letter.")
    assert live_entries(tmp_path) == []


def test_cancelled_opening_releases_its_unpublished_session(
    tmp_path, monkeypatch
):
    signal = Cancellation()

    def answer(*_args):
        signal.cancel()
        return "Late opening answer."

    engine = Engine(answer)
    released = []
    monkeypatch.setattr(
        engine, "close_conversation", released.append, raising=False
    )
    caching = client(tmp_path, engine)
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        caching.open_conversation("Read the fictional letter.")
    assert len(released) == 1
    assert live_entries(tmp_path) == []


def test_cancelled_replay_preserves_opening_and_releases_new_session(
    tmp_path, monkeypatch
):
    original = client(tmp_path, Engine())
    answer, _ = original.open_conversation("Read the fictional letter.")
    before = live_entries(tmp_path)
    signal = Cancellation()

    def replay(*_args):
        signal.cancel()
        return "A different opening that arrived after cancellation."

    engine = Engine(replay)
    released = []
    monkeypatch.setattr(
        engine, "close_conversation", released.append, raising=False
    )
    caching = client(tmp_path, engine)
    reused, handle = caching.open_conversation("Read the fictional letter.")
    assert reused == answer
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        caching.fork(handle, "Extract the next fictional passage.")
    assert live_entries(tmp_path) == before
    assert not handle.changed
    assert handle.session is None
    assert len(released) == 1


@pytest.mark.parametrize(
    "cause", [RunCancelled(), ValueError("sibling failed")]
)
def test_cancelled_keep_releases_a_changed_replay_session(
    tmp_path, monkeypatch, cause
):
    client(tmp_path, Engine()).open_conversation("Fictional letter.")
    signal = Cancellation()
    engine = Engine(lambda *_args: "A changed opening.")
    released = []

    def keep(_session):
        signal.cancel(exception=cause)
        return {"session.json": b"fictional session"}

    monkeypatch.setattr(engine, "keep_conversation", keep, raising=False)
    monkeypatch.setattr(
        engine, "close_conversation", released.append, raising=False
    )
    caching = client(tmp_path, engine)
    _, handle = caching.open_conversation("Fictional letter.")
    with cancellation_scope(signal), pytest.raises(type(cause)) as caught:
        caching.fork(handle, "Extract the next fictional passage.")
    assert caught.value is cause
    assert len(released) == 1
    assert not list((tmp_path / "cache").glob("*/*.session"))


def test_cancelled_fork_stops_waiting_for_another_replay(tmp_path):
    client(tmp_path, Engine()).open_conversation("Fictional letter.")
    caching = client(tmp_path, Refusing())
    _, handle = caching.open_conversation("Fictional letter.")
    handle.lock = lock = WaitCountingLock()
    signal = Cancellation()
    errors = []

    def fork():
        try:
            with cancellation_scope(signal):
                caching.fork(handle, "Extract the next passage.")
        except BaseException as exc:
            errors.append(exc)

    with lock:
        worker = threading.Thread(target=fork)
        worker.start()
        try:
            assert lock.wait_for(2, HANG_GUARD)
            signal.cancel()
            worker.join(HANG_GUARD)
            assert not worker.is_alive()
        finally:
            signal.cancel()
    worker.join(HANG_GUARD)
    assert len(errors) == 1
    assert isinstance(errors[0], RunCancelled)


def test_cancelled_stage_is_not_reported_done_failed_or_running(tmp_path):
    source = next(s for s in SOURCES if s.key == "postcard-1938")
    sha1 = hashlib.sha1(source.text.encode()).hexdigest()
    run = RunDir(tmp_path / "run")
    run.start(
        documents=[
            {
                "sha1": sha1,
                "file": "postcard-1938.txt",
                "kind": "text",
                "stages": ["extraction"],
            }
        ]
    )
    run.emit("started", subject=sha1, stage="extraction", kind="text")
    run.emit("cancelled", subject=sha1, stage="extraction", kind="text")
    assert run_state(run)[sha1, "extraction"].status == CANCELLED
    watcher = RunMonitor(run.path)
    watcher.refresh()
    summary = watcher.summary()
    stage = next(s for s in summary.stages if s.name == "extraction")
    assert stage.cancelled == 1
    assert stage.running == stage.done == stage.failed == 0
    assert "cancelled" in render_terminal(summary)
    report = build(run.path)
    row = next(
        s for s in report["aggregate"]["stages"] if s["name"] == "extraction"
    )
    assert row[CANCELLED] == 1
    assert row["failed"] == row["computed"] == row["running"] == 0
    assert "Cancelled" in render_markdown(report["aggregate"])
    run.end("stopped")
    assert run_state(run)[sha1, "extraction"].status == CANCELLED


def test_registration_stops_before_the_next_effect(tmp_path, monkeypatch):
    wiki = FakeWiki()
    orchestrator, _ = RegisterScenario.orchestrator(
        tmp_path, wiki, [("portrait-1890s.png", PORTRAIT)]
    )
    (document,) = orchestrator.documents()
    signal = Cancellation()
    upload = wiki.upload_file

    def finish_upload_then_abort(*args, **kwargs):
        result = upload(*args, **kwargs)
        signal.cancel("abort after completed upload")
        return result

    monkeypatch.setattr(wiki, "upload_file", finish_upload_then_abort)
    with cancellation_scope(signal), pytest.raises(RunCancelled):
        orchestrator._step_register(document)
    assert wiki.files
    assert wiki.writes == 0
    assert not wiki.entities
    # Cancellation does not undo the upload; a later attempt can reuse it.
    monkeypatch.setattr(wiki, "upload_file", upload)
    qid = orchestrator._step_register(document)
    assert qid in wiki.entities
    assert len(wiki.files) == 1
