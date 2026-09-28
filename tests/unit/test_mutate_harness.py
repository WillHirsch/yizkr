"""The mutation harness, scripts/mutate.py (#250).

The end-to-end tests copy a small git repository holding a tiny module
and its tests, then run the harness on it the way an agent would: as a
subprocess, from inside the repository. The harness in turn runs pytest
there. Each of those pytest runs costs a few tenths of a second, so the
tests share runs where they can, and check the rules that pick results,
exit statuses and the mutations to run in-process. The module is
neutral arithmetic, and nothing touches the network or the live stack.
"""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mutate.py"
_spec = importlib.util.spec_from_file_location("mutate_harness", SCRIPT)
mutate = importlib.util.module_from_spec(_spec)
sys.modules["mutate_harness"] = mutate
_spec.loader.exec_module(mutate)

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Example Agent",
    "GIT_AUTHOR_EMAIL": "agent@example.invalid",
    "GIT_COMMITTER_NAME": "Example Agent",
    "GIT_COMMITTER_EMAIL": "agent@example.invalid",
}
# calc.py's modification time: far from any test run's, so a restore
# that loses it cannot pass by accident (2001-09-09)
STAMP = 1_000_000_000 * 10**9

CALC = '''\
"""A tiny module for the mutation harness's tests."""

import os
import signal
import subprocess
import sys
import time


def add(a, b):
    return a + b


def double(n):
    return n * 2


def hang(stubborn=False):
    """Start two processes, note their pids and pytest's, then block for
    far longer than any test waits. One is a Python grandchild. The other
    is a shell's background job, which ignores SIGINT, as POSIX has it.

    A stubborn hang ignores SIGINT itself, and so does its grandchild."""
    if stubborn:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    job = subprocess.run(
        ["sh", "-c", "sleep 10 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pidfile = os.environ.get("CALC_PIDFILE")
    if pidfile:
        with open(pidfile + ".tmp", "w") as out:
            out.write(f"{os.getpid()} {child.pid} {job.stdout.strip()}")
        os.replace(pidfile + ".tmp", pidfile)
    time.sleep(10)
'''

TEST_CALC = """\
from calc import add


def test_add():
    assert add(2, 3) == 5
"""

TEST_BYTECODE = '''\
import importlib.util
import py_compile
import sys

import calc


def test_no_bytecode_is_written():
    assert sys.dont_write_bytecode
    assert sys.pycache_prefix is None


def test_compile_calc():
    """Cache bytecode compiled from calc.py as it is now, in the
    hash-based form that Python loads without reading the source."""
    py_compile.compile(
        calc.__file__,
        cfile=importlib.util.cache_from_source(calc.__file__),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )
'''


TEST_HELPER = '''\
import os
import signal
import subprocess

from calc import add


def test_add_beside_a_background_job():
    """Start a shell's background job, and stop it only if the test gets
    that far, as a test that cleans up at the end of its body does."""
    job = subprocess.run(
        ["sh", "-c", "sleep 10 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(job.stdout)
    pidfile = os.environ.get("HELPER_PIDS")
    if pidfile:
        with open(pidfile, "a") as out:
            out.write(f"{pid}\\n")
    assert add(2, 3) == 5
    os.kill(pid, signal.SIGKILL)
'''

# test_b.py passes after test_a.py, but fails alone
REGISTRY = "HANDLERS = {}\n"

TEST_A = """\
import registry
from calc import add


def test_add_registers_its_handler():
    registry.HANDLERS["add"] = add
    assert add(2, 3) == 5
"""

TEST_B = """\
import registry


def test_the_registered_handler_adds():
    assert registry.HANDLERS["add"](2, 2) == 4
"""


def mutation(name, old, new, tests=("test_calc.py::test_add",), **extra):
    """One [[mutation]] table, of calc.py unless `extra` names a file.
    JSON strings and lists are valid TOML."""
    fields = {"name": name, "file": "calc.py", "old": old, "new": new}
    fields |= {"tests": list(tests), **extra}
    lines = [f"{key} = {json.dumps(value)}\n" for key, value in fields.items()]
    return "[[mutation]]\n" + "".join(lines)


SUM = mutation("sum becomes difference", "a + b", "a - b")
DOUBLE = mutation("untested double", "n * 2", "n * 3")
BROKEN = mutation("broken import", "import subprocess", "import no_such_mod")


def environment(**extra):
    return {**os.environ, **GIT_ENV, **extra}


def git(root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=environment(),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A repository with calc.py and its tests committed."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path_factory.mktemp("template") / "repo"
    root.mkdir()
    files = {
        "pytest.ini": "[pytest]\n",
        "calc.py": CALC,
        "test_calc.py": TEST_CALC,
        "test_bytecode.py": TEST_BYTECODE,
        "test_helper.py": TEST_HELPER,
        "registry.py": REGISTRY,
        "test_a.py": TEST_A,
        "test_b.py": TEST_B,
    }
    for name, text in files.items():
        (root / name).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    return root


def copy(template, where):
    """A fresh copy of the template, with calc.py's mtime set to STAMP."""
    root = where / "repo"
    shutil.copytree(template, root, symlinks=True)
    os.utime(root / "calc.py", ns=(STAMP, STAMP))
    return root


@pytest.fixture
def repo(template, tmp_path):
    return copy(template, tmp_path)


def harness(repo, spec, *options, env=None):
    """Run the harness on `spec`, TOML text kept outside the repository."""
    path = repo.parent / "spec.toml"
    path.write_text(spec)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *options],
        cwd=repo,
        env=env or environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def results(stdout):
    """The harness's results table as {mutation name: row}."""
    lines = [line for line in stdout.splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(dict(zip(header, cells, strict=True)))
    return {row["mutation"]: row for row in rows}


def outcomes(stdout):
    return {name: row["result"] for name, row in results(stdout).items()}


def snapshot(path):
    """What a restore must bring back: the bytes and modification time."""
    return path.read_bytes(), path.stat().st_mtime_ns


def plant_bytecode(module, source, scratch):
    """Cache bytecode compiled from `source` as `module`'s, in the
    unchecked hash-based form that Python loads without reading the
    source. It stands in for a timestamp-checked cache that an edit of
    the same size, within the same second, leaves looking current."""
    staged = scratch / "staged.py"
    staged.write_text(source)
    py_compile.compile(
        str(staged),
        cfile=importlib.util.cache_from_source(str(module)),
        dfile=str(module),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        doraise=True,
    )


def load(module):
    """Import `module` from its path as a test run would, bytecode cache
    included."""
    spec = importlib.util.spec_from_file_location(f"as_{module.stem}", module)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def running(pid):
    """Whether `pid` is a live process; a zombie awaiting reaping is not."""
    if Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until(condition, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"gave up waiting for {condition}")
        time.sleep(0.02)


def kill_leftovers(pidfile):
    """Clean up after a failure: kill the processes that a test noted in
    `pidfile`, if any still run."""
    if not pidfile.exists():
        return
    for pid in map(int, pidfile.read_text().split()):
        if running(pid):
            os.kill(pid, signal.SIGKILL)


# -- a default run, shared by the tests below ------------------------------


DEFAULT_TESTS = [
    "test_calc.py::test_add",
    "test_bytecode.py",
    "test_helper.py",
]


@pytest.fixture(scope="module")
def default_run(template, tmp_path_factory):
    """One default run, the baseline included, of a mutant that the tests
    kill, with traps set:

    * before the run, bytecode compiled from broken code is cached for
      calc.py, in the unchecked hash-based form that Python loads without
      reading the source, so the baseline fails unless the harness
      deletes it first;
    * test_bytecode.py caches calc.py's bytecode in the same form each
      time it runs. In the baseline it caches the original, which would
      mask the mutant (of the same size) unless the harness deletes it
      before the mutant's run. In that run it caches the mutant, which
      would stand in for the restored original unless the harness
      deletes it after the restore;
    * PYTHONPYCACHEPREFIX is set, and test_bytecode.py checks that pytest
      runs without it and without writing bytecode;
    * PY_COLORS asks pytest for colour, which would hide its failures
      from the harness unless the harness turns colour off;
    * PYTEST_ADDOPTS asks for --collect-only, which would make every run
      pass without running a test, unless the harness drops it;
    * test_helper.py starts a background job and stops it only when its
      assertion holds, so the mutant's run leaves the job to the harness
      to kill, though pytest exits by itself."""
    scratch = tmp_path_factory.mktemp("default-run")
    repo = copy(template, scratch)
    calc = repo / "calc.py"
    before = snapshot(calc)
    plant_bytecode(calc, CALC.replace("a + b", "a * b"), scratch)
    # The control: the cached bytecode, not the source, is what runs.
    control = load(calc).add(2, 3)
    report = scratch / "report.json"
    helper_pids = scratch / "helper-pids"
    spec = mutation("sum becomes difference", "a + b", "a - b", DEFAULT_TESTS)
    env = environment(
        PYTHONPYCACHEPREFIX=str(scratch / "prefix"),
        PY_COLORS="1",
        PYTEST_ADDOPTS="--collect-only",
        HELPER_PIDS=str(helper_pids),
    )
    result = harness(repo, spec, "--json", str(report), env=env)
    yield SimpleNamespace(
        repo=repo,
        calc=calc,
        before=before,
        control=control,
        result=result,
        report=report,
        helper_pids=helper_pids,
    )
    kill_leftovers(helper_pids)


def test_a_killed_mutant_is_reported(default_run):
    result = default_run.result
    assert result.returncode == 0, result.stdout + result.stderr
    row = results(result.stdout)["sum becomes difference"]
    assert (row["file"], row["result"], row["detail"]) == (
        "calc.py",
        "killed",
        "2 failed, 2 passed",
    )
    for test in (
        "test_calc.py::test_add",
        "test_helper.py::test_add_beside_a_background_job",
    ):
        assert f"failed: {test}" in result.stderr


def test_nothing_the_tests_started_outlives_their_run(default_run):
    # One job from the baseline, which stopped it itself, and one from the
    # mutant's run, which pytest left behind when it exited.
    pids = [int(pid) for pid in default_run.helper_pids.read_text().split()]
    assert len(pids) == 2
    wait_until(lambda: not any(running(pid) for pid in pids), timeout=5)


def test_pytest_runs_without_the_callers_pytest_addopts(default_run):
    stderr = default_run.result.stderr
    assert "PYTEST_ADDOPTS is set here; pytest runs without it" in stderr


def test_the_mutated_file_is_restored_byte_for_byte_with_its_mtime(
    default_run,
):
    assert snapshot(default_run.calc) == default_run.before


def test_the_baseline_ignores_stale_bytecode(default_run):
    assert default_run.control == 6
    assert "baseline: 4 passed" in default_run.result.stderr


def test_stale_bytecode_cannot_mask_a_mutant(default_run):
    # The mutant is the same size as the original, whose bytecode the
    # baseline cached, yet the mutant's own code ran and was caught.
    mutant = CALC.replace("a + b", "a - b")
    assert len(mutant.encode()) == len(CALC.encode())
    assert outcomes(default_run.result.stdout) == {
        "sum becomes difference": "killed"
    }


def test_bytecode_from_a_mutant_does_not_outlive_the_run(default_run):
    assert list((default_run.repo / "__pycache__").glob("calc.*.pyc")) == []


def test_pytest_leaves_no_cache_behind(default_run):
    assert not (default_run.repo / ".pytest_cache").exists()


def test_the_json_report(default_run):
    data = json.loads(default_run.report.read_text())
    head = git(default_run.repo, "rev-parse", "HEAD").stdout.strip()
    assert data["commit"] == head
    # one entry for each distinct list of tests
    [baseline] = data["baseline"]
    assert {key: baseline[key] for key in ("tests", "result", "detail")} == {
        "tests": DEFAULT_TESTS,
        "result": "passed",
        "detail": "4 passed",
    }
    [entry] = data["mutations"]
    assert {key: entry[key] for key in ("name", "file", "result")} == {
        "name": "sum becomes difference",
        "file": "calc.py",
        "result": "killed",
    }
    assert entry["failed"] == [
        "test_calc.py::test_add",
        "test_helper.py::test_add_beside_a_background_job",
    ]
    assert data["counts"]["killed"] == 1
    assert data["exit_status"] == 0


# -- other results, and restoration after each ------------------------------


def test_restore_brings_back_bytes_mode_and_mtime_and_never_follows_a_link(
    tmp_path,
):
    path = tmp_path / "calc.py"
    path.write_bytes(CALC.encode())
    path.chmod(0o640)
    os.utime(path, ns=(STAMP, STAMP))
    stat = path.stat()
    decoy = tmp_path / "decoy.py"
    decoy.write_text("left alone\n")

    def link_to_the_decoy():
        path.unlink()
        path.symlink_to(decoy)

    damages = {
        "changed": lambda: path.write_text("def add(a, b): ...\n"),
        "deleted": path.unlink,
        "read-only": lambda: path.chmod(0o444),
        "unreadable": lambda: path.chmod(0),
        "a symlink": link_to_the_decoy,
    }
    for damage, harm in damages.items():
        harm()
        assert mutate.restore(path, CALC.encode(), stat) is None, damage
        assert not path.is_symlink(), damage
        restored = path.stat()
        assert (S_IMODE(restored.st_mode), restored.st_mtime_ns) == (
            0o640,
            STAMP,
        ), damage
        assert path.read_bytes() == CALC.encode(), damage
    assert decoy.read_text() == "left alone\n"


def test_an_internal_error_still_restores_the_file_and_exits_2(
    repo, monkeypatch, capsys
):
    """Whatever escapes a test run, the finally block restores the file,
    and the exit status does not claim a survivor."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(repo)
    calc = repo / "calc.py"
    before = snapshot(calc)
    spec = repo.parent / "spec.toml"
    spec.write_text(SUM)
    during = []

    def broken_run(self, tests):
        during.append(calc.read_bytes())
        raise RuntimeError("the test run broke")

    monkeypatch.setattr(mutate.Harness, "run_tests", broken_run)
    assert mutate.main([str(spec), "--no-baseline"]) == 2
    assert during == [CALC.replace("a + b", "a - b").encode()]
    assert snapshot(calc) == before
    assert "RuntimeError: the test run broke" in capsys.readouterr().err


def scripted(monkeypatch, repo, spec, run):
    """Get ready to run the harness in-process on `spec`, with `run`
    standing in for every pytest run."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(repo)
    path = repo.parent / "spec.toml"
    path.write_text(spec)
    monkeypatch.setattr(mutate.Harness, "run_tests", run)
    return str(path)


def test_a_file_git_does_not_show_clean_after_its_restore_is_an_error(
    repo, monkeypatch, capsys
):
    """A test that stages the mutant leaves git's index changed. The bytes
    come back, but the harness says git disagrees, and stops the run."""
    calc = repo / "calc.py"

    def staging_run(self, tests):
        git(repo, "add", "calc.py")
        return mutate.Run(
            1,
            SUMMARY + "FAILED test_calc.py::test_add - assert -1 == 5\n",
            0.1,
        )

    spec = scripted(monkeypatch, repo, SUM + DOUBLE, staging_run)
    assert mutate.main([spec, "--no-baseline", "--keep-going"]) == 2
    captured = capsys.readouterr()
    assert (
        "calc.py is restored, but git does not show it clean: calc.py has "
        "unstaged changes" in captured.err
    )
    assert outcomes(captured.out) == {
        "sum becomes difference": "killed",
        "untested double": "not run",
    }
    assert calc.read_bytes() == CALC.encode()


# -- the baseline ------------------------------------------------------------


def test_the_baseline_runs_each_list_of_tests_as_its_mutants_will(
    repo, tmp_path
):
    """test_b.py passes after test_a.py, but fails alone. A baseline of
    both mutations' tests in one run would pass, and the docstring edit,
    which runs test_b.py alone, would then look killed."""
    calc = repo / "calc.py"
    before = snapshot(calc)
    spec = mutation(
        "sum becomes difference",
        "a + b",
        "a - b",
        ["test_a.py", "test_b.py"],
    ) + mutation(
        "the docstring changes",
        "A tiny module",
        "A small module",
        ["test_b.py"],
    )
    report = tmp_path / "report.json"
    result = harness(repo, spec, "--keep-going", "--json", str(report))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "the tests in test_b.py fail unmutated (1 failed)" in result.stderr
    assert result.stdout == ""
    baseline = json.loads(report.read_text())["baseline"]
    assert [(entry["tests"], entry["result"]) for entry in baseline] == [
        (["test_a.py", "test_b.py"], "passed"),
        (["test_b.py"], "failed"),
    ]
    assert snapshot(calc) == before


def test_a_baseline_in_which_no_test_passes_is_refused(
    repo, monkeypatch, capsys
):
    """Tests that only skip or xfail pass, yet no mutant could fail them."""
    ran = []

    def skipping_run(self, tests):
        ran.append(tests)
        counts = "1 skipped, 1 xfailed, 1 xpassed in 0.01s"
        return mutate.Run(0, f"=========== {counts} ===========\n", 0.1)

    spec = scripted(monkeypatch, repo, SUM, skipping_run)
    assert mutate.main([spec]) == 2
    assert ran == [["test_calc.py::test_add"]]
    assert (
        "no test in test_calc.py::test_add passes unmutated (1 skipped, "
        "1 xfailed, 1 xpassed)" in capsys.readouterr().err
    )


def test_a_survivor_stops_the_run_and_fails_it(repo):
    calc = repo / "calc.py"
    before = snapshot(calc)
    result = harness(repo, DOUBLE + SUM, "--no-baseline")
    assert result.returncode == 1, result.stdout + result.stderr
    assert outcomes(result.stdout) == {
        "untested double": "survived",
        "sum becomes difference": "not run",
    }
    assert snapshot(calc) == before


def test_a_collection_error_is_an_error_and_the_file_is_restored(repo):
    calc = repo / "calc.py"
    before = snapshot(calc)
    result = harness(repo, BROKEN + SUM, "--no-baseline")
    assert result.returncode == 2, result.stdout + result.stderr
    rows = results(result.stdout)
    assert (
        rows["broken import"]["result"],
        rows["broken import"]["detail"],
    ) == (
        "error",
        "collection error",
    )
    assert rows["sum becomes difference"]["result"] == "not run"
    assert "ModuleNotFoundError" in result.stderr
    assert snapshot(calc) == before


def test_a_timeout_is_an_error_that_stops_all_the_tests_started(
    repo, tmp_path
):
    """pytest exits on the harness's SIGINT at once, but the shell's
    background job that hang() started ignores SIGINT: only SIGKILL to
    the group, once pytest has exited, stops it. The job must have started
    before the timeout, so a busy machine gets longer timeouts."""
    calc = repo / "calc.py"
    before = snapshot(calc)
    spec = mutation("sum hangs", "return a + b", "return hang()")
    pidfile = tmp_path / "pids"
    env = environment(CALC_PIDFILE=str(pidfile))
    try:
        for timeout in ("1.5", "5", "15"):
            result = harness(
                repo, spec, "--no-baseline", "--timeout", timeout, env=env
            )
            if pidfile.exists():
                break
        else:
            pytest.fail("the tests never reached hang() before the timeout")
        assert result.returncode == 2, result.stdout + result.stderr
        row = results(result.stdout)["sum hangs"]
        assert (row["result"], row["detail"]) == (
            "error",
            f"timed out after {timeout} s",
        )
        assert snapshot(calc) == before
        pids = [int(pid) for pid in pidfile.read_text().split()]
        wait_until(lambda: not any(running(pid) for pid in pids), timeout=5)
    finally:
        kill_leftovers(pidfile)


# Runs the harness after undoing any "ignore" of its stop signals that
# this test run inherited, as a shell's background job does. It does so
# in the harness's own interpreter, not in a preexec_fn, which is unsafe
# in a process with threads (as a pytest-xdist worker has).
LAUNCH = """\
import runpy, signal, sys
for name in ("SIGINT", "SIGTERM", "SIGHUP"):
    signal.signal(getattr(signal, name), signal.SIG_DFL)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_sigint_stops_the_tests_even_if_they_ignore_it_and_restores(
    repo, tmp_path
):
    """The tests ignore SIGINT, and so do the processes they started: the
    harness passes SIGINT on to them, then SIGKILL after --grace."""
    calc = repo / "calc.py"
    before = snapshot(calc)
    pidfile = tmp_path / "pids"
    spec = tmp_path / "spec.toml"
    spec.write_text(
        mutation("sum hangs", "return a + b", "return hang(stubborn=True)")
    )
    process = subprocess.Popen(
        [sys.executable, "-c", LAUNCH, str(SCRIPT), str(spec)]
        + ["--no-baseline", "--grace", "0.2"],
        cwd=repo,
        env=environment(CALC_PIDFILE=str(pidfile)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pids = []
    try:
        wait_until(lambda: pidfile.exists() or process.poll() is not None)
        assert pidfile.exists(), process.communicate()
        # pytest, and the processes it started, are running the mutant
        pids = [int(pid) for pid in pidfile.read_text().split()]
        assert b"return hang(" in calc.read_bytes()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 128 + signal.SIGINT, stderr
        assert "stopped by SIGINT; files restored" in stderr
        assert outcomes(stdout) == {"sum hangs": "interrupted"}
        assert snapshot(calc) == before
        wait_until(lambda: not any(running(pid) for pid in pids), timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        kill_leftovers(pidfile)


# Runs the harness in a fresh interpreter that inherits an ignored
# SIGCHLD, as one started by a careless parent would: Linux keeps the
# ignore across exec.
IGNORING_SIGCHLD = """\
import os, signal, sys
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""


@pytest.mark.skipif(not hasattr(signal, "SIGCHLD"), reason="no SIGCHLD")
def test_an_inherited_sigchld_ignore_cannot_hide_a_failing_baseline(
    repo, tmp_path
):
    """With SIGCHLD ignored, the kernel would reap pytest itself, and its
    exit status would read as 0. Then this baseline, in which one test
    passes and one fails, would pass, and the mutant would survive."""
    spec = tmp_path / "spec.toml"
    spec.write_text(
        mutation(
            "sum becomes difference",
            "a + b",
            "a - b",
            ["test_calc.py::test_add", "test_b.py"],
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", IGNORING_SIGCHLD, str(SCRIPT), str(spec)],
        cwd=repo,
        env=environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "fail unmutated (1 failed, 1 passed)" in result.stderr
    assert result.stdout == ""


def test_sigint_sigterm_and_sighup_reach_the_handler_unless_ignored():
    def handler(signum, frame):
        pass

    names = ["SIGINT", "SIGTERM", "SIGHUP"]
    assert [signal.Signals(s).name for s in mutate.STOP_SIGNALS] == names
    before = {
        signum: signal.getsignal(signum) for signum in mutate.STOP_SIGNALS
    }
    try:
        with mutate.stopping_on_signals(handler):
            for signum in mutate.STOP_SIGNALS:
                assert signal.getsignal(signum) is handler
        # An ignored signal, as a shell's background job inherits, stays
        # ignored, so it does not stop the harness either.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with mutate.stopping_on_signals(handler):
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
            assert signal.getsignal(signal.SIGTERM) is handler
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        for signum, previous in before.items():
            signal.signal(signum, previous)
    after = {
        signum: signal.getsignal(signum) for signum in mutate.STOP_SIGNALS
    }
    assert after == before


# -- which mutations run, and the exit status --------------------------------


@pytest.mark.parametrize(
    "scripted, options, ran, status",
    [
        (["survived", "killed"], [], ["survived", "not run"], 1),
        (
            ["survived", "killed"],
            ["--allow-survivors"],
            ["survived", "killed"],
            0,
        ),
        (
            ["error", "killed"],
            ["--allow-survivors"],
            ["error", "not run"],
            2,
        ),
        (
            ["error", "survived", "killed"],
            ["--keep-going"],
            ["error", "survived", "killed"],
            2,
        ),
        (
            ["killed", "interrupted", "killed"],
            ["--keep-going"],
            ["killed", "interrupted", "not run"],
            None,
        ),
    ],
    ids=[
        "a-survivor-stops",
        "allowed-survivors-go-on",
        "an-error-stops",
        "keep-going-goes-on",
        "an-interrupted-run-stops",
    ],
)
def test_which_mutations_run(
    repo, monkeypatch, capsys, scripted, options, ran, status
):
    """The run loop, with each mutation's result scripted in place of a
    test run."""
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(repo)
    spec = repo.parent / "spec.toml"
    spec.write_text(
        "".join(mutation(f"m{i}", "a + b", "a - b") for i in range(len(ran)))
    )
    script = iter(scripted)

    def scripted_mutate(self, result, position):
        result.result = next(script)

    monkeypatch.setattr(mutate.Harness, "_mutate", scripted_mutate)
    exit_status = mutate.main([str(spec), "--no-baseline", *options])
    table = results(capsys.readouterr().out)
    assert [row["result"] for row in table.values()] == ran
    if status is not None:
        assert exit_status == status


@pytest.mark.parametrize(
    "found, allow, problems, signum, status",
    [
        (["killed", "killed"], False, False, None, 0),
        (["killed", "survived"], False, False, None, 1),
        (["killed", "survived"], True, False, None, 0),
        (["survived", "error"], True, False, None, 2),
        (["killed", "not run"], False, True, None, 2),
        ([], False, True, None, 2),
        (["survived", "interrupted"], False, False, signal.SIGINT, 130),
    ],
    ids=[
        "all-killed",
        "survivor",
        "allowed-survivor",
        "error",
        "problem",
        "refused",
        "signal",
    ],
)
def test_exit_status(found, allow, problems, signum, status):
    assert (
        mutate.exit_status(
            found,
            allow_survivors=allow,
            problems=problems,
            signum=signum,
        )
        == status
    )


# -- refusals ----------------------------------------------------------------


def test_refuses_files_with_uncommitted_changes(repo):
    calc = repo / "calc.py"
    calc.write_text(CALC + "# an unstaged edit\n")
    test_calc = repo / "test_calc.py"
    test_calc.write_text(TEST_CALC + "# a staged edit\n")
    git(repo, "add", "test_calc.py")
    extra = repo / "extra.py"
    extra.write_text("LIMIT = 1\n")
    files = [calc, test_calc, extra]
    before = [snapshot(path) for path in files]
    spec = (
        mutation("unstaged", "a + b", "a - b")
        + mutation("staged", "(2, 3)", "(3, 2)", file="test_calc.py")
        + mutation("untracked", "LIMIT = 1", "LIMIT = 2", file="extra.py")
    )
    result = harness(repo, spec)
    assert result.returncode == 2
    assert "refusing to run; nothing was changed" in result.stderr
    for problem in (
        "mutation 1 ('unstaged'): calc.py has unstaged changes",
        "mutation 2 ('staged'): test_calc.py has staged changes",
        "mutation 3 ('untracked'): extra.py is not tracked by git",
    ):
        assert problem in result.stderr
    assert result.stdout == ""
    assert [snapshot(path) for path in files] == before


def test_refuses_old_text_that_is_missing_or_ambiguous(repo):
    calc = repo / "calc.py"
    before = snapshot(calc)
    spec = (
        mutation("missing", "a * b", "a - b")
        + mutation("ambiguous", "return", "yield")
        + mutation("miscounted", "return", "yield", count=3)
    )
    result = harness(repo, spec)
    assert result.returncode == 2
    for problem in (
        "mutation 1 ('missing'): old text not found in calc.py",
        "mutation 2 ('ambiguous'): old text occurs 2 times in calc.py, "
        "not 1; make it unique or set count",
        "mutation 3 ('miscounted'): old text occurs 2 times in calc.py, not 3",
    ):
        assert problem in result.stderr
    assert result.stdout == ""
    assert snapshot(calc) == before


GOOD = {
    "name": "sum",
    "file": "calc.py",
    "old": "a + b",
    "new": "a - b",
    "tests": ["test_calc.py::test_add"],
}


def toml(**changes):
    """GOOD as a [[mutation]] table, with `changes`; None drops a key."""
    fields = {**GOOD, **changes}
    lines = [
        f"{key} = {json.dumps(value)}\n"
        for key, value in fields.items()
        if value is not None
    ]
    return "[[mutation]]\n" + "".join(lines)


@pytest.mark.parametrize(
    "text, problem",
    [
        ("", "the spec has no [[mutation]] tables"),
        ("[[mutation]\n", "is not valid TOML"),
        ('[mutation]\nfile = "calc.py"\n', "the spec has no [[mutation]]"),
        ("verbose = true\n" + toml(), "unknown top-level key 'verbose'"),
        (toml(colour="red"), "mutation 1 ('sum'): unknown key 'colour'"),
        (toml(file=None), "mutation 1 ('sum'): file must be a non-empty"),
        (toml(old=""), "old must be a non-empty string"),
        (toml(new=None), "new must be a string"),
        (toml(new="a + b"), "new is the same as old"),
        (toml(tests=[]), "tests must name at least one test id or path"),
        (toml(tests=["-x", "test_calc.py"]), "not pytest options"),
        (toml(tests=["@more-args.txt"]), "not pytest options or @argfiles"),
        (toml(count=0), "count must be a positive integer"),
        (toml(count=True), "count must be a positive integer"),
        (toml() + toml(), "mutation 2 ('sum'): the name is used twice"),
    ],
)
def test_refuses_a_malformed_spec(tmp_path, text, problem):
    path = tmp_path / "spec.toml"
    path.write_text(text)
    _, problems = mutate.load_spec(path)
    assert any(problem in found for found in problems), problems


def test_a_spec_may_name_one_test_and_leave_out_the_name(tmp_path):
    path = tmp_path / "spec.toml"
    path.write_text(toml(name=None, tests="test_calc.py"))
    [parsed], problems = mutate.load_spec(path)
    assert problems == []
    assert (parsed.name, parsed.tests, parsed.count) == (
        "mutation 1",
        ["test_calc.py"],
        1,
    )


@pytest.mark.parametrize(
    "file, new, problem",
    [
        ("../calc.py", "a - b", "../calc.py is outside the repository"),
        ("/calc.py", "a - b", "is not relative to the repository root"),
        ("link.py", "a - b", "link.py is a symlink or lies under one"),
        ("folder", "a - b", "folder does not exist or is not a regular file"),
        ("calc.py", "a +", "leaves calc.py unparseable: SyntaxError"),
    ],
    ids=["outside", "absolute", "symlink", "directory", "unparseable"],
)
def test_refuses_what_it_cannot_mutate_safely(repo, file, new, problem):
    (repo / "link.py").symlink_to("calc.py")
    (repo / "folder").mkdir()
    doomed = mutate.Mutation(
        name="m",
        label="mutation 1",
        file=file,
        old="a + b",
        new=new,
        tests=["test_calc.py"],
    )
    problems = mutate.check([doomed], Path(os.path.realpath(repo)))
    assert len(problems) == 1
    assert problems[0].startswith("mutation 1: ")
    assert problem in problems[0]


def test_occurrences_overlaps_and_counted_replacement():
    assert mutate.occurrences(b"a+b; a+b", b"a+b") == [0, 5]
    assert mutate.occurrences(b"aaa", b"aa") == [0, 1]

    def prepared(old, data, count):
        counted = mutate.Mutation(
            name="m",
            label="mutation 1",
            file="notes.txt",
            old=old,
            new="x",
            tests=["t"],
            count=count,
        )
        return mutate.prepare(counted, data)

    assert prepared("a+b", b"a+b; a+b", 2) == (b"x; x", None)
    assert prepared("aa", b"aaa", 2) == (
        b"",
        "occurrences of old text overlap in notes.txt",
    )


# -- classifying a test run ---------------------------------------------------

SUMMARY = "=========== short test summary info ============\n"
COLLECTING = "_____ ERROR collecting t.py _____\nE   ImportError: boom\n"


@pytest.mark.parametrize(
    "run, expected",
    [
        (
            mutate.Run(0, "===== 2 passed in 0.01s =====\n", 1.0),
            ("survived", "2 passed", []),
        ),
        (
            mutate.Run(
                1,
                "FAILED printed by a test, before the summary\n"
                + SUMMARY
                + "FAILED t.py::test_a - assert 1 == 2\n"
                + "ERROR t.py::test_b - RuntimeError: boom\n"
                + "=== 1 failed, 1 passed, 1 error in 0.02s ===\n",
                1.0,
            ),
            (
                "killed",
                "1 failed, 1 passed, 1 error",
                ["t.py::test_a", "t.py::test_b"],
            ),
        ),
        (
            mutate.Run(1, "python: No module named pytest\n", 0.1),
            ("error", "pytest exited 1 but reported no failing test", []),
        ),
        (
            mutate.Run(
                2,
                COLLECTING
                + SUMMARY
                + "ERROR t.py\n"
                + "!!! Interrupted: 1 error during collection !!!\n",
                0.3,
            ),
            ("error", "collection error", ["t.py"]),
        ),
        (
            mutate.Run(
                4,
                COLLECTING
                + SUMMARY
                + "ERROR t.py\n"
                + "ERROR: found no collectors for t.py::test_a\n",
                0.3,
            ),
            ("error", "collection error", ["t.py"]),
        ),
        (
            mutate.Run(4, "ERROR: not found: t.py::test_nope\n", 0.3),
            ("error", "pytest usage error; not found: t.py::test_nope", []),
        ),
        (
            mutate.Run(5, "=== 3 deselected in 0.01s ===\n", 0.3),
            ("error", "no tests ran", []),
        ),
        (
            mutate.Run(-9, "", 0.3),
            ("error", "pytest died of SIGKILL", []),
        ),
        (
            mutate.Run(-2, "", 0.6, stopped="timeout"),
            ("error", "timed out after 0.5 s", []),
        ),
        (
            mutate.Run(-2, "", 0.6, stopped="signal"),
            ("interrupted", "stopped by a signal", []),
        ),
    ],
    ids=[
        "passed",
        "failed",
        "no-failure-reported",
        "collection",
        "collection-under-a-node-id",
        "unknown-id",
        "none-ran",
        "crash",
        "timeout",
        "signal",
    ],
)
def test_classifies_a_test_run(run, expected):
    assert mutate.classify(run, timeout=0.5) == expected


@pytest.mark.skipif(
    not hasattr(signal, "SIGRTMIN"), reason="no real-time signals here"
)
def test_names_a_real_time_signal_by_number():
    # signal.Signals has no member for most real-time signals
    signum = signal.SIGRTMIN + 1
    run = mutate.Run(-signum, "", 0.3)
    assert mutate.classify(run, timeout=0.5) == (
        "error",
        f"pytest died of signal {signum}",
        [],
    )
