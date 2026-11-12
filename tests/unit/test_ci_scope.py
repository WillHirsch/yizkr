"""CI may omit executable suites only for a complete, verified prose diff."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts.ci_scope import classify, is_prose, main, prose_diff


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE
    ).strip()


def commit(repo: Path) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Fictional change")
    return git(repo, "rev-parse", "HEAD")


def write(repo: Path, name: str, text: str = "Fictional documentation\n"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Fictional contributor")
    git(tmp_path, "config", "user.email", "fictional@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    write(tmp_path, "README.md")
    write(tmp_path, "docs/guide.md")
    write(tmp_path, "src/program.py", "VALUE = 1\n")
    commit(tmp_path)
    return tmp_path


def push_scope(repo: Path, before: str) -> bool:
    after = git(repo, "rev-parse", "HEAD")
    return classify("push", {"before": before, "after": after}, after, repo)[0]


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/new-guide.md",
        "docs/adr/current.md",
        "docs/runbooks/operation.md",
        "src/yizkr/pipeline/README.md",
        "src/yizkr/queries/README.md",
        "docs/guide with\na newline.md",
    ],
)
def test_regular_prose_changes_can_use_the_light_route(repo, path):
    before = git(repo, "rev-parse", "HEAD")
    write(repo, path, "Updated fictional documentation\n")
    commit(repo)
    assert push_scope(repo, before)


@pytest.mark.parametrize(
    "path",
    [
        "src/yizkr/pipeline/prompts/transcription.md",
        "src/yizkr/queries/query.rq",
        "src/yizkr/model/manifest.yaml",
        "tests/fixtures/source.md",
        "tests/unit/test_example.py",
        "scripts/tool.py",
        ".github/workflows/ci.yml",
        ".pre-commit-config.yaml",
        "pyproject.toml",
        "uv.lock",
        "CLAUDE.md",
        "docs/unknown-subdirectory/input.md",
        "docs/example.py",
    ],
)
def test_behavior_resources_and_unknown_paths_are_not_prose(path):
    assert not is_prose(path)


def test_a_mixed_push_includes_code_from_earlier_commits(repo):
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "src/program.py", "VALUE = 2\n")
    middle = commit(repo)
    write(repo, "docs/guide.md", "New prose\n")
    commit(repo)
    assert push_scope(repo, middle)
    assert not push_scope(repo, before)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("docs/guide.md", "docs/renamed.md", True),
        ("src/program.py", "docs/renamed.md", False),
        ("docs/guide.md", "src/yizkr/pipeline/prompts/renamed.md", False),
    ],
)
def test_renames_consider_the_removed_and_added_paths(
    repo, old, new, expected
):
    before = git(repo, "rev-parse", "HEAD")
    target = repo / new
    target.parent.mkdir(parents=True, exist_ok=True)
    (repo / old).rename(target)
    commit(repo)
    assert push_scope(repo, before) is expected


@pytest.mark.parametrize("path", ["docs/guide.md", "src/program.py"])
def test_deletions_still_classify_the_removed_path(repo, path):
    before = git(repo, "rev-parse", "HEAD")
    (repo / path).unlink()
    commit(repo)
    assert push_scope(repo, before) is (path == "docs/guide.md")


@pytest.mark.parametrize("mode", ["executable", "symlink"])
def test_markdown_file_mode_changes_require_full_verification(repo, mode):
    before = git(repo, "rev-parse", "HEAD")
    path = repo / "docs/guide.md"
    if mode == "executable":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("../src/program.py")
    commit(repo)
    assert not push_scope(repo, before)
    before = git(repo, "rev-parse", "HEAD")
    if mode == "executable":
        path.chmod(0o644)
    else:
        path.unlink()
        path.write_text("Ordinary guide again\n")
    commit(repo)
    assert not push_scope(repo, before)


def test_prose_does_not_exempt_a_markdown_model_prompt(repo):
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "docs/guide.md", "Updated guide\n")
    write(repo, "src/yizkr/pipeline/prompts/new.md")
    commit(repo)
    assert not push_scope(repo, before)


def test_pr_scope_uses_the_tested_merge_not_unrelated_base_changes(repo):
    git(repo, "checkout", "-qb", "topic")
    write(repo, "docs/guide.md", "Topic prose\n")
    head = commit(repo)
    git(repo, "checkout", "-q", "main")
    write(repo, "src/program.py", "VALUE = 2\n")
    base = commit(repo)
    git(repo, "merge", "--no-ff", "-qm", "Fictional merge", "topic")
    merged = git(repo, "rev-parse", "HEAD")
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    assert classify("pull_request", event, merged, repo)[0]
    assert not classify("pull_request", event, head, repo)[0]
    event["pull_request"]["head"]["sha"] = base
    assert not classify("pull_request", event, merged, repo)[0]


@pytest.mark.parametrize("before", ["0" * 40, "f" * 40, "HEAD^", None])
def test_missing_or_invalid_push_history_requires_full_verification(
    repo, before
):
    after = git(repo, "rev-parse", "HEAD")
    assert not classify(
        "push", {"before": before, "after": after}, after, repo
    )[0]


def test_empty_manual_unknown_and_malformed_events_require_full_verification(
    repo,
):
    head = git(repo, "rev-parse", "HEAD")
    assert not push_scope(repo, head)
    for event_name in (
        "workflow_dispatch",
        "merge_group",
        "push",
        "pull_request",
    ):
        assert not classify(event_name, {}, head, repo)[0]
        assert not classify(event_name, None, head, repo)[0]
    assert not classify(
        "push", {"before": head, "after": head}, "f" * 40, repo
    )[0]
    assert not classify(
        "push", {"before": head, "after": "f" * 40}, head, repo
    )[0]


@pytest.mark.parametrize("raw", [b"", b"M\0docs/guide.md\0", b"bad", b"bad\0"])
def test_incomplete_or_unrecognized_diff_records_cannot_approve_a_skip(raw):
    assert not prose_diff(raw)


def test_event_entrypoint_emits_only_a_boolean_output(
    repo, monkeypatch, capsys
):
    before = git(repo, "rev-parse", "HEAD")
    write(repo, "docs/new guide.md")
    after = commit(repo)
    event = repo / "event.json"
    event.write_text(json.dumps({"before": before, "after": after}))
    output = repo / "output.txt"
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_SHA", after)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    main()
    assert output.read_text() == "prose_only=true\n"
    assert "CI scope: prose" in capsys.readouterr().out
    event.write_text("broken JSON")
    main()
    assert output.read_text().endswith("prose_only=false\n")
    event.unlink()
    main()
    assert output.read_text().splitlines() == [
        "prose_only=true",
        "prose_only=false",
        "prose_only=false",
    ]


def test_an_unwritable_output_is_a_failure_not_a_silent_skip(
    repo, monkeypatch
):
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(repo / "absent.json"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(repo))
    with pytest.raises(OSError):
        main()
