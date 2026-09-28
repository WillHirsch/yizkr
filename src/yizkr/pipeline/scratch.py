"""The scratch driver: `yizkr scratch run` takes the whole corpus through
the pipeline, unattended (ADR-0025).

A scratch run is one run directory under the runs directory
(`var/scratch/<run id>/` by default), with fresh state directories: every
document's views are made again in it by the code under test, and every
model call goes through the LLM cache, which answers the calls the code
leaves the same (ADR-0032). The run directory is the orchestrator's
state directory and run directory at once (yizkr.pipeline.orchestrate,
yizkr.pipeline.rundir), so the live monitor (yizkr.pipeline.monitor)
follows it as it follows any run.

**Stages.** Each document takes the per-document stages its kind plans
(orchestrate.DOCUMENT_STAGES): transcription, which the transcription
check guards, extraction and the critique; or the GEDCOM adapter for a
.ged file. Nothing before the barrier reads or writes the wiki: a scratch
run registers no artifact, and a deck's derived media are recorded in its
directory (ADR-0025 §2). There is no human stop. When every document's
per-document stages have ended, done or failed, the run is at the
**barrier**, and the driver runs the corpus stages registered with it
(CORPUS_STAGES), in order, once each, over the documents whose stages all
completed. Face detection is never a stage of a scratch run.

**Order.** One document of each kind, the first by name, goes through
every per-document stage first: the canaries. The rest follow stage by
stage, every transcription before any extraction and every extraction
before any critique, a document's own stages always in order. So a
misalignment of any kind shows within the first calls, and each stage's
rate is known early. `until` stops the run after a stage.

**Control.** Before each stage of each document, and before each corpus
stage, the driver reads the run's control file (rundir.fold_control): a
pause of the whole run, a stage or a document kind holds dispatch for its
slice until a resume, and the driver waits while nothing else can go on;
an abort holds its slice for the rest of the invocation. The monitor, the
anomaly checks and `yizkr scratch pause|resume|abort` write those
requests. A stage in flight when a request arrives finishes. Ctrl-C
stops the invocation at once, and the engine's calls in flight with it.
Every completed call is in the cache and every completed view in its
directory, so invoking the run again resumes it with nothing completed
made again: a view that stands is reused.

**The engine's pauses.** A subscription limit pauses the pass with the
time it can go on (llm.UsageLimitReached). The invocation ends `paused`,
the driver waits until then while honouring the control file (an abort
of the run ends the wait), and invokes the run again, with the slices
the control file stopped still stopped. Any other pause (a claude CLI
that changed, a cache another process holds, a call that ran on usage
credits) ends the driver, which says what to do.

**The manifest.** A scratch run keeps its settings (rundir: fixed
settings): an invocation that asks for others is refused. The one
exception is the code, once every per-document stage of the run has
ended: an invocation on other code, one that registers a corpus stage
say, goes past the barrier alone, dispatching no per-document stage, and
records its own code beside the run's. Besides the settings, run.json
records the engine's pacing and the invocation's `scratch` facts: how far
it was asked to go, the canaries, the corpus stages and whether it went
to the end.

**Before any call** the driver refuses to start without a never-ingest
list, and runs its preflight: the engine's CLI and its login, a full
model id for every role, the engine's pacing settings, free disk space,
the PDF renderer when the corpus holds a PDF, and, only when a stage
that touches the wiki is to run, a reachable stack and an instance that
carries no kept-state marker. One driver at a time uses a runs
directory (hold).

Run directories hold transcriptions, claims and the cache's raw model
output: they are local-only data, kept under var/ (docs/AGENT_PROTOCOL.md
§6).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yizkr.core.corpus import MISSING_LIST, ExclusionConfigError
from yizkr.pipeline.orchestrate import (
    BATCHED,
    DOCUMENT_STAGES,
    DUPLICATE,
    FAILED,
    MODEL_ROLES,
    MODEL_VARIABLE,
    PAUSED,
    DocumentRun,
    Orchestrator,
)
from yizkr.pipeline.rundir import (
    ABORT,
    CONTROL,
    DEVELOPMENT_STAGE,
    DONE,
    MANIFEST,
    NEEDS_ATTENTION,
    PAUSE,
    RESUME,
    DispatchStopped,
    ModelAliasRefused,
    RunDir,
    RunSettingsConflict,
    code_version,
    exclusions_record,
    run_state,
    settings_conflicts,
    summarise,
)
from yizkr.pipeline.rundir import FAILED as STAGE_FAILED
from yizkr.pipeline.stages import read_jsonl

# the run manifest's mode for a scratch run
MODE = "scratch run"
# where the runs, and the LLM cache, are kept unless told otherwise
DEFAULT_RUNS = Path("var/scratch")
DEFAULT_CACHE = Path("var/llm-cache")
# the file in the runs directory that the driver using it locks and
# records itself in; no run directory's name starts with a dot
LOCK_FILE = ".lock"
# the directory of the run directory the corpus stages keep their outputs
# in, one directory each; the orchestrator gives no document its name
CORPUS_DIR = "corpus"
# the event that says the run is past the barrier
BARRIER = "barrier"
# a document whose transcription is its last stage done so far
TRANSCRIBED = "transcribed"
# the rank of each per-document stage: the rest of the documents take them
# rank by rank, after the canaries; the barrier and the corpus stages come
# after them
PHASES = {"transcription": 0, "extraction": 1, "gedcom": 1, "critique": 2}
_BARRIER_RANK = max(PHASES.values()) + 1
# a stage's states that end it: done, needing attention, or failed
_ENDED = frozenset({DONE, NEEDS_ATTENTION, STAGE_FAILED})
_COMPLETED = frozenset({DONE, NEEDS_ATTENTION})
# what a run's state is read with, until the run report takes its place
REPORT_COMMAND = "yizkr stages state"
# how often the driver reads the control file while nothing can go on
POLL_SECONDS = 5.0
# the free space a run needs on the filesystems of its run directory and
# of the LLM cache
MIN_FREE_BYTES = 2 * 1024**3
# how long after a subscription limit's reported time the driver invokes
# the run again: the engine holds its pause a moment past that time
RESUME_MARGIN = 60.0
# `--resume` with no run named: the latest run
LATEST = "latest"

# exit statuses: the invocation finished and no document failed; it
# finished with a failure, or the driver refused to start; it stopped
# short, and invoking the run again goes on; Ctrl-C
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 2
EXIT_INTERRUPTED = 130

# what a stage of a document came to in the dispatch loop: it ended (done
# or reused), the control file held it as it started, or it failed
_ENDED_HERE = "ended"
_HELD = "held"
_FAILED_HERE = "failed"


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# -- corpus stages ------------------------------------------------------------


@dataclass(frozen=True)
class CorpusContext:
    """What a corpus stage is given: the run directory (`run`: its
    manifest, events and control file), the documents whose per-document
    stages all completed, with their views in their directories, and a
    directory of its own in the run directory (`directory`, which the
    stage makes when it writes there), the connection settings' env file,
    and the driver's log."""

    run: RunDir
    documents: tuple[DocumentRun, ...]
    directory: Path
    env_file: Path | None
    log: Callable[..., None]


@dataclass(frozen=True)
class CorpusStage:
    """A stage the driver runs once over the whole corpus, after the
    barrier. `run` takes the stage's CorpusContext and returns the reasons
    its output needs attention, if any: labels that never quote a
    document, since they reach the run's events. An exception fails the
    stage, and the stages after it wait for the next invocation.

    `touches_wiki` marks a stage that reads or writes the wiki: the driver
    runs one only when the stack is reachable and the instance carries no
    kept-state marker, which it checks before the run starts and again
    before the stage."""

    name: str
    run: Callable[[CorpusContext], Sequence[str] | None]
    touches_wiki: bool = False


# the corpus stages, in the order they run after the barrier
CORPUS_STAGES: tuple[CorpusStage, ...] = ()

# whether the instance carries the kept-state marker (ADR-0025 §6): a
# callable of the env file. None while nothing can tell, and then a stage
# that touches the wiki is refused
KEPT_STATE: Callable[[Path | None], bool] | None = None


# -- refusals -----------------------------------------------------------------


class ScratchRefused(RuntimeError):
    """The driver will not start, or go on: what the message names has to
    change first. Nothing was dispatched for it."""


class PreflightFailed(ScratchRefused):
    """The host or the configuration cannot serve a scratch run: every
    problem the preflight found, before anything was written or called."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "a scratch run cannot start:\n"
            + "\n".join(f"  - {problem}" for problem in self.problems)
        )


class ScratchRunInUse(ScratchRefused):
    """Another driver uses the runs directory: one scratch run at a time."""


# -- the host -----------------------------------------------------------------


def claude_logged_in() -> bool:
    """Whether the claude CLI has a login to run on, as far as can be told
    without running it: a subscription token in the environment
    (CLAUDE_CODE_OAUTH_TOKEN), the credentials file in the CLI's config
    directory, or an account in its global config. Nothing is read from
    them but whether they are there."""
    from yizkr.pipeline.llm import (
        claude_config_dir,
        claude_global_config_path,
        claude_projects_dir,
    )

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    config_dir = claude_config_dir()
    # the projects folder is in the config directory, ~/.claude by default
    home = claude_projects_dir(config_dir).parent
    if (home / ".credentials.json").is_file():
        return True
    try:
        config = json.loads(
            claude_global_config_path(config_dir).read_text(
                encoding="utf-8-sig"
            )
        )
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and bool(config.get("oauthAccount"))


def stack_unreachable(env_file: Path | None) -> str | None:
    """Why the wiki cannot be reached with the connection settings, or
    None when its API answers."""
    from yizkr.core.config import Config
    from yizkr.core.wiki import WikiClient

    try:
        client = WikiClient(Config.load(env_file, require_admin=False))
        client.api_get({"action": "query", "meta": "siteinfo"})
    except (Exception, SystemExit) as exc:
        return _why(exc)
    return None


def _free_bytes(path: Path) -> int:
    """Free space on the filesystem that holds `path`, or would."""
    probe = Path(os.path.abspath(path))
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


# -- the runs directory -------------------------------------------------------


@contextlib.contextmanager
def hold(runs: Path) -> Iterator[None]:
    """Hold the runs directory for as long as the block runs: one driver
    at a time uses it, and so runs one scratch run at a time. The lock is
    flock's on its LOCK_FILE, which the kernel releases however the
    holder ends; the holder records itself there, and empties the record
    as it lets go. Another holder is refused at once, by name
    (ScratchRunInUse). The directory is made private to its owner (mode
    700): its runs hold corpus content."""
    runs = Path(runs)
    runs.mkdir(parents=True, exist_ok=True)
    os.chmod(runs, 0o700)
    fd = os.open(runs / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record = os.pread(fd, 1 << 16, 0).decode("utf-8", "replace")
            raise ScratchRunInUse(
                f"{runs} is in use by another scratch driver "
                f"({record.strip() or 'not yet recorded'}): one scratch run "
                "runs at a time. Follow it with `yizkr scratch watch`, or "
                "wait for it to end."
            ) from None
        held = True
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "command": " ".join(sys.argv)[:500],
            "since": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(record) + "\n").encode(), 0)
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
        os.close(fd)


def new_run_id(runs: Path) -> str:
    """A new run's id, the name of its directory in `runs`: the UTC time
    it starts, to the second, then the microsecond in six hex digits, so
    that the runs in a runs directory sort as they started. An id a
    directory there has already is not taken."""
    while True:
        now = datetime.now(UTC)
        run_id = f"{now:%Y%m%dT%H%M%SZ}-{now.microsecond:06x}"
        if not (Path(runs) / run_id).exists():
            return run_id


def runs_in(runs: Path) -> list[Path]:
    """The run directories in `runs` (each with its run.json), oldest
    first (new_run_id)."""
    runs = Path(runs)
    if not runs.is_dir():
        return []
    return sorted(
        path
        for path in runs.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and (path / MANIFEST).is_file()
    )


def resolve_run(runs: Path, name: str | None = None) -> Path:
    """The run directory `name` names: a path to one, or a run id in
    `runs`; the latest run in `runs` when `name` is None or LATEST."""
    if name is None or name == LATEST:
        found = runs_in(runs)
        if not found:
            raise ScratchRefused(f"no scratch run in {runs}")
        return found[-1]
    path = Path(name)
    if not (path / MANIFEST).is_file():
        path = Path(runs) / name
    if not (path / MANIFEST).is_file():
        raise ScratchRefused(
            f"{name}: no run there (no {MANIFEST} in {path}); name a run "
            f"directory, or the id of a run in {runs}"
        )
    return path


def is_complete(run: Path) -> bool:
    """Whether the run's latest invocation went through every stage to
    the end (a scratch run's `scratch.complete`)."""
    scratch = RunDir(run).manifest().get("scratch")
    return isinstance(scratch, dict) and scratch.get("complete") is True


def choose_run(
    runs: Path, *, resume: str | None = None, new: bool = False
) -> Path:
    """The run directory a `scratch run` invocation takes: a new one
    (`new`); the run `resume` names (LATEST, the latest), which must be a
    scratch run; otherwise the latest run when it has not been taken to
    the end, so that running the same command again resumes it, and a new
    one when it has."""
    runs = Path(runs)
    if new:
        return runs / new_run_id(runs)
    if resume is not None:
        path = resolve_run(runs, resume)
        mode = RunDir(path).manifest().get("mode")
        if mode != MODE:
            raise ScratchRefused(
                f"{path} is not a scratch run (its mode is {mode!r})"
            )
        return path
    found = [
        path
        for path in runs_in(runs)
        if RunDir(path).manifest().get("mode") == MODE
    ]
    if found and not is_complete(found[-1]):
        return found[-1]
    return runs / new_run_id(runs)


# -- the driver ---------------------------------------------------------------


class ScratchDriver(Orchestrator):
    """Runs one scratch run in `run_dir` (the module docstring says how).

    The per-document stages are the orchestrator's own, taken one stage of
    one document at a time; `run` replaces the orchestrator's loop and its
    gates. `until` names the stage the run stops after: a per-document
    stage (`gedcom` stops with `extraction`, the rank they share) or a
    corpus stage. `corpus_stages` defaults to CORPUS_STAGES and
    `kept_state` to KEPT_STATE. `cache_dir` is required: every model call
    of a scratch run goes through the LLM cache. `min_free` is the free
    space, in bytes, the preflight asks for. `clock`, `sleep` and `poll`
    serve the waits (for a resume, and for the subscription); the other
    options are the orchestrator's. A scratch run keeps its settings."""

    def __init__(
        self,
        folder: Path,
        run_dir: Path,
        *,
        run_id: str | None = None,
        until: str | None = None,
        corpus_stages: Sequence[CorpusStage] | None = None,
        kept_state: Callable[[Path | None], bool] | None = None,
        cache_dir: Path | None = DEFAULT_CACHE,
        min_free: int = MIN_FREE_BYTES,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        poll: float = POLL_SECONDS,
        llm: str = "claude-code",
        **options: Any,
    ) -> None:
        if cache_dir is None:
            raise ValueError(
                "a scratch run takes every model call through the LLM cache"
            )
        super().__init__(
            folder,
            run_dir,
            llm=llm,
            cache_dir=cache_dir,
            fixed_settings=True,
            **options,
        )
        self.run_id = run_id or Path(run_dir).name
        self.corpus_stages = tuple(
            CORPUS_STAGES if corpus_stages is None else corpus_stages
        )
        names = [stage.name for stage in self.corpus_stages]
        clashes = sorted(
            {name for name in names if name in PHASES or names.count(name) > 1}
        )
        if clashes:
            raise ValueError(
                f"corpus stages need names of their own: {', '.join(clashes)}"
            )
        if until is not None and until not in self.stage_names():
            raise ValueError(
                f"no stage {until!r} to stop after: "
                f"{', '.join(self.stage_names())}"
            )
        self.until = until
        self.kept_state = KEPT_STATE if kept_state is None else kept_state
        self.min_free = min_free
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self.poll = poll
        # the batch each document's extraction made or reused in this
        # invocation, for its critique
        self._batches: dict[str, Any] = {}
        # the directories made ready in this invocation
        self._prepared: set[Path] = set()

    # -- stages -----------------------------------------------------------

    def stage_names(self) -> list[str]:
        """Every stage the run can stop after, in order."""
        return [*PHASES, *(stage.name for stage in self.corpus_stages)]

    def _rank(self, stage: str) -> int:
        if stage in PHASES:
            return PHASES[stage]
        names = [corpus.name for corpus in self.corpus_stages]
        return _BARRIER_RANK + 1 + names.index(stage)

    def _within(self, stage: str) -> bool:
        return self.until is None or self._rank(stage) <= self._rank(
            self.until
        )

    def _past_the_barrier(self) -> bool:
        """Whether this invocation is to go past the barrier."""
        return self.until is None or self._rank(self.until) >= _BARRIER_RANK

    def _to_the_end(self) -> bool:
        """Whether this invocation is to go through every stage."""
        last = max(self._rank(name) for name in self.stage_names())
        return self.until is None or self._rank(self.until) >= last

    def _corpus_in_range(self) -> list[CorpusStage]:
        return [
            stage for stage in self.corpus_stages if self._within(stage.name)
        ]

    # -- preflight --------------------------------------------------------

    def _pacing(self) -> dict[str, float] | None:
        """The engine's pacing (docs/LLM_CACHE.md, Subscription limits):
        the share of each usage window left to development sessions and
        the longest a call waits for the subscription. ValueError for a
        setting the engine would refuse."""
        if self.llm != "claude-code":
            return None
        from yizkr.pipeline.llm import _max_wait_setting, _reserve_setting

        return {
            "reserve": _reserve_setting(None),
            "max_wait": _max_wait_setting(None),
        }

    def _engine_problems(self) -> list[str]:
        if self.llm == "claude-code":
            from yizkr.pipeline.llm import ClaudeCodeClient

            if not ClaudeCodeClient.available():
                return [
                    "the claude CLI is not installed or not on PATH: install "
                    "it, and log in once"
                ]
            if not claude_logged_in():
                return [
                    "the claude CLI has no login: run `claude` once and log "
                    "in (a Claude Pro or Max subscription), or set "
                    "CLAUDE_CODE_OAUTH_TOKEN"
                ]
        elif self.llm == "anthropic" and not os.environ.get(
            "ANTHROPIC_API_KEY"
        ):
            return ["the anthropic engine needs ANTHROPIC_API_KEY set"]
        return []

    def _disk_problems(self) -> list[str]:
        problems = []
        gib = 1024**3
        for what, path in (
            ("the run directory", self.state_dir),
            ("the LLM cache", self.cache_dir),
        ):
            free = _free_bytes(path)
            if free < self.min_free:
                problems.append(
                    f"{what} ({path}) has {free / gib:.1f} GiB free, less "
                    f"than the {self.min_free / gib:.1f} GiB a run asks for"
                )
        return problems

    def _kept_problem(self) -> str | None:
        """Why no stage may touch the wiki, as far as the kept-state
        marker goes, or None."""
        if self.kept_state is None:
            return (
                "nothing can tell whether the instance carries the "
                "kept-state marker, so no stage may touch the wiki"
            )
        try:
            kept = self.kept_state(self.env_file)
        except Exception as exc:
            return (
                f"whether the instance carries the kept-state marker "
                f"cannot be told ({_why(exc)})"
            )
        if kept:
            return (
                "the instance carries the kept-state marker, and a scratch "
                "run never touches a kept instance"
            )
        return None

    def _wiki_problems(self) -> list[str]:
        touching = [
            stage.name
            for stage in self._corpus_in_range()
            if stage.touches_wiki
        ]
        if not touching:
            return []
        problems = []
        unreachable = stack_unreachable(self.env_file)
        if unreachable is not None:
            problems.append(
                f"the stack cannot be reached ({unreachable}), and "
                f"{', '.join(touching)} touch the wiki"
            )
        kept = self._kept_problem()
        if kept is not None:
            problems.append(f"{kept} ({', '.join(touching)} would)")
        return problems

    def _preflight_host(self) -> None:
        """Everything that needs no document: the models, the pacing, the
        engine and its login, disk space, and the wiki when a stage to run
        touches it. PreflightFailed lists every problem."""
        problems = []
        try:
            self._refuse_model_aliases()
        except ModelAliasRefused as exc:
            problems.append(str(exc))
        unnamed = [
            role for role in MODEL_ROLES if not self._configured_model(role)[0]
        ]
        if unnamed:
            problems.append(
                f"no model is configured for {', '.join(unnamed)}: set "
                f"${MODEL_VARIABLE} to a full model id, such as "
                "claude-opus-5-5. The engine's own default is not keyed, so "
                "the LLM cache could not tell its answers from another "
                "model's"
            )
        try:
            self._pacing()
        except ValueError as exc:
            problems.append(str(exc))
        problems += self._engine_problems()
        problems += self._disk_problems()
        problems += self._wiki_problems()
        if problems:
            raise PreflightFailed(problems)

    def _preflight_documents(self, docs: Sequence[DocumentRun]) -> None:
        """The PDF renderer, when the corpus holds a PDF (the vision
        engine reads one through it, and the transcription check counts
        its pages with it)."""
        from yizkr.pipeline.transcribe import (
            MissingPrerequisite,
            preflight_vision_transcription,
        )

        try:
            preflight_vision_transcription(
                doc.source for doc in docs if doc.kind == "media"
            )
        except MissingPrerequisite as exc:
            raise PreflightFailed([str(exc)]) from None

    # -- the manifest -----------------------------------------------------

    def _fields(
        self, docs: Sequence[DocumentRun], canaries: Sequence[DocumentRun]
    ) -> dict[str, Any]:
        """What the invocation records in run.json (docs/RUN_MANIFEST.md):
        settings and hashes, never a prompt or a document's content."""
        return {
            "run_id": self.run_id,
            "mode": MODE,
            "project_stage": DEVELOPMENT_STAGE,
            "engine": {"name": self.llm, "cli_version": None},
            "roles": self._roles(),
            "cache": os.path.abspath(self.cache_dir),
            "corpus": {
                "folder": os.path.abspath(self.folder),
                "exclusions": exclusions_record(self.exclusions),
            },
            "code": code_version(),
            "config": {
                "engine": self.llm,
                "language": self.language,
                "fallback_language": self.fallback_language,
                "extract_effort": self.extract_effort,
                "review_effort": self.review_effort,
                "extract_model": self.extract_model,
                "refresh": list(self.refresh),
                "epochs": dict(sorted(self.epochs.items())),
                "chunk_concurrency": self.chunk_concurrency,
            },
            "documents": [
                {
                    "sha1": doc.sha1,
                    "file": doc.source.name,
                    "kind": doc.kind,
                    "stages": list(DOCUMENT_STAGES[doc.kind]),
                }
                for doc in docs
            ],
            "pacing": self._pacing(),
            "scratch": {
                "until": self.until,
                "canaries": [doc.sha1 for doc in canaries],
                "corpus_stages": [stage.name for stage in self.corpus_stages],
                "past_barrier_only": False,
                "complete": False,
            },
        }

    def _past_barrier_only(
        self, fields: dict[str, Any], docs: Sequence[DocumentRun]
    ) -> bool:
        """Whether this invocation asks for the settings the run recorded
        but for its code, and every per-document stage of the run has
        ended: then it goes past the barrier alone."""
        recorded = self.rundir.manifest()
        if not recorded.get("run_id"):
            return False
        conflicts = settings_conflicts(recorded, fields)
        if not conflicts or any(
            name != "code" and not name.startswith("code.")
            for name, _recorded, _asked in conflicts
        ):
            return False
        states = run_state(self.rundir)
        return all(
            (state := states.get((doc.sha1, stage))) is not None
            and state.status in _ENDED
            for doc in docs
            for stage in DOCUMENT_STAGES[doc.kind]
        )

    def _complete(self, complete: bool) -> None:
        """Record whether the invocation went through every stage."""
        scratch = dict(self.rundir.manifest().get("scratch") or {})
        scratch["complete"] = complete
        self.rundir.update_manifest(scratch=scratch)

    # -- control ------------------------------------------------------------

    def _stops_in_force(self) -> list[dict]:
        """The requests that stop a slice (a stage or a kind) now, as the
        control file holds them since the latest invocation's start: the
        abort that holds a slice, or the pause that does. Read as
        rundir.fold_control reads the file."""
        held: dict[str, dict] = {}
        for request in read_jsonl(self.state_dir / CONTROL):
            action = request.get("action")
            if action == "start":
                held.clear()
                continue
            scope = request.get("scope") or "run"
            if not isinstance(scope, str):
                continue
            if (held.get(scope) or {}).get("action") == ABORT:
                continue
            if action in (PAUSE, ABORT):
                held[scope] = request
            elif action == RESUME:
                held.pop(scope, None)
        # a scope that names no stage or kind stops nothing
        return [
            request
            for scope, request in held.items()
            if scope.startswith(("stage:", "kind:"))
            and scope.partition(":")[2]
        ]

    def _carry(self, requests: Sequence[dict]) -> None:
        """Stop again, in this invocation, the slices the last one left
        stopped: its requests, with who asked and why, marked carried."""
        for request in requests:
            which, _, name = request["scope"].partition(":")
            self.rundir.request(
                request["action"],
                stage=name if which == "stage" else None,
                kind=name if which == "kind" else None,
                by=request.get("by") or "operator",
                reason=request.get("reason") or "",
                carried=True,
            )

    def _await_resume(self, stops: set[str], announced: set[str]) -> None:
        """Wait a moment for the control file to let something go on,
        saying once what holds dispatch."""
        if stops != announced:
            self.log(
                f"waiting: {'; '.join(sorted(stops))} (resume or abort it: "
                "yizkr scratch resume|abort)"
            )
            announced.clear()
            announced.update(stops)
        self._sleep(self.poll)

    # -- one document -------------------------------------------------------

    def _prepare(self, doc: DocumentRun) -> None:
        """Ready the document's directory once in the invocation, as the
        orchestrator does before a document's stages, without its
        registration: a directory whose source bytes changed, or whose
        files nothing records, is set aside and started afresh (ADR-0010);
        the directory's record comes first; and a deck's derived media are
        recorded in it (ADR-0025 §2)."""
        if doc.dir in self._prepared:
            return
        doc.state.error = None
        if doc.state.content_sha1 and doc.state.content_sha1 != doc.sha1:
            self._set_aside(doc, "content changed")
        elif doc.unrecorded:
            self._set_aside(
                doc,
                "no derivations.json records what the files in its "
                "directory were derived from"
                if not doc.derivations.path.exists()
                else "no state.json records the artifact the files in its "
                "directory were made for",
            )
        doc.dir.mkdir(parents=True, exist_ok=True)
        doc.derivations.begin()
        doc.state.content_sha1 = doc.sha1
        doc.state.file = str(doc.source)
        if doc.kind == "presentation" and not self._derived_media_current(doc):
            record = self._step_derive_media(doc)
            self.log(f"  derived media: {len(record.media)} recorded")
        self._prepared.add(doc.dir)

    def _critique(self, doc: DocumentRun, batch: Any) -> None:
        """Keep the batch for the critique stage: the orchestrator asks for
        the critique right after the batch, and a scratch run makes it in
        its own turn (_run_stage_of)."""
        self._batches[doc.sha1] = batch

    def _run_stage_of(self, doc: DocumentRun, stage: str) -> None:
        """One per-document stage of one document, through the
        orchestrator's views: made, or reused when it stands."""
        self._prepare(doc)
        if stage == "transcription":
            self._transcription(doc)
            doc.state.status = TRANSCRIBED
        elif stage in ("extraction", "gedcom"):
            self._derive(doc)
            doc.state.status = BATCHED
        else:
            super()._critique(doc, self._batches[doc.sha1])

    def _dispatch_one(self, doc: DocumentRun, stage: str) -> str:
        """Run a stage of a document, and fail only the document when it
        fails: _ENDED_HERE, _HELD (the control file held it as it started)
        or _FAILED_HERE. A pause of the pass or Ctrl-C goes on through,
        with the state the document reached saved where it can be."""
        self.log(f"{stage:13} {doc.source.name}")
        try:
            self._run_stage_of(doc, stage)
        except DispatchStopped as exc:
            self.log(f"  {doc.source.name}: {exc}")
            return _HELD
        except Exception as exc:
            self._fail(doc, _why(exc))
            return _FAILED_HERE
        except BaseException:
            unsaved = self._save(doc)
            if unsaved is not None:
                self.log(f"  {doc.source.name}: {unsaved}", file=sys.stderr)
            raise
        unsaved = self._save(doc)
        if unsaved is not None:
            self._fail(doc, unsaved, save=False)
            return _FAILED_HERE
        return _ENDED_HERE

    # -- the corpus ---------------------------------------------------------

    @staticmethod
    def _canonical(
        docs: Sequence[DocumentRun],
    ) -> tuple[list[DocumentRun], list[tuple[DocumentRun, DocumentRun]]]:
        """(the documents the run takes, (duplicate, the first document
        with its bytes)): identical bytes are one artifact (ADR-0010), and
        the first by name is the one taken."""
        first: dict[str, DocumentRun] = {}
        taken, duplicates = [], []
        for doc in docs:
            if doc.sha1 in first:
                duplicates.append((doc, first[doc.sha1]))
            else:
                first[doc.sha1] = doc
                taken.append(doc)
        return taken, duplicates

    def _duplicate(self, doc: DocumentRun, first: DocumentRun) -> None:
        """Record a document whose bytes an earlier one has, in a
        directory of its own, and take it no further."""
        try:
            self._prepare(doc)
        except Exception as exc:
            self._fail(doc, _why(exc))
            return
        doc.state.status = DUPLICATE
        doc.state.detail = (doc.state.detail or {}) | {
            "duplicate_of": first.source.name
        }
        unsaved = self._save(doc)
        if unsaved is not None:
            self._fail(doc, unsaved, save=False)
            return
        self.log(
            f"{doc.source.name}: identical content to {first.source.name}; "
            f"skipping (one artifact per distinct file, ADR-0010)"
        )

    @staticmethod
    def _canaries(docs: Sequence[DocumentRun]) -> list[DocumentRun]:
        """The first document of each kind, in the documents' order."""
        first: dict[str, DocumentRun] = {}
        for doc in docs:
            first.setdefault(doc.kind, doc)
        return [doc for doc in docs if first[doc.kind] is doc]

    def _stages(
        self, docs: Sequence[DocumentRun], canaries: Sequence[DocumentRun]
    ) -> list[tuple[DocumentRun, str]]:
        """Every (document, stage) the invocation is to take, in the order
        it takes them: each canary through its stages, then the rest by
        the stages' rank, each rank in the documents' order."""

        def planned(doc: DocumentRun) -> list[str]:
            return [s for s in DOCUMENT_STAGES[doc.kind] if self._within(s)]

        order = {id(doc): index for index, doc in enumerate(docs)}
        first = [(doc, stage) for doc in canaries for stage in planned(doc)]
        rest = [
            (doc, stage)
            for doc in docs
            if doc not in canaries
            for stage in planned(doc)
        ]
        rest.sort(key=lambda item: (PHASES[item[1]], order[id(item[0])]))
        return first + rest

    def _dispatch(
        self, pending: list[tuple[DocumentRun, str]]
    ) -> tuple[str | None, list[tuple[DocumentRun, str]]]:
        """Take the (document, stage) pairs in order, as the control file
        lets them through. Each round takes the first pair whose document
        has no earlier stage waiting and whose slice nothing stops; a pair
        a pause or an abort stops is held, with a `held` event, and the
        pairs after it go on. While a pause holds everything left, the
        driver waits for a resume or an abort. A failed document's later
        stages are dropped. Returns (why dispatch stopped short, the pairs
        it left), or (None, []) when every pair has ended."""
        pending = list(pending)
        noted: dict[tuple[str, str], str] = {}
        announced: set[str] = set()
        failed: set[int] = set()
        while True:
            pending = [item for item in pending if id(item[0]) not in failed]
            if not pending:
                return None, []
            control = self.rundir.control()
            whole = control.stopped()
            if whole is not None:
                if whole.endswith("aborted"):
                    self.log(f"run stopped ({whole})")
                    return whole, pending
                self._await_resume({whole}, announced)
                continue
            chosen = None
            waiting: set[str] = set()
            aborted: set[str] = set()
            behind: set[int] = set()
            for doc, stage in pending:
                if id(doc) in behind:
                    continue
                behind.add(id(doc))
                stop = control.stopped(stage=stage, kind=doc.kind)
                if stop is None:
                    chosen = (doc, stage)
                    break
                (aborted if stop.endswith("aborted") else waiting).add(stop)
                if noted.get((doc.sha1, stage)) != stop:
                    noted[(doc.sha1, stage)] = stop
                    self.rundir.emit(
                        "held",
                        subject=doc.sha1,
                        kind=doc.kind,
                        stage=stage,
                        reason=stop,
                    )
            if chosen is None:
                if waiting:
                    self._await_resume(waiting, announced)
                    continue
                reason = "; ".join(sorted(aborted))
                self.log(f"dispatch stopped ({reason})")
                return reason, pending
            announced.clear()
            doc, stage = chosen
            outcome = self._dispatch_one(doc, stage)
            if outcome == _HELD:
                # the stage's own check held it, with its event
                stop = self.rundir.control().stopped(
                    stage=stage, kind=doc.kind
                )
                noted[(doc.sha1, stage)] = stop or ""
                if stop is None:
                    # let through again since it was held: a moment first,
                    # so that no disagreement between the checks spins
                    self._sleep(self.poll)
                continue
            pending.remove(chosen)
            if outcome == _FAILED_HERE:
                failed.add(id(doc))

    def _completed(self, docs: Sequence[DocumentRun]) -> list[DocumentRun]:
        """The documents whose per-document stages all completed, as the
        run's events tell."""
        states = run_state(self.rundir)
        return [
            doc
            for doc in docs
            if all(
                (state := states.get((doc.sha1, stage))) is not None
                and state.status in _COMPLETED
                for stage in DOCUMENT_STAGES[doc.kind]
            )
        ]

    def _hold_corpus(self, stage: CorpusStage) -> str | None:
        """Wait while the control file pauses the corpus stage (or the
        run); the abort that stops it, if one does."""
        announced: set[str] = set()
        noted = None
        while True:
            control = self.rundir.control()
            stop = control.stopped(stage=stage.name)
            if stop is None:
                return None
            if stop != noted:
                noted = stop
                self.rundir.emit("held", stage=stage.name, reason=stop)
            if stop.endswith("aborted"):
                self.log(f"{stage.name}: not run ({stop})")
                return stop
            self._await_resume({stop}, announced)

    def _corpus(
        self, docs: Sequence[DocumentRun]
    ) -> tuple[str | None, str | None]:
        """Take the run past the barrier: the `barrier` event, then each
        corpus stage within `until`, in order. Returns (the abort that
        stopped them, the exception that failed one): (None, None) when
        every one ran."""
        completed = tuple(self._completed(docs))
        states = run_state(self.rundir)
        failed = sum(
            1
            for doc in docs
            if any(
                (state := states.get((doc.sha1, stage))) is not None
                and state.status == STAGE_FAILED
                for stage in DOCUMENT_STAGES[doc.kind]
            )
        )
        self.rundir.emit(BARRIER, documents=len(completed), failed=failed)
        self.log(
            f"barrier: {len(completed)} documents completed their stages, "
            f"{failed} failed"
        )
        for stage in self._corpus_in_range():
            stop = self._hold_corpus(stage)
            if stop is not None:
                return stop, None
            if stage.touches_wiki:
                problem = self._kept_problem()
                if problem is not None:
                    raise ScratchRefused(f"{stage.name} not run: {problem}")
            context = CorpusContext(
                run=self.rundir,
                documents=completed,
                directory=self.state_dir / CORPUS_DIR / stage.name,
                env_file=self.env_file,
                log=self.log,
            )
            self.log(f"{stage.name}: over {len(completed)} documents")
            self.rundir.emit("started", stage=stage.name)
            try:
                attention = list(stage.run(context) or ())
            except Exception as exc:
                self.rundir.emit(
                    "failed", stage=stage.name, reason=_why(exc)[:500]
                )
                self.log(
                    f"{stage.name}: FAILED — {_why(exc)}", file=sys.stderr
                )
                return None, type(exc).__name__
            self.rundir.emit(
                "done", stage=stage.name, attention=attention or None
            )
        return None, None

    # -- one invocation -----------------------------------------------------

    def _report(self, outcome: str, reason: str | None) -> None:
        """Say how the invocation ended, the stages' states, and the
        commands that show the run and resume it."""
        self.log("")
        self.log(
            f"scratch run {self.run_id}: {outcome}"
            + (f" ({reason})" if reason else "")
        )
        for stage, counts in summarise(run_state(self.rundir)).items():
            self.log(
                f"  {stage:14} "
                + ", ".join(
                    f"{n} {label}" for label, n in sorted(counts.items())
                )
            )
        self.log(f"report: {REPORT_COMMAND} {self.state_dir}")
        self.log(f"watch:  yizkr scratch watch {self.state_dir}")
        if not is_complete(self.state_dir):
            self.log(f"resume: yizkr scratch run --resume {self.state_dir}")

    def _invocation(self, carried: Sequence[dict]) -> Any:
        """One invocation of the run: preflight, the manifest, the
        per-document stages, the barrier and the corpus stages. Returns
        the exit status, or the UsageLimitReached to wait out before the
        run is invoked again."""
        from yizkr.pipeline.llm import PassPaused, UsageLimitReached

        # fail closed: no never-ingest list, nothing read
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        self._preflight_host()
        found = self.documents()
        if not found:
            self.log(f"no supported documents in {self.folder}")
            return EXIT_OK
        unread = [doc for doc in found if doc.unreadable is not None]
        docs = [doc for doc in found if doc.unreadable is None]
        self._preflight_documents(docs)
        taken, duplicates = self._canonical(docs)
        canaries = self._canaries(taken)

        self.rundir = RunDir(self.state_dir)
        fields = self._fields(docs, canaries)
        past = self._past_barrier_only(fields, taken)
        if past:
            if self.refresh:
                raise ScratchRefused(
                    "the code differs from the run's, so this invocation "
                    "only takes the run past the barrier and makes no view "
                    "again: drop --refresh, or start a new run"
                )
            fields["scratch"] |= {
                "past_barrier_only": True,
                "code": fields["code"],
            }
            fields["code"] = self.rundir.manifest().get("code")
        self._views, self._derived, self._batches = {}, {}, {}
        self._prepared = set()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # refused here, before anything is written, when the settings differ
        self.rundir.start(fixed_settings=True, **fields)
        self._carry(carried)
        if canaries and not past:
            self.log(
                "canaries: "
                + ", ".join(
                    f"{doc.source.name} ({doc.kind})" for doc in canaries
                )
            )
        stop = failed = None
        try:
            for doc in unread:
                self._fail(doc, doc.unreadable)
            for doc, first in duplicates:
                self._duplicate(doc, first)
            if past:
                self.log(
                    "the code differs from the run's, and every document's "
                    "stages have ended: past the barrier only"
                )
            else:
                stop, left = self._dispatch(self._stages(taken, canaries))
                for doc in {id(doc): doc for doc, _stage in left}.values():
                    doc.state.status = PAUSED
                    self._save(doc)
            if stop is None and self._past_the_barrier():
                stop, failed = self._corpus(taken)
        except KeyboardInterrupt:
            self._end_invocation(found, "interrupted")
            self._report("interrupted", None)
            raise
        except BaseException as exc:
            if isinstance(exc, PassPaused):
                outcome = "paused"
            elif isinstance(exc, RunSettingsConflict):
                outcome = "refused"
            else:
                outcome = "failed"
            # only the exception's name: its message may quote a document
            self._end_invocation(found, outcome, reason=type(exc).__name__)
            self._report(outcome, type(exc).__name__)
            if (
                isinstance(exc, UsageLimitReached)
                and exc.resumes_at is not None
            ):
                self.log(str(exc))
                return exc
            raise
        finally:
            self._close_engines()
        if failed is not None:
            outcome, reason = "failed", failed
        elif stop is not None:
            outcome, reason = "stopped", stop
        else:
            outcome, reason = "finished", None
        self._end_invocation(found, outcome, reason=reason)
        self._complete(outcome == "finished" and self._to_the_end())
        self._report(outcome, reason)
        if outcome == "stopped":
            return EXIT_STOPPED
        if outcome == "failed" or any(
            doc.state.status == FAILED for doc in found
        ):
            return EXIT_FAILED
        return EXIT_OK

    def _await_subscription(self, pause: Any) -> bool:
        """Wait until the pass can go on after a subscription limit, and a
        moment past it, while honouring the control file: False when an
        abort of the run ends the wait. A pause of the run keeps it
        waiting until a resume, past that time if need be."""
        resumes = max(
            pause.resumes_at + timedelta(seconds=RESUME_MARGIN),
            self._clock() + timedelta(seconds=self.poll),
        )
        self.log(
            f"waiting until {resumes:%Y-%m-%d %H:%M:%S} UTC, then invoking "
            "the run again (`yizkr scratch abort` or Ctrl-C stops the wait)"
        )
        announced: set[str] = set()
        while True:
            whole = RunDir(self.state_dir).control().stopped()
            if whole is not None and whole.endswith("aborted"):
                self.log(f"not invoked again ({whole})")
                return False
            if whole is None and self._clock() >= resumes:
                return True
            if whole is not None:
                self._await_resume({whole}, announced)
            else:
                left = (resumes - self._clock()).total_seconds()
                self._sleep(max(0.0, min(self.poll, left)))

    def run(self) -> int:
        """Run the scratch run: one invocation, and another after each
        subscription limit that paused it, with the slices the control
        file stopped still stopped. Returns the exit status (EXIT_*)."""
        carried: list[dict] = []
        while True:
            result = self._invocation(carried)
            if isinstance(result, int):
                return result
            if not self._await_subscription(result):
                return EXIT_STOPPED
            carried = self._stops_in_force()


def scratch_run(
    folder: Path,
    *,
    runs: Path = DEFAULT_RUNS,
    resume: str | None = None,
    new: bool = False,
    driver: type[ScratchDriver] = ScratchDriver,
    **options: Any,
) -> int:
    """`yizkr scratch run`: hold the runs directory, take the run the
    command names (choose_run), and run it (ScratchDriver). Returns the
    exit status."""
    with hold(runs):
        run_dir = choose_run(runs, resume=resume, new=new)
        return driver(folder, run_dir, **options).run()
