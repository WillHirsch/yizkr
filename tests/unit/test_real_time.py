"""The finder of real-clock uses in a test module (tests/fixtures/real_time,
#432): every spelling of a sleep, a clock read, a timed wait or a real
timer is found, and what a test may do is not."""

from __future__ import annotations

import textwrap

import pytest

from tests.fixtures.real_time import real_time_uses


def uses(source: str, **options) -> list[str]:
    return real_time_uses(textwrap.dedent(source), **options)


@pytest.mark.parametrize(
    "source",
    [
        "import time\ntime.sleep(0)\n",
        "import time as t\nt.sleep(0)\n",
        "import time\ntime.time()\n",
        "import time\ntime.monotonic_ns()\n",
        "import time\ntime.perf_counter()\n",
        "import datetime\ndatetime.datetime.now()\n",
        "from datetime import UTC, datetime\ndatetime.now(UTC)\n",
        "from datetime import datetime\ndatetime.utcnow()\n",
        "from datetime import date\ndate.today()\n",
        "import threading\nthreading.Timer(1, print)\n",
        "import threading\nthreading.Event().wait(0.01)\n",
        "import threading\nthreading.Event().wait(timeout=5)\n",
        "def f(thread):\n    thread.join(5)\n",
        "def f(thread):\n    thread.join(timeout=1.5)\n",
        "def f(event, seconds):\n    event.wait(timeout=seconds)\n",
        "def f(event):\n    event.wait(False)\n",
        "def f(thread):\n    thread.join(timeout=0.001)\n",
        "START_TIMER(0.05, print)\n",
        "START_TIMER(seconds, print)\n",
        "START_TIMER(False, print)\n",
    ],
)
def test_each_spelling_of_a_real_clock_is_found(source):
    assert len(uses(source, real_timers={"START_TIMER"})) == 1


def test_a_from_time_import_is_found_whatever_it_imports():
    assert uses("from time import sleep\nsleep(0)\n") == [
        "line 1: from time import sleep",
        "line 2: sleep(",
    ]
    assert uses("from time import struct_time\n") == [
        "line 1: from time import struct_time"
    ]


def test_what_a_test_may_do_is_not_found():
    source = """
        import threading
        from datetime import UTC, datetime, timedelta

        HANG_GUARD = 60
        start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1)
        ", ".join(["a", "b"])
        "".join(chunk for chunk in "ab")
        threading.Event().wait(timeout=HANG_GUARD)
        threading.Thread(target=print).join(HANG_GUARD)
        START_TIMER(0, print)
        START_TIMER(0.0, print)
        threading.Event().wait(0)
        threading.Thread(target=print).join(timeout=0.0)
    """
    assert (
        uses(source, hang_guards={"HANG_GUARD"}, real_timers={"START_TIMER"})
        == []
    )


def test_the_exempt_classes_and_functions_are_left_out():
    source = """
        import time

        class FakeClock:
            def now(self):
                return time.monotonic()

        def test_unstarted():
            START_TIMER(10, print)

        def test_other():
            time.sleep(1)
    """
    assert uses(
        source,
        exempt={"FakeClock", "test_unstarted"},
        real_timers={"START_TIMER"},
    ) == ["line 12: sleep("]


def test_the_uses_come_in_line_order():
    source = "import time\n" + "time.sleep(0)\n" * 12
    found = uses(source)
    assert found == [f"line {n}: sleep(" for n in range(2, 14)]
