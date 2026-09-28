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

import hashlib
import io
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from yizkr.pipeline import monitor
from yizkr.pipeline.monitor import (
    ABORTED,
    ACTIVE,
    CLEAR_BELOW,
    GRAMMAR,
    HELP,
    HOME,
    LATEST_ALERTS,
    OTHER,
    PAUSED,
    SHOW_CURSOR,
    STATUS_DIR,
    STATUS_PAGE,
    NotARunDirectory,
    RunMonitor,
    failure_label,
    handle_command,
    parse_command,
    prefix,
    reason_label,
    render_html,
    render_lines,
    render_terminal,
    request,
    summarise_run,
    watch,
    write_status_page,
)
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    NEEDS_ATTENTION,
    PENDING,
    RUNNING,
    RunDir,
    run_state,
    summarise,
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


def disagreement(run: Path, summary=None) -> str | None:
    """How the monitor's counts differ from rundir.run_state's for the
    same files (the monitor's split recombined: done and reused are
    done, running and interrupted are running), or None."""
    summary = summary or summarise_run(run, NOW)
    expected = summarise(run_state(RunDir(run)))
    seen = {}
    for counts in summary.stages:
        row = {
            PENDING: counts.pending - counts.interrupted,
            RUNNING: counts.running + counts.interrupted,
            DONE: counts.done + sum(counts.reused.values()),
            NEEDS_ATTENTION: counts.attention,
            FAILED: counts.failed,
        }
        row = {status: count for status, count in row.items() if count}
        if row:
            seen[counts.name] = row
    return None if seen == expected else f"{seen} != {expected}"


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
        that reads them afresh."""
        stages = ["transcription", "extraction", "critique"]
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
                elif roll < 0.12:
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

    def test_an_interrupted_invocation(self, run):
        append(run, [event(600, "run-interrupted", statuses={})])
        summary = summarise_run(run, NOW)
        assert summary.state == "interrupted"
        assert summary.stage("critique").interrupted == 1


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
            "usage limit", "five_hour", datetime(2026, 9, 27, 14, tzinfo=UTC)
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
            ("Ebba", "-"),  # a name made of the letters a to f
            ("ebba", "-"),
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

    @pytest.mark.parametrize("scope", [["stage:critique"], {"a": 1}])
    def test_a_request_the_driver_cannot_fold_is_skipped(self, run, scope):
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

    def test_the_control_file_is_read_as_the_driver_reads_it(self, run):
        """A request whose reason holds a line separator is one line, for
        the driver and for the monitor alike."""
        request(run, "pause", stage="critique", reason="slow\u2028again")
        assert RunDir(run).control().stopped(stage="critique")
        stopped = summarise_run(run, NOW).slice("stage:critique")
        assert (stopped.state, stopped.reason) == (PAUSED, "slow again")


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
        one refresh; then one appended line, which the next refresh folds
        alone."""
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
        started = time.monotonic()
        watcher = RunMonitor(path)
        watcher.refresh()
        assert len(folded) == len(lines)
        summary = watcher.summary(NOW)
        assert summary.stage("transcription").reused == {"key match": 3000}
        assert summary.stage("extraction").running == 10
        assert summary.stage("critique").pending == 3000
        assert len(render_lines(summary, width=100, height=40)) <= 40
        render_html(summary)
        assert time.monotonic() - started < 20
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
        render_lines(summary, width=120, height=50, color=True)
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

    @pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, "9"])
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

    def test_with_colour_it_uses_ansi_styles(self, summary):
        assert "\x1b[31m" in render_terminal(summary, color=True)

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

    def test_the_page_is_served(self, served):
        run, url = served
        for path in ("", "index.html", "?reload=1"):
            status, body, headers = self.fetch(url + path)
            assert status == 200
            assert body == page_of(run).read_bytes()
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert headers["Cache-Control"] == "no-store"
        status, body, _ = self.fetch(url, method="HEAD")
        assert (status, body) == (200, b"")

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
        out = io.StringIO()
        seen = []

        def between():
            (url,) = set(
                re.findall(r"http://127\.0\.0\.1:\d+/", out.getvalue())
            )
            seen.append((url, self.fetch(url)[0]))

        keys = ScriptedKeys([None, "q"], between=between)
        watch(
            run,
            interval=0,
            serve="127.0.0.1:0",
            out=out,
            keys=keys,
            clock=lambda: NOW,
            size=lambda: (120, 60),
            terminal=True,
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
        _, output = _watch(run, ScriptedKeys(["q"]))
        assert f"':' or Enter: {GRAMMAR}" in output

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
        self.entered = self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def wait(self, timeout):
        if self.between is not None:
            self.between()
        key = self.keys.pop(0)
        if isinstance(key, BaseException):
            raise key
        return key

    def prompt(self, text):
        self.prompts.append(text)
        return self.lines.pop(0) if self.lines else None


def _watch(run, keys, *, terminal=True, out=None, **options):
    out = out or io.StringIO()
    code = watch(
        run,
        interval=0,
        out=out,
        keys=keys,
        clock=lambda: NOW,
        size=lambda: (120, 60),
        terminal=terminal,
        **options,
    )
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

    def test_on_a_terminal_it_redraws_in_place_until_q(self, run):
        keys = ScriptedKeys([None, None, "q"])
        code, output = _watch(run, keys)
        assert code == 0 and keys.entered and keys.exited
        assert output.count(HOME) == 3
        assert output.count(CLEAR_BELOW) == 3
        assert output.endswith(SHOW_CURSOR + "\n")

    def test_lines_stop_short_of_the_last_column(self, run):
        _, output = _watch(run, ScriptedKeys(["q"]))
        lines = [
            re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
            for line in output.split("\n")
        ]
        assert max(len(line) for line in lines) == 119

    def test_a_typed_command_goes_to_the_control_file(self, run):
        keys = ScriptedKeys([":", "q"], lines=["pause stage critique slow"])
        code, output = _watch(run, keys)
        assert code == 0
        assert keys.prompts == ["command> "]
        requested = RunDir(run).control()
        assert requested.stopped(stage="critique") == "stage:critique paused"
        # the next frame shows the request
        assert "pause requested for stage:critique: now paused" in output

    def test_an_abort_typed_in_the_view_is_confirmed(self, run):
        keys = ScriptedKeys(
            [":", ":", "q"], lines=["abort", "n", "abort", "y"]
        )
        _watch(run, keys)
        assert RunDir(run).control().stopped() == "run aborted"
        assert len(keys.prompts) == 4 and "[y/N]" in keys.prompts[1]

    def test_ctrl_c_leaves_the_terminal_as_it_was(self, run):
        keys = ScriptedKeys([KeyboardInterrupt()])
        code, output = _watch(run, keys)
        assert code == 0 and keys.exited
        assert output.endswith(SHOW_CURSOR + "\n")

    def test_the_hint_names_only_keys_that_work(self, run):
        _, interactive = _watch(run, ScriptedKeys(["q"]))
        assert "':' or Enter: pause|resume|abort" in interactive

        class Unread(monitor.NoKeys):
            """A terminal to draw on, but no keys to read."""

            def wait(self, timeout):
                raise KeyboardInterrupt

        _, drawn = _watch(run, Unread())
        assert "Ctrl-C: quit" in drawn
        assert "':' or Enter" not in drawn

    def test_off_a_terminal_it_prints_only_when_the_run_changes(self, run):
        waits = []

        def between():
            waits.append(None)
            if len(waits) == 2:
                append(run, [stage("done", 500, POSTCARD, "critique")])

        keys = ScriptedKeys([None, None, None, "q"], between=between)
        _, output = _watch(run, keys, terminal=False)
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


class TestTerminalKeys:
    """On a pseudo-terminal: no real terminal is needed."""

    def test_keys_are_read_without_enter_and_the_terminal_restored(self):
        pty = pytest.importorskip("pty")
        termios = pytest.importorskip("termios")
        leader, follower = pty.openpty()
        try:
            with open(follower, closefd=False) as stream:
                before = termios.tcgetattr(follower)
                out = io.StringIO()
                with monitor.TerminalKeys(stream, out) as keys:
                    assert keys.wait(0) is None
                    # a key arrives without Enter: the line is not buffered
                    os.write(leader, b"q")
                    assert keys.wait(1) == "q"
                    # a prompt takes a whole line, and returns to single keys
                    os.write(leader, b"pause stage critique\n")
                    assert keys.prompt("command> ") == "pause stage critique"
                    assert "command> " in out.getvalue()
                    os.write(leader, b":")
                    assert keys.wait(1) == ":"
                assert termios.tcgetattr(follower) == before
        finally:
            os.close(leader)
            os.close(follower)


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
