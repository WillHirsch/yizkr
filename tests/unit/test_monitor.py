"""Offline tests for the live run monitor (#195): the summary it builds from
a run directory, its terminal view and status page, and the control
requests it writes.

Most tests read one recorded event stream of a fictional run over
documents of the example universe (docs/EXAMPLES.md): two invocations,
the first killed mid-stage, the second running, with the LLM cache's
events, alerts and control requests. `record` writes it into a run
directory, whole or up to a point, as a driver in another process would."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures import example_universe as u
from yizkr.pipeline import monitor
from yizkr.pipeline.monitor import (
    ABORTED,
    ACTIVE,
    CLEAR_BELOW,
    HOME,
    LATEST_ALERTS,
    PAUSED,
    SHOW_CURSOR,
    STATUS_PAGE,
    NotARunDirectory,
    RunMonitor,
    attention_label,
    failure_label,
    handle_command,
    parse_command,
    prefix,
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

RUN_ID = "20260927T100000Z-4f2a9c"
FIRST = "a1b2c3d4e5f6"  # killed mid-stage: no end recorded
SECOND = "0f1e2d3c4b5a"  # running
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=10)

# what details after a label say, which no view may show: a failure's
# message and an attention reason's details can quote a document
FAILURE = (
    "RuntimeError: 2 chunks failed after retries (c1, c2; c1: "
    "Hutmacherei zum Blauen Hut, gegr. 1863)"
)
FALLBACK = (
    "entity manifest fell back to line-range segmentation: "
    "input_value='Hildegard Laudler'"
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


def alert(seconds, stage_, check, subject, value, status):
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
    stage("started", 301, CERT, "transcription"),
    cache(
        "cache-miss",
        302,
        CERT,
        "transcribe",
        call="complete",
        reason="absent",
    ),
    # its stored transcription fails the check now: kept aside, made again
    event(
        309,
        "set-aside",
        subject=DIRECTORY,
        kind="text",
        stage="transcription",
        reason="the transcription check fails it now",
        kept="transcription.superseded-1.json",
    ),
    stage("started", 310, DIRECTORY, "transcription"),
    stage("done", 311, DIRECTORY, "transcription"),
    stage("reused", 312, CARD, "transcription"),
    control(330, "pause", "stage:extraction", reason="checking the prompt"),
    control(345, "pause", "stage:extraction", reason="still checking"),
    stage("held", 350, CARD, "extraction", reason="stage:extraction paused"),
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
        reason="chunk: no terminal sentinel",
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
    *alert(486, "extraction", "claim rate", "run", 0.5, "new"),
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


@pytest.fixture
def run(tmp_path) -> Path:
    return record(tmp_path / "run")


@pytest.fixture
def summary(run):
    return summarise_run(run, NOW)


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
        """The monitor splits rundir's states (done into made and reused;
        running into in flight and interrupted) but never disagrees with
        them."""
        expected = summarise(run_state(RunDir(run)))
        for counts in summary.stages:
            states = expected[counts.name]
            assert counts.done + sum(counts.reused.values()) == states.get(
                DONE, 0
            )
            assert counts.attention == states.get(NEEDS_ATTENTION, 0)
            assert counts.failed == states.get(FAILED, 0)
            assert counts.running + counts.interrupted == states.get(
                RUNNING, 0
            )
            assert counts.pending - counts.interrupted == states.get(
                PENDING, 0
            )

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
        assert (calls.replayed, calls.rejected) == (1, 1)
        assert calls.roles["extract"] == {"engine": 4, "cached": 4}
        assert calls.to_engine == 7

    def test_remaining_calls_are_estimated_per_stage_and_kind(self, summary):
        """Extraction of the two text documents to go, at the one text
        extraction's 2 engine calls; the postcard's critique at the media
        critiques' 1; the text transcription to go at 0. No text document
        has been critiqued yet, so that part has no rate."""
        assert summary.remaining_calls == 5
        assert summary.unrated == ["critique (text)"]

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
        assert summary.last_event_at == T0 + timedelta(seconds=486)

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


class TestCallEvents:
    """Call events are optional: shown when the run has them."""

    CALLS = [
        (
            "events.jsonl",
            {
                "at": at(490),
                "event": "call",
                "invocation": SECOND,
                "role": "critique",
                "subject": POSTCARD,
                "kind": "media",
                "stage": "critique",
                "duration_s": 12.5,
                "outcome": "ok",
            },
        ),
        (
            "events.jsonl",
            {
                "at": at(495),
                "event": "call",
                "invocation": SECOND,
                "role": "critique",
                "subject": POSTCARD,
                "stage": "critique",
                "duration_s": 7.5,
                "outcome": "rate-limited",
                "wait_s": 90,
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
        assert calls.outcomes == {"ok": 1, "rate-limited": 1}
        assert (calls.waits, calls.waited) == (1, 90.0)
        # the cache's events still say what reached the engine
        assert calls.to_engine == 7
        view = render_terminal(summary, width=200)
        assert (
            "logged   2 calls | mean 10.0s | 1 rate-limited | 1 rate-limit "
            "waits, 1m 30s in all" in view
        )

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


class TestAlerts:
    def test_the_latest_alerts_come_first_tagged_new_or_carried(self, summary):
        assert [
            (alert.status, alert.check, alert.subject, alert.kind)
            for alert in summary.alerts
        ] == [
            ("new", "claim rate", "-", None),
            ("carried", "fallback manifest", POSTCARD[:10], "media"),
            ("new", "implausibly short", CERT[:10], "media"),
        ]
        assert summary.alert_counts == {"new": 2, "carried": 1}
        assert summary.alerts[-1].value == "0.12"

    def test_the_views_tag_each_alert(self, summary):
        view = render_terminal(summary, width=200)
        lines = [line for line in view.splitlines() if "fallback" in line]
        assert "carried" in lines[-1] and POSTCARD[:10] in lines[-1]
        page = render_html(summary)
        assert '<span class="muted">carried</span>' in page
        assert '<span class="warn">new</span>' in page

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

    @pytest.mark.parametrize("how", ["truncated", "replaced by a longer one"])
    def test_a_replaced_log_starts_the_monitor_over(self, run, how):
        watcher = RunMonitor(run)
        watcher.refresh()
        assert watcher.summary(NOW).stage("extraction").failed == 1
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        events = run / "events.jsonl"
        if how == "truncated":
            # the same file, shorter than what was read of it
            events.write_text(line + "\n")
        else:
            # another file in its place, longer than what was read
            size = events.stat().st_size
            fresh = run / "events.fresh"
            fresh.write_text((line + "\n") * (size // len(line) + 2))
            os.replace(fresh, events)
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.stage("extraction").failed == 0
        assert summary.stage("transcription").running == 1
        assert summary.calls.engine == 0

    def test_a_summary_stays_as_it_was_taken(self, run):
        watcher = RunMonitor(run)
        watcher.refresh()
        before = watcher.summary(NOW)
        append(run, [cache("cache-miss", 500, POSTCARD, "critique")])
        watcher.refresh()
        assert watcher.summary(NOW).calls.engine == 8
        assert before.calls.engine == 7
        assert before.calls.roles.get("critique") == {"engine": 1}

    def test_another_run_in_the_directory_starts_the_monitor_over(self, run):
        """Another run's log written over this one's in place, and longer:
        the same file, so nothing in it tells it from a log that grew, but
        run.json names another run."""
        watcher = RunMonitor(run)
        watcher.refresh()
        assert watcher.summary(NOW).stage("extraction").failed == 1
        events = run / "events.jsonl"
        size = events.stat().st_size
        line = json.dumps(stage("started", 301, CERT, "transcription")[1])
        with open(events, "r+") as handle:
            handle.write((line + "\n") * (size // len(line) + 2))
        replacement = manifest()
        replacement["run_id"] = "20260928T090000Z-0a0b0c"
        (run / "run.json").write_text(json.dumps(replacement))
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.run_id == "20260928T090000Z-0a0b0c"
        assert summary.stage("extraction").failed == 0
        assert summary.stage("transcription").running == 1
        assert summary.skipped_lines == 0

    def test_a_long_run(self, tmp_path):
        """Thousands of documents, tens of thousands of events: one read,
        then a refresh that reads a single appended line."""
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
        started = time.monotonic()
        watcher = RunMonitor(path)
        watcher.refresh()
        summary = watcher.summary(NOW)
        assert summary.stage("transcription").reused == {"key match": 3000}
        assert summary.stage("extraction").running == 10
        assert summary.stage("critique").pending == 3000
        view = render_lines(summary, width=100, height=40)
        assert len(view) <= 40
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
        assert watcher.summary(NOW).stage("extraction").running == 9


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
        assert write_status_page(path, summary) == path / STATUS_PAGE

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
        assert prefix(CERT[:7]) == CERT[:7]
        assert prefix("postcard-1938.jpg") == "-"
        assert prefix(None) == "-"

    def test_a_failure_shows_its_exception_only(self):
        assert failure_label(FAILURE) == "RuntimeError"
        assert failure_label("OSError: [Errno 2] no such file") == "OSError"
        assert (
            failure_label("yizkr.pipeline.llm.PassPaused: the CLI changed")
            == "yizkr.pipeline.llm.PassPaused"
        )
        # a reason that does not start with an exception's name shows none
        # of its words: the first could be a name
        assert failure_label("Löbig: no such thing") == "error"
        assert failure_label("Klara: gone") == "error"
        assert failure_label("no colon at all") == "error"
        assert failure_label(None) == "error"

    def test_an_attention_reason_shows_its_label_only(self):
        assert attention_label(FALLBACK) == (
            "entity manifest fell back to line-range segmentation"
        )
        assert attention_label("no claims extracted") == "no claims extracted"


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
        assert "last event 1m 54s ago" in view
        assert "reused     transcription: 1 adopted, 1 view current" in view
        assert "failed     extraction: 1 RuntimeError" in view
        assert f"{POSTCARD[:10]}  media        critique" in view
        assert "to go    ~5 engine calls at the rates so far" in view
        assert "stage:extraction       PAUSED" in view
        assert "kind:gedcom            ABORTED" in view

    def test_it_names_no_document_and_quotes_none(self, summary):
        view = render_terminal(summary, width=300)
        for leak in _leaks():
            assert leak not in view


# -- the status page ----------------------------------------------------------


def _leaks() -> list[str]:
    """What no view may show: file names, titles and text of the documents,
    whole SHA-1s, and the details of failures and attention reasons."""
    leaks = [name for name, _ in FILES.values()] + ["postcard-1938 copy"]
    for key in (
        "marriage-cert-1905",
        "postcard-1938",
        "directory-1912",
        "registration-card-1945",
        "interview-2002",
    ):
        leaks += [SOURCES[key].title, SOURCES[key].text[:30]]
    leaks += list(FILES)  # whole SHA-1s
    leaks += ["Hildegard Laudler", "Hutmacherei", "chunks failed"]
    return leaks


class TestStatusPage:
    def test_it_is_written_whole_into_the_run_directory(self, run, summary):
        page = write_status_page(run, summary)
        assert page == run / STATUS_PAGE
        text = page.read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>")
        assert text.rstrip().endswith("</html>")
        # nothing is left beside it from the atomic write
        assert sorted(p.name for p in run.iterdir()) == [
            "alerts.jsonl",
            "control.jsonl",
            "events.jsonl",
            "run.json",
            STATUS_PAGE,
        ]

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

    def test_text_is_escaped(self, run):
        append(run, [control(600, "pause", reason="<b>&")])
        page = render_html(summarise_run(run, NOW))
        assert "&lt;b&gt;&amp;" in page and "<b>&" not in page


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

    def test_an_empty_name_is_refused_not_taken_for_the_run(self, run):
        before = (run / "control.jsonl").read_text()
        with pytest.raises(ValueError, match="empty stage"):
            request(run, "abort", stage="")
        with pytest.raises(ValueError, match="empty kind"):
            request(run, "abort", kind=" ")
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
    def test_unclear_commands_are_refused(self, line):
        with pytest.raises(monitor.CommandError):
            parse_command(line)

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

    def test_an_unknown_slice_is_noted(self, run, summary):
        result = handle_command(run, "pause stage extracton", summary=summary)
        assert "no document the run knows has this slice" in result.message
        known = handle_command(run, "pause kind media", summary=summary)
        assert "no document" not in known.message

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
        assert (run / STATUS_PAGE).is_file()

    def test_once_without_the_page(self, run):
        watch(run, once=True, page=False, out=io.StringIO())
        assert not (run / STATUS_PAGE).exists()

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

    def test_a_page_that_cannot_be_written_is_reported(self, run, monkeypatch):
        def refuse(*args, **kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(monitor, "atomic_write_text", refuse)
        out = io.StringIO()
        assert watch(run, once=True, out=out) == 0
        assert "status page not written: read-only" in out.getvalue()


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
    assert monitor.main(["watch", str(run), "--once", "--no-page"]) == 0
    assert "kind:media" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        monitor.main(["abort", str(run.parent / "typo")])
