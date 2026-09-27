"""The pre-push hook refuses direct pushes to main (ADR-0024).

Offline: the hook is fed git's stdin protocol directly, then exercised by
real pushes into a throwaway bare repository.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[2] / "scripts" / "git-hooks" / "pre-push"
)
Z40 = "0" * 40
A = "a" * 40
B = "b" * 40


def _line(local_ref, local_sha, remote_ref, remote_sha):
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def run_hook(stdin):
    return subprocess.run(
        ["sh", str(HOOK), "origin", "https://example.invalid/repo.git"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_is_marked_for_make_hooks():
    # `make hooks` overwrites only a hook carrying this marker.
    assert "\n# yizkr-pre-push:" in HOOK.read_text()


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        _line("refs/heads/20-slug", A, "refs/heads/20-slug", Z40),
        _line("refs/heads/20-slug", A, "refs/heads/20-slug", B),
        _line("refs/heads/feature/main", A, "refs/heads/feature/main", B),
        _line("refs/heads/x", A, "refs/heads/main-notes", Z40),
        _line("refs/tags/v1", A, "refs/tags/main", Z40),
        _line("(delete)", Z40, "refs/heads/20-slug", B),
    ],
    ids=[
        "nothing",
        "new-branch",
        "update-branch",
        "branch-ending-main",
        "branch-starting-main",
        "tag-named-main",
        "delete-branch",
    ],
)
def test_allows_pushes_that_leave_main_alone(stdin):
    result = run_hook(stdin)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    "stdin",
    [
        _line("refs/heads/main", A, "refs/heads/main", B),
        _line("refs/heads/20-slug", A, "refs/heads/main", B),
        _line("refs/heads/main", A, "refs/heads/main", Z40),
        _line("(delete)", Z40, "refs/heads/main", B),
        _line("refs/heads/20-slug", A, "refs/heads/20-slug", B)
        + _line("refs/heads/main", A, "refs/heads/main", B),
    ],
    ids=[
        "update-main",
        "branch-to-main",
        "create-main",
        "delete-main",
        "main-second-of-two",
    ],
)
def test_refuses_any_update_of_main(stdin):
    result = run_hook(stdin)
    assert result.returncode == 1
    assert "refusing to update main" in result.stderr
    assert "gh pr create --draft" in result.stderr


@pytest.fixture
def clone(tmp_path):
    """A clone of a bare repo with the hook installed, isolated from any
    user or system git config (and so from any core.hooksPath)."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Example Agent",
        "GIT_AUTHOR_EMAIL": "agent@example.invalid",
        "GIT_COMMITTER_NAME": "Example Agent",
        "GIT_COMMITTER_EMAIL": "agent@example.invalid",
    }

    def git(*args, cwd, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("init", "-b", "main", str(work), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=work)
    (work / "README.md").write_text("Springfield\n")
    git("add", "README.md", cwd=work)
    git("commit", "-m", "seed", cwd=work)
    # Seed the remote's main before the hook exists, as GitHub already has.
    git("push", "origin", "main", cwd=work)
    hooks = git("rev-parse", "--git-path", "hooks", cwd=work).stdout.strip()
    dest = work / hooks  # the same lookup `make hooks` uses
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(HOOK, dest / "pre-push")
    (dest / "pre-push").chmod(0o755)
    git("switch", "-c", "20-slug", cwd=work)
    (work / "README.md").write_text("Springfield\nLakeside\n")
    git("commit", "-am", "change", cwd=work)
    return work, remote, git


def test_real_push_to_branch_passes(clone):
    work, remote, git = clone
    result = git("push", "origin", "20-slug", cwd=work, check=False)
    assert result.returncode == 0, result.stderr
    assert git("rev-parse", "20-slug", cwd=remote).stdout.strip()


@pytest.mark.parametrize(
    "refspecs",
    [
        ["20-slug:main"],
        ["HEAD:main"],
        ["20-slug", "20-slug:main"],
        [":main"],
    ],
    ids=["branch-to-main", "head-to-main", "two-refs", "delete-main"],
)
def test_real_push_to_main_is_refused(clone, refspecs):
    work, remote, git = clone
    before = git("rev-parse", "main", cwd=remote).stdout
    result = git("push", "origin", *refspecs, cwd=work, check=False)
    assert result.returncode != 0
    assert "refusing to update main" in result.stderr
    assert git("rev-parse", "main", cwd=remote).stdout == before
