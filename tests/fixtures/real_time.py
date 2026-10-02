"""Where a test module waits on, or reads, the real clock: a unit test runs
on fake clocks and fake timers, so that no result depends on how long
anything takes or on what the wall clock reads (#432).

`real_time_uses` reads a module's source and lists each use it finds,
whatever the spelling: a call whose final name reads or waits on a clock
(`time.sleep`, `t.sleep` after `import time as t`, `sleep` after `from time
import sleep`, `datetime.datetime.now`, `date.today`, ...), any `from time
import`, a `.wait(...)` or `.join(...)` whose timeout is a number rather
than a named hang guard, and a real timer started with a wait other than
0. Uses inside the classes and functions named as exempt (the fakes, say)
are left out.
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
    }
)
# the calls that wait for an event or a thread, up to their timeout
WAITS = frozenset({"wait", "join"})


def _final_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _number(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(
        node.value, int | float
    )


def _timeout(call: ast.Call) -> ast.expr | None:
    """The timeout a `.wait(...)` or `.join(...)` call passes: its
    `timeout` keyword, or a lone positional number (a str.join's iterable
    is no timeout)."""
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    if len(call.args) == 1 and _number(call.args[0]):
        return call.args[0]
    return None


def real_time_uses(
    source: str,
    *,
    exempt: Iterable[str] = (),
    hang_guards: Iterable[str] = (),
    real_timers: Iterable[str] = (),
) -> list[str]:
    """Each use of the real clock in `source`, as 'line N: what', outside
    the classes and functions named in `exempt`. A `.wait` or `.join`
    may take, as its timeout, only a name in `hang_guards`, a bound on a
    wait that a synchronisation ends, never a time the test waits out. A
    call of a
    name in `real_timers` (a real timer's starter, called with its wait
    and its callback) must ask for a wait of 0."""
    tree = ast.parse(source)
    names, hang_guards = set(exempt), set(hang_guards)
    real_timers = set(real_timers)
    left_out: set[int] = set()
    scopes = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.name in names:
            left_out.update(id(inner) for inner in ast.walk(node))
    found = []
    for node in ast.walk(tree):
        if id(node) in left_out:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            names = ", ".join(alias.name for alias in node.names)
            found.append(f"line {node.lineno}: from time import {names}")
        if not isinstance(node, ast.Call):
            continue
        name = _final_name(node.func)
        if name in CLOCK_CALLS:
            found.append(f"line {node.lineno}: {name}(")
        elif name in WAITS:
            timeout = _timeout(node)
            if timeout is not None and not (
                isinstance(timeout, ast.Name) and timeout.id in hang_guards
            ):
                found.append(f"line {node.lineno}: {name}( with a timeout")
        elif name in real_timers:
            wait = node.args[0] if node.args else None
            if not (
                isinstance(wait, ast.Constant)
                and wait.value == 0
                and not isinstance(wait.value, bool)
            ):
                found.append(f"line {node.lineno}: {name}( with a wait")
    return sorted(found, key=lambda use: int(use.split()[1].rstrip(":")))
