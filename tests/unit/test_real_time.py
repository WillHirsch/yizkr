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
        "def f(event, seconds):\n    event.wait(seconds)\n",
        "DELAY = 0.5\n\ndef f(event):\n    event.wait(DELAY)\n",
        "def f(event):\n    event.wait(0.1 * 5)\n",
        "def f(thread):\n    thread.join(-1 + 2)\n",
        "def f(event):\n    event.wait(timeout=None or 5)\n",
        "import threading\nthreading.Barrier(2, timeout=30)\n",
        "import threading\nthreading.Barrier(2, None, 30)\n",
        "def f(c, p):\n    c.wait_for(p, timeout=5)\n",
        "def f(lock):\n    lock.acquire(timeout=0.5)\n",
        "def f(q):\n    q.get(timeout=0.5)\n",
        "def f(q):\n    q.put(1, timeout=0.5)\n",
        "def f(s):\n    s.settimeout(5)\n",
        "import select\nselect.select([], [], [], 0.5)\n",
        "import signal\nsignal.alarm(1)\n",
        "import signal\nsignal.setitimer(signal.ITIMER_REAL, 1)\n",
        "import time\ntime.localtime()\n",
        "import time as t\nt.gmtime()\n",
        "import time\ntime.ctime()\n",
        "import time\ntime.asctime()\n",
        "import time\ntime.strftime('%Y')\n",
        "import time\nclock = time.monotonic\n",
        "import time as t\nf(sleep=t.sleep)\n",
        "from datetime import datetime\nf = datetime.now\n",
        "import datetime\nf = datetime.date.today\n",
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
        import select
        import threading
        import time
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
        threading.Event().wait(None)
        threading.Event().wait(timeout=None)
        threading.Barrier(2, timeout=HANG_GUARD)
        threading.Barrier(2)
        "-".join(parts)
        separator.join(parts)
        {}.get("key", 5)
        helper.wait_for("busy", 5)
        select.select([], [], [], HANG_GUARD)
        select.select([], [], [], 0)
        sock.settimeout(HANG_GUARD)
        sock.settimeout(None)
        time.localtime(0)
        time.gmtime(0)
        time.strftime("%Y", time.gmtime(0))
        stamp.strftime("%Y")
        clock.now
        clock.sleep
        start.today
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
