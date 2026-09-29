"""Offline tests for the run directory: its manifest, append-only logs,
control requests, and the run state and cache counts derived from its
events (#87, #225)."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tests.unit.stage_counts import disagreement
from yizkr.pipeline import rundir
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    INTERRUPTED,
    NEEDS_ATTENTION,
    PENDING,
    RUN_END,
    RUN_INTERRUPTED,
    RUNNING,
    RunDir,
    cache_counts,
    fold_control,
    run_state,
    summarise,
)

DOC_A = hashlib.sha1(b"a letter").hexdigest()
DOC_B = hashlib.sha1(b"a census page").hexdigest()


class TestRunDir:
    def test_nothing_is_written_until_something_happens(self, tmp_path):
        run = RunDir(tmp_path / "run")
        assert run.events() == [] and run.manifest() == {}
        assert not (tmp_path / "run").exists()

    def test_a_torn_last_line_is_skipped(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("done", stage="transcription")
        with open(tmp_path / "events.jsonl", "a") as handle:
            handle.write('{"event": "sta')  # a writer killed mid-line
        assert [event["event"] for event in run.events()] == ["done"]

    def test_fields_that_are_none_are_left_out(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("done", stage="extraction", attention=None)
        (event,) = run.events()
        assert "attention" not in event
        assert event["invocation"] == run.invocation

    def test_the_manifest_is_merged_and_the_run_id_kept(self, tmp_path):
        run = RunDir(tmp_path)
        first = run.start(cache="var/llm-cache")
        second = RunDir(tmp_path).start(documents=[])
        assert second["run_id"] == first["run_id"]
        assert second["cache"] == "var/llm-cache"

    def test_alerts_are_logged_and_announced(self, tmp_path):
        run = RunDir(tmp_path)
        run.alert(
            stage="transcription",
            check="tool-failure text",
            subject=DOC_A[:10],
            value=1,
            carried=True,
        )
        (alert,) = run.alerts()
        assert alert["status"] == "carried"
        assert run.events()[-1]["event"] == "alert"


# the first bytes of a record whose writer died mid-write
TORN = b'{"event": "sta'

# a line nested past Python's recursion limit: `json.loads` raises
# RecursionError decoding it, never produced by a writer here
NESTED = b"[" * 100_000


def tear(log, fragment=TORN):
    """End `log` in a torn line, as a writer that died mid-write leaves it."""
    with open(log, "ab") as handle:
        handle.write(fragment)


def nest(log):
    """Append a complete line nested too deep for `json.loads` to decode,
    between the record before it and whatever is appended after."""
    with open(log, "ab") as handle:
        handle.write(NESTED + b"\n")


def names(path):
    """The names of the events a reader finds in the run directory."""
    return [event["event"] for event in RunDir(path).events()]


class _OsWrites:
    """The os module as rundir sees it, with every write recorded and cut
    to at most `most` bytes, and `before` called ahead of the first."""

    def __init__(self, *, most=None, before=None):
        self.writes = []
        self.most = most
        self.before = before

    def __getattr__(self, name):
        return getattr(os, name)

    def write(self, descriptor, data):
        if self.before and not self.writes:
            self.writes.append(None)
            self.before()
        data = bytes(data)[: self.most]
        self.writes.append(data)
        return os.write(descriptor, data)


class TestAppendAfterATornLine:
    """An append after a torn last line starts a new line: the fragment is
    skipped and the new record kept (#271)."""

    def test_an_event_after_a_torn_line_is_kept(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("done", stage="transcription")
        tear(tmp_path / "events.jsonl")
        run.emit("started", stage="extraction")
        assert names(tmp_path) == ["done", "started"]
        lines = (tmp_path / "events.jsonl").read_bytes().split(b"\n")
        assert lines[1] == TORN

    def test_the_next_start_marker_clears_the_requests_before_it(
        self, tmp_path
    ):
        RunDir(tmp_path).request("pause", reason="the Flörburg postcard")
        tear(tmp_path / "control.jsonl", b'{"action": "abo')
        run = RunDir(tmp_path)
        run.start()
        assert run.control().scopes == {}
        assert run.events()[-1]["event"] == "run-start"

    @pytest.mark.parametrize(
        ("action", "state"), [("pause", "paused"), ("abort", "aborted")]
    )
    def test_a_request_after_a_torn_line_stops_the_run(
        self, tmp_path, action, state
    ):
        tear(tmp_path / "control.jsonl", b'{"action": "resu')
        RunDir(tmp_path).request(action, reason="Klara Löbig's letters")
        assert RunDir(tmp_path).control().scopes == {"run": state}

    def test_an_alert_after_a_torn_line_is_kept(self, tmp_path):
        tear(tmp_path / "alerts.jsonl", b'{"stage": "transcri')
        RunDir(tmp_path).alert(
            stage="transcription",
            check="tool-failure text",
            subject=DOC_A[:10],
            value=1,
            carried=False,
        )
        (alert,) = RunDir(tmp_path).alerts()
        assert alert["status"] == "new"

    def test_a_line_torn_inside_a_character_is_skipped(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("done", note="Klara Löbig")
        whole = (tmp_path / "events.jsonl").read_bytes()
        tear(tmp_path / "events.jsonl", whole[: whole.index(b"\xc3\xb6") + 1])
        run.emit("started", stage="extraction")
        assert names(tmp_path) == ["done", "started"]

    @pytest.mark.parametrize("ending", [b"", b"\r"], ids=["bare", "cr"])
    def test_a_last_record_without_its_newline_is_kept(self, tmp_path, ending):
        """A last line that is a whole record without its newline, or with
        only the carriage return of a CRLF: only a newline ends a line,
        so the record is ended and read, and so is the next."""
        (tmp_path / "events.jsonl").write_bytes(b'{"event": "done"}' + ending)
        RunDir(tmp_path).emit("started", stage="extraction")
        assert names(tmp_path) == ["done", "started"]

    @pytest.mark.parametrize(
        "fragment",
        [TORN, b"{", b"\xc3"],
        ids=["fragment", "brace", "lead-byte"],
    )
    def test_a_log_holding_only_a_fragment(self, tmp_path, fragment):
        """A log of one byte is checked like any other."""
        tear(tmp_path / "events.jsonl", fragment)
        RunDir(tmp_path).emit("run-start")
        data = (tmp_path / "events.jsonl").read_bytes()
        assert data.startswith(fragment + b"\n{")
        assert names(tmp_path) == ["run-start"]

    @pytest.mark.parametrize("exists", [False, True], ids=["missing", "empty"])
    def test_the_first_record_starts_the_log(self, tmp_path, exists):
        log = tmp_path / "run" / "events.jsonl"
        if exists:
            log.parent.mkdir()
            log.touch()
        RunDir(tmp_path / "run").emit("run-start")
        data = log.read_bytes()
        assert data.startswith(b"{") and data.count(b"\n") == 1
        assert not os.stat(log).st_mode & 0o111

    def test_a_whole_last_line_gets_no_blank_line(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("started", stage="transcription")
        run.emit("done", stage="transcription")
        data = (tmp_path / "events.jsonl").read_bytes()
        assert data.count(b"\n") == 2 and b"\n\n" not in data

    def test_the_newline_and_the_record_are_one_write(
        self, tmp_path, monkeypatch
    ):
        """Another process's append can land between two writes but never
        inside one, so the newline that ends a torn line is written with
        the record."""
        tear(tmp_path / "events.jsonl")
        recorder = _OsWrites()
        monkeypatch.setattr(rundir, "os", recorder)
        RunDir(tmp_path).emit("run-start")
        (write,) = recorder.writes
        assert write.startswith(b"\n{") and write.endswith(b"}\n")

    def test_a_short_write_is_finished(self, tmp_path, monkeypatch):
        recorder = _OsWrites(most=7)
        monkeypatch.setattr(rundir, "os", recorder)
        RunDir(tmp_path).emit("run-start", note="Klara Löbig")
        monkeypatch.undo()
        (event,) = RunDir(tmp_path).events()
        assert event["note"] == "Klara Löbig"
        assert len(recorder.writes) > 1

    def test_two_appenders_that_find_one_torn_line_keep_both_records(
        self, tmp_path, monkeypatch
    ):
        """Two appenders that the log's lock does not order (two RunDirs
        in one process, with a lock each, or two processes where no lock
        is to be had) both find the last line torn and both end it: that
        leaves a blank line, which readers skip."""
        tear(tmp_path / "events.jsonl")
        first, second = RunDir(tmp_path), RunDir(tmp_path)
        # the second appends after the first has found the line torn and
        # before it writes
        recorder = _OsWrites(before=lambda: second.emit("run-start"))
        monkeypatch.setattr(rundir, "os", recorder)
        first.emit("done", stage="transcription")
        monkeypatch.undo()
        data = (tmp_path / "events.jsonl").read_bytes()
        assert data.startswith(TORN + b"\n{") and data.count(b"\n\n") == 1
        assert names(tmp_path) == ["run-start", "done"]


class TestDeeplyNestedLines:
    """A line nested past Python's recursion limit makes `json.loads`
    raise RecursionError, never produced by a writer here but read like
    any other line a reader cannot decode: skipped, with the records
    around it read whole (#285)."""

    def test_events_around_it_set_the_state(self, tmp_path):
        run = RunDir(tmp_path)
        run.update_manifest(
            documents=[
                {"sha1": DOC_A, "kind": "text", "stages": ["transcription"]}
            ]
        )
        run.emit("started", subject=DOC_A, kind="text", stage="transcription")
        nest(tmp_path / "events.jsonl")
        run.emit("done", subject=DOC_A, kind="text", stage="transcription")
        assert run_state(run)[(DOC_A, "transcription")].status == DONE

    def test_cache_events_around_it_are_counted(self, tmp_path):
        run = RunDir(tmp_path)
        run.emit("cache-miss", role="extract", reason="absent", key="k")
        nest(tmp_path / "events.jsonl")
        run.emit("cache-hit", role="extract", reason="key match", key="k")
        assert cache_counts(run) == {
            "extract": {"miss: absent": 1, "hit: key match": 1}
        }

    def test_alerts_around_it_are_kept(self, tmp_path):
        run = RunDir(tmp_path)
        run.alert(
            stage="transcription",
            check="tool-failure text",
            subject=DOC_A[:10],
            value=1,
            carried=False,
        )
        nest(tmp_path / "alerts.jsonl")
        run.alert(
            stage="extraction",
            check="tool-failure text",
            subject=DOC_B[:10],
            value=1,
            carried=True,
        )
        assert [a["subject"] for a in run.alerts()] == [
            DOC_A[:10],
            DOC_B[:10],
        ]

    def test_control_requests_around_it_fold(self, tmp_path):
        run = RunDir(tmp_path)
        run.request("pause", reason="before the deep nesting")
        nest(tmp_path / "control.jsonl")
        run.request("resume")
        assert run.control().scopes == {}


# another process's writer: it takes the log's lock, says so, and on a line
# from its stdin writes a fragment and dies
HOLDER = """
import fcntl, os, signal, sys
log = os.open(sys.argv[1], os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
fcntl.lockf(log, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.readline()
os.write(log, sys.argv[2].encode())
os.kill(os.getpid(), signal.SIGKILL)
"""

# another process that tries for a shared lock on the log without waiting
PROBE = """
import fcntl, os, sys
log = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.lockf(log, fcntl.LOCK_SH | fcntl.LOCK_NB)
except OSError:
    print("refused")
else:
    print("taken")
"""


class TestTheAppendLock:
    """An append locks the log from before it reads the last byte until
    its record is written, so, wherever the lock holds, a writer in
    another process that dies mid-write costs only its own record; where
    lockf fails, the append goes ahead unlocked (#271)."""

    def test_an_append_checks_the_last_line_after_the_lock_is_free(
        self, tmp_path, monkeypatch
    ):
        """A writer in another process holds the lock and dies mid-write.
        An append begun meanwhile waits, then finds the fragment and ends
        it. Were the check made before the lock, the append would find the
        last line whole, and its write would land on the fragment."""
        log = tmp_path / "events.jsonl"
        RunDir(tmp_path).emit("started", stage="transcription")
        command = [sys.executable, "-c", HOLDER, str(log), TORN.decode()]
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        ) as holder:
            once = threading.Lock()

            def die():
                with once:
                    if holder.poll() is None:
                        holder.stdin.write(b"go\n")
                        holder.stdin.flush()
                        holder.wait()

            try:
                assert holder.stdout.readline() == b"locked\n"
                # an append that reaches its write first lets the writer
                # die before it writes
                monkeypatch.setattr(rundir, "os", _OsWrites(before=die))
                append = threading.Thread(
                    target=RunDir(tmp_path).emit,
                    args=("run-start",),
                    daemon=True,
                )
                append.start()
                append.join(0.2)
                assert append.is_alive()  # waiting for the lock
                die()
                append.join(10)
                assert not append.is_alive()
            finally:
                holder.kill()
        monkeypatch.undo()
        assert holder.returncode == -signal.SIGKILL
        assert names(tmp_path) == ["started", "run-start"]
        assert log.read_bytes().count(TORN + b"\n{") == 1

    def test_the_lock_is_held_until_the_record_is_written(
        self, tmp_path, monkeypatch
    ):
        log = tmp_path / "events.jsonl"
        seen = []

        def probe():
            command = [sys.executable, "-c", PROBE, str(log)]
            seen.append(subprocess.run(command, capture_output=True).stdout)

        monkeypatch.setattr(rundir, "os", _OsWrites(before=probe))
        RunDir(tmp_path).emit("run-start")
        assert seen == [b"refused\n"]

    @pytest.mark.parametrize(
        "error", [errno.ENOLCK, errno.EINVAL], ids=["ENOLCK", "EINVAL"]
    )
    def test_an_append_goes_ahead_where_no_lock_is_to_be_had(
        self, tmp_path, monkeypatch, error
    ):
        def lockf(descriptor, command):
            raise OSError(error, os.strerror(error))

        no_locks = SimpleNamespace(LOCK_EX=fcntl.LOCK_EX, lockf=lockf)
        monkeypatch.setattr(rundir, "fcntl", no_locks)
        tear(tmp_path / "events.jsonl")
        RunDir(tmp_path).emit("run-start")
        assert names(tmp_path) == ["run-start"]


class TestControl:
    def test_pause_resume_and_abort(self):
        state = fold_control(
            [
                {"action": "pause", "scope": "stage:critique"},
                {"action": "pause", "scope": "kind:media"},
                {"action": "resume", "scope": "kind:media"},
                {"action": "abort", "scope": "kind:gedcom"},
                {"action": "resume", "scope": "kind:gedcom"},
            ]
        )
        assert state.stopped(stage="critique") == "stage:critique paused"
        assert state.stopped(kind="media") is None
        # an abort holds for the rest of the invocation
        assert state.stopped(kind="gedcom") == "kind:gedcom aborted"
        assert state.stopped(stage="extraction", kind="text") is None

    def test_a_new_invocation_starts_clear(self, tmp_path):
        run = RunDir(tmp_path)
        run.request("abort", by="monitor", reason="futile")
        assert run.control().stopped() == "run aborted"
        RunDir(tmp_path).start()
        assert run.control().stopped() is None

    def test_a_request_names_one_slice(self, tmp_path):
        run = RunDir(tmp_path)
        with pytest.raises(ValueError):
            run.request("pause", stage="extraction", kind="media")
        with pytest.raises(ValueError):
            run.request("stop")


class TestRunState:
    def plan(self, run, *stages):
        run.update_manifest(
            documents=[
                {"sha1": DOC_A, "kind": "text", "stages": list(stages)},
                {"sha1": DOC_B, "kind": "media", "stages": list(stages)},
            ]
        )

    def emit(self, run, event, stage, subject=DOC_A, **fields):
        run.emit(event, subject=subject, kind="text", stage=stage, **fields)

    def test_planned_stages_start_pending(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "transcription", "extraction")
        states = run_state(run)
        assert set(states) == {
            (DOC_A, "transcription"),
            (DOC_A, "extraction"),
            (DOC_B, "transcription"),
            (DOC_B, "extraction"),
        }
        assert {state.status for state in states.values()} == {PENDING}

    def test_the_latest_event_sets_the_state(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "transcription", "extraction", "critique")
        self.emit(run, "started", "transcription")
        self.emit(run, "done", "transcription")
        self.emit(run, "started", "extraction")
        self.emit(run, "done", "extraction", attention=["fell back"])
        self.emit(run, "started", "critique")
        self.emit(run, "failed", "critique", reason="RuntimeError: boom")
        self.emit(run, "started", "transcription", subject=DOC_B)
        states = run_state(run)
        assert states[(DOC_A, "transcription")].status == DONE
        extraction = states[(DOC_A, "extraction")]
        assert (extraction.status, extraction.reason) == (
            NEEDS_ATTENTION,
            "fell back",
        )
        critique = states[(DOC_A, "critique")]
        assert critique.status == FAILED and "boom" in critique.reason
        # started with no later event: running, or interrupted
        assert states[(DOC_B, "transcription")].status == RUNNING
        assert states[(DOC_B, "critique")].status == PENDING

    def test_a_held_stage_is_pending_with_its_reason(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "critique")
        self.emit(run, "held", "critique", reason="stage:critique paused")
        state = run_state(run)[(DOC_A, "critique")]
        assert (state.status, state.reason) == (
            PENDING,
            "stage:critique paused",
        )

    def test_a_reused_view_is_done_with_its_attention(self, tmp_path):
        """A run that reuses a stage's view (#225) leaves the stage done,
        or needing attention with the reasons noted when it was made."""
        run = RunDir(tmp_path)
        self.plan(run, "transcription", "extraction")
        self.emit(run, "failed", "transcription", reason="RuntimeError: x")
        self.emit(run, "reused", "transcription")
        self.emit(run, "reused", "extraction", attention=["fell back"])
        states = run_state(run)
        assert states[(DOC_A, "transcription")].status == DONE
        extraction = states[(DOC_A, "extraction")]
        assert (extraction.status, extraction.reason) == (
            NEEDS_ATTENTION,
            "fell back",
        )

    def test_a_later_run_retries_a_failed_stage(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "critique")
        self.emit(run, "failed", "critique", reason="RuntimeError: boom")
        self.emit(run, "started", "critique")
        self.emit(run, "done", "critique")
        assert run_state(run)[(DOC_A, "critique")].status == DONE

    def test_other_events_leave_the_state_alone(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "transcription")
        run.emit("cache-hit", subject=DOC_A, role="transcribe", key="k")
        run.emit("set-aside", subject=DOC_A, reason="content changed")
        assert run_state(run)[(DOC_A, "transcription")].status == PENDING

    def test_summary_counts_states_per_stage(self, tmp_path):
        run = RunDir(tmp_path)
        self.plan(run, "transcription")
        self.emit(run, "done", "transcription")
        assert summarise(run_state(run)) == {
            "transcription": {DONE: 1, PENDING: 1}
        }


# how an invocation's end can be on record: as the driver records it, in
# run.json and then in its closing event, and each alone (a driver killed
# between the two, or a run.json that does not record it)
ENDS = ["run.json and closing event", "run.json alone", "closing event alone"]


class TestStagesCutShort:
    """A stage whose latest event is its start is running while the
    invocation that started it runs, and interrupted once that invocation
    is over (#300), as the live monitor reads it: the monitor's tests
    compare its counts with run_state's on the same streams."""

    def invocation(self, tmp_path, *stages: str) -> RunDir:
        """An invocation of the run in `tmp_path`, started, which plans
        `stages` for both documents."""
        run = RunDir(tmp_path)
        run.start(
            documents=[
                {"sha1": DOC_A, "kind": "text", "stages": list(stages)},
                {"sha1": DOC_B, "kind": "media", "stages": list(stages)},
            ]
        )
        return run

    def emit(self, run, event, stage, subject=DOC_A, **fields):
        run.emit(event, subject=subject, kind="text", stage=stage, **fields)

    def end(self, run, outcome="interrupted", how=ENDS[0]):
        """End the invocation as the driver ends it: its end in run.json,
        then the event that closes its events, or one of them alone."""
        if how != "closing event alone":
            run.end(outcome)
        if how != "run.json alone":
            closing = RUN_INTERRUPTED if outcome == "interrupted" else RUN_END
            run.emit(closing, outcome=outcome, statuses={"paused": 1})

    def status(self, tmp_path, stage, subject=DOC_A) -> str:
        """The stage's state as run_state reads it, once the live
        monitor's counts are shown to agree with run_state's."""
        assert disagreement(tmp_path) is None
        return run_state(RunDir(tmp_path))[(subject, stage)].status

    @pytest.mark.parametrize("how", ENDS)
    @pytest.mark.parametrize("outcome", ["interrupted", "paused", "failed"])
    def test_a_stage_its_invocation_left_started_is_interrupted(
        self, tmp_path, outcome, how
    ):
        """Ctrl-C, a paused pass or an error ends the invocation with a
        stage started: nothing runs it, so it is interrupted, not running,
        with no reason; the next invocation starts it again."""
        run = self.invocation(tmp_path, "transcription", "extraction")
        self.emit(run, "started", "transcription")
        self.emit(run, "done", "transcription")
        self.emit(run, "started", "extraction")
        assert self.status(tmp_path, "extraction") == RUNNING
        self.end(run, outcome, how)
        assert disagreement(tmp_path) is None
        states = run_state(RunDir(tmp_path))
        assert states[(DOC_A, "extraction")] == rundir.StageState(INTERRUPTED)
        assert summarise(states) == {
            "transcription": {DONE: 1, PENDING: 1},
            "extraction": {INTERRUPTED: 1, PENDING: 1},
        }

    def test_the_end_of_an_earlier_invocation_leaves_a_later_one_running(
        self, tmp_path
    ):
        """Only the latest invocation's end counts: a stage it started
        runs, while the stage the ended one left is interrupted."""
        first = self.invocation(tmp_path, "transcription")
        self.emit(first, "started", "transcription")
        self.end(first)
        second = self.invocation(tmp_path, "transcription")
        self.emit(second, "started", "transcription", subject=DOC_B)
        assert self.status(tmp_path, "transcription") == INTERRUPTED
        assert self.status(tmp_path, "transcription", DOC_B) == RUNNING

    def test_an_earlier_invocations_late_closing_event(self, tmp_path):
        """The closing event of an earlier invocation that lands after a
        later one's start closes the earlier one, not the latest: the
        stage the latest has started runs."""
        first = self.invocation(tmp_path, "critique")
        self.emit(first, "started", "critique")
        first.end("interrupted")
        second = self.invocation(tmp_path, "critique")
        self.emit(second, "started", "critique", subject=DOC_B)
        first.emit(RUN_INTERRUPTED, outcome="interrupted", statuses={})
        assert self.status(tmp_path, "critique") == INTERRUPTED
        assert self.status(tmp_path, "critique", DOC_B) == RUNNING

    def test_a_killed_invocation_reads_as_running_until_another_starts(
        self, tmp_path
    ):
        """A killed invocation records no end and has no closing event:
        while it is the latest, nothing tells it from one still running,
        as in the monitor; a later invocation's start shows it is over."""
        killed = self.invocation(tmp_path, "extraction")
        self.emit(killed, "started", "extraction")
        assert self.status(tmp_path, "extraction") == RUNNING
        later = self.invocation(tmp_path, "extraction")
        assert self.status(tmp_path, "extraction") == INTERRUPTED
        self.emit(later, "started", "extraction", subject=DOC_B)
        assert self.status(tmp_path, "extraction", DOC_B) == RUNNING

    def test_an_interrupted_stage_is_started_and_done_again(self, tmp_path):
        first = self.invocation(tmp_path, "critique")
        self.emit(first, "started", "critique")
        self.end(first)
        second = self.invocation(tmp_path, "critique")
        self.emit(second, "started", "critique")
        assert self.status(tmp_path, "critique") == RUNNING
        self.emit(second, "done", "critique")
        self.end(second, "finished")
        assert self.status(tmp_path, "critique") == DONE

    def test_a_stage_interrupted_twice_is_interrupted(self, tmp_path):
        first = self.invocation(tmp_path, "critique")
        self.emit(first, "started", "critique")
        self.end(first)
        second = self.invocation(tmp_path, "critique")
        self.emit(second, "started", "critique")
        self.end(second, "paused")
        assert self.status(tmp_path, "critique") == INTERRUPTED
        # a third invocation that has not reached it yet
        self.invocation(tmp_path, "critique")
        assert self.status(tmp_path, "critique") == INTERRUPTED

    def test_held_and_failed_stages_stay_so_when_the_invocation_ends(
        self, tmp_path
    ):
        """Only a stage whose latest event is its start is cut short: a
        held stage is pending with its reason, and a failed one failed,
        whether the invocation that left them has ended or not."""
        first = self.invocation(tmp_path, "extraction", "critique")
        self.emit(first, "started", "extraction")
        self.emit(first, "failed", "extraction", reason="RuntimeError: x")
        self.emit(first, "started", "critique")
        self.end(first)
        second = self.invocation(tmp_path, "extraction", "critique")
        self.emit(second, "held", "critique", reason="stage:critique paused")
        self.emit(second, "started", "critique", subject=DOC_B)
        self.emit(
            second, "failed", "critique", subject=DOC_B, reason="OSError: y"
        )
        self.end(second, "stopped")
        assert disagreement(tmp_path) is None
        states = run_state(RunDir(tmp_path))
        assert states[(DOC_A, "extraction")] == rundir.StageState(
            FAILED, "RuntimeError: x"
        )
        assert states[(DOC_A, "critique")] == rundir.StageState(
            PENDING, "stage:critique paused"
        )
        assert states[(DOC_B, "critique")] == rundir.StageState(
            FAILED, "OSError: y"
        )
        assert states[(DOC_B, "extraction")] == rundir.StageState(PENDING)

    def test_a_run_with_no_end_on_record_is_running(self, tmp_path):
        """No run.json invocation and no event that opens or closes one,
        as a run directory written by hand has: nothing says the stage is
        over."""
        run = RunDir(tmp_path)
        run.update_manifest(
            documents=[{"sha1": DOC_A, "kind": "text", "stages": ["critique"]}]
        )
        self.emit(run, "started", "critique")
        assert self.status(tmp_path, "critique") == RUNNING

    def test_without_invocations_in_run_json_the_events_tell(self, tmp_path):
        """A run.json that records no invocation: the latest event that
        opens or closes one says whether the latest invocation has
        ended, as the monitor reads it."""
        run = RunDir(tmp_path)
        run.update_manifest(
            documents=[{"sha1": DOC_A, "kind": "text", "stages": ["critique"]}]
        )
        run.emit("run-start")
        self.emit(run, "started", "critique")
        assert self.status(tmp_path, "critique") == RUNNING
        run.emit(RUN_INTERRUPTED, outcome="interrupted")
        assert self.status(tmp_path, "critique") == INTERRUPTED
        # a later invocation starts: the one that started the stage is over
        later = RunDir(tmp_path)
        later.emit("run-start")
        assert self.status(tmp_path, "critique") == INTERRUPTED

    def test_a_start_that_names_no_invocation(self, tmp_path):
        """A start event without an invocation is read by the latest
        invocation's end alone: running until it ends."""
        run = self.invocation(tmp_path, "critique")
        start = {"event": "started", "subject": DOC_B, "stage": "critique"}
        with open(tmp_path / rundir.EVENTS, "a") as log:
            log.write(json.dumps(start) + "\n")
        assert self.status(tmp_path, "critique", DOC_B) == RUNNING
        self.end(run)
        assert self.status(tmp_path, "critique", DOC_B) == INTERRUPTED

    def test_an_invocation_that_starts_between_the_reads(self, tmp_path):
        """A new invocation starts, and starts a stage, while run_state
        reads the run: the stage is never read against the ended
        invocation before it and taken for that one's leftover."""
        first = self.invocation(tmp_path, "critique")
        self.emit(first, "started", "critique")
        self.end(first)

        def start_another():
            second = self.invocation(tmp_path, "critique")
            self.emit(second, "started", "critique", subject=DOC_B)

        states = run_state(Racing(tmp_path, first=start_another))
        assert states[(DOC_A, "critique")].status == INTERRUPTED
        assert states[(DOC_B, "critique")].status == RUNNING
        assert disagreement(tmp_path) is None

    def test_a_stage_done_as_its_invocation_ends_between_the_reads(
        self, tmp_path
    ):
        """The latest invocation finishes a stage and ends just after
        run_state has read the events: the stage is done, never read as
        cut short by an end that came after its done."""
        run = self.invocation(tmp_path, "critique")
        self.emit(run, "started", "critique")

        def finish(reads):
            if reads == 1:
                self.emit(run, "done", "critique")
                self.end(run, "finished")

        racing = Racing(tmp_path, each=finish)
        states = run_state(racing)
        assert states[(DOC_A, "critique")].status == DONE
        # one reading disagreed, and the one after it agreed
        assert racing.event_reads == 2
        assert disagreement(tmp_path) is None

    def test_the_reads_are_bounded_while_invocations_keep_starting(
        self, tmp_path
    ):
        """Invocations that keep starting, each starting a stage, while
        run_state reads leave its readings of run.json never agreeing: it
        stops after a bounded number of reads, with the last events read
        against the run.json read after them. So the stage started by the
        latest invocation those events hold is interrupted: that run.json
        holds a later invocation, whose start shows the earlier one is
        over."""
        run = self.invocation(tmp_path, "critique")
        self.emit(run, "started", "critique")

        def start_another(reads):
            if reads <= 2 * rundir._READS:
                later = self.invocation(tmp_path, "critique")
                self.emit(later, "started", "critique", subject=DOC_B)

        racing = Racing(tmp_path, each=start_another)
        states = run_state(racing)
        assert racing.event_reads == rundir._READS
        assert states[(DOC_A, "critique")].status == INTERRUPTED
        assert states[(DOC_B, "critique")].status == INTERRUPTED


class Racing(RunDir):
    """A run directory that something else writes to while run_state
    reads it: `first` runs once, just after the first of its reads,
    whichever file that reads, and `each` just after each reading of the
    events, given how many there have been."""

    def __init__(self, path, *, first=None, each=None) -> None:
        super().__init__(path)
        self.first, self.each = first, each
        self.event_reads = 0

    def _after_a_read(self) -> None:
        first, self.first = self.first, None
        if first is not None:
            first()

    def manifest(self) -> dict:
        manifest = super().manifest()
        self._after_a_read()
        return manifest

    def events(self) -> list[dict]:
        events = super().events()
        self._after_a_read()
        self.event_reads += 1
        if self.each is not None:
            self.each(self.event_reads)
        return events


class TestCacheCounts:
    def test_cache_events_are_counted_per_role_and_reason(self, tmp_path):
        run = RunDir(tmp_path)
        for event, role, reason in [
            ("cache-hit", "transcribe", "key match"),
            ("cache-hit", "transcribe", "key match"),
            ("cache-hit", "transcribe", "adopted"),
            ("cache-miss", "extract", "absent"),
            ("cache-miss", "extract", "rejected"),
            ("cache-resume", "extract", None),
            ("cache-replay", "extract", None),
            ("cache-reject", "extract", "chunk: no terminal sentinel"),
            ("cache-adopt", "transcribe", "adopted"),
            ("done", None, None),
        ]:
            run.emit(event, role=role, reason=reason, key="k")
        assert cache_counts(run) == {
            "transcribe": {
                "hit: key match": 2,
                "hit: adopted": 1,
                "adopt": 1,
            },
            "extract": {
                "miss: absent": 1,
                "miss: rejected": 1,
                "resume": 1,
                "replay": 1,
                "reject": 1,
            },
        }

    def test_the_counts_are_totals_over_every_invocation(self, tmp_path):
        first = RunDir(tmp_path)
        first.emit("cache-miss", role="extract", reason="absent", key="k")
        second = RunDir(tmp_path)
        second.emit("cache-hit", role="extract", reason="key match", key="k")
        assert first.invocation != second.invocation
        assert cache_counts(second) == {
            "extract": {"miss: absent": 1, "hit: key match": 1}
        }


class TestStagesStateCommand:
    def test_it_prints_stages_and_cache_counts(self, tmp_path, capsys):
        from yizkr.cli import build_parser

        run = RunDir(tmp_path)
        run.start(documents=[{"sha1": DOC_A, "stages": ["transcription"]}])
        run.emit("done", subject=DOC_A, stage="transcription")
        run.emit("cache-hit", role="transcribe", reason="key match", key="k")
        args = build_parser().parse_args(["stages", "state", str(tmp_path)])
        assert args.func(args) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].split() == ["transcription", "1", "done"]
        assert out[1].split() == [
            "cache",
            "transcribe",
            "1",
            "hit:",
            "key",
            "match",
            "(all",
            "invocations)",
        ]

    def test_json(self, tmp_path, capsys):
        import json

        from yizkr.cli import build_parser

        run = RunDir(tmp_path)
        run.start(documents=[{"sha1": DOC_A, "stages": ["extraction"]}])
        run.emit(
            "failed", subject=DOC_A, stage="extraction", reason="RuntimeError"
        )
        args = build_parser().parse_args(
            ["stages", "state", str(tmp_path), "--json"]
        )
        assert args.func(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert report == {
            "stages": [
                {
                    "subject": DOC_A,
                    "stage": "extraction",
                    "status": FAILED,
                    "reason": "RuntimeError",
                }
            ],
            "cache": {},
        }

    def test_a_stage_cut_short_shows_as_interrupted(self, tmp_path, capsys):
        """After Ctrl-C, the stage the invocation cut short is interrupted,
        in the counts and in --json, as the monitor shows it (#300)."""
        from yizkr.cli import build_parser

        run = RunDir(tmp_path)
        run.start(
            documents=[
                {"sha1": DOC_A, "stages": ["extraction"]},
                {"sha1": DOC_B, "stages": ["extraction"]},
            ]
        )
        run.emit("started", subject=DOC_A, stage="extraction")
        run.end("interrupted")
        run.emit(RUN_INTERRUPTED, outcome="interrupted", statuses={})
        parser = build_parser()
        args = parser.parse_args(["stages", "state", str(tmp_path)])
        assert args.func(args) == 0
        out = capsys.readouterr().out.splitlines()
        assert out == ["extraction       1 interrupted, 1 pending"]
        args = parser.parse_args(["stages", "state", str(tmp_path), "--json"])
        assert args.func(args) == 0
        rows = json.loads(capsys.readouterr().out)["stages"]
        assert {
            row["subject"]: (row["status"], row["reason"]) for row in rows
        } == {
            DOC_A: (INTERRUPTED, None),
            DOC_B: (PENDING, None),
        }

    def test_a_directory_without_a_run_is_refused(self, tmp_path):
        from yizkr.cli import build_parser

        args = build_parser().parse_args(["stages", "state", str(tmp_path)])
        with pytest.raises(SystemExit, match="no run.json"):
            args.func(args)

    def test_a_deeply_nested_run_json_is_refused_like_a_missing_one(
        self, tmp_path
    ):
        """A run.json nested past Python's recursion limit makes
        `json.loads` raise RecursionError, not ValueError:
        `RunDir.manifest()` treats it like any other run.json it cannot
        read, and returns {}, so the command gives its friendly refusal
        rather than a traceback (#285)."""
        from yizkr.cli import build_parser

        (tmp_path / "run.json").write_bytes(b"[" * 100_000)
        args = build_parser().parse_args(["stages", "state", str(tmp_path)])
        with pytest.raises(SystemExit, match="no run.json"):
            args.func(args)
