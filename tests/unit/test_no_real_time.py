"""No unit test depends on real elapsed time or on the real wall clock
(#437): every module in tests/unit, conftest.py included and each new one
as it is added, is read by the finder in tests/fixtures/real_time.py, and
each use it finds fails here unless the allow-list below names it, with
its reason.

A wait kept only as a hang guard takes HANG_GUARD as its timeout, and
anomalies' real timer starter is checked to start its timers with a wait
of 0, in every module. An entry that no longer matches a use is stale and
fails, as does an entry naming a module that does not exist. A held
module, which another open pull request edits, is left out whole until
that request merges.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.real_time import real_time_uses

UNIT = Path(__file__).parent
# the timeout a wait kept only as a hang guard names
HANG_GUARDS = frozenset({"HANG_GUARD"})
# the names a real timer is started by, which must ask for a wait of 0
REAL_TIMERS = frozenset({"START_TIMER", "_start_timer"})
# the call of an entry that holds its module whole
HELD = "*"


@dataclass(frozen=True)
class Allowed:
    """A use of the real clock a module may keep: `call` is the use as the
    finder words it without its line ('sleep(', 'wait( with a timeout'),
    or HELD for the whole module; `inside` limits it to one class or
    function."""

    file: str
    call: str
    reason: str
    inside: str = ""


ALLOWED = (
    Allowed(
        "test_anomalies.py",
        "START_TIMER( with a wait",
        "its timer is made, never started: threading.Timer is patched, so"
        " the clamp on the wait is checked without a thread",
        inside="test_a_timer_waits_at_most_the_longest_wait_a_thread_can",
    ),
    Allowed("test_backup.py", HELD, "phase 2 of #437, after PR #429 merges"),
    Allowed("test_llmcache.py", HELD, "phase 2 of #437, after PR #439 merges"),
    Allowed(
        "test_scratch_estimate.py",
        HELD,
        "phase 2 of #437, after PR #439 merges",
    ),
    Allowed("test_scratch.py", HELD, "phase 2 of #437, after PR #419 merges"),
)


def _uses(source: str, exempt: Iterable[str] = ()) -> set[str]:
    return set(
        real_time_uses(
            source,
            exempt=exempt,
            hang_guards=HANG_GUARDS,
            real_timers=REAL_TIMERS,
        )
    )


def _call(use: str) -> str:
    """A use without its line: 'line 3: sleep(' -> 'sleep('."""
    return use.split(": ", 1)[1]


def _line(use: str) -> int:
    return int(use.split()[1].rstrip(":"))


def problems(directory: Path, allowed: Iterable[Allowed]) -> list[str]:
    """What fails the guard over the modules in `directory`: each use of
    the real clock no entry of `allowed` names, each entry that matches
    no use (a held module's excepted) and each entry naming no module."""
    allowed = tuple(allowed)
    found = [
        f"{entry.file}: the allow-list names a module that does not exist"
        for entry in allowed
        if not (directory / entry.file).is_file()
    ]
    held = {entry.file for entry in allowed if entry.call == HELD}
    matched: set[Allowed] = set()
    for path in sorted(directory.rglob("*.py")):
        name = path.relative_to(directory).as_posix()
        if name in held:
            continue
        source = path.read_text(encoding="utf-8")
        entries = [entry for entry in allowed if entry.file == name]
        uses = _uses(source)
        within = {
            entry.inside: uses - _uses(source, exempt={entry.inside})
            for entry in entries
            if entry.inside
        }
        for use in sorted(uses, key=_line):
            covering = [
                entry
                for entry in entries
                if entry.call == _call(use)
                and (not entry.inside or use in within[entry.inside])
            ]
            matched.update(covering)
            if not covering:
                found.append(f"{name} {use}: no allow-list entry names it")
    found.extend(
        f"{entry.file}: the allow-list entry for {entry.call!r}"
        + (f" inside {entry.inside}" if entry.inside else "")
        + " matches no use; remove it"
        for entry in allowed
        if entry.call != HELD
        and entry not in matched
        and (directory / entry.file).is_file()
    )
    return found


def test_no_unit_test_waits_on_or_reads_the_real_clock():
    """Each use of the real clock in tests/unit is one the allow-list
    names, with its reason, and each entry still matches a use."""
    assert problems(UNIT, ALLOWED) == []


def test_each_allow_list_entry_gives_its_reason():
    assert all(entry.reason.strip() for entry in ALLOWED)
    assert len(set(ALLOWED)) == len(ALLOWED)


class TestTheGuard:
    """The guard's own logic, on modules written to a temporary folder."""

    @staticmethod
    def write(directory: Path, name: str, source: str) -> None:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")

    def test_a_new_module_that_sleeps_is_caught_by_file_and_line(
        self, tmp_path
    ):
        self.write(tmp_path, "test_clean.py", "def test_x():\n    pass\n")
        self.write(
            tmp_path,
            "test_new.py",
            """
            import time

            def test_x():
                time.sleep(0.1)
            """,
        )
        assert problems(tmp_path, ()) == [
            "test_new.py line 5: sleep(: no allow-list entry names it"
        ]

    def test_conftest_and_modules_in_subfolders_are_read(self, tmp_path):
        self.write(tmp_path, "conftest.py", "import time\ntime.time()\n")
        self.write(tmp_path, "deeper/test_x.py", "from time import sleep\n")
        assert problems(tmp_path, ()) == [
            "conftest.py line 2: time(: no allow-list entry names it",
            "deeper/test_x.py line 1: from time import sleep:"
            " no allow-list entry names it",
        ]

    def test_hang_guards_polls_and_real_timers_follow_the_suite_rules(
        self, tmp_path
    ):
        self.write(
            tmp_path,
            "test_x.py",
            """
            HANG_GUARD = 60

            def test_x(event, thread):
                assert event.wait(HANG_GUARD)
                thread.join(timeout=HANG_GUARD)
                assert event.wait(0) is False
                START_TIMER(0, print)
                event.wait(5)
                _start_timer(1, print)
            """,
        )
        assert problems(tmp_path, ()) == [
            "test_x.py line 9: wait( with a timeout:"
            " no allow-list entry names it",
            "test_x.py line 10: _start_timer( with a wait:"
            " no allow-list entry names it",
        ]

    def test_an_entry_allows_only_its_call_in_its_module(self, tmp_path):
        source = "import time\ntime.sleep(1)\ntime.sleep(2)\ntime.time()\n"
        self.write(tmp_path, "test_a.py", source)
        self.write(tmp_path, "test_b.py", source)
        allowed = [Allowed("test_a.py", "sleep(", "a reason")]
        assert problems(tmp_path, allowed) == [
            "test_a.py line 4: time(: no allow-list entry names it",
            "test_b.py line 2: sleep(: no allow-list entry names it",
            "test_b.py line 3: sleep(: no allow-list entry names it",
            "test_b.py line 4: time(: no allow-list entry names it",
        ]

    def test_an_entry_inside_a_function_allows_its_call_only_there(
        self, tmp_path
    ):
        self.write(
            tmp_path,
            "test_x.py",
            """
            import time

            def test_allowed():
                time.sleep(1)
                time.time()

            def test_other():
                time.sleep(1)
            """,
        )
        allowed = [Allowed("test_x.py", "sleep(", "why", "test_allowed")]
        assert problems(tmp_path, allowed) == [
            "test_x.py line 6: time(: no allow-list entry names it",
            "test_x.py line 9: sleep(: no allow-list entry names it",
        ]

    @pytest.mark.parametrize(
        "entry",
        [
            Allowed("test_x.py", "sleep(", "why"),
            Allowed("test_x.py", "time(", "why", "test_x"),
        ],
    )
    def test_an_entry_that_matches_no_use_is_stale(self, tmp_path, entry):
        self.write(
            tmp_path,
            "test_x.py",
            """
            import time

            def test_x():
                pass

            def test_y():
                time.time()
            """,
        )
        allowed = [entry, Allowed("test_x.py", "time(", "why")]
        where = f" inside {entry.inside}" if entry.inside else ""
        assert problems(tmp_path, allowed) == [
            f"test_x.py: the allow-list entry for {entry.call!r}{where}"
            " matches no use; remove it"
        ]

    def test_a_held_module_is_left_out_whole_and_never_stale(self, tmp_path):
        self.write(tmp_path, "test_held.py", "import time\ntime.sleep(1)\n")
        self.write(tmp_path, "test_quiet.py", "x = 1\n")
        allowed = [
            Allowed("test_held.py", HELD, "phase 2"),
            Allowed("test_quiet.py", HELD, "phase 2"),
        ]
        assert problems(tmp_path, allowed) == []
        assert problems(tmp_path, allowed[1:]) == [
            "test_held.py line 2: sleep(: no allow-list entry names it"
        ]

    @pytest.mark.parametrize("call", [HELD, "sleep("])
    def test_an_entry_naming_no_module_fails(self, tmp_path, call):
        self.write(tmp_path, "test_x.py", "x = 1\n")
        assert problems(tmp_path, [Allowed("test_gone.py", call, "why")]) == [
            "test_gone.py: the allow-list names a module that does not exist"
        ]
