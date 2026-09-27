"""Offline tests for the run directory: its manifest, append-only logs,
control requests, and the run state and cache counts derived from its
events (#87, #225)."""

from __future__ import annotations

import hashlib

import pytest

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
                "replay": 1,
                "reject": 1,
            },
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
