"""Offline guards for the disposable fictional integration runner."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "fictional_integration.py"
)
_spec = importlib.util.spec_from_file_location("fictional_integration", SCRIPT)
lane = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lane)

# Only bounds a missed readiness/exit cue; never a delay under test.
HANG_GUARD = 60
# Fake seconds a stack start, guard probe and stack removal take.
UP_SECONDS, PROBE_SECONDS, DOWN_SECONDS = 7.0, 2.0, 3.0


def group_names(cases=lane.REQUIRED_CASES) -> list[str]:
    """Group names in the order of each one's first declared case."""
    return list(dict.fromkeys(case.group for case in cases))


def declared(name, cases=lane.REQUIRED_CASES) -> tuple:
    """A group's cases in the order the declaration lists them."""
    return tuple(case for case in cases if case.group == name)


def spread(count: int) -> tuple:
    """The required cases dealt into `count` groups no declaration names."""
    return tuple(
        lane.Case(f"part-{index % count}", case.module, case.name)
        for index, case in enumerate(lane.REQUIRED_CASES)
    )


# Expectations come from the declaration itself, not from the launcher's
# grouping of it; nothing below depends on the groups' names.
NAMES = group_names()
FIRST, LAST = NAMES[0], NAMES[-1]


def put(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    files = {
        "pyproject.toml": "# fictional test project\n",
        "uv.lock": "# fictional lock\n",
        "README.md": "Fictional integration\n",
        "src/yizkr/example.py": "VALUE = 'fictional'\n",
        "tests/__init__.py": "",
        "tests/fixtures/example.txt": "fictional source\n",
        "tests/fictional_integration/test_example.py": "# fixture test\n",
        "scripts/fictional_integration.py": "# fixture runner\n",
        "deploy/fictional-integration/compose.yml": "services: {}\n",
        "deploy/fictional-integration/Dockerfile": "# fixture build\n",
        "deploy/config/Uploads.php": "<?php // fixture uploads\n",
        "deploy/.env": "MW_ADMIN_PASS=not-a-real-credential\n",
        "var/example.txt": "fictional runtime sentinel\n",
        ".gitignore": "src/ignored.txt\n",
    }
    for name, content in files.items():
        put(repo, name, content)
    # Even accidentally tracked runtime/configuration files must stay out.
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    put(repo, "private-notes.txt", "fictional untracked sentinel\n")
    put(repo, "src/ignored.txt", "fictional ignored sentinel\n")
    put(repo, "tests/fixtures/new.txt", "new fictional fixture\n")
    return repo


def test_build_context_contains_only_selected_repository_inputs(
    source_repo, tmp_path
):
    context = tmp_path / "context"
    first_hash = lane.stage_context(source_repo, context)
    files = {
        path.relative_to(context).as_posix()
        for path in context.rglob("*")
        if path.is_file()
    }
    assert "src/yizkr/example.py" in files
    assert "tests/fixtures/new.txt" in files
    assert "deploy/fictional-integration/Uploads.php" in files
    assert not files.intersection(
        {
            "deploy/.env",
            "var/example.txt",
            "private-notes.txt",
            "src/ignored.txt",
            ".git/config",
        }
    )

    put(source_repo, "deploy/.env", "MW_ADMIN_PASS=changed-fictional-value\n")
    put(source_repo, "var/example.txt", "changed fictional runtime\n")
    assert lane.stage_context(source_repo, context) == first_hash
    put(source_repo, "src/yizkr/example.py", "VALUE = 'changed fixture'\n")
    assert lane.stage_context(source_repo, context) != first_hash


def test_runtime_code_probe_uses_real_git_and_detects_executable_edits(
    tmp_path,
):
    snapshot = tmp_path / "snapshot"
    shutil.copytree(
        SCRIPT.parents[1] / "src",
        snapshot / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (snapshot / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
        "PYTHONPATH": str(snapshot / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "YIZKR_ENV_FILE": "/nonexistent",
    }
    for args in (
        ["init", "--quiet", "--initial-branch=fictional-integration"],
        ["add", "."],
        [
            "-c",
            "user.name=Fictional Integration",
            "-c",
            "user.email=fictional@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--message=Fictional source snapshot",
        ],
    ):
        subprocess.run(["git", *args], cwd=snapshot, env=env, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=snapshot,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    original = (snapshot / "src/yizkr/__init__.py").read_bytes()
    probe = subprocess.run(
        [sys.executable, "-c", lane.CODE_ADMISSION_PROBE],
        cwd=snapshot,
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert lane.runtime_code_evidence(probe.stdout) == {
        "runtime_snapshot_kind": "fictional-source-snapshot",
        "runtime_snapshot_commit": commit,
        "dirty_code_refused": True,
    }
    assert (snapshot / "src/yizkr/__init__.py").read_bytes() == original
    subprocess.run(
        ["git", "diff", "--exit-code"],
        cwd=snapshot,
        env=env,
        check=True,
    )


@pytest.mark.parametrize(
    "output",
    [
        "not-json",
        "{}",
        "[]",
        json.dumps(
            {
                "runtime_snapshot_kind": "fictional-source-snapshot",
                "runtime_snapshot_commit": "unknown",
                "dirty_code_refused": True,
            }
        ),
    ],
)
def test_runtime_code_evidence_refuses_incomplete_probe(output):
    with pytest.raises(lane.LaneError, match="runtime code admission"):
        lane.runtime_code_evidence(output)


@pytest.mark.parametrize("outside_directory", [False, True])
def test_build_context_refuses_symlink_escape(
    source_repo, tmp_path, outside_directory
):
    outside = put(tmp_path, "outside/fictional.txt", "outside sentinel\n")
    target = outside.parent if outside_directory else outside
    (source_repo / "src" / "escape").symlink_to(
        target, target_is_directory=outside_directory
    )
    with pytest.raises(lane.LaneError, match="non-symlink"):
        lane.stage_context(source_repo, tmp_path / "context")


def test_docker_transport_does_not_inherit_archive_or_compose_settings(
    monkeypatch, tmp_path
):
    forbidden = {
        "MW_ADMIN_PASS": "fictional-host-password",
        "WIKIBASE_URL": "http://fictional-archive.invalid",
        "WDQS_SPARQL_URL": "http://fictional-query.invalid/sparql",
        "YIZKR_ENV_FILE": str(tmp_path / "fictional.env"),
        "YIZKR_CORPUS_DIR": str(tmp_path / "fictional-corpus"),
        "YIZKR_INSTANCE_STATE": str(tmp_path / "fictional-instance"),
        "YIZKR_LIVE_LOCK": str(tmp_path / "fictional-live.lock"),
        "COMPOSE_FILE": "fictional-archive.yml",
        "COMPOSE_PROJECT_NAME": "fictional-archive",
        "COMPOSE_ENV_FILES": "fictional.env",
        "DOCKER_HOST": "tcp://fictional-daemon.invalid:2376",
        "DOCKER_CONTEXT": "fictional-remote",
        "DOCKER_TLS": "1",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": str(tmp_path / "fictional-tls"),
    }
    transport = {
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
        "HTTPS_PROXY": "http://fictional-proxy.invalid:8080",
        "NO_PROXY": "localhost,127.0.0.1",
        "SSL_CERT_FILE": str(tmp_path / "fictional-ca.pem"),
    }
    for name, value in {**forbidden, **transport}.items():
        monkeypatch.setenv(name, value)
    env = lane.docker_environment("fictional-runner:test")
    assert not forbidden.keys() & env.keys()
    assert transport.items() <= env.items()
    assert env["YIZKR_FICTIONAL_IMAGE"] == "fictional-runner:test"


def results(path: Path, cases, seconds=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for class_name, name, outcome in cases:
        attributes = {"classname": class_name, "name": name}
        # A time of None leaves the attribute out.
        if (value := (seconds or {}).get(name, 1.5)) is not None:
            attributes["time"] = str(value)
        case = ET.SubElement(suite, "testcase", **attributes)
        if outcome:
            ET.SubElement(case, outcome)
    ET.ElementTree(root).write(path)
    return path


def ran(observed: dict) -> list[tuple[str, str]]:
    return [(case["module"], case["name"]) for case in observed["cases"]]


def passing(cases) -> list[tuple[str, str, None]]:
    return [(*case.result, None) for case in cases]


def test_required_results_accept_completed_boundary_case(tmp_path):
    path = results(tmp_path / "results.xml", passing(lane.REQUIRED_CASES))
    observed = lane.verify_results(path)
    assert observed["passed"] == len(lane.REQUIRED_CASES)
    assert observed["skipped"] == 0
    assert ran(observed) == [case.result for case in lane.REQUIRED_CASES]


def test_required_results_record_each_case_duration(tmp_path):
    seconds = {
        case.name: 10.25 + index
        for index, case in enumerate(lane.REQUIRED_CASES)
    }
    path = results(
        tmp_path / "results.xml", passing(lane.REQUIRED_CASES), seconds
    )
    observed = lane.verify_results(path)["cases"]
    assert {case["name"]: case["seconds"] for case in observed} == seconds


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_required_results_refuse_unexecuted_or_failed_check(tmp_path, outcome):
    # In particular, pytest can exit zero with a skipped required case.
    path = results(
        tmp_path / "results.xml", [(*lane.REQUIRED_CASES[0].result, outcome)]
    )
    with pytest.raises(lane.LaneError, match="failed or skipped"):
        lane.verify_results(path)


@pytest.mark.parametrize("cases", [[], [("another.module", "other", None)]])
def test_required_results_refuse_missing_boundary_case(tmp_path, cases):
    path = results(tmp_path / "results.xml", cases)
    with pytest.raises(lane.LaneError, match="did not run exactly once"):
        lane.verify_results(path)


def test_required_results_refuse_duplicate_boundary_case(tmp_path):
    case = (*lane.REQUIRED_CASES[0].result, None)
    path = results(tmp_path / "results.xml", [case, case])
    with pytest.raises(lane.LaneError, match="did not run exactly once"):
        lane.verify_results(path)


def test_required_results_refuse_extra_cases_even_when_all_required_pass(
    tmp_path,
):
    path = results(
        tmp_path / "results.xml",
        [
            *passing(lane.REQUIRED_CASES),
            ("fictional.extra", "test_not_in_the_required_lane", None),
        ],
    )
    with pytest.raises(lane.LaneError, match="unexpected"):
        lane.verify_results(path)


def test_a_case_without_a_recorded_time_is_reported_last_without_one(
    tmp_path,
):
    cases = lane.REQUIRED_CASES[:3]
    untimed = cases[0]
    path = results(
        tmp_path / "results.xml", passing(cases), {untimed.name: None}
    )
    observed = lane.verify_results(path, cases)
    assert [case["seconds"] for case in observed["cases"]] == [None, 1.5, 1.5]

    report = lane.timing_report({"groups": {FIRST: {"checks": observed}}})
    rows = [line for line in report.splitlines() if "::" in line]
    assert len(rows) == len(cases)
    assert untimed.name in rows[-1]
    assert rows[-1].startswith("| - |")


def test_group_results_cannot_borrow_a_case_from_another_group(tmp_path):
    own = declared(FIRST)
    other = declared(LAST)
    path = results(tmp_path / "results.xml", passing((*own[:-1], other[0])))
    with pytest.raises(lane.LaneError):
        lane.verify_results(path, own)


def test_group_reports_combine_into_complete_required_results(tmp_path):
    paths = [
        results(tmp_path / name / "results.xml", passing(declared(name)))
        for name in NAMES
    ]
    combined = tmp_path / "results.xml"
    lane.combine_results(paths, combined, lane.REQUIRED_CASES)

    observed = lane.verify_results(combined)
    assert observed["passed"] == len(lane.REQUIRED_CASES)
    assert set(ran(observed)) == {case.result for case in lane.REQUIRED_CASES}


def test_combining_reports_cannot_hide_duplicate_or_missing_groups(tmp_path):
    only = results(tmp_path / "first.xml", passing(declared(FIRST)))
    with pytest.raises(lane.LaneError):
        lane.combine_results(
            [only, only], tmp_path / "results.xml", lane.REQUIRED_CASES
        )


@pytest.mark.parametrize("omitted", range(len(lane.REQUIRED_CASES)))
def test_required_results_refuse_any_omitted_boundary_case(tmp_path, omitted):
    path = results(
        tmp_path / "results.xml",
        passing(
            case
            for index, case in enumerate(lane.REQUIRED_CASES)
            if index != omitted
        ),
    )
    with pytest.raises(lane.LaneError, match="did not run exactly once"):
        lane.verify_results(path)


@pytest.mark.parametrize("content", [None, "not XML"])
def test_required_results_refuse_missing_or_invalid_report(tmp_path, content):
    path = tmp_path / "results.xml"
    if content is not None:
        path.write_text(content)
    with pytest.raises(lane.LaneError, match="missing or invalid"):
        lane.verify_results(path)


def test_lane_lock_excludes_a_second_open_and_releases_after_failure(tmp_path):
    state = tmp_path / "first-lane"
    with (
        pytest.raises(RuntimeError, match="fixture interruption"),
        lane.locked(state),
    ):
        with (
            pytest.raises(lane.LaneError, match="already running"),
            lane.locked(state),
        ):
            pytest.fail("two independent descriptors held the lock")
        # Independent lane state does not contend for an archive lock.
        with lane.locked(tmp_path / "second-lane"):
            pass
        raise RuntimeError("fixture interruption")
    with lane.locked(state):
        pass


def test_interrupted_command_reaps_its_process_and_stops_descendants(
    monkeypatch,
):
    ready_read, ready_write = os.pipe()
    cue_read, cue_write = os.pipe()
    open_fds = {ready_read, ready_write, cue_read, cue_write}
    children = []
    descendant = []
    popen = subprocess.Popen

    def interrupt_when_ready(timeout=None):
        readable, _, _ = select.select([ready_read], [], [], HANG_GUARD)
        assert readable, "the child did not start its descendant"
        descendant.append(int(os.read(ready_read, 100)))
        raise KeyboardInterrupt("fictional command interrupted")

    def start(args, **kwargs):
        child = popen(args, pass_fds=(ready_write, cue_read), **kwargs)
        children.append(child)
        for fd in (ready_write, cue_read):
            os.close(fd)
            open_fds.remove(fd)
        monkeypatch.setattr(child, "communicate", interrupt_when_ready)
        return child

    # The descendant inherits the ready pipe, so EOF proves it exited too.
    script = """\
import os
import subprocess
import sys

ready, cue = map(int, sys.argv[1:])
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os, sys; os.read(int(sys.argv[1]), 1)",
        str(cue),
    ],
    pass_fds=(ready, cue),
)
os.write(ready, str(child.pid).encode())
child.wait()
"""
    monkeypatch.setattr(lane.subprocess, "Popen", start)
    try:
        with pytest.raises(KeyboardInterrupt, match="fictional command"):
            lane.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ready_write),
                    str(cue_read),
                ]
            )
        assert children[0].returncode is not None
        readable, _, _ = select.select([ready_read], [], [], HANG_GUARD)
        assert readable, "command descendant survived process-group cleanup"
        assert os.read(ready_read, 1) == b""
    finally:
        for child in children:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=HANG_GUARD)
        for pid in descendant:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        for fd in open_fds:
            os.close(fd)


class CheckProcess:
    """A Compose child and its test container have separate lifetimes."""

    def __init__(self, docker, args, log):
        self.docker = docker
        self.args = args
        self.project = args[args.index("--project-name") + 1]
        self.name = args[args.index("--name") + 1]
        self.pid = 10000 + len(docker.processes)
        self.group = docker.group(self.project)
        # pytest reports exactly the selected cases, in selection order.
        self.cases = []
        for arg in args:
            if arg.startswith("tests/fictional_integration/"):
                path, _, name = arg.partition("::")
                module = path.removesuffix(".py").replace("/", ".")
                self.cases.append((module, name))
        self.returncode = None
        self.created = docker.ticks
        self.container_running = True
        self.terminated = False
        self.killed = False
        self.completed = False
        self.log = log
        self.log.write(f"fictional {self.group} checks started\n")
        self.log.flush()

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.docker.ticks > self.created:
            code = self.docker.returncodes.get(self.group, 0)
            if code is not None:
                self.returncode = code
                self.container_running = False
                self.completed = True
                self.docker.finished.append(self.group)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.group not in self.docker.stubborn:
            self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def stop_container(self):
        self.container_running = False
        if self.group not in self.docker.stubborn:
            self.returncode = -signal.SIGTERM


class Docker:
    """Stateful transport: commands affect projects, processes and reports."""

    def __init__(self, monkeypatch, state):
        self.state = state
        self.active = set()
        self.up = []
        self.down = []
        self.processes = {}
        self.environments = []
        self.finished = []
        self.stopped = []
        self.returncodes = {}
        self.reports = {}
        self.seconds = {}
        self.leftovers = {}
        self.stubborn = set()
        # A group's stack removal makes another group's checks exit.
        self.exits_on_down = {}
        # Polls see a check finish once ticks pass; only evidence reads the
        # clock, which also advances while a stack starts or is removed.
        self.ticks = 0
        self.clock = 0.0
        self.up_times = []
        self.maximum_running = 0
        self.maximum_stacks = 0
        self.builds = 0
        self.stages = 0
        self.interrupt = None
        self.stage_failure = False
        self.setup_failure = None
        self.cleanup_failure = None
        self.log_failure = None
        self.guard_probe_failure = None
        self.guard_probes = []
        self.runtime_code_probes = 0
        self.runtime_code_failure = False
        monkeypatch.setattr(lane, "run", self.run)
        monkeypatch.setattr(lane, "stage_context", self.stage)
        monkeypatch.setattr(lane.subprocess, "run", self.run)
        monkeypatch.setattr(lane.subprocess, "Popen", self.start)
        monkeypatch.setattr(lane.time, "sleep", self.advance)
        monkeypatch.setattr(lane.time, "monotonic", lambda: self.clock)
        monkeypatch.setattr(lane.os, "killpg", self.signal_process)

    @staticmethod
    def group(project):
        """A group project's suffix; the unsuffixed project is "legacy"."""
        match = re.fullmatch(
            r"yizkr-fictional-[0-9a-f]{16}(?:-(.+))?", project
        )
        assert match, project
        return match.group(1) or "legacy"

    def stage(self, repo, context):
        self.stages += 1
        assert self.evidence()["status"] == "failed"
        assert self.evidence()["revision"] == "fixture-revision"
        assert not (self.state / "results.xml").exists()
        for name in self.evidence()["groups"]:
            assert not (self.state / name / "results.xml").exists()
            assert self.evidence(name)["status"] == "failed"
        if self.stage_failure:
            raise lane.LaneError("fictional setup unavailable")
        return "fixture-source-hash"

    def advance(self, delay):
        # An interruption arrives while the launcher waits for its checks.
        if self.interrupt:
            interruption, self.interrupt = self.interrupt, None
            if interruption == "term":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            raise KeyboardInterrupt("fictional interrupted check")
        self.ticks += 1
        self.clock += 1
        assert self.ticks < 10, "supervisor did not stop after child failure"

    def start(self, args, **kwargs):
        self.environments.append(kwargs["env"])
        process = CheckProcess(self, args, kwargs["stdout"])
        self.processes[process.name] = process
        self.maximum_running = max(
            self.maximum_running,
            sum(p.returncode is None for p in self.processes.values()),
        )
        return process

    def signal_process(self, pid, signum):
        process = next(p for p in self.processes.values() if p.pid == pid)
        if signum == signal.SIGTERM:
            process.terminate()
        elif signum == signal.SIGKILL:
            process.kill()
        else:
            pytest.fail(f"unexpected child signal: {signum}")

    def run(self, args, **kwargs):
        args = list(args)
        output = ""
        code = 0
        project = (
            args[args.index("--project-name") + 1]
            if "--project-name" in args
            else None
        )
        if args and "docker" in Path(args[0]).name:
            self.environments.append(kwargs.get("env", {}))
        group = self.group(project) if project else None
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            output = "fixture-revision\n"
        elif "build" in args:
            self.builds += 1
        elif "info" in args:
            output = (
                '{"ServerVersion":"fixture","NCPU":4,"MemTotal":8000000000}'
            )
        elif "up" in args:
            self.up.append(project)
            self.up_times.append(self.clock)
            self.clock += UP_SECONDS
            self.active.add(project)
            self.maximum_stacks = max(self.maximum_stacks, len(self.active))
            if group == self.setup_failure:
                raise subprocess.CalledProcessError(1, args)
        elif "run" in args and "-c" in args:
            assert "--no-deps" in args and "--rm" in args
            assert args[-1] == lane.CODE_ADMISSION_PROBE
            self.runtime_code_probes += 1
            self.active.add(project)
            output = json.dumps(
                {
                    "runtime_snapshot_kind": "fictional-source-snapshot",
                    "runtime_snapshot_commit": "f" * 40,
                    "dirty_code_refused": not self.runtime_code_failure,
                }
            )
        elif "exec" in args:
            assert args[-3:] == [
                "wikibase",
                "php",
                "/yizkr-fictional-mutation-guard-probe.php",
            ]
            self.guard_probes.append(group)
            self.clock += PROBE_SECONDS
            result = {
                "protocol": "yizkr-mutation-guard-v1",
                "request_drain": True,
                "shutdown_drain": True,
                "restart": True,
                "stale_capability": True,
                "invalid_state": True,
                "database_drain": True,
            }
            if self.guard_probe_failure == group:
                result["database_drain"] = False
            output = json.dumps(result)
        elif "down" in args:
            self.down.append(project)
            self.clock += DOWN_SECONDS
            assert_cleanup_covers_all_volumes(args)
            if group in self.exits_on_down and project in self.active:
                other, exit_code = self.exits_on_down.pop(group)
                self.returncodes[other] = exit_code
            if group == self.cleanup_failure and project in self.active:
                raise OSError("fictional cleanup transport failed")
            self.active.discard(project)
        elif "logs" in args:
            if group == self.log_failure:
                raise subprocess.TimeoutExpired(args, 30)
        elif "stop" in args:
            for name in args:
                if name in self.processes:
                    self.stopped.append(name)
                    self.processes[name].stop_container()
        elif "cp" in args:
            name = args[-2].split(":", 1)[0]
            process = self.processes.get(name)
            if process is None or not process.completed:
                code = 1
            else:
                cases = self.reports.get(
                    process.group,
                    [(*case, None) for case in process.cases],
                )
                if cases is None:
                    code = 1
                else:
                    results(Path(args[-1]), cases, self.seconds)
        elif "ps" in args:
            output = f"{project}-checks\n"
        elif "inspect" in args:
            output = "fictional-test-image sha256:fixture\n"
        elif "ls" in args and "--filter" in args:
            project = args[args.index("--filter") + 1].rsplit("=", 1)[1]
            group = self.group(project)
            kind = args[args.index("ls") - 1]
            count = self.leftovers.get((group, kind), 0)
            if project in self.active:
                count += 1
            output = "fictional-leftover\n" * count
        if code and kwargs.get("check", True):
            raise subprocess.CalledProcessError(code, args)
        return subprocess.CompletedProcess(args, code, stdout=output)

    def evidence(self, group=None):
        folder = self.state if group is None else self.state / group
        return json.loads((folder / "evidence.json").read_text())

    def assert_clean(self):
        assert self.active == set()
        assert all(not p.container_running for p in self.processes.values())
        assert all(p.returncode is not None for p in self.processes.values())
        assert not (self.state / "instance.json").exists()
        assert self.evidence()["teardown"] == "passed"


@pytest.fixture
def docker(monkeypatch, tmp_path):
    return Docker(monkeypatch, tmp_path / "lane")


def assert_cleanup_covers_all_volumes(args):
    """An inactive test profile leaves its /state volume behind."""
    spec = yaml.safe_load(
        (
            SCRIPT.parents[1] / "deploy/fictional-integration/compose.yml"
        ).read_text()
    )
    active_profiles = {
        args[index + 1]
        for index, arg in enumerate(args[: args.index("down")])
        if arg == "--profile"
    }
    covered = set()
    for service in spec["services"].values():
        profiles = set(service.get("profiles", []))
        if (
            profiles
            and "*" not in active_profiles
            and not profiles.intersection(active_profiles)
        ):
            continue
        covered.update(
            mount.split(":")[0] for mount in service.get("volumes", [])
        )
    assert "test-state" in spec["volumes"]
    assert set(spec["volumes"]) <= covered
    assert "--volumes" in args


def test_success_runs_every_group_once_and_requires_complete_evidence(
    docker,
):
    lane.execute("run", docker.state)

    assert docker.stages == docker.builds == 1
    assert docker.runtime_code_probes == 1
    assert docker.maximum_running == min(len(NAMES), lane.DEFAULT_STACKS)
    assert docker.guard_probes == NAMES
    assert [docker.group(project) for project in docker.up] == NAMES
    assert sorted(docker.finished) == sorted(NAMES)
    assert docker.evidence()["status"] == "passed"
    assert docker.evidence()["revision"] == "fixture-revision"
    assert docker.evidence()["source_sha256"] == "fixture-source-hash"
    assert docker.evidence()["runtime_snapshot_commit"] == "f" * 40
    assert (
        docker.evidence()["runtime_snapshot_kind"]
        == "fictional-source-snapshot"
    )
    assert docker.evidence()["dirty_code_refused"] is True
    observed = lane.verify_results(docker.state / "results.xml")
    assert set(ran(observed)) == {case.result for case in lane.REQUIRED_CASES}
    for name in NAMES:
        assert docker.evidence(name)["status"] == "passed"
        assert docker.evidence(name)["runtime_snapshot_commit"] == "f" * 40
        assert docker.evidence(name)["mutation_guard_probe"]["database_drain"]
        expected = declared(name)
        observed = lane.verify_results(
            docker.state / name / "results.xml", expected
        )
        assert ran(observed) == [case.result for case in expected]
    docker.assert_clean()


@pytest.mark.parametrize("stacks", [1, 2, 3])
def test_groups_beyond_the_stack_limit_wait_for_a_removed_stack(
    docker, stacks
):
    # Five groups, none of them declared ones: a new group needs no change
    # to the launcher or to any other group.
    cases = spread(5)

    lane.execute("run", docker.state, stacks=stacks, cases=cases)

    assert docker.maximum_stacks == docker.maximum_running == stacks
    names = group_names(cases)
    assert [docker.group(project) for project in docker.up] == names
    assert docker.evidence()["stacks"] == stacks
    for name in names:
        members = declared(name, cases)
        observed = lane.verify_results(
            docker.state / name / "results.xml", members
        )
        assert ran(observed) == [case.result for case in members]
    combined = lane.verify_results(docker.state / "results.xml", cases)
    assert combined["passed"] == len(cases)
    docker.assert_clean()


def test_a_failure_stops_the_lane_before_a_waiting_group_starts(docker):
    docker.returncodes[FIRST] = 1

    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state, stacks=1)

    assert [docker.group(project) for project in docker.up] == [FIRST]
    assert docker.evidence()["status"] == "failed"
    assert not (docker.state / "results.xml").exists()
    docker.assert_clean()


def test_a_failure_during_another_groups_teardown_starts_no_waiting_group(
    docker,
):
    cases = spread(3)
    first, second, _ = group_names(cases)
    # The second group's checks fail while the first group's stack is
    # being removed, before the third group has started.
    docker.returncodes[second] = None
    docker.exits_on_down[first] = (second, 1)

    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state, stacks=2, cases=cases)

    assert [docker.group(project) for project in docker.up] == [first, second]
    assert docker.evidence()["status"] == "failed"
    docker.assert_clean()


def test_a_finished_groups_failed_teardown_starts_no_waiting_group(docker):
    cases = spread(3)
    first = group_names(cases)[0]
    docker.cleanup_failure = first

    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state, stacks=1, cases=cases)

    assert [docker.group(project) for project in docker.up] == [first]
    assert docker.evidence(first)["teardown"] == "failed"
    assert docker.evidence()["teardown"] == "failed"


def test_run_records_case_and_group_durations_and_reports_slowest_first(
    docker, monkeypatch, capsys, tmp_path
):
    summary = tmp_path / "job-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    docker.seconds = {
        case.name: 5.0 * index
        for index, case in enumerate(lane.REQUIRED_CASES)
    }

    # One stack at a time, so no group's phases overlap another's.
    lane.execute("run", docker.state, stacks=1)

    assert docker.evidence()["setup_seconds"] == docker.up_times[0]
    assert docker.evidence()["elapsed_seconds"] == docker.clock
    for group in NAMES:
        recorded = docker.evidence(group)
        assert {
            case["name"]: case["seconds"]
            for case in recorded["checks"]["cases"]
        } == {case.name: docker.seconds[case.name] for case in declared(group)}
        assert recorded["stack_seconds"] == UP_SECONDS + PROBE_SECONDS
        assert recorded["checks_seconds"] > 0
        assert recorded["teardown_seconds"] == DOWN_SECONDS
        phases = ("stack_seconds", "checks_seconds", "teardown_seconds")
        assert recorded["elapsed_seconds"] == sum(map(recorded.get, phases))
    printed = capsys.readouterr().out
    slowest = sorted(docker.seconds, key=docker.seconds.get, reverse=True)
    shown = [printed.index(name) for name in slowest[: lane.SLOWEST]]
    assert shown == sorted(shown)
    assert not any(name in printed for name in slowest[lane.SLOWEST :])
    assert summary.read_text()
    assert summary.read_text() in printed


def test_failed_run_still_reports_the_durations_it_recorded(
    docker, monkeypatch, tmp_path
):
    summary = tmp_path / "job-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    docker.returncodes[LAST] = 1

    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state)

    report = summary.read_text()
    assert all(case.name in report for case in declared(FIRST))
    assert not any(case.name in report for case in declared(LAST))


def test_runtime_code_admission_failure_prevents_service_startup(docker):
    docker.runtime_code_failure = True
    with pytest.raises(lane.LaneError, match="runtime code admission"):
        lane.execute("run", docker.state)
    assert docker.runtime_code_probes == 1
    assert docker.up == []
    assert not docker.processes
    assert docker.evidence()["status"] == "failed"
    docker.assert_clean()


@pytest.mark.parametrize("declaration", ["duplicate", "group name", "empty"])
@pytest.mark.parametrize("command", ["run", "down"])
def test_an_invalid_declaration_touches_no_stack_or_state(
    docker, command, declaration
):
    cases = list(lane.REQUIRED_CASES)
    if declaration == "duplicate":
        # The same case in a second group would run twice.
        cases.append(lane.Case(LAST, cases[0].module, cases[0].name))
    elif declaration == "group name":
        cases[0] = lane.Case("../outside", cases[0].module, cases[0].name)
    else:
        cases = []

    with pytest.raises(lane.LaneError):
        lane.execute(command, docker.state, cases=tuple(cases))

    assert docker.up == docker.down == []
    assert docker.stages == docker.builds == 0
    assert not docker.state.exists()


def test_list_prints_each_group_with_its_cases_in_run_order(
    monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "list"])

    assert lane.main() == 0

    lines = capsys.readouterr().out.splitlines()
    headings = [
        index
        for index, line in enumerate(lines)
        if line.split(" ", 1)[0] in NAMES
    ]
    assert [lines[index].split(" ", 1)[0] for index in headings] == NAMES
    for name, heading in zip(NAMES, headings, strict=True):
        members = declared(name)
        listed = lines[heading + 1 : heading + 1 + len(members)]
        assert [line.strip() for line in listed] == [
            case.selector for case in members
        ]
    selectors = [line.strip() for line in lines if "::" in line]
    assert len(selectors) == len(lane.REQUIRED_CASES)


def test_list_refuses_an_invalid_declaration(monkeypatch, capsys):
    twice = (lane.REQUIRED_CASES[0], lane.REQUIRED_CASES[0])
    monkeypatch.setattr(lane, "REQUIRED_CASES", twice)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "list"])

    assert lane.main() == 1

    assert "::" not in capsys.readouterr().out


def test_a_stack_limit_given_on_the_command_line_bounds_the_run(
    docker, monkeypatch
):
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "run", "--stacks", "2", "--state", str(docker.state)],
    )
    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        assert lane.main() == 0
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    assert docker.evidence()["stacks"] == 2
    assert docker.maximum_stacks == min(2, len(NAMES))
    docker.assert_clean()


def test_execute_refuses_a_stack_limit_below_one(docker):
    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state, stacks=0)

    assert docker.up == []
    assert docker.stages == docker.builds == 0
    assert not docker.state.exists()


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_a_stack_limit_below_one_is_refused_before_any_command(
    docker, monkeypatch, value
):
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "run", "--stacks", value, "--state", str(docker.state)],
    )
    with pytest.raises(SystemExit) as refused:
        lane.main()
    assert refused.value.code == 2
    assert docker.up == []
    assert docker.stages == 0


def test_group_children_do_not_inherit_archive_or_compose_settings(
    docker, monkeypatch
):
    inherited = {
        "MW_ADMIN_PASS": "fictional-archive-password",
        "YIZKR_INSTANCE_STATE": "fictional-archive-state",
        "COMPOSE_PROJECT_NAME": "fictional-archive-project",
        "COMPOSE_FILE": "fictional-archive-compose.yml",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)

    lane.execute("run", docker.state)

    assert docker.environments
    assert all(
        not inherited.keys() & env.keys() for env in docker.environments
    )
    assert all(p != inherited["COMPOSE_PROJECT_NAME"] for p in docker.up)
    docker.assert_clean()


@pytest.mark.parametrize("group", NAMES)
@pytest.mark.parametrize("failure", ["exit", "missing", "skipped", "wrong"])
def test_a_failed_or_invalid_group_stops_its_running_siblings(
    docker, failure, group
):
    for name in NAMES:
        if name != group:
            docker.returncodes[name] = None
    if failure == "exit":
        docker.returncodes[group] = 1
    elif failure == "missing":
        docker.reports[group] = None
    elif failure == "skipped":
        docker.reports[group] = [
            (*case.result, "skipped" if index == 0 else None)
            for index, case in enumerate(declared(group))
        ]
    else:
        other = LAST if group != LAST else FIRST
        docker.reports[group] = passing(declared(other))

    with pytest.raises((lane.LaneError, subprocess.CalledProcessError)):
        lane.execute("run", docker.state, stacks=len(NAMES))

    siblings = [p for p in docker.processes.values() if p.group != group]
    assert len(siblings) == len(NAMES) - 1
    assert all(p.name in docker.stopped for p in siblings)
    assert not any(p.completed for p in siblings)
    assert docker.evidence()["status"] == "failed"
    assert (docker.state / group / "tests.log").exists()
    docker.assert_clean()


def test_cleanup_kills_a_compose_child_that_ignores_termination(docker):
    docker.returncodes.update({FIRST: 1, LAST: None})
    docker.stubborn.add(LAST)
    with pytest.raises(lane.LaneError):
        lane.execute("run", docker.state, stacks=len(NAMES))

    sibling = next(p for p in docker.processes.values() if p.group == LAST)
    assert sibling.terminated and sibling.killed
    docker.assert_clean()


@pytest.mark.parametrize("interruption", ["keyboard", "term"])
def test_interruption_stops_children_and_cleans_every_project(
    docker, monkeypatch, interruption
):
    docker.interrupt = interruption
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "run", "--state", str(docker.state)]
    )
    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        assert lane.main() == 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    running = min(len(NAMES), lane.DEFAULT_STACKS)
    assert len(docker.processes) == running
    assert len(set(docker.stopped)) == running
    assert docker.evidence()["status"] == "failed"
    docker.assert_clean()


@pytest.mark.parametrize("setup_failure", ["stage", *NAMES])
@pytest.mark.parametrize("diagnostic_failure", ["open", "timeout"])
def test_failed_setup_cleans_every_project_even_when_diagnostics_fail(
    docker, monkeypatch, diagnostic_failure, setup_failure
):
    for folder in [docker.state, *(docker.state / n for n in NAMES)]:
        put(folder, "evidence.json", '{"status": "passed", "revision": "old"}')
        put(folder, "results.xml", "old successful result")
    docker.stage_failure = setup_failure == "stage"
    docker.setup_failure = setup_failure
    docker.log_failure = FIRST if diagnostic_failure == "timeout" else None
    original_open = Path.open

    def fail_log_open(path, *args, **kwargs):
        if path == docker.state / FIRST / "services.log":
            raise PermissionError("fictional log unavailable")
        return original_open(path, *args, **kwargs)

    if diagnostic_failure == "open":
        monkeypatch.setattr(Path, "open", fail_log_open)
    with pytest.raises((lane.LaneError, subprocess.CalledProcessError)):
        lane.execute("run", docker.state, stacks=len(NAMES))

    assert {docker.group(p) for p in docker.down} >= set(NAMES)
    assert docker.evidence()["status"] == "failed"
    assert docker.evidence()["revision"] == "fixture-revision"
    assert docker.evidence(FIRST)["diagnostics_error"]
    assert not (docker.state / "results.xml").exists()
    assert not any((docker.state / n / "results.xml").exists() for n in NAMES)
    docker.assert_clean()


@pytest.mark.parametrize("group", NAMES)
def test_guard_probe_failure_prevents_that_groups_checks_and_cleans_projects(
    docker, group
):
    docker.guard_probe_failure = group
    with pytest.raises(lane.LaneError, match="mutation guard"):
        lane.execute("run", docker.state, stacks=len(NAMES))
    assert docker.evidence()["status"] == "failed"
    assert group not in {p.group for p in docker.processes.values()}
    assert all(p.name in docker.stopped for p in docker.processes.values())
    assert not (docker.state / "results.xml").exists()
    docker.assert_clean()


@pytest.mark.parametrize("group", NAMES)
def test_one_group_cleanup_error_does_not_leave_its_siblings_running(
    docker, group
):
    docker.cleanup_failure = group
    with pytest.raises(lane.LaneError, match="teardown"):
        lane.execute("run", docker.state, stacks=len(NAMES))

    assert {docker.group(p) for p in docker.active} == {group}
    assert docker.evidence()["status"] == "failed"
    assert docker.evidence()["teardown"] == "failed"
    assert all(
        docker.evidence(n)["teardown"] == "passed" for n in NAMES if n != group
    )
    assert (docker.state / "instance.json").exists()

    failed = docker.evidence(group)
    assert failed["cleanup_error"]
    # A previous attempt can also have failed to reap its Compose child.
    failed["process_cleanup_error"] = "fictional previous reap failure"
    put(docker.state / group, "evidence.json", json.dumps(failed))
    docker.cleanup_failure = None
    lane.execute("down", docker.state)

    docker.assert_clean()
    assert docker.evidence()["status"] == "failed"
    assert docker.evidence()["revision"] == "fixture-revision"
    for name in NAMES:
        recovered = docker.evidence(name)
        assert recovered["teardown"] == "passed"
        assert "cleanup_error" not in recovered
        assert "process_cleanup_error" not in recovered


@pytest.mark.parametrize("group", NAMES)
@pytest.mark.parametrize("command", ["run", "down"])
def test_leftover_group_volume_fails_lane_and_keeps_recovery_marker(
    docker, command, group
):
    put(docker.state, "instance.json", '{"status": "interrupted"}')
    docker.leftovers[group, "volume"] = 1
    with pytest.raises(lane.LaneError, match="teardown|resources remain"):
        lane.execute(command, docker.state)

    assert (docker.state / "instance.json").exists()
    assert {docker.group(p) for p in docker.down} >= set(NAMES)
    if command == "run":
        assert docker.evidence()["status"] == "failed"
        assert docker.evidence()["teardown"] == "failed"


def test_down_recovers_every_group_and_legacy_without_staging_sources(
    docker, monkeypatch
):
    def invalid_source(*args):
        pytest.fail("cleanup tried to stage invalid application sources")

    monkeypatch.setattr(lane, "stage_context", invalid_source)
    lane.execute("down", docker.state)
    # A hard kill leaves projects and a marker but no live Python process.
    docker.active.update(docker.down)
    docker.down.clear()
    put(docker.state, "instance.json", '{"status": "interrupted"}')
    lane.execute("down", docker.state)
    assert {docker.group(p) for p in docker.down} == {*NAMES, "legacy"}
    assert docker.active == set()
    assert not (docker.state / "instance.json").exists()


def test_down_removes_projects_even_when_evidence_is_not_an_object(docker):
    lane.execute("down", docker.state)
    docker.active.update(docker.down)
    put(docker.state, "evidence.json", "[]")
    for index, name in enumerate(NAMES):
        put(docker.state / name, "evidence.json", ("null", "false")[index % 2])
    put(docker.state, "instance.json", '{"status": "interrupted"}')

    lane.execute("down", docker.state)

    docker.assert_clean()


def test_distinct_state_directories_control_disjoint_projects(
    docker, tmp_path
):
    def projects(state):
        start = len(docker.down)
        lane.execute("down", state)
        return set(docker.down[start:])

    first = projects(tmp_path / "first-lane")
    second = projects(tmp_path / "second-lane")
    again = projects(tmp_path / "first-lane")
    assert first == again
    assert first.isdisjoint(second)
    assert all(
        project.startswith("yizkr-fictional-") for project in first | second
    )
