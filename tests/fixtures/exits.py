"""Waiting for a process to end on an event, never on a time (#437): each
helper gives a file descriptor that turns readable once the process has
ended, for a test to select() on, bounded only by its hang guard.

`child_exit_fd` serves a child of this process, wherever it runs: a
thread waits in waitid(2) with WNOWAIT, which leaves the child unreaped,
and closes a pipe's write end once the child has exited.

`pid_exit_fd` serves any process, a grandchild say, through a pidfd:
`os.pidfd_open` where this Python has it, else the pidfd_open(2) system
call itself, which Linux has had since 5.3 whether or not the Python
build wraps it. A test skips only where the kernel has no pidfd.

`running` says whether a process, by pid, has yet to end, from its state
in /proc: one that has ended but awaits reaping (Z), or is being reaped
(X), has ended, though kill(pid, 0) would still find it (#542). Where a
pidfd has turned readable, the process has ended, and a test trusts that
rather than asking `running` again.
"""

from __future__ import annotations

import ctypes
import errno
import os
import threading
from pathlib import Path

import pytest

# pidfd_open's number in Linux's system call table, the same on every
# architecture
SYS_PIDFD_OPEN = 434

# the states in /proc/<pid>/stat of a process that has ended: a zombie
# awaiting reaping, and one being reaped (dead, upper or lower case by
# kernel version)
ENDED_STATES = frozenset("ZXx")


def child_exit_fd(pid: int) -> int:
    """A descriptor that turns readable (at EOF) once `pid`, a child of
    this process, has exited, leaving it unreaped; the caller closes it.
    A child already reaped counts as exited."""
    read, write = os.pipe()

    def wait() -> None:
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            pass
        finally:
            os.close(write)

    threading.Thread(target=wait, daemon=True).start()
    return read


def _syscall_pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(SYS_PIDFD_OPEN, pid, 0)
    if fd < 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EPERM):
            pytest.skip("no pidfd_open(2) in this kernel")
        # OSError picks its subclass by errno: ESRCH, a process that has
        # ended and been reaped, is ProcessLookupError
        raise OSError(error, os.strerror(error))
    return fd


def pid_exit_fd(pid: int) -> int | None:
    """A pidfd of `pid`, any process, which turns readable once it ends,
    as a zombie awaiting reaping or reaped; None when it has ended
    already. The caller closes it."""
    pidfd_open = getattr(os, "pidfd_open", None) or _syscall_pidfd_open
    try:
        return pidfd_open(pid)
    except ProcessLookupError:
        return None


def process_state(pid: int) -> str | None:
    """The one-letter state of `pid` in /proc/<pid>/stat, None once it is
    gone. Linux only: a test skips where there is no /proc."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("no /proc/<pid>/stat on this system")
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # the command name, in parentheses, may itself hold ") " and spaces
    return stat.rsplit(")", 1)[1].split()[0]


def running(pid: int) -> bool:
    """Whether `pid` is a process that has yet to end, stopped or not; one
    that has ended, reaped or not, is not."""
    state = process_state(pid)
    return state is not None and state not in ENDED_STATES
