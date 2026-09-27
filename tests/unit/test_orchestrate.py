"""Offline tests for folder orchestration (ROADMAP item 15).

The wiki steps are replaced with scripted fakes and the engine with a
scripted model; what is under test is the state machine: per-document
state, the stage store underneath it (#87), the two human gates, resume,
and per-document failure isolation.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil

import pytest

from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionConfigError, ExclusionList
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    AWAITING_EQUIVALENCES,
    BATCHED,
    DUPLICATE,
    FAILED,
    PAUSED,
    UNRECORDED,
    WRITTEN,
    DocumentRun,
    Orchestrator,
    slugify,
)
from yizkr.pipeline.reconcile import (
    EQUIVALENCE_FIELDS,
    apply_equivalences,
    read_approved_equivalences,
)
from yizkr.pipeline.review import CSV_COLUMNS, export_review_csv
from yizkr.pipeline.rundir import run_state

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-14T09:00:00Z",
}


def make_batch(entities=True) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e1",
                        "label": "Anna",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1901},
                    "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
                }
            ],
            "entities": [{"id": "e1", "label": "Anna", "kind": "human"}]
            if entities
            else [],
        }
    )


def _person(prompt: str) -> tuple[str, int]:
    """The first word and year of a prompt's first numbered line."""
    match = re.search(r"^1: (.*)$", prompt, re.M)
    line = match.group(1) if match else ""
    words = re.findall(r"[A-Za-z]+", line)
    year = re.search(r"\b(1[5-9]\d\d)\b", line)
    return (words[0] if words else "Someone"), (
        int(year.group(1)) if year else 1900
    )


class ScriptedModel:
    """A scripted engine: answers the entity-manifest, chunk and critique
    prompts from the document text it is shown, one person per document,
    and logs each call as '<role>:<file name>'. It has no conversation
    capability, so chunks see the document inline."""

    model_identifier = "fake-model"

    def __init__(self, calls: list[str], name: str, fail: str | None = None):
        self.calls, self.name, self.fail = calls, name, fail

    def complete(self, prompt, image_paths=()):
        if "manifest of EVERY distinct entity" in prompt:
            role = "manifest"
        elif prompt.startswith("You review a genealogical extraction"):
            role = "critique"
        else:
            role = "chunk"
        self.calls.append(f"{role}:{self.name}")
        if role == self.fail:
            raise RuntimeError(f"{role} engine down")
        person, year = _person(prompt)
        if role == "manifest":
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": "e1",
                            "label": person,
                            "kind": "human",
                            "mention_lines": [1],
                        }
                    ]
                }
            )
        if role == "critique":
            return '{"done": true, "findings_emitted": 0}'
        artifact = re.search(r"artifact QID (Q\d+)", prompt).group(1)
        claim = {
            "subject": {"kind": "local", "local_id": "e1", "label": person},
            "property": "date_of_birth",
            "value": {"type": "time", "year": year},
            "source": {"artifact": {"kind": "qid", "qid": artifact}},
        }
        return "\n".join(
            [json.dumps(claim), '{"done": true, "claims_emitted": 1}']
        )


class DriftingModel(ScriptedModel):
    """The scripted engine answering the same prompts again, differently,
    as a model can: it reads the year 1901 as 1902."""

    def complete(self, prompt, image_paths=()):
        return super().complete(prompt, image_paths).replace("1901", "1902")


class ScriptedOrchestrator(Orchestrator):
    """Every wiki step replaced with a scripted fake, the engine with a
    scripted model; transcription (text files), extraction, critique and
    the stage store run for real."""

    def __init__(self, *args, proposals=1, fail_step=None, **kwargs):
        # run() refuses without a never-ingest list (#29); the state-machine
        # tests run with the explicit empty one unless they pass their own
        kwargs.setdefault("exclusions", ExclusionList())
        super().__init__(*args, **kwargs)
        self.proposals = proposals
        self.fail_step = fail_step
        self.calls: list[str] = []

    def _llm(self, role, doc):
        return ScriptedModel(self.calls, doc.source.name, fail=self.fail_step)

    def _step_register(self, doc):
        self.calls.append(f"register:{doc.source.name}")
        if self.fail_step == "register":
            raise RuntimeError("wiki down")
        return "Q100"

    def _step_reconcile(self, doc, batch):
        self.calls.append(f"reconcile:{doc.source.name}")
        with open(doc.equivalences, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS)
            writer.writeheader()
            for index in range(self.proposals):
                writer.writerow(
                    {
                        "entity_id": "e1",
                        "entity_label": "Anna",
                        "qid": f"Q{40 + index}",
                        "qid_label": "Anna Maria",
                        "verdict": "same",
                        "confidence": 0.9,
                        "evidence": "same spouse",
                        "decision": "",
                    }
                )
        return self.proposals

    def _step_resolve(self, doc, batch):
        self.calls.append(f"resolve:{doc.source.name}")
        return batch

    def _step_export_review(self, doc, batch):
        self.calls.append(f"export:{doc.source.name}")
        return export_review_csv(batch, doc.review)

    def _step_write(self, doc, batch):
        self.calls.append(f"write:{doc.source.name}")
        return {
            "run_qid": "Q999",
            "written": len(batch.claims),
            "skipped": 0,
            "referenced": 0,
            "created_items": {},
            "approved_claims": len(batch.claims),
        }


class DriftingOrchestrator(ScriptedOrchestrator):
    def _llm(self, role, doc):
        return DriftingModel(self.calls, doc.source.name, fail=self.fail_step)


def quiet(*args, **kwargs):
    pass


def make_run(tmp_path, name="letter.txt", **kwargs):
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text("Anna was born in 1901.")
    kwargs.setdefault("log", quiet)
    orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", **kwargs)
    return orchestrator


def collect(lines: list[str]):
    """A log that keeps each message."""

    def log(*args, **kwargs):
        lines.append(" ".join(str(arg) for arg in args))

    return log


def decide_all_equivalences(doc: DocumentRun, decision: str) -> None:
    with open(doc.equivalences, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["decision"] = decision
    with open(doc.equivalences_reviewed, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def approve_all_equivalences(doc: DocumentRun) -> None:
    decide_all_equivalences(doc, "approve")


def approve_all_claims(doc: DocumentRun) -> None:
    with open(doc.review, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["decision"] = "approve"
    with open(doc.review_reviewed, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def saved_last(path) -> None:
    """Give `path` a modification time after every other file beside it,
    as a file saved after them has. Files written moments apart can share
    a timestamp on a coarse filesystem clock, so the tests set it."""
    newest = max(
        other.stat().st_mtime_ns
        for other in path.parent.iterdir()
        if other.is_file() and other != path
    )
    stamp = newest + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def completed_run(tmp_path, **kwargs):
    """A one-document run taken through both gates to `written`."""
    orchestrator = make_run(tmp_path, **kwargs)
    orchestrator.run()
    (doc,) = orchestrator.documents()
    approve_all_equivalences(doc)
    orchestrator.run()
    (doc,) = orchestrator.documents()
    approve_all_claims(doc)
    assert orchestrator.run() == 0
    return orchestrator


def snapshot(directory) -> dict:
    """Every file in a document directory but its state.json, with its
    bytes and modification time: a run that touches none of them leaves
    this unchanged."""
    return {
        path.relative_to(directory): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "state.json"
    }


class TestGates:
    def test_pauses_at_equivalence_gate(self, tmp_path):
        orchestrator = make_run(tmp_path)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert doc.equivalences.exists()
        assert "write:letter.txt" not in orchestrator.calls

    def test_reviewed_equivalences_advance_to_claim_gate(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert doc.review.exists()

    def test_reviewed_claims_reach_write(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert json.loads(doc.write_result.read_text())["written"] == 1

    def test_no_proposals_skip_equivalence_gate(self, tmp_path):
        orchestrator = make_run(tmp_path, proposals=0)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS  # straight to claim review

    def test_half_filled_equivalences_csv_is_not_consent(self, tmp_path):
        """Only the reviewed file's PRESENCE opens the gate."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        with open(doc.equivalences, newline="") as f:
            rows = list(csv.DictReader(f))
        rows[0]["decision"] = "approve"
        with open(doc.equivalences, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES


class TestResume:
    def test_steps_are_not_repeated_on_rerun(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        calls_after_first = list(orchestrator.calls)
        orchestrator.run()  # still waiting: nothing new should execute
        repeated = orchestrator.calls[len(calls_after_first) :]
        # every stage before the gates is reused from the store
        assert not any(
            call.startswith(("register", "manifest", "chunk", "critique"))
            for call in repeated
        )

    def test_written_document_is_a_noop(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_claims(doc)
        orchestrator.run()
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert orchestrator.calls == before

    def test_state_survives_new_orchestrator(self, tmp_path):
        first = make_run(tmp_path)
        first.run()
        (doc,) = first.documents()
        approve_all_equivalences(doc)
        second = ScriptedOrchestrator(first.folder, first.state_dir, log=quiet)
        second.run()
        (doc,) = second.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert doc.state.artifact_qid == "Q100"
        # register was not repeated
        assert "register:letter.txt" not in second.calls

    def test_cached_batch_rerun_clears_stale_failed(self, tmp_path):
        # serial mode extracts every doc up front (stop_after_extract),
        # returning BEFORE the reconcile gates that recompute status. So a
        # stale FAILED from a prior downstream outage (e.g. a write-time
        # DNS failure) would be echoed as-is on every re-run unless the
        # cached-batch branch clears it — the batch proves extraction
        # succeeded, so "failed" is wrong.
        orchestrator = make_run(tmp_path)
        orchestrator.run()  # produce a valid cached batch
        (doc,) = orchestrator.documents()
        assert doc.batch.exists()
        doc.state.status = FAILED  # simulate the prior downstream outage
        doc.state.error = None
        doc.save()

        # the extract-only pass no longer echoes the stale failure...
        (doc,) = orchestrator.documents()
        assert orchestrator.advance(doc, stop_after_extract=True) == BATCHED
        assert doc.state.status == BATCHED
        # ...and a full re-run advances to the gate instead of failing
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES


class TestIsolationAndDiscovery:
    def test_failure_in_one_document_does_not_stop_others(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        # distinct content so the dedup (ADR-0010) treats them as two
        # artifacts — this test is about failure isolation, not dedup
        (folder / "a-good.txt").write_text("good text")
        (folder / "b-bad.txt").write_text("bad text")

        class OneBad(ScriptedOrchestrator):
            def _step_batch(self, doc):
                if "bad" in doc.source.name:
                    raise RuntimeError("boom")
                return super()._step_batch(doc)

        orchestrator = OneBad(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 1  # failure wins the exit code
        good, bad = orchestrator.documents()
        assert good.state.status == AWAITING_EQUIVALENCES
        assert bad.state.status == FAILED
        assert "boom" in bad.state.error

    def test_state_dir_holds_documents_and_the_run_files(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        names = sorted(p.name for p in orchestrator.state_dir.iterdir())
        assert names == [
            ".stages",
            "control.jsonl",
            "events.jsonl",
            "letter",
            "run.json",
        ]

    def test_unsupported_files_are_skipped(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "notes.txt").write_text("text")
        (folder / "data.xlsx").write_text("not a document")
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        assert [d.source.name for d in orchestrator.documents()] == [
            "notes.txt"
        ]

    def test_gedcom_routes_through_deterministic_adapter(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "family.ged").write_text(
            "0 @I1@ INDI\n1 NAME Anna /Beispiel/\n"
            "1 BIRT\n2 DATE 1901\n0 TRLR\n"
        )
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        orchestrator.run()
        (doc,) = orchestrator.documents()
        saved = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert saved.extraction_run.model_identifier == "gedcom-adapter"
        assert saved.entities[0].label == "Anna Beispiel"

    def test_slugify_keeps_paths_safe(self):
        assert slugify("Hirsch Family Tree") == "Hirsch-Family-Tree"
        assert "/" not in slugify("weird/name")
        assert slugify("..") == "document"


class TestContentDedup:
    """ADR-0010: identical bytes are one artifact — a re-encoded or
    renamed copy in the same folder is not processed twice."""

    def _folder(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        return folder

    def test_identical_files_processed_once(self, tmp_path):
        folder = self._folder(tmp_path)
        (folder / "a-original.txt").write_text("same bytes")
        (folder / "b-copy.txt").write_text("same bytes")  # identical content
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        orchestrator.run()
        original, copy = orchestrator.documents()
        # the alphabetically-first file is canonical; only it is worked on
        assert "register:a-original.txt" in orchestrator.calls
        assert "manifest:a-original.txt" in orchestrator.calls
        assert not any(c.endswith("b-copy.txt") for c in orchestrator.calls)
        assert copy.state.status == DUPLICATE
        assert copy.state.detail["duplicate_of"] == "a-original.txt"

    def test_distinct_content_both_processed(self, tmp_path):
        folder = self._folder(tmp_path)
        (folder / "one.txt").write_text("first document")
        (folder / "two.txt").write_text("second document")
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        orchestrator.run()
        assert "manifest:one.txt" in orchestrator.calls
        assert "manifest:two.txt" in orchestrator.calls
        assert all(
            d.state.status != DUPLICATE for d in orchestrator.documents()
        )

    def test_dedup_decision_is_stable_across_reruns(self, tmp_path):
        folder = self._folder(tmp_path)
        (folder / "a-original.txt").write_text("same bytes")
        (folder / "b-copy.txt").write_text("same bytes")
        first = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        first.run()
        # a fresh orchestrator (new run) must reach the same verdict, and
        # must not re-do the canonical's expensive steps
        second = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        second.run()
        assert not any(c.endswith("b-copy.txt") for c in second.calls)
        _, copy = second.documents()
        assert copy.state.status == DUPLICATE

    def _complete(self, orchestrator):
        """Drive the run's single document all the way to WRITTEN."""
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN

    def test_cross_run_identical_file_skips_extraction(self, tmp_path):
        # a file fully ingested in a prior run, re-encountered under a new
        # name in a later run, must be recognised before any LLM step
        folder = self._folder(tmp_path)
        (folder / "original.txt").write_text("Anna was born in 1901.")
        first = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        self._complete(first)

        (folder / "renamed-scan.txt").write_text("Anna was born in 1901.")
        second = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        second.run()
        original, renamed = second.documents()
        assert renamed.source.name == "renamed-scan.txt"
        assert renamed.state.status == DUPLICATE
        assert renamed.state.detail["duplicate_of"] == "original.txt"
        # the copy is never registered, transcribed, or extracted
        assert not any(c.endswith("renamed-scan.txt") for c in second.calls)

    def test_cross_run_skip_needs_a_completed_prior(self, tmp_path):
        # an UNFINISHED prior document must not block a fresh identical
        # file from completing the ingestion
        folder = self._folder(tmp_path)
        (folder / "stalled.txt").write_text("Anna was born in 1901.")
        first = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        first.run()  # stops at the equivalence gate, never WRITTEN
        (stalled,) = first.documents()
        assert stalled.state.status == AWAITING_EQUIVALENCES

        (folder / "fresh.txt").write_text("Anna was born in 1901.")
        second = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        second.run()
        # nothing WRITTEN yet, so both are live; in-run dedup still keeps
        # the alphabetically-first ("fresh") canonical and the other a dup
        by_name = {d.source.name: d for d in second.documents()}
        assert by_name["fresh.txt"].state.status != DUPLICATE
        # it advances through the pipeline; the identical bytes reuse the
        # stored extraction instead of calling the model again (#87)
        assert "reconcile:fresh.txt" in second.calls
        assert "manifest:fresh.txt" not in second.calls

    def test_changed_content_forces_reingest(self, tmp_path):
        folder = self._folder(tmp_path)
        source = folder / "record.txt"
        source.write_text("Anna was born in 1901.")
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        self._complete(orchestrator)

        source.write_text("Bernard was born in 1880.")  # same name, new bytes
        again = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        again.run()
        (doc,) = again.documents()
        # the stale WRITTEN state was discarded and the document re-worked
        assert "register:record.txt" in again.calls
        assert "manifest:record.txt" in again.calls
        assert doc.state.status != DUPLICATE


class TestFreshness:
    """Make-style dependencies by content hash: upstream redos invalidate
    downstream outputs AND the human gates (a corrected transcription must
    force re-extraction, re-review, re-write), and an identical recompute
    invalidates nothing. A directory that has lost its derivations.json
    while the stage store still holds its stages is adopted once, by
    modification times (#87)."""

    def completed_run(self, tmp_path):
        return completed_run(tmp_path)

    def test_corrected_transcription_invalidates_derived_files_and_gates(
        self, tmp_path
    ):
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.correction.write_text("Klara was born in 1917.")
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        # stale reviewed files did NOT open the gates
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert not doc.resolved.exists()
        assert not doc.write_result.exists()
        # ...and the human decisions were moved aside, not deleted
        assert not doc.equivalences_reviewed.exists()
        assert not doc.review_reviewed.exists()
        assert (doc.dir / "equivalences.reviewed.superseded-1.csv").exists()
        assert (doc.dir / "review.reviewed.superseded-1.csv").exists()
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert batch.entities[0].label == "Klara"
        assert json.loads(doc.transcription.read_text())["engine"] == (
            "correction"
        )

    def test_identical_recompute_invalidates_nothing(self, tmp_path):
        """Early cutoff: a correction that says what the source says is a
        new transcription with the same content, so nothing downstream is
        recomputed and every gate stays open."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.correction.write_text(doc.source.read_text())
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert orchestrator.calls == before  # no extraction, no write
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        latest = {
            event["stage"]: event["event"]
            for event in orchestrator.rundir.events()
            if event["event"] in ("stored", "reused")
        }
        assert latest["transcription"] == "stored"
        assert latest["entity_manifest"] == "reused"
        assert latest["critique"] == "reused"

    def test_repeated_redo_keeps_every_superseded_review(self, tmp_path):
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.correction.write_text("Klara was born in 1917.")
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        doc.correction.write_text("Gustav was born in 1919.")  # a second one
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert (doc.dir / "equivalences.reviewed.superseded-1.csv").exists()
        assert (doc.dir / "equivalences.reviewed.superseded-2.csv").exists()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_rebuilt_batch_view_invalidates_nothing(self, tmp_path):
        """A deleted view is rebuilt from the store with the same content,
        so the reviews and the write it fed stay valid."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.batch.unlink()
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert orchestrator.calls == before
        (doc,) = orchestrator.documents()
        assert doc.batch.exists()
        assert doc.review_reviewed.exists()

    def test_a_directory_the_store_records_keeps_its_files(self, tmp_path):
        """A directory that has lost its derivations.json, while the stage
        store still holds its stages, keeps its derived files and reviews
        with no calls: the stages are reused by key, and each derived file
        is no older than what it was made from, so it is adopted as
        current, once. Without those store records the stages would be
        computed again, so a directory from before the records is held
        instead (TestUnrecordedDirectory)."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert orchestrator.calls == before
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert doc.review_reviewed.exists()
        assert not (doc.dir / "review.reviewed.superseded-1.csv").exists()

    def stale_review(self, tmp_path):
        """A directory waiting at the claim gate that has lost its
        derivations.json, while the store still holds its stages, and
        whose claim review is older than its CSV: the CSV was re-exported
        with other rows after the review was saved."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_claims(doc)
        export_review_csv(make_batch(), doc.review)
        saved_last(doc.review)
        (doc.dir / "derivations.json").unlink()
        return orchestrator

    def test_adoption_moves_aside_a_review_older_than_its_csv(self, tmp_path):
        """A claim review older than its CSV decided on rows that have
        since been replaced. Adoption does not bind it: it is moved aside,
        the gate waits, and nothing is written until the current rows are
        reviewed. The store holds the directory's stages, so the document
        is adopted rather than held, and no stage is computed."""
        orchestrator = self.stale_review(tmp_path)
        before = len(orchestrator.calls)
        assert orchestrator.run() == 2
        assert orchestrator.calls[before:] == []
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert not doc.write_result.exists()
        assert not doc.review_reviewed.exists()
        assert (doc.dir / "review.reviewed.superseded-1.csv").exists()
        # a review of the CSV as it is now opens the gate
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        assert [c.split(":")[0] for c in orchestrator.calls[before:]] == [
            "write"
        ]

    def test_an_interrupted_adoption_adopts_again(self, tmp_path, monkeypatch):
        """An adoption (of a directory whose stages the store holds) that
        stops before its record is saved leaves none, so the next run
        adopts again, and a review it judged stale never comes to
        consent."""
        import yizkr.pipeline.orchestrate as orchestrate

        orchestrator = self.stale_review(tmp_path)

        def refuse(reviewed):
            raise OSError("the disk refused")

        monkeypatch.setattr(orchestrate, "_supersede", refuse)
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.state.status == FAILED
        assert not (doc.dir / "derivations.json").exists()
        monkeypatch.undo()
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert not any(c.startswith("write") for c in orchestrator.calls)
        assert (doc.dir / "review.reviewed.superseded-1.csv").exists()

    def test_adoption_re_resolves_after_a_newer_equivalence_review(
        self, tmp_path
    ):
        """In a directory that has lost its derivations.json, while the
        store still holds its stages, an equivalence review saved again
        after the resolved batch was made is newer than it, so the
        resolved batch is not adopted: it is resolved again with the
        curator's new decisions, and the claim review of the old
        resolution is moved aside instead of written."""

        class Resolves(ScriptedOrchestrator):
            """Resolves with the reviewed equivalence decisions, as the
            real step does before its graph lookups."""

            def _step_resolve(self, doc, batch):
                self.calls.append(f"resolve:{doc.source.name}")
                return apply_equivalences(
                    batch,
                    read_approved_equivalences(doc.equivalences_reviewed),
                )

        first = make_run(tmp_path)
        orchestrator = Resolves(first.folder, first.state_dir, log=quiet)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        resolved = ClaimBatch.model_validate_json(doc.resolved.read_text())
        assert resolved.claims[0].subject.kind == "qid"
        # the curator saves the equivalence review again, rejecting the match
        decide_all_equivalences(doc, "reject")
        saved_last(doc.equivalences_reviewed)
        (doc.dir / "derivations.json").unlink()
        before = len(orchestrator.calls)
        assert orchestrator.run() == 2
        assert [c.split(":")[0] for c in orchestrator.calls[before:]] == [
            "resolve",
            "export",
        ]
        (doc,) = orchestrator.documents()
        resolved = ClaimBatch.model_validate_json(doc.resolved.read_text())
        assert resolved.claims[0].subject.kind == "local"
        assert doc.state.status == AWAITING_CLAIMS
        assert not doc.review_reviewed.exists()
        assert (doc.dir / "review.reviewed.superseded-1.csv").exists()

    def test_after_adoption_modification_times_are_not_read(self, tmp_path):
        """Adoption (here of a directory whose stages the store holds)
        happens once. After it, a CSV that is only touched, not changed,
        re-arms nothing: content hashes decide."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        saved_last(doc.review)
        saved_last(doc.equivalences)
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert orchestrator.calls == before
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert doc.review_reviewed.exists()
        assert doc.equivalences_reviewed.exists()

    def test_edited_csv_rearms_its_gate(self, tmp_path):
        """A reviewed file is consent for the CSV it was first seen with:
        once that CSV changes, the gate waits for a new review."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS  # gate 1 passed
        with open(doc.equivalences, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS).writerow(
                {"entity_id": "e1", "qid": "Q77", "verdict": "same"}
            )
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        # saving a fresh review of the edited CSV reopens it
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS

    def test_changed_reexport_moves_the_review_aside(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        # the CSV is regenerated with different rows
        doc.equivalences.unlink()
        orchestrator.proposals = 2
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert not doc.equivalences_reviewed.exists()
        assert (doc.dir / "equivalences.reviewed.superseded-1.csv").exists()

    def test_identical_reexport_keeps_the_review(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()  # the review is seen, and bound to its CSV
        doc.equivalences.unlink()  # regenerated with the same rows
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert doc.equivalences_reviewed.exists()

    def test_re_reviewed_claims_rewrite(self, tmp_path):
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        writes_before = len(
            [c for c in orchestrator.calls if c.startswith("write")]
        )
        with open(doc.review_reviewed, newline="") as f:
            rows = list(csv.DictReader(f))
        rows[0]["notes"] = "checked against the register"
        with open(doc.review_reviewed, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        assert orchestrator.run() == 0
        writes_after = len(
            [c for c in orchestrator.calls if c.startswith("write")]
        )
        assert writes_after == writes_before + 1


class TestUnrecordedDirectory:
    """A document directory from before the stage records: its stage
    outputs (transcription.json, batch.json, critique.json) and reviews
    are in place, but it has no derivations.json and the stage store holds
    none of its stages. Deriving them again would call the engine and,
    when a model answers differently, move the reviews aside, so the run
    holds the document until it opts in with --rederive-unrecorded (#87).
    """

    VIEWS = ["transcription.json", "batch.json", "critique.json"]

    def from_before_the_records(self, tmp_path):
        """A written document's directory as a run before the records left
        it: every file in place, no derivations.json, and no stage store."""
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        shutil.rmtree(orchestrator.store.root)
        return orchestrator

    def test_a_directory_from_before_the_records_is_held(self, tmp_path):
        """No stage runs and no file in the directory changes. The hold
        and its reason are in state.json, a held event, an alert, the log
        and the exit code, on every run until one opts in."""
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        files = snapshot(doc.dir)
        calls = list(orchestrator.calls)
        lines: list[str] = []
        orchestrator.log = collect(lines)
        assert orchestrator.run() == 2
        # no wiki, engine or gate step, and no stage output stored
        assert orchestrator.calls == calls
        assert not orchestrator.store.root.exists()
        assert snapshot(doc.dir) == files
        (doc,) = orchestrator.documents()
        assert doc.state.status == UNRECORDED
        held = doc.state.detail["held"]
        assert held["outputs"] == self.VIEWS
        assert held["previous_status"] == WRITTEN
        assert "--rederive-unrecorded" in held["reason"]
        assert "calling the engine" in held["reason"]
        assert any(f"held: {held['reason']}" in line for line in lines)
        (event,) = [
            e for e in orchestrator.rundir.events() if e["event"] == "held"
        ]
        assert (event["subject"], event["stage"]) == (
            doc.sha1,
            "transcription",
        )
        assert "unrecorded outputs" in event["reason"]
        (alert,) = orchestrator.rundir.alerts()
        assert alert["check"] == "unrecorded outputs"
        assert alert["value"] == self.VIEWS
        assert doc.sha1.startswith(alert["subject"])
        state = run_state(orchestrator.rundir, orchestrator.store)[
            (doc.sha1, "transcription", None)
        ]
        assert (state.status, state.reason) == ("pending", event["reason"])
        # the next run holds it again, and still touches nothing
        assert orchestrator.run() == 2
        assert orchestrator.calls == calls
        assert snapshot(doc.dir) == files
        (doc,) = orchestrator.documents()
        assert doc.state.detail["held"]["previous_status"] == WRITTEN
        assert len(orchestrator.rundir.alerts()) == 2

    def test_rederiving_moves_the_reviews_aside_when_the_batch_differs(
        self, tmp_path
    ):
        """With --rederive-unrecorded every stage is derived again. A
        model's second answer can differ from its first; when the batch
        does, what was made from the old one is derived again, and the
        reviews are moved aside, never deleted."""
        first = self.from_before_the_records(tmp_path)
        assert first.run() == 2  # held
        lines: list[str] = []
        orchestrator = DriftingOrchestrator(
            first.folder,
            first.state_dir,
            log=collect(lines),
            rederive_unrecorded=True,
        )
        assert orchestrator.run() == 2
        assert [c.split(":")[0] for c in orchestrator.calls] == [
            "manifest",
            "chunk",
            "critique",
            "reconcile",
        ]
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert "held" not in doc.state.detail
        assert not doc.write_result.exists()
        assert not doc.equivalences_reviewed.exists()
        assert not doc.review_reviewed.exists()
        assert (doc.dir / "equivalences.reviewed.superseded-1.csv").exists()
        assert (doc.dir / "review.reviewed.superseded-1.csv").exists()
        assert any("no record of" in line for line in lines)
        assert any(
            "equivalences.reviewed.csv, review.reviewed.csv moved aside"
            in line
            for line in lines
        )
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert batch.claims[0].value.year == 1902

    def test_rederiving_the_same_batch_keeps_the_reviews(self, tmp_path):
        """Stages derived again that give the directory's batch back
        change nothing after it: the hold is over, the reviews stay bound,
        the write stands, and from then on the directory is recorded."""
        first = self.from_before_the_records(tmp_path)
        assert first.run() == 2  # held
        orchestrator = ScriptedOrchestrator(
            first.folder, first.state_dir, log=quiet, rederive_unrecorded=True
        )
        # the extract-only pass (serial mode's first) reports the batch
        (doc,) = orchestrator.documents()
        assert orchestrator.advance(doc, stop_after_extract=True) == BATCHED
        assert orchestrator.run() == 0
        assert [c.split(":")[0] for c in orchestrator.calls] == [
            "manifest",
            "chunk",
            "critique",
        ]
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert "held" not in doc.state.detail
        assert doc.equivalences_reviewed.exists()
        assert doc.review_reviewed.exists()
        assert not list(doc.dir.glob("*.superseded-*"))
        again = ScriptedOrchestrator(first.folder, first.state_dir, log=quiet)
        assert again.run() == 0
        assert again.calls == []

    def test_a_directory_that_lost_its_state_is_held_too(self, tmp_path):
        """Without state.json the document has no artifact QID, but its
        stage outputs are still unrecorded: it is held before it is
        registered again, and nothing in the directory changes."""
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        doc.state_path.unlink()
        files = snapshot(doc.dir)
        calls = list(orchestrator.calls)
        assert orchestrator.run() == 2
        assert orchestrator.calls == calls
        assert snapshot(doc.dir) == files
        (doc,) = orchestrator.documents()
        assert doc.state.status == UNRECORDED
        assert doc.state.artifact_qid is None

    def test_a_first_run_that_stopped_before_its_batch_is_not_held(
        self, tmp_path
    ):
        """A first run that fails before the batch leaves transcription.json
        and no derivations.json. The store holds that transcription, so
        the next run carries on instead of holding the document."""
        orchestrator = make_run(tmp_path, fail_step="manifest")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.transcription.exists()
        assert not (doc.dir / "derivations.json").exists()
        orchestrator.fail_step = None
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_a_partly_recorded_directory_is_held_for_the_rest(self, tmp_path):
        """A store that holds some of a directory's stages, and not the
        stages behind batch.json and critique.json, would have those
        computed again: the document is held, naming those two."""
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        scope = orchestrator.store.root / "doc" / doc.sha1
        for stage in ("entity_manifest", "chunk", "batch", "critique"):
            shutil.rmtree(scope / stage)
        files = snapshot(doc.dir)
        before = list(orchestrator.calls)
        assert orchestrator.run() == 2
        assert orchestrator.calls == before
        assert snapshot(doc.dir) == files
        (doc,) = orchestrator.documents()
        assert doc.state.status == UNRECORDED
        assert doc.state.detail["held"]["outputs"] == [
            "batch.json",
            "critique.json",
        ]

    def test_serial_mode_reports_a_hold_once(self, tmp_path):
        """Serial mode advances a document twice in one run (extract-only,
        then in sequence); a held document is reported once."""
        first = self.from_before_the_records(tmp_path)
        orchestrator = ScriptedOrchestrator(
            first.folder, first.state_dir, log=quiet, serial=True
        )
        assert orchestrator.run() == 2
        assert orchestrator.calls == []
        events = orchestrator.rundir.events()
        assert [e["event"] for e in events].count("held") == 1
        assert len(orchestrator.rundir.alerts()) == 1
        (doc,) = orchestrator.documents()
        assert doc.state.status == UNRECORDED

    def test_a_copy_of_a_held_written_document_is_a_duplicate(self, tmp_path):
        """A document held after it was written is still the one ingested
        for its bytes (ADR-0010): a renamed copy is a duplicate, and is
        never registered, transcribed or extracted."""
        orchestrator = self.from_before_the_records(tmp_path)
        assert orchestrator.run() == 2
        folder = orchestrator.folder
        (folder / "a-copy.txt").write_bytes(
            (folder / "letter.txt").read_bytes()
        )
        again = ScriptedOrchestrator(folder, orchestrator.state_dir, log=quiet)
        assert again.run() == 2
        assert again.calls == []
        assert {d.source.name: d.state.status for d in again.documents()} == {
            "a-copy.txt": DUPLICATE,
            "letter.txt": UNRECORDED,
        }

    def test_a_gedcom_directory_is_held_only_without_its_stage(self, tmp_path):
        """A GEDCOM document's one stage output is batch.json, from the
        gedcom stage, which calls no engine. With the store holding that
        stage, a lost derivations.json is adopted; without it the document
        is held."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "family.ged").write_text(
            "0 @I1@ INDI\n1 NAME Klara /Löbig/\n1 BIRT\n2 DATE 1917\n0 TRLR\n"
        )
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        (doc.dir / "derivations.json").unlink()
        shutil.rmtree(orchestrator.store.root)
        files = snapshot(doc.dir)
        assert orchestrator.run() == 2
        assert snapshot(doc.dir) == files
        (doc,) = orchestrator.documents()
        assert doc.state.status == UNRECORDED
        held = doc.state.detail["held"]
        assert held["outputs"] == ["batch.json"]
        assert "calling the engine" not in held["reason"]
        events = orchestrator.rundir.events()
        assert [e["stage"] for e in events if e["event"] == "held"] == [
            "gedcom"
        ]

    def test_ingest_run_opts_in_by_flag(self, tmp_path, monkeypatch):
        from yizkr.cli import build_parser
        from yizkr.core.corpus import CORPUS_DIR_VAR, EXCLUDE_FILE_VAR

        listed = tmp_path / "exclude.txt"
        listed.write_text("")
        env_file = tmp_path / "test.env"
        env_file.write_text(f"{EXCLUDE_FILE_VAR}={listed}\n")
        monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
        monkeypatch.delenv(EXCLUDE_FILE_VAR, raising=False)
        monkeypatch.delenv(CORPUS_DIR_VAR, raising=False)
        folder = tmp_path / "docs"
        folder.mkdir()
        seen = []
        monkeypatch.setattr(
            Orchestrator,
            "run",
            lambda self: seen.append(self.rederive_unrecorded) or 0,
        )
        state = str(tmp_path / "state")
        for flags in ([], ["--rederive-unrecorded"]):
            args = build_parser().parse_args(
                ["ingest", "run", str(folder), "--state", state, *flags]
            )
            assert args.func(args) == 0
        assert seen == [False, True]


class TestStageStore:
    """The stages before the gates read and write through the stage store,
    and the run directory records what each run did (#87)."""

    def test_second_run_reuses_every_stage(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        first = len(orchestrator.rundir.events())
        orchestrator.run()
        second = [
            event
            for event in orchestrator.rundir.events()[first:]
            if event.get("stage")
        ]
        assert {event["event"] for event in second} == {"reused"}
        assert {event["reason"] for event in second} == {"key match"}
        assert {event["stage"] for event in second} == {
            "transcription",
            "entity_manifest",
            "chunk",
            "batch",
            "critique",
        }

    def test_run_state_is_derived_from_events_and_store(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        states = run_state(orchestrator.rundir, orchestrator.store)
        by_stage = {
            stage: state.status for (_, stage, _), state in states.items()
        }
        assert by_stage == {
            "transcription": "done",
            "entity_manifest": "done",
            "chunk": "done",
            "batch": "done",
            "critique": "done",
        }
        assert {subject for subject, _, _ in states} == {doc.sha1}
        orchestrator.run()
        states = run_state(orchestrator.rundir, orchestrator.store)
        assert {state.status for state in states.values()} == {"reused"}

    def test_failed_stage_retries_and_keeps_its_reason(self, tmp_path):
        orchestrator = make_run(tmp_path, fail_step="critique")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.state.status == FAILED
        states = run_state(orchestrator.rundir, orchestrator.store)
        critique = states[(doc.sha1, "critique", None)]
        assert critique.status == "failed"
        assert "critique engine down" in critique.reason
        # the stages before it are stored and are not redone
        orchestrator.fail_step = None
        orchestrator.calls.clear()
        assert orchestrator.run() == 2
        assert [c.split(":")[0] for c in orchestrator.calls] == [
            "critique",
            "reconcile",
        ]
        states = run_state(orchestrator.rundir, orchestrator.store)
        assert states[(doc.sha1, "critique", None)].status == "done"
        failures = orchestrator.store.failures(f"doc/{doc.sha1}", "critique")
        assert "critique engine down" in failures[0]["reason"]

    def test_recompute_forces_one_stage(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        again = ScriptedOrchestrator(
            orchestrator.folder,
            orchestrator.state_dir,
            log=quiet,
            recompute=["critique"],
        )
        again.run()
        assert [c.split(":")[0] for c in again.calls] == ["critique"]
        again.run()  # once per run: a second pass reuses it
        assert [c.split(":")[0] for c in again.calls] == ["critique"]

    def test_stages_share_a_named_store_across_state_dirs(self, tmp_path):
        store = tmp_path / "stages"
        first = make_run(tmp_path, store_dir=store)
        first.run()
        second = ScriptedOrchestrator(
            first.folder, tmp_path / "other-state", log=quiet, store_dir=store
        )
        second.run()
        assert not any(
            c.startswith(("manifest", "chunk", "critique"))
            for c in second.calls
        )

    def test_a_held_stage_pauses_the_document(self, tmp_path):
        """A pause request for a stage stops its dispatch; nothing fails,
        completed outputs stay stored, and the next run resumes."""

        class PausesCritique(ScriptedOrchestrator):
            def _step_register(self, doc):
                self.rundir.request("pause", stage="critique", by="test")
                return super()._step_register(doc)

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "letter.txt").write_text("Rudolf was born in 1916.")
        orchestrator = PausesCritique(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == PAUSED
        assert "critique" not in [c.split(":")[0] for c in orchestrator.calls]
        states = run_state(orchestrator.rundir, orchestrator.store)
        assert states[(doc.sha1, "batch", None)].status == "done"
        assert states[(doc.sha1, "critique", None)].status == "pending"
        # a new invocation starts clear of the old requests
        resumed = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        assert resumed.run() == 2
        assert [c.split(":")[0] for c in resumed.calls] == [
            "critique",
            "reconcile",
        ]

    def test_run_abort_stops_dispatch(self, tmp_path):
        class AbortsAfterFirst(ScriptedOrchestrator):
            def _step_register(self, doc):
                self.rundir.request("abort", by="test", reason="futile")
                return super()._step_register(doc)

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("Ursula was born in 1911.")
        (folder / "b.txt").write_text("Leopold was born in 1914.")
        orchestrator = AbortsAfterFirst(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        assert not any(c.endswith("b.txt") for c in orchestrator.calls)
        events = [e["event"] for e in orchestrator.rundir.events()]
        assert events[0] == "run-start" and events[-1] == "run-end"


class TestUnpinnedModel:
    """A key names only the model that configuration names. With none,
    the engine's own default model is outside the keys, and a run that
    calls the engine says so (#87, #133)."""

    @staticmethod
    def warnings(lines):
        return [line for line in lines if "no model is pinned" in line]

    def test_roles_without_a_model_are_unpinned(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YIZKR_LLM_MODEL", raising=False)
        orchestrator = make_run(tmp_path, extract_model="model-for-extraction")
        assert orchestrator.unpinned_roles() == ["transcribe", "critique"]
        monkeypatch.setenv("YIZKR_LLM_MODEL", "model-from-env")
        assert orchestrator.unpinned_roles() == []

    def test_a_run_warns_about_unpinned_roles(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YIZKR_LLM_MODEL", raising=False)
        lines: list[str] = []
        make_run(tmp_path, log=collect(lines)).run()
        (warning,) = self.warnings(lines)
        assert "transcribe, extract, critique" in warning
        assert "'engine default'" in warning

    def test_pinned_models_need_no_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YIZKR_LLM_MODEL", "model-from-env")
        lines: list[str] = []
        make_run(tmp_path, log=collect(lines)).run()
        assert self.warnings(lines) == []

    def test_a_run_without_engine_stages_needs_no_model(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("YIZKR_LLM_MODEL", raising=False)
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "family.ged").write_text(
            "0 @I1@ INDI\n1 NAME Klara /Löbig/\n1 BIRT\n2 DATE 1917\n0 TRLR\n"
        )
        lines: list[str] = []
        ScriptedOrchestrator(
            folder, tmp_path / "state", log=collect(lines)
        ).run()
        assert self.warnings(lines) == []


class TestTranscribeLanguage:
    """Whole-folder ingestion detects each document's language (offline,
    via the text-file engine — no LLM needed to exercise the wiring)."""

    def _doc(self, tmp_path, name, text):
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text)
        source = folder / name
        doc = DocumentRun(source, tmp_path / "state")
        doc.dir.mkdir(
            parents=True, exist_ok=True
        )  # register does this in the real flow
        doc.state.artifact_qid = "Q1"
        return doc

    def test_auto_detects_per_document(self, tmp_path):
        german = (
            "Berlin, den 8. Dezember 1937. Sehr geehrter Herr Direktor, "
            "hiermit bestaetige ich den Empfang Ihres Schreibens. Die "
            "Steuerangelegenheit ist geklaert und der Betrag beglichen."
        )
        english = (
            "I was born in Berlin in 1929, the youngest of three children. "
            "My father ran a small flour mill and my earliest memories are "
            "of the smell of grain and the turning of the great wheels."
        )
        # one orchestrator, default language (auto), two languages in a folder
        orch = Orchestrator(tmp_path / "docs", tmp_path / "state", log=quiet)
        de = orch._step_transcribe(self._doc(tmp_path, "brief.txt", german))
        en = orch._step_transcribe(self._doc(tmp_path, "memoir.txt", english))
        assert de.language == "de"
        assert en.language == "en"

    def test_explicit_language_forces_and_records_source(self, tmp_path):
        german = "Sehr geehrter Herr Direktor, die Steuerangelegenheit."
        orch = Orchestrator(
            tmp_path / "docs", tmp_path / "state", language="fr", log=quiet
        )
        doc = self._doc(tmp_path, "brief.txt", german)
        transcription = orch._step_transcribe(doc)
        assert transcription.language == "fr"  # forced, not detected
        assert doc.state.detail["language"]["source"] == "override"

    def test_short_document_uses_fallback(self, tmp_path):
        orch = Orchestrator(
            tmp_path / "docs",
            tmp_path / "state",
            language="auto",
            fallback_language="de",
            log=quiet,
        )
        doc = self._doc(tmp_path, "receipt.txt", "1937")  # too short to detect
        transcription = orch._step_transcribe(doc)
        assert transcription.language == "de"
        assert doc.state.detail["language"]["source"] == "fallback"


class TestRegisterArtifactType:
    """Suffix-specific artifact classes at registration (item 10)."""

    def test_ged_registers_as_genealogical_report(self, tmp_path, monkeypatch):
        import yizkr.pipeline.registrar as registrar

        seen = []

        class Result:
            artifact_qid = "Q1"

        def fake_register(client, **kwargs):
            seen.append(kwargs["artifact_type"])
            return Result()

        monkeypatch.setattr(registrar, "register_artifact", fake_register)
        source = tmp_path / "tree.ged"
        source.write_text("0 HEAD\n0 TRLR\n")
        orchestrator = Orchestrator(tmp_path, tmp_path / "state")
        orchestrator._client = object()  # the fake never touches the wiki
        doc = DocumentRun(source, tmp_path / "state")
        assert orchestrator._step_register(doc) == "Q1"
        assert seen == ["genealogical_report"]


class TestSerialMode:
    """--serial (ADR-0019): one artifact at a time, each written before the
    next reconciles, so cross-artifact identity resolves at the gates."""

    def _two_doc_folder(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("Anna was born in 1901.")
        (folder / "b.txt").write_text("Bella was born in 1902.")
        return ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet, serial=True
        )

    def test_extracts_all_then_reconciles_only_the_first(self, tmp_path):
        orch = self._two_doc_folder(tmp_path)
        assert orch.run() == 2
        docs = {d.source.name: d for d in orch.documents()}
        # every document is extracted (the sequencer needs the manifests)
        assert (
            "manifest:a.txt" in orch.calls and "manifest:b.txt" in orch.calls
        )
        # but only the first-in-sequence reconciles; the next stays queued
        assert "reconcile:a.txt" in orch.calls
        assert "reconcile:b.txt" not in orch.calls
        assert docs["a.txt"].state.status == AWAITING_EQUIVALENCES
        assert docs["b.txt"].state.status == BATCHED

    def test_next_reconciles_only_after_the_first_is_written(self, tmp_path):
        orch = self._two_doc_folder(tmp_path)
        orch.run()
        approve_all_equivalences(
            {d.source.name: d for d in orch.documents()}["a.txt"]
        )
        orch.run()  # a advances to its claim gate; b must NOT reconcile yet
        assert "reconcile:b.txt" not in orch.calls
        approve_all_claims(
            {d.source.name: d for d in orch.documents()}["a.txt"]
        )
        orch.run()  # a is written; only now does b reconcile
        docs = {d.source.name: d for d in orch.documents()}
        assert docs["a.txt"].state.status == WRITTEN
        assert docs["b.txt"].state.status == AWAITING_EQUIVALENCES
        assert "reconcile:b.txt" in orch.calls

    def test_default_mode_advances_every_document_in_one_pass(self, tmp_path):
        # contrast: without --serial, both documents reach the gate at once
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("Anna was born in 1901.")
        (folder / "b.txt").write_text("Bella was born in 1902.")
        orch = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        orch.run()
        assert "reconcile:a.txt" in orch.calls
        assert "reconcile:b.txt" in orch.calls

    def test_extract_concurrency_extracts_every_document(self, tmp_path):
        # --extract-concurrency parallelises the extract-all phase; all
        # documents must still be extracted, and the serial invariant holds.
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("Anna was born in 1901.")
        (folder / "b.txt").write_text("Bella was born in 1902.")
        orch = ScriptedOrchestrator(
            folder,
            tmp_path / "state",
            log=quiet,
            serial=True,
            extract_concurrency=2,
        )
        orch.run()
        assert (
            "manifest:a.txt" in orch.calls and "manifest:b.txt" in orch.calls
        )
        # only one document reconciles (the next waits for it to be written)
        reconciled = [c for c in orch.calls if c.startswith("reconcile:")]
        assert len(reconciled) == 1

    def test_extract_concurrency_still_dedups_identical_files(self, tmp_path):
        # ADR-0010 content-dedup must survive parallel extraction: two
        # identical files -> one artifact, one extraction.
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("identical bytes")
        (folder / "b.txt").write_text("identical bytes")
        orch = ScriptedOrchestrator(
            folder,
            tmp_path / "state",
            log=quiet,
            serial=True,
            extract_concurrency=2,
        )
        orch.run()
        statuses = {d.source.name: d.state.status for d in orch.documents()}
        assert DUPLICATE in statuses.values()
        assert len([c for c in orch.calls if c.startswith("manifest:")]) == 1


class TestNeverIngestList:
    """#29: a run refuses to start without a never-ingest list, and a
    listed document is dropped at discovery, before any step reads it."""

    def _folder(self, tmp_path):
        from tests.fixtures import example_universe as u

        sources = {source.key: source for source in u.SOURCES}
        folder = tmp_path / "docs"
        folder.mkdir()
        postcard = sources["postcard-1938"].text
        (folder / "postcard-1938.txt").write_text(postcard)
        (folder / "postcard copy.txt").write_text(postcard)  # same bytes
        (folder / "Flörburg address book.txt").write_text(
            sources["directory-1912"].text
        )
        (folder / "address book.docx").write_text("unsupported and listed")
        (folder / "naturalisation-1952.txt").write_text(
            sources["naturalisation-1952"].text
        )
        return folder

    def test_no_list_no_run(self, tmp_path):
        folder = self._folder(tmp_path)
        orch = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet, exclusions=None
        )
        with pytest.raises(ExclusionConfigError):
            orch.run()
        assert orch.calls == []
        assert not (tmp_path / "state").exists()

    def test_the_empty_list_runs_everything(self, tmp_path):
        folder = self._folder(tmp_path)
        orch = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        assert [d.source.name for d in orch.documents()] == [
            "Flörburg address book.txt",
            "naturalisation-1952.txt",
            "postcard copy.txt",
            "postcard-1938.txt",
        ]

    def test_listed_documents_are_never_touched(self, tmp_path):
        import hashlib

        folder = self._folder(tmp_path)
        postcard_sha1 = hashlib.sha1(
            (folder / "postcard-1938.txt").read_bytes()
        ).hexdigest()
        listed = ExclusionList.parse(f"{postcard_sha1}\n*address*book*\n")
        logged = []
        orch = ScriptedOrchestrator(
            folder,
            tmp_path / "state",
            exclusions=listed,
            log=lambda *a, **k: logged.append(" ".join(map(str, a))),
        )
        orch.run()
        # the hash excludes both names for those bytes; the glob excludes
        # the directory scan and the unsupported file alike
        excluded = [line for line in logged if "never-ingest" in line]
        assert len(excluded) == 4
        assert not any("unsupported" in line for line in logged)
        assert [c for c in orch.calls if c.startswith("register:")] == [
            "register:naturalisation-1952.txt"
        ]
        # document directories only: the store and run files sit beside
        assert sorted(
            p.name
            for p in (tmp_path / "state").iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ) == ["naturalisation-1952"]
        assert [d.source.name for d in orch.documents()] == [
            "naturalisation-1952.txt"
        ]
