"""Owned model clients and response cache for one execution.

The driver and transcription importer share client construction and cleanup.
Borrowed stage clients do not own these resources; the execution closes engines
before releasing their cache, and retains failed resources for a cleanup retry.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from yizkr.pipeline.cancellation import Cancellation, check_cancelled
from yizkr.pipeline.rundir import RunDir

MODEL_VARIABLE = "YIZKR_LLM_MODEL"


class RunResources:
    def __init__(
        self,
        cache_dir: Path | None,
        *,
        refresh: Collection[str] = (),
        epochs: Mapping[str, int] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.refresh = tuple(refresh)
        self.epochs = dict(epochs or {})
        self.engines: dict[str, Any] = {}
        # A client rejected by settings validation is never reusable, even
        # when stopping it fails and the execution must retry cleanup.
        self._rejected_engines: list[Any] = []
        self.cache: Any = None
        self._lock = threading.Lock()

    def engine(
        self,
        role: str,
        *,
        engine: str,
        effort: str,
        model: str | None = None,
        cancellation: Cancellation | None = None,
        rundir: RunDir | None = None,
        fixed_settings: bool = False,
    ) -> Any:
        """Start one engine per role, checking recorded invocation settings."""
        with self._lock:
            if role not in self.engines:
                from yizkr.pipeline.llm import make_client

                check_cancelled()
                client = make_client(
                    engine,
                    effort=effort,
                    model=model,
                    cancellation=cancellation,
                )
                try:
                    if rundir is not None:
                        rundir.record_engine(
                            role,
                            {
                                "model": _text(client, "model"),
                                "effort": _text(client, "effort"),
                                "config_hash": _text(client, "config_hash"),
                                "model_identifier": _text(
                                    client, "model_identifier"
                                ),
                            },
                            cli_version=_text(client, "cli_version"),
                            fixed_settings=fixed_settings,
                        )
                except BaseException as primary:
                    try:
                        close = getattr(client, "close", None)
                        if callable(close):
                            close()
                    except BaseException as cleanup:
                        self._rejected_engines.append(client)
                        primary.add_note(
                            "execution cleanup also failed: "
                            f"{type(cleanup).__name__}"
                        )
                    raise
                self.engines[role] = client
            return self.engines[role]

    def response_cache(self) -> Any:
        """Open the execution's response cache only when a client needs it."""
        if self.cache_dir is None:
            return None
        with self._lock:
            if self.cache is None:
                from yizkr.pipeline.llm import open_cache

                self.cache = open_cache(
                    self.cache_dir, refresh=self.refresh, epochs=self.epochs
                )
            return self.cache

    def role_client(
        self,
        role: str,
        engine: Any,
        *,
        events: Callable[..., Any] | None = None,
    ) -> Any:
        """Observe a borrowed role client and apply shared response keying."""
        from yizkr.pipeline.llm import cached_client, observed_client

        check_cancelled()
        observed = observed_client(engine, role=role, events=events)
        cache = self.response_cache()
        if cache is None:
            return observed
        return cached_client(observed, cache, role=role, events=events)

    def close(self) -> None:
        """Release engines before the cache; retain failed owners for retry."""
        primary = sys.exception()
        failures = []
        with self._lock:
            engines = self.engines
            self.engines = {}
            rejected = self._rejected_engines
            self._rejected_engines = []
            cache = self.cache
        for role, engine in [
            *engines.items(),
            *((None, engine) for engine in rejected),
        ]:
            try:
                close = getattr(engine, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                failures.append(exc)
                with self._lock:
                    if role is None:
                        self._rejected_engines.append(engine)
                    else:
                        self.engines[role] = engine
        if not failures and cache is not None:
            try:
                cache.close()
            except BaseException as exc:
                failures.append(exc)
            else:
                with self._lock:
                    self.cache = None
        if failures:
            terminal = primary if primary is not None else failures[0]
            for failure in failures:
                if failure is not terminal:
                    terminal.add_note(
                        "execution cleanup also failed: "
                        f"{type(failure).__name__}"
                    )
            if primary is None:
                raise terminal


def _text(engine: Any, name: str) -> str | None:
    value = getattr(engine, name, None)
    return value if isinstance(value, str) and value else None
