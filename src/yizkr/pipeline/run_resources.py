"""Model role settings, owned model clients and response cache for one
execution.

RoleSettings says which effort and model each model role runs with, from
the execution's options and the environment as it stands at each call, and
refuses a model named by an alias. It holds no client and needs no driver.

The driver and transcription importer share client construction and cleanup.
Borrowed stage clients do not own these resources; the execution closes engines
before releasing their cache, and retains failed resources for a cleanup retry.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

from yizkr.core.cancellation import Cancellation, check_cancelled
from yizkr.pipeline.rundir import ModelAliasRefused, RunDir, model_alias
from yizkr.pipeline.stage_planning import MODEL_ROLES

MODEL_VARIABLE = "YIZKR_LLM_MODEL"
# the engine a run records whose engines its caller lends it
BORROWED = "borrowed"


class _Rule(NamedTuple):
    """Which of an execution's settings one model role runs with."""

    # the review effort, else the extraction effort
    review: bool
    # the extraction model in place of the main one
    extraction_model: bool


# Reasoning effort per role: the mechanical bulk steps (transcription,
# extraction) run the extraction effort, low by default; the judgment step,
# the critique, runs the review effort, high by default, where the thinking
# is cheap and the decision matters. Claim extraction is a high-volume,
# structured task where source discipline (extract only what the source
# asserts) matters more than raw capability, so it alone can run a model of
# its own, the extraction model; the other roles run the main one. See
# inference configuration in docs/SOURCE_READING.md.
_RULES: Mapping[str, _Rule] = {
    "transcribe": _Rule(review=False, extraction_model=False),
    "extract": _Rule(review=False, extraction_model=True),
    "critique": _Rule(review=True, extraction_model=False),
}


@dataclass(frozen=True)
class RoleSettings:
    """What each model role (MODEL_ROLES) runs with in one execution: the
    engine, the extraction and review efforts, the extraction model (None
    inherits the main model), each role's epoch and the roles refreshed.
    The value holds copies of the epochs and refreshed roles it is given.
    The main model is the one $YIZKR_LLM_MODEL names, read at each call.
    Under the BORROWED engine configured_model names no model: the clients
    the caller lends choose theirs. Every operation that takes a role
    raises KeyError for a role outside MODEL_ROLES, under any engine."""

    engine: str
    extract_effort: str = "low"
    review_effort: str = "high"
    extract_model: str | None = None
    epochs: Mapping[str, int] = field(default_factory=dict)
    refresh: Collection[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "epochs", MappingProxyType(dict(self.epochs)))
        object.__setattr__(self, "refresh", tuple(self.refresh))

    def engine_settings(self, role: str) -> tuple[str, str | None]:
        """(effort, model override) for a role's engine client: the
        override is the extraction model for the role that takes it, and
        None for the others, whose engine runs the main model."""
        rule = _RULES[role]
        effort = self.review_effort if rule.review else self.extract_effort
        return effort, self.extract_model if rule.extraction_model else None

    def configured_model(self, role: str) -> tuple[str | None, str]:
        """(model, where it is configured) for a role: its own model (the
        extraction model), else $YIZKR_LLM_MODEL; None when neither names
        one and the engine runs a default of its own, and None for a
        borrowed engine, whose model is its caller's."""
        _effort, model = self.engine_settings(role)
        if self.engine == BORROWED:
            return None, "the borrowed engine"
        if model:
            return model, "the extraction model (--extract-model)"
        return os.environ.get(MODEL_VARIABLE) or None, f"${MODEL_VARIABLE}"

    def epoch(self, role: str) -> int:
        """The role's epoch, which its LLM cache keys carry: 0 unless
        configured."""
        if role not in _RULES:
            raise KeyError(role)
        return self.epochs.get(role, 0)

    def refuse_model_aliases(self, roles: Iterable[str] = MODEL_ROLES) -> None:
        """Refuse a configuration that names a model by an alias
        (rundir.model_alias) for any of `roles`: the engine would choose
        the model, and neither the run manifest nor the LLM cache's keys
        could say which. ModelAliasRefused names each setting that does
        once, with the roles it serves."""
        refused: dict[tuple[str, str | None], list[str]] = {}
        for role in roles:
            model, where = self.configured_model(role)
            if model_alias(model):
                refused.setdefault((where, model), []).append(role)
        if refused:
            raise ModelAliasRefused(
                "; ".join(
                    f"{where} names the model {model!r} for "
                    f"{', '.join(served)}, an alias"
                    for (where, model), served in refused.items()
                )
                + ". The engine resolves an alias to a model of its own "
                "choosing, which neither the run manifest nor the LLM "
                "cache's keys can name. Configure the full model id "
                "instead, such as claude-sonnet-5."
            )

    def manifest_roles(self) -> dict[str, dict[str, Any]]:
        """Each model role as the execution configures it, for the run
        manifest's `roles`; the role's engine client adds what it applies
        when it starts (RunDir.record_engine)."""
        return {
            role: {
                "model": self.configured_model(role)[0],
                "effort": self.engine_settings(role)[0],
                "epoch": self.epoch(role),
                "refresh": role in self.refresh,
                "client": None,
            }
            for role in MODEL_ROLES
        }


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
