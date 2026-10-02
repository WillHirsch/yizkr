"""Where a test module waits on, or reads, the real clock: a unit test runs
on fake clocks and fake timers, so that no result depends on how long
anything takes or on what the wall clock reads (#432).

`real_time_uses` reads a module's source and lists each use it finds of
these spellings: a call whose final name reads or waits on a clock
(`time.sleep`, `t.sleep` after `import time as t`, `sleep` after `from time
import sleep`, `datetime.datetime.now`, `date.today`, `signal.alarm`, ...),
a `time.localtime()`, `gmtime()`, `ctime()`, `asctime()` or
`strftime(format)` that reads the current time, a clock function taken
without a call (`clock = time.monotonic`, `now=datetime.now`), any `from
time import`, a timed wait (`.wait`, `.join`, `.wait_for`, `.acquire`,
`.get`, `.put`, `threading.Barrier`, `.settimeout`, `select.select`) whose
timeout is anything but a named hang guard, None or 0, and a real timer
started with a wait other than 0. Uses inside the classes and functions
named as exempt (the fakes, say) are left out.

What it cannot tell from a module's own code is left to review: a
`.join` given a timeout by a positional name (a str.join's iterable looks
the same), a timeout given positionally to `.wait_for`, `.acquire`, `.get`
or `.put` (a dict.get's default, or a test helper's own argument, looks
the same), a timeout given to any other call (`subprocess.run`,
`communicate`, `urlopen`, ...), a clock reached through another name than
the time module's, a datetime's or a date's, or through a default in
`src/`, and a child's script held in a string.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

# the final names of the calls that read or wait on a real clock
CLOCK_CALLS = frozenset(
    {
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
        "now",
        "utcnow",
        "today",
        "Timer",
        "alarm",
        "setitimer",
    }
)
# the time module's functions that read the current time when they are
# not given one: the number of arguments that leaves it to them
CURRENT_TIME = {"localtime": 0, "gmtime": 0, "ctime": 0, "asctime": 0}
CURRENT_TIME["strftime"] = 1
# the clock functions a datetime or a date class has
DATETIME_CLOCKS = frozenset({"now", "utcnow", "today"})
# the calls that wait, up to a timeout: the position of the timeout
# among their arguments, when it can be told from them (a `.join`'s, only
# when it is a number), else None, and only its `timeout` keyword counts
WAITS = {
    "wait": 0,
    "join": 0,
    "wait_for": None,
    "acquire": None,
    "get": None,
    "put": None,
    "Barrier": 2,
    "settimeout": 0,
    "select": 3,
}


def _final_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _number(node: ast.expr) -> bool:
    """Whether `node` is a number, or arithmetic on numbers."""
    if isinstance(node, ast.UnaryOp):
        return _number(node.operand)
    if isinstance(node, ast.BinOp):
        return _number(node.left) and _number(node.right)
    return isinstance(node, ast.Constant) and isinstance(
        node.value, int | float
    )


def _zero(node: ast.expr | None) -> bool:
    """Whether `node` is a literal 0 (or 0.0), and not False."""
    return (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and node.value == 0
    )


def _timeout(call: ast.Call, name: str) -> ast.expr | None:
    """The timeout a timed wait passes: its `timeout` keyword, or the
    argument at its timeout's position (a `.join`'s only when it is a
    number, since a str.join's iterable is no timeout)."""
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    position = WAITS[name]
    if position is None or len(call.args) <= position:
        return None
    timeout = call.args[position]
    if name == "join" and not (len(call.args) == 1 and _number(timeout)):
        return None
    return timeout


def _untimed(node: ast.expr | None, hang_guards: set[str]) -> bool:
    """Whether a wait given `node` as its timeout waits on its event
    alone: no timeout, None, a poll of 0 or a named hang guard."""
    return (
        node is None
        or (isinstance(node, ast.Constant) and node.value is None)
        or _zero(node)
        or (isinstance(node, ast.Name) and node.id in hang_guards)
    )


def _time_modules(tree: ast.Module) -> set[str]:
    """The names the time module is imported as."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "time"
    }


def real_time_uses(
    source: str,
    *,
    exempt: Iterable[str] = (),
    hang_guards: Iterable[str] = (),
    real_timers: Iterable[str] = (),
) -> list[str]:
    """Each use of the real clock in `source`, as 'line N: what', outside
    the classes and functions named in `exempt`. A timed wait may take,
    as its timeout, only a name in `hang_guards`, a bound on a wait that
    a synchronisation ends, never a time the test waits out, None, or a
    literal 0, a poll that waits no time. A call of a name in
    `real_timers` (a real timer's starter, called with its wait
    and its callback) must ask for a wait of 0."""
    tree = ast.parse(source)
    names, hang_guards = set(exempt), set(hang_guards)
    real_timers = set(real_timers)
    left_out: set[int] = set()
    scopes = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.name in names:
            left_out.update(id(inner) for inner in ast.walk(node))
    time_modules = _time_modules(tree)
    called = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    def of_time(func: ast.expr) -> bool:
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in time_modules
        )

    found = []
    for node in ast.walk(tree):
        if id(node) in left_out:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            names = ", ".join(alias.name for alias in node.names)
            found.append(f"line {node.lineno}: from time import {names}")
        if isinstance(node, ast.Attribute) and id(node) not in called:
            receiver = _final_name(node.value)
            if (of_time(node) and node.attr in CLOCK_CALLS) or (
                node.attr in DATETIME_CLOCKS
                and receiver in {"datetime", "date"}
            ):
                found.append(f"line {node.lineno}: {node.attr} uncalled")
        if not isinstance(node, ast.Call):
            continue
        name = _final_name(node.func)
        reads_the_time = (
            name in CURRENT_TIME
            and of_time(node.func)
            and len(node.args) <= CURRENT_TIME[name]
        )
        if name in CLOCK_CALLS or reads_the_time:
            found.append(f"line {node.lineno}: {name}(")
        elif name in WAITS:
            if not _untimed(_timeout(node, name), hang_guards):
                found.append(f"line {node.lineno}: {name}( with a timeout")
        elif name in real_timers and not _zero(
            node.args[0] if node.args else None
        ):
            found.append(f"line {node.lineno}: {name}( with a wait")
    return sorted(found, key=lambda use: int(use.split()[1].rstrip(":")))
