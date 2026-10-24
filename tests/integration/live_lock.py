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

As the child imports this module, which conftest.py does before any
test runs, the module makes the descriptor close-on-exec, as the one
that hold() opens is. So no program that the child starts shares the
hold in turn, not even one that a test starts before any test uses the
stack; a process that the child forks without exec does, as with any
holder's descriptor. The import does nothing else: it locks nothing, and
it leaves the descriptor alone unless it is open on the lock file
itself, so a stray variable naming another descriptor, such as stdout,
changes nothing. hold() makes the shared descriptor close-on-exec too,
for a process that imported the module before the variable was set.

A holder killed while a child shares its hold leaves the lock held until
the child, and every process that it forked without exec, has ended. For
a harness ended by a signal that it does not catch, such as SIGKILL, that
is its live run's pytest process, which nothing then stops at the
harness's --timeout, and any process that a test forked from it. (On
SIGINT, SIGTERM or SIGHUP the harness stops the run itself.) A waiting
process names the holder from its record, and says that it has ended
and that the lock is held by a process with no record of its own.

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
    """$YIZKR_LIVE_LOCK, else ~/.cache/yizkr/live-stack.lock. A setting
    that names no usable lock raises ValueError, naming the setting: a
    relative path, or a home folder that cannot be found."""
    configured = os.environ.get(LOCK_ENV)
    try:
        if not configured:
            return Path.home() / ".cache" / "yizkr" / "live-stack.lock"
        path = Path(configured).expanduser()
    except RuntimeError:  # pathlib's error for a ~ that it cannot expand
        if not configured:
            raise ValueError(
                f"{LOCK_ENV} is not set, and the home folder that holds the "
                f"default lock cannot be found: set {LOCK_ENV} to an "
                "absolute path"
            ) from None
        raise ValueError(
            f"{LOCK_ENV}={configured!r} names a home folder that cannot be "
            "found"
        ) from None
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
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
) -> Iterator[int]:
    """Hold the live-stack lock for the duration of the block, and give
    the block the descriptor that holds it.

    `path` and `timeout` default to lock_path() and lock_timeout(). While
    another process holds the lock, `announce` (by default, a print to
    stderr) is told who holds it, once, and the lock is tried again every
    POLL_INTERVAL seconds. After `timeout` seconds LiveStackBusy is
    raised, naming the holder. Once this process has the lock, it writes
    its record over the one in the file, and it empties the record again
    as it releases the lock. `monotonic` and `sleep` time the wait, and
    `now` gives the time in the record: by default, the real clocks.

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
        _take(fd, path, timeout, announce, monotonic, sleep)
        try:
            _record(fd, now)
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
    (a $YIZKR_LIVE_LOCK that is relative or names a home folder that
    cannot be found, or a $YIZKR_LIVE_LOCK_TIMEOUT that is not a number
    of seconds), that test and every later one that uses the stack error
    at setup, each with the message alone and no traceback, so the
    message appears once per such test. pytest keeps a session fixture's
    error for the whole session, so the session waits only once."""
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
    fd: int,
    path: Path,
    timeout: float,
    announce: Callable[[str], None],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Lock `fd` exclusively, waiting up to `timeout` seconds from the
    first attempt that is refused: a free lock is taken with no look at
    the clock."""
    started: float | None = None
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            if started is not None:
                waited = monotonic() - started
                announce(f"Took the live-stack lock after {waited:.0f} s.")
            return
        first_refusal = started is None
        if first_refusal:
            started = monotonic()
        waited = monotonic() - started
        if waited >= timeout:
            raise LiveStackBusy(
                f"Gave up after {timeout:g} s waiting for the live-stack "
                f"lock {path}, held by {_describe(_holder(fd, sleep))}\n"
                f"Wait for that session to end, or give {TIMEOUT_ENV} "
                "more seconds."
            )
        if first_refusal:
            announce(
                f"The live-stack lock {path} is held by "
                f"{_describe(_holder(fd, sleep))}\n"
                f"Waiting up to {timeout:g} s for it ({TIMEOUT_ENV})."
            )
        sleep(min(POLL_INTERVAL, timeout - waited))


def _shared(path: Path) -> int | None:
    """The descriptor that $YIZKR_LIVE_LOCK_FD names, when this process
    holds the lock through it: the descriptor is open on the lock file at
    `path`, and flock on it succeeds at once. Otherwise None.

    The descriptor is then made close-on-exec, as the one that hold()
    opens is, so that a program this process starts (os.system, a shell's
    background job, a subprocess with close_fds=False) does not share the
    hold and keep the lock after this process ends. The import of this
    module has done so already, unless the variable was set after it."""
    fd = _named(path)
    if fd is None:
        return None
    # On the description that holds the lock this changes nothing, and on
    # any other it fails while that one holds the lock. A free lock is
    # taken through this descriptor.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    os.set_inheritable(fd, False)
    return fd


def _named(path: Path) -> int | None:
    """The descriptor that $YIZKR_LIVE_LOCK_FD names, when it is open in
    this process on the lock file at `path`. Otherwise None."""
    try:
        fd = int(os.environ.get(SHARED_ENV, ""))
        inherited = os.fstat(fd)
        lock = os.stat(path)
    except (ValueError, OverflowError, OSError):
        return None
    # A descriptor of any other file would lock that file instead, or, made
    # close-on-exec, be kept from programs that inherit it, stdout say.
    if (inherited.st_dev, inherited.st_ino) != (lock.st_dev, lock.st_ino):
        return None
    return fd


def _close_on_exec() -> None:
    """Make the descriptor that $YIZKR_LIVE_LOCK_FD names close-on-exec,
    when it is open on the lock file: run as this module is imported
    (module docstring). A holder passes its descriptor down open across
    exec, so it is inheritable until then. Nothing is locked, and a lock
    path that lock_path() refuses is left for hold() to raise, as a
    session takes the lock."""
    try:
        fd = _named(lock_path())
    except ValueError:
        return
    if fd is not None:
        os.set_inheritable(fd, False)


def _record(fd: int, now: Callable[[], datetime]) -> None:
    """Write this process's record over the one in the lock file, with
    the time from `now`."""
    record = {
        "pid": os.getpid(),
        "command": shlex.join(sys.orig_argv),
        "cwd": os.getcwd(),
        "started": now().astimezone().isoformat(timespec="seconds"),
    }
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps(record).encode() + b"\n", 0)


def _holder(fd: int, sleep: Callable[[float], None]) -> dict[str, Any] | None:
    """The record in the lock file open as `fd`: the first one read that
    names a running process or, when no read finds one, the last record
    read, the reads `sleep`ing RECORD_INTERVAL apart. None when no read
    finds a record, as when each is nested too deep to decode."""
    found = None
    for attempt in range(RECORD_READS):
        if attempt:
            sleep(RECORD_INTERVAL)
        try:
            record = json.loads(os.pread(fd, 1 << 20, 0))
        except (ValueError, RecursionError):
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
        # The recorded holder has ended, yet the lock is held, by a
        # process that has not recorded itself: one that shared the hold
        # of a holder killed since, one that has taken the lock and not
        # yet written its record over one left by a crash, or one that
        # took the lock through a descriptor named in $YIZKR_LIVE_LOCK_FD
        # while it was free.
        described += (
            f"\n  pid {pid} has ended; the lock is held by a process with "
            "no record of its own, such as a killed mutation harness's "
            "live run"
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


# In a live session, before any test runs: conftest.py imports this module.
_close_on_exec()
