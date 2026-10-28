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
.ged file. An independent image source a PDF places on any page, or a
deck paints on a slide, read on its own, is a document of its own, an
excerpt (orchestrate.ExcerptRun), which takes the same stages once its
container's transcription has found it, and joins the run plan then.
Nothing before the barrier reads or writes the wiki: no artifact is
registered before the register stage, and a deck's or a PDF's image
sources are recorded in its directory (`container-inventory.json`;
ADR-0025 §2). There is no
human stop. When every document's per-document stages have ended, done
or failed, the run is at the **barrier**, and the driver runs the corpus
stages registered with it (CORPUS_STAGES), in order, over the documents
whose stages all completed, excerpts among them: every one, in each
invocation that goes past the barrier, so a corpus stage reuses its own
outputs while their inputs stand, as the views do. They are clustering,
which makes the identity map and adjudicates its ambiguous pairs
(yizkr.pipeline.adjudication), and then the write side
(yizkr.pipeline.scratchwrite): reset, register, write, places and site,
which touch the wiki. Of them clustering's adjudication and places, the
place-hierarchy reconciliation after the write, call the engine, through
the LLM cache like every stage, so their calls pause for a subscription
limit as any call does. Face detection is never a stage of a scratch
run, and no scratch run writes a depicts statement.

**Order.** One document of each kind, the first by name, goes through
every per-document stage first: the canaries. The rest follow stage by
stage, every transcription before any extraction and every extraction
before any critique, a document's own stages always in order. So a
misalignment of any kind shows within the first calls, and each stage's
rate is known early. An excerpt joins as its container's transcription
ends: the invocation's first is the excerpts' canary, which goes through
its stages next, and the others take their stages rank by rank, after
the documents in each rank. `until` stops the run after a stage.

**Control.** Before each stage of each document, and before each corpus
stage, the driver reads the run's control file (rundir.fold_control): a
pause of the whole run, a stage or a document kind holds dispatch for its
slice until a resume, and the driver waits while nothing else can go on;
an abort holds its slice for the rest of the invocation, and outranks a
pause of another scope that also covers a stage, since no resume can let
that stage through. The monitor, the anomaly checks and `yizkr scratch
pause|resume|abort` write those requests. A stage in flight when a
request arrives finishes. Ctrl-C stops the invocation at once, and the
engine's calls in flight with it. Every completed call is in the cache
and every completed view in its directory, so invoking the run again
resumes it with nothing completed made again: a view that stands is
reused, and so is one that a refresh of its role (`--refresh`) has made
again in the run already.

**The engine's pauses.** A subscription limit pauses the pass with the
time it can go on (llm.UsageLimitReached). The invocation ends `paused`,
the driver waits until a margin past then, never less than the margin,
while honouring the control file (an abort of the run ends the wait),
and invokes the run again, with every request in force at the new
invocation's start written again after it. The waits and the requests
carried count only what was asked since this driver began: an earlier
`scratch run`'s requests are not its own, even before its first
invocation's start marker has cleared them, as while its preflight
waits. Any other pause (a claude CLI that changed, a cache another
process holds, a call that ran on usage credits, a model the CLI does
not support) ends the driver, which says what to do.

**The manifest.** A scratch run keeps its settings (rundir: fixed
settings): an invocation that asks for others is refused. The one
exception is the code, once every document's per-document stages have
ended, or one of them has failed: an invocation on other code, one that
registers a corpus stage say, goes past the barrier alone, dispatching
no per-document stage, and records its own code beside the run's.
Besides the settings, run.json records the engine's pacing and the
invocation's `scratch` facts: how far it was asked to go, the canaries,
the corpus stages and whether it went to the end.

**Before any call** the driver refuses to start without a never-ingest
list, and runs its preflight: the engine's CLI and its login, a full
model id for every role, the engine's pacing settings, anomaly
thresholds the checks can load (`$YIZKR_ANOMALIES`), free disk space,
the PDF renderer when the corpus holds a PDF, and, only when a stage
that touches the wiki is to run, a reachable stack and an instance that
carries no kept-state marker. Once those pass and the settings match
those a resumed run recorded, it refuses an LLM cache folder the cache
would refuse to open, unless the invocation calls no engine; and before
any document is dispatched or anything written, it makes one trivial
call per distinct (model, effort) pair the roles this invocation will
call run, through the engine as the run calls it but not through the
cache, and refuses the run, naming the model and the engine's reason,
when one fails with an error that is not a subscription limit or an
overload: a model the claude CLI does not support among them. The pairs
are those of the roles whose stages fall within the invocation's range
(`--until`), and every role when a corpus stage in range may call the
engine; it reads no run state, so a resume makes them though it reuses
every view, and an invocation past the barrier only, whose corpus stages
call no engine, makes none. On a claude CLI other than the run recorded
it checks no model and refuses nothing: the run's own client refuses the
CLI when a stage starts the engine. One driver at a time uses a runs
directory (hold), and a run is invoked only from the runs directory it
is in.

Run directories hold transcriptions, claims and the cache's raw model
output: they are local-only data, kept under var/ (docs/AGENT_PROTOCOL.md
§6). So the runs directory is private to its owner, and never one that
a git work tree would track.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yizkr.core.corpus import MISSING_LIST, ExclusionConfigError

# the CLI's exit statuses (yizkr.core.exits), which the scratch commands
# return
from yizkr.core.exits import EXIT_FAILED as EXIT_FAILED
from yizkr.core.exits import EXIT_INTERRUPTED as EXIT_INTERRUPTED
from yizkr.core.exits import EXIT_OK as EXIT_OK
from yizkr.core.exits import EXIT_STOPPED as EXIT_STOPPED
from yizkr.pipeline.orchestrate import (
    BATCHED,
    DUPLICATE,
    FAILED,
    MODEL_VARIABLE,
    PAUSED,
    PDF,
    DocumentRun,
    ExcerptRun,
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
    ControlState,
    DispatchStopped,
    ModelAliasRefused,
    RunDir,
    RunSettingsConflict,
    code_version,
    exclusions_record,
    fold_control,
    run_state,
    settings_conflicts,
    summarise,
)
from yizkr.pipeline.rundir import FAILED as STAGE_FAILED
from yizkr.pipeline.stage_planning import (
    DOCUMENT_STAGES,
    MODEL_ROLES,
    PHASES,
    STAGE_ROLES,
    batch_stage,
    stages_for,
)
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
# the event that says a document's stage was made again under a refresh of
# its role, which the run's later invocations reuse
REFRESHED = "refreshed"
# a document whose transcription is its last stage done so far
TRANSCRIBED = "transcribed"
# the rank of each per-document stage: the rest of the documents take them
# rank by rank, after the canaries; the barrier and the corpus stages come
# after them
_BARRIER_RANK = max(PHASES.values()) + 1
# a stage's states that end it: done, needing attention, or failed
_ENDED = frozenset({DONE, NEEDS_ATTENTION, STAGE_FAILED})
_COMPLETED = frozenset({DONE, NEEDS_ATTENTION})
# what a run's state is read with, until the run report takes its place
REPORT_COMMAND = "yizkr stages state"
# how often the driver reads the control file while nothing can go on
POLL_SECONDS = 5.0
# what the preflight asks each model the run's roles name: a call that
# reads no file and needs no thought, so that it costs next to nothing
PREFLIGHT_PROMPT = "Reply with the single word: ready"
# the free space a run needs on the filesystems of its run directory and
# of the LLM cache
MIN_FREE_BYTES = 2 * 1024**3
# how long after a subscription limit's reported time the driver invokes
# the run again, and after a pause whose time has passed already: the
# engine can hold its pause past that time
RESUME_MARGIN = 60.0
# `--resume` with no run named: the latest run
LATEST = "latest"


# what a stage of a document came to in the dispatch loop: it ended (done
# or reused), the control file held it as it started, or it failed
_ENDED_HERE = "ended"
_HELD = "held"
_FAILED_HERE = "failed"


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def stop_of(
    control: ControlState,
    *,
    stage: str | None = None,
    kind: str | None = None,
) -> str | None:
    """What stops a stage, of a kind, as '<scope> paused|aborted': the
    run's scope, the stage's or the kind's, an abort before a pause, since
    a stage an abort covers stays stopped whatever else is resumed; among
    equals, the run's scope, then the stage's, then the kind's. None when
    nothing does."""
    scopes = ["run"]
    if stage:
        scopes.append(f"stage:{stage}")
    if kind:
        scopes.append(f"kind:{kind}")
    stops = [
        f"{scope} {control.scopes[scope]}"
        for scope in scopes
        if scope in control.scopes
    ]
    aborts = [stop for stop in stops if stop.endswith(" aborted")]
    return (aborts or stops or [None])[0]


# -- corpus stages ------------------------------------------------------------


@dataclass(frozen=True)
class CorpusContext:
    """What a corpus stage is given: the run directory (`run`: its
    manifest, events and control file), the documents whose per-document
    stages all completed, with their views in their directories, and a
    directory of its own in the run directory (`directory`, which the
    stage makes when it writes there), the connection settings' env file,
    the driver's log, and the driver itself, whose wiki client and whose
    registration of a document at its write a stage that writes uses."""

    run: RunDir
    documents: tuple[DocumentRun, ...]
    directory: Path
    env_file: Path | None
    log: Callable[..., None]
    driver: Any = None


@dataclass(frozen=True)
class CorpusStage:
    """A stage the driver runs over the whole corpus, after the barrier:
    once in every invocation that goes past the barrier, a later one
    after a subscription limit or a resume included. So a stage keeps its
    outputs in its directory and reuses them while their inputs stand, as
    the documents' views are reused; the driver keeps no record of it
    between invocations. `run` takes the stage's CorpusContext and returns
    the reasons its output needs attention, if any: labels that never
    quote a document, since they reach the run's events. An exception
    fails the stage, and the stages after it wait for the next invocation.

    `touches_wiki` marks a stage that reads or writes the wiki: the driver
    runs one only when the stack is reachable and the instance carries no
    kept-state marker, which it checks before the run starts and again
    before the stage. `calls_engine` marks a stage that may call the
    engine; the call estimate (yizkr.pipeline.estimate) says a stage that
    does not makes no call."""

    name: str
    run: Callable[[CorpusContext], Sequence[str] | None]
    touches_wiki: bool = False
    calls_engine: bool = True


def _clustering(context: CorpusContext) -> Sequence[str]:
    """Corpus clustering (yizkr.pipeline.clustering) and the adjudication
    of its ambiguous pairs (yizkr.pipeline.adjudication): the identity map
    of the corpus, in the stage's directory. The module is imported as the
    stage runs, since only a run past the barrier needs it."""
    from yizkr.pipeline.adjudication import run_stage

    return run_stage(context)


def _write_side(name: str) -> Callable[[CorpusContext], Sequence[str]]:
    """The stage `name` of the write side (yizkr.pipeline.scratchwrite):
    reset, register, write, places or site. The module is imported as the
    stage runs, since only a run past the barrier needs it."""

    def run(context: CorpusContext) -> Sequence[str]:
        from yizkr.pipeline import scratchwrite

        return getattr(scratchwrite, f"run_{name}")(context)

    run.__name__ = f"_{name}"
    return run


# the corpus stages, in the order they run after the barrier: clustering,
# which calls the engine to adjudicate its ambiguous pairs, then the write
# side, which touches the wiki; of it only places, the place-hierarchy
# reconciliation, calls the engine
CORPUS_STAGES: tuple[CorpusStage, ...] = (
    CorpusStage("clustering", _clustering),
    *(
        CorpusStage(
            name,
            _write_side(name),
            touches_wiki=True,
            calls_engine=name == "places",
        )
        for name in ("reset", "register", "write", "places", "site")
    ),
)


def _instance_is_kept(env_file: Path | None) -> bool:
    """Whether the instance the env file names carries the kept-state
    marker, on the host or on the instance (yizkr.core.instance)."""
    from yizkr.core.instance import instance_is_kept

    return instance_is_kept(env_file)


# whether the instance carries the kept-state marker (ADR-0025 §6): a
# callable of the env file. A stage that touches the wiki is refused when
# it says so, when it cannot tell, and when it is None
KEPT_STATE: Callable[[Path | None], bool] | None = _instance_is_kept


# -- refusals -----------------------------------------------------------------


class ScratchRefused(RuntimeError):
    """The driver will not start, or go on: what the message names has to
    change first. Nothing was dispatched for it."""


class PreflightFailed(ScratchRefused):
    """The host or the configuration cannot serve a scratch run: every
    problem the preflight found, before anything was written or any
    document dispatched."""

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
    except (OSError, ValueError, RecursionError):
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


def _ask_git(
    where: Path, *arguments: str
) -> subprocess.CompletedProcess | None:
    """git's answer to a command run in `where`, or None when git cannot
    be run."""
    try:
        return subprocess.run(
            ["git", "-C", str(where), *arguments],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def tracked_by_git(path: Path) -> Path | None:
    """The top of the git work tree that holds the directory `path`, when
    git there does not ignore it and so would track what is put in it;
    None when no work tree holds it, git ignores it, or there is no git to
    ask. The path need not exist yet: git matches its ignore rules against
    the name, as a directory's."""
    path = Path(os.path.realpath(path))
    probe = path
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    top = _ask_git(probe, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return None
    tree = Path(top.stdout.strip())
    ignored = _ask_git(tree, "check-ignore", "-q", f"{path}/")
    if ignored is not None and ignored.returncode == 0:
        return None
    return tree


def runs_refusal(runs: Path) -> str | None:
    """Why `runs` cannot be the runs directory, as hold refuses it, found
    without making or changing anything; None when it can be.

    Its runs hold corpus content, so it must be private (a runs directory
    that exists and is open to other users is refused, and its mode left
    alone) and never one a git work tree would track. And hold must be
    able to open its lock file as hold opens it: a lock file there
    already, to read and write, in a directory it can pass through, and
    otherwise a directory it can make one in, or can make. A path that is
    a file, or a link to nothing, or lies under a file, is refused."""
    runs = Path(runs)
    tree = tracked_by_git(runs)
    if tree is not None:
        return (
            f"{runs} is in the git work tree {tree}, which does not ignore "
            "it: a run directory holds corpus content, which is never "
            "committed (docs/AGENT_PROTOCOL.md §6). Name a runs directory "
            "git ignores, such as var/scratch, or one outside the work tree."
        )
    try:
        if os.path.lexists(runs):
            if not runs.is_dir():
                return (
                    f"{runs} cannot be the runs directory: it is not a "
                    "directory"
                )
            mode = stat.S_IMODE(runs.stat().st_mode)
            if mode & 0o077:
                return (
                    f"the runs directory {runs} is open to other users "
                    f"(mode {mode:o}), and its runs hold corpus content: "
                    "make it private (chmod 700), or name another"
                )
            lock = runs / LOCK_FILE
            if os.path.lexists(lock):
                # hold opens the lock file there, and makes nothing else
                if not (
                    lock.is_file()
                    and os.access(runs, os.X_OK)
                    and os.access(lock, os.R_OK | os.W_OK)
                ):
                    return (
                        f"{runs} cannot be the runs directory: this user "
                        f"cannot open its lock file {lock} to read and "
                        "write it"
                    )
                return None
            folder = runs
        else:
            # the nearest folder that exists, which the runs directory is
            # to be made in
            folder = Path(os.path.abspath(runs)).parent
            while not os.path.lexists(folder) and folder != folder.parent:
                folder = folder.parent
            if not folder.is_dir():
                return (
                    f"{runs} cannot be the runs directory: {folder} is not "
                    "a directory"
                )
        if not os.access(folder, os.W_OK | os.X_OK):
            return (
                f"{runs} cannot be the runs directory: {folder} cannot be "
                "written"
            )
    except OSError as exc:
        return f"{runs} cannot be the runs directory: {exc}"
    return None


@contextlib.contextmanager
def hold(runs: Path) -> Iterator[None]:
    """Hold the runs directory for as long as the block runs: one driver
    at a time uses it, and so runs one scratch run at a time. The lock is
    flock's on its LOCK_FILE, which the kernel releases however the
    holder ends; the holder records itself there, and empties the record
    as it lets go. Another holder is refused at once, by name
    (ScratchRunInUse).

    Its runs hold corpus content, so the runs directory is private: made
    so (mode 700) when it is made here. A runs directory runs_refusal
    refuses is refused (ScratchRefused), and so is one that cannot be
    made, read or written when it comes to it."""
    runs = Path(runs)
    refusal = runs_refusal(runs)
    if refusal is not None:
        raise ScratchRefused(refusal)
    try:
        if not runs.exists():
            runs.mkdir(parents=True, mode=0o700)
            os.chmod(runs, 0o700)
        fd = os.open(runs / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ScratchRefused(
            f"{runs} cannot be the runs directory: {exc}"
        ) from None
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


def runs_holder(runs: Path) -> str | None:
    """The driver that holds the runs directory now, described, as the
    record in its LOCK_FILE says (hold), read without taking the lock;
    None when the record names no running process, or there is none or
    none that reads (nested too deep to decode included). A process on
    another host is taken to run, since this one cannot see
    it."""
    try:
        record = json.loads((Path(runs) / LOCK_FILE).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    pid, host = record.get("pid"), record.get("host")
    if host == socket.gethostname():
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
    held = f"process {pid}"
    if host != socket.gethostname():
        held += f" on {host}"
    if record.get("command"):
        held += f" ({record['command']})"
    if record.get("since"):
        held += f", since {record['since']}"
    return held


def _run_id(moment: datetime, step: int) -> str:
    # the year in four digits, which %Y gives only from 1000 on
    return f"{moment.year:04d}{moment:%m%dT%H%M%SZ}-{step:06x}"


def _run_id_after(name: str, runs: Path) -> str | None:
    """The first run id that sorts after `name`, an entry in `runs`, when
    `name` is a run id (new_run_id): the next microsecond's, or the next
    second's first; None for any other name. ScratchRefused, naming the
    entry, when no run id sorts after it: the last microsecond of the
    year 9999, or a name past it in that second."""
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", name):
        return None
    try:
        second = datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    step = int(name[17:], 16) + 1
    if step < 1_000_000:
        return _run_id(second, step)
    try:
        return _run_id(second + timedelta(seconds=1), 0)
    except OverflowError:
        raise ScratchRefused(
            f"{runs / name} is named like a run id that no other sorts "
            "after, so no new run in that directory could sort after it: "
            "move it out of the runs directory"
        ) from None


def new_run_id(
    runs: Path, *, clock: Callable[[], datetime] | None = None
) -> str:
    """A new run's id, the name of its directory in `runs`: the UTC time
    `clock` reads (the system's by default), to the second, then the
    microsecond in six hex digits. It sorts after every run id a
    directory entry in `runs` has, run or not: when the clock reads at or
    before the latest of them (a second id in one microsecond, a clock
    set back), the id is the first after that latest, so that the runs in
    a runs directory sort as they were started, however the clock moves.
    One driver at a time takes a new run there (hold), so the id is not
    taken by another before its directory is made. ScratchRefused when
    an entry's name is a run id that no run id sorts after
    (_run_id_after)."""
    now = (clock or (lambda: datetime.now(UTC)))().astimezone(UTC)
    run_id = _run_id(now, now.microsecond)
    runs = Path(runs)
    if runs.is_dir():
        after = (_run_id_after(path.name, runs) for path in runs.iterdir())
        first = max((name for name in after if name), default=None)
        if first is not None and first > run_id:
            run_id = first
    return run_id


def runs_in(runs: Path) -> list[Path]:
    """The run directories in `runs` (each with its run.json), oldest
    first: by name, which for run ids is the order the runs were started
    in (new_run_id)."""
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
    runs: Path,
    *,
    resume: str | None = None,
    new: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """The run directory a `scratch run` invocation takes, in `runs`: a
    new one (`new`); the run `resume` names (LATEST, the latest), which
    must be a scratch run in `runs`, the directory whose hold (hold)
    keeps a second driver off it; otherwise the latest run when it has
    not been taken to the end, so that running the same command again
    resumes it, and a new one when it has. A new one's id is read from
    `clock` (new_run_id)."""
    runs = Path(runs)
    if new:
        return runs / new_run_id(runs, clock=clock)
    if resume is not None:
        path = resolve_run(runs, resume)
        if os.path.realpath(path.parent) != os.path.realpath(runs):
            raise ScratchRefused(
                f"{path} is not in the runs directory {runs}: a run is "
                "invoked from its own, which one driver at a time holds. "
                f"Name it: --runs {path.parent} --resume {path.name}"
            )
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
    return runs / new_run_id(runs, clock=clock)


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

    # the command an artifact's description says ingested it
    command = "yizkr scratch run"

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
        # None keeps helpers usable before discovery; each invocation binds
        # preflight to the kinds actually present before checking the host.
        self._planned_kinds: set[str] | None = None
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
        # (document SHA-1, stage) whose current view a refresh of the
        # stage's role made in this run, read from its events at the
        # start of each invocation: it stands for the rest of the run
        self._refreshed: set[tuple[str, str]] = set()
        # (document, stage, role) of each view the stage in hand made
        # under a refresh, recorded once it is saved (_dispatch_one)
        self._refreshing: list[tuple[DocumentRun, str, str]] = []
        # the SHA-1s of the corpus's documents, which no excerpt is taken
        # for (_excerpts_of), and the excerpts dispatched in this
        # invocation, by their SHA-1s
        self._corpus_sha1s: frozenset[str] = frozenset()
        self._queued: set[str] = set()
        # (id of a document, stage) -> where the pair stands in dispatch
        # order (_stages, _arrivals), and the next place for an excerpt
        self._slots: dict[tuple[int, str], tuple] = {}
        self._next_order = 0
        # how many requests the control file held as this driver began
        # (run): every wait and carry reads only what was asked since,
        # never an earlier `scratch run`'s requests, which no start marker
        # has cleared before this one's first invocation starts
        self._asked_since = 0

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

    def _plan_documents(self, docs: Sequence[DocumentRun]) -> None:
        self._planned_kinds = {
            doc.kind for doc in docs if doc.unreadable is None
        }

    def _preflight_roles(self, past: bool = False) -> tuple[str, ...]:
        """Model roles in the declared document and corpus stages in range."""
        kinds = (
            DOCUMENT_STAGES
            if self._planned_kinds is None
            else self._planned_kinds
        )
        roles = {
            stage.role
            for kind in kinds
            for stage in stages_for(kind)
            if not past
            and stage.process != "deterministic"
            and self._within(stage.name)
        }
        if any(stage.calls_engine for stage in self._corpus_in_range()):
            roles.update(MODEL_ROLES)
        return tuple(role for role in MODEL_ROLES if role in roles)

    def _pacing(self) -> dict[str, float] | None:
        """The engine's pacing (docs/LLM_CACHE.md, Subscription limits):
        the share of each usage window left to development sessions and
        the longest a call waits for the subscription. ValueError for a
        setting the engine would refuse."""
        if not self._preflight_roles() or self.llm != "claude-code":
            return None
        from yizkr.pipeline.llm import _max_wait_setting, _reserve_setting

        return {
            "reserve": _reserve_setting(None),
            "max_wait": _max_wait_setting(None),
        }

    def _engine_problems(self) -> list[str]:
        if not self._preflight_roles():
            return []
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
        paths = [("the run directory", self.state_dir)]
        if self._preflight_roles():
            paths.append(("the LLM cache", self.cache_dir))
        for what, path in paths:
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

    def _model_problems(self) -> list[str]:
        """What is wrong with the models the roles run: a model named by
        an alias, or a role that names none. The LLM cache's keys name
        each role's model, so every one must be a full model id."""
        roles = self._preflight_roles()
        problems = []
        try:
            self._refuse_model_aliases(roles)
        except ModelAliasRefused as exc:
            problems.append(str(exc))
        unnamed = [
            role for role in roles if not self._configured_model(role)[0]
        ]
        if unnamed:
            problems.append(
                f"no model is configured for {', '.join(unnamed)}: set "
                f"${MODEL_VARIABLE} to a full model id, such as "
                "claude-opus-5-5. The engine's own default is not keyed, so "
                "the LLM cache could not tell its answers from another "
                "model's"
            )
        return problems

    def _pacing_problems(self) -> list[str]:
        """What is wrong with the engine's pacing settings: a value the
        engine would refuse as its client starts."""
        try:
            self._pacing()
        except ValueError as exc:
            return [str(exc)]
        return []

    def _anomaly_problems(self) -> list[str]:
        """What is wrong with the anomaly thresholds the run's checks
        would load (anomalies.load_config: anomalies.yaml, with the file
        `$YIZKR_ANOMALIES` names over it): a file that cannot be read, a
        key the defaults do not have, or a value out of range."""
        from yizkr.pipeline import anomalies

        try:
            anomalies.load_config()
        except anomalies.AnomalyConfigError as exc:
            return [str(exc)]
        return []

    def _cache_problems(self, past: bool) -> list[str]:
        """Why the LLM cache's folder cannot hold the cache, as opening it
        would refuse it, found without making or changing anything
        (llm.read_cache, which refuses it as a pause, CacheRefused): for
        an invocation whose range calls the engine (_preflight_pairs).
        One past the barrier only, whose corpus stages call no engine,
        never opens the cache, and nothing is refused for it."""
        from yizkr.pipeline import llm

        if not self._preflight_pairs(past):
            return []
        try:
            llm.read_cache(self.cache_dir)
        except llm.PassPaused as exc:
            return [str(exc)]
        return []

    def _preflight_host(self) -> None:
        """Check host resources the discovered stage plans need, including
        the engine and cache only when a stage can use them. Also check
        anomaly settings, run disk space and any required wiki boundary.
        PreflightFailed lists every problem."""
        problems = self._model_problems() + self._pacing_problems()
        problems += self._anomaly_problems()
        problems += self._engine_problems()
        problems += self._disk_problems()
        problems += self._wiki_problems()
        if problems:
            raise PreflightFailed(problems)

    def _preflight_pairs(self, past: bool) -> list[tuple[str, str | None]]:
        """(role, effort) for each distinct (model, effort) pair the roles
        of this invocation's range run, in MODEL_ROLES order, each the
        first role that runs it: the roles of the per-document stages
        within `--until`, none for an invocation past the barrier only,
        and every role when a corpus stage within the range may call the
        engine (CorpusStage.calls_engine), since none says which. It reads
        no run state: a stage whose view the invocation will reuse counts
        all the same."""
        roles = self._preflight_roles(past)
        pairs: dict[tuple[str | None, str | None], str] = {}
        for role in MODEL_ROLES:
            if role in roles:
                effort, _model = self._engine_settings(role)
                model = self._configured_model(role)[0]
                pairs.setdefault((model, effort), role)
        return [(role, effort) for (_m, effort), role in pairs.items()]

    def _preflight_models(self, past: bool) -> None:
        """One trivial call (PREFLIGHT_PROMPT) per distinct (model, effort)
        pair the roles this invocation calls run (_preflight_pairs), through
        a client the engine makes as the run's for the first role that runs
        the pair (its engine, model and effort), and never through the LLM
        cache, whose answer would be no call. When the claude CLI installed
        is not the one the run recorded, no pair is called and nothing
        refused: the run's own client refuses the CLI as another setting
        when a stage starts the engine (rundir.record_engine), and a resume
        with nothing left for the engine goes on. So does a client that
        cannot start for a pause the process has latched (llm.PassPaused
        other than a subscription limit, such as a CLI updated while the
        driver waited): no pair is called, and the run's own client start
        meets the pause again within the invocation, which records it.
        PreflightFailed names each pair whose call fails with an error that
        is not a subscription limit or an overload, with the engine's
        reason, a model the CLI does not support (llm.ModelUnsupported)
        among them; the pause that refusal latches holds every later call of
        the process, so the pairs after it are named as not tried. The
        claude-code engine waits out a limit or an overload, or pauses the
        pass on it (UsageLimitReached), as for any call; an API error with a
        limit's status (429, 529), which the anthropic engine raises once
        its own retries are spent, refuses the run with a request to try
        again. Any other pause stops the run as it would a document's
        call."""
        from yizkr.pipeline.llm import (
            LIMIT_STATUSES,
            ModelUnsupported,
            PassPaused,
            UsageLimitReached,
            make_client,
        )

        pinned = (self.rundir.manifest().get("engine") or {}).get(
            "cli_version"
        )
        problems: list[str] = []
        pairs = self._preflight_pairs(past)
        for index, (role, effort) in enumerate(pairs):
            model = self._configured_model(role)[0]
            what = f"{model} ({role}, effort {effort or 'unset'})"
            try:
                try:
                    engine = make_client(
                        self.llm,
                        effort=effort,
                        model=self._engine_settings(role)[1],
                    )
                except PassPaused as exc:
                    if isinstance(exc, UsageLimitReached):
                        raise
                    # a pause the process has latched (another CLI since
                    # the driver began, a relative HOME, the CLI's own
                    # environment): the run's own client start meets it
                    # again, within the invocation, which records it
                    self.log(
                        "preflight: the engine's client cannot start "
                        f"({type(exc).__name__}), so no model is checked"
                    )
                    break
                try:
                    cli_version = getattr(engine, "cli_version", None)
                    if pinned and cli_version and cli_version != pinned:
                        self.log(
                            f"preflight: the claude CLI is {cli_version}, "
                            f"not the run's {pinned}, so no model is "
                            "checked: a stage that starts the engine "
                            "refuses the CLI as another setting"
                        )
                        break
                    engine.complete(PREFLIGHT_PROMPT)
                finally:
                    close = getattr(engine, "close", None)
                    if callable(close):
                        close()
            except ModelUnsupported as exc:
                problems.append(
                    f"the engine cannot run {what}: {exc.reason or exc}"
                )
                untried = [
                    f"{self._configured_model(later)[0]} ({later})"
                    for later, _effort in pairs[index + 1 :]
                ]
                if untried:
                    problems.append(
                        "not tried, since that pause holds every later "
                        f"call: {', '.join(untried)}"
                    )
                break
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status in LIMIT_STATUSES:
                    problems.append(
                        f"the API is "
                        f"{'overloaded' if status == 529 else 'rate-limited'}"
                        f" for {what}: try again later"
                    )
                    continue
                problems.append(
                    f"a trivial call to {what} failed: {_why(exc)}"
                )
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
                doc.source for doc in docs if doc.kind == PDF
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
        but for its code, and every document has ended its per-document
        stages, done or failed (a failed stage leaves the stages after it
        undispatched): then it goes past the barrier alone."""
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

        def ended(doc: DocumentRun) -> bool:
            found = [
                states.get((doc.sha1, s)) for s in DOCUMENT_STAGES[doc.kind]
            ]
            statuses = {state.status for state in found if state is not None}
            return STAGE_FAILED in statuses or (
                None not in found and statuses <= _ENDED
            )

        return all(ended(doc) for doc in docs)

    def _complete(self, complete: bool) -> None:
        """Record whether the invocation went through every stage."""
        scratch = dict(self.rundir.manifest().get("scratch") or {})
        scratch["complete"] = complete
        self.rundir.update_manifest(scratch=scratch)

    # -- control ------------------------------------------------------------

    def _stops_in_force(
        self, before: str | None = None, since: int | None = None
    ) -> list[dict]:
        """The requests that stop the run or a slice (a stage or a kind),
        as the control file holds them since a start marker: the abort
        that holds a scope, or the pause that does. Read as
        rundir.fold_control reads the file. With `before`, an invocation's
        id, the requests in force just before that invocation's start
        marker, since the one before it; otherwise those in force now.
        With `since`, a count of the file's requests, only the requests
        after that many count (_requests_since)."""
        held: dict[str, dict] = {}
        for request in self._requests_since(since or 0):
            action = request.get("action")
            if action == "start":
                if before is not None and request.get("invocation") == before:
                    break
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
        # a slice's scope that names no stage or kind stops nothing
        return [
            request
            for scope, request in held.items()
            if scope == "run"
            or (
                scope.startswith(("stage:", "kind:"))
                and scope.partition(":")[2]
            )
        ]

    def _carry(self) -> None:
        """Keep in force, in this invocation, what the control file held
        when it started: the requests in force just before its start
        marker, which voids them, written again after it, with who asked
        and why, and marked carried. So a request written while the driver
        waited, or while this invocation was getting ready, is honoured.
        Only the requests written since this driver began count
        (_asked_since): an earlier `scratch run`'s are not its to honour
        again."""
        for request in self._stops_in_force(
            before=self.rundir.invocation, since=self._asked_since
        ):
            which, _, name = (request.get("scope") or "run").partition(":")
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
        the directory's record comes first; and a presentation's common
        source inventory is published before reading."""
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
        if doc.kind == "presentation":
            self._record_container_inventory(doc)
        self._prepared.add(doc.dir)

    # -- refreshes ------------------------------------------------------------

    def _refreshes_made(self) -> set[tuple[str, str]]:
        """(document SHA-1, stage) whose current view a refresh made in
        this run, of a role this invocation refreshes too: the stage's
        latest `done` event is followed by a `refreshed` one."""
        if not self.refresh:
            return set()
        made: dict[tuple[str, str], str | None] = {}
        for event in self.rundir.events():
            name = event.get("event")
            key = (event.get("subject"), event.get("stage"))
            if name == "done":
                made[key] = None
            elif name == REFRESHED:
                made[key] = event.get("role")
        return {key for key, role in made.items() if role in self.refresh}

    def _run_stage(self, doc, stage, compute, attention=None):
        """The orchestrator's stage, noting a view it makes under a
        refresh of its role. The view is written and recorded after the
        stage, so the refresh is recorded only once it is saved
        (_dispatch_one): one whose view was never saved is made again."""
        result = super()._run_stage(doc, stage, compute, attention)
        role = STAGE_ROLES.get(stage)
        if role in self.refresh:
            self._refreshing.append((doc, stage, role))
        return result

    def _record_refreshes(self) -> None:
        """Record each view the stage in hand made under a refresh, now
        saved: a `refreshed` event, so that the run's later invocations
        reuse it rather than make it again."""
        for doc, stage, role in self._refreshing:
            self._refreshed.add((doc.sha1, stage))
            self.rundir.emit(
                REFRESHED,
                subject=doc.sha1,
                kind=doc.kind,
                stage=stage,
                role=role,
            )
        self._refreshing = []

    def _refresh_requested(
        self, doc: DocumentRun, role: str, stage: str
    ) -> bool:
        """Refresh a requested role only until its view is saved in this run.

        A successfully refreshed view stands across later invocations;
        deciding whether to reuse it does not change the run's settings.
        """
        refreshed = (doc.sha1, stage) in self._refreshed
        return super()._refresh_requested(doc, role, stage) and not refreshed

    def _run_stage_of(self, doc: DocumentRun, stage: str) -> None:
        """One per-document stage of one document, through the
        orchestrator's views: made, or reused when it stands."""
        self._prepare(doc)
        if stage == "transcription":
            self._transcription(doc)
            doc.state.status = TRANSCRIBED
        elif stage == batch_stage(doc.kind).name:
            self._batches[doc.sha1] = self._derive(doc)
            doc.state.status = BATCHED
        else:
            self._critique(doc, self._batches[doc.sha1])

    def _dispatch_one(self, doc: DocumentRun, stage: str) -> str:
        """Run a stage of a document, and fail only the document when it
        fails: _ENDED_HERE, _HELD (the control file held it as it started)
        or _FAILED_HERE. A pause of the pass or Ctrl-C goes on through,
        with the state the document reached saved where it can be. A view
        the stage made under a refresh is recorded as refreshed only once
        the stage has ended with its view and the document's state
        saved."""
        self.log(f"{stage:13} {doc.source.name}")
        self._refreshing = []
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
        self._record_refreshes()
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

    def _planned(self, doc: DocumentRun) -> list[str]:
        """The document's stages this invocation is to take."""
        return [s for s in DOCUMENT_STAGES[doc.kind] if self._within(s)]

    def _stages(
        self, docs: Sequence[DocumentRun], canaries: Sequence[DocumentRun]
    ) -> list[tuple[DocumentRun, str]]:
        """Every (document, stage) the invocation is to take, in the order
        it takes them: each canary through its stages, then the rest by
        the stages' rank, each rank in the documents' order. Each pair's
        place is kept (`_slots`), for the excerpts that join them
        (`_arrivals`)."""
        self._slots = {}
        for index, doc in enumerate(canaries):
            for step, stage in enumerate(self._planned(doc)):
                self._slots[(id(doc), stage)] = (0, index, step)
        for index, doc in enumerate(docs):
            if doc not in canaries:
                for stage in self._planned(doc):
                    self._slots[(id(doc), stage)] = (1, PHASES[stage], index)
        self._next_order = len(docs)
        pairs = [(doc, stage) for doc in docs for stage in self._planned(doc)]
        return sorted(pairs, key=self._slot)

    def _slot(self, pair: tuple[DocumentRun, str]) -> tuple:
        return self._slots[(id(pair[0]), pair[1])]

    def _arrivals(self, doc: DocumentRun) -> list[tuple[DocumentRun, str]]:
        """The (excerpt, stage) pairs that join dispatch once the
        document's transcription has ended: its excerpts that this
        invocation has not dispatched yet (_excerpts_of), added to the run
        plan. The first excerpt of the invocation is the excerpts'
        canary, recorded among the run's canaries, and goes through its
        stages next; the others take their stages rank by rank with the
        rest of the documents, after them in each rank."""
        new = [
            excerpt
            for excerpt in self._excerpts_of(doc, self._corpus_sha1s)
            if excerpt.sha1 not in self._queued
        ]
        if not new:
            return []
        self._plan_excerpts(new)
        pairs = []
        for excerpt in new:
            canary = not self._queued
            self._queued.add(excerpt.sha1)
            if canary:
                scratch = dict(self.rundir.manifest().get("scratch") or {})
                known = list(scratch.get("canaries") or [])
                if excerpt.sha1 not in known:
                    scratch["canaries"] = [*known, excerpt.sha1]
                    self.rundir.update_manifest(scratch=scratch)
            order, self._next_order = self._next_order, self._next_order + 1
            for step, stage in enumerate(self._planned(excerpt)):
                self._slots[(id(excerpt), stage)] = (
                    (0, -1, step) if canary else (1, PHASES[stage], order)
                )
                pairs.append((excerpt, stage))
        return pairs

    def _known_excerpts(self, docs: Sequence[DocumentRun]) -> list[ExcerptRun]:
        """The excerpts the documents' directories hold now, each once
        (_excerpts_of): read from files alone, as the run's plan and the
        barrier take them."""
        found: dict[str, ExcerptRun] = {}
        for doc in docs:
            for excerpt in self._excerpts_of(doc, self._corpus_sha1s):
                found.setdefault(excerpt.sha1, excerpt)
        return list(found.values())

    def _dispatch(
        self, pending: list[tuple[DocumentRun, str]]
    ) -> tuple[str | None, list[tuple[DocumentRun, str]]]:
        """Take the (document, stage) pairs in order, as the control file
        lets them through. Each round takes the first pair whose document
        has no earlier stage waiting and that nothing stops (stop_of: the
        run, its stage or its kind; an abort before a pause). A pair its
        stage's or kind's request stops is held, with a `held` event, and
        the pairs after it go on. While a pause holds a pair that nothing
        aborts, and no pair can go on, the driver waits for a resume or an
        abort; once an abort holds every pair left, no resume can let one
        through, and dispatch stops. A failed document's later stages are
        dropped. Returns (why dispatch stopped short, the pairs it left),
        or (None, []) when every pair has ended."""
        pending = list(pending)
        noted: dict[tuple[str, str], str] = {}
        announced: set[str] = set()
        failed: set[int] = set()
        while True:
            pending = [item for item in pending if id(item[0]) not in failed]
            if not pending:
                return None, []
            control = self.rundir.control()
            chosen = None
            waiting: set[str] = set()
            aborted: set[str] = set()
            behind: set[int] = set()
            for doc, stage in pending:
                if id(doc) in behind:
                    continue
                behind.add(id(doc))
                stop = stop_of(control, stage=stage, kind=doc.kind)
                if stop is None:
                    chosen = (doc, stage)
                    break
                (aborted if stop.endswith(" aborted") else waiting).add(stop)
                # a stop of the whole run holds every pair: the run's slice
                # says so, with no event per pair
                if (
                    not stop.startswith("run ")
                    and noted.get((doc.sha1, stage)) != stop
                ):
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
            if outcome == _ENDED_HERE and stage == "transcription":
                arrivals = self._arrivals(doc)
                if arrivals:
                    pending = sorted([*pending, *arrivals], key=self._slot)
            if outcome == _HELD:
                # the stage's own check held it, with its event: a request
                # landed between the two checks. A moment passes before the
                # next round, so that however the checks disagree, the
                # driver never spins on the stage
                stop = self.rundir.control().stopped(
                    stage=stage, kind=doc.kind
                )
                noted[(doc.sha1, stage)] = stop or ""
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
        run) and nothing aborts it; the abort that stops it, if one does,
        whatever is paused besides."""
        announced: set[str] = set()
        noted = None
        while True:
            control = self.rundir.control()
            stop = stop_of(control, stage=stage.name)
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
                driver=self,
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
            self.log(
                f"resume: yizkr scratch run --runs {self.state_dir.parent} "
                f"--resume {self.state_dir.name}"
            )

    def _invocation(self, carry: bool = False) -> Any:
        """One invocation of the run: preflight, the manifest, the
        per-document stages, the barrier and the corpus stages. With
        `carry`, the driver's own invocation after a subscription limit,
        the requests in force when it starts stay in force (_carry).
        Returns the exit status, or the UsageLimitReached to wait out
        before the run is invoked again."""
        from yizkr.pipeline.llm import PassPaused, UsageLimitReached

        # fail closed: no never-ingest list, nothing read
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        found = self.documents()
        self._plan_documents(found)
        self._preflight_host()
        if not found:
            self.log(f"no supported documents in {self.folder}")
            return EXIT_OK
        unread = [doc for doc in found if doc.unreadable is not None]
        docs = [doc for doc in found if doc.unreadable is None]
        self._preflight_documents(docs)
        taken, duplicates = self._canonical(docs)
        canaries = self._canaries(taken)
        # the excerpts the documents' directories hold already join the
        # plan; the others join it as their containers' transcriptions find
        # them
        self._excerpts, self._queued = {}, set()
        self._corpus_sha1s = frozenset(doc.sha1 for doc in docs)
        known = self._known_excerpts(taken)

        self.rundir = RunDir(self.state_dir)
        fields = self._fields([*docs, *known], canaries)
        past = self._past_barrier_only(fields, [*taken, *known])
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
        # settings other than the run's are refused before any call, as
        # rundir.start would refuse them, with nothing written
        recorded = self.rundir.manifest()
        if recorded.get("run_id"):
            conflicts = settings_conflicts(recorded, fields)
            if conflicts:
                raise RunSettingsConflict(self.state_dir, conflicts)
        # a cache folder the cache would refuse, before the preflight's
        # calls and before anything is written
        problems = self._cache_problems(past)
        if problems:
            raise PreflightFailed(problems)
        try:
            self._preflight_models(past)
        except UsageLimitReached as exc:
            # waited out as a limit a document's call meets is, before
            # anything is written
            if exc.resumes_at is None:
                raise
            self.log(str(exc))
            return exc
        self._views, self._derived, self._batches = {}, {}, {}
        self._prepared = set()
        self._refreshed = self._refreshes_made()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # refused here, before anything is written, when the settings differ
        self.rundir.start(fixed_settings=True, **fields)
        if carry:
            self._carry()
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
                stop, failed = self._corpus(
                    [*taken, *self._known_excerpts(taken)]
                )
        except KeyboardInterrupt:
            found = [*found, *self._excerpts.values()]
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
            found = [*found, *self._excerpts.values()]
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
        found = [*found, *self._excerpts.values()]
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
        margin past it, while honouring the control file: False when an
        abort of the run ends the wait. The margin counts from now when
        that time has passed already, as it has when the engine holds a
        pause past the time it reported, so the run is never invoked again
        sooner than the margin after a pause. A pause of the run keeps it
        waiting until a resume, past that time if need be. The wait reads
        only the requests written since this driver began (_requests_since)."""
        margin = timedelta(seconds=RESUME_MARGIN)
        resumes = max(pause.resumes_at, self._clock()) + margin
        if RunDir(self.state_dir).manifest().get("run_id"):
            stops = "`yizkr scratch abort` or Ctrl-C stops the wait"
        else:
            # the preflight met the limit before the run started: there is
            # no run yet for an abort, or for watch, to reach
            stops = (
                "Ctrl-C stops the wait: the run has not started, so "
                "`yizkr scratch abort` cannot reach it, and the wait shows "
                "only here"
            )
        self.log(
            f"waiting until {resumes:%Y-%m-%d %H:%M:%S} UTC, then invoking "
            f"the run again ({stops})"
        )
        announced: set[str] = set()
        while True:
            whole = fold_control(self._requests_since()).stopped()
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

    def _requests_since(self, since: int | None = None) -> list[dict]:
        """The control file's requests after the first `since` of them
        (_asked_since by default): all of them when the file holds fewer,
        since one replaced holds none of those counted."""
        requests = read_jsonl(self.state_dir / CONTROL)
        since = self._asked_since if since is None else since
        return requests[since:] if len(requests) >= since else requests

    def run(self) -> int:
        """Run the scratch run: one invocation, and another after each
        subscription limit that paused it, with every request in force as
        it starts still in force. Returns the exit status (EXIT_*). Only
        the requests asked since it began count in its waits and carries:
        an earlier `scratch run`'s, which no start marker has cleared yet
        while its first invocation's preflight waits, are not its own."""
        self._asked_since = len(read_jsonl(self.state_dir / CONTROL))
        carry = False
        while True:
            result = self._invocation(carry)
            if isinstance(result, int):
                return result
            if not self._await_subscription(result):
                return EXIT_STOPPED
            carry = True


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
        run_dir = choose_run(
            runs, resume=resume, new=new, clock=options.get("clock")
        )
        return driver(folder, run_dir, **options).run()
