#!/usr/bin/env python3
"""A throwaway, network-less query engine for query reviews.

  make wdqs-sandbox-up        (or: uv run python scripts/wdqs_sandbox.py up)
  uv run python scripts/synthetic_graph.py --persons 250 --out DIR/g.ttl
  uv run python scripts/wdqs_sandbox.py load DIR/g.ttl
  uv run python scripts/wdqs_sandbox.py query \\
      src/yizkr/queries/disagreement-report.rq --repeat 3 --max-rows 20
  make wdqs-sandbox-down      (or: uv run python scripts/wdqs_sandbox.py down)

up starts a wikibase/wdqs:2 container (the Blazegraph build the deployment
runs) with no network, no ports and no volumes, capped at 768 MB without
swap and at one CPU, under a unique name that a small state file records
(~/.cache/yizkr/wdqs-sandbox.json). It never touches the deployed stack:
every request reaches the engine through docker exec and the container's
own curl. The image's journal settings ask for 1000 one-megabyte
write-cache buffers, more direct memory than the cap leaves, so up copies
the image's RWStore.properties into the container with 8 before it starts.
up refuses while the recorded container runs; down removes the container
and the state file, and is safe to repeat. Another --state (or
$YIZKR_WDQS_SANDBOX_STATE) runs a second sandbox beside the first.

load adds Turtle (.ttl) or N-Triples (.nt) files to the store; --replace
empties it first. query renders a template as yizkr publish-queries does:
it resolves the {P:key}/{Q:key} placeholders through a vocabulary map
(synthetic_graph.py writes one beside each graph, and query uses the one
beside the graph loaded last unless --vocab names another) and prepends
PREFIX lines for the map's concept URI; --raw sends a finished query as
it is. It prints the rows as tab-separated values on stdout (IRIs
shortened to wd:, wds:, wdref: and wdv:, language tags as value@lang) and
the engine's wall time on stderr; --max-rows 0 prints the header only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE = "wikibase/wdqs:2"
ENDPOINT = "http://localhost:9999/bigdata/namespace/wdq/sparql"
CONFIG = "/wdqs/RWStore.properties"
WRITE_CACHE_BUFFERS = (
    "com.bigdata.journal.AbstractJournal.writeCacheBufferCount"
)
STATE_ENV = "YIZKR_WDQS_SANDBOX_STATE"
LABEL = "org.yizkr.sandbox=wdqs"
FORMATS = {".ttl": "text/turtle", ".nt": "text/plain"}
# curl reports the request's wall time on stderr after this marker.
TIMING = "time_total="
# The engine failed to start; waiting longer will not help.
FATAL_LOG = ("Failed startup of context", "OutOfMemoryError")


class SandboxError(Exception):
    """A failure to report without a traceback."""


def default_state() -> Path:
    if os.environ.get(STATE_ENV):
        return Path(os.environ[STATE_ENV]).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "yizkr" / "wdqs-sandbox.json"


# -- docker -------------------------------------------------------------------


def docker(*args: str, stdin: bytes | None = None, check=True):
    try:
        done = subprocess.run(
            ["docker", *args], input=stdin, capture_output=True
        )
    except FileNotFoundError:
        raise SandboxError("docker is not installed or not on PATH") from None
    if check and done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip()
        raise SandboxError(f"docker {args[0]} failed: {detail}")
    return done


def container_status(name: str) -> str | None:
    """'running', 'exited', 'created', ...; None when there is none."""
    done = docker("inspect", "-f", "{{.State.Status}}", name, check=False)
    if done.returncode != 0:
        return None
    return done.stdout.decode().strip()


def curl(name: str, *args: str, stdin: bytes | None = None):
    """Run curl inside the container (the only way in: it has no
    network)."""
    flags = ["-i"] if stdin is not None else []
    return docker(
        "exec", *flags, name, "curl", "-sS", *args, stdin=stdin, check=False
    )


# -- state --------------------------------------------------------------------


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SandboxError(f"unreadable state file {path}: {exc}") from None


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(path: Path):
    """Serialise up and down on one state file, so two sessions starting
    a sandbox at once cannot both start one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def running_sandbox(state_path: Path) -> str:
    name = read_state(state_path).get("container")
    if not name:
        raise SandboxError(
            f"no sandbox recorded in {state_path}; start one with `up`"
        )
    status = container_status(name)
    if status != "running":
        raise SandboxError(
            f"sandbox {name} is {status or 'gone'}; run `down`, then `up`"
        )
    return name


# -- up / down ----------------------------------------------------------------


def patched_config(name: str) -> bytes:
    """The image's own RWStore.properties with a write cache that fits
    the memory cap, as a tar stream for `docker cp -`."""
    done = docker("cp", f"{name}:{CONFIG}", "-")
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        member = archive.getmembers()[0]
        text = archive.extractfile(member).read().decode()
    pattern = re.compile(rf"^{re.escape(WRITE_CACHE_BUFFERS)}=.*$", re.M)
    if not pattern.search(text):
        raise SandboxError(f"{CONFIG} in {IMAGE} has no {WRITE_CACHE_BUFFERS}")
    data = pattern.sub(f"{WRITE_CACHE_BUFFERS}=8", text).encode()
    info = tarfile.TarInfo(Path(CONFIG).name)
    info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def wait_ready(name: str, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        probe = curl(name, "-m", "5", f"{ENDPOINT}?query=ASK%7B%7D")
        if b"true" in probe.stdout:
            return time.monotonic() - start
        status = container_status(name)
        if status != "running":
            raise SandboxError(f"the container stopped ({status})")
        logs = docker("logs", "--tail", "200", name, check=False)
        output = (logs.stdout + logs.stderr).decode(errors="replace")
        for marker in FATAL_LOG:
            if marker in output:
                line = next(x for x in output.splitlines() if marker in x)
                raise SandboxError(f"the engine failed to start: {line}")
        time.sleep(1)
    raise SandboxError(f"the engine was not ready after {timeout:.0f}s")


def cmd_up(args) -> int:
    # The lock is held until the engine answers, so a second `up` on this
    # state file waits and then finds the sandbox running.
    with locked(args.state):
        old = read_state(args.state).get("container")
        if old:
            status = container_status(old)
            if status == "running":
                raise SandboxError(
                    f"sandbox {old} is already running (state {args.state});"
                    " run `down` first, or pass another --state for a second"
                    " sandbox"
                )
            if status is not None:
                docker("rm", "-f", old)
                print(f"removed the stopped sandbox {old}")
        name = f"yizkr-wdqs-sandbox-{secrets.token_hex(4)}"
        docker(
            "create",
            "--name", name,
            "--label", LABEL,
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
        write_state(
            args.state,
            {
                "container": name,
                "image": args.image,
                "memory": args.memory,
                "cpus": args.cpus,
                "heap": args.heap,
                "direct": args.direct,
                "loaded": [],
            },
        )
        try:
            config = patched_config(name)
            docker("cp", "-", f"{name}:{Path(CONFIG).parent}", stdin=config)
            docker("start", name)
            waited = wait_ready(name, args.timeout)
        except KeyboardInterrupt:
            remove(args.state)
            raise SandboxError(
                "interrupted; the sandbox was removed"
            ) from None
        except SandboxError as exc:
            logs = docker("logs", "--tail", "20", name, check=False)
            output = (logs.stdout + logs.stderr).decode(errors="replace")
            sys.stderr.write(output)
            remove(args.state)
            raise SandboxError(f"{exc}; the sandbox was removed") from None
    print(
        f"sandbox {name} ready after {waited:.0f}s "
        f"(memory {args.memory}, {args.cpus} CPU, heap {args.heap}); "
        f"state {args.state}"
    )
    return 0


def remove(state_path: Path) -> tuple[str | None, bool]:
    """Remove the recorded container and the state file: (the name
    recorded, whether a container by that name existed)."""
    name = read_state(state_path).get("container")
    existed = bool(name) and container_status(name) is not None
    if existed:
        docker("rm", "-f", name)
    with contextlib.suppress(FileNotFoundError):
        state_path.unlink()
    return name, existed


def cmd_down(args) -> int:
    with locked(args.state):
        try:
            read_state(args.state)
        except SandboxError as exc:
            # an unreadable state file names no container to stop
            args.state.unlink()
            raise SandboxError(
                f"{exc}; removed it. List sandbox containers with: docker "
                f"ps -a --filter label={LABEL}"
            ) from None
        name, existed = remove(args.state)
    if not name:
        print("no sandbox recorded")
    elif existed:
        print(f"removed sandbox {name}")
    else:
        print(f"sandbox {name} was already gone; cleared its state")
    return 0


# -- load ---------------------------------------------------------------------


def update(name: str, text: str) -> None:
    done = curl(
        name, "--fail-with-body", "-X", "POST",
        "--data-urlencode", f"update={text}", ENDPOINT,
    )  # fmt: skip
    if done.returncode != 0:
        raise SandboxError(f"update failed: {engine_error(done)}")


def triple_count(name: str) -> int:
    rows, _, _ = run_query(name, "SELECT (COUNT(*) AS ?n) { ?s ?p ?o }", 60)
    return int(rows[0]["n"]["value"])


def cmd_load(args) -> int:
    for path in args.files:
        if path.suffix not in FORMATS:
            raise SandboxError(
                f"{path}: unknown format (use {', '.join(FORMATS)})"
            )
        if not path.is_file():
            raise SandboxError(f"{path}: no such file")
    name = running_sandbox(args.state)
    if args.replace:
        update(name, "DROP ALL")
    loaded = [] if args.replace else read_state(args.state).get("loaded", [])
    for path in args.files:
        done = curl(
            name, "--fail-with-body", "-X", "POST",
            "-H", f"Content-Type: {FORMATS[path.suffix]}",
            "--data-binary", "@-", ENDPOINT,
            stdin=path.read_bytes(),
        )  # fmt: skip
        if done.returncode != 0:
            raise SandboxError(f"{path}: load failed: {engine_error(done)}")
        body = done.stdout.decode(errors="replace")
        added = re.search(r'modified="(\d+)"', body)
        millis = re.search(r'milliseconds="(\d+)"', body)
        print(
            f"loaded {path}: {added.group(1) if added else '?'} triples"
            f" in {int(millis.group(1)) / 1000 if millis else 0:.1f}s"
        )
        loaded.append(str(path.resolve()))
    state = read_state(args.state)
    state["loaded"] = loaded
    write_state(args.state, state)
    print(f"the store holds {triple_count(name)} triples")
    return 0


# -- query --------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{([PQ]):([a-z][a-z0-9_]*)\}")


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


def run_query(name: str, text: str, timeout: float):
    """(rows, variables, seconds the engine took)."""
    done = curl(
        name, "--fail-with-body", "-X", "POST",
        "-m", str(int(timeout) + 30),
        "-H", "Accept: application/sparql-results+json",
        "-H", f"X-BIGDATA-MAX-QUERY-MILLIS: {int(timeout * 1000)}",
        "--data-urlencode", "query@-",
        "-w", f"%{{stderr}}{TIMING}%{{time_total}}\n",
        ENDPOINT,
        stdin=text.encode(),
    )  # fmt: skip
    if done.returncode != 0:
        raise SandboxError(f"query failed: {engine_error(done)}")
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise SandboxError(f"query failed: {engine_error(done)}") from None
    timing = re.search(rf"^{TIMING}([\d.]+)$", done.stderr.decode(), re.M)
    seconds = float(timing.group(1)) if timing else float("nan")
    return result["results"]["bindings"], result["head"]["vars"], seconds


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
    name = running_sandbox(args.state)
    prefixes: list[tuple[str, str]] = []
    if args.raw:
        if PLACEHOLDER.search(template):
            raise SandboxError(
                f"{args.file} has {{P:...}}/{{Q:...}} placeholders; drop "
                f"--raw to render them"
            )
        text = template
    else:
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
    times = []
    for _ in range(args.repeat):
        try:
            rows, variables, seconds = run_query(name, text, args.timeout)
        except SandboxError as exc:
            if not args.raw and "Multiple prefix declarations" in str(exc):
                raise SandboxError(
                    f"{exc} (the file declares its own prefixes: pass --raw)"
                ) from None
            raise
        times.append(seconds)
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
        default=default_state(),
        help=(
            "state file naming the sandbox (default "
            "~/.cache/yizkr/wdqs-sandbox.json, or $YIZKR_WDQS_SANDBOX_STATE);"
            " use another for a second sandbox"
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
        default=180,
        metavar="SECONDS",
        help="how long to wait for the engine (180)",
    )
    up.set_defaults(handler=cmd_up)

    load = commands.add_parser(
        "load",
        help="load Turtle or N-Triples files into the sandbox",
        description=(
            "Add .ttl or .nt files to the sandbox's store, and print how "
            "many triples each added and the store's total."
        ),
    )
    load.add_argument("files", nargs="+", type=Path, metavar="FILE")
    load.add_argument(
        "--replace",
        action="store_true",
        help="empty the store before loading",
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
        default=600,
        metavar="SECONDS",
        help="when the engine stops the query (600)",
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
    try:
        return args.handler(args)
    except SandboxError as exc:
        print(f"wdqs_sandbox {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
