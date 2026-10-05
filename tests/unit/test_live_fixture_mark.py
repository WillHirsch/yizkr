"""The live tests' session marker, offline (#430; docs/TESTING.md, The
mutation harness).

`fixture_mark` must be fixed within one pytest session, so a rewrite finds
the session's own items, and differ between sessions, so no session, a
live mutant's run included, writes onto the items a later one reads. A
live run cannot show the second, since each run is one session.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from tests.integration import conftest
from tests.integration.conftest import fixture_mark
from yizkr.pipeline import registrar, writer

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = "a birth register extract issued by the Standesamt Flörburg"


def mark_in_a_process_of_its_own() -> str:
    """The marker another session (a process of its own) gives FIXTURE
    over the writer."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.integration.conftest import fixture_mark\n"
            "from yizkr.pipeline import writer\n"
            f"print(fixture_mark({FIXTURE!r}, writer))",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_the_mark_is_fixed_within_a_session():
    first = fixture_mark(FIXTURE, writer)
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert fixture_mark(FIXTURE, writer) == first
    assert len(conftest.SESSION_BYTES) == 8


def test_each_session_draws_a_mark_of_its_own():
    marks = {mark_in_a_process_of_its_own() for _ in range(2)}
    assert len(marks) == 2
    assert fixture_mark(FIXTURE, writer) not in marks


def test_the_fixture_and_the_code_under_test_change_the_mark():
    mark = fixture_mark(FIXTURE, writer)
    assert fixture_mark(FIXTURE + ", again", writer) != mark
    assert fixture_mark(FIXTURE) != mark
    assert fixture_mark(FIXTURE, writer, registrar) != mark
    assert fixture_mark(FIXTURE, registrar) != mark
