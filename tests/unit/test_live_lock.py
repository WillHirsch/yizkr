"""The live-stack lock (#253): live test sessions take turns on the one
instance, and a holder that crashed never blocks.

Real processes contend for lock files in temporary folders, so nothing
here touches the real lock ($YIZKR_LIVE_LOCK, by default
~/.cache/yizkr/live-stack.lock). Holders are small scripts around
`hold`. Sessions are real pytest runs of a stack test through the
integration conftest, against a stand-in stack that answers the
conftest's reachability check. A pytest run spends about a second
starting up, so the runs that the session tests look at all start
together, once per module (the `runs` fixture). Every process under test
tries a held lock more often than a real session does.

No test here waits out a time or reads the real clock (#437). A test
waits on what a process prints: that it holds the lock, that an attempt
to take it was refused, that its stack test runs. It cues a process on
stdin, and it waits for a process to exit. A hold() in this process runs
on a fake clock. A bound on any wait is only HANG_GUARD.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.server
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.real_time import real_time_uses
from tests.integration import live_lock
from tests.integration.live_lock import (
    DEFAULT_TIMEOUT,
    LOCK_ENV,
    POLL_INTERVAL,
    RECORD_INTERVAL,
    RECORD_READS,
    TIMEOUT_ENV,
    LiveStackBusy,
    hold,
    lock_path,
    lock_timeout,
)

REPO = Path(__file__).resolve().parents[2]
# Seconds a step may take before the test gives up on it: a bound on a
# wait that an event ends, never a time a test waits out (#437).
# Generous: a broken lock shows itself by what happens, not by being slow.
HANG_GUARD = 60
# how often a process under test tries a lock that another holds
POLL = 0.02
# the variables that name the stdout and stdin a process under test
# started with, kept from a pytest session's output capture for its cues
CUE_OUT = "CUE_OUT_FD"
CUE_IN = "CUE_IN_FD"
# what a process under test prints, on a line of its own, at each attempt
# to take the lock that is refused: each such line shows the lock still
# keeping the process out
REFUSED = "refused"
# what the stack test prints, on a line of its own, as it starts
RUNNING = "stack test running"

# The start of the scripts that take the lock. It keeps the process's
# stdout and stdin for cues, and makes each refused attempt print REFUSED.
CUES = f"""\
import fcntl
import os

from tests.integration import live_lock

live_lock.POLL_INTERVAL = {POLL}
os.environ[{CUE_OUT!r}] = str(os.dup(1))
os.environ[{CUE_IN!r}] = str(os.dup(0))
take = fcntl.flock


def flock(fd, operation):
    try:
        take(fd, operation)
    except BlockingIOError:
        os.write(int(os.environ[{CUE_OUT!r}]), b"\\n{REFUSED}\\n")
        raise


fcntl.flock = flock
"""

# Takes the lock, releases it on the first line from stdin, and exits on
# the second: the process outlives its hold on the lock.
HOLDER = f"""\
{CUES}
import sys

with live_lock.hold(announce=lambda message: print(message, flush=True)):
    print("acquired", flush=True)
    sys.stdin.readline()
print("released", flush=True)
sys.stdin.readline()
"""

# Says that it released the lock, and exits on a line from stdin. As it
# exits it empties the files named in its arguments: a record that
# empties late.
HOLDER_SAYS_RELEASED = """\
import sys
from pathlib import Path

print("released", flush=True)
sys.stdin.readline()
for name in sys.argv[1:]:
    Path(name).write_text("")
"""

# Runs a pytest session, then waits for a line from stdin before it
# exits: the process outlives its session.
SESSION = f"""\
{CUES}
import sys

import pytest

code = pytest.main(sys.argv[1:])
print(f"session ended: {{code}}", flush=True)
sys.stdin.readline()
"""

# A test that uses the stack. It says that it is running, and ends on a
# line from stdin, or fails once stdin closes.
STACK_TEST = f"""\
import os


def test_uses_the_stack(config):
    os.write(int(os.environ[{CUE_OUT!r}]), b"\\n{RUNNING}\\n")
    told = os.read(int(os.environ[{CUE_IN!r}]), 1)
    assert told == b"\\n", "never told to end"
"""

# settings with which Config.load finds no stack
NO_STACK = {
    name: ""
    for name in (
        "WIKIBASE_URL",
        "WIKIBASE_PUBLIC_HOST",
        "MW_ADMIN_NAME",
        "MW_ADMIN_PASS",
    )
}

# Notified whenever a process under test prints a line or ends: each wait
# on what a process prints waits on this.
OUTPUT = threading.Condition()


class Child:
    """A process under test. A thread reads its output as it comes, and
    the process takes cues from the test on stdin."""

    def __init__(self, args: list[str], cwd: Path, env: dict[str, str]):
        self.cwd = cwd
        self.lines: list[str] = []
        # whether its output has reached its end
        self.ended = False
        self.process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.pid = self.process.pid
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.process.stdout:
            with OUTPUT:
                self.lines.append(line)
                OUTPUT.notify_all()
        with OUTPUT:
            self.ended = True
            OUTPUT.notify_all()

    def output(self) -> str:
        with OUTPUT:
            return "".join(self.lines)

    def running(self) -> bool:
        return self.process.poll() is None

    def cue(self) -> None:
        self.process.stdin.write("\n")
        self.process.stdin.flush()

    def wait_for(self, text: str) -> None:
        until(lambda: text in self.output(), f"{text!r}", self)

    def said(self, line: str, times: int = 1) -> bool:
        """Whether the process has printed `line` as a whole line, at
        least `times` times. A cue such as "released" is matched whole, as
        the announcement before it quotes paths and a command line that
        may hold the word."""
        return self.output().splitlines().count(line) >= times

    def wait_for_line(self, line: str, times: int = 1) -> None:
        until(lambda: self.said(line, times), f"the line {line!r}", self)

    def end(self) -> None:
        """Cue the process to exit, and wait until it has."""
        with contextlib.suppress(BrokenPipeError):
            self.cue()
        self.process.wait(timeout=HANG_GUARD)

    def stop(self) -> None:
        if self.running():
            self.process.kill()
        self.process.wait(timeout=HANG_GUARD)
        self.reader.join(timeout=HANG_GUARD)
        assert not self.reader.is_alive(), f"the output of {self.pid} goes on"
        with contextlib.suppress(BrokenPipeError):
            self.process.stdin.close()
        self.process.stdout.close()


def until(
    condition: Callable[[], bool], what: str, child: Child | None = None
) -> None:
    """Wait until `condition()` holds, checking it again each time a
    process under test prints a line or ends. Fail once `child`'s output
    has ended without it holding, or when no process prints a line for
    HANG_GUARD seconds. A process that waits for the lock prints a line
    at each refused attempt, and gives up after HANG_GUARD seconds (its
    $YIZKR_LIVE_LOCK_TIMEOUT), so a wait on it ends then at the latest."""
    with OUTPUT:
        while not condition():
            if child is not None and child.ended:
                break
            if not OUTPUT.wait(HANG_GUARD):
                break
        else:
            return
    output = f"\noutput of pid {child.pid}:\n{child.output()}" if child else ""
    pytest.fail(f"gave up waiting for {what}{output}")


def until_empty(path: Path, what: str, child: Child) -> None:
    """Check that the file at `path` is empty, as a lock record goes with
    the lock that `child` released. A holder empties its record before it
    releases the lock, so the first read should find the file empty. On
    one machine a read across processes once found the record still
    there (#379), with no cause found. So a first read that finds the
    record is warned about, naming the child's output, so a recurrence
    stays visible, and the file is read again once `child` has exited on
    a cue, when all that it wrote has long been written: the record must
    be gone then."""
    if path.read_text() == "":
        return
    warnings.warn(
        f"{what}: {path} was not empty at the first read after the "
        f"release; reading it again once pid {child.pid} has exited\n"
        f"output of pid {child.pid}:\n{child.output()}",
        stacklevel=2,
    )
    child.end()
    if path.read_text() != "":
        pytest.fail(
            f"{what}: {path} still holds a record after pid {child.pid} "
            f"exited\noutput of pid {child.pid}:\n{child.output()}"
        )


class FakeClock:
    """The clocks of a hold() in this process: `monotonic` reads the time
    that only `sleep` moves on, and either is counted.

    It fails the test when it is read READS times while its time stands
    still, or sleeps past LIMIT seconds: a hold() whose wait stopped
    moving the clock on, or never reached its timeout, would otherwise
    spin for ever rather than fail."""

    READS = 50
    LIMIT = 60.0

    def __init__(self) -> None:
        self.time = 0.0
        self.reads = 0
        self.sleeps: list[float] = []
        # called after the nth sleep, keyed by n
        self.after_sleep: dict[int, Callable[[], None]] = {}
        self._still = 0  # reads since the time last moved on

    def monotonic(self) -> float:
        self.reads += 1
        self._still += 1
        if self._still > self.READS:
            pytest.fail("the clock was read over and over, standing still")
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self._still = 0
        self.time += seconds
        if self.time > self.LIMIT:
            pytest.fail(f"slept {self.time:g} s, past {self.LIMIT:g} s")
        self.after_sleep.get(len(self.sleeps), lambda: None)()


def test_the_fake_clock_fails_a_wait_that_stops_moving_it_on():
    clock = FakeClock()
    read, advance = clock.monotonic, clock.sleep
    for _ in range(FakeClock.READS):
        read()
    advance(0)  # a sleep of nothing moves nothing on
    with pytest.raises(pytest.fail.Exception, match="standing still"):
        read()


def test_the_fake_clock_fails_a_wait_past_its_limit():
    advance = FakeClock().sleep
    advance(FakeClock.LIMIT)
    with pytest.raises(pytest.fail.Exception, match="past 60 s"):
        advance(1)


@contextlib.contextmanager
def spawner(root: Path) -> Iterator[Callable[..., Child]]:
    """Start processes, each in a folder of its own under `root`. None of
    them is left running afterwards."""
    children: list[Child] = []

    def start(name: str, args: list[str], env: dict[str, str]) -> Child:
        cwd = root / name
        cwd.mkdir()
        children.append(Child(args, cwd, env))
        return children[-1]

    try:
        yield start
    finally:
        for child in children:
            child.stop()


@contextlib.contextmanager
def stand_in_stack() -> Iterator[str]:
    """A stand-in for the stack at the URL it yields. It answers any GET,
    such as the conftest's reachability check, with an empty JSON
    object."""

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def write_scripts(root: Path) -> None:
    (root / "holder.py").write_text(HOLDER)
    (root / "session.py").write_text(SESSION)
    (root / "test_stack.py").write_text(STACK_TEST)
    (root / "pytest.ini").write_text("[pytest]\n")


def holder_args(root: Path, *args: str) -> list[str]:
    return [sys.executable, str(root / "holder.py"), *args]


def session_args(root: Path, *options: str) -> list[str]:
    """A pytest session of STACK_TEST through the integration conftest,
    loaded as a plugin."""
    return [
        sys.executable,
        str(root / "session.py"),
        str(root / "test_stack.py"),
        "-p",
        "tests.integration.conftest",
        "-c",
        str(root / "pytest.ini"),
        "--rootdir",
        str(root),
        "-p",
        "no:cacheprovider",
        "-q",
        *options,
    ]


def child_env(
    lock: Path, settings: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment of a process under test: this one's without
    pytest's own settings, with `lock` as the live-stack lock."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTEST_")
    }
    env.update(
        {
            "PYTHONPATH": str(REPO),
            LOCK_ENV: str(lock),
            TIMEOUT_ENV: f"{HANG_GUARD:g}",
        }
    )
    env.update(settings or {})
    return env


def session_env(
    root: Path, lock: Path, stack: str, settings: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment of a session that uses the stand-in `stack`."""
    configured = {
        "WIKIBASE_URL": stack,
        "MW_ADMIN_NAME": "tester",
        "MW_ADMIN_PASS": "not-a-password",
        # no env file: the settings here are all there is
        "YIZKR_ENV_FILE": str(root / "absent.env"),
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }
    return child_env(lock, {**configured, **(settings or {})})


def record(lock: Path) -> dict:
    return json.loads(lock.read_text())


@dataclass
class Runs:
    """The pytest runs that the session tests look at."""

    # two sessions of the stack test on one lock, started together
    pair: tuple[Child, Child]
    pair_lock: Path
    # holds its lock throughout, for the next two
    holder: Child
    # a session with no stack configured, while the lock is held
    no_stack: Child
    # sessions that cannot take the lock, by what stops them
    lock_errors: dict[str, Child]
    # pytest of tests/integration, as a default run starts it
    default_run: Child
    default_lock: Path


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runs]:
    """Every pytest run that the session tests look at, started together:
    each spends about a second starting up, and together they spend it
    once."""
    root = tmp_path_factory.mktemp("runs")
    write_scripts(root)
    with stand_in_stack() as stack, spawner(root) as start:
        held_lock = root / "held" / "live-stack.lock"
        holder = start("holder", holder_args(root), child_env(held_lock))
        holder.wait_for_line("acquired")

        pair_lock = root / "pair" / "live-stack.lock"
        pair_a, pair_b = (
            start(
                name, session_args(root), session_env(root, pair_lock, stack)
            )
            for name in ("pair-a", "pair-b")
        )
        no_stack = start(
            "no-stack",
            session_args(root),
            session_env(root, held_lock, stack, NO_STACK),
        )
        # -rN: no short summary, whose line would repeat the message
        refusals = {
            "gives up": {TIMEOUT_ENV: "0"},
            "relative path": {LOCK_ENV: "relative/live-stack.lock"},
            "unknown home": {LOCK_ENV: "~no-such-user/live-stack.lock"},
            "unusable timeout": {TIMEOUT_ENV: "soon"},
        }
        lock_errors = {
            error: start(
                error.replace(" ", "-"),
                session_args(root, "-rN"),
                session_env(root, held_lock, stack, settings),
            )
            for error, settings in refusals.items()
        }
        default_lock = root / "default" / "live-stack.lock"
        default_run = start(
            "default-run",
            [
                sys.executable,
                "-m",
                "pytest",
                str(REPO / "tests" / "integration"),
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            child_env(default_lock),
        )
        yield Runs(
            pair=(pair_a, pair_b),
            pair_lock=pair_lock,
            holder=holder,
            no_stack=no_stack,
            lock_errors=lock_errors,
            default_run=default_run,
            default_lock=default_lock,
        )


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    """A lock file in a folder that does not exist yet. The folder's
    path holds both cues that a holder prints, so the announcement that
    quotes it holds them too: only a cue matched as a whole line
    (Child.said) tells a holder's state."""
    return (
        tmp_path / "released acquired" / "cache" / "yizkr" / "live-stack.lock"
    )


@pytest.fixture
def start_holder(tmp_path: Path, lock: Path) -> Iterator[Callable[..., Child]]:
    write_scripts(tmp_path)
    with spawner(tmp_path) as start:

        def start_holder(
            name: str, *args: str, settings: dict[str, str] | None = None
        ) -> Child:
            return start(
                name, holder_args(tmp_path, *args), child_env(lock, settings)
            )

        yield start_holder


def test_a_live_session_holds_the_lock_from_its_first_stack_test_to_its_end(
    runs,
):
    # the pair started together, and either may have taken the lock
    until(
        lambda: any(session.said(RUNNING) for session in runs.pair),
        "a stack test to run",
    )
    first, second = sorted(runs.pair, key=lambda s: not s.said(RUNNING))
    # the other waits, and says for whom, around pytest's output capture
    second.wait_for(f"held by pid {first.pid} ")
    # each refused attempt shows the lock still keeping it out
    second.wait_for_line(REFUSED, times=3)
    assert not second.said(RUNNING)

    first.cue()  # its stack test ends
    first.wait_for("session ended:")
    assert "session ended: 0" in first.output(), first.output()
    # the end of the session released the lock, not the end of its process
    second.wait_for_line(RUNNING)
    assert first.running()

    second.cue()
    second.wait_for("session ended:")
    assert "session ended: 0" in second.output(), second.output()
    assert second.output().count("is held by") == 1
    # each emptied its record as it released the lock
    until_empty(runs.pair_lock, "the pair's record to go", second)
    with hold(runs.pair_lock, timeout=0):
        pass


def test_a_session_with_no_stack_configured_skips_without_waiting(runs):
    """The config fixture takes the lock only once the session knows
    which stack it uses: a session that cannot use one does not queue."""
    runs.no_stack.wait_for("session ended:")
    output = runs.no_stack.output()
    assert "session ended: 0" in output, output
    assert "1 skipped" in output
    assert "is held by" not in output
    assert runs.holder.running()  # and holds the lock still


@pytest.mark.parametrize(
    "error, message",
    [
        ("gives up", "Gave up after 0 s waiting for the live-stack lock"),
        ("relative path", "is a relative path"),
        ("unknown home", "names a home folder that cannot be found"),
        ("unusable timeout", "is not a number of seconds"),
    ],
)
def test_a_lock_error_is_a_setup_error_with_its_message_once(
    runs, error, message
):
    session = runs.lock_errors[error]
    session.wait_for("session ended:")
    output = session.output()
    assert "session ended: 1" in output, output
    assert "1 error" in output, output
    assert output.count(message) == 1, output
    assert "During handling" not in output, output


def test_a_default_run_never_touches_the_lock(runs):
    """A run without -m live deselects every live test, so nothing makes
    the lock's folder, let alone takes the lock."""
    run = runs.default_run
    returned = run.process.wait(timeout=HANG_GUARD)
    assert returned == pytest.ExitCode.NO_TESTS_COLLECTED, run.output()
    assert "deselected" in run.output()
    assert not runs.default_lock.parent.exists()


def test_a_second_holder_waits_until_the_first_releases_the_lock(
    lock, start_holder
):
    first = start_holder("first")
    first.wait_for_line("acquired")
    assert record(lock)["pid"] == first.pid

    second = start_holder("second")
    second.wait_for(f"held by pid {first.pid} ")
    # each refused attempt shows the lock still keeping it out
    second.wait_for_line(REFUSED, times=3)
    assert not second.said("acquired")

    first.cue()  # release the lock, and stay running
    first.wait_for_line("released")
    second.wait_for_line("acquired")
    assert first.running()
    assert record(lock)["pid"] == second.pid
    # the holder is announced once, however many times the second tried
    assert second.output().count("is held by") == 1

    second.cue()
    second.wait_for_line("released")
    # The record goes with the lock. The holder empties it before it
    # releases the lock and so before it prints "released", as the next
    # test checks step by step, in this process.
    until_empty(lock, "the record to go", second)


def test_the_record_is_emptied_before_the_lock_is_released(lock, monkeypatch):
    """A holder empties its record, then releases the lock, then returns
    from hold(): so no holder that takes the lock next finds the record
    of one that released it, and whatever a holder does after its hold,
    such as announcing the release, comes after both.

    The release watched is the explicit LOCK_UN call. Closing the file
    alone would release the lock as well, and in order, but hold()'s
    order is empty, unlock, close, and a hold() that leaves the release
    to the close fails here on purpose."""
    at_release: list[str] = []
    flock = fcntl.flock

    def watched(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            at_release.append(lock.read_text())
        flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", watched)
    with hold(lock, timeout=0):
        assert record(lock)["pid"] == os.getpid()
        assert at_release == []
    assert at_release == [""]  # released once, with the record emptied
    assert lock.read_text() == ""
    # and the lock is free: another open file description takes it at once
    with hold(lock, timeout=0):
        pass


def test_a_record_that_empties_late_is_warned_about_then_read_again(
    tmp_path,
):
    record_file = tmp_path / "live-stack.lock"
    record_file.write_text("")
    with spawner(tmp_path) as start:
        holder = start(
            "holder",
            [sys.executable, "-c", HOLDER_SAYS_RELEASED, str(record_file)],
            dict(os.environ),
        )
        holder.wait_for_line("released")
        # an empty file passes at the first read, with no warning, and
        # leaves the holder running
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            until_empty(record_file, "the record to go", holder)
        assert holder.running()

        # the holder empties the file as it exits on its cue
        record_file.write_text('{"pid": 1}')
        with pytest.warns(UserWarning) as warned:
            until_empty(record_file, "the record to go", holder)
        assert holder.process.returncode == 0

        # a record still there once the holder has exited fails the test
        stays = start(
            "stays",
            [sys.executable, "-c", HOLDER_SAYS_RELEASED],
            dict(os.environ),
        )
        stays.wait_for_line("released")
        record_file.write_text('{"pid": 1}')
        with (
            pytest.warns(UserWarning),
            pytest.raises(pytest.fail.Exception, match="still holds a record"),
        ):
            until_empty(record_file, "the record to go", stays)
        assert stays.process.returncode == 0
    [warning] = warned
    message = str(warning.message)
    assert message.startswith("the record to go: ")
    # it names the holder and gives its output
    assert f"output of pid {holder.pid}:\nreleased\n" in message


def test_a_holder_killed_with_sigkill_releases_the_lock(lock, start_holder):
    # The first holder's record is the longer, so a record written over it
    # without emptying the file first would not parse.
    first = start_holder("first", "x" * 200)
    first.wait_for_line("acquired")
    second = start_holder("second")
    second.wait_for(f"held by pid {first.pid} ")

    first.process.send_signal(signal.SIGKILL)
    first.process.wait(timeout=HANG_GUARD)
    second.wait_for_line("acquired")
    holder = record(lock)
    assert holder["pid"] == second.pid
    assert holder["cwd"] == str(second.cwd)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    """The fake clock of a hold() in this process, with no real clock to
    fall back on: the module's own `time` refuses to be read or slept on,
    so a hold() that ignored the clock it is given fails."""

    def refuse(*args: object) -> None:
        raise AssertionError("hold() used the real clock")

    monkeypatch.setattr(
        live_lock, "time", SimpleNamespace(monotonic=refuse, sleep=refuse)
    )
    return FakeClock()


@contextlib.contextmanager
def held_here(lock: Path, pid: int) -> Iterator[int]:
    """Hold `lock` through a descriptor of this process's own, which
    hold() here cannot share, with a record that names `pid`."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, json.dumps({"pid": pid, "command": "held"}).encode())
        yield fd
    finally:
        os.close(fd)


def test_giving_up_names_the_holder(lock, start_holder, clock):
    first = start_holder("first", "--an-argument")
    first.wait_for_line("acquired")

    announced: list[str] = []
    with (
        pytest.raises(LiveStackBusy) as gave_up,
        hold(
            lock,
            timeout=1.25,
            announce=announced.append,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ),
    ):
        pytest.fail("took a lock that another process holds")
    # it tried every POLL_INTERVAL, the last wait cut short at the timeout,
    # and gave up once the timeout had passed
    assert POLL_INTERVAL == 0.5
    assert clock.sleeps == [0.5, 0.5, 0.25]
    assert clock.time == 1.25
    message = str(gave_up.value)
    assert message.startswith("Gave up after 1.25 s waiting")
    assert f"held by pid {first.pid} " in message
    assert "holder.py --an-argument" in message  # its command line
    assert f"cwd: {first.cwd}" in message
    assert TIMEOUT_ENV in message
    assert len(announced) == 1
    assert f"held by pid {first.pid} " in announced[0]
    assert "Waiting up to 1.25 s for it" in announced[0]

    # The timeout a process waits comes from $YIZKR_LIVE_LOCK_TIMEOUT: one
    # of 0 gives up at the first refused attempt, where the default
    # (HANG_GUARD here) would outlast the wait for the process to exit.
    second = start_holder("second", settings={TIMEOUT_ENV: "0"})
    assert second.process.wait(timeout=HANG_GUARD) == 1
    assert "LiveStackBusy: Gave up after 0 s" in second.output()


def test_a_holder_that_waited_says_how_long(lock, clock):
    announced: list[str] = []
    with held_here(lock, os.getpid()) as fd:
        # the lock comes free during the second wait
        clock.after_sleep[2] = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        with hold(
            lock,
            timeout=5,
            announce=announced.append,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ):
            assert record(lock)["pid"] == os.getpid()
    assert clock.sleeps == [POLL_INTERVAL, POLL_INTERVAL]
    first, took = announced
    assert f"held by pid {os.getpid()} " in first
    assert took == "Took the live-stack lock after 1 s."


def test_a_free_lock_is_taken_with_no_look_at_the_clock(lock, clock):
    taken = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    announced: list[str] = []
    with hold(
        lock,
        timeout=0,
        announce=announced.append,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: taken,
    ):
        # the record gives the time from `now`
        assert record(lock)["started"] == taken.astimezone().isoformat(
            timespec="seconds"
        )
    assert (clock.reads, clock.sleeps, announced) == (0, [], [])


def test_a_record_of_an_ended_process_is_read_again_before_it_is_named(
    lock, clock
):
    """A process that has just taken the lock writes its record right
    after, so a record that names no running process is read again,
    RECORD_INTERVAL apart, before the holder is named from it."""
    ended = (1 << 22) + 1  # above the kernel's limit on pids: never running
    with (
        held_here(lock, ended),
        pytest.raises(LiveStackBusy) as gave_up,
        hold(
            lock,
            timeout=0,
            announce=lambda message: pytest.fail(message),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ),
    ):
        pytest.fail("took a lock that this process holds")
    assert clock.sleeps == [RECORD_INTERVAL] * (RECORD_READS - 1)
    message = str(gave_up.value)
    assert message.startswith("Gave up after 0 s waiting")
    assert f"pid {ended} has ended" in message


def test_a_record_nested_too_deep_is_read_as_none(lock, clock):
    """A held lock whose record cannot be decoded is waited on, and given
    up on as held by a process with no record, rather than raising out of
    the first refusal (#615)."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, b"[" * 100_000)
        with (
            pytest.raises(LiveStackBusy) as gave_up,
            hold(
                lock,
                timeout=0,
                announce=lambda message: pytest.fail(message),
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ),
        ):
            pytest.fail("took a lock that this process holds")
    finally:
        os.close(fd)
    assert clock.sleeps == [RECORD_INTERVAL] * (RECORD_READS - 1)
    assert "a process with no record in the lock file" in str(gave_up.value)


def test_a_hold_waits_on_the_real_clocks_by_default():
    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(
            hold.__wrapped__
        ).parameters.items()
    }
    assert defaults["monotonic"] is time.monotonic
    assert defaults["sleep"] is time.sleep
    assert defaults["now"] == datetime.now


def test_the_lock_path_and_timeout_come_from_the_environment(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(LOCK_ENV, raising=False)
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    assert lock_path() == tmp_path / ".cache" / "yizkr" / "live-stack.lock"
    assert lock_timeout() == DEFAULT_TIMEOUT == 7200

    monkeypatch.setenv(LOCK_ENV, "~/elsewhere/stack.lock")
    assert lock_path() == tmp_path / "elsewhere" / "stack.lock"
    # a relative path would name another file in each worktree
    monkeypatch.setenv(LOCK_ENV, "var/stack.lock")
    with pytest.raises(ValueError, match=LOCK_ENV):
        lock_path()
    # A home folder that cannot be found is refused as well, naming the
    # setting, rather than raising pathlib's RuntimeError (#330 review).
    monkeypatch.setenv(LOCK_ENV, "~no-such-user/stack.lock")
    with pytest.raises(ValueError, match=f"{LOCK_ENV}='~no-such-user/"):
        lock_path()
    with monkeypatch.context() as patch:
        # no HOME, and no passwd entry either: ~ stays as it is
        patch.delenv(LOCK_ENV)
        patch.delenv("HOME")
        patch.setattr(os.path, "expanduser", lambda path: path)
        with pytest.raises(ValueError, match=f"{LOCK_ENV} is not set"):
            lock_path()
    monkeypatch.setenv(TIMEOUT_ENV, "2.5")
    assert lock_timeout() == 2.5
    for unusable in ("soon", "-1", "nan"):
        monkeypatch.setenv(TIMEOUT_ENV, unusable)
        with pytest.raises(ValueError, match=TIMEOUT_ENV):
            lock_timeout()


def test_no_test_here_waits_on_or_reads_the_real_clock():
    """No test in this file sleeps, reads a real clock or waits out a
    time (#437): the processes under test cue the tests through what they
    print, a hold() here runs on FakeClock, and a wait for a process, a
    thread or a line is bounded only by HANG_GUARD."""
    source = Path(__file__).read_text(encoding="utf-8")
    # the one exception names the real clocks, to check the defaults by
    # identity, and calls none of them
    pin = "test_a_hold_waits_on_the_real_clocks_by_default"
    assert (
        real_time_uses(source, exempt={pin}, hang_guards={"HANG_GUARD"}) == []
    )
    assert {
        use.split(": ", 1)[1]
        for use in real_time_uses(source, hang_guards={"HANG_GUARD"})
    } == {"monotonic uncalled", "sleep uncalled", "now uncalled"}
