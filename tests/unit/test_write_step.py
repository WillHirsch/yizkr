"""Offline tests for the orchestrator's write step (`_step_write`).

The step applies a document's reviewed sheet to its resolved batch, writes
what the sheet approves through BatchWriter, and reports what it wrote.
These tests run the real step against the unit tests' in-memory instance,
with no LLM and no stack: a depicts claim goes through it only as its own
row of the sheet decides (ADR-0005, #258), and its summary and log line
count the type statements of items the batch found (#240).
"""

from __future__ import annotations

import csv

from tests.unit.test_faces import (
    decide_by_hand,
    depicted,
    portrait_faces,
    portrait_text,
    wiki_with_portrait,
)
from tests.unit.test_pipeline import (
    _directory_batch,
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
    line count the type statements of items the batch found rather than
    minted beside its claims (#240)."""

    def test_type_statements_are_counted(self, tmp_path):
        wiki = _two_sources()
        BatchWriter(wiki).write_batch(_firm_batch())
        # the directory gives the firm the interview's kind, and a type
        # hint of its own
        directory = _directory_batch(
            "unclassified_entity", type_hint="Hutmacherei"
        )
        step = WriteStep(tmp_path, wiki, "directory.txt")
        step.review(directory, decide=lambda row: "approve")

        summary = step.write(directory)

        assert summary["written"] == 1
        assert summary["type_statements_referenced"] == 1
        assert summary["type_statements_written"] == 1
        assert step.lines[-1] == (
            "  written: 1 new, 0 referenced, 0 skipped; type statements of "
            "items found: 1 new, 1 referenced"
        )
