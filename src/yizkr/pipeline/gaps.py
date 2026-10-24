"""Cross-document gap rollup (ADR-0013 §B): every relation gap the
extraction stage logged in a run, grouped by its normalised predicate,
into one CSV a curator reads in OpenRefine.

A run directory holds a directory per document (and the excerpts'
directories under EXCERPTS_DIR), each with a state.json. A document's
gaps are read, after its extraction, from

- `extraction/gaps.ndjson`, which the chunked extractor writes, one gap
  object a line (ChunkedClaimExtractor, SIDE_OUTPUTS); or, where the
  directory holds none of the extraction's side outputs (no
  `extraction/coverage.json`),
- `gaps.csv`, the per-document review CSV the orchestrator renders from
  the extraction's gaps, with the columns subject, predicate, object
  and evidence.

A complete chunked extraction writes its coverage report always and its
gap log only when it found gaps, removing a stale one, so where the
coverage report stands the gap log is the document's whole account: an
absent one means no gaps, and a gaps.csv beside it, which renders the
same gaps, is not read again. A document whose state says it duplicates
another holds no views and is passed over, and a content SHA-1 two
directories name is read once. A line or row that is not a gap (no
subject or no predicate) is passed over, as the extractor passes it over.

The frequency key is the gap's predicate in its comparison form
(predicate_key): Unicode compatibility forms, letter case, underscores,
hyphens and runs of whitespace do not make two predicates different, so
"arrived per", "Arrived_Per" and "arrived-per" count as one. Each row of
the rollup (COLUMNS) is one key:

- `predicate`: the key;
- `frequency`: how many gap lines have it;
- `documents`: how many documents logged it;
- `variants`: the predicate as the gap lines spell it, each distinct
  spelling once, the most frequent first, `|`-separated;
- `example_subject`, `example_object`, `example_evidence`: one gap line
  with the key, the first in document order;
- `document_sha1s`: the content SHA-1 of each document that logged it,
  `|`-separated, sorted;
- `decision` and `notes`: empty, for the curator's call (map to an
  existing property, mint one, or defer) and why. The rollup never fills
  them, and nothing reads them back: minting stays a human decision
  (ADR-0013, Not in scope).

Rows are ordered by frequency, then by documents, both descending, then
by key. The CSV holds evidence excerpts from the corpus, so it is
local-only runtime state, written into the run directory, never the
repository.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from yizkr.pipeline.orchestrate import DUPLICATE, EXCERPTS_DIR
from yizkr.pipeline.stages import atomic_write_text

ROLLUP_FILE = "gaps-rollup.csv"
# the chunked extractor's state directory in a document's directory
# (DocumentRun.extraction), and what it writes there
EXTRACTION_DIR = "extraction"
NDJSON = "gaps.ndjson"
COVERAGE = "coverage.json"
# the per-document review CSV (DocumentRun.gaps)
CSV = "gaps.csv"
GAP_FIELDS = ("subject", "predicate", "object", "evidence")
COLUMNS = (
    "predicate",
    "frequency",
    "documents",
    "variants",
    "example_subject",
    "example_object",
    "example_evidence",
    "document_sha1s",
    "decision",
    "notes",
)
SEPARATOR = "|"


def predicate_key(predicate: str) -> str:
    """A gap predicate's comparison form: NFKC, casefolded, with each run
    of underscores, hyphens and whitespace one space, stripped."""
    text = unicodedata.normalize("NFKC", predicate).casefold()
    return re.sub(r"[\s_\-]+", " ", text).strip()


def _gap(value: object) -> dict[str, str] | None:
    """A gap's four fields as text, or None when `value` is no gap: not
    an object, or with no subject or no predicate."""
    if not isinstance(value, dict):
        return None
    gap = {
        name: "" if value.get(name) is None else str(value.get(name))
        for name in GAP_FIELDS
    }
    if not gap["subject"].strip() or not predicate_key(gap["predicate"]):
        return None
    return gap


def _ndjson_gaps(path: Path) -> list[dict[str, str]]:
    gaps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        gap = _gap(value)
        if gap is not None:
            gaps.append(gap)
    return gaps


def _csv_gaps(path: Path) -> list[dict[str, str]]:
    # newline="" keeps a line break inside a quoted field as written
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [gap for gap in map(_gap, rows) if gap is not None]


def document_gaps(directory: Path) -> list[dict[str, str]]:
    """The gaps one document's directory holds: its chunked extraction's
    gap log, or its gaps.csv where it has no chunked extraction."""
    extraction = directory / EXTRACTION_DIR
    if (extraction / NDJSON).is_file():
        return _ndjson_gaps(extraction / NDJSON)
    if (extraction / COVERAGE).is_file():
        return []
    if (directory / CSV).is_file():
        return _csv_gaps(directory / CSV)
    return []


def _state(directory: Path) -> dict | None:
    try:
        state = json.loads((directory / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def document_directories(run: Path) -> list[tuple[str, Path]]:
    """(content SHA-1, directory) for each document of the run directory:
    each directory, and each in EXCERPTS_DIR, whose state.json can be
    read and does not mark a duplicate, in name order, a SHA-1 once. A
    state without a SHA-1 is keyed by its directory's name."""
    entries: list[Path] = []
    for parent in (run, run / EXCERPTS_DIR):
        if parent.is_dir():
            entries += sorted(p for p in parent.iterdir() if p.is_dir())
    found: dict[str, Path] = {}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        state = _state(entry)
        if state is None or state.get("status") == DUPLICATE:
            continue
        sha1 = state.get("content_sha1")
        key = sha1 if isinstance(sha1, str) and sha1 else entry.name
        found.setdefault(key, entry)
    return list(found.items())


@dataclass
class _Row:
    key: str
    first: dict[str, str]
    frequency: int = 0
    spellings: Counter = field(default_factory=Counter)
    # the documents that logged the key, in the order they were read
    documents: dict[str, None] = field(default_factory=dict)

    def record(self) -> dict[str, str | int]:
        variants = sorted(
            self.spellings.items(), key=lambda kv: (-kv[1], kv[0])
        )
        return {
            "predicate": self.key,
            "frequency": self.frequency,
            "documents": len(self.documents),
            "variants": SEPARATOR.join(spelling for spelling, _ in variants),
            "example_subject": self.first["subject"],
            "example_object": self.first["object"],
            "example_evidence": self.first["evidence"],
            "document_sha1s": SEPARATOR.join(sorted(self.documents)),
            "decision": "",
            "notes": "",
        }


@dataclass
class Rollup:
    """The rollup of one run: its rows (COLUMNS), how many documents were
    read and how many of them logged a gap, and how many gaps."""

    rows: list[dict[str, str | int]]
    documents_read: int
    documents_with_gaps: int
    gaps: int


def rollup(run: Path) -> Rollup:
    """Every gap the run's documents logged, one row per predicate key."""
    rows: dict[str, _Row] = {}
    documents_read = documents_with_gaps = total = 0
    for sha1, directory in document_directories(Path(run)):
        documents_read += 1
        gaps = document_gaps(directory)
        if gaps:
            documents_with_gaps += 1
        for gap in gaps:
            key = predicate_key(gap["predicate"])
            row = rows.setdefault(key, _Row(key, gap))
            row.frequency += 1
            row.spellings[" ".join(gap["predicate"].split())] += 1
            row.documents[sha1] = None
            total += 1
    ordered = sorted(
        rows.values(),
        key=lambda row: (-row.frequency, -len(row.documents), row.key),
    )
    return Rollup(
        [row.record() for row in ordered],
        documents_read,
        documents_with_gaps,
        total,
    )


def write_rollup(run: Path, out: Path | None = None) -> tuple[Path, Rollup]:
    """Write the run's rollup to `out`, by default ROLLUP_FILE in the run
    directory, and return where with the rollup."""
    result = rollup(run)
    target = out if out is not None else Path(run) / ROLLUP_FILE
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(result.rows)
    atomic_write_text(target, buffer.getvalue())
    return target, result
