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
fails, an answer cut off before its sentinel), a call another document
asks earlier in the run, which the run's cache answers by then, and the
replay of a conversation whose session was not kept, for its forks. A
chunk the cache cannot answer is answered empty, so that the chunks after
it are keyed and counted too. So is a page or an image of a PDF or a deck read
page by page (yizkr.pipeline.containers), each a call of its own: one the cache
cannot answer is given a reading that stands in for the engine's, which the
transcription check passes, so that every page and image after it is
keyed and counted too, and the transcription is then the engine's to
make. The images and pages such a reading keeps in a scratch folder are
removed when its transcription is counted.

**Estimated calls.** A call's key can depend on an answer the engine has
yet to give: an extraction after a transcription the engine makes, the
chunks after an entity manifest it answers, the forks after a replayed
conversation, and the critique of a batch that holds chunks it answers,
or of a transcription it makes, since the critique reads both.
Each such call is counted as the engine's, since a new answer changes
the keys after it: an extraction as the latest run to make it measured
it (llm.run_calls), else by the mean for its kind over the runs
measured, else by its text's length (llm.estimate_calls); the forks
after a replay as the chunks the cache kept leave them unanswered, as
they are when the replay answers as the cache did; and a critique as one
call.

**Up to more.** What only the engine's answers decide is counted apart.
An entity manifest the engine answers, a replay's among them, can be
refused, and then its second attempt and the segmentation's two are
asked: 3 more for an extraction at most. A replayed conversation that
answers differently starts its extraction over, and asks the forks the
cache answered too, which can number otherwise. These are estimates, not
ceilings: the chunks an entity manifest makes follow its entities, which
a text's length only approaches, so a dense document can take more. Not
counted at all: a call asked again after an engine error, or after a
chunk answer cut off (up to 2 a chunk), the preflight's one trivial call
per distinct (model, effort) pair of the roles within the invocation's
`--until` range (scratch.PREFLIGHT_PROMPT), which every invocation whose
range holds a stage that may call the engine makes, a resume that reuses
every view included, unless the installed claude CLI is not the run's;
and the waits for the subscription, which are no calls. Nor is a slice
the anomaly checks pause as the run unfolds (docs/ANOMALIES.md): it
makes fewer calls than counted.

**Excerpts.** An independent image a PDF places on any of its pages, or
a deck paints on a slide, read on its own, becomes a document of its own, an
excerpt (ADR-0008), found only as its container is transcribed, so no
plan names it beforehand. The container's transcription stage, taken
through the pipeline's own code, reads it page by page as the run does
(yizkr.pipeline.containers: a PDF with pypdfium2, a deck from its XML,
with no model call), so the estimate knows each image it places from the
file itself: by the SHA-1 of its bytes, once however many PDFs and
decks place it, and none that the never-ingest list names or whose bytes a
document of the corpus has. An image whose kept reading (one that passed
the check, which the cache answers) calls every page blank or not a
document page is left out, as the run makes no excerpt of it; an image
whose reading the cache cannot answer, or answers with one the check
fails, is read again and counted. A container whose transcription
stands reads nothing again: its excerpts are those its directory holds, read
from files as the driver reads them (ScratchDriver._known_excerpts), and
each counts the stages whose views do not stand, as the driver rechecks
them (its transcription against the check, its critique's entries): a
transcription made again from its container's reading, with the content
its batch was made from, leaves the views after it standing; otherwise
every stage after the first of them counts too. An image of a container whose
transcription is made again counts every excerpt stage, though an excerpt
the run holds for it may have views that stand. An excerpt stage left to
make starts its role's engine client, as the run does, so a client the run
refuses (another claude CLI than the run's among them) refuses the
estimate too. Each excerpt stage's calls are counted by the mean
for excerpts over every run in the runs directory that measured the
stage for one, older runs, on other prompts or models, among them, else
as typical (llm.estimate_calls), rounded up, and given as up to so many
more: an image the engine reads blank, or as no document page, makes no
excerpt, and the cache may answer an excerpt's calls.

**Nothing written, held or asked.** The estimate reads the corpus, the
runs directory and the cache (llm.read_cache), and writes none of them;
it refuses a runs directory the run refuses (scratch.runs_refusal).
It takes no lock, neither the runs directory's nor the cache's, so it
runs while a driver runs, counting what they hold at that moment, and it
says who holds them. It makes no model call and asks the wiki nothing
(ADR-0025 §2). What the keys name, it reads from the engine's clients,
made as the driver makes them, as a role's first stale stage needs one:
for the claude-code engine that runs `claude --version`, which calls no
model.
"""

from __future__ import annotations

import contextlib
import functools
import json
import math
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.corpus import MISSING_LIST, ExclusionConfigError
from yizkr.pipeline import llm
from yizkr.pipeline.chunking import ChunkedClaimExtractor, ExtractionFailed
from yizkr.pipeline.containers import VISION_SUFFIXES
from yizkr.pipeline.critique import run_critique
from yizkr.pipeline.orchestrate import (
    BATCH_VOLATILE,
    CHECKED_ROUTES,
    EXCERPT,
    PAGED,
    TRANSCRIPTION_VOLATILE,
    DocumentRun,
    _findings,
    _unreadable_critique,
    route_transcription,
)
from yizkr.pipeline.rundir import (
    _CLIENT_FIXED,
    RunDir,
    RunSettingsConflict,
    settings_conflicts,
)
from yizkr.pipeline.scratch import (
    CORPUS_DIR,
    DEFAULT_RUNS,
    PreflightFailed,
    ScratchDriver,
    ScratchRefused,
    choose_run,
    runs_holder,
    runs_in,
    runs_refusal,
)
from yizkr.pipeline.stage_planning import (
    DOCUMENT_STAGES,
    PHASES,
    STAGE_ROLES,
    stages_for,
)
from yizkr.pipeline.stages import content_hash
from yizkr.pipeline.transcribe import ImageReading, Transcription
from yizkr.pipeline.transcription_record import BLANK, NOT_A_DOCUMENT_PAGE

# the corpus stage whose adjudication calls the estimate counts
CLUSTERING = "clustering"
# the LLM cache's events the estimate counts (rundir.CACHE_EVENTS): an
# answer it gives, a call it cannot answer, and the replay of an opening
# turn for a fork
HIT, MISS, REPLAY = "cache-hit", "cache-miss", "cache-replay"
# how a stale stage's calls are counted without their keys: an
# extraction after a transcription the engine makes, the chunks of one
# after an entity manifest it answers, and a critique of a batch that
# holds chunks it answers, or of a transcription it makes
WHOLE = "whole"
CHUNKS = "chunks"
CRITIQUE = "critique"
# the extraction's first turns: two attempts at the entity manifest, then,
# when both are refused, two at the segmentation (ChunkedClaimExtractor)
FIRST_TURNS = 4
# the answer the estimate gives a chunk the cache cannot answer: the
# terminal sentinel the chunk prompt asks for, alone, so whole and empty,
# and the chunks after it are keyed and counted too
EMPTY_CHUNK = '{"done": true}'
# the reading the estimate gives a page of a PDF, or an image of a PDF or a
# deck, read page by page, that the cache cannot answer: one the transcription
# check passes (a transcription record of one illegible page, whose text is a
# single editorial mark), so that the calls after it are keyed and counted too
STAND_IN_READING = json.dumps(
    {
        "document_class": "unclassified",
        "pages": [
            {
                "page": 1,
                "status": "illegible",
                "text": "[a reading the engine is to give]",
            }
        ],
    }
)
# the files of such a page or image (yizkr.pipeline.containers), as
# against a PDF read whole
_READ_FILES = tuple(sorted(VISION_SUFFIXES))
# the parts of an extraction the estimate counts apart
_FIRST, _CHUNK_CALLS = "first turns", "chunks"
# the role whose engine an excerpt's stage calls: its transcription is the
# reading its container made of it, which asks no model
_EXCERPT_ROLES = {
    stage: STAGE_ROLES[stage]
    for stage in DOCUMENT_STAGES[EXCERPT]
    if stage != "transcription"
}


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
    # the extraction's first turns asked (answered or not); its chunk
    # calls the cache answered, those it cannot answer that are new to the
    # run, and every one the estimate answered empty, which leaves the
    # batch the engine's to make
    first_turns: int = 0
    chunk_hits: int = 0
    chunk_misses: int = 0
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
            if self.phase == _CHUNK_CALLS:
                self.chunk_misses += 1
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
    # the SHA-1s of the images a container's transcription read on its own
    # that may each become an excerpt (_may_be_excerpt)
    images: list[str] = field(default_factory=list)


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
    # engine calls counted without their keys (all of them, below); the
    # documents whose calls llm.estimate_calls counts, and how many of
    # those a baseline run measured
    estimated: int = 0
    estimated_documents: int = 0
    measured: int = 0
    # conversations replayed for their forks, and the forks after them,
    # which are among the estimated calls
    replays: int = 0
    replay_calls: int = 0
    # the engine calls more that only the engine's answers can bring
    more: int = 0

    @property
    def engine_calls(self) -> int:
        return self.known + self.estimated


@dataclass
class ExcerptCalls:
    """The excerpts the planned run may make: the images, the paged
    containers (PDFs and decks) that place them, and per excerpt stage
    the calls, how many of the images it counts (`stale`), and how many
    excerpts the runs measured it for
    (0 when none did and the count is typical), all of them up to so many
    (the module docstring says why)."""

    images: int = 0
    containers: int = 0
    calls: dict[str, int] = field(default_factory=dict)
    measured_stages: dict[str, int] = field(default_factory=dict)
    stale: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


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
    # the corpus stages the invocation runs that may call the engine,
    # which are not estimated, and those that make no call
    corpus_stages: list[str] = field(default_factory=list)
    callless_stages: list[str] = field(default_factory=list)
    # the clustering stage's adjudication calls, as the latest identity
    # map counts them, and whose map that is (_adjudication)
    adjudication: tuple[int, str] | None = None
    # who holds the runs directory and the cache now
    holders: list[str] = field(default_factory=list)
    # what the run's preflight would refuse now, beyond what the estimate
    # refuses itself
    preflight: list[str] = field(default_factory=list)
    excerpts: ExcerptCalls = field(default_factory=ExcerptCalls)

    def total(self) -> StageCalls:
        total = StageCalls()
        for calls in self.stages.values():
            for name in vars(total):
                setattr(
                    total, name, getattr(total, name) + getattr(calls, name)
                )
        return total


# -- an engine that answers nothing -------------------------------------------


class _Keyed:
    """An engine's client as the LLM cache keys its calls, which answers
    none of them: its engine, model, effort, configuration hash, system
    prompt and call text are the client's own. A call the cache cannot
    answer reaches it, and it ends the stage there (_Unanswered); or, for
    a chunk, answers it empty (EMPTY_CHUNK), while `answering`; or, for a
    page of a PDF, or an image of a PDF or a deck, read page by page,
    while `reading_pages`,
    stands in for the engine's reading (STAND_IN_READING), and counts it
    in `stood_in`."""

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
        self.reading_pages = False
        self.stood_in = 0

    def last_report(self) -> dict[str, Any]:
        return {}

    def complete(self, prompt: str, image_paths: Any = ()) -> str:
        if self.answering:
            return EMPTY_CHUNK
        if (
            self.reading_pages
            and image_paths
            and all(str(path).endswith(_READ_FILES) for path in image_paths)
        ):
            self.stood_in += 1
            return STAND_IN_READING
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
        # at a time, reading the statement about the document's
        # preparation: how many run at once changes no key
        super().__init__(client, read_preparation=True)
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
        # each role's engine client, made as the driver makes it when the
        # role's first stale stage needs it, and its stand-in behind the
        # cache, which answers nothing
        self._clients: dict[str, Any] = {}
        self._keyed: dict[str, _Keyed] = {}
        # the LLM cache as it is (llm.read_cache)
        self._as_is: Any = None
        # the run manifest of the run it would invoke again, if any
        self._recorded: dict[str, Any] = {}
        # the keys the engine is asked in the planned run, so far
        self._asked: set[str] = set()
        # the stage being counted
        self._tally = _Tally(self._asked)
        self._context_made: dict[str, _Made] = {}
        self._context_images: dict[str, dict[str, Transcription]] = {}
        self._reading_pages = False

    def run(self) -> int:
        """An estimator counts a run's calls, and never makes the run:
        `estimate`."""
        raise TypeError("an Estimator never runs a run: call estimate()")

    def _llm(self, role: str, doc: DocumentRun) -> Any:
        """The role's client for one document, as the driver's stages see
        it, but for the engine: behind the cache as it is, keyed as the
        run keys its calls, and answering none itself; what the cache
        does is counted for the stage in hand. A deterministic reading
        never requests this capability and starts no client."""
        self._engine_for(role)
        if role == "transcribe":
            self._keyed[role].reading_pages = self._reading_pages
        return llm.cached_client(
            self._keyed[role],
            self._as_is,
            role=role,
            events=self._tally.record,
        )

    def estimate(self) -> Estimate:
        """The calls of the invocation `yizkr scratch run` would make now.
        Refuses as the driver would, before any call: without a
        never-ingest list, or with a deck that cannot be read as an
        archive while the list holds a glob (ExclusionConfigError); with a
        role whose model is no full id, a pacing setting the engine
        refuses, anomaly thresholds the checks cannot load, a PDF and no
        renderer, or a cache folder the cache refuses, for an invocation
        that calls the engine (PreflightFailed); with other settings, or
        another claude CLI, than the run it would invoke again recorded
        (RunSettingsConflict); with an engine client that cannot start
        (ScratchRefused), or a pause as one starts (llm.PassPaused). A
        corpus with no document is no call, as it is to the driver."""
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        found = self.documents()
        self._plan_documents(found)
        problems = self._model_problems() + self._pacing_problems()
        problems += self._anomaly_problems()
        if problems:
            raise PreflightFailed(problems)
        self._recorded = self.rundir.manifest()
        estimate = Estimate(
            run=self.state_dir,
            resumed=bool(self._recorded.get("run_id")),
            cache=Path(self.cache_dir),
            holders=_holders(
                self.state_dir.parent,
                Path(self.cache_dir) if self._preflight_roles() else None,
            ),
            preflight=self._engine_problems() + self._disk_problems(),
        )
        if not found:
            return estimate
        docs = [doc for doc in found if doc.unreadable is None]
        self._preflight_documents(docs)
        taken, duplicates = self._canonical(docs)
        fields = self._fields(docs, self._canaries(taken))
        past = self._past_barrier_only(fields, taken)
        if past and self.refresh:
            raise ScratchRefused(
                "the code differs from the run's, so the invocation would "
                "only take the run past the barrier and make no view "
                "again: drop --refresh, or start a new run"
            )
        if estimate.resumed and not past:
            conflicts = settings_conflicts(self._recorded, fields)
            if conflicts:
                raise RunSettingsConflict(self.state_dir, conflicts)
        problems = self._cache_problems(past)
        if problems:
            raise PreflightFailed(problems)
        estimate.past_barrier_only = past
        estimate.duplicates = len(duplicates)
        estimate.unreadable = len(found) - len(docs)
        estimate.corpus_stages = [
            s.name for s in self._corpus_in_range() if s.calls_engine
        ]
        estimate.callless_stages = [
            s.name for s in self._corpus_in_range() if not s.calls_engine
        ]
        if CLUSTERING in estimate.corpus_stages:
            estimate.adjudication = self._adjudication()
            if estimate.adjudication is not None:
                estimate.corpus_stages.remove(CLUSTERING)
        if past or not taken:
            return estimate
        self._refreshed = self._refreshes_made()
        try:
            # Context-dependent owners need every matching parent reading
            # before their extraction keys can be known, including canaries.
            parents = {
                parent.sha1: parent
                for group in self._interpretation_parents.values()
                for parent in group
            }
            if self._within("transcription"):
                for owner in sorted(parents):
                    parent = parents[owner]
                    self._require_processing_dependencies(
                        parent,
                        "transcription",
                        route=self._transcription_route(
                            parent, self._fresh(parent)
                        ),
                    )
                    self._tally = _Tally(self._asked)
                    outcome = self._transcription(
                        parent, self._fresh(parent), _Made()
                    )
                    self._context_made[owner] = outcome
                    if outcome.view is not None and not outcome.fails:
                        self._interpretation_readings[owner] = outcome.view
            made = {doc: self._count(doc) for doc in taken}
            # an excerpt's stage left to make starts its role's engine
            # client too, as the run starts it
            excerpts = self._excerpts_found(made, docs)
        finally:
            estimate.cli_version = next(
                (_text(c, "cli_version") for c in self._clients.values()),
                None,
            )
            self._close_clients()
        for stages in made.values():
            for stage, outcome in stages.items():
                self._add(estimate, stage, outcome)
        self._estimate_extractions(estimate, made)
        estimate.excerpts = excerpts
        estimate.stages = dict(
            sorted(estimate.stages.items(), key=lambda item: PHASES[item[0]])
        )
        return estimate

    # -- the engine's clients ------------------------------------------------

    def _engine_for(self, role: str) -> None:
        """Start the role's engine client on first use, as the driver
        does (Orchestrator._engine): the role's first stale stage starts
        it. So a client that cannot start (ScratchRefused), a pause as it
        starts (llm.PassPaused), or one that applies other settings than
        the run it would invoke again recorded, a claude CLI updated
        since among them (RunSettingsConflict, as RunDir.record_engine
        refuses it), stops the estimate where it would stop the run, and
        nothing stale starts none. The cache is read from here on, as the
        run opens it here; a cache folder the cache refuses was refused
        already, as the run's preflight refuses it
        (ScratchDriver._cache_problems). Called before a stage's own
        code, whose failures are the stage's."""
        if role in self._keyed:
            return
        effort, model = self._engine_settings(role)
        try:
            client = llm.make_client(self.llm, effort=effort, model=model)
        except (RuntimeError, ValueError) as exc:
            raise ScratchRefused(
                f"the {role} role's engine client cannot start, and its "
                f"calls' keys name what it applies: {exc}"
            ) from None
        self._clients[role] = client
        conflicts = self._client_conflicts(role, client)
        if conflicts:
            raise RunSettingsConflict(self.state_dir, conflicts)
        if self._as_is is None:
            # the run opens its cache here too, as a stage first needs
            # the engine: a folder it would refuse is refused
            # (llmcache.CacheRefused)
            self._as_is = llm.read_cache(
                self.cache_dir, refresh=self.refresh, epochs=self.epochs
            )
        self._keyed[role] = _keyed(client)

    def _client_conflicts(self, role: str, client: Any) -> list[tuple]:
        """What `client` applies otherwise than the run it would invoke
        again recorded for the role, as RunDir.record_engine compares
        them: the claude CLI's version first."""
        conflicts = []
        pinned = (self._recorded.get("engine") or {}).get("cli_version")
        version = _text(client, "cli_version")
        if version and pinned and version != pinned:
            conflicts.append(("engine.cli_version", pinned, version))
        roles = self._recorded.get("roles") or {}
        before = (roles.get(role) or {}).get("client") or {}
        conflicts += [
            (f"roles.{role}.client.{key}", before[key], _text(client, key))
            for key in _CLIENT_FIXED
            if key in before and before[key] != _text(client, key)
        ]
        return conflicts

    def _close_clients(self) -> None:
        """Stop all clients before releasing their session resources."""
        primary = sys.exception()
        failures = []
        clients = self._clients
        self._clients = {}
        for role, client in clients.items():
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                failures.append(exc)
                self._clients[role] = client
        if not failures and self._as_is is not None:
            try:
                self._as_is.close()
            except BaseException as exc:
                failures.append(exc)
            else:
                self._as_is = None
        if failures:
            terminal = primary if primary is not None else failures[0]
            for failure in failures:
                if failure is not terminal:
                    terminal.add_note(
                        "estimate cleanup also failed: "
                        f"{type(failure).__name__}"
                    )
            if primary is None:
                raise terminal

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
        """Inspect reuse through the driver's read-only view planner.

        Publication, events and approval bindings belong to execution;
        observation applies the same dependencies, refresh policy and
        validation without changing any persisted state.
        """
        return self._inspect_view(doc, view, load, recheck).require_value()

    def _transcription_route(self, doc: DocumentRun, fresh: bool) -> str:
        """The route after execution would set aside an obsolete directory."""
        if not fresh:
            return self._route(doc)
        route = route_transcription(doc.source, None)
        return EXCERPT if doc.kind == EXCERPT and route == "vision" else route

    def _count(self, doc: DocumentRun) -> dict[str, _Made]:
        """What each stage the invocation plans for the document comes to,
        in order, up to one that fails in the run."""
        planned = [
            stage for stage in stages_for(doc.kind) if self._within(stage.name)
        ]
        fresh = self._fresh(doc)
        made: dict[str, _Made] = {}
        count = {
            "transcription": self._transcription,
            "extraction": self._extraction,
        }
        before = _Made()
        for descriptor in planned:
            stage = descriptor.name
            if stage == "transcription" and doc.sha1 in self._context_made:
                before = made[stage] = self._context_made[doc.sha1]
                if before.fails:
                    break
                continue
            if descriptor.process == "deterministic":
                # Even a missing output needs observable generation inputs.
                # Deterministic adapters neither open a model nor its cache.
                self._require_processing_dependencies(doc, stage)
                view = (
                    self._stands(
                        doc,
                        doc.dir / descriptor.artifact,
                        ClaimBatch.model_validate_json,
                    )
                    if not fresh
                    else None
                )
                before = made[stage] = _Made(
                    reused=view is not None,
                    view=view,
                    changed=view is None,
                )
                continue
            # Fresh views and changed upstream products cannot be reused,
            # but still need inspectable generation inputs before any
            # model client or estimated call is planned.
            self._require_processing_dependencies(
                doc,
                stage,
                **(
                    {"route": self._transcription_route(doc, fresh)}
                    if stage == "transcription"
                    else {}
                ),
            )
            self._tally = _Tally(self._asked)
            if stage == "critique":
                # the critique reads the batch and the transcription
                found = self._critique(
                    doc, fresh, before, made.get("transcription", _Made())
                )
            else:
                found = count[stage](doc, fresh, before)
            before = made[stage] = found
            if before.fails:
                break
        return made

    def _transcription(
        self, doc: DocumentRun, fresh: bool, _before: _Made
    ) -> _Made:
        from yizkr.pipeline.textlayer import PdfExclusionError, is_pdf_input

        method = self._transcription_route(doc, fresh)
        if not fresh:
            recheck = (
                functools.partial(self._transcription_failure, doc)
                if method in CHECKED_ROUTES or is_pdf_input(doc.source)
                else None
            )
            try:
                view = self._stands(
                    doc,
                    doc.transcription,
                    Transcription.model_validate_json,
                    recheck,
                )
            except PdfExclusionError as exc:
                return _Made(fails=_why(exc))
            if view is not None:
                located = self._locate_transcription(doc, view)
                changed = located.source_context != view.source_context
                return _Made(reused=True, view=located, changed=changed)
        made = _Made(tally=self._tally)
        keyed = self._keyed.get("transcribe")
        self._reading_pages = method == PAGED
        if keyed is not None:
            keyed.stood_in = 0
        result = None
        try:
            result = self._transcribe(doc, method)
            transcription = result.require_complete()
            transcription = self._locate_transcription(doc, transcription)
            # Keep actual cached image readings before temporary prepared
            # files are closed. Unknown model answers remain estimates.
            if result.products is not None:
                self._context_images[doc.sha1] = {
                    image.sha1: image.transcription
                    for image in result.products.images
                    if image.transcription is not None
                    and image.failure is None
                }
                made.images = [
                    image.sha1
                    for image in result.products.images
                    if _may_be_excerpt(image)
                ]
        except _Unanswered:
            return made
        except (ScratchRefused, RunSettingsConflict, llm.PassPaused):
            raise
        except Exception as exc:
            made.fails = _why(exc)
            return made
        finally:
            self._reading_pages = False
            keyed = self._keyed.get("transcribe")
            if keyed is not None:
                keyed.reading_pages = False
            if result is not None:
                result.close()
        if method == PAGED and keyed is not None and keyed.stood_in:
            # a reading stood in for the engine's: the transcription is the
            # engine's to make, and the extraction after it is estimated
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
        unknown_context = self._estimate_interpretation_context(
            doc, before.view
        )
        if unknown_context:
            self._engine_for("extract")
            return _Made(
                estimated=CHUNKS if before.view is not None else WHOLE,
                size=len(before.view.text)
                if before.view is not None
                else None,
            )
        if not fresh and before.view is not None and not before.changed:
            view = self._stands(doc, doc.batch, ClaimBatch.model_validate_json)
            if view is not None:
                return _Made(reused=True, view=view, changed=False)
        from yizkr.pipeline import text_stages

        if (
            before.view is not None
            and doc.pointed() is None
            and text_stages.empty_container_reading(before.view)
        ):
            batch = text_stages.extract(
                text_stages.ExtractionRequest(
                    transcription=before.view,
                    artifact=ArtifactRef(sha1=doc.sha1),
                    strategy="chunked",
                )
            ).require_complete()
            return _Made(view=batch, changed=self._batch_changed(doc, batch))
        self._engine_for("extract")
        if before.view is None:
            return _Made(estimated=WHOLE)
        made = _Made(tally=self._tally)
        keyed = self._keyed["extract"]
        extractor = _Extractor(self._llm("extract", doc), keyed, made.tally)
        extractor.context = self._interpretation_contexts.get(doc.sha1)
        try:
            # read as the driver reads it, within the stage
            extractor.pointer = doc.pointed()
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
        if extractor.incomplete_reasons:
            made.fails = "; ".join(extractor.incomplete_reasons)
            return made
        if not made.tally.answered:
            made.view = batch
            made.changed = self._batch_changed(doc, batch)
        return made

    def _critique(
        self,
        doc: DocumentRun,
        fresh: bool,
        before: _Made,
        transcribed: _Made,
    ) -> _Made:
        """The critique of the batch `before` holds, which reads the
        transcription `transcribed` holds, as the driver builds its prompt
        (Orchestrator._step_critique): reused only when neither changed
        and the view stands, and estimated when either is not in hand."""
        unknown_context = self._estimate_interpretation_context(
            doc, transcribed.view
        )
        if (
            not unknown_context
            and not fresh
            and before.view is not None
            and not before.changed
            and transcribed.view is not None
            and not transcribed.changed
            and self._stands(
                doc, doc.critique, _findings, _unreadable_critique
            )
            is not None
        ):
            return _Made(reused=True)
        self._engine_for("critique")
        if before.view is None or transcribed.view is None or unknown_context:
            return _Made(estimated=CRITIQUE)
        made = _Made(tally=self._tally)
        with contextlib.suppress(_Unanswered):
            run_critique(
                self._llm("critique", doc),
                before.view,
                transcribed.view,
                context=self._interpretation_contexts.get(doc.sha1),
            )
        return made

    def _interpretation_images(self, parent, sources):
        if parent.sha1 in self._context_images:
            return self._context_images[parent.sha1]
        return super()._interpretation_images(parent, sources)

    def _estimate_interpretation_context(self, doc, transcription) -> bool:
        """Future model answers leave context-dependent keys unknown."""
        if any(
            (made := self._context_made.get(parent.sha1)) is not None
            and made.view is None
            and not made.fails
            for parent in self._interpretation_parents.get(doc.sha1, ())
        ):
            return True
        if transcription is not None:
            self._interpretation_contexts[doc.sha1] = (
                self._interpretation_context(doc, transcription)
            )
        return False

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
            # the replay is known; the forks after it are estimated, those
            # the cache's chunks leave unanswered, since a replay that
            # answers differently starts the extraction over from its new
            # answer: its forks, the ones the cache answered among them,
            # can number otherwise, and a refused answer takes the
            # fallback
            calls.known -= made.tally.chunk_misses
            calls.estimated += made.tally.chunk_misses
            calls.replays += made.tally.replays
            calls.replay_calls += made.tally.chunk_misses
            calls.more += made.tally.chunk_hits + FIRST_TURNS - 1
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
        answers, document by document (llm.estimate_calls): the whole
        stage after a transcription the engine makes, and the rest of it
        after an entity manifest the engine answers. A run's measure
        counts every call of the stage, so the calls the estimate keyed
        already (the first turns, a kept refused attempt among them, and
        any chunk) are taken off it; the mean for the kind, or the text's
        length, counts one first turn, the one the engine answers, known
        already."""
        extractions = {
            doc: stages["extraction"]
            for doc, stages in made.items()
            if "extraction" in stages
        }
        waiting = {
            doc: e
            for doc, e in extractions.items()
            if e.estimated in (WHOLE, CHUNKS)
        }
        if not waiting:
            return
        baseline = self._baseline(
            {(doc.sha1, "extraction") for doc in waiting}
        )
        plan = [
            {"sha1": doc.sha1, "kind": doc.kind, "stages": ["extraction"]}
            for doc in extractions
        ]
        everyone = {(doc.sha1, "extraction") for doc in extractions}
        calls = estimate.stages["extraction"]
        for doc, extraction in waiting.items():
            counted = llm.estimate_calls(
                plan,
                current=everyone - {(doc.sha1, "extraction")},
                baseline=baseline,
                sizes={doc.sha1: extraction.size} if extraction.size else {},
            )["extraction"]
            tally = extraction.tally
            keyed = tally.chunk_hits + tally.chunk_misses
            if extraction.estimated == WHOLE:
                count = counted.calls
            elif counted.measured:
                count = counted.calls - tally.first_turns - keyed
            else:
                count = counted.calls - 1 - keyed
            calls.estimated += max(0, count)
            calls.estimated_documents += 1
            calls.measured += counted.measured

    def _adjudication(self) -> tuple[int, str] | None:
        """The calls the clustering stage's adjudication asks at most, and
        whose identity map counts them: the run's own, when the run it
        would invoke again has one, else the newest run's in the runs
        directory that has one; None when no run has. A map the stage
        adjudicated records the count it planned before any verdict; an
        older one, the calls its ambiguous pairs would take."""
        from yizkr.pipeline import adjudication, clustering

        own = [self.state_dir] if self.state_dir.is_dir() else []
        others = [
            run
            for run in reversed(runs_in(self.state_dir.parent))
            if run.resolve() != self.state_dir.resolve()
        ]
        for run in own + others:
            directory = run / CORPUS_DIR / CLUSTERING
            record = adjudication.read_record(directory)
            planned = ((record or {}).get("counts") or {}).get("planned_calls")
            if isinstance(planned, int):
                return planned, run.name
            identity = clustering.read_identity_map(
                directory / clustering.IDENTITY_MAP
            )
            if identity is None:
                continue
            counts = identity["summary"].get("adjudication") or {}
            calls = counts.get(
                "calls_ambiguous_clusters", counts.get("calls_ambiguous_pairs")
            )
            if isinstance(calls, int):
                return calls, run.name
        return None

    def _baseline(self, needed: set[tuple[str, str]]) -> dict:
        """(document SHA-1, stage) -> the calls the latest invocation to
        finish the stage made for it (_finished), from the newest run in
        the runs directory back, until every pair in `needed` has its
        count."""
        measured: dict[tuple[str, str], int] = {}
        for run in reversed(runs_in(self.state_dir.parent)):
            if needed <= measured.keys():
                break
            for pair, count in _finished(RunDir(run)).items():
                measured.setdefault(pair, count)
        return measured

    def _excerpts_found(
        self,
        made: dict[DocumentRun, dict[str, _Made]],
        docs: list[DocumentRun],
    ) -> ExcerptCalls:
        """The excerpts the planned run may make, and their calls (the
        module docstring says how): each image once, however many containers
        place it, and none whose bytes a document of the corpus (`docs`) has,
        as the driver takes them (Orchestrator._excerpts_of). Each stage
        left to make starts the engine client of its role (_engine_for),
        as it would in the run, so that a client the run refuses stops the
        estimate here."""
        corpus = frozenset(doc.sha1 for doc in docs)
        planned = [s for s in DOCUMENT_STAGES[EXCERPT] if self._within(s)]
        # image SHA-1 -> the excerpt stages it counts
        images: dict[str, set[str]] = {}
        containers = 0
        self._corpus_sha1s, self._excerpts = corpus, {}
        for doc, stages in made.items():
            transcription = stages.get("transcription")
            if transcription is None:
                continue
            if transcription.reused:
                found = {
                    excerpt.sha1: self._stale_stages(excerpt, planned)
                    for excerpt in self._excerpts_of(doc, corpus)
                }
            else:
                found = {
                    sha1: list(planned)
                    for sha1 in transcription.images
                    if sha1 not in corpus
                }
            found = {sha1: left for sha1, left in found.items() if left}
            for stage in planned:
                if stage in _EXCERPT_ROLES and any(
                    stage in left for left in found.values()
                ):
                    self._engine_for(_EXCERPT_ROLES[stage])
            containers += bool(found)
            for sha1, left in found.items():
                images.setdefault(sha1, set()).update(left)
        excerpts = ExcerptCalls(images=len(images), containers=containers)
        if not images:
            return excerpts
        measures = self._excerpt_measures()
        typical = llm.estimate_calls(
            [{"sha1": "", "kind": EXCERPT, "stages": planned}]
        )
        for stage in planned:
            stale = sum(stage in left for left in images.values())
            counts = [
                count for (_, of), count in measures.items() if of == stage
            ]
            if counts:
                calls = math.ceil(stale * sum(counts) / len(counts))
            else:
                calls = stale * typical[stage].calls
            excerpts.calls[stage] = calls
            excerpts.measured_stages[stage] = len(counts)
            excerpts.stale[stage] = stale
        return excerpts

    def _stale_stages(
        self, excerpt: DocumentRun, planned: list[str]
    ) -> list[str]:
        """The planned stages of an excerpt the run holds already whose
        views it makes, as the driver tells (_stands, with its rechecks:
        Orchestrator._transcription, _critique). A transcription that does
        not stand is made again as the driver makes it, from the reading a
        container keeps, which reads files only: when its content is the
        one the excerpt's batch was made from, the views after it are
        judged as they stand; otherwise, or when it cannot be made (the
        excerpt fails there), it is the first stage made, and every one
        after it is made too."""
        fresh = self._fresh(excerpt)
        route = self._transcription_route(excerpt, fresh)
        checked = route in CHECKED_ROUTES
        views = {
            "transcription": (
                excerpt.transcription,
                Transcription.model_validate_json,
                functools.partial(self._transcription_failure, excerpt)
                if checked
                else None,
            ),
            "extraction": (
                excerpt.batch,
                ClaimBatch.model_validate_json,
                None,
            ),
            "critique": (excerpt.critique, _findings, _unreadable_critique),
        }
        for stage in planned:
            self._require_processing_dependencies(
                excerpt,
                stage,
                **({"route": route} if stage == "transcription" else {}),
            )
        if fresh:
            return list(planned)
        unknown_context = False
        if any(stage in {"extraction", "critique"} for stage in planned):
            current = self._transcription_view(excerpt)
            unknown_context = self._estimate_interpretation_context(
                excerpt, current
            )
        left: list[str] = []
        for at, stage in enumerate(planned):
            if stage in {"extraction", "critique"} and unknown_context:
                return left + planned[at:]
            if self._stands(excerpt, *views[stage]) is not None:
                continue
            if stage != "transcription":
                return left + planned[at:]
            try:
                with self._transcribe(excerpt, route) as result:
                    remade = result.require_complete()
            except Exception:
                return [stage]
            if content_hash(
                remade.model_dump(mode="json"),
                volatile=TRANSCRIPTION_VOLATILE,
            ) != excerpt.derivations.content(excerpt.transcription.name):
                return planned[at:]
            left.append(stage)
        return left

    def _excerpt_measures(self) -> dict[tuple[str, str], int]:
        """(excerpt SHA-1, stage) -> the calls the latest invocation to
        finish the stage made for it (_finished), over every run in the
        runs directory, newest first: the excerpts each run's plan names
        (its manifest's documents of kind EXCERPT)."""
        measured: dict[tuple[str, str], int] = {}
        for run in reversed(runs_in(self.state_dir.parent)):
            rundir = RunDir(run)
            manifest = rundir.manifest()
            planned = (
                manifest.get("documents")
                if isinstance(manifest, dict)
                else None
            )
            if not isinstance(planned, list):
                continue
            excerpts = {
                entry["sha1"]
                for entry in planned
                if isinstance(entry, dict)
                and entry.get("kind") == EXCERPT
                and isinstance(entry.get("sha1"), str)
            }
            if not excerpts:
                continue
            for pair, count in _finished(rundir).items():
                if pair[0] in excerpts:
                    measured.setdefault(pair, count)
        return measured


def _finished(rundir: RunDir) -> dict[tuple[str, str], int]:
    """(document SHA-1, stage) -> the calls the latest invocation of the
    run to finish the stage made for it (llm.run_calls, over that
    invocation's events). A stage counts only as an invocation that
    finished it made it, so a later invocation cut short in it, a
    refresh's say, leaves the measure whole."""
    invocations: dict[Any, list[dict]] = {}
    for event in rundir.events():
        invocation, subject = event.get("invocation"), event.get("subject")
        # a line no writer makes (an invocation or subject that is a list
        # or an object) is passed over, as a torn one is
        if not isinstance(invocation, str | int | None) or not isinstance(
            subject, str | None
        ):
            continue
        invocations.setdefault(invocation, []).append(event)
    finished: dict[tuple[str, str], int] = {}
    for events in invocations.values():
        done = {
            (event.get("subject"), event.get("stage"))
            for event in events
            if event.get("event") == "done"
        }
        for pair, count in llm.run_calls(events).items():
            if pair in done:
                finished[pair] = count
    return finished


def _may_be_excerpt(image: ImageReading) -> bool:
    """Whether the run may make an excerpt of an image a container's
    transcription read on its own, as the driver judges the reading it
    keeps (Orchestrator._excerpts_of): one whose record calls some page
    transcribed or illegible, not every page blank or not a document
    page. Every image has a reading here: the cache serves none the check
    fails, and a reading that stands in for the engine's passes it and
    calls its page illegible, so its image may be one."""
    record = image.transcription.record()
    return record is None or not set(record.statuses()) <= {
        BLANK,
        NOT_A_DOCUMENT_PAGE,
    }


def _holders(runs: Path, cache: Path | None) -> list[str]:
    """Who holds the runs directory and the LLM cache now, as their lock
    files record it, read without taking either lock. A deterministic
    document plan supplies no cache and never observes its state."""
    held = []
    observed = [("the runs directory", runs, runs_holder(runs))]
    if cache is not None:
        observed.append(("the LLM cache", cache, llm.cache_holder(cache)))
    for what, where, holder in observed:
        if holder is not None:
            held.append(f"{what} {where} by {holder}")
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
    Estimator), with the runs directory neither held nor written. A runs
    directory the run refuses (runs_refusal) is refused here too
    (ScratchRefused)."""
    refusal = runs_refusal(runs)
    if refusal is not None:
        raise ScratchRefused(refusal)
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
    excerpts = estimate.excerpts
    summary = (
        f"Engine calls: {total.engine_calls}, {total.known} known and "
        f"{total.estimated} estimated"
    )
    if total.more + excerpts.total:
        summary += f", and up to {total.more + excerpts.total} more"
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
            f"- Up to {total.more} more, where only the engine's answers "
            "decide: an entity manifest the engine answers may be refused, "
            "and then its second attempt and the segmentation's two are "
            "asked (3 more for an extraction at most); a replayed "
            "conversation that answers differently starts its extraction "
            "over, asks its forks the cache answered too, and can make a "
            "number of chunks of its own. The chunks an entity manifest "
            "makes follow its entities, which a text's length only "
            "approaches, so a dense document can take more still: an "
            "estimate, not a ceiling."
        )
    if excerpts.total:
        lines.append(_excerpt_line(excerpts))
    lines.append(
        "- Not counted: calls asked again after an engine error or after a "
        "chunk answer cut off (at most 2 for a chunk), the preflight's one "
        "trivial call per (model, effort) pair of the roles within the "
        "invocation's --until range, which every invocation whose range "
        "holds a stage that may call the engine makes, a resume that "
        "reuses every view included, unless the installed claude CLI is not "
        "the run's; and the waits for the subscription, which are no calls. "
        "A slice the anomaly checks pause makes fewer calls than counted."
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
                "measured them, the others by the mean for their kind over "
                "the runs measured, or by their text's length"
            )
        else:
            how = (
                "by the mean for their kind over the runs measured, or by "
                "their text's length"
            )
        parts.append(
            _plural(
                extraction.estimated - extraction.replay_calls,
                "extraction call",
            )
            + f" for {_plural(documents, 'document')}, {how}"
        )
    if extraction.replays:
        parts.append(
            f"{_plural(extraction.replay_calls, 'chunk call')} after "
            f"{_plural(extraction.replays, 'replayed conversation')}, those "
            "the cache's chunks leave unanswered"
        )
    critique = estimate.stages.get("critique", StageCalls())
    if critique.estimated:
        parts.append(f"{_plural(critique.estimated, 'critique')}, one each")
    return parts


def _excerpt_line(excerpts: ExcerptCalls) -> str:
    """What the excerpts the run may make come to, and how each stage's
    calls are counted."""
    placed = (
        f"{_plural(excerpts.images, 'image')} that "
        f"{_plural(excerpts.containers, 'PDF or deck', 'PDFs or decks')} "
        + (
            "places on its pages or slides"
            if excerpts.containers == 1
            else "place on their pages or slides"
        )
        + ", each read on its own,"
    )
    stages = []
    for stage, count in excerpts.calls.items():
        if not excerpts.stale.get(stage):
            continue
        measured = excerpts.measured_stages.get(stage, 0)
        if measured:
            how = (
                "by the mean over "
                f"{_plural(measured, 'excerpt')} the runs measured"
            )
        else:
            how = "as typical, no run having measured it"
        stages.append(f"{count} {stage}, {how}")
    return (
        f"- Up to {excerpts.total} more for excerpts: {placed} may each "
        "be an excerpt, a document of its own (ADR-0008): every stage of "
        "one whose PDF or deck this invocation transcribes, though views "
        "held for it may stand, and of one held from an earlier invocation, "
        "the "
        "stages whose views do not stand. Their calls: "
        + "; ".join(stages)
        + ". Up to, since an image the engine reads blank or as no document "
        "page makes no excerpt, and the cache may answer an excerpt's calls."
    )


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
    if estimate.callless_stages:
        one = len(estimate.callless_stages) == 1
        notes.append(
            f"- No call: the corpus stage{'' if one else 's'} "
            + ", ".join(estimate.callless_stages)
            + (" makes none." if one else " make none.")
        )
    if estimate.adjudication is not None:
        calls, run = estimate.adjudication
        notes.append(
            f"- Up to {_plural(calls, 'adjudication call')} more for the "
            "clustering stage's ambiguous pairs, as the identity map of run "
            f"{run} counts them: the pairs of each ambiguous cluster, "
            "packed as the stage packs them. Up to, since the LLM cache may "
            "answer them, and people the verdicts merge can settle a union's "
            "or an event's pair with no call."
        )
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
            + "; ".join(problem.rstrip(".") for problem in estimate.preflight)
            + "."
        )
    return notes
