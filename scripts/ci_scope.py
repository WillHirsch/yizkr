"""Select full CI unless the tested Git diff contains only known prose.

Run with the ordinary GitHub Actions event environment after checkout.
The classifier uses local Git objects, not a truncated API file listing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

PROSE_FILES = frozenset(
    {
        "README.md",
        "src/yizkr/pipeline/README.md",
        "src/yizkr/queries/README.md",
    }
)
PROSE_DIRS = frozenset({"docs", "docs/adr", "docs/runbooks"})
REGULAR_MODES = {b"000000", b"100644"}


def is_prose(path: str) -> bool:
    """Only ordinary guide locations are exempt, never a general *.md rule."""
    parsed = PurePosixPath(path)
    return path in PROSE_FILES or (
        str(parsed.parent) in PROSE_DIRS and parsed.suffix == ".md"
    )


def prose_diff(raw: bytes) -> bool:
    """Read raw changes, keeping paths and modes on both sides."""
    if not raw or not raw.endswith(b"\0"):
        return False
    parts = raw[:-1].split(b"\0")
    if len(parts) % 2:
        return False
    for header, path in zip(parts[::2], parts[1::2], strict=True):
        fields = header.split()
        if (
            len(fields) != 5
            or not fields[0].startswith(b":")
            or fields[0][1:] not in REGULAR_MODES
            or fields[1] not in REGULAR_MODES
            or any(not re.fullmatch(rb"[0-9a-f]{40}", x) for x in fields[2:4])
            or fields[4] not in {b"A", b"M", b"D"}
            or not path
            or not is_prose(os.fsdecode(path))
        ):
            return False
    return True


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("missing commit identity")
    if value == "0" * 40:
        raise ValueError("no previous commit")
    return value


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout


def classify(
    event_name: str, event: dict[str, Any], sha: str, repo: Path
) -> tuple[bool, str]:
    """Return a prose decision or a conservative full-suite reason."""
    if event_name not in {"pull_request", "push"}:
        return False, "manual or unsupported event"
    try:
        tested = _sha(sha)
        if _git(repo, "rev-parse", "HEAD").decode().strip() != tested:
            return False, "checkout does not match the event"
        if event_name == "pull_request":
            pr = event["pull_request"]
            base, head = _sha(pr["base"]["sha"]), _sha(pr["head"]["sha"])
            parents = (
                _git(repo, "rev-list", "--parents", "-n", "1", tested)
                .decode()
                .split()
            )
            if parents != [tested, base, head]:
                return False, "checkout is not the expected PR merge"
        else:
            base = _sha(event["before"])
            if _sha(event["after"]) != tested:
                return False, "checkout does not match the push"
        raw = _git(
            repo,
            "diff",
            "--raw",
            "-z",
            "--no-renames",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-textconv",
            base,
            tested,
            "--",
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.CalledProcessError,
    ):
        return False, "comparison unavailable or malformed"
    if prose_diff(raw):
        return True, "only ordinary documentation changed"
    return False, "non-prose, file-mode, empty or unrecognized change"


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        prose_only, reason = classify(
            os.environ.get("GITHUB_EVENT_NAME", ""),
            event,
            os.environ.get("GITHUB_SHA", ""),
            Path.cwd(),
        )
    except (KeyError, ValueError, OSError):
        prose_only, reason = False, "event data unavailable or malformed"
    value = str(prose_only).lower()
    print(f"CI scope: {'prose' if prose_only else 'full'} ({reason})")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"prose_only={value}\n")


if __name__ == "__main__":
    main()
