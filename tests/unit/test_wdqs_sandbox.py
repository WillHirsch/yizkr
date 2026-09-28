"""Offline tests for the query-engine sandbox's safety (#251).

A fake `docker` comes first on PATH: its containers and engine state live
in a JSON world file and it logs every call. DOCKER_HOST names a socket
that does not exist, so no real daemon is ever reached. The tests check
that the sandbox acts only on containers it made, that an interrupted or
failed `up` leaves no container, and that an interrupted load or query
leaves nothing running in the engine.
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
and every call is logged. It never talks to a daemon."""
import io, json, os, sys, tarfile, time, uuid

WORLD = os.environ["FAKE_DOCKER_WORLD"]
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
with open(WORLD) as handle:
    world = json.load(handle)


def save():
    with open(WORLD, "w") as handle:
        json.dump(world, handle)


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


def hang(kind):
    """An engine task, or a create, that runs until it is killed."""
    if kind != "create":
        world["running"].append(str(uuid.uuid4()))
        save()
    with open(world["marker"], "w") as handle:
        handle.write(kind)
    time.sleep(30)
    sys.exit(0)


def curl(argv):
    url = next(a for a in argv if a.startswith("http://"))
    if "query=ASK" in url:
        print("<boolean>true</boolean>")
    elif url.endswith("status?showQueries"):
        print("".join(
            f'<a href="?showQueries=details&amp;queryId={q}">details</a>'
            for q in world["running"]
        ))
    elif "cancelQuery" in url:
        query_id = url.rsplit("queryId=", 1)[1]
        world["cancelled"].append(query_id)
        if query_id in world["running"]:
            world["running"].remove(query_id)
        save()
    elif "--data-binary" in argv:
        if world["hang"] == "load":
            hang("load")
        world["triples"] += 12
        save()
        print('<?xml version="1.0"?><data modified="12" milliseconds="3"/>')
    elif "update=DROP ALL" in argv:
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
            rows = [{"n": {"type": "literal", "value": str(world["triples"])}}]
        else:
            if world["hang"] == "query":
                hang("query")
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
    world["containers"].append({
        "name": name, "id": uuid.uuid4().hex + uuid.uuid4().hex,
        "status": "created", "labels": labels,
    })
    save()
    if world["hang"] == "create":
        hang("create")
    print(world["containers"][-1]["id"])
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
        world["killed"].append(program[3])
        save()
    elif program[:2] == ["sh", "-c"]:
        world["pidfiles"].append(program[3])
        save()
        curl(program[4:])
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
            "marker": str(self.marker),
        }
        world.update(settings)
        self.world_path.write_text(json.dumps(world))

    def world(self) -> dict:
        return json.loads(self.world_path.read_text())

    def calls(self) -> list[list[str]]:
        text = self.log_path.read_text()
        return [json.loads(line) for line in text.splitlines()]

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

    def interrupt(self, *args: str, signum=signal.SIGINT):
        """Start a command, send it `signum` once the fake engine (or
        daemon) is busy, and wait for it by its pid."""
        process = subprocess.Popen(
            self.command(*args),
            env=self.env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 30
        while not self.marker.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the fake never got busy"
            time.sleep(0.05)
        process.send_signal(signum)
        out, err = process.communicate(timeout=60)
        return process.returncode, out, err


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
        code, _, err = fake.interrupt("up", signum=signum)
        assert code == 128 + signum
        assert one_line_error(err) == (
            "wdqs_sandbox up: interrupted; the sandbox was removed"
        )
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
        fake.set_world([SANDBOX], hang="query")
        fake.record(SANDBOX["name"], loaded=[])
        code, _, err = fake.interrupt(
            "query", "--raw", str(fake.query), signum=signum
        )
        assert code == 128 + signum
        assert "interrupted; cancelled 1 running engine task" in (
            one_line_error(err)
        )
        world = fake.world()
        assert world["running"] == []
        assert len(world["cancelled"]) == 1
        assert world["killed"] == world["pidfiles"][:1]
        assert not [path for path in world["files"] if path.endswith(".rq")]

    def test_load_replace(self, fake):
        """An interrupted load --replace cancels the load, records the
        emptied store, and uploads the file rather than streaming it."""
        fake.set_world([SANDBOX], hang="load", triples=99)
        fake.record(SANDBOX["name"], loaded=["/elsewhere/old.ttl"])
        code, _, err = fake.interrupt("load", "--replace", str(fake.graph))
        assert code == 130
        message = one_line_error(err)
        assert f"interrupted while loading {fake.graph}" in message
        assert "the store holds 0 triples" in message
        assert json.loads(fake.state.read_text())["loaded"] == []
        world = fake.world()
        assert world["running"] == []
        assert len(world["cancelled"]) == 1
        assert world["killed"]
        (load,) = [c for c in fake.calls() if "--data-binary" in c]
        body = load[load.index("--data-binary") + 1]
        assert body.startswith("@/tmp/yizkr-sandbox-") and body != "@-"
        assert not [path for path in world["files"] if path.endswith(".ttl")]


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
