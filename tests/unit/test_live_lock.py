"""The live-stack lock (#253): live test sessions take turns on the one
instance, and a holder that crashed never blocks.

Real processes contend for a lock file under tmp_path, so nothing here
touches the real lock ($YIZKR_LIVE_LOCK, by default
~/.cache/yizkr/live-stack.lock). Holders are small scripts around
`hold`. Sessions are real pytest runs of a stack test through the
integration conftest, against a stand-in stack that answers the
conftest's reachability check.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.integration.live_lock import (
    DEFAULT_TIMEOUT,
    LOCK_ENV,
    TIMEOUT_ENV,
    LiveStackBusy,
    hold,
    lock_path,
    lock_timeout,
)

REPO = Path(__file__).resolve().parents[2]
# Seconds a step may take before the test gives up on it. Generous: a
# broken lock shows itself by what happens, not by being slow.
PATIENCE = 30.0

# Takes the lock, releases it on the first line from stdin, and exits on
# the second: the process outlives its hold on the lock.
HOLDER = """\
import sys

from tests.integration.live_lock import hold

with hold(announce=lambda message: print(message, flush=True)):
    print("acquired", flush=True)
    sys.stdin.readline()
print("released", flush=True)
sys.stdin.readline()
"""

# Runs a pytest session, then waits for a line from stdin before it
# exits: the process outlives its session.
SESSION = """\
import sys

import pytest

code = pytest.main(sys.argv[1:])
print(f"session ended: {code}", flush=True)
sys.stdin.readline()
"""

# A test that uses the stack. It says that it is running with a file in
# its working directory, and ends once another file appears there.
STACK_TEST = """\
import time
from pathlib import Path


def test_uses_the_stack(config):
    Path("running").touch()
    deadline = time.monotonic() + 60
    while not Path("go").exists():
        assert time.monotonic() < deadline, "never told to end"
        time.sleep(0.05)
"""


class Child:
    """A process under test. Its output goes to a file that the test
    reads while it runs, and it takes cues from the test on stdin."""

    def __init__(self, args: list[str], cwd: Path, env: dict[str, str]):
        self.cwd = cwd
        self.log = cwd / "output.txt"
        with self.log.open("w") as log:
            self.process = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.pid = self.process.pid

    def output(self) -> str:
        return self.log.read_text()

    def running(self) -> bool:
        return self.process.poll() is None

    def cue(self) -> None:
        self.process.stdin.write("\n")
        self.process.stdin.flush()

    def wait_for(self, text: str) -> None:
        until(lambda: text in self.output(), f"{text!r}", self)

    def stop(self) -> None:
        if self.running():
            self.process.kill()
        self.process.wait(timeout=PATIENCE)
        with contextlib.suppress(BrokenPipeError):
            self.process.stdin.close()


def until(
    condition: Callable[[], bool], what: str, child: Child | None = None
) -> None:
    """Wait until `condition()` holds. Fail when PATIENCE runs out, or
    once `child` has ended without it holding."""
    deadline = time.monotonic() + PATIENCE
    while True:
        ended = child is not None and not child.running()
        if condition():
            return
        if ended or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    output = f"\noutput of pid {child.pid}:\n{child.output()}" if child else ""
    pytest.fail(f"gave up waiting for {what}{output}")


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
            TIMEOUT_ENV: f"{PATIENCE:g}",
        }
    )
    env.update(settings or {})
    return env


def record(lock: Path) -> dict:
    return json.loads(lock.read_text())


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    """A lock file in a folder that does not exist yet."""
    return tmp_path / "cache" / "yizkr" / "live-stack.lock"


@pytest.fixture
def spawn(tmp_path: Path) -> Iterator[Callable[..., Child]]:
    """Start processes, each in a folder of its own. The test leaves
    none of them running."""
    children: list[Child] = []

    def start(name: str, args: list[str], env: dict[str, str]) -> Child:
        cwd = tmp_path / name
        cwd.mkdir()
        children.append(Child(args, cwd, env))
        return children[-1]

    yield start
    for child in children:
        child.stop()


@pytest.fixture
def start_holder(tmp_path: Path, spawn, lock: Path):
    script = tmp_path / "holder.py"
    script.write_text(HOLDER)

    def start(
        name: str, *args: str, settings: dict[str, str] | None = None
    ) -> Child:
        return spawn(
            name,
            [sys.executable, str(script), *args],
            child_env(lock, settings),
        )

    return start


@pytest.fixture
def stack() -> Iterator[str]:
    """A stand-in for the stack. It answers any GET, such as the
    conftest's reachability check, with an empty JSON object."""

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


@pytest.fixture
def start_session(tmp_path: Path, spawn, lock: Path, stack: str):
    """Start a pytest session of STACK_TEST through the integration
    conftest, configured for the stand-in stack (or, with `settings`,
    otherwise)."""
    script = tmp_path / "session.py"
    script.write_text(SESSION)
    test_file = tmp_path / "test_stack.py"
    test_file.write_text(STACK_TEST)
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\n")
    configured = {
        "WIKIBASE_URL": stack,
        "MW_ADMIN_NAME": "tester",
        "MW_ADMIN_PASS": "not-a-password",
        # no env file: the settings here are all there is
        "YIZKR_ENV_FILE": str(tmp_path / "absent.env"),
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }

    def start(name: str, settings: dict[str, str] | None = None) -> Child:
        return spawn(
            name,
            [
                sys.executable,
                str(script),
                str(test_file),
                # the integration conftest, as a plugin
                "-p",
                "tests.integration.conftest",
                "-c",
                str(ini),
                "--rootdir",
                str(tmp_path),
                "-p",
                "no:cacheprovider",
                "-q",
            ],
            child_env(lock, {**configured, **(settings or {})}),
        )

    return start


def test_a_second_holder_waits_until_the_first_releases_the_lock(
    lock, start_holder
):
    first = start_holder("first")
    first.wait_for("acquired")
    assert record(lock)["pid"] == first.pid

    second = start_holder("second")
    second.wait_for(f"held by pid {first.pid} ")
    time.sleep(1)  # time enough for a broken lock to let the second in
    assert "acquired" not in second.output()

    first.cue()  # release the lock, and stay running
    first.wait_for("released")
    second.wait_for("acquired")
    assert first.running()
    assert record(lock)["pid"] == second.pid
    # the holder is announced once, however many times the second tried
    assert second.output().count("is held by") == 1


def test_a_holder_killed_with_sigkill_releases_the_lock(lock, start_holder):
    # The first holder's record is the longer, so a record written over it
    # without emptying the file first would not parse.
    first = start_holder("first", "x" * 200)
    first.wait_for("acquired")
    second = start_holder("second")
    second.wait_for(f"held by pid {first.pid} ")

    first.process.send_signal(signal.SIGKILL)
    first.process.wait(timeout=PATIENCE)
    second.wait_for("acquired")
    holder = record(lock)
    assert holder["pid"] == second.pid
    assert holder["cwd"] == str(second.cwd)


def test_giving_up_names_the_holder(lock, start_holder):
    first = start_holder("first", "--an-argument")
    first.wait_for("acquired")

    announced: list[str] = []
    started = time.monotonic()
    with (
        pytest.raises(LiveStackBusy) as gave_up,
        hold(lock, timeout=1, announce=announced.append),
    ):
        pytest.fail("took a lock that another process holds")
    assert 1 <= time.monotonic() - started < PATIENCE
    message = str(gave_up.value)
    assert f"held by pid {first.pid} " in message
    assert "holder.py --an-argument" in message  # its command line
    assert f"cwd: {first.cwd}" in message
    assert TIMEOUT_ENV in message
    assert len(announced) == 1
    assert f"held by pid {first.pid} " in announced[0]

    # the timeout a process waits comes from $YIZKR_LIVE_LOCK_TIMEOUT
    second = start_holder("second", settings={TIMEOUT_ENV: "1"})
    assert second.process.wait(timeout=PATIENCE) == 1
    assert "LiveStackBusy: Gave up after 1 s" in second.output()


def test_a_default_run_never_touches_the_lock(tmp_path, lock):
    """A run without -m live deselects every live test, so nothing makes
    the lock's folder, let alone takes the lock."""
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=child_env(lock),
        capture_output=True,
        text=True,
        timeout=4 * PATIENCE,
    )
    assert run.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, (
        run.stdout + run.stderr
    )
    assert "deselected" in run.stdout
    assert not (tmp_path / "cache").exists()


def test_a_live_session_holds_the_lock_from_its_first_stack_test_to_its_end(
    lock, start_session
):
    first = start_session("first")
    until((first.cwd / "running").exists, "the first stack test", first)

    second = start_session("second")
    # announced on the terminal, around pytest's output capture
    second.wait_for(f"held by pid {first.pid} ")
    time.sleep(1)  # time enough for a broken lock to let the second in
    assert not (second.cwd / "running").exists()

    (first.cwd / "go").touch()
    first.wait_for("session ended:")
    assert "session ended: 0" in first.output(), first.output()
    # the end of the session released the lock, not the end of its process
    until((second.cwd / "running").exists, "the second stack test", second)
    assert first.running()

    (second.cwd / "go").touch()
    second.wait_for("session ended:")
    assert "session ended: 0" in second.output(), second.output()
    assert second.output().count("is held by") == 1
    with hold(lock, timeout=0):
        pass  # free again


def test_a_session_with_no_stack_configured_skips_without_waiting(
    lock, start_holder, start_session
):
    """The config fixture takes the lock only once the session knows
    which stack it uses: a session that cannot use one does not queue."""
    holder = start_holder("holder")
    holder.wait_for("acquired")
    unconfigured = {
        name: ""
        for name in (
            "WIKIBASE_URL",
            "WIKIBASE_PUBLIC_HOST",
            "MW_ADMIN_NAME",
            "MW_ADMIN_PASS",
        )
    }
    session = start_session("session", settings=unconfigured)
    session.wait_for("session ended:")
    output = session.output()
    assert "session ended: 0" in output, output
    assert "1 skipped" in output
    assert "is held by" not in output


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
    monkeypatch.setenv(TIMEOUT_ENV, "2.5")
    assert lock_timeout() == 2.5
    for unusable in ("soon", "-1", "nan"):
        monkeypatch.setenv(TIMEOUT_ENV, unusable)
        with pytest.raises(ValueError, match=TIMEOUT_ENV):
            lock_timeout()
