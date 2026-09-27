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
atomically. Readers skip a torn last line, so a reader never needs a lock
and an interrupted writer never corrupts the log. Every stage start,
completion and failure is an event, and so is every view a run reuses
(yizkr.pipeline.orchestrate) and every answer the LLM cache gives or
cannot give (yizkr.pipeline.llmcache); the driver, the live monitor, the
anomaly checks and pacing all read and write these files, and
docs/LLM_CACHE.md and docs/RUN_MANIFEST.md are their contract.

The manifest records settings and hashes, never a prompt or any document
content. It describes the run as its latest invocation ran it. A run
that must keep its settings across invocations (``fixed_settings``)
refuses an invocation that asks for others (RunSettingsConflict); one
that may change them, as `ingest run` may, records the latest.

Run state per document and stage is derived from the run manifest and
the events alone (never from file modification times or a per-document
status file): a stage is pending until an event names it; running once
it has started; done, or needs attention when its output carries
attention reasons, whether it was made in the run or reused from an
earlier one; failed, with the reason, until a later start; and pending
again, with the reason, while the control file holds it.
"""

from __future__ import annotations

import copy
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

# control actions; "start" is the marker a new invocation writes, after
# which earlier requests no longer apply
PAUSE = "pause"
RESUME = "resume"
ABORT = "abort"
CONTROL_ACTIONS = (PAUSE, RESUME, ABORT)

# run states of one stage of one document
PENDING = "pending"
RUNNING = "running"
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
    than the ones the run recorded.

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
        super().__init__(
            f"{Path(run) / MANIFEST} records other settings for this run:\n"
            f"{lines}\n"
            "A run is resumed only with the settings it started with. "
            "Invoke it again with those, or start a new run in a new run "
            "directory: the LLM cache answers every call the change leaves "
            "the same."
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
    base = _MODEL_SELECTOR.sub("", name.strip())
    return base.lower().endswith("-latest") or not any(
        character.isdigit() for character in base
    )


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
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.mkdir(parents=True, exist_ok=True)
            with open(self.path / name, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
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
        marks an alert on an output made from answers the LLM cache gave
        (new ones are on outputs the engine answered in this run)."""
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
        except (OSError, ValueError):
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
                recorded = settings.get("client") or {}
                conflicts = [
                    (f"roles.{role}.client.{key}", recorded[key], client[key])
                    for key in _CLIENT_FIXED
                    if key in recorded
                    and key in client
                    and recorded[key] != client[key]
                ]
                pinned = engine.get("cli_version")
                if cli_version and pinned and pinned != cli_version:
                    conflicts.append(
                        ("engine.cli_version", pinned, cli_version)
                    )
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


def run_state(run: RunDir) -> dict[tuple[str, str], StageState]:
    """State per (document SHA-1, stage).

    Every stage the manifest plans starts as pending, and the latest event
    for it sets its state. A stage that started and has no later event is
    running, or was interrupted: the next run starts it again."""
    states: dict[tuple[str, str], StageState] = {}
    for document in run.manifest().get("documents") or []:
        for stage in document.get("stages") or []:
            states[(document["sha1"], stage)] = StageState(PENDING)
    for event in run.events():
        if event.get("event") not in STAGE_EVENTS:
            continue
        subject, stage = event.get("subject"), event.get("stage")
        if subject and stage:
            states[(subject, stage)] = _state_from_event(event)
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
