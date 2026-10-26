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
import shutil
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIRED_CASE = (
    "tests.fictional_integration.test_write_query",
    "test_fictional_write_query_roundtrip",
)
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
    "deploy/config/Uploads.php",
)


class LaneError(Exception):
    """The required lane could not establish its boundary checks."""


def run(args, *, env=None, capture=False, check=True, cwd=REPO):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=check,
    )


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


def verify_results(path: Path) -> dict:
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
    if observed.count(REQUIRED_CASE) != 1:
        raise LaneError(
            "required writer/query roundtrip did not run exactly once"
        )
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
            ).stdout.split()
        )
        for kind in ("container", "volume", "network")
    }


def execute(command: str, state: Path) -> None:
    key = hashlib.sha256(f"{REPO}\0{state}".encode()).hexdigest()[:16]
    project = f"yizkr-fictional-{key}"
    image = f"{project}:test"
    env = docker_environment(image)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    # Keep Compose's project directory and env-file away from any host .env.
    with (
        locked(state),
        tempfile.TemporaryDirectory(prefix=f"{project}-") as tmp,
    ):
        context = Path(tmp)
        compose = [
            *docker,
            "compose",
            "--project-name",
            project,
            "--env-file",
            "/dev/null",
            "--file",
            str(REPO / "deploy/fictional-integration/compose.yml"),
        ]
        down = [
            *compose,
            "--profile",
            "test",
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
        ]
        if command == "down":
            run(down, env=env)
            if any(remaining_resources(docker, env, project).values()):
                raise LaneError(
                    "fictional project resources remain after teardown"
                )
            (state / "instance.json").unlink(missing_ok=True)
            return
        revision = run(
            ["git", "rev-parse", "HEAD"], capture=True
        ).stdout.strip()
        dirty = bool(
            run(["git", "status", "--porcelain"], capture=True).stdout
        )
        evidence = {
            "revision": revision,
            "dirty": dirty,
            "project": project,
            "command": "make test-integration",
            "status": "failed",
            "teardown": "pending",
        }
        (state / "results.xml").unlink(missing_ok=True)
        (state / "instance.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        # Invalidate an earlier passing record before any setup can fail.
        (state / "evidence.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
        try:
            evidence["source_sha256"] = stage_context(REPO, context)
            compose[-1] = str(
                context / "deploy/fictional-integration/compose.yml"
            )
            down = [
                *compose,
                "--profile",
                "test",
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "10",
            ]
            run([*docker, "info", "--format", "{{.ServerVersion}}"], env=env)
            # A previous interrupted run can never supply a passing fixture.
            run(down, env=env)
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
            run(
                [
                    *compose,
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "600",
                ],
                env=env,
            )
            result = run(
                [
                    *compose,
                    "run",
                    "--name",
                    f"{project}-checks",
                    "--no-deps",
                    "tests",
                ],
                env=env,
                check=False,
            )
            run(
                [
                    *docker,
                    "cp",
                    f"{project}-checks:/state/results.xml",
                    str(state / "results.xml"),
                ],
                env=env,
            )
            if result.returncode:
                raise LaneError(
                    f"required integration pytest exited {result.returncode}"
                )
            evidence["checks"] = verify_results(state / "results.xml")
            ids = run(
                [*compose, "ps", "--all", "--quiet"], env=env, capture=True
            ).stdout.split()
            evidence["images"] = run(
                [
                    *docker,
                    "inspect",
                    "--format",
                    "{{.Config.Image}} {{.Image}}",
                    *ids,
                ],
                env=env,
                capture=True,
            ).stdout.splitlines()
            evidence["status"] = "passed"
        finally:
            previous = {
                sig: signal.signal(sig, signal.SIG_IGN)
                for sig in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                # A log failure must never prevent resource cleanup.
                try:
                    with (state / "services.log").open("w") as log:
                        subprocess.run(
                            [*compose, "logs", "--no-color", "--tail", "200"],
                            env=env,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=30,
                        )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    evidence["diagnostics_error"] = str(exc)
                cleanup = run(down, env=env, check=False)
                remaining = remaining_resources(docker, env, project)
                evidence["remaining_resources"] = remaining
                evidence["teardown"] = (
                    "passed"
                    if cleanup.returncode == 0 and not any(remaining.values())
                    else "failed"
                )
                if evidence["teardown"] != "passed":
                    evidence["status"] = "failed"
                else:
                    (state / "instance.json").unlink(missing_ok=True)
            finally:
                if evidence["teardown"] != "passed":
                    evidence["status"] = "failed"
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                (state / "evidence.json").write_text(
                    json.dumps(evidence, indent=2) + "\n"
                )
                print(
                    "Fictional integration evidence: "
                    f"{state / 'evidence.json'}",
                    flush=True,
                )
            if evidence["teardown"] != "passed":
                raise LaneError("teardown failed; run make integration-down")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "down"))
    key = hashlib.sha256(str(REPO).encode()).hexdigest()[:16]
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(tempfile.gettempdir()) / f"yizkr-fictional-{key}",
    )
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stopped)
    try:
        execute(args.command, args.state.resolve())
    except (
        LaneError,
        OSError,
        subprocess.CalledProcessError,
        KeyboardInterrupt,
    ) as exc:
        print(f"Required fictional integration failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
