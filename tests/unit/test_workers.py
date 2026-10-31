"""Controlled concurrent work, cancellation and resource ownership."""

import contextvars
import threading

import pytest

from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
    current_cancellation,
)
from yizkr.pipeline.llm import PassPaused
from yizkr.pipeline.workers import WorkerShutdownTimeout, map_workers

HANG_GUARD = 60


def run_in_thread(function):
    results, failures = [], []

    def run():
        try:
            results.append(function())
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, failures


def test_parallel_work_is_bounded_ordered_and_inherits_context():
    first_two = threading.Barrier(3, timeout=HANG_GUARD)
    release = threading.Event()
    visited = []
    setting = contextvars.ContextVar("fictional_stage_setting", default=None)
    parent = Cancellation()

    def items():
        for item in range(5):
            visited.append(item)
            yield item

    def work(item):
        assert setting.get() == "family-booklet-2011"
        assert current_cancellation() is not parent
        setting.set(item)
        if item < 2:
            first_two.wait()
            assert release.wait(HANG_GUARD)
        return item * 2

    def invoke():
        setting.set("family-booklet-2011")
        with cancellation_scope(parent):
            return map_workers(work, items(), concurrency=2)

    thread, results, failures = run_in_thread(invoke)
    try:
        first_two.wait()
        assert visited == [0, 1]
    finally:
        release.set()
        thread.join(HANG_GUARD)
    assert not thread.is_alive()
    assert failures == []
    assert results == [[0, 2, 4, 6, 8]]
    assert not parent.is_set()
    assert setting.get() is None


def test_pause_stops_siblings_and_pending_work_before_releasing_resources():
    entered = threading.Event()
    stopped = threading.Event()
    cleaned = []
    started = []
    pause = PassPaused("fictional provider pause")
    parent = Cancellation()

    def work(item):
        started.append(item)
        if item == 0:
            assert entered.wait(HANG_GUARD)
            raise pause
        entered.set()
        token = current_cancellation()
        try:
            assert token.wait(HANG_GUARD)
            token.check()
        finally:
            stopped.set()

    def cleanup():
        assert stopped.is_set()
        cleaned.append(True)

    with pytest.raises(PassPaused) as caught:
        map_workers(
            work,
            range(8),
            concurrency=2,
            cancellation=parent,
            after_workers=cleanup,
        )
    assert caught.value is pause
    assert sorted(started) == [0, 1]
    assert cleaned == [True]
    assert not parent.is_set()
    assert map_workers(
        lambda value: value, [3], concurrency=1, cancellation=parent
    ) == [3]


def test_parent_cancellation_wakes_active_workers_and_dispatcher():
    entered = threading.Barrier(3, timeout=HANG_GUARD)
    parent = Cancellation()
    started, exited = [], []

    def work(item):
        started.append(item)
        entered.wait()
        token = current_cancellation()
        try:
            assert token.wait(HANG_GUARD)
            token.check()
        finally:
            exited.append(item)

    thread, results, failures = run_in_thread(
        lambda: map_workers(work, range(9), concurrency=2, cancellation=parent)
    )
    try:
        entered.wait()
        parent.cancel("fictional operator abort")
    finally:
        parent.cancel()
        thread.join(HANG_GUARD)
    assert not thread.is_alive()
    assert results == []
    assert len(failures) == 1 and isinstance(failures[0], RunCancelled)
    assert sorted(started) == sorted(exited) == [0, 1]


def test_uncooperative_worker_reports_timeout_and_keeps_lease_until_exit():
    entered, release, cleaned = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    parent = Cancellation()
    started = []

    def work(item):
        started.append(item)
        entered.set()
        assert release.wait(HANG_GUARD)
        return item

    thread, results, failures = run_in_thread(
        lambda: map_workers(
            work,
            [0, 1],
            concurrency=1,
            cancellation=parent,
            join_timeout=0,
            after_workers=cleaned.set,
        )
    )
    try:
        assert entered.wait(HANG_GUARD)
        parent.cancel("fictional operator abort")
        thread.join(HANG_GUARD)
        assert not thread.is_alive()
        assert results == [] and started == [0]
        (error,) = failures
        assert isinstance(error, WorkerShutdownTimeout)
        assert error.remaining_count == 1
        assert isinstance(error.__cause__, RunCancelled)
        assert not cleaned.is_set()
    finally:
        release.set()
        thread.join(HANG_GUARD)
        assert cleaned.wait(HANG_GUARD)
