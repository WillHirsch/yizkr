"""Where a test module waits on, or reads, the real clock: a unit test runs on
fake clocks and fake timers, so that no result depends on how long anything
takes or on what the wall clock reads (#432).

`real_time_uses` reads a module's source and lists each use it finds of these
spellings: a call whose final name reads or waits on a clock (`time.sleep`,
`t.sleep` after `import time as t`, `sleep` after `from time import sleep`,
`time.clock_gettime`, `datetime.datetime.now`, `date.today`, `signal.alarm`,
`threading.Timer`, `T` after `from threading import Timer as T`,
`loop.call_later`, ...), a `time.localtime()`, `gmtime()`, `ctime()`,
`asctime()` or `strftime(format)` that reads the current time, a clock function
taken without a call (`clock = time.monotonic`, `now=datetime.now`), any `from
time import`, a timed wait (`.wait`, `.join`, `.wait_for`, `.acquire`, `.get`,
`.put`, `.result`, `.exception`, `.poll`, `threading.Barrier`, `.settimeout`,
`select.select`, a selector's `.select`, `asyncio.timeout`) whose timeout is
anything but a named hang guard, None or 0, any `asyncio.timeout_at` given a
deadline, a real timer started with a wait other than 0, a clock function
reached by its name as a string (`getattr(time, "sleep")`,
`time.__dict__["sleep"]`, `vars(time)["sleep"]`, `getattr(datetime, "now")`),
an `os.utime` that sets a file's times to the current time (given no times),
a ZIP archive's `.writestr` given its member's name rather than a `ZipInfo`
(a `ZipInfo(...)` call, or a name the module assigns one to), which stamps the
member with the current time, and a file's time read (`.st_mtime`,
`.st_mtime_ns`, `.st_ctime`, `.st_atime` and their `_ns` forms, `getmtime`,
`getctime`, `getatime`) in a function or module that also touches a file
(`.touch()`), which stamps it with the current time. Uses inside the classes
and functions named as exempt (the fakes, say), by qualified name
(`TestX.test_y`) or by bare name wherever they sit, are left out.

A `.join`'s, a `.poll`'s or a selector's `.select`'s lone positional argument
counts as a timeout when it gives a time: a number or a named hang guard,
arithmetic on one with no sequence or string beside it (`HANG_GUARD / 10`, but
not `[name] * 4`), a conditional giving one (`0.5 if slow else 1`), or a call
that makes a number (`float("0.5")`, `min(HANG_GUARD, 5)`).

What it cannot tell from a module's own code is left to review: a `.join`, a
`.poll` or a selector's `.select` given its timeout by another name, or by any
other expression (a str.join's iterable looks the same), a timeout given
positionally to `.wait_for`, `.acquire`, `.get`, `.put`, `.result` or
`.exception` (a dict.get's default, or a test helper's own argument, looks the
same), a timeout given to any other call (`subprocess.run`, `communicate`,
`urlopen`, ...), `asyncio.timeout` or `timeout_at` reached through another name
(`from asyncio import timeout`), a clock function's name given to `getattr` or
a subscript as anything but a string literal, or read through `__dict__.get`, a
file's time compared with the clock after any other write that stamps it
(`write_text`, `open`) or in another function than the touch, a clock reached
through another name than the time module's, a datetime's, a date's or one a
clock call is imported as (`T = threading.Timer`, or `dt.now` taken uncalled
after `from datetime import datetime as dt`), or through a default in `src/`, a
wait reached through another name (`s.select` after `import select as s`, `sl`
after `from select import select as sl`, `B` after `from threading import
Barrier as B`), and a child's script held in a string.

A `ZipInfo` given to `.writestr` by any other route (a function's argument, an
attribute) is found as a name: the finder errs towards finding it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

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
        "clock_gettime",
        "clock_gettime_ns",
        "now",
        "utcnow",
        "today",
        "Timer",
        "alarm",
        "setitimer",
        "call_later",
        "call_at",
    }
)
# the time module's functions that read the current time when they are
# not given one: the number of arguments that leaves it to them
CURRENT_TIME = {"localtime": 0, "gmtime": 0, "ctime": 0, "asctime": 0}
CURRENT_TIME["strftime"] = 1
# the clock functions a datetime or a date class has
DATETIME_CLOCKS = frozenset({"now", "utcnow", "today"})
# the calls that wait, up to a timeout: the position of the timeout
# among their arguments, when it can be told from them (a `.join`'s and a
# selector's `.select`'s at 0, only when it gives a time: _a_time), else
# None, and only its `timeout` keyword counts
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
    "result": None,
    "exception": None,
    "poll": 0,
}
# the waits whose lone positional argument counts only when it gives a
# time (_a_time): a str.join's iterable, or a dict's, is no timeout
LONE_TIMES = frozenset({"join", "poll"})
# asyncio's timeout context managers: the one given a delay, and the one
# given a deadline, which reads the event loop's clock
ASYNC_TIMEOUTS = {"timeout": "delay", "timeout_at": "deadline"}
# a file's times, as a stat result's attributes or os.path's functions
FILE_TIMES = frozenset(
    {
        "st_mtime",
        "st_mtime_ns",
        "st_ctime",
        "st_ctime_ns",
        "st_atime",
        "st_atime_ns",
        "getmtime",
        "getctime",
        "getatime",
    }
)


def _final_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _number(node: ast.expr) -> bool:
    """Whether `node` is a literal number (arithmetic on one: _a_time)."""
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


# the calls that make a number of what they are given (`float("0.5")`)
NUMBER_CALLS = frozenset({"float", "int", "abs", "round", "min", "max"})


# the expressions that never give a time: a sequence, a mapping, a
# comprehension or a string (`[name] * 4` repeats a list)
NOT_TIMES = (
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.JoinedStr,
)


def _a_time(node: ast.expr, hang_guards: set[str]) -> bool:
    """Whether `node`, a `.join`'s or a selector's lone argument, gives a
    time: a number or a named hang guard, arithmetic on one (with no
    sequence or string beside it) or a conditional giving one, or a call
    that makes a number. A str.join's iterable, a name or a string, a
    list or a comprehension, gives none."""
    if _number(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in hang_guards
    if isinstance(node, ast.UnaryOp):
        return _a_time(node.operand, hang_guards)
    if isinstance(node, ast.BinOp):
        sides = (node.left, node.right)
        return not any(
            isinstance(side, NOT_TIMES)
            or (
                isinstance(side, ast.Constant)
                and isinstance(side.value, str | bytes)
            )
            for side in sides
        ) and any(_a_time(side, hang_guards) for side in sides)
    if isinstance(node, ast.IfExp):
        return _a_time(node.body, hang_guards) or _a_time(
            node.orelse, hang_guards
        )
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in NUMBER_CALLS
    )


def _timeout(
    call: ast.Call, name: str, hang_guards: set[str]
) -> ast.expr | None:
    """The timeout a timed wait passes: its `timeout` keyword, or the
    argument at its timeout's position (a `.join`'s only when it gives a
    time, since a str.join's iterable is no timeout)."""
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    position = WAITS[name]
    # a selector's `.select(timeout)`, unlike `select.select`'s four
    # arguments, and like a `.join`, counts only an argument that gives a
    # time
    selector = name == "select" and not (
        isinstance(call.func, ast.Name)
        or (
            isinstance(call.func.value, ast.Name)
            and call.func.value.id == "select"
        )
    )
    if selector:
        position = 0
    if position is None or len(call.args) <= position:
        return None
    timeout = call.args[position]
    if (name in LONE_TIMES or selector) and not (
        len(call.args) == 1 and _a_time(timeout, hang_guards)
    ):
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


def _string(node: ast.expr) -> str | None:
    """The text of `node` when it is a string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _touching_scopes(tree: ast.Module) -> set[int]:
    """The ids of the nodes in each function, and in the module outside
    any, that also touches a file (`.touch()`): a file time read there
    reads a time the clock stamped."""
    touching: set[int] = set()

    def own(scope: ast.AST) -> list[ast.AST]:
        """The nodes of `scope` outside the functions nested in it."""
        nodes, pending = [], list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            nodes.append(node)
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                pending.extend(ast.iter_child_nodes(node))
        return nodes

    scopes = [tree] + [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    for scope in scopes:
        nodes = own(scope)
        if any(
            isinstance(node, ast.Call) and _final_name(node.func) == "touch"
            for node in nodes
        ):
            touching.update(id(node) for node in nodes)
    return touching


def _clock_aliases(tree: ast.Module) -> dict[str, str]:
    """The other names a clock call is imported as, each with the name it
    has (`from threading import Timer as T` gives T -> Timer)."""
    return {
        alias.asname: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in CLOCK_CALLS and alias.asname
    }


SCOPES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _scopes(tree: ast.AST, prefix: str = "") -> Iterator[tuple[str, ast.AST]]:
    """Each class and function in `tree` with its qualified name: the
    names of the classes and functions it sits in, then its own, joined
    by dots (`TestX.test_y`)."""
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, SCOPES):
            qualified = prefix + child.name
            yield qualified, child
            yield from _scopes(child, qualified + ".")
        else:
            yield from _scopes(child, prefix)


def _names(qualified: str, name: str) -> bool:
    """Whether `name` names the scope `qualified`: by its qualified name,
    or, without a dot, by its own name wherever it sits."""
    return name == qualified or (
        "." not in name and name == qualified.split(".")[-1]
    )


def scopes_named(source: str, name: str) -> int:
    """How many classes and functions in `source` `name` names, as an
    `exempt` name of `real_time_uses` names them."""
    return sum(
        1
        for qualified, _ in _scopes(ast.parse(source))
        if _names(qualified, name)
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


def _zip_infos(tree: ast.Module) -> set[str]:
    """The names the module assigns a `ZipInfo(...)` call to."""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _final_name(node.value.func) == "ZipInfo"
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _stamps_the_time(call: ast.Call, zip_infos: set[str]) -> bool:
    """Whether a `.writestr` call gives its member as a name, which
    zipfile stamps with the current time, rather than as a `ZipInfo`,
    which carries its own."""
    member = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "zinfo_or_arcname":
            member = keyword.value
    if member is None:
        return False
    if isinstance(member, ast.Call):
        return _final_name(member.func) != "ZipInfo"
    return not (isinstance(member, ast.Name) and member.id in zip_infos)


def real_time_uses(
    source: str,
    *,
    exempt: Iterable[str] = (),
    hang_guards: Iterable[str] = (),
    real_timers: Iterable[str] = (),
) -> list[str]:
    """Each use of the real clock in `source`, as 'line N: what', outside
    the classes and functions named in `exempt` (by qualified name, or by
    bare name wherever they sit). A timed wait may take,
    as its timeout, only a name in `hang_guards`, a bound on a wait that
    a synchronisation ends, never a time the test waits out, None, or a
    literal 0, a poll that waits no time. A call of a name in
    `real_timers` (a real timer's starter, called with its wait
    and its callback) must ask for a wait of 0."""
    tree = ast.parse(source)
    hang_guards, real_timers = set(hang_guards), set(real_timers)
    exempt = set(exempt)
    left_out = {
        id(inner)
        for qualified, scope in _scopes(tree)
        if any(_names(qualified, name) for name in exempt)
        for inner in ast.walk(scope)
    }
    time_modules = _time_modules(tree)
    aliases = _clock_aliases(tree)
    touching = _touching_scopes(tree)
    zip_infos = _zip_infos(tree)
    called = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    def of_time(func: ast.expr) -> bool:
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in time_modules
        )

    def a_clock_named(receiver: ast.expr, attr: str | None) -> bool:
        """Whether `attr`, a name given as a string, names a clock
        function of `receiver`: the time module's, or a datetime's or a
        date's."""
        if isinstance(receiver, ast.Name) and receiver.id in time_modules:
            return attr in CLOCK_CALLS
        return (
            _final_name(receiver) in {"datetime", "date"}
            and attr in DATETIME_CLOCKS
        )

    def namespace_of(node: ast.expr) -> ast.expr | None:
        """The module whose namespace `node` is (`time.__dict__`,
        `vars(time)`), else None."""
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            return node.value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "vars"
            and len(node.args) == 1
        ):
            return node.args[0]
        return None

    found = []
    for node in ast.walk(tree):
        if id(node) in left_out:
            continue
        if isinstance(node, ast.Subscript):
            module, key = namespace_of(node.value), _string(node.slice)
            if module is not None and a_clock_named(module, key):
                found.append(f"line {node.lineno}: {key} through __dict__")
        if (
            isinstance(node, ast.Attribute | ast.Name)
            and _final_name(node) in FILE_TIMES
            and id(node) in touching
            and not (isinstance(node, ast.Name) and id(node) not in called)
        ):
            found.append(
                f"line {node.lineno}: {_final_name(node)} of a file touched"
            )
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
        if isinstance(node.func, ast.Name):
            name = aliases.get(name, name)
        if name == "getattr" and len(node.args) >= 2:
            attr = _string(node.args[1])
            if a_clock_named(node.args[0], attr):
                found.append(f"line {node.lineno}: getattr {attr}")
            continue
        if (
            name in ASYNC_TIMEOUTS
            and isinstance(node.func, ast.Attribute)
            and _final_name(node.func.value) == "asyncio"
        ):
            given = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg in ("delay", "when"):
                    given = keyword.value
            if ASYNC_TIMEOUTS[name] == "deadline":
                timed = not (
                    isinstance(given, ast.Constant) and given.value is None
                )
            else:
                timed = not _untimed(given, hang_guards)
            if timed:
                found.append(
                    f"line {node.lineno}: {name}( with a "
                    + ASYNC_TIMEOUTS[name]
                )
            continue
        if (
            name == "utime"
            and len(node.args) < 2
            and not any(k.arg in ("times", "ns") for k in node.keywords)
        ) or (
            name == "utime"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value is None
        ):
            found.append(f"line {node.lineno}: utime( to the current time")
            continue
        if (
            name == "writestr"
            and isinstance(node.func, ast.Attribute)
            and _stamps_the_time(node, zip_infos)
        ):
            found.append(f"line {node.lineno}: writestr( given a name")
            continue
        reads_the_time = (
            name in CURRENT_TIME
            and of_time(node.func)
            and len(node.args) <= CURRENT_TIME[name]
        )
        if name in CLOCK_CALLS or reads_the_time:
            found.append(f"line {node.lineno}: {name}(")
        elif name in WAITS:
            if not _untimed(_timeout(node, name, hang_guards), hang_guards):
                found.append(f"line {node.lineno}: {name}( with a timeout")
        elif name in real_timers and not _zero(
            node.args[0] if node.args else None
        ):
            found.append(f"line {node.lineno}: {name}( with a wait")
    return sorted(found, key=lambda use: int(use.split()[1].rstrip(":")))
