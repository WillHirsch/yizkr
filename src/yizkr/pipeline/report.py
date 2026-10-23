"""The run report: what a run did, per stage and per document, read from
its run directory (`yizkr scratch report`; ADR-0025 §3).

It reports a run that went to the end, one still running, and one that
stopped short (stopped by the control file, paused, interrupted, refused,
failed or killed): every count is what the run directory holds as it is
read. It reads the run directory, and the model manifest for the names
report.md may give claims and entities (claims.load_vocabulary); never
the wiki, the engine, the LLM cache or the corpus:

    run.json         the plan (each document's SHA-1, file, kind and
                     stages), what shapes the run, and each invocation's
                     start, end and outcome
    events.jsonl     stage, cache, call, failure and invocation events
    alerts.jsonl     the anomaly checks' alerts
    control.jsonl    pause, resume and abort requests
    <document>/      a document's state.json and its views:
                     transcription.json, batch.json, critique.json and
                     the extraction's extraction/coverage.json
    excerpts/<sha1>/ the same, for an image a PDF or a deck places on a
                     page or a slide, read as a document of its own
                     (ADR-0008)
    corpus/<stage>/  a corpus stage's outputs: the clustering stage's
                     identity-map.json

The logs are read a line at a time and folded as they are read, so a long
run's log is never held in memory. They are read as rundir.run_state
reads them: run.json before the events and again after them, and all of
it again, a few times at most, while an invocation starts or ends in
between. A stage is told cut short (interrupted) by the rule the live
monitor and run_state use (rundir.interrupted), so the report's states
agree with theirs. A run.json that cannot be read or holds no JSON
object, and an event log that cannot be read, are refused (ReportError);
any other field of another shape than the pipeline writes counts as
nothing, and the report goes on.

**Per stage**, each document's stage is pending (held, while the control
file holds it), running, interrupted, computed, reused, needs attention
or failed, as its latest events leave it. Computed: the engine answered
at least one of its calls, or it makes none (a text file's transcription,
the GEDCOM adapter). Reused: the LLM cache answered every call it made,
by the answers' reasons (`key match`, `adopted`), or its view came from
before the run's events and stands (`view current`). A view that one
invocation made and a later one reuses counts as the one that made it,
so a resumed run's stages keep how the run came by them. A completed
stage with attention reasons needs attention whichever way it came about,
as in the monitor.
What forced a computed stage to the engine is what the cache's events
record of its calls: each miss's reason (`absent`, `rejected`,
`refresh`) and each replay of a conversation's opening turn (`replay`, or
`replay changed` when it answered otherwise). The components of a missed
call's key are kept in the cache's entry, not in the run directory, and
which of them changed shows only against a baseline run (ADR-0032 §11).

**Outputs.** `write_report` writes three files into the run directory's
REPORT_DIR, a folder made private to its owner and opened once: each file
is written through the folder as it was opened, never through its path
again. No document's directory can take that name: the orchestrator
names them by slugs, which never start with a dot, and a directory it
sets aside by `.<name>.superseded-<n>`.

    report.json  everything the report holds: the aggregates, every
                 document's stages, calls, views, alerts and reasons,
                 and the place links refused as cycles
    report.html  the same as one self-contained page (inline style, no
                 script), with a drill-down per document
    report.md    the aggregates alone, for an issue or a PR

report.json and report.html hold file names, and reasons that can quote a
document, so they stay in the run directory, local-only data as the rest
of it is (docs/AGENT_PROTOCOL.md §6). report.md is rendered from the
aggregates alone (`aggregate`), which hold counts, rates, times and the
names of the pipeline's own vocabulary: never a document's name, a file
name, a path, a label, an excerpt, a quote, or a number a document gives.
Every name in them is checked against a fixed vocabulary (stages, kinds,
roles, statuses, reasons, the anomaly checks) or the model manifest
(properties and item keys), and one it does not know is counted as
OTHER; an exception is named by its class alone. No document appears in
them, not even by its SHA-1, and neither does the never-ingest list's
hash, which a short list could be recovered from by guessing, nor the
hash of the code's difference from its commit, which a small difference
could be confirmed by. The aggregates count documents per language, but
report.md leaves that table out and says the counts are in the local
report alone: the codes would list the languages the corpus is written
in, which the operator keeps private (their anomaly thresholds name
them). No other line of report.md names a language: an alert is counted
by its check and stage, never by its value or limit.

Some counts need what the run directory does not hold, and the report
says so (NOT_AVAILABLE) rather than count none: depicts proposals, since
face detection is never a stage of a scratch run; the place stage's
questions and links, until the run's place stage has run (its record in
CORPUS_DIR/places/ gives them); different-source contradictions, which
only a written graph holds; and flags by tier and class, until a stage
makes them (`_flags` is where the report reads them). The links the
place stage refused as each would close a cycle are counted in the
aggregates and named, by their places' labels and their documents, in
report.json and report.html alone (`place_cycles`).
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from yizkr.claims import (
    JudgementNote,
    PersonStructure,
    PlaceStructure,
    load_vocabulary,
)
from yizkr.pipeline.anomalies import (
    ERROR_EVENT,
    OUTPUT_LIMIT_REACHED,
    RECHECK_EVENT,
    Checks,
    route,
)
from yizkr.pipeline.critique import AXES, CATEGORIES, Finding
from yizkr.pipeline.critique import entry as critique_entry
from yizkr.pipeline.llm import OUTPUT_LIMIT, ROLE_STAGES, USAGE_TOKENS
from yizkr.pipeline.monitor import (
    _CLASS_NAME,
    _duration,
    _exception_name,
    _seconds,
    _time,
    _value,
    failure_label,
    reason_label,
)
from yizkr.pipeline.orchestrate import (
    DOCUMENT_STAGES,
    EXCERPT,
    EXCERPTS_DIR,
    MODEL_ROLES,
)
from yizkr.pipeline.rundir import (
    ALERTS,
    CACHE_EVENTS,
    CALL,
    CONTROL,
    DOCUMENT_FAILED,
    DONE,
    EVENTS,
    FAILED,
    INTERRUPTED,
    MANIFEST,
    NEEDS_ATTENTION,
    OUTCOMES,
    PENDING,
    RUN_END,
    RUN_INTERRUPTED,
    RUN_START,
    RUNNING,
    SET_ASIDE,
    STAGE_EVENTS,
    Invocations,
    interrupted,
    invocation_of,
    invocation_records,
)
from yizkr.pipeline.scratch import BARRIER, CORPUS_DIR, CORPUS_STAGES
from yizkr.pipeline.stages import read_jsonl, staging_path

# the version of report.json's layout
FORMAT = 1
# the folder of the run directory the report is written into, and its files
REPORT_DIR = ".report"
JSON_FILE = "report.json"
HTML_FILE = "report.html"
MARKDOWN_FILE = "report.md"

# what a count shows when the run directory cannot give it, and what a
# stage no event names shows
NOT_AVAILABLE = "not available"
NOT_RUN = "not run"
# what a name the aggregates do not know is counted as
OTHER = "other"

# how a completed stage came about
COMPUTED = "computed"
REUSED = "reused"
VIEW_CURRENT = "view current"
KEY_MATCH = "key match"
# what forced a computed stage to the engine, besides a miss's reason
NO_CALL = "no call"
NO_CACHE = "no cache"
REPLAY = "replay"
REPLAY_CHANGED = "replay changed"
# a pending stage that the control file holds, as the report counts it
HELD = "held"
KILLED = "killed"
# the stage an alert on the run as a whole names
RUN = "run"
# whether the run's call events carry what the calls used
RECORDED = "recorded"

# the clustering stage and its output (in CORPUS_DIR/<stage>/)
CLUSTERING = "clustering"
IDENTITY_MAP = "identity-map.json"
IDENTITY_MAP_FORMAT = 1
# the write stage and its record of what it wrote (in CORPUS_DIR/<stage>/)
WRITE_STAGE = "write"
WRITE_RECORD = "record.json"
# the place stage, which keeps its counts and the links it refused in a
# record of the same name
PLACE_STAGE = "places"
# the counts of the place stage's record, as the report names them
PLACE_COUNTS = {
    "questions": "questions",
    "unnamed": "links_left_out_unnamed",
    "calls": "calls",
    "answered": "answered",
    "uncertain": "uncertain",
    "written": "links_written",
    "referenced": "links_referenced",
    "present": "already_present",
    "record_only": "record_only_links",
    "record_only_written": "record_only_links_written",
    "unresolved": "links_unresolved",
    "schema_rejected": "rejected_by_schema",
    "cycles": "refused_cycles",
    "conflicts": "refused_conflicts",
    "unanswered": "unanswered",
}

# how many times the report reads run.json and the events while an
# invocation starts or ends as it reads them, as run_state reads them
_READS = 5

# the view each stage makes, in a document's directory
VIEWS = {
    "transcription": "transcription.json",
    "extraction": "batch.json",
    "gedcom": "batch.json",
    "critique": "critique.json",
}

# -- the aggregates' vocabulary -----------------------------------------------
#
# Every name report.md shows comes from these, from the model manifest or
# from the anomaly checks' own names; any other name is OTHER.

KINDS = frozenset(DOCUMENT_STAGES)
DOCUMENT_STAGE_NAMES = frozenset(
    stage for stages in DOCUMENT_STAGES.values() for stage in stages
)
# the roles calls are made in: the models', and the scratch run's place
# stage, whose calls the run's events name apart
ROLES = frozenset(MODEL_ROLES) | {PLACE_STAGE}
MODES = frozenset({"scratch run", "ingest run"})
PROJECT_STAGES = frozenset({"development", "kept"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
ENGINES = frozenset({"claude-code", "anthropic"})
RUN_STATES = frozenset(OUTCOMES) | {"running", "ended", "not started"}
INVOCATION_ENDS = frozenset(OUTCOMES) | {"running", KILLED}
STATUSES = frozenset(
    {PENDING, HELD, RUNNING, INTERRUPTED, DONE, NEEDS_ATTENTION, FAILED}
) | {NOT_RUN}
ROUTES = frozenset(
    {"llm-vision", "pdf-pages", "text-file", "pptx-adapter", "correction"}
)
# the route of a deck's transcription (transcribe.DECK_ENGINE)
DECK = "pptx-adapter"
# how a page of a container read page by page is read
# (yizkr.pipeline.containers)
PAGE_METHODS = frozenset({"text-layer", "slide-xml", "vision", "none"})
RESOLUTIONS = frozenset({"record", "detected", "fallback", "override"})
HIT_REASONS = frozenset({KEY_MATCH, "adopted"})
MISS_REASONS = frozenset({"absent", "rejected", "refresh"})
FORCED = MISS_REASONS | {NO_CALL, NO_CACHE, REPLAY, REPLAY_CHANGED}
REUSE_REASONS = HIT_REASONS | {VIEW_CURRENT}
CALL_OUTCOMES = frozenset({"ok", "error", "paused", "interrupted"})
HOLDS = frozenset(
    {
        "usage limit",
        "reserve",
        "rate limit",
        "overloaded",
        "usage credits",
        "closed",
    }
)
REQUESTERS = frozenset({"operator", "monitor", "anomaly"})
ACTIONS = frozenset({"pause", "resume", "abort"})
FALLBACK_TIERS = frozenset({"manifest", "segmentation"})
# the leading labels of the reasons an extraction sets a mention record
# aside by (ADR-0035 §5): claims.set_aside_structure's `schema` and
# `dropped` (a name part dropped for an empty string, which leaves the
# record standing when what is left can stand) and
# claims.structure_problems' own
SET_ASIDE_REASONS = frozenset(
    {
        "schema",
        "dropped",
        "kind",
        "addition",
        "initial",
        "no name part",
        "no toponym",
    }
)
# the reason an entity's judgement note is set aside by (ADR-0035 §5),
# which coverage.json counts among the records' reasons, once a note
NOTE_SET_ASIDE = "note"
# a judgement note's categories (ADR-0035 §4)
NOTE_CATEGORIES = frozenset(
    get_args(JudgementNote.model_fields["category"].annotation)
)
# a mention record's label statuses (ADR-0035 §2.1, §2.2)
RECORD_STATUSES = frozenset(
    status
    for model in (PersonStructure, PlaceStructure)
    for status in get_args(model.model_fields["status"].annotation)
)
MAP_STATES = frozenset({"read", "missing", "unreadable"})
# the leading labels of the reasons stages give an output that needs
# attention: the extraction's fallback tiers and empty batches, and the
# clustering stage's clusters left unmerged
ATTENTION_LABELS = frozenset(
    {
        "entity manifest fell back to line-range segmentation",
        "segmentation fell back to line windows",
        "no claims extracted",
        "ambiguous clusters not merged",
        "conflicting clusters not merged",
        "ambiguous pairs to adjudicate",
        "hub keys not blocked on",
        # a container's image its reading does not read on its own
        # (presentation.UNREAD_IMAGE, and a PDF's image PDFium cannot give)
        "image not read on its own",
        # an image whose own reading the check failed
        # (transcribe.READING_FAILED)
        "image reading failed the check",
    }
)
# the leading labels of a document's failure outside its stages; any
# other failure is shown by the exception it names
FAILURE_LABELS = frozenset(
    {
        "its source cannot be read",
        "its state.json cannot be read",
        "its state could not be saved",
    }
)
CHECKS = frozenset(name.replace("_", "-") for name in Checks.model_fields)
CHECKS = CHECKS | {"anomaly-config"}
# the checks registration raises as alerts of the write, and the stage they
# name: a copy of yizkr.pipeline.registrar.REGISTRATION_CHECKS, which a unit
# test keeps equal to it
REGISTRATION_CHECKS = frozenset({"shared-file", "unhashed-title"})
CHECKS = CHECKS | REGISTRATION_CHECKS
WRITE = "write"

_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{1,32}")
_STAGE = re.compile(r"[a-z][a-z0-9-]{0,39}")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-(?:[a-z]{2}|[0-9]{3}))?")
# a full model id: lowercase words and numbers, a version among them
_MODEL = re.compile(
    r"[a-z][a-z0-9.-]{0,60}[0-9][a-z0-9.-]{0,20}(?:\[[a-z0-9]+\])?"
)
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9.]{1,20})?")
_HEX = re.compile(r"[0-9a-f]{12,64}")
_PREFIX = re.compile(r"[0-9a-f]{4,40}")
_QID = re.compile(r"Q[1-9][0-9]*")
_STOP = re.compile(r"(run|stage:[a-z][a-z0-9-]*|kind:[a-z]+) (paused|aborted)")
# the claims a failed extraction's reason says it could not use
# (chunking.ExtractionFailed), read as the anomaly checks read them
_FAILED_EXTRACTION = "ExtractionFailed: "
_EVERY_CLAIM_REJECTED = re.compile(
    r"validation rejected every claim \((\d+)\)"
)
_UNREAD_OBJECTS = re.compile(r"(\d+) unreadable objects? in the chunk answers")


class ReportError(RuntimeError):
    """The run directory cannot be reported: it holds no run.json, its
    event log cannot be read, or the report cannot be written."""


# -- reading ------------------------------------------------------------------


def _lines(path: Path) -> Iterator[dict]:
    """The entries of an append-only log, one at a time, as
    stages.read_jsonl reads it (split at newlines only; a blank line, a
    torn one, and one that holds no JSON object or is nested too deeply
    for the decoder are skipped), without holding the log in memory.
    Nothing for a path that holds no file; OSError for a file that cannot
    be read."""
    if not path.is_file():
        return
    with open(path, "rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                yield entry


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: Any) -> int:
    """A count an output gives: a whole number, at least 0; else 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _counted(value: Any) -> int | None:
    """A count an output gives: a whole number, at least 0; else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _tally(value: Any) -> dict[str, int] | None:
    """A {name: count} an output gives, less each entry whose count is no
    count (`_counted`); None when it gives none."""
    if not isinstance(value, dict):
        return None
    return {
        name: count
        for name, count in value.items()
        if isinstance(name, str) and _counted(count) is not None
    }


def _list(value: Any) -> list:
    """A list a file gives, or none: whatever else a field holds counts
    as nothing."""
    return value if isinstance(value, list) else []


class _Unreadable:
    """A file that is there but cannot be read, or holds no JSON."""

    def __init__(self, why: str) -> None:
        self.why = why


def _json(path: Path) -> Any:
    """The JSON the file at `path` holds; None when there is no file; an
    _Unreadable when it cannot be read or parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as exc:
        return _Unreadable(type(exc).__name__)
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        return _Unreadable(type(exc).__name__)


# -- the events ---------------------------------------------------------------


@dataclass
class Attempt:
    """One stage of one document, or one corpus stage, as its latest
    events leave it, with the calls of its latest attempt: the engine's
    (the cache's misses by reason, and replays of a conversation's
    opening turn), the cache's answers by reason, and the engine's call
    events. `made` says that the view which stands was made by an attempt
    the events hold: a later invocation that reuses it (a `reused`
    event) leaves how it was made, and its calls, as they were."""

    status: str = PENDING
    invocation: str | None = None
    since: datetime | None = None
    held: bool = False
    reason: str | None = None
    how: str | None = None
    made: bool = False
    attention: tuple[str, ...] = ()
    started: int = 0
    seconds: float | None = None
    engine: int = 0
    hits: Counter = field(default_factory=Counter)
    misses: Counter = field(default_factory=Counter)
    replays: int = 0
    changed: int = 0
    calls: int = 0

    def begin(self) -> None:
        self.engine = self.replays = self.changed = self.calls = 0
        self.hits, self.misses = Counter(), Counter()

    @property
    def shown_status(self) -> str:
        """The status, with a pending stage the control file holds shown
        as HELD."""
        return HELD if self.status == PENDING and self.held else self.status

    def reuse_reason(self) -> str:
        """How a completed attempt came about: COMPUTED when the engine
        was asked, or no call was made; else the reasons the cache
        answered with, joined."""
        if self.engine or not self.hits:
            return COMPUTED
        return " + ".join(sorted(self.hits))

    def forced(self) -> str:
        """What sent a computed attempt to the engine: its misses' reasons
        and its replays, joined; NO_CACHE for calls that reached the
        engine with no cache between; NO_CALL when it made none."""
        reasons = set(self.misses)
        if self.changed:
            reasons.add(REPLAY_CHANGED)
        if self.replays > self.changed:
            reasons.add(REPLAY)
        if not reasons:
            return NO_CACHE if self.calls else NO_CALL
        return " + ".join(sorted(reasons))


@dataclass
class _StageTotals:
    """A stage's calls over every attempt, and how long the attempts that
    ended took, start to end."""

    engine: int = 0
    cached: int = 0
    calls: int = 0
    seconds: float = 0.0
    timed: int = 0


@dataclass
class _Calls:
    """What the LLM cache did, and what the engine's call events say, over
    every invocation."""

    cache_events: int = 0
    engine: int = 0
    cached: int = 0
    hits: Counter = field(default_factory=Counter)
    misses: Counter = field(default_factory=Counter)
    resumes: int = 0
    replays: int = 0
    changed: int = 0
    rejects: int = 0
    adopts: int = 0
    roles: dict[str, Counter] = field(default_factory=dict)
    events: int = 0
    outcomes: Counter = field(default_factory=Counter)
    event_roles: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    timed: int = 0
    waited: float = 0.0
    waits: int = 0
    refused: int = 0
    holds: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    # the model each call's stream reported; and per (stage, role), as
    # the call event names them, its calls, those whose stream reported
    # what they used, and the tokens they reported
    models: Counter = field(default_factory=Counter)
    usage: dict[tuple[str, str], Counter] = field(default_factory=dict)
    # per stage, as the call event names it, the calls stopped at the
    # output limit: answers cut off there, and calls the engine gave up on;
    # and the call events that record why a call stopped at all
    limit_stops: dict[str, Counter] = field(default_factory=dict)
    stops_recorded: int = 0
    # per stage of the role a cache hit names, the hits that replayed a
    # give-up at the output limit the cache kept: no call was made
    gave_up_hits: Counter = field(default_factory=Counter)

    def role(self, name: str) -> Counter:
        counts = self.roles.get(name)
        if counts is None:
            counts = self.roles[name] = Counter()
        return counts


class _Fold:
    """What the report takes from events.jsonl, one event at a time, in
    the order the events were written."""

    def __init__(self) -> None:
        self.events = 0
        self.invocations = Invocations()
        self.attempts: dict[tuple[str, str], Attempt] = {}
        self.corpus: dict[str, Attempt] = {}
        # the stage each document is in while one runs: the cache's and
        # the engine's events name the document, not the stage
        self.current: dict[str, str] = {}
        self.stages: dict[str, _StageTotals] = {}
        self.kinds: dict[str, str] = {}
        self.calls = _Calls()
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None
        self.invocation_last_at: dict[str, datetime] = {}
        # every document-failed event, in order, and per invocation the
        # documents it failed, by file (else SHA-1): the later failure of
        # a document stands
        self.failure_events: list[dict] = []
        self.failures: dict[str | None, dict[Any, dict]] = {}
        self.failure_invocation: str | None = None
        self.set_aside: list[dict] = []
        self.barrier: dict | None = None
        self.anomaly_errors = 0
        self.rechecks = 0

    def fold(self, event: dict) -> None:
        self.events += 1
        name = event.get("event")
        at = _time(event.get("at"))
        invocation = invocation_of(event)
        if at is not None:
            if self.first_at is None or at < self.first_at:
                self.first_at = at
            if self.last_at is None or at > self.last_at:
                self.last_at = at
            if invocation is not None:
                self.invocation_last_at[invocation] = at
        if name in STAGE_EVENTS:
            self._stage(event, name, at, invocation)
        elif name in CACHE_EVENTS:
            self._cache(event, name)
        elif name == CALL:
            self._call(event)
        elif name == DOCUMENT_FAILED:
            self._failure(event, invocation)
        elif name in (RUN_START, RUN_END, RUN_INTERRUPTED):
            self.invocations.fold(event)
            if name == RUN_START:
                self.current.clear()
        elif name == SET_ASIDE:
            self.set_aside.append(
                {
                    "subject": _text(event.get("subject")),
                    "file": _text(event.get("file")),
                    "stage": _text(event.get("stage")),
                    "reason": _text(event.get("reason")),
                    "kept": _text(event.get("kept")),
                }
            )
        elif name == BARRIER:
            self.barrier = {
                "documents": _count(event.get("documents")),
                "failed": _count(event.get("failed")),
            }
        elif name == ERROR_EVENT:
            self.anomaly_errors += 1
        elif name == RECHECK_EVENT:
            self.rechecks += 1

    def _totals(self, stage: str) -> _StageTotals:
        totals = self.stages.get(stage)
        if totals is None:
            totals = self.stages[stage] = _StageTotals()
        return totals

    def _stage(
        self,
        event: dict,
        name: str,
        at: datetime | None,
        invocation: str | None,
    ) -> None:
        stage = _text(event.get("stage"))
        subject = event.get("subject")
        if stage is None:
            return
        if subject is None:
            # a corpus stage names no document
            attempt = self.corpus.setdefault(stage, Attempt())
        elif isinstance(subject, str) and subject:
            attempt = self.attempts.setdefault((subject, stage), Attempt())
            kind = _text(event.get("kind"))
            if kind is not None:
                self.kinds.setdefault(subject, kind)
        else:
            return
        self._totals(stage)
        attempt.invocation = invocation
        if name == "started":
            attempt.status, attempt.since, attempt.held = RUNNING, at, False
            attempt.reason, attempt.attention = None, ()
            attempt.made = False
            attempt.started += 1
            attempt.begin()
            if subject is not None:
                self.current[subject] = stage
            return
        if subject is not None and self.current.get(subject) == stage:
            del self.current[subject]
        if name == "held":
            attempt.status, attempt.held = PENDING, True
            attempt.reason = _text(event.get("reason"))
            return
        attempt.held = False
        seconds = None
        if name != "reused" and attempt.since is not None and at is not None:
            seconds = max(0.0, (at - attempt.since).total_seconds())
            totals = self._totals(stage)
            totals.seconds += seconds
            totals.timed += 1
        attempt.since = None
        if name == "failed":
            attempt.status, attempt.seconds = FAILED, seconds
            attempt.reason, attempt.made = _text(event.get("reason")), False
            return
        if name == "reused" and not attempt.made:
            # a view from before the run's events: how it was made is
            # not in them
            attempt.begin()
            attempt.how, attempt.seconds = VIEW_CURRENT, None
        elif name == "done":
            attempt.how, attempt.seconds = attempt.reuse_reason(), seconds
            attempt.made = True
        reasons = event.get("attention")
        if not isinstance(reasons, list):
            reasons = [reasons]
        attempt.attention = tuple(
            reason for reason in reasons if isinstance(reason, str) and reason
        )
        attempt.reason = None
        attempt.status = NEEDS_ATTENTION if attempt.attention else DONE

    def _in_flight(self, event: dict) -> tuple[Attempt | None, str | None]:
        """The attempt of the stage the event's document is in, if one is
        running: a document's stages run one at a time."""
        subject = event.get("subject")
        stage = self.current.get(subject) if isinstance(subject, str) else None
        if stage is None:
            return None, None
        return self.attempts.get((subject, stage)), stage

    def _cache(self, event: dict, name: str) -> None:
        calls = self.calls
        calls.cache_events += 1
        role = calls.role(_text(event.get("role")) or "?")
        reason = _text(event.get("reason"))
        attempt, stage = self._in_flight(event)
        totals = self._totals(stage) if stage is not None else None
        if name in ("cache-miss", "cache-replay"):
            calls.engine += 1
            role["engine"] += 1
            if totals is not None:
                totals.engine += 1
            if attempt is not None:
                attempt.engine += 1
        if name == "cache-miss":
            calls.misses[reason or "?"] += 1
            if attempt is not None:
                attempt.misses[reason or "?"] += 1
        elif name == "cache-replay":
            changed = event.get("matches") is False
            calls.replays += 1
            calls.changed += changed
            role["replayed"] += 1
            if attempt is not None:
                attempt.replays += 1
                attempt.changed += changed
        elif name == "cache-hit":
            calls.cached += 1
            calls.hits[reason or KEY_MATCH] += 1
            role["cached"] += 1
            if event.get("gave_up") is True:
                calls.gave_up_hits[
                    ROLE_STAGES.get(_text(event.get("role")) or "?", "?")
                ] += 1
            if totals is not None:
                totals.cached += 1
            if attempt is not None:
                attempt.hits[reason or KEY_MATCH] += 1
        elif name == "cache-resume":
            calls.resumes += 1
            role["resumed"] += 1
        elif name == "cache-reject":
            calls.rejects += 1
            role["rejected"] += 1
        elif name == "cache-adopt":
            calls.adopts += 1
            role["adopted"] += 1

    def _limit_stop(self, stage: str, event: dict) -> None:
        """Count a call stopped at the output limit, by its stage: a call
        that raised OutputLimitReached (gave up), told by its error alone
        as the anomaly checks tell it, or an answer whose stop reason is
        max_tokens (cut off). A call event records why its call stopped
        when it carries a stop reason or an API error, or is a give-up:
        the run's events record stops at all only when one of them does."""
        gave_up = (
            event.get("outcome") == "error"
            and event.get("error") == OUTPUT_LIMIT_REACHED
        )
        if gave_up or "stop_reason" in event or "api_error" in event:
            self.calls.stops_recorded += 1
        if gave_up:
            stop = "gave_up"
        elif event.get("stop_reason") == OUTPUT_LIMIT:
            stop = "cut_off"
        else:
            return
        self.calls.limit_stops.setdefault(stage, Counter())[stop] += 1

    def _call(self, event: dict) -> None:
        calls = self.calls
        calls.events += 1
        calls.outcomes[_text(event.get("outcome")) or "?"] += 1
        calls.event_roles[_text(event.get("role")) or "?"] += 1
        duration = _seconds(event.get("duration_s"))
        if duration is not None:
            calls.seconds += duration
            calls.timed += 1
        wait = _seconds(event.get("wait_s"))
        if wait:
            calls.waited += wait
            calls.waits += 1
        refused = event.get("refused")
        if isinstance(refused, int) and not isinstance(refused, bool):
            calls.refused += max(0, refused)
        held = _text(event.get("held"))
        if held is not None:
            calls.holds[held] += 1
        error = _text(event.get("error"))
        if error is not None:
            calls.errors[failure_label(error)] += 1
        model = _text(event.get("model"))
        if model is not None:
            calls.models[model] += 1
        where = (
            _text(event.get("stage")) or "?",
            _text(event.get("role")) or "?",
        )
        used = calls.usage.setdefault(where, Counter())
        used["calls"] += 1
        self._limit_stop(where[0], event)
        usage = event.get("usage")
        tokens = {
            name: usage[name]
            for name in USAGE_TOKENS
            if isinstance(usage, dict)
            and isinstance(usage.get(name), int)
            and not isinstance(usage[name], bool)
            and usage[name] >= 0
        }
        if tokens:
            used["reported"] += 1
            used.update(tokens)
        attempt, stage = self._in_flight(event)
        if stage is not None:
            self._totals(stage).calls += 1
        if attempt is not None:
            attempt.calls += 1

    def _failure(self, event: dict, invocation: str | None) -> None:
        file, subject = _text(event.get("file")), _text(event.get("subject"))
        failure = {
            "file": file,
            "subject": subject,
            "kind": _text(event.get("kind")),
            "reason": _text(event.get("reason")),
            "invocation": invocation,
        }
        self.failure_events.append(failure)
        key: Any = file or subject or object()
        self.failures.setdefault(invocation, {})[key] = failure
        self.failure_invocation = invocation


def _manifest(path: Path) -> dict:
    """The run manifest, run.json, which the driver replaces whole:
    ReportError when it cannot be read or holds no JSON object, since the
    plan and the invocations the report reads the run by are in it."""
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ReportError(
            f"{path / MANIFEST} cannot be read ({type(exc).__name__}), and "
            "the report reads the run by its plan and its invocations"
        ) from None
    if not isinstance(manifest, dict):
        raise ReportError(f"{path / MANIFEST} holds no JSON object")
    return manifest


def _read(path: Path) -> tuple[_Fold, dict]:
    """The run's events, folded, and a reading of run.json that holds for
    them: run.json is read before the events and again after them, and
    all of it again while the two readings differ in the invocations they
    record, _READS times at most, as rundir.run_state reads them."""
    for _ in range(_READS):
        before = _manifest(path)
        fold = _Fold()
        try:
            for event in _lines(path / EVENTS):
                fold.fold(event)
        except OSError as exc:
            raise ReportError(
                f"{path / EVENTS} cannot be read ({type(exc).__name__}), "
                "and the report is made from the run's events"
            ) from None
        manifest = _manifest(path)
        if invocation_records(before) == invocation_records(manifest):
            break
    return fold, manifest


# -- the documents ------------------------------------------------------------


@dataclass
class Document:
    """One document of the run: its SHA-1 and kind, the files planned with
    its bytes, its stages as the events leave them, what its views hold,
    the views that could not be read, and its alerts, failures (every
    document-failed event that names it) and set-asides."""

    sha1: str
    kind: str | None
    files: list[str] = field(default_factory=list)
    planned: bool = True
    canary: bool = False
    stages: dict[str, Attempt] = field(default_factory=dict)
    directory: str | None = None
    views: dict[str, Any] = field(default_factory=dict)
    unread: dict[str, str] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    set_aside: list[dict] = field(default_factory=list)
    # whether the document counts as failed (_failed), and the claims a
    # failed extraction's reason says it could not use
    failed: bool = False
    unusable: int | None = None

    def completed(self, stage: str) -> bool:
        attempt = self.stages.get(stage)
        return attempt is not None and attempt.status in (
            DONE,
            NEEDS_ATTENTION,
        )


def _plan(manifest: Mapping[str, Any]) -> tuple[dict[str, Document], int]:
    """The documents run.json plans, by SHA-1 in plan order, and how many
    entries plan them: a SHA-1 planned twice (two files with the same
    bytes) is one document, with the stages of both and the first one's
    kind, as the live monitor reads the plan."""
    documents: dict[str, Document] = {}
    entries = 0
    planned = manifest.get("documents")
    for entry in planned if isinstance(planned, list) else []:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("sha1"), str
        ):
            continue
        entries += 1
        sha1 = entry["sha1"]
        document = documents.get(sha1)
        if document is None:
            document = documents[sha1] = Document(
                sha1=sha1, kind=_text(entry.get("kind"))
            )
        if _text(entry.get("file")):
            document.files.append(entry["file"])
        stages = entry.get("stages")
        for stage in stages if isinstance(stages, list) else []:
            if isinstance(stage, str):
                document.stages.setdefault(stage, Attempt())
    scratch = manifest.get("scratch")
    canaries = scratch.get("canaries") if isinstance(scratch, dict) else None
    for sha1 in canaries if isinstance(canaries, list) else []:
        if isinstance(sha1, str) and sha1 in documents:
            documents[sha1].canary = True
    return documents, entries


def _directories(path: Path) -> dict[str, tuple[Path, dict]]:
    """content SHA-1 -> (the directory that holds the document's views, its
    state), from each directory of the run directory whose state.json
    names the SHA-1 its document was last processed at. A directory set
    aside, whose name starts with a dot, is passed over; where two name
    one SHA-1 (a file with another's bytes), the one that is no duplicate
    holds the views."""
    found: dict[str, tuple[Path, dict]] = {}
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return found
    # an excerpt's directory is in EXCERPTS_DIR, named for its image
    with contextlib.suppress(OSError):
        entries += sorted((path / EXCERPTS_DIR).iterdir())
    for entry in entries:
        if entry.name.startswith(".") or entry.name in (
            CORPUS_DIR,
            EXCERPTS_DIR,
        ):
            continue
        state = _json(entry / "state.json")
        if not isinstance(state, dict):
            continue
        sha1 = _text(state.get("content_sha1"))
        if sha1 is None:
            continue
        if sha1 in found and state.get("status") == "duplicate":
            continue
        found[sha1] = (entry, state)
    return found


def _view(document: Document, stage: str, path: Path, kind: type) -> Any:
    """A view of the document when the file holds a `kind`; None, with
    why noted under the stage in `document.unread`, otherwise."""
    value = _json(path)
    if value is None:
        document.unread[stage] = f"{path.name} is missing"
    elif isinstance(value, _Unreadable):
        document.unread[stage] = f"{path.name} cannot be read ({value.why})"
    elif not isinstance(value, kind):
        document.unread[stage] = f"{path.name} holds no {kind.__name__}"
    else:
        return value
    return None


def _pages(pages: Any) -> dict[str, Any]:
    """A container read page by page (a PDF's pages, a deck's slides), as
    its transcription's `pages` give it: its pages by the method that read
    them, how many of its text-layer pages show annotations the text layer
    leaves unread, and the images placed on its pages that were read on
    their own, by their SHA-1s, each once: its derived images (ADR-0008).
    Pages whose provenance cannot be read are not counted, and neither is
    a name of an image that is no SHA-1."""
    methods: Counter = Counter()
    annotated = 0
    images: set[str] = set()
    for page in _list(pages):
        if isinstance(page, dict):
            images.update(
                sha1
                for sha1 in _list(page.get("images"))
                if isinstance(sha1, str) and re.fullmatch("[0-9a-f]{40}", sha1)
            )
        provenance = page.get("provenance") if isinstance(page, dict) else None
        if not isinstance(provenance, dict):
            continue
        method = _known(provenance.get("method"), PAGE_METHODS)
        methods[method] += 1
        unread = provenance.get("annotations")
        if (
            method == "text-layer"
            and isinstance(unread, int)
            and not isinstance(unread, bool)
            and unread > 0
        ):
            annotated += 1
    return {
        "page_methods": dict(methods),
        "annotated_pages": annotated,
        "images": sorted(images),
    }


def _read_views(document: Document, directory: Path, state: dict) -> None:
    """What the document's views hold, for each stage that completed: the
    transcription's language, route and how its language was resolved;
    the batch's entities by kind and claims by property; the extraction's
    coverage (chunks, rejects, fallback tiers, and the mention records
    and judgement notes it counts: None for both unless it counts both);
    and the critique's findings by class. A view that is missing or
    cannot be read counts nothing, and is noted."""
    document.directory = directory.name
    views = document.views
    detail = state.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    if document.completed("transcription"):
        found = _view(
            document, "transcription", directory / VIEWS["transcription"], dict
        )
        if found is not None:
            language = detail.get("language")
            views["transcription"] = {
                "language": _text(found.get("language")),
                "route": route(found.get("engine")),
                "resolved": _text(language.get("source"))
                if isinstance(language, dict)
                else None,
                **_pages(found.get("pages")),
            }
    stage = next(
        (s for s in ("extraction", "gedcom") if document.completed(s)), None
    )
    batch = (
        _view(document, stage, directory / VIEWS[stage], dict)
        if stage is not None
        else None
    )
    if batch is not None:
        entities = [
            e for e in _list(batch.get("entities")) if isinstance(e, dict)
        ]
        claims = [c for c in _list(batch.get("claims")) if isinstance(c, dict)]
        views["batch"] = {
            "stage": stage,
            "entities": Counter(_text(e.get("kind")) or "?" for e in entities),
            "not_extracted": sum(e.get("extract") is False for e in entities),
            "claims": Counter(_text(c.get("property")) or "?" for c in claims),
        }
        coverage = _json(directory / "extraction" / "coverage.json")
        if stage == "extraction" and isinstance(coverage, dict):
            fallback = coverage.get("fallback")
            views["coverage"] = {
                "chunks": _count(coverage.get("chunks")),
                "rejected_claims": _count(coverage.get("rejected_claims")),
                "unread_objects": _count(coverage.get("unread_objects")),
                "fallback": sorted(
                    tier for tier in fallback if isinstance(tier, str)
                )
                if isinstance(fallback, dict)
                else [],
                **_records(coverage),
            }
    if document.completed("critique"):
        findings = _view(
            document, "critique", directory / VIEWS["critique"], list
        )
        if findings is not None:
            views["critique"] = _critique(findings)


def _records(coverage: dict) -> dict[str, Any]:
    """The mention records and judgement notes an extraction's coverage
    counts (ADR-0035 §4, §5): the records given, by status, and the
    unsplit name forms they hold, the mentions recordless, the records set
    aside and the reasons they were set aside by, by category, and the
    labels that were appellations and became names, and the record name
    parts that are appellations (ADR-0018); and the notes by category,
    and the notes set aside. Each name is folded into
    the report's fixed vocabulary, OTHER for one it does not know.

    The coverage's `set_aside` counts reasons, not records: a record that
    does not parse counts once, as `schema`, one of the wrong kind once,
    as `kind`, and any other once for each problem found in it; an
    entity's note set aside counts once, as `note`, which is told apart
    here as a note set aside.
    The extraction writes its records and its notes together, so both
    are None unless the coverage holds both, as the line-range tier's and
    one written before these counts do not: absent, never zero. The
    records set aside, the unsplit forms, the appellation labels and the
    appellation parts are each None when the coverage, though it holds
    both, gives no count of them."""
    records, notes = coverage.get("records"), _tally(coverage.get("notes"))
    if not isinstance(records, dict) or notes is None:
        return {"records": None, "notes": None}
    reasons = _tally(records.get("set_aside")) or {}
    noted = reasons.pop(NOTE_SET_ASIDE, 0)
    return {
        "records": {
            "given": _count(records.get("given")),
            "recordless": _count(records.get("recordless")),
            "by_status": _folded(
                _tally(records.get("by_status")) or {},
                lambda value: _known(value, RECORD_STATUSES),
            ),
            "unsplit_forms": _counted(records.get("unsplit_forms")),
            "set_aside_records": _counted(records.get("set_aside_records")),
            "appellation_labels": _counted(records.get("appellation_labels")),
            "appellation_parts": _counted(records.get("appellation_parts")),
            "set_aside_by_reason": _folded(
                reasons, lambda value: _known(value, SET_ASIDE_REASONS)
            ),
        },
        "notes": {
            "by_category": _folded(
                notes, lambda value: _known(value, NOTE_CATEGORIES)
            ),
            "set_aside": noted,
        },
    }


def _uncounted(coverage: Mapping[str, Any]) -> str:
    """Why an extraction's coverage counts no mention records or notes:
    `line_range` when its entity manifest fell back to line ranges, a tier
    that counts none and leaves every mention recordless; else
    `predating`, a coverage written before these counts (or holding only
    one half of them, which the extraction never writes)."""
    return "line_range" if "manifest" in coverage["fallback"] else "predating"


def _critique(findings: list) -> dict[str, Any]:
    """A critique view counted: its valid findings by the statement-part
    each doubts (ADR-0023) and by category, and the findings that failed
    their checks (or are no finding at all) as invalid."""
    counted: dict[str, Any] = {
        "findings": 0,
        "invalid": 0,
        "by_axis": Counter(),
        "by_category": Counter(),
    }
    for stored in findings:
        try:
            found = (
                critique_entry(stored) if isinstance(stored, dict) else None
            )
        except (ValueError, TypeError):
            found = None
        if not isinstance(found, Finding):
            counted["invalid"] += 1
            continue
        counted["findings"] += 1
        counted["by_axis"][found.axis] += 1
        counted["by_category"][found.category] += 1
    return counted


@dataclass
class _Alerts:
    """The anomaly alerts, counted a line at a time: the lines per check,
    and each finding once (a document, or the run, a stage and a check),
    new when any of its alerts was new, else carried."""

    lines: int = 0
    by_check: Counter = field(default_factory=Counter)
    findings: dict[tuple[str, str, str], str] = field(default_factory=dict)


def _alerts(path: Path, documents: dict[str, Document]) -> _Alerts | None:
    """The run's alerts, counted, each one that names a planned document by
    the start of its SHA-1 added to it; None when alerts.jsonl cannot be
    read."""
    counts = _Alerts()
    known = sorted(documents)
    try:
        for alert in _lines(path / ALERTS):
            counts.lines += 1
            check = _text(alert.get("check")) or "?"
            stage = _text(alert.get("stage")) or "?"
            status = "new" if alert.get("status") == "new" else "carried"
            subject = _text(alert.get("subject")) or ""
            sha1 = _match(subject, known)
            counts.by_check[check] += 1
            key = (sha1 or subject, stage, check)
            if counts.findings.get(key) != "new":
                counts.findings[key] = status
            if sha1 is not None:
                documents[sha1].alerts.append(
                    {
                        "at": _text(alert.get("at")),
                        "stage": stage,
                        "check": check,
                        "status": status,
                        "value": _value(alert.get("value")),
                        "limit": _value(alert.get("limit")),
                        # a registration flag's items, by QID
                        "artifact": _qid(alert.get("artifact")),
                        "others": _qids(alert.get("others")),
                    }
                )
    except OSError:
        return None
    return counts


def _qid(value: Any) -> str | None:
    """`value` when it is an item's QID, else None."""
    return value if isinstance(value, str) and _QID.fullmatch(value) else None


def _qids(value: Any) -> list[str] | None:
    """The QIDs among `value`, a list, or None when it is not one."""
    if not isinstance(value, list):
        return None
    return [qid for qid in map(_qid, value) if qid is not None]


def _match(subject: str, known: list[str]) -> str | None:
    """The one SHA-1 in `known` (sorted) that `subject` is, or is a hex
    prefix of; None otherwise."""
    text = subject.lower()
    if not _PREFIX.fullmatch(text):
        return None
    index = bisect_left(known, text)
    if index < len(known) and known[index].startswith(text):
        after = index + 1
        if after == len(known) or not known[after].startswith(text):
            return known[index]
    return None


# -- the report ---------------------------------------------------------------


def build(path: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """The report of the run directory at `path`, as report.json holds it:
    the run, its `aggregate` (what report.md shows, but for the documents
    per language), each document, the documents that failed before their
    source was read, the corpus stages and the control requests.
    ReportError when the directory holds no run.json, or its event log
    cannot be read."""
    path = Path(path)
    now = now or datetime.now(UTC)
    if not (path / MANIFEST).is_file():
        raise ReportError(f"{path}: no {MANIFEST} there, so no run to report")
    fold, manifest = _read(path)
    latest, ended = fold.invocations.latest(manifest)
    documents, entries = _plan(manifest)
    for (sha1, stage), attempt in fold.attempts.items():
        if sha1 not in documents:
            documents[sha1] = Document(
                sha1=sha1, kind=fold.kinds.get(sha1), planned=False
            )
        documents[sha1].stages[stage] = attempt
    attempts = [a for d in documents.values() for a in d.stages.values()]
    for attempt in [*attempts, *fold.corpus.values()]:
        if attempt.status == RUNNING and interrupted(
            attempt.invocation, latest, ended
        ):
            attempt.status = INTERRUPTED
    directories = _directories(path)
    for sha1, document in documents.items():
        if sha1 in directories:
            _read_views(document, *directories[sha1])
        else:
            for stage in document.stages:
                if document.completed(stage) and stage in VIEWS:
                    document.unread[stage] = "no directory holds its views"
    for record in fold.set_aside:
        if record["subject"] in documents:
            documents[record["subject"]].set_aside.append(record)
    for failure in fold.failure_events:
        if failure["subject"] in documents:
            documents[failure["subject"]].failures.append(failure)
    failed = _failed(documents, fold, latest)
    for document in documents.values():
        document.unusable = _unusable(document.stages.get("extraction"))
    alerts = _alerts(path, documents)
    control = _control(path)
    vocabulary = _Vocabulary.of(manifest)
    run = _run(manifest, fold, ended, now)
    clusters = _clusters(path, fold)
    written = _written(path)
    placed = _place_stage(path)
    return {
        "format": FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "run": run,
        "aggregate": _aggregate(
            run=run,
            fold=fold,
            documents=documents,
            entries=entries,
            failed=failed,
            alerts=alerts,
            control=control,
            clusters=clusters,
            flags=_flags(path),
            vocabulary=vocabulary,
            now=now,
            written=written,
            placed=placed,
        ),
        "documents": [_document_record(d) for d in documents.values()],
        "failed_unread": [
            {key: failure[key] for key in ("file", "kind", "reason")}
            for failure in failed
            if failure["subject"] is None
        ],
        "corpus_stages": {
            stage: _attempt_record(attempt)
            for stage, attempt in fold.corpus.items()
        },
        "clusters": clusters,
        "control": control,
        "place_cycles": _place_cycles(placed, documents),
    }


def _failed(
    documents: dict[str, Document], fold: _Fold, latest: str | None
) -> list[dict]:
    """The documents that failed, each once with the reason it failed
    for: each whose stage failed, as its latest events leave it, and each
    that the latest invocation failed outside its stages, a document whose
    source could not be read (and so has no SHA-1) among them, as the
    live monitor counts them. Each is marked `failed` among `documents`
    when it is one of them; `planned` says whether the run plans it, and
    `kind` is the kind the plan gives it, else the one its event does."""
    failed: dict[Any, dict] = {}
    for document in documents.values():
        stage = next(
            (a for a in document.stages.values() if a.status == FAILED), None
        )
        if stage is not None:
            failed[document.sha1] = {
                "subject": document.sha1,
                "file": document.files[0] if document.files else None,
                "kind": document.kind,
                "reason": stage.reason,
            }
    invocation = latest if latest is not None else fold.failure_invocation
    for key, failure in (fold.failures.get(invocation) or {}).items():
        subject = failure["subject"]
        if subject is not None and subject in failed:
            continue
        failed[subject or key] = dict(failure)
    for failure in failed.values():
        document = documents.get(failure["subject"])
        failure["planned"] = document is not None and document.planned
        if document is not None:
            document.failed = True
            failure["kind"] = document.kind or failure["kind"]
    return list(failed.values())


def _unusable(attempt: Attempt | None) -> int | None:
    """The claims a failed extraction's reason says it could not use:
    every one validation rejected, and the objects the parser could not
    read (chunking.ExtractionFailed), as the anomaly checks read them;
    None for any other stage's state or reason. The reason can quote a
    model's answer, so the number stays with its document, local."""
    reason = attempt.reason if attempt is not None else None
    if (
        attempt is None
        or attempt.status != FAILED
        or not reason
        or not reason.startswith(_FAILED_EXTRACTION)
    ):
        return None
    unusable = sum(map(int, _EVERY_CLAIM_REJECTED.findall(reason)))
    unusable += sum(map(int, _UNREAD_OBJECTS.findall(reason)))
    return unusable or None


def _control(path: Path) -> list[dict] | None:
    """The control file's requests (its start markers left out); None when
    it cannot be read."""
    try:
        requests = read_jsonl(path / CONTROL)
    except (OSError, ValueError):
        return None
    return [
        {
            "at": _text(request.get("at")),
            "action": _text(request.get("action")),
            "scope": _text(request.get("scope")) or "run",
            "by": _text(request.get("by")),
            "reason": _text(request.get("reason")),
        }
        for request in requests
        if request.get("action") != "start"
    ]


def _attempt_record(attempt: Attempt) -> dict[str, Any]:
    completed = attempt.status in (DONE, NEEDS_ATTENTION)
    return {
        "status": attempt.shown_status,
        "how": attempt.how if completed else None,
        "forced": attempt.forced()
        if completed and attempt.how == COMPUTED
        else None,
        "attention": list(attempt.attention),
        "reason": attempt.reason,
        "started": attempt.started,
        "seconds": attempt.seconds,
        "engine": attempt.engine,
        "cached": sum(attempt.hits.values()),
        "hits": dict(attempt.hits),
        "misses": dict(attempt.misses),
        "replays": attempt.replays,
        "calls": attempt.calls,
        "invocation": attempt.invocation,
    }


def _document_record(document: Document) -> dict[str, Any]:
    views = document.views
    batch = views.get("batch")
    return {
        "sha1": document.sha1,
        "kind": document.kind,
        "files": document.files,
        "planned": document.planned,
        "canary": document.canary,
        "directory": document.directory,
        "stages": {
            stage: _attempt_record(attempt)
            for stage, attempt in document.stages.items()
        },
        "transcription": views.get("transcription"),
        "batch": {
            "stage": batch["stage"],
            "entities": dict(batch["entities"]),
            "not_extracted": batch["not_extracted"],
            "claims": dict(batch["claims"]),
        }
        if batch is not None
        else None,
        "coverage": views.get("coverage"),
        "critique": {
            key: dict(value) if isinstance(value, Counter) else value
            for key, value in views["critique"].items()
        }
        if "critique" in views
        else None,
        "unread": dict(document.unread),
        "failed": document.failed,
        "unusable": document.unusable,
        "alerts": document.alerts,
        "failures": document.failures,
        "set_aside": document.set_aside,
    }


def _run(
    manifest: Mapping[str, Any], fold: _Fold, ended: bool, now: datetime
) -> dict[str, Any]:
    """The run as report.json holds it: what shaped it, and each
    invocation's span, outcome and reason. An invocation that has not
    ended runs to now when it is the latest, and was killed otherwise,
    running to its last event, as the live monitor counts the run's
    time."""
    records = invocation_records(manifest)
    invocations = []
    for index, entry in enumerate(records):
        name = invocation_of(entry)
        closing = fold.invocations.closing.get(name) or {}
        start = _time(entry.get("started_at"))
        end = _time(entry.get("ended_at"))
        running = index == len(records) - 1 and not ended
        killed = end is None and not running and not closing
        if end is None:
            end = now if running else fold.invocation_last_at.get(name or "")
            end = end or start
        span = (
            max(0.0, (end - start).total_seconds())
            if start is not None and end is not None
            else None
        )
        outcome = _text(entry.get("outcome")) or _text(closing.get("outcome"))
        invocations.append(
            {
                "invocation": name,
                "started_at": _text(entry.get("started_at")),
                "ended_at": _text(entry.get("ended_at")),
                "end": outcome
                or ("running" if running else KILLED if killed else "ended"),
                "reason": _text(entry.get("reason"))
                or _text(closing.get("reason")),
                "seconds": span,
            }
        )
    if not records:
        # a run.json that records no invocation: its events tell
        marker = fold.invocations.marker or ""
        state = {
            RUN_START: "running",
            RUN_END: "finished",
            RUN_INTERRUPTED: "interrupted",
        }.get(marker, "not started")
        reason = None
    elif not ended:
        state, reason = "running", None
    else:
        state = invocations[-1]["end"]
        reason = invocations[-1]["reason"]
    scratch = manifest.get("scratch")
    scratch = scratch if isinstance(scratch, dict) else {}

    def section(name: str) -> dict | None:
        value = manifest.get(name)
        return value if isinstance(value, dict) else None

    return {
        "run_id": _text(manifest.get("run_id")),
        "mode": _text(manifest.get("mode")),
        "project_stage": _text(manifest.get("project_stage")),
        "state": state,
        "reason": reason,
        "complete": scratch.get("complete")
        if isinstance(scratch.get("complete"), bool)
        else None,
        "until": _text(scratch.get("until")),
        "past_barrier_only": scratch.get("past_barrier_only") is True,
        "past_barrier_code": scratch.get("code")
        if isinstance(scratch.get("code"), dict)
        else None,
        "corpus_stages": [
            stage
            for stage in _list(scratch.get("corpus_stages"))
            if isinstance(stage, str)
        ],
        "started_at": _text(manifest.get("started_at")),
        "ended_at": _text(manifest.get("ended_at")),
        "seconds": sum(i["seconds"] or 0.0 for i in invocations)
        if invocations
        else None,
        "first_event_at": fold.first_at.isoformat() if fold.first_at else None,
        "last_event_at": fold.last_at.isoformat() if fold.last_at else None,
        "events": fold.events,
        "invocations": invocations,
        "code": section("code"),
        "engine": section("engine"),
        "roles": section("roles"),
        "cache": _text(manifest.get("cache")),
        "corpus": section("corpus"),
        "barrier": fold.barrier,
    }


def _written(path: Path) -> dict[str, Any] | None:
    """The write stage's record of what it wrote (yizkr.pipeline.
    scratchwrite), when the run directory holds one it reads."""
    found = _json(path / CORPUS_DIR / WRITE_STAGE / WRITE_RECORD)
    if not isinstance(found, dict) or not isinstance(
        found.get("depicts_left_out"), int
    ):
        return None
    return found


def _depicts(written: Mapping[str, Any] | None) -> dict[str, Any]:
    """The depicts claims the write stage left out of the batches: no
    scratch run writes one (ADR-0025 §1)."""
    if written is None:
        return {
            "status": NOT_AVAILABLE,
            "why": "no write stage of this run has run, and the write "
            "counts the depicts claims it leaves out",
        }
    return {"left_out": _count(written["depicts_left_out"])}


def _aggregate_write(written: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the write stage wrote, in counts alone."""
    if written is None:
        return {
            "status": NOT_AVAILABLE,
            "why": "no write stage of this run has run",
        }
    if written.get("skipped"):
        return {"skipped": True}
    documents = [
        record
        for record in (written.get("documents") or {}).values()
        if isinstance(record, dict)
    ]

    def total(key: str) -> int:
        return sum(
            _count(record.get(key))
            for record in documents
            if isinstance(record.get(key), int)
        )

    counted = {
        "skipped": False,
        "documents_written": len(documents),
        "statements_written": total("written"),
        "statements_referenced": total("referenced"),
        "claims_skipped": total("skipped"),
        "claims_refused": total("refused"),
        "items_minted": total("items_minted"),
        "batches_refused": len(written.get("refused") or {}),
        "documents_failed": len(written.get("failed") or {}),
        "contents_recorded": bool(written.get("recorded")),
    }
    # of the claims refused, those whose item value is their subject's
    # item (yizkr.pipeline.writer.self_references): a record of a write
    # that did not count them holds none, so the figure is absent, not 0
    if not documents or any(
        isinstance(record.get("self_references"), int) for record in documents
    ):
        counted["claims_self_referencing"] = total("self_references")
    return counted


def _place_stage(path: Path) -> dict[str, Any] | None:
    """The place stage's record (yizkr.pipeline.scratchwrite), when the run
    directory holds one with its counts, or one of a stage that skipped."""
    found = _json(path / CORPUS_DIR / PLACE_STAGE / WRITE_RECORD)
    if not isinstance(found, dict):
        return None
    if found.get("skipped") is True or isinstance(found.get("counts"), dict):
        return found
    return None


def _place_cycles(
    placed: Mapping[str, Any] | None, documents: Mapping[str, Document]
) -> list[dict[str, str]]:
    """Each link the place stage refused as it would close a cycle, by the
    labels of its two places and the document that states it: for the
    local report alone, as labels and file names are local-only data."""
    if placed is None or placed.get("skipped"):
        return []
    found = []
    for refused in placed.get("refused") or []:
        if not isinstance(refused, dict):
            continue
        sha1 = refused.get("sha1")
        document = documents.get(sha1) if isinstance(sha1, str) else None
        found.append(
            {
                "place": str(refused.get("child_label") or ""),
                "container": str(refused.get("parent_label") or ""),
                "document": (
                    f"{sha1[:12]} {document.files[0]}"
                    if document is not None and document.files
                    else str(sha1 or "")[:12]
                ),
            }
        )
    return found


def _aggregate_places(placed: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the place stage asked and wrote, in counts alone."""
    if placed is None:
        return {
            "status": NOT_AVAILABLE,
            "why": "no place stage of this run has run to its end",
        }
    if placed.get("skipped"):
        return {"skipped": True}
    counts = placed["counts"]
    return {
        "skipped": False,
        **{
            shown: _count(counts.get(key))
            for key, shown in PLACE_COUNTS.items()
        },
        "contents_recorded": bool(placed.get("recorded")),
    }


def _clusters(path: Path, fold: _Fold) -> dict[str, Any]:
    """The clustering stage: its state, and the counts the summary of its
    identity map gives, when the run directory holds a map of the format
    the report reads; NOT_RUN when the run has no such stage."""
    attempt = fold.corpus.get(CLUSTERING)
    found = _json(path / CORPUS_DIR / CLUSTERING / IDENTITY_MAP)
    record: dict[str, Any] = {
        "status": attempt.shown_status if attempt is not None else NOT_RUN
    }
    if found is None:
        if attempt is not None:
            record["map"] = "missing"
        return record
    summary = found.get("summary") if isinstance(found, dict) else None
    if (
        not isinstance(found, dict)
        or found.get("format") != IDENTITY_MAP_FORMAT
        or not isinstance(summary, dict)
    ):
        record["map"] = "unreadable"
        return record

    def number(*keys: str) -> int | None:
        value: Any = summary
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        return _count(value) if isinstance(value, int) else None

    record["map"] = "read"
    record["counts"] = {
        "documents": number("documents"),
        "mentions": number("mentions"),
        "clustered mentions": number("clustered_mentions"),
        "unclustered mentions": number("unclustered_mentions"),
        "recordless mentions": number("recordless_mentions"),
        "identity clusters": number("identity_clusters"),
        "confident merges": number("clusters", "confident"),
        "mentions merged": number("cluster_mentions", "confident"),
        "ambiguous, left split": number("clusters", "ambiguous"),
        "ambiguous mentions": number("cluster_mentions", "ambiguous"),
        "conflicting": number("clusters", "conflicting"),
        "conflicting mentions": number("cluster_mentions", "conflicting"),
        "conflicting, hard": number("conflicting_hard"),
        "singletons": number("singletons"),
        "candidate pairs": number("candidate_pairs"),
        "confident pairs": number("confident_pairs"),
        "ambiguous pairs": number("ambiguous_pairs"),
        "adjudication calls": number("adjudication", "calls_ambiguous_pairs"),
        "hub keys, not blocked on": number("hub_keys"),
        "mentions holding them": number("hub_mentions"),
    }
    by_kind = summary.get("clustered_by_kind")
    record["by_kind"] = (
        {k: _count(v) for k, v in by_kind.items() if isinstance(k, str)}
        if isinstance(by_kind, dict)
        else {}
    )
    return record


def _flags(path: Path) -> dict[str, Any]:
    """Flag counts by tier and class. No stage of a run makes flags yet,
    so there are none to read: the report says they are not available,
    rather than count none. This is where the report reads the flags a
    stage keeps among its outputs in the run directory at `path`, once
    one does."""
    return {"status": NOT_AVAILABLE, "why": "no stage of this run makes flags"}


# -- the aggregates -----------------------------------------------------------


@dataclass(frozen=True)
class _Vocabulary:
    """The stages the aggregates may name (the per-document ones, the
    corpus stages registered, and those the run records), and the model
    manifest's properties and item keys."""

    stages: frozenset[str]
    properties: frozenset[str]
    items: frozenset[str]

    @classmethod
    def of(cls, manifest: Mapping[str, Any]) -> _Vocabulary:
        scratch = manifest.get("scratch")
        recorded = (
            scratch.get("corpus_stages") if isinstance(scratch, dict) else None
        )
        corpus = {stage.name for stage in CORPUS_STAGES} | {CLUSTERING}
        corpus |= {
            stage
            for stage in (recorded if isinstance(recorded, list) else [])
            if isinstance(stage, str) and _STAGE.fullmatch(stage)
        }
        properties, items = load_vocabulary()
        return cls(
            stages=DOCUMENT_STAGE_NAMES | frozenset(corpus),
            properties=frozenset(properties),
            items=frozenset(items),
        )

    def stage(self, name: Any) -> str:
        return _known(name, self.stages)


def _known(value: Any, names: Iterable[str]) -> str:
    """`value` when it is one of `names`, else OTHER."""
    return value if isinstance(value, str) and value in names else OTHER


def _kind(value: Any) -> str:
    return _known(value, KINDS)


def _joined(value: Any, names: Iterable[str]) -> str:
    """Reasons joined by ` + ` (`adopted + key match`), when each is one
    of `names`; else OTHER."""
    names = frozenset(names)
    if isinstance(value, str) and all(
        part in names for part in value.split(" + ")
    ):
        return value
    return OTHER


def _language(value: Any) -> str:
    if isinstance(value, str) and _LANGUAGE.fullmatch(value):
        return value
    return OTHER


def _attention_label(reason: Any) -> str:
    return _known(reason_label(reason), ATTENTION_LABELS)


def _exception(name: Any) -> str | None:
    """The class an exception's name names, without the module path
    before it (`ExtractionFailed` for `yizkr.pipeline.chunking.
    ExtractionFailed`); None when `name` is no exception's name
    (monitor._exception_name)."""
    if not isinstance(name, str) or not _exception_name(name):
        return None
    return _CLASS_NAME.fullmatch(name).group(1)


def _failure_label(reason: Any) -> str:
    """A failure as the aggregates show it: the class of the exception it
    names, or the label of a document's failure outside its stages; else
    OTHER."""
    label = failure_label(reason)
    if label in FAILURE_LABELS:
        return label
    return _exception(label) or OTHER


def _stop_reason(reason: Any, vocabulary: _Vocabulary) -> str | None:
    """Why an invocation ended short, as the aggregates show it: the
    class of the exception it names, or the slices that stopped it
    (`kind:media aborted`), each of a known stage or kind; else OTHER;
    None for no reason."""
    if not isinstance(reason, str) or not reason:
        return None
    found = _exception(reason)
    if found is not None:
        return found
    for part in reason.split("; "):
        match = _STOP.fullmatch(part)
        what, _, name = (match.group(1) if match else "").partition(":")
        if (
            match is None
            or (what == "stage" and name not in vocabulary.stages)
            or (what == "kind" and name not in KINDS)
        ):
            return OTHER
    return reason


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _mean(total: float, count: int) -> float | None:
    return round(total / count, 3) if count else None


def _by_count(counter: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _folded(
    counter: Mapping[Any, int], known: Callable[[Any], str]
) -> dict[str, int]:
    """A counter with each name as `known` shows it (OTHER for a name it
    does not know), the most frequent first."""
    shown: Counter = Counter()
    for name, count in counter.items():
        shown[known(name)] += count
    return _by_count(shown)


def _aggregate(
    *,
    run: Mapping[str, Any],
    fold: _Fold,
    documents: dict[str, Document],
    entries: int,
    failed: list[dict],
    alerts: _Alerts | None,
    control: list[dict] | None,
    clusters: Mapping[str, Any],
    flags: Mapping[str, Any],
    vocabulary: _Vocabulary,
    now: datetime,
    written: Mapping[str, Any] | None = None,
    placed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report's aggregates, which report.md shows all of but the
    documents per language: counts, rates and times, keyed by names of
    the vocabulary alone, with OTHER for any name it does not hold. No
    document, file, path, label, excerpt, quote or free-text reason is
    ever put in them."""
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "run": _aggregate_run(run, vocabulary),
        "documents": _aggregate_documents(documents, entries, failed, fold),
        "stages": _aggregate_stages(documents, fold, vocabulary),
        "corpus_stages": _aggregate_corpus(fold, run, vocabulary),
        "anomalies": _aggregate_alerts(alerts, fold, vocabulary),
        "content": _aggregate_content(
            documents, clusters, flags, vocabulary, written, placed
        ),
        "calls": _aggregate_calls(fold, vocabulary),
        "control": _aggregate_control(control, vocabulary),
    }


def _hexed(value: Any) -> str | None:
    if isinstance(value, str) and _HEX.fullmatch(value):
        return value[:12]
    return None


def _version(value: Any) -> str | None:
    if isinstance(value, str) and _VERSION.fullmatch(value):
        return value
    return None


def _aggregate_run(
    run: Mapping[str, Any], vocabulary: _Vocabulary
) -> dict[str, Any]:
    code = run.get("code") or {}
    engine = run.get("engine") or {}
    past = run.get("past_barrier_code") or {}
    roles = {}
    for role, settings in (run.get("roles") or {}).items():
        name = _known(role, ROLES)
        if not isinstance(settings, dict) or name in roles:
            continue
        client = settings.get("client")
        client = client if isinstance(client, dict) else {}
        model = (client or settings).get("model")
        effort = (client or settings).get("effort")
        epoch = settings.get("epoch")
        roles[name] = {
            "model": (
                model
                if isinstance(model, str) and _MODEL.fullmatch(model)
                else OTHER
            )
            if model
            else None,
            "effort": _known(effort, EFFORTS) if effort else None,
            "epoch": epoch
            if isinstance(epoch, int) and not isinstance(epoch, bool)
            else None,
            "refresh": settings.get("refresh") is True,
            "config": _hexed(client.get("config_hash")),
        }
    invocations = run.get("invocations") or []
    barrier = run.get("barrier")
    return {
        "run_id": run["run_id"]
        if isinstance(run.get("run_id"), str)
        and _RUN_ID.fullmatch(run["run_id"])
        else None,
        "mode": _known(run.get("mode"), MODES) if run.get("mode") else None,
        "project_stage": _known(run.get("project_stage"), PROJECT_STAGES)
        if run.get("project_stage")
        else None,
        "state": _known(run.get("state"), RUN_STATES),
        "reason": _stop_reason(run.get("reason"), vocabulary),
        "complete": run.get("complete"),
        "until": vocabulary.stage(run["until"]) if run.get("until") else None,
        "past_barrier_only": bool(run.get("past_barrier_only")),
        "invocations": len(invocations),
        "ends": _folded(
            Counter(invocation["end"] for invocation in invocations),
            lambda value: _known(value, INVOCATION_ENDS),
        ),
        "started_at": _iso(run.get("started_at")),
        "ended_at": _iso(run.get("ended_at")),
        "seconds": run.get("seconds"),
        "barrier": {
            "documents": barrier["documents"],
            "failed": barrier["failed"],
        }
        if isinstance(barrier, dict)
        else None,
        "code": {
            "version": _version(code.get("version")),
            "commit": _hexed(code.get("commit")),
            # only whether the code differed from its commit: the hash of
            # a small difference would confirm a guess at it
            "dirty": code.get("dirty")
            if isinstance(code.get("dirty"), bool)
            else None,
            "past_barrier_commit": _hexed(past.get("commit")),
        },
        "engine": {
            "name": _known(engine.get("name"), ENGINES)
            if engine.get("name")
            else None,
            "cli_version": _version(engine.get("cli_version")),
        },
        "roles": dict(sorted(roles.items())),
    }


def _iso(value: Any) -> str | None:
    moment = _time(value)
    return moment.isoformat() if moment is not None else None


def _aggregate_documents(
    documents: dict[str, Document],
    entries: int,
    failed: list[dict],
    fold: _Fold,
) -> dict[str, Any]:
    planned = [document for document in documents.values() if document.planned]
    languages: Counter = Counter()
    language_kinds: dict[str, Counter] = {}
    routes: Counter = Counter()
    resolved: Counter = Counter()
    page_methods: Counter = Counter()
    annotated_pages = 0
    # each container's derived images, by the container's route (a PDF
    # read page by page, a deck): how many containers place how many
    # images, and the images themselves, each once however many place it
    images_per: dict[str, Counter] = {"pdf-pages": Counter(), DECK: Counter()}
    images: dict[str, set[str]] = {"pdf-pages": set(), DECK: set()}
    untranscribed = 0
    for document in planned:
        if "transcription" not in document.stages:
            continue
        transcription = document.views.get("transcription")
        if transcription is None:
            untranscribed += 1
            continue
        language = _language(transcription["language"])
        languages[language] += 1
        language_kinds.setdefault(language, Counter())[
            _kind(document.kind)
        ] += 1
        routes[_known(transcription["route"], ROUTES)] += 1
        if transcription["resolved"]:
            resolved[_known(transcription["resolved"], RESOLUTIONS)] += 1
        container = transcription["route"]
        if container not in images:
            continue
        if container == "pdf-pages":
            page_methods.update(transcription.get("page_methods") or {})
            annotated_pages += transcription.get("annotated_pages") or 0
        placed = transcription.get("images") or []
        if placed:
            images_per[container][str(len(placed))] += 1
            images[container].update(placed)
    return {
        "planned": len(planned),
        "files": entries,
        "duplicates": max(0, entries - len(planned)),
        "canaries": sum(document.canary for document in planned),
        "not_planned": len(documents) - len(planned),
        "by_kind": _folded(Counter(d.kind for d in planned), _kind),
        "languages": {
            language: {
                "documents": count,
                "by_kind": _by_count(language_kinds[language]),
            }
            for language, count in _by_count(languages).items()
        },
        "untranscribed": untranscribed,
        "routes": _by_count(routes),
        "language_resolved": _by_count(resolved),
        # the pages of the PDFs read page by page, by the method that read
        # each, and those of their text-layer pages that show annotations
        # the text layer leaves unread
        "pdf_pages": _by_count(page_methods),
        "pdf_pages_annotated": annotated_pages,
        # the images placed within the pages of the PDFs read page by page
        # and read on their own, per PDF (ADR-0008): the PDFs that place
        # any, the images they place (an image counted once per PDF), the
        # distinct images (once however many PDFs place one), and how many
        # PDFs place how many; and the same of the images the decks embed
        # on their slides and read on their own
        "pdf_images": _placed(images_per["pdf-pages"], images["pdf-pages"]),
        "deck_images": _placed(images_per[DECK], images[DECK], "deck"),
        # the images of either that are documents of their own, excerpts
        "excerpts": sum(d.kind == EXCERPT for d in planned),
        "failed": len(failed),
        # the planned documents that failed, by the kind the plan gives
        # each; the others (a source that could not be read has no SHA-1
        # to plan) by the kind their events give
        "failed_by_kind": _folded(
            Counter(f["kind"] for f in failed if f["planned"]), _kind
        ),
        "failed_unplanned": sum(not f["planned"] for f in failed),
        "failed_unplanned_by_kind": _folded(
            Counter(f["kind"] for f in failed if not f["planned"]), _kind
        ),
        "failed_unread": sum(f["subject"] is None for f in failed),
        "failed_by_label": _folded(
            Counter(f["reason"] for f in failed), _failure_label
        ),
        "set_aside": _by_count(
            Counter(
                "transcription views" if record["stage"] else "directories"
                for record in fold.set_aside
            )
        ),
        "views_not_read": sum(bool(d.unread) for d in planned),
    }


def _placed(
    per: Counter, images: set[str], container: str = "pdf"
) -> dict[str, Any]:
    """The images a kind of container places and reads on their own, as
    the documents' aggregates give them (_aggregate_documents)."""
    return {
        f"{container}s": sum(per.values()),
        "placed": sum(int(n) * c for n, c in per.items()),
        "distinct": len(images),
        f"per_{container}": dict(
            sorted(per.items(), key=lambda item: int(item[0]))
        ),
    }


# the stage table's columns, which partition a stage's documents
STAGE_COLUMNS = (
    PENDING,
    HELD,
    RUNNING,
    INTERRUPTED,
    COMPUTED,
    REUSED,
    NEEDS_ATTENTION,
    FAILED,
)


def _aggregate_stages(
    documents: dict[str, Document], fold: _Fold, vocabulary: _Vocabulary
) -> list[dict[str, Any]]:
    """One row per stage, in the order the plan gives them, then any other
    stage the events name: its documents by status, how its completed
    ones came about, what forced its computed ones to the engine, its
    attention and failure labels, its calls and its time."""
    rows: dict[str, dict[str, Any]] = {}
    for document in documents.values():
        for stage, attempt in document.stages.items():
            name = vocabulary.stage(stage)
            row = rows.get(name)
            if row is None:
                row = rows[name] = {
                    "documents": 0,
                    **dict.fromkeys(STAGE_COLUMNS, 0),
                    "reused_by": Counter(),
                    "attention_by": Counter(),
                    "attention_labels": Counter(),
                    "failure_labels": Counter(),
                    "forced": Counter(),
                    "views_not_read": 0,
                    "totals": _StageTotals(),
                }
            row["documents"] += 1
            status = attempt.shown_status
            if status in (DONE, NEEDS_ATTENTION):
                how = attempt.how or COMPUTED
                came = COMPUTED if how == COMPUTED else REUSED
                if how == COMPUTED:
                    row["forced"][_joined(attempt.forced(), FORCED)] += 1
                if status == NEEDS_ATTENTION:
                    row[NEEDS_ATTENTION] += 1
                    row["attention_by"][came] += 1
                    for reason in attempt.attention:
                        row["attention_labels"][_attention_label(reason)] += 1
                else:
                    row[came] += 1
                    if came == REUSED:
                        row["reused_by"][_joined(how, REUSE_REASONS)] += 1
                row["views_not_read"] += stage in document.unread
            elif status == FAILED:
                row[FAILED] += 1
                row["failure_labels"][_failure_label(attempt.reason)] += 1
            else:
                row[status] += 1
    for stage, totals in fold.stages.items():
        row = rows.get(vocabulary.stage(stage))
        if row is not None and stage not in fold.corpus:
            shown = row["totals"]
            shown.engine += totals.engine
            shown.cached += totals.cached
            shown.calls += totals.calls
            shown.seconds += totals.seconds
            shown.timed += totals.timed
    by_cache = fold.calls.cache_events > 0
    return [_stage_row(name, row, by_cache) for name, row in rows.items()]


def _stage_row(
    name: str, row: dict[str, Any], by_cache: bool
) -> dict[str, Any]:
    """A stage's row. Its engine calls are the cache's misses and replays
    when the run has the cache's events (`by_cache`), and else the
    engine's call events, as the live monitor counts them."""
    totals = row.pop("totals")
    completed = row[COMPUTED] + row[REUSED] + row[NEEDS_ATTENTION]
    engine = totals.engine if by_cache else totals.calls
    return {
        "name": name,
        **{
            key: _by_count(value) if isinstance(value, Counter) else value
            for key, value in row.items()
        },
        "failure_rate": _share(row[FAILED], row["documents"]),
        "attention_rate": _share(row[NEEDS_ATTENTION], completed),
        "engine_calls": engine,
        "cached_calls": totals.cached,
        "cache_share": _share(totals.cached, engine + totals.cached)
        if by_cache
        else None,
        "call_events": totals.calls,
        "timed": totals.timed,
        "seconds": round(totals.seconds, 3),
        "mean_seconds": _mean(totals.seconds, totals.timed),
    }


def _aggregate_corpus(
    fold: _Fold, run: Mapping[str, Any], vocabulary: _Vocabulary
) -> list[dict[str, Any]]:
    """Each corpus stage the run records or the events name: its state
    (NOT_RUN for one no event names), its attention labels and its
    failure."""
    names = dict.fromkeys(run.get("corpus_stages") or [])
    names.update(dict.fromkeys(fold.corpus))
    shown = []
    for stage in names:
        attempt = fold.corpus.get(stage)
        shown.append(
            {
                "name": vocabulary.stage(stage),
                "state": attempt.shown_status if attempt else NOT_RUN,
                "attention": _by_count(
                    Counter(map(_attention_label, attempt.attention))
                    if attempt
                    else Counter()
                ),
                "failure": _failure_label(attempt.reason)
                if attempt is not None and attempt.status == FAILED
                else None,
                "seconds": attempt.seconds if attempt else None,
            }
        )
    return shown


def _aggregate_alerts(
    alerts: _Alerts | None, fold: _Fold, vocabulary: _Vocabulary
) -> dict[str, Any]:
    if alerts is None:
        return {"status": NOT_AVAILABLE, "why": f"{ALERTS} cannot be read"}
    by_check: dict[str, Counter] = {}
    by_stage: dict[str, Counter] = {}
    for (_subject, stage, check), status in alerts.findings.items():
        by_check.setdefault(_known(check, CHECKS), Counter())[status] += 1
        # an alert on the run as a whole (its thresholds) names no stage
        shown = stage if stage in (RUN, WRITE) else vocabulary.stage(stage)
        by_stage.setdefault(shown, Counter())[status] += 1
    lines = _folded(alerts.by_check, lambda value: _known(value, CHECKS))
    statuses = Counter(alerts.findings.values())
    return {
        "alerts": alerts.lines,
        "findings": len(alerts.findings),
        "new": statuses["new"],
        "carried": statuses["carried"],
        "by_check": {
            check: {
                "new": counts["new"],
                "carried": counts["carried"],
                "alerts": lines.get(check, 0),
            }
            for check, counts in sorted(by_check.items())
        },
        "by_stage": {
            stage: {"new": counts["new"], "carried": counts["carried"]}
            for stage, counts in sorted(by_stage.items())
        },
        "check_errors": fold.anomaly_errors,
        "rechecks": fold.rechecks,
    }


def _aggregate_content(
    documents: dict[str, Document],
    clusters: Mapping[str, Any],
    flags: Mapping[str, Any],
    vocabulary: _Vocabulary,
    written: Mapping[str, Any] | None = None,
    placed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    entities: Counter = Counter()
    claims: Counter = Counter()
    zero_claims: Counter = Counter()
    # the `name` claims, name forms of no stated type (ADR-0011), by the
    # kind of the document that gives them
    untyped_names: Counter = Counter()
    fallbacks: Counter = Counter()
    by_axis: Counter = Counter()
    by_category: Counter = Counter()
    set_aside: Counter = Counter()
    statuses: Counter = Counter()
    notes: Counter = Counter()
    absent: Counter = Counter()
    record_extractions = records_given = recordless = notes_set_aside = 0
    records_set_aside = unsplit_forms = appellation_labels = 0
    appellation_parts = 0
    batches = not_extracted = zero_entities = 0
    extractions = chunks = rejected = unread = with_rejects = given = 0
    failed_rejecting = critiques = with_findings = findings = invalid = 0
    for document in documents.values():
        if not document.planned:
            continue
        batch = document.views.get("batch")
        if batch is not None:
            batches += 1
            entities.update(batch["entities"])
            claims.update(batch["claims"])
            not_extracted += batch["not_extracted"]
            if not batch["claims"].total():
                zero_claims[_kind(document.kind)] += 1
            if batch["claims"].get("name"):
                untyped_names[_kind(document.kind)] += batch["claims"]["name"]
            zero_entities += not batch["entities"].total()
        coverage = document.views.get("coverage")
        if coverage is not None and batch is not None:
            extractions += 1
            chunks += coverage["chunks"]
            rejected += coverage["rejected_claims"]
            unread += coverage["unread_objects"]
            given += batch["claims"].total()
            with_rejects += bool(
                coverage["rejected_claims"] or coverage["unread_objects"]
            )
            fallbacks.update(
                _known(tier, FALLBACK_TIERS) for tier in coverage["fallback"]
            )
            records, noted = coverage["records"], coverage["notes"]
            if records is None:
                absent[_uncounted(coverage)] += 1
            else:
                record_extractions += 1
                records_given += records["given"]
                recordless += records["recordless"]
                statuses.update(records["by_status"])
                if records["set_aside_records"] is None:
                    absent["set_aside_records"] += 1
                else:
                    records_set_aside += records["set_aside_records"]
                if records["unsplit_forms"] is None:
                    absent["unsplit_forms"] += 1
                else:
                    unsplit_forms += records["unsplit_forms"]
                if records["appellation_labels"] is None:
                    absent["appellation_labels"] += 1
                else:
                    appellation_labels += records["appellation_labels"]
                if records["appellation_parts"] is None:
                    absent["appellation_parts"] += 1
                else:
                    appellation_parts += records["appellation_parts"]
                set_aside.update(records["set_aside_by_reason"])
                notes.update(noted["by_category"])
                notes_set_aside += noted["set_aside"]
        failed_rejecting += document.unusable is not None
        if "critique" in document.views:
            critique = document.views["critique"]
            critiques += 1
            findings += critique["findings"]
            invalid += critique["invalid"]
            by_axis.update(critique["by_axis"])
            by_category.update(critique["by_category"])
            with_findings += bool(critique["findings"])
    return {
        "batches": batches,
        "entities": {
            "total": entities.total(),
            "not_extracted": not_extracted,
            "per_batch": _mean(entities.total(), batches),
            "by_kind": _folded(
                entities, lambda value: _known(value, vocabulary.items)
            ),
        },
        "claims": {
            "total": claims.total(),
            "per_batch": _mean(claims.total(), batches),
            "by_property": _folded(
                claims, lambda value: _known(value, vocabulary.properties)
            ),
            "untyped_names_by_kind": _by_count(untyped_names),
        },
        "rejects": {
            "extractions": extractions,
            "rejected_claims": rejected,
            "unread_objects": unread,
            "documents": with_rejects,
            "rate": _share(rejected + unread, rejected + unread + given),
            "failed_extractions": failed_rejecting,
        },
        "zero_claims": {
            "documents": zero_claims.total(),
            "share": _share(zero_claims.total(), batches),
            "by_kind": _by_count(zero_claims),
        },
        "zero_entities": zero_entities,
        "chunks": chunks,
        "fallbacks": _by_count(fallbacks),
        "records": {
            "extractions": record_extractions,
            "given": records_given,
            "recordless": recordless,
            # each document's names are folded already (`_records`)
            "by_status": _by_count(statuses),
            "unsplit_forms": unsplit_forms,
            "set_aside_records": records_set_aside,
            "appellation_labels": appellation_labels,
            "appellation_parts": appellation_parts,
            "set_aside_reasons": set_aside.total(),
            "set_aside_by_reason": _by_count(set_aside),
            "absent": {
                "predating": absent["predating"],
                "line_range": absent["line_range"],
                "set_aside_records": absent["set_aside_records"],
                "unsplit_forms": absent["unsplit_forms"],
                "appellation_labels": absent["appellation_labels"],
                "appellation_parts": absent["appellation_parts"],
            },
        },
        "notes": {
            "total": notes.total(),
            "by_category": _by_count(notes),
            "set_aside": notes_set_aside,
        },
        "critic": {
            "critiques": critiques,
            "findings": findings,
            "invalid": invalid,
            "documents": with_findings,
            "per_critique": _mean(findings, critiques),
            "by_axis": _folded(by_axis, lambda value: _known(value, AXES)),
            "by_category": _folded(
                by_category, lambda value: _known(value, CATEGORIES)
            ),
        },
        "clusters": _aggregate_clusters(clusters, vocabulary),
        "depicts": _depicts(written),
        "write": _aggregate_write(written),
        "place_hierarchy": _aggregate_places(placed),
        "contradictions": {
            "status": NOT_AVAILABLE,
            "why": "different-source contradictions are read from a written "
            "graph, and the report reads the run directory alone",
        },
        "flags": {
            key: flags[key] for key in ("status", "why") if key in flags
        },
    }


def _aggregate_clusters(
    clusters: Mapping[str, Any], vocabulary: _Vocabulary
) -> dict[str, Any]:
    shown: dict[str, Any] = {
        "status": _known(clusters.get("status"), STATUSES)
    }
    if "map" in clusters:
        shown["map"] = _known(clusters["map"], MAP_STATES)
    if clusters.get("map") == "read":
        shown["counts"] = dict(clusters["counts"])
        shown["by_kind"] = _folded(
            clusters.get("by_kind") or {},
            lambda value: _known(value, vocabulary.items),
        )
    return shown


def _aggregate_calls(fold: _Fold, vocabulary: _Vocabulary) -> dict[str, Any]:
    """The calls: those that reached the engine and those the cache
    answered. The engine's are the cache's misses and replays when the
    run has the cache's events, and else its call events (a run made
    without the cache), as the live monitor counts them."""
    calls = fold.calls
    by_cache = calls.cache_events > 0
    engine = calls.engine if by_cache else calls.events
    roles: dict[str, Counter] = {}
    for role, counts in calls.roles.items():
        shown = roles.setdefault(_known(role, ROLES), Counter())
        for key in ("engine", "cached", "rejected", "replayed", "resumed"):
            shown[key] += counts[key]
    return {
        "cache_events": calls.cache_events,
        "engine": engine,
        "cached": calls.cached,
        "cache_share": _share(calls.cached, engine + calls.cached)
        if by_cache
        else None,
        "hits_by_reason": _folded(
            calls.hits, lambda value: _known(value, HIT_REASONS)
        ),
        "misses_by_reason": _folded(
            calls.misses, lambda value: _known(value, MISS_REASONS)
        ),
        "resumes": calls.resumes,
        "replays": calls.replays,
        "replays_changed": calls.changed,
        "rejections": calls.rejects,
        "adoptions": calls.adopts,
        "by_role": {
            role: dict(counts) for role, counts in sorted(roles.items())
        },
        "engine_calls": {
            "count": calls.events,
            "by_role": _folded(
                calls.event_roles, lambda value: _known(value, ROLES)
            ),
            "outcomes": _folded(
                calls.outcomes, lambda value: _known(value, CALL_OUTCOMES)
            ),
            "seconds": round(calls.seconds, 3),
            "mean_seconds": _mean(calls.seconds, calls.timed),
            "waited_seconds": round(calls.waited, 1),
            "waits": calls.waits,
            "refused": calls.refused,
            "holds": _folded(calls.holds, lambda value: _known(value, HOLDS)),
            "errors": _folded(
                calls.errors, lambda value: _exception(value) or OTHER
            ),
            "models": _folded(
                calls.models,
                lambda value: value if _MODEL.fullmatch(value) else OTHER,
            ),
        },
        "usage": _aggregate_usage(calls, vocabulary),
        "output_limit_stops": _aggregate_limit_stops(calls, vocabulary),
        "give_ups_replayed": _folded(calls.gave_up_hits, vocabulary.stage),
    }


def _aggregate_limit_stops(
    calls: _Calls, vocabulary: _Vocabulary
) -> dict[str, Any]:
    """Per stage the call events name, the calls stopped at the output
    limit: cut off there, and given up on. Counts only. RECORDED false
    when no call event of the run records why its call stopped."""
    if not calls.stops_recorded:
        return {RECORDED: False}
    rows: dict[str, Counter] = {}
    for stage, stops in calls.limit_stops.items():
        rows.setdefault(vocabulary.stage(stage), Counter()).update(stops)
    return {
        stage: {stop: rows[stage][stop] for stop in ("cut_off", "gave_up")}
        for stage in sorted(rows)
    }


def _aggregate_usage(calls: _Calls, vocabulary: _Vocabulary) -> dict[str, Any]:
    """What the calls that reached the engine reported they used, per
    stage and role as their call events name them, and in all: their
    calls, those whose stream reported their usage, and the tokens
    reported. The sums are of what was reported, so they fall short of
    what was used by the calls that reported nothing. RECORDED false when
    no call event of the run carries its usage."""
    rows: dict[tuple[str, str], Counter] = {}
    for (stage, role), used in calls.usage.items():
        key = (vocabulary.stage(stage), _known(role, ROLES))
        rows.setdefault(key, Counter()).update(used)
    total: Counter = Counter()
    for used in rows.values():
        total.update(used)
    if not total["reported"]:
        return {RECORDED: False}
    fields = ("calls", "reported", *USAGE_TOKENS)
    return {
        RECORDED: True,
        "by_stage": [
            {"stage": stage, "role": role}
            | {name: rows[(stage, role)][name] for name in fields}
            for stage, role in sorted(rows)
        ],
        "total": {name: total[name] for name in fields},
    }


def _aggregate_control(
    control: list[dict] | None, vocabulary: _Vocabulary
) -> dict[str, Any]:
    if control is None:
        return {"status": NOT_AVAILABLE, "why": f"{CONTROL} cannot be read"}

    def scope(value: Any) -> str:
        what, _, name = str(value).partition(":")
        if value == "run":
            return "run"
        if what == "stage" and name in vocabulary.stages:
            return value
        if what == "kind" and name in KINDS:
            return value
        return OTHER

    def of(action: str) -> dict[str, int]:
        return _folded(
            Counter(r["scope"] for r in control if r["action"] == action),
            scope,
        )

    return {
        "requests": len(control),
        "by_action": _folded(
            Counter(r["action"] for r in control),
            lambda value: _known(value, ACTIONS),
        ),
        "by_requester": _folded(
            Counter(r["by"] for r in control),
            lambda value: _known(value, REQUESTERS),
        ),
        "pauses": of("pause"),
        "aborts": of("abort"),
    }


# -- the sections both pages show ---------------------------------------------


@dataclass(frozen=True)
class _Table:
    head: tuple[str, ...]
    rows: list[tuple[str, ...]]
    # the columns of counts, right-aligned
    numbers: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _Section:
    title: str
    blocks: list[Any]
    level: int = 2


def _n(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _pct(share: float | None) -> str:
    return "-" if share is None else f"{100 * share:.1f}%"


def _time_of(seconds: float | None) -> str:
    return "-" if seconds is None else _duration(seconds)


def _absent(count: int | None) -> str:
    """A document's count, or why it has none: its coverage gives none
    (it predates the count, or what it gives is no count)."""
    if count is None:
        return "not counted (its coverage gives no count of it)"
    return _n(count)


def _listed(counts: Mapping[str, int], none: str = "none") -> str:
    return (
        ", ".join(f"{name} {_n(count)}" for name, count in counts.items())
        or none
    )


def _table(
    head: Iterable[str], rows: Iterable[Iterable[Any]], numbers=()
) -> _Table:
    return _Table(
        tuple(head),
        [tuple(_n(cell) for cell in row) for row in rows],
        frozenset(numbers),
    )


def _sections(aggregate: Mapping[str, Any], *, local: bool) -> list[_Section]:
    """The aggregates as sections of paragraphs, lists and tables, which
    report.md renders as Markdown and report.html (`local`) as HTML."""
    return [
        _run_section(aggregate["run"]),
        _documents_section(aggregate["documents"], local=local),
        *_stage_sections(aggregate["stages"], aggregate["corpus_stages"]),
        _anomalies_section(aggregate["anomalies"]),
        *_content_sections(aggregate["content"]),
        *_calls_sections(aggregate["calls"]),
        _control_section(aggregate["control"]),
    ]


def _run_section(run: Mapping[str, Any]) -> _Section:
    state = run["state"]
    if run["reason"]:
        state += f": {run['reason']}"
    if run["complete"] is not None:
        state += (
            "; went through every stage"
            if run["complete"]
            else "; not through every stage"
        )
    code = run["code"]
    engine = run["engine"]
    barrier = run["barrier"]
    rows = [
        (
            "Mode",
            ", ".join(
                part
                for part in (
                    run["mode"],
                    f"{run['project_stage']} stage"
                    if run["project_stage"]
                    else None,
                )
                if part
            )
            or "-",
        ),
        ("State", state),
        (
            "Invocations",
            f"{_n(run['invocations'])}: {_listed(run['ends'])}",
        ),
        ("Wall time", _time_of(run["seconds"]) + ", summed over invocations"),
        ("Started", run["started_at"] or "-"),
        ("Ended", run["ended_at"] or "-"),
        ("Stops after", run["until"] or "every stage"),
        (
            "Barrier",
            f"passed: {_n(barrier['documents'])} documents completed their "
            f"stages, {_n(barrier['failed'])} failed"
            if barrier
            else "not reached",
        ),
        (
            "Code",
            (code["commit"] or "unknown")
            + (", dirty" if code["dirty"] else "")
            + (f", version {code['version']}" if code["version"] else "")
            + (
                f"; past the barrier on {code['past_barrier_commit']}"
                if code["past_barrier_commit"]
                else ""
            ),
        ),
        (
            "Engine",
            (engine["name"] or "-")
            + (
                f", CLI {engine['cli_version']}"
                if engine["cli_version"]
                else ""
            ),
        ),
    ]
    blocks: list[Any] = [_table(("", ""), rows)]
    if run["state"] == "running":
        blocks.append(
            "The run is still running, or its latest invocation was killed "
            "(a killed invocation records no end): every count is as the run "
            "directory held it when the report was made."
        )
    if run["roles"]:
        blocks.append(
            _table(
                ("Role", "Model", "Effort", "Epoch", "Refresh", "Config"),
                [
                    (
                        role,
                        settings["model"] or "engine default",
                        settings["effort"],
                        settings["epoch"],
                        settings["refresh"],
                        settings["config"] or "not started",
                    )
                    for role, settings in run["roles"].items()
                ],
            )
        )
    title = "Run report" + (f" {run['run_id']}" if run["run_id"] else "")
    return _Section(title, blocks, level=1)


def _documents_section(
    documents: Mapping[str, Any], *, local: bool
) -> _Section:
    """The documents: by kind, failed, and, in the local report alone, by
    language; report.md says where the counts per language are, and
    names no language."""
    line = (
        f"{_n(documents['planned'])} documents planned, from "
        f"{_n(documents['files'])} files ({_n(documents['duplicates'])} "
        f"with another's bytes); {_n(documents['canaries'])} canaries."
    )
    if documents["not_planned"]:
        line += (
            f" {_n(documents['not_planned'])} more that the events name and "
            "the plan does not."
        )
    blocks: list[Any] = [
        line,
        _table(
            ("Kind", "Documents", "Failed"),
            [
                (kind, count, documents["failed_by_kind"].get(kind, 0))
                for kind, count in documents["by_kind"].items()
            ],
            numbers=(1, 2),
        ),
    ]
    items = [
        f"Failed: {_n(documents['failed'])}; by reason: "
        f"{_listed(documents['failed_by_label'])}",
        f"Of them not planned: {_n(documents['failed_unplanned'])} "
        f"({_listed(documents['failed_unplanned_by_kind'])}), "
        f"{_n(documents['failed_unread'])} of them before their source "
        "could be read",
        f"Set aside to be made again: {_listed(documents['set_aside'])}",
        "Documents with a view that could not be read: "
        f"{_n(documents['views_not_read'])}",
    ]
    blocks.append(items)
    languages = documents["languages"]
    if not local:
        # the codes would list the corpus's languages, which stay private
        blocks.append(
            "Documents per language: in the local report alone "
            "(report.html, report.json)."
        )
    elif languages:
        blocks.append(
            _table(
                ("Language", "Documents", "By kind"),
                [
                    (language, entry["documents"], _listed(entry["by_kind"]))
                    for language, entry in languages.items()
                ],
                numbers=(1,),
            )
        )
    else:
        blocks.append("No transcription read.")
    blocks.append(
        [
            "Not transcribed, or its transcription not read: "
            f"{_n(documents['untranscribed'])}",
            f"Transcription routes: {_listed(documents['routes'])}",
            "Pages of PDFs read page by page, by method: "
            f"{_listed(documents['pdf_pages'])}; with annotations the text "
            f"layer leaves unread: {_n(documents['pdf_pages_annotated'])}",
            _images_line(documents),
            f"Language resolved: {_listed(documents['language_resolved'])}",
        ]
    )
    return _Section("Documents", blocks)


def _images_line(documents: Mapping[str, Any]) -> str:
    """The derived images of the PDFs read page by page and of the decks,
    and the excerpts among them, as one line."""
    parts = []
    for key, where, container, name in (
        ("pdf_images", "placed within PDFs read page by page", "pdf", "PDF"),
        ("deck_images", "embedded in decks", "deck", "deck"),
    ):
        images = documents[key]
        per = ", ".join(
            f"{count} in {_n(n)}"
            for count, n in images[f"per_{container}"].items()
        )
        parts.append(
            f"{'images' if parts else 'Images'} {where} and read on their "
            "own: "
            f"{_n(images['placed'])} in {_n(images[f'{container}s'])} "
            f"{name}s ({_n(images['distinct'])} distinct; images per "
            f"{name}: {per or 'none'})"
        )
    return (
        "; ".join(parts)
        + f"; documents of their own (excerpts): {_n(documents['excerpts'])}"
    )


def _stage_sections(
    stages: list[Mapping[str, Any]], corpus: list[Mapping[str, Any]]
) -> list[_Section]:
    head = (
        "Stage",
        "Documents",
        "Pending",
        "Held",
        "Running",
        "Interrupted",
        "Computed",
        "Reused",
        "Needs attention",
        "Failed",
    )
    counts = _table(
        head,
        [
            (row["name"], row["documents"], *(row[c] for c in STAGE_COLUMNS))
            for row in stages
        ],
        numbers=range(1, len(head)),
    )
    how = _table(
        ("Stage", "Reused, by reason", "Computed: what sent it to the engine",
         "Needs attention", "Failed"),
        [
            (
                row["name"],
                _listed(row["reused_by"]),
                _listed(row["forced"]),
                _listed(row["attention_labels"])
                + (
                    f"; {_listed(row['attention_by'])}"
                    if row["attention_by"]
                    else ""
                ),
                _listed(row["failure_labels"]),
            )
            for row in stages
        ],
    )  # fmt: skip
    calls = _table(
        ("Stage", "Engine calls", "Cache hits", "Cache share", "Call events",
         "Attempts timed", "Time", "Mean", "Failure rate", "Attention rate"),
        [
            (
                row["name"],
                row["engine_calls"],
                row["cached_calls"],
                _pct(row["cache_share"]),
                row["call_events"],
                row["timed"],
                _time_of(row["seconds"]),
                _time_of(row["mean_seconds"]),
                _pct(row["failure_rate"]),
                _pct(row["attention_rate"]),
            )
            for row in stages
        ],
        numbers=range(1, 10),
    )  # fmt: skip
    unread = sum(row["views_not_read"] for row in stages)
    blocks: list[Any] = [counts] if stages else ["No stage planned or seen."]
    if stages:
        blocks.append(
            "Computed: the engine answered at least one of the stage's "
            "calls, or it makes none. Reused: its view was current and "
            "stands, or the LLM cache answered every call it made. A "
            "completed stage with attention reasons counts under needs "
            "attention, however it came about."
        )
    sections = [_Section("Stages", blocks)]
    if stages:
        explained = (
            "What sent a computed stage to the engine is what the cache's "
            "events record of its calls: absent (the cache held no answer "
            "under the call's key), rejected (a caller rejected the answer "
            "it held), refresh (the role was refreshed), replay (a "
            "conversation's opening turn asked again for its forks; replay "
            "changed when it answered otherwise); no call for a stage that "
            "makes none, and no cache for calls made without the cache. "
            "Which component of an absent call's key changed is kept by the "
            "cache's entries, and shows only against a baseline run."
        )
        if unread:
            explained += (
                " Completed stages whose view could not be read: "
                f"{_n(unread)}."
            )
        sections.append(
            _Section("How each stage came about", [how, explained], 3)
        )
        sections.append(_Section("Calls and time per stage", [calls], 3))
    sections.append(
        _Section(
            "Corpus stages",
            [
                _table(
                    ("Stage", "State", "Needs attention", "Failed", "Time"),
                    [
                        (
                            stage["name"],
                            stage["state"],
                            _listed(stage["attention"]),
                            stage["failure"] or "no",
                            _time_of(stage["seconds"]),
                        )
                        for stage in corpus
                    ],
                )
                if corpus
                else "No corpus stage in this run."
            ],
            3,
        )
    )
    return sections


def _anomalies_section(anomalies: Mapping[str, Any]) -> _Section:
    if "status" in anomalies:
        return _Section(
            "Anomalies", [f"{anomalies['status']}: {anomalies['why']}."]
        )
    blocks: list[Any] = [
        f"{_n(anomalies['alerts'])} alerts, of {_n(anomalies['findings'])} "
        "findings (a document or the run, a stage and a check): "
        f"{_n(anomalies['new'])} new and {_n(anomalies['carried'])} carried. "
        f"Checks that failed: {_n(anomalies['check_errors'])}; rechecks: "
        f"{_n(anomalies['rechecks'])}."
    ]
    if anomalies["by_check"]:
        blocks.append(
            _table(
                ("Check", "New", "Carried", "Alerts"),
                [
                    (check, row["new"], row["carried"], row["alerts"])
                    for check, row in anomalies["by_check"].items()
                ],
                numbers=(1, 2, 3),
            )
        )
        blocks.append(
            _table(
                ("Stage", "New", "Carried"),
                [
                    (stage, row["new"], row["carried"])
                    for stage, row in anomalies["by_stage"].items()
                ],
                numbers=(1, 2),
            )
        )
    return _Section("Anomalies", blocks)


def _content_sections(content: Mapping[str, Any]) -> list[_Section]:
    entities, claims = content["entities"], content["claims"]
    rejects, zero = content["rejects"], content["zero_claims"]
    critic, clusters = content["critic"], content["clusters"]
    sections = [
        _Section(
            "Content",
            [
                f"{_n(content['batches'])} batches read, from completed "
                f"extractions and GEDCOM adapters; {_n(content['chunks'])} "
                "chunks."
            ],
        ),
        _Section(
            "Entities by kind",
            [
                f"{_n(entities['total'])} entities, "
                f"{_n(entities['per_batch'])} a batch; "
                f"{_n(entities['not_extracted'])} not extracted.",
                _table(
                    ("Kind", "Entities"),
                    entities["by_kind"].items(),
                    numbers=(1,),
                ),
            ],
            3,
        ),
        _Section(
            "Claims by property",
            [
                f"{_n(claims['total'])} claims, {_n(claims['per_batch'])} a "
                "batch. Name forms of no stated type (`name`) by source "
                f"kind: {_listed(claims['untyped_names_by_kind'])}.",
                _table(
                    ("Property", "Claims"),
                    claims["by_property"].items(),
                    numbers=(1,),
                ),
            ],
            3,
        ),
        _Section(
            "Rejects",
            [
                [
                    f"Claims rejected by validation: "
                    f"{_n(rejects['rejected_claims'])}; objects the parser "
                    f"could not read: {_n(rejects['unread_objects'])}; in "
                    f"{_n(rejects['documents'])} of "
                    f"{_n(rejects['extractions'])} extractions",
                    f"Rate: {_pct(rejects['rate'])} of the claims given",
                    "Extractions that failed with every claim rejected or "
                    f"unread: {_n(rejects['failed_extractions'])}",
                    f"Fallback tiers taken: {_listed(content['fallbacks'])}",
                ]
            ],
            3,
        ),
        _Section(
            "Mention records and judgement notes",
            _records_blocks(content["records"], content["notes"]),
            3,
        ),
        _Section(
            "Zero-claim documents",
            [
                f"{_n(zero['documents'])} of {_n(content['batches'])} batches "
                f"({_pct(zero['share'])}) hold no claim: "
                f"{_listed(zero['by_kind'])}. Batches with no entity: "
                f"{_n(content['zero_entities'])}."
            ],
            3,
        ),
        _Section(
            "Critic findings by axis and category",
            [
                f"{_n(critic['findings'])} findings from "
                f"{_n(critic['critiques'])} critiques "
                f"({_n(critic['per_critique'])} a critique), in "
                f"{_n(critic['documents'])} documents; "
                f"{_n(critic['invalid'])} failed their checks and are "
                f"kept as given.",
                _table(
                    ("Axis", "Findings"),
                    critic["by_axis"].items(),
                    numbers=(1,),
                ),
                _table(
                    ("Category", "Findings"),
                    critic["by_category"].items(),
                    numbers=(1,),
                ),
            ],
            3,
        ),
        _Section("Clusters", _cluster_blocks(clusters), 3),
        *_write_sections(content),
        _Section(
            "Not available",
            [
                [
                    f"{what}: {content[key]['status']}: {content[key]['why']}"
                    for what, key in (
                        ("Depicts claims left out", "depicts"),
                        ("The write", "write"),
                        (
                            "Place questions and links",
                            "place_hierarchy",
                        ),
                        ("Different-source contradictions", "contradictions"),
                        ("Flags by tier and class", "flags"),
                    )
                    if content[key].get("status") == NOT_AVAILABLE
                ]
            ],
            3,
        ),
    ]
    return sections


def _write_sections(content: Mapping[str, Any]) -> list[_Section]:
    """The write stage's counts, when it ran."""
    write, depicts = content["write"], content["depicts"]
    if write.get("status") == NOT_AVAILABLE:
        return []
    if write.get("skipped"):
        lines = [
            "The instance held what this run's write key makes: no reset, "
            "and nothing written."
        ]
    else:
        lines = [
            f"{_n(write['documents_written'])} documents written: "
            f"{_n(write['statements_written'])} statements new, "
            f"{_n(write['statements_referenced'])} gained a reference, "
            f"{_n(write['claims_skipped'])} claims already there, "
            f"{_n(write['claims_refused'])} refused"
            + (
                f", {_n(write['claims_self_referencing'])} of them for an "
                "item value that is their subject's item; "
                if "claims_self_referencing" in write
                else "; "
            )
            + f"{_n(write['items_minted'])} items minted. "
            f"{_n(write['batches_refused'])} batches refused whole, "
            f"{_n(write['documents_failed'])} documents failed; the "
            "instance-contents key is "
            + ("recorded." if write["contents_recorded"] else "not recorded.")
        ]
    lines.append(
        f"Depicts claims left out, never written: {_n(depicts['left_out'])}."
    )
    sections = [_Section("Write", [lines], 3)]
    places = content["place_hierarchy"]
    if places.get("status") == NOT_AVAILABLE:
        return sections
    if places.get("skipped"):
        shown = [
            "The instance held what this run's key makes: no place was "
            "reconciled again."
        ]
    else:
        shown = [
            f"{_n(places['questions'])} place questions asked in "
            f"{_n(places['calls'])} calls, of which container a record "
            f"states each place means: {_n(places['answered'])} answered "
            f"with a place, {_n(places['uncertain'])} uncertain, "
            f"{_n(places['unanswered'])} unanswered; "
            f"{_n(places['links_left_out_unnamed'])} stated links left "
            "out, with no place to name.",
            f"Links written: {_n(places['links_written'])} new, "
            f"{_n(places['links_referenced'])} referenced on an equal "
            f"statement, {_n(places['already_present'])} already there; "
            f"{_n(places['links_unresolved'])} unresolved, their inner "
            "place unanswered.",
            f"Links a record states and no claim of its document gives: "
            f"{_n(places['record_only_links'])}, of them "
            f"{_n(places['record_only_links_written'])} written, with no "
            "page and no time.",
            f"Rejected by the schema: {_n(places['rejected_by_schema'])} "
            "answers; refused: "
            f"{_n(places['refused_cycles'])} links that would close a "
            f"cycle (named in the local report), "
            f"{_n(places['refused_conflicts'])} conflicting answers. The "
            "instance-contents key is "
            + (
                "recorded." if places["contents_recorded"] else "not recorded."
            ),
        ]
    sections.append(_Section("Place hierarchy", [shown], 3))
    return sections


def _records_blocks(
    records: Mapping[str, Any], notes: Mapping[str, Any]
) -> list[Any]:
    absent = records["absent"]
    blocks: list[Any] = [
        [
            f"Extractions counted: {_n(records['extractions'])}; mention "
            f"records given: {_n(records['given'])}; recordless mentions "
            "(no record given, or it was set aside): "
            f"{_n(records['recordless'])}",
            "Unsplit name forms in the records given: "
            f"{_n(records['unsplit_forms'])}",
            "Labels that were appellations, replaced with the name the "
            f"record gives: {_n(records['appellation_labels'])}; record "
            "name parts that are appellations: "
            f"{_n(records['appellation_parts'])}",
            f"Records set aside: {_n(records['set_aside_records'])}; "
            "reasons records were set aside: "
            f"{_n(records['set_aside_reasons'])} (a record can give "
            "several)",
            f"Judgement notes: {_n(notes['total'])}; notes set aside: "
            f"{_n(notes['set_aside'])}",
            "Extractions not counted: "
            f"{_n(absent['predating'])} whose coverage predates these "
            f"counts; {_n(absent['line_range'])} that fell back to line "
            "ranges (all their mentions recordless)",
            "Extractions counted whose coverage gives no count of it, so "
            "left out of that count: records set aside "
            f"{_n(absent['set_aside_records'])}; unsplit name forms "
            f"{_n(absent['unsplit_forms'])}; appellation labels "
            f"{_n(absent['appellation_labels'])}; appellation parts "
            f"{_n(absent['appellation_parts'])}",
        ]
    ]
    if records["by_status"]:
        blocks.append(
            _table(
                ("Record status", "Records"),
                records["by_status"].items(),
                numbers=(1,),
            )
        )
    if records["set_aside_by_reason"]:
        blocks.append(
            _table(
                ("Set-aside reason", "Reasons"),
                records["set_aside_by_reason"].items(),
                numbers=(1,),
            )
        )
    if notes["by_category"]:
        blocks.append(
            _table(
                ("Note category", "Notes"),
                notes["by_category"].items(),
                numbers=(1,),
            )
        )
    return blocks


def _cluster_blocks(clusters: Mapping[str, Any]) -> list[Any]:
    status = clusters["status"]
    if status == NOT_RUN and "counts" not in clusters:
        return [
            "Not run: the run has no clustering stage, or never reached it."
        ]
    blocks: list[Any] = [
        f"The clustering stage: {status}; its identity map: "
        f"{clusters.get('map', 'missing')}."
    ]
    if "counts" in clusters:
        blocks.append(
            _table(("", "Count"), clusters["counts"].items(), numbers=(1,))
        )
        if clusters["by_kind"]:
            blocks.append(
                _table(
                    ("Kind", "Mentions clustered"),
                    clusters["by_kind"].items(),
                    numbers=(1,),
                )
            )
    return blocks


def _calls_sections(calls: Mapping[str, Any]) -> list[_Section]:
    events = calls["engine_calls"]
    blocks: list[Any] = [
        f"{_n(calls['engine'])} calls reached the engine and the LLM cache "
        f"answered {_n(calls['cached'])} ({_pct(calls['cache_share'])})."
        if calls["cache_events"]
        else f"{_n(calls['engine'])} calls reached the engine, as the "
        "engine's call events count them: the run has no events of the LLM "
        "cache.",
    ]
    if calls["by_role"]:
        blocks.append(
            _table(
                (
                    "Role",
                    "Engine",
                    "Cached",
                    "Rejected",
                    "Replayed",
                    "Resumed",
                ),
                [
                    (
                        role,
                        counts["engine"],
                        counts["cached"],
                        counts["rejected"],
                        counts["replayed"],
                        counts["resumed"],
                    )
                    for role, counts in calls["by_role"].items()
                ],
                numbers=range(1, 6),
            )
        )
    blocks.append(
        [
            f"Hits by reason: {_listed(calls['hits_by_reason'])}",
            "Give-ups replayed from the cache by stage (no call made): "
            + _listed(calls["give_ups_replayed"]),
            f"Misses by reason: {_listed(calls['misses_by_reason'])}",
            f"Resumed: {_n(calls['resumes'])}; replayed: "
            f"{_n(calls['replays'])}, {_n(calls['replays_changed'])} of them "
            "answered otherwise",
            f"Rejected: {_n(calls['rejections'])}; adopted: "
            f"{_n(calls['adoptions'])}",
            f"Engine call events: {_n(events['count'])} "
            f"({_listed(events['by_role'])}); outcomes: "
            f"{_listed(events['outcomes'])}",
            f"Engine time: {_time_of(events['seconds'])}, mean "
            f"{_n(events['mean_seconds'])} s a call; calls that waited for "
            f"the subscription: {_n(events['waits'])}, "
            f"{_time_of(events['waited_seconds'])} in all; attempts refused: "
            f"{_n(events['refused'])}",
            f"Held: {_listed(events['holds'])}; errors: "
            f"{_listed(events['errors'])}",
            "Calls by the model the engine reported: "
            + _listed(events["models"], "not recorded"),
            "Stops at the output limit by stage: "
            + _limit_stops(calls["output_limit_stops"]),
        ]
    )
    return [
        _Section("Calls and the cache", blocks),
        _Section("Usage", _usage_blocks(calls["usage"]), 3),
    ]


def _limit_stops(stops: Mapping[str, Any]) -> str:
    """The output-limit stops, by stage, as the Calls section gives them."""
    if stops.get(RECORDED) is False:
        return "not recorded: the run's call events carry no stop reason"
    return (
        "; ".join(
            f"{stage}: {_n(counts['cut_off'])} cut off, "
            f"{_n(counts['gave_up'])} given up"
            for stage, counts in stops.items()
        )
        or "none"
    )


def _usage_blocks(usage: Mapping[str, Any]) -> list[Any]:
    if not usage[RECORDED]:
        return ["Usage: not recorded. The run's call events carry no usage."]
    head = (
        "Stage",
        "Role",
        "Calls",
        "Reported",
        "Input",
        "Output",
        "Cache creation",
        "Cache read",
    )
    total = usage["total"]
    return [
        "The tokens the calls that reached the engine reported they used, "
        "by the stage and role their call events name. The sums are of "
        "what was reported: a call that reported nothing (one cut off "
        "before its result) adds nothing to them.",
        _table(
            head,
            [
                (
                    row["stage"],
                    row["role"],
                    *(row[name] for name in ("calls", "reported")),
                    *(row[name] for name in USAGE_TOKENS),
                )
                for row in usage["by_stage"]
            ]
            + [
                (
                    "all",
                    "",
                    total["calls"],
                    total["reported"],
                    *(total[name] for name in USAGE_TOKENS),
                )
            ],
            numbers=range(2, len(head)),
        ),
    ]


def _control_section(control: Mapping[str, Any]) -> _Section:
    if "status" in control:
        return _Section(
            "Control requests", [f"{control['status']}: {control['why']}."]
        )
    return _Section(
        "Control requests",
        [
            [
                f"{_n(control['requests'])} requests: "
                f"{_listed(control['by_action'])}",
                f"By: {_listed(control['by_requester'])}",
                f"Pauses: {_listed(control['pauses'])}",
                f"Aborts: {_listed(control['aborts'])}",
            ]
        ],
    )


# -- report.md ----------------------------------------------------------------


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(aggregate: Mapping[str, Any]) -> str:
    """report.md: the aggregates alone, as Markdown."""
    out: list[str] = []
    for index, section in enumerate(_sections(aggregate, local=False)):
        out.append("#" * section.level + " " + section.title)
        out.append("")
        if index == 0:
            out.append(
                "Aggregates only: counts, rates and times, with no document, "
                "file, name, label, excerpt or quote. Made by `yizkr scratch "
                f"report` at {aggregate['generated_at']}; the per-document "
                "report (report.html, report.json) stays local."
            )
            out.append("")
        for block in section.blocks:
            if isinstance(block, _Table):
                out.append("| " + " | ".join(map(_md_cell, block.head)) + " |")
                out.append(
                    "|"
                    + "|".join(
                        "---:" if column in block.numbers else "---"
                        for column in range(len(block.head))
                    )
                    + "|"
                )
                out += [
                    "| " + " | ".join(map(_md_cell, row)) + " |"
                    for row in block.rows
                ]
            elif isinstance(block, list):
                out += [f"- {item}" for item in block]
            else:
                out.append(block)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# -- report.html --------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fbfbfa;
  --muted: #6b6b70; --line: #deded9; --warn: #8a5a00; --bad: #b3261e;
  --good: #1f6f43; --panel: #f1f1ee; }
@media (prefers-color-scheme: dark) { :root { --fg: #e8e8e6;
  --bg: #161617; --muted: #a0a0a6; --line: #333336; --warn: #e0a64a;
  --bad: #f2807a; --good: #6fcf97; --panel: #202022; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1200px; padding: 16px; color: var(--fg);
  background: var(--bg); font: 15px/1.45 system-ui, sans-serif; }
h1 { font-size: 21px; margin: 0 0 8px; }
h2 { font-size: 17px; margin: 28px 0 8px; }
h3 { font-size: 15px; margin: 20px 0 6px; }
p { margin: 6px 0; }
.muted { color: var(--muted); }
.good { color: var(--good); } .warn { color: var(--warn); }
.bad { color: var(--bad); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; margin: 6px 0;
  font-variant-numeric: tabular-nums; }
th, td { padding: 3px 8px; border-bottom: 1px solid var(--line);
  text-align: left; vertical-align: top; }
td.num, th.num { text-align: right; white-space: nowrap; }
code { font: 13px ui-monospace, monospace; }
ul { margin: 4px 0; padding-left: 20px; }
details { border: 1px solid var(--line); border-radius: 6px;
  margin: 6px 0; padding: 4px 10px; background: var(--panel); }
summary { cursor: pointer; }
nav a { margin-right: 12px; }
footer { margin-top: 28px; font-size: 13px; color: var(--muted); }
"""

# how a status is coloured in the drill-down
_STYLES = {
    DONE: "good",
    NEEDS_ATTENTION: "warn",
    HELD: "warn",
    INTERRUPTED: "warn",
    FAILED: "bad",
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _inline(text: str) -> str:
    """Escaped text, with `code` marked up."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _e(text))


def _html_table(table: _Table) -> str:
    def cell(tag: str, column: int, text: str) -> str:
        style = ' class="num"' if column in table.numbers else ""
        return f"<{tag}{style}>{text}</{tag}>"

    head = "".join(
        cell("th", column, _e(name)) for column, name in enumerate(table.head)
    )
    body = "".join(
        "<tr>"
        + "".join(
            cell("td", column, _inline(text))
            for column, text in enumerate(row)
        )
        + "</tr>"
        for row in table.rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _html_block(block: Any) -> str:
    if isinstance(block, _Table):
        return _html_table(block)
    if isinstance(block, list):
        return (
            "<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in block) + "</ul>"
        )
    return f"<p>{_inline(block)}</p>"


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def render_html(report: Mapping[str, Any]) -> str:
    """report.html: the aggregates, as report.md shows them and with the
    documents per language, then what only the local report holds: each
    invocation and control request with its reason, and every document one
    by one, with its stages, calls, views, alerts, failures and
    set-asides."""
    sections = _sections(report["aggregate"], local=True)
    run = report["run"]
    parts = []
    for index, section in enumerate(sections):
        level = section.level
        parts.append(
            f'<h{level} id="{_anchor(section.title)}">{_e(section.title)}'
            f"</h{level}>"
        )
        if index == 0:
            parts.append(
                '<p class="warn">Local-only: this page holds the run\'s '
                "documents one by one, by file name, and reasons that can "
                "quote a document. Keep it on this machine; post report.md, "
                "the aggregates alone.</p>"
            )
            parts.append(
                "<nav>"
                + "".join(
                    f'<a href="#{_anchor(s.title)}">{_e(s.title)}</a>'
                    for s in sections[1:]
                    if s.level == 2
                )
                + '<a href="#invocations">Invocations</a>'
                '<a href="#each-document">Each document</a></nav>'
            )
        parts += [_html_block(block) for block in section.blocks]
    parts.append('<h2 id="invocations">Invocations</h2>')
    parts.append(
        _html_table(
            _table(
                ("Invocation", "Started", "Ended", "End", "Reason", "Time"),
                [
                    (
                        entry["invocation"],
                        entry["started_at"],
                        entry["ended_at"],
                        entry["end"],
                        entry["reason"],
                        _time_of(entry["seconds"]),
                    )
                    for entry in run["invocations"]
                ],
            )
        )
    )
    if report["control"]:
        parts.append("<h3>Control requests</h3>")
        parts.append(
            _html_table(
                _table(
                    ("At", "Action", "Scope", "By", "Reason"),
                    [
                        (
                            r["at"],
                            r["action"],
                            r["scope"],
                            r["by"],
                            r["reason"],
                        )
                        for r in report["control"]
                    ],
                )
            )
        )
    cycles = report.get("place_cycles") or []
    if cycles:
        parts.append(
            '<h2 id="place-cycles">Place links refused as cycles</h2>'
        )
        parts.append(
            _html_block(
                [
                    f"{cycle['place']} within {cycle['container']}, as "
                    f"{cycle['document']} states: refused, as it would "
                    "close a cycle"
                    for cycle in cycles
                ]
            )
        )
    parts += _html_documents(report)
    parts.append(
        "<footer>Made "
        f"{_e(report['generated_at'])} by <code>yizkr scratch report</code>, "
        "from the run directory alone. Local-only, as report.json is: it "
        "stays on this machine, under var/, and is never posted or sent to "
        "a cloud session.</footer>"
    )
    title = "Run report " + (run["run_id"] or "")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f"<title>{_e(title.strip())}</title>\n<style>{_CSS}</style>\n"
        "</head>\n<body>\n" + "\n".join(parts) + "\n</body>\n</html>\n"
    )


def _doc_anchor(sha1: str) -> str:
    return "doc-" + re.sub(r"[^0-9a-z]", "", sha1.lower())[:12]


def _doc_name(document: Mapping[str, Any]) -> str:
    files = document["files"]
    return f"{document['sha1'][:12]} {files[0] if files else '(no file)'}"


def _html_documents(report: Mapping[str, Any]) -> list[str]:
    documents = report["documents"]
    parts = ['<h2 id="each-document">Each document</h2>']
    # failed now, as the aggregates count it (_failed): a document that
    # failed in one invocation and was made in a later one is not
    lists = (
        ("Failed", lambda d: d["failed"]),
        ("Needs attention", lambda d: any(
            s["status"] == NEEDS_ATTENTION for s in d["stages"].values()
        )),
        ("With alerts", lambda d: d["alerts"]),
        ("With a view that could not be read", lambda d: d["unread"]),
    )  # fmt: skip
    for title, chosen in lists:
        found = [d for d in documents if chosen(d)]
        if not found:
            continue
        links = ", ".join(
            f'<a href="#{_doc_anchor(d["sha1"])}">{_e(_doc_name(d))}</a>'
            for d in found
        )
        parts.append(f"<p><strong>{_e(title)}</strong> ({len(found)}): "
                     f"{links}</p>")  # fmt: skip
    unread = report["failed_unread"]
    if unread:
        parts.append("<h3>Failed before their source could be read</h3>")
        parts.append(
            _html_block(
                [
                    f"{failure['file'] or '(no file)'} ({failure['kind']}): "
                    f"{failure['reason']}"
                    for failure in unread
                ]
            )
        )
    for document in documents:
        parts.append(_html_document(document))
    return parts


def _status_span(status: str) -> str:
    style = _STYLES.get(status, "muted")
    return f'<span class="{style}">{_e(status)}</span>'


def _html_document(document: Mapping[str, Any]) -> str:
    stages = document["stages"]
    summary = (
        f"<code>{_e(document['sha1'][:12])}</code> "
        f"{_e(document['kind'] or '?')} "
        f"<strong>{_e(', '.join(document['files']) or '(no file)')}</strong> "
        + " ".join(
            f"{_e(stage)}: {_status_span(record['status'])}"
            for stage, record in stages.items()
        )
    )
    if document["canary"]:
        summary += ' <span class="muted">canary</span>'
    if not document["planned"]:
        summary += ' <span class="warn">not planned</span>'
    body = []
    body.append(
        _html_table(
            _table(
                ("Stage", "Status", "How", "What sent it to the engine",
                 "Engine", "Cached", "Call events", "Started", "Time",
                 "Reasons"),
                [
                    (
                        stage,
                        record["status"],
                        record["how"],
                        record["forced"],
                        record["engine"],
                        record["cached"],
                        record["calls"],
                        record["started"],
                        _time_of(record["seconds"]),
                        "; ".join(
                            [*record["attention"], record["reason"] or ""]
                        ).strip("; "),
                    )
                    for stage, record in stages.items()
                ],
                numbers=range(4, 8),
            )
        )
    )  # fmt: skip
    items = [f"SHA-1: {document['sha1']}"]
    if document["directory"]:
        items.append(f"Directory: {document['directory']}")
    transcription = document["transcription"]
    if transcription:
        items.append(
            f"Transcription: language {transcription['language']}, "
            f"{transcription['resolved'] or 'resolved ?'}; route "
            f"{transcription['route']}"
        )
    batch = document["batch"]
    if batch:
        items.append(
            f"Entities ({batch['stage']}): "
            f"{_listed(_by_count(batch['entities']))}; not extracted: "
            f"{batch['not_extracted']}"
        )
        items.append(f"Claims: {_listed(_by_count(batch['claims']))}")
    coverage = document["coverage"]
    if coverage:
        items.append(
            f"Chunks: {coverage['chunks']}; rejected claims: "
            f"{coverage['rejected_claims']}; unread objects: "
            f"{coverage['unread_objects']}; fallback tiers: "
            f"{', '.join(coverage['fallback']) or 'none'}"
        )
        records, notes = coverage.get("records"), coverage.get("notes")
        if records is None or notes is None:
            why = (
                "it fell back to line ranges (all its mentions recordless)"
                if _uncounted(coverage) == "line_range"
                else "its coverage predates these counts"
            )
            items.append(
                f"Mention records and judgement notes: not counted, as {why}"
            )
        else:
            items.append(
                f"Mention records given: {records['given']}, by status: "
                f"{_listed(_by_count(records['by_status']))}; unsplit name "
                f"forms: {_absent(records['unsplit_forms'])}; appellation "
                f"labels: {_absent(records['appellation_labels'])}; "
                "appellation parts: "
                f"{_absent(records['appellation_parts'])}; "
                "recordless: "
                f"{records['recordless']}; records set aside: "
                f"{_absent(records['set_aside_records'])}; reasons set "
                "aside: "
                f"{_listed(_by_count(records['set_aside_by_reason']))}; "
                "judgement notes: "
                f"{_listed(_by_count(notes['by_category']))}; notes set "
                f"aside: {notes['set_aside']}"
            )
    if document["critique"] is not None:
        critique = document["critique"]
        items.append(
            f"Critic findings: {critique['findings']}; invalid: "
            f"{critique['invalid']}; by axis: "
            f"{_listed(_by_count(critique['by_axis']))}; by category: "
            f"{_listed(_by_count(critique['by_category']))}"
        )
    items += [
        f"View not read ({stage}): {why}"
        for stage, why in document["unread"].items()
    ]
    items += [
        f"Failed ({failure['invocation'] or '?'}): {failure['reason']}"
        for failure in document["failures"]
    ]
    if document["unusable"] is not None:
        items.append(
            "Its extraction failed with every claim rejected or unread: "
            f"{document['unusable']}, as its reason says"
        )
    items += [
        f"Set aside ({record['stage'] or 'its directory'}): "
        f"{record['reason']}; kept as {record['kept']}"
        for record in document["set_aside"]
    ]
    body.append(_html_block(items))
    if document["alerts"]:
        body.append(
            _html_table(
                _table(
                    ("At", "Stage", "Check", "Status", "Value", "Limit",
                     "Items"),
                    [
                        (a["at"], a["stage"], a["check"], a["status"],
                         a["value"], a["limit"], _alert_items(a))
                        for a in document["alerts"]
                    ],
                )
            )
        )  # fmt: skip
    return (
        f'<details id="{_doc_anchor(document["sha1"])}"><summary>{summary}'
        f"</summary>{''.join(body)}</details>"
    )


def _alert_items(alert: Mapping[str, Any]) -> str | None:
    """A registration flag's items, ``"<artifact> against <others>"``, or
    None for an alert that names none."""
    if not alert.get("artifact"):
        return None
    others = ", ".join(alert.get("others") or [])
    return f"{alert['artifact']} against {others}" if others else None


def render_json(report: Mapping[str, Any]) -> str:
    """report.json: everything the report holds."""
    return json.dumps(report, indent=1, ensure_ascii=False, default=str) + "\n"


# -- writing ------------------------------------------------------------------


def write_report(
    path: Path | str, *, now: datetime | None = None
) -> dict[str, Path]:
    """Build the report of the run directory at `path`, and write
    report.json, report.html and report.md into its REPORT_DIR, each
    replaced whole, in a folder private to its owner (_private_folder).
    The files are written through the folder as it was opened and made
    private, never through its path again (_write_in), so a folder swapped
    for another, or for a link, once opened takes none of them.
    ReportError for a folder that cannot be made, opened or written.
    Returns each file's path by its name."""
    path = Path(path)
    report = build(path, now=now)
    texts = {
        JSON_FILE: render_json(report),
        HTML_FILE: render_html(report),
        MARKDOWN_FILE: render_markdown(report["aggregate"]),
    }
    folder = path / REPORT_DIR
    descriptor = _private_folder(folder)
    try:
        for name, text in texts.items():
            try:
                _write_in(descriptor, name, text)
            except OSError as exc:
                raise ReportError(
                    f"{folder / name} cannot be written: {exc}"
                ) from None
    finally:
        os.close(descriptor)
    return {name: folder / name for name in texts}


def _private_folder(folder: Path) -> int:
    """Make `folder` when it is not there, and make it private to its
    owner (mode 700) every time, a folder there already included: it is
    opened as a folder, never through a symbolic link, and its mode is
    set through what was opened. Returns the open folder, a descriptor
    the caller closes. ReportError for a symbolic link, anything else
    that is no folder, and a folder that cannot be made, opened or made
    private."""
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReportError(f"{folder} cannot be made: {exc}") from None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(folder, flags | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ReportError(
            f"{folder} is not a folder of its own ({type(exc).__name__})"
        ) from None
    try:
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        os.close(descriptor)
        raise ReportError(f"{folder} cannot be made private: {exc}") from None
    return descriptor


def _write_in(folder: int, name: str, text: str) -> None:
    """Replace the file `name` in the folder open as `folder` with `text`,
    whole: it is written under a fresh name beside it (stages.
    staging_path's), private to its owner (mode 600), and then moved into
    place, all through the folder's descriptor. A reader sees the old file
    or the new one, and a write cut short leaves the file as it was."""
    staging = staging_path(Path(name)).name
    descriptor = os.open(
        staging,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=folder,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staging, dir_fd=folder)
        raise
