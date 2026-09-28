"""Offline tests for the query-engine sandbox's safety (#251).

A fake `docker` comes first on PATH: its containers and engine state live
in a JSON world file and it logs every call. DOCKER_HOST names a socket
that does not exist, so no real daemon is ever reached. Its engine wedges
its writer when a write is cancelled, as the real one does (#265 review),
and aborts a write whose curl is killed. The tests check that the sandbox
acts only on containers it made, that an interrupted or failed `up`
leaves no container (even one the daemon makes late), and that an
interrupted load or query leaves nothing running and the engine writable,
even when a second signal reaches the whole process group while it
cleans up. The pid guard of the curl kill runs against real local
processes.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "wdqs_sandbox.py"

_spec = importlib.util.spec_from_file_location("wdqs_sandbox", SCRIPT)
ws = importlib.util.module_from_spec(_spec)
sys.modules["wdqs_sandbox"] = ws
_spec.loader.exec_module(ws)

FAKE_DOCKER = r'''
"""A fake docker CLI: containers and the engine live in a JSON world file,
and every call is logged. It never talks to a daemon. Its engine behaves as
the real one does where the sandbox depends on it: a write task that is
cancelled wedges the writer, so that every later write fails, while a write
whose curl is killed is aborted by the engine itself a moment later. Like
docker's CLI, it takes SIGINT and SIGTERM even when it starts with them
ignored, and then exits 0 with nothing on stdout."""
import html, io, json, os, signal, subprocess, sys, tarfile, time, uuid

WORLD = os.environ["FAKE_DOCKER_WORLD"]
INSERT = "InsertWithBodyTask{namespace=wdq,timestamp=0, isGRSRequired=false}"
args = sys.argv[1:]


def stopped(signum, frame):
    """What a `docker exec` does when a signal reaches the CLI: it exits 0
    with no output, and its command runs on in the container."""
    with open(f"{WORLD}.signalled", "a") as handle:
        handle.write(json.dumps([signum, *args]) + "\n")
    os._exit(0)


signal.signal(signal.SIGINT, stopped)
signal.signal(signal.SIGTERM, stopped)
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
with open(WORLD) as handle:
    world = json.load(handle)


def save():
    tmp = f"{WORLD}.{os.getpid()}"
    with open(tmp, "w") as handle:
        json.dump(world, handle)
    os.replace(tmp, WORLD)


def resolve(ref):
    """As docker resolves: a name, a full ID or a unique ID prefix."""
    for c in world["containers"]:
        if ref in (c["name"], c["id"]):
            return c
    hits = [c for c in world["containers"] if c["id"].startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def no_such(ref):
    sys.stderr.write(f"Error response from daemon: No such container: {ref}\n")
    sys.exit(1)


def busy():
    """Tell the test the fake is now busy, then run until killed."""
    with open(world["marker"], "w") as handle:
        handle.write("busy")
    time.sleep(30)
    sys.exit(0)


def task(kind, text, pidfile, until=None, then=None):
    world["running"].append({
        "id": str(uuid.uuid4()), "kind": kind, "text": text,
        "pidfile": pidfile, "until": until, "then": then,
    })
    save()


def end_tasks():
    """Tasks past their end leave the engine, and a write that ends leaves
    its effect (a drop empties the store)."""
    now = time.time()
    for t in list(world["running"]):
        if t["until"] is not None and t["until"] <= now:
            world["running"].remove(t)
            world.update(t.get("then") or {})


def status_page():
    """The real page's shape: per task, its id in a cancel form, then its
    text (a query as sent, or a write task's name)."""
    end_tasks()
    world["status_seen"].append(len(world["running"]))
    save()
    parts = []
    for t in world["running"]:
        query = t["kind"] == "query"
        tid, text = t["id"], html.escape(t["text"])
        parts.append(
            f'<h1>{"Query" if query else "Task"}</h1>'
            '<FORM method="POST" action=""><p>'
            f'<a href="?showQueries=details&amp;queryId={tid}">details</a>'
            f'</p><p><INPUT type="hidden" name="queryId" value="{tid}">'
            '<INPUT type="submit" name="cancelQuery" value="Cancel"></p>'
            f'</FORM><h2>{"SPARQL" if query else "TASK"}</h2>'
            f'<p class="query-string">{text}</p>'
        )
    return "<html>" + "".join(parts) + "</html>"


def curl(argv, pidfile=None):
    url = next(a for a in argv if a.startswith("http://"))
    writes = "--data-binary" in argv or "update=DROP ALL" in argv
    if "query=ASK" in url:
        print("<boolean>true</boolean>")
    elif url.endswith("status?showQueries"):
        if world["blank_status"]:
            # a status call cut short: exit 0 with nothing on stdout
            world["blank_status"] -= 1
            save()
            sys.exit(0)
        print(status_page())
    elif "cancelQuery" in url:
        task_id = url.rsplit("queryId=", 1)[1]
        world["cancelled"].append(task_id)
        for t in list(world["running"]):
            if t["id"] == task_id:
                world["running"].remove(t)
                if t["kind"] == "write":
                    world["wedged"] = True
        save()
    elif writes and world["wedged"]:
        print("java.util.concurrent.ExecutionException: "
              "AccessSemaphoreNotReentrantException")
        sys.exit(22)
    elif "--data-binary" in argv:
        if world["hang"] == "load":
            task("write", INSERT, pidfile)
            busy()
        if world["hang"] == "load_timeout":
            # curl gives up; the engine goes on with the write a while
            task("write", INSERT, pidfile, until=time.time() + 3)
            sys.stderr.write("curl: (28) Operation timed out\n")
            sys.exit(28)
        world["triples"] += 12
        save()
        print('<?xml version="1.0"?><data modified="12" milliseconds="3"/>')
    elif "update=DROP ALL" in argv:
        if world["hang"] == "drop":
            # the engine goes on with the drop after its curl is killed
            task("write", "DROP ALL", pidfile, then={"triples": 0})
            busy()
        world["triples"] = 0
        save()
        print('<?xml version="1.0"?><data modified="0" milliseconds="1"/>')
    else:
        source = next(a for a in argv if a.startswith("query"))
        if source.startswith("query@"):
            text = world["files"][source[6:]]["text"]
        else:
            text = source[6:]
        if "COUNT(*)" in text:
            end_tasks()
            save()
            rows = [{"n": {"type": "literal", "value": str(world["triples"])}}]
        else:
            if world["hang"] == "query":
                task("query", text, pidfile)
                busy()
            rows = [{"x": {"type": "literal", "value": "1"}}]
        variables = list(rows[0])
        print(json.dumps(
            {"head": {"vars": variables}, "results": {"bindings": rows}}
        ))
        sys.stderr.write("time_total=0.042\n")


command = args[0]
if command == "inspect":
    fmt = args[args.index("-f") + 1]
    c = resolve(args[-1])
    if c is None:
        no_such(args[-1])
    label = c["labels"].get("org.yizkr.sandbox", "<no value>")
    print(
        fmt.replace("{{.Name}}", "/" + c["name"])
        .replace("{{.State.Status}}", c["status"])
        .replace('{{index .Config.Labels "org.yizkr.sandbox"}}', label)
    )
elif command == "create":
    name = args[args.index("--name") + 1]
    labels = dict(
        args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label"
    )
    container = {
        "name": name, "id": uuid.uuid4().hex + uuid.uuid4().hex,
        "status": "created", "labels": labels,
    }
    if world["hang"] == "create_late":
        # the daemon makes the container a second after the client is gone
        late = (
            "import json, os, time\n"
            "time.sleep(1)\n"
            f"path = {WORLD!r}\n"
            "world = json.load(open(path))\n"
            f"world['containers'].append({container!r})\n"
            "tmp = path + '.late'\n"
            "json.dump(world, open(tmp, 'w'))\n"
            "os.replace(tmp, path)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", late], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        busy()
    world["containers"].append(container)
    save()
    if world["hang"] == "create":
        busy()
    print(container["id"])
elif command == "rm":
    c = resolve(args[-1])
    if c is None:
        no_such(args[-1])
    world["containers"].remove(c)
    save()
elif command == "start":
    resolve(args[-1])["status"] = "running"
    save()
elif command == "logs":
    pass
elif command == "cp":
    source, target = args[1], args[2]
    if source == "-":
        directory = target.split(":", 1)[1]
        stream = io.BytesIO(sys.stdin.buffer.read())
        with tarfile.open(fileobj=stream) as archive:
            for member in archive.getmembers():
                path = f"{directory}/{member.name}"
                text = archive.extractfile(member).read().decode()
                world["files"][path] = {"text": text, "mode": member.mode}
                world["uploaded"].append({"path": path, "mode": member.mode})
        save()
    elif world["cp_garbage"]:
        sys.stdout.buffer.write(b"this is not a tar stream")
    else:
        data = (
            b"com.bigdata.journal.AbstractJournal."
            b"writeCacheBufferCount=1000\n"
        )
        info = tarfile.TarInfo("RWStore.properties")
        info.size = len(data)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.addfile(info, io.BytesIO(data))
        sys.stdout.buffer.write(buffer.getvalue())
elif command == "exec":
    rest = args[1:]
    while rest[0].startswith("-"):
        rest = rest[2:] if rest[0] == "-u" else rest[1:]
    c = resolve(rest[0])
    if c is None or c["status"] != "running":
        sys.stderr.write("Error response from daemon: container not running\n")
        sys.exit(1)
    program = rest[1:]
    if program[:2] == ["sh", "-c"] and "kill" in program[2]:
        if world["slow_kill"]:
            # a kill that takes a while, so that a test can signal meanwhile
            with open(world["kill_marker"], "w") as handle:
                handle.write("killing")
            time.sleep(world["slow_kill"])
        pidfile = program[3]
        world["killed"].append(pidfile)
        for t in world["running"]:
            if t["pidfile"] == pidfile and t["kind"] == "write":
                # the engine ends a write that has lost its client: it
                # aborts a load, and runs a drop to its end
                t["until"] = time.time() + world["write_ends_after"]
        save()
    elif program[:2] == ["sh", "-c"]:
        world["pidfiles"].append(program[3])
        save()
        curl(program[4:], program[3])
    elif program[0] == "curl":
        curl(program)
    elif program[0] == "rm":
        for path in program[2:]:
            world["files"].pop(path, None)
            world["removed"].append(path)
        save()
else:
    sys.stderr.write(f"fake docker: unexpected command {args}\n")
    sys.exit(2)
'''

SANDBOX = {
    "name": "yizkr-wdqs-sandbox-feedface-abc123",
    "id": "0dd5" + "4" * 60,
    "status": "running",
    "labels": {"org.yizkr.sandbox": "wdqs"},
}
DEPLOYED = {
    "name": "yizkr-wikibase-wdqs-1",
    "id": "5ca1ab1e" + "0" * 56,
    "status": "running",
    "labels": {"com.docker.compose.project": "yizkr-wikibase"},
}
FOREIGN = {
    "name": "some-app",
    "id": "f00d" + "1" * 60,
    "status": "exited",
    "labels": {},
}
IMPOSTOR = {
    "name": "yizkr-wdqs-sandbox-impostor",
    "id": "baad" + "2" * 60,
    "status": "exited",
    "labels": {},
}
LABELLED_ELSEWHERE = {
    "name": "labelled-elsewhere",
    "id": "c0de" + "3" * 60,
    "status": "running",
    "labels": {"org.yizkr.sandbox": "wdqs"},
}
# Names a state file might hold that are not a sandbox this tool made.
REFUSED = {
    "the deployed query service": DEPLOYED["name"],
    "another tool's container": FOREIGN["name"],
    "an ID prefix of the deployed service": DEPLOYED["id"][:8],
    "an ID prefix of a real sandbox": SANDBOX["id"][:12],
    "a sandbox-like name without the label": IMPOSTOR["name"],
    "the label on another name": LABELLED_ELSEWHERE["name"],
}


class FakeDocker:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        shim = root / "fake_docker.py"
        shim.write_text(FAKE_DOCKER)
        docker = self.bin / "docker"
        docker.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{shim}" "$@"\n'
        )
        docker.chmod(0o755)
        self.world_path = root / "world.json"
        self.log_path = root / "calls.jsonl"
        self.log_path.write_text("")
        self.state = root / "state.json"
        self.marker = root / "started"
        self.graph = root / "g.ttl"
        self.graph.write_text("<urn:s> <urn:p> <urn:o> .\n")
        self.graph.chmod(0o600)
        self.query = root / "q.rq"
        self.query.write_text("SELECT ?x WHERE { ?x ?y ?z }\n")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///nonexistent/yizkr-test/docker.sock",
            "FAKE_DOCKER_WORLD": str(self.world_path),
            "FAKE_DOCKER_LOG": str(self.log_path),
        }
        self.env.pop("YIZKR_WDQS_SANDBOX_STATE", None)
        self.set_world()

    def set_world(self, containers=(), **settings):
        world = {
            "containers": [dict(c) for c in containers],
            "running": [],
            "cancelled": [],
            "killed": [],
            "pidfiles": [],
            "files": {},
            "uploaded": [],
            "removed": [],
            "triples": 0,
            "hang": None,
            "cp_garbage": False,
            "wedged": False,
            "status_seen": [],
            "blank_status": 0,
            "write_ends_after": 0.5,
            "slow_kill": 0,
            "marker": str(self.marker),
            "kill_marker": str(self.root / "killing"),
        }
        world.update(settings)
        self.world_path.write_text(json.dumps(world))

    def world(self) -> dict:
        return json.loads(self.world_path.read_text())

    def calls(self) -> list[list[str]]:
        text = self.log_path.read_text()
        return [json.loads(line) for line in text.splitlines()]

    def signalled(self) -> list[list]:
        """The docker calls a signal stopped: [signum, *args] each."""
        path = self.world_path.with_name(self.world_path.name + ".signalled")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def record(self, container: str, **extra):
        self.state.write_text(json.dumps({"container": container, **extra}))

    def command(self, *args: str) -> list[str]:
        return [sys.executable, str(SCRIPT), "--state", str(self.state), *args]

    def run(self, *args: str, cwd=ROOT) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(*args),
            env=self.env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def main(self, monkeypatch, *args: str) -> int:
        """Run a command in this process, with the fake first on PATH."""
        for key in ("PATH", "DOCKER_HOST", "FAKE_DOCKER_WORLD"):
            monkeypatch.setenv(key, self.env[key])
        monkeypatch.setenv("FAKE_DOCKER_LOG", self.env["FAKE_DOCKER_LOG"])
        monkeypatch.delenv("YIZKR_WDQS_SANDBOX_STATE", raising=False)
        return ws.main(["--state", str(self.state), *args])

    def interrupt(self, *args: str, signum=signal.SIGINT, during=None):
        """Start a command, send it `signum` once the fake engine (or
        daemon) is busy, and wait for it by its pid. `during` is called
        while the fake is busy, before the signal; its result is returned
        last."""
        process = subprocess.Popen(
            self.command(*args),
            env=self.env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for(self.marker, process)
        seen = during() if during else None
        process.send_signal(signum)
        out, err = process.communicate(timeout=60)
        return process.returncode, out, err, seen

    def interrupt_group(self, *args: str, signum, then: Path):
        """As a terminal's Ctrl-C reaches a command: start it in a process
        group of its own, send `signum` to the whole group once the fake
        is busy, and again once the file `then` appears; wait for it by
        its pid."""
        process = subprocess.Popen(
            self.command(*args),
            env=self.env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            for marker in (self.marker, then):
                self.wait_for(marker, process)
                os.killpg(process.pid, signum)
            out, err = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        return process.returncode, out, err

    @staticmethod
    def wait_for(path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while not path.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, f"no {path.name} in time"
            time.sleep(0.05)


@pytest.fixture
def fake(tmp_path) -> FakeDocker:
    fake = FakeDocker(tmp_path)
    assert shutil.which("docker", path=fake.env["PATH"]) == str(
        fake.bin / "docker"
    )
    return fake


def one_line_error(err: str) -> str:
    assert "Traceback" not in err, err
    lines = err.strip().splitlines()
    return lines[-1]


class TestOnlyItsOwnContainers:
    COMMANDS = {
        "up": ["up"],
        "down": ["down"],
        "load": ["load", "{graph}"],
        "load --replace": ["load", "--replace", "{graph}"],
        "query": ["query", "--raw", "{query}"],
    }

    @pytest.mark.parametrize("command", COMMANDS)
    @pytest.mark.parametrize("named", REFUSED)
    def test_refuses_a_container_it_did_not_make(
        self, fake, monkeypatch, capsys, named, command
    ):
        """Whatever a state file names, a command never removes, execs in
        or empties a container this tool did not make."""
        containers = [SANDBOX, DEPLOYED, FOREIGN, IMPOSTOR, LABELLED_ELSEWHERE]
        fake.set_world(containers, triples=99)
        fake.record(REFUSED[named], loaded=[])
        args = [
            a.format(graph=fake.graph, query=fake.query)
            for a in self.COMMANDS[command]
        ]
        assert fake.main(monkeypatch, *args) == 1
        assert "nothing was done" in one_line_error(capsys.readouterr().err)
        assert {call[0] for call in fake.calls()} <= {"inspect"}
        world = fake.world()
        assert world["containers"] == containers
        assert world["triples"] == 99
        assert (
            json.loads(fake.state.read_text())["container"] == (REFUSED[named])
        )

    def test_acts_on_its_own_sandbox(self, fake):
        """The same commands do reach a sandbox this tool made."""
        fake.set_world([SANDBOX, DEPLOYED])
        fake.record(SANDBOX["name"], loaded=[])
        done = fake.run("query", "--raw", str(fake.query))
        assert done.returncode == 0, done.stderr
        assert done.stdout == "x\n1\n"
        done = fake.run("down")
        assert done.returncode == 0, done.stderr
        assert ["rm", "-fv", SANDBOX["name"]] in fake.calls()
        assert fake.world()["containers"] == [DEPLOYED]
        assert not fake.state.exists()

    def test_up_refuses_while_its_sandbox_runs(self, fake):
        fake.set_world([SANDBOX])
        fake.record(SANDBOX["name"])
        done = fake.run("up")
        assert done.returncode == 1
        message = one_line_error(done.stderr)
        assert "already running" in message
        assert "down" not in message
        assert {call[0] for call in fake.calls()} == {"inspect"}


class TestUpLeavesNoContainer:
    @pytest.mark.parametrize(
        "signum", [signal.SIGINT, signal.SIGTERM], ids=["INT", "TERM"]
    )
    def test_interrupted_during_create(self, fake, signum):
        fake.set_world(hang="create")
        code, _, err, _ = fake.interrupt("up", signum=signum)
        assert code == 128 + signum
        assert one_line_error(err) == (
            "wdqs_sandbox up: interrupted; the sandbox was removed"
        )
        (create,) = [c for c in fake.calls() if c[0] == "create"]
        name = create[create.index("--name") + 1]
        assert ["rm", "-fv", name] in fake.calls()
        assert fake.world()["containers"] == []
        assert not fake.state.exists()

    def test_a_create_that_lands_after_the_interrupt(self, fake):
        """The daemon may make the container after the client is killed:
        up waits for it and removes it."""
        fake.set_world(hang="create_late")
        code, _, err, _ = fake.interrupt("up")
        assert code == 130
        assert one_line_error(err).endswith("the sandbox was removed")
        time.sleep(2)  # well past the fake daemon's late create
        (create,) = [c for c in fake.calls() if c[0] == "create"]
        name = create[create.index("--name") + 1]
        assert ["rm", "-fv", name] in fake.calls()
        assert fake.world()["containers"] == []
        assert not fake.state.exists()

    def test_a_failure_that_is_not_a_sandbox_error(self, fake):
        """An unexpected error (here the image's config is not a tar
        stream) still removes the container, then surfaces as itself."""
        fake.set_world(cp_garbage=True)
        done = fake.run("up")
        assert done.returncode == 1
        assert "ReadError" in done.stderr
        assert fake.world()["containers"] == []
        assert not fake.state.exists()
        assert [c for c in fake.calls() if c[:2] == ["rm", "-fv"]]

    def test_names_the_container_after_the_checkout(self, fake, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        fake.set_world()
        done = fake.run("up", cwd=checkout)
        assert done.returncode == 0, done.stderr
        (container,) = fake.world()["containers"]
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        key = ws.checkout_key(Path(top))
        assert container["name"].startswith(f"yizkr-wdqs-sandbox-{key}-")
        assert container["labels"]["org.yizkr.sandbox"] == "wdqs"
        state = json.loads(fake.state.read_text())
        assert state["container"] == container["name"]


class TestInterruptedWorkStops:
    @pytest.mark.parametrize(
        "signum", [signal.SIGINT, signal.SIGTERM], ids=["INT", "TERM"]
    )
    def test_query(self, fake, signum):
        """An interrupted query cancels its own task and no other: a write
        that happens to be running is left to the engine."""
        other_write = {
            "id": "0e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b",
            "kind": "write",
            "text": "InsertWithBodyTask{namespace=wdq}",
            "pidfile": None,
            "until": None,
        }
        fake.set_world([SANDBOX], hang="query", running=[other_write])
        fake.record(SANDBOX["name"], loaded=[])
        code, _, err, _ = fake.interrupt(
            "query", "--raw", str(fake.query), signum=signum
        )
        assert code == 128 + signum
        assert "interrupted; cancelled the query in the engine" in (
            one_line_error(err)
        )
        world = fake.world()
        (cancelled,) = world["cancelled"]
        assert cancelled != other_write["id"]
        assert world["running"] == [other_write]
        assert not world["wedged"]
        assert world["killed"] == world["pidfiles"][:1]
        assert not [path for path in world["files"] if path.endswith(".rq")]

    def test_load_replace(self, fake):
        """An interrupted load --replace records the emptied store before
        it loads, never cancels the write (which would wedge the engine's
        writer), kills its curl, waits for the engine to abort the write,
        and leaves the engine writable. The file is uploaded, not
        streamed."""
        fake.set_world([SANDBOX], hang="load", triples=99)
        fake.record(SANDBOX["name"], loaded=["/elsewhere/old.ttl"])
        code, _, err, loaded_during = fake.interrupt(
            "load",
            "--replace",
            str(fake.graph),
            during=lambda: json.loads(fake.state.read_text())["loaded"],
        )
        assert loaded_during == []
        assert code == 130
        assert (
            "waiting up to 20 s for the engine to finish or abort the write"
            in err.splitlines()
        )
        message = one_line_error(err)
        assert f"interrupted while loading {fake.graph}" in message
        assert "the engine finished or aborted the write" in message
        assert "the store holds 0 triples" in message
        assert json.loads(fake.state.read_text())["loaded"] == []
        world = fake.world()
        assert world["cancelled"] == []
        assert not world["wedged"]
        assert world["killed"]
        assert world["running"] == []
        assert world["status_seen"][-1] == 0
        (load,) = [c for c in fake.calls() if "--data-binary" in c]
        body = load[load.index("--data-binary") + 1]
        assert body.startswith("@/tmp/yizkr-sandbox-") and body != "@-"
        assert not [path for path in world["files"] if path.endswith(".ttl")]
        # the engine still takes writes
        fake.world_path.write_text(json.dumps({**world, "hang": None}))
        done = fake.run("load", str(fake.graph))
        assert done.returncode == 0, done.stderr
        assert fake.world()["triples"] == 12

    def test_load_past_its_timeout(self, fake):
        """curl gives up at --timeout while the engine goes on writing: the
        load waits for the engine to finish, and cancels nothing."""
        fake.set_world([SANDBOX], hang="load_timeout")
        fake.record(SANDBOX["name"], loaded=[])
        done = fake.run("load", "--timeout", "5", str(fake.graph))
        assert done.returncode == 1
        message = one_line_error(done.stderr)
        assert "the load ran past --timeout" in message
        assert "the engine finished or aborted the write" in message
        world = fake.world()
        assert world["cancelled"] == []
        assert not world["wedged"]
        # it saw the write still running, and waited until it was gone
        assert max(world["status_seen"]) >= 1
        assert world["status_seen"][-1] == 0
        assert json.loads(fake.state.read_text())["loaded"] == []

    def test_a_status_page_that_did_not_arrive_whole(self, fake):
        """A status call that a signal stops early exits 0 with nothing on
        stdout (#265 review). The settle wait takes that as an unreadable
        page, not as an idle engine: it waits for a whole page that shows
        no task, so it never reports a drop the engine is still running as
        finished, nor the store's count from before the drop."""
        fake.set_world(
            [SANDBOX],
            hang="drop",
            triples=99,
            blank_status=1,
            write_ends_after=3.0,
        )
        fake.record(SANDBOX["name"], loaded=["/elsewhere/old.ttl"])
        code, _, err, _ = fake.interrupt("load", "--replace", str(fake.graph))
        assert code == 130
        assert (
            "waiting up to 20 s for the engine to finish or abort the write"
            in err.splitlines()
        )
        message = one_line_error(err)
        assert "the engine finished or aborted the write" in message
        assert "the store holds 0 triples" in message
        world = fake.world()
        assert world["blank_status"] == 0  # the blank page was served
        # it waited on, saw the drop still running, then saw it end
        assert world["status_seen"][0] == 1
        assert world["status_seen"][-1] == 0
        assert world["running"] == []
        assert world["cancelled"] == []

    @pytest.mark.parametrize(
        "signum", [signal.SIGINT, signal.SIGTERM], ids=["INT", "TERM"]
    )
    def test_a_second_signal_to_the_group_reaches_no_docker_call(
        self, fake, signum
    ):
        """A terminal's Ctrl-C goes to the whole process group, and docker's
        CLI takes it even while the load holds signals off for its
        clean-up: a stopped `docker exec` exits 0 while its command runs on
        (#265 review). Every docker call runs in a session of its own, so
        neither signal reaches one: a second signal while the load kills
        its curl leaves the kill to finish, and the engine aborts the
        write."""
        fake.set_world([SANDBOX], hang="load", slow_kill=1.5)
        fake.record(SANDBOX["name"], loaded=[])
        code, _, err = fake.interrupt_group(
            "load", str(fake.graph), signum=signum, then=fake.root / "killing"
        )
        assert code == 128 + signum
        message = one_line_error(err)
        assert "the engine finished or aborted the write" in message
        assert fake.signalled() == []
        world = fake.world()
        assert world["killed"] == world["pidfiles"][:1]
        assert world["running"] == []
        assert world["cancelled"] == []
        assert not world["wedged"]


class TestWork:
    def test_load_uploads_a_readable_copy_and_records_it(self, fake):
        """The graph (mode 600 here) reaches the engine's user as a 644
        copy, which is removed once it is loaded."""
        fake.set_world([SANDBOX])
        fake.record(SANDBOX["name"], loaded=[])
        done = fake.run("load", str(fake.graph))
        assert done.returncode == 0, done.stderr
        world = fake.world()
        (upload,) = world["uploaded"]
        assert upload["path"].endswith(".ttl")
        assert upload["mode"] == 0o644
        assert upload["path"] in world["removed"]
        assert world["files"] == {}
        assert world["triples"] == 12
        state = json.loads(fake.state.read_text())
        assert state["loaded"] == [str(fake.graph.resolve())]
        assert "the store holds 12 triples" in done.stdout

    def test_waits_for_another_command_and_says_so(self, fake):
        fake.set_world()
        lock_path = fake.state.with_name(fake.state.name + ".lock")
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                fake.command("down"),
                env=fake.env,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(1)
            assert process.poll() is None
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0
        assert "waiting for" in err
        assert out.strip() == "no sandbox recorded"

    def test_negative_max_rows_is_refused(self, fake):
        done = fake.run("query", "--raw", str(fake.query), "--max-rows", "-1")
        assert done.returncode == 2
        assert "--max-rows" in done.stderr
        assert fake.calls() == []


@pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(), reason="needs /proc"
)
class TestKillCurl:
    """The snippet that kills an interrupted command's curl, run here with
    sh against real processes: it kills the pid in the pid file only while
    that pid's command line carries the command's token."""

    TOKEN = "c0ffee42"

    def kill(self, pidfile: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "-c", ws.KILL_CURL, str(pidfile), self.TOKEN],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_kills_the_process_that_carries_the_token(self, tmp_path):
        curl = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                f"X-Yizkr-Sandbox: {self.TOKEN}",
            ]
        )
        pidfile = tmp_path / "curl.pid"
        pidfile.write_text(f"{curl.pid}\n")
        assert self.kill(pidfile).returncode == 0
        assert curl.wait(timeout=10) == -signal.SIGTERM

    def test_spares_a_process_that_took_the_pid(self, tmp_path):
        stranger = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            pidfile = tmp_path / "curl.pid"
            pidfile.write_text(f"{stranger.pid}\n")
            assert self.kill(pidfile).returncode == 0
            time.sleep(0.3)
            assert stranger.poll() is None
        finally:
            stranger.kill()
            stranger.wait()

    def test_a_missing_pid_file_is_quiet(self, tmp_path):
        done = self.kill(tmp_path / "none.pid")
        assert (done.returncode, done.stderr) == (0, "")


class TestStatePerCheckout:
    def test_default_state_follows_the_checkout(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YIZKR_WDQS_SANDBOX_STATE", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        paths = []
        for name in ("one", "two"):
            checkout = tmp_path / name
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            monkeypatch.chdir(checkout)
            paths.append(ws.default_state())
        assert paths[0] != paths[1]
        for path in paths:
            assert path.parent == tmp_path / "cache" / "yizkr"
            assert path.name.startswith("wdqs-sandbox-")

    def test_the_environment_names_the_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YIZKR_WDQS_SANDBOX_STATE", str(tmp_path / "s"))
        assert ws.default_state() == tmp_path / "s"
