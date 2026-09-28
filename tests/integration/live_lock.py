"""The live-stack lock: live test sessions take turns on the one instance.

A live session that uses the stack holds an exclusive lock (flock) on the
file ``$YIZKR_LIVE_LOCK`` (default ``~/.cache/yizkr/live-stack.lock``)
from its first stack test to its end: conftest.py's ``live_stack_lock``
fixture takes it. A session that finds the lock held prints who holds it,
once, and waits. It gives up after ``$YIZKR_LIVE_LOCK_TIMEOUT`` seconds
(default 7200) with an error that names the holder.

The holder records itself in the lock file: its pid, command line,
working directory and the time it took the lock. The kernel releases a
flock once every descriptor of the open file description that holds it
is closed, as a process's descriptors are when it ends, however it ends.
So a holder that crashed or was killed blocks nothing, unless it shared
its hold (below). A holder that releases the lock itself empties its
record first, so a record in a free lock file was left by a holder that
crashed or was killed; the next holder writes its own over it.

A process that a holder starts can share its hold, as the pytest runs of
scripts/mutate.py share the harness's. The holder passes its locked
descriptor down, open, and names it in ``$YIZKR_LIVE_LOCK_FD``. A flock
belongs to the open file description, which the two processes then
share, so the child holds the lock through that descriptor: it neither
waits nor records itself, and it never releases the lock, which would
release the holder's too. The variable counts only when it names a
descriptor, open in the process, of the lock file itself, and flock on
that descriptor succeeds at once, as it does only while the descriptor's
open file description holds the lock or nothing does. Otherwise the
process takes the lock as any other does. So even a stray or stale
variable never lets two sessions hold the lock at once.

As the child's hold() finds the descriptor, it makes it close-on-exec,
as the one that hold() opens is, so that no program the child starts
from then on shares the hold in turn; a process it forks without exec
does, as with any holder's descriptor. A holder killed while a child
shares its hold leaves the lock held until the child ends: a killed
harness's live run keeps it until the run ends. A waiting process then
names the holder from its record, and says that it has ended and that a
process sharing its hold holds the lock.

Never delete the lock file. A session that then found no file would
create and lock a new one, while the session still holding the old one
went on: two sessions on the stack at once.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import shlex
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

LOCK_ENV = "YIZKR_LIVE_LOCK"
TIMEOUT_ENV = "YIZKR_LIVE_LOCK_TIMEOUT"
# names the descriptor through which a process shares its parent's hold
SHARED_ENV = "YIZKR_LIVE_LOCK_FD"
DEFAULT_TIMEOUT = 7200.0
# seconds between attempts to take the lock while another process holds it
POLL_INTERVAL = 0.5
# A process that has just taken the lock writes its record right after, so
# a waiting process reads the record a few times, a moment apart, while it
# names no running process.
RECORD_READS = 10
RECORD_INTERVAL = 0.05


class LiveStackBusy(TimeoutError):
    """Another session held the live-stack lock for longer than this one
    would wait."""


def lock_path() -> Path:
    """$YIZKR_LIVE_LOCK, else ~/.cache/yizkr/live-stack.lock."""
    configured = os.environ.get(LOCK_ENV)
    if not configured:
        return Path.home() / ".cache" / "yizkr" / "live-stack.lock"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"{LOCK_ENV}={configured!r} is a relative path: sessions in "
            "other working directories would lock other files"
        )
    return path


def lock_timeout() -> float:
    """$YIZKR_LIVE_LOCK_TIMEOUT in seconds, else DEFAULT_TIMEOUT."""
    configured = os.environ.get(TIMEOUT_ENV, "").strip()
    if not configured:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(configured)
    except ValueError:
        seconds = math.nan
    if not seconds >= 0:  # refuses nan as well as negative numbers
        raise ValueError(
            f"{TIMEOUT_ENV}={configured!r} is not a number of seconds "
            "(0 or more) to wait for the live-stack lock"
        )
    return seconds


@contextlib.contextmanager
def hold(
    path: Path | None = None,
    timeout: float | None = None,
    announce: Callable[[str], None] | None = None,
) -> Iterator[int]:
    """Hold the live-stack lock for the duration of the block, and give
    the block the descriptor that holds it.

    `path` and `timeout` default to lock_path() and lock_timeout(). While
    another process holds the lock, `announce` (by default, a print to
    stderr) is told who holds it, once, and the lock is tried again every
    POLL_INTERVAL seconds. After `timeout` seconds LiveStackBusy is
    raised, naming the holder. Once this process has the lock, it writes
    its record over the one in the file, and it empties the record again
    as it releases the lock.

    A process that shares its parent's hold ($YIZKR_LIVE_LOCK_FD, in the
    module docstring) gets the shared descriptor at once. It writes no
    record, and it leaves the descriptor open and locked: releasing it
    would release the parent's lock too."""
    path = lock_path() if path is None else path
    timeout = lock_timeout() if timeout is None else timeout
    announce = announce or _print_to_stderr
    shared = _shared(path)
    if shared is not None:
        yield shared
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # opened without truncating: the record in the file is the holder's
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _take(fd, path, timeout, announce)
        try:
            _record(fd)
            yield fd
        finally:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold_for_session(pytest_config: pytest.Config) -> Iterator[None]:
    """hold(), for the live_stack_lock fixture of a pytest session.

    The fixture runs while the session's first stack test sets up, when
    pytest captures output, so the messages go to the terminal around the
    capture. If the lock cannot be taken in time, or a setting is refused
    (a relative $YIZKR_LIVE_LOCK, or a $YIZKR_LIVE_LOCK_TIMEOUT that is not
    a number of seconds), that test and every later one that uses the
    stack error at setup, each with the message alone and no traceback, so
    the message appears once per such test. pytest keeps a session
    fixture's error for the whole session, so the session waits only
    once."""
    # imported here, so that processes that only hold the lock need not
    # load pytest
    import pytest

    with contextlib.ExitStack() as held:
        try:
            held.enter_context(hold(announce=_terminal(pytest_config)))
        except (LiveStackBusy, ValueError) as exc:
            raise pytest.fail.Exception(str(exc), pytrace=False) from None
        yield


def _take(
    fd: int, path: Path, timeout: float, announce: Callable[[str], None]
) -> None:
    """Lock `fd` exclusively, waiting up to `timeout` seconds."""
    started = time.monotonic()
    waiting = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            if waiting:
                waited = time.monotonic() - started
                announce(f"Took the live-stack lock after {waited:.0f} s.")
            return
        waited = time.monotonic() - started
        if waited >= timeout:
            raise LiveStackBusy(
                f"Gave up after {timeout:g} s waiting for the live-stack "
                f"lock {path}, held by {_describe(_holder(fd))}\n"
                f"Wait for that session to end, or give {TIMEOUT_ENV} "
                "more seconds."
            )
        if not waiting:
            announce(
                f"The live-stack lock {path} is held by "
                f"{_describe(_holder(fd))}\n"
                f"Waiting up to {timeout:g} s for it ({TIMEOUT_ENV})."
            )
            waiting = True
        time.sleep(min(POLL_INTERVAL, timeout - waited))


def _shared(path: Path) -> int | None:
    """The descriptor that $YIZKR_LIVE_LOCK_FD names, when this process
    holds the lock through it: the descriptor is open on the lock file at
    `path`, and flock on it succeeds at once. Otherwise None.

    The descriptor is then made close-on-exec, as the one that hold()
    opens is, so that a program this process starts (os.system, a shell's
    background job, a subprocess with close_fds=False) does not share the
    hold and keep the lock after this process ends."""
    try:
        fd = int(os.environ.get(SHARED_ENV, ""))
        inherited = os.fstat(fd)
        lock = os.stat(path)
    except (ValueError, OverflowError, OSError):
        return None
    # A descriptor of any other file would lock that file instead.
    if (inherited.st_dev, inherited.st_ino) != (lock.st_dev, lock.st_ino):
        return None
    # On the description that holds the lock this changes nothing, and on
    # any other it fails while that one holds the lock. A free lock is
    # taken through this descriptor.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    # The holder passed it down open across exec, so it is inheritable.
    os.set_inheritable(fd, False)
    return fd


def _record(fd: int) -> None:
    """Write this process's record over the one in the lock file."""
    record = {
        "pid": os.getpid(),
        "command": shlex.join(sys.orig_argv),
        "cwd": os.getcwd(),
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps(record).encode() + b"\n", 0)


def _holder(fd: int) -> dict[str, Any] | None:
    """The record in the lock file open as `fd`: the first one read that
    names a running process or, when no read finds one, the last record
    read. None when no read finds a record."""
    found = None
    for attempt in range(RECORD_READS):
        if attempt:
            time.sleep(RECORD_INTERVAL)
        try:
            record = json.loads(os.pread(fd, 1 << 20, 0))
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if _running(record.get("pid")):
            return record
        found = record
    return found


def _running(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # running, as another user
        return True
    return True


def _describe(record: dict[str, Any] | None) -> str:
    """Who a record names, for a message about the lock while it is
    held."""
    if record is None:
        return "a process with no record in the lock file"
    pid = record.get("pid")
    described = (
        f"pid {pid} since {record.get('started')}\n"
        f"  command: {record.get('command')}\n"
        f"  cwd: {record.get('cwd')}"
    )
    if not _running(pid):
        # The recorded holder has ended, yet the lock is held: a process
        # that shared its hold holds it.
        described += (
            f"\n  pid {pid} has ended, and a process that shares its hold "
            "holds the lock: a killed mutation harness's live run, say, "
            "which keeps it until the run ends"
        )
    return described


def _terminal(pytest_config: pytest.Config) -> Callable[[str], None]:
    """A print to a pytest session's terminal, around its output
    capture."""
    plugins = pytest_config.pluginmanager
    capture = plugins.getplugin("capturemanager")
    reporter = plugins.getplugin("terminalreporter")

    def write(message: str) -> None:
        with (
            capture.global_and_fixture_disabled()
            if capture is not None
            else contextlib.nullcontext()
        ):
            if reporter is None:
                _print_to_stderr(message)
            else:
                reporter.write_line(message)
                reporter.flush()

    return write


def _print_to_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
