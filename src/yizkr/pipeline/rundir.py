"""Run directory: the manifest, event log, alerts and control requests of
one run, and the run state derived from them.

    <run dir>/run.json       the run manifest: run id, what shapes the run
                             (mode, project stage, engine and CLI version,
                             each role's model, effort, epoch and client
                             configuration hash, the LLM cache, the corpus
                             and its never-ingest list, the code version),
                             the planned documents (SHA-1, kind, stages)
                             and each invocation's start, end and outcome
    <run dir>/events.jsonl   append-only events, one JSON object per line
    <run dir>/alerts.jsonl   append-only anomaly alerts
    <run dir>/control.jsonl  append-only pause/resume/abort requests

Every file is append-only JSON lines except run.json, which is replaced
atomically. Readers skip a torn line, so a reader never needs a lock. A
writer locks the log while it reads the last byte and appends, and ends
a torn last line before its own record, so a writer that dies mid-write
loses only the record it was writing wherever the lock holds
(_append_line says where it does not). Every stage start,
completion and failure is an event, and so is every view a run reuses
(yizkr.pipeline.orchestrate), every answer the LLM cache gives or cannot
give (yizkr.pipeline.llmcache) and every call that reaches the engine,
with what its stream reported of it (yizkr.pipeline.llm); the driver,
the live monitor, the anomaly checks and pacing all read and write these
files, and docs/LLM_CACHE.md and docs/RUN_MANIFEST.md are their
contract. `cache_counts` and `usage_counts` total, from the events,
what the cache did and what the engine's calls used.

The manifest records settings and hashes, never a prompt or any document
content. It describes the run as its latest invocation ran it. A run
that must keep its settings across invocations (``fixed_settings``)
refuses an invocation that asks for others (RunSettingsConflict); one
that may change them, as `ingest run` may, records the latest.

Run state per document and stage is derived from the run manifest and
the events alone (never from file modification times or a per-document
status file): a stage is pending until an event names it; running once
it has started, and interrupted once the invocation that started it is
over with no later event for the stage, which the next invocation then
starts again; done, or needs attention when its output carries
attention reasons, whether it was made in the run or reused from an
earlier one; failed, with the reason, until a later start; and pending
again, with the reason, while the control file holds it. Which
invocation is over is read from run.json and the events that open and
close invocations (Invocations), and a stage is told interrupted by one
rule (interrupted), which the live monitor reads it by too.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from yizkr import __version__
from yizkr.pipeline.stages import (
    atomic_write_text,
    canonical_json,
    read_jsonl,
    sha256_text,
)

MANIFEST = "run.json"
EVENTS = "events.jsonl"
ALERTS = "alerts.jsonl"
CONTROL = "control.jsonl"

# the version of run.json's layout (docs/RUN_MANIFEST.md); a reader that
# meets a later one knows it may find fields it does not know
MANIFEST_FORMAT = 1

# the project stage a run belongs to (ADR-0024 §7, ADR-0025 §6): runs are
# in the development stage until keeper mode records the kept state
DEVELOPMENT_STAGE = "development"
KEPT_STAGE = "kept"

# how an invocation ended: it went through its documents; the control
# file stopped it; Ctrl-C; the pass paused (llm.PassPaused); it refused
# to go on with other settings than its run's (RunSettingsConflict); or
# an error escaped it
OUTCOMES = (
    "finished",
    "stopped",
    "interrupted",
    "paused",
    "refused",
    "failed",
)

# event names
RUN_START = "run-start"
RUN_END = "run-end"
RUN_INTERRUPTED = "run-interrupted"
SET_ASIDE = "set-aside"
# a document failed, and only it: in a stage or outside its stages
DOCUMENT_FAILED = "document-failed"
# a stage's events: "reused" says its view was current and stands
STAGE_EVENTS = ("started", "done", "reused", "failed", "held")
# the LLM cache's events (yizkr.pipeline.llmcache), counted per role
CACHE_EVENTS = (
    "cache-hit",
    "cache-miss",
    "cache-resume",
    "cache-replay",
    "cache-reject",
    "cache-adopt",
)
# a call that reached the engine, with what its stream reported of it
# (yizkr.pipeline.llm.ObservedClient)
CALL = "call"

# control actions; "start" is the marker a new invocation writes, after
# which earlier requests no longer apply
PAUSE = "pause"
RESUME = "resume"
ABORT = "abort"
CONTROL_ACTIONS = (PAUSE, RESUME, ABORT)

# run states of one stage of one document
PENDING = "pending"
RUNNING = "running"
INTERRUPTED = "interrupted"
DONE = "done"
FAILED = "failed"
NEEDS_ATTENTION = "needs attention"


class DispatchStopped(Exception):
    """The run's control file paused or aborted this stage's slice."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]


# -- what shapes a run --------------------------------------------------------

# the manifest's settings: a run that keeps its settings must be invoked
# again with the same values for each
SETTINGS = (
    "mode",
    "project_stage",
    "engine",
    "roles",
    "corpus",
    "code",
    "config",
)
# settings that may still change between the invocations of such a run: a
# refresh belongs to its invocation, how many calls run at once shapes
# pacing and not what is made, and what an engine applies (its CLI
# version and its clients) is checked when the engine starts
# (RunDir.record_engine)
_NOT_FIXED = (
    "engine.cli_version",
    "roles.*.refresh",
    "roles.*.client",
    "roles.*.client.*",
    "config.refresh",
    "config.chunk_concurrency",
    "config.extract_concurrency",
)
# what a role's engine client applies, which must stay the same once
# recorded in a run that keeps its settings
_CLIENT_FIXED = ("model", "effort", "config_hash")


class RunSettingsConflict(BaseException):
    """An invocation of a run that keeps its settings asked for others
    than the ones the run recorded, or its engine applies others: a claude
    CLI updated since the run recorded its version among them.

    Like a paused pass (yizkr.pipeline.llm.PassPaused), it is no
    document's failure: it passes through the per-document isolation and
    stops the invocation, before any call is made with the other
    settings. `conflicts` lists (setting, recorded, asked for)."""

    def __init__(
        self, run: Path, conflicts: list[tuple[str, Any, Any]]
    ) -> None:
        self.conflicts = conflicts
        lines = "\n".join(
            f"  {name}: {recorded!r} recorded, {asked!r} now"
            for name, recorded, asked in conflicts
        )
        cli = next(
            (item for item in conflicts if item[0] == "engine.cli_version"),
            None,
        )
        super().__init__(
            f"{Path(run) / MANIFEST} records other settings for this run:\n"
            f"{lines}\n"
            "A run is resumed only with the settings it started with. "
            + (
                f"The claude CLI is {cli[2]}, not {cli[1]}, the version the "
                "run recorded: it was updated since, for instance by "
                "another Claude Code session's auto-update (a client's "
                "configuration hash covers the CLI version). Install the "
                "recorded version, keep auto-update off everywhere for the "
                "length of the run (DISABLE_AUTOUPDATER=1), and invoke the "
                "run again; or start a new run in a new run directory on "
                "this version: the LLM cache's keys name the CLI version, so "
                "it answers none of the new run's calls from this run's."
                if cli
                else "Invoke it again with those, or start a new run in a "
                "new run directory: the LLM cache answers every call the "
                "change leaves the same."
            )
        )


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Nested objects as dotted paths to their leaves; a list, or an empty
    object, is one leaf."""
    if isinstance(value, dict) and value:
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
        return flat
    return {prefix: value}


def _fixed_settings(manifest: Mapping[str, Any]) -> dict[str, Any]:
    flat = _flatten({name: manifest.get(name) for name in SETTINGS})
    return {
        path: value
        for path, value in flat.items()
        if not any(fnmatchcase(path, pattern) for pattern in _NOT_FIXED)
    }


def settings_conflicts(
    recorded: Mapping[str, Any], asked: Mapping[str, Any]
) -> list[tuple[str, Any, Any]]:
    """(setting, recorded, asked for) for every setting a run that keeps
    its settings would change, by dotted path ("roles.extract.model");
    what may change between its invocations is left out (_NOT_FIXED)."""
    before = _fixed_settings(recorded)
    after = _fixed_settings(json.loads(json.dumps(dict(asked))))
    return [
        (path, before.get(path), after.get(path))
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    ]


def _keep_observed(
    recorded: Mapping[str, Any], fields: dict[str, Any]
) -> dict[str, Any]:
    """`fields` with what the run's engines recorded when they started
    (the CLI version, each role's client) carried over from `recorded`,
    for a run that keeps its settings: a later invocation that starts no
    engine still says what the run ran on."""
    fields = copy.deepcopy(fields)
    cli_version = (recorded.get("engine") or {}).get("cli_version")
    if cli_version and isinstance(fields.get("engine"), dict):
        fields["engine"]["cli_version"] = cli_version
    for role, settings in (fields.get("roles") or {}).items():
        client = ((recorded.get("roles") or {}).get(role) or {}).get("client")
        if client and isinstance(settings, dict):
            settings["client"] = client
    return fields


# the package's source folder, whose git checkout gives the code version
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _git(where: Path, *arguments: str) -> bytes | None:
    """A git command's output in `where`, or None when git is missing or
    the command fails (not a checkout, say). The code version is a
    record, never a precondition: no failure to ask git stops a run."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=where,
            capture_output=True,
            timeout=60,
        )
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def code_version(where: Path = PACKAGE_DIR) -> dict[str, Any]:
    """The code a run runs: the package `version`, and, when it runs from
    a git checkout, the `commit` and whether tracked files differ from it
    (`dirty`), with the SHA-256 of that difference (`diff_sha256`), so
    two runs of one commit with different edits tell apart. Untracked
    files are not counted. The difference itself is not recorded. Outside
    a checkout, or without git, the commit and the flag are None.
    Measured once per process for each location."""
    return dict(_code_version(Path(where)))


@functools.cache
def _code_version(where: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "version": __version__,
        "commit": None,
        "dirty": None,
        "diff_sha256": None,
    }
    commit = _git(where, "rev-parse", "HEAD")
    if commit is None:
        return record
    record["commit"] = commit.decode().strip()
    diff = _git(
        where,
        "diff",
        "HEAD",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
    )
    if diff is not None:
        record["dirty"] = bool(diff)
        if diff:
            record["diff_sha256"] = hashlib.sha256(diff).hexdigest()
    return record


def exclusions_record(exclusions: Any) -> dict[str, Any] | None:
    """The never-ingest list a run honours (yizkr.core.corpus.
    ExclusionList): the file it was read from, and the SHA-256 of what it
    excludes (its SHA-1s and its globs, each sorted), so two lists that
    exclude the same hash the same whatever their comments and order. The
    entries themselves are not recorded. None for no list."""
    if exclusions is None:
        return None
    source = getattr(exclusions, "source", None)
    return {
        "file": os.path.abspath(source) if source else None,
        "sha256": sha256_text(
            canonical_json(
                {
                    "sha1s": sorted(exclusions.sha1s),
                    "patterns": sorted(exclusions.patterns),
                }
            )
        ),
    }


# a context-window selector an engine accepts after a model name ("[1m]")
_MODEL_SELECTOR = re.compile(r"\[[^\]]*\]$")


def model_alias(name: str | None) -> bool:
    """Whether a configured model name is an alias, which the engine
    resolves to a model of its own choosing: one that asks for the latest
    model (`...-latest`), or one that names no version at all (the claude
    CLI's `sonnet`, `opus`, `default` and the like). A full model id names
    its version. No name is no alias: the engine then runs its own
    default, which the LLM cache warns is not keyed. An undated name of a
    model that also has dated snapshots cannot be told from a full id by
    its form, and passes."""
    if not name or not name.strip():
        return False
    base = model_base(name)
    return base.lower().endswith("-latest") or not any(
        character.isdigit() for character in base
    )


def model_base(name: str) -> str:
    """A model name without a context-window selector after it
    (`claude-opus-5-5` for `claude-opus-5-5[1m]`): the selector picks a
    context window, not the model."""
    return _MODEL_SELECTOR.sub("", name.strip())


class ModelAliasRefused(ValueError):
    """A run's configuration names a model by an alias (model_alias)."""


@dataclass(frozen=True)
class ControlState:
    """Who is stopped: 'run', 'stage:<name>' or 'kind:<name>' -> paused or
    aborted, folded from the requests since the invocation started."""

    scopes: dict[str, str]

    def stopped(
        self, *, stage: str | None = None, kind: str | None = None
    ) -> str | None:
        """'<scope> paused|aborted' when the run, the stage or the kind is
        stopped, else None."""
        candidates = ["run"]
        if stage:
            candidates.append(f"stage:{stage}")
        if kind:
            candidates.append(f"kind:{kind}")
        for scope in candidates:
            if scope in self.scopes:
                return f"{scope} {self.scopes[scope]}"
        return None


def fold_control(requests: list[dict]) -> ControlState:
    """Apply control requests in order. A pause holds until a resume; an
    abort holds for the rest of the invocation. A 'start' marker (a new
    invocation, which is how an aborted run resumes) clears everything
    before it."""
    scopes: dict[str, str] = {}
    for request in requests:
        action = request.get("action")
        if action == "start":
            scopes.clear()
            continue
        scope = request.get("scope") or "run"
        if scopes.get(scope) == "aborted":
            continue
        if action == PAUSE:
            scopes[scope] = "paused"
        elif action == ABORT:
            scopes[scope] = "aborted"
        elif action == RESUME:
            scopes.pop(scope, None)
    return ControlState(scopes)


def _append_line(path: Path, line: bytes) -> None:
    """Append `line`, which ends in a newline, to the log at `path` in one
    write, so that appends from several processes never interleave.

    When the log's last line is torn (its writer died mid-write), the same
    write ends it first with a newline: the fragment is left a line of its
    own, which readers skip, and `line` stays whole. Only a newline ends a
    line, as readers split at newlines only, so a log ending in a carriage
    return counts as torn.

    An exclusive POSIX lock on the log (fcntl.lockf) covers the check and
    the write, and closing the descriptor, or the writer's death,
    releases it. Appenders in different processes therefore take turns: a
    writer that dies mid-write leaves its fragment before the next
    appender's check, which ends it. The lock does not hold everywhere:

    - Where lockf raises OSError (a filesystem without POSIX locks), the
      append goes ahead unlocked rather than failing.
    - A POSIX lock belongs to the process, so appenders in one process
      (two RunDirs, each with its own in-process lock) share it, and
      closing any descriptor on the log in that process, a reader's in
      another thread say, releases it.

    Where the lock does not hold, an appender may find the last line torn
    when another has just ended it, or while another's large write is
    under way, and its newline then leaves a blank line, which readers
    skip. And a writer in another process that dies mid-write between
    this append's check and its write takes this record with it.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
    try:
        # where no lock is to be had, the append goes ahead unlocked
        with contextlib.suppress(OSError):
            fcntl.lockf(descriptor, fcntl.LOCK_EX)
        size = os.lseek(descriptor, 0, os.SEEK_END)
        if size:
            os.lseek(descriptor, size - 1, os.SEEK_SET)
            if os.read(descriptor, 1) != b"\n":
                line = b"\n" + line
        # O_APPEND writes at the end wherever the offset is; a short write
        # (a full disk, say) is finished with the rest or raises
        rest = memoryview(line)
        while rest:
            rest = rest[os.write(descriptor, rest) :]
    finally:
        os.close(descriptor)


class RunDir:
    """One run's directory. Nothing is created until something is written."""

    def __init__(self, path: Path, *, invocation: str | None = None) -> None:
        self.path = Path(path)
        self.invocation = invocation or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        # whether this invocation has started (start): what an engine
        # applies is recorded only within an invocation
        self._started = False

    # -- append-only logs ---------------------------------------------------

    def _append(self, name: str, entry: dict) -> dict:
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self.path.mkdir(parents=True, exist_ok=True)
            _append_line(self.path / name, line)
        return entry

    def emit(self, event: str, **fields: Any) -> dict:
        """Append one event; fields that are None are left out."""
        entry = {"at": _now(), "event": event, "invocation": self.invocation}
        entry.update({k: v for k, v in fields.items() if v is not None})
        return self._append(EVENTS, entry)

    def events(self) -> list[dict]:
        return read_jsonl(self.path / EVENTS)

    def alert(
        self,
        *,
        stage: str,
        check: str,
        subject: str,
        value: Any,
        carried: bool,
        **fields: Any,
    ) -> dict:
        """Append an anomaly alert, and an event pointing at it. `carried`
        marks an alert on an output the LLM cache answered every call of,
        or one reused whole; a new one is on an output the engine answered
        a call of in this run, or one its stage makes without a call
        (docs/ANOMALIES.md, New and carried)."""
        entry = {
            "at": _now(),
            "invocation": self.invocation,
            "stage": stage,
            "check": check,
            "subject": subject,
            "value": value,
            "status": "carried" if carried else "new",
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        self._append(ALERTS, entry)
        self.emit(
            "alert",
            stage=stage,
            check=check,
            subject=subject,
            status=entry["status"],
        )
        return entry

    def alerts(self) -> list[dict]:
        return read_jsonl(self.path / ALERTS)

    # -- control --------------------------------------------------------------

    def request(
        self,
        action: str,
        *,
        stage: str | None = None,
        kind: str | None = None,
        by: str = "operator",
        reason: str = "",
        **fields: Any,
    ) -> dict:
        """Ask the driver to pause, resume or abort the run or one slice of
        it (a stage, or a document kind; at most one of them)."""
        if action not in CONTROL_ACTIONS:
            raise ValueError(f"unknown control action {action!r}")
        if stage and kind:
            raise ValueError("a request names a stage or a kind, not both")
        scope = (
            f"stage:{stage}" if stage else f"kind:{kind}" if kind else "run"
        )
        entry = {
            "at": _now(),
            "action": action,
            "scope": scope,
            "by": by,
            "reason": reason,
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        return self._append(CONTROL, entry)

    def control(self) -> ControlState:
        return fold_control(read_jsonl(self.path / CONTROL))

    # -- manifest -------------------------------------------------------------

    def manifest(self) -> dict:
        try:
            return json.loads((self.path / MANIFEST).read_text())
        except (OSError, ValueError, RecursionError):
            return {}

    def _write_manifest(self, manifest: dict) -> None:
        atomic_write_text(
            self.path / MANIFEST,
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )

    def update_manifest(self, **fields: Any) -> dict:
        """Merge fields into run.json (replaced atomically)."""
        with self._lock:
            manifest = self.manifest()
            manifest.update(fields)
            self._write_manifest(manifest)
        return manifest

    def start(self, *, fixed_settings: bool = False, **fields: Any) -> dict:
        """Open an invocation: a run id on first start, the fields merged
        into the manifest, the invocation's start among its
        `invocations`, a control marker that clears earlier requests, and
        a run-start event.

        With `fixed_settings`, an invocation of a run that has started
        before must ask for the settings it recorded (SETTINGS, without
        what may change between invocations): otherwise it raises
        RunSettingsConflict and changes nothing. What the run's engines
        recorded when they started is kept."""
        now = _now()
        with self._lock:
            manifest = self.manifest()
            if fixed_settings and manifest.get("run_id"):
                conflicts = settings_conflicts(manifest, fields)
                if conflicts:
                    raise RunSettingsConflict(self.path, conflicts)
                fields = _keep_observed(manifest, fields)
            manifest.update(fields)
            # the format and the run id lead, for a reader of the file
            manifest = {
                "format": MANIFEST_FORMAT,
                "run_id": manifest.get("run_id") or new_run_id(),
            } | {
                key: value
                for key, value in manifest.items()
                if key not in ("format", "run_id")
            }
            manifest.setdefault("started_at", now)
            manifest["ended_at"] = None
            manifest["invocations"] = [
                *(manifest.get("invocations") or []),
                {
                    "invocation": self.invocation,
                    "started_at": now,
                    "ended_at": None,
                    "outcome": None,
                },
            ]
            self._write_manifest(manifest)
            self._started = True
        self._append(
            CONTROL,
            {"at": now, "action": "start", "invocation": self.invocation},
        )
        self.emit(RUN_START, run_id=manifest["run_id"])
        return manifest

    def end(self, outcome: str, *, reason: str | None = None) -> dict:
        """Close the invocation in the manifest: its end, its `outcome`
        (OUTCOMES) and, when it did not finish, the `reason` (the control
        request that stopped it, or the exception's name). The run's
        `ended_at` is the latest invocation's end."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        now = _now()
        with self._lock:
            manifest = self.manifest()
            for entry in manifest.get("invocations") or []:
                if entry.get("invocation") == self.invocation:
                    entry.update(ended_at=now, outcome=outcome)
                    if reason:
                        entry["reason"] = reason
            manifest["ended_at"] = now
            self._write_manifest(manifest)
        return manifest

    def record_engine(
        self,
        role: str,
        client: Mapping[str, Any],
        *,
        cli_version: str | None = None,
        fixed_settings: bool = False,
    ) -> None:
        """Record what a role's engine client applies once it has started
        (`roles[role].client`: its model, effort, configuration hash and
        model identifier) and the CLI version the pass is pinned to
        (`engine.cli_version`). Outside an invocation (before `start`)
        nothing is recorded.

        With `fixed_settings`, a client or a CLI version other than the one
        the run recorded raises RunSettingsConflict, and nothing is
        recorded."""
        if not self._started:
            return
        with self._lock:
            manifest = self.manifest()
            roles = manifest.setdefault("roles", {})
            settings = roles.get(role)
            if not isinstance(settings, dict):
                settings = roles[role] = {}
            engine = manifest.get("engine")
            if not isinstance(engine, dict):
                engine = manifest["engine"] = {}
            if fixed_settings:
                conflicts = []
                # a CLI updated since the run recorded its version comes
                # first: it changes each client's configuration hash too
                pinned = engine.get("cli_version")
                if cli_version and pinned and pinned != cli_version:
                    conflicts.append(
                        ("engine.cli_version", pinned, cli_version)
                    )
                recorded = settings.get("client") or {}
                conflicts += [
                    (f"roles.{role}.client.{key}", recorded[key], client[key])
                    for key in _CLIENT_FIXED
                    if key in recorded
                    and key in client
                    and recorded[key] != client[key]
                ]
                if conflicts:
                    raise RunSettingsConflict(self.path, conflicts)
            settings["client"] = dict(client)
            if cli_version is not None:
                engine["cli_version"] = cli_version
            self._write_manifest(manifest)


# -- run state ----------------------------------------------------------------


@dataclass(frozen=True)
class StageState:
    status: str
    reason: str | None = None


def _state_from_event(event: dict) -> StageState:
    kind = event["event"]
    if kind in ("done", "reused"):
        if event.get("attention"):
            return StageState(NEEDS_ATTENTION, "; ".join(event["attention"]))
        return StageState(DONE)
    if kind == "failed":
        return StageState(FAILED, event.get("reason"))
    if kind == "held":
        return StageState(PENDING, event.get("reason"))
    return StageState(RUNNING)


def invocation_of(entry: Mapping[str, Any]) -> str | None:
    """The invocation an event, or an entry of run.json's `invocations`,
    names; None when it names none, or names it other than as a string."""
    invocation = entry.get("invocation")
    return invocation if isinstance(invocation, str) else None


def invocation_records(manifest: Mapping[str, Any]) -> list[dict]:
    """The invocations run.json records, in the order they started: each
    entry of its `invocations` that is an object; anything else there is
    passed over."""
    entries = manifest.get("invocations")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


class Invocations:
    """Which of a run's invocations is the latest, and whether it has
    ended, as run.json and the events that open and close invocations
    (run-start; run-end or run-interrupted) tell. The events are taken one
    at a time, in the order they were written (`fold`), so a reader that
    follows the event log as it grows (the live monitor) and one that
    reads it whole (run_state) come to the same reading.

    `closing` holds each invocation's closing event (the later, should it
    have two), and `marker` and `marker_invocation` the latest event that
    opens or closes an invocation, and the invocation it names."""

    def __init__(self) -> None:
        self.closing: dict[str | None, dict] = {}
        self.marker: str | None = None
        self.marker_invocation: str | None = None

    def fold(self, event: dict) -> None:
        """Take the log's next event; one that neither opens nor closes an
        invocation changes nothing."""
        name = event.get("event")
        if name not in (RUN_START, RUN_END, RUN_INTERRUPTED):
            return
        invocation = invocation_of(event)
        self.marker, self.marker_invocation = name, invocation
        if name != RUN_START:
            self.closing[invocation] = event

    def latest(self, manifest: Mapping[str, Any]) -> tuple[str | None, bool]:
        """(the latest invocation's id, whether it has ended), from the run
        manifest and the events taken so far.

        The latest invocation is the last that run.json records, and it
        has ended once run.json records its end or its closing event has
        been taken. A run.json that records no invocation leaves it to the
        events: the latest is the invocation that the latest event opening
        or closing one names, ended when that event closes it. A killed
        invocation records no end and has no closing event, so while it is
        the latest it reads as not ended, as one still running does."""
        records = invocation_records(manifest)
        if not records:
            ended = self.marker in (RUN_END, RUN_INTERRUPTED)
            return self.marker_invocation, ended
        last = records[-1]
        latest = invocation_of(last)
        return latest, bool(last.get("ended_at")) or latest in self.closing


def interrupted(
    started_by: str | None, latest: str | None, ended: bool
) -> bool:
    """Whether a stage whose latest event is its start, by the invocation
    `started_by`, was cut short: that invocation is over, so nothing runs
    the stage, and the next invocation starts it again. `latest` and
    `ended` are the run's latest invocation and whether it has ended
    (Invocations.latest). Every such stage is cut short once the latest
    has ended; and one an earlier invocation started is cut short whether
    the latest has ended or not, since a later start shows that the
    earlier invocation is over, however it ended, killed included.
    Otherwise the stage is running: the latest invocation started it and
    has not ended, or its start, or the run, names no invocation to tell
    by."""
    if ended:
        return True
    return (
        latest is not None and started_by is not None and started_by != latest
    )


# how many times run_state reads a run directory while invocations start
# or end as it reads (_consistent_read)
_READS = 5


def _consistent_read(run: RunDir) -> tuple[list[dict], dict]:
    """The run's events, and a reading of run.json that holds for them.

    run.json is read before the events and again after them. While the two
    readings differ in the invocations they record, or in their ends (an
    invocation started or ended while the events were read), all three
    are read again, _READS times at most. When they agree, the invocations
    and ends run.json records held while the events were read. An
    invocation records its start in run.json before its first event, and
    its end before its closing event, so a stage an invocation started
    after the one before it ended is not read against that one, and a
    stage done just before its invocation ended is not read without its
    done. When they never agree, the last events are read against the
    run.json read after them."""
    for _ in range(_READS):
        before = run.manifest()
        events = run.events()
        manifest = run.manifest()
        if invocation_records(before) == invocation_records(manifest):
            break
    return events, manifest


def run_state(run: RunDir) -> dict[tuple[str, str], StageState]:
    """State per (document SHA-1, stage).

    Every stage the manifest plans starts as pending, and the latest event
    for it sets its state. A stage whose latest event is its start is
    running while the invocation that started it runs, and interrupted
    once that invocation is over (interrupted): the next invocation starts
    it again. The events are read with a reading of run.json that holds
    for them (_consistent_read)."""
    events, manifest = _consistent_read(run)
    states: dict[tuple[str, str], StageState] = {}
    for document in manifest.get("documents") or []:
        for stage in document.get("stages") or []:
            states[(document["sha1"], stage)] = StageState(PENDING)
    invocations = Invocations()
    # the invocation of each stage's latest event: a running stage's start
    started_by: dict[tuple[str, str], str | None] = {}
    for event in events:
        invocations.fold(event)
        if event.get("event") not in STAGE_EVENTS:
            continue
        subject, stage = event.get("subject"), event.get("stage")
        if subject and stage:
            states[(subject, stage)] = _state_from_event(event)
            started_by[(subject, stage)] = invocation_of(event)
    latest, ended = invocations.latest(manifest)
    for key, invocation in started_by.items():
        if states[key].status == RUNNING and interrupted(
            invocation, latest, ended
        ):
            states[key] = StageState(INTERRUPTED)
    return states


def summarise(
    states: dict[tuple[str, str], StageState],
) -> dict[str, dict[str, int]]:
    """stage -> status -> count, for a quick look at a run."""
    summary: dict[str, dict[str, int]] = {}
    for (_subject, stage), state in states.items():
        counts = summary.setdefault(stage, {})
        counts[state.status] = counts.get(state.status, 0) + 1
    return summary


def cache_counts(run: RunDir) -> dict[str, dict[str, int]]:
    """role -> what the LLM cache did for it -> count: lifetime totals,
    over every invocation the run directory has seen. Hits and misses
    are counted by reason ("hit: key match", "miss: absent"), besides
    resumes, replays, rejections and adoptions."""
    counts: dict[str, dict[str, int]] = {}
    for event in run.events():
        name = event.get("event")
        if name not in CACHE_EVENTS:
            continue
        label = name.removeprefix("cache-")
        if name in ("cache-hit", "cache-miss") and event.get("reason"):
            label = f"{label}: {event['reason']}"
        role = counts.setdefault(event.get("role") or "?", {})
        role[label] = role.get(label, 0) + 1
    return counts


def usage_counts(
    run: RunDir, *, replays: bool | None = None
) -> dict[str, dict[str, dict[str, int]]]:
    """stage -> role -> what the calls that reached the engine used:
    lifetime totals, over every invocation the run directory has seen,
    from its `call` events. A call the LLM cache answered, by key match or
    by adoption, reached no engine, used nothing and has no call event.

    Each total counts the `calls`, those whose event carries a usage
    (`reported`), and sums the tokens those carry, by the CLI's names for
    them (llm.USAGE_TOKENS), each 0 when none was reported. A call's usage
    is summed over its attempts, so a call whose final attempt was cut off
    before its result event is reported with what its earlier attempts,
    refused for a limit, used, and one none of whose attempts reported
    any is not reported: the sums are of what was reported. A usage of
    zeros is reported. `replays` narrows the calls to the replays of a
    conversation's opening turn (True), which the cache asked for a
    fork's session although it held the answer, or to every other call
    (False). A call names its stage and role, else counts under "?"."""
    # imported here, where it is needed, so that what reads a run
    # directory without its usage (the monitor, say) never loads the
    # engine's module
    from yizkr.pipeline.llm import USAGE_TOKENS

    counts: dict[str, dict[str, dict[str, int]]] = {}
    for event in run.events():
        if event.get("event") != CALL:
            continue
        if replays is not None and (event.get("replay") is True) != replays:
            continue
        stage, role = event.get("stage"), event.get("role")
        totals = counts.setdefault(
            stage if isinstance(stage, str) and stage else "?", {}
        ).setdefault(
            role if isinstance(role, str) and role else "?",
            {"calls": 0, "reported": 0, **dict.fromkeys(USAGE_TOKENS, 0)},
        )
        totals["calls"] += 1
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        tokens = {
            name: usage[name]
            for name in USAGE_TOKENS
            if isinstance(usage.get(name), int)
            and not isinstance(usage[name], bool)
            and usage[name] >= 0
        }
        if tokens:
            totals["reported"] += 1
            for name, count in tokens.items():
                totals[name] += count
    return counts


def describe_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """What shapes a run, one line per setting, from its manifest: for
    `yizkr stages manifest` and anything else that shows a run's
    configuration to a person. Each role shows what its engine client
    applied when one started in the latest invocation, else what the run
    configured."""

    def short(digest: Any) -> str:
        return str(digest)[:12] if digest else "none"

    engine = manifest.get("engine") or {}
    code = manifest.get("code") or {}
    corpus = manifest.get("corpus") or {}
    exclusions = corpus.get("exclusions")
    invocations = manifest.get("invocations") or []
    stage = manifest.get("project_stage")
    lines = [
        f"run         {manifest.get('run_id')} ({manifest.get('mode')}"
        + (f", {stage} stage" if stage else "")
        + ")",
        f"engine      {engine.get('name') or '?'}"
        + (
            f", CLI {engine['cli_version']}"
            if engine.get("cli_version")
            else ""
        ),
        "code        "
        + (short(code.get("commit")) if code.get("commit") else "unknown")
        + (
            f", dirty (diff {short(code.get('diff_sha256'))})"
            if code.get("dirty")
            else ""
        )
        + (f", version {code['version']}" if code.get("version") else ""),
        f"corpus      {corpus.get('folder') or '?'}",
        "exclusions  "
        + (
            f"{exclusions.get('file') or 'no file'} "
            f"(sha256 {short(exclusions.get('sha256'))})"
            if isinstance(exclusions, dict)
            else "none recorded"
        ),
        f"cache       {manifest.get('cache') or 'off'}",
    ]
    for role, settings in (manifest.get("roles") or {}).items():
        settings = settings or {}
        client = settings.get("client") or {}
        model = client.get("model") if client else settings.get("model")
        effort = client.get("effort") if client else settings.get("effort")
        parts = [
            f"model {model or 'engine default'}",
            f"effort {effort or 'none'}",
            f"epoch {settings.get('epoch', 0)}",
        ]
        if settings.get("refresh"):
            parts.append("refreshed")
        parts.append(
            f"config {short(client.get('config_hash'))}"
            if client
            else "not started"
        )
        lines.append(f"role        {role}: " + ", ".join(parts))
    last = invocations[-1] if invocations else {}
    lines.append(
        f"started     {manifest.get('started_at') or '?'}; "
        + (
            f"ended {manifest['ended_at']}"
            if manifest.get("ended_at")
            else "not ended"
        )
        + (f" ({last['outcome']})" if last.get("outcome") else "")
        + f"; {len(invocations)} invocation"
        + ("" if len(invocations) == 1 else "s")
    )
    return lines
