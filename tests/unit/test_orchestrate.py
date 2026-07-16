"""Offline tests for folder orchestration (ROADMAP item 15).

The heavy steps are replaced with scripted fakes; what is under test is
the state machine: per-document state, the two human gates, resume, and
per-document failure isolation.
"""

from __future__ import annotations

import csv
import json

from yizkr.claims import ClaimBatch
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    AWAITING_EQUIVALENCES,
    DUPLICATE,
    FAILED,
    WRITTEN,
    DocumentRun,
    Orchestrator,
    slugify,
)
from yizkr.pipeline.reconcile import EQUIVALENCE_FIELDS
from yizkr.pipeline.review import CSV_COLUMNS, export_review_csv

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
                    "subject": {"kind": "local", "local_id": "e1", "label": "Anna"},
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


class ScriptedOrchestrator(Orchestrator):
    """Every wiki/LLM-touching step replaced with a scripted fake."""

    def __init__(self, *args, proposals=1, fail_step=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.proposals = proposals
        self.fail_step = fail_step
        self.calls: list[str] = []

    def _step_register(self, doc):
        self.calls.append(f"register:{doc.source.name}")
        if self.fail_step == "register":
            raise RuntimeError("wiki down")
        return "Q100"

    def _step_batch(self, doc):
        self.calls.append(f"batch:{doc.source.name}")
        if doc.source.suffix == ".ged":
            return super()._step_batch(doc)  # exercises the real adapter
        return make_batch()

    def _step_critique(self, doc, batch):
        # ADR-0013's extraction critic invokes the LLM; the offline
        # state-machine tests stub it out, like every other heavy step,
        # so they never reach for a client (CI has none).
        self.calls.append(f"critique:{doc.source.name}")
        return []

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


def quiet(*args, **kwargs):
    pass


def make_run(tmp_path, name="letter.txt", **kwargs):
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text("Anna was born in 1901.")
    orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet, **kwargs)
    return orchestrator


def approve_all_equivalences(doc: DocumentRun) -> None:
    with open(doc.equivalences, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["decision"] = "approve"
    with open(doc.equivalences_reviewed, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def approve_all_claims(doc: DocumentRun) -> None:
    with open(doc.review, newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["decision"] = "approve"
    with open(doc.review_reviewed, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


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
        assert not any(call.startswith(("register", "batch")) for call in repeated)

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

    def test_unsupported_files_are_skipped(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "notes.txt").write_text("text")
        (folder / "data.xlsx").write_text("not a document")
        orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        assert [d.source.name for d in orchestrator.documents()] == ["notes.txt"]

    def test_gedcom_routes_through_deterministic_adapter(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "family.ged").write_text(
            "0 @I1@ INDI\n1 NAME Anna /Beispiel/\n1 BIRT\n2 DATE 1901\n0 TRLR\n"
        )
        orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
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
        orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        original, copy = orchestrator.documents()
        # the alphabetically-first file is canonical; only it is worked on
        assert "register:a-original.txt" in orchestrator.calls
        assert "batch:a-original.txt" in orchestrator.calls
        assert not any(c.endswith("b-copy.txt") for c in orchestrator.calls)
        assert copy.state.status == DUPLICATE
        assert copy.state.detail["duplicate_of"] == "a-original.txt"

    def test_distinct_content_both_processed(self, tmp_path):
        folder = self._folder(tmp_path)
        (folder / "one.txt").write_text("first document")
        (folder / "two.txt").write_text("second document")
        orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        orchestrator.run()
        assert "batch:one.txt" in orchestrator.calls
        assert "batch:two.txt" in orchestrator.calls
        assert all(d.state.status != DUPLICATE for d in orchestrator.documents())

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
        assert "batch:fresh.txt" in second.calls

    def test_changed_content_forces_reingest(self, tmp_path):
        folder = self._folder(tmp_path)
        source = folder / "record.txt"
        source.write_text("Anna was born in 1901.")
        orchestrator = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        self._complete(orchestrator)

        source.write_text("Bernard was born in 1880.")  # same name, new bytes
        again = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        again.run()
        (doc,) = again.documents()
        # the stale WRITTEN state was discarded and the document re-worked
        assert "register:record.txt" in again.calls
        assert "batch:record.txt" in again.calls
        assert doc.state.status != DUPLICATE


class TestFreshness:
    """Make-style dependencies: upstream redos invalidate downstream
    outputs AND the human gates (found by the second trial — a corrected
    transcription must force re-extraction, re-review, re-write)."""

    def completed_run(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        return orchestrator

    def test_upstream_redo_invalidates_derived_files_and_gates(self, tmp_path):
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.batch.unlink()  # curator corrected the transcription upstream
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        # stale reviewed files did NOT open the gates
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert not doc.resolved.exists()
        assert not doc.write_result.exists()

    def test_stale_reviewed_file_is_not_consent(self, tmp_path):
        import os

        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        # the reviewed file predates a regenerated CSV
        past = doc.equivalences.stat().st_mtime + 10
        os.utime(doc.equivalences, (past, past))
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_re_reviewed_claims_rewrite(self, tmp_path):
        import os

        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        writes_before = len([c for c in orchestrator.calls if c.startswith("write")])
        future = doc.write_result.stat().st_mtime + 10
        os.utime(doc.review_reviewed, (future, future))
        assert orchestrator.run() == 0
        writes_after = len([c for c in orchestrator.calls if c.startswith("write")])
        assert writes_after == writes_before + 1


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
