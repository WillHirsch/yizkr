#!/usr/bin/env python3
"""A throwaway, network-less query engine for query reviews.

  make wdqs-sandbox-up        (or: uv run python scripts/wdqs_sandbox.py up)
  uv run python scripts/synthetic_graph.py --persons 250 --out DIR/g.ttl
  uv run python scripts/wdqs_sandbox.py load DIR/g.ttl
  uv run python scripts/wdqs_sandbox.py query \\
      src/yizkr/queries/disagreement-report.rq --repeat 3 --max-rows 20
  make wdqs-sandbox-down      (or: uv run python scripts/wdqs_sandbox.py down)

Each checkout (git worktree) has a sandbox of its own: the state file
(~/.cache/yizkr/wdqs-sandbox-KEY.json) and the container's name
(yizkr-wdqs-sandbox-KEY-...) carry a key made from the top directory of
the checkout the command runs in, so sessions in different worktrees never
share, refuse or remove each other's sandbox. --state (or
$YIZKR_WDQS_SANDBOX_STATE) names another state file. One command at a
time uses a sandbox; a second one says it is waiting.

up starts a wikibase/wdqs:2 container (the Blazegraph build the deployment
runs) with no network, no ports and no volumes, capped at 768 MB without
swap and at one CPU, and waits until the engine answers. Nothing here
touches the deployed stack: requests reach the engine through docker exec
and the container's own curl, and every command acts only on a container
named yizkr-wdqs-sandbox-... that carries the org.yizkr.sandbox=wdqs label,
refusing any other container a state file names. The image's journal
settings ask for 1000 one-megabyte write-cache buffers, more direct memory
than the cap leaves, so up copies the image's RWStore.properties into the
container with 8 before it starts. up refuses while its sandbox runs, and
a failed or interrupted up removes the container it was starting. down
removes the container and the state file, and is safe to repeat.

load copies Turtle (.ttl) or N-Triples (.nt) files into the container and
adds them to the store; --replace empties the store first. query renders
a template as yizkr publish-queries does: it resolves the {P:key}/{Q:key}
placeholders through a vocabulary map (synthetic_graph.py writes one
beside each graph, and query uses the one beside the graph loaded last
unless --vocab names another) and prepends PREFIX lines for the map's
concept URI; --raw sends a finished query as it is. It prints the rows as
tab-separated values on stdout (IRIs shortened to wd:, wds:, wdref: and
wdv:, language tags as value@lang) and the engine's wall time on stderr;
--max-rows 0 prints the header only.

The engine stops each run of a query at --timeout (100 s by default), and
load stops each request it sends (the empty of --replace, then one per
file) at its own (90 s), so that each request ends within two minutes. A
command that sends several (load --replace, a load of several files,
query --repeat N) can take longer, and so can a stopped load, which waits
for the engine as below. Ctrl-C or SIGTERM during a query cancels that
query in the engine and kills its curl in the container. During a load
(or the empty of --replace) it kills only the curl and gives the engine
up to 20 s to finish or abort the write itself, and says so while it
waits: cancelling a write leaves the engine's writer unable to take
another one until the sandbox restarts. A second Ctrl-C or SIGTERM
meanwhile is ignored. The engine commits a load whole or not at all.

Timings are indicative. One CPU makes them noisy, since garbage
collection and CPU throttling land inside a query's wall time: compare
variants in one session with --repeat 3 and their medians, or start the
sandbox with --cpus 2 for steadier numbers. The heap (320 MB) and direct
memory (192 MB) are well below the deployed service's (a 1 GB heap by
default), so a query that needs memory may be slower here, or fail where
the deployment runs it: the sandbox compares queries, it does not predict
the deployment's times.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import io
import json
import os
import re
import secrets
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE = "wikibase/wdqs:2"
ENGINE = "http://localhost:9999/bigdata"
ENDPOINT = f"{ENGINE}/namespace/wdq/sparql"
STATUS = f"{ENGINE}/status"
CONFIG = "/wdqs/RWStore.properties"
WRITE_CACHE_BUFFERS = (
    "com.bigdata.journal.AbstractJournal.writeCacheBufferCount"
)
STATE_ENV = "YIZKR_WDQS_SANDBOX_STATE"
LABEL = "org.yizkr.sandbox"
LABEL_VALUE = "wdqs"
PREFIX = "yizkr-wdqs-sandbox-"
SANDBOX_NAME = re.compile(rf"{re.escape(PREFIX)}[a-z0-9][a-z0-9-]*")
# What sandbox() reads of a container: its name, status and sandbox label.
INSPECT = (
    "{{.Name}}|{{.State.Status}}|{{index .Config.Labels " + f'"{LABEL}"' + "}}"
)
# A command's files in the container (uploads, its curl's pid file).
WORK_DIR = "/tmp"
WORK_PREFIX = "yizkr-sandbox-"
FORMATS = {".ttl": "text/turtle", ".nt": "text/plain"}
# curl reports the request's wall time on stderr after this marker.
TIMING = "time_total="
# The engine failed to start; waiting longer will not help.
FATAL_LOG = ("Failed startup of context", "OutOfMemoryError")
# A running task on the engine's status page: its id and its text (a
# query as sent, or a write task's name).
TASK = re.compile(
    r'name="queryId" value="([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12})".*?class="query-string">(.*?)</p',
    re.S,
)
# The first line of every query this tool sends, so that an interrupted
# command cancels its own query and nothing else.
MARKER = "#yizkr-sandbox:"
# How long a create that was interrupted may still land on the daemon.
CREATE_GRACE = 5.0
# How long a stopped load waits for the engine to finish or abort it.
SETTLE = 20.0
# Kills the curl whose pid is in the file "$0", only while that pid's
# command line carries the token "$1" (the pid may since have gone to
# another process). dash has kill built in; the image has no kill binary.
KILL_CURL = (
    'pid=$(cat "$0" 2>/dev/null) || exit 0; '
    'case "$(tr "\\0" " " < /proc/$pid/cmdline 2>/dev/null)" in '
    '*"$1"*) kill "$pid" ;; esac'
)
PLACEHOLDER = re.compile(r"\{([PQ]):([a-z][a-z0-9_]*)\}")
# The clock the waits below read, and the sleep they poll with; a test
# replaces both with a fake clock that only its sleeps move.
monotonic = time.monotonic
sleep = time.sleep


class SandboxError(Exception):
    """A failure to report in one line."""


class Stopped(KeyboardInterrupt):
    """Ctrl-C or SIGTERM; after a clean-up, the message says what it did."""

    def __init__(self, message: str = "", signum: int = signal.SIGINT):
        super().__init__(message)
        self.signum = signum


def on_sigterm(signum, frame):
    raise Stopped("terminated", signum)


def signum_of(exc: BaseException) -> int:
    return getattr(exc, "signum", signal.SIGINT)


@contextlib.contextmanager
def shielded():
    """Hold off Ctrl-C and SIGTERM while a clean-up runs."""
    previous = {
        signum: signal.signal(signum, signal.SIG_IGN)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def checkout() -> Path:
    """The top directory of the checkout the command runs in, or of this
    script's when it runs outside one."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return REPO
    top = done.stdout.strip()
    return Path(top) if done.returncode == 0 and top else REPO


def checkout_key(top: Path) -> str:
    return hashlib.sha1(str(top).encode()).hexdigest()[:8]


def default_state() -> Path:
    if os.environ.get(STATE_ENV):
        return Path(os.environ[STATE_ENV]).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = checkout_key(checkout())
    return cache / "yizkr" / f"wdqs-sandbox-{key}.json"


# -- docker -------------------------------------------------------------------


def docker(*args: str, stdin: bytes | None = None, check=True):
    """Run the docker CLI in a session of its own. A terminal's Ctrl-C and
    a signal to the process group then reach only this script, which kills
    the CLI when it stops a command. Sharing the group, the CLI would take
    the signal even while a clean-up holds signals off (docker's CLI
    handles SIGINT and SIGTERM whatever it inherits), and a `docker exec`
    it stops exits 0 with no output while its command runs on in the
    container."""
    try:
        done = subprocess.run(
            ["docker", *args],
            input=stdin,
            capture_output=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise SandboxError("docker is not installed or not on PATH") from None
    if check and done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip()
        raise SandboxError(f"docker {args[0]} failed: {detail}")
    return done


@dataclass(frozen=True)
class Sandbox:
    """A container this tool made. Everything that removes a container or
    runs a command in one takes a Sandbox, and only sandbox() makes one."""

    name: str
    status: str


def sandbox(name) -> Sandbox | None:
    """The sandbox container `name`, or None when no container has that
    name. Any other container is refused: a name without the sandbox
    prefix (the deployed stack's, another tool's, a container ID or ID
    prefix) or a container without the sandbox label."""
    if not isinstance(name, str) or not SANDBOX_NAME.fullmatch(name):
        raise SandboxError(
            f"{name!r} is not a sandbox this tool made (their names start "
            f"{PREFIX}); nothing was done"
        )
    done = docker(
        "inspect", "--type", "container", "-f", INSPECT, name, check=False
    )
    if done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip()
        if "No such container" in detail:
            return None
        raise SandboxError(f"docker inspect failed: {detail}")
    resolved, status, label = done.stdout.decode().strip().split("|", 2)
    if resolved != f"/{name}" or label != LABEL_VALUE:
        raise SandboxError(
            f"container {name} does not carry the {LABEL}={LABEL_VALUE} "
            f"label, so this tool did not make it; nothing was done"
        )
    return Sandbox(name, status)


def remove(box: Sandbox) -> None:
    docker("rm", "-fv", box.name)


# -- state --------------------------------------------------------------------


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(
            f"unreadable state file {path} ({exc}); check it and remove it "
            f"by hand (docker ps -a --filter label={LABEL}={LABEL_VALUE} "
            f"lists the sandboxes)"
        ) from None
    if not isinstance(state, dict):
        raise SandboxError(f"{path} is not a sandbox state file")
    return state


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def record_loaded(path: Path, loaded: list[str]) -> None:
    state = read_state(path)
    state["loaded"] = loaded
    write_state(path, state)


@contextlib.contextmanager
def locked(path: Path):
    """One command at a time per state file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"waiting for {lock_path}: another command is using this "
                f"sandbox",
                file=sys.stderr,
                flush=True,
            )
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def running_sandbox(state_path: Path) -> Sandbox:
    name = read_state(state_path).get("container")
    if not name:
        raise SandboxError(
            f"no sandbox recorded in {state_path}; start one with `up`"
        )
    box = sandbox(name)
    if box is None or box.status != "running":
        status = box.status if box else "gone"
        raise SandboxError(
            f"sandbox {name} is {status}; run `down`, then `up`"
        )
    return box


# -- up / down ----------------------------------------------------------------


def patched_config(box: Sandbox) -> bytes:
    """The image's own RWStore.properties with a write cache that fits
    the memory cap, as a tar stream for `docker cp -`."""
    done = docker("cp", f"{box.name}:{CONFIG}", "-")
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        member = archive.getmembers()[0]
        text = archive.extractfile(member).read().decode()
    pattern = re.compile(rf"^{re.escape(WRITE_CACHE_BUFFERS)}=.*$", re.M)
    if not pattern.search(text):
        raise SandboxError(f"{CONFIG} in {IMAGE} has no {WRITE_CACHE_BUFFERS}")
    return tar_of(
        Path(CONFIG).name,
        pattern.sub(f"{WRITE_CACHE_BUFFERS}=8", text).encode(),
    )


def tar_of(name: str, data: bytes) -> bytes:
    """A one-file tar stream; the file is readable by the engine's user,
    whatever the source's mode."""
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def wait_ready(box: Sandbox, timeout: float) -> float:
    start = monotonic()
    while monotonic() - start < timeout:
        probe = docker(
            "exec", box.name, "curl", "-sS", "-m", "5",
            f"{ENDPOINT}?query=ASK%7B%7D", check=False,
        )  # fmt: skip
        if b"true" in probe.stdout:
            return monotonic() - start
        now = sandbox(box.name)
        if now is None or now.status != "running":
            status = now.status if now else "gone"
            raise SandboxError(f"the container stopped ({status})")
        logs = docker("logs", "--tail", "200", box.name, check=False)
        output = (logs.stdout + logs.stderr).decode(errors="replace")
        for marker in FATAL_LOG:
            if marker in output:
                line = next(x for x in output.splitlines() if marker in x)
                raise SandboxError(f"the engine failed to start: {line}")
        sleep(1)
    raise SandboxError(f"the engine was not ready after {timeout:.0f}s")


def discard(state_path: Path, name: str, grace: float) -> None:
    """Remove the container `up` was starting, waiting up to `grace`
    seconds for an interrupted create to land on the daemon, and then the
    state file."""
    deadline = monotonic() + grace
    while True:
        box = sandbox(name)
        if box is not None:
            remove(box)
            break
        if monotonic() >= deadline:
            break
        sleep(0.25)
    with contextlib.suppress(FileNotFoundError):
        state_path.unlink()


def cmd_up(args) -> int:
    with locked(args.state):
        old = read_state(args.state).get("container")
        if old:
            box = sandbox(old)
            if box is not None and box.status == "running":
                raise SandboxError(
                    f"sandbox {old} is already running (state {args.state});"
                    " load and query it, or pass another --state for a "
                    "second sandbox"
                )
            if box is not None:
                remove(box)
                print(f"removed the stopped sandbox {old}")
        top = checkout()
        name = f"{PREFIX}{checkout_key(top)}-{secrets.token_hex(3)}"
        # The name is recorded before the container exists, so whatever
        # interrupts up, the clean-up (or a later down) knows what to remove.
        write_state(
            args.state,
            {
                "container": name,
                "checkout": str(top),
                "image": args.image,
                "memory": args.memory,
                "cpus": args.cpus,
                "heap": args.heap,
                "direct": args.direct,
                "loaded": [],
            },
        )
        created = False
        try:
            docker(
                "create",
                "--name", name,
                "--label", f"{LABEL}={LABEL_VALUE}",
                "--label", f"{LABEL}.checkout={top}",
                "--hostname", "localhost",
                "--network", "none",
                "--memory", args.memory,
                "--memory-swap", args.memory,
                "--cpus", args.cpus,
                "--ulimit", "nofile=32768:32768",
                "-e", f"HEAP_SIZE={args.heap}",
                "-e", f"EXTRA_JVM_OPTS=-XX:MaxDirectMemorySize={args.direct}",
                args.image,
                "/runBlazegraph.sh",
            )  # fmt: skip
            created = True
            box = sandbox(name)
            if box is None:
                raise SandboxError(f"docker create made no container {name}")
            config = patched_config(box)
            docker(
                "cp", "-", f"{box.name}:{Path(CONFIG).parent}", stdin=config
            )
            docker("start", box.name)
            waited = wait_ready(box, args.timeout)
        except BaseException as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            with shielded():
                if created and not interrupted:
                    logs = docker("logs", "--tail", "20", name, check=False)
                    sys.stderr.write(
                        (logs.stdout + logs.stderr).decode(errors="replace")
                    )
                grace = CREATE_GRACE if interrupted and not created else 0
                discard(args.state, name, grace)
            if interrupted:
                raise Stopped(
                    "interrupted; the sandbox was removed", signum_of(exc)
                ) from None
            if isinstance(exc, SandboxError):
                raise SandboxError(f"{exc}; the sandbox was removed") from None
            raise
    print(
        f"sandbox {name} ready after {waited:.0f}s "
        f"(memory {args.memory}, {args.cpus} CPU, heap {args.heap}); "
        f"state {args.state}"
    )
    return 0


def cmd_down(args) -> int:
    with locked(args.state):
        name = read_state(args.state).get("container")
        if not name:
            print("no sandbox recorded")
            return 0
        box = sandbox(name)
        if box is not None:
            remove(box)
        with contextlib.suppress(FileNotFoundError):
            args.state.unlink()
    if box is not None:
        print(f"removed sandbox {name}")
    else:
        print(f"sandbox {name} was already gone; cleared its state")
    return 0


# -- work in the engine -------------------------------------------------------


@dataclass
class Work:
    """What one load or query puts in the sandbox: files it uploads and
    the curl it runs, named by one token so that stop() can find them."""

    box: Sandbox
    token: str = field(default_factory=lambda: secrets.token_hex(4))
    files: list[str] = field(default_factory=list)

    @property
    def pidfile(self) -> str:
        return f"{WORK_DIR}/{WORK_PREFIX}{self.token}.pid"

    def upload(self, data: bytes, suffix: str) -> str:
        """Copy bytes into the container; returns the file's path there.
        The path is recorded first, so that a copy cut short is removed
        too."""
        name = f"{WORK_PREFIX}{self.token}-{len(self.files)}{suffix}"
        path = f"{WORK_DIR}/{name}"
        self.files.append(path)
        docker(
            "cp", "-", f"{self.box.name}:{WORK_DIR}", stdin=tar_of(name, data)
        )
        return path

    def curl(self, *args: str):
        """curl in the container, with its pid in the pid file and the token
        on its command line, so that stop() kills this curl and no other
        process that later takes its pid."""
        return docker(
            "exec", self.box.name,
            "sh", "-c", 'echo $$ > "$0"; exec "$@"', self.pidfile,
            "curl", "-sS", "-H", f"X-Yizkr-Sandbox: {self.token}", *args,
            check=False,
        )  # fmt: skip

    @property
    def marker(self) -> str:
        return f"{MARKER}{self.token}"

    def kill(self) -> None:
        """Kill this command's curl, if it still runs."""
        docker(
            "exec", self.box.name,
            "sh", "-c", KILL_CURL, self.pidfile, self.token,
            check=False,
        )  # fmt: skip

    def stop_query(self) -> list[str]:
        """Cancel this command's query in the engine (the task whose text
        carries its marker, never another), kill its curl and remove its
        files; returns the ids cancelled."""
        mine = [
            task_id
            for task_id, text in engine_tasks(self.box) or []
            if self.marker in text
        ]
        for task_id in mine:
            docker(
                "exec", self.box.name,
                "curl", "-sS", "-m", "10", "-o", "/dev/null", "-X", "POST",
                f"{STATUS}?cancelQuery&queryId={task_id}",
                check=False,
            )  # fmt: skip
        self.kill()
        self.clean()
        return mine

    def stop_write(self) -> bool:
        """Stop this command's load or empty. Its curl is killed and the
        write is never cancelled: a cancelled write leaves the engine's
        writer unable to take another until the sandbox restarts, while a
        write that loses its client is finished or aborted by the engine
        itself. Waits up to SETTLE seconds for the engine to run nothing,
        saying so when it has to wait; returns whether it did."""
        self.kill()
        self.clean()
        deadline = monotonic() + SETTLE
        waiting = False
        while engine_tasks(self.box) != []:
            if monotonic() >= deadline:
                return False
            if not waiting:
                waiting = True
                print(
                    f"waiting up to {SETTLE:.0f} s for the engine to finish "
                    f"or abort the write",
                    file=sys.stderr,
                    flush=True,
                )
            sleep(0.5)
        return True

    def clean(self) -> None:
        docker(
            "exec", "-u", "0", self.box.name,
            "rm", "-f", self.pidfile, *self.files,
            check=False,
        )  # fmt: skip
        self.files.clear()


def engine_tasks(box: Sandbox) -> list[tuple[str, str]] | None:
    """(id, text) of each task the engine is running, from its status
    page; None when the page cannot be read or did not arrive whole (an
    empty page is not an idle engine)."""
    done = docker(
        "exec", box.name,
        "curl", "-sS", "-m", "10", f"{STATUS}?showQueries",
        check=False,
    )  # fmt: skip
    if done.returncode != 0 or b"</html" not in done.stdout:
        return None
    page = done.stdout.decode(errors="replace")
    return [
        (task_id, html.unescape(text)) for task_id, text in TASK.findall(page)
    ]


def engine_error(done) -> str:
    """The telling line of an engine error: the root cause of a Java
    stack trace, or curl's own message."""
    text = (done.stdout + done.stderr).decode(errors="replace")
    if "TimeoutException" in text:
        return "the engine stopped it at the timeout (--timeout)"
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.strip().startswith(("at ", "...", TIMING))
        and "The requested URL returned error" not in line
    ]
    causes = [line for line in lines if line.startswith("Caused by:")]
    thrown = [
        line for line in lines if re.search(r"(Exception|Error)\b", line)
    ]
    if causes:
        return causes[-1][:1000]
    if thrown:
        return thrown[0][:1000]
    return " | ".join(lines[-3:])[:1000]


def failure(done, what: str) -> SandboxError:
    if done.returncode == 28:
        return SandboxError(f"{what} ran past --timeout")
    return SandboxError(f"{what} failed: {engine_error(done)}")


def engine_query(work: Work, source: str, timeout: float):
    """(rows, variables, seconds the engine took); `source` is curl's
    query@FILE or query=TEXT."""
    done = work.curl(
        "--fail-with-body", "-X", "POST",
        "-m", str(int(timeout) + 10),
        "-H", "Accept: application/sparql-results+json",
        "-H", f"X-BIGDATA-MAX-QUERY-MILLIS: {int(timeout * 1000)}",
        "--data-urlencode", source,
        "-w", f"%{{stderr}}{TIMING}%{{time_total}}\n",
        ENDPOINT,
    )  # fmt: skip
    if done.returncode != 0:
        raise failure(done, "query")
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise SandboxError(f"query failed: {engine_error(done)}") from None
    timing = re.search(rf"^{TIMING}([\d.]+)$", done.stderr.decode(), re.M)
    seconds = float(timing.group(1)) if timing else float("nan")
    return result["results"]["bindings"], result["head"]["vars"], seconds


def triple_count(box: Sandbox, timeout: float = 60) -> int:
    work = Work(box)
    try:
        rows, _, _ = engine_query(
            work, "query=SELECT (COUNT(*) AS ?n) { ?s ?p ?o }", timeout
        )
    finally:
        work.clean()
    return int(rows[0]["n"]["value"])


# -- load ---------------------------------------------------------------------


def cmd_load(args) -> int:
    for path in args.files:
        if path.suffix not in FORMATS:
            raise SandboxError(
                f"{path}: unknown format (use {', '.join(FORMATS)})"
            )
        if not path.is_file():
            raise SandboxError(f"{path}: no such file")
    with locked(args.state):
        box = running_sandbox(args.state)
        loaded = [] if args.replace else read_state(args.state).get("loaded")
        loaded = list(loaded or [])
        work = Work(box)
        current = None
        try:
            if args.replace:
                done = work.curl(
                    "--fail-with-body", "-X", "POST",
                    "-m", str(int(args.timeout)),
                    "--data-urlencode", "update=DROP ALL", ENDPOINT,
                )  # fmt: skip
                if done.returncode != 0:
                    raise failure(done, "emptying the store")
                # the store is empty now, whatever happens next
                record_loaded(args.state, [])
            for path in args.files:
                current = path
                remote = work.upload(path.read_bytes(), path.suffix)
                done = work.curl(
                    "--fail-with-body", "-X", "POST",
                    "-m", str(int(args.timeout)),
                    "-H", f"Content-Type: {FORMATS[path.suffix]}",
                    "--data-binary", f"@{remote}", ENDPOINT,
                )  # fmt: skip
                if done.returncode != 0:
                    raise failure(done, "the load")
                work.clean()
                body = done.stdout.decode(errors="replace")
                added = re.search(r'modified="(\d+)"', body)
                millis = re.search(r'milliseconds="(\d+)"', body)
                print(
                    f"loaded {path}: {added.group(1) if added else '?'} "
                    f"triples in "
                    f"{int(millis.group(1)) / 1000 if millis else 0:.1f}s"
                )
                resolved = str(path.resolve())
                loaded = [p for p in loaded if p != resolved] + [resolved]
                record_loaded(args.state, loaded)
            current = None
        except BaseException as exc:
            with shielded():
                settled = work.stop_write()
                if args.replace:
                    # the store may have been emptied even if DROP ALL
                    # was cut short: record only what this command loaded
                    record_loaded(args.state, loaded)
                try:
                    holds = f"{triple_count(box, 10)} triples"
                except SandboxError:
                    holds = "an unknown number of triples"
            engine = (
                "the engine finished or aborted the write it had under way"
                if settled
                else f"the engine was still writing after {SETTLE:.0f}s"
            )
            detail = (
                f"{' while loading ' + str(current) if current else ''}; "
                f"stopped the load's curl and {engine}. A load lands whole "
                f"or not at all: the store holds {holds}"
            )
            if isinstance(exc, KeyboardInterrupt):
                raise Stopped(f"interrupted{detail}", signum_of(exc)) from None
            if isinstance(exc, SandboxError):
                raise SandboxError(f"{exc}{detail}") from None
            raise
        print(f"the store holds {triple_count(box)} triples")
    return 0


# -- query --------------------------------------------------------------------


def adapter_render(template, properties, items, concept_uri) -> str:
    """The query library's rendering (yizkr.queries.publish), for when
    the package cannot be imported: the PREFIX block for the concept URI,
    then the template with its placeholders resolved."""

    def substitute(match: re.Match) -> str:
        kind, key = match.groups()
        table = properties if kind == "P" else items
        if key not in table:
            raise KeyError(
                f"template references unknown manifest "
                f"{'property' if kind == 'P' else 'item'} key {key!r}"
            )
        return table[key]

    prefixes = "\n".join(
        [
            f"PREFIX wd: <{concept_uri}/entity/>",
            f"PREFIX wdt: <{concept_uri}/prop/direct/>",
            f"PREFIX p: <{concept_uri}/prop/>",
            f"PREFIX ps: <{concept_uri}/prop/statement/>",
            f"PREFIX pq: <{concept_uri}/prop/qualifier/>",
            f"PREFIX pr: <{concept_uri}/prop/reference/>",
            "PREFIX wikibase: <http://wikiba.se/ontology#>",
            "PREFIX prov: <http://www.w3.org/ns/prov#>",
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
            "PREFIX bd: <http://www.bigdata.com/rdf#>",
            "",
        ]
    )
    return prefixes + PLACEHOLDER.sub(substitute, template)


def renderer():
    """The repository's own renderer when it imports, else the adapter."""
    sys.path.insert(0, str(REPO / "src"))
    try:
        from yizkr.queries.publish import render_for_instance
    except ImportError as exc:
        print(
            f"yizkr is not importable ({exc}); rendering with the "
            f"script's adapter",
            file=sys.stderr,
        )
        return adapter_render
    finally:
        sys.path.pop(0)
    return render_for_instance


def vocabulary(args) -> tuple[dict, Path]:
    if args.vocab:
        path = args.vocab
    else:
        loaded = read_state(args.state).get("loaded") or []
        if not loaded:
            raise SandboxError(
                "no graph loaded: pass --vocab MAP (or --raw for a query "
                "without placeholders)"
            )
        path = Path(loaded[-1]).with_suffix(".vocab.json")
        if not path.is_file():
            raise SandboxError(
                f"no vocabulary map {path} beside the graph loaded last; "
                f"pass --vocab MAP"
            )
    try:
        vocab = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxError(
            f"unreadable vocabulary map {path}: {exc}"
        ) from None
    if not isinstance(vocab, dict) or not {
        "concept_uri",
        "properties",
        "items",
    } <= set(vocab):
        raise SandboxError(
            f"{path} is not a vocabulary map (it needs concept_uri, "
            f"properties and items)"
        )
    return vocab, path


def shorten(value: str, prefixes: list[tuple[str, str]]) -> str:
    for prefix, iri in prefixes:
        if value.startswith(iri):
            return f"{prefix}:{value[len(iri) :]}"
    return value


def cell(binding: dict | None, prefixes) -> str:
    if binding is None:
        return ""
    value = binding["value"]
    if binding["type"] == "uri":
        value = shorten(value, prefixes)
    elif binding.get("xml:lang"):
        value = f"{value}@{binding['xml:lang']}"
    return value.replace("\t", " ").replace("\n", " ")


def cmd_query(args) -> int:
    try:
        template = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SandboxError(f"cannot read {args.file}: {exc}") from None
    if args.raw and PLACEHOLDER.search(template):
        raise SandboxError(
            f"{args.file} has {{P:...}}/{{Q:...}} placeholders; drop "
            f"--raw to render them"
        )
    with locked(args.state):
        box = running_sandbox(args.state)
        prefixes: list[tuple[str, str]] = []
        text = template
        if not args.raw:
            vocab, path = vocabulary(args)
            concept = vocab["concept_uri"]
            try:
                text = renderer()(
                    template, vocab["properties"], vocab["items"], concept
                )
            except KeyError as exc:
                raise SandboxError(f"{args.file}: {exc.args[0]}") from None
            print(f"vocabulary {path}", file=sys.stderr)
            prefixes = [
                ("wds", f"{concept}/entity/statement/"),
                ("wd", f"{concept}/entity/"),
                ("wdref", f"{concept}/reference/"),
                ("wdv", f"{concept}/value/"),
            ]
        if args.show:
            print(text, file=sys.stderr)
        work = Work(box)
        times = []
        try:
            # the marker line lets stop_query() find this query, and only
            # this one, among the engine's tasks
            sent = f"{work.marker}\n{text}".encode()
            source = f"query@{work.upload(sent, '.rq')}"
            for _ in range(args.repeat):
                rows, variables, seconds = engine_query(
                    work, source, args.timeout
                )
                times.append(seconds)
        except BaseException as exc:
            with shielded():
                cancelled = work.stop_query()
            if isinstance(exc, KeyboardInterrupt):
                raise Stopped(
                    f"interrupted; cancelled the query in the engine "
                    f"({len(cancelled)} task) and stopped its curl",
                    signum_of(exc),
                ) from None
            if (
                isinstance(exc, SandboxError)
                and not args.raw
                and "Multiple prefix declarations" in str(exc)
            ):
                raise SandboxError(
                    f"{exc} (the file declares its own prefixes: pass --raw)"
                ) from None
            raise
        work.clean()
    print("\t".join(variables))
    shown = rows if args.max_rows is None else rows[: args.max_rows]
    for row in shown:
        print("\t".join(cell(row.get(v), prefixes) for v in variables))
    sys.stdout.flush()
    if len(shown) < len(rows):
        print(f"... {len(rows) - len(shown)} more rows", file=sys.stderr)
    summary = f"{len(rows)} rows in {times[-1]:.2f}s"
    if len(times) > 1:
        summary = (
            f"{len(rows)} rows; runs "
            + ", ".join(f"{t:.2f}s" for t in times)
            + f"; min {min(times):.2f}s, median "
            f"{statistics.median(times):.2f}s"
        )
    print(f"{args.file}: {summary}", file=sys.stderr)
    return 0


# -- command line -------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    top.add_argument(
        "--state",
        type=Path,
        help=(
            "state file naming the sandbox (default: "
            "~/.cache/yizkr/wdqs-sandbox-KEY.json, one per checkout, or "
            "$YIZKR_WDQS_SANDBOX_STATE); another one runs a second sandbox"
        ),
    )
    commands = top.add_subparsers(dest="command", required=True)

    up = commands.add_parser(
        "up",
        help="start the sandbox and wait until the engine answers",
        description=(
            "Start a network-less wikibase/wdqs:2 container with a unique "
            "name, recorded in the state file, and wait until the engine "
            "answers. Refuses while the recorded container is running."
        ),
    )
    up.add_argument(
        "--memory", default="768m", metavar="SIZE", help="memory cap (768m)"
    )
    up.add_argument("--cpus", default="1", metavar="N", help="CPU cap (1)")
    up.add_argument(
        "--heap", default="320m", metavar="SIZE", help="Java heap (320m)"
    )
    up.add_argument(
        "--direct",
        default="192m",
        metavar="SIZE",
        help="Java direct memory cap (192m)",
    )
    up.add_argument("--image", default=IMAGE, help=f"image ({IMAGE})")
    up.add_argument(
        "--timeout",
        type=float,
        default=110,
        metavar="SECONDS",
        help="how long to wait for the engine (110)",
    )
    up.set_defaults(handler=cmd_up)

    load = commands.add_parser(
        "load",
        help="load Turtle or N-Triples files into the sandbox",
        description=(
            "Copy .ttl or .nt files into the sandbox and add them to its "
            "store, and print how many triples each added and the store's "
            "total."
        ),
    )
    load.add_argument("files", nargs="+", type=Path, metavar="FILE")
    load.add_argument(
        "--replace",
        action="store_true",
        help="empty the store before loading",
    )
    load.add_argument(
        "--timeout",
        type=float,
        default=90,
        metavar="SECONDS",
        help=(
            "when to stop each request: the empty of --replace, and each "
            "file's load (90)"
        ),
    )
    load.set_defaults(handler=cmd_load)

    query = commands.add_parser(
        "query",
        help="render a query template, run it, print rows and wall time",
        description=(
            "Render a query template as `yizkr publish-queries` does, "
            "with the repository's renderer (or the script's adapter when "
            "yizkr cannot be imported) and a vocabulary map, run it, and "
            "print the rows as tab-separated values on stdout and the "
            "engine's wall time on stderr."
        ),
    )
    query.add_argument("file", type=Path, metavar="FILE.rq")
    query.add_argument(
        "--vocab",
        type=Path,
        metavar="MAP",
        help=(
            "vocabulary map (default: the .vocab.json beside the graph "
            "loaded last)"
        ),
    )
    query.add_argument(
        "--raw",
        action="store_true",
        help="send the file as it is, without rendering",
    )
    query.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="run it N times and report each time (1)",
    )
    query.add_argument(
        "--timeout",
        type=float,
        default=100,
        metavar="SECONDS",
        help="when the engine stops each run of the query (100)",
    )
    query.add_argument(
        "--max-rows",
        type=int,
        metavar="N",
        help="print at most N rows (default: all)",
    )
    query.add_argument(
        "--show",
        action="store_true",
        help="print the rendered query on stderr",
    )
    query.set_defaults(handler=cmd_query)

    down = commands.add_parser(
        "down",
        help="stop and remove the sandbox (safe to repeat)",
        description="Remove the recorded container and the state file.",
    )
    down.set_defaults(handler=cmd_down)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "repeat", 1) < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    if (getattr(args, "max_rows", None) or 0) < 0:
        print("--max-rows must be 0 or more", file=sys.stderr)
        return 2
    if args.state is None:
        args.state = default_state()
    previous = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        return args.handler(args)
    except SandboxError as exc:
        print(f"wdqs_sandbox {args.command}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        message = str(exc) or "interrupted"
        print(f"wdqs_sandbox {args.command}: {message}", file=sys.stderr)
        return 128 + signum_of(exc)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
