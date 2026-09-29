"""Offline tests for the orchestrator's write step (`_step_write`).

The step applies a document's reviewed sheet to its resolved batch, writes
what the sheet approves through BatchWriter, and reports what it wrote.
These tests run the real step against the unit tests' in-memory instance,
with no LLM and no stack: a depicts claim goes through it only as its own
row of the sheet decides (ADR-0005, #258), its summary and log line
count the type statements of items the batch found (#240), and its
summary names the claims the writer refuses (#366).
"""

from __future__ import annotations

import csv

import pytest

from tests.unit.test_faces import (
    decide_by_hand,
    depicted,
    portrait_faces,
    portrait_text,
    wiki_with_portrait,
)
from tests.unit.test_pipeline import (
    DIRECTORY_SHA1,
    FIRM_ENTRY,
    _directory_batch,
    _directory_lines_batch,
    _firm_batch,
    _two_sources,
)
from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionList
from yizkr.pipeline.orchestrate import DocumentRun, Orchestrator
from yizkr.pipeline.review import (
    claim_id,
    export_review_csv,
    unapproved_depicts,
)
from yizkr.pipeline.writer import BatchWriter


class WriteStep:
    """An orchestrator over one document, its instance the fake `wiki`,
    and the lines it logs."""

    def __init__(self, tmp_path, wiki, name: str) -> None:
        self.lines: list[str] = []
        self.orchestrator = Orchestrator(
            tmp_path / "docs",
            tmp_path / "state",
            exclusions=ExclusionList(),
            log=lambda *args, **kwargs: self.lines.append(
                " ".join(map(str, args))
            ),
        )
        self.orchestrator._client = wiki
        self.doc = DocumentRun(tmp_path / "docs" / name, tmp_path / "state")
        self.doc.dir.mkdir(parents=True)

    def review(self, batch: ClaimBatch, decide, keep=lambda row: True):
        """Export `batch` for review, keep the rows `keep` passes, and save
        each one's decision, `decide(row)`, as the reviewed sheet."""
        export_review_csv(batch, self.doc.review)
        with open(self.doc.review, newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = [row for row in reader if keep(row)]
        with open(self.doc.review_reviewed, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        decide_by_hand(self.doc.review_reviewed, decide)

    def write(self, batch: ClaimBatch) -> dict:
        return self.orchestrator._step_write(self.doc, batch)

    def approved(self) -> ClaimBatch:
        return ClaimBatch.model_validate_json(self.doc.approved.read_text())


class TestDepictsThroughTheWriteStep:
    """ADR-0005 at the orchestrator's write step: a depicts claim reaches
    the graph only when its own row of the sheet approves it."""

    def test_a_depicts_claim_approved_on_its_row_is_written(self, tmp_path):
        faces = portrait_faces()
        wiki = wiki_with_portrait()
        step = WriteStep(tmp_path, wiki, "portrait.jpg")
        step.review(faces, decide=lambda row: "approve")

        summary = step.write(faces)

        assert (summary["written"], summary["approved_claims"]) == (2, 2)
        assert set(depicted(wiki)) == {"Q26", "Q27"}
        # the approved batch the step kept holds each row's approval
        assert unapproved_depicts(step.approved()) == []

    def test_a_depicts_claim_missing_from_the_sheet_is_excluded(
        self, tmp_path
    ):
        # the curator deleted Rahel's depicts row and approved the others
        # on their rows: her claim is left out, the rest written
        text = portrait_text()
        date, eugen, rahel = text.claims
        wiki = wiki_with_portrait()
        step = WriteStep(tmp_path, wiki, "portrait.jpg")
        step.review(
            text,
            decide=lambda row: "approve",
            keep=lambda row: row["claim_id"] != claim_id(rahel),
        )

        summary = step.write(text)

        assert (summary["written"], summary["approved_claims"]) == (2, 2)
        assert set(depicted(wiki)) == {"Q26"}
        assert [claim_id(c) for c in step.approved().claims] == [
            claim_id(date),
            claim_id(eugen),
        ]
        assert any(
            f"claim {claim_id(rahel)} has no row in the CSV: excluded" in line
            for line in step.lines
        )


class TestWriteStepSummary:
    """The step's summary, kept as the document's write.json, and its log
    line count the type statements of items a ref or record of the batch
    found (step 5 of the writer's module docstring), beside its claims
    (#240)."""

    # what the directory gives the firm, which the interview holds as an
    # unclassified entity with the type hint "hat shop" -> (type statements
    # written, type statements referenced)
    DIRECTORY_SAYS = {
        # the interview's kind and hint: both gain the directory's source
        "agrees": (("unclassified_entity", "Hat Shop"), (0, 2)),
        # a kind and a hint of its own: two new statements
        "differs": (("organization", "Hutmacherei"), (2, 0)),
    }

    @pytest.mark.parametrize(
        ("says", "counts"), DIRECTORY_SAYS.values(), ids=DIRECTORY_SAYS.keys()
    )
    def test_type_statements_are_counted(self, tmp_path, says, counts):
        wiki = _two_sources()
        BatchWriter(wiki).write_batch(_firm_batch())
        kind, type_hint = says
        directory = _directory_batch(kind, type_hint=type_hint)
        step = WriteStep(tmp_path, wiki, "directory.txt")
        step.review(directory, decide=lambda row: "approve")

        summary = step.write(directory)

        written, referenced = counts
        assert summary["written"] == 1
        assert summary["type_statements_written"] == written
        assert summary["type_statements_referenced"] == referenced
        assert step.lines[-1] == (
            "  written: 1 new, 0 referenced, 0 skipped; type statements of "
            f"items found: {written} new, {referenced} referenced"
        )

    def test_the_writers_warnings_are_logged(self, tmp_path):
        # the directory's line-range chunk typed the firm by a kind the
        # vocabulary lacks: the writer records no kind for it, and says so
        wiki = _two_sources()
        BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_lines_batch("hat_shop")
        step = WriteStep(tmp_path, wiki, "directory.txt")
        step.review(directory, decide=lambda row: "approve")

        step.write(directory)

        assert (
            "  directory.txt: entity 'firm' has the kind 'hat_shop', which "
            "the vocabulary lacks: no kind is recorded for it"
        ) in step.lines

    def test_the_claims_the_writer_refuses_are_named(self, tmp_path):
        # (here) the directory's extraction took the firm's entry, over and
        # over, as a name the directory refers to it by: longer than a text
        # value holds, so the writer refuses the claim, and the summary
        # names it by its review-sheet id beside the claim it writes
        entry = (f"{FIRM_ENTRY}. " * 11)[:400] + "."
        name = {
            "property": "referred_to_as",
            "value": {
                "type": "monolingual",
                "value": {"text": entry, "language": "de"},
            },
            "source": {
                "artifact": {"kind": "artifact", "sha1": DIRECTORY_SHA1},
                "transcription_excerpt": {
                    "text": FIRM_ENTRY,
                    "language": "de",
                },
            },
        }
        directory = _directory_batch(extra_claims=(name,))
        step = WriteStep(tmp_path, _two_sources(), "directory.txt")
        step.review(directory, decide=lambda row: "approve")

        summary = step.write(directory)

        refused = claim_id(directory.claims[1])
        assert (summary["written"], summary["refused"]) == (1, [refused])
        assert any(
            line.startswith(f"  directory.txt: claim {refused} ")
            for line in step.lines
        )
