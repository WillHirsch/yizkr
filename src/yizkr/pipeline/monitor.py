"""The live run monitor: what a run is doing while it unfolds, read from its
run directory, and the operator's pause, resume and abort requests.

A monitor attaches to any run directory (yizkr.pipeline.rundir), wherever
and whenever the run was started, and follows its files as they grow:

    run.json       the plan (each document's SHA-1, kind and stages) and
                   each invocation's start, end and outcome
    events.jsonl   stage events, the LLM cache's events, the engine's call
                   events, documents that failed, and each invocation's
                   start and close
    alerts.jsonl   anomaly alerts, new or carried
    control.jsonl  pause, resume and abort requests

From them it builds a summary (RunMonitor.summary):

* per stage, how many documents are pending, running, done (made in this
  run), reused (every call answered by the LLM cache, by the answers'
  reason, or the view current, so it stands), need attention or failed;
* the documents in flight: stages the running invocation has started and
  not finished;
* engine calls and the cache's answers, what the engine reports of its
  calls (outcomes, waits and holds for the subscription), the run's
  elapsed time, and the engine calls the work still to go will take at
  the run's rates so far;
* the documents that failed in the latest invocation, by kind and reason;
* the latest alerts, tagged new or carried;
* the state of each slice (the run, each stage, each document kind):
  active, paused or aborted, with the request that stopped it, or unknown
  while control.jsonl cannot be read.

It shows the summary in two views: a terminal view refreshed in place,
in plain ANSI (`watch`), and a status page, index.html in the `status`
directory of the run directory: one self-contained HTML file, with no
external assets, that reloads itself and is written again as events
arrive. `watch` can serve that page, and nothing else, over HTTP
(serve_status), when asked to.

What the views show never quotes a document. A document appears by its
SHA-1 prefix and kind only, never by its file name or content. A name
from the pipeline's vocabulary (a stage, a kind, a check, a status) is
shown only when it is one: lowercase ASCII words. A failure shows the
exception it names, and a failure or attention reason otherwise only its
leading label, when that is such a phrase, since the details can quote a
document. An alert's value is shown when it is a number or a yes or no,
and by its type otherwise. Three fields are shown as they are written, cut
short and cleaned of control characters: a control request's reason, a
cache hit's reason, and an invocation's reason; their writers never put a
document's words in them. The page stays in the run directory under var/,
local-only data like everything there (docs/AGENT_PROTOCOL.md §6).

From the terminal view the operator asks for a pause, resume or abort of
the run or of one slice; the monitor appends the request to
control.jsonl through RunDir.request, and the driver honours it. The
monitor reads control.jsonl as the driver does (rundir's reader and
fold), so a slice it shows as stopped is one the driver stops; while the
file itself cannot be read, by the driver either, every slice's state is
unknown, and the file is tried again at every refresh. It writes nothing
else but the status page.

Reading is incremental: each refresh parses only the lines appended to
events.jsonl and alerts.jsonl since the last one, and a line not yet
ended (a writer mid-line) waits for the next refresh, so a monitor keeps
up with a long run. A log that shrinks, is replaced, or starts with other
bytes than it did starts the monitor over. A log that cannot be read, by
the driver's reader either, leaves nothing read from it standing: what
the views would show from it is unknown, and every refresh tries it
again, from its start. docs/MONITOR.md describes the views, the commands,
the events the monitor reads and serving the page.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import html
import http.server
import json
import math
import os
import re
import select
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
import unicodedata
from bisect import bisect_left
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
    DOCUMENT_FAILED,
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
    Invocations,
    RunDir,
    fold_control,
    interrupted,
    invocation_of,
    invocation_records,
)
from yizkr.pipeline.stages import read_jsonl, staging_path

# the status page, in a directory of its own in the run directory: that
# directory is all that may be served (serve_status), never the run
# directory, which holds the documents' views
STATUS_DIR = "status"
STATUS_PAGE = "index.html"
# how many hex digits of a SHA-1 name a document in the views
PREFIX = 10
# how many of the latest alerts the views list
LATEST_ALERTS = 10
# what a request the monitor writes says it came from
MONITOR = "monitor"

# one call that reached the engine, as the engine reports it: `role`,
# `stage`, `subject`, `kind`, `call`, `outcome` (ok, error, paused or
# interrupted), `duration_s`, and when there is something to say `wait_s`,
# `refused`, `held`, `error` (an exception's name), `window` and
# `resumes_at`; a run without them is shown from the cache's events alone
CALL = "call"
# how a completed stage came about: made in the run (by the engine, or by
# code that calls none), or reused: the view current, or every call the
# stage made answered by the cache, labelled by the answers' reasons
MADE = "made"
VIEW_CURRENT = "view current"
KEY_MATCH = "key match"
# what a reason's label shows when its words could be a document's
OTHER = "other"

# the cache's events that are an engine call: a miss asks the engine, and
# so does a replay of a conversation's opening turn
_ENGINE_EVENTS = ("cache-miss", "cache-replay")

# slice states: every slice's is unknown while the control file cannot be
# read, by the driver either
ACTIVE = "active"
PAUSED = "paused"
ABORTED = "aborted"
UNKNOWN = "unknown"
# why a slice's state cannot be told, as the views and the commands say it
CONTROL_UNREADABLE = "the control file cannot be read"
# why what a log holds cannot be told, as the views say it
EVENTS_UNREADABLE = f"{EVENTS} cannot be read"
ALERTS_UNREADABLE = f"{ALERTS} cannot be read"

# the status page reloads itself this often (seconds), and is written at
# most this often while events arrive, and at least this often otherwise
PAGE_RELOAD = 5
PAGE_MIN_INTERVAL = 5.0
PAGE_MAX_AGE = 30.0

# the command grammar, as the help, the key hint and the errors give it
GRAMMAR = "pause|resume|abort [run | stage NAME | kind NAME] [reason]"

# a name from the pipeline's vocabulary: lowercase ASCII words
_TOKEN = re.compile(r"[a-z0-9][a-z0-9 _.:/+%(),-]*")
# where a reason's details start, after its leading label
_DETAIL = re.compile(r"[:;(\[{=\"'<]")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_HEX = re.compile(r"[0-9a-f]{4,40}")
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{1,32}")
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")
# an exception class's name, after any module path: a failure reason
# starts with one ("RuntimeError: ...")
_CLASS_NAME = re.compile(r"(?:[a-z_][a-z0-9_]*\.)*(_?[A-Z][A-Za-z0-9]*)")
# the words exception names end in: a CamelCase name before a colon
# that ends in none of them ("McLaudler: ...") is no exception's
_EXCEPTION_ENDINGS = (
    "Error",
    "Exception",
    "Warning",
    "Exit",
    "Interrupt",
    "Failed",
    "Failure",
    "Refused",
    "Rejected",
    "Conflict",
    "Mismatch",
    "Paused",
    "Stopped",
    "Reached",
    "Changed",
    "InUse",
    "Incomplete",
    "Expired",
    "Timeout",
    "Environment",
    "Relative",
    "Prerequisite",
)


# -- what a view may show -----------------------------------------------------


def _safe(text: str) -> str:
    """`text` with every control, format, surrogate and separator
    character but the space made a space: nothing shown can move the
    cursor, break a line or fail to encode."""
    if text.isascii() and text.isprintable():
        return text
    return "".join(
        char
        if char == " " or unicodedata.category(char)[0] not in "CZ"
        else " "
        for char in text
    )


def _clean(text: str) -> str:
    """_safe, with runs of spaces made one and the ends trimmed."""
    return " ".join(_safe(text).split())


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def _token(value: Any, width: int = 40) -> str:
    """A name from the pipeline's vocabulary (a stage, a kind, a role, a
    check, a status, an outcome) as shown: itself when it is lowercase
    ASCII words, since anything else could be a document's words; "?"
    otherwise."""
    if isinstance(value, str):
        text = _clean(value)
        if len(text) <= width and _TOKEN.fullmatch(text):
            return text
    return "?"


def _verbatim(value: Any, width: int = 80) -> str | None:
    """A field shown as it is written (a control request's reason, a cache
    hit's reason, an invocation's reason), cleaned and cut short; None
    when there is nothing to show."""
    if not isinstance(value, str):
        return None
    text = _clean(value)
    return _clip(text, width) if text else None


def reason_label(reason: Any) -> str:
    """A reason's leading label, before its details start (a colon, a
    bracket, a quote, an equals sign): shown when it is a phrase of the
    pipeline's vocabulary ("no claims extracted", "its source cannot be
    read"), OTHER when it is anything else, since a reason's words can be
    a document's."""
    if isinstance(reason, str):
        head = _DETAIL.split(_clean(reason), maxsplit=1)[0].strip()
        if head and len(head) <= 60 and _TOKEN.fullmatch(head):
            return head
    return OTHER


def _exception_name(name: str) -> bool:
    """Whether `name` is an exception class's: a builtin one, or a CamelCase
    name that ends in a word exception names end in."""
    match = _CLASS_NAME.fullmatch(name) if len(name) <= 80 else None
    if match is None:
        return False
    last = match.group(1)
    builtin = getattr(builtins, last, None)
    if isinstance(builtin, type) and issubclass(builtin, BaseException):
        return True
    return any(
        last.endswith(ending) and len(last) > len(ending)
        for ending in _EXCEPTION_ENDINGS
    )


def failure_label(reason: Any) -> str:
    """How a failure is shown: the exception's name its reason starts with
    ("RuntimeError: ..." gives "RuntimeError"), else its leading label
    (reason_label): the message after either can quote a document."""
    if isinstance(reason, str):
        head = reason.split(":", 1)[0].strip()
        if _exception_name(head):
            return head
    return reason_label(reason)


def _value(value: Any) -> str:
    """An alert's measured value: a number or a yes or no as it is,
    anything else by its type alone, since text can quote a document."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.3g}" if math.isfinite(value) else str(value)
    if value is None:
        return "-"
    return {str: "(text)", list: "(list)", dict: "(object)"}.get(
        type(value), "(value)"
    )


def prefix(subject: Any) -> str:
    """A document's name in the views: the first PREFIX hex digits of its
    SHA-1; '-' for a subject that is not a whole SHA-1."""
    if isinstance(subject, str) and _SHA1.fullmatch(subject.lower()):
        return subject[:PREFIX].lower()
    return "-"


def _match(subject: Any, known: list[str]) -> str | None:
    """The one SHA-1 an alert's subject names: the subject, when it is a
    whole SHA-1, or else the only SHA-1 in `known` (sorted) it is a hex
    prefix of; None for anything else, such as a name made of the letters
    a to f."""
    if not isinstance(subject, str):
        return None
    text = subject.lower()
    if _SHA1.fullmatch(text):
        return text
    if not _HEX.fullmatch(text):
        return None
    index = bisect_left(known, text)
    if index < len(known) and known[index].startswith(text):
        after = index + 1
        if after == len(known) or not known[after].startswith(text):
            return known[index]
    return None


def _run_id(value: Any) -> str | None:
    if isinstance(value, str) and _RUN_ID.fullmatch(value):
        return value
    return _token(value) if value else None


def _suffix(file: Any) -> str:
    if isinstance(file, str):
        suffix = Path(file).suffix.lower()
        if _SUFFIX.fullmatch(suffix):
            return suffix
    return "?"


def _seconds(value: Any) -> float | None:
    """A finite, non-negative number of seconds from an event, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _time(value: Any) -> datetime | None:
    """An event's time in UTC; None for anything that is not one, or lies
    outside the years a datetime holds once in UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _duration(seconds: float | None) -> str:
    """'3m 05s'; '?' for no time, or for one that is not finite, as a sum
    of long waits can be."""
    if seconds is None or not math.isfinite(seconds):
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
    if moment is None:
        return "?"
    try:
        return moment.astimezone(UTC).strftime(spec)
    except (OverflowError, ValueError):
        return "?"


def _n(count: int) -> str:
    """'1,234'; '?' for a count too long for Python to write out, as a sum
    of the counts events give can be."""
    try:
        return f"{count:,}"
    except ValueError:
        return "?"


def _counts(counter: Counter | dict[str, int]) -> str:
    """'3 key match, 1 adopted': the most frequent first."""
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{_n(count)} {label}" for label, count in ordered)


# -- reading append-only logs -------------------------------------------------


class _Tail:
    """The complete lines appended to a JSON-lines file since the last read.

    The file is looked at as the driver's reader looks at it (read_jsonl):
    a path that holds no regular file (none yet, a directory, a FIFO) has
    no entries, and `read` raises OSError where that reader raises, when
    the file is there but cannot be looked at or read (its mode forbids
    it, say). A read that raises leaves the tail as it was.

    A line not yet ended by its newline is left for the next read, since a
    writer may be mid-line; a complete line that is not a JSON object,
    including one nested too deeply for the decoder to recurse through, is
    skipped and counted. A file that shrinks, is replaced, disappears
    after it was read, or starts with other bytes than the first line it
    was read with (one deleted and written afresh on a reused inode, or
    rewritten in place) makes `read` report a restart, and the next read
    starts from its beginning."""

    # how much of the first line is kept to tell the file by
    HEAD = 256

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.skipped = 0
        self._identity: tuple[int, int] | None = None
        self._head = b""

    def _restart(self) -> tuple[list[dict], bool]:
        self.offset, self._identity, self._head = 0, None, b""
        return [], True

    def read(self) -> tuple[list[dict], bool]:
        """(the new entries, whether the file restarted)."""
        if not self.path.is_file():
            if self._identity is None:
                return [], False
            return self._restart()
        with open(self.path, "rb") as handle:
            status = os.fstat(handle.fileno())
            identity = (status.st_dev, status.st_ino)
            if self._identity is not None and (
                identity != self._identity or status.st_size < self.offset
            ):
                return self._restart()
            if self._head and handle.read(len(self._head)) != self._head:
                return self._restart()
            if status.st_size == self.offset:
                self._identity = identity
                return [], False
            handle.seek(self.offset)
            offset, head, skipped = self.offset, self._head, 0
            entries: list[dict] = []
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                if not offset:
                    head = line[: self.HEAD]
                offset += len(line)
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line.decode("utf-8"))
                except (ValueError, RecursionError):
                    entry = None
                if isinstance(entry, dict):
                    entries.append(entry)
                else:
                    skipped += 1
        self._identity, self.offset, self._head = identity, offset, head
        self.skipped += skipped
        return entries, False


# -- the summary --------------------------------------------------------------


@dataclass
class StageCounts:
    """One stage over the run's documents. pending, running, done,
    reused, attention and failed partition its documents: a completed
    output that needs attention counts there, whether it was made or
    reused, and a stage an invocation that has ended left started counts
    as pending (interrupted, by the rule rundir.run_state reads it by too:
    the next invocation starts it again)."""

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
    kind: str
    stage: str
    since: datetime | None


@dataclass(frozen=True)
class AlertLine:
    at: datetime | None
    status: str
    stage: str
    check: str
    subject: str
    kind: str
    value: str


@dataclass(frozen=True)
class SliceState:
    """A slice ('run', 'stage:<name>' or 'kind:<name>') and whether the
    control file stops it, since when, and who stopped it and why. Its
    state is active, paused or aborted, or unknown, with nothing more,
    while the control file cannot be read."""

    scope: str
    state: str
    since: datetime | None = None
    by: str | None = None
    reason: str | None = None


@dataclass
class CallPause:
    """The latest invocation's pause for the subscription, as the engine's
    call event gives it."""

    held: str
    window: str | None
    resumes_at: datetime | None


@dataclass
class CallCounts:
    """What the run's calls came to. `engine` and `cached` count the LLM
    cache's events: a miss or a replay asked the engine, a hit answered
    from the cache. `logged` and the rest count the engine's call events,
    where it writes them."""

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
    refused: int = 0
    holds: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    pause: CallPause | None = None

    @property
    def to_engine(self) -> int | None:
        """Engine calls: from the cache's events when the run has them,
        else from call events; None when it has neither."""
        if self.cache_events:
            return self.engine
        return self.logged if self.logged else None


@dataclass
class Summary:
    """Where a run stands, ready to show: every name, label and value in
    it is as the views show it."""

    path: Path
    now: datetime
    found: bool = False
    run_id: str | None = None
    mode: str | None = None
    project_stage: str | None = None
    state: str = "waiting"
    reason: str | None = None
    statuses: Counter = field(default_factory=Counter)
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
    remaining_work: int = 0
    remaining_calls: int | None = None
    unrated: list[str] = field(default_factory=list)
    failed_documents: int = 0
    unread_documents: int = 0
    failed_by_kind: Counter = field(default_factory=Counter)
    failed_by_reason: Counter = field(default_factory=Counter)
    alerts: list[AlertLine] = field(default_factory=list)
    alert_counts: Counter = field(default_factory=Counter)
    alert_checks: Counter = field(default_factory=Counter)
    slices: list[SliceState] = field(default_factory=list)
    # the control file could not be read at the latest refresh, by the
    # driver either: every slice's state is unknown
    control_unreadable: bool = False
    # the log could not be read at the latest refresh, by the driver's
    # reader either: nothing is read from it, and what would come from it
    # is unknown, not empty
    events_unreadable: bool = False
    alerts_unreadable: bool = False
    # the slices the plan and the events know, as requests name them
    known_scopes: set[str] = field(default_factory=set)
    set_aside: int = 0
    skipped_lines: int = 0

    def stage(self, name: str) -> StageCounts:
        for counts in self.stages:
            if counts.name == name:
                return counts
        raise KeyError(name)

    def slice(self, scope: str) -> SliceState:
        """`scope`'s state as the slices give it; a scope they do not list
        is one no current request names: active, or unknown while the
        control file cannot be read."""
        for state in self.slices:
            if state.scope == scope:
                return state
        return SliceState(
            scope, UNKNOWN if self.control_unreadable else ACTIVE
        )


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
    hits: Counter | None = None
    calls: int = 0
    # the attempts that ended (done or failed; a view that stood makes no
    # call and is left out), and their calls: the rates of the estimate
    ended: int = 0
    ended_engine: int = 0
    ended_calls: int = 0

    def begin(self) -> None:
        self.engine, self.hits, self.calls = 0, None, 0

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
        return " + ".join(
            sorted(_verbatim(reason, 40) or "?" for reason in self.hits)
        )


# the state of a planned stage no event has named
_NOT_STARTED = _Attempt()


@dataclass
class _Rate:
    """Attempts at a stage that ended, and the calls they made."""

    attempts: int = 0
    engine: int = 0
    calls: int = 0


@dataclass
class _Plan:
    """What run.json plans: each document's kind and stages (a SHA-1
    planned twice, for two files with the same bytes, has the stages of
    both, and the first one's kind), the stages in the order they come,
    and the planned SHA-1s, sorted."""

    documents: dict[str, tuple[str | None, list[str]]] = field(
        default_factory=dict
    )
    order: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _plan(manifest: dict) -> _Plan:
    plan = _Plan()
    order: dict[str, None] = {}
    for document in _list(manifest.get("documents")):
        if not isinstance(document, dict):
            continue
        sha1 = document.get("sha1")
        if not isinstance(sha1, str):
            continue
        kind = document.get("kind")
        stages = [
            stage
            for stage in _list(document.get("stages"))
            if isinstance(stage, str)
        ]
        if sha1 in plan.documents:
            first_kind, first_stages = plan.documents[sha1]
            kind = first_kind
            stages = first_stages + [
                stage for stage in stages if stage not in first_stages
            ]
        plan.documents[sha1] = (
            kind if isinstance(kind, str) else None,
            stages,
        )
        for stage in stages:
            order.setdefault(stage)
    plan.order = list(order)
    plan.known = sorted(
        sha1.lower()
        for sha1 in plan.documents
        if _SHA1.fullmatch(sha1.lower())
    )
    return plan


def _foldable(request: dict) -> bool:
    """Whether the driver's fold of the control file (rundir.fold_control)
    takes a request as it is written: a start marker, whatever its scope,
    or a request whose scope is a name or is not set (an empty or false
    one is the whole run). A request whose scope is set but is not a name
    is left out: the fold fails on one it cannot hash, and no slice is
    named by any other."""
    scope = request.get("scope")
    return (
        request.get("action") == "start" or not scope or isinstance(scope, str)
    )


def _control_state(path: Path) -> dict[str, str] | None:
    """The scopes the control file in the run directory `path` stops, and
    how: the driver's fold of the requests it takes (_foldable), with a
    line that does not decode skipped as the driver's reader skips it.
    None when the file itself cannot be read."""
    try:
        requests = read_jsonl(path / CONTROL)
    except (OSError, ValueError):
        return None
    return fold_control([r for r in requests if _foldable(r)]).scopes


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
        self._reset_events()
        self._reset_alerts()
        # the logs the latest refresh could not read
        self._unreadable: set[str] = set()
        # the control file's requests as the latest refresh read them: None
        # while the file cannot be read
        self._requests: list[dict] | None = []
        self._control_skipped = 0
        self._manifest_key: tuple | None = None
        self._manifest: dict = {}
        self._plan = _Plan()

    def _reset_events(self) -> None:
        """Forget everything read from events.jsonl: the next read starts
        from its beginning."""
        self._events = _Tail(self.path / EVENTS)
        self._attempts: dict[tuple[str, str], _Attempt] = {}
        # the stage each document is in, while one runs: the cache's
        # events name the document and the role, not the stage
        self._current: dict[str, str] = {}
        self._stages_seen: dict[str, None] = {}
        self._kinds: dict[str, str] = {}
        # SHA-1s the events name that the plan does not, and the sorted
        # list of every SHA-1 known, made again when either changes
        self._strays: set[str] = set()
        self._known: list[str] | None = None
        self._stage_calls: dict[str, Counter] = {}
        self._calls = CallCounts()
        self._pause_invocation: str | None = None
        self._last_event_at: datetime | None = None
        self._last_at: dict[str, datetime] = {}
        # the events that open and close invocations, taken as run_state
        # takes them, and the documents that failed in each invocation, by
        # file (else SHA-1)
        self._invocations = Invocations()
        self._failures: dict[str | None, dict[Any, tuple]] = {}
        self._failure_invocation: str | None = None
        self._set_aside = 0

    def _reset_alerts(self) -> None:
        """Forget everything read from alerts.jsonl: the next read starts
        from its beginning."""
        self._alerts = _Tail(self.path / ALERTS)
        self._latest_alerts: deque[dict] = deque(maxlen=LATEST_ALERTS)
        self._alert_counts: Counter = Counter()
        self._alert_checks: Counter = Counter()

    def refresh(self) -> bool:
        """Read what the run directory gained since the last refresh, and
        whether it gained anything. The logs are read before run.json: an
        invocation records itself there before its first event.

        A log that cannot be read (_Tail.read raises OSError, where the
        driver's reader, read_jsonl, raises too) leaves nothing read from it
        standing, since the driver has no last good read of it: what was
        read of it is forgotten, and every refresh tries it again, from its
        beginning, so no record in it is skipped or counted twice once it
        can be read. The refresh at which it becomes unreadable, or
        readable again, counts as a change."""
        changed = False
        for name, tail, fold, forget in (
            (EVENTS, self._events, self._fold_event, self._reset_events),
            (ALERTS, self._alerts, self._fold_alert, self._reset_alerts),
        ):
            try:
                entries, restarted = tail.read()
            except OSError:
                if name not in self._unreadable:
                    forget()
                    self._unreadable.add(name)
                    changed = True
                continue
            if name in self._unreadable:
                self._unreadable.discard(name)
                changed = True
            if restarted:
                return self._start_over()
            for entry in entries:
                fold(entry)
            changed = changed or bool(entries)
        changed = self._read_control() or changed
        manifest = self._read_manifest()
        if manifest is not None:
            self._manifest = manifest
            self._plan = _plan(manifest)
            self._known = None
            changed = True
        if changed:
            self.version += 1
        return changed

    def _start_over(self) -> bool:
        self._reset()
        self.refresh()
        self.version += 1
        return True

    def _read_manifest(self) -> dict | None:
        """run.json when it has changed since the last read, else None. It
        is parsed again only when its inode, size or modification time has
        changed; one that cannot be read (half written by a writer that
        does not replace it whole, say) or is gone leaves the last good
        one standing, and is tried again at the next refresh."""
        try:
            status = os.stat(self.path / MANIFEST)
        except OSError:
            return None
        key = (status.st_ino, status.st_size, status.st_mtime_ns)
        if key == self._manifest_key:
            return None
        manifest = RunDir(self.path).manifest()
        if not isinstance(manifest, dict) or not manifest:
            return None
        self._manifest_key = key
        return manifest

    def _read_control(self) -> bool:
        """control.jsonl, read whole at every refresh as the driver reads it
        (rundir's reader) before each stage it dispatches; whether the
        requests it gives changed. A request the driver's fold does not
        take as it is written (_foldable) is left out and counted as
        skipped.

        When the file itself cannot be read, the driver cannot read it
        either (RunDir.control raises), so its requests are unknown (None),
        not the last ones read, and so is every slice's state, until a
        refresh reads the file again. A change of its mode or owner shows
        in no size or modification time, which is why every refresh reads
        it."""
        try:
            requests = read_jsonl(self.path / CONTROL)
        except (OSError, ValueError):
            changed = self._requests is not None
            self._requests, self._control_skipped = None, 0
            return changed
        kept = [request for request in requests if _foldable(request)]
        self._control_skipped = len(requests) - len(kept)
        changed = kept != self._requests
        self._requests = kept
        return changed

    # -- folding the logs --------------------------------------------------

    def _fold_event(self, event: dict) -> None:
        name = event.get("event")
        at = _time(event.get("at"))
        invocation = invocation_of(event)
        if at is not None:
            if self._last_event_at is None or at > self._last_event_at:
                self._last_event_at = at
            if invocation is not None:
                self._last_at[invocation] = at
        if not isinstance(name, str):
            return
        if name in STAGE_EVENTS:
            self._fold_stage(event, name, at, invocation)
        elif name in CACHE_EVENTS:
            self._fold_cache(event, name)
        elif name == CALL:
            self._fold_call(event, invocation)
        elif name == DOCUMENT_FAILED:
            self._fold_failure(event, invocation)
        elif name in (RUN_START, RUN_END, RUN_INTERRUPTED):
            self._invocations.fold(event)
            if name == RUN_START:
                self._current.clear()
        elif name == SET_ASIDE:
            self._set_aside += 1

    def _fold_stage(
        self,
        event: dict,
        name: str,
        at: datetime | None,
        invocation: str | None,
    ) -> None:
        subject, stage = event.get("subject"), event.get("stage")
        if not (isinstance(subject, str) and subject):
            return
        if not (isinstance(stage, str) and stage):
            return
        if stage not in self._stages_seen:
            self._stages_seen[stage] = None
        if subject not in self._kinds:
            kind = event.get("kind")
            if isinstance(kind, str):
                self._kinds[subject] = kind
        if subject not in self._plan.documents and subject not in self._strays:
            self._strays.add(subject)
            self._known = None
        attempt = self._attempts.get((subject, stage))
        if attempt is None:
            attempt = self._attempts[(subject, stage)] = _Attempt()
        if name == "started":
            attempt.status, attempt.since = RUNNING, at
            attempt.invocation, attempt.held = invocation, False
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
            attempt.end()
            return
        if name == "reused":
            attempt.begin()
            attempt.how = VIEW_CURRENT
        else:
            attempt.how = attempt.reuse_reason()
            attempt.end()
        reasons = event.get("attention")
        if not reasons:
            reasons = []
        elif not isinstance(reasons, list):
            reasons = [reasons]
        attempt.attention = tuple(reason_label(reason) for reason in reasons)
        attempt.status = NEEDS_ATTENTION if attempt.attention else DONE

    def _fold_cache(self, event: dict, name: str) -> None:
        calls = self._calls
        calls.cache_events += 1
        role = event.get("role")
        role = role if isinstance(role, str) and role else "?"
        reason = event.get("reason")
        reason = reason if isinstance(reason, str) and reason else None
        per_role = calls.roles.get(role)
        if per_role is None:
            per_role = calls.roles[role] = Counter()
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
        attempt, stage = self._in_flight(event)
        if attempt is None:
            return
        stage_calls = self._stage_calls.get(stage)
        if stage_calls is None:
            stage_calls = self._stage_calls[stage] = Counter()
        if name in _ENGINE_EVENTS:
            attempt.engine += 1
            stage_calls["engine"] += 1
        elif name == "cache-hit":
            if attempt.hits is None:
                attempt.hits = Counter()
            attempt.hits[reason or KEY_MATCH] += 1
            stage_calls["cached"] += 1

    def _in_flight(self, event: dict) -> tuple[_Attempt | None, str | None]:
        """The attempt of the stage the event's document is in, if one is
        running: an event about a call names the document, not the
        stage, and a document's stages run one at a time."""
        subject = event.get("subject")
        stage = (
            self._current.get(subject) if isinstance(subject, str) else None
        )
        if stage is None:
            return None, None
        return self._attempts[(subject, stage)], stage

    def _fold_call(self, event: dict, invocation: str | None) -> None:
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
        refused = event.get("refused")
        if isinstance(refused, int) and not isinstance(refused, bool):
            calls.refused += max(0, refused)
        held = event.get("held")
        if isinstance(held, str) and held:
            calls.holds[held] += 1
        error = event.get("error")
        if isinstance(error, str) and error:
            calls.errors[failure_label(error)] += 1
        if outcome == "paused":
            calls.pause = CallPause(
                held=_token(held) if held else "?",
                window=_token(event.get("window"))
                if event.get("window")
                else None,
                resumes_at=_time(event.get("resumes_at")),
            )
            self._pause_invocation = invocation
        attempt, _stage = self._in_flight(event)
        if attempt is not None:
            attempt.calls += 1

    def _fold_failure(self, event: dict, invocation: str | None) -> None:
        """A failure is keyed by its file, else its SHA-1: a document that
        fails twice in one invocation counts once, and its later failure
        stands. One that names neither counts alone."""
        file, subject = event.get("file"), event.get("subject")
        if isinstance(file, str) and file:
            key: Any = file
        elif isinstance(subject, str) and subject:
            key = subject
        else:
            key = object()
        failures = self._failures.get(invocation)
        if failures is None:
            failures = self._failures[invocation] = {}
        failures[key] = (
            _token(event.get("kind")),
            _suffix(file),
            failure_label(event.get("reason")),
            isinstance(subject, str) and bool(subject),
        )
        self._failure_invocation = invocation

    def _fold_alert(self, alert: dict) -> None:
        self._latest_alerts.append(alert)
        self._alert_counts[_token(alert.get("status"))] += 1
        self._alert_checks[_token(alert.get("check"))] += 1

    # -- the summary --------------------------------------------------------

    def _known_sha1s(self) -> list[str]:
        if self._known is None:
            strays = {
                sha1.lower()
                for sha1 in self._strays
                if _SHA1.fullmatch(sha1.lower())
            }
            self._known = sorted(strays.union(self._plan.known))
        return self._known

    def _kind(self, sha1: str) -> str | None:
        planned = self._plan.documents.get(sha1)
        if planned is not None and planned[0] is not None:
            return planned[0]
        return self._kinds.get(sha1)

    def summary(self, now: datetime | None = None) -> Summary:
        """Where the run stands at `now` (the current time by default),
        from what the refreshes have read."""
        now = now or datetime.now(UTC)
        manifest, plan = self._manifest, self._plan
        summary = Summary(path=self.path, now=now)
        summary.events_unreadable = EVENTS in self._unreadable
        summary.alerts_unreadable = ALERTS in self._unreadable
        # a run is there once run.json or events are read, or while its
        # event log cannot be read
        summary.found = (
            bool(manifest)
            or self._events.offset > 0
            or summary.events_unreadable
        )
        summary.skipped_lines = (
            self._events.skipped + self._alerts.skipped + self._control_skipped
        )
        summary.control_unreadable = self._requests is None
        summary.last_event_at = self._last_event_at
        summary.set_aside = self._set_aside
        if not summary.found:
            return summary
        summary.run_id = _run_id(manifest.get("run_id"))
        mode, project_stage = (
            manifest.get("mode"),
            manifest.get("project_stage"),
        )
        summary.mode = _token(mode) if mode else None
        summary.project_stage = (
            _token(project_stage) if project_stage else None
        )
        latest, ended = self._invocation_state(summary, now)
        summary.calls = self._calls_shown(latest)
        self._document_failures(summary, latest)
        summary.documents = len(plan.documents)
        summary.kinds = Counter(
            _token(kind) for kind, _ in plan.documents.values()
        )

        order = dict.fromkeys(plan.order)
        order.update(dict.fromkeys(self._stages_seen))
        stages = {stage: StageCounts(_token(stage)) for stage in order}
        for stage, counts in stages.items():
            calls = self._stage_calls.get(stage)
            if calls is not None:
                counts.engine, counts.cached = calls["engine"], calls["cached"]
        remaining: Counter = Counter()
        rates: dict[tuple[str, str | None], _Rate] = {}

        def count(sha1: str, stage: str, attempt: _Attempt) -> None:
            counts = stages[stage]
            kind = self._kind(sha1)
            if attempt.ended:
                rate = rates.get((stage, kind))
                if rate is None:
                    rate = rates[(stage, kind)] = _Rate()
                rate.attempts += attempt.ended
                rate.engine += attempt.ended_engine
                rate.calls += attempt.ended_calls
            status = attempt.status
            if status == RUNNING and interrupted(
                attempt.invocation, latest, ended
            ):
                counts.pending += 1
                counts.interrupted += 1
                remaining[(stage, kind)] += 1
            elif status == RUNNING:
                counts.running += 1
                remaining[(stage, kind)] += 1
                summary.in_flight.append(
                    InFlight(sha1, _token(kind), counts.name, attempt.since)
                )
            elif status == PENDING:
                counts.pending += 1
                counts.held += attempt.held
                remaining[(stage, kind)] += 1
            elif status == FAILED:
                counts.failed += 1
                counts.failures[attempt.failure or OTHER] += 1
                remaining[(stage, kind)] += 1
            elif status == NEEDS_ATTENTION:
                counts.attention += 1
                counts.attention_reasons.update(attempt.attention)
            elif attempt.how == MADE:
                counts.done += 1
            else:
                counts.reused[attempt.how] += 1

        # while events.jsonl cannot be read, no stage is counted: its counts
        # are unknown, not the plan's stages all pending
        if not summary.events_unreadable:
            for sha1, (_kind, planned) in plan.documents.items():
                for stage in planned:
                    count(
                        sha1,
                        stage,
                        self._attempts.get((sha1, stage), _NOT_STARTED),
                    )
            for (sha1, stage), attempt in self._attempts.items():
                planned_stages = plan.documents.get(sha1, (None, ()))[1]
                if stage not in planned_stages:
                    count(sha1, stage, attempt)
            summary.stages = list(stages.values())
        summary.in_flight.sort(
            key=lambda item: (item.since or now, item.stage, item.sha1)
        )
        summary.remaining_work = sum(remaining.values())
        summary.remaining_calls, summary.unrated = self._estimate(
            remaining, rates
        )

        known = self._known_sha1s()
        summary.alerts = [
            self._alert_line(alert, known)
            for alert in reversed(self._latest_alerts)
        ]
        summary.alert_counts = Counter(self._alert_counts)
        summary.alert_checks = Counter(self._alert_checks)
        kinds = {kind for kind, _ in plan.documents.values() if kind}
        kinds.update(self._kinds.values())
        summary.known_scopes = {
            "run",
            *(f"stage:{stage}" for stage in order),
            *(f"kind:{kind}" for kind in kinds),
        }
        summary.slices = self._slices(list(order), sorted(kinds))
        return summary

    def _invocation_state(
        self, summary: Summary, now: datetime
    ) -> tuple[str | None, bool]:
        """Fill in the run's state and times; (the latest invocation's id,
        whether it has ended), read as run_state reads them
        (rundir.Invocations.latest): an invocation has ended when run.json
        records its end, or its closing event says how it ended. What only
        the events can tell is unknown while events.jsonl cannot be read:
        the state of a run.json without invocations, and the time the run
        has been running when an invocation was killed."""
        manifest = self._manifest
        invocations = invocation_records(manifest)
        summary.invocations = len(invocations)
        summary.started_at = _time(manifest.get("started_at"))
        latest, ended = self._invocations.latest(manifest)
        closing = self._invocations.closing.get(latest)
        if not invocations:
            # a run.json from before invocations were recorded: the last
            # start or close event says whether the run is going
            if EVENTS in self._unreadable:
                summary.state = UNKNOWN
            else:
                summary.state = {
                    RUN_START: "running",
                    RUN_END: "finished",
                    RUN_INTERRUPTED: "interrupted",
                }.get(self._invocations.marker or "", "not started")
            if ended:
                self._closed(summary, closing)
            return latest, ended
        total: float | None = 0.0
        for index, entry in enumerate(invocations):
            start = _time(entry.get("started_at"))
            if start is None:
                continue
            end = _time(entry.get("ended_at"))
            if end is None and index == len(invocations) - 1:
                end = now
            elif end is None and EVENTS in self._unreadable:
                # killed: how far it ran is in its events, which cannot be
                # read, so the run's time is unknown
                total = None
                continue
            elif end is None:
                # killed: its last event is as far as it is known to have run
                end = self._last_at.get(str(entry.get("invocation"))) or start
            span = max(0.0, (end - start).total_seconds())
            if total is not None:
                total += span
            if index == len(invocations) - 1:
                summary.invocation_elapsed = span
        summary.elapsed = total
        last = invocations[-1]
        if not ended:
            summary.state = "running"
        elif last.get("ended_at"):
            outcome = last.get("outcome")
            summary.state = _token(outcome) if outcome else "ended"
            summary.reason = _verbatim(last.get("reason"))
            if closing is not None:
                summary.statuses = _statuses(closing)
        else:
            # ended by its closing event alone
            self._closed(summary, closing)
        return latest, ended

    @staticmethod
    def _closed(summary: Summary, closing: dict) -> None:
        """How an invocation ended, as its closing event says."""
        outcome = closing.get("outcome")
        if outcome:
            summary.state = _token(outcome)
        elif closing.get("event") == RUN_INTERRUPTED:
            summary.state = "interrupted"
        else:
            summary.state = "finished"
        summary.reason = _verbatim(closing.get("reason"))
        summary.statuses = _statuses(closing)

    def _calls_shown(self, latest: str | None) -> CallCounts:
        """The run's call counts, with every name as the views show it; a
        pause for the subscription only when the latest invocation made
        it."""
        calls = self._calls
        roles: dict[str, Counter] = {}
        for role, counts in calls.roles.items():
            roles.setdefault(_token(role), Counter()).update(counts)
        return CallCounts(
            engine=calls.engine,
            cached=calls.cached,
            hit_reasons=_relabel(
                calls.hit_reasons, lambda reason: _verbatim(reason, 40)
            ),
            resumed=calls.resumed,
            replayed=calls.replayed,
            rejected=calls.rejected,
            adopted=calls.adopted,
            roles=roles,
            cache_events=calls.cache_events,
            logged=calls.logged,
            seconds=calls.seconds,
            timed=calls.timed,
            outcomes=_relabel(calls.outcomes, _token),
            waited=calls.waited,
            waits=calls.waits,
            refused=calls.refused,
            holds=_relabel(calls.holds, _token),
            errors=Counter(calls.errors),
            pause=calls.pause
            if calls.pause is not None and self._pause_invocation == latest
            else None,
        )

    def _document_failures(self, summary: Summary, latest: str | None) -> None:
        """The documents that failed in the latest invocation: how many, by
        kind and file suffix, and by reason; and how many failed before
        their source could be read (no SHA-1). Never their file names."""
        if latest is None:
            latest = self._failure_invocation
        failures = self._failures.get(latest) or {}
        summary.failed_documents = len(failures)
        for kind, suffix, label, read in failures.values():
            summary.failed_by_kind[f"{kind} ({suffix})"] += 1
            summary.failed_by_reason[label] += 1
            summary.unread_documents += not read

    def _alert_line(self, alert: dict, known: list[str]) -> AlertLine:
        sha1 = _match(alert.get("subject"), known)
        return AlertLine(
            at=_time(alert.get("at")),
            status=_token(alert.get("status")),
            stage=_token(alert.get("stage")),
            check=_token(alert.get("check")),
            subject=prefix(sha1),
            kind=_token(self._kind(sha1)) if sha1 else "?",
            value=_value(alert.get("value")),
        )

    def _estimate(
        self, remaining: Counter, rates: dict[tuple[str, str | None], _Rate]
    ) -> tuple[int | None, list[str]]:
        """(the engine calls the work still to go will take, the stages and
        kinds it could not rate). Each stage of each kind is rated at the
        engine calls its ended attempts made on average, since kinds differ
        (a text file's transcription makes no call, a scan's does); a view
        that stood made no call and is left out. The calls are None when
        nothing to go could be rated, or the run shows no calls at all (no
        cache events, and no call events)."""
        by_cache = self._calls.cache_events > 0
        if not by_cache and not self._calls.logged:
            return None, []
        estimate = 0.0
        rated = False
        unrated: list[str] = []
        for (stage, kind), count in sorted(
            remaining.items(), key=lambda item: (item[0][0], item[0][1] or "")
        ):
            rate = rates.get((stage, kind))
            if rate is None or not rate.attempts:
                unrated.append(f"{_token(stage)} ({_token(kind)})")
                continue
            made = rate.engine if by_cache else rate.calls
            estimate += count * made / rate.attempts
            rated = True
        if remaining and not rated:
            return None, unrated
        return round(estimate), unrated

    def _slices(self, stages: list[str], kinds: list[str]) -> list[SliceState]:
        """Every slice the operator can stop: the run, each stage and each
        kind the run knows, and any other scope a current request names.
        Their states are the driver's fold of the requests
        (rundir.fold_control); one pass over the requests since the latest
        invocation's start marker finds since when each stopped scope has
        been stopped, and who stopped it and why: a pause stops a scope
        until a resume, and another pause meanwhile gives a new reason, not
        a new start; an abort stops it for good. While the control file
        cannot be read, every slice the run knows is unknown."""
        scopes: dict[str, None] = {"run": None}
        scopes.update(dict.fromkeys(f"stage:{stage}" for stage in stages))
        scopes.update(dict.fromkeys(f"kind:{kind}" for kind in kinds))
        requests = self._requests
        if requests is None:
            return [
                SliceState(_scope_shown(scope), UNKNOWN) for scope in scopes
            ]
        state = fold_control(requests).scopes
        start = max(
            (
                index
                for index, request in enumerate(requests)
                if request.get("action") == "start"
            ),
            default=-1,
        )
        stopped: dict[str, str] = {}
        causes: dict[str, tuple] = {}
        for request in requests[start + 1 :]:
            scope = request.get("scope") or "run"
            scopes.setdefault(scope)
            if stopped.get(scope) == ABORTED:
                continue
            action = request.get("action")
            if action in (PAUSE, ABORT):
                if stopped.get(scope) == PAUSED and action == PAUSE:
                    since = causes[scope][0]
                else:
                    since = _time(request.get("at"))
                stopped[scope] = ABORTED if action == ABORT else PAUSED
                causes[scope] = (
                    since,
                    _token(request["by"]) if request.get("by") else None,
                    _verbatim(request.get("reason")),
                )
            elif action == RESUME:
                stopped.pop(scope, None)
                causes.pop(scope, None)
        slices = []
        for scope in scopes:
            status = state.get(scope, ACTIVE)
            cause = causes.get(scope) if status != ACTIVE else None
            slices.append(
                SliceState(_scope_shown(scope), status, *(cause or ()))
            )
        return slices


def _relabel(counter: Counter, label: Callable[[Any], Any]) -> Counter:
    shown: Counter = Counter()
    for key, count in counter.items():
        shown[label(key) or "?"] += count
    return shown


def _statuses(closing: dict) -> Counter:
    """The documents per status an invocation's closing event gives."""
    statuses = closing.get("statuses")
    shown: Counter = Counter()
    if isinstance(statuses, dict):
        for status, count in statuses.items():
            if isinstance(count, int) and not isinstance(count, bool):
                shown[_token(status)] += count
    return shown


def _scope_shown(scope: str) -> str:
    kind, sep, name = scope.partition(":")
    if scope == "run":
        return "run"
    if sep and kind in ("stage", "kind"):
        return f"{kind}:{_token(name)}"
    return "?"


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
    f"commands: {GRAMMAR}",
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
    if run.state == UNKNOWN:
        state += "; run state unknown"
    elif run.state != ACTIVE:
        state += f"; run {run.state}"
    parts = [state]
    if summary.invocations:
        parts.append(f"invocation {summary.invocations}")
        elapsed = "elapsed unknown"
        if summary.elapsed is not None:
            elapsed = f"elapsed {_duration(summary.elapsed)}"
        if summary.invocations > 1:
            elapsed += f" (this one {_duration(summary.invocation_elapsed)})"
        parts.append(elapsed)
    if summary.last_event_at is not None:
        age = (summary.now - summary.last_event_at).total_seconds()
        parts.append(f"last event {_duration(age)} ago")
    elif summary.events_unreadable:
        parts.append("last event unknown")
    kinds = _counts(summary.kinds)
    plan = (
        f"started {_stamp(summary.started_at)} | "
        f"{_n(summary.documents)} documents" + (f": {kinds}" if kinds else "")
    )
    lines = [
        (title, "title"),
        (" | ".join(parts), _state_style(summary.state)),
        (plan, ""),
    ]
    if summary.statuses:
        lines.append((f"statuses   {_counts(summary.statuses)}", ""))
    return lines


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
    """The stage table; or, while events.jsonl cannot be read, one line
    that says its counts are unknown."""
    if summary.events_unreadable:
        return [(f"stages     counts unknown: {EVENTS_UNREADABLE}", "warn")]
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
    attention and failure labels, pending stages that are held or were
    interrupted; and the documents that failed in the latest invocation."""
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
    if summary.failed_documents:
        unread = (
            f", {_n(summary.unread_documents)} before their source was read"
            if summary.unread_documents
            else ""
        )
        notes.append(
            (
                f"failed     {_n(summary.failed_documents)} documents in "
                f"this invocation{unread}: {_counts(summary.failed_by_kind)}"
                f"; {_counts(summary.failed_by_reason)}",
                "bad",
            )
        )
    return notes


def _to_go(summary: Summary) -> str | None:
    """What is left, as the calls rows say it."""
    if not summary.remaining_work:
        return "nothing" if summary.stages else None
    line = f"{_n(summary.remaining_work)} stages"
    if summary.remaining_calls is None:
        line += "; engine calls unknown"
        if summary.unrated:
            line += f" (no rate yet for {', '.join(summary.unrated)})"
        return line
    line += (
        f", ~{_n(summary.remaining_calls)} engine calls at the rates so far"
    )
    if summary.unrated:
        line += f", besides {', '.join(summary.unrated)}: no rate yet"
    return line


def _call_rows(summary: Summary, detail: bool) -> list[tuple[str, str]]:
    """The calls and the work to go; or, while events.jsonl cannot be read,
    one line that says they are unknown."""
    if summary.events_unreadable:
        return [(f"calls      unknown: {EVENTS_UNREADABLE}", "warn")]
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
    to_go = _to_go(summary)
    if to_go is not None:
        rows.append((f"  to go    {to_go}", ""))
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
        if calls.errors:
            parts.append(f"errors: {_counts(calls.errors)}")
        rows.append(("  logged   " + " | ".join(parts), ""))
        waits = []
        if calls.waits:
            waits.append(
                f"{_n(calls.waits)} waited for the subscription, "
                f"{_duration(calls.waited)} in all"
            )
        if calls.refused:
            waits.append(f"{_n(calls.refused)} attempts refused")
        if calls.holds:
            waits.append(f"held: {_counts(calls.holds)}")
        if waits and detail:
            rows.append(("  limits   " + " | ".join(waits), ""))
    if calls.pause is not None:
        pause = calls.pause
        line = f"  paused   for the subscription ({pause.held}"
        line += f", {pause.window} window)" if pause.window else ")"
        if pause.resumes_at is not None:
            line += f"; it can go on from {_stamp(pause.resumes_at)}"
        rows.append((line, "warn"))
    return rows


def _in_flight_rows(summary: Summary, limit: int) -> list[tuple[str, str]]:
    if summary.events_unreadable:
        return [(f"in flight  unknown: {EVENTS_UNREADABLE}", "warn")]
    rows = [(f"in flight  {_n(len(summary.in_flight))}", "head")]
    for item in summary.in_flight[:limit]:
        age = (
            _duration((summary.now - item.since).total_seconds())
            if item.since
            else "?"
        )
        rows.append(
            (
                f"  {prefix(item.sha1):<{PREFIX}}  {item.kind:<13}"
                f"{item.stage:<15} for {age}",
                "",
            )
        )
    hidden = len(summary.in_flight) - limit
    if hidden > 0:
        rows.append((f"  ... and {_n(hidden)} more", "dim"))
    return rows


def _slice_rows(summary: Summary, detail: bool) -> list[tuple[str, str]]:
    """The stopped slices, one per line, and the active ones in one; or,
    while the control file cannot be read, one line that says every
    slice's state is unknown."""
    if summary.control_unreadable:
        return [(f"slices     state unknown: {CONTROL_UNREADABLE}", "warn")]
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
            line += f": {item.reason}"
        rows.append((line, "bad" if item.state == ABORTED else "warn"))
    if detail and active:
        rows.append((f"  active: {', '.join(active)}", "dim"))
    return rows


def _alert_rows(summary: Summary, limit: int) -> list[tuple[str, str]]:
    if summary.alerts_unreadable:
        return [(f"alerts     unknown: {ALERTS_UNREADABLE}", "warn")]
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
                f" {alert.kind:<13}{alert.value}",
                "warn" if alert.status == "new" else "",
            )
        )
    return rows


def _body(summary: Summary, full: bool) -> list[tuple[str, str]]:
    """The view's lines above the footer: in full, or, to fit a short
    terminal, with two documents in flight and two alerts at most, and
    without the stage notes and other details."""
    limit = 8 if full else 2
    body = [*_header(summary), ("", ""), *_stage_rows(summary)]
    if full:
        body += [(f"  {text}", style) for text, style in _stage_notes(summary)]
    body += [
        ("", ""),
        *_call_rows(summary, full),
        ("", ""),
        *_in_flight_rows(summary, limit),
        ("", ""),
        *_slice_rows(summary, full),
        ("", ""),
        *_alert_rows(summary, limit),
    ]
    if full and summary.set_aside:
        body.append(
            (
                "set aside to be made again (set-aside events): "
                f"{_n(summary.set_aside)}",
                "dim",
            )
        )
    if full and summary.skipped_lines:
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
    `footer` lines last. To fit, the view drops its details and shortens
    its lists (_body), and then is cut short above the footer. No line
    holds a control character but the view's own styles."""
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
        body = _body(summary, full=True)
        if room is not None and len(body) > room:
            body = _body(summary, full=False)
    if room is not None:
        body = body[:room]
    plain = body + tail
    if height is not None:
        plain = plain[: max(1, height)]
    lines = []
    for text, style in plain:
        text = _clip(_safe(text), max(1, width))
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
    return html.escape(_clean(str(value)), quote=True)


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


def _span(style: str, text: str) -> str:
    return f'<span class="{_e(style)}">{_e(text)}</span>'


def render_html(summary: Summary, *, reload: int = PAGE_RELOAD) -> str:
    """The status page: one self-contained HTML document (inline style,
    no script, no external asset) that reloads itself every `reload`
    seconds. It shows what the terminal view shows."""
    title = f"yizkr run {summary.run_id or '?'}"
    parts = [f"<h1>{_e(title)}</h1>"]
    for text, style in _header(summary)[1:]:
        parts.append(f'<p class="{_e(style)}">{_e(text)}</p>')

    rows = [
        [_e(stage.name), *(_e(_n(value)) for value in _stage_values(stage))]
        for stage in summary.stages
    ]
    parts.append("<h2>Stages</h2>")
    notes: list[str] = []
    if summary.events_unreadable:
        parts.append(
            f'<p class="warn">Counts unknown: {_e(EVENTS_UNREADABLE)}.</p>'
        )
    elif rows:
        parts.append(
            _table(
                ["stage", *(name for name, _ in _COLUMNS)],
                rows,
                numeric=range(1, len(_COLUMNS) + 1),
            )
        )
        notes = [
            f'<li class="{_e(style)}">{_e(text)}</li>'
            for text, style in _stage_notes(summary)
        ]
        if summary.set_aside:
            notes.append(
                '<li class="muted">set aside to be made again (set-aside '
                f"events): {_e(_n(summary.set_aside))}</li>"
            )
    else:
        parts.append('<p class="muted">No stages planned or seen yet.</p>')
    # lines are skipped in any of the logs, not only in events.jsonl: they
    # are noted whether or not there are stage counts to show, as the
    # terminal view notes them
    if summary.skipped_lines:
        notes.append(
            '<li class="muted">unreadable log lines skipped: '
            f"{_e(_n(summary.skipped_lines))}</li>"
        )
    if notes:
        parts.append(f"<ul>{''.join(notes)}</ul>")

    parts.append("<h2>Calls</h2>")
    if summary.events_unreadable:
        parts.append(f'<p class="warn">Unknown: {_e(EVENTS_UNREADABLE)}.</p>')
    else:
        parts += [
            f'<p class="{_e(style)}">{_e(text)}</p>'
            for text, style in _call_rows(summary, detail=True)
        ]

    heading = "In flight"
    if not summary.events_unreadable:
        heading += f" ({_n(len(summary.in_flight))})"
    parts.append(f"<h2>{_e(heading)}</h2>")
    if summary.events_unreadable:
        parts.append(f'<p class="warn">Unknown: {_e(EVENTS_UNREADABLE)}.</p>')
    elif summary.in_flight:
        parts.append(
            _table(
                ["document", "kind", "stage", "for"],
                [
                    [
                        f"<code>{_e(prefix(item.sha1))}</code>",
                        _e(item.kind),
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
    if summary.control_unreadable:
        parts.append(
            f'<p class="warn">State unknown: {_e(CONTROL_UNREADABLE)}.</p>'
        )
    parts.append(
        _table(
            ["slice", "state", "since", "by", "reason"],
            [
                [
                    f"<code>{_e(item.scope)}</code>",
                    _span(
                        "good"
                        if item.state == ACTIVE
                        else "bad"
                        if item.state == ABORTED
                        else "warn",
                        item.state,
                    ),
                    _e(_stamp(item.since, "%H:%M:%S") if item.since else ""),
                    _e(item.by or ""),
                    _e(item.reason or ""),
                ]
                for item in summary.slices
            ],
        )
    )

    parts.append("<h2>Alerts</h2>")
    if summary.alerts_unreadable:
        parts.append(f'<p class="warn">Unknown: {_e(ALERTS_UNREADABLE)}.</p>')
    elif summary.alert_counts:
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
                        _span(
                            "warn" if alert.status == "new" else "muted",
                            alert.status,
                        ),
                        _e(alert.stage),
                        _e(alert.check),
                        f"<code>{_e(alert.subject)}</code>",
                        _e(alert.kind),
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
    """Write the status page, `status/index.html` in the run directory, and
    return its path; None, writing nothing, when the directory holds no
    run.json.

    The page is written beside its place and moved there whole, so a
    reader sees the old page or the new one. Nothing is made but the
    `status` directory, and only in a run directory that is there: one
    removed meanwhile is not made again (FileNotFoundError), nor is a page
    written through a `status` that is a symbolic link. The page is a live
    view, not a record, and is not flushed to disk."""
    path = Path(path)
    if not (path / MANIFEST).is_file():
        return None
    folder = path / STATUS_DIR
    with contextlib.suppress(FileExistsError):
        folder.mkdir()
    if folder.is_symlink() or not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not a directory of its own")
    page = folder / STATUS_PAGE
    data = render_html(summary, reload=reload).encode("utf-8", "replace")
    staging = staging_path(page)
    try:
        with open(staging, "xb") as handle:
            handle.write(data)
        os.replace(staging, page)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return page


# -- serving the status page --------------------------------------------------


class _PageHandler(http.server.BaseHTTPRequestHandler):
    """Serves the status page and nothing else: `/` and `/index.html` are
    the page, read from the `status` directory without following a
    symbolic link, neither the directory's nor the page's; every other
    path, and a page that is not there or is a link, is not found. No
    directory is ever listed. Every response with headers, an error's
    as well as the page's, carries the headers end_headers adds."""

    status_dir: Path
    server_version = "yizkr-monitor"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._send(body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send(body=False)

    def _send(self, *, body: bool) -> None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        page = self._page() if path in ("/", f"/{STATUS_PAGE}") else None
        if page is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        if body:
            self.wfile.write(page)

    def end_headers(self) -> None:
        """End the headers of any response, the standard library's error
        pages (404, 501 and the like) included, with these: nothing keeps
        a copy, nothing reads the body as another type, no referrer is
        sent, and nothing may load but inline style. A request the
        standard library reads as HTTP/0.9 (no version, a version it
        cannot read or does not serve, a request line it cannot parse)
        is answered with a body alone, without a status line or any
        header."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'",
        )
        super().end_headers()

    def _page(self) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            folder = os.open(
                self.status_dir, flags | getattr(os, "O_DIRECTORY", 0)
            )
        except OSError:
            return None
        try:
            descriptor = os.open(STATUS_PAGE, flags, dir_fd=folder)
        except (OSError, NotImplementedError):
            return None
        finally:
            os.close(folder)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            return handle.read()

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet: a line on the terminal would break the view."""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        """Bind, without the reverse name lookup HTTPServer makes of its
        address, which can take seconds and serves nothing here."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Server6(_Server):
    address_family = socket.AF_INET6


def parse_address(value: str) -> tuple[str, int]:
    """'ADDRESS:PORT' (an IPv6 address in brackets) -> (address, port)."""
    address, sep, port = value.rpartition(":")
    address = address.strip("[]")
    if not sep or not address or not port.isdigit() or int(port) > 65535:
        raise ValueError(f"expected ADDRESS:PORT, not {value!r}")
    return address, int(port)


def page_url(server: http.server.HTTPServer) -> str:
    """Where a server serve_status started serves the page."""
    host, port = server.server_address[:2]
    host = f"[{host}]" if ":" in str(host) else host
    return f"http://{host}:{port}/"


def serve_status(
    path: Path | str, address: str, port: int
) -> http.server.ThreadingHTTPServer:
    """Serve the status page of the run directory at `path`, and nothing
    else (_PageHandler), on `address`:`port`, from a thread of its own,
    until `server.shutdown()`; `server.server_close()` then frees the
    port. Only the `status` directory is read: the run directory holds
    the documents' views and is never served."""
    handler = type(
        "PageHandler",
        (_PageHandler,),
        {"status_dir": Path(path) / STATUS_DIR},
    )
    server_class = _Server6 if ":" in address else _Server
    server = server_class((address, port), handler)
    # shutdown() waits for the loop's next look at its flag
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="yizkr-status-page",
        daemon=True,
    )
    thread.start()
    return server


# -- control requests ---------------------------------------------------------


class NotARunDirectory(ValueError):
    """A control request for a directory that holds no run.json."""


def _scope(stage: str | None, kind: str | None) -> str:
    """The scope a request for `stage` or `kind` names, as RunDir.request
    writes it."""
    return f"stage:{stage}" if stage else f"kind:{kind}" if kind else "run"


def _slice_name(label: str, name: str | None) -> str | None:
    """A stage or kind name for a request, without surrounding spaces;
    refused when it is empty, which would name the whole run, or holds a
    space or a character that is not printable, which no slice's name
    does."""
    if name is None:
        return None
    stripped = name.strip()
    if not stripped:
        raise ValueError(f"an empty {label} name")
    if any(char.isspace() for char in stripped):
        raise ValueError(f"a {label} name has no spaces: {stripped!r}")
    if not stripped.isprintable():
        raise ValueError(f"a {label} name is printable: {stripped!r}")
    return stripped


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
    (RunDir.request). A name is written without surrounding spaces.
    Refused for a directory without run.json, and for an empty name or one
    that holds a space (_slice_name)."""
    path = Path(path)
    if not (path / MANIFEST).is_file():
        raise NotARunDirectory(
            f"{path}: no {MANIFEST}; is it a run directory?"
        )
    return RunDir(path).request(
        action,
        stage=_slice_name("stage", stage),
        kind=_slice_name("kind", kind),
        by=by,
        reason=reason,
    )


def state_now(path: Path | str, scope: str) -> str:
    """'now paused', 'now aborted' or 'now active': where `scope` stands
    once the driver folds the run's control file (_control_state), or
    that it cannot be told when the file cannot be read."""
    state = _control_state(Path(path))
    if state is None:
        return f"its state unknown: {CONTROL_UNREADABLE}"
    return f"now {state.get(scope, ACTIVE)}"


def unknown_slice_note(scope: str, summary: Summary) -> str:
    """A note for a request whose slice the run knows of no document in,
    from its plan or its events, which then stops nothing it plans (a
    misspelt name, say); '' when the run knows the slice."""
    if scope in summary.known_scopes:
        return ""
    return " (no document the run knows has this slice)"


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
        return _scope(self.stage, self.kind)


def parse_command(line: str) -> Command | None:
    """A command of GRAMMAR (a slice may also be written `stage:NAME` or
    `kind:NAME`), `help` or `quit`; None for an empty line."""
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
            f"unknown command {words[0]!r}: {GRAMMAR}, help or quit"
        )
    rest = words[1:]
    stage = kind = None
    if rest:
        head = rest[0].lower()
        if head == "run":
            rest = rest[1:]
        elif head in ("stage", "kind"):
            if len(rest) < 2:
                raise CommandError(f"name the {head}: {GRAMMAR}")
            name, rest = rest[1], rest[2:]
            stage, kind = (name, None) if head == "stage" else (None, name)
        elif head.startswith(("stage:", "kind:")):
            which, _, name = rest[0].partition(":")
            if not name:
                raise CommandError(f"name the {which.lower()}: {GRAMMAR}")
            rest = rest[1:]
            stage, kind = (
                (name, None) if which.lower() == "stage" else (None, name)
            )
        else:
            raise CommandError(
                f"say what to {verb}, then any reason: {GRAMMAR}"
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
    scope's state is then, with a note when the run knows of no document
    in the slice (unknown_slice_note; from `summary`, else a fresh one).
    An abort asks `confirm` first, when given. A resume of an aborted
    scope is not written: an abort holds for the rest of the invocation."""
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
    if (
        command.action == RESUME
        and (_control_state(path) or {}).get(scope) == ABORTED
    ):
        return CommandResult(
            f"{scope} is aborted for the rest of this invocation, and a "
            "resume does not lift an abort: invoke the run again to "
            "resume it"
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
    written = entry["scope"]
    summary = summary or summarise_run(path)
    message = (
        f"{command.action} requested for {written}: "
        + state_now(path, written)
        + unknown_slice_note(written, summary)
    )
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


def _write(out: TextIO, text: str) -> None:
    """Write `text` as the stream's encoding can take it: what it cannot
    encode is replaced, never raised."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    out.write(text.encode(encoding, "replace").decode(encoding))
    out.flush()


class _StatusPage:
    """When `watch` writes the status page: at the first refresh, then
    once the run has changed and PAGE_MIN_INTERVAL has passed since the
    last write, or once PAGE_MAX_AGE has, whatever changed. `note` says
    how the latest write went."""

    def __init__(
        self, path: Path, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.path = path
        self.note = ""
        self._clock = clock
        self._version: int | None = None
        self._written = 0.0

    def update(self, summary: Summary, version: int) -> None:
        if not summary.found:
            return
        elapsed = self._clock() - self._written
        if (
            self._version is not None
            and elapsed < PAGE_MAX_AGE
            and (version == self._version or elapsed < PAGE_MIN_INTERVAL)
        ):
            return
        try:
            written = write_status_page(self.path, summary)
        except (OSError, ValueError) as exc:
            self.note = f"status page not written: {exc}"
        else:
            if written is not None:
                self.note = (
                    f"status page: {written} (written "
                    f"{_stamp(summary.now, '%H:%M:%S')})"
                )
        self._version, self._written = version, self._clock()


def _frame(lines: list[str], first: bool) -> str:
    """A view drawn over the last one: from the top left, each line
    cleared to its end, and the screen cleared below the last."""
    start = (HIDE_CURSOR + CLEAR_SCREEN if first else "") + HOME
    body = "".join(line + CLEAR_LINE + "\n" for line in lines)
    return start + body + CLEAR_BELOW


def _typed_command(
    keys: Keys, path: Path, summary: Summary
) -> CommandResult | None:
    """The command typed at the prompt, carried out (handle_command, with
    an abort confirmed at a second prompt); None when nothing was typed."""
    line = keys.prompt("command> ")
    if line is None:
        return None

    def confirm(question: str) -> bool:
        answer = keys.prompt(question) or ""
        return answer.strip().lower() in ("y", "yes")

    return handle_command(path, line, confirm=confirm, summary=summary)


def watch(
    path: Path | str,
    *,
    interval: float = 2.0,
    once: bool = False,
    page: bool = True,
    serve: str | None = None,
    out: TextIO | None = None,
    keys: Keys | None = None,
    clock: Callable[[], datetime] | None = None,
    size: Callable[[], tuple[int, int]] | None = None,
    terminal: bool | None = None,
) -> int:
    """Follow a run directory until the operator quits (`q`, or Ctrl-C):
    refresh every `interval` seconds, draw the terminal view in place, and
    write the status page into the run directory (`page`; _StatusPage says
    when). The run goes on whatever the monitor does.

    `serve` ('ADDRESS:PORT') serves the page, and nothing else, for as
    long as the monitor runs (serve_status); it needs `page`, and is off
    by default. ':' or Enter opens a command line (handle_command): pause,
    resume or abort the run or a slice. On an output that is not a
    terminal the view is printed afresh whenever the run changes, without
    styles, and commands are not read. `once` prints one view, writes the
    page and returns. `keys`, `clock`, `size` (columns, rows) and
    `terminal` (whether `out` is one) stand in for the terminal in tests."""
    path = Path(path)
    out = out or sys.stdout
    clock = clock or (lambda: datetime.now(UTC))
    terminal = _is_tty(out) if terminal is None else terminal
    color = (
        terminal
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )
    if serve is not None and not page:
        raise ValueError("serving the status page needs the page written")
    address = parse_address(serve) if serve is not None and not once else None
    if keys is None:
        keys = NoKeys() if once else _default_keys(out)
    hint = (
        "Ctrl-C: quit (keys are read only from a terminal)"
        if isinstance(keys, NoKeys)
        else f"':' or Enter: {GRAMMAR}   q: quit"
    )
    monitor = RunMonitor(path)
    status_page = _StatusPage(path) if page else None
    server = serve_status(path, *address) if address is not None else None
    served = (
        f"serving the status page at {page_url(server)}"
        if server is not None
        else ""
    )
    message = ""
    shown = None
    first = True
    try:
        with keys:
            while True:
                monitor.refresh()
                summary = monitor.summary(clock())
                if status_page is not None:
                    status_page.update(summary, monitor.version)
                notes = (
                    status_page.note if status_page else "",
                    served,
                    message,
                )
                footer = [note for note in notes if note]
                columns, rows = (
                    size() if size else shutil.get_terminal_size((100, 40))
                )
                # a line as wide as the terminal would wrap on some
                width = max(1, columns - 1)
                if once:
                    view = render_terminal(summary, width=width, footer=footer)
                    _write(out, view + "\n")
                    return 0
                if terminal:
                    lines = render_lines(
                        summary,
                        width=width,
                        height=max(1, rows - 1),
                        color=color,
                        footer=[*footer, hint],
                    )
                    _write(out, _frame(lines, first))
                    first = False
                elif monitor.version != shown:
                    view = render_terminal(summary, width=width, footer=footer)
                    _write(out, view + "\n\n")
                shown = monitor.version
                key = keys.wait(interval)
                if key in ("q", "Q"):
                    return 0
                if key in (":", "\r", "\n"):
                    result = _typed_command(keys, path, summary)
                    if result is None:
                        continue
                    if result.quit:
                        return 0
                    # the request shows at once, not after the interval
                    message, shown = result.message, None
                elif key in ("?", "h"):
                    message = " | ".join(line.strip() for line in HELP)
                    shown = None
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if terminal and not once:
            _write(out, SHOW_CURSOR + "\n")


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
        help=f"do not write {STATUS_DIR}/{STATUS_PAGE} into the run directory",
    )
    follow.add_argument(
        "--serve",
        metavar="ADDRESS:PORT",
        help="serve the status page, and nothing else, on ADDRESS:PORT "
        "while the monitor runs (off by default)",
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
        if args.serve is not None:
            if args.no_page:
                parser.error("--serve needs the page: drop --no-page")
            try:
                parse_address(args.serve)
            except ValueError as exc:
                parser.error(str(exc))
        try:
            return watch(
                args.run,
                interval=args.interval,
                once=args.once,
                page=not args.no_page,
                serve=args.serve,
            )
        except OSError as exc:
            parser.exit(2, f"{parser.prog}: {exc}\n")
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
    state = state_now(args.run, entry["scope"])
    note = unknown_slice_note(entry["scope"], summarise_run(args.run))
    print(f"{args.command} requested for {entry['scope']}: {state}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
