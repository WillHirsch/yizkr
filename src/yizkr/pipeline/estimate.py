"""The call estimate: what a scratch run is to ask the engine, counted
before it runs (`yizkr scratch estimate`; ADR-0025, ADR-0032).

The planned run is the one `yizkr scratch run` with the same options
would make now: a new run, or the run it would invoke again, with the
same documents (the never-ingest list asked first), stages, `--until` and
`--refresh`. Its stale work is each stage of a document whose view does
not stand: every stage in a new run, whose state directories are fresh,
and in a run invoked again, the stages whose views the driver would make
again (not current, refreshed, or failing the transcription check now).

**Known calls.** The estimate takes each stale stage through the
pipeline's own code, as the run takes it, with a client that keys every
call as the run's LLM cache keys it (the same prompts and files, models,
efforts and engine configuration, the claude CLI's version among them)
and answers it from the cache alone. So it knows which calls the cache
answers and which reach the engine, among them an answer the cache kept
that the run rejects and asks for again (a transcription the check
fails, an answer cut off before its sentinel), and the replay of a
conversation whose session was not kept, for its forks. A chunk the
cache cannot answer is answered empty, so that the chunks after it are
keyed and counted too.

**Estimated calls.** A call's key can depend on an answer the engine has
yet to give: an extraction after a transcription the engine makes, the
chunks after an entity manifest it answers, and the critique of a batch
that holds chunks it answers. Each such call is counted as the engine's,
since a new answer changes the keys after it: an extraction as the
latest run that made the document's measured it (llm.run_calls), else
by the mean for its kind there, else by its text's length
(llm.estimate_calls), and a critique as one call.

**At most more.** What only the engine's answers decide is counted as a
bound. An entity manifest the engine answers can be refused, and then its
second attempt and the segmentation's two are asked: at most 3 more for
an extraction. A replayed conversation that answers differently starts
its extraction over, and asks the forks the cache answered too. Not
counted at all: a call asked again after an engine error, or after a
chunk answer cut off (up to 2 a chunk), and the waits for the
subscription, which are no calls.

**Nothing written, held or asked.** The estimate reads the corpus, the
runs directory and the cache, and writes none of them. It takes no lock,
neither the runs directory's nor the cache's, so it runs while a driver
runs, counting what they hold at that moment, and it says who holds
them. It makes no model call and asks the wiki nothing (ADR-0025 §2).
What the keys name, it reads from the engine's clients, made as the
driver makes them: for the claude-code engine that runs
`claude --version`, which calls no model.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import MISSING_LIST, ExclusionConfigError
from yizkr.pipeline import llm
from yizkr.pipeline.chunking import ChunkedClaimExtractor, ExtractionFailed
from yizkr.pipeline.critique import run_critique
from yizkr.pipeline.llmcache import (
    HIT,
    MISS,
    REPLAY,
    EngineCache,
    _describe,
    _parse_record,
    _running,
)
from yizkr.pipeline.llmcache import LOCK_FILE as CACHE_LOCK
from yizkr.pipeline.orchestrate import (
    BATCH_VOLATILE,
    DOCUMENT_STAGES,
    MODEL_ROLES,
    TRANSCRIPTION_VOLATILE,
    DocumentRun,
    _findings,
    route_transcription,
)
from yizkr.pipeline.rundir import (
    _CLIENT_FIXED,
    RunDir,
    RunSettingsConflict,
    settings_conflicts,
)
from yizkr.pipeline.scratch import (
    DEFAULT_RUNS,
    LOCK_FILE,
    PHASES,
    PreflightFailed,
    ScratchDriver,
    ScratchRefused,
    choose_run,
    runs_in,
)
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.transcribe import Transcription

# how a stale stage's calls are counted without their keys: an
# extraction after a transcription the engine makes, the chunks of one
# after an entity manifest it answers, and a critique of a batch that
# holds chunks it answers
WHOLE = "whole"
CHUNKS = "chunks"
CRITIQUE = "critique"
# the extraction's first turns: two attempts at the entity manifest, then,
# when both are refused, two at the segmentation (ChunkedClaimExtractor)
FIRST_TURNS = 4
# the answer the estimate gives a chunk the cache cannot answer: whole,
# and empty, so that the chunks after it are keyed and counted too
EMPTY_CHUNK = '{"done": true, "claims_emitted": 0, "gaps_emitted": 0}'
# the parts of an extraction the estimate counts apart
_FIRST, _CHUNK_CALLS = "first turns", "chunks"


class _Unanswered(BaseException):
    """A call the cache cannot answer, whose answer the stage goes on
    from: what the stage makes after it is the engine's to decide. A
    BaseException, as a paused pass is, so that no step takes it for a
    failed call of its own and falls back."""


# -- counting -----------------------------------------------------------------


@dataclass
class _Tally:
    """The calls one stage of one document makes, as the LLM cache's
    events report them (docs/LLM_CACHE.md). A call under a key the engine
    is asked earlier in the planned run is a hit, since the run's cache
    holds its answer by then: `asked` holds those keys, for the whole
    run."""

    asked: set[str] = field(default_factory=set)
    hits: int = 0
    # engine calls whose keys are known: misses and replays
    known: int = 0
    replays: int = 0
    # the extraction's first turns asked (answered or not), the chunk
    # calls the cache answered, and those the estimate answered empty
    first_turns: int = 0
    chunk_hits: int = 0
    answered: int = 0
    phase: str = ""

    def record(self, event: str, **fields: Any) -> None:
        if event == MISS and self.phase == _CHUNK_CALLS:
            # the estimate answers it empty (_Keyed)
            self.answered += 1
        key = fields.get("key")
        if event == MISS and key in self.asked:
            event = HIT
        if event == HIT:
            self.hits += 1
            if self.phase == _CHUNK_CALLS:
                self.chunk_hits += 1
        elif event == MISS:
            self.known += 1
            self.asked.add(key)
        elif event == REPLAY:
            self.known += 1
            self.replays += 1
        if self.phase == _FIRST and event in (HIT, MISS):
            self.first_turns += 1


@dataclass
class _Made:
    """What one stage of one document comes to in the planned run."""

    # the view stands, and nothing is made
    reused: bool = False
    tally: _Tally = field(default_factory=_Tally)
    # what the stage makes, when the cache answers all of it, or the view
    # that stands; None when the engine's answers decide it
    view: Any = None
    # whether that differs from the view the directory recorded before
    changed: bool = True
    # why the stage fails in the run, so that the stages after it are not
    # reached
    fails: str | None = None
    # how its calls are counted without their keys (WHOLE, CHUNKS,
    # CRITIQUE), and the length of the text an extraction reads
    estimated: str | None = None
    size: int | None = None


@dataclass
class StageCalls:
    """One stage's part of a planned run: the documents whose view it
    makes and those whose view stands, and the calls its stale work makes
    (the module docstring says how each count is known)."""

    made: int = 0
    reused: int = 0
    # of those made, the ones that fail in the run, as the cache answers
    # them: the stages after them are not reached
    failing: int = 0
    # calls the cache answers
    hits: int = 0
    # engine calls whose keys are known
    known: int = 0
    # engine calls counted without their keys, for how many documents,
    # and for how many of those a baseline run measured them
    estimated: int = 0
    estimated_documents: int = 0
    measured: int = 0
    # at most this many engine calls more
    more: int = 0

    @property
    def engine_calls(self) -> int:
        return self.known + self.estimated


@dataclass
class Estimate:
    """The calls of one planned invocation of a scratch run, per stage."""

    run: Path
    resumed: bool
    cache: Path
    stages: dict[str, StageCalls] = field(default_factory=dict)
    cli_version: str | None = None
    # an invocation on other code that goes past the barrier alone
    past_barrier_only: bool = False
    duplicates: int = 0
    unreadable: int = 0
    # the corpus stages the invocation runs, which are not estimated
    corpus_stages: list[str] = field(default_factory=list)
    # who holds the runs directory and the cache now
    holders: list[str] = field(default_factory=list)
    # what the run's preflight would refuse now, beyond what the estimate
    # refuses itself
    preflight: list[str] = field(default_factory=list)

    def total(self) -> StageCalls:
        total = StageCalls()
        for calls in self.stages.values():
            for name in vars(total):
                setattr(
                    total, name, getattr(total, name) + getattr(calls, name)
                )
        return total


# -- the cache as it is, and an engine that answers nothing -------------------


class _CacheAsIs(EngineCache):
    """The LLM cache's entries as a run is served them, read as they are:
    without the cache's lock, which a running driver may hold, and without
    writing anything. What the estimate's own calls change (an answer
    rejected, a chunk answered empty, a key refreshed) it keeps in memory.

    It takes the cache's layout, and its reading of an entry, from
    EngineCache, but none of its making: opening a cache makes its folders
    and holds its lock."""

    def __init__(
        self,
        root: Path,
        *,
        refresh: Iterable[str] = (),
        epochs: dict[str, int] | None = None,
    ) -> None:
        self.root = Path(root)
        self.refresh = frozenset(refresh)
        self.epochs = {
            role: int(epoch) for role, epoch in (epochs or {}).items()
        }
        self._lock = threading.Lock()
        self._refreshed: set[str] = set()
        self._answered: dict[str, dict] = {}
        self._rejected: set[str] = set()

    def warn_unpinned(self, role: str) -> None:
        """Nothing to say: the estimate refuses a role without a model."""

    def remember_session(self, *args: Any) -> None:
        """No session is made, so none is held."""

    def take_session(self, key: str, client: Any) -> None:
        return None

    def get(self, key: str) -> dict | None:
        if key in self._answered:
            return self._answered[key]
        if key in self._rejected:
            return None
        return super().get(key)

    def rejected(self, key: str) -> list[Path]:
        if key in self._rejected:
            return [self.path(key)]
        return super().rejected(key)

    def put(self, key: str, entry: Any) -> None:
        self._answered[key] = dict(entry)

    def reject(self, key: str, response: str, reason: str, **fields) -> bool:
        entry = self.get(key)
        if entry is None or entry.get("response") != response:
            return False
        self._answered.pop(key, None)
        self._rejected.add(key)
        return True

    def seed(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def keep_session(self, *args: Any) -> None:
        """No session is made, so none is kept."""

    def kept_session(self, key: str, opening: str) -> dict[str, bytes]:
        """The names of the session files kept for the conversation, with
        none of their bytes: whether they can resume it is all that
        counts."""
        folder = self._session_dir(key, opening)
        if not folder.is_dir():
            return {}
        return {
            path.relative_to(folder).as_posix(): b""
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }


class _Keyed:
    """An engine's client as the LLM cache keys its calls, which answers
    none of them: its engine, model, effort, configuration hash, system
    prompt and call text are the client's own. A call the cache cannot
    answer reaches it, and it ends the stage there (_Unanswered), or, for
    a chunk, answers it empty (EMPTY_CHUNK), while `answering`."""

    def __init__(self, client: Any) -> None:
        self.engine = getattr(client, "engine", type(client).__name__)
        self.model = getattr(client, "model", None)
        self.effort = getattr(client, "effort", None)
        self.config_hash = client.config_hash
        self.system_prompt = getattr(client, "system_prompt", None)
        self.call_text = getattr(client, "call_text", None)
        self.model_identifier = getattr(client, "model_identifier", "")
        self.cli_version = getattr(client, "cli_version", None)
        self.answering = False

    def last_report(self) -> dict[str, Any]:
        return {}

    def complete(self, prompt: str, image_paths: Any = ()) -> str:
        if self.answering:
            return EMPTY_CHUNK
        raise _Unanswered

    def close(self) -> None:
        """Nothing to release."""


class _KeyedConversations(_Keyed):
    """A _Keyed client for an engine that holds conversations. A fork the
    cache cannot answer needs its conversation's session: resumed from
    the files the cache kept, when they can resume it, which is no call,
    or else replayed, which is a call, and taken to answer as the cache
    did (the bound counts the other case)."""

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        # the conversations whose kept files cannot resume them, by their
        # opening prompt, with the answer a replay is taken to give
        self._replays: dict[str, str] = {}
        if callable(getattr(client, "resume_conversation", None)):
            self.resume_conversation = self._resume
        if callable(getattr(client, "keep_conversation", None)):
            self.keep_conversation = self._keep

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        if prompt in self._replays:
            return self._replays.pop(prompt), ("replayed", prompt)
        raise _Unanswered

    def fork(self, handle: Any, prompt: str) -> str:
        if self.answering:
            return EMPTY_CHUNK
        raise _Unanswered

    def close_conversation(self, handle: Any) -> None:
        """Nothing to release."""

    def _keep(self, handle: Any) -> dict[str, bytes]:
        return {}

    def _resume(
        self, prompt: str, response: str, files: dict[str, bytes]
    ) -> Any:
        if self._resumable(files):
            return ("resumed", prompt)
        self._replays[prompt] = response
        raise ValueError("no session is kept that can resume it")

    def _resumable(self, files: Iterable[str]) -> bool:
        """Whether the kept files resume a conversation: for the
        claude-code engine, when they hold one session transcript, named
        by its session id (ClaudeCodeClient.resume_conversation); another
        engine rebuilds it from its opening turn."""
        if self.engine != "claude-code":
            return True
        transcripts = [name for name in files if name.endswith(".jsonl")]
        if len(transcripts) != 1:
            return False
        try:
            uuid.UUID(Path(transcripts[0]).stem)
        except ValueError:
            return False
        return True


def _keyed(client: Any) -> _Keyed:
    """The _Keyed stand-in for `client`, conversational exactly when the
    client is, as the cache's client for it is (llmcache.wrap)."""
    if isinstance(client, llm.ConversationalLlmClient):
        return _KeyedConversations(client)
    return _Keyed(client)


class _Extractor(ChunkedClaimExtractor):
    """The chunked extractor, telling its first turns from its chunks: a
    first turn the cache cannot answer ends the count of the stage's
    known calls, since the chunks are made from its answer; a chunk it
    cannot answer is answered empty (_Keyed)."""

    def __init__(self, client: Any, keyed: _Keyed, tally: _Tally) -> None:
        # as the driver makes it (Orchestrator._step_extract), one chunk
        # at a time: how many run at once changes no key
        super().__init__(client)
        self._keyed = keyed
        self._tally = tally

    def _phase_one(self, transcription, artifact):
        self._tally.phase = _FIRST
        self._keyed.answering = False
        try:
            return super()._phase_one(transcription, artifact)
        finally:
            self._tally.phase = _CHUNK_CALLS
            self._keyed.answering = True


# -- the estimator ------------------------------------------------------------


def _text(client: Any, name: str) -> str | None:
    """A client's attribute as the run manifest records it (as the
    orchestrator reads it off an engine it starts)."""
    value = getattr(client, name, None)
    return value if isinstance(value, str) and value else None


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class Estimator(ScratchDriver):
    """Plans an invocation of a scratch run as the driver would, and
    counts its calls without making any (the module docstring says how).
    It takes the driver's options. It neither holds nor writes the runs
    directory, the run or the cache, and runs no stage's side of the
    driver: no event, view, state or record is written."""

    def __init__(self, folder: Path, run_dir: Path, **options: Any) -> None:
        super().__init__(folder, run_dir, **options)
        # each role's engine client, made as the driver makes it, and its
        # stand-in behind the cache, which answers nothing
        self._clients: dict[str, Any] = {}
        self._keyed: dict[str, _Keyed] = {}
        self._as_is: _CacheAsIs | None = None
        # the roles whose clients the planned stale work starts
        self._used: set[str] = set()
        # the keys the engine is asked in the planned run, so far
        self._asked: set[str] = set()
        # the stage being counted
        self._tally = _Tally(self._asked)

    def run(self) -> int:
        """An estimator counts a run's calls, and never makes the run:
        `estimate`."""
        raise TypeError("an Estimator never runs a run: call estimate()")

    def _pacing(self) -> dict[str, float] | None:
        """The engine's pacing, or None when the preflight would refuse
        it: pacing decides when calls are made, never what they send, and
        the estimate counts them all the same."""
        try:
            return super()._pacing()
        except ValueError:
            return None

    def _llm(self, role: str, doc: DocumentRun) -> Any:
        """The role's client for one document, as the driver's stages see
        it, but for the engine: behind the cache as it is, keyed as the
        run keys its calls, and answering none itself; what the cache
        does is counted for the stage in hand."""
        self._used.add(role)
        return llm.cached_client(
            self._keyed[role],
            self._as_is,
            role=role,
            events=self._tally.record,
        )

    def estimate(self) -> Estimate:
        """The calls of the invocation `yizkr scratch run` would make now.
        Refuses as the driver would, before any call: without a
        never-ingest list (ExclusionConfigError); with a role whose model
        is no full id, or a PDF and no renderer (PreflightFailed); and
        with other settings, or another claude CLI, than the run it would
        invoke again recorded (RunSettingsConflict)."""
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        problems = self._model_problems()
        if problems:
            raise PreflightFailed(problems)
        found = self.documents()
        docs = [doc for doc in found if doc.unreadable is None]
        self._preflight_documents(docs)
        taken, duplicates = self._canonical(docs)
        recorded = self.rundir.manifest()
        resumed = bool(recorded.get("run_id"))
        fields = self._fields(docs, self._canaries(taken))
        past = self._past_barrier_only(fields, taken)
        if past and self.refresh:
            raise ScratchRefused(
                "the code differs from the run's, so the invocation would "
                "only take the run past the barrier and make no view "
                "again: drop --refresh, or start a new run"
            )
        if resumed and not past:
            conflicts = settings_conflicts(recorded, fields)
            if conflicts:
                raise RunSettingsConflict(self.state_dir, conflicts)
        estimate = Estimate(
            run=self.state_dir,
            resumed=resumed,
            cache=Path(self.cache_dir),
            past_barrier_only=past,
            duplicates=len(duplicates),
            unreadable=len(found) - len(docs),
            corpus_stages=[stage.name for stage in self._corpus_in_range()],
            holders=_holders(self.state_dir.parent, Path(self.cache_dir)),
            preflight=self._preflight_notes(),
        )
        if past or not taken:
            return estimate
        try:
            self._start_clients()
            estimate.cli_version = _text(
                next(iter(self._clients.values())), "cli_version"
            )
            self._refreshed = self._refreshes_made()
            self._as_is = _CacheAsIs(
                Path(self.cache_dir), refresh=self.refresh, epochs=self.epochs
            )
            made = {doc: self._count(doc) for doc in taken}
            self._refuse_other_clients(recorded)
        finally:
            self._close_clients()
        for stages in made.values():
            for stage, outcome in stages.items():
                self._add(estimate, stage, outcome)
        self._estimate_extractions(estimate, made)
        estimate.stages = dict(
            sorted(estimate.stages.items(), key=lambda item: PHASES[item[0]])
        )
        return estimate

    # -- the host -------------------------------------------------------------

    def _preflight_notes(self) -> list[str]:
        """What the run's preflight would refuse now that the estimate
        needs no answer to: the pacing, the engine's login, disk space.
        The stack is left to the run, since asking it is a wiki read."""
        notes = []
        try:
            super()._pacing()
        except ValueError as exc:
            notes.append(str(exc))
        return notes + self._engine_problems() + self._disk_problems()

    def _start_clients(self) -> None:
        """Make each role's engine client as the driver makes it
        (Orchestrator._engine), for what its calls' keys name."""
        for role in MODEL_ROLES:
            effort, model = self._engine_settings(role)
            try:
                client = llm.make_client(self.llm, effort=effort, model=model)
            except (RuntimeError, ValueError) as exc:
                raise ScratchRefused(
                    f"the {role} role's engine client cannot start, and its "
                    f"calls' keys name what it applies: {exc}"
                ) from None
            self._clients[role] = client
            self._keyed[role] = _keyed(client)

    def _refuse_other_clients(self, recorded: dict) -> None:
        """Refuse, as RunDir.record_engine would, a client the run's stale
        work starts that applies other settings than the run it would
        invoke again recorded: a claude CLI updated since among them."""
        conflicts = []
        pinned = (recorded.get("engine") or {}).get("cli_version")
        for role in MODEL_ROLES:
            if role not in self._used:
                continue
            client = self._clients[role]
            version = _text(client, "cli_version")
            if version and pinned and version != pinned and not conflicts:
                conflicts.append(("engine.cli_version", pinned, version))
            before = (
                ((recorded.get("roles") or {}).get(role) or {}).get("client")
            ) or {}
            conflicts += [
                (f"roles.{role}.client.{key}", before[key], _text(client, key))
                for key in _CLIENT_FIXED
                if key in before and before[key] != _text(client, key)
            ]
        if conflicts:
            raise RunSettingsConflict(self.state_dir, conflicts)

    def _close_clients(self) -> None:
        for client in self._clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._clients = {}

    # -- one document ---------------------------------------------------------

    def _fresh(self, doc: DocumentRun) -> bool:
        """Whether every view of the document is made afresh: it has no
        directory yet, or the driver sets its directory aside (_prepare)."""
        if not doc.dir.is_dir():
            return True
        if doc.state.content_sha1 and doc.state.content_sha1 != doc.sha1:
            return True
        return doc.unrecorded

    def _stands(
        self,
        doc: DocumentRun,
        view: Path,
        load: Callable[[str], Any],
        recheck: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """The view, loaded, when the driver would reuse it
        (_reused_view), without a word of it said: current, not refreshed
        in this invocation unless a refresh made it in the run already,
        and passing `recheck`. None when the view is to be made."""
        role, stage = doc.views[view]
        if role in self.refresh and (doc.sha1, stage) not in self._refreshed:
            return None
        if not doc.derivations.current(view):
            return None
        try:
            loaded = load(view.read_text())
        except (OSError, ValueError):
            return None
        if recheck is not None and recheck(loaded) is not None:
            return None
        return loaded

    def _count(self, doc: DocumentRun) -> dict[str, _Made]:
        """What each stage the invocation plans for the document comes to,
        in order, up to one that fails in the run."""
        planned = [
            stage for stage in DOCUMENT_STAGES[doc.kind] if self._within(stage)
        ]
        fresh = self._fresh(doc)
        made: dict[str, _Made] = {}
        if doc.kind == "gedcom":
            # the adapter makes no call
            for stage in planned:
                made[stage] = _Made(
                    reused=not fresh
                    and self._stands(
                        doc, doc.batch, ClaimBatch.model_validate_json
                    )
                    is not None
                )
            return made
        count = {
            "transcription": self._transcription,
            "extraction": self._extraction,
            "critique": self._critique,
        }
        before = _Made()
        for stage in planned:
            self._tally = _Tally(self._asked)
            before = made[stage] = count[stage](doc, fresh, before)
            if before.fails:
                break
        return made

    def _transcription(
        self, doc: DocumentRun, fresh: bool, _before: _Made
    ) -> _Made:
        method = route_transcription(
            doc.source, None if fresh else doc.correction
        )
        if not fresh:
            recheck = (
                functools.partial(self._transcription_failure, doc)
                if method in ("vision", "text-file")
                else None
            )
            view = self._stands(
                doc,
                doc.transcription,
                Transcription.model_validate_json,
                recheck,
            )
            if view is not None:
                return _Made(reused=True, view=view, changed=False)
        made = _Made(tally=self._tally)
        try:
            transcription, _resolution = self._transcribe(doc, method)
        except _Unanswered:
            return made
        except Exception as exc:
            made.fails = _why(exc)
            return made
        made.view = transcription
        made.changed = content_hash(
            transcription.model_dump(mode="json"),
            volatile=TRANSCRIPTION_VOLATILE,
        ) != doc.derivations.content(doc.transcription.name)
        return made

    def _extraction(
        self, doc: DocumentRun, fresh: bool, before: _Made
    ) -> _Made:
        if not fresh and before.view is not None and not before.changed:
            view = self._stands(doc, doc.batch, ClaimBatch.model_validate_json)
            if view is not None:
                return _Made(reused=True, view=view, changed=False)
        if before.view is None:
            return _Made(estimated=WHOLE)
        made = _Made(tally=self._tally)
        keyed = self._keyed["extract"]
        extractor = _Extractor(self._llm("extract", doc), keyed, made.tally)
        try:
            batch = extractor.extract(before.view, ArtifactRef(sha1=doc.sha1))
        except _Unanswered:
            made.estimated = CHUNKS
            made.size = len(before.view.text)
            return made
        except ExtractionFailed as exc:
            # with chunks the engine answers, the batch is not this one
            if not made.tally.answered:
                made.fails = _why(exc)
            return made
        except Exception as exc:
            made.fails = _why(exc)
            return made
        finally:
            keyed.answering = False
        if extractor.failed_chunks:
            made.fails = f"{len(extractor.failed_chunks)} chunks failed"
            return made
        if not made.tally.answered:
            made.view = batch
            made.changed = self._batch_changed(doc, batch)
        return made

    def _critique(self, doc: DocumentRun, fresh: bool, before: _Made) -> _Made:
        if (
            not fresh
            and before.view is not None
            and not before.changed
            and self._stands(doc, doc.critique, _findings) is not None
        ):
            return _Made(reused=True)
        if before.view is None:
            return _Made(estimated=CRITIQUE)
        made = _Made(tally=self._tally)
        with contextlib.suppress(_Unanswered):
            run_critique(self._llm("critique", doc), before.view)
        return made

    def _batch_changed(self, doc: DocumentRun, batch: ClaimBatch) -> bool:
        """Whether the batch differs from the one the document's views
        after it were made from, as the driver tells (_update_batch_view)."""
        digest = content_hash(
            batch.model_dump(mode="json"), volatile=BATCH_VOLATILE
        )
        recorded = doc.derivations.content(doc.batch.name)
        if recorded is None and doc.batch.exists():
            try:
                existing = ClaimBatch.model_validate_json(
                    doc.batch.read_text()
                )
            except (OSError, ValueError):
                return True
            recorded = content_hash(
                existing.model_dump(mode="json"), volatile=BATCH_VOLATILE
            )
        return recorded != digest

    # -- the sums -------------------------------------------------------------

    @staticmethod
    def _add(estimate: Estimate, stage: str, made: _Made) -> None:
        calls = estimate.stages.setdefault(stage, StageCalls())
        if made.reused:
            calls.reused += 1
            return
        calls.made += 1
        calls.failing += bool(made.fails)
        calls.hits += made.tally.hits
        calls.known += made.tally.known
        if made.tally.replays:
            # a replay that answers differently asks every fork again
            calls.more += made.tally.chunk_hits
        if made.estimated == WHOLE:
            calls.more += FIRST_TURNS - 1
        elif made.estimated == CHUNKS:
            calls.more += FIRST_TURNS - made.tally.first_turns
        elif made.estimated == CRITIQUE:
            calls.estimated += 1
            calls.estimated_documents += 1

    def _estimate_extractions(
        self, estimate: Estimate, made: dict[DocumentRun, dict[str, _Made]]
    ) -> None:
        """Count the extraction calls whose keys wait for the engine's
        answers (llm.estimate_calls): the whole stage after a transcription
        the engine makes, and the chunks after an entity manifest it
        answers."""
        extractions = {
            doc: stages["extraction"]
            for doc, stages in made.items()
            if "extraction" in stages
        }
        whole = [doc for doc, e in extractions.items() if e.estimated == WHOLE]
        chunks = {
            doc: e.size
            for doc, e in extractions.items()
            if e.estimated == CHUNKS
        }
        if not whole and not chunks:
            return
        needed = {(doc.sha1, "extraction") for doc in [*whole, *chunks]}
        baseline = self._baseline(needed)
        plan = [
            {"sha1": doc.sha1, "kind": doc.kind, "stages": ["extraction"]}
            for doc in extractions
        ]
        everyone = {(doc.sha1, "extraction") for doc in extractions}
        calls = estimate.stages["extraction"]
        # a document's count holds its entity manifest, which the known
        # calls hold already after a manifest the engine answers
        for group, sizes, known_first in (
            (whole, {}, 0),
            (
                list(chunks),
                {doc.sha1: size for doc, size in chunks.items()},
                1,
            ),
        ):
            if not group:
                continue
            asked = {(doc.sha1, "extraction") for doc in group}
            counted = llm.estimate_calls(
                plan, current=everyone - asked, baseline=baseline, sizes=sizes
            )["extraction"]
            calls.estimated += max(0, counted.calls - known_first * len(group))
            calls.estimated_documents += len(group)
            calls.measured += counted.measured

    def _baseline(self, needed: set[tuple[str, str]]) -> dict:
        """(document SHA-1, stage) -> the calls the latest run to finish
        the stage made for it (llm.run_calls), from the newest run in the
        runs directory back, until every pair in `needed` has its count.
        A stage counts only from a run that finished it, whose count is
        whole."""
        measured: dict[tuple[str, str], int] = {}
        for run in reversed(runs_in(self.state_dir.parent)):
            if needed <= measured.keys():
                break
            events = RunDir(run).events()
            done = {
                (event.get("subject"), event.get("stage"))
                for event in events
                if event.get("event") == "done"
            }
            for pair, count in llm.run_calls(events).items():
                if pair in done:
                    measured.setdefault(pair, count)
        return measured


def _holders(runs: Path, cache: Path) -> list[str]:
    """Who holds the runs directory and the LLM cache now, as their lock
    files record it, read without taking either lock."""
    held = []
    for what, lock in (
        ("the runs directory", runs / LOCK_FILE),
        ("the LLM cache", cache / CACHE_LOCK),
    ):
        try:
            text = lock.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        record = _parse_record(text.strip())
        if record is not None and _running(record):
            held.append(f"{what} {lock.parent} by {_describe(record)}")
    return held


def scratch_estimate(
    folder: Path,
    *,
    runs: Path = DEFAULT_RUNS,
    resume: str | None = None,
    new: bool = False,
    estimator: type[Estimator] = Estimator,
    **options: Any,
) -> Estimate:
    """`yizkr scratch estimate`: the calls of the invocation `yizkr
    scratch run` with the same options would make now (choose_run,
    Estimator), with the runs directory neither held nor written."""
    run_dir = choose_run(runs, resume=resume, new=new)
    return estimator(folder, run_dir, **options).estimate()


# -- the report ---------------------------------------------------------------


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def describe(estimate: Estimate) -> list[str]:
    """The estimate as `yizkr scratch estimate` prints it: a line per
    stage and the total, then what is known, estimated and bounded."""
    if estimate.resumed:
        what = (
            f"run {estimate.run.name} in {estimate.run.parent}, invoked again"
        )
    else:
        what = f"a new run in {estimate.run.parent}"
    lines = [f"Estimate of {what}"]
    keyed = f"the LLM cache {estimate.cache}"
    if estimate.cli_version:
        keyed = f"claude CLI {estimate.cli_version}, {keyed}"
    lines.append(f"Keyed as the run keys its calls: {keyed}")
    if estimate.past_barrier_only:
        lines.append(
            "The code differs from the run's, and every document has ended "
            "its stages: the invocation goes past the barrier alone, and "
            "makes no document's stage, so no call."
        )
    if not estimate.stages:
        if not estimate.past_barrier_only:
            lines.append("No document to take: no call.")
        return lines + _notes(estimate)
    lines += [
        "",
        f"{'stage':14} {'made':>5} {'reused':>6} {'cache hits':>10} "
        f"{'engine calls':>12} {'estimated':>9}",
    ]
    rows = [*estimate.stages.items(), ("total", estimate.total())]
    for stage, calls in rows:
        lines.append(
            f"{stage:14} {calls.made:>5} {calls.reused:>6} {calls.hits:>10} "
            f"{calls.engine_calls:>12} {calls.estimated:>9}"
        )
    total = estimate.total()
    summary = (
        f"Engine calls: {total.engine_calls}, {total.known} known and "
        f"{total.estimated} estimated"
    )
    if total.more:
        summary += f", and at most {total.more} more"
    lines += ["", summary + "."]
    lines.append(
        "- Known: keyed as the run keys them, from the same prompts, "
        "files, models, efforts and engine configuration, and answered by "
        "the cache or by the engine."
    )
    if total.estimated:
        lines.append(
            "- Estimated: calls whose keys wait on an answer the engine is "
            "yet to give (a reading, an entity manifest, a chunk): "
            + "; ".join(_estimated(estimate))
            + "."
        )
    if total.more:
        lines.append(
            f"- At most {total.more} more: an entity manifest the engine "
            "answers may be refused, and then its second attempt and the "
            "segmentation's two are asked (at most 3 for an extraction); a "
            "replayed conversation that answers differently asks its forks "
            "again."
        )
    lines.append(
        "- Not counted: calls asked again after an engine error or after a "
        "chunk answer cut off (at most 2 for a chunk), and the waits for "
        "the subscription, which are no calls."
    )
    return lines + _notes(estimate)


def _estimated(estimate: Estimate) -> list[str]:
    """What the estimated calls are, stage by stage, and how each is
    counted."""
    parts = []
    extraction = estimate.stages.get("extraction", StageCalls())
    if extraction.estimated_documents:
        documents = extraction.estimated_documents
        if extraction.measured == documents:
            how = "each as the latest run to make it measured it"
        elif extraction.measured:
            how = (
                f"{extraction.measured} as the latest run to make them "
                "measured them, the others by the mean for their kind there "
                "or by their text's length"
            )
        else:
            how = "by the mean for their kind in the latest run, or by their "
            how += "text's length"
        parts.append(
            f"{_plural(extraction.estimated, 'extraction call')} for "
            f"{_plural(documents, 'document')}, {how}"
        )
    critique = estimate.stages.get("critique", StageCalls())
    if critique.estimated:
        parts.append(f"{_plural(critique.estimated, 'critique')}, one each")
    return parts


def _notes(estimate: Estimate) -> list[str]:
    notes = []
    failing = [
        _plural(calls.failing, stage)
        for stage, calls in estimate.stages.items()
        if calls.failing
    ]
    if failing:
        notes.append(
            "- Failing in the run, as the cache answers it: "
            + ", ".join(failing)
            + ". The stages after them are not reached."
        )
    if estimate.duplicates or estimate.unreadable:
        taken = []
        if estimate.duplicates:
            taken.append(_plural(estimate.duplicates, "duplicate"))
        if estimate.unreadable:
            taken.append(f"{estimate.unreadable} unreadable")
        notes.append(f"- No stage for {' and '.join(taken)}.")
    if estimate.corpus_stages:
        notes.append(
            "- Not estimated: the corpus stages "
            + ", ".join(estimate.corpus_stages)
            + "."
        )
    for held in estimate.holders:
        notes.append(
            f"- Held now: {held}. The estimate takes no lock, and counts "
            "what the run and the cache hold as it reads them."
        )
    if estimate.preflight:
        notes.append(
            "- The run's preflight would refuse it now: "
            + "; ".join(estimate.preflight)
        )
    return notes
