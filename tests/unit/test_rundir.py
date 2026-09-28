"""Offline tests for the run directory: its manifest, append-only logs,
control requests, and the run state and cache counts derived from its
events (#87, #225)."""

from __future__ import annotations

import hashlib
import os

import pytest

from yizkr.pipeline import rundir
from yizkr.pipeline.rundir import (
    DONE,
    FAILED,
    NEEDS_ATTENTION,
    PENDING,
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


def tear(log, fragment=TORN):
    """End `log` in a torn line, as a writer that died mid-write leaves it."""
    with open(log, "ab") as handle:
        handle.write(fragment)


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

    def test_a_log_holding_only_a_fragment(self, tmp_path):
        tear(tmp_path / "events.jsonl")
        RunDir(tmp_path).emit("run-start")
        data = (tmp_path / "events.jsonl").read_bytes()
        assert data.startswith(TORN + b"\n{")
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
        """Two appenders (two processes, or two RunDirs with a lock each)
        both find the last line torn and both end it: that leaves a blank
        line, which readers skip."""
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

    def test_a_directory_without_a_run_is_refused(self, tmp_path):
        from yizkr.cli import build_parser

        args = build_parser().parse_args(["stages", "state", str(tmp_path)])
        with pytest.raises(SystemExit, match="no run.json"):
            args.func(args)
