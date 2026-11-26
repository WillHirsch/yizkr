"""Design citations and relative links in the tracked tree resolve.

docs/TESTING.md, "Design citations and links", states the convention
this checks.

The check reads every tracked file that is text, as `git ls-files` lists
them, and never the rest of the checkout. It leaves out a symlink, a file
deleted from the working tree, a file holding a NUL byte or invalid
UTF-8, this module and its baseline. A citation wrapped across comment,
docstring or YAML lines, or split between adjacent string literals, is
joined into one line before it is read; a finding keeps the number of
the original line where it starts.

Four kinds of finding are counted for each file against the baseline:

- removed-number: an ADR-NNNN whose number has no file in docs/adr;
- section-key: a section key after the number of an ADR that has a file,
  matching no numbered heading of that file;
- stand-alone-sign: a section sign in no key list that follows an ADR
  number or AGENT_PROTOCOL, outside docs/AGENT_PROTOCOL.md and docs/adr,
  which number their own sections, and outside EXEMPT_SIGNS;
- section-name: the path or name of a tracked Markdown document followed
  by a quoted name, with or without parentheses, that begins none of its
  headings or bold paragraph labels; or a Markdown path or name that
  resolves to no tracked Markdown file, followed by a quoted name without
  parentheses.

A key list is "§3", "§3, §7", "§2 and §6", "§8.5-8.7", "§8.5 to §8.7",
"§A" or "§10.1(c)", and may hold "Q" keys. A section sign in other words,
as in "§5 before §8", stands alone.

The baseline, doc_references_baseline.json, records for each file the
number of findings of each kind it held when the check was added. The
test fails when a file holds more or fewer findings of a kind than its
entry, and when an entry records zero or no kind, so a change that
removes a citation lowers its entry in the same change. It compares the
tree with the baseline alone, not with the baseline before the change,
so an entry raised or added together with a new citation passes; review
reads every baseline diff. The change that removes the last entry
deletes the baseline, the code that reads it and the sentences of
docs/TESTING.md that describe it.

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
    def markdown(self) -> frozenset[str]:
        """Every tracked *.md file, prompt templates included."""
        return frozenset(path for path in self.texts if path.endswith(".md"))

    @cached_property
    def documents(self) -> frozenset[str]:
        """The tracked Markdown documents: every *.md file but prompts."""
        return frozenset(
            path for path in self.markdown if not path.startswith(PROMPTS)
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
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
# A run of asterisks, or of underscores not inside a word: what
# CommonMark can read as emphasis.
EMPHASIS = re.compile(r"\*+|(?<![^\W_])_+|_+(?![^\W_])")


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
    pieces, last = [], 0
    for code in CODE_SPAN.finditer(text):
        pieces += [EMPHASIS.sub("", text[last : code.start()]), code[2]]
        last = code.end()
    pieces.append(EMPHASIS.sub("", text[last:]))
    return "".join(pieces).strip()


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
    r"(?P<parenthesised>\()?"
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


def markdown_named(tree: Tree, citing: str, name: str) -> str | None:
    """The tracked Markdown file `name` means in `citing`: beside the
    citing file, else from the repository root, else the one Markdown
    file whose path ends in it."""
    for candidate in (
        posixpath.normpath(posixpath.join(posixpath.dirname(citing), name)),
        posixpath.normpath(name),
    ):
        if candidate in tree.markdown:
            return candidate
    ending = [d for d in tree.markdown if d.endswith(f"/{name}")]
    return ending[0] if len(ending) == 1 else None


def exemption(path: str, text: str, at: int) -> tuple[str, str] | None:
    """The entry of EXEMPT_SIGNS that exempts the sign at offset `at`."""
    for entry in EXEMPT_SIGNS:
        exempt_path, prefix = entry
        if path == exempt_path and text.endswith(prefix, 0, at + 1):
            return entry
    return None


def citation_findings(tree: Tree, path: str) -> Iterator[Finding]:
    """The findings of the four baseline kinds in one file, of
    "protocol-key", an AGENT_PROTOCOL key with no numbered heading, and
    of "exempt-sign", a sign that an entry of EXEMPT_SIGNS exempts."""
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
            if entry := exemption(path, text.text, at):
                yield Finding(path, text.line(at), "exempt-sign", entry[1])
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
        document = markdown_named(tree, path, match.group("document"))
        line = text.line(match.start())
        if document is not None and document not in tree.documents:
            continue  # a prompt template
        if document is None and match.group("parenthesised"):
            # a quoted name in parentheses after a file that is not
            # tracked, such as a report a run writes, cites no document
            continue
        if document is None:
            yield Finding(
                path,
                line,
                "section-name",
                f"{match.group('document')} is no tracked Markdown file",
            )
        elif not begins(name, tree.outlines[document]):
            yield Finding(
                path,
                line,
                "section-name",
                f'{document} has no heading or label "{name}"',
            )


# -- links -------------------------------------------------------------------

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
    if location.startswith("/"):  # from the repository root
        resolved = posixpath.normpath(location.lstrip("/"))
    elif location:
        resolved = posixpath.join(posixpath.dirname(path), location)
        resolved = posixpath.normpath(resolved)
    if resolved == ".":
        return None  # the repository root
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


def empty(
    baseline: Mapping[str, Mapping[str, int]],
) -> list[tuple[str, str | None]]:
    """Each (file, kind) that the baseline records as zero, and (file,
    None) for a file that it records with no kind."""
    entries = []
    for path, recorded in sorted(baseline.items()):
        if not recorded:
            entries.append((path, None))
        entries += [(path, k) for k, n in sorted(recorded.items()) if not n]
    return entries


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
    unused = empty(baseline)
    assert unused == [], f"remove these empty kinds and entries: {unused}"
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


def test_each_exemption_is_used(repository):
    _, found = repository
    used = {(f.path, f.detail) for f in found if f.kind == "exempt-sign"}
    unused = sorted(EXEMPT_SIGNS - used)
    assert unused == [], f"remove the unused entries of EXEMPT_SIGNS: {unused}"


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
        "### 2.1 Detail\n\n"
        "### A. Lettered\n"
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
        "# ADR-0801's §1, ADR-0801, §2, ADR-0801 (§1) and ADR-0801 §§1-2\n"
        "# ADR-0801 §2.1(c), §1; ADR-0801 §1 or §2; ADR-0801 §1, Q4 and §2\n"
        "# ADR-0801 §A\n"
        '"""A rule (ADR-0801\n'
        "§1, §2), a range wrapped across a\n"
        'comment (docs/AGENT_PROTOCOL.md §1 and §2)."""\n'
        'TEXT = ("the record (ADR-0801 "\n'
        '        "§2) holds it")\n'
        'NOTE = ("see docs/GUIDE.md, "\n'
        "        '\"Bold label\"')\n"
        '# docs/GUIDE.md, "Unions and their events"; GUIDE.md "Bold label"\n'
        "# docs/GUIDE.md, 'Unions and their'; AGENT_PROTOCOL.md, \"Labels\"\n"
        '# deploy/README.md ("Resetting"); report.md ("Totals")\n'
        '# prompts/reading.md, "Not a heading"; '
        'src/yizkr/pipeline/prompts/reading.md, "Not a heading"\n'
        'PATH = ROOT / "docs" / "GUIDE.md", "Not a section"\n'
    ),
    "src/yizkr/static/site.js": (
        "// as ADR-0801\n// §1 says\n/* as ADR-0801\n * §2 says */\n"
    ),
    "deploy/init.sql": "-- as ADR-0801\n-- §1 says\n",
    "src/yizkr/pipeline/gedcom.py": "# GEDCOM NAME TYPE (5.5.1 §, other)\n",
    "deploy/.env.example": '# README.md, "Resetting"\n',
    "tests/unit/test_doc_references.py": "# ADR-0802 §9\n",
    "tests/unit/doc_references_baseline.json": '{"ADR-0802": 1}\n',
    "docs/LINKS.md": (
        "# Links\n\n"
        "[a](GUIDE.md#unions-and-their-events) "
        "[b](GUIDE.md#unions-and-their-events-1) "
        "[c](GUIDE.md#kept-anchor) [d](#links) [e](../assets/scan.png) "
        "[f](adr/) [g](https://example.org/x.md) [h](<OTHER.md>) "
        "[i](../) [j](/docs/GUIDE.md#unions-and-their-events) "
        "[k](#the-emphasis-case) [l](#the-_private-name)\n\n"
        "`[not a link](missing.md)`\n\n"
        "[ref]: ../README.md\n\n"
        "```md\n[fenced](missing.md)\n```\n\n"
        'A rule (ADR-0801 §2; docs/GUIDE.md, "Bold label"), as '
        "[AGENT_PROTOCOL §2](AGENT_PROTOCOL.md#2-labels) and "
        "`docs/AGENT_PROTOCOL.md` §1 say, and as "
        "[ADR-0801](adr/0801-planted-record.md) §1 states.\n\n"
        "## The _emphasis_ case\n\n"
        "## The `_private` name\n"
    ),
}


def test_citations_and_links_that_resolve_pass():
    # TREE's prompt template links to a missing file: it is no document
    assert kinds(planted(PASSING)) == Counter(
        {("src/yizkr/pipeline/gedcom.py", "exempt-sign"): 1}
    )


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
                "src/yizkr/pipeline/gedcom.py": (
                    "# GEDCOM NAME TYPE (5.5.1 §, other) (§5)\n"
                )
            },
            {
                ("src/yizkr/pipeline/gedcom.py", "exempt-sign"): 1,
                ("src/yizkr/pipeline/gedcom.py", "stand-alone-sign"): 1,
            },
            id="exemption-is-for-its-sign-only",
        ),
        pytest.param(
            {
                "tests/unit/test_rules.py": (
                    '# docs/GUIDE.md, "Marriages and periods"; GUIDE.md '
                    "“Unions of”\n"
                    "# docs/MISSING.md, 'Names'\n"
                    '# docs/GUIDE.md, "Unions and their even"\n'
                    '# docs/GUIDE.md ("Gone")\n'
                )
            },
            {("tests/unit/test_rules.py", "section-name"): 5},
            id="unresolved-section-name",
        ),
        pytest.param(
            {
                "docs/runbooks/GUIDE.md": (
                    "# Runbook\n\n## Unions and their events\n"
                ),
                "src/yizkr/rules.py": '# GUIDE.md, "Unions and their"\n',
            },
            {("src/yizkr/rules.py", "section-name"): 1},
            id="name-that-two-documents-end-in",
        ),
        pytest.param(
            {"docs/NOTES.md": "Cited in docs/AGENT_PROTOCOL.md §2 and §3.\n"},
            {("docs/NOTES.md", "protocol-key"): 1},
            id="protocol-key-with-no-numbered-heading",
        ),
        pytest.param(
            {
                "docs/NOTES.md": (
                    "[a](MISSING.md) [b](../../outside.md) [c](adr/09/) "
                    "[e](/docs/MISSING.md)\n\n"
                    "[d]: GONE.md\n"
                )
            },
            {("docs/NOTES.md", "link"): 5},
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


# A key list wrapped across comment lines, between a double-quoted and a
# single-quoted literal, and across YAML continuation lines.
WRAPPED = (
    "# A rule (ADR-0801\n"
    "# §3).\n"
    'TEXT = ("a rule (ADR-0801 "\n'
    "        f'§4)')\n"
    "description: >-\n"
    "  as ADR-0801\n"
    "  §5 says\n"
)


def test_a_wrapped_citation_is_read_as_one_at_its_first_line():
    found = findings(planted({"src/yizkr/rules.py": WRAPPED}))
    assert [(f.kind, f.line) for f in found] == [
        ("section-key", 1),
        ("section-key", 3),
        ("section-key", 6),
    ]


def test_a_file_above_its_entry_fails():
    tree = planted({"src/yizkr/rules.py": "# ADR-0802, ADR-0803\n"})
    held = counts(findings(tree))
    baseline = {"src/yizkr/rules.py": {"removed-number": 1}}
    assert drift(held, baseline) == [
        ("src/yizkr/rules.py", "removed-number", 2, 1)
    ]
    assert drift(held, {}) == [("src/yizkr/rules.py", "removed-number", 2, 0)]


def test_an_entry_above_its_file_fails():
    tree = planted({"src/yizkr/rules.py": "# ADR-0802\n"})
    baseline = {
        "src/yizkr/rules.py": {"removed-number": 3, "section-name": 1},
        "src/yizkr/gone.py": {"stand-alone-sign": 2},
    }
    assert drift(counts(findings(tree)), baseline) == [
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


def test_the_failure_names_each_file_and_what_to_record():
    tree = planted(
        {
            "src/yizkr/rules.py": "# ADR-0802\n# ADR-0803\n",
            "docs/NOTES.md": "ADR-0804\n",
        }
    )
    found = findings(tree)
    baseline = {
        "src/yizkr/rules.py": {"removed-number": 1},
        "docs/NOTES.md": {"removed-number": 3},
        "src/yizkr/gone.py": {"section-name": 1},
    }
    lines = explained(drift(counts(found), baseline), found).splitlines()
    by_file = {
        line.split(":")[0]: line for line in lines if not line[0].isspace()
    }
    assert "record 1" in by_file["docs/NOTES.md"]
    assert "remove the kind" in by_file["src/yizkr/gone.py"]
    assert "src/yizkr/rules.py" in by_file
    listed = [line.split()[0] for line in lines if line[0].isspace()]
    assert listed == ["src/yizkr/rules.py:1:", "src/yizkr/rules.py:2:"]


def test_an_entry_of_zero_or_no_kind_is_found():
    baseline = {
        "src/yizkr/rules.py": {"removed-number": 0, "section-name": 1},
        "src/yizkr/gone.py": {},
        "docs/NOTES.md": {"stand-alone-sign": 2},
    }
    assert empty(baseline) == [
        ("src/yizkr/gone.py", None),
        ("src/yizkr/rules.py", "removed-number"),
    ]


def test_only_tracked_text_files_are_read(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)

    git("init", "-q")
    (tmp_path / "kept.md").write_text("# Kept (ADR-0802)\n", encoding="utf-8")
    (tmp_path / "deleted.md").write_text("ADR-0803\n", encoding="utf-8")
    (tmp_path / "scan.bin").write_bytes(b"ADR-0804\0")
    (tmp_path / "latin.txt").write_bytes("ADR-0805 caf\xe9".encode("latin-1"))
    (tmp_path / "link.md").symlink_to("kept.md")
    git("add", ".")
    (tmp_path / "deleted.md").unlink()
    (tmp_path / "untracked.md").write_text("ADR-0806\n", encoding="utf-8")
    tree = tracked(tmp_path)
    assert tree.paths == {"kept.md", "scan.bin", "latin.txt"}
    assert set(tree.texts) == {"kept.md"}
    assert kinds(tree) == Counter({("kept.md", "removed-number"): 1})
