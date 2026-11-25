"""Design citations and relative links in the tracked tree resolve.

docs/TESTING.md, "Design citations and links", states the convention: a
rule is cited by the path of the document that states it and the quoted
heading, or in a Markdown document by a relative link to the heading; an
ADR that has a file in docs/adr is cited by its number, with a section
number only where the file has that numbered heading; a number whose
file was removed is not cited.

The check reads every tracked file that is text, as `git ls-files` lists
them, and never the rest of the checkout. It leaves out a file deleted
from the working tree, a file holding a NUL byte or invalid UTF-8, this
module and its baseline. A citation wrapped across comment, docstring or
YAML lines, or split between adjacent string literals, is joined into one
line before it is read.

Four kinds of finding are counted for each file against the baseline:

- removed-number: an ADR-NNNN whose number has no file in docs/adr;
- section-key: a section key after the number of an ADR that has a file,
  matching no numbered heading of that file;
- stand-alone-sign: a section sign in no key list that follows an ADR
  number or AGENT_PROTOCOL, outside docs/AGENT_PROTOCOL.md and docs/adr,
  which number their own sections, and outside EXEMPT_SIGNS;
- section-name: the path or name of a tracked Markdown document followed
  by a quoted name that begins none of its headings or bold paragraph
  labels.

A key list is "§3", "§3, §7", "§2 and §6", "§8.5-8.7", "§8.5 to §8.7",
"§A" or "§10.1(c)", and may hold "Q" keys. A section sign in other words,
as in "§5 before §8", stands alone.

The baseline, doc_references_baseline.json, records for each file the
number of findings of each kind it held when the check was added. The
test fails when a file holds more findings of a kind than its entry and
when an entry is higher than what the file holds, so a change that
removes a citation lowers its entry in the same change, and the baseline
only shrinks. The change that removes the last entry deletes the
baseline, the code that reads it and the sentences of docs/TESTING.md
that describe it.

Two checks need no baseline. Every relative link in a tracked Markdown
document resolves to a tracked file or folder, and its fragment to a
heading or explicit anchor where the target is a Markdown document;
prompt templates under src/yizkr/pipeline/prompts/ are not documents.
Every section key after AGENT_PROTOCOL matches a numbered heading of
docs/AGENT_PROTOCOL.md.
"""

from __future__ import annotations

import bisect
import json
import posixpath
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("doc_references_baseline.json")
OWN = frozenset(
    path.resolve().relative_to(ROOT).as_posix()
    for path in (Path(__file__), BASELINE)
)
ADRS = "docs/adr/"
PROTOCOL = "docs/AGENT_PROTOCOL.md"
PROMPTS = "src/yizkr/pipeline/prompts/"
KINDS = ("removed-number", "section-key", "stand-alone-sign", "section-name")

# Section signs that cite a specification outside the repository, each as
# its file and the text that ends with the sign.
EXEMPT_SIGNS = frozenset(
    {
        # the NAME TYPE values of the GEDCOM 5.5.1 specification
        ("src/yizkr/pipeline/gedcom.py", "GEDCOM NAME TYPE (5.5.1 §"),
    }
)

# -- reading the tree --------------------------------------------------------


@dataclass(frozen=True)
class Outline:
    """What a citation or a link can name in a Markdown document."""

    # each heading as rendered, also without the number or letter that
    # opens it, and each bold label that opens a paragraph or list item
    names: tuple[str, ...]
    anchors: frozenset[str]  # heading slugs and explicit anchors
    keys: frozenset[str]  # numbers and letters that open a heading


class Tree:
    """Tracked paths, with the text of each that is readable text."""

    def __init__(self, texts: Mapping[str, str], paths: Iterable[str]):
        self.texts = dict(texts)
        self.paths = frozenset(paths)

    @cached_property
    def folders(self) -> frozenset[str]:
        found = set()
        for path in self.paths:
            parts = path.split("/")[:-1]
            found.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))
        return frozenset(found)

    @cached_property
    def records(self) -> dict[str, str]:
        """The path of each ADR file in docs/adr, by its number."""
        return {
            match.group(1): path
            for path in self.paths
            if (match := re.fullmatch(r"docs/adr/(\d{4})-[^/]*\.md", path))
        }

    @cached_property
    def documents(self) -> frozenset[str]:
        """The tracked Markdown documents: every *.md file but prompts."""
        return frozenset(
            path
            for path in self.texts
            if path.endswith(".md") and not path.startswith(PROMPTS)
        )

    @cached_property
    def outlines(self) -> dict[str, Outline]:
        return {path: outline(self.texts[path]) for path in self.documents}


def tracked(root: Path) -> Tree:
    """The tracked files of the checkout at `root`, as `git ls-files`
    lists them; only files that are text have their text read."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    paths, texts = set(), {}
    for path in filter(None, listed.split("\0")):
        file = root / path
        if file.is_symlink() or not file.is_file():
            continue
        paths.add(path)
        data = file.read_bytes()
        if b"\0" in data:
            continue
        try:
            texts[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return Tree(texts, paths)


# -- Markdown ----------------------------------------------------------------

FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
LABEL = re.compile(r"[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?\*\*([^*\n]+)\*\*")
ANCHOR = re.compile(r"<a\s[^>]*?\b(?:id|name)=[\"']([^\"']+)[\"']")
HEADING_KEY = re.compile(r"(\d+(?:\.\d+)*)\.?\s|([A-Z])\.\s")


def prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """The numbered lines of a Markdown text outside fenced code."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        mark = match.group(1) if (match := FENCE.match(line)) else None
        if mark and fence is None:
            fence = mark
        elif mark and mark[0] == fence[0] and len(mark) >= len(fence):
            fence = None
        elif fence is None:
            yield number, line


def rendered(markdown: str) -> str:
    """Inline Markdown as its text: link text, code without backticks,
    no emphasis markers or HTML tags."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("`", "")
    text = re.sub(r"\*+", "", text)
    return text.strip()


def slug(heading: str) -> str:
    """GitHub's anchor for a heading of that rendered text."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def outline(text: str) -> Outline:
    names, anchors, keys = [], set(), set()
    used = Counter()
    for _, line in prose_lines(text):
        anchors.update(ANCHOR.findall(line))
        if heading := HEADING.fullmatch(line):
            title = rendered(heading.group(1))
            names.append(title)
            base = slug(title)
            anchors.add(f"{base}-{used[base]}" if used[base] else base)
            used[base] += 1
            if key := HEADING_KEY.match(title):
                keys.add(key.group(1) or key.group(2))
                names.append(title[key.end() :])
        elif label := LABEL.match(line):
            names.append(rendered(label.group(1)))
    return Outline(tuple(names), frozenset(anchors), frozenset(keys))


def begins(name: str, outline: Outline) -> bool:
    """Whether a heading or bold label of `outline` begins with `name`,
    up to the end of a word."""
    wanted = " ".join(name.split()).casefold()
    for candidate in outline.names:
        text = candidate.casefold()
        if text.startswith(wanted) and not text[len(wanted) :][:1].isalnum():
            return True
    return False


# -- joining wrapped lines ---------------------------------------------------

# A line break, with the indentation and comment marker of the next line,
# or the end of a string literal and the start of the next.
BREAK = re.compile(
    r"([\"'])[ \t]*\n[ \t]*[rRbBfFuU]{0,2}[\"']"
    r"|[ \t]*\n[ \t]*(?:(?:#+|//+|\*+|--)(?=[ \t\n])[ \t]*)?"
)


@dataclass(frozen=True)
class Joined:
    """A text with its line breaks joined, and where each line starts."""

    text: str
    starts: tuple[int, ...]

    def line(self, at: int) -> int:
        """The number of the original line holding offset `at`."""
        return bisect.bisect_right(self.starts, at)


def joined(text: str) -> Joined:
    parts, starts, length, last = [], [0], 0, 0
    for match in BREAK.finditer(text):
        if match.group(1):
            filler = ""
        elif text.startswith("\n", match.end()):
            filler = "\n"  # a blank line ends the paragraph
        else:
            filler = " "
        parts += [text[last : match.start()], filler]
        length += match.start() - last + len(filler)
        starts.append(length)
        last = match.end()
    parts.append(text[last:])
    return Joined("".join(parts), tuple(starts))


# -- citations ---------------------------------------------------------------

NUMBER = r"\d+(?:\.\d+)*"
POINTS = r"(?:\([a-z]\))*"
KEY = rf"(?:{NUMBER}|[A-Z](?!\w))"
UNTIL = r"(?:[ \t]?[-–][ \t]?|[ \t]+to[ \t]+)§?[ \t]?"
ITEM = rf"(?:§§?[ \t]?{KEY}{POINTS}(?:{UNTIL}{NUMBER}{POINTS})?|Q\d+)"
SEPARATOR = r"(?:,[ \t]*(?:and[ \t]+|or[ \t]+)?|[ \t]+(?:and|or)[ \t]+)"
CITATION = re.compile(
    r"\b(?:ADR-(?P<adr>\d{4})|(?P<protocol>AGENT_PROTOCOL)(?:\.md)?)\b"
    r"(?:`?(?:\]\([^()\s]*\))?(?:'s)?,?[ \t]*\(?[ \t]*"
    rf"(?P<keys>{ITEM}(?:{SEPARATOR}{ITEM})*))?"
)
CITED_KEY = re.compile(rf"§§?[ \t]?({KEY}){POINTS}(?:{UNTIL}({NUMBER}))?")
SIGN = re.compile("§+")
SECTION_NAME = re.compile(
    r"(?<![\w/.\-])(?P<document>(?:[\w.\-]+/)*[\w\-]+\.md)`?,?[ \t]+"
    r"(?:\"(?P<double>[^\"\n]{1,120})\""
    r"|“(?P<curly>[^”\n]{1,120})”"
    r"|'(?P<single>[^'\n]{1,120})')"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def cited_keys(keys: str) -> Iterator[str]:
    """Each section key of a key list, and the end of each range."""
    for match in CITED_KEY.finditer(keys):
        yield from filter(None, match.groups())


def document_named(tree: Tree, citing: str, name: str) -> str | None:
    """The tracked document `name` means in `citing`: beside the citing
    file, else from the repository root, else the one document whose
    path ends in it."""
    for candidate in (
        posixpath.normpath(posixpath.join(posixpath.dirname(citing), name)),
        posixpath.normpath(name),
    ):
        if candidate in tree.documents:
            return candidate
    ending = [d for d in tree.documents if d.endswith(f"/{name}")]
    return ending[0] if len(ending) == 1 else None


def exempt(path: str, text: str, at: int) -> bool:
    return any(
        path == exempt_path and text.endswith(prefix, 0, at + 1)
        for exempt_path, prefix in EXEMPT_SIGNS
    )


def citation_findings(tree: Tree, path: str) -> Iterator[Finding]:
    """The findings of the four baseline kinds in one file, and of
    "protocol-key", an AGENT_PROTOCOL key with no numbered heading."""
    raw = tree.texts[path]
    if "ADR-" not in raw and "§" not in raw and ".md" not in raw:
        return
    text = joined(raw)
    attributed = []
    protocol = tree.outlines.get(PROTOCOL)
    for match in CITATION.finditer(text.text):
        line = text.line(match.start())
        number, keys = match.group("adr"), match.group("keys")
        if keys:
            attributed.append(match.span("keys"))
        if number is not None and number not in tree.records:
            yield Finding(
                path, line, "removed-number", f"ADR-{number} has no file"
            )
            continue
        if not keys:
            continue
        if number is not None:
            known = tree.outlines[tree.records[number]].keys
            kind, cited = "section-key", f"ADR-{number}"
        else:
            known = protocol.keys if protocol else frozenset()
            kind, cited = "protocol-key", PROTOCOL
        for key in cited_keys(keys):
            if key not in known:
                yield Finding(
                    path, line, kind, f"{cited} has no numbered heading {key}"
                )
    if path != PROTOCOL and not path.startswith(ADRS):
        starts = [start for start, _ in attributed]
        for sign in SIGN.finditer(text.text):
            at = sign.start()
            index = bisect.bisect_right(starts, at) - 1
            if index >= 0 and at < attributed[index][1]:
                continue
            if exempt(path, text.text, at):
                continue
            yield Finding(
                path,
                text.line(at),
                "stand-alone-sign",
                "a section sign that names no document",
            )
    for match in SECTION_NAME.finditer(text.text):
        name = match.group("double") or match.group("curly")
        name = name or match.group("single")
        document = document_named(tree, path, match.group("document"))
        line = text.line(match.start())
        if document is None:
            yield Finding(
                path,
                line,
                "section-name",
                f"{match.group('document')} is no tracked document",
            )
        elif not begins(name, tree.outlines[document]):
            yield Finding(
                path,
                line,
                "section-name",
                f'{document} has no heading or label "{name}"',
            )


# -- links -------------------------------------------------------------------

CODE_SPAN = re.compile(r"(`+).+?\1")
LINK = re.compile(
    r"!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]"
    r"\([ \t]*(?:<([^>\n]*)>|([^()\s]+))(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'))?"
    r"[ \t]*\)"
)
DEFINITION = re.compile(r" {0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]*)>|(\S+))")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def link_findings(tree: Tree, path: str) -> Iterator[Finding]:
    """The relative links of one Markdown document that resolve to no
    tracked file or folder, or to no heading or anchor of a document."""
    for number, line in prose_lines(tree.texts[path]):
        line = CODE_SPAN.sub(lambda m: " " * len(m.group()), line)
        targets = [m.group(1) or m.group(2) for m in LINK.finditer(line)]
        if definition := DEFINITION.match(line):
            targets.append(definition.group(1) or definition.group(2))
        for target in targets:
            if SCHEME.match(target) or target.startswith("//"):
                continue
            problem = unresolved(tree, path, target)
            if problem:
                yield Finding(path, number, "link", problem)


def unresolved(tree: Tree, path: str, target: str) -> str | None:
    """Why a relative link from `path` to `target` does not resolve."""
    location, _, fragment = target.partition("#")
    location, fragment = unquote(location), unquote(fragment)
    resolved = path
    if location:
        resolved = posixpath.join(posixpath.dirname(path), location)
        resolved = posixpath.normpath(resolved)
    if resolved not in tree.paths and resolved not in tree.folders:
        return f"{target}: {resolved} is not tracked"
    if (
        fragment
        and resolved in tree.documents
        and fragment not in tree.outlines[resolved].anchors
    ):
        return f"{target}: {resolved} has no anchor #{fragment}"
    return None


# -- the baseline ------------------------------------------------------------


def findings(tree: Tree) -> list[Finding]:
    """Every finding of the tree, links included."""
    found = []
    for path in sorted(tree.texts):
        if path in OWN:
            continue
        found += citation_findings(tree, path)
        if path in tree.documents:
            found += link_findings(tree, path)
    return found


def counts(found: list[Finding]) -> dict[str, dict[str, int]]:
    """The number of findings of each baseline kind, by file."""
    tally: dict[str, dict[str, int]] = {}
    for finding in found:
        if finding.kind in KINDS:
            held = tally.setdefault(finding.path, {})
            held[finding.kind] = held.get(finding.kind, 0) + 1
    return tally


def drift(
    held: Mapping[str, Mapping[str, int]],
    baseline: Mapping[str, Mapping[str, int]],
) -> list[tuple[str, str, int, int]]:
    """Each (file, kind, findings held, findings recorded) where the tree
    and the baseline differ."""
    differences = []
    for path in sorted(held.keys() | baseline.keys()):
        now, recorded = held.get(path, {}), baseline.get(path, {})
        for kind in sorted(now.keys() | recorded.keys()):
            if now.get(kind, 0) != recorded.get(kind, 0):
                differences.append(
                    (path, kind, now.get(kind, 0), recorded.get(kind, 0))
                )
    return differences


def explained(
    differences: list[tuple[str, str, int, int]], found: list[Finding]
) -> str:
    lines = []
    for path, kind, now, recorded in differences:
        if now < recorded:
            fix = f"record {now}" if now else "remove the kind"
            lines.append(
                f"{path}: {now} {kind} findings, below its entry of "
                f"{recorded}: {fix}"
            )
        else:
            lines.append(
                f"{path}: {now} {kind} findings, above its entry of "
                f"{recorded}: rewrite the new one"
            )
            lines += [
                f"    {f}" for f in found if f.path == path and f.kind == kind
            ]
    return "\n".join(lines)


# -- the rule, applied to the repository -------------------------------------


@pytest.fixture(scope="module")
def repository() -> tuple[Tree, list[Finding]]:
    tree = tracked(ROOT)
    return tree, findings(tree)


def test_the_citations_match_the_baseline(repository):
    tree, found = repository
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    differences = drift(counts(found), baseline)
    assert differences == [], (
        f"design citations differ from {BASELINE.relative_to(ROOT)}. A "
        "change that removes a citation, or a file holding one, lowers "
        "the file's entry in the same change; a new citation names a "
        "document and its heading instead "
        '(docs/TESTING.md, "Design citations and links"):\n'
        + explained(differences, found)
    )


def test_links_and_protocol_sections_resolve(repository):
    _, found = repository
    broken = [f for f in found if f.kind in {"link", "protocol-key"}]
    assert broken == [], "\n".join(map(str, broken))


def test_each_exempt_sign_still_occurs(repository):
    tree, _ = repository
    gone = [
        (path, prefix)
        for path, prefix in EXEMPT_SIGNS
        if prefix not in tree.texts.get(path, "")
    ]
    assert gone == [], "an exempt section sign no longer occurs: remove it"


# -- planted trees -----------------------------------------------------------

# A record with numbered sections, the protocol and a guide; the numbers
# 0801 to 0899 are invented for these cases.
TREE = {
    "docs/adr/0801-planted-record.md": (
        "# ADR-0801: Planted record\n\n"
        "## Decision\n\n"
        "### 1. Scope\n\n"
        "Text that cites its own §2.\n\n"
        "### 2. Placement\n\n"
        "### 2.1 Detail\n"
    ),
    "docs/AGENT_PROTOCOL.md": (
        "# Agent protocol\n\n## 1. Plan\n\n## 2. Labels\n\nSee §1.\n"
    ),
    "docs/GUIDE.md": (
        "# Guide\n\n"
        "## Unions and their events\n\n"
        "**Bold label.** A paragraph.\n\n"
        '<a id="kept-anchor"></a>\n\n'
        "## Unions and their events\n\n"
        "```sh\n"
        "## Not a heading\n"
        "```\n"
    ),
    "docs/OTHER.md": "# Other\n\n[The guide](GUIDE.md).\n",
    "src/yizkr/pipeline/prompts/reading.md": "# Prompt\n\n[x](missing.md)\n",
    "deploy/README.md": "# Deploy\n\n## Resetting\n",
    "README.md": "# Readme\n",
    "assets/scan.png": None,
}


def planted(files: Mapping[str, str | None]) -> Tree:
    """TREE with `files` replacing or adding files; a file given as None
    is tracked but not text."""
    every = TREE | dict(files)
    texts = {path: text for path, text in every.items() if text is not None}
    return Tree(texts, every)


def kinds(tree: Tree) -> Counter:
    return Counter((f.path, f.kind) for f in findings(tree))


PASSING = {
    "src/yizkr/rules.py": (
        "# ADR-0801 §1, §2 and §2.1, ADR-0801 §1-2 and ADR-0801 §1 to §2\n"
        '"""A rule (ADR-0801\n'
        "§1, §2), a range wrapped across a\n"
        'comment (docs/AGENT_PROTOCOL.md §1 and §2)."""\n'
        'TEXT = ("the record (ADR-0801 "\n'
        '        "§2) holds it")\n'
        '# docs/GUIDE.md, "Unions and their events"; GUIDE.md "Bold label"\n'
        "# docs/GUIDE.md, 'Unions and their'\n"
        'PATH = ROOT / "docs" / "GUIDE.md", "Not a section"\n'
    ),
    "src/yizkr/pipeline/gedcom.py": "# GEDCOM NAME TYPE (5.5.1 §, other)\n",
    "deploy/.env.example": '# README.md, "Resetting"\n',
    "docs/LINKS.md": (
        "# Links\n\n"
        "[a](GUIDE.md#unions-and-their-events) "
        "[b](GUIDE.md#unions-and-their-events-1) "
        "[c](GUIDE.md#kept-anchor) [d](#links) [e](../assets/scan.png) "
        "[f](adr/) [g](https://example.org/x.md) [h](<OTHER.md>)\n\n"
        "`[not a link](missing.md)`\n\n"
        "[ref]: ../README.md\n\n"
        "```md\n[fenced](missing.md)\n```\n\n"
        'A rule (ADR-0801 §2; docs/GUIDE.md, "Bold label"), as '
        "[AGENT_PROTOCOL §2](AGENT_PROTOCOL.md#2-labels) and "
        "`docs/AGENT_PROTOCOL.md` §1 say.\n"
    ),
}


def test_citations_and_links_that_resolve_pass():
    # TREE's prompt template links to a missing file: it is no document
    assert kinds(planted(PASSING)) == Counter()


@pytest.mark.parametrize(
    "files, expected",
    [
        pytest.param(
            {"src/yizkr/rules.py": "# A rule (ADR-0802, ADR-0803 §4).\n"},
            {("src/yizkr/rules.py", "removed-number"): 2},
            id="number-without-a-file",
        ),
        pytest.param(
            {"docs/NOTES.md": "A rule (ADR-0801 §3, §1 and §2.4-2.9).\n"},
            {("docs/NOTES.md", "section-key"): 3},
            id="section-key-with-no-numbered-heading",
        ),
        pytest.param(
            {"src/yizkr/rules.py": "# the record (ADR-0801 §B)\n"},
            {("src/yizkr/rules.py", "section-key"): 1},
            id="lettered-key-with-no-heading",
        ),
        pytest.param(
            {
                "src/yizkr/rules.py": (
                    "# A rule (ADR-0801\n"
                    "# §3).\n"
                    'TEXT = ("a rule (ADR-0801 "\n'
                    '        f"§4)")\n'
                    "description: >-\n"
                    "  as ADR-0801\n"
                    "  §5 says\n"
                )
            },
            {("src/yizkr/rules.py", "section-key"): 3},
            id="wrapped-citation",
        ),
        pytest.param(
            {
                "src/yizkr/rules.py": (
                    '"""ADR-0801 §1 says so.\n\nThe rule (§2) holds.\n'
                    'Checked first (ADR-0801 §1 before §2)."""\n'
                    "# as ADR-0801\n#\n# §2, a paragraph later, says\n"
                )
            },
            {("src/yizkr/rules.py", "stand-alone-sign"): 3},
            id="section-sign-that-names-no-document",
        ),
        pytest.param(
            {"src/yizkr/other.py": "# GEDCOM NAME TYPE (5.5.1 §, other)\n"},
            {("src/yizkr/other.py", "stand-alone-sign"): 1},
            id="exemption-is-for-its-file-only",
        ),
        pytest.param(
            {
                "tests/unit/test_rules.py": (
                    '# docs/GUIDE.md, "Marriages and periods"; GUIDE.md '
                    "“Unions of”\n"
                    "# docs/MISSING.md, 'Names'\n"
                    '# docs/GUIDE.md, "Unions and their even"\n'
                )
            },
            {("tests/unit/test_rules.py", "section-name"): 4},
            id="unresolved-section-name",
        ),
        pytest.param(
            {"docs/NOTES.md": "Cited in docs/AGENT_PROTOCOL.md §2 and §3.\n"},
            {("docs/NOTES.md", "protocol-key"): 1},
            id="protocol-key-with-no-numbered-heading",
        ),
        pytest.param(
            {
                "docs/NOTES.md": (
                    "[a](MISSING.md) [b](../../outside.md) [c](adr/09/)\n\n"
                    "[d]: GONE.md\n"
                )
            },
            {("docs/NOTES.md", "link"): 4},
            id="broken-link",
        ),
        pytest.param(
            {
                "docs/NOTES.md": (
                    "[a](GUIDE.md#marriages) [b](#no-heading) "
                    "[c](GUIDE.md#not-a-heading) "
                    "[d](GUIDE.md#unions-and-their-events-2)\n"
                )
            },
            {("docs/NOTES.md", "link"): 4},
            id="missing-anchor",
        ),
    ],
)
def test_a_planted_reference_that_does_not_resolve_fails(files, expected):
    assert kinds(planted(files)) == Counter(expected)


def test_a_file_above_its_entry_fails():
    held = {"src/yizkr/rules.py": {"removed-number": 2}}
    baseline = {"src/yizkr/rules.py": {"removed-number": 1}}
    assert drift(held, baseline) == [
        ("src/yizkr/rules.py", "removed-number", 2, 1)
    ]
    assert drift(held, {}) == [("src/yizkr/rules.py", "removed-number", 2, 0)]


def test_an_entry_above_its_file_fails():
    held = {"src/yizkr/rules.py": {"removed-number": 1}}
    baseline = {
        "src/yizkr/rules.py": {"removed-number": 3, "section-name": 1},
        "src/yizkr/gone.py": {"stand-alone-sign": 2},
    }
    assert drift(held, baseline) == [
        ("src/yizkr/gone.py", "stand-alone-sign", 0, 2),
        ("src/yizkr/rules.py", "removed-number", 1, 3),
        ("src/yizkr/rules.py", "section-name", 0, 1),
    ]


def test_a_tree_at_its_entries_passes():
    tree = planted(
        {
            "src/yizkr/rules.py": "# ADR-0802; a rule (§2)\n",
            "docs/NOTES.md": 'docs/GUIDE.md, "Gone"\n',
        }
    )
    baseline = {
        "src/yizkr/rules.py": {"removed-number": 1, "stand-alone-sign": 1},
        "docs/NOTES.md": {"section-name": 1},
    }
    assert drift(counts(findings(tree)), baseline) == []


def test_only_tracked_text_files_are_read(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)

    git("init", "-q")
    (tmp_path / "kept.md").write_text("# Kept (ADR-0802)\n", encoding="utf-8")
    (tmp_path / "deleted.md").write_text("ADR-0803\n", encoding="utf-8")
    (tmp_path / "scan.bin").write_bytes(b"ADR-0804\0")
    (tmp_path / "latin.txt").write_bytes("ADR-0805 caf\xe9".encode("latin-1"))
    git("add", ".")
    (tmp_path / "deleted.md").unlink()
    (tmp_path / "untracked.md").write_text("ADR-0806\n", encoding="utf-8")
    tree = tracked(tmp_path)
    assert tree.paths == {"kept.md", "scan.bin", "latin.txt"}
    assert set(tree.texts) == {"kept.md"}
    assert kinds(tree) == Counter({("kept.md", "removed-number"): 1})
