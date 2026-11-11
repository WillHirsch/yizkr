"""Run status follows an invocation's process without changing its history."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.exits import child_exit_fd, process_state
from tests.unit.test_rundir import DOC_A, DOC_B
from yizkr.pipeline import run_liveness
from yizkr.pipeline.monitor import RunMonitor
from yizkr.pipeline.run_liveness import capture_owner, owner_liveness
from yizkr.pipeline.rundir import (
    DONE,
    INTERRUPTED,
    RUNNING,
    UNKNOWN,
    Invocations,
    RunDir,
    run_state,
)

HANG_GUARD = 30

INVOCATION = """
import os, sys
from pathlib import Path
from yizkr.pipeline.rundir import RunDir

run = RunDir(Path(sys.argv[1]))
first, second = sys.argv[2:]
run.start(documents=[
    {"sha1": first, "kind": "text", "stages": ["transcription", "extraction"]},
    {"sha1": second, "kind": "text", "stages": ["extraction"]},
])
run.emit("done", subject=first, kind="text", stage="transcription")
run.emit("started", subject=first, kind="text", stage="extraction")
run.emit("started", subject=second, kind="text", stage="extraction")
print("ready", flush=True)
sys.stdin.readline()
os._exit(0)
"""


@contextmanager
def invocation_process(path):
    child = subprocess.Popen(
        [sys.executable, "-c", INVOCATION, str(path), DOC_A, DOC_B],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], HANG_GUARD)
        assert ready, "fictional invocation did not report readiness"
        assert child.stdout.readline() == b"ready\n"
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=HANG_GUARD)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.parametrize("ending", ["exit", "kill"])
def test_process_exit_updates_status_without_rewriting_history_and_can_resume(
    tmp_path, ending
):
    """Death is observable before reaping, with no closing event or timer."""
    with invocation_process(tmp_path) as child:
        run = RunDir(tmp_path)
        owner = run.manifest()["invocations"][-1]["owner"]
        assert owner_liveness(owner)[0] == "active"
        states = run_state(run)
        assert states[(DOC_A, "transcription")].status == DONE
        assert states[(DOC_A, "extraction")].status == RUNNING
        monitor = RunMonitor(tmp_path)
        monitor.refresh()
        assert monitor.summary().state == "running"
        history = {
            name: (tmp_path / name).read_bytes()
            for name in ("run.json", "events.jsonl", "control.jsonl")
        }
        original = run.manifest()["invocations"][0]

        ended = child_exit_fd(child.pid)
        try:
            if ending == "kill":
                os.kill(child.pid, signal.SIGKILL)
            else:
                child.stdin.close()
            ready, _, _ = select.select([ended], [], [], HANG_GUARD)
            assert ready, "fictional invocation did not end"
        finally:
            os.close(ended)
        assert process_state(child.pid) == "Z"
        assert owner_liveness(owner)[0] == "abandoned"
        assert run_state(run)[(DOC_A, "extraction")].status == INTERRUPTED
        # The monitor has already folded these events. It must recheck the
        # process even though none of the files changed.
        monitor.refresh()
        summary = monitor.summary()
        assert summary.state == "interrupted"
        assert not summary.in_flight
        assert all(stage.running == 0 for stage in summary.stages)
        child.wait(timeout=HANG_GUARD)
        assert owner_liveness(owner)[0] == "abandoned"
        assert run_state(run)[(DOC_A, "transcription")].status == DONE
        assert {
            name: (tmp_path / name).read_bytes() for name in history
        } == history

    resumed = RunDir(tmp_path)
    resumed.start()
    resumed.emit("started", subject=DOC_A, kind="text", stage="extraction")
    states = run_state(resumed)
    assert states[(DOC_A, "extraction")].status == RUNNING
    assert states[(DOC_B, "extraction")].status == INTERRUPTED
    resumed.emit("done", subject=DOC_A, kind="text", stage="extraction")
    resumed.end("finished")
    assert run_state(resumed)[(DOC_A, "extraction")].status == DONE
    assert resumed.manifest()["invocations"][0] == original
    assert (
        (tmp_path / "events.jsonl")
        .read_bytes()
        .startswith(history["events.jsonl"])
    )


def test_the_current_process_has_verifiable_identity():
    owner = capture_owner()
    assert isinstance(owner, dict)
    assert owner_liveness(owner)[0] == "active"


def test_a_live_pid_with_another_start_identity_is_abandoned():
    owner = capture_owner()
    # This PID exists, but is not the process that the invocation recorded.
    previous = {**owner, "start_ticks": owner["start_ticks"] + 1}
    state, reason = owner_liveness(previous)
    assert state == "abandoned"
    assert reason


def test_process_identity_reads_past_parentheses_and_non_utf8_command_names(
    monkeypatch,
):
    owner = capture_owner()
    # /proc field 22 identifies the process. The parenthesized command field
    # is arbitrary bytes and is not a safe delimiter for naive word splitting.
    fields = [b"S", *([b"0"] * 18), str(owner["start_ticks"]).encode()]
    stat = (
        str(owner["pid"]).encode()
        + b" (odd (name) \xff) "
        + b" ".join(fields)
        + b"\n"
    )
    path = Path(f"/proc/{owner['pid']}/stat")
    read_bytes = Path.read_bytes

    def read(current):
        return stat if current == path else read_bytes(current)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert owner_liveness(owner)[0] == "active"


@pytest.mark.parametrize("key", ["host", "boot_id", "pid_namespace"])
def test_another_process_context_is_unknown_without_probing_its_pid(
    monkeypatch, key
):
    owner = capture_owner()
    if key == "host":
        owner[key] += "-another-host"
    elif key == "boot_id":
        owner[key] = "00000000-0000-0000-0000-000000000000"
    else:
        owner[key] += 1

    def refuse_probe(_pid):
        pytest.fail("a foreign process context must not probe a local PID")

    monkeypatch.setattr(run_liveness, "_process_identity", refuse_probe)
    state, reason = owner_liveness(owner)
    assert state == "unknown"
    assert reason


@pytest.mark.parametrize("unavailable", ["_context", "_process_identity"])
def test_inaccessible_process_evidence_is_unknown(monkeypatch, unavailable):
    owner = capture_owner()

    def denied(*args):
        raise PermissionError("fictional process evidence is inaccessible")

    monkeypatch.setattr(run_liveness, unavailable, denied)
    assert capture_owner() is None
    state, reason = owner_liveness(owner)
    assert state == "unknown"
    assert reason


@pytest.mark.parametrize("denied", [False, True])
def test_a_live_process_hidden_by_procfs_is_unknown(monkeypatch, denied):
    owner = capture_owner()

    def hidden(_pid):
        raise FileNotFoundError("fictional hidden process")

    monkeypatch.setattr(run_liveness, "_process_identity", hidden)
    if denied:

        def no_permission(_pid, _signal):
            raise PermissionError("fictional inaccessible process")

        monkeypatch.setattr(run_liveness.os, "kill", no_permission)
    # Signal zero succeeds for this PID, but cannot prove its start identity.
    assert owner_liveness(owner)[0] == "unknown"


def test_final_records_win_when_the_process_ends_after_the_status_snapshot(
    tmp_path, monkeypatch
):
    from yizkr.pipeline import rundir

    run = RunDir(tmp_path)
    run.start()
    run.emit("started", subject=DOC_A, stage="extraction")
    calls = []

    def finished_between_reads(owner):
        calls.append(owner)
        run.emit("done", subject=DOC_A, stage="extraction")
        run.end("finished")
        run.emit("run-end", outcome="finished")
        return "abandoned", "invocation process has exited"

    monkeypatch.setattr(rundir, "owner_liveness", finished_between_reads)
    assert run_state(run)[(DOC_A, "extraction")].status == DONE
    assert len(calls) == 1


@pytest.mark.parametrize("owner", [None, {}, {"pid": True}, "unreadable"])
def test_absent_or_malformed_identity_is_unknown(owner):
    state, reason = owner_liveness(owner)
    assert state == "unknown"
    assert reason


@pytest.mark.parametrize("outcome", ["paused", "finished"])
def test_recorded_outcomes_take_priority_over_missing_process_identity(
    tmp_path, outcome
):
    run = RunDir(tmp_path)
    run.start()
    run.emit("started", subject=DOC_A, stage="extraction")
    # Old or copied manifests can lack process evidence; explicit history
    # still establishes the outcome, without requiring any owner probe.
    manifest = run.manifest()
    manifest["invocations"][-1].pop("owner")
    run.update_manifest(**manifest)
    assert run_state(run)[(DOC_A, "extraction")].status == UNKNOWN
    run.end(outcome)
    current = Invocations().current(run.manifest())
    assert current.ended
    assert current.liveness == "ended"
    assert run_state(run)[(DOC_A, "extraction")].status == INTERRUPTED
    monitor = RunMonitor(tmp_path)
    monitor.refresh()
    assert monitor.summary().state == outcome
