"""Invocation and work-scope cancellation, separate from model inputs.

Scopes borrow their parent's stop signal. A child may stop its own workers
without preventing a driver from resuming later work with a fresh scope.
Callbacks only wake waits or signal owned operations; their owners join them.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class RunCancelled(BaseException):
    """Stop affected work without treating it as an ordinary stage failure."""


class Cancellation:
    """A thread-safe, one-way signal carrying its first terminal outcome."""

    def __init__(self, parent: Cancellation | None = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._exception: BaseException | None = None
        self._callbacks: dict[object, Callable[[], None]] = {}
        self._unregister_parent: Callable[[], None] | None = None
        if parent is not None:

            def inherit() -> None:
                try:
                    parent.check()
                except BaseException as exc:
                    self.cancel(exception=exc)

            self._unregister_parent = parent.add_callback(inherit)

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def check(self) -> None:
        with self._lock:
            exception = self._exception
        if exception is not None:
            raise exception

    def cancel(
        self,
        reason: str = "run cancelled",
        *,
        exception: BaseException | None = None,
    ) -> None:
        """Signal once; preserve a pause or interrupt supplied by its owner."""
        with self._lock:
            if self._exception is not None:
                return
            self._exception = (
                exception if exception is not None else RunCancelled(reason)
            )
            self._event.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            self._notify(callback)

    @staticmethod
    def _notify(callback: Callable[[], None]) -> None:
        try:
            callback()
        except BaseException:
            # One broken wakeup must not prevent other owned work stopping.
            logger.exception("cancellation wakeup failed")

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a nonblocking wakeup, or invoke it if already stopped.

        Removing a registration does not wait for an already-started wakeup.
        Owners must therefore make their callbacks safe after operation exit.
        """
        key = object()
        with self._lock:
            stopped = self._exception is not None
            if not stopped:
                self._callbacks[key] = callback
        if stopped:
            self._notify(callback)

        def unregister() -> None:
            with self._lock:
                self._callbacks.pop(key, None)

        return unregister

    def close(self) -> None:
        """Detach a finished scope from its parent without cancelling it."""
        if self._unregister_parent is not None:
            self._unregister_parent()
            self._unregister_parent = None


_current: contextvars.ContextVar[Cancellation | None] = contextvars.ContextVar(
    "yizkr_cancellation", default=None
)


def current_cancellation() -> Cancellation | None:
    return _current.get()


def check_cancelled() -> None:
    cancellation = current_cancellation()
    if cancellation is not None:
        cancellation.check()


@contextmanager
def cancellation_scope(cancellation: Cancellation) -> Iterator[Cancellation]:
    """Bind borrowed work to a scope; its creator owns stopping and closing."""
    token = _current.set(cancellation)
    try:
        yield cancellation
    finally:
        _current.reset(token)
