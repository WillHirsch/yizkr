"""Bounded concurrent work borrowing an invocation's cancellation scope.

Only the current worker slots are submitted. Stopping one scope prevents new
dispatch and interrupts its cooperative operations without stopping its parent.
Python cannot kill an arbitrary thread: a worker that ignores cancellation is
reported after the join bound, and its resource cleanup waits for its exit.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TypeVar

from yizkr.pipeline.cancellation import (
    Cancellation,
    cancellation_scope,
    current_cancellation,
)

_T = TypeVar("_T")
_R = TypeVar("_R")
logger = logging.getLogger(__name__)


class WorkerShutdownTimeout(RuntimeError):
    """Custom work ignored cancellation and cannot safely be resumed here."""

    def __init__(self, remaining_count: int) -> None:
        self.remaining_count = remaining_count
        super().__init__(
            f"{remaining_count} worker(s) did not stop within the join bound; "
            "Python threads cannot be forcibly terminated"
        )


def _after_exit(pending: set[Future], cleanup: Callable[[], None]) -> None:
    """Release a lease once the last uncooperative worker has exited."""
    lock = threading.Lock()
    remaining = set(pending)

    def finished(future: Future) -> None:
        with lock:
            remaining.discard(future)
            last = not remaining
        if last:
            try:
                cleanup()
            except BaseException:
                logger.exception("worker resource cleanup failed")

    for future in pending:
        future.add_done_callback(finished)


def map_workers(
    function: Callable[[_T], _R],
    items: Iterable[_T],
    *,
    concurrency: int,
    cancellation: Cancellation | None = None,
    join_timeout: float = 5.0,
    after_workers: Callable[[], None] | None = None,
) -> list[_R]:
    """Run bounded work, returning results in input order only on completion.

    A worker's terminal exception stops its siblings and is re-raised
    unchanged, preserving resumable pauses. Completed work remains its caller's
    property. A join timeout instead reports the uncooperative worker boundary.
    ``after_workers`` releases resources borrowed by the entire map; it runs
    only after every submitted operation has stopped, even on join timeout.
    """
    parent = (
        cancellation if cancellation is not None else current_cancellation()
    )
    stopped = Cancellation(parent)
    wake = threading.Event()
    unsubscribe = stopped.add_callback(wake.set)
    pool: ThreadPoolExecutor | None = None
    pending: dict[Future[_R], int] = {}
    results: list[_R] = []
    failure: BaseException | None = None

    def run(item: _T) -> _R:
        with cancellation_scope(stopped):
            try:
                stopped.check()
                result = function(item)
                stopped.check()
                return result
            except BaseException as exc:
                stopped.cancel(exception=exc)
                raise

    try:
        stopped.check()
        pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
        remaining = iter(items)
        exhausted = False

        def fill() -> None:
            nonlocal exhausted
            while not exhausted and len(pending) < max(1, concurrency):
                stopped.check()
                try:
                    item = next(remaining)
                except StopIteration:
                    exhausted = True
                    break
                context = contextvars.copy_context()
                future = pool.submit(context.run, run, item)
                pending[future] = len(results)
                results.append(None)  # type: ignore[arg-type]
                future.add_done_callback(lambda _future: wake.set())

        fill()
        while pending:
            wake.clear()
            stopped.check()
            ready = [future for future in pending if future.done()]
            if not ready:
                wake.wait()
                continue
            for future in ready:
                results[pending.pop(future)] = future.result()
            fill()
        stopped.check()
        return results
    except BaseException as exc:
        failure = exc
        stopped.cancel(exception=exc)
        raise
    finally:
        for future in pending:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        running = {future for future in pending if not future.done()}
        if running:
            _done, running = wait(running, timeout=max(0.0, join_timeout))
        unsubscribe()
        stopped.close()
        if running:
            if after_workers is not None:
                _after_exit(running, after_workers)
            raise WorkerShutdownTimeout(len(running)) from failure
        elif after_workers is not None:
            try:
                after_workers()
            except BaseException as exc:
                if failure is None:
                    raise
                failure.add_note(
                    "worker resource cleanup also failed: "
                    f"{type(exc).__name__}"
                )
