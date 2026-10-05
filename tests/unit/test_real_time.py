"""The finder of real-clock uses in a test module (tests/fixtures/real_time,
#432): every spelling of a sleep, a clock read, a timed wait or a real
timer is found, and what a test may do is not."""

from __future__ import annotations

import textwrap
import time

import pytest

from tests.fixtures.real_time import (
    CLOCK_CALLS,
    DATETIME_CLOCKS,
    real_time_uses,
    scopes_named,
)

# the time module's clock functions, each found called and uncalled
TIME_CLOCKS = (
    "sleep",
    "time",
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "process_time",
    "process_time_ns",
    "thread_time",
    "thread_time_ns",
    "clock_gettime",
    "clock_gettime_ns",
)
# a call of each clock call the finder must find, written out by name so
# that taking a name out of CLOCK_CALLS fails its case
CALLED = {
    **{name: f"import time\ntime.{name}()\n" for name in TIME_CLOCKS},
    **{
        name: f"from datetime import datetime\ndatetime.{name}()\n"
        for name in ("now", "utcnow", "today")
    },
    "Timer": "import threading\nthreading.Timer(1, print)\n",
    "alarm": "import signal\nsignal.alarm(1)\n",
    "setitimer": "import signal\nsignal.setitimer(signal.ITIMER_REAL, 1)\n",
    "call_later": "def f(loop):\n    loop.call_later(1, print)\n",
    "call_at": "def f(loop):\n    loop.call_at(when, print)\n",
}


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
        "import time\ntime.clock_gettime(time.CLOCK_MONOTONIC)\n",
        "import time as t\nt.clock_gettime_ns(t.CLOCK_REALTIME)\n",
        "import time\nclock = time.clock_gettime\n",
        "import time as t\nf(clock=t.clock_gettime_ns)\n",
        "from threading import Timer as Later\nLater(1, print)\n",
        "from signal import alarm as wake\nwake(1)\n",
        "def f(selector):\n    selector.select(0.5)\n",
        "def f(selector):\n    selector.select(timeout=0.5)\n",
        "def f(thread):\n    thread.join(float('0.5'))\n",
        "def f(thread, slow):\n    thread.join(0.5 if slow else 1)\n",
        "def f(thread):\n    thread.join(-0.5)\n",
        "from select import select\nselect([], [], [], 0.5)\n",
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
        'import time\ngetattr(time, "sleep")(1)\n',
        'import time as t\nclock = getattr(t, "monotonic")\n',
        'from datetime import datetime\ngetattr(datetime, "now")()\n',
        'import datetime\ngetattr(datetime.date, "today")()\n',
        'import time\ntime.__dict__["sleep"](1)\n',
        'import time\nvars(time)["time"]()\n',
        "import asyncio\nasyncio.timeout(1)\n",
        "import asyncio\nasyncio.timeout(delay=0.5)\n",
        "import asyncio\nasyncio.timeout_at(when)\n",
        "import asyncio\nasyncio.timeout_at(0)\n",
        "def f(future):\n    future.result(timeout=1)\n",
        "def f(future):\n    future.exception(timeout=0.5)\n",
        "def f(conn):\n    conn.poll(1)\n",
        "def f(conn):\n    conn.poll(timeout=0.5)\n",
        "import select\nselect.poll().poll(500)\n",
        "import os\nos.utime(path)\n",
        "import os\nos.utime(path, None)\n",
        "def f(path):\n    path.touch()\n    return path.stat().st_mtime_ns\n",
        "import os\n\ndef f(p):\n    p.touch()\n    os.path.getmtime(p)\n",
        "path.touch()\nstamp = path.stat().st_ctime\n",
    ],
)
def test_each_spelling_of_a_real_clock_is_found(source):
    assert len(uses(source, real_timers={"START_TIMER"})) == 1


def test_each_clock_call_has_a_case_here():
    assert set(CALLED) == CLOCK_CALLS
    assert {"now", "utcnow", "today"} == DATETIME_CLOCKS
    assert all(hasattr(time, name) for name in TIME_CLOCKS)


@pytest.mark.parametrize("name", sorted(CALLED))
def test_each_clock_call_is_found_called(name):
    assert [use.split(": ", 1)[1] for use in uses(CALLED[name])] == [
        f"{name}("
    ]


@pytest.mark.parametrize("name", TIME_CLOCKS)
def test_each_clock_function_of_the_time_module_is_found_uncalled(name):
    assert uses(f"import time as t\nclock = t.{name}\n") == [
        f"line 2: {name} uncalled"
    ]


@pytest.mark.parametrize("name", ["now", "utcnow", "today"])
def test_each_clock_of_a_datetime_or_a_date_is_found_uncalled(name):
    for receiver in ("datetime", "date"):
        source = (
            f"from datetime import {receiver}\nclock = {receiver}.{name}\n"
        )
        assert uses(source) == [f"line 2: {name} uncalled"]


def test_a_clock_call_imported_under_another_name_is_found_by_its_own():
    source = """
        from threading import Thread as Worker, Timer as Later

        Worker(target=print)
        Later(1, print)
    """
    assert uses(source) == ["line 5: Timer("]


def test_a_from_time_import_is_found_whatever_it_imports():
    assert uses("from time import sleep\nsleep(0)\n") == [
        "line 1: from time import sleep",
        "line 2: sleep(",
    ]
    assert uses("from time import struct_time\n") == [
        "line 1: from time import struct_time"
    ]


@pytest.mark.parametrize(
    "call",
    [
        "thread.join(HANG_GUARD / 600)",
        "selector.select(HANG_GUARD / 600)",
        "thread.join(min(HANG_GUARD, seconds))",
        "thread.join(HANG_GUARD if slow else seconds)",
    ],
)
def test_a_lone_argument_that_makes_a_time_of_a_hang_guard_is_found(call):
    """A `.join` or a selector's `.select` given a hang guard worked on
    waits for less, or for longer, than the hang guard itself."""
    source = f"HANG_GUARD = 60\n{call}\n"
    assert len(uses(source, hang_guards={"HANG_GUARD"})) == 1


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
        "".join(map(str, [1, 2]))
        "".join(sorted(names))
        "-".join(str(n) for n in range(3))
        ", ".join(names if names else ["none"])
        ", ".join([name] * 4)
        "".join("ab" * 4)
        "".join(4 * b"ab")
        " ".join([part] * (length // 2 + 1) + ([end] if end else []))
        "".join(map(chr, (0x09, 0x20)))
        {}.get("key", 5)
        helper.wait_for("busy", 5)
        select.select([], [], [], HANG_GUARD)
        select.select([], [], [], 0)
        selector.select()
        selector.select(None)
        selector.select(HANG_GUARD)
        selector.select(seconds)
        soup.select("a.link")
        sock.settimeout(HANG_GUARD)
        sock.settimeout(None)
        time.localtime(0)
        time.gmtime(0)
        time.strftime("%Y", time.gmtime(0))
        stamp.strftime("%Y")
        clock.now
        clock.sleep
        start.today
        getattr(time, "struct_time")
        getattr(clock, "sleep")
        getattr(time, name)
        time.__dict__["struct_time"]
        vars(clock)["sleep"]
        asyncio.timeout(HANG_GUARD)
        asyncio.timeout(None)
        asyncio.timeout_at(None)
        helper.timeout(5)
        future.result()
        future.result(timeout=HANG_GUARD)
        future.exception(timeout=None)
        process.poll()
        conn.poll(0)
        conn.poll(HANG_GUARD)
        conn.poll(seconds)
        os.utime(path, ns=(1, 1))
        os.utime(path, (1, 1))
        os.utime(path, times=(1, 1))
        before = path.stat().st_mtime_ns

        def touched(path):
            path.touch()

        def stamped(path):
            return path.stat().st_mtime_ns
    """
    assert (
        uses(source, hang_guards={"HANG_GUARD"}, real_timers={"START_TIMER"})
        == []
    )


def test_a_file_time_is_found_only_where_a_file_is_touched():
    """A touch in one function marks the file time read there, not one
    read in another function, nor one read beside the function in the
    module."""
    source = """
        def touched(path):
            path.touch()
            return path.stat().st_mtime

        def read(path):
            return path.stat().st_mtime

        stamp = path.stat().st_mtime
    """
    assert uses(source) == ["line 4: st_mtime of a file touched"]


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


def test_an_exempt_name_with_dots_leaves_out_only_that_scope():
    source = """
        import time

        class TestA:
            def test_x(self):
                time.sleep(1)

        class TestB:
            def test_x(self):
                time.sleep(1)

        def test_x():
            time.sleep(1)
    """
    assert uses(source, exempt={"TestA.test_x"}) == [
        "line 10: sleep(",
        "line 13: sleep(",
    ]
    assert uses(source, exempt={"test_x"}) == []
    assert uses(source, exempt={"TestB"}) == [
        "line 6: sleep(",
        "line 13: sleep(",
    ]
    source = textwrap.dedent(source)
    assert scopes_named(source, "test_x") == 3
    assert scopes_named(source, "TestA.test_x") == 1
    assert scopes_named(source, "TestC.test_x") == 0


def test_the_uses_come_in_line_order():
    source = "import time\n" + "time.sleep(0)\n" * 12
    found = uses(source)
    assert found == [f"line {n}: sleep(" for n in range(2, 14)]
