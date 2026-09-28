#!/usr/bin/env python3
r"""Mutation harness: show that tests catch the regressions they guard.

A spec file lists mutations. Each is an exact text edit to one file in
the repository, plus the tests that should catch it. For each mutation
in turn, the harness writes the edit, runs those tests and restores the
file. The mutant is *killed* when the tests fail and *survives* when
they pass. A test that guards against a regression should kill the
mutant that brings it back.

    uv run python scripts/mutate.py SPEC [--keep-going] [--allow-survivors]
                                         [--no-baseline] [--timeout SECONDS]
                                         [--grace SECONDS] [--json PATH] [-q]

Run it from anywhere inside the repository, with that checkout's own
environment: `uv run` from the checkout or worktree whose files the spec
names. The harness runs pytest with the interpreter it runs under, and
an environment made for another checkout imports that checkout's src/
through its editable install, where no mutation lands: every mutant
would falsely survive. It needs only the standard library and pytest.

The spec
--------
SPEC is a TOML file with one [[mutation]] table per mutation:

    [[mutation]]
    name = "blank names are kept"    # optional and unique; the table's label
    file = "src/yizkr/example.py"    # relative to the repository root
    old = 'if not name.strip():'     # exact text that occurs exactly once
    new = 'if False:'
    tests = ["tests/unit/test_example.py::test_blank_names_are_dropped"]
    # count = 2   # optional: old occurs exactly this many times, and
    #             # every occurrence is replaced

`tests` holds pytest node ids or paths, relative to the repository
root; a single string is fine too. Write code in TOML literal strings,
in single quotes ('...', or three of them around several lines), which
keep every character as written: '\n' there is a backslash and an n.
In double-quoted basic strings a backslash starts an escape, and in a
multi-line one a backslash at the end of a line joins that line to the
next. A multi-line string of either kind drops a newline that directly
follows its opening quotes.

Guarantees
----------
Refusal up front. Nothing runs if the spec has an error, or if any
mutation's file is outside the repository, is a symlink, is not tracked
by git, or has uncommitted changes, staged or not. The same goes for a
mutation whose old text is missing, occurs a different number of times
than count, or overlaps itself, and for one that would leave a Python
file unparseable. `tests` takes test ids and paths only, never pytest
options or @argfiles. Because every file is clean and tracked,
`git checkout -- FILE` can always recover it, whatever happens to the
harness.

Baseline. First, the tests run on the unmutated tree, once for each
distinct list of tests in the spec, just as the mutants' runs will run
them. Each run must pass, and at least one of its tests must pass rather
than skip: a kill proves nothing when the tests fail anyway, and a
survivor proves nothing when they only skip. The baseline stops at the
first list that fails. --no-baseline skips these runs.

No stale bytecode. Python loads a module's cached bytecode without
reading the source when the source's recorded size and timestamp still
match, so a mutant of the same length, written within the same second,
could run the old code. Before each run, the harness deletes the
module's cached bytecode (__pycache__/<stem>.*.pyc, which also covers
pytest's rewritten test modules). pytest runs with
PYTHONDONTWRITEBYTECODE=1 and without PYTHONPYCACHEPREFIX, so it
compiles the mutated source. The bytecode is deleted again after the
restore, so nothing compiled from a mutant outlives the run.
-p no:cacheprovider keeps mutant runs out of pytest's cache.

Restoration. Each file is written back byte for byte, with its mode and
modification time, in a `finally` block. This covers test failures,
errors and timeouts, and SIGINT (Ctrl-C), SIGTERM and SIGHUP. A file
that a test deleted is written back, and a symlink that a test left in
its place is removed, never written through. After each restore, git
must show the file clean again. If it does not (say a test staged the
mutant), the harness reports it and stops with an error. A signal stops
the test run. The harness then restores the file, prints the table so
far and exits with 128 + the signal number. A harness killed any other
way, such as by SIGKILL, can leave a mutation in place. The progress
lines name the file being mutated, and `git checkout -- FILE` recovers
it.

Test runs
---------
pytest runs as `python -P -m pytest -p no:cacheprovider -rfE
--color=no TESTS` from the repository root, in a process group of its
own, and without PYTEST_ADDOPTS. When a run outlives --timeout, or a
signal stops the harness, the whole group gets SIGINT, so pytest can
tear its fixtures down. Once pytest has exited, or after --grace
seconds, the group gets SIGKILL. It gets SIGKILL when pytest exits on
its own too, so what a test started in the group, such as a shell's
background job, dies with the run.

A process that leaves the group escapes that kill: one started with
setsid or start_new_session=True, with setpgid or process_group=0, or a
daemon that detaches itself. Left running, it can hold a lock or a port
and make later mutants look killed. Tests must stop what they detach
themselves, in a fixture's teardown say, which pytest runs even when
the test fails.

The harness resets SIGCHLD to its default before it starts pytest. A
SIGCHLD ignored by whatever started the harness would let the kernel
reap pytest at once, and every run would then read as passing.

Results
-------
killed       pytest exited 1 and reported failing or erroring tests
survived     pytest exited 0
error        anything else, reported separately: a collection error,
             an unknown test id, no tests collected, a crash or a
             timeout
interrupted  a signal stopped the run
not run      the run stopped before this mutation

The run stops at the first mutation that fails it: a survivor (unless
--allow-survivors) or an error. --keep-going runs every mutation.
Progress goes to stderr (-q silences it). The results table goes to
stdout as Markdown, ready to paste into a PR, and --json PATH also
writes a report.

Exit status: 0 when every mutation that ran was killed, or survived
under --allow-survivors; 1 when a mutant survived and nothing else went
wrong; 2 after a refusal, a failing baseline, a mutation's error, a
failed restore, a file git does not show clean after its restore, or an
internal error; 128 + N after signal N.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import warnings
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from stat import S_IMODE

DEFAULT_TIMEOUT = 300.0
DEFAULT_GRACE = 5.0
# how often, in seconds, the harness checks for a signal while pytest runs
POLL = 0.1
# lines of pytest output shown for an error
TAIL = 30

KILLED = "killed"
SURVIVED = "survived"
ERROR = "error"
INTERRUPTED = "interrupted"
NOT_RUN = "not run"
RESULTS = (KILLED, SURVIVED, ERROR, INTERRUPTED, NOT_RUN)

# Signals after which the harness restores the file and exits.
STOP_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP")
    if hasattr(signal, name)
)
MUTATION_KEYS = frozenset({"name", "file", "old", "new", "tests", "count"})
# pytest's closing line of counts, such as "1 failed, 2 passed in 0.05s"
COUNTS = re.compile(
    r"\b\d+ (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed)\b"
)
# the tests that passed outright, in those counts ("xpassed" is not one)
PASSED = re.compile(r"\b(\d+) passed\b")
# the heading pytest gives each module that it fails to collect
COLLECTION_ERROR = re.compile(r"^_+ ERROR collecting ", re.MULTILINE)


@dataclass
class Mutation:
    """One exact text edit, and the tests that should catch it."""

    name: str
    label: str  # how problems name it: "mutation 2", maybe with its name
    file: str
    old: str
    new: str
    tests: list[str]
    count: int = 1
    # set by check(): the file's absolute path, and its path in the repo
    path: Path | None = None
    rel: str = ""


@dataclass
class Run:
    """How one pytest run ended."""

    returncode: int | None
    output: str
    seconds: float
    # "timeout" or "signal" when the harness stopped the run
    stopped: str | None = None


@dataclass
class Result:
    """What happened to one mutation."""

    mutation: Mutation
    result: str = NOT_RUN
    seconds: float | None = None
    returncode: int | None = None
    detail: str = ""
    failed: list[str] = field(default_factory=list)
    output: str = ""

    def as_json(self) -> dict:
        mutation = self.mutation
        return {
            "name": mutation.name,
            "file": mutation.rel or mutation.file,
            "old": mutation.old,
            "new": mutation.new,
            "count": mutation.count,
            "tests": mutation.tests,
            "result": self.result,
            "seconds": _rounded(self.seconds),
            "returncode": self.returncode,
            "detail": self.detail,
            "failed": self.failed,
            "output_tail": tail(self.output) if self.result == ERROR else "",
        }


# -- the spec ---------------------------------------------------------------


def load_spec(path: Path) -> tuple[list[Mutation], list[str]]:
    """Parse a spec. Returns its mutations and the problems found in it."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        return [], [f"cannot read the spec {path}: {exc.strerror or exc}"]
    except tomllib.TOMLDecodeError as exc:
        return [], [f"the spec {path} is not valid TOML: {exc}"]
    problems = [
        f"unknown top-level key {key!r} in the spec"
        for key in sorted(set(data) - {"mutation"})
    ]
    tables = data.get("mutation")
    if (
        not isinstance(tables, list)
        or not tables
        or not all(isinstance(table, dict) for table in tables)
    ):
        return [], [*problems, "the spec has no [[mutation]] tables"]
    mutations = []
    names: set[str] = set()
    for index, table in enumerate(tables, start=1):
        mutation, found = _mutation(index, table)
        problems += found
        if mutation is None:
            continue
        if mutation.name in names:
            problems.append(f"{mutation.label}: the name is used twice")
        names.add(mutation.name)
        mutations.append(mutation)
    return mutations, problems


def _mutation(index: int, table: dict) -> tuple[Mutation | None, list[str]]:
    """One [[mutation]] table, or the problems that keep it from being one."""
    where = f"mutation {index}"
    name = table.get("name", where)
    if "name" in table and isinstance(name, str) and name.strip():
        where += f" ({name!r})"
    problems = []
    unknown = sorted(set(table) - MUTATION_KEYS)
    if unknown:
        problems.append(f"unknown key {', '.join(map(repr, unknown))}")
    if not isinstance(name, str) or not name.strip():
        problems.append("name must be a non-empty string")
    for key in ("file", "old"):
        if not isinstance(table.get(key), str) or not table[key]:
            problems.append(f"{key} must be a non-empty string")
    new = table.get("new")
    if not isinstance(new, str):
        problems.append("new must be a string")
    elif new == table.get("old"):
        problems.append("new is the same as old")
    tests = table.get("tests")
    if isinstance(tests, str):
        tests = [tests]
    if (
        not isinstance(tests, list)
        or not tests
        or not all(isinstance(test, str) and test.strip() for test in tests)
    ):
        problems.append("tests must name at least one test id or path")
    elif any(test.startswith(("-", "@")) for test in tests):
        problems.append(
            "tests takes test ids and paths, not pytest options or @argfiles"
        )
    count = table.get("count", 1)
    # bool is a subclass of int, and `count = true` is no count
    if type(count) is not int or count < 1:
        problems.append("count must be a positive integer")
    if problems:
        return None, [f"{where}: {problem}" for problem in problems]
    mutation = Mutation(
        name=name,
        label=where,
        file=table["file"],
        old=table["old"],
        new=new,
        tests=list(tests),
        count=count,
    )
    return mutation, []


# -- the repository ---------------------------------------------------------


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `root`, reading paths literally rather than as globs."""
    return subprocess.run(
        ["git", "--literal-pathspecs", "--no-optional-locks", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def repository_root() -> tuple[Path | None, str | None]:
    """The root of the repository around the working directory."""
    try:
        found = git(Path.cwd(), "rev-parse", "--show-toplevel")
    except OSError as exc:
        return None, f"cannot run git: {exc}"
    if found.returncode != 0:
        return None, "run the harness from inside a git repository"
    return Path(os.path.realpath(found.stdout.strip())), None


def locate(root: Path, file: str) -> tuple[Path | None, str | None]:
    """The absolute path that a spec's `file` names, or why it can't be
    mutated. Symlinks are refused: Python caches bytecode under the name
    it imports a module by, which need not be the name of the target."""
    if Path(file).is_absolute():
        return None, f"{file} is not relative to the repository root"
    path = Path(os.path.normpath(root / file))
    if root not in path.parents:
        return None, f"{file} is outside the repository"
    if os.path.realpath(path) != str(path):
        return None, f"{file} is a symlink or lies under one"
    if not path.is_file():
        return None, f"{file} does not exist or is not a regular file"
    return path, None


def git_problem(root: Path, rel: str) -> str | None:
    """Why git cannot vouch for `rel` as committed and unchanged, or None.

    A clean, tracked file can always be recovered with git checkout."""
    listed = git(root, "ls-files", "-v", "--", rel)
    if listed.returncode != 0:
        return f"git ls-files failed on {rel}: {listed.stderr.strip()}"
    tags = [line[:1] for line in listed.stdout.splitlines()]
    if not tags:
        return f"{rel} is not tracked by git"
    if tags != ["H"]:
        # lower case: assume-unchanged; S: skip-worktree; M: unmerged
        return (
            f"{rel} is marked in git's index (ls-files tag "
            f"{''.join(tags)!r}), so git cannot vouch that it is clean"
        )
    for staged in (False, True):
        cached = ["--cached"] if staged else []
        status = git(
            root, "diff", *cached, "--quiet", "--no-ext-diff", "--", rel
        ).returncode
        if status == 1:
            state = "staged" if staged else "unstaged"
            return f"{rel} has {state} changes"
        if status != 0:
            return f"git diff failed on {rel}"
    return None


def occurrences(data: bytes, old: bytes) -> list[int]:
    """Every offset where `old` starts in `data`, overlaps included."""
    starts = []
    start = data.find(old)
    while start != -1:
        starts.append(start)
        start = data.find(old, start + 1)
    return starts


def apply(data: bytes, starts: list[int], old: bytes, new: bytes) -> bytes:
    """`data` with `new` in place of the `old` that starts at each offset."""
    pieces = []
    end = 0
    for start in starts:
        pieces += [data[end:start], new]
        end = start + len(old)
    pieces.append(data[end:])
    return b"".join(pieces)


def syntax_error(source: bytes, filename: str) -> str | None:
    """Why `source` does not compile as Python, or None. Nothing is
    written: compile() makes a code object in memory only."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            compile(source, filename, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
    return None


def prepare(mutation: Mutation, data: bytes) -> tuple[bytes, str | None]:
    """The mutant of `data`, the file's bytes, or why there can't be one."""
    old = mutation.old.encode()
    where = mutation.rel or mutation.file
    starts = occurrences(data, old)
    if not starts:
        return b"", f"old text not found in {where}"
    if any(b - a < len(old) for a, b in pairwise(starts)):
        return b"", f"occurrences of old text overlap in {where}"
    if len(starts) != mutation.count:
        hint = "; make it unique or set count" if mutation.count == 1 else ""
        return b"", (
            f"old text occurs {len(starts)} times in {where}, "
            f"not {mutation.count}{hint}"
        )
    mutant = apply(data, starts, old, mutation.new.encode())
    if where.endswith(".py"):
        error = syntax_error(mutant, where)
        if error:
            return b"", f"the mutation leaves {where} unparseable: {error}"
    return mutant, None


def check(mutations: list[Mutation], root: Path) -> list[str]:
    """Why the mutations cannot run safely in `root`; empty when they can.

    Sets each good mutation's `path` and `rel`."""
    problems = []
    vouched: dict[Path, str | None] = {}
    for mutation in mutations:
        path, problem = locate(root, mutation.file)
        if path is not None:
            mutation.path = path
            mutation.rel = path.relative_to(root).as_posix()
            if path not in vouched:
                vouched[path] = git_problem(root, mutation.rel)
            problem = vouched[path]
            if problem is None:
                try:
                    _, problem = prepare(mutation, path.read_bytes())
                except OSError as exc:
                    problem = f"cannot read {mutation.rel}: {exc}"
        if problem:
            problems.append(f"{mutation.label}: {problem}")
    return problems


# -- bytecode and restoration -----------------------------------------------


def clear_bytecode(path: Path) -> None:
    """Delete the bytecode that CPython and pytest cache for a module.

    Both keep it as __pycache__/<stem>.<tag>.pyc next to the source
    (pytest's rewritten test modules as <stem>.<tag>-pytest-<v>.pyc)."""
    if path.suffix != ".py":
        return
    prefix = path.stem + "."
    try:
        with os.scandir(path.parent / "__pycache__") as entries:
            stale = [
                entry.path
                for entry in entries
                if entry.name.startswith(prefix)
                and entry.name.endswith(".pyc")
            ]
    except (FileNotFoundError, NotADirectoryError):
        return
    for pyc in stale:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(pyc)


def restore(path: Path, data: bytes, stat: os.stat_result) -> str | None:
    """Put the file back as `stat` and `data` found it: its bytes, mode and
    modification time. Deletes any bytecode compiled meanwhile. Returns
    what went wrong, or None."""
    mode = S_IMODE(stat.st_mode)
    try:
        if path.is_symlink():
            # a test swapped the file for a link: never write through it
            path.unlink()
        if path.is_file() and S_IMODE(path.stat().st_mode) != mode:
            # a test made it unwritable or unreadable, say
            os.chmod(path, mode)
        if _contents(path) != data:
            path.write_bytes(data)
            if _contents(path) != data:
                return "the file still differs from its original"
        if S_IMODE(path.stat().st_mode) != mode:
            os.chmod(path, mode)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        clear_bytecode(path)
    except OSError as exc:
        return str(exc)
    return None


def _contents(path: Path) -> bytes | None:
    # A test may have deleted the file; the restore writes it back.
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


# -- test runs --------------------------------------------------------------


def pytest_command(tests: list[str]) -> list[str]:
    # -P: like the pytest script, keep the working directory off sys.path
    return [
        sys.executable,
        "-P",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-rfE",
        "--color=no",
        *tests,
    ]


def pytest_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # bytecode under a prefix directory would escape clear_bytecode()
    env.pop("PYTHONPYCACHEPREFIX", None)
    # options from outside the spec, such as --collect-only or -x, would
    # change what a run means
    env.pop("PYTEST_ADDOPTS", None)
    return env


def failures(output: str) -> list[str]:
    """The test ids in pytest's short summary of failures and errors."""
    lines = output.splitlines()
    headers = [
        index
        for index, line in enumerate(lines)
        if "short test summary info" in line
    ]
    if not headers:
        return []
    return [
        line.split(" ", 1)[1].split(" - ", 1)[0].strip()
        for line in lines[headers[-1] + 1 :]
        if line.startswith(("FAILED ", "ERROR "))
    ]


def counts(output: str) -> str:
    """pytest's closing counts, such as '1 failed, 2 passed', or ''."""
    for line in reversed(output.splitlines()):
        if COUNTS.search(line):
            text = line.strip().strip("=").strip()
            return re.sub(r"\s+in [\d.]+s\b.*$", "", text)
    return ""


def passes(output: str) -> int:
    """How many tests passed outright, by pytest's closing counts."""
    found = PASSED.search(counts(output))
    return int(found.group(1)) if found else 0


def classify(run: Run, timeout: float) -> tuple[str, str, list[str]]:
    """The result of a test run, a short detail, and the ids of the tests
    that failed."""
    failed = failures(run.output)
    code = run.returncode
    if run.stopped == "signal":
        return INTERRUPTED, "stopped by a signal", failed
    if run.stopped == "timeout":
        return ERROR, f"timed out after {timeout:g} s", failed
    if code == 0:
        return SURVIVED, counts(run.output) or "passed", failed
    if code == 1 and failed:
        return KILLED, counts(run.output) or f"{len(failed)} failed", failed
    if code == 1:
        return ERROR, "pytest exited 1 but reported no failing test", failed
    # A module that fails to import ends collection with exit 2, or with 4
    # when a node id in it then matches nothing.
    if code in (2, 4) and COLLECTION_ERROR.search(run.output):
        return ERROR, "collection error", failed
    if code == 4:
        usage = [
            line.removeprefix("ERROR:").strip()
            for line in run.output.splitlines()
            if line.startswith("ERROR:")
        ]
        return ERROR, "; ".join(["pytest usage error", *usage[:1]]), failed
    if code == 5:
        return ERROR, "no tests ran", failed
    if code is None:
        return ERROR, "pytest could not be stopped", failed
    if code < 0:
        return ERROR, f"pytest died of {signal_name(-code)}", failed
    return ERROR, f"pytest exit status {code}", failed


def signal_name(signum: int) -> str:
    """SIGKILL for 9; a real-time signal has no name of its own."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def tail(output: str) -> str:
    return "\n".join(output.rstrip().splitlines()[-TAIL:])


def exited(child: subprocess.Popen, timeout: float) -> bool:
    """Whether the child has exited, or does within `timeout` seconds.

    The child is left unreaped, so its pid, and with it the id of the
    process group it leads, cannot be given to another process yet."""
    if child.returncode is not None:
        return True
    deadline = time.monotonic() + timeout
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            if os.waitid(os.P_PID, child.pid, flags) is not None:
                return True
        except ChildProcessError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def signal_group(child: subprocess.Popen, signum: int) -> None:
    """Send `signum` to the process group of a child that leads one."""
    # Only while the child is unreaped does its pid still name its group.
    if child.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signum)


def describe(tests: list[str]) -> str:
    shown = ", ".join(tests[:3])
    return shown + (f" and {len(tests) - 3} more" if len(tests) > 3 else "")


def _rounded(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds, 3)


def _write(stream, text: str) -> None:
    # After SIGHUP the terminal may be gone; the restore matters more.
    with contextlib.suppress(OSError):
        stream.write(text)
        stream.flush()


def exit_status(
    results: list[str],
    *,
    allow_survivors: bool,
    problems: bool,
    signum: int | None,
) -> int:
    """128 + N after signal N; 2 after a problem (a refusal, a failing
    baseline or a failed restore) or a mutation's error; 1 when a mutant
    survived and survivors are not allowed; otherwise 0."""
    if signum is not None:
        return 128 + signum
    if problems or ERROR in results:
        return 2
    if SURVIVED in results and not allow_survivors:
        return 1
    return 0


def table(results: list[Result]) -> str:
    """The results as a Markdown table, padded to line up in a terminal."""
    rows = [("mutation", "file", "result", "time", "detail")]
    for result in results:
        mutation = result.mutation
        seconds = "" if result.seconds is None else f"{result.seconds:.1f}s"
        where = mutation.rel or mutation.file
        rows.append(
            (mutation.name, where, result.result, seconds, result.detail)
        )
    cells = [
        [" ".join(cell.split()).replace("|", r"\|") for cell in row]
        for row in rows
    ]
    widths = [max(len(row[i]) for row in cells) for i in range(len(rows[0]))]
    lines = [
        "| "
        + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))
        + " |"
        for row in cells
    ]
    lines.insert(1, "|" + "|".join("-" * (w + 2) for w in widths) + "|")
    return "\n".join(lines) + "\n"


# -- the harness ------------------------------------------------------------


class Harness:
    """Runs the mutations of one spec, one at a time."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root: Path | None = None
        self.signum: int | None = None
        self.results: list[Result] = []
        # one entry per list of tests the baseline ran
        self.baseline: list[dict] | None = None
        self.problems: list[str] = []
        self.unrestored: list[str] = []

    def on_signal(self, signum: int, frame: object) -> None:
        # Only note the signal: raising here could interrupt a restore.
        # The main loop polls for it and stops the test run.
        if self.signum is None:
            self.signum = signum

    def run(self) -> int:
        root, problem = repository_root()
        if root is None:
            return self._refuse([problem])
        self.root = root
        mutations, problems = load_spec(self.args.spec)
        if not problems:
            problems = check(mutations, root)
        if self.signum is not None:
            return self._finish()
        if problems:
            return self._refuse(problems)
        if os.environ.get("PYTEST_ADDOPTS"):
            self._say("PYTEST_ADDOPTS is set here; pytest runs without it")
        self.results = [Result(mutation) for mutation in mutations]
        if not self.args.no_baseline and not self._baseline(mutations):
            return self._finish()
        total = len(self.results)
        for position, result in enumerate(self.results, start=1):
            if self.signum is not None:
                break
            self._mutate(result, f"[{position}/{total}]")
            if self.unrestored or result.result == INTERRUPTED:
                break
            if self._fails(result) and not self.args.keep_going:
                break
        return self._finish()

    def _fails(self, result: Result) -> bool:
        if result.result == SURVIVED:
            return not self.args.allow_survivors
        return result.result == ERROR

    def _baseline(self, mutations: list[Mutation]) -> bool:
        """Run each distinct list of tests on the unmutated tree, just as
        the mutants' runs will: a test may pass after another yet fail
        alone. True if every list passes; stops at the first that fails."""
        self.baseline = []
        paths = {mutation.path for mutation in mutations}
        for tests in dict.fromkeys(tuple(m.tests) for m in mutations):
            if self.signum is not None:
                return False
            try:
                for path in paths:
                    clear_bytecode(path)
            except OSError as exc:
                self._problem(f"cannot delete stale bytecode: {exc}")
                return False
            if not self._baseline_run(list(tests)):
                return False
        return True

    def _baseline_run(self, tests: list[str]) -> bool:
        self._say(f"baseline: running {describe(tests)} unmutated")
        run = self.run_tests(tests)
        result, detail, _ = classify(run, self.args.timeout)
        detail = self._relative(detail)
        # A run whose tests only skip or xfail passes, but proves nothing:
        # every mutant would survive it.
        passed = result == SURVIVED and passes(run.output) > 0
        self.baseline.append(
            {
                "tests": tests,
                "result": "passed" if passed else "failed",
                "seconds": _rounded(run.seconds),
                "returncode": run.returncode,
                "detail": detail,
                "output_tail": "" if passed else tail(run.output),
            }
        )
        if passed:
            self._say(f"baseline: {detail} in {run.seconds:.1f} s")
        elif result == SURVIVED:
            self._problem(
                f"no test in {describe(tests)} passes unmutated ({detail}), "
                "so every mutant would survive; they only skip or xfail, or "
                "pytest printed no counts"
            )
        elif result != INTERRUPTED:
            self._problem(
                f"the tests in {describe(tests)} fail unmutated ({detail}), "
                "so no kill would prove anything; fix them or their ids"
            )
            self._tail(run.output)
        return passed

    def _read(self, mutation: Mutation):
        """The file's stat, its bytes and its mutant, or why the mutation
        cannot run. The tree may have changed since check()."""
        problem = git_problem(self.root, mutation.rel)
        if problem is not None:
            return None, problem
        try:
            stat = mutation.path.stat()
            data = mutation.path.read_bytes()
        except OSError as exc:
            return None, f"cannot read {mutation.rel}: {exc}"
        mutant, problem = prepare(mutation, data)
        if problem is not None:
            return None, problem
        return (stat, data, mutant), None

    def _mutate(self, result: Result, position: str) -> None:
        mutation = result.mutation
        path = mutation.path
        started = time.monotonic()
        read, problem = self._read(mutation)
        if read is None:
            result.result, result.detail = ERROR, problem
            result.seconds = time.monotonic() - started
            self._say(f"{position} {mutation.name}: error ({problem})")
            return
        stat, data, mutant = read
        self._say(
            f"{position} {mutation.name}: mutating {mutation.rel}, "
            f"running {describe(mutation.tests)}"
        )
        run = None
        failure = None
        try:
            try:
                path.write_bytes(mutant)
                clear_bytecode(path)
            except OSError as exc:
                failure = f"cannot write the mutant: {exc}"
            else:
                if self.signum is None:
                    run = self.run_tests(mutation.tests)
        finally:
            problem = restore(path, data, stat)
        result.seconds = time.monotonic() - started
        if problem is not None:
            self.unrestored.append(mutation.rel)
            self._problem(
                f"COULD NOT RESTORE {mutation.rel}: {problem}. "
                f"Recover it with: git checkout -- {mutation.rel}"
            )
        elif (unclean := git_problem(self.root, mutation.rel)) is not None:
            # The bytes are back, yet git disagrees: a test changed the
            # index, the file's type or its mode, say.
            self.unrestored.append(mutation.rel)
            self._problem(
                f"{mutation.rel} is restored, but git does not show it "
                f"clean: {unclean}. Check it with: git status; "
                f"git diff HEAD -- {mutation.rel}"
            )
        if failure is not None:
            result.result, result.detail = ERROR, failure
        elif run is None:
            result.result = INTERRUPTED
            result.detail = "stopped before the tests ran"
        else:
            result.result, detail, result.failed = classify(
                run, self.args.timeout
            )
            result.detail = self._relative(detail)
            result.returncode = run.returncode
            result.output = run.output
        self._say(
            f"{position} {mutation.name}: {result.result} in "
            f"{result.seconds:.1f} s ({result.detail})"
        )
        if result.result == KILLED:
            for test in result.failed:
                self._say(f"    failed: {test}")
        elif result.result == ERROR:
            self._tail(result.output)

    def run_tests(self, tests: list[str]) -> Run:
        """Run pytest on `tests` in a process group of its own. Stop the
        whole group on a timeout or a signal, and kill whatever is left in
        it once pytest has exited."""
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            # Output goes to a file, not a pipe, so that a straggling
            # grandchild holding it open cannot keep the harness waiting.
            child = subprocess.Popen(
                pytest_command(tests),
                cwd=self.root,
                env=pytest_environment(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            stopped = None
            try:
                stopped = self._wait(child, started + self.args.timeout)
            finally:
                # _wait() halts the run itself, unless something escaped it
                if child.returncode is None and stopped is None:
                    self._halt(child)
            output.seek(0)
            text = output.read().decode("utf-8", "replace")
        seconds = time.monotonic() - started
        return Run(child.returncode, text, seconds, stopped)

    def _wait(self, child: subprocess.Popen, deadline: float) -> str | None:
        """Wait for pytest. Returns why the harness stopped it, if it did."""
        while True:
            if self.signum is not None:
                self._halt(child)
                return "signal"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._halt(child)
                return "timeout"
            if exited(child, min(remaining, POLL)):
                self._reap(child)
                return None

    def _halt(self, child: subprocess.Popen) -> None:
        """SIGINT to pytest's process group, so that pytest can tear its
        fixtures down. Once pytest exits, or the grace period is over,
        SIGKILL to what is left of the group."""
        signal_group(child, signal.SIGINT)
        exited(child, self.args.grace)
        self._reap(child)

    def _reap(self, child: subprocess.Popen) -> None:
        # pytest is unreaped, so its pid still names its process group:
        # kill whatever its tests left running there, then reap it.
        signal_group(child, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=self.args.grace)

    # -- output -------------------------------------------------------------

    def _relative(self, text: str) -> str:
        # pytest names an unknown test by its absolute path
        return text.replace(f"{self.root}{os.sep}", "")

    def _say(self, message: str) -> None:
        if not self.args.quiet:
            _write(sys.stderr, f"mutate: {message}\n")

    def _tail(self, output: str) -> None:
        if output and not self.args.quiet:
            lines = tail(output).splitlines()
            _write(sys.stderr, "".join(f"    {line}\n" for line in lines))

    def _problem(self, message: str) -> None:
        self.problems.append(message)
        _write(sys.stderr, f"mutate: {message}\n")

    def _refuse(self, problems: list[str]) -> int:
        self.problems += problems
        _write(sys.stderr, "mutate: refusing to run; nothing was changed:\n")
        _write(sys.stderr, "".join(f"  - {p}\n" for p in problems))
        return self._finish()

    def _finish(self) -> int:
        if self.signum is not None:
            name = signal_name(self.signum)
            restored = "" if self.unrestored else "; files restored"
            _write(sys.stderr, f"mutate: stopped by {name}{restored}\n")
        if any(result.result != NOT_RUN for result in self.results):
            _write(sys.stdout, table(self.results))
        status = exit_status(
            [result.result for result in self.results],
            allow_survivors=self.args.allow_survivors,
            problems=bool(self.problems),
            signum=self.signum,
        )
        if self.args.json is not None:
            text = json.dumps(self._report(status), indent=2) + "\n"
            try:
                self.args.json.write_text(text)
            except OSError as exc:
                _write(sys.stderr, f"mutate: cannot write the report: {exc}\n")
                status = max(status, 2)
        return status

    def _report(self, status: int) -> dict:
        commit = None
        if self.root is not None:
            head = git(self.root, "rev-parse", "HEAD")
            commit = head.stdout.strip() if head.returncode == 0 else None
        tally = Counter(result.result for result in self.results)
        return {
            "commit": commit,
            "timeout": self.args.timeout,
            "baseline": self.baseline,
            "mutations": [result.as_json() for result in self.results],
            "counts": {name: tally[name] for name in RESULTS},
            "problems": self.problems,
            "signal": signal_name(self.signum) if self.signum else None,
            "exit_status": status,
        }


@contextlib.contextmanager
def stopping_on_signals(handler):
    """Route SIGINT, SIGTERM and SIGHUP to `handler` for the duration,
    except a signal the process inherited as ignored, as a shell's
    background jobs do: an ignored signal never stops the harness."""
    previous = {}
    for signum in STOP_SIGNALS:
        current = signal.getsignal(signum)
        if current == signal.SIG_IGN:
            continue
        previous[signum] = signal.SIG_DFL if current is None else current
        signal.signal(signum, handler)
    try:
        yield
    finally:
        for signum, current in previous.items():
            signal.signal(signum, current)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not value >= 0:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number of seconds"
        )
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "spec", type=Path, metavar="SPEC", help="the TOML spec to run"
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every mutation, past survivors and errors",
    )
    parser.add_argument(
        "--allow-survivors",
        action="store_true",
        help="neither stop at a survivor nor exit non-zero for one",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="skip the run of the listed tests on the unmutated tree",
    )
    parser.add_argument(
        "--timeout",
        type=_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="stop any one pytest run after this long (default %(default)g)",
    )
    parser.add_argument(
        "--grace",
        type=_seconds,
        default=DEFAULT_GRACE,
        metavar="SECONDS",
        help="how long a stopped pytest run has to exit after SIGINT, "
        "before SIGKILL (default %(default)g)",
    )
    parser.add_argument(
        "--json", type=Path, metavar="PATH", help="also write a JSON report"
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print only the table and any problems",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be more than 0 seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    # An ignored SIGCHLD survives exec. With it, the kernel reaps pytest
    # as soon as it exits, and its exit status reads as 0: every run,
    # however its tests fail, would pass.
    if hasattr(signal, "SIGCHLD"):
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = parse_args(argv)
    harness = Harness(args)
    with stopping_on_signals(harness.on_signal):
        try:
            return harness.run()
        except Exception:
            # Exit 1 means a survivor; an internal error must not look
            # like one. Each mutation's finally block has run by now.
            traceback.print_exc()
            _write(sys.stderr, "mutate: internal error\n")
            return 2


if __name__ == "__main__":
    sys.exit(main())
