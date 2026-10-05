"""Offline tests for the live run monitor (#195): the summary it builds from
a run directory, its terminal view and status page, serving the page, and
the control requests it writes.

Most tests read one recorded event stream of a fictional run over
documents of the example universe (docs/EXAMPLES.md): two invocations,
the first killed mid-stage, the second running, with the LLM cache's
events, alerts, control requests, and a document that failed before its
source could be read. Every free-text field the views must not show
carries a name from the universe (_leaks). `record` writes the stream into
a run directory, whole or up to a point, as a driver in another process
would."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import platform
import random
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from tests.unit.stage_counts import disagreement
from yizkr.pipeline import monitor
from yizkr.pipeline.monitor import (
    ABORTED,
    ACTIVE,
    GRAMMAR,
    HELP,
    LATEST_ALERTS,
    OTHER,
    PAUSED,
    STATUS_DIR,
    STATUS_PAGE,
    UNKNOWN,
    NotARunDirectory,
    RunMonitor,
    failure_label,
    handle_command,
    parse_command,
    prefix,
    reason_label,
    render_html,
    render_lines,
    render_rows,
    render_terminal,
    request,
    state_now,
    summarise_run,
    watch,
    write_status_page,
)
from yizkr.pipeline.rundir import (
    INTERRUPTED,
    RUN_END,
    RUN_INTERRUPTED,
    RunDir,
    run_state,
)

SOURCES = {source.key: source for source in u.SOURCES}


def _sha1(key: str) -> str:
    return hashlib.sha1(SOURCES[key].text.encode()).hexdigest()


CERT = _sha1("marriage-cert-1905")
POSTCARD = _sha1("postcard-1938")
DIRECTORY = _sha1("directory-1912")
CARD = _sha1("registration-card-1945")
INTERVIEW = _sha1("interview-2002")
TREE = hashlib.sha1(u.GEDCOM_FIXTURE.read_bytes()).hexdigest()

FILES = {
    CERT: ("marriage-cert-1905.jpg", "media"),
    POSTCARD: ("postcard-1938.jpg", "media"),
    DIRECTORY: ("directory-1912.txt", "text"),
    CARD: ("registration-card-1945.txt", "text"),
    INTERVIEW: ("interview-2002.txt", "text"),
    TREE: ("example-family.ged", "gedcom"),
}
EXTRACTED = ["transcription", "extraction", "critique"]
# a source that could not be read: it has no SHA-1, and is never planned
UNREAD = "Stupfield letters of Maisie Cutmore.pdf"

RUN_ID = "20260927T100000Z-4f2a9c"
FIRST = "a1b2c3d4e5f6"  # killed mid-stage: no end recorded
SECOND = "0f1e2d3c4b5a"  # running
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=10)
# the bound on a wait that an event ends, so that a hang fails rather than
# blocks the suite; no test waits it out (#437)
HANG_GUARD = 60

# what free text says that no view may show: a failure's message and an
# attention reason's details can quote a document, and so can the other
# fields below, whatever their writers mean them for
FAILURE = (
    "RuntimeError: 2 chunks failed after retries (c1, c2; c1: "
    "Hutmacherei zum Blauen Hut, gegr. 1863)"
)
FALLBACK = (
    "entity manifest fell back to line-range segmentation: "
    "input_value='Hildegard Laudler'"
)
UNREADABLE = (
    "its source cannot be read (PermissionError: [Errno 13] Permission "
    f"denied: '{UNREAD}')"
)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def manifest(*, ended: str | None = None, outcome: str | None = None) -> dict:
    documents = []
    for sha1, (name, kind) in FILES.items():
        stages = ["gedcom"] if kind == "gedcom" else EXTRACTED
        documents.append(
            {"sha1": sha1, "file": name, "kind": kind, "stages": stages}
        )
        if sha1 == POSTCARD:
            # a second file with the same bytes plans the same document
            documents.append(
                {
                    "sha1": sha1,
                    "file": "postcard-1938 copy.jpg",
                    "kind": kind,
                    "stages": stages,
                }
            )
    return {
        "format": 1,
        "run_id": RUN_ID,
        "mode": "ingest run",
        "project_stage": "development",
        "documents": documents,
        "started_at": at(0),
        "ended_at": ended,
        "invocations": [
            {
                "invocation": FIRST,
                "started_at": at(0),
                "ended_at": None,
                "outcome": None,
            },
            {
                "invocation": SECOND,
                "started_at": at(300),
                "ended_at": ended,
                "outcome": outcome,
            },
        ],
    }


def stage(event, seconds, subject, name, invocation=SECOND, **fields):
    return (
        "events.jsonl",
        {
            "at": at(seconds),
            "event": event,
            "invocation": invocation,
            "subject": subject,
            "kind": FILES[subject][1],
            "stage": name,
            **fields,
        },
    )


def cache(event, seconds, subject, role, invocation=SECOND, **fields):
    return (
        "events.jsonl",
        {
            "at": at(seconds),
            "event": event,
            "invocation": invocation,
            "subject": subject,
            "kind": FILES[subject][1],
            "role": role,
            "key": hashlib.sha256(f"{subject}{seconds}".encode()).hexdigest(),
            **fields,
        },
    )


def control(seconds, action, scope=None, by="monitor", reason="", **fields):
    entry = {"at": at(seconds), "action": action}
    if action != "start":
        entry |= {"scope": scope or "run", "by": by, "reason": reason}
    return ("control.jsonl", entry | fields)


def alert(seconds, stage_, check, subject, value, status, **fields):
    return [
        (
            "alerts.jsonl",
            {
                "at": at(seconds),
                "invocation": SECOND,
                "stage": stage_,
                "check": check,
                "subject": subject,
                "value": value,
                "status": status,
                **fields,
            },
        ),
        (
            "events.jsonl",
            {
                "at": at(seconds),
                "event": "alert",
                "invocation": SECOND,
                "stage": stage_,
                "check": check,
                "subject": subject,
                "status": status,
            },
        ),
    ]


def event(seconds, name, invocation=SECOND, **fields):
    return (
        "events.jsonl",
        {"at": at(seconds), "event": name, "invocation": invocation, **fields},
    )


# The recorded stream, in the order a driver wrote it. The first
# invocation starts the interview's transcription and is killed; the
# second, running at NOW, takes the other documents through their stages.
STREAM = [
    control(0, "start", invocation=FIRST),
    event(0, "run-start", invocation=FIRST, run_id=RUN_ID),
    stage("started", 2, INTERVIEW, "transcription", invocation=FIRST),
    cache(
        "cache-miss",
        30,
        INTERVIEW,
        "transcribe",
        invocation=FIRST,
        call="complete",
        reason="absent",
    ),
    control(40, "pause", reason="before the restart"),
    # the second invocation: its start marker clears the pause above
    control(300, "start", invocation=SECOND),
    event(300, "run-start", run_id=RUN_ID),
    # a source that could not be read fails alone, before any stage
    event(
        300,
        "document-failed",
        file=UNREAD,
        kind="media",
        reason=UNREADABLE,
    ),
    stage("started", 301, CERT, "transcription"),
    cache(
        "cache-miss",
        302,
        CERT,
        "transcribe",
        call="complete",
        reason="absent (Florence Faxridge)",
    ),
    # its stored transcription fails the check now: kept aside, made again
    event(
        309,
        "set-aside",
        subject=DIRECTORY,
        kind="text",
        stage="transcription",
        reason="the check fails it now: 'Löbig, Ursula, Wwe., Angerring 57'",
        kept=".Adressbuch Flörburg.superseded-1",
    ),
    stage("started", 310, DIRECTORY, "transcription"),
    stage("done", 311, DIRECTORY, "transcription"),
    stage("reused", 312, CARD, "transcription"),
    control(330, "pause", "stage:extraction", reason="checking the prompt"),
    control(345, "pause", "stage:extraction", reason="still checking"),
    stage(
        "held",
        350,
        CARD,
        "extraction",
        reason="stage:extraction paused (Klara Löbig)",
    ),
    stage("started", 355, TREE, "gedcom"),
    stage("done", 356, TREE, "gedcom"),
    stage("done", 360, CERT, "transcription"),
    *alert(360, "transcription", "implausibly short", CERT[:10], 0.12, "new"),
    stage("started", 361, CERT, "extraction"),
    cache("cache-hit", 362, CERT, "extract", call="open", reason="key match"),
    cache("cache-hit", 363, CERT, "extract", call="fork", reason="key match"),
    cache("cache-hit", 364, CERT, "extract", call="fork", reason="key match"),
    control(
        370,
        "pause",
        "kind:media",
        by="anomaly-check",
        reason="transcription failure rate 0.4 over the last 20",
    ),
    control(380, "resume", "kind:media"),
    stage("done", 390, CERT, "extraction"),
    stage("started", 391, CERT, "critique"),
    cache(
        "cache-miss",
        392,
        CERT,
        "critique",
        call="complete",
        reason="absent",
    ),
    stage("done", 420, CERT, "critique"),
    cache(
        "cache-adopt",
        420,
        POSTCARD,
        "transcribe",
        reason="adopted: the archive's transcription of Rahel Löbig's card",
    ),
    stage("started", 421, POSTCARD, "transcription"),
    cache(
        "cache-hit",
        422,
        POSTCARD,
        "transcribe",
        call="complete",
        reason="adopted",
    ),
    stage("done", 423, POSTCARD, "transcription"),
    stage("started", 424, POSTCARD, "extraction"),
    cache(
        "cache-hit", 425, POSTCARD, "extract", call="open", reason="key match"
    ),
    cache(
        "cache-miss", 426, POSTCARD, "extract", call="fork", reason="absent"
    ),
    cache("cache-replay", 427, POSTCARD, "extract", call="open", matches=True),
    cache(
        "cache-reject",
        428,
        POSTCARD,
        "extract",
        reason="chunk: no terminal sentinel after 'Gustav Löbig, Hutmacher'",
    ),
    control(450, "abort", "kind:gedcom", reason="not needed this run"),
    control(455, "abort", "kind:gedcom", reason="a second abort"),
    control(460, "resume", "kind:gedcom"),
    stage("done", 480, POSTCARD, "extraction", attention=[FALLBACK]),
    *alert(
        480, "extraction", "fallback manifest", POSTCARD[:10], 1, "carried"
    ),
    stage("started", 481, POSTCARD, "critique"),
    stage("started", 482, DIRECTORY, "extraction"),
    cache(
        "cache-miss", 483, DIRECTORY, "extract", call="open", reason="absent"
    ),
    cache(
        "cache-miss", 484, DIRECTORY, "extract", call="fork", reason="absent"
    ),
    stage("failed", 485, DIRECTORY, "extraction", reason=FAILURE),
    event(
        485,
        "document-failed",
        file="directory-1912.txt",
        subject=DIRECTORY,
        kind="text",
        reason=FAILURE,
    ),
    *alert(486, "extraction", "claim rate", "run", 0.5, "new"),
    *alert(
        487,
        "transcription",
        "language anomaly",
        CARD[:10],
        "Hildegard Laudler, Brunnenring 35",
        "carried",
        detail="the card of Gustav Löbig",
    ),
]


def record(path: Path, upto: int | None = None, **ended) -> Path:
    """Write the run's manifest and the stream's first `upto` entries (all
    by default), as the driver would have written them."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_text(json.dumps(manifest(**ended), indent=2))
    append(path, STREAM[:upto])
    return path


def append(path: Path, entries) -> None:
    for name, entry in entries:
        with open(path / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def page_of(run: Path) -> Path:
    return run / STATUS_DIR / STATUS_PAGE


@pytest.fixture
def run(tmp_path) -> Path:
    return record(tmp_path / "run")


@pytest.fixture
def summary(run):
    return summarise_run(run, NOW)


def cut_short(run: Path) -> set[tuple[str, str]]:
    """The (document, stage) pairs run_state reads as interrupted."""
    return {
        key
        for key, state in run_state(RunDir(run)).items()
        if state.status == INTERRUPTED
    }


def skip_unless_modes_hold(where: Path) -> None:
    """Skip the test where this process reads a file whatever its mode (as
    root does): a probe in `where` with mode 0 must be unreadable."""
    probe = where / "mode-probe"
    probe.write_text("x")
    probe.chmod(0)
    try:
        probe.read_text()
    except PermissionError:
        pass
    else:
        pytest.skip("this process reads a file whatever its mode")
    finally:
        probe.chmod(0o600)
        probe.unlink()


def skip_unless_unsearchable_directories_raise(where: Path) -> None:
    """Skip the test where Path.is_file does not raise for a link into a
    directory that cannot be searched, as on Python 3.14 and later, where
    it returns False: read_jsonl then finds no entries there, as for a
    missing file, so nothing is unreadable. A probe in `where` must
    raise."""
    folder = where / "search-probe"
    folder.mkdir()
    (folder / "probe").write_text("x")
    link = where / "search-probe-link"
    link.symlink_to(folder / "probe")
    folder.chmod(0)
    try:
        link.is_file()
    except PermissionError:
        pass
    else:
        pytest.skip(
            "Path.is_file does not raise for a directory that cannot be "
            "searched"
        )
    finally:
        folder.chmod(0o700)
        link.unlink()
        shutil.rmtree(folder)


# what makes a run's file (its control file, events.jsonl, alerts.jsonl)
# unreadable, to the driver too: its own mode, or its directory's, as a
# link into a directory that cannot be searched, so that not even its size
# can be looked at
UNREADABLE_BY = ["its mode", "its directory"]


@contextlib.contextmanager
def unreadable_file(run: Path, name: str, by: str):
    """The run's file `name`, unreadable for the block (UNREADABLE_BY),
    then readable again, with its own mode or its directory's put back,
    unless the block removed it. Skipped where this process reads a file
    whatever its mode, and, by its directory, where Path.is_file does not
    raise for it."""
    skip_unless_modes_hold(run.parent)
    if by == "its directory":
        skip_unless_unsearchable_directories_raise(run.parent)
    path = run / name
    target = path
    if by == "its directory":
        target = run.parent / f"hidden-{name}"
        target.mkdir()
        path.rename(target / name)
        path.symlink_to(target / name)
    mode = target.stat().st_mode & 0o777
    target.chmod(0)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            target.chmod(mode)


def unreadable_control(run: Path, by: str):
    """The run's control file, unreadable for the block (unreadable_file)."""
    return unreadable_file(run, "control.jsonl", by)


# -- the summary --------------------------------------------------------------


class TestStageCounts:
    def test_each_stage_is_counted_by_state(self, summary):
        counts = {
            counts.name: (
                counts.pending,
                counts.running,
                counts.done,
                sum(counts.reused.values()),
                counts.attention,
                counts.failed,
            )
            for counts in summary.stages
        }
        assert counts == {
            # pending, running, done, reused, attention, failed
            "transcription": (1, 0, 2, 2, 0, 0),
            "extraction": (2, 0, 0, 1, 1, 1),
            "critique": (3, 1, 1, 0, 0, 0),
            "gedcom": (0, 0, 1, 0, 0, 0),
        }

    def test_stages_come_in_plan_order_and_duplicates_count_once(
        self, summary
    ):
        assert [counts.name for counts in summary.stages] == [
            "transcription",
            "extraction",
            "critique",
            "gedcom",
        ]
        assert summary.documents == 6
        assert summary.kinds == {"text": 3, "media": 2, "gedcom": 1}
        assert {counts.total for counts in summary.stages} == {5, 1}

    def test_the_counts_agree_with_run_state(self, run, summary):
        assert disagreement(run, summary) is None

    def test_held_and_interrupted_stages_are_pending(self, summary):
        extraction = summary.stage("extraction")
        assert extraction.held == 1
        transcription = summary.stage("transcription")
        # started by the killed invocation: the next one starts it again
        assert transcription.interrupted == 1
        assert transcription.running == 0

    def test_failures_and_attention_show_their_labels_only(self, summary):
        extraction = summary.stage("extraction")
        assert extraction.failures == {"RuntimeError": 1}
        assert extraction.attention_reasons == {
            "entity manifest fell back to line-range segmentation": 1
        }

    def test_calls_are_attributed_to_the_stage_that_made_them(self, summary):
        calls = {c.name: (c.engine, c.cached) for c in summary.stages}
        assert calls == {
            "transcription": (2, 1),
            "extraction": (4, 4),
            "critique": (1, 0),
            "gedcom": (0, 0),
        }


class TestAgreement:
    """The counts agree with rundir.run_state for what the driver can
    write, and for what a reader may meet."""

    def test_one_sha1_planned_under_two_kinds(self, tmp_path):
        """A .ged file and a .txt copy of the same bytes: the plan names
        the SHA-1 twice, with the stages of both."""
        path = tmp_path / "run"
        rundir = RunDir(path)
        rundir.start(
            documents=[
                {"sha1": TREE, "kind": "gedcom", "stages": ["gedcom"]},
                {"sha1": TREE, "kind": "text", "stages": EXTRACTED},
            ]
        )
        rundir.emit("started", subject=TREE, kind="gedcom", stage="gedcom")
        rundir.emit("done", subject=TREE, kind="gedcom", stage="gedcom")
        summary = summarise_run(path, NOW)
        assert disagreement(path, summary) is None
        assert summary.stage("extraction").pending == 1
        assert summary.kinds == {"gedcom": 1}

    @pytest.mark.parametrize("field", ["subject", "stage"])
    def test_a_stage_event_with_an_empty_name_is_left_out(
        self, tmp_path, field
    ):
        path = tmp_path / "run"
        rundir = RunDir(path)
        rundir.start(documents=[])
        where = {"subject": CERT, "kind": "media", "stage": "transcription"}
        rundir.emit("started", **(where | {field: ""}))
        summary = summarise_run(path, NOW)
        assert disagreement(path, summary) is None
        assert summary.stages == []

    @pytest.mark.parametrize("empty", ["sha1", "stage"])
    def test_a_plan_entry_with_an_empty_name_is_counted(self, tmp_path, empty):
        """run_state counts every planned stage, whatever its SHA-1 or
        name; only a stage event with an empty one is left out."""
        path = tmp_path / "run"
        rundir = RunDir(path)
        planned = [{"sha1": CERT, "kind": "media", "stages": EXTRACTED}]
        if empty == "sha1":
            planned.append({"sha1": "", "kind": "text", "stages": EXTRACTED})
        else:
            planned[0]["stages"] = ["", *EXTRACTED]
        rundir.start(documents=planned)
        rundir.emit("started", subject="", kind="text", stage="extraction")
        rundir.emit("started", subject=CERT, kind="media", stage="")
        summary = summarise_run(path, NOW)
        assert disagreement(path, summary) is None
        pending = {counts.name: counts.pending for counts in summary.stages}
        if empty == "sha1":
            assert pending == dict.fromkeys(EXTRACTED, 2)
        else:
            assert pending == {"?": 1} | dict.fromkeys(EXTRACTED, 1)

    @pytest.mark.parametrize("before", ["started", "failed", "done", "reused"])
    def test_a_held_stage_is_pending_whatever_it_was(self, tmp_path, before):
        """A stage the control file holds in a later invocation, after it
        started in a killed one, failed, or was made or reused."""
        path = tmp_path / "run"
        first = RunDir(path)
        first.start(
            documents=[{"sha1": CERT, "kind": "media", "stages": EXTRACTED}]
        )
        where = {"subject": CERT, "kind": "media", "stage": "extraction"}
        first.emit("started", **where)
        if before != "started":
            first.emit(before, reason="RuntimeError: x", **where)
        second = RunDir(path)
        second.start()
        second.emit("held", reason="stage:extraction paused", **where)
        summary = summarise_run(path, NOW)
        assert disagreement(path, summary) is None
        extraction = summary.stage("extraction")
        assert (extraction.pending, extraction.held) == (1, 1)

    def test_random_streams_agree_with_run_state_and_the_driver(
        self, tmp_path
    ):
        """Random streams through the real writers, compared after every
        write: the stage counts with run_state, the slices with the
        driver's fold, and a monitor that followed the files with one
        that reads them afresh. Invocations start, end as a driver's do
        (their end in run.json, then their closing event, or one of them
        alone) and are killed, so stages are cut short in every way the
        readers must agree on (#300)."""
        stages = ["transcription", "extraction", "critique"]
        # comparisons made while the latest invocation had ended and
        # stages were interrupted
        cut_short = 0
        for seed in range(4):
            rng = random.Random(seed)
            path = tmp_path / f"run{seed}"
            docs = [
                hashlib.sha1(f"fictional page {seed} {n}".encode()).hexdigest()
                for n in range(5)
            ]
            kinds = {sha1: rng.choice(["media", "text"]) for sha1 in docs}
            plan = [
                {"sha1": sha1, "kind": kinds[sha1], "stages": stages}
                for sha1 in docs[:4]
            ]
            watcher = RunMonitor(path)
            rundir = None
            for step in range(80):
                roll = rng.random()
                if rundir is None or roll < 0.05:
                    if rundir is not None and rng.random() < 0.5:
                        rundir.end(rng.choice(["finished", "stopped"]))
                    rundir = RunDir(path)
                    rundir.start(documents=plan)
                elif roll < 0.09:
                    outcome = rng.choice(["finished", "interrupted", "paused"])
                    how = rng.choice(["both", "run.json", "event"])
                    if how != "event":
                        rundir.end(outcome)
                    if how != "run.json":
                        rundir.emit(
                            RUN_INTERRUPTED
                            if outcome == "interrupted"
                            else RUN_END,
                            outcome=outcome,
                            statuses={},
                        )
                elif roll < 0.16:
                    target = rng.choice(
                        [{}, {"stage": "extraction"}, {"kind": "media"}]
                    )
                    rundir.request(
                        rng.choice(["pause", "resume", "abort"]), **target
                    )
                else:
                    sha1 = rng.choice(docs)
                    where = {
                        "subject": sha1,
                        "kind": kinds[sha1],
                        "stage": rng.choice(stages),
                    }
                    name = rng.choice(
                        ["started", "started", "done", "reused", "failed"]
                        + ["held", "cache-hit", "cache-miss"]
                    )
                    if name.startswith("cache"):
                        rundir.emit(
                            name,
                            subject=sha1,
                            kind=kinds[sha1],
                            role="extract",
                            reason="absent",
                        )
                    else:
                        attention = rng.choice([None, ["no claims extracted"]])
                        rundir.emit(
                            name,
                            attention=attention if name == "done" else None,
                            reason="RuntimeError: x",
                            **where,
                        )
                watcher.refresh()
                followed = watcher.summary(NOW)
                assert disagreement(path, followed) is None, (seed, step)
                cut_short += followed.state != "running" and any(
                    counts.interrupted for counts in followed.stages
                )
                shown = {
                    item.scope: item.state
                    for item in followed.slices
                    if item.state != ACTIVE
                }
                assert shown == RunDir(path).control().scopes, (seed, step)
                if step % 20 == 19:
                    fresh = summarise_run(path, NOW)
                    assert [vars(c) for c in fresh.stages] == [
                        vars(c) for c in followed.stages
                    ], (seed, step)
        assert cut_short


class TestReuse:
    def test_reuse_is_counted_by_reason(self, summary):
        assert summary.stage("transcription").reused == {
            "adopted": 1,
            "view current": 1,
        }
        assert summary.stage("extraction").reused == {"key match": 1}

    def test_a_stage_that_asked_the_engine_is_made_not_reused(self, tmp_path):
        """A hit and a miss in one stage: the engine was asked, so the
        output was made in the run."""
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                stage("started", 301, CERT, "extraction"),
                cache("cache-hit", 302, CERT, "extract", reason="key match"),
                cache("cache-miss", 303, CERT, "extract", reason="absent"),
                stage("done", 304, CERT, "extraction"),
                stage("started", 305, POSTCARD, "extraction"),
                cache("cache-hit", 306, POSTCARD, "extract", reason="adopted"),
                cache(
                    "cache-hit", 307, POSTCARD, "extract", reason="key match"
                ),
                stage("done", 308, POSTCARD, "extraction"),
            ],
        )
        extraction = summarise_run(path, NOW).stage("extraction")
        assert extraction.done == 1
        assert extraction.reused == {"adopted + key match": 1}

    def test_a_stage_without_calls_is_made(self, summary):
        """A text file's transcription and the GEDCOM adapter call no
        engine: their output is made in the run, not reused."""
        assert summary.stage("gedcom").done == 1
        assert summary.stage("gedcom").reused == {}

    def test_a_reused_output_that_needs_attention_counts_as_attention(
        self, tmp_path
    ):
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                stage(
                    "reused",
                    301,
                    CERT,
                    "extraction",
                    attention=["no claims extracted"],
                ),
            ],
        )
        extraction = summarise_run(path, NOW).stage("extraction")
        assert extraction.attention == 1 and extraction.reused == {}
        assert extraction.attention_reasons == {"no claims extracted": 1}


class TestInFlightCallsAndTime:
    def test_documents_in_flight(self, summary):
        (item,) = summary.in_flight
        assert (item.sha1, item.kind, item.stage) == (
            POSTCARD,
            "media",
            "critique",
        )
        assert item.since == T0 + timedelta(seconds=481)

    def test_calls_come_from_the_cache_events(self, summary):
        calls = summary.calls
        # misses and a replay asked the engine; hits answered from the cache
        assert (calls.engine, calls.cached) == (7, 5)
        assert calls.hit_reasons == {"key match": 4, "adopted": 1}
        assert (calls.replayed, calls.rejected, calls.adopted) == (1, 1, 1)
        assert calls.roles["extract"] == {"engine": 4, "cached": 4}
        assert calls.to_engine == 7

    def test_the_calls_to_go_are_estimated_per_stage_and_kind(self, summary):
        """Extraction of the three text documents to go (the failed one
        with them), at the one text extraction's 2 engine calls; the
        postcard's critique at the media critiques' 1; the interview's
        transcription at the text transcriptions' 0 (the view reused for
        the card made no call, and is left out of the rate). No text
        document has been critiqued yet, so that part has no rate."""
        assert summary.remaining_work == 8
        assert summary.remaining_calls == 7
        assert summary.unrated == ["critique (text)"]

    def test_the_calls_to_go_are_unknown_when_nothing_is_rated(self, tmp_path):
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                stage("started", 301, CERT, "transcription"),
                cache("cache-miss", 302, CERT, "transcribe", reason="absent"),
            ],
        )
        summary = summarise_run(path, NOW)
        assert summary.remaining_calls is None
        assert summary.remaining_work == 16
        view = render_terminal(summary, width=300)
        assert "16 stages; engine calls unknown" in view
        assert "~0" not in view

    def test_a_reused_view_leaves_the_rate_alone(self, tmp_path):
        """The card's transcription view stood; the directory's was made
        with 3 calls. The interview's, to go, is rated at 3, not 1.5."""
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                stage("reused", 301, CARD, "transcription"),
                stage("started", 302, DIRECTORY, "transcription"),
                *[
                    cache("cache-miss", 303 + n, DIRECTORY, "transcribe")
                    for n in range(3)
                ],
                stage("done", 306, DIRECTORY, "transcription"),
            ],
        )
        summary = summarise_run(path, NOW)
        assert "transcription (text)" not in summary.unrated
        assert summary.remaining_calls == 3

    def test_events_without_a_kind_are_rated_by_the_planned_kind(
        self, tmp_path
    ):
        """Read in one refresh with run.json: the rate still goes by the
        kind the plan gives the document."""
        path = record(tmp_path / "run", upto=0)
        entries = [
            stage("started", 301, CERT, "critique"),
            cache("cache-miss", 302, CERT, "critique", reason="absent"),
            cache("cache-miss", 303, CERT, "critique", reason="absent"),
            stage("done", 304, CERT, "critique"),
        ]
        for _, entry in entries:
            del entry["kind"]
        append(path, entries)
        summary = summarise_run(path, NOW)
        # the postcard's critique, at the certificate's 2 calls
        assert summary.remaining_calls == 2
        assert "critique (media)" not in summary.unrated

    def test_elapsed_time_spans_the_invocations(self, summary):
        """The killed invocation ran to its last event (30 s), and the
        running one to now (300 s)."""
        assert summary.state == "running"
        assert summary.invocations == 2
        assert summary.elapsed == 330
        assert summary.invocation_elapsed == 300
        assert summary.last_event_at == T0 + timedelta(seconds=487)

    def test_an_ended_invocation_leaves_nothing_in_flight(self, tmp_path):
        path = record(
            tmp_path / "run",
            ended=at(500),
            outcome="stopped",
        )
        manifest_ = json.loads((path / "run.json").read_text())
        manifest_["invocations"][-1]["reason"] = "run aborted"
        (path / "run.json").write_text(json.dumps(manifest_))
        summary = summarise_run(path, NOW)
        assert (summary.state, summary.reason) == ("stopped", "run aborted")
        assert summary.in_flight == []
        assert summary.stage("critique").interrupted == 1
        assert summary.elapsed == 30 + 200
        # run.json alone records the end: run_state reads the stage the
        # invocation left started as interrupted too (#300)
        assert disagreement(path, summary) is None
        assert cut_short(path) == {
            (INTERVIEW, "transcription"),
            (POSTCARD, "critique"),
        }

    def test_the_set_aside_views_are_counted(self, summary):
        assert summary.set_aside == 1


class TestClosingEvents:
    """Each invocation closes with run-end or run-interrupted, with its
    outcome, its reason and the documents per status."""

    def test_the_closing_event_says_how_an_invocation_ended(self, run):
        append(
            run,
            [
                event(
                    600,
                    "run-end",
                    outcome="paused",
                    reason="UsageLimitReached",
                    statuses={"awaiting-claims": 3, "failed": 1, "Löbig": 2},
                )
            ],
        )
        summary = summarise_run(run, NOW)
        assert (summary.state, summary.reason) == (
            "paused",
            "UsageLimitReached",
        )
        assert summary.statuses == {"awaiting-claims": 3, "failed": 1, "?": 2}
        assert summary.in_flight == []
        view = render_terminal(summary, width=200)
        assert "paused: UsageLimitReached | invocation 2" in view
        assert "statuses   3 awaiting-claims, 2 ?, 1 failed" in view
        # the closing event alone says it ended: run_state reads the stage
        # it left started as interrupted too (#300)
        assert disagreement(run, summary) is None
        assert (POSTCARD, "critique") in cut_short(run)

    def test_an_interrupted_invocation(self, run):
        append(run, [event(600, "run-interrupted", statuses={})])
        summary = summarise_run(run, NOW)
        assert summary.state == "interrupted"
        assert summary.stage("critique").interrupted == 1
        assert summary.stage("critique").running == 0
        assert disagreement(run, summary) is None
        assert cut_short(run) == {
            (INTERVIEW, "transcription"),
            (POSTCARD, "critique"),
        }


class TestCallEvents:
    """Call events are optional: shown when the run has them, in the shape
    the engine writes them."""

    CALLS = [
        (
            "events.jsonl",
            {
                "at": at(490),
                "event": "call",
                "invocation": SECOND,
                "role": "critique",
                "stage": "critique",
                "subject": POSTCARD,
                "kind": "media",
                "call": "complete",
                "outcome": "ok",
                "duration_s": 12.5,
                "wait_s": 90.0,
                "refused": 2,
                "held": "rate limit",
            },
        ),
        (
            "events.jsonl",
            {
                "at": at(495),
                "event": "call",
                "invocation": SECOND,
                "role": "critique",
                "stage": "critique",
                "subject": POSTCARD,
                "kind": "media",
                "call": "complete",
                "outcome": "paused",
                "duration_s": 7.5,
                "held": "usage limit",
                "error": "UsageLimitReached",
                "window": "five_hour",
                "resumes_at": "2026-09-27T14:00:00+00:00",
            },
        ),
    ]

    def test_a_run_without_call_events_shows_none(self, summary):
        assert summary.calls.logged == 0
        assert "logged" not in render_terminal(summary)

    def test_call_events_are_shown_when_present(self, run):
        append(run, self.CALLS)
        summary = summarise_run(run, NOW)
        calls = summary.calls
        assert (calls.logged, calls.timed, calls.seconds) == (2, 2, 20.0)
        assert calls.outcomes == {"ok": 1, "paused": 1}
        assert (calls.waits, calls.waited, calls.refused) == (1, 90.0, 2)
        assert calls.holds == {"rate limit": 1, "usage limit": 1}
        assert calls.errors == {"UsageLimitReached": 1}
        assert calls.pause == monitor.CallPause(
            "usage limit",
            "five_hour",
            datetime(2026, 9, 27, 14, tzinfo=UTC),
            "UsageLimitReached",
        )
        # the cache's events still say what reached the engine
        assert calls.to_engine == 7
        view = render_terminal(summary, width=200)
        assert (
            "logged   2 calls | mean 10.0s | 1 paused | errors: 1 "
            "UsageLimitReached" in view
        )
        assert (
            "limits   1 waited for the subscription, 1m 30s in all | 2 "
            "attempts refused | held: 1 rate limit, 1 usage limit" in view
        )
        assert (
            "paused   for the subscription (usage limit, five_hour window); "
            "it can go on from 2026-09-27 14:00 UTC" in view
        )

    @staticmethod
    def paused(**fields) -> list:
        """The call events of CALLS, the pause's fields replaced."""
        pause = {
            k: v
            for k, v in TestCallEvents.CALLS[1][1].items()
            if k not in ("held", "error", "window", "resumes_at")
        }
        return [TestCallEvents.CALLS[0], ("events.jsonl", pause | fields)]

    def test_a_pause_of_a_model_the_cli_does_not_support(self, run):
        """A pause that names no hold or window is not the subscription's
        (#455): it is shown by its name, with no time to go on."""
        append(run, self.paused(error="ModelUnsupported"))
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   ModelUnsupported; no time to go on" in view
        assert "paused   for the subscription" not in view

    def test_a_pause_that_says_nothing_is_no_subscriptions(self, run):
        append(run, self.paused())
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   ?; no time to go on" in view

    def test_a_model_pause_after_a_limit_held_the_call(self, run):
        """The call was held for a rate limit first, then paused for a
        model the CLI does not support: the pause is the model's, not the
        subscription's (#455)."""
        append(run, self.paused(held="rate limit", error="ModelUnsupported"))
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   ModelUnsupported; no time to go on" in view
        assert "paused   for the subscription" not in view

    def test_a_pause_the_event_does_not_name_is_the_subscriptions(self, run):
        append(run, self.paused(held="usage limit"))
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   for the subscription (usage limit)" in view

    def test_a_subscription_pause_that_names_only_a_window(self, run):
        append(run, self.paused(window="seven_day", error="UsageLimitReached"))
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   for the subscription (?, seven_day window)" in view

    def test_a_subscription_pause_with_no_time_says_none(self, run):
        """Usage credits: the subscription's pause, with no time to go
        on (#455)."""
        append(
            run, self.paused(held="usage credits", error="UsageLimitReached")
        )
        view = render_terminal(summarise_run(run, NOW), width=200)
        assert "paused   for the subscription (usage credits)" in view
        assert "it can go on" not in view and "no time to go on" not in view

    def test_a_pause_of_an_earlier_invocation_is_not_shown(self, run):
        append(run, self.CALLS)
        append(run, [control(600, "start", invocation="c0ffee000001")])
        manifest_ = json.loads((run / "run.json").read_text())
        manifest_["invocations"].append(
            {"invocation": "c0ffee000001", "started_at": at(600)}
        )
        (run / "run.json").write_text(json.dumps(manifest_))
        assert summarise_run(run, NOW).calls.pause is None

    def test_without_the_cache_call_events_count_the_engine_calls(
        self, tmp_path
    ):
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                stage("started", 301, CERT, "critique"),
                *[
                    (
                        "events.jsonl",
                        {
                            "at": at(302 + index),
                            "event": "call",
                            "subject": CERT,
                            "stage": "critique",
                            "outcome": "ok",
                        },
                    )
                    for index in range(3)
                ],
                stage("done", 310, CERT, "critique"),
            ],
        )
        summary = summarise_run(path, NOW)
        assert summary.calls.to_engine == 3
        # the other media document's critique, at 3 calls a critique
        assert summary.remaining_calls == 3

    def test_a_run_with_no_calls_has_no_estimate(self, tmp_path):
        path = record(tmp_path / "run", upto=0)
        summary = summarise_run(path, NOW)
        assert summary.calls.to_engine is None
        assert summary.remaining_calls is None
        assert "none recorded yet" in render_terminal(summary)


class TestDocumentFailures:
    def test_the_invocation_s_failed_documents_by_kind_and_reason(
        self, summary
    ):
        assert summary.failed_documents == 2
        assert summary.unread_documents == 1
        assert summary.failed_by_kind == {"media (.pdf)": 1, "text (.txt)": 1}
        assert summary.failed_by_reason == {
            "its source cannot be read": 1,
            "RuntimeError": 1,
        }
        view = render_terminal(summary, width=300)
        assert (
            "failed     2 documents in this invocation, 1 before their source "
            "was read: 1 media (.pdf), 1 text (.txt); 1 RuntimeError, 1 its "
            "source cannot be read" in view
        )

    def test_they_belong_to_the_latest_invocation(self, run):
        manifest_ = json.loads((run / "run.json").read_text())
        manifest_["invocations"].append(
            {"invocation": "c0ffee000001", "started_at": at(600)}
        )
        (run / "run.json").write_text(json.dumps(manifest_))
        append(run, [control(600, "start", invocation="c0ffee000001")])
        assert summarise_run(run, NOW).failed_documents == 0

    def test_a_document_that_fails_twice_counts_once_by_its_later_failure(
        self, run
    ):
        append(
            run,
            [
                event(
                    490,
                    "document-failed",
                    file="directory-1912.txt",
                    subject=DIRECTORY,
                    kind="text",
                    reason="ValueError: no page of the Adressbuch was read",
                )
            ],
        )
        summary = summarise_run(run, NOW)
        assert summary.failed_documents == 2
        assert summary.failed_by_kind == {"media (.pdf)": 1, "text (.txt)": 1}
        assert summary.failed_by_reason == {
            "its source cannot be read": 1,
            "ValueError": 1,
        }


class TestAlerts:
    def test_the_latest_alerts_come_first_tagged_new_or_carried(self, summary):
        assert [
            (alert.status, alert.check, alert.subject, alert.kind)
            for alert in summary.alerts
        ] == [
            ("carried", "language anomaly", CARD[:10], "text"),
            ("new", "claim rate", "-", "?"),
            ("carried", "fallback manifest", POSTCARD[:10], "media"),
            ("new", "implausibly short", CERT[:10], "media"),
        ]
        assert summary.alert_counts == {"new": 2, "carried": 2}
        assert [alert.value for alert in summary.alerts] == [
            "(text)",
            "0.5",
            "1",
            "0.12",
        ]

    def test_the_views_tag_each_alert(self, summary):
        view = render_terminal(summary, width=200)
        (line,) = [
            line
            for line in view.splitlines()
            if "fallback manifest" in line and POSTCARD[:10] in line
        ]
        assert line.split()[1] == "carried"
        (line,) = [line for line in view.splitlines() if CERT[:10] in line]
        assert line.split()[1] == "new"
        page = render_html(summary)
        assert '<span class="muted">carried</span>' in page
        assert '<span class="warn">new</span>' in page

    @pytest.mark.parametrize(
        ("subject", "shown"),
        [
            (CARD, CARD[:10]),  # a whole SHA-1
            (CARD[:7], CARD[:10]),  # a prefix of one document's
            ("Faded", "-"),  # a word made of the letters a to f
            ("faded", "-"),
            ("run", "-"),
            (None, "-"),
        ],
    )
    def test_an_alert_names_a_document_only_by_its_sha1(
        self, tmp_path, subject, shown
    ):
        path = record(tmp_path / "run", upto=0)
        append(path, alert(1, "transcription", "c", subject, 1, "new")[:1])
        (line,) = summarise_run(path, NOW).alerts
        assert line.subject == shown

    def test_a_prefix_two_documents_share_names_neither(self, tmp_path):
        path = record(tmp_path / "run", upto=0)
        manifest_ = json.loads((path / "run.json").read_text())
        twin = CARD[:6] + "0" * 34
        manifest_["documents"].append(
            {"sha1": twin, "kind": "text", "stages": []}
        )
        (path / "run.json").write_text(json.dumps(manifest_))
        append(path, alert(1, "transcription", "c", CARD[:6], 1, "new")[:1])
        (line,) = summarise_run(path, NOW).alerts
        assert line.subject == "-"

    def test_only_the_latest_are_kept(self, tmp_path):
        path = record(tmp_path / "run", upto=0)
        append(
            path,
            [
                (
                    "alerts.jsonl",
                    {
                        "at": at(index),
                        "stage": "transcription",
                        "check": f"check {index}",
                        "subject": CERT[:10],
                        "value": index,
                        "status": "new",
                    },
                )
                for index in range(3 * LATEST_ALERTS)
            ],
        )
        summary = summarise_run(path, NOW)
        assert len(summary.alerts) == LATEST_ALERTS
        assert summary.alerts[0].check == f"check {3 * LATEST_ALERTS - 1}"
        assert summary.alert_counts == {"new": 3 * LATEST_ALERTS}


class TestSlices:
    def test_each_slice_has_its_state(self, summary):
        states = {item.scope: item.state for item in summary.slices}
        assert states == {
            # the first invocation's pause was cleared by the second start
            "run": ACTIVE,
            "stage:transcription": ACTIVE,
            "stage:extraction": PAUSED,
            "stage:critique": ACTIVE,
            "stage:gedcom": ACTIVE,
            "kind:gedcom": ABORTED,
            "kind:media": ACTIVE,
            "kind:text": ACTIVE,
        }

    def test_the_slices_agree_with_the_driver(self, run, summary):
        shown = {
            item.scope: item.state
            for item in summary.slices
            if item.state != ACTIVE
        }
        assert shown == RunDir(run).control().scopes

    def test_a_slice_shows_since_when_who_and_why(self, summary):
        paused = summary.slice("stage:extraction")
        # paused since the first pause; a second gives the latest reason
        assert paused.since == T0 + timedelta(seconds=330)
        assert (paused.by, paused.reason) == ("monitor", "still checking")
        aborted = summary.slice("kind:gedcom")
        # an abort holds from the first, and a resume does not lift it
        assert aborted.since == T0 + timedelta(seconds=450)
        assert aborted.reason == "not needed this run"

    def test_a_pause_after_a_resume_starts_afresh(self, run):
        append(
            run,
            [
                control(600, "pause", "stage:critique", reason="first"),
                control(610, "resume", "stage:critique"),
                control(620, "pause", "stage:critique", reason="again"),
                control(630, "abort", "stage:critique", by="pacing"),
            ],
        )
        stopped = summarise_run(run, NOW).slice("stage:critique")
        assert stopped.state == ABORTED
        assert stopped.since == T0 + timedelta(seconds=630)
        assert (stopped.by, stopped.reason) == ("pacing", None)
        append(run, [control(640, "pause", "stage:transcription")])
        append(run, [control(650, "resume", "stage:transcription")])
        append(run, [control(660, "pause", "stage:transcription")])
        again = summarise_run(run, NOW).slice("stage:transcription")
        assert again.since == T0 + timedelta(seconds=660)

    def test_a_new_invocation_clears_every_slice(self, run):
        append(run, [control(600, "start", invocation="c0ffee000001")])
        summary = summarise_run(run, NOW)
        assert {item.state for item in summary.slices} == {ACTIVE}

    def test_a_paused_run_shows_in_the_header(self, run):
        append(run, [control(600, "pause", reason="lunch")])
        summary = summarise_run(run, NOW)
        assert summary.slice("run").state == PAUSED
        header = render_terminal(summary, width=200).splitlines()[1]
        assert header.startswith("running; run paused")

    @pytest.mark.parametrize("scope", [["stage:critique"], {"a": 1}, 5])
    def test_a_request_whose_scope_is_no_name_is_skipped(self, run, scope):
        """The driver's fold fails on a scope it cannot hash, and one it
        can stops no slice."""
        append(
            run,
            [
                (
                    "control.jsonl",
                    {"at": at(600), "action": "pause", "scope": scope},
                )
            ],
        )
        summary = summarise_run(run, NOW)
        assert summary.skipped_lines == 1
        assert summary.slice("stage:critique").state == ACTIVE
        assert summary.slice("stage:extraction").state == PAUSED

    @staticmethod
    def stopped(summary) -> dict:
        return {
            item.scope: item.state
            for item in summary.slices
            if item.state != ACTIVE
        }

    @pytest.mark.parametrize("scope", [False, 0, 0.0, [], {}, ""])
    def test_an_empty_or_false_scope_is_the_whole_run(self, run, scope):
        """As the driver's fold has it."""
        pause = {"at": at(600), "action": "pause", "scope": scope}
        append(run, [("control.jsonl", pause)])
        summary = summarise_run(run, NOW)
        assert summary.skipped_lines == 0
        assert self.stopped(summary) == RunDir(run).control().scopes
        assert summary.slice("run").state == PAUSED

    @pytest.mark.parametrize(
        "scope", [["stage:extraction"], {"a": 1}, [], 5, True]
    )
    def test_a_start_marker_clears_every_slice_whatever_its_scope(
        self, run, scope
    ):
        """As the driver's fold has it."""
        append(
            run,
            [
                ("control.jsonl", {"at": at(600), "action": "abort"}),
                (
                    "control.jsonl",
                    {"at": at(610), "action": "start", "scope": scope},
                ),
            ],
        )
        summary = summarise_run(run, NOW)
        assert summary.skipped_lines == 0
        assert self.stopped(summary) == RunDir(run).control().scopes == {}

    def test_the_control_file_is_read_as_the_driver_reads_it(self, run):
        """A request whose reason holds a line separator is one line, for
        the driver and for the monitor alike."""
        request(run, "pause", stage="critique", reason="slow\u2028again")
        assert RunDir(run).control().stopped(stage="critique")
        stopped = summarise_run(run, NOW).slice("stage:critique")
        assert (stopped.state, stopped.reason) == (PAUSED, "slow again")

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_a_control_file_that_cannot_be_read_leaves_every_slice_unknown(
        self, run, by
    ):
        """The driver cannot read it either, so the watch view, on the
        terminal and on the page, says the slices' state is unknown rather
        than guessing them active. The counts, which the control file does
        not touch, agree with run_state all the same."""
        out = io.StringIO()
        with unreadable_control(run, by):
            with pytest.raises(PermissionError):
                RunDir(run).control()
            summary = summarise_run(run, NOW)
            code = watch(
                run,
                once=True,
                out=out,
                clock=lambda: NOW,
                size=lambda: (200, 60),
            )
        assert code == 0
        # every slice the run knows, from its plan and its events
        stages = ["transcription", "extraction", "critique", "gedcom"]
        kinds = ["gedcom", "media", "text"]
        assert [(item.scope, item.state) for item in summary.slices] == [
            (scope, UNKNOWN)
            for scope in [
                "run",
                *(f"stage:{name}" for name in stages),
                *(f"kind:{name}" for name in kinds),
            ]
        ]
        assert {
            (item.since, item.by, item.reason) for item in summary.slices
        } == {(None, None, None)}
        assert summary.control_unreadable
        assert summary.slice("stage:nonesuch").state == UNKNOWN
        assert summary.skipped_lines == 0
        assert disagreement(run, summary) is None
        view = out.getvalue()
        assert "running; run state unknown | invocation 2" in view
        assert (
            "slices     state unknown: the control file cannot be read" in view
        )
        assert "all active" not in view
        assert "unreadable log lines" not in view
        page = page_of(run).read_text(encoding="utf-8")
        assert "running; run state unknown | invocation 2" in page
        assert page.count('<span class="warn">unknown</span>') == 8
        assert (
            '<p class="warn">State unknown: the control file cannot be '
            "read.</p>" in page
        )
        assert '<span class="good">active</span>' not in page

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_a_followed_control_file_is_read_again_at_every_refresh(
        self, run, by, monkeypatch
    ):
        """A monitor following the run shows every slice unknown from the
        refresh at which the file cannot be read, a change the view is
        drawn again for, though the file's size and modification time are
        as they were; and the slices' states again from the refresh at
        which it can be read. Every refresh reads the file, whether or not
        anything about it changed: no size, time or mode of it tells every
        change of who may read it (an owner's, say)."""
        reads = []
        read = monitor.read_jsonl
        monkeypatch.setattr(
            monitor,
            "read_jsonl",
            lambda path: reads.append(path.name) or read(path),
        )
        watcher = RunMonitor(run)
        watcher.refresh()
        known = watcher.summary(NOW).slices
        assert {item.state for item in known} == {ACTIVE, PAUSED, ABORTED}
        with unreadable_control(run, by):
            assert watcher.refresh()
            states = {item.state for item in watcher.summary(NOW).slices}
            assert states == {UNKNOWN}
            # read again, and still unreadable: nothing new to draw
            assert not watcher.refresh()
        assert watcher.refresh()
        assert watcher.summary(NOW).slices == known
        # nothing changed since: read all the same
        assert not watcher.refresh()
        assert reads.count("control.jsonl") == 5

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_no_line_of_a_control_file_that_cannot_be_read_is_skipped(
        self, run, by
    ):
        """A line skipped while the file can be read is not counted while
        it cannot be: its lines are as unknown as its requests. It counts
        again once the file can be read."""
        # a request whose scope is no name: skipped (_foldable)
        pause = {"at": at(600), "action": "pause", "scope": ["x"]}
        append(run, [("control.jsonl", pause)])
        watcher = RunMonitor(run)
        watcher.refresh()
        assert watcher.summary(NOW).skipped_lines == 1
        with unreadable_control(run, by):
            watcher.refresh()
            summary = watcher.summary(NOW)
            assert summary.control_unreadable
            assert summary.skipped_lines == 0
        watcher.refresh()
        assert watcher.summary(NOW).skipped_lines == 1

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_a_control_file_gone_while_it_cannot_be_read_stops_nothing(
        self, run, by
    ):
        """Once the file is gone, the driver has no request, and the view
        shows every slice active: a change it is drawn again for."""
        watcher = RunMonitor(run)
        watcher.refresh()
        with unreadable_control(run, by):
            watcher.refresh()
            assert watcher.summary(NOW).control_unreadable
            (run / "control.jsonl").unlink()
            assert RunDir(run).control().scopes == {}
            assert watcher.refresh()
            summary = watcher.summary(NOW)
        assert not summary.control_unreadable
        assert {item.state for item in summary.slices} == {ACTIVE}


class TestLogsThatCannotBeRead:
    """events.jsonl or alerts.jsonl that cannot be read, by the driver's
    reader either (read_jsonl): the views say which, and show nothing that
    would come from it (#295). What the other files give stands, and the
    stage counts agree with run_state wherever it can read the events."""

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_an_event_log_that_cannot_be_read_leaves_its_counts_unknown(
        self, run, by
    ):
        """rundir.run_state cannot read it either. The watch view, on the
        terminal and on the page, says what would come from it is unknown,
        rather than showing every planned stage pending; the plan, the
        slices and the alerts stand."""
        out = io.StringIO()
        with unreadable_file(run, "events.jsonl", by):
            with pytest.raises(PermissionError):
                run_state(RunDir(run))
            summary = summarise_run(run, NOW)
            code = watch(
                run,
                once=True,
                out=out,
                clock=lambda: NOW,
                size=lambda: (200, 60),
            )
        assert code == 0
        assert summary.events_unreadable and not summary.alerts_unreadable
        # nothing read from it, and no count the plan alone would give
        assert summary.stages == [] and summary.in_flight == []
        assert (summary.remaining_work, summary.remaining_calls) == (0, None)
        assert summary.calls.to_engine is None
        assert (summary.failed_documents, summary.set_aside) == (0, 0)
        assert summary.last_event_at is None and summary.skipped_lines == 0
        # the first invocation was killed: how far it ran is in its events
        assert summary.elapsed is None and summary.invocation_elapsed == 300
        # what run.json, the control file and alerts.jsonl give stands
        assert (summary.state, summary.invocations) == ("running", 2)
        assert summary.documents == 6
        assert summary.slice("stage:extraction").state == PAUSED
        assert summary.alert_counts == {"new": 2, "carried": 2}
        view = out.getvalue()
        assert (
            "running | invocation 2 | elapsed unknown (this one 5m 00s) | "
            "last event unknown" in view
        )
        for section in (
            "stages     counts unknown",
            "calls      unknown",
            "in flight  unknown",
        ):
            assert f"{section}: events.jsonl cannot be read" in view
        assert "alerts     2 carried, 2 new" in view
        for nothing in ("no stages planned", "none recorded", "to go"):
            assert nothing not in view
        page = page_of(run).read_text(encoding="utf-8")
        assert "elapsed unknown (this one 5m 00s) | last event unknown" in page
        assert (
            '<h2>Stages</h2>\n<p class="warn">Counts unknown: events.jsonl '
            "cannot be read.</p>" in page
        )
        assert (
            '<h2>Calls</h2>\n<p class="warn">Unknown: events.jsonl cannot be '
            "read.</p>" in page
        )
        assert (
            '<h2>In flight</h2>\n<p class="warn">Unknown: events.jsonl '
            "cannot be read.</p>" in page
        )
        assert "No stages planned" not in page
        assert "Nothing is running" not in page

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_an_alert_log_that_cannot_be_read_leaves_the_alerts_unknown(
        self, run, by
    ):
        """The view says the alerts are unknown rather than none. The stage
        counts do not come from alerts.jsonl: they stand, and agree with
        run_state, which reads no alert."""
        out = io.StringIO()
        with unreadable_file(run, "alerts.jsonl", by):
            with pytest.raises(PermissionError):
                RunDir(run).alerts()
            summary = summarise_run(run, NOW)
            assert disagreement(run, summary) is None
            watch(
                run,
                once=True,
                out=out,
                clock=lambda: NOW,
                size=lambda: (200, 60),
            )
        assert summary.alerts_unreadable and not summary.events_unreadable
        assert summary.alerts == [] and summary.alert_counts == {}
        assert summary.skipped_lines == 0
        view = out.getvalue()
        assert "alerts     unknown: alerts.jsonl cannot be read" in view
        assert "alerts     none" not in view
        assert "last event 1m 53s ago" in view
        page = page_of(run).read_text(encoding="utf-8")
        assert (
            '<h2>Alerts</h2>\n<p class="warn">Unknown: alerts.jsonl cannot be '
            "read.</p>" in page
        )
        assert "No alerts." not in page

    # what the driver appends to each log
    APPENDED = {
        "events.jsonl": [
            cache("cache-miss", 500, POSTCARD, "critique", reason="absent"),
            stage("done", 510, POSTCARD, "critique"),
        ],
        "alerts.jsonl": alert(
            510, "critique", "claim rate", "run", 0.25, "new"
        )[:1],
    }

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    @pytest.mark.parametrize("name", ["events.jsonl", "alerts.jsonl"])
    def test_a_followed_log_is_read_afresh_once_it_can_be_read(
        self, run, name, by
    ):
        """A monitor following the run forgets what it read of the log at
        the refresh at which it cannot be read, a change the view is drawn
        again for, and tries it at every refresh. It reads it again from
        its start at the refresh at which it can: what the driver appended
        before, and a line skipped, count once. At every step it shows
        what a monitor reading the run afresh shows."""
        # a line that is not an object: skipped, and counted
        append(run, [(name, "not an object")])
        watcher = RunMonitor(run)
        watcher.refresh()
        assert watcher.summary(NOW).skipped_lines == 1
        # appended, and not read yet
        append(run, self.APPENDED[name])
        with unreadable_file(run, name, by):
            assert watcher.refresh()
            unknown = watcher.summary(NOW)
            assert unknown == summarise_run(run, NOW)
            assert (unknown.events_unreadable, unknown.alerts_unreadable) == (
                name == "events.jsonl",
                name == "alerts.jsonl",
            )
            assert unknown.skipped_lines == 0
            # tried again, and still unreadable: nothing new to draw
            assert not watcher.refresh()
        assert watcher.refresh()
        followed = watcher.summary(NOW)
        assert followed == summarise_run(run, NOW)
        assert not (followed.events_unreadable or followed.alerts_unreadable)
        assert followed.skipped_lines == 1
        assert disagreement(run, followed) is None
        if name == "events.jsonl":
            # the postcard's critique is done, and its call counted once
            assert followed.stage("critique").running == 0
            assert followed.calls.engine == 8
        else:
            assert followed.alert_counts == {"new": 3, "carried": 2}

    def test_a_read_that_fails_partway_leaves_the_tail_as_it_was(
        self, run, monkeypatch
    ):
        """An error partway through the new lines (EIO, say) raises, as it
        does for the driver's reader, and takes none of them: the next read
        gives them all. A monitor keeps the tail of a log it cannot read
        from one refresh to the next, so a line taken then would be lost."""
        tail = monitor._Tail(run / "events.jsonl")
        tail.read()
        offset = tail.offset
        append(
            run,
            [
                stage("done", 510, POSTCARD, "critique"),
                stage("started", 511, CARD, "critique"),
            ],
        )

        class Failing(io.BufferedReader):
            """The log as the tail opens it, but its lines fail after the
            first."""

            def __iter__(self):
                yield self.readline()
                raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(
            monitor,
            "open",
            lambda path, mode: Failing(io.FileIO(path, "r")),
            raising=False,
        )
        with pytest.raises(OSError):
            tail.read()
        assert tail.offset == offset
        monkeypatch.undo()
        entries, restarted = tail.read()
        assert not restarted
        assert [entry["event"] for entry in entries] == ["done", "started"]

    def test_a_deeply_nested_line_is_skipped_by_the_tail(self, tmp_path):
        """A line nested past Python's recursion limit makes `json.loads`
        raise RecursionError while the tail decodes it: `read` skips the
        line, as it does one that raises ValueError, rather than
        propagating the error, and the entries around it come back
        whole (#285)."""
        log = tmp_path / "events.jsonl"
        started = json.dumps(stage("started", 1, CERT, "transcription")[1])
        done = json.dumps(stage("done", 2, CERT, "transcription")[1])
        log.write_bytes(f"{started}\n{'[' * 100_000}\n{done}\n".encode())
        tail = monitor._Tail(log)
        entries, restarted = tail.read()
        assert not restarted
        assert [entry["event"] for entry in entries] == ["started", "done"]
        assert tail.skipped == 1

    @pytest.mark.parametrize("name", ["events.jsonl", "alerts.jsonl"])
    def test_a_log_that_fails_partway_is_unknown_until_it_is_read_whole(
        self, run, name, monkeypatch
    ):
        """An I/O error partway through a followed log's new lines makes it
        unknown, as any error the driver's reader raises does, and so does
        the same error at the next refresh, which reads it from its start:
        no change then. A line skipped before the error is not counted
        meanwhile; once the log is read whole, it counts once, and the
        monitor shows what one reading the run afresh shows."""
        watcher = RunMonitor(run)
        watcher.refresh()
        append(run, [(name, "not an object"), *self.APPENDED[name]])
        real_open = open

        class Failing(io.BufferedReader):
            """The log as the tail opens it: every line but the last, then
            an I/O error."""

            def __iter__(self):
                lines = list(iter(self.readline, b""))
                yield from lines[:-1]
                raise OSError(errno.EIO, "Input/output error")

        def failing(path, mode):
            if Path(path).name == name:
                return Failing(io.FileIO(path, "r"))
            return real_open(path, mode)

        monkeypatch.setattr(monitor, "open", failing, raising=False)
        assert watcher.refresh()
        unknown = watcher.summary(NOW)
        assert (unknown.events_unreadable, unknown.alerts_unreadable) == (
            name == "events.jsonl",
            name == "alerts.jsonl",
        )
        assert unknown.skipped_lines == 0
        # read again from its start, and failing partway again
        assert not watcher.refresh()
        assert watcher.summary(NOW).skipped_lines == 0
        monkeypatch.undo()
        assert watcher.refresh()
        followed = watcher.summary(NOW)
        assert followed == summarise_run(run, NOW)
        assert followed.skipped_lines == 1
        assert disagreement(run, followed) is None

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    def test_the_page_notes_lines_skipped_in_the_other_logs(self, run, by):
        """While events.jsonl cannot be read, the page still says how many
        lines of the other logs were skipped, as the terminal view does,
        though it has no stage table to note them under."""
        append(run, [("alerts.jsonl", "not an object")])
        with unreadable_file(run, "events.jsonl", by):
            summary = summarise_run(run, NOW)
        assert summary.events_unreadable
        assert summary.skipped_lines == 1
        note = "unreadable log lines skipped: 1"
        assert note in render_terminal(summary, width=200)
        assert f'<li class="muted">{note}</li>' in render_html(summary)

    def test_every_unknown_line_is_a_warning(self, run):
        """On a terminal, each line that says what cannot be told is in
        the warning style, the control file's as the logs'."""
        with contextlib.ExitStack() as stack:
            for name in ("events.jsonl", "alerts.jsonl", "control.jsonl"):
                stack.enter_context(unreadable_file(run, name, "its mode"))
            summary = summarise_run(run, NOW)
        rows = render_rows(summary)
        for start in (
            "stages     counts unknown: events.jsonl",
            "calls      unknown: events.jsonl",
            "in flight  unknown: events.jsonl",
            "slices     state unknown: the control file",
            "alerts     unknown: alerts.jsonl",
        ):
            (style,) = [style for text, style in rows if start in text]
            assert style == "warn"

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    @pytest.mark.parametrize("name", ["events.jsonl", "alerts.jsonl"])
    def test_an_empty_log_that_can_be_read_again_is_a_change(
        self, tmp_path, name, by
    ):
        """Nothing is read from it then, but the view is drawn again: it no
        longer says the log cannot be read."""
        run = record(tmp_path / "run", upto=0)
        (run / name).touch()
        watcher = RunMonitor(run)
        watcher.refresh()
        with unreadable_file(run, name, by):
            assert watcher.refresh()
            # no event yet: the last one is unknown only while events.jsonl
            # cannot be read
            view = render_terminal(watcher.summary(NOW), width=200)
            assert ("last event unknown" in view) == (name == "events.jsonl")
            assert not watcher.refresh()
        assert watcher.refresh()
        summary = watcher.summary(NOW)
        assert not (summary.events_unreadable or summary.alerts_unreadable)
        view = render_terminal(summary, width=200)
        assert "cannot be read" not in view
        # no event yet: nothing to say of the last one
        assert "last event" not in view

    @pytest.mark.parametrize("by", UNREADABLE_BY)
    @pytest.mark.parametrize(
        "plan", ["no run.json", "a run.json without invocations"]
    )
    def test_a_run_its_events_alone_tell_of_is_in_an_unknown_state(
        self, run, plan, by
    ):
        """Its start and close events say whether it is going, and they
        cannot be read: its state is unknown, not 'not started', and an
        event log that cannot be read is a run's, not one yet to start."""
        if plan == "no run.json":
            (run / "run.json").unlink()
        else:
            (run / "run.json").write_text(json.dumps({"run_id": RUN_ID}))
        assert summarise_run(run, NOW).state == "running"
        with unreadable_file(run, "events.jsonl", by):
            summary = summarise_run(run, NOW)
        assert summary.found and summary.state == UNKNOWN
        view = render_terminal(summary, width=200)
        assert view.splitlines()[1] == "unknown | last event unknown"
        assert "waiting for" not in view
        assert "stages     counts unknown: events.jsonl cannot be read" in view

    def test_the_time_run_is_known_where_run_json_ends_each_invocation(
        self, run
    ):
        """Only a killed invocation's time comes from its events."""
        manifest_ = json.loads((run / "run.json").read_text())
        manifest_["invocations"][0]["ended_at"] = at(30)
        (run / "run.json").write_text(json.dumps(manifest_))
        with unreadable_file(run, "events.jsonl", "its mode"):
            summary = summarise_run(run, NOW)
        assert summary.elapsed == 330
        assert (
            "elapsed 5m 30s (this one 5m 00s) | last event unknown"
            in render_terminal(summary, width=200)
        )

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
    @pytest.mark.parametrize("what", ["a directory", "a FIFO"])
    def test_a_log_that_is_no_regular_file_holds_no_entries(self, run, what):
        """As the driver's reader has it (read_jsonl): the log is not said
        to be unreadable, and the counts agree with run_state. A FIFO is not
        opened, which would wait for a writer."""
        events = run / "events.jsonl"
        events.unlink()
        if what == "a directory":
            events.mkdir()
        else:
            os.mkfifo(events)
        summaries = []
        reader = threading.Thread(
            target=lambda: summaries.append(summarise_run(run, NOW)),
            daemon=True,
        )
        reader.start()
        reader.join(timeout=HANG_GUARD)
        if reader.is_alive():
            # it waits in open() for a writer: one that comes and goes
            # frees it
            os.close(os.open(events, os.O_WRONLY | os.O_NONBLOCK))
            reader.join(timeout=HANG_GUARD)
            pytest.fail("the monitor opened the FIFO")
        (summary,) = summaries
        assert not summary.events_unreadable
        assert disagreement(run, summary) is None
        assert summary.stage("transcription").pending == 5


# -- reading as the run grows -------------------------------------------------


class TestIncrementalReading:
    def test_a_line_not_yet_ended_waits_for_its_end(self, tmp_path):
        path = record(tmp_path / "run", upto=0)
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        with open(path / "events.jsonl", "a") as handle:
            handle.write(line[:25])  # a writer mid-line
        watcher = RunMonitor(path)
        watcher.refresh()
        assert watcher.summary(NOW).stage("transcription").running == 0
        with open(path / "events.jsonl", "a") as handle:
            handle.write(line[25:] + "\n")
        watcher.refresh()
        assert watcher.summary(NOW).stage("transcription").running == 1
        assert watcher.summary(NOW).skipped_lines == 0

    def test_a_torn_line_is_skipped_and_counted(self, tmp_path):
        """A writer killed mid-line, then a new invocation appending after
        it: the merged line is unreadable, and the lines after it count."""
        path = record(tmp_path / "run", upto=0)
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        with open(path / "events.jsonl", "a") as handle:
            handle.write(line[:25] + line + "\n")
        append(path, [stage("started", 302, POSTCARD, "transcription")])
        summary = summarise_run(path, NOW)
        assert summary.skipped_lines == 1
        assert summary.stage("transcription").running == 1

    def test_a_deeply_nested_line_is_skipped_and_counted(self, tmp_path):
        """A line nested past Python's recursion limit makes `json.loads`
        raise RecursionError, not ValueError: the tail skips it like any
        other line it cannot decode, and the events around it are read
        (#285). No writer here produces such a line."""
        path = record(tmp_path / "run", upto=0)
        append(path, [stage("started", 301, CERT, "transcription")])
        with open(path / "events.jsonl", "a") as handle:
            handle.write("[" * 100_000 + "\n")
        append(path, [stage("done", 302, CERT, "transcription")])
        summary = summarise_run(path, NOW)
        assert summary.skipped_lines == 1
        assert summary.stage("transcription").done == 1

    def test_each_refresh_reads_only_what_was_appended(self, run, monkeypatch):
        folded = []
        original = RunMonitor._fold_event

        def counting(self, event):
            folded.append(event.get("event"))
            original(self, event)

        monkeypatch.setattr(RunMonitor, "_fold_event", counting)
        watcher = RunMonitor(run)
        assert watcher.refresh()
        first = len(folded)
        assert first == sum(1 for name, _ in STREAM if name == "events.jsonl")
        version = watcher.version
        assert not watcher.refresh()
        assert watcher.version == version
        append(run, [stage("done", 500, POSTCARD, "critique")])
        assert watcher.refresh()
        assert folded[first:] == ["done"]
        assert watcher.summary(NOW).stage("critique").running == 0

    @pytest.mark.parametrize(
        "how",
        [
            "truncated",
            "cut short, its first line kept",
            "replaced by a longer one",
            "replaced by one that starts alike",
            "rewritten in place, longer",
        ],
    )
    def test_a_replaced_log_starts_the_monitor_over(self, run, how):
        watcher = RunMonitor(run)
        watcher.refresh()
        assert watcher.summary(NOW).stage("extraction").failed == 1
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        events = run / "events.jsonl"
        original = events.read_text().splitlines(keepends=True)
        size = events.stat().st_size
        longer = (line + "\n") * (size // len(line) + 2)
        fresh = run / "events.fresh"
        if how == "truncated":
            # the same file, shorter than what was read of it
            events.write_text(line + "\n")
        elif how == "cut short, its first line kept":
            # nothing but its size tells it from the file that was read
            with open(events, "r+") as handle:
                handle.truncate(len("".join(original[:3]).encode()))
        elif how == "replaced by a longer one":
            fresh.write_text(longer)
            os.replace(fresh, events)
        elif how == "replaced by one that starts alike":
            # nothing but its inode tells it from the file that was read
            fresh.write_text(original[0] + longer)
            os.replace(fresh, events)
        else:
            # the same file, as long as before at least, as a log deleted
            # and written afresh on a reused inode is: only its first line
            # tells it apart
            with open(events, "r+") as handle:
                handle.write(longer)
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert disagreement(run, summary) is None
        assert summary.stage("extraction").failed == 0
        assert summary.skipped_lines == 0

    def test_a_log_rewritten_in_place_after_a_later_read_starts_over(
        self, run
    ):
        """The first line a log was read with is kept over the reads that
        go on from where the last ended, so a rewrite in place, as long as
        before at least, starts the monitor over however many reads came
        before it."""
        watcher = RunMonitor(run)
        watcher.refresh()
        append(run, [stage("done", 500, POSTCARD, "critique")])
        watcher.refresh()
        events = run / "events.jsonl"
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        longer = (line + "\n") * (events.stat().st_size // len(line) + 2)
        with open(events, "r+") as handle:
            handle.write(longer)
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary == summarise_run(run, NOW)
        assert disagreement(run, summary) is None

    def test_the_lines_skipped_add_up_over_the_reads(self, run):
        """A line skipped at one refresh stays counted when another is
        skipped at the next."""
        watcher = RunMonitor(run)
        for _ in range(2):
            append(run, [("events.jsonl", "not an object")])
            watcher.refresh()
        assert watcher.summary(NOW).skipped_lines == 2
        assert summarise_run(run, NOW).skipped_lines == 2

    def test_a_summary_stays_as_it_was_taken(self, run):
        watcher = RunMonitor(run)
        watcher.refresh()
        before = watcher.summary(NOW)
        append(run, [cache("cache-miss", 500, POSTCARD, "critique")])
        watcher.refresh()
        assert watcher.summary(NOW).calls.engine == 8
        assert before.calls.engine == 7
        assert before.calls.roles.get("critique") == {"engine": 1}

    def test_a_run_json_for_another_run_is_taken_as_it_comes(self, run):
        """Its plan and its run id stand at once; the logs, unchanged, say
        what they said, as they do for run_state."""
        watcher = RunMonitor(run)
        watcher.refresh()
        replacement = manifest()
        replacement["run_id"] = "20260928T090000Z-0a0b0c"
        replacement["documents"] = replacement["documents"][:1]
        (run / "run.json").write_text(json.dumps(replacement))
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.run_id == "20260928T090000Z-0a0b0c"
        assert summary.documents == 1
        assert disagreement(run, summary) is None

    def test_an_unreadable_run_json_leaves_the_last_good_one(self, run):
        watcher = RunMonitor(run)
        watcher.refresh()
        (run / "run.json").write_text('{"run_id": "20260928T')  # half written
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.run_id == RUN_ID
        assert summary.documents == 6
        (run / "run.json").write_text("[1, 2]")  # not an object
        watcher.refresh()
        assert watcher.summary(NOW).documents == 6
        (run / "run.json").write_text(json.dumps(manifest()))
        watcher.refresh()
        assert watcher.summary(NOW).run_id == RUN_ID

    def test_a_deeply_nested_run_json_leaves_the_last_good_one(self, run):
        """run.json nested past Python's recursion limit makes
        `json.loads` raise RecursionError, not ValueError:
        `RunDir.manifest()` treats it like any other run.json it cannot
        read, so the monitor keeps the last good manifest instead of
        raising (#285)."""
        watcher = RunMonitor(run)
        watcher.refresh()
        (run / "run.json").write_bytes(b"[" * 100_000)
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.run_id == RUN_ID
        assert summary.documents == 6

    def test_run_json_is_parsed_again_only_when_it_changes(
        self, run, monkeypatch
    ):
        parsed = []
        original = RunDir.manifest

        def counting(self):
            parsed.append(None)
            return original(self)

        monkeypatch.setattr(RunDir, "manifest", counting)
        watcher = RunMonitor(run)
        for _ in range(3):
            watcher.refresh()
        assert len(parsed) == 1
        manifest_ = manifest()
        manifest_["mode"] = "scratch"
        (run / "run.json").write_text(json.dumps(manifest_))
        watcher.refresh()
        assert len(parsed) == 2
        assert watcher.summary(NOW).mode == "scratch"

    def test_a_long_run(self, tmp_path, monkeypatch):
        """Thousands of documents and tens of thousands of events read in
        one refresh, each event folded once; then one appended line, which
        the next refresh folds alone. The cost is counted in folds, not
        timed (#437)."""
        path = tmp_path / "run"
        path.mkdir()
        shas = [
            hashlib.sha1(f"page {n}".encode()).hexdigest() for n in range(3000)
        ]
        (path / "run.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "documents": [
                        {"sha1": sha1, "kind": "media", "stages": EXTRACTED}
                        for sha1 in shas
                    ],
                    "invocations": [
                        {"invocation": SECOND, "started_at": at(0)}
                    ],
                }
            )
        )
        lines = []
        for index, sha1 in enumerate(shas):
            for name in EXTRACTED[:2]:
                base = {"invocation": SECOND, "subject": sha1, "kind": "media"}
                lines.append(base | {"event": "started", "stage": name})
                lines.append(
                    base
                    | {
                        "event": "cache-hit",
                        "role": "r",
                        "reason": "key match",
                    }
                )
                if index < 2990 or name == "transcription":
                    lines.append(base | {"event": "done", "stage": name})
        with open(path / "events.jsonl", "w") as handle:
            handle.writelines(json.dumps(line) + "\n" for line in lines)
        folded = []
        original = RunMonitor._fold_event
        monkeypatch.setattr(
            RunMonitor,
            "_fold_event",
            lambda self, event: folded.append(1) or original(self, event),
        )
        watcher = RunMonitor(path)
        watcher.refresh()
        assert len(folded) == len(lines)
        summary = watcher.summary(NOW)
        assert summary.stage("transcription").reused == {"key match": 3000}
        assert summary.stage("extraction").running == 10
        assert summary.stage("critique").pending == 3000
        assert len(render_lines(summary, width=100, height=40)) <= 40
        render_html(summary)
        assert len(folded) == len(lines)
        append(
            path,
            [
                (
                    "events.jsonl",
                    {
                        "event": "done",
                        "subject": shas[-1],
                        "stage": "extraction",
                    },
                )
            ],
        )
        assert watcher.refresh()
        assert len(folded) == len(lines) + 1
        assert watcher.summary(NOW).stage("extraction").running == 9


class TestMalformedLines:
    """Lines that are JSON but not what their writers write, and files
    that are not what they should be: the views still come."""

    def base(self, path: Path) -> Path:
        path.mkdir()
        (path / "run.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "documents": [
                        {"sha1": CERT, "kind": "media", "stages": EXTRACTED}
                    ],
                    "invocations": [
                        {"invocation": SECOND, "started_at": at(0)}
                    ],
                    "started_at": at(0),
                }
            )
        )
        return path

    def done(self, **fields) -> tuple[str, dict]:
        name, entry = stage("done", 60, CERT, "transcription")
        return name, entry | fields

    CASES = {
        "attention a number": lambda s: [s.done(attention=1)],
        "attention true": lambda s: [s.done(attention=True)],
        "attention a float": lambda s: [s.done(event="reused", attention=2.5)],
        "an event's name a list": lambda s: [s.done(event=["done"])],
        "an event's name an object": lambda s: [s.done(event={"a": 1})],
        "a cache event's subject a list": lambda s: [
            ("events.jsonl", {"event": "cache-hit", "subject": [CERT]})
        ],
        "a control scope a list": lambda s: [
            ("control.jsonl", {"action": "pause", "scope": ["x"]})
        ],
        "a control scope an object": lambda s: [
            ("control.jsonl", {"action": "pause", "scope": {"a": 1}})
        ],
        "a control action a list": lambda s: [
            ("control.jsonl", {"action": ["pause"], "scope": "run"})
        ],
        "an alert at year 1, east of UTC": lambda s: [
            (
                "alerts.jsonl",
                {"at": "0001-01-01T00:00:00+05:00", "subject": CERT[:10]},
            )
        ],
        "a control request at year 1, east of UTC": lambda s: [
            (
                "control.jsonl",
                {
                    "at": "0001-01-01T00:00:00+05:00",
                    "action": "pause",
                    "scope": "run",
                },
            )
        ],
        "a start at year 9999, west of UTC": lambda s: [
            s.done(event="started", at="9999-12-31T23:59:59-05:00")
        ],
        "a call waiting for ever": lambda s: [
            ("events.jsonl", {"event": "call", "wait_s": float("inf")})
        ],
        "a call of no duration": lambda s: [
            ("events.jsonl", {"event": "call", "duration_s": float("nan")})
        ],
        # 1e308 seconds is finite; two of them sum past a float's range
        "two calls waiting past a float's range": lambda s: (
            [("events.jsonl", {"event": "call", "wait_s": 1e308})] * 2
        ),
        "a call waiting 400 digits of seconds": lambda s: [
            ("events.jsonl", {"event": "call", "wait_s": 10**400})
        ],
        # json reads an int of up to 4,300 digits, and writes out no more:
        # two such refusals sum to one digit past what a count shows
        "refusals summed past the digits an int writes out": lambda s: (
            [("events.jsonl", {"event": "call", "refused": 10**4300 - 1})] * 2
        ),
        "an alert's value huge": lambda s: [
            ("alerts.jsonl", {"value": {"a": [1] * 10000}, "subject": CERT})
        ],
        "a control reason with a lone surrogate": lambda s: [
            ("control.jsonl", {"action": "pause", "reason": "\ud800"})
        ],
        "a control scope with a lone surrogate": lambda s: [
            ("control.jsonl", {"action": "pause", "scope": "stage:\udcff"})
        ],
    }
    MANIFESTS = {
        "documents a number": {"run_id": "x", "documents": 5},
        "stages a number": {
            "run_id": "x",
            "documents": [{"sha1": CERT, "stages": 5}],
        },
        "invocations a number": {"run_id": "x", "invocations": 5},
        "a start at year 1, east of UTC": {
            "run_id": "x",
            "started_at": "0001-01-01T00:00:00+05:00",
            "invocations": [
                {"invocation": "i", "started_at": "0001-01-01T00:00:00+05:00"}
            ],
        },
    }

    def views(self, path: Path) -> monitor.Summary:
        summary = summarise_run(path, NOW)
        render_rows(summary, height=50)
        render_terminal(summary)
        render_html(summary)
        return summary

    @pytest.mark.parametrize("case", sorted(CASES))
    def test_a_malformed_line(self, tmp_path, case):
        path = self.base(tmp_path / "run")
        entries = self.CASES[case](self)
        with open(path / entries[0][0], "a", encoding="utf-8") as handle:
            for _, entry in entries:
                # a lone surrogate reaches the log as JSON escapes it
                handle.write(json.dumps(entry) + "\n")
        self.views(path)
        page = write_status_page(path, summarise_run(path, NOW))
        assert page is not None and page.is_file()

    @pytest.mark.parametrize("case", sorted(MANIFESTS))
    def test_a_malformed_run_json(self, tmp_path, case):
        path = tmp_path / "run"
        path.mkdir()
        (path / "run.json").write_text(json.dumps(self.MANIFESTS[case]))
        self.views(path)

    def test_truthy_attention_needs_attention_and_falsy_does_not(
        self, tmp_path
    ):
        path = self.base(tmp_path / "run")
        append(
            path,
            [
                self.done(attention=1),
                stage("done", 61, CERT, "extraction", attention=0),
            ],
        )
        summary = self.views(path)
        assert summary.stage("transcription").attention_reasons == {OTHER: 1}
        assert summary.stage("extraction").done == 1

    def test_logs_that_are_no_files(self, tmp_path):
        path = self.base(tmp_path / "run")
        (path / "events.jsonl").mkdir()
        (path / "control.jsonl").write_bytes(b'{"action": "pause"}\n\xff\n')
        self.views(path)

    @pytest.mark.parametrize(
        "value",
        [
            float("inf"),
            float("nan"),
            -1,
            "9",
            pytest.param(10**400, id="400 digits"),
        ],
    )
    def test_seconds_are_finite_and_not_negative(self, value):
        assert monitor._seconds(value) is None

    def test_an_output_takes_only_what_it_can_encode(self):
        out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        monitor._write(out, "Flörburg \ud800")
        out.seek(0)
        assert out.buffer.getvalue() == b"Fl?rburg ?"

    def test_the_page_takes_only_what_utf8_can_encode(
        self, tmp_path, monkeypatch
    ):
        path = self.base(tmp_path / "run")
        monkeypatch.setattr(
            monitor, "render_html", lambda summary, reload: "a\ud800b"
        )
        page = write_status_page(path, summarise_run(path, NOW))
        assert page.read_bytes() == b"a?b"

    def test_a_lone_surrogate_reaches_no_output(self, tmp_path):
        path = self.base(tmp_path / "run")
        with open(path / "control.jsonl", "a") as handle:
            handle.write('{"action": "pause", "reason": "\\ud800"}\n')
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        assert watch(path, once=True, out=out, clock=lambda: NOW) == 0
        assert page_of(path).is_file()


class TestEmptyRuns:
    def test_a_directory_without_a_run(self, tmp_path):
        summary = summarise_run(tmp_path / "missing", NOW)
        assert not summary.found
        assert "waiting for" in render_terminal(summary)
        assert write_status_page(tmp_path / "missing", summary) is None
        assert not (tmp_path / "missing").exists()

    def test_a_run_with_nothing_planned_or_done(self, tmp_path):
        path = tmp_path / "run"
        path.mkdir()
        (path / "run.json").write_text(json.dumps({"run_id": RUN_ID}))
        summary = summarise_run(path, NOW)
        assert summary.found and summary.stages == []
        assert summary.state == "not started"
        view = render_terminal(summary)
        assert "no stages planned or seen yet" in view
        assert "none recorded yet" in view
        assert "all active" in view
        assert write_status_page(path, summary) == page_of(path)

    def test_a_run_with_no_stages_notes_its_skipped_lines(self, tmp_path):
        """On the page as in the terminal view, though there is no stage
        table to note them under."""
        path = tmp_path / "run"
        path.mkdir()
        (path / "run.json").write_text(json.dumps({"run_id": RUN_ID}))
        append(path, [("control.jsonl", {"action": "pause", "scope": ["x"]})])
        summary = summarise_run(path, NOW)
        assert summary.stages == [] and summary.skipped_lines == 1
        note = "unreadable log lines skipped: 1"
        assert note in render_terminal(summary, width=200)
        assert f'<li class="muted">{note}</li>' in render_html(summary)

    def test_a_run_json_without_invocations_goes_by_its_events(self, tmp_path):
        path = tmp_path / "run"
        path.mkdir()
        (path / "run.json").write_text(json.dumps({"run_id": RUN_ID}))
        append(path, [event(0, "run-start"), event(9, "run-end")])
        assert summarise_run(path, NOW).state == "finished"


# -- labels -------------------------------------------------------------------


class TestLabels:
    def test_a_document_is_named_by_its_sha1_prefix(self):
        assert prefix(CERT) == CERT[:10]
        assert prefix(CERT.upper()) == CERT[:10]
        assert prefix(CERT[:7]) == "-"
        assert prefix("postcard-1938.jpg") == "-"
        assert prefix(None) == "-"

    @pytest.mark.parametrize(
        ("reason", "label"),
        [
            (FAILURE, "RuntimeError"),
            ("OSError: [Errno 2] no such file", "OSError"),
            (
                "yizkr.pipeline.llm.PassPaused: the CLI changed",
                "yizkr.pipeline.llm.PassPaused",
            ),
            (
                "TranscriptionCheckFailed: too short",
                "TranscriptionCheckFailed",
            ),
            ("KeyboardInterrupt", "KeyboardInterrupt"),
            (UNREADABLE, "its source cannot be read"),
            # a word before a colon that is no exception's is not shown:
            # it could be a name, CamelCase or not
            ("McLaudler: gone", OTHER),
            ("Löbig: no such thing", OTHER),
            ("Klara: gone", OTHER),
            ("Error: x", OTHER),
            (None, OTHER),
        ],
    )
    def test_a_failure_shows_its_exception_or_label_only(self, reason, label):
        assert failure_label(reason) == label

    @pytest.mark.parametrize(
        ("reason", "label"),
        [
            (FALLBACK, "entity manifest fell back to line-range segmentation"),
            ("no claims extracted", "no claims extracted"),
            (
                "segmentation fell back to line windows: x",
                "segmentation fell back to line windows",
            ),
            ("the letter of Hildegard Laudler to Klara", OTHER),
            ("Hildegard: x", OTHER),
            ("fell back (Hildegard)", "fell back"),
            ("", OTHER),
            (3, OTHER),
        ],
    )
    def test_a_reason_shows_its_leading_label_only(self, reason, label):
        assert reason_label(reason) == label

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            (0.12, "0.12"),
            (1234, "1,234"),
            (True, "yes"),
            (None, "-"),
            ("Hildegard Laudler", "(text)"),
            (["Klara"], "(list)"),
            ({"name": "Klara"}, "(object)"),
        ],
    )
    def test_an_alert_value_shows_as_a_number_or_its_type(self, value, shown):
        assert monitor._value(value) == shown


# -- the terminal view --------------------------------------------------------


class TestTerminalView:
    def test_without_a_tty_it_is_plain_text_within_the_width(self, summary):
        lines = render_lines(summary, width=60)
        assert all(len(line) <= 60 for line in lines)
        assert not any("\x1b" in line for line in lines)

    def test_its_rows_carry_their_styles(self, summary):
        """A screen colours each row by its style; the plain lines are the
        rows' text, cut to the width."""
        rows = render_rows(summary)
        assert rows[0][0].startswith("yizkr run 20260927T100000Z-4f2a9c")
        assert rows[0][1] == "title"
        assert any(
            text.startswith("  kind:gedcom") and style == "bad"
            for text, style in rows
        )
        assert any(
            text.startswith("  stage:extraction") and style == "warn"
            for text, style in rows
        )
        assert {style for _, style in rows} <= {
            "title",
            "head",
            "good",
            "warn",
            "bad",
            "dim",
            "",
        }
        assert render_lines(summary, width=1000) == [text for text, _ in rows]
        # no row is cut: the screen clips at its edge
        assert max(len(text) for text, _ in rows) > 60
        assert render_rows(summary) == render_rows(summary, height=None)

    def test_it_fits_the_height_dropping_details_before_sections(
        self, summary
    ):
        full = render_lines(summary, width=100, footer=["q: quit"])
        assert any("reused     transcription" in line for line in full)
        short = render_lines(summary, width=100, height=26, footer=["q: quit"])
        assert len(short) <= 26
        # the stage notes and details go; the slices, alerts and footer stay
        assert not any("reused     transcription" in line for line in short)
        assert any("stage:extraction       PAUSED" in line for line in short)
        assert any(line.startswith("alerts") for line in short)
        assert short[-1] == "q: quit"
        tiny = render_lines(summary, width=100, height=5, footer=["q: quit"])
        assert len(tiny) == 5 and tiny[-1] == "q: quit"

    def test_filled_the_footer_keeps_the_last_rows(self, summary):
        footer = ["serving the status page at http://127.0.0.1:8000/", "q"]
        plain = render_lines(summary, width=100, height=50, footer=footer)
        filled = render_lines(
            summary, width=100, height=50, footer=footer, fill=True
        )
        assert len(plain) < 50 and len(filled) == 50
        assert filled[-2:] == plain[-2:] == footer
        # while waiting for a run as well
        waiting = summarise_run(summary.path.parent / "missing", NOW)
        assert not waiting.found
        filled = render_lines(
            waiting, width=100, height=50, footer=footer, fill=True
        )
        assert len(filled) == 50 and filled[-2:] == footer
        assert filled[0].startswith("waiting for")
        filled = render_lines(
            summary, width=100, height=50, footer=footer, fill=True
        )
        # the body as it was, blank lines between it and the footer
        body = len(plain) - 3
        assert filled[:body] == plain[:body]
        assert set(filled[body:-2]) == {""}
        # a view cut to fit is as long either way
        assert render_lines(
            summary, width=100, height=5, footer=footer, fill=True
        ) == render_lines(summary, width=100, height=5, footer=footer)

    @pytest.mark.parametrize(
        "text, width, clipped",
        [
            ("Peiltal", 7, "Peiltal"),
            ("Peiltal", 6, "Pei..."),
            ("Peiltal", 4, "P..."),
            # no room for the dots: the text is only cut
            ("Peiltal", 3, "Pei"),
            ("Peiltal", 1, "P"),
            ("Калдовское", 10, "Калдовское"),
            ("Калдовское", 6, "Кал..."),
            # characters are counted, whatever a terminal's columns: off a
            # terminal the width is the environment's guess
            ("Ｐｅｉｌｔａｌ", 7, "Ｐｅｉｌｔａｌ"),
            ("Ｐｅｉｌｔａｌ", 6, "Ｐｅｉ..."),
            ("Lo\u0308bigsen", 6, "Lo\u0308..."),
        ],
    )
    def test_plain_lines_are_cut_to_the_width(self, text, width, clipped):
        assert monitor._clip(text, width) == clipped
        assert len(clipped) <= width

    def test_a_fullwidth_reason_is_clipped_by_the_screen(self, run):
        append(
            run,
            [
                control(
                    600,
                    "pause",
                    reason="Ｐｅｉｌｔａｌ ｃｅｒｔｉｆｉｃａｔｅｓ and "
                    "Ｋｒｅｄｆｅｌｄ ｌｅｔｔｅｒｓ",
                )
            ],
        )
        # the reason reaches a screen whole, for it to clip at its edge;
        # a plain line is cut inside its fullwidth words
        summary = summarise_run(run, NOW)
        assert any(
            text.endswith("Ｋｒｅｄｆｅｌｄ ｌｅｔｔｅｒｓ")
            for text, _ in render_rows(summary)
        )
        lines = render_lines(summary, width=79)
        assert any(
            "Ｐｅｉｌｔａｌ" in line and line.endswith("...") for line in lines
        )
        assert all(len(line) <= 79 for line in lines)

    def test_it_shows_what_the_run_is_doing(self, summary):
        view = render_terminal(summary, width=200)
        assert "yizkr run 20260927T100000Z-4f2a9c" in view
        assert "running | invocation 2" in view
        assert "elapsed 5m 30s (this one 5m 00s)" in view
        assert "last event 1m 53s ago" in view
        assert "reused     transcription: 1 adopted, 1 view current" in view
        assert "failed     extraction: 1 RuntimeError" in view
        assert f"{POSTCARD[:10]}  media        critique" in view
        assert (
            "to go    8 stages, ~7 engine calls at the rates so far, besides "
            "critique (text): no rate yet" in view
        )
        assert "stage:extraction       PAUSED" in view
        assert "kind:gedcom            ABORTED" in view
        # both logs can be read: nothing says either cannot
        assert "jsonl cannot be read" not in view
        assert "unknown" not in view

    def test_it_names_no_document_and_quotes_none(self, summary):
        view = render_terminal(summary, width=300)
        for leak in _leaks():
            assert leak not in view

    def test_what_the_footer_says_reaches_the_terminal_printable(
        self, tmp_path
    ):
        """The page's note names the run directory, whatever its name."""
        path = record(tmp_path / "run\x1b]0;x\x07")
        out = io.StringIO()
        watch(path, once=True, out=out, clock=lambda: NOW)
        assert "status page: " in out.getvalue()
        assert "\x1b" not in out.getvalue() and "\x07" not in out.getvalue()

    def test_no_control_character_reaches_the_terminal(self, run):
        append(
            run,
            [
                stage("started", 600, CARD, "critique\x1b[2J"),
                control(600, "pause", reason="slow\nnow \x1b[31mred"),
            ],
        )
        summary = summarise_run(run, NOW)
        lines = render_lines(summary, width=300)
        assert not any(char in line for line in lines for char in "\x1b\n\r\t")
        assert any("slow now [31mred" in line for line in lines)
        assert "?" in [counts.name for counts in summary.stages]


# -- the status page ----------------------------------------------------------


def _leaks() -> list[str]:
    """What no view may show: file names, titles and text of the documents,
    whole SHA-1s, the details of failures and attention reasons, and the
    names the other free-text fields of the recorded stream carry."""
    leaks = [name for name, _ in FILES.values()]
    leaks += ["postcard-1938 copy", UNREAD, "Stupfield", "Maisie Cutmore"]
    for key in (
        "marriage-cert-1905",
        "postcard-1938",
        "directory-1912",
        "registration-card-1945",
        "interview-2002",
    ):
        leaks += [SOURCES[key].title, SOURCES[key].text[:30]]
    leaks += list(FILES)  # whole SHA-1s
    leaks += ["Hildegard", "Laudler", "Hutmacherei", "chunks failed"]
    # the set-aside's reason and kept name, the held reason, the cache's
    # rejection, adoption and miss reasons, and the alert's value and extra
    # field
    leaks += ["Ursula", "Angerring", "Adressbuch", "Flörburg", "superseded"]
    leaks += ["Klara", "Gustav", "Rahel", "Florence Faxridge", "Brunnenring"]
    leaks += ["Löbig", "(Florence", "Permission denied"]
    return leaks


class TestStatusPage:
    def test_it_is_written_whole_into_its_own_directory(self, run, summary):
        page = write_status_page(run, summary)
        assert page == page_of(run)
        text = page.read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>")
        assert text.rstrip().endswith("</html>")
        # nothing else is written, and nothing is left from the write
        assert sorted(p.name for p in run.iterdir()) == [
            "alerts.jsonl",
            "control.jsonl",
            "events.jsonl",
            "run.json",
            STATUS_DIR,
        ]
        assert [p.name for p in (run / STATUS_DIR).iterdir()] == [STATUS_PAGE]

    def test_each_write_replaces_the_page_whole(self, run, summary):
        """Written beside its place and moved there: a reader has the old
        page or the new, never one half written."""
        first = write_status_page(run, summary)
        inode = first.stat().st_ino
        again = write_status_page(run, summary)
        assert again == first and again.stat().st_ino != inode
        assert [p.name for p in (run / STATUS_DIR).iterdir()] == [STATUS_PAGE]

    def test_a_run_directory_removed_meanwhile_is_not_made_again(
        self, run, summary, monkeypatch
    ):
        """Removed between the check for run.json and the write, as a reset
        of the run directory may remove it."""
        shutil.rmtree(run)
        monkeypatch.setattr(monitor.Path, "is_file", lambda self: True)
        with pytest.raises(FileNotFoundError):
            write_status_page(run, summary)
        monkeypatch.undo()
        assert not run.exists()

    def test_a_status_directory_that_is_a_link_is_refused(
        self, run, summary, tmp_path
    ):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (run / STATUS_DIR).symlink_to(elsewhere)
        with pytest.raises(NotADirectoryError):
            write_status_page(run, summary)
        assert list(elsewhere.iterdir()) == []

    def test_it_is_self_contained_and_reloads_itself(self, summary):
        page = render_html(summary, reload=7).lower()
        assert '<meta http-equiv="refresh" content="7">' in page
        for external in (
            "http:",
            "https:",
            "//",
            "src=",
            "href=",
            "<script",
            "<link",
            "<img",
            "url(",
            "@import",
        ):
            assert external not in page

    def test_it_names_documents_by_sha1_prefix_and_kind_only(self, summary):
        page = render_html(summary)
        for leak in _leaks():
            assert leak not in page
        assert f"<code>{POSTCARD[:10]}</code></td><td>media</td>" in page

    def test_it_shows_the_run(self, summary):
        page = render_html(summary)
        assert "<title>yizkr run 20260927T100000Z-4f2a9c</title>" in page
        assert "<h2>In flight (1)</h2>" in page
        assert '<span class="bad">aborted</span>' in page
        assert '<span class="warn">paused</span>' in page
        assert "still checking" in page
        assert "<td>(text)</td>" in page
        # the control file can be read: nothing says the state is unknown
        assert "State unknown" not in page
        # both logs can be read: the stages, the calls, the documents in
        # flight and the alerts are shown, and nothing says they are unknown
        assert "<th>stage</th>" in page
        assert "jsonl cannot be read" not in page
        assert "nknown" not in page
        # the notes on what the stage counts are made of are shown too
        assert (
            '<li class="">reused transcription: 1 adopted, 1 view current</li>'
            in page
        )
        assert (
            '<li class="muted">set aside to be made again (set-aside '
            "events): 1</li>" in page
        )

    def test_text_is_escaped(self, run):
        append(run, [control(600, "pause", reason="<b>&")])
        page = render_html(summarise_run(run, NOW))
        assert "&lt;b&gt;&amp;" in page and "<b>&" not in page


class TestServing:
    """The page, and nothing else, on 127.0.0.1."""

    @pytest.fixture
    def served(self, run, summary):
        write_status_page(run, summary)
        server = monitor.serve_status(run, "127.0.0.1", 0)
        try:
            yield run, monitor.page_url(server)
        finally:
            server.shutdown()
            server.server_close()

    @staticmethod
    def fetch(url: str, method: str = "GET") -> tuple[int, bytes, dict]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request_ = urllib.request.Request(url, method=method)
        try:
            with opener.open(request_, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_a_reset_connection_writes_nothing(self, run, summary, capfd):
        """A client that resets the connection mid-request (a browser
        cancelling a reload, a port-forwarding proxy) is no error the
        terminal hears of: under watch, the terminal is the view's."""
        write_status_page(run, summary)
        server = monitor.serve_status(run, "127.0.0.1", 0)
        handled = threading.Event()
        shutdown_request = server.shutdown_request

        def finished(request_):
            # after the request's error, if any, is handled
            shutdown_request(request_)
            handled.set()

        server.shutdown_request = finished
        try:
            client = socket.create_connection(server.server_address[:2])
            # a request line, and no end to the headers: the server waits
            # for them, and reads the reset instead
            client.sendall(b"GET / HTTP/1.1\r\nHost: Kredfeld\r\n")
            client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            client.close()
            assert handled.wait(HANG_GUARD)
        finally:
            server.shutdown()
            server.server_close()
        assert capfd.readouterr() == ("", "")

    # what every response of the server carries, an error's included
    HEADERS = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'"
        ),
    }

    def test_the_page_is_served(self, served):
        run, url = served
        for path in ("", "index.html", "?reload=1"):
            status, body, headers = self.fetch(url + path)
            assert status == 200
            assert body == page_of(run).read_bytes()
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert headers | self.HEADERS == headers
        status, body, headers = self.fetch(url, method="HEAD")
        assert (status, body) == (200, b"")
        assert headers | self.HEADERS == headers

    @pytest.mark.parametrize(
        ("method", "path", "code"),
        [
            ("GET", "run.json", 404),
            ("HEAD", "nope", 404),
            ("POST", "", 501),
            ("PUT", "index.html", 501),
        ],
    )
    def test_an_error_carries_the_same_headers(
        self, served, method, path, code
    ):
        run, url = served
        status, _, headers = self.fetch(url + path, method=method)
        assert status == code
        assert headers | self.HEADERS == headers

    def test_a_missing_page_carries_the_same_headers(self, served):
        run, url = served
        page_of(run).unlink()
        status, _, headers = self.fetch(url)
        assert status == 404
        assert headers | self.HEADERS == headers

    @pytest.mark.parametrize(
        "path",
        [
            "run.json",
            "events.jsonl",
            "control.jsonl",
            "status/",
            "status/index.html",
            "../run.json",
            "%2e%2e/run.json",
            "index.html/",
            "index.htm",
        ],
    )
    def test_nothing_else_is_served(self, served, path):
        run, url = served
        status, body, _ = self.fetch(url + path)
        assert status == 404
        assert b"20260927T100000Z" not in body and b"sha1" not in body

    def test_a_planted_link_is_not_followed(self, served):
        run, url = served
        page = page_of(run)
        page.unlink()
        page.symlink_to(run / "run.json")
        status, body, _ = self.fetch(url)
        assert status == 404 and b"sha1" not in body

    def test_a_status_directory_that_is_a_link_is_not_followed(self, served):
        run, url = served
        shutil.rmtree(run / STATUS_DIR)
        (run / "index.html").write_text("the run directory's own")
        (run / STATUS_DIR).symlink_to(run)
        status, body, _ = self.fetch(url)
        assert status == 404 and b"own" not in body

    def test_watch_serves_while_it_runs_and_stops(self, run):
        seen = []

        def between():
            shown = "\n".join(text for text, _ in screen.frames[-1][1])
            (url,) = set(re.findall(r"http://127\.0\.0\.1:\d+/", shown))
            seen.append((url, self.fetch(url)[0]))

        screen = FakeScreen([None, "q"], between=between)
        watch(
            run,
            interval=0,
            serve="127.0.0.1:0",
            screen=screen,
            clock=lambda: NOW,
        )
        assert [status for _, status in seen] == [200, 200]
        with pytest.raises(OSError):
            self.fetch(seen[0][0])

    def test_serving_is_off_by_default_and_needs_the_page(self, run):
        with pytest.raises(ValueError):
            watch(run, page=False, serve="127.0.0.1:0", out=io.StringIO())
        with pytest.raises(SystemExit):
            monitor.main(["watch", str(run), "--no-page", "--serve", "x:1"])
        with pytest.raises(SystemExit):
            monitor.main(["watch", str(run), "--serve", "no-port"])


# -- control requests ---------------------------------------------------------


class TestRequests:
    def test_a_request_goes_to_the_control_file(self, run):
        entry = request(run, "pause", stage="critique", reason="checking")
        assert (entry["scope"], entry["by"]) == ("stage:critique", "monitor")
        state = RunDir(run).control()
        assert state.stopped(stage="critique") == "stage:critique paused"
        request(run, "resume", stage="critique")
        assert RunDir(run).control().stopped(stage="critique") is None
        request(run, "abort", kind="text")
        assert (
            RunDir(run).control().stopped(kind="text") == "kind:text aborted"
        )

    def test_a_directory_without_a_run_is_refused(self, tmp_path):
        with pytest.raises(NotARunDirectory):
            request(tmp_path / "typo", "abort")
        assert not (tmp_path / "typo").exists()

    def test_a_name_is_written_without_surrounding_spaces(self, run):
        entry = request(run, "pause", stage=" extraction ")
        assert entry["scope"] == "stage:extraction"
        assert RunDir(run).control().stopped(stage="extraction")

    @pytest.mark.parametrize(
        ("field", "name"),
        [
            ("stage", ""),
            ("kind", " "),
            ("stage", "extr action"),
            ("stage", "extr\x1b[2Jaction"),
        ],
    )
    def test_an_empty_or_spaced_name_is_refused(self, run, field, name):
        before = (run / "control.jsonl").read_text()
        with pytest.raises(ValueError, match=field):
            request(run, "abort", **{field: name})
        assert (run / "control.jsonl").read_text() == before

    def test_bad_requests_are_refused(self, run):
        with pytest.raises(ValueError):
            request(run, "stop")
        with pytest.raises(ValueError):
            request(run, "pause", stage="extraction", kind="media")


class TestCommands:
    @pytest.mark.parametrize(
        ("line", "action", "scope", "reason"),
        [
            ("pause", "pause", "run", ""),
            ("pause run lunch", "pause", "run", "lunch"),
            (
                "pause stage extraction checking the prompt",
                "pause",
                "stage:extraction",
                "checking the prompt",
            ),
            ("RESUME kind:media", "resume", "kind:media", ""),
            (
                "abort stage:critique futile",
                "abort",
                "stage:critique",
                "futile",
            ),
        ],
    )
    def test_commands_are_parsed(self, line, action, scope, reason):
        command = parse_command(line)
        assert (command.action, command.scope, command.reason) == (
            action,
            scope,
            reason,
        )

    @pytest.mark.parametrize(
        "line", ["stop", "pause lunch", "pause stage", "abort kind:"]
    )
    def test_unclear_commands_are_refused_with_the_grammar(self, line):
        with pytest.raises(monitor.CommandError, match=re.escape(GRAMMAR)):
            parse_command(line)

    def test_the_help_and_the_hint_give_the_grammar(self, run):
        assert GRAMMAR in HELP[0]
        screen = FakeScreen(["q"])
        _watch(run, screen)
        assert f"':' or Enter: {GRAMMAR}" in screen.shown()[-1]

    def test_a_command_writes_its_request(self, run, summary):
        result = handle_command(
            run, "pause stage critique checking", summary=summary
        )
        assert (
            result.message == "pause requested for stage:critique: now paused"
        )
        assert result.requested["reason"] == "checking"

    def test_an_abort_is_confirmed_first(self, run):
        refused = handle_command(run, "abort", confirm=lambda question: False)
        assert refused.requested is None
        assert RunDir(run).control().stopped() is None
        questions = []
        handle_command(
            run,
            "abort",
            confirm=lambda question: questions.append(question) or True,
        )
        assert RunDir(run).control().stopped() == "run aborted"
        assert "Completed work is kept" in questions[0]

    def test_a_resume_does_not_lift_an_abort(self, run):
        before = (run / "control.jsonl").read_text()
        result = handle_command(run, "resume kind gedcom")
        assert "invoke the run again" in result.message
        assert result.requested is None
        assert (run / "control.jsonl").read_text() == before

    @pytest.mark.parametrize("given", [True, False])
    def test_an_unknown_slice_is_noted(self, run, summary, given):
        known = {"summary": summary} if given else {}
        result = handle_command(run, "pause stage extracton", **known)
        assert "no document the run knows has this slice" in result.message
        result = handle_command(run, "pause kind media", **known)
        assert "no document" not in result.message

    @pytest.mark.parametrize(
        ("line", "state"),
        [
            # a request the driver's fold cannot take: skipped
            (
                json.dumps({"action": "pause", "scope": ["x"]}).encode(),
                ("now paused", "now active", "now aborted"),
            ),
            # not UTF-8: skipped, as the driver's reader skips a line it
            # cannot decode
            (
                b'{"action": "pause", "reason": "Fl\xf6rburg"}',
                ("now paused", "now active", "now aborted"),
            ),
        ],
        ids=["unhashable-scope", "not-utf8"],
    )
    def test_commands_after_a_line_the_driver_cannot_fold(
        self, run, line, state, capsys
    ):
        """The view, and the module's command line, say where a slice
        stands without failing on the control file's other lines."""
        with open(run / "control.jsonl", "ab") as handle:
            handle.write(line + b"\n")
        assert state_now(run, "run") == "now active"
        assert summarise_run(run, NOW).slice("run").state == ACTIVE
        keys = ScriptedKeys(
            [":", ":", "q"], lines=["pause stage extraction", "resume"]
        )
        code, output = _watch(run, keys)
        assert code == 0
        assert f"resume requested for run: {state[1]}" in output
        result = handle_command(run, "pause stage extraction")
        assert result.message.endswith(state[0])
        result = handle_command(run, "abort kind media")
        assert result.message.endswith(state[2])
        assert monitor.main(["abort", str(run), "--kind", "text"]) == 0
        assert capsys.readouterr().out.strip() == (
            f"abort requested for kind:text: {state[2]}"
        )
        # the view reads the control file as the driver does
        view = {
            item.scope: item.state
            for item in summarise_run(run, NOW).slices
            if item.state != ACTIVE
        }
        assert view and view == monitor._control_state(run)

    def test_a_control_file_that_cannot_be_read(self, run, monkeypatch):
        """Where the control file itself cannot be read, a command that has
        just written its request, and state_now, say the state cannot be
        told rather than guessing it."""
        skip_unless_modes_hold(run.parent)
        control = run / "control.jsonl"
        mode = control.stat().st_mode & 0o777
        written = monitor.request

        def then_unreadable(*args, **kwargs):
            entry = written(*args, **kwargs)
            control.chmod(0)
            return entry

        monkeypatch.setattr(monitor, "request", then_unreadable)
        unknown = "its state unknown: the control file cannot be read"
        try:
            result = handle_command(run, "pause stage extraction")
            assert result.requested is not None
            assert result.message.endswith(unknown)
            assert state_now(run, "run") == unknown
        finally:
            control.chmod(mode)

    def test_help_quit_and_nonsense(self, run):
        assert handle_command(run, "help").message.startswith("commands:")
        assert handle_command(run, "q").quit
        assert "unknown command" in handle_command(run, "stop").message
        assert handle_command(run, "  ").message == ""


# -- the watch loop -----------------------------------------------------------


class ScriptedKeys:
    """Keys and typed lines from a script, for the watch loop; `between`
    runs at each wait, before its key is given."""

    def __init__(self, keys, lines=(), between=None):
        self.keys = list(keys)
        self.lines = list(lines)
        self.between = between
        self.prompts = []
        self.waits = []
        self.entered = self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.between is not None:
            self.between()
        key = self.keys.pop(0)
        if isinstance(key, BaseException):
            raise key
        return key

    def prompt(self, text):
        self.prompts.append(text)
        return self.lines.pop(0) if self.lines else None


class FakeScreen(ScriptedKeys):
    """A terminal for the watch loop (monitor.Screen): keys and typed
    lines from a script, as ScriptedKeys gives them, and each frame drawn,
    as the window's size then and its rows. `window` (columns, rows) is
    the size the window has; a test changes it, with a RESIZE key, to
    resize the window. A repaint is kept among the frames as "repaint"."""

    def __init__(self, keys, lines=(), between=None, window=(120, 60)):
        super().__init__(keys, lines, between)
        self.window = window
        self.frames = []

    def size(self):
        return self.window

    def draw(self, rows):
        assert self.entered and not self.exited
        self.frames.append((self.window, list(rows)))

    def repaint(self):
        self.frames.append("repaint")

    def shown(self, frame=-1):
        """The text of a frame's rows."""
        return [text for text, _ in self.frames[frame][1]]


def _watch(run, keys, *, out=None, **options):
    """watch with `keys`: drawn on `keys` when it is a FakeScreen, and
    printed to `out` otherwise."""
    out = out or io.StringIO()
    if isinstance(keys, FakeScreen):
        options["screen"] = keys
    else:
        options["keys"] = keys
    options.setdefault("interval", 0)
    options.setdefault("size", lambda: (120, 60))
    code = watch(run, out=out, clock=lambda: NOW, **options)
    return code, out.getvalue()


class TestWatch:
    def test_once_prints_one_plain_view_and_writes_the_page(self, run):
        out = io.StringIO()
        assert watch(run, once=True, out=out, clock=lambda: NOW) == 0
        assert "\x1b" not in out.getvalue()
        assert "yizkr run 20260927T100000Z-4f2a9c" in out.getvalue()
        assert page_of(run).is_file()

    def test_once_without_the_page(self, run):
        watch(run, once=True, page=False, out=io.StringIO())
        assert not (run / STATUS_DIR).exists()

    def test_it_waits_for_a_run_and_writes_nothing(self, tmp_path):
        out = io.StringIO()
        watch(tmp_path, once=True, out=out)
        assert "waiting for" in out.getvalue()
        assert list(tmp_path.iterdir()) == []

    def test_on_a_terminal_it_draws_each_refresh_until_q(self, run):
        screen = FakeScreen([None, None, "q"])
        code, output = _watch(run, screen, interval=2.5)
        assert code == 0 and screen.entered and screen.exited
        # each refresh draws the whole view; the screen sends what changed
        assert len(screen.frames) == 3
        assert screen.shown(0)[0] == (
            "yizkr run 20260927T100000Z-4f2a9c  (ingest run, development "
            "stage)"
        )
        # nothing is written to the output but through the screen
        assert output == ""
        # keys are waited for for the interval
        assert screen.waits == [2.5] * 3

    def test_the_view_fills_the_window_and_the_footer_keeps_the_bottom(
        self, tmp_path
    ):
        """The view takes every row, however the run changes, the key hint
        the bottom one and the served address the one above; nothing is
        cut to a width, since the screen clips at its edge."""
        run = record(tmp_path / "run")
        documents = [CERT, POSTCARD, DIRECTORY, CARD]

        def between():
            if len(screen.frames) <= len(documents):
                subject = documents[len(screen.frames) - 1]
                append(run, [stage("started", 590, subject, "critique")])

        keys = [None] * len(documents) + ["q"]
        for window in [(30, 6), (120, 60)]:
            screen = FakeScreen(keys, between=between, window=window)
            _watch(run, screen, serve="127.0.0.1:0")
            assert len(screen.frames) == len(documents) + 1
            for size, rows in screen.frames:
                assert len(rows) == size[1]
                assert rows[-1][0].startswith("':' or Enter: pause")
                assert rows[-2][0].startswith("serving the status page at")
                assert rows[-3][0].startswith("status page: ")
            assert max(len(text) for text, _ in rows) > 30
        # the documents started show as they start
        assert f"  {POSTCARD[:10]}" in "\n".join(screen.shown(2))

    def test_a_typed_command_goes_to_the_control_file(self, run):
        screen = FakeScreen([":", "q"], lines=["pause stage critique slow"])
        code, _ = _watch(run, screen)
        assert code == 0
        assert screen.prompts == ["command> "]
        requested = RunDir(run).control()
        assert requested.stopped(stage="critique") == "stage:critique paused"
        # the next frame shows the request, at once
        assert len(screen.frames) == 2
        assert screen.shown()[-2] == (
            "pause requested for stage:critique: now paused"
        )

    def test_enter_opens_the_command_line_too(self, run):
        for key in ("\n", "\r"):
            screen = FakeScreen([key, "q"], lines=["help"])
            _watch(run, screen)
            assert screen.prompts == ["command> "]
            assert screen.shown()[-2].startswith("commands: pause")

    def test_an_abort_typed_in_the_view_is_confirmed(self, run):
        screen = FakeScreen(
            [":", ":", "q"], lines=["abort", "n", "abort", "y"]
        )
        _watch(run, screen)
        assert RunDir(run).control().stopped() == "run aborted"
        assert len(screen.prompts) == 4 and "[y/N]" in screen.prompts[1]
        # asked on the screen's command line, as the command was
        assert screen.prompts[3] == screen.prompts[1]

    def test_an_abort_without_an_answer_aborts_nothing(self, run):
        screen = FakeScreen([":", "q"], lines=["abort kind media", None])
        _watch(run, screen)
        assert len(screen.prompts) == 2
        assert RunDir(run).control().stopped(kind="media") is None

    def test_plain_lines_stop_short_of_the_last_column(self, run):
        """Off a terminal, a line as wide as the width given would wrap on
        some terminals: lines stop a column short."""
        _, output = _watch(run, ScriptedKeys(["q"]), size=lambda: (30, 10))
        assert max(len(line) for line in output.split("\n")) == 29

    def test_a_plain_view_on_a_terminal_says_how_to_quit(self, run):
        """Printed to a terminal curses does not draw on, where no key is
        read, the footer says Ctrl-C quits; off a terminal, nothing."""

        class Unread(monitor.NoKeys):
            def wait(self, timeout):
                raise KeyboardInterrupt

        _, printed = _watch(run, Unread(), out=Tty())
        assert printed.rstrip().endswith(monitor.PLAIN_HINT)
        assert monitor.PLAIN_HINT == (
            "Ctrl-C: quit (keys are read only from a terminal)"
        )
        _, piped = _watch(run, Unread())
        assert monitor.PLAIN_HINT not in piped
        # nor with keys to read, nor once
        _, scripted = _watch(run, ScriptedKeys(["q"]), out=Tty())
        assert monitor.PLAIN_HINT not in scripted
        once = Tty()
        watch(run, once=True, out=once, clock=lambda: NOW)
        assert monitor.PLAIN_HINT not in once.getvalue()

    def test_ctrl_c_leaves_the_terminal_as_it_was(self, run):
        screen = FakeScreen([KeyboardInterrupt()])
        code, _ = _watch(run, screen)
        assert code == 0 and screen.exited

    def test_ctrl_l_repaints_the_whole_screen(self, run):
        screen = FakeScreen([monitor.REPAINT, None, "q"])
        _watch(run, screen)
        assert monitor.REPAINT == "\x0c"
        # the repaint comes before the next frame, which then sends it all
        assert [frame == "repaint" for frame in screen.frames] == [
            False,
            True,
            False,
            False,
        ]

    def test_a_resize_draws_the_view_at_the_new_size_at_once(self, run):
        def between():
            if len(screen.waits) == 2:
                screen.window = (90, 20)

        screen = FakeScreen(
            [None, monitor.RESIZE, "q"], between=between, window=(120, 60)
        )
        _watch(run, screen, interval=30)
        assert [size for size, _ in screen.frames] == [
            (120, 60),
            (120, 60),
            (90, 20),
        ]
        rows = screen.frames[-1][1]
        assert len(rows) == 20 and rows[-1][0].startswith("':' or Enter")
        # the short window shows the short view
        assert not any("reused     transcription" in text for text, _ in rows)

    def test_the_hint_names_the_keys(self, run):
        screen = FakeScreen(["q"])
        _watch(run, screen)
        assert screen.shown()[-1] == f"':' or Enter: {GRAMMAR}   q: quit"
        assert screen.frames[0][1][-1][1] == "dim"

    def test_the_help_is_the_footer_message(self, run):
        screen = FakeScreen(["?", "q"])
        _watch(run, screen)
        assert screen.shown()[-2].startswith("commands: pause|resume|abort")

    def test_off_a_terminal_it_prints_only_when_the_run_changes(self, run):
        waits = []

        def between():
            waits.append(None)
            if len(waits) == 2:
                append(run, [stage("done", 500, POSTCARD, "critique")])

        keys = ScriptedKeys([None, None, None, "q"], between=between)
        _, output = _watch(run, keys)
        assert "\x1b" not in output
        # the first view, and one after the run changed; no reprint for
        # the waits in which nothing happened
        assert output.count("yizkr run 20260927T100000Z-4f2a9c") == 2

    def test_the_page_is_written_as_the_run_changes_and_now_and_then(
        self, run, summary, monkeypatch
    ):
        clock = [1000.0]
        writes = []

        def write(path, summary_):
            writes.append(clock[0])
            return page_of(path)

        monkeypatch.setattr(monitor, "write_status_page", write)
        page = monitor._StatusPage(run, clock=lambda: clock[0])
        steps = [
            (0, 1),  # the first refresh: written
            (1, 2),  # the run changed, too soon after the last write
            (monitor.PAGE_MIN_INTERVAL, 2),  # changed, and due: written
            (1, 2),  # nothing changed
            (monitor.PAGE_MAX_AGE, 2),  # nothing changed, but stale: written
        ]
        for seconds, version in steps:
            clock[0] += seconds
            page.update(summary, version)
        assert writes == [
            1000.0,
            1001.0 + monitor.PAGE_MIN_INTERVAL,
            1002.0 + monitor.PAGE_MIN_INTERVAL + monitor.PAGE_MAX_AGE,
        ]
        assert page.note.startswith("status page: ")

    def test_no_page_before_a_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(monitor, "write_status_page", pytest.fail)
        page = monitor._StatusPage(tmp_path)
        page.update(summarise_run(tmp_path, NOW), 1)
        assert page.note == ""

    @pytest.mark.parametrize(
        "error",
        [
            PermissionError("read-only"),
            UnicodeEncodeError("utf-8", "", 0, 1, "surrogate"),
        ],
    )
    def test_a_page_that_cannot_be_written_is_reported(
        self, run, monkeypatch, error
    ):
        def refuse(*args, **kwargs):
            raise error

        monkeypatch.setattr(monitor, "render_html", refuse)
        out = io.StringIO()
        assert watch(run, once=True, out=out) == 0
        assert "status page not written" in out.getvalue()


class FakeWindow:
    """A curses window as _CursesScreen uses it: each call kept, in order,
    in its FakeCurses's `calls`, and keys from a script. A key of None is
    no key before the timeout (curses.error, as get_wch raises it); a
    callable key is called (to resize the window, say) and gives the key.
    addstr on the bottom row refuses text that would reach the last
    column, counting two cells for a wide or fullwidth character, as
    curses does."""

    def __init__(self, curses, keys, size):
        self.curses = curses
        self.keys = list(keys)
        self.rows, self.columns = size

    def __getattr__(self, name):
        def call(*args):
            self.curses.calls.append((name, *args))

        return call

    def getmaxyx(self):
        return self.rows, self.columns

    def insstr(self, *args):
        self.curses.calls.append(("insstr", *args))
        if self.curses.refuse_text:
            raise self.curses.error("the text does not fit")

    def addstr(self, row, column, text):
        self.curses.calls.append(("addstr", row, column, text))
        cells = sum(
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            for char in text
        )
        if column + cells > self.columns - 1:
            raise self.curses.error("addwstr() returned ERR")

    def get_wch(self):
        self.curses.calls.append(("get_wch",))
        key = self.keys.pop(0) if self.keys else None
        if callable(key):
            key = key()
        if key is None:
            raise self.curses.error("no input")
        if isinstance(key, BaseException):
            raise key
        return key


class FakeCurses:
    """The curses module as _CursesScreen and _curses_styles use it."""

    class error(Exception):  # noqa: N801 - curses' own name
        pass

    KEY_RESIZE, KEY_ENTER, KEY_BACKSPACE, KEY_UP = 410, 343, 263, 259
    A_BOLD, A_DIM = 1 << 21, 1 << 20
    COLOR_BLACK, COLOR_RED, COLOR_GREEN, COLOR_YELLOW = 0, 1, 2, 3

    def __init__(
        self,
        keys=(),
        *,
        size=(24, 80),
        colors=True,
        default_colors=True,
        fail=None,
    ):
        self.calls = []
        self.window = FakeWindow(self, keys, size)
        self.colors = colors
        self.default_colors = default_colors
        self.fail = fail
        self.refuse_text = False
        self.cup = b"\x1b[%i%p1%d;%p2%dH"

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name == self.fail:
            raise self.error(f"{name} failed")

    def initscr(self):
        self._call("initscr")
        return self.window

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **keywords: self._call(
            name, *args, *keywords.items()
        )

    def has_colors(self):
        return self.colors

    def use_default_colors(self):
        self._call("use_default_colors")
        if not self.default_colors:
            raise self.error("no default colours")

    def color_pair(self, number):
        return number << 8

    def tigetstr(self, name):
        return self.cup if name == "cup" else None

    def names(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def fake_locale(monkeypatch):
    """locale.setlocale as the monitor calls it: each call kept, the
    environment's character type "C.UTF-8", the one before "C"."""
    calls = []

    def setlocale(category, value=None):
        calls.append((category, value))
        return "C" if value is None else "C.UTF-8"

    monkeypatch.setattr(monitor.locale, "setlocale", setlocale)
    return calls


class TestCursesScreen:
    """_CursesScreen, on a fake curses module: what it asks of curses."""

    def test_it_opens_and_closes_the_screen(self, fake_locale):
        curses = FakeCurses()
        screen = monitor._CursesScreen(False, curses=curses)
        with screen as entered:
            assert entered is screen and screen.window is curses.window
            assert curses.calls == [
                ("initscr",),
                ("noecho",),
                ("cbreak",),
                ("keypad", True),
                ("set_escdelay", monitor.ESCAPE_DELAY),
                ("curs_set", 0),
            ]
            assert monitor.ESCAPE_DELAY == 25
            # the environment's character type, for wide characters
            assert fake_locale == [
                (monitor.locale.LC_CTYPE, None),
                (monitor.locale.LC_CTYPE, ""),
            ]
            del curses.calls[:]
        assert curses.calls == [
            ("keypad", False),
            ("nocbreak",),
            ("echo",),
            ("curs_set", 1),
            ("endwin",),
        ]
        # the one before, put back
        assert fake_locale[-1] == (monitor.locale.LC_CTYPE, "C")
        assert screen.window is None

    def test_the_screen_closes_however_the_loop_ends(self, fake_locale):
        curses = FakeCurses()
        with (
            pytest.raises(KeyboardInterrupt),
            monitor._CursesScreen(False, curses=curses),
        ):
            raise KeyboardInterrupt
        assert curses.names()[-1] == "endwin"
        assert fake_locale[-1] == (monitor.locale.LC_CTYPE, "C")

    @pytest.mark.parametrize("fail", ["cbreak", "start_color"])
    def test_a_screen_that_fails_to_open_is_closed(self, fake_locale, fail):
        curses = FakeCurses(fail=fail)
        with pytest.raises(curses.error):
            monitor._CursesScreen(True, curses=curses).__enter__()
        assert curses.names()[-1] == "endwin"
        assert fake_locale[-1] == (monitor.locale.LC_CTYPE, "C")

    def test_a_terminal_curses_cannot_start_on_is_left_alone(
        self, fake_locale
    ):
        curses = FakeCurses(fail="initscr")
        with pytest.raises(curses.error):
            monitor._CursesScreen(True, curses=curses).__enter__()
        assert curses.names() == ["initscr"]
        assert fake_locale[-1] == (monitor.locale.LC_CTYPE, "C")

    @pytest.mark.parametrize("fail", [None, "initscr", "cbreak"])
    def test_lines_and_columns_are_set_aside_while_it_is_open(
        self, fake_locale, monkeypatch, fail
    ):
        """Exported, they would fix curses' size, and a resize would
        change nothing: they are set aside while the screen is open, and
        put back however it closes."""
        monkeypatch.setenv("LINES", "24")
        monkeypatch.setenv("COLUMNS", "80")
        seen = []
        curses = FakeCurses(fail=fail)
        initscr = curses.initscr

        def opened():
            seen.append(("LINES" in os.environ, "COLUMNS" in os.environ))
            return initscr()

        curses.initscr = opened
        with (
            contextlib.suppress(curses.error),
            monitor._CursesScreen(False, curses=curses),
        ):
            assert "LINES" not in os.environ
        assert seen == [(False, False)]
        assert (os.environ["LINES"], os.environ["COLUMNS"]) == ("24", "80")

    def test_unset_size_variables_stay_unset(self, fake_locale, monkeypatch):
        monkeypatch.delenv("LINES", raising=False)
        monkeypatch.setenv("COLUMNS", "132")
        with monitor._CursesScreen(False, curses=FakeCurses()):
            pass
        assert "LINES" not in os.environ and os.environ["COLUMNS"] == "132"

    def test_rows_are_drawn_from_the_top_with_their_styles(self, fake_locale):
        curses = FakeCurses(size=(3, 40))
        with monitor._CursesScreen(True, curses=curses) as screen:
            del curses.calls[:]
            screen.draw(
                [
                    ("yizkr run", "title"),
                    ("", ""),
                    ("kind:gedcom ABORTED", "bad"),
                    # below the window: not drawn
                    ("':' or Enter", "dim"),
                ]
            )
        assert curses.calls[:4] == [
            ("erase",),
            ("insstr", 0, 0, "yizkr run", curses.A_BOLD),
            ("insstr", 2, 0, "kind:gedcom ABORTED", 3 << 8),
            ("refresh",),
        ]

    def test_a_row_curses_refuses_is_left_out(self, fake_locale):
        curses = FakeCurses()
        with monitor._CursesScreen(False, curses=curses) as screen:
            curses.refuse_text = True
            screen.draw([("Ｐｅｉｌｔａｌ", ""), ("Kredfeld", "")])
            assert ("insstr", 1, 0, "Kredfeld", 0) in curses.calls
            assert curses.calls[-1] == ("refresh",)

    def test_ctrl_l_repaints_the_whole_screen(self, fake_locale):
        curses = FakeCurses()
        with monitor._CursesScreen(False, curses=curses) as screen:
            screen.repaint()
            assert curses.calls[-1] == ("clearok", True)

    def test_its_size_is_the_window_s(self, fake_locale):
        with monitor._CursesScreen(
            False, curses=FakeCurses(size=(30, 100))
        ) as screen:
            assert screen.size() == (100, 30)

    def test_keys_are_waited_for_for_the_interval(self, fake_locale):
        keys = [
            "q",
            None,
            FakeCurses.KEY_RESIZE,
            FakeCurses.KEY_ENTER,
            "\x0c",
            FakeCurses.KEY_UP,
            "П",
        ]
        curses = FakeCurses(keys)
        with monitor._CursesScreen(False, curses=curses) as screen:
            got = [screen.wait(timeout) for timeout in (2.5, 0.05, 0, 2, 2)]
            got += [screen.wait(2), screen.wait(2)]
        assert got == ["q", None, monitor.RESIZE, "\n", "\x0c", None, "П"]
        timeouts = [call[1] for call in curses.calls if call[0] == "timeout"]
        assert timeouts == [2500, 50, 0, 2000, 2000, 2000, 2000]

    def test_a_command_is_typed_on_the_bottom_row(self, fake_locale):
        keys = [
            *"pausx",
            FakeCurses.KEY_BACKSPACE,
            "e",
            "\x01",
            *" stage critique",
            "\x7f",
            "e",
            "x",
            "\x08",
            "\n",
        ]
        curses = FakeCurses(keys, size=(24, 80))
        with monitor._CursesScreen(False, curses=curses) as screen:
            del curses.calls[:]
            assert screen.prompt("command> ") == "pause stage critique"
        # the cursor shown, the row cleared and written, and the keys
        # waited for without a timeout; then the cursor hidden again
        assert curses.calls[:6] == [
            ("curs_set", 1),
            ("move", 23, 0),
            ("clrtoeol",),
            ("addstr", 23, 0, "command> "),
            ("refresh",),
            ("timeout", -1),
        ]
        closing = curses.calls.index(("keypad", False))
        assert curses.calls[closing - 1] == ("curs_set", 0)
        rows = [call[1:] for call in curses.calls if call[0] == "addstr"]
        assert rows[0] == (23, 0, "command> ")
        assert rows[5] == (23, 0, "command> pausx")
        assert rows[6] == (23, 0, "command> paus")
        assert rows[-1] == (23, 0, "command> pause stage critique")
        assert ("clrtoeol",) in curses.calls

    def test_a_long_command_line_shows_its_end(self, fake_locale):
        keys = [*("Koshnovka " * 3), "\r"]
        curses = FakeCurses(keys, size=(24, 20))
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert (
                screen.prompt("command> ") == "Koshnovka Koshnovka Koshnovka"
            )
        rows = [call[3] for call in curses.calls if call[0] == "addstr"]
        # a column short of the window's width, for the cursor
        assert rows[-1] == ("command> " + "Koshnovka " * 3)[-19:]
        assert all(len(row) <= 19 for row in rows)

    def test_an_empty_line_or_a_closed_input_types_nothing(self, fake_locale):
        curses = FakeCurses(["\n", " ", " ", "\n", "a"])
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert screen.prompt("command> ") is None
            assert screen.prompt("command> ") is None
            # the input ends mid-line
            assert screen.prompt("command> ") is None
        assert curses.calls.count(("curs_set", 0)) == 4

    def test_a_command_line_is_cut_at_its_limit(
        self, fake_locale, monkeypatch
    ):
        assert monitor.PROMPT_LIMIT == 500
        monkeypatch.setattr(monitor, "PROMPT_LIMIT", 5)
        curses = FakeCurses([*"Kredfeld", "\n"])
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert screen.prompt("command> ") == "Kredf"

    def test_a_wide_command_line_shows_its_end(self, fake_locale):
        """curses measures what fits: the line's start goes, a character
        at a time, until its end fits before the last column."""
        reason = "Ｋｒｅｄｆｅｌｄ ｌｅｔｔｅｒｓ END"
        keys = [*f"pause run {reason}", "\n"]
        curses = FakeCurses(keys, size=(24, 40))
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert screen.prompt("command> ") == f"pause run {reason}"
        line = f"command> pause run {reason}"

        def cells(text):
            return sum(
                2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                for char in text
            )

        # the longest end of the line that fits before the last column
        fits = max(
            (line[start:] for start in range(len(line))),
            key=lambda end: len(end) if cells(end) <= 39 else -1,
        )
        assert fits.endswith("ｌｅｔｔｅｒｓ END") and cells(fits) <= 39
        rows = [call[3] for call in curses.calls if call[0] == "addstr"]
        assert rows[-1] == fits

    def test_escape_cancels_the_command_line_and_its_burst(self, fake_locale):
        """An Escape cancels; the rest of its sequence (an arrow key curses
        did not read as one) is dropped, never typed into a command; keys
        typed after it are read as keys."""
        curses = FakeCurses(["p", "a", "\x1b", "[", "A", "\n", None, "q"])
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert screen.prompt("command> ") is None
            # the sequence read without waiting, and no more of it
            assert ("timeout", 0) in curses.calls
            assert curses.window.keys == ["\n", None, "q"]
            rows = [call[3] for call in curses.calls if call[0] == "addstr"]
            assert rows[-1] == "command> pa"

    @pytest.mark.parametrize(
        "sequence",
        [
            # Alt and a key: the one key
            ["x"],
            # a control sequence: up to its final byte, parameters and all
            ["[", "A"],
            ["[", "1", ";", "5", "C"],
            ["[", "2", "0", "0", "~"],
            # a single shift: one key more
            ["O", "P"],
            # an Escape alone
            [],
        ],
    )
    def test_an_escape_drops_one_sequence_in_the_view(
        self, fake_locale, sequence
    ):
        """A key typed ahead after an Escape's sequence is read as a key:
        `q` still quits."""
        curses = FakeCurses(["\x1b", *sequence, "q", None])
        with monitor._CursesScreen(False, curses=curses) as screen:
            if not sequence:
                # an Escape and a key waiting behind it are Alt and that
                # key, which goes with it
                assert screen.wait(0) is None
                assert curses.window.keys == [None]
                return
            assert screen.wait(0) is None
            assert screen.wait(0) == "q"

    def test_a_control_sequence_is_dropped_up_to_its_limit(self, fake_locale):
        assert monitor.SEQUENCE_LIMIT == 32
        parameters = ["1"] * 40
        curses = FakeCurses(["\x1b", "[", *parameters, "q"])
        with monitor._CursesScreen(False, curses=curses) as screen:
            assert screen.wait(0) is None
        assert curses.window.keys == ["1"] * 8 + ["q"]

    def test_a_resize_during_the_command_line_draws_the_view_again(
        self, fake_locale
    ):
        """The view as last drawn, at the new size, and the prompt on the
        new bottom row."""
        curses = FakeCurses(size=(24, 80))

        def resize():
            curses.window.rows, curses.window.columns = 10, 60
            return FakeCurses.KEY_RESIZE

        curses.window.keys = ["p", resize, "a", "\n"]
        rows = [("yizkr run", "title"), ("in flight  2", "head")]
        with monitor._CursesScreen(False, curses=curses) as screen:
            screen.draw(rows)
            del curses.calls[:]
            assert screen.prompt("command> ") == "pa"
        after = curses.calls[curses.calls.index(("get_wch",), 7) :]
        assert after[1:5] == [
            ("erase",),
            ("insstr", 0, 0, "yizkr run", 0),
            ("insstr", 1, 0, "in flight  2", 0),
            ("refresh",),
        ]
        prompts = [call[1:] for call in curses.calls if call[0] == "addstr"]
        assert prompts[0] == (23, 0, "command> ")
        assert prompts[-1] == (9, 0, "command> pa")


class TestCursesStyles:
    def test_colours_come_from_colour_pairs(self):
        curses = FakeCurses()
        styles = monitor._curses_styles(curses, True)
        assert styles == {
            "title": curses.A_BOLD,
            "head": curses.A_BOLD,
            "dim": curses.A_DIM,
            "good": 1 << 8,
            "warn": 2 << 8,
            "bad": 3 << 8,
        }
        # on the terminal's own background
        assert [call for call in curses.calls if call[0] == "init_pair"] == [
            ("init_pair", 1, curses.COLOR_GREEN, -1),
            ("init_pair", 2, curses.COLOR_YELLOW, -1),
            ("init_pair", 3, curses.COLOR_RED, -1),
        ]

    def test_without_default_colours_the_background_is_black(self):
        curses = FakeCurses(default_colors=False)
        monitor._curses_styles(curses, True)
        assert ("init_pair", 3, curses.COLOR_RED, curses.COLOR_BLACK) in (
            curses.calls
        )

    def test_a_terminal_without_colours_gets_none(self):
        curses = FakeCurses(colors=False)
        assert monitor._curses_styles(curses, True) == {
            "title": curses.A_BOLD,
            "head": curses.A_BOLD,
            "dim": curses.A_DIM,
        }
        assert curses.calls == []

    def test_no_colour_asked_for_is_no_style(self):
        curses = FakeCurses()
        assert monitor._curses_styles(curses, False) == {}
        assert curses.calls == []


class Tty(io.StringIO):
    """A stream that says it is a terminal."""

    def isatty(self):
        return True

    def fileno(self):
        return 1


class NotTty(Tty):
    """A stream with a file descriptor that is no terminal: a file."""

    def isatty(self):
        return False


class TestDefaultScreen:
    """Which output gets curses: the standard output, when it and the
    standard input are a terminal that can move its cursor."""

    @pytest.fixture
    def terminal(self, monkeypatch):
        """A fake curses, and terminals for the standard input and output:
        set in the test's call, since pytest's capture sets the standard
        streams again between a test's setup and its call."""
        curses = FakeCurses()
        monkeypatch.setitem(sys.modules, "curses", curses)
        monkeypatch.delenv("NO_COLOR", raising=False)

        def ttys():
            monkeypatch.setattr(sys, "stdin", Tty())
            monkeypatch.setattr(sys, "stdout", Tty())
            return curses

        return ttys

    def test_a_terminal_gets_curses(self, terminal):
        terminal = terminal()
        screen = monitor._default_screen(sys.stdout)
        assert isinstance(screen, monitor._CursesScreen)
        assert screen.curses is terminal and screen.color
        assert ("setupterm",) in [call[:1] for call in terminal.calls]

    def test_no_color_asks_for_no_colours(self, terminal, monkeypatch):
        terminal()
        # set but empty, it asks for nothing
        monkeypatch.setenv("NO_COLOR", "")
        assert monitor._default_screen(sys.stdout).color
        monkeypatch.setenv("NO_COLOR", "1")
        assert not monitor._default_screen(sys.stdout).color

    def test_another_output_gets_none(self, terminal, monkeypatch):
        curses = terminal()
        assert monitor._default_screen(sys.stdout) is not None
        del curses.calls[:]
        # an output that is no terminal, though it has a file descriptor
        # (`watch > view.txt`): curses is not even set up
        monkeypatch.setattr(sys, "stdout", NotTty())
        assert monitor._default_screen(sys.stdout) is None
        assert curses.calls == []
        monkeypatch.setattr(sys, "stdout", Tty())
        assert monitor._default_screen(Tty()) is None
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        assert monitor._default_screen(sys.stdout) is None
        monkeypatch.setattr(sys, "stdout", Tty())
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        assert monitor._default_screen(sys.stdout) is None

    def test_a_terminal_that_cannot_move_its_cursor_gets_none(self, terminal):
        terminal = terminal()
        assert monitor._default_screen(sys.stdout) is not None
        terminal.cup = None
        assert monitor._default_screen(sys.stdout) is None

    def test_a_terminal_curses_does_not_know_gets_none(self, terminal):
        terminal = terminal()
        assert monitor._default_screen(sys.stdout) is not None
        terminal.fail = "setupterm"
        assert monitor._default_screen(sys.stdout) is None

    def test_without_curses_there_is_none(self, terminal, monkeypatch):
        terminal()
        assert monitor._default_screen(sys.stdout) is not None
        monkeypatch.setitem(sys.modules, "curses", None)
        assert monitor._default_screen(sys.stdout) is None

    def test_watch_draws_on_it_unless_once(self, terminal, run, monkeypatch):
        opened = []

        class Opened(FakeScreen):
            def __init__(self, color):
                super().__init__(["q"])
                opened.append(self)

        monkeypatch.setattr(monitor, "_CursesScreen", Opened)
        terminal()
        # a screen, or the loop below would never read its 'q'
        assert isinstance(monitor._default_screen(sys.stdout), Opened)
        del opened[:]
        assert watch(run, interval=0, clock=lambda: NOW) == 0
        (screen,) = opened
        assert screen.frames and screen.exited
        watch(run, once=True, clock=lambda: NOW)
        assert len(opened) == 1
        # what --once printed
        assert sys.stdout.getvalue().startswith("yizkr run")


# what the child process runs: watch on the pseudo-terminal it is given as
# its standard input and output, which it takes as its controlling
# terminal, so that a resize signals it as a terminal does
CHILD = """
import fcntl, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
from yizkr.pipeline.monitor import main
sys.exit(main(sys.argv[1:]))
"""

# a shell's job control, in small: watch runs in a process group of its
# own, in the foreground of the pseudo-terminal, and the shell gives its
# process id on its report pipe; when it stops (Ctrl-Z), the shell takes
# the terminal back and says "stopped" there, and a line on its control
# pipe brings watch to the foreground and continues it (`fg`). The shell
# ends with watch's exit code.
SHELL = """
import fcntl, os, signal, sys, termios, traceback
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
report = os.fdopen(int(sys.argv[1]), "w")
control = os.fdopen(int(sys.argv[2]))
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpgrp())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    try:
        from yizkr.pipeline.monitor import main
        code = main(sys.argv[3:])
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    os._exit(code)
try:
    os.setpgid(pid, pid)
except OSError:
    pass
os.tcsetpgrp(0, pid)
report.write(f"{pid}\\n")
report.flush()
while True:
    _, status = os.waitpid(pid, os.WUNTRACED)
    if os.WIFSTOPPED(status):
        os.tcsetpgrp(0, os.getpgrp())
        report.write(f"stopped {os.WSTOPSIG(status)}\\n")
        report.flush()
        control.readline()
        os.tcsetpgrp(0, pid)
        os.killpg(pid, signal.SIGCONT)
        continue
    sys.exit(os.waitstatus_to_exitcode(status))
"""


class PseudoTerminal:
    """A child running `watch` on a pseudo-terminal of `columns` by
    `rows`, and the bytes it writes there, read as they come. `script` is
    what the child runs (CHILD, or SHELL with its two pipes' descriptors
    in `script_args` and `pass_fds`); `environment` is set in the child's
    environment on top of the test's own."""

    def __init__(
        self,
        run,
        *args,
        term="xterm",
        columns=100,
        rows=30,
        script=CHILD,
        script_args=(),
        pass_fds=(),
        environment=None,
    ):
        pty = pytest.importorskip("pty")
        self.termios = pytest.importorskip("termios")
        self.fcntl = pytest.importorskip("fcntl")
        self.leader, self.follower = pty.openpty()
        self.resize(columns, rows)
        self.before = self.termios.tcgetattr(self.follower)
        self.data = b""
        self.changed = threading.Condition()
        # LINES and COLUMNS, exported, would fix curses' size whatever the
        # pseudo-terminal's: GNU readline sets them in the environment of
        # any process that imports it, as pytest's capture does, and
        # pytest-xdist's workers inherit them
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LC_")
            and key not in ("NO_COLOR", "LINES", "COLUMNS")
        }
        env.update(TERM=term, LANG="C.UTF-8", **(environment or {}))
        self.child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                *map(str, script_args),
                "watch",
                str(run),
                *args,
            ],
            stdin=self.follower,
            stdout=self.follower,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=pass_fds,
            env=env,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.leader, 65536)
            except OSError:
                chunk = b""
            with self.changed:
                self.data += chunk
                self.changed.notify_all()
            if not chunk:
                return

    def resize(self, columns, rows):
        size = struct.pack("HHHH", rows, columns, 0, 0)
        self.fcntl.ioctl(self.leader, self.termios.TIOCSWINSZ, size)

    def resize_view(self, columns, rows):
        """Resize the window while the view waits for a key, and wait for
        the footer drawn again. A key the view does not use follows: a
        resize signalled just before curses blocks reading is seen only
        at its next read, which a long interval would put off."""
        mark = self.mark()
        self.resize(columns, rows)
        self.type(b"x")
        self.until(lambda data: HINT in data[mark:])
        return mark

    def type(self, keys: bytes):
        os.write(self.leader, keys)

    def until(self, predicate):
        """Wait until what the child has written meets `predicate`; how
        much it had written then."""
        with self.changed:
            met = self.changed.wait_for(
                lambda: predicate(self.data), timeout=HANG_GUARD
            )
            assert met, self.data[-2000:]
            return len(self.data)

    def mark(self):
        """How much the child has written so far."""
        with self.changed:
            return len(self.data)

    def whole_frame(self, mark):
        """Ctrl-L, and the whole screen it sends, once sent: what the
        child wrote after `mark` before it, and the screen."""
        self.type(b"\x0c")
        self.until(lambda data: HINT in data[mark:].partition(CLEAR)[2])
        before, _, screen = self.data[mark:].partition(CLEAR)
        return before, screen

    def count(self, text: bytes, since=0):
        return self.data[since:].count(text)

    def __enter__(self):
        return self

    def __exit__(self, kind, *exc):
        """The child waited for (killed at once when the test failed),
        and its exit code and standard error kept as `code` and
        `errors`."""
        if kind is not None and self.child.poll() is None:
            self.child.kill()
        self.code, self.errors = self.close()

    def close(self):
        """Wait for the child to end, and read what it wrote last; its
        exit code and standard error."""
        try:
            code = self.child.wait(timeout=HANG_GUARD)
            errors = self.child.stderr.read()
        finally:
            if self.child.poll() is None:
                self.child.kill()
            self.child.stderr.close()
            self.after = self.termios.tcgetattr(self.follower)
            os.close(self.follower)
            self.reader.join(HANG_GUARD)
            os.close(self.leader)
        assert not self.reader.is_alive()
        return code, errors


# the read system call's number, by machine, as /proc/PID/syscall gives it
READ_CALLS = {"x86_64": "0", "aarch64": "63"}
# how many times to look before giving up on a process reading its keys
LOOKS = 1_000_000


def _until_reading_keys(pid):
    """Wait until process `pid` sleeps reading its standard input: in the
    read system call, on descriptor 0 (/proc/PID/syscall), looking again
    after each yield of the processor. curses holds Ctrl-Z off (ignores
    SIGTSTP) while it updates the screen, so a Ctrl-Z meant to stop a
    view waiting for keys must reach it while it waits."""
    if not Path("/proc/self/syscall").exists():
        pytest.skip("no /proc/PID/syscall to tell a process waits for keys")
    call = READ_CALLS.get(platform.machine())
    if call is None:
        pytest.skip(f"no read system call number for {platform.machine()}")
    status = Path(f"/proc/{pid}/syscall")
    for _ in range(LOOKS):
        with contextlib.suppress(OSError):
            if status.read_text().split()[:2] == [call, "0x0"]:
                return
        os.sched_yield()
    pytest.fail(f"process {pid} never waited for a key")


def _xterm(name, *parameters):
    """The string terminfo gives xterm's capability `name`, with its
    `parameters`, as curses writes it."""
    curses = pytest.importorskip("curses")
    if not _xterm.ready:
        with open(os.devnull, "w") as null:
            curses.setupterm("xterm", null.fileno())
        _xterm.ready = True
    value = curses.tigetstr(name)
    assert value is not None, name
    return curses.tparm(value, *parameters) if parameters else value


_xterm.ready = False

TITLE = f"yizkr run {RUN_ID}".encode()
HINT = b"':' or Enter: pause"
URL = re.compile(rb"http://127\.0\.0\.1:\d+/")
# xterm's clear (a screen sent whole), as terminfo gives it
CLEAR = b"\x1b[H\x1b[2J"


def _left_as_found(terminal, mark):
    """After quitting: the alternate screen left (rmcup) and the cursor
    shown (cnorm), as written after `mark`, and the terminal's settings as
    they were."""
    assert _xterm("clear") == CLEAR
    written = terminal.data[mark:]
    assert _xterm("rmcup") in written
    assert _xterm("cnorm") in written
    assert terminal.after == terminal.before


class TestRealTerminal:
    """`watch` through the real curses, on a pseudo-terminal: no real
    terminal is needed. Each step waits on what the child writes."""

    def test_a_watch_on_a_terminal(self, tmp_path):
        """With an interval no test waits out, the view is drawn again
        only for a key: each key a refresh, and Ctrl-L the screen sent
        whole, which shows what the view holds then."""
        run = record(tmp_path / "run")
        # tall enough for the whole view, every document in flight listed
        with PseudoTerminal(
            run, "--interval", "3600", "--serve", "127.0.0.1:0", rows=60
        ) as terminal:
            terminal.until(lambda data: TITLE in data and HINT in data)
            assert len(URL.findall(terminal.data)) == 1
            # the counters change: documents start, one at a time; a key
            # (one the view does not use) brings a refresh, which sends
            # what changed, never the served address; then the whole
            # screen shows the document in flight
            starts = [
                (CERT, b"media"),
                (DIRECTORY, b"text"),
                (CARD, b"text"),
                (INTERVIEW, b"text"),
            ]
            for count, (subject, kind) in enumerate(starts, 2):
                append(run, [stage("started", 590, subject, "critique")])
                mark = terminal.mark()
                terminal.type(b"x")
                terminal.until(lambda data, m=mark: len(data) > m)
                changed, screen = terminal.whole_frame(mark)
                assert changed and not URL.search(changed)
                assert TITLE not in changed
                assert f"in flight  {count}".encode() in screen
                assert subject[:10].encode() + b"  " + kind in screen
                assert len(URL.findall(screen)) == 1
            # a resize draws the view at the new size: the footer moves to
            # the new bottom rows
            terminal.resize_view(80, 24)
            # a command, typed on the command line
            mark = terminal.mark()
            terminal.type(b":")
            terminal.until(lambda data: b"command>" in data[mark:])
            terminal.type(b"pause stage critique checking\r")
            terminal.until(lambda data: b"now paused" in data[mark:])
            requested = RunDir(run).control()
            assert requested.stopped(stage="critique") == (
                "stage:critique paused"
            )
            mark = terminal.mark()
            terminal.type(b"q")
        assert (terminal.code, terminal.errors) == (0, b"")
        _left_as_found(terminal, mark)

    def test_exported_lines_and_columns_do_not_fix_the_size(self, tmp_path):
        """The view takes the window's size, not LINES and COLUMNS, and
        follows it as it is resized: 40 rows hold the whole view, the
        stage notes with it; 22 do not; 40 again do."""
        run = record(tmp_path / "run")
        notes = b"reused     transcription"
        with PseudoTerminal(
            run,
            "--interval",
            "3600",
            "--no-page",
            rows=40,
            environment={"LINES": "24", "COLUMNS": "80"},
        ) as terminal:
            terminal.until(lambda data: HINT in data)
            assert _xterm("csr", 0, 39) in terminal.data
            assert notes in terminal.data
            for rows, shown in ((22, False), (40, True)):
                terminal.resize_view(100, rows)
                # as drawn at the new size: curses' own repaint of the old
                # screen, as the resize takes it, comes first
                _, screen = terminal.whole_frame(terminal.mark())
                assert (notes in screen) is shown
            mark = terminal.mark()
            terminal.type(b"q")
        assert (terminal.code, terminal.errors) == (0, b"")
        _left_as_found(terminal, mark)

    def test_an_abort_is_confirmed_on_the_command_line(self, tmp_path):
        run = record(tmp_path / "run")
        with PseudoTerminal(
            run, "--interval", "0.05", "--no-page"
        ) as terminal:
            terminal.until(lambda data: HINT in data)
            mark = terminal.mark()
            terminal.type(b"\rabort kind media\r")
            terminal.until(lambda data: b"[y/N]" in data[mark:])
            terminal.type(b"y\r")
            terminal.until(lambda data: b"ABORTED" in data[mark:])
            assert RunDir(run).control().stopped(kind="media") == (
                "kind:media aborted"
            )
            mark = terminal.mark()
            terminal.type(b"q")
        assert (terminal.code, terminal.errors) == (0, b"")
        _left_as_found(terminal, mark)

    def test_ctrl_z_and_fg_leave_the_view_as_it_was(self, tmp_path):
        """Under a shell's job control: Ctrl-Z, with the command line
        open, gives the shell its screen back and stops the monitor; `fg`
        brings back the view, the half-typed command with it, which then
        goes on as typed."""
        run = record(tmp_path / "run")
        report_read, report_write = os.pipe()
        control_read, control_write = os.pipe()
        try:
            with PseudoTerminal(
                run,
                "--interval",
                "3600",
                "--no-page",
                script=SHELL,
                script_args=(report_write, control_read),
                pass_fds=(report_write, control_read),
            ) as terminal:
                os.close(report_write)
                os.close(control_read)
                report_write = control_read = None
                ready, _, _ = select.select([report_read], [], [], HANG_GUARD)
                assert ready
                watcher = int(os.read(report_read, 64))
                terminal.until(lambda data: TITLE in data and HINT in data)
                mark = terminal.mark()
                terminal.type(b":pause run Kred")
                terminal.until(lambda data: b"Kred" in data[mark:])
                _until_reading_keys(watcher)
                mark = terminal.mark()
                terminal.type(b"\x1a")
                ready, _, _ = select.select([report_read], [], [], HANG_GUARD)
                assert ready
                stopped = f"stopped {signal.SIGTSTP.value}\n".encode()
                assert os.read(report_read, 64) == stopped
                # stopped, the shell has its screen, cursor and settings
                terminal.until(lambda data: _xterm("rmcup") in data[mark:])
                # the cursor shown (the command line had shown it): the last
                # of xterm's visibility strings written is cnorm
                written = terminal.data
                assert written.rfind(_xterm("cnorm")) > written.rfind(
                    _xterm("civis")
                )
                assert terminal.termios.tcgetattr(terminal.follower) == (
                    terminal.before
                )
                mark = terminal.mark()
                os.write(control_write, b"fg\n")
                terminal.until(
                    lambda data: (
                        TITLE in data[mark:]
                        and b"command> pause run Kred" in data[mark:]
                    )
                )
                terminal.type(b"feld\r")
                terminal.until(lambda data: b"now paused" in data[mark:])
                assert RunDir(run).control().stopped() == "run paused"
                *_, line = (run / "control.jsonl").read_text().splitlines()
                assert json.loads(line)["reason"] == "Kredfeld"
                # an Alt-x (an Escape and a key) drops the one key: the 'q'
                # typed after it quits
                mark = terminal.mark()
                terminal.type(b"\x1bxq")
        finally:
            for fd in (report_read, report_write, control_read, control_write):
                if fd is not None:
                    os.close(fd)
        assert (terminal.code, terminal.errors) == (0, b"")
        _left_as_found(terminal, mark)

    def test_a_terminal_that_cannot_move_its_cursor_is_printed_to(
        self, tmp_path
    ):
        """On a terminal without cursor addressing the view is printed
        plain, as off a terminal, its footer saying how to quit; Ctrl-C
        quits."""
        run = record(tmp_path / "run")
        with PseudoTerminal(
            run, "--interval", "0.05", term="dumb"
        ) as terminal:
            terminal.until(
                lambda data: (
                    TITLE in data and monitor.PLAIN_HINT.encode() in data
                )
            )
            terminal.type(b"\x03")
        assert (terminal.code, terminal.errors) == (0, b"")
        assert b"\x1b" not in terminal.data
        assert terminal.after == terminal.before


def test_main_requests_and_watches_once(run, capsys):
    assert monitor.main(["pause", str(run), "--kind", "media"]) == 0
    assert capsys.readouterr().out.strip() == (
        "pause requested for kind:media: now paused"
    )
    assert monitor.main(["pause", str(run), "--stage", "extracton"]) == 0
    assert "no document the run knows has this slice" in (
        capsys.readouterr().out
    )
    assert monitor.main(["watch", str(run), "--once", "--no-page"]) == 0
    assert "kind:media" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        monitor.main(["abort", str(run.parent / "typo")])
