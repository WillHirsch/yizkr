"""Run directory: the manifest, event log, alerts and control requests of
one run, and the run state derived from them.

    <run dir>/run.json       the run manifest: run id, the LLM cache, the
                             planned documents (SHA-1, kind, stages) and
                             the run's configuration
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
docs/LLM_CACHE.md is their contract.

Run state per document and stage is derived from the run manifest and
the events alone (never from file modification times or a per-document
status file): a stage is pending until an event names it; running once
it has started; done, or needs attention when its output carries
attention reasons, whether it was made in the run or reused from an
earlier one; failed, with the reason, until a later start; and pending
again, with the reason, while the control file holds it.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yizkr.pipeline.stages import atomic_write_text, read_jsonl

MANIFEST = "run.json"
EVENTS = "events.jsonl"
ALERTS = "alerts.jsonl"
CONTROL = "control.jsonl"

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

    def update_manifest(self, **fields: Any) -> dict:
        """Merge fields into run.json (replaced atomically)."""
        with self._lock:
            manifest = self.manifest()
            manifest.update(fields)
            atomic_write_text(
                self.path / MANIFEST,
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            )
        return manifest

    def start(self, **fields: Any) -> dict:
        """Open an invocation: a run id on first start, a control marker
        that clears earlier requests, and a run-start event."""
        manifest = self.manifest()
        run_id = manifest.get("run_id") or new_run_id()
        manifest = self.update_manifest(run_id=run_id, **fields)
        self._append(
            CONTROL,
            {"at": _now(), "action": "start", "invocation": self.invocation},
        )
        self.emit(RUN_START, run_id=run_id)
        return manifest


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
