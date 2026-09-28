"""Guard for the code-comment standard (CLAUDE.md, Engineering conventions).

Comments and docstrings in src/yizkr/ describe how the code works now,
never its development history. This test enforces the mechanical part of
that rule. It fails on an issue or PR reference (``#`` followed by
digits, or a GitHub issue/PR link) or a reference into the retired
ROADMAP (the word itself, or an ``item N`` / ``task N`` number) in:

* Python comments, docstrings and prompt templates (string constants
  named ``*PROMPT``);
* SPARQL comments in the packaged ``.rq`` queries;
* YAML comments and string values in the model manifest, whose
  descriptions are rendered into prompts and onto the wiki.

``README.md`` files are development docs and may cite issues, so they
are not scanned. Narration such as "previously ..." needs a reader's
judgement and is left to review (docs/AGENT_PROTOCOL.md §4).
"""

import ast
import io
import re
import shutil
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parents[2] / "src" / "yizkr"

# An issue or PR reference: `#` then digits, not inside a longer token or
# an HTML entity (`&#39;`); or a link to a GitHub issue or pull request.
ISSUE_REF = re.compile(
    r"(?<![\w&#])#\d+\b"
    r"|github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+"
)
# A reference into the retired ROADMAP: its name, or one of its item or
# task numbers ("item 11", "W1 task 7").
ROADMAP_REF = re.compile(r"\bROADMAP\b|\b(?:item|task)\s+\d+\b")

# A text found in a source file: (line number, kind, text).
Found = tuple[int, str, str]


def references(text: str) -> Iterator[re.Match[str]]:
    """Every development-history reference in one comment or docstring."""
    for pattern in (ISSUE_REF, ROADMAP_REF):
        yield from pattern.finditer(text)


def violations(text: str) -> list[str]:
    return [match.group(0) for match in references(text)]


def python_texts(source: str) -> Iterator[Found]:
    """Comments, docstrings (any bare string statement) and prompt
    templates of one Python module."""
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], "comment", token.string
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            yield node.lineno, "docstring", node.value.value
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(
                isinstance(target, ast.Name) and target.id.endswith("PROMPT")
                for target in node.targets
            )
        ):
            yield node.lineno, "prompt", node.value.value


# IRIs and string literals, which may hold a `#` that is not a comment
# (`<http://www.w3.org/2002/07/owl#sameAs>`).
_SPARQL_NON_COMMENT = re.compile(
    r"<[^\s<>]*>|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"
)


def sparql_texts(source: str) -> Iterator[Found]:
    """Comments of one SPARQL query, whole-line and trailing."""
    for number, line in enumerate(source.splitlines(), start=1):
        masked = _SPARQL_NON_COMMENT.sub(
            lambda match: " " * len(match.group(0)), line
        )
        start = masked.find("#")
        if start != -1:
            yield number, "comment", line[start:]


def _yaml_strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _yaml_strings(key)
            yield from _yaml_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _yaml_strings(value)


# A block scalar's header: its `|` or `>` indicator, then optional
# chomping and indentation indicators. A comment may follow it.
_YAML_BLOCK_HEADER = re.compile(r"[|>][1-9+-]{0,2}")


def yaml_texts(source: str) -> Iterator[Found]:
    """Comments and string values of one YAML document.

    A comment is whatever follows the last token on a line, so a `#`
    inside a quoted or block scalar is never mistaken for one."""
    lines = source.splitlines()
    whole_line = 1 << 30
    covered: dict[int, int] = {}
    for token in yaml.scan(source):
        start, end = token.start_mark, token.end_mark
        for line in range(start.line, end.line + 1):
            if line == end.line:
                reach = end.column
            elif line == start.line and getattr(token, "style", None) in (
                "|",
                ">",
            ):
                header = _YAML_BLOCK_HEADER.match(lines[line], start.column)
                reach = header.end() if header else whole_line
            else:
                reach = whole_line
            covered[line] = max(covered.get(line, 0), reach)
    for index, line in enumerate(lines):
        rest = line[covered.get(index, 0) :].lstrip()
        if rest.startswith("#"):
            yield index + 1, "comment", rest
    for text in _yaml_strings(yaml.safe_load(source)):
        yield 0, "value", text


SCANNERS = {
    ".py": python_texts,
    ".rq": sparql_texts,
    ".yaml": yaml_texts,
    ".yml": yaml_texts,
}


def tree_violations(root: Path) -> list[str]:
    """`path:line: kind: reference` for every violation under `root`."""
    problems = []
    for path in sorted(root.rglob("*")):
        scan = SCANNERS.get(path.suffix)
        if scan is None or not path.is_file():
            continue
        where = path.relative_to(root.parent)
        for line, kind, text in scan(path.read_text(encoding="utf-8")):
            for match in references(text):
                # a docstring or prompt spans lines; point at the match
                at = line + text.count("\n", 0, match.start()) if line else 0
                problems.append(f"{where}:{at}: {kind}: {match.group(0)!r}")
    return problems


# -- the rule, applied to the package ---------------------------------------


def test_src_comments_carry_no_development_history():
    problems = tree_violations(SRC)
    assert not problems, (
        "comments and docstrings in src/yizkr describe the code as it "
        "is now; move development history to commits, PRs, issues or "
        "ADRs (CLAUDE.md, Engineering conventions):\n" + "\n".join(problems)
    )


def test_every_source_kind_is_scanned():
    """The scanners see real text in each kind of packaged file, so a
    silent parse failure cannot pass the rule vacuously."""
    counts = {
        suffix: sum(
            len(list(SCANNERS[suffix](path.read_text(encoding="utf-8"))))
            for path in SRC.rglob(f"*{suffix}")
        )
        for suffix in (".py", ".rq", ".yaml")
    }
    assert all(counts.values()), counts


# -- planted violations are caught ------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "x = 1  # see #123\n",
        "#123: a comment that opens with an issue number\n",
        '"""Backup and export (ROADMAP W1 task 7)."""\n',
        '"""GEDCOM adapter (item 13)."""\n',
        'def f():\n    """Formats the census (issue #8)."""\n',
        'class C:\n    x: int = 0\n    """Set by #45\'s fix."""\n',
        "# -- corrections (ROADMAP, item 11) --\n",
        "# https://github.com/owner/repo/pull/7\n",
        'CHUNK_PROMPT = """Extract claims (#12)."""\n',
    ],
)
def test_planted_python_violation_is_caught(source):
    assert any(violations(text) for _, _, text in python_texts(source))


@pytest.mark.parametrize(
    "source",
    [
        "import os  # noqa: F401\n",
        "x = y  # type: ignore[attr-defined]\n",
        "T = (1, 2)  # fmt: skip\n",
        "x = 1  # pragma: no cover\n",
        '"""Content identity (ADR-0010, R7, docs/MODELING.md)."""\n',
        '"""Escaped as &#39; in HTML."""\n',
        'INK = "#222"  # a CSS colour in code, not in a comment\n',
        'MESSAGE = "see #12"  # a string that is neither doc nor prompt\n',
    ],
)
def test_compliant_python_passes(source):
    assert not [
        text for _, _, text in python_texts(source) if violations(text)
    ]


def test_planted_sparql_violation_is_caught():
    source = (
        "# Gallery (ROADMAP, frontend ladder).\n"
        "SELECT ?x WHERE {\n"
        "  ?x <http://www.w3.org/2002/07/owl#sameAs> ?y .  # see #50\n"
        '  FILTER(?label != "#9")\n'
        "}\n"
    )
    found = [
        (line, violations(text))
        for line, _, text in sparql_texts(source)
        if violations(text)
    ]
    assert found == [(1, ["ROADMAP"]), (3, ["#50"])]


def test_planted_yaml_violation_is_caught():
    source = (
        "# Model manifest (item 4).\n"
        "properties:\n"
        "  spouse:\n"
        "    label: spouse  # added for #31\n"
        "    description: >-  # reworded in #40\n"
        "      the subject has the object as their spouse (see #77)\n"
        "      # this line is scalar text, not a comment\n"
        "  # between keys (ROADMAP)\n"
        "  note:\n"
        "    label: 'a quoted # sign, not a comment'\n"
    )
    texts = list(yaml_texts(source))
    assert [text for _, kind, text in texts if kind == "comment"] == [
        "# Model manifest (item 4).",
        "# added for #31",
        "# reworded in #40",
        "# between keys (ROADMAP)",
    ]
    found = sorted(
        (kind, reference)
        for _, kind, text in texts
        for reference in violations(text)
    )
    assert found == [
        ("comment", "#31"),
        ("comment", "#40"),
        ("comment", "ROADMAP"),
        ("comment", "item 4"),
        ("value", "#77"),
    ]


def test_planted_violation_fails_the_tree_and_readmes_are_exempt(tmp_path):
    tree = tmp_path / "yizkr"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "README.md").write_text("Development docs may cite #189.\n")
    assert tree_violations(tree) == []

    target = tree / "pipeline" / "places.py"
    target.write_text(
        target.read_text() + "\n\n# fixed in #189 (ROADMAP, item 18)\n"
    )
    problems = tree_violations(tree)
    assert len(problems) == 3
    assert all(
        problem.startswith("yizkr/pipeline/places.py:") for problem in problems
    )
