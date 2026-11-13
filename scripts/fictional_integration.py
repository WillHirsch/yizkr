#!/usr/bin/env python3
"""Run required writer/query checks on an isolated, disposable fictional stack.

No archive configuration or existing deployment is loaded. Only allowlisted
repository files enter the image; runtime containers have an internal network,
no published ports and project-specific volumes. Every run tears down its own
stack, including on failure. `down` recovers resources after a hard kill.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager, suppress
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIRED_CASES = (
    (
        "tests.fictional_integration.test_cohort_additions",
        "test_protected_cohort_choices_claims_and_creation_recovery",
    ),
    (
        "tests.fictional_integration.test_assignment_authority",
        "test_reviewed_assignments_are_atomic_recoverable_and_graph_preserving",
    ),
    (
        "tests.fictional_integration.test_assignment_writes",
        "test_reviewed_writes_keep_exact_shared_support_and_assignment_roles",
    ),
    (
        "tests.fictional_integration.test_assignment_writes",
        "test_partial_writes_recover_and_ambiguous_support_refuses",
    ),
    (
        "tests.fictional_integration.test_assignment_writes",
        "test_competing_reviewed_plans_dispatch_one_historical_write",
    ),
    (
        "tests.fictional_integration.test_container_sources",
        "test_common_sources_preserve_owners_placements_and_citations",
    ),
    (
        "tests.fictional_integration.test_slide_attribution",
        "test_slide_readings_preserve_attribution_through_write_and_query",
    ),
    (
        "tests.fictional_integration.test_identity_migration",
        "test_verified_migration_preserves_evidence_and_publishes_graph_origins",
    ),
    (
        "tests.fictional_integration.test_owner_override_provenance",
        "test_verified_owner_override_preserves_evidence_and_has_graph_provenance",
    ),
    (
        "tests.fictional_integration.test_run_provenance",
        "test_structured_run_records_roundtrip_without_rewriting",
    ),
    (
        "tests.fictional_integration.test_write_query",
        "test_fictional_write_query_roundtrip",
    ),
    (
        "tests.fictional_integration.test_write_recovery",
        "test_interrupted_writer_recovers_after_curator_term_changes",
    ),
    (
        "tests.fictional_integration.test_write_recovery",
        "test_interrupted_source_registration_recovers_without_orphan",
    ),
    (
        "tests.fictional_integration.test_write_recovery",
        "test_concurrent_creation_reserves_one_dispatch",
    ),
    (
        "tests.fictional_integration.test_graph_projection",
        "test_recorded_graph_projection_replays_and_refuses_stale_capture",
    ),
)
GROUPS = {
    "assignments": tuple(
        case
        for case in REQUIRED_CASES
        if case[0].endswith(".test_assignment_writes")
    ),
    "other": tuple(
        case
        for case in REQUIRED_CASES
        if not case[0].endswith(".test_assignment_writes")
    ),
}
SOURCES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "src",
    "tests/__init__.py",
    "tests/fixtures",
    "tests/fictional_integration",
)
CONTROLS = (
    "scripts/fictional_integration.py",
    "deploy/fictional-integration/compose.yml",
    "deploy/fictional-integration/Dockerfile",
    "deploy/fictional-integration/mutation-guard-probe.php",
    "deploy/config/Uploads.php",
    "deploy/config/MutationGuard.php",
    "deploy/config/mutation-guard",
)

# Run once in an ephemeral test-image container. This exercises production
# admission against the image's actual Git state, including a tracked edit.
CODE_ADMISSION_PROBE = """
import json
from pathlib import Path
import yizkr
from yizkr.core.instance import require_clean_code

before = require_clean_code()
source = Path(yizkr.__file__)
original = source.read_bytes()
try:
    source.write_bytes(original + b'\\n# fictional executable change probe\\n')
    try:
        require_clean_code()
    except ValueError as error:
        if 'verified clean software' not in str(error):
            raise
    else:
        raise RuntimeError('retained admission accepted an executable edit')
finally:
    source.write_bytes(original)
after = require_clean_code()
if after != before:
    raise RuntimeError('source snapshot changed during admission probe')
print(json.dumps({
    'runtime_snapshot_kind': 'fictional-source-snapshot',
    'runtime_snapshot_commit': before['commit'],
    'dirty_code_refused': True,
}))
"""


class LaneError(Exception):
    """The required lane could not establish its boundary checks."""


def kill_process(process) -> None:
    """Reap a CLI and its plugin subprocesses before removing its resources."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run(args, *, env=None, capture=False, check=True, cwd=REPO, timeout=None):
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        kill_process(process)
        raise
    if check and process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, args, output=output
        )
    return subprocess.CompletedProcess(args, process.returncode, output)


def docker_environment(image: str) -> dict[str, str]:
    """Keep transport/registry trust, never Compose or archive selectors."""
    allowed = {
        "PATH",
        "HOME",
        "DOCKER_CONFIG",
        "XDG_RUNTIME_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    return {
        **{key: value for key, value in os.environ.items() if key in allowed},
        "YIZKR_FICTIONAL_IMAGE": image,
    }


def selected_files(repo: Path) -> list[Path]:
    """Use Git's allowlist, including new source files while developing."""
    names = run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *SOURCES,
            *CONTROLS,
        ],
        cwd=repo,
        capture=True,
    ).stdout.split("\0")
    paths = sorted({Path(name) for name in names if name})
    for relative in paths:
        path = repo / relative
        if not path.is_file() or path.resolve() != path.absolute():
            raise LaneError(
                f"source must be a regular, non-symlink file: {relative}"
            )
    return paths


def stage_context(repo: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    for relative in selected_files(repo):
        content = (repo / relative).read_bytes()
        digest.update(str(relative).encode() + b"\0" + content + b"\0")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    shutil.copyfile(
        destination / "deploy/config/Uploads.php",
        destination / "deploy/fictional-integration/Uploads.php",
    )
    return digest.hexdigest()


def runtime_code_evidence(output: str) -> dict:
    """Validate fixture-image evidence without relabeling the host revision."""
    try:
        value = json.loads(output)
    except ValueError as exc:
        raise LaneError(
            "runtime code admission probe returned invalid JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "runtime_snapshot_kind",
            "runtime_snapshot_commit",
            "dirty_code_refused",
        }
        or value["runtime_snapshot_kind"] != "fictional-source-snapshot"
        or not isinstance(value["runtime_snapshot_commit"], str)
        or not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", value["runtime_snapshot_commit"]
        )
        or value["dirty_code_refused"] is not True
    ):
        raise LaneError(
            "runtime code admission probe did not establish evidence"
        )
    return value


def verify_results(path: Path, required=None) -> dict:
    """A zero pytest exit alone does not prove the intended checks ran."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise LaneError(
            f"missing or invalid integration results: {exc}"
        ) from exc
    cases = root.findall(".//testcase")
    if any(
        case.find(tag) is not None
        for case in cases
        for tag in ("skipped", "failure", "error")
    ):
        raise LaneError("required integration checks failed or skipped")
    observed = [(case.get("classname"), case.get("name")) for case in cases]
    required = REQUIRED_CASES if required is None else required
    for case in required:
        if observed.count(case) != 1:
            raise LaneError(
                f"required integration case {case[1]} did not run exactly once"
            )
    if len(observed) != len(required):
        raise LaneError("unexpected integration case ran")
    return {"passed": len(cases), "skipped": 0, "cases": observed}


@contextmanager
def locked(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lane.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaneError(
                "this checkout's fictional lane is already running"
            ) from exc
        yield


def stopped(signum, frame):
    raise KeyboardInterrupt(f"received signal {signum}")


def remaining_resources(docker, env, project: str) -> dict[str, int]:
    """Check every project resource, including one-off test volumes."""
    return {
        kind: len(
            run(
                [
                    *docker,
                    kind,
                    "ls",
                    "--quiet",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    *(["--all"] if kind == "container" else []),
                ],
                env=env,
                capture=True,
                timeout=30,
            ).stdout.split()
        )
        for kind in ("container", "volume", "network")
    }


def combine_results(paths: list[Path], destination: Path) -> dict:
    """Retain pytest's suites and require the complete, exact case union."""
    combined = ET.Element("testsuites")
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise LaneError(
                f"missing or invalid integration results: {exc}"
            ) from exc
        combined.extend([root] if root.tag == "testsuite" else list(root))
    ET.ElementTree(combined).write(destination, encoding="unicode")
    return verify_results(destination)


class Group:
    def __init__(self, name, cases, project, state, compose):
        self.name, self.cases, self.project = name, cases, project
        self.state, self.compose = state, compose
        self.process = None
        self.started = None
        self.finished = False
        self.cleaned = False
        self.evidence = {
            "project": project,
            "status": "failed",
            "teardown": "pending",
        }


def start_checks(group: Group, env: dict) -> None:
    selectors = [
        module.replace(".", "/") + ".py::" + name
        for module, name in group.cases
    ]
    with (group.state / "tests.log").open("w") as log:
        group.process = subprocess.Popen(
            [
                *group.compose,
                "run",
                "--name",
                f"{group.project}-checks",
                "--no-deps",
                "tests",
                "python",
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-m",
                "live",
                "-v",
                "-x",
                "--durations=20",
                "--junitxml=/state/results.xml",
                *selectors,
            ],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )


def copy_results(group: Group, docker, env, *, check=True):
    return run(
        [
            *docker,
            "cp",
            f"{group.project}-checks:/state/results.xml",
            str(group.state / "results.xml"),
        ],
        env=env,
        check=check,
        timeout=30,
    )


def wait_checks(groups: list[Group], docker, env) -> None:
    while any(not group.finished for group in groups):
        for group in groups:
            if group.finished:
                continue
            code = group.process.poll()
            if code is None:
                continue
            group.process.wait(timeout=10)
            group.finished = True
            group.evidence["pytest_exit_code"] = code
            print(f"Fictional integration group: {group.name}", flush=True)
            print((group.state / "tests.log").read_text(), end="", flush=True)
            if code:
                raise LaneError(
                    f"required integration pytest ({group.name}) exited {code}"
                )
            copy_results(group, docker, env)
            group.evidence["checks"] = verify_results(
                group.state / "results.xml", group.cases
            )
            ids = run(
                [*group.compose, "ps", "--all", "--quiet"],
                env=env,
                capture=True,
                timeout=30,
            ).stdout.split()
            group.evidence["images"] = run(
                [
                    *docker,
                    "inspect",
                    "--format",
                    "{{.Config.Image}} {{.Image}}",
                    *ids,
                ],
                env=env,
                capture=True,
                timeout=30,
            ).stdout.splitlines()
            group.evidence["status"] = "passed"
        if any(not group.finished for group in groups):
            time.sleep(0.1)


def cleanup_groups(
    groups: list[Group], docker, env, *, diagnostics=True
) -> None:
    pending = [group for group in groups if not group.cleaned]
    for group in pending:
        for key in ("cleanup_error", "process_cleanup_error"):
            group.evidence.pop(key, None)
    # Reap every CLI before any final down: a live Compose plugin could
    # otherwise create a container after its project's teardown.
    for group in pending:
        process = group.process
        if process is None:
            continue
        try:
            live = process.poll() is None
            if live:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                # Terminating the CLI alone leaves its container alive.
                run(
                    [
                        *docker,
                        "stop",
                        "--time",
                        "10",
                        f"{group.project}-checks",
                    ],
                    env=env,
                    check=False,
                    timeout=30,
                )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_process(process)
            if live:
                # The first stop can race an in-flight container creation.
                run(
                    [
                        *docker,
                        "stop",
                        "--time",
                        "10",
                        f"{group.project}-checks",
                    ],
                    env=env,
                    check=False,
                    timeout=30,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            group.evidence["process_cleanup_error"] = str(exc)
            try:
                kill_process(process)
            except (OSError, subprocess.SubprocessError) as reap_error:
                group.evidence["process_cleanup_error"] += f"; {reap_error}"
    for group in pending:
        if diagnostics and group.cases:
            try:
                if (
                    group.process is not None
                    and not (group.state / "results.xml").exists()
                ):
                    copy_results(group, docker, env, check=False)
                output = run(
                    [*group.compose, "logs", "--no-color", "--tail", "200"],
                    env=env,
                    capture=True,
                    timeout=30,
                ).stdout
                (group.state / "services.log").write_text(output)
            except (OSError, subprocess.SubprocessError) as exc:
                group.evidence["diagnostics_error"] = str(exc)
        try:
            cleanup = run(
                [
                    *group.compose,
                    "--profile",
                    "test",
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "10",
                ],
                env=env,
                check=False,
                timeout=90,
            )
            if cleanup.returncode:
                group.evidence["cleanup_error"] = (
                    f"compose down exited {cleanup.returncode}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            group.evidence["cleanup_error"] = str(exc)
        try:
            remaining = remaining_resources(docker, env, group.project)
        except (OSError, subprocess.SubprocessError) as exc:
            remaining = {"unknown": 1}
            group.evidence["cleanup_error"] = str(exc)
        group.evidence["remaining_resources"] = remaining
        clean = not any(remaining.values()) and not any(
            key in group.evidence
            for key in ("cleanup_error", "process_cleanup_error")
        )
        group.evidence["teardown"] = "passed" if clean else "failed"
        if not clean:
            group.evidence["status"] = "failed"
        if group.started is not None:
            group.evidence["elapsed_seconds"] = round(
                time.monotonic() - group.started, 3
            )
        group.cleaned = True


def write_evidence(path: Path, evidence: dict) -> None:
    path.write_text(json.dumps(evidence, indent=2) + "\n")


def execute(command: str, state: Path, *, sequential=False) -> None:
    key = hashlib.sha256(f"{REPO}\0{state}".encode()).hexdigest()[:16]
    project = f"yizkr-fictional-{key}"
    image = f"{project}:test"
    env = docker_environment(image)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    started = time.monotonic()
    with (
        locked(state),
        tempfile.TemporaryDirectory(prefix=f"{project}-") as tmp,
    ):
        groups = []
        # Unsuffixed project is cleanup-only recovery for older launchers.
        for name, cases in [*GROUPS.items(), ("legacy", ())]:
            name_project = project if name == "legacy" else f"{project}-{name}"
            folder = state if name == "legacy" else state / name
            folder.mkdir(parents=True, exist_ok=True)
            groups.append(
                Group(
                    name,
                    cases,
                    name_project,
                    folder,
                    [
                        *docker,
                        "compose",
                        "--project-name",
                        name_project,
                        "--env-file",
                        "/dev/null",
                        "--file",
                        str(REPO / "deploy/fictional-integration/compose.yml"),
                    ],
                )
            )
        active = groups[:-1]
        evidence = {
            "project": project,
            "command": "make test-integration",
            "status": "failed",
            "teardown": "pending",
            "mode": "sequential" if sequential else "concurrent",
            "groups": {group.name: group.evidence for group in active},
        }
        # Cleanup-only preserves the verification record of an earlier run.
        if command == "down":
            for group in active:
                with suppress(OSError, ValueError):
                    old = json.loads(
                        (group.state / "evidence.json").read_text()
                    )
                    if isinstance(old, dict):
                        group.evidence.update(old)
            try:
                old = json.loads((state / "evidence.json").read_text())
                if isinstance(old, dict):
                    old.pop("groups", None)
                    evidence.update(old)
            except (OSError, ValueError):
                pass
        failure = None
        try:
            if command == "run":
                for folder in [state, *(group.state for group in active)]:
                    for name in (
                        "results.xml",
                        "evidence.json",
                        "tests.log",
                        "services.log",
                    ):
                        (folder / name).unlink(missing_ok=True)
                write_evidence(state / "instance.json", evidence)
                write_evidence(state / "evidence.json", evidence)
                for group in active:
                    write_evidence(
                        group.state / "evidence.json", group.evidence
                    )
                revision = run(
                    ["git", "rev-parse", "HEAD"], capture=True
                ).stdout.strip()
                dirty = bool(
                    run(["git", "status", "--porcelain"], capture=True).stdout
                )
                shared = {"revision": revision, "dirty": dirty}
                evidence.update(shared)
                for group in active:
                    group.evidence.update(shared)
                    write_evidence(
                        group.state / "evidence.json", group.evidence
                    )
                write_evidence(state / "evidence.json", evidence)
                partition = [case for group in active for case in group.cases]
                if (
                    set(GROUPS) != {"assignments", "other"}
                    or not all(group.cases for group in active)
                    or len(partition) != len(REQUIRED_CASES)
                    or len(set(partition)) != len(partition)
                    or set(partition) != set(REQUIRED_CASES)
                ):
                    raise LaneError(
                        "invalid required integration group partition"
                    )
                context = Path(tmp)
                source_hash = stage_context(REPO, context)
                evidence["source_sha256"] = source_hash
                for group in active:
                    group.evidence["source_sha256"] = source_hash
                    group.compose[-1] = str(
                        context / "deploy/fictional-integration/compose.yml"
                    )
                info = run(
                    [*docker, "info", "--format", "{{json .}}"],
                    env=env,
                    capture=True,
                )
                host = json.loads(info.stdout)
                evidence["docker_host"] = {
                    key: host[key]
                    for key in ("ServerVersion", "NCPU", "MemTotal")
                }
                cleanup_groups(groups, docker, env, diagnostics=False)
                if any(
                    group.evidence["teardown"] != "passed" for group in groups
                ):
                    raise LaneError(
                        "fictional project resources remain after teardown"
                    )
                build = [
                    *docker,
                    "build",
                    "--tag",
                    image,
                    "--file",
                    str(context / "deploy/fictional-integration/Dockerfile"),
                ]
                ca_bundle = os.environ.get("CODEX_PROXY_CERT")
                if ca_bundle:
                    build += ["--secret", f"id=proxy_ca,src={ca_bundle}"]
                run([*build, str(context)], env=env)
                probe_group = active[0]
                probe_group.cleaned = False
                code_probe = run(
                    [
                        *probe_group.compose,
                        "run",
                        "--rm",
                        "--no-deps",
                        "--name",
                        f"{probe_group.project}-code-admission",
                        "tests",
                        "python",
                        "-c",
                        CODE_ADMISSION_PROBE,
                    ],
                    env=env,
                    capture=True,
                    timeout=120,
                )
                runtime_code = runtime_code_evidence(code_probe.stdout)
                evidence.update(runtime_code)
                for group in active:
                    group.evidence.update(runtime_code)
                for group in active:
                    group.cleaned = False
                    group.started = time.monotonic()
                    group.evidence["teardown"] = "pending"
                    run(
                        [
                            *group.compose,
                            "up",
                            "--detach",
                            "--wait",
                            "--wait-timeout",
                            "600",
                        ],
                        env=env,
                    )
                    probe = run(
                        [
                            *group.compose,
                            "exec",
                            "-T",
                            "wikibase",
                            "php",
                            "/yizkr-fictional-mutation-guard-probe.php",
                        ],
                        env=env,
                        capture=True,
                        timeout=90,
                    )
                    guard_evidence = json.loads(probe.stdout)
                    if guard_evidence != {
                        "protocol": "yizkr-mutation-guard-v1",
                        "request_drain": True,
                        "shutdown_drain": True,
                        "restart": True,
                        "stale_capability": True,
                        "invalid_state": True,
                        "database_drain": True,
                    }:
                        raise LaneError(
                            "mutation guard subprocess probe failed"
                        )
                    group.evidence["mutation_guard_probe"] = guard_evidence
                    if sequential:
                        start_checks(group, env)
                        wait_checks([group], docker, env)
                        cleanup_groups([group], docker, env)
                        if group.evidence["teardown"] != "passed":
                            raise LaneError(
                                "teardown failed; run make integration-down"
                            )
                if not sequential:
                    for group in active:
                        start_checks(group, env)
                    wait_checks(active, docker, env)
                evidence["checks"] = combine_results(
                    [group.state / "results.xml" for group in active],
                    state / "results.xml",
                )
                evidence["status"] = "passed"
        except BaseException as exc:
            failure = exc
            evidence["status"] = "failed"
            evidence["error"] = str(exc)
        finally:
            # Once cancellation reaches cleanup, finish every project even
            # if another signal or diagnostic/teardown failure arrives.
            previous = {
                sig: signal.signal(sig, signal.SIG_IGN)
                for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                cleanup_groups(groups, docker, env)
                remaining = {}
                for group in groups:
                    for kind, count in group.evidence[
                        "remaining_resources"
                    ].items():
                        remaining[kind] = remaining.get(kind, 0) + count
                evidence["remaining_resources"] = remaining
                clean = all(
                    group.evidence["teardown"] == "passed" for group in groups
                )
                evidence["teardown"] = "passed" if clean else "failed"
                if not clean:
                    evidence["status"] = "failed"
                    failure = failure or LaneError(
                        "teardown failed; run make integration-down"
                    )
                else:
                    (state / "instance.json").unlink(missing_ok=True)
                if command == "run":
                    evidence["elapsed_seconds"] = round(
                        time.monotonic() - started, 3
                    )
                # All resource cleanup precedes evidence writes that can fail.
                for group in active:
                    try:
                        write_evidence(
                            group.state / "evidence.json", group.evidence
                        )
                    except OSError as exc:
                        evidence["status"] = "failed"
                        evidence["evidence_error"] = str(exc)
                        failure = failure or exc
                write_evidence(state / "evidence.json", evidence)
                print(
                    "Fictional integration evidence: "
                    f"{state / 'evidence.json'}",
                    flush=True,
                )
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        if failure is not None:
            raise failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "down"))
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="run and tear down each fixture group before starting the next",
    )
    key = hashlib.sha256(str(REPO).encode()).hexdigest()[:16]
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(tempfile.gettempdir()) / f"yizkr-fictional-{key}",
    )
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stopped)
    try:
        execute(args.command, args.state.resolve(), sequential=args.sequential)
    except (
        LaneError,
        OSError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        print(f"Required fictional integration failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
