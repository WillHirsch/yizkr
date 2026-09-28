"""The live run monitor: what a run is doing while it unfolds, read from its
run directory, and the operator's pause, resume and abort requests.

A monitor attaches to any run directory (yizkr.pipeline.rundir), wherever
and whenever the run was started, and follows its files as they grow:

    run.json       the plan (each document's SHA-1, kind and stages) and
                   each invocation's start, end and outcome
    events.jsonl   stage events, the LLM cache's events, and call events
                   when the engine writes them
    alerts.jsonl   anomaly alerts, new or carried
    control.jsonl  pause, resume and abort requests

From them it builds a summary (RunMonitor.summary):

* per stage, how many documents are pending, running, done (made in this
  run), reused (every call answered by the LLM cache, by the answers'
  reason, or the view current, so it stands), need attention or failed;
* the documents in flight: stages the running invocation has started and
  not finished;
* engine calls and the cache's answers, the run's elapsed time, and the
  engine calls the remaining work will take at the run's rates so far;
* the latest alerts, tagged new or carried;
* the state of each slice (the run, each stage, each document kind):
  active, paused or aborted, with the request that stopped it.

It shows the summary in two views: a terminal view refreshed in place,
in plain ANSI (`watch`), and a status page, status.html in the run
directory: one self-contained HTML file, with no external assets, that
reloads itself and is written again as events arrive. Both name a
document by its SHA-1 prefix and kind only, never by its file name or
content, and show only the leading label of a failure or attention
reason (an exception's name, "no claims extracted"), since the details
can quote a document. The page stays in the run directory under var/,
local-only data like everything there (docs/AGENT_PROTOCOL.md §6).

From the terminal view the operator asks for a pause, resume or abort of
the run or of one slice; the monitor appends the request to
control.jsonl through RunDir.request, and the driver honours it. The
monitor writes nothing else but the status page.

Reading is incremental: each refresh parses only the lines appended
since the last one, and a line not yet ended (a writer mid-line) waits
for the next refresh, so a monitor keeps up with a long run. A file that
shrinks or is replaced, or a run.json that names another run, starts the
monitor over. docs/MONITOR.md describes the views, the commands and the
events the monitor reads.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import select
import shutil
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from yizkr.pipeline.rundir import (
    ABORT,
    ALERTS,
    CACHE_EVENTS,
    CONTROL,
    CONTROL_ACTIONS,
    DONE,
    EVENTS,
    FAILED,
    MANIFEST,
    NEEDS_ATTENTION,
    PAUSE,
    PENDING,
    RESUME,
    RUN_END,
    RUN_INTERRUPTED,
    RUN_START,
    RUNNING,
    SET_ASIDE,
    STAGE_EVENTS,
    RunDir,
    fold_control,
)
from yizkr.pipeline.stages import atomic_write_text

# the status page, beside the run's other files
STATUS_PAGE = "status.html"
# how many hex digits of a SHA-1 name a document in the views
PREFIX = 10
# how many of the latest alerts the views list
LATEST_ALERTS = 10
# what a request the monitor writes says it came from
MONITOR = "monitor"

# one engine call, when the engine records them: its fields are optional
# (`role`, `subject`, `kind`, `stage`, `duration_s`, `outcome`, `wait_s`),
# and a run without call events is shown from the cache's events alone
CALL = "call"

# how a completed stage came about: made in the run (by the engine, or by
# code that calls none), or reused: the view current, or every call the
# stage made answered by the cache, labelled by the answers' reasons
MADE = "made"
VIEW_CURRENT = "view current"
KEY_MATCH = "key match"

# the cache's events that are an engine call: a miss asks the engine, and
# so does a replay of a conversation's opening turn
_ENGINE_EVENTS = ("cache-miss", "cache-replay")

# slice states
ACTIVE = "active"
PAUSED = "paused"
ABORTED = "aborted"

# the status page reloads itself this often (seconds), and is written at
# most this often while events arrive, and at least this often otherwise
PAGE_RELOAD = 5
PAGE_MIN_INTERVAL = 5.0
PAGE_MAX_AGE = 30.0

_HEX = re.compile(r"[0-9a-fA-F]{4,}")
# an exception class's name, as a failure reason starts with it: ASCII,
# in CamelCase with two capitals at least, after any module path; a word
# that is not one ("Error", a name) is not shown
_EXCEPTION = re.compile(
    r"(?:[a-z_][a-z0-9_]*\.)*[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*"
)


# -- reading append-only logs -------------------------------------------------


class _Tail:
    """The complete lines appended to a JSON-lines file since the last read.

    A line not yet ended by its newline is left for the next read, since a
    writer may be mid-line; a complete line that is not a JSON object is
    skipped and counted. A file that shrinks, is replaced or disappears
    after it was read makes `read` report a restart, and the next read
    starts from its beginning."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.skipped = 0
        self._identity: tuple[int, int] | None = None

    def read(self) -> tuple[list[dict], bool]:
        """(the new entries, whether the file restarted)."""
        try:
            stat = os.stat(self.path)
        except OSError:
            restarted = self._identity is not None
            self.offset, self._identity = 0, None
            return [], restarted
        identity = (stat.st_dev, stat.st_ino)
        if self._identity is not None and (
            identity != self._identity or stat.st_size < self.offset
        ):
            self.offset, self._identity = 0, None
            return [], True
        self._identity = identity
        if stat.st_size == self.offset:
            return [], False
        entries: list[dict] = []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                for line in handle:
                    if not line.endswith(b"\n"):
                        break
                    self.offset += len(line)
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        entry = None
                    if isinstance(entry, dict):
                        entries.append(entry)
                    else:
                        self.skipped += 1
        except OSError:
            pass
        return entries, False


def _read_manifest(path: Path) -> dict:
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _seconds(value: Any) -> float | None:
    """A non-negative number of seconds from an event, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# -- labels that never quote a document ---------------------------------------


def prefix(subject: Any) -> str:
    """A document's name in the views: its SHA-1's first PREFIX hex
    digits; '-' for anything that is not a SHA-1 or a prefix of one."""
    if isinstance(subject, str) and _HEX.fullmatch(subject):
        return subject[:PREFIX].lower()
    return "-"


def failure_label(reason: Any) -> str:
    """The exception's name a failure reason starts with ("RuntimeError:
    ..." gives "RuntimeError"), or "error" when it starts with anything
    else: the message after the name can quote a document."""
    if isinstance(reason, str):
        head = reason.split(":", 1)[0].strip()
        if len(head) <= 80 and _EXCEPTION.fullmatch(head):
            return head
    return "error"


def attention_label(reason: Any) -> str:
    """An attention reason up to its first colon, where its details start
    ("entity manifest fell back to line-range segmentation: ..." gives
    the part before the colon)."""
    if not isinstance(reason, str):
        return "?"
    return _clip(reason.split(":", 1)[0].strip(), 60) or "?"


def _value(value: Any) -> str:
    """An alert's measured value, short."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.3g}"
    if value is None:
        return "-"
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return _clip(" ".join(value.split()), 32)


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _stamp(moment: datetime | None, spec: str = "%Y-%m-%d %H:%M UTC") -> str:
    return moment.astimezone(UTC).strftime(spec) if moment else "?"


def _n(count: int) -> str:
    return f"{count:,}"


def _counts(counter: Counter | dict[str, int]) -> str:
    """'3 key match, 1 adopted': the most frequent first."""
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{_n(count)} {label}" for label, count in ordered)


# -- the summary --------------------------------------------------------------


@dataclass
class StageCounts:
    """One stage over the run's documents. pending, running, done,
    reused, attention and failed partition its documents: a completed
    output that needs attention counts there, whether it was made or
    reused, and a stage an invocation that has ended left started counts
    as pending (interrupted: the next invocation starts it again)."""

    name: str
    pending: int = 0
    running: int = 0
    done: int = 0
    attention: int = 0
    failed: int = 0
    reused: Counter = field(default_factory=Counter)
    # of the pending: held by the control file, and interrupted
    held: int = 0
    interrupted: int = 0
    failures: Counter = field(default_factory=Counter)
    attention_reasons: Counter = field(default_factory=Counter)
    # the stage's calls, as far as the cache's events show them
    engine: int = 0
    cached: int = 0

    @property
    def total(self) -> int:
        return (
            self.pending
            + self.running
            + self.done
            + sum(self.reused.values())
            + self.attention
            + self.failed
        )


@dataclass(frozen=True)
class InFlight:
    sha1: str
    kind: str | None
    stage: str
    since: datetime | None


@dataclass(frozen=True)
class AlertLine:
    at: datetime | None
    status: str
    stage: str
    check: str
    subject: str
    kind: str | None
    value: str


@dataclass(frozen=True)
class SliceState:
    """A slice ('run', 'stage:<name>' or 'kind:<name>') and whether the
    control file stops it, with the request that did."""

    scope: str
    state: str
    since: datetime | None = None
    by: str | None = None
    reason: str | None = None


@dataclass
class CallCounts:
    """What the run's calls came to. `engine` and `cached` count the LLM
    cache's events: a miss or a replay asked the engine, a hit answered
    from the cache. `logged` and the rest count call events, where the
    engine writes them."""

    engine: int = 0
    cached: int = 0
    hit_reasons: Counter = field(default_factory=Counter)
    resumed: int = 0
    replayed: int = 0
    rejected: int = 0
    adopted: int = 0
    roles: dict[str, Counter] = field(default_factory=dict)
    cache_events: int = 0
    logged: int = 0
    seconds: float = 0.0
    timed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    waited: float = 0.0
    waits: int = 0

    @property
    def to_engine(self) -> int | None:
        """Engine calls: from the cache's events when the run has them,
        else from call events; None when it has neither."""
        if self.cache_events:
            return self.engine
        return self.logged if self.logged else None


@dataclass
class Summary:
    path: Path
    now: datetime
    found: bool = False
    run_id: str | None = None
    mode: str | None = None
    project_stage: str | None = None
    state: str = "waiting"
    reason: str | None = None
    invocations: int = 0
    started_at: datetime | None = None
    elapsed: float | None = None
    invocation_elapsed: float | None = None
    last_event_at: datetime | None = None
    documents: int = 0
    kinds: Counter = field(default_factory=Counter)
    stages: list[StageCounts] = field(default_factory=list)
    in_flight: list[InFlight] = field(default_factory=list)
    calls: CallCounts = field(default_factory=CallCounts)
    remaining_calls: int | None = None
    unrated: list[str] = field(default_factory=list)
    alerts: list[AlertLine] = field(default_factory=list)
    alert_counts: Counter = field(default_factory=Counter)
    alert_checks: Counter = field(default_factory=Counter)
    slices: list[SliceState] = field(default_factory=list)
    set_aside: int = 0
    skipped_lines: int = 0

    def stage(self, name: str) -> StageCounts:
        for counts in self.stages:
            if counts.name == name:
                return counts
        raise KeyError(name)

    def slice(self, scope: str) -> SliceState:
        for state in self.slices:
            if state.scope == scope:
                return state
        return SliceState(scope, ACTIVE)


# -- following a run ----------------------------------------------------------


@dataclass
class _Attempt:
    """One stage of one document, as its latest events leave it."""

    status: str = PENDING
    since: datetime | None = None
    invocation: str | None = None
    held: bool = False
    how: str = MADE
    attention: tuple[str, ...] = ()
    failure: str | None = None
    # the calls of the latest attempt: engine calls (the cache's misses
    # and replays), the cache's answers by reason, and call events
    engine: int = 0
    hits: Counter = field(default_factory=Counter)
    calls: int = 0
    # every attempt that ended (done, reused or failed), and their calls:
    # what the estimate of the calls to go is made from
    ended: int = 0
    ended_engine: int = 0
    ended_calls: int = 0

    def begin(self) -> None:
        self.engine, self.hits, self.calls = 0, Counter(), 0

    def end(self) -> None:
        self.ended += 1
        self.ended_engine += self.engine
        self.ended_calls += self.calls

    def reuse_reason(self) -> str:
        """How the attempt's output came about: MADE when the engine was
        asked, or no call was made; otherwise the reasons the cache
        answered with."""
        if self.engine or not self.hits:
            return MADE
        return " + ".join(sorted(self.hits))


@dataclass
class _Rate:
    """Attempts at a stage that ended, and the calls they made."""

    attempts: int = 0
    engine: int = 0
    calls: int = 0


class RunMonitor:
    """Follows one run directory. `refresh` reads what its files gained
    since the last refresh; `summary` says where the run stands."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # bumped whenever a refresh finds something new; never reset, so a
        # view can tell a restart from no change
        self.version = 0
        self._reset()

    def _reset(self) -> None:
        self._events = _Tail(self.path / EVENTS)
        self._alerts = _Tail(self.path / ALERTS)
        self._control = _Tail(self.path / CONTROL)
        self._manifest: dict = {}
        self._attempts: dict[tuple[str, str], _Attempt] = {}
        # the stage each document is in, while one runs: the cache's
        # events name the document and the role, not the stage
        self._current: dict[str, str] = {}
        self._stages_seen: dict[str, None] = {}
        self._kinds: dict[str, str] = {}
        self._stage_calls: dict[str, Counter] = {}
        self._calls = CallCounts()
        self._latest_alerts: deque[dict] = deque(maxlen=LATEST_ALERTS)
        self._alert_counts: Counter = Counter()
        self._alert_checks: Counter = Counter()
        self._requests: list[dict] = []
        self._last_event_at: datetime | None = None
        self._last_at: dict[str, datetime] = {}
        self._marker: str | None = None
        self._set_aside = 0

    def refresh(self) -> bool:
        """Read what the run directory gained since the last refresh, and
        whether it gained anything. The logs are read before run.json: an
        invocation records itself there before its first event."""
        changed = False
        for tail, fold in (
            (self._events, self._fold_event),
            (self._alerts, self._fold_alert),
            (self._control, self._requests.append),
        ):
            entries, restarted = tail.read()
            if restarted:
                self._reset()
                self.refresh()
                self.version += 1
                return True
            for entry in entries:
                fold(entry)
            changed = changed or bool(entries)
        manifest = _read_manifest(self.path)
        known, named = self._manifest.get("run_id"), manifest.get("run_id")
        if known and named and known != named:
            # another run in the directory: start over
            self._reset()
            self.refresh()
            self.version += 1
            return True
        if manifest != self._manifest:
            self._manifest = manifest
            changed = True
        if changed:
            self.version += 1
        return changed

    # -- folding the logs --------------------------------------------------

    def _fold_event(self, event: dict) -> None:
        name = event.get("event")
        at = _time(event.get("at"))
        if at is not None:
            if self._last_event_at is None or at > self._last_event_at:
                self._last_event_at = at
            invocation = event.get("invocation")
            if isinstance(invocation, str):
                self._last_at[invocation] = at
        if name in STAGE_EVENTS:
            self._fold_stage(event, name, at)
        elif name in CACHE_EVENTS:
            self._fold_cache(event, name)
        elif name == CALL:
            self._fold_call(event)
        elif name in (RUN_START, RUN_END, RUN_INTERRUPTED):
            self._marker = name
            if name == RUN_START:
                self._current.clear()
        elif name == SET_ASIDE:
            self._set_aside += 1

    def _fold_stage(self, event: dict, name: str, at: datetime | None) -> None:
        subject, stage = event.get("subject"), event.get("stage")
        if not isinstance(subject, str) or not isinstance(stage, str):
            return
        self._stages_seen.setdefault(stage)
        kind = event.get("kind")
        if isinstance(kind, str):
            self._kinds.setdefault(subject, kind)
        attempt = self._attempts.setdefault((subject, stage), _Attempt())
        if name == "started":
            attempt.status, attempt.since = RUNNING, at
            invocation = event.get("invocation")
            attempt.invocation = (
                invocation if isinstance(invocation, str) else None
            )
            attempt.held = False
            attempt.begin()
            self._current[subject] = stage
            return
        if self._current.get(subject) == stage:
            del self._current[subject]
        if name == "held":
            attempt.status, attempt.held = PENDING, True
            return
        attempt.held = False
        if name == "failed":
            attempt.status = FAILED
            attempt.failure = failure_label(event.get("reason"))
        else:
            if name == "reused":
                attempt.begin()
                attempt.how = VIEW_CURRENT
            else:
                attempt.how = attempt.reuse_reason()
            reasons = event.get("attention") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            attempt.attention = tuple(
                attention_label(reason) for reason in reasons
            )
            attempt.status = NEEDS_ATTENTION if attempt.attention else DONE
        attempt.end()

    def _fold_cache(self, event: dict, name: str) -> None:
        calls = self._calls
        calls.cache_events += 1
        role = event.get("role")
        role = role if isinstance(role, str) and role else "?"
        reason = event.get("reason")
        reason = reason if isinstance(reason, str) and reason else None
        per_role = calls.roles.setdefault(role, Counter())
        if name in _ENGINE_EVENTS:
            calls.engine += 1
            per_role["engine"] += 1
        elif name == "cache-hit":
            calls.cached += 1
            calls.hit_reasons[reason or KEY_MATCH] += 1
            per_role["cached"] += 1
        elif name == "cache-resume":
            calls.resumed += 1
        elif name == "cache-reject":
            calls.rejected += 1
        elif name == "cache-adopt":
            calls.adopted += 1
        if name == "cache-replay":
            calls.replayed += 1
        subject = event.get("subject")
        stage = (
            self._current.get(subject) if isinstance(subject, str) else None
        )
        if stage is None:
            return
        attempt = self._attempts[(subject, stage)]
        stage_calls = self._stage_calls.setdefault(stage, Counter())
        if name in _ENGINE_EVENTS:
            attempt.engine += 1
            stage_calls["engine"] += 1
        elif name == "cache-hit":
            attempt.hits[reason or KEY_MATCH] += 1
            stage_calls["cached"] += 1

    def _fold_call(self, event: dict) -> None:
        calls = self._calls
        calls.logged += 1
        duration = _seconds(event.get("duration_s"))
        if duration is not None:
            calls.seconds += duration
            calls.timed += 1
        outcome = event.get("outcome")
        calls.outcomes[outcome if isinstance(outcome, str) else "?"] += 1
        wait = _seconds(event.get("wait_s"))
        if wait:
            calls.waited += wait
            calls.waits += 1
        subject = event.get("subject")
        stage = (
            self._current.get(subject) if isinstance(subject, str) else None
        )
        if stage is not None:
            self._attempts[(subject, stage)].calls += 1

    def _fold_alert(self, alert: dict) -> None:
        self._latest_alerts.append(alert)
        status = alert.get("status")
        self._alert_counts[status if isinstance(status, str) else "?"] += 1
        check = alert.get("check")
        self._alert_checks[check if isinstance(check, str) else "?"] += 1

    # -- the plan -----------------------------------------------------------

    def _plan(self) -> dict[str, tuple[str | None, list[str]]]:
        """SHA-1 -> (kind, stages) of each planned document; a SHA-1
        planned twice (two files with the same bytes) counts once."""
        plan: dict[str, tuple[str | None, list[str]]] = {}
        for document in self._manifest.get("documents") or []:
            if not isinstance(document, dict):
                continue
            sha1 = document.get("sha1")
            if not isinstance(sha1, str) or sha1 in plan:
                continue
            kind = document.get("kind")
            stages = [
                stage
                for stage in document.get("stages") or []
                if isinstance(stage, str)
            ]
            plan[sha1] = (kind if isinstance(kind, str) else None, stages)
        return plan

    # -- the summary --------------------------------------------------------

    def summary(self, now: datetime | None = None) -> Summary:
        """Where the run stands at `now` (the current time by default),
        from what the refreshes have read."""
        now = now or datetime.now(UTC)
        manifest = self._manifest
        summary = Summary(path=self.path, now=now)
        summary.found = bool(manifest) or self._events.offset > 0
        summary.skipped_lines = (
            self._events.skipped + self._alerts.skipped + self._control.skipped
        )
        summary.last_event_at = self._last_event_at
        summary.set_aside = self._set_aside
        summary.calls = copy.deepcopy(self._calls)
        if not summary.found:
            return summary
        summary.run_id = _string(manifest.get("run_id"))
        summary.mode = _string(manifest.get("mode"))
        summary.project_stage = _string(manifest.get("project_stage"))
        self._invocations(summary, now)

        plan = self._plan()
        kinds = {sha1: kind for sha1, (kind, _) in plan.items()}
        for sha1, kind in self._kinds.items():
            kinds.setdefault(sha1, kind)
        summary.documents = len(plan)
        summary.kinds = Counter(kind or "?" for kind, _ in plan.values())

        order: dict[str, None] = {}
        pairs: dict[tuple[str, str], None] = {}
        for sha1, (_, stages) in plan.items():
            for stage in stages:
                order.setdefault(stage)
                pairs.setdefault((sha1, stage))
        for stage in self._stages_seen:
            order.setdefault(stage)
        for pair in self._attempts:
            pairs.setdefault(pair)
        stages = {stage: StageCounts(stage) for stage in order}
        for stage, counts in stages.items():
            calls = self._stage_calls.get(stage) or Counter()
            counts.engine, counts.cached = calls["engine"], calls["cached"]

        latest = _latest_invocation(manifest)
        remaining: Counter = Counter()
        rates: dict[tuple[str, str | None], _Rate] = {}
        for sha1, stage in pairs:
            counts = stages[stage]
            attempt = self._attempts.get((sha1, stage)) or _Attempt()
            if attempt.ended:
                rate = rates.setdefault((stage, kinds.get(sha1)), _Rate())
                rate.attempts += attempt.ended
                rate.engine += attempt.ended_engine
                rate.calls += attempt.ended_calls
            status = attempt.status
            if status == RUNNING and _interrupted(attempt, latest):
                counts.pending += 1
                counts.interrupted += 1
                remaining[(stage, kinds.get(sha1))] += 1
            elif status == RUNNING:
                counts.running += 1
                remaining[(stage, kinds.get(sha1))] += 1
                summary.in_flight.append(
                    InFlight(sha1, kinds.get(sha1), stage, attempt.since)
                )
            elif status == PENDING:
                counts.pending += 1
                counts.held += attempt.held
                remaining[(stage, kinds.get(sha1))] += 1
            elif status == FAILED:
                counts.failed += 1
                counts.failures[attempt.failure or "error"] += 1
            elif status == NEEDS_ATTENTION:
                counts.attention += 1
                counts.attention_reasons.update(attempt.attention)
            elif attempt.how == MADE:
                counts.done += 1
            else:
                counts.reused[attempt.how] += 1
        summary.stages = list(stages.values())
        summary.in_flight.sort(
            key=lambda item: (item.since or now, item.stage, item.sha1)
        )
        summary.remaining_calls, summary.unrated = self._estimate(
            remaining, rates
        )

        summary.alerts = [
            _alert_line(alert, kinds)
            for alert in reversed(self._latest_alerts)
        ]
        summary.alert_counts = Counter(self._alert_counts)
        summary.alert_checks = Counter(self._alert_checks)
        summary.slices = self._slices(
            list(order), sorted({kind for kind in kinds.values() if kind})
        )
        return summary

    def _invocations(self, summary: Summary, now: datetime) -> None:
        manifest = self._manifest
        invocations = [
            entry
            for entry in manifest.get("invocations") or []
            if isinstance(entry, dict)
        ]
        summary.invocations = len(invocations)
        summary.started_at = _time(manifest.get("started_at"))
        if not invocations:
            # a run.json from before invocations were recorded: the last
            # marker event says whether the run is going
            summary.state = {
                RUN_START: "running",
                RUN_END: "finished",
                RUN_INTERRUPTED: "interrupted",
            }.get(self._marker or "", "not started")
            return
        total = 0.0
        for index, entry in enumerate(invocations):
            start = _time(entry.get("started_at"))
            if start is None:
                continue
            end = _time(entry.get("ended_at"))
            if end is None and index == len(invocations) - 1:
                end = now
            elif end is None:
                # killed: its last event is as far as it is known to have run
                end = self._last_at.get(str(entry.get("invocation"))) or start
            span = max(0.0, (end - start).total_seconds())
            total += span
            if index == len(invocations) - 1:
                summary.invocation_elapsed = span
        summary.elapsed = total
        latest = invocations[-1]
        if latest.get("ended_at"):
            summary.state = _string(latest.get("outcome")) or "ended"
            summary.reason = _string(latest.get("reason"))
        else:
            summary.state = "running"

    def _estimate(
        self, remaining: Counter, rates: dict[tuple[str, str | None], _Rate]
    ) -> tuple[int | None, list[str]]:
        """The engine calls the remaining stages will take, at the run's
        rates so far: each stage of each kind at the engine calls its
        ended attempts made on average, since kinds differ (a text file's
        transcription makes no call, a scan's does). The stages of kinds
        with no ended attempt yet are listed apart ('critique (media)').
        None when the run shows no calls at all: no cache events, and no
        call events."""
        by_cache = self._calls.cache_events > 0
        if not by_cache and not self._calls.logged:
            return None, []
        estimate = 0.0
        unrated: list[str] = []
        for (stage, kind), count in sorted(
            remaining.items(), key=lambda item: (item[0][0], item[0][1] or "")
        ):
            rate = rates.get((stage, kind))
            if rate is None or not rate.attempts:
                unrated.append(f"{stage} ({kind or '?'})")
                continue
            made = rate.engine if by_cache else rate.calls
            estimate += count * made / rate.attempts
        return round(estimate), unrated

    def _slices(self, stages: list[str], kinds: list[str]) -> list[SliceState]:
        """Every slice the operator can stop: the run, each stage and each
        kind the run knows, and any other scope a current request names;
        each with its state (rundir.fold_control) and the request that
        stopped it. Requests before the latest invocation's start marker
        no longer apply."""
        state = fold_control(self._requests).scopes
        current: list[dict] = []
        for entry in self._requests:
            if entry.get("action") == "start":
                current = []
            else:
                current.append(entry)
        scopes: dict[str, None] = {"run": None}
        for stage in stages:
            scopes.setdefault(f"stage:{stage}")
        for kind in kinds:
            scopes.setdefault(f"kind:{kind}")
        for entry in current:
            scope = entry.get("scope") or "run"
            if isinstance(scope, str):
                scopes.setdefault(scope)
        slices = []
        for scope in scopes:
            status = state.get(scope)
            if status is None:
                slices.append(SliceState(scope, ACTIVE))
            else:
                since, by, reason = _stopped(current, scope)
                slices.append(SliceState(scope, status, since, by, reason))
        return slices


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _stopped(
    requests: list[dict], scope: str
) -> tuple[datetime | None, str | None, str | None]:
    """Since when a stopped scope has been stopped, and who stopped it and
    why, from the requests since the invocation started, in the order
    rundir.fold_control applies them: a pause stops a scope until a
    resume, and another pause meanwhile gives a new reason, not a new
    start; an abort stops it for good, and what follows it changes
    nothing."""
    since: datetime | None = None
    by = reason = None
    state = ACTIVE
    for entry in requests:
        if (entry.get("scope") or "run") != scope or state == ABORTED:
            continue
        action = entry.get("action")
        if action in (PAUSE, ABORT):
            if state == ACTIVE or action == ABORT:
                since = _time(entry.get("at"))
            state = ABORTED if action == ABORT else PAUSED
            by, reason = _string(entry.get("by")), _string(entry.get("reason"))
        elif action == RESUME:
            since, by, reason, state = None, None, None, ACTIVE
    return since, by, reason


def _latest_invocation(manifest: dict) -> dict | None:
    invocations = [
        entry
        for entry in manifest.get("invocations") or []
        if isinstance(entry, dict)
    ]
    return invocations[-1] if invocations else None


def _interrupted(attempt: _Attempt, latest: dict | None) -> bool:
    """Whether a started stage was left by an invocation that has ended,
    so that nothing runs it now: the next invocation starts it again."""
    if latest is None:
        return False
    if latest.get("ended_at"):
        return True
    running = latest.get("invocation")
    return attempt.invocation is not None and attempt.invocation != running


def _alert_line(alert: dict, kinds: dict[str, str | None]) -> AlertLine:
    subject = prefix(alert.get("subject"))
    kind = None
    if subject != "-":
        found = {
            kinds[sha1] for sha1 in kinds if sha1.lower().startswith(subject)
        }
        kind = found.pop() if len(found) == 1 else None
    return AlertLine(
        at=_time(alert.get("at")),
        status=_string(alert.get("status")) or "?",
        stage=_string(alert.get("stage")) or "?",
        check=_string(alert.get("check")) or "?",
        subject=subject,
        kind=kind,
        value=_value(alert.get("value")),
    )


def summarise_run(path: Path | str, now: datetime | None = None) -> Summary:
    """A run directory's summary, read once."""
    monitor = RunMonitor(path)
    monitor.refresh()
    return monitor.summary(now)


# -- the terminal view --------------------------------------------------------

_STYLES = {
    "title": "\x1b[1m",
    "head": "\x1b[1m",
    "good": "\x1b[32m",
    "warn": "\x1b[33m",
    "bad": "\x1b[31m",
    "dim": "\x1b[2m",
}
_RESET = "\x1b[0m"
HOME = "\x1b[H"
CLEAR_SCREEN = "\x1b[2J"
CLEAR_LINE = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

_COLUMNS = (
    ("pending", 8),
    ("running", 8),
    ("done", 7),
    ("reused", 7),
    ("attention", 10),
    ("failed", 7),
    ("engine", 8),
    ("cached", 8),
)

HELP = (
    "commands: pause|resume|abort [run | stage NAME | kind NAME] [reason]",
    "  e.g. 'pause stage extraction checking the prompt', 'abort kind media'",
    "  a pause holds until a resume; an abort holds for the rest of the "
    "invocation, and invoking the run again resumes it",
    "keys: ':' or Enter types a command; 'q' quits the monitor (the run "
    "goes on)",
)


def _state_style(state: str) -> str:
    if state in ("running", "finished"):
        return "good"
    if state in ("failed", "refused", "interrupted"):
        return "bad"
    return "warn"


def _header(summary: Summary) -> list[tuple[str, str]]:
    title = f"yizkr run {summary.run_id or '?'}"
    if summary.mode:
        title += f"  ({summary.mode}"
        if summary.project_stage:
            title += f", {summary.project_stage} stage"
        title += ")"
    state = summary.state + (f": {summary.reason}" if summary.reason else "")
    run = summary.slice("run")
    if run.state != ACTIVE:
        state += f"; run {run.state}"
    parts = [state]
    if summary.invocations:
        parts.append(f"invocation {summary.invocations}")
    if summary.elapsed is not None:
        elapsed = f"elapsed {_duration(summary.elapsed)}"
        if summary.invocations > 1:
            elapsed += f" (this one {_duration(summary.invocation_elapsed)})"
        parts.append(elapsed)
    if summary.last_event_at is not None:
        age = (summary.now - summary.last_event_at).total_seconds()
        parts.append(f"last event {_duration(age)} ago")
    kinds = _counts(summary.kinds)
    plan = (
        f"started {_stamp(summary.started_at)} | "
        f"{_n(summary.documents)} documents" + (f": {kinds}" if kinds else "")
    )
    return [
        (title, "title"),
        (" | ".join(parts), _state_style(summary.state)),
        (plan, ""),
    ]


def _stage_values(stage: StageCounts) -> tuple[int, ...]:
    """A stage's row, in the order of _COLUMNS."""
    return (
        stage.pending,
        stage.running,
        stage.done,
        sum(stage.reused.values()),
        stage.attention,
        stage.failed,
        stage.engine,
        stage.cached,
    )


def _stage_rows(summary: Summary) -> list[tuple[str, str]]:
    if not summary.stages:
        return [("no stages planned or seen yet", "dim")]
    width = max(14, *(len(stage.name) + 1 for stage in summary.stages))
    head = "stage".ljust(width) + "".join(
        name.rjust(size) for name, size in _COLUMNS
    )
    rows = [(head, "head")]
    for stage in summary.stages:
        line = stage.name.ljust(width) + "".join(
            _n(value).rjust(size)
            for value, (_, size) in zip(
                _stage_values(stage), _COLUMNS, strict=True
            )
        )
        style = "bad" if stage.failed else "warn" if stage.attention else ""
        rows.append((line, style))
    return rows


def _stage_notes(summary: Summary) -> list[tuple[str, str]]:
    """What the stage table's counts are made of: reuse by reason,
    attention and failure labels, and pending stages that are held or
    were interrupted."""
    notes: list[tuple[str, str]] = []
    for stage in summary.stages:
        name = stage.name
        if stage.reused:
            notes.append((f"reused     {name}: {_counts(stage.reused)}", ""))
        if stage.attention_reasons:
            notes.append(
                (
                    f"attention  {name}: {_counts(stage.attention_reasons)}",
                    "warn",
                )
            )
        if stage.failures:
            notes.append(
                (f"failed     {name}: {_counts(stage.failures)}", "bad")
            )
        held = []
        if stage.held:
            held.append(f"{_n(stage.held)} held by the control file")
        if stage.interrupted:
            held.append(
                f"{_n(stage.interrupted)} interrupted (the next invocation "
                "starts them again)"
            )
        if held:
            notes.append((f"pending    {name}: {', '.join(held)}", "warn"))
    return notes


def _call_rows(summary: Summary, detail: bool) -> list[tuple[str, str]]:
    calls = summary.calls
    rows: list[tuple[str, str]] = []
    engine = calls.to_engine
    if engine is None and not calls.cache_events:
        rows.append(("calls      none recorded yet", "dim"))
    else:
        line = f"calls      {_n(engine or 0)} to the engine"
        if calls.cache_events:
            answered = calls.engine + calls.cached
            share = f" ({100 * calls.cached // answered}%)" if answered else ""
            line += f" | {_n(calls.cached)} from the cache{share}"
            if calls.hit_reasons:
                line += f": {_counts(calls.hit_reasons)}"
        rows.append((line, ""))
        other = [
            f"{_n(count)} {label}"
            for count, label in (
                (calls.resumed, "resumed"),
                (calls.replayed, "replayed"),
                (calls.rejected, "rejected"),
                (calls.adopted, "adopted"),
            )
            if count
        ]
        if detail and other:
            rows.append(("  cache    " + " | ".join(other), "dim"))
        if detail and calls.roles:
            roles = " | ".join(
                f"{role} {_n(counts['engine'])} engine, "
                f"{_n(counts['cached'])} cached"
                for role, counts in sorted(calls.roles.items())
            )
            rows.append(("  by role  " + roles, "dim"))
    if summary.remaining_calls is not None:
        line = (
            f"  to go    ~{_n(summary.remaining_calls)} engine calls at the "
            "rates so far"
        )
        if summary.unrated:
            line += f", besides {', '.join(summary.unrated)}: no rate yet"
        rows.append((line, ""))
    if calls.logged:
        parts = [f"{_n(calls.logged)} calls"]
        if calls.timed:
            parts.append(f"mean {calls.seconds / calls.timed:.1f}s")
        other = {
            outcome: count
            for outcome, count in calls.outcomes.items()
            if outcome not in ("ok", "?")
        }
        if other:
            parts.append(_counts(other))
        if calls.waits:
            parts.append(
                f"{_n(calls.waits)} rate-limit waits, "
                f"{_duration(calls.waited)} in all"
            )
        rows.append(("  logged   " + " | ".join(parts), ""))
    return rows


def _in_flight_rows(summary: Summary, limit: int) -> list[tuple[str, str]]:
    rows = [(f"in flight  {_n(len(summary.in_flight))}", "head")]
    for item in summary.in_flight[:limit]:
        age = (
            _duration((summary.now - item.since).total_seconds())
            if item.since
            else "?"
        )
        rows.append(
            (
                f"  {prefix(item.sha1):<{PREFIX}}  {item.kind or '?':<13}"
                f"{item.stage:<15} for {age}",
                "",
            )
        )
    hidden = len(summary.in_flight) - limit
    if hidden > 0:
        rows.append((f"  ... and {_n(hidden)} more", "dim"))
    return rows


def _slice_rows(summary: Summary, detail: bool) -> list[tuple[str, str]]:
    stopped = [item for item in summary.slices if item.state != ACTIVE]
    active = [item.scope for item in summary.slices if item.state == ACTIVE]
    if not stopped:
        return [("slices     all active", "")]
    rows = [("slices", "head")]
    for item in stopped:
        line = f"  {item.scope:<22} {item.state.upper()}"
        if item.since:
            line += (
                f" {_duration((summary.now - item.since).total_seconds())} ago"
            )
        if item.by:
            line += f" by {item.by}"
        if item.reason:
            line += f": {_clip(item.reason, 80)}"
        rows.append((line, "bad" if item.state == ABORTED else "warn"))
    if detail and active:
        rows.append((f"  active: {', '.join(active)}", "dim"))
    return rows


def _alert_rows(summary: Summary, limit: int) -> list[tuple[str, str]]:
    counts = summary.alert_counts
    if not counts:
        return [("alerts     none", "")]
    head = f"alerts     {_counts(counts)}"
    if summary.alert_checks:
        head += f" | by check: {_counts(summary.alert_checks)}"
    rows = [(head, "head")]
    for alert in summary.alerts[:limit]:
        rows.append(
            (
                f"  {_stamp(alert.at, '%H:%M:%S')}  {alert.status:<8}"
                f"{alert.stage:<15}{alert.check:<24}{alert.subject:<{PREFIX}}"
                f" {alert.kind or '?':<13}{alert.value}",
                "warn" if alert.status == "new" else "",
            )
        )
    return rows


# how the terminal view shrinks to fit: (how many in-flight documents and
# alerts it lists, whether it shows the stage notes, whether it shows the
# other details); the first level that fits is drawn
_LEVELS = (
    (8, True, True),
    (4, True, True),
    (2, True, True),
    (2, False, True),
    (2, False, False),
    (0, False, False),
)


def _body(summary: Summary, level: int) -> list[tuple[str, str]]:
    limit, notes, detail = _LEVELS[level]
    body = [*_header(summary), ("", ""), *_stage_rows(summary)]
    if notes:
        body += [(f"  {text}", style) for text, style in _stage_notes(summary)]
    body += [
        ("", ""),
        *_call_rows(summary, detail),
        ("", ""),
        *_in_flight_rows(summary, limit),
        ("", ""),
        *_slice_rows(summary, detail),
        ("", ""),
        *_alert_rows(summary, limit),
    ]
    if detail and summary.set_aside:
        body.append(
            (
                "set aside to be made again (set-aside events): "
                f"{_n(summary.set_aside)}",
                "dim",
            )
        )
    if detail and summary.skipped_lines:
        body.append(
            (
                f"unreadable log lines skipped: {_n(summary.skipped_lines)}",
                "dim",
            )
        )
    return body


def render_lines(
    summary: Summary,
    *,
    width: int = 100,
    height: int | None = None,
    color: bool = False,
    footer: Sequence[str] = (),
) -> list[str]:
    """The terminal view as lines, each at most `width` characters wide
    (ANSI styles aside, with `color`), and at most `height` of them, the
    `footer` lines last. To fit, the in-flight and alert lists shrink
    first, then the stage notes and other details go, and then the view is
    cut short above the footer."""
    tail = [("", ""), *((line, "dim") for line in footer)] if footer else []
    room = None if height is None else max(1, height - len(tail))
    if not summary.found:
        body = [
            (
                f"waiting for {summary.path / MANIFEST}: no run has started "
                "in this directory yet",
                "warn",
            )
        ]
    else:
        for level in range(len(_LEVELS)):
            body = _body(summary, level)
            if room is None or len(body) <= room:
                break
    if room is not None:
        body = body[:room]
    plain = body + tail
    if height is not None:
        plain = plain[: max(1, height)]
    lines = []
    for text, style in plain:
        text = _clip(text, max(1, width))
        if color and style in _STYLES and text:
            text = f"{_STYLES[style]}{text}{_RESET}"
        lines.append(text)
    return lines


def render_terminal(
    summary: Summary,
    *,
    width: int = 100,
    height: int | None = None,
    color: bool = False,
    footer: Sequence[str] = (),
) -> str:
    """The terminal view as text: render_lines, one per line."""
    return "\n".join(
        render_lines(
            summary, width=width, height=height, color=color, footer=footer
        )
    )


# -- the status page ----------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fbfbfa;
  --muted: #6b6b70; --line: #deded9; --warn: #8a5a00; --bad: #b3261e;
  --good: #1f6f43; }
@media (prefers-color-scheme: dark) { :root { --fg: #e8e8e6;
  --bg: #161617; --muted: #a0a0a6; --line: #333336; --warn: #e0a64a;
  --bad: #f2807a; --good: #6fcf97; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 16px; color: var(--fg);
  background: var(--bg); font: 15px/1.45 system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 24px 0 8px; }
p { margin: 4px 0; }
.muted, .dim { color: var(--muted); }
.good { color: var(--good); } .warn { color: var(--warn); }
.bad { color: var(--bad); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%;
  font-variant-numeric: tabular-nums; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--line);
  text-align: left; white-space: nowrap; }
td.num, th.num { text-align: right; }
code { font: 13px ui-monospace, monospace; }
ul { margin: 4px 0; padding-left: 20px; }
footer { margin-top: 28px; font-size: 13px; color: var(--muted); }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _table(head: Sequence[str], rows: list[list[str]], numeric=()) -> str:
    cells = "".join(
        f'<th class="num">{_e(name)}</th>'
        if index in numeric
        else f"<th>{_e(name)}</th>"
        for index, name in enumerate(head)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell}</td>'
            if index in numeric
            else f"<td>{cell}</td>"
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{cells}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def render_html(summary: Summary, *, reload: int = PAGE_RELOAD) -> str:
    """The status page: one self-contained HTML document (inline style,
    no script, no external asset) that reloads itself every `reload`
    seconds. Documents appear by SHA-1 prefix and kind only."""
    title = f"yizkr run {summary.run_id or '?'}"
    parts = [f"<h1>{_e(title)}</h1>"]
    for text, style in _header(summary)[1:]:
        parts.append(f'<p class="{_e(style)}">{_e(text)}</p>')

    rows = [
        [_e(stage.name), *(_e(_n(value)) for value in _stage_values(stage))]
        for stage in summary.stages
    ]
    parts.append("<h2>Stages</h2>")
    if rows:
        parts.append(
            _table(
                ["stage", *(name for name, _ in _COLUMNS)],
                rows,
                numeric=range(1, len(_COLUMNS) + 1),
            )
        )
        notes = [
            f'<li class="{_e(style)}">{_e(" ".join(text.split()))}</li>'
            for text, style in _stage_notes(summary)
        ]
        if summary.set_aside:
            notes.append(
                '<li class="muted">set aside to be made again (set-aside '
                f"events): {_e(_n(summary.set_aside))}</li>"
            )
        if summary.skipped_lines:
            notes.append(
                '<li class="muted">unreadable log lines skipped: '
                f"{_e(_n(summary.skipped_lines))}</li>"
            )
        if notes:
            parts.append(f"<ul>{''.join(notes)}</ul>")
    else:
        parts.append('<p class="muted">No stages planned or seen yet.</p>')

    parts.append("<h2>Calls</h2>")
    parts += [
        f'<p class="{_e(style)}">{_e(" ".join(text.split()))}</p>'
        for text, style in _call_rows(summary, detail=True)
    ]

    parts.append(f"<h2>In flight ({_e(_n(len(summary.in_flight)))})</h2>")
    if summary.in_flight:
        parts.append(
            _table(
                ["document", "kind", "stage", "for"],
                [
                    [
                        f"<code>{_e(prefix(item.sha1))}</code>",
                        _e(item.kind or "?"),
                        _e(item.stage),
                        _e(
                            _duration(
                                (summary.now - item.since).total_seconds()
                            )
                            if item.since
                            else "?"
                        ),
                    ]
                    for item in summary.in_flight
                ],
            )
        )
    else:
        parts.append('<p class="muted">Nothing is running.</p>')

    parts.append("<h2>Slices</h2>")
    parts.append(
        _table(
            ["slice", "state", "since", "by", "reason"],
            [
                [
                    f"<code>{_e(item.scope)}</code>",
                    '<span class="{}">{}</span>'.format(
                        "good"
                        if item.state == ACTIVE
                        else "bad"
                        if item.state == ABORTED
                        else "warn",
                        _e(item.state),
                    ),
                    _e(_stamp(item.since, "%H:%M:%S") if item.since else ""),
                    _e(item.by or ""),
                    _e(_clip(item.reason or "", 120)),
                ]
                for item in summary.slices
            ],
        )
    )

    parts.append("<h2>Alerts</h2>")
    if summary.alert_counts:
        head = _counts(summary.alert_counts)
        if summary.alert_checks:
            head += f"; by check: {_counts(summary.alert_checks)}"
        parts.append(f"<p>{_e(head)}</p>")
        parts.append(
            _table(
                [
                    "at",
                    "status",
                    "stage",
                    "check",
                    "document",
                    "kind",
                    "value",
                ],
                [
                    [
                        _e(_stamp(alert.at, "%H:%M:%S")),
                        '<span class="{}">{}</span>'.format(
                            "warn" if alert.status == "new" else "muted",
                            _e(alert.status),
                        ),
                        _e(alert.stage),
                        _e(alert.check),
                        f"<code>{_e(alert.subject)}</code>",
                        _e(alert.kind or "?"),
                        _e(alert.value),
                    ]
                    for alert in summary.alerts
                ],
            )
        )
    else:
        parts.append('<p class="muted">No alerts.</p>')

    parts.append(
        "<footer>"
        f"Generated {_e(_stamp(summary.now, '%Y-%m-%d %H:%M:%S UTC'))} by the "
        "yizkr run monitor; reloads every "
        f"{_e(reload)} s while the monitor runs. Local-only: this page stays "
        "on this machine, and is never published or sent to a cloud "
        "session. Documents appear by SHA-1 prefix and kind only."
        "</footer>"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n'
        f'<meta http-equiv="refresh" content="{int(reload)}">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n"
        "<body>\n" + "\n".join(parts) + "\n</body>\n</html>\n"
    )


def write_status_page(
    path: Path | str, summary: Summary, *, reload: int = PAGE_RELOAD
) -> Path | None:
    """Write the status page into the run directory, whole (atomically),
    and return its path; None, writing nothing, when the directory holds
    no run.json."""
    path = Path(path)
    if not (path / MANIFEST).is_file():
        return None
    page = path / STATUS_PAGE
    atomic_write_text(page, render_html(summary, reload=reload))
    return page


# -- control requests ---------------------------------------------------------


class NotARunDirectory(ValueError):
    """A control request for a directory that holds no run.json."""


def request(
    path: Path | str,
    action: str,
    *,
    stage: str | None = None,
    kind: str | None = None,
    reason: str = "",
    by: str = MONITOR,
) -> dict:
    """Ask the run's driver to pause, resume or abort the run, or one
    slice of it (a stage, or a document kind), through its control file
    (RunDir.request). Refused for a directory without run.json, and for an
    empty stage or kind name, which would name the whole run."""
    path = Path(path)
    if not (path / MANIFEST).is_file():
        raise NotARunDirectory(
            f"{path}: no {MANIFEST}; is it a run directory?"
        )
    for label, name in (("stage", stage), ("kind", kind)):
        if name is not None and not name.strip():
            raise ValueError(f"an empty {label} name")
    return RunDir(path).request(
        action, stage=stage, kind=kind, by=by, reason=reason
    )


class CommandError(ValueError):
    """A command the monitor does not understand."""


@dataclass(frozen=True)
class Command:
    action: str
    stage: str | None = None
    kind: str | None = None
    reason: str = ""

    @property
    def scope(self) -> str:
        if self.stage:
            return f"stage:{self.stage}"
        if self.kind:
            return f"kind:{self.kind}"
        return "run"


def parse_command(line: str) -> Command | None:
    """`pause|resume|abort [run | stage NAME | kind NAME] [reason ...]`
    (a slice may also be written `stage:NAME` or `kind:NAME`), `help` or
    `quit`; None for an empty line."""
    words = line.split()
    if not words:
        return None
    verb = words[0].lower()
    if verb in ("q", "quit", "exit"):
        return Command("quit")
    if verb in ("?", "h", "help"):
        return Command("help")
    if verb not in CONTROL_ACTIONS:
        raise CommandError(
            f"unknown command {words[0]!r}: pause, resume, abort, help or quit"
        )
    rest = words[1:]
    stage = kind = None
    if rest:
        head = rest[0].lower()
        if head == "run":
            rest = rest[1:]
        elif head in ("stage", "kind"):
            if len(rest) < 2:
                raise CommandError(f"name the {head}: {verb} {head} NAME")
            name, rest = rest[1], rest[2:]
            stage, kind = (name, None) if head == "stage" else (None, name)
        elif head.startswith(("stage:", "kind:")):
            which, _, name = rest[0].partition(":")
            if not name:
                raise CommandError(
                    f"name the {which.lower()}: {verb} {head}NAME"
                )
            rest = rest[1:]
            stage, kind = (
                (name, None) if which.lower() == "stage" else (None, name)
            )
        else:
            raise CommandError(
                f"say what to {verb}: run, stage NAME or kind NAME, then any "
                "reason"
            )
    return Command(verb, stage, kind, " ".join(rest))


@dataclass(frozen=True)
class CommandResult:
    message: str
    quit: bool = False
    requested: dict | None = None


def handle_command(
    path: Path | str,
    line: str,
    *,
    confirm: Callable[[str], bool] | None = None,
    summary: Summary | None = None,
    by: str = MONITOR,
) -> CommandResult:
    """Carry out one typed command. A pause, resume or abort is appended
    to the run's control file (`request`), and the result says what the
    scope's state is then. An abort asks `confirm` first, when given. A
    resume of an aborted scope is not written: an abort holds for the
    rest of the invocation. With `summary`, a stage or kind the run does
    not know is written but noted, since it stops nothing the run plans."""
    path = Path(path)
    try:
        command = parse_command(line)
    except CommandError as exc:
        return CommandResult(str(exc))
    if command is None:
        return CommandResult("")
    if command.action == "quit":
        return CommandResult("", quit=True)
    if command.action == "help":
        return CommandResult(" | ".join(line.strip() for line in HELP))
    scope = command.scope
    before = RunDir(path).control().scopes.get(scope)
    if command.action == RESUME and before == ABORTED:
        return CommandResult(
            f"{scope} is aborted for the rest of this invocation, and a "
            "resume does not lift an abort: invoke the run again to resume "
            "it"
        )
    if command.action == ABORT and confirm is not None:
        question = (
            f"abort {scope} for the rest of this invocation? Completed work "
            "is kept, and invoking the run again resumes it. [y/N] "
        )
        if not confirm(question):
            return CommandResult(f"not aborted: {scope}")
    try:
        entry = request(
            path,
            command.action,
            stage=command.stage,
            kind=command.kind,
            reason=command.reason,
            by=by,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(f"not requested: {exc}")
    after = RunDir(path).control().scopes.get(scope, ACTIVE)
    message = f"{command.action} requested for {scope}: now {after}"
    if summary is not None and scope != "run":
        known = {item.scope for item in summary.slices}
        if scope not in known:
            message += " (no document the run knows has this slice)"
    return CommandResult(message, requested=entry)


# -- watching -----------------------------------------------------------------


class Keys(Protocol):
    """Where the watch loop's key presses and typed commands come from."""

    def __enter__(self) -> Keys: ...

    def __exit__(self, *exc: object) -> None: ...

    def wait(self, timeout: float) -> str | None:
        """A key pressed within `timeout` seconds, or None."""

    def prompt(self, text: str) -> str | None:
        """A line typed after `text`, or None."""


class NoKeys:
    """No input: the loop only waits between refreshes."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def __enter__(self) -> NoKeys:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def wait(self, timeout: float) -> str | None:
        self._sleep(timeout)
        return None

    def prompt(self, text: str) -> str | None:
        return None


class TerminalKeys:
    """Key presses from a terminal as they are typed (cbreak mode), and a
    line typed at a prompt, with echo; the terminal's settings are put
    back on the way out, however the loop ends."""

    def __init__(self, stream: TextIO, out: TextIO) -> None:
        self.fd = stream.fileno()
        self.out = out
        self._saved: list | None = None

    def __enter__(self) -> TerminalKeys:
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc: object) -> None:
        self._restore()

    def _restore(self) -> None:
        import termios

        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def wait(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        data = os.read(self.fd, 64)
        if not data:
            # the input has closed: wait out the interval instead
            time.sleep(max(0.0, timeout))
            return None
        return data.decode("utf-8", errors="replace")[:1]

    def prompt(self, text: str) -> str | None:
        import tty

        self._restore()
        self.out.write(f"{SHOW_CURSOR}\r\n{text}")
        self.out.flush()
        try:
            data = os.read(self.fd, 4096)
        finally:
            tty.setcbreak(self.fd)
            self.out.write(HIDE_CURSOR)
            self.out.flush()
        return data.decode("utf-8", errors="replace").strip() or None


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _default_keys(out: TextIO) -> Keys:
    stdin = sys.stdin
    if _is_tty(stdin) and _is_tty(out):
        try:
            import termios  # noqa: F401
        except ImportError:
            return NoKeys()
        return TerminalKeys(stdin, out)
    return NoKeys()


def watch(
    path: Path | str,
    *,
    interval: float = 2.0,
    once: bool = False,
    page: bool = True,
    out: TextIO | None = None,
    keys: Keys | None = None,
    clock: Callable[[], datetime] | None = None,
    size: Callable[[], tuple[int, int]] | None = None,
    terminal: bool | None = None,
) -> int:
    """Follow a run directory until the operator quits (`q`, or Ctrl-C):
    refresh every `interval` seconds, draw the terminal view in place, and
    write the status page into the run directory (`page`), at most every
    PAGE_MIN_INTERVAL seconds while events arrive and at least every
    PAGE_MAX_AGE seconds. The run goes on whatever the monitor does.

    ':' or Enter opens a command line (handle_command): pause, resume or
    abort the run or a slice. On an output that is not a terminal the view
    is printed afresh whenever the run changes, without styles, and
    commands are not read. `once` prints one view, writes the page and
    returns. `keys`, `clock`, `size` (columns, rows) and `terminal`
    (whether `out` is one) stand in for the terminal in tests."""
    path = Path(path)
    out = out or sys.stdout
    clock = clock or (lambda: datetime.now(UTC))
    terminal = _is_tty(out) if terminal is None else terminal
    color = (
        terminal
        and "NO_COLOR" not in os.environ
        and (os.environ.get("TERM") != "dumb")
    )

    def dimensions() -> tuple[int, int]:
        if size is not None:
            return size()
        columns, rows = shutil.get_terminal_size((100, 40))
        return columns, rows

    monitor = RunMonitor(path)
    page_version = None
    page_written = 0.0
    page_note = ""
    message = ""
    shown_version = None
    first = True
    if keys is None:
        keys = NoKeys() if once else _default_keys(out)
    hint = (
        "Ctrl-C: quit (keys are read only from a terminal)"
        if isinstance(keys, NoKeys)
        else "':' or Enter: pause|resume|abort [run|stage NAME|kind NAME] "
        "[reason]   q: quit"
    )
    try:
        with keys:
            while True:
                monitor.refresh()
                summary = monitor.summary(clock())
                elapsed = time.monotonic() - page_written
                if (
                    page
                    and summary.found
                    and (
                        page_version is None
                        or (
                            monitor.version != page_version
                            and elapsed >= PAGE_MIN_INTERVAL
                        )
                        or elapsed >= PAGE_MAX_AGE
                    )
                ):
                    try:
                        written = write_status_page(path, summary)
                    except OSError as exc:
                        page_note = f"status page not written: {exc}"
                    else:
                        if written is not None:
                            page_note = (
                                f"status page: {written} (written "
                                f"{_stamp(summary.now, '%H:%M:%S')})"
                            )
                    page_version = monitor.version
                    page_written = time.monotonic()
                footer = [line for line in (page_note, message) if line]
                if once:
                    columns, _ = dimensions()
                    out.write(
                        render_terminal(summary, width=columns, footer=footer)
                        + "\n"
                    )
                    out.flush()
                    return 0
                if terminal:
                    columns, rows = dimensions()
                    footer.append(hint)
                    lines = render_lines(
                        summary,
                        width=columns,
                        height=max(1, rows - 1),
                        color=color,
                        footer=footer,
                    )
                    frame = (
                        HIDE_CURSOR + CLEAR_SCREEN if first else ""
                    ) + HOME
                    frame += "".join(
                        line + CLEAR_LINE + "\n" for line in lines
                    )
                    out.write(frame + CLEAR_BELOW)
                    out.flush()
                    first = False
                elif monitor.version != shown_version:
                    columns, _ = dimensions()
                    out.write(
                        render_terminal(summary, width=columns, footer=footer)
                        + "\n\n"
                    )
                    out.flush()
                shown_version = monitor.version
                key = keys.wait(interval)
                if key is None:
                    continue
                if key in ("q", "Q"):
                    return 0
                if key in (":", "\r", "\n"):
                    line = keys.prompt("command> ")
                    if line is None:
                        continue

                    def confirm(question: str) -> bool:
                        answer = keys.prompt(question) or ""
                        return answer.strip().lower() in ("y", "yes")

                    result = handle_command(
                        path, line, confirm=confirm, summary=summary
                    )
                    if result.quit:
                        return 0
                    message = result.message
                    # the request shows at once, not after the interval
                    shown_version = None
                elif key in ("?", "h"):
                    message = " | ".join(line.strip() for line in HELP)
                    shown_version = None
    except KeyboardInterrupt:
        return 0
    finally:
        if terminal and not once:
            out.write(SHOW_CURSOR + "\n")
            out.flush()


# -- a command line -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m yizkr.pipeline.monitor watch RUN` follows a run;
    `... pause|resume|abort RUN [--stage NAME | --kind NAME] [--reason
    TEXT]` asks its driver to stop or go on."""
    parser = argparse.ArgumentParser(
        prog="python -m yizkr.pipeline.monitor",
        description="Follow a run directory, or ask its driver to pause, "
        "resume or abort it or one of its slices.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    follow = commands.add_parser("watch", help="follow a run as it unfolds")
    follow.add_argument("run", type=Path, help="the run directory")
    follow.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between refreshes (default 2)",
    )
    follow.add_argument(
        "--once", action="store_true", help="print one view and exit"
    )
    follow.add_argument(
        "--no-page",
        action="store_true",
        help=f"do not write {STATUS_PAGE} into the run directory",
    )
    for action in CONTROL_ACTIONS:
        control = commands.add_parser(
            action, help=f"ask the run's driver to {action}"
        )
        control.add_argument("run", type=Path, help="the run directory")
        slice_ = control.add_mutually_exclusive_group()
        slice_.add_argument("--stage", help="only this stage")
        slice_.add_argument("--kind", help="only this document kind")
        control.add_argument("--reason", default="", help="why, for the log")
    args = parser.parse_args(argv)
    if args.command == "watch":
        if args.interval <= 0:
            parser.error("--interval must be positive")
        return watch(
            args.run,
            interval=args.interval,
            once=args.once,
            page=not args.no_page,
        )
    try:
        entry = request(
            args.run,
            args.command,
            stage=args.stage,
            kind=args.kind,
            reason=args.reason,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    state = RunDir(args.run).control().scopes.get(entry["scope"], ACTIVE)
    print(f"{args.command} requested for {entry['scope']}: now {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
