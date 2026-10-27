"""Offline guards for the disposable fictional integration runner."""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
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


def results(path: Path, cases) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for class_name, name, outcome in cases:
        case = ET.SubElement(
            suite, "testcase", classname=class_name, name=name
        )
        if outcome:
            ET.SubElement(case, outcome)
    ET.ElementTree(root).write(path)
    return path


def test_required_results_accept_completed_boundary_case(tmp_path):
    path = results(
        tmp_path / "results.xml",
        [(*case, None) for case in lane.REQUIRED_CASES],
    )
    assert lane.verify_results(path) == {
        "passed": len(lane.REQUIRED_CASES),
        "skipped": 0,
        "cases": list(lane.REQUIRED_CASES),
    }


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_required_results_refuse_unexecuted_or_failed_check(tmp_path, outcome):
    # In particular, pytest can exit zero with a skipped required case.
    path = results(
        tmp_path / "results.xml", [(*lane.REQUIRED_CASES[0], outcome)]
    )
    with pytest.raises(lane.LaneError, match="failed or skipped"):
        lane.verify_results(path)


@pytest.mark.parametrize("cases", [[], [("another.module", "other", None)]])
def test_required_results_refuse_missing_boundary_case(tmp_path, cases):
    path = results(tmp_path / "results.xml", cases)
    with pytest.raises(lane.LaneError, match="did not run exactly once"):
        lane.verify_results(path)


def test_required_results_refuse_duplicate_boundary_case(tmp_path):
    case = (*lane.REQUIRED_CASES[0], None)
    path = results(tmp_path / "results.xml", [case, case])
    with pytest.raises(lane.LaneError, match="did not run exactly once"):
        lane.verify_results(path)


@pytest.mark.parametrize("omitted", range(len(lane.REQUIRED_CASES)))
def test_required_results_refuse_any_omitted_boundary_case(tmp_path, omitted):
    path = results(
        tmp_path / "results.xml",
        [
            (*case, None)
            for index, case in enumerate(lane.REQUIRED_CASES)
            if index != omitted
        ],
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


def test_down_recovers_without_staging_invalid_application_sources(
    monkeypatch, tmp_path
):
    state = tmp_path / "lane"
    put(state, "instance.json", '{"status": "interrupted"}')
    calls = []

    def invalid_source(*args):
        pytest.fail("cleanup tried to stage invalid application sources")

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(lane, "stage_context", invalid_source)
    monkeypatch.setattr(lane, "run", fake_run)
    lane.execute("down", state)
    cleanup = [args for args in calls if "compose" in args and "down" in args]
    assert len(cleanup) == 1
    assert_cleanup_covers_all_volumes(cleanup[0])
    assert not (state / "instance.json").exists()


def test_distinct_state_directories_control_distinct_projects(
    monkeypatch, tmp_path
):
    projects = []

    def fake_run(args, **kwargs):
        if "down" in args:
            projects.append(args[args.index("--project-name") + 1])
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(lane, "run", fake_run)
    lane.execute("down", tmp_path / "first-lane")
    lane.execute("down", tmp_path / "second-lane")
    lane.execute("down", tmp_path / "first-lane")
    assert projects[0] == projects[2]
    assert projects[0] != projects[1]
    assert all(project.startswith("yizkr-fictional-") for project in projects)


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
        if profiles and not profiles.intersection(active_profiles):
            continue
        covered.update(
            mount.split(":")[0] for mount in service.get("volumes", [])
        )
    assert "test-state" in spec["volumes"]
    assert set(spec["volumes"]) <= covered
    assert "--volumes" in args


@pytest.mark.parametrize("setup_failure", ["stage", "up"])
@pytest.mark.parametrize("diagnostic_failure", ["open", "timeout"])
def test_failed_setup_cleans_up_even_when_diagnostics_fail(
    monkeypatch, tmp_path, diagnostic_failure, setup_failure
):
    state = tmp_path / "lane"
    put(state, "evidence.json", '{"status": "passed", "revision": "old"}')
    put(state, "results.xml", "old successful result")
    calls = []

    def failed_stage(*args):
        # A failure here must not leave yesterday's successful evidence.
        evidence = json.loads((state / "evidence.json").read_text())
        assert evidence["status"] == "failed"
        assert evidence["revision"] == "fixture-revision"
        if setup_failure == "stage":
            raise lane.LaneError("fictional setup unavailable")
        return "fixture-source-hash"

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            output = "fixture-revision\n"
        else:
            output = ""
        if "compose" in args and "up" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=output)

    original_open = Path.open

    def fail_log_open(path, *args, **kwargs):
        if path == state / "services.log":
            raise PermissionError("fictional log unavailable")
        return original_open(path, *args, **kwargs)

    def timed_out_capture(args, **kwargs):
        assert "logs" in args
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(lane, "run", fake_run)
    monkeypatch.setattr(lane, "stage_context", failed_stage)
    monkeypatch.setattr(lane.subprocess, "run", timed_out_capture)
    if diagnostic_failure == "open":
        monkeypatch.setattr(Path, "open", fail_log_open)
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "run", "--state", str(state)]
    )
    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        assert lane.main() == 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    cleanup = [args for args in calls if "compose" in args and "down" in args]
    assert len(cleanup) == (1 if setup_failure == "stage" else 2)
    for args in cleanup:
        assert_cleanup_covers_all_volumes(args)
    assert not (state / "instance.json").exists()
    assert not (state / "results.xml").exists()
    evidence = json.loads((state / "evidence.json").read_text())
    assert evidence["status"] == "failed"
    assert evidence["teardown"] == "passed"
    assert evidence["revision"] == "fixture-revision"
    assert evidence["diagnostics_error"]


@pytest.mark.parametrize("command", ["run", "down"])
def test_leftover_test_volume_fails_lane_and_keeps_recovery_marker(
    monkeypatch, tmp_path, command
):
    state = tmp_path / "lane"
    put(state, "instance.json", '{"status": "interrupted"}')

    def fake_run(args, **kwargs):
        output = ""
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            output = "fixture-revision\n"
        elif "cp" in args:
            results(
                Path(args[-1]), [(*case, None) for case in lane.REQUIRED_CASES]
            )
        elif "volume" in args and "ls" in args:
            assert any(
                arg.startswith("label=com.docker.compose.project=")
                for arg in args
            )
            output = "fictional-project_test-state\n"
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(lane, "run", fake_run)
    monkeypatch.setattr(lane, "stage_context", lambda *args: "fixture-hash")
    monkeypatch.setattr(
        lane.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0),
    )
    with pytest.raises(lane.LaneError, match="teardown"):
        lane.execute(command, state)
    assert (state / "instance.json").exists()
    if command == "run":
        evidence = json.loads((state / "evidence.json").read_text())
        assert evidence["checks"]["passed"] == len(lane.REQUIRED_CASES)
        assert evidence["status"] == "failed"
        assert evidence["teardown"] == "failed"
        assert evidence["remaining_resources"]["volume"] == 1
