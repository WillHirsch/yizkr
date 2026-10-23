"""The waits for a process's end in tests/fixtures/exits.py (#437): each
descriptor turns readable when the process ends and not before, with or
without os.pidfd_open, so that a test waits on the event alone."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys

import pytest

from tests.fixtures import exits
from tests.fixtures.exits import (
    child_exit_fd,
    pid_exit_fd,
    process_state,
    running,
)

# seconds a test waits for an event before it fails as hung (#437)
HANG_GUARD = 60

# a child that runs until its stdin closes
UNTIL_STDIN_CLOSES = "import sys; sys.stdin.read()"


@pytest.fixture(params=["with", "without"])
def pidfd_open(request, monkeypatch):
    """Each test runs with os.pidfd_open, where this Python has it, and
    without, as on a Python build that lacks it."""
    if request.param == "without":
        monkeypatch.delattr(os, "pidfd_open", raising=False)
    elif not hasattr(os, "pidfd_open"):
        pytest.skip("this Python has no os.pidfd_open")
    return request.param


@pytest.fixture
def child():
    process = subprocess.Popen(
        [sys.executable, "-c", UNTIL_STDIN_CLOSES], stdin=subprocess.PIPE
    )
    yield process
    if process.poll() is None:
        process.kill()
    process.wait()
    process.stdin.close()


def ended_yet(fd: int) -> bool:
    """Whether `fd` says its process has ended, by a poll."""
    ready, _, _ = select.select([fd], [], [], 0)
    return bool(ready)


def ended(fd: int) -> bool:
    """Whether `fd` says its process has ended, waiting for it."""
    ready, _, _ = select.select([fd], [], [], HANG_GUARD)
    return bool(ready)


@pytest.mark.parametrize("exit_fd", [child_exit_fd, pid_exit_fd])
def test_readable_once_the_process_ends_and_not_before(
    exit_fd, child, pidfd_open
):
    fd = exit_fd(child.pid)
    try:
        assert not ended_yet(fd)
        child.stdin.close()
        assert ended(fd)
    finally:
        os.close(fd)
    # left unreaped, so its pid, and the group it may lead, stay its own
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    assert os.waitid(os.P_PID, child.pid, flags) is not None
    assert child.returncode is None
    assert child.wait() == 0


def test_a_child_already_reaped_counts_as_ended(child, pidfd_open):
    child.stdin.close()
    child.wait()
    fd = child_exit_fd(child.pid)
    try:
        assert ended(fd)
    finally:
        os.close(fd)


def test_a_process_already_reaped_has_no_pidfd(child, pidfd_open):
    child.stdin.close()
    child.wait()
    assert pid_exit_fd(child.pid) is None


def test_without_os_pidfd_open_the_system_call_is_made(child, monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    called = []
    syscall = exits._syscall_pidfd_open

    def noting(pid):
        called.append(pid)
        return syscall(pid)

    monkeypatch.setattr(exits, "_syscall_pidfd_open", noting)
    fd = pid_exit_fd(child.pid)
    os.close(fd)
    assert called == [child.pid]


def test_a_process_that_has_yet_to_end_is_running(child):
    assert running(child.pid)


def test_a_stopped_process_is_running(child):
    """Stopped is not ended: a test that asserts a process still runs is
    not fooled by its state."""
    os.kill(child.pid, signal.SIGSTOP)
    try:
        flags = os.WSTOPPED | os.WNOWAIT
        assert os.waitid(os.P_PID, child.pid, flags) is not None
        assert process_state(child.pid) == "T"
        assert running(child.pid)
    finally:
        os.kill(child.pid, signal.SIGCONT)


def test_a_zombie_awaiting_reaping_has_ended(child):
    """A child that has exited but is not yet reaped (#542): kill(pid, 0)
    still finds it, and `running` reads it as ended."""
    fd = child_exit_fd(child.pid)
    try:
        child.stdin.close()
        assert ended(fd)
    finally:
        os.close(fd)
    assert process_state(child.pid) == "Z"
    os.kill(child.pid, 0)
    assert not running(child.pid)
    assert child.returncode is None
    assert child.wait() == 0


def test_a_process_already_reaped_has_ended(child):
    child.stdin.close()
    child.wait()
    assert process_state(child.pid) is None
    assert not running(child.pid)


@pytest.mark.parametrize(
    "state, is_running",
    [
        ("R", True),
        ("S", True),
        ("D", True),
        ("T", True),
        ("t", True),
        ("I", True),
        ("Z", False),
        ("X", False),
        ("x", False),
    ],
)
def test_each_state_reads_as_running_or_ended(monkeypatch, state, is_running):
    """The states of a process being reaped (X, or x on older kernels)
    pass too briefly to catch in a real process, so each state is given."""
    monkeypatch.setattr(exits, "process_state", lambda pid: state)
    assert exits.running(1) is is_running


def test_the_state_follows_a_command_name_holding_parentheses(
    tmp_path, monkeypatch
):
    stat = tmp_path / "stat"
    stat.write_text("4242 (an (odd) S name) Z 1 4242 4242 0 -1\n")
    real = exits.Path

    def at(path):
        return stat if str(path) == "/proc/4242/stat" else real(path)

    monkeypatch.setattr(exits, "Path", at)
    assert process_state(4242) == "Z"
