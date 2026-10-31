"""Owned local work stops without running a provider or using credentials."""

from __future__ import annotations

import asyncio
import ctypes
import os
import select
import subprocess
import sys
import threading
import types
from contextlib import nullcontext
from pathlib import Path

import pytest

from yizkr.pipeline import llm
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
)

# The unit suite refuses unmocked provider processes. Only these tests restore
# the process runner, using Python scripts created in their temporary folder.
RUN_PROCESS = llm._run_process
HANG_GUARD = 30


def background(operation, token=None):
    result = {}

    def run():
        with cancellation_scope(token) if token else nullcontext():
            try:
                result["value"] = operation()
            except BaseException as exc:
                result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def joined(thread):
    thread.join(HANG_GUARD)
    assert not thread.is_alive(), "owned provider work did not stop"


class Ready:
    """A child reports its PID through a FIFO, with no elapsed-time guess."""

    def __init__(self, path):
        self.path = path
        self.arrived = threading.Event()
        self.pid = None
        os.mkfifo(path)

        def receive():
            with path.open() as pipe:
                self.pid = int(pipe.read())
            self.arrived.set()

        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()

    def get(self):
        assert self.arrived.wait(HANG_GUARD), "fictional process did not start"
        joined(self.reader)
        return self.pid


def inactive(pid):
    """A descendant may await the host init's reap; it must not be running."""
    status = Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().split(") ", 1)[1][0] == "Z"
    except FileNotFoundError:
        return True


def pidfd_open(pid):
    """Use Linux's exit notification even in Python builds without its shim."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = (ctypes.c_int, ctypes.c_uint)
    libc.pidfd_open.restype = ctypes.c_int
    descriptor = libc.pidfd_open(pid, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


@pytest.fixture
def real_runner(monkeypatch):
    monkeypatch.setattr(llm, "_run_process", RUN_PROCESS)


@pytest.fixture
def fictional_cli(tmp_path, real_runner):
    executable = tmp_path / "fictional-cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "base = Path(__file__)\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    if base.with_suffix('.hold-version').exists():\n"
        "        marker = base.with_suffix('.version-ready')\n"
        "        marker.write_text(str(os.getpid()))\n"
        "        signal.pause()\n"
        "    print('2.1.237 (fictional CLI)')\n"
        "else:\n"
        "    prompt = sys.stdin.read()\n"
        "    if prompt.startswith('wait-'):\n"
        "        base.with_suffix('.' + prompt).write_text(str(os.getpid()))\n"
        "        signal.pause()\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init',\n"
        "        'cli_version': '2.1.237', 'tools': ['Read']}))\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success',\n"
        "        'result': 'completed fictional operation'}))\n"
    )
    executable.chmod(0o700)
    return executable


def client(executable, **kwargs):
    return llm.ClaudeCodeClient(
        executable=str(executable), reserve=0, max_wait=0, **kwargs
    )


def test_version_probe_is_cancellable(fictional_cli):
    fictional_cli.with_suffix(".hold-version").touch()
    token = Cancellation()
    started = Ready(fictional_cli.with_suffix(".version-ready"))
    thread, result = background(lambda: client(fictional_cli), token)
    pid = started.get()
    token.cancel()
    joined(thread)
    assert isinstance(result["error"], RunCancelled)
    assert inactive(pid)


def test_close_joins_active_call_without_cancelling_its_parent(fictional_cli):
    parent = Cancellation()
    engine = client(fictional_cli, cancellation=parent)
    assert engine.complete("ready") == "completed fictional operation"
    started = Ready(fictional_cli.with_suffix(".wait-close"))
    thread, result = background(lambda: engine.complete("wait-close"))
    pid = started.get()
    engine.close()
    joined(thread)
    assert isinstance(result["error"], RunCancelled)
    assert inactive(pid)
    assert not parent.is_set()
    with pytest.raises(RunCancelled):
        engine.complete("must not start")


def test_shared_client_stops_only_the_cancelled_call(fictional_cli):
    engine = client(fictional_cli)
    first, second = Cancellation(), Cancellation()
    first_ready = Ready(fictional_cli.with_suffix(".wait-first"))
    second_ready = Ready(fictional_cli.with_suffix(".wait-second"))
    a, result_a = background(lambda: engine.complete("wait-first"), first)
    b, result_b = background(lambda: engine.complete("wait-second"), second)
    first_pid, second_pid = first_ready.get(), second_ready.get()
    try:
        first.cancel()
        joined(a)
        assert isinstance(result_a["error"], RunCancelled)
        assert inactive(first_pid)
        assert b.is_alive() and not second.is_set()
        assert not inactive(second_pid)
    finally:
        engine.close()
        joined(b)
    assert isinstance(result_b["error"], RunCancelled)
    assert inactive(second_pid)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux process exit notification"
)
@pytest.mark.parametrize("termination", ["cancel", "timeout", "interrupt"])
def test_process_group_cleanup_includes_term_ignoring_descendant(
    tmp_path, monkeypatch, termination
):
    child_ready = Ready(tmp_path / "child-ready")
    leader_ready = Ready(tmp_path / "leader-ready")
    child = (
        "import os, signal; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_ready.path)!r}).write_text(str(os.getpid())); "
        "signal.pause()"
    )
    leader = (
        "import os, signal, subprocess, sys; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"Path({str(leader_ready.path)!r}).write_text(str(os.getpid())); "
        "signal.pause()"
    )
    token = Cancellation()
    if termination == "interrupt":
        original = subprocess.Popen.communicate
        interrupted = False

        def interrupt_once(process, *args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                child_ready.get()
                raise KeyboardInterrupt()
            return original(process, *args, **kwargs)

        monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_once)
    clock = {"now": 0.0, "calls": 0}
    awaiting_process = threading.Event()

    def monotonic():
        clock["calls"] += 1
        if clock["calls"] > 1:
            awaiting_process.set()
        return clock["now"]

    if termination == "timeout":
        monkeypatch.setattr(
            llm, "time", types.SimpleNamespace(monotonic=monotonic)
        )
    thread, result = background(
        lambda: RUN_PROCESS([sys.executable, "-c", leader], timeout=30),
        token,
    )
    leader_pid, child_pid = leader_ready.get(), child_ready.get()
    try:
        child_exit = pidfd_open(child_pid)
    except ProcessLookupError:
        child_exit = None
    except BaseException:
        token.cancel()
        joined(thread)
        raise
    try:
        if termination == "cancel":
            token.cancel()
        elif termination == "timeout":
            assert awaiting_process.wait(HANG_GUARD)
            clock["now"] = 31.0
        joined(thread)
        expected = {
            "cancel": RunCancelled,
            "timeout": subprocess.TimeoutExpired,
            "interrupt": KeyboardInterrupt,
        }[termination]
        assert isinstance(result["error"], expected)
        # A killed descendant closes inherited pipes before Linux finishes
        # exit bookkeeping. EOF and the reaped leader do not yet guarantee
        # /proc reports Z. Observe this exact process's exit, without polling
        # or treating the kill request itself as proof it stopped.
        if child_exit is not None:
            exited, _, _ = select.select([child_exit], [], [], HANG_GUARD)
            assert exited, "owned descendant did not exit"
        assert inactive(leader_pid) and inactive(child_pid)
        with pytest.raises(ChildProcessError):
            os.waitpid(leader_pid, os.WNOHANG)
    finally:
        if child_exit is not None:
            os.close(child_exit)


def test_already_cancelled_process_never_starts(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("cancelled work started a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    token = Cancellation()
    token.cancel()
    with cancellation_scope(token), pytest.raises(RunCancelled):
        RUN_PROCESS([sys.executable, "-c", "pass"], timeout=1)


class AsyncTransport:
    """A local task whose cleanup must finish before its caller can return."""

    def __init__(self):
        self.started = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.requests = []
        self.messages = self

    async def create(self, **request):
        self.requests.append(request)
        self.started.set()
        try:
            # Yield without elapsed time; this also works in test sandboxes
            # that deny the loop's local socketpair send. Only cancellation
            # finishes the request.
            while True:
                await self.yield_turn()
        finally:
            self.finished.set()

    @staticmethod
    async def yield_turn():
        loop = asyncio.get_running_loop()
        resumed = loop.create_future()
        loop.call_soon(
            lambda: None if resumed.done() else resumed.set_result(None)
        )
        await resumed

    async def close(self):
        await self.yield_turn()
        self.closed.set()


def api_client(monkeypatch, transport, **kwargs):
    package = types.ModuleType("anthropic")
    package.AsyncAnthropic = lambda **_kwargs: transport
    monkeypatch.setitem(sys.modules, "anthropic", package)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fictional-test-key")
    return llm.AnthropicClient(model="claude-sonnet-5", **kwargs)


@pytest.mark.parametrize("cause", ["cancel", "pause", "close"])
def test_api_cancellation_awaits_local_task_and_transport(monkeypatch, cause):
    transport = AsyncTransport()
    token = Cancellation()
    engine = api_client(monkeypatch, transport, cancellation=token)
    thread, result = background(lambda: engine.complete("fictional operation"))
    assert transport.started.wait(HANG_GUARD)
    pause = llm.PassPaused("operator action required")
    if cause == "close":
        engine.close()
    elif cause == "pause":
        token.cancel(exception=pause)
    else:
        token.cancel()
    joined(thread)
    assert transport.finished.is_set() and transport.closed.is_set()
    assert "value" not in result
    if cause == "pause":
        assert result["error"] is pause
    else:
        assert isinstance(result["error"], RunCancelled)
    assert len(transport.requests) == 1
    assert transport.requests[0] == {
        "model": "claude-sonnet-5",
        "max_tokens": engine.MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "fictional operation"}],
            }
        ],
    }
    if cause == "close":
        assert not token.is_set()


def test_process_cleanup_error_preserves_cancellation(
    fictional_cli, monkeypatch
):
    original = llm._stop_process

    def stop_then_fail(process):
        original(process)
        raise OSError("fictional cleanup failure")

    monkeypatch.setattr(llm, "_stop_process", stop_then_fail)
    engine = client(fictional_cli)
    token = Cancellation()
    started = Ready(fictional_cli.with_suffix(".wait-cleanup"))
    thread, result = background(lambda: engine.complete("wait-cleanup"), token)
    pid = started.get()
    token.cancel()
    joined(thread)
    failure = result["error"]
    assert isinstance(failure, RunCancelled)
    assert isinstance(failure.__cause__, OSError)
    assert "provider process cleanup also failed" in failure.__notes__[0]
    assert inactive(pid)


def test_transport_cleanup_error_preserves_pause(monkeypatch):
    class FailingClose(AsyncTransport):
        async def close(self):
            await super().close()
            raise OSError("fictional cleanup failure")

    transport = FailingClose()
    token = Cancellation()
    engine = api_client(monkeypatch, transport, cancellation=token)
    thread, result = background(lambda: engine.complete("fictional operation"))
    assert transport.started.wait(HANG_GUARD)
    pause = llm.PassPaused("operator action required")
    token.cancel(exception=pause)
    joined(thread)
    assert result["error"] is pause
    assert "provider transport cleanup also failed" in pause.__notes__[0]
    assert transport.finished.is_set() and transport.closed.is_set()


def test_cli_completed_result_after_client_close_is_not_returned(
    fictional_cli, monkeypatch
):
    engine = client(fictional_cli)

    def complete_then_close(*args, **kwargs):
        result = RUN_PROCESS(*args, **kwargs)
        engine.close()
        return result

    monkeypatch.setattr(llm, "_run_process", complete_then_close)
    with pytest.raises(RunCancelled):
        engine.complete("fictional operation")


def test_api_close_during_transport_cleanup_refuses_late_answer(monkeypatch):
    class CloseAfterResponse:
        messages = None

        async def create(self, **request):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="text", text="completed fictional operation"
                    )
                ]
            )

        async def close(self):
            engine.close()

    transport = CloseAfterResponse()
    transport.messages = transport
    engine = api_client(monkeypatch, transport)
    with pytest.raises(RunCancelled):
        engine.complete("fictional operation")
