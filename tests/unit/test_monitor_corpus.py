"""Offline tests for the monitor's view of a scratch run's corpus stages
(#561): clustering, then the write side's reset, register, write, places
and site, whose events name no document. Each shows its state in the
latest invocation (pending, held, running, interrupted, done, skipped or
failed), and, from the `progress` events the write side emits, what it
does, its counts, and its rate and time to go; `to go` names what remains
past the barrier. A run directory written before the progress events
shows its corpus stages from their `started` and `done` events alone.

The run is fictional (docs/EXAMPLES.md): two documents of the example
universe, past the barrier. Free text the views must not show carries a
name from the universe."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.test_monitor import (
    CERT,
    DIRECTORY,
    EXTRACTED,
    FAILURE,
    FIRST,
    RUN_ID,
    SECOND,
    T0,
    append,
    at,
    event,
)
from yizkr.pipeline.monitor import (
    RunMonitor,
    Summary,
    render_html,
    render_lines,
    render_rows,
    summarise_run,
)
from yizkr.pipeline.rundir import PROGRESS

CORPUS = ["clustering", "reset", "register", "write", "places", "site"]
DOCUMENTS = {CERT: "media", DIRECTORY: "text"}
# the barrier, a minute in; the corpus stages run after it
BARRIER = 60
# what no view may show: a document's words, put where a name goes
LEAK = "Hutmacherei zum Blauen Hut"


def manifest(
    invocations=((SECOND, 0, None),), until=None, scratch=True
) -> dict:
    run = {
        "format": 1,
        "run_id": RUN_ID,
        "mode": "scratch run",
        "project_stage": "development",
        "documents": [
            {
                "sha1": sha1,
                "file": f"{sha1[:6]}.{kind}",
                "kind": kind,
                "stages": EXTRACTED,
            }
            for sha1, kind in DOCUMENTS.items()
        ],
        "started_at": at(0),
        "ended_at": None,
        "invocations": [
            {
                "invocation": invocation,
                "started_at": at(start),
                "ended_at": None if end is None else at(end),
                "outcome": None if end is None else "failed",
            }
            for invocation, start, end in invocations
        ],
    }
    if scratch:
        run["scratch"] = {
            "until": until,
            "canaries": [],
            "corpus_stages": CORPUS,
            "past_barrier_only": False,
            "complete": False,
        }
    return run


def documents_done(invocation=SECOND) -> list:
    """Every document's stages, done, and the barrier."""
    entries = [event(0, "run-start", invocation, run_id=RUN_ID)]
    for sha1, kind in DOCUMENTS.items():
        for offset, stage in enumerate(EXTRACTED):
            for name in ("started", "done"):
                entries.append(
                    event(
                        1 + offset,
                        name,
                        invocation,
                        subject=sha1,
                        kind=kind,
                        stage=stage,
                    )
                )
    entries.append(
        event(BARRIER, "barrier", invocation, documents=2, failed=0)
    )
    return entries


def corpus(name, seconds, stage, invocation=SECOND, **fields):
    return event(BARRIER + seconds, name, invocation, stage=stage, **fields)


def progress(seconds, stage, phase, invocation=SECOND, **fields):
    return event(
        BARRIER + seconds,
        PROGRESS,
        invocation,
        stage=stage,
        phase=phase,
        **fields,
    )


def written(seconds, done, of=4, **fields):
    return progress(
        seconds,
        "write",
        "writing",
        unit="documents",
        done=done,
        of=of,
        failed=0,
        **fields,
    )


def make(tmp_path: Path, entries, **options) -> Path:
    path = tmp_path / "run"
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_text(json.dumps(manifest(**options)))
    append(path, entries)
    return path


def ran(stage, start, end, **fields) -> list:
    return [
        corpus("started", start, stage),
        corpus("done", end, stage, **fields),
    ]


def through_register() -> list:
    """Clustering, reset and register, done, with the write side's
    progress events."""
    return [
        *documents_done(),
        *ran("clustering", 1, 30),
        corpus("started", 31, "reset"),
        progress(
            31,
            "reset",
            "resetting",
            unit="steps",
            done=0,
            of=9,
            step="check the kept-state marker",
        ),
        corpus("done", 150, "reset"),
        corpus("started", 151, "register"),
        progress(
            151,
            "register",
            "registering",
            unit="documents",
            done=0,
            of=4,
            failed=0,
            refused=1,
        ),
        progress(
            211,
            "register",
            "registering",
            unit="documents",
            done=4,
            of=4,
            failed=1,
            refused=1,
        ),
        corpus(
            "done",
            211,
            "register",
            attention=["1 batches refused whole", "1 documents failed"],
        ),
    ]


def mid_write() -> list:
    """The write a quarter through four documents: one done in the minute
    since the stage's first progress event."""
    return [
        *through_register(),
        corpus("started", 212, "write"),
        written(212, 0, written=0, referenced=0, skipped=0, claims_refused=0),
        written(272, 1, written=12, referenced=3, skipped=1, claims_refused=2),
    ]


def at_(seconds: float):
    return T0 + timedelta(seconds=BARRIER + seconds)


def states(summary: Summary) -> dict[str, str]:
    return {stage.name: stage.state for stage in summary.corpus}


def stage_of(summary: Summary, name: str):
    (found,) = [stage for stage in summary.corpus if stage.name == name]
    return found


def lines(summary: Summary) -> list[str]:
    return render_lines(summary, width=400)


def line_of(summary: Summary, name: str) -> str:
    (found,) = [
        line for line in lines(summary) if line.startswith(f"  {name} ")
    ]
    return found


def to_go(summary: Summary) -> str:
    (found,) = [line for line in lines(summary) if "to go    " in line]
    return found.split("to go    ", 1)[1]


class TestStates:
    def test_before_the_barrier_every_corpus_stage_is_pending(self, tmp_path):
        run = make(tmp_path, documents_done()[:-1])
        summary = summarise_run(run, at_(0))
        assert states(summary) == dict.fromkeys(CORPUS, "pending")
        assert "corpus stages" in lines(summary)

    def test_a_run_with_no_corpus_stages_shows_none(self, tmp_path):
        run = make(tmp_path, documents_done()[:-1], scratch=False)
        summary = summarise_run(run, at_(0))
        assert summary.corpus == []
        assert "corpus stages" not in lines(summary)
        assert "Corpus stages" not in render_html(summary)

    def test_mid_write(self, tmp_path):
        run = make(tmp_path, mid_write())
        summary = summarise_run(run, at_(280))
        assert states(summary) == {
            "clustering": "done",
            "reset": "done",
            "register": "done",
            "write": "running",
            "places": "pending",
            "site": "pending",
        }
        write = stage_of(summary, "write")
        assert (write.done, write.of, write.unit) == (1, 4, "documents")
        assert write.started == at_(212)
        # one document in the minute between its first and latest events,
        # so three more take three minutes
        assert write.rate == pytest.approx(1.0)
        assert write.to_go == pytest.approx(180.0)
        assert dict(write.counts) == {
            "failed": 0,
            "written": 12,
            "referenced": 3,
            "skipped": 1,
            "claims refused": 2,
        }
        assert line_of(summary, "write") == (
            "  write       running for 1m 08s; writing: 1 of 4 documents; "
            "12 written, 3 referenced, 1 skipped, 2 claims refused; "
            "1 documents a minute, ~3m 00s to go"
        )

    def test_a_done_stage_shows_how_long_it_took_and_its_counts(
        self, tmp_path
    ):
        summary = summarise_run(make(tmp_path, mid_write()), at_(280))
        register = stage_of(summary, "register")
        assert register.seconds == 60
        assert line_of(summary, "register") == (
            "  register    done in 1m 00s; registering: 4 of 4 documents; "
            "1 failed, 1 refused whole; attention: 1 batches refused whole, "
            "1 documents failed"
        )
        # a reset that is done has gone through its steps
        assert line_of(summary, "reset") == "  reset       done in 1m 59s"

    def test_a_running_reset_shows_its_step(self, tmp_path):
        entries = through_register()[:-5] + [
            progress(
                40,
                "reset",
                "resetting",
                unit="steps",
                done=5,
                of=9,
                step="start the stack",
            )
        ]
        summary = summarise_run(make(tmp_path, entries), at_(100))
        assert line_of(summary, "reset") == (
            "  reset       running for 1m 09s; resetting: step 6 of 9, "
            "start the stack"
        )
        # a reset's steps differ too much in length to be rated
        assert stage_of(summary, "reset").rate is None

    def test_a_stage_with_one_progress_event_has_no_rate_yet(self, tmp_path):
        entries = mid_write()[:-1]
        summary = summarise_run(make(tmp_path, entries), at_(215))
        write = stage_of(summary, "write")
        assert (write.rate, write.to_go) == (None, None)
        assert line_of(summary, "write").endswith("rate unknown yet")

    def test_waiting_for_the_query_service(self, tmp_path):
        entries = [
            *mid_write(),
            written(332, 4, written=40, referenced=3, skipped=1),
            corpus("done", 333, "write"),
            corpus("started", 334, "places"),
            progress(335, "places", "waiting"),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(395))
        places = stage_of(summary, "places")
        assert (places.state, places.phase) == ("running", "waiting")
        assert places.phase_since == at_(335)
        assert line_of(summary, "places") == (
            "  places      running for 1m 01s; waiting for the query service "
            "since 10:06:35 UTC, 1m 00s"
        )
        assert to_go(summary) == (
            "corpus stages: places (waiting for the query service), site"
        )

    def test_the_place_stage_asks_in_batches_and_counts_its_calls(
        self, tmp_path
    ):
        def call(seconds, name):
            return event(
                BARRIER + seconds, name, role="places", key=f"{seconds:064x}"
            )

        entries = [
            *mid_write(),
            corpus("done", 333, "write"),
            corpus("started", 334, "places"),
            progress(335, "places", "waiting"),
            progress(340, "places", "asking", unit="batches", done=0, of=3),
            call(350, "cache-hit"),
            progress(350, "places", "asking", unit="batches", done=1, of=3),
            call(400, "cache-miss"),
            progress(400, "places", "asking", unit="batches", done=2, of=3),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(410))
        places = stage_of(summary, "places")
        # the new phase rates afresh, from its own first event
        assert places.rate == pytest.approx(2.0)
        assert places.to_go == pytest.approx(30.0)
        assert (places.engine, places.cached) == (1, 1)
        assert line_of(summary, "places") == (
            "  places      running for 1m 16s; asking: 2 of 3 batches; "
            "2 batches a minute, ~30s to go; 1 engine calls, 1 from the cache"
        )
        assert to_go(summary) == (
            "corpus stages: places (2 of 3 batches, ~30s), site"
        )

    def test_the_site_waits_then_builds(self, tmp_path):
        entries = [
            *mid_write(),
            corpus("done", 333, "write"),
            *ran("places", 334, 335),
            corpus("started", 336, "site"),
            progress(336, "site", "waiting"),
            progress(400, "site", "building"),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(420))
        assert line_of(summary, "site") == (
            "  site        running for 1m 24s; building"
        )
        assert to_go(summary) == "corpus stages: site (running)"

    def test_a_skipped_stage(self, tmp_path):
        entries = [
            *through_register()[:-5],
            progress(31, "reset", "skipped"),
            corpus("done", 32, "reset"),
            corpus("started", 33, "register"),
            progress(33, "register", "skipped"),
            corpus("done", 33, "register"),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(40))
        assert states(summary)["reset"] == "skipped"
        assert states(summary)["register"] == "skipped"
        assert line_of(summary, "register") == (
            "  register    skipped in 0s (the instance holds what the key "
            "makes)"
        )

    def test_every_stage_done_leaves_nothing_to_go(self, tmp_path):
        entries = [
            *mid_write(),
            corpus("done", 333, "write"),
            *ran("places", 334, 335),
            *ran("site", 336, 337),
            event(BARRIER + 338, "run-end", outcome="finished"),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(400))
        assert set(states(summary).values()) == {"done"}
        assert to_go(summary) == "nothing"

    def test_a_failed_stage_shows_its_exception_only(self, tmp_path):
        entries = [
            *mid_write(),
            corpus("failed", 300, "write", reason=FAILURE),
            event(BARRIER + 301, "run-end", outcome="failed"),
        ]
        run = make(
            tmp_path, entries, invocations=((SECOND, 0, BARRIER + 301),)
        )
        summary = summarise_run(run, at_(400))
        write = stage_of(summary, "write")
        assert (write.state, write.failure, write.seconds) == (
            "failed",
            "RuntimeError",
            88,
        )
        assert line_of(summary, "write").startswith(
            "  write       failed in 1m 28s: RuntimeError; writing: 1 of 4"
        )
        # the next invocation runs it again, and the stages after it
        assert to_go(summary) == "corpus stages: write, places, site"
        for view in ("\n".join(lines(summary)), render_html(summary)):
            assert "Hutmacherei" not in view

    def test_a_held_stage_shows_the_request_that_holds_it(self, tmp_path):
        entries = [
            *through_register(),
            corpus("held", 212, "write", reason="stage:write paused"),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(300))
        assert states(summary)["write"] == "held"
        assert line_of(summary, "write") == (
            "  write       held: stage:write paused"
        )
        assert to_go(summary) == "corpus stages: write, places, site"

    def test_a_hold_lapses_with_its_invocation(self, tmp_path):
        entries = [
            *through_register(),
            corpus("held", 212, "write", reason="stage:write aborted"),
            event(BARRIER + 213, "run-end", outcome="stopped"),
        ]
        run = make(
            tmp_path, entries, invocations=((SECOND, 0, BARRIER + 213),)
        )
        summary = summarise_run(run, at_(300))
        assert states(summary)["write"] == "pending"

    def test_a_stage_its_invocation_left_started_is_interrupted(
        self, tmp_path
    ):
        entries = [
            *mid_write(),
            event(BARRIER + 280, "run-interrupted", outcome="interrupted"),
        ]
        run = make(
            tmp_path, entries, invocations=((SECOND, 0, BARRIER + 280),)
        )
        summary = summarise_run(run, at_(400))
        write = stage_of(summary, "write")
        assert write.state == "interrupted"
        # nothing runs it: no rate, and no time to go
        assert (write.rate, write.to_go) == (None, None)
        assert line_of(summary, "write").startswith(
            "  write       interrupted (the next invocation runs it again); "
            "writing: 1 of 4 documents"
        )
        assert to_go(summary) == "corpus stages: write, places, site"

    def test_a_killed_invocation_s_stage_is_interrupted_by_the_next_start(
        self, tmp_path
    ):
        entries = [
            *documents_done(FIRST),
            corpus("started", 1, "clustering", invocation=FIRST),
            event(BARRIER + 400, "run-start", run_id=RUN_ID),
        ]
        run = make(
            tmp_path,
            entries,
            invocations=((FIRST, 0, None), (SECOND, BARRIER + 400, None)),
        )
        summary = summarise_run(run, at_(410))
        # the new invocation has not reached it: it runs it afresh
        assert states(summary) == dict.fromkeys(CORPUS, "pending")

    def test_an_earlier_invocation_s_done_stage_is_pending_again(
        self, tmp_path
    ):
        entries = [
            *documents_done(FIRST),
            *(
                event(BARRIER + seconds, name, FIRST, stage=stage)
                for stage, start in (("clustering", 1), ("reset", 3))
                for seconds, name in ((start, "started"), (start + 1, "done"))
            ),
            event(BARRIER + 10, "run-end", FIRST, outcome="failed"),
            event(BARRIER + 20, "run-start", run_id=RUN_ID),
            corpus("started", 21, "clustering"),
        ]
        run = make(
            tmp_path,
            entries,
            invocations=(
                (FIRST, 0, BARRIER + 10),
                (SECOND, BARRIER + 20, None),
            ),
        )
        summary = summarise_run(run, at_(30))
        assert states(summary)["clustering"] == "running"
        assert states(summary)["reset"] == "pending"

    def test_until_leaves_the_later_stages_out(self, tmp_path):
        run = make(tmp_path, mid_write(), until="write")
        summary = summarise_run(run, at_(280))
        assert [stage.name for stage in summary.corpus] == CORPUS[:4]
        assert to_go(summary).startswith("corpus stages: write (1 of 4")
        assert to_go(summary).endswith(")")

    def test_until_before_the_barrier_leaves_every_corpus_stage_out(
        self, tmp_path
    ):
        run = make(tmp_path, documents_done()[:-1], until="critique")
        assert summarise_run(run, at_(0)).corpus == []


class TestARunBeforeProgressEvents:
    """A run directory written before the write side emitted progress
    events shows its corpus stages from `started` and `done` alone."""

    def entries(self) -> list:
        return [
            *documents_done(),
            *ran("clustering", 1, 30),
            *ran("reset", 31, 150),
            corpus("started", 151, "register"),
        ]

    def test_from_started_and_done_alone(self, tmp_path):
        summary = summarise_run(make(tmp_path, self.entries()), at_(271))
        assert states(summary) == {
            "clustering": "done",
            "reset": "done",
            "register": "running",
            "write": "pending",
            "places": "pending",
            "site": "pending",
        }
        assert line_of(summary, "register") == (
            "  register    running for 2m 00s"
        )
        assert to_go(summary) == (
            "corpus stages: register (running), write, places, site"
        )
        page = render_html(summary)
        assert "<h2>Corpus stages</h2>" in page
        assert "running for 2m 00s" in page

    def test_a_manifest_without_the_scratch_record_shows_what_ran(
        self, tmp_path
    ):
        run = make(tmp_path, self.entries(), scratch=False)
        summary = summarise_run(run, at_(271))
        assert states(summary) == {
            "clustering": "done",
            "reset": "done",
            "register": "running",
        }


class TestNeverQuoted:
    def test_names_from_an_event_that_are_no_names_show_as_unknown(
        self, tmp_path
    ):
        entries = [
            *through_register(),
            corpus("started", 212, "write"),
            progress(
                213,
                "write",
                f"writing {LEAK}",
                unit=LEAK,
                done=1,
                of=4,
                written=f"7 ({LEAK})",
                referenced=True,
                skipped=-1,
            ),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(220))
        write = stage_of(summary, "write")
        assert (write.phase, write.unit) == ("?", "?")
        # a count that is not a whole number at or above zero is none
        assert write.counts == ()
        for view in ("\n".join(lines(summary)), render_html(summary)):
            assert "Hutmacherei" not in view

    def test_a_reset_step_that_is_no_name_is_left_out(self, tmp_path):
        entries = [
            *through_register()[:-5],
            progress(
                40,
                "reset",
                "resetting",
                unit="steps",
                done=4,
                of=9,
                step="set LocalSettings.php aside",
            ),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(50))
        assert line_of(summary, "reset").endswith("resetting: step 5 of 9")

    def test_a_progress_event_outside_a_running_stage_is_ignored(
        self, tmp_path
    ):
        entries = [
            *through_register(),
            # before the write starts, and of another invocation
            written(212, 3),
            corpus("started", 213, "write"),
            written(214, 2, invocation=FIRST),
        ]
        summary = summarise_run(make(tmp_path, entries), at_(220))
        write = stage_of(summary, "write")
        assert (write.state, write.done, write.phase) == (
            "running",
            None,
            None,
        )


class TestTheViews:
    def test_the_status_page_shows_each_corpus_stage(self, tmp_path):
        summary = summarise_run(make(tmp_path, mid_write()), at_(280))
        page = render_html(summary)
        assert "<h2>Corpus stages</h2>" in page
        assert "writing: 1 of 4 documents" in page
        assert "~3m 00s to go" in page
        assert "corpus stages: write (1 of 4 documents, ~3m 00s)" in page

    def test_the_short_view_keeps_the_corpus_stages(self, tmp_path):
        summary = summarise_run(make(tmp_path, mid_write()), at_(280))
        full = len(render_rows(summary))
        short = [text for text, _ in render_rows(summary, height=full - 1)]
        assert any(text.startswith("  write ") for text in short)

    def test_while_the_events_cannot_be_read_the_states_are_unknown(
        self, tmp_path
    ):
        summary = Summary(
            path=tmp_path,
            now=at_(0),
            found=True,
            events_unreadable=True,
            corpus_names=list(CORPUS),
        )
        assert "corpus     unknown: events.jsonl cannot be read" in lines(
            summary
        )
        assert "<h2>Corpus stages</h2>" in render_html(summary)

    def test_the_monitor_follows_a_stage_as_its_events_arrive(self, tmp_path):
        entries = mid_write()
        run = make(tmp_path, entries[:-1])
        follower = RunMonitor(run)
        follower.refresh()
        assert stage_of(follower.summary(at_(215)), "write").done == 0
        append(run, entries[-1:])
        assert follower.refresh()
        assert stage_of(follower.summary(at_(280)), "write").done == 1


def test_past_the_barrier_to_go_names_what_remains(tmp_path):
    """The documents' stages are all done: `to go` names the corpus
    stages left, not nothing."""
    summary = summarise_run(make(tmp_path, through_register()), at_(212))
    assert to_go(summary) == "corpus stages: write, places, site"


def test_to_go_names_the_corpus_stages_after_the_documents_stages(tmp_path):
    # the last document's critique started, not done; no barrier yet
    entries = documents_done()[:-2]
    summary = summarise_run(make(tmp_path, entries), at_(0))
    assert to_go(summary).startswith("1 stages")
    assert to_go(summary).endswith(
        "; then corpus stages: clustering, reset, register, write, places, "
        "site"
    )
