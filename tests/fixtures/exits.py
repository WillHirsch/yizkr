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
"""

from __future__ import annotations

import ctypes
import errno
import os
import threading

import pytest

# pidfd_open's number in Linux's system call table, the same on every
# architecture
SYS_PIDFD_OPEN = 434


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
        if error == errno.ESRCH:
            raise ProcessLookupError(error, os.strerror(error))
        if error in (errno.ENOSYS, errno.EPERM):
            pytest.skip("no pidfd_open(2) in this kernel")
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
