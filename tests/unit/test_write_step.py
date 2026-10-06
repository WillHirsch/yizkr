"""Offline tests for the orchestrator's write step (`_step_write`).

The step applies a document's reviewed sheet to its resolved batch, writes
what the sheet approves through BatchWriter, and reports what it wrote.
These tests run the real step against the unit tests' in-memory instance,
with no LLM and no stack: a depicts claim goes through it only as its own
row of the sheet decides (ADR-0005, #258), its summary and log line
count the type statements of items the batch found (#240), and its
summary names the claims the writer refuses (#366) and the terms it
leaves out (#431).
"""

from __future__ import annotations

import csv
import unicodedata

import pytest

from tests.fixtures.identity import corpus, mapped, write_mapped
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
    FIRM_LABEL,
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
    and the lines it logs. Its write resolves the batch's entities through
    `identity`, or the map of the batch alone: the run's identity map
    (Orchestrator._identity_map) is the map of the documents of a run, and
    these tests write a batch the step is given."""

    def __init__(self, tmp_path, wiki, name: str, identity=None) -> None:
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
        self.identity = identity
        self.orchestrator._identity_map = lambda: self.identity
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
        if self.identity is None:
            self.identity = mapped(batch)
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
        kind, type_hint = says
        directory = _directory_batch(kind, type_hint=type_hint)
        # the map puts the directory's firm in the interview's firm's
        # cluster
        identity = corpus(_firm_batch(), directory, entities=(FIRM_LABEL,))
        write_mapped(BatchWriter(wiki), _firm_batch(), identity=identity)
        step = WriteStep(tmp_path, wiki, "directory.txt", identity)
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
        directory = _directory_lines_batch("hat_shop")
        identity = corpus(_firm_batch(), directory, entities=(FIRM_LABEL,))
        write_mapped(BatchWriter(wiki), _firm_batch(), identity=identity)
        step = WriteStep(tmp_path, wiki, "directory.txt", identity)
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
        # names it by its claim id beside the claim it writes
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

    @pytest.mark.skipif(
        unicodedata.category("\U0001e08f") != "Cn",
        reason="this interpreter's Unicode tables assign U+1E08F",
    )
    def test_the_terms_the_writer_leaves_out_are_named(self, tmp_path):
        # (here) the directory's heading for the firm's trade, which a
        # transcriber gave with the combining Cyrillic i (U+1E08F) and a dot
        # below on its a: Python's tables do not know the i, so the writer
        # leaves the hint out, and the summary names it (#431)
        directory = _directory_batch(
            "unclassified_entity", type_hint="шляпа\U0001e08f\u0323"
        )
        step = WriteStep(tmp_path, _two_sources(), "directory.txt")
        step.review(directory, decide=lambda row: "approve")

        summary = step.write(directory)

        assert (summary["written"], summary["refused"]) == (1, [])
        assert summary["refused_terms"] == ["firm: type hint"]
        assert any(
            line.startswith("  directory.txt: entity 'firm' has a type hint")
            for line in step.lines
        )


class TestTheMappedBatch:
    """The step reads a document's mentions from its extraction
    (`doc.batch`), the batch the run's map was made from, not from the
    batch its review approves."""

    def test_a_rejected_claim_never_swaps_two_mentions(self, tmp_path):
        from tests.unit.test_identity_map_writes import (
            _two_eugens_without_a_manifest,
            _wiki,
        )

        # the extraction names the father (c001) first, then the son
        extracted = _two_eugens_without_a_manifest()
        wiki = _wiki()
        first = write_mapped(
            BatchWriter(wiki), extracted, identity=mapped(extracted)
        )
        step = WriteStep(tmp_path, wiki, "report.pdf", mapped(extracted))
        step.doc.batch.write_text(extracted.model_dump_json())
        # the reviewer rejects the father's birth: the son's ref comes
        # first in the batch the review approves
        father_born = claim_id(extracted.claims[0])
        step.review(
            extracted,
            decide=lambda row: (
                "reject" if row["claim_id"] == father_born else "approve"
            ),
        )

        step.write(extracted)

        assert [claim_id(c) for c in step.approved().claims] == [
            claim_id(c) for c in extracted.claims[1:]
        ]
        # read from the approved batch alone, the son's mention would be
        # the father's, and his birth written on the father's item
        father = first.created_items["c001:father"]
        son = first.created_items["c002:son"]
        assert father != son
        assert [_years_born(wiki, qid) for qid in (father, son)] == [
            [1836],
            [1870],
        ]


def _years_born(wiki, qid: str) -> list[int]:
    """The years of the births stated on item `qid` of `wiki`."""
    from tests.unit.test_pipeline import PROPERTIES

    return [
        int(statement.mainsnak.datavalue["value"]["time"][1:5])
        for statement in wiki.wbi.item.get(qid).claims.get(
            PROPERTIES["date_of_birth"]
        )
    ]
