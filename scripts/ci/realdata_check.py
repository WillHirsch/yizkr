"""Real-data check for pull requests, over salted token hashes.

Scans the text a pull request would add -- the added lines of its
unified diff, the paths of files it adds or renames, its commit
messages and any extra text such as the PR title and body -- and
reports every place where a run of words hashes to an entry of the
committed hash list (``.github/realdata/hashes.txt``).

The hash list holds ``sha256(salt + canonical token)`` for every entry
of the owner's out-of-repo denylist (names, places, organisations,
dates in each rendering). The salt is the ``REALDATA_SALT`` Actions
secret, so the list cannot be dictionary-attacked without it. Output
names the location and category only, never the matched text.

Canonical form: lowercase, NFKD, combining marks dropped, then the
``\\w+`` words joined by single spaces. A denylist entry of N words
therefore matches any N consecutive words of added text, whatever the
case, accents or punctuation between them ("1 2 3" matches "1.2.3").
Consecutive added lines are scanned as one word stream, so an entry
wrapped across two lines is still found.

Stdlib only: the pull_request_target workflow runs this file from the
base-branch checkout with the runner's python3, installs nothing and
never checks out or executes pull-request code.

Exit status: 0 clean, 1 hits found, 2 cannot check (no salt, salt
does not match the hash list, bad hash list, unreadable input). The
check fails closed.

Usage:
  REALDATA_SALT=... realdata_check.py [--hashes FILE]
      [--diff FILE|-]... [--commits-json FILE]... [--text LABEL=FILE]...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HASHES = (
    Path(__file__).resolve().parents[2] / ".github" / "realdata" / "hashes.txt"
)
SALT_ENV = "REALDATA_SALT"
# Hashed with the salt into the list's salt-check line, so a wrong or
# rotated secret fails closed instead of silently matching nothing.
# It contains non-word characters, so no canonical token can equal it.
SALT_CHECK_TEXT = "#salt-check"
MAX_NGRAM_KEY = "max-ngram"
SALT_CHECK_KEY = "salt-check"
_WORD = re.compile(r"\w+")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ENTRY = re.compile(r"^([a-z][a-z0-9_]*):([0-9a-f]{64})$")


class CheckError(Exception):
    """The check cannot run; callers fail closed."""


# --- canonical form and hashing -------------------------------------------


def norm(text: str) -> str:
    """Lowercase, NFKD, drop combining marks (as the local check does)."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _WORD.findall(norm(text))


def canonical(text: str) -> str:
    return " ".join(words(text))


def digest(salt: str, canon: str) -> str:
    return hashlib.sha256((salt + canon).encode("utf-8")).hexdigest()


# --- the hash list ----------------------------------------------------------


@dataclass(frozen=True)
class HashList:
    max_ngram: int
    salt_check: str
    table: dict[str, tuple[str, ...]]  # digest -> categories


def render_hash_list(salt: str, entries: Iterable[tuple[str, str]]) -> str:
    """Render the committed hash list for (category, token) entries.

    Deterministic: the same salt and entries give the same bytes.
    Entries whose canonical form is empty are skipped.
    """
    lines: set[str] = set()
    max_ngram = 0
    for category, token in entries:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", category):
            raise ValueError(f"bad category name: {category!r}")
        toks = words(token)
        if not toks:
            continue
        max_ngram = max(max_ngram, len(toks))
        lines.add(f"{category}:{digest(salt, ' '.join(toks))}")
    if not lines:
        raise ValueError("no entries")
    header = [
        "# Real-data hash list for scripts/ci/realdata_check.py.",
        "# Generated out of repo from the owner's private denylist;",
        "# do not edit by hand (see .github/realdata/README.md).",
        "# <category>:<sha256(REALDATA_SALT + canonical token)>",
        f"{MAX_NGRAM_KEY}: {max_ngram}",
        f"{SALT_CHECK_KEY}: {digest(salt, SALT_CHECK_TEXT)}",
    ]
    return "\n".join(header + sorted(lines)) + "\n"


def parse_hash_list(text: str) -> HashList:
    max_ngram = salt_check = None
    table: dict[str, tuple[str, ...]] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(": ")
        if key == MAX_NGRAM_KEY and value.isdigit() and int(value) > 0:
            max_ngram = int(value)
        elif key == SALT_CHECK_KEY and re.fullmatch(r"[0-9a-f]{64}", value):
            salt_check = value
        elif m := _ENTRY.match(line):
            cat, h = m.groups()
            if cat not in table.get(h, ()):
                table[h] = (*table.get(h, ()), cat)
        else:
            raise CheckError(f"hash list line {n} is malformed")
    if max_ngram is None:
        raise CheckError(f"hash list has no '{MAX_NGRAM_KEY}' line")
    if salt_check is None:
        raise CheckError(f"hash list has no '{SALT_CHECK_KEY}' line")
    if not table:
        raise CheckError("hash list has no entries")
    return HashList(max_ngram, salt_check, table)


def load_hash_list(path: Path) -> HashList:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CheckError(f"cannot read hash list {path}: {e}") from e
    return parse_hash_list(text)


# --- scanning ----------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Consecutive lines scanned as one word stream.

    ``label`` names the source; hits are reported as ``label:line``, or
    as the bare label when ``numbered`` is false (e.g. a file path).
    """

    label: str
    lines: Sequence[tuple[int, str]]
    numbered: bool = True


class Scanner:
    def __init__(self, hashes: HashList, salt: str):
        if not salt:
            raise CheckError(f"{SALT_ENV} is empty")
        if digest(salt, SALT_CHECK_TEXT) != hashes.salt_check:
            raise CheckError(
                f"{SALT_ENV} does not match the hash list's salt-check "
                "(secret missing, wrong or rotated without regenerating)"
            )
        self._salted = hashlib.sha256(salt.encode("utf-8"))
        self._max = hashes.max_ngram
        self._table = hashes.table

    def scan(self, run: Run) -> list[tuple[str, str]]:
        """(location, category) for each hit, in order, de-duplicated."""
        toks = [(n, w) for n, text in run.lines for w in words(text)]
        hits: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for i, (line_no, word) in enumerate(toks):
            loc = f"{run.label}:{line_no}" if run.numbered else run.label
            # Extend one salted hash word by word: n-grams of 1..max.
            h = self._salted.copy()
            h.update(word.encode("utf-8"))
            for j in range(i, min(i + self._max, len(toks))):
                if j > i:
                    h.update((" " + toks[j][1]).encode("utf-8"))
                for cat in self._table.get(h.hexdigest(), ()):
                    if (loc, cat) not in seen:
                        seen.add((loc, cat))
                        hits.append((loc, cat))
        return hits


# --- inputs --------------------------------------------------------------


def _unquote_path(path: str) -> str:
    """Undo git's C-style quoting of unusual paths ("a\\303\\251")."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        try:
            raw = path[1:-1].encode("ascii").decode("unicode_escape")
            return raw.encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError):
            return path[1:-1]
    return path


def _strip_prefix(path: str) -> str:
    path = _unquote_path(path.strip())
    return path[2:] if path[:2] in ("a/", "b/") else path


def diff_runs(diff: str) -> Iterator[Run]:
    """Added text of a unified diff (``git diff`` / GitHub .diff format).

    Yields each block of consecutive added lines, numbered by their
    line in the new file, and the path of every added, renamed or
    copied file (reported by its position in the diff, not by name,
    since the name itself is what matched).
    """
    file_no = 0
    path = "?"
    git_path: str | None = None  # from "diff --git a/P b/P"
    is_new = path_checked = False
    old_left = new_left = new_line = 0
    block: list[tuple[int, str]] = []

    def flush() -> Iterator[Run]:
        if block:
            yield Run(path, list(block))
            block.clear()

    def path_run(p: str) -> Run:
        nonlocal path_checked
        path_checked = True
        # Underscores join words for \w; paths use them as separators.
        return Run(
            f"new path of diff file #{file_no}",
            [(0, p.replace("_", " "))],
            numbered=False,
        )

    def unchecked_new_path() -> Iterator[Run]:
        # A new empty or binary file has no "+++" line.
        if is_new and not path_checked and git_path:
            yield path_run(git_path)

    for line in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            tag = line[:1]
            if tag == "+":
                block.append((new_line, line[1:]))
                new_line += 1
                new_left -= 1
                continue
            if tag == "\\":  # "\ No newline at end of file"
                continue
            yield from flush()
            if tag == "-":
                old_left -= 1
                continue
            if tag == " " or line == "":
                old_left -= 1
                new_left -= 1
                new_line += 1
                continue
            old_left = new_left = 0  # malformed hunk: resync on headers
        yield from flush()
        if line.startswith("diff --git "):
            yield from unchecked_new_path()
            file_no += 1
            path, is_new, path_checked = "?", False, False
            pair = line[len("diff --git ") :].rstrip("\r")
            half = (len(pair) - 1) // 2  # "a/P b/P": both halves equal
            old, new = pair[:half], pair[half + 1 :]
            same = _strip_prefix(old) == _strip_prefix(new)
            git_path = _strip_prefix(new) if same else None
        elif line.startswith(("new file mode", "--- /dev/null")):
            is_new = True
        elif line.startswith(("rename to ", "copy to ")):
            yield path_run(_unquote_path(line.split(" ", 2)[2]))
        elif line.startswith("+++ "):
            target = line[4:].rstrip("\r")
            if target != "/dev/null":
                path = _strip_prefix(target)
                if is_new:
                    yield path_run(path)
        elif m := _HUNK.match(line):
            old_left = int(m.group(1) or 1)
            new_line = int(m.group(2))
            new_left = int(m.group(3) or 1)
    yield from flush()
    yield from unchecked_new_path()


def _json_values(text: str) -> Iterator[object]:
    """Parse one JSON value, or several concatenated (gh --paginate)."""
    dec = json.JSONDecoder()
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return
        value, i = dec.raw_decode(text, i)
        yield value


def _flatten(value: object) -> Iterator[dict]:
    if isinstance(value, list):
        for v in value:
            yield from _flatten(v)
    elif isinstance(value, dict):
        yield value


def commit_runs(text: str) -> Iterator[Run]:
    """Commit messages from the pulls/{n}/commits API (any paging)."""
    try:
        commits = [c for v in _json_values(text) for c in _flatten(v)]
    except json.JSONDecodeError as e:
        raise CheckError(f"commits JSON is malformed: {e}") from e
    for c in commits:
        msg = (c.get("commit") or {}).get("message")
        if not isinstance(msg, str):
            raise CheckError("commits JSON entry has no commit.message")
        sha = str(c.get("sha", "?"))[:7]
        yield text_run(f"commit {sha} message", msg)


def text_run(label: str, text: str) -> Run:
    return Run(label, list(enumerate(text.split("\n"), 1)))


def _read(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise CheckError(f"cannot read {path}: {e}") from e


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fail on added text that matches the salted "
        "real-data hash list. Reports location and category only."
    )
    ap.add_argument("--hashes", type=Path, default=DEFAULT_HASHES)
    ap.add_argument("--diff", action="append", default=[], metavar="FILE")
    ap.add_argument(
        "--commits-json", action="append", default=[], metavar="FILE"
    )
    ap.add_argument("--text", action="append", default=[], metavar="L=FILE")
    args = ap.parse_args(argv)

    try:
        if not (args.diff or args.commits_json or args.text):
            raise CheckError("nothing to check (give --diff/--text/...)")
        salt = os.environ.get(SALT_ENV, "").strip()
        if not salt:
            raise CheckError(f"{SALT_ENV} is not set")
        scanner = Scanner(load_hash_list(args.hashes), salt)
        runs: list[Run] = []
        for f in args.diff:
            runs.extend(diff_runs(_read(f)))
        for f in args.commits_json:
            runs.extend(commit_runs(_read(f)))
        for spec in args.text:
            label, sep, f = spec.partition("=")
            if not sep or not label or not f:
                raise CheckError(f"--text wants LABEL=FILE, got {spec!r}")
            runs.append(text_run(label, _read(f)))
    except CheckError as e:
        print(
            f"realdata-check: cannot check, failing closed: {e}. "
            "See .github/realdata/README.md.",
            file=sys.stderr,
        )
        return 2

    hits = [hit for run in runs for hit in scanner.scan(run)]
    for loc, cat in hits:
        print(f"{loc}: {cat}")
    lines = sum(len(r.lines) for r in runs)
    if not hits:
        print(f"realdata-check: clean ({lines} line(s) scanned)")
        return 0
    by_cat = dict(sorted(Counter(cat for _, cat in hits).items()))
    locations = len({loc for loc, _ in hits})
    print(
        f"realdata-check: {len(hits)} hit(s) at {locations} location(s) "
        f"in {lines} scanned line(s); by category: {by_cat}. Replace the "
        "real data with fictional examples; do not paste the matched "
        "text anywhere."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
