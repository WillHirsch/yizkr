"""Offline tests for folder orchestration (ROADMAP item 15).

The wiki steps are replaced with scripted fakes and the engine with a
scripted model, called directly: these tests run without the LLM cache
(#225), which tests/unit/test_llmcache.py takes the orchestrator through.
What is under test is the state machine: per-document state, the two
human gates, derivation by content hash, the set-aside of a directory
whose source changed or that has no record, the run directory, and
per-document failure isolation.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import re
from pathlib import Path

import pytest

from yizkr.claims import ClaimBatch
from yizkr.core.corpus import ExclusionConfigError, ExclusionList
from yizkr.pipeline import transcribe
from yizkr.pipeline.orchestrate import (
    AWAITING_CLAIMS,
    AWAITING_EQUIVALENCES,
    BATCHED,
    DUPLICATE,
    FAILED,
    PAUSED,
    WRITTEN,
    DocumentRun,
    Orchestrator,
    slugify,
)
from yizkr.pipeline.reconcile import EQUIVALENCE_FIELDS
from yizkr.pipeline.review import CSV_COLUMNS, export_review_csv
from yizkr.pipeline.rundir import NEEDS_ATTENTION, run_state
from yizkr.pipeline.transcribe import MissingPrerequisite

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
                        "label": "Rahel",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1871},
                    "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
                }
            ],
            "entities": [{"id": "e1", "label": "Rahel", "kind": "human"}]
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


class ScriptedOrchestrator(Orchestrator):
    """Every wiki step replaced with a scripted fake, the engine with a
    scripted model that each step calls directly; transcription (text
    files), extraction and critique run for real."""

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
                        "entity_label": "Rahel",
                        "qid": f"Q{40 + index}",
                        "qid_label": "Rahel Löbig",
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


# the steps that touch the wiki or wait on a gate, as the scripted
# orchestrator logs them; the rest of its calls are the engine's
WIKI_STEPS = ("register", "reconcile", "resolve", "export", "write")


def steps(calls: list[str]) -> list[str]:
    return [call.split(":")[0] for call in calls]


def wiki_steps(calls: list[str]) -> list[str]:
    return [step for step in steps(calls) if step in WIKI_STEPS]


def quiet(*args, **kwargs):
    pass


def make_run(tmp_path, name="letter.txt", **kwargs):
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text("Rahel was born in 1871.")
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


def snapshot(directory, *, skip=("state.json",)) -> dict:
    """Every file under `directory` but those named in `skip` (its
    state.json, by default), with its bytes and modification time: a run
    that touches none of them leaves this unchanged."""
    return {
        path.relative_to(directory): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name not in skip
    }


def contents(directory) -> dict:
    """Every file under `directory` with its bytes: what a run that
    rewrites nothing, or rewrites it the same, leaves unchanged."""
    return {
        path.relative_to(directory): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def this_run(orchestrator, *names: str) -> list[dict]:
    """The events of these names the orchestrator's invocation wrote."""
    return [
        event
        for event in orchestrator.rundir.events()
        if event["event"] in names
        and event["invocation"] == orchestrator.rundir.invocation
    ]


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
        """A re-run reuses the document's views, whose inputs are
        unchanged (#225): it asks the engine nothing, even without a
        cache, and repeats no wiki or gate step."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        calls_after_first = list(orchestrator.calls)
        orchestrator.run()  # still waiting: nothing new should execute
        repeated = orchestrator.calls[len(calls_after_first) :]
        assert repeated == []
        reused = [event["stage"] for event in this_run(orchestrator, "reused")]
        assert reused == ["transcription", "extraction", "critique"]
        assert this_run(orchestrator, "started") == []

    def test_written_document_changes_nothing_on_a_rerun(self, tmp_path):
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        before = list(orchestrator.calls)
        files = contents(doc.dir)
        assert orchestrator.run() == 0
        assert wiki_steps(orchestrator.calls[len(before) :]) == []
        assert contents(doc.dir) == files

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
            "0 @I1@ INDI\n1 NAME Rahel /Löbig/\n1 BIRT\n2 DATE 1871\n0 TRLR\n"
        )
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        orchestrator.run()
        (doc,) = orchestrator.documents()
        saved = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert saved.extraction_run.model_identifier == "gedcom-adapter"
        assert saved.entities[0].label == "Rahel Löbig"

    def test_slugify_keeps_paths_safe(self):
        assert slugify("Lobig Family Tree") == "Lobig-Family-Tree"
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
        (folder / "original.txt").write_text("Rahel was born in 1871.")
        first = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        self._complete(first)

        (folder / "renamed-scan.txt").write_text("Rahel was born in 1871.")
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
        (folder / "stalled.txt").write_text("Rahel was born in 1871.")
        first = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        first.run()  # stops at the equivalence gate, never WRITTEN
        (stalled,) = first.documents()
        assert stalled.state.status == AWAITING_EQUIVALENCES

        (folder / "fresh.txt").write_text("Rahel was born in 1871.")
        second = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        second.run()
        # nothing WRITTEN yet, so both are live; in-run dedup still keeps
        # the alphabetically-first ("fresh") canonical and the other a dup
        by_name = {d.source.name: d for d in second.documents()}
        assert by_name["fresh.txt"].state.status != DUPLICATE
        assert by_name["stalled.txt"].state.status == DUPLICATE
        # it advances through the pipeline
        assert "reconcile:fresh.txt" in second.calls

    def test_changed_content_forces_reingest(self, tmp_path):
        folder = self._folder(tmp_path)
        source = folder / "record.txt"
        source.write_text("Rahel was born in 1871.")
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        self._complete(orchestrator)

        source.write_text("Felix was born in 1867.")  # same name, new bytes
        again = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        again.run()
        (doc,) = again.documents()
        # the stale WRITTEN state was set aside and the document re-worked
        assert "register:record.txt" in again.calls
        assert "manifest:record.txt" in again.calls
        assert doc.state.status != DUPLICATE

    @pytest.mark.parametrize(
        "recorded", [True, False], ids=["recorded", "from-before-the-records"]
    )
    def test_a_changed_source_sets_the_old_directory_aside(
        self, tmp_path, recorded
    ):
        """Different bytes are a different artifact, so the document starts
        over in a fresh directory. The old one, recorded or from before the
        records, is moved aside whole, reviews and state included, under a
        name no document slug can take; nothing in it is deleted."""
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        if not recorded:
            (doc.dir / "derivations.json").unlink()
        before = snapshot(doc.dir, skip=())
        doc.source.write_text("Klara was born in 1917.")
        lines: list[str] = []
        orchestrator.log = collect(lines)
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 2
        kept = doc.dir.with_name(".letter.superseded-1")
        assert snapshot(kept, skip=()) == before
        assert steps(orchestrator.calls[calls:]) == [
            "register",
            "manifest",
            "chunk",
            "critique",
            "reconcile",
        ]
        assert any(
            "content changed; the old directory is kept as "
            ".letter.superseded-1" in line
            for line in lines
        )
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert (doc.dir / "derivations.json").exists()
        assert not doc.equivalences_reviewed.exists()
        (event,) = this_run(orchestrator, "set-aside")
        assert event["reason"] == "content changed"
        assert event["kept"] == ".letter.superseded-1"
        # a second change keeps the first set-aside directory too
        doc.source.write_text("Gustav was born in 1919.")
        orchestrator.run()
        assert snapshot(kept, skip=()) == before
        assert doc.dir.with_name(".letter.superseded-2").is_dir()

    def test_a_set_aside_directory_claims_no_bytes(self, tmp_path):
        """A set-aside directory's state recorded bytes that are no longer
        its document's, so it seeds no content dedup in a later run: a
        copy of the old source is ingested, not called a duplicate."""
        orchestrator = completed_run(tmp_path)
        folder = orchestrator.folder
        old = (folder / "letter.txt").read_bytes()
        (folder / "letter.txt").write_text("Klara was born in 1917.")
        assert orchestrator.run() == 2
        (folder / "old-copy.txt").write_bytes(old)
        again = ScriptedOrchestrator(folder, orchestrator.state_dir, log=quiet)
        assert again.run() == 2
        assert "register:old-copy.txt" in again.calls
        statuses = {d.source.name: d.state.status for d in again.documents()}
        assert statuses["old-copy.txt"] == AWAITING_EQUIVALENCES

    def test_a_changed_source_claims_no_bytes_in_its_run(self, tmp_path):
        """In the run that sets a directory aside, the bytes it recorded
        seed no content dedup either: a copy of the old source added in
        the same run is ingested."""
        orchestrator = completed_run(tmp_path)
        folder = orchestrator.folder
        old = (folder / "letter.txt").read_bytes()
        (folder / "letter.txt").write_text("Klara was born in 1917.")
        (folder / "old-copy.txt").write_bytes(old)
        again = ScriptedOrchestrator(folder, orchestrator.state_dir, log=quiet)
        assert again.run() == 2
        assert "register:old-copy.txt" in again.calls
        statuses = {d.source.name: d.state.status for d in again.documents()}
        assert statuses == {
            "letter.txt": AWAITING_EQUIVALENCES,
            "old-copy.txt": AWAITING_EQUIVALENCES,
        }


class TestFreshness:
    """Make-style dependencies by content hash: upstream redos invalidate
    downstream outputs AND the human gates (a corrected transcription must
    force re-extraction, re-review, re-write), and an identical recompute
    invalidates nothing. Modification times are never read."""

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
        new transcription with the same content, so the batch comes out
        the same, nothing after it is redone and every gate stays open."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.correction.write_text(doc.source.read_text())
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert wiki_steps(orchestrator.calls[len(before) :]) == []
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert json.loads(doc.transcription.read_text())["engine"] == (
            "correction"
        )
        # the transcription is made again, from the correction; the batch
        # it gives is the one recorded, so the batch and the critique stand
        done = [e["stage"] for e in this_run(orchestrator, "done")]
        assert done == ["transcription"]
        reused = [e["stage"] for e in this_run(orchestrator, "reused")]
        assert reused == ["extraction", "critique"]
        assert steps(orchestrator.calls[len(before) :]) == []

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
        """A deleted batch view rebuilt with the same content (the scripted
        engine answers as the cache would) leaves the reviews and the write
        it fed valid. Without the cache, a sampled engine words it afresh,
        and the reviews are moved aside (docs/LLM_CACHE.md)."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        doc.batch.unlink()
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert wiki_steps(orchestrator.calls[len(before) :]) == []
        (doc,) = orchestrator.documents()
        assert doc.batch.exists()
        assert doc.review_reviewed.exists()

    def test_modification_times_are_never_read(self, tmp_path):
        """A CSV that is only touched, not changed, re-arms nothing, and a
        review older than its CSV still consents while the CSV's content
        is the one it was bound to: content hashes decide."""
        orchestrator = self.completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        saved_last(doc.review)
        saved_last(doc.equivalences)
        before = list(orchestrator.calls)
        assert orchestrator.run() == 0
        assert wiki_steps(orchestrator.calls[len(before) :]) == []
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


class SampledModel(ScriptedModel):
    """The scripted engine worded afresh on every call, as a sampled model
    is: the critic's finding and each claim's confidence differ from one
    call to the next, the facts do not."""

    def __init__(self, calls, name, draws):
        super().__init__(calls, name)
        self.draws = draws

    def complete(self, prompt, image_paths=()):
        answer = super().complete(prompt, image_paths)
        draw = next(self.draws)
        if prompt.startswith("You review a genealogical extraction"):
            finding = {
                "target": "e1",
                "issue": f"is the year right? (draw {draw})",
                "suggestion": "keep",
            }
            return (
                json.dumps(finding) + '\n{"done": true, "findings_emitted": 1}'
            )
        if '"claims_emitted"' in answer:
            claim, sentinel = answer.splitlines()
            sampled = json.loads(claim) | {"confidence": (draw % 9 + 1) / 10}
            return json.dumps(sampled) + "\n" + sentinel
        return answer


class Sampled(ScriptedOrchestrator):
    """The scripted orchestrator with a sampled engine and the real review
    export, which carries the critic's findings into the review CSV."""

    _step_export_review = Orchestrator._step_export_review

    def __init__(self, *args, draws=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.draws = draws if draws is not None else itertools.count()

    def _llm(self, role, doc):
        return SampledModel(self.calls, doc.source.name, self.draws)


def engine_steps(calls: list[str]) -> list[str]:
    return [step for step in steps(calls) if step not in WIKI_STEPS]


class TestViews:
    """A document's views (its transcription, batch and critique) persist
    in its directory, recorded in derivations.json by content hash, and
    are reused while their inputs are unchanged: the pipeline stands
    still without the LLM cache (#225). `--refresh ROLE` makes a role's
    views again, in place."""

    def sampled(self, tmp_path, **kwargs):
        folder = tmp_path / "docs"
        folder.mkdir(exist_ok=True)
        (folder / "letter.txt").write_text("Klara was born in 1917.")
        kwargs.setdefault("log", quiet)
        return Sampled(folder, tmp_path / "state", **kwargs)

    def test_a_sampled_engine_without_a_cache_reaches_written(self, tmp_path):
        orchestrator = self.sampled(tmp_path)
        assert orchestrator.cache_dir is None
        assert orchestrator.run() == 2
        asked = len(orchestrator.calls)
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        approve_all_equivalences(doc)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_CLAIMS
        assert "is the year right?" in doc.review.read_text()
        approve_all_claims(doc)
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        assert orchestrator.run() == 0
        (doc,) = orchestrator.documents()
        assert doc.state.status == WRITTEN
        # the engine was asked in the first run only, no review was moved
        # aside, and no cache was read or written
        assert engine_steps(orchestrator.calls[asked:]) == []
        assert list(doc.dir.glob("*superseded*")) == []
        assert not any(
            event["event"].startswith("cache-")
            for event in orchestrator.rundir.events()
        )

    def test_the_views_are_recorded_by_content(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        record = json.loads((doc.dir / "derivations.json").read_text())
        derived, content = record["derived"], record["content"]
        assert derived["transcription.json"] == {
            "transcription.corrected.txt": None
        }
        assert derived["batch.json"] == {
            "transcription.json": content["transcription.json"]
        }
        assert derived["critique.json"] == {
            "batch.json": content["batch.json"]
        }

    def test_a_forgotten_view_keeps_no_content(self, tmp_path):
        """A view whose record is forgotten (one set aside) leaves no
        content hash behind for the views made from it to match."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert doc.derivations.content(doc.transcription.name) is not None
        doc.derivations.forget(doc.transcription)
        assert doc.derivations.content(doc.transcription.name) is None
        assert not doc.derivations.current(doc.transcription)
        assert not doc.derivations.current(doc.batch)

    def test_a_deleted_view_is_made_again_alone(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        doc.critique.unlink()
        asked = len(orchestrator.calls)
        assert orchestrator.run() == 2
        assert steps(orchestrator.calls[asked:]) == ["critique"]
        reused = [event["stage"] for event in this_run(orchestrator, "reused")]
        assert reused == ["transcription", "extraction"]

    def test_refresh_makes_a_role_view_again_in_place(self, tmp_path):
        orchestrator = self.sampled(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        approve_all_equivalences(doc)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        critique, batch = doc.critique.read_text(), doc.batch.read_bytes()
        refreshed = self.sampled(
            tmp_path, refresh=["critique"], draws=orchestrator.draws
        )
        assert refreshed.run() == 2
        (doc,) = refreshed.documents()
        assert engine_steps(refreshed.calls) == ["critique"]
        assert doc.critique.read_text() != critique
        assert doc.batch.read_bytes() == batch
        assert doc.state.status == AWAITING_CLAIMS
        # the next run without the flag reuses the view made again
        again = self.sampled(tmp_path, draws=orchestrator.draws)
        assert again.run() == 2
        assert engine_steps(again.calls) == []
        (doc,) = again.documents()
        assert doc.critique.read_text() != critique

    def test_a_refreshed_view_that_comes_out_the_same_stands(self, tmp_path):
        """Early cutoff: a transcription made again with the same content
        leaves the batch and the critique standing, and every gate open."""
        first = completed_run(tmp_path)
        again = ScriptedOrchestrator(
            first.folder, first.state_dir, log=quiet, refresh=["transcribe"]
        )
        assert again.run() == 0
        assert [e["stage"] for e in this_run(again, "done")] == [
            "transcription"
        ]
        assert [e["stage"] for e in this_run(again, "reused")] == [
            "extraction",
            "critique",
        ]
        assert again.calls == []

    def test_serial_mode_makes_a_refreshed_view_once(self, tmp_path):
        first = make_run(tmp_path)
        first.run()
        serial = ScriptedOrchestrator(
            first.folder,
            first.state_dir,
            log=quiet,
            serial=True,
            refresh=["extract"],
        )
        assert serial.run() == 2
        assert engine_steps(serial.calls) == ["manifest", "chunk"]

    def test_views_without_their_state_are_set_aside(self, tmp_path):
        """Views whose state.json is lost were made for a registration
        nothing records: the directory is set aside whole, and the
        document starts afresh."""
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        doc.state_path.unlink()
        before = snapshot(doc.dir, skip=())
        asked = len(orchestrator.calls)
        assert orchestrator.run() == 2
        kept = doc.dir.with_name(".letter.superseded-1")
        assert snapshot(kept, skip=()) == before
        (event,) = this_run(orchestrator, "set-aside")
        assert "no state.json" in event["reason"]
        assert steps(orchestrator.calls[asked:]) == [
            "register",
            "manifest",
            "chunk",
            "critique",
            "reconcile",
        ]


class TestUnrecordedDirectory:
    """A document directory without derivations.json, from before the
    records or one that lost its record: nothing says what its files were
    derived from, so it is set aside whole, as a changed source is, and
    the document starts afresh in a new one (#225)."""

    def from_before_the_records(self, tmp_path):
        """A written document's directory as a run before the records left
        it: every file in place, and no derivations.json."""
        orchestrator = completed_run(tmp_path)
        (doc,) = orchestrator.documents()
        (doc.dir / "derivations.json").unlink()
        return orchestrator

    def test_it_is_set_aside_whole_and_started_afresh(self, tmp_path):
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        before = snapshot(doc.dir, skip=())
        lines: list[str] = []
        orchestrator.log = collect(lines)
        calls = len(orchestrator.calls)
        assert orchestrator.run() == 2
        kept = doc.dir.with_name(".letter.superseded-1")
        assert snapshot(kept, skip=()) == before
        # the document is registered and derived again, in a new directory
        assert steps(orchestrator.calls[calls:]) == [
            "register",
            "manifest",
            "chunk",
            "critique",
            "reconcile",
        ]
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert (doc.dir / "derivations.json").exists()
        assert not doc.equivalences_reviewed.exists()
        assert any(
            "no derivations.json records what the files in its directory "
            "were derived from; the old directory is kept as "
            ".letter.superseded-1" in line
            for line in lines
        )
        (event,) = this_run(orchestrator, "set-aside")
        assert event["subject"] == doc.sha1
        assert event["kept"] == ".letter.superseded-1"
        assert "no derivations.json" in event["reason"]
        # from then on the directory is recorded, and nothing more moves
        assert orchestrator.run() == 2
        assert not doc.dir.with_name(".letter.superseded-2").exists()

    @pytest.mark.parametrize(
        "keep", [(), ("state.json",)], ids=["no-state", "state-only"]
    )
    def test_any_file_without_a_record_sets_it_aside(self, tmp_path, keep):
        """A directory that lost its state.json too, or holds nothing but
        a state.json (a document whose registration failed before the
        records, say), is set aside all the same."""
        orchestrator = self.from_before_the_records(tmp_path)
        (doc,) = orchestrator.documents()
        for path in doc.dir.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()
        before = snapshot(doc.dir, skip=())
        assert before
        assert orchestrator.run() == 2
        assert (
            snapshot(doc.dir.with_name(".letter.superseded-1"), skip=())
            == before
        )
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_a_gedcom_directory_is_set_aside_too(self, tmp_path):
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
        assert doc.dir.with_name(".family.superseded-1").is_dir()
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_serial_mode_sets_it_aside_once(self, tmp_path):
        """Serial mode advances a document twice in one run (extract-only,
        then in sequence); the directory is set aside on the first."""
        first = self.from_before_the_records(tmp_path)
        orchestrator = ScriptedOrchestrator(
            first.folder, first.state_dir, log=quiet, serial=True
        )
        assert orchestrator.run() == 2
        assert len(this_run(orchestrator, "set-aside")) == 1
        (doc,) = orchestrator.documents()
        assert doc.dir.with_name(".letter.superseded-1").is_dir()
        assert not doc.dir.with_name(".letter.superseded-2").exists()
        assert doc.state.status == AWAITING_EQUIVALENCES

    def test_a_first_run_that_stopped_before_its_batch_carries_on(
        self, tmp_path
    ):
        """A directory is recorded from its first run: derivations.json is
        written before any other file. A first run that fails before the
        batch leaves transcription.json, and the next run carries on in
        the same directory."""
        orchestrator = make_run(tmp_path, fail_step="manifest")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.transcription.exists()
        assert (doc.dir / "derivations.json").exists()
        orchestrator.fail_step = None
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert this_run(orchestrator, "set-aside") == []

    @pytest.mark.parametrize(
        "error",
        [OSError("the disk refused"), KeyboardInterrupt()],
        ids=["failure", "interrupt"],
    )
    def test_a_stop_before_the_first_record_leaves_nothing_behind(
        self, tmp_path, monkeypatch, error
    ):
        """A run that stops while a new directory's record is being written
        saves no state there, so the next run starts it afresh in place
        rather than setting it aside."""
        import yizkr.pipeline.orchestrate as orchestrate

        def refuse(self):
            raise error

        orchestrator = make_run(tmp_path)
        monkeypatch.setattr(orchestrate.Derivations, "begin", refuse)
        if isinstance(error, KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                orchestrator.run()
        else:
            assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert not doc.state_path.exists()
        assert not doc.unrecorded
        monkeypatch.undo()
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        assert doc.state.status == AWAITING_EQUIVALENCES
        assert not doc.dir.with_name(".letter.superseded-1").exists()

    def test_a_duplicate_is_recorded_from_its_first_run(self, tmp_path):
        """A duplicate's directory gets its record too, so when the file
        it duplicated is gone it is ingested in place, not set aside."""
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a-original.txt").write_text("Leopold was born in 1914.")
        (folder / "b-copy.txt").write_text("Leopold was born in 1914.")
        orchestrator = ScriptedOrchestrator(
            folder, tmp_path / "state", log=quiet
        )
        orchestrator.run()
        _, copy = orchestrator.documents()
        assert copy.state.status == DUPLICATE
        (folder / "a-original.txt").unlink()
        assert orchestrator.run() == 2
        (copy,) = orchestrator.documents()
        assert copy.state.status == AWAITING_EQUIVALENCES
        assert this_run(orchestrator, "set-aside") == []


class TestRunDirectory:
    """The run directory records what each run did: an event per stage
    started, done or failed, and run state derived from the events."""

    def test_the_manifest_names_the_cache_by_its_absolute_path(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        orchestrator = make_run(tmp_path, cache_dir=Path("var/llm-cache"))
        orchestrator.run()
        assert orchestrator.rundir.manifest()["cache"] == os.path.join(
            os.getcwd(), "var", "llm-cache"
        )

    def test_every_stage_is_an_event(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        stages = [
            (event["event"], event["stage"])
            for event in this_run(orchestrator, "started", "done")
        ]
        assert stages == [
            ("started", "transcription"),
            ("done", "transcription"),
            ("started", "extraction"),
            ("done", "extraction"),
            ("started", "critique"),
            ("done", "critique"),
        ]
        assert {
            (event["subject"], event["kind"])
            for event in this_run(orchestrator, "started")
        } == {(doc.sha1, "text")}
        manifest = orchestrator.rundir.manifest()
        assert manifest["documents"] == [
            {
                "sha1": doc.sha1,
                "file": "letter.txt",
                "kind": "text",
                "stages": ["transcription", "extraction", "critique"],
            }
        ]
        assert manifest["cache"] is None

    def test_run_state_is_derived_from_the_events(self, tmp_path):
        orchestrator = make_run(tmp_path)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        states = run_state(orchestrator.rundir)
        assert {
            stage: state.status for (_, stage), state in states.items()
        } == {
            "transcription": "done",
            "extraction": "done",
            "critique": "done",
        }
        assert {subject for subject, _ in states} == {doc.sha1}

    def test_a_failed_stage_keeps_its_reason(self, tmp_path):
        orchestrator = make_run(tmp_path, fail_step="critique")
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.state.status == FAILED
        critique = run_state(orchestrator.rundir)[(doc.sha1, "critique")]
        assert critique.status == "failed"
        assert "critique engine down" in critique.reason
        # the next run reuses the views that stand and makes the critique
        orchestrator.fail_step = None
        orchestrator.calls.clear()
        assert orchestrator.run() == 2
        assert steps(orchestrator.calls) == ["critique", "reconcile"]
        states = run_state(orchestrator.rundir)
        assert states[(doc.sha1, "critique")].status == "done"
        assert states[(doc.sha1, "extraction")].status == "done"

    def test_attention_reaches_the_run_state(self, tmp_path):
        """An extraction that fell back to line-range segmentation and to
        line windows is done, and needs attention, with its reasons."""

        class NoManifest(ScriptedModel):
            def complete(self, prompt, image_paths=()):
                if "manifest of EVERY distinct entity" in prompt:
                    self.calls.append(f"manifest:{self.name}")
                    return "no manifest here"
                if prompt.startswith("You segment a document"):
                    self.calls.append(f"segmentation:{self.name}")
                    return "no segmentation either"
                return super().complete(prompt, image_paths)

        class Falls(ScriptedOrchestrator):
            def _llm(self, role, doc):
                return NoManifest(self.calls, doc.source.name)

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "letter.txt").write_text("Ursula was born in 1911.")
        orchestrator = Falls(folder, tmp_path / "state", log=quiet)
        assert orchestrator.run() == 2
        (doc,) = orchestrator.documents()
        extraction = run_state(orchestrator.rundir)[(doc.sha1, "extraction")]
        assert extraction.status == NEEDS_ATTENTION
        assert "fell back to line-range segmentation" in extraction.reason
        assert "fell back to line windows" in extraction.reason
        # a run that reuses the batch view carries its reasons on
        assert orchestrator.run() == 2
        (reused,) = [
            event
            for event in this_run(orchestrator, "reused")
            if event["stage"] == "extraction"
        ]
        assert reused["attention"] == extraction.reason.split("; ")
        again = run_state(orchestrator.rundir)[(doc.sha1, "extraction")]
        assert again == extraction

    def test_a_held_stage_pauses_the_document(self, tmp_path):
        """A pause request for a stage stops its dispatch; nothing fails,
        and the next run resumes."""

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
        assert "critique" not in steps(orchestrator.calls)
        states = run_state(orchestrator.rundir)
        assert states[(doc.sha1, "extraction")].status == "done"
        critique = states[(doc.sha1, "critique")]
        assert critique.status == "pending"
        assert critique.reason == "stage:critique paused"
        # a new invocation starts clear of the old requests
        resumed = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        assert resumed.run() == 2
        assert steps(resumed.calls) == ["critique", "reconcile"]

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


class TestEngines:
    """One engine client per role serves a whole run, and the client
    factory wraps it with the LLM cache only when the run has one."""

    class Engine:
        model_identifier = "scripted"
        config_hash = "c" * 64

        def __init__(self, role):
            self.role = role
            self.closed = False

        def complete(self, prompt, image_paths=()):  # pragma: no cover
            raise AssertionError("not called here")

        def close(self):
            self.closed = True

    def orchestrator(self, tmp_path, monkeypatch, **kwargs):
        from yizkr.pipeline import llm

        made = []

        def make_client(engine, *, effort=None, model=None, **_):
            made.append((effort, model))
            return self.Engine(len(made))

        monkeypatch.setattr(llm, "make_client", make_client)
        folder = tmp_path / "docs"
        folder.mkdir()
        return Orchestrator(folder, tmp_path / "state", **kwargs), made

    def test_one_engine_per_role_serves_every_document(
        self, tmp_path, monkeypatch
    ):
        orchestrator, made = self.orchestrator(
            tmp_path, monkeypatch, extract_model="model-x"
        )
        docs = [
            DocumentRun(tmp_path / name, tmp_path / "state")
            for name in ("a.txt", "b.txt")
        ]
        for doc in docs:
            doc.source.write_text(doc.source.name)
            for role in ("transcribe", "extract", "critique"):
                orchestrator._llm(role, doc)
        assert made == [("low", None), ("low", "model-x"), ("high", None)]
        engines = list(orchestrator._engines.values())
        orchestrator._close_engines()
        assert all(engine.closed for engine in engines)
        assert orchestrator._engines == {}

    def test_without_a_cache_the_engine_is_called_directly(
        self, tmp_path, monkeypatch
    ):
        from yizkr.pipeline.llm import RejectingLlmClient

        orchestrator, _ = self.orchestrator(tmp_path, monkeypatch)
        doc = DocumentRun(tmp_path / "a.txt", tmp_path / "state")
        doc.source.write_text("a")
        # the engine's calls are reported to the run (#101), and nothing
        # keeps their answers
        client = orchestrator._llm("extract", doc)
        assert isinstance(client.client, self.Engine)
        assert not isinstance(client, RejectingLlmClient)
        assert not (tmp_path / "state").exists()

    def test_with_a_cache_the_factory_wraps_the_engine(
        self, tmp_path, monkeypatch
    ):
        from yizkr.pipeline.llm import RejectingLlmClient

        orchestrator, _ = self.orchestrator(
            tmp_path,
            monkeypatch,
            cache_dir=tmp_path / "cache",
            refresh=["critique"],
            epochs={"extract": 2},
        )
        doc = DocumentRun(tmp_path / "a.txt", tmp_path / "state")
        doc.source.write_text("a")
        client = orchestrator._llm("extract", doc)
        assert isinstance(client, RejectingLlmClient)
        assert client.cache.refresh == {"critique"}
        assert client.cache.epoch("extract") == 2
        # one cache per run, shared by every client
        assert orchestrator._llm("critique", doc).cache is client.cache


class TestPreflight:
    """The PDF renderer is checked once for the whole run, before any
    document, whether its transcriptions come from the engine or the
    cache (#218, #225)."""

    def folder(self, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a-scan.pdf").write_bytes(b"%PDF-1.4 not really")
        (folder / "b-scan.pdf").write_bytes(b"%PDF-1.4 not really either")
        (folder / "c-letter.txt").write_text("Ursula was born in 1911.")
        return folder

    def test_a_missing_renderer_stops_the_run_before_any_document(
        self, tmp_path, monkeypatch
    ):
        asked = []

        def which(name):
            asked.append(name)
            return None

        monkeypatch.setattr(transcribe.shutil, "which", which)
        orchestrator = ScriptedOrchestrator(
            self.folder(tmp_path), tmp_path / "state", log=quiet
        )
        with pytest.raises(MissingPrerequisite, match="poppler-utils"):
            orchestrator.run()
        assert orchestrator.calls == []
        assert not (tmp_path / "state").exists()
        assert asked == ["pdftoppm", "pdfinfo"]  # once, for both PDFs

    def test_a_run_without_pdfs_needs_no_renderer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(transcribe.shutil, "which", lambda name: None)
        assert make_run(tmp_path).run() == 2

    def test_ingest_run_says_what_to_install(self, tmp_path, monkeypatch):
        from yizkr.cli import build_parser
        from yizkr.core.corpus import CORPUS_DIR_VAR, EXCLUDE_FILE_VAR

        listed = tmp_path / "exclude.txt"
        listed.write_text("")
        env_file = tmp_path / "test.env"
        env_file.write_text(f"{EXCLUDE_FILE_VAR}={listed}\n")
        monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
        monkeypatch.delenv(EXCLUDE_FILE_VAR, raising=False)
        monkeypatch.delenv(CORPUS_DIR_VAR, raising=False)
        monkeypatch.setattr(transcribe.shutil, "which", lambda name: None)
        args = build_parser().parse_args(
            [
                "ingest",
                "run",
                str(self.folder(tmp_path)),
                "--state",
                str(tmp_path / "state"),
                "--no-cache",
            ]
        )
        with pytest.raises(SystemExit, match="apt-get install poppler-utils"):
            args.func(args)


class TestIngestRunFlags:
    def parse(self, tmp_path, monkeypatch, *flags):
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
        folder.mkdir(exist_ok=True)
        seen = []
        monkeypatch.setattr(
            Orchestrator, "run", lambda self: seen.append(self) or 0
        )
        state = str(tmp_path / "state")
        args = build_parser().parse_args(
            ["ingest", "run", str(folder), "--state", state, *flags]
        )
        assert args.func(args) == 0
        return seen[0]

    def test_the_cache_is_on_by_default(self, tmp_path, monkeypatch):
        orchestrator = self.parse(tmp_path, monkeypatch)
        assert orchestrator.cache_dir == Path("var/llm-cache")
        assert orchestrator.refresh == ()
        assert orchestrator.epochs == {}

    def test_no_cache_calls_the_engine_directly(self, tmp_path, monkeypatch):
        orchestrator = self.parse(tmp_path, monkeypatch, "--no-cache")
        assert orchestrator.cache_dir is None
        assert orchestrator._llm_cache() is None

    def test_cache_refresh_and_epoch(self, tmp_path, monkeypatch):
        orchestrator = self.parse(
            tmp_path,
            monkeypatch,
            "--cache",
            str(tmp_path / "cache"),
            "--refresh",
            "critique",
            "--refresh",
            "transcribe",
            "--epoch",
            "extract=2",
        )
        assert orchestrator.cache_dir == tmp_path / "cache"
        assert orchestrator.refresh == ("critique", "transcribe")
        assert orchestrator.epochs == {"extract": 2}

    @pytest.mark.parametrize(
        "flags",
        [
            ["--refresh", "reconcile"],
            ["--epoch", "extract"],
            ["--epoch", "extract=-1"],
            ["--epoch", "nobody=1"],
            ["--cache", "x", "--no-cache"],
        ],
    )
    def test_bad_flags_are_refused(self, tmp_path, monkeypatch, flags):
        with pytest.raises(SystemExit):
            self.parse(tmp_path, monkeypatch, *flags)

    @pytest.mark.parametrize(
        "flags",
        [["--refresh", "critique"], ["--epoch", "extract=1"]],
        ids=["refresh", "epoch"],
    )
    def test_no_cache_refuses_the_cache_switches(
        self, tmp_path, monkeypatch, flags
    ):
        with pytest.raises(SystemExit, match="act through the LLM cache"):
            self.parse(tmp_path, monkeypatch, "--no-cache", *flags)


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
        from tests.fixtures import example_universe as u

        sources = {source.key: source for source in u.SOURCES}
        german = sources["marriage-cert-1905"].text  # a register entry
        english = sources["interview-2002"].text  # an oral history
        # one orchestrator, default language (auto), two languages in a folder
        orch = Orchestrator(tmp_path / "docs", tmp_path / "state", log=quiet)
        de = orch._step_transcribe(
            self._doc(tmp_path, "marriage-cert-1905.txt", german)
        )
        en = orch._step_transcribe(
            self._doc(tmp_path, "interview-2002.txt", english)
        )
        assert de.language == "de"
        assert en.language == "en"

    def test_explicit_language_forces_and_records_source(self, tmp_path):
        german = "Liebes Pünktchen! Seit Du am 22. Juni abgereist bist."
        orch = Orchestrator(
            tmp_path / "docs", tmp_path / "state", language="fr", log=quiet
        )
        doc = self._doc(tmp_path, "postcard-1938.txt", german)
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
        doc = self._doc(tmp_path, "postmark.txt", "19.8.38")  # too short
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
        (folder / "a.txt").write_text("Rahel was born in 1871.")
        (folder / "b.txt").write_text("Felix was born in 1867.")
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
        (folder / "a.txt").write_text("Rahel was born in 1871.")
        (folder / "b.txt").write_text("Felix was born in 1867.")
        orch = ScriptedOrchestrator(folder, tmp_path / "state", log=quiet)
        orch.run()
        assert "reconcile:a.txt" in orch.calls
        assert "reconcile:b.txt" in orch.calls

    def test_extract_concurrency_extracts_every_document(self, tmp_path):
        # --extract-concurrency parallelises the extract-all phase; all
        # documents must still be extracted, and the serial invariant holds.
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("Rahel was born in 1871.")
        (folder / "b.txt").write_text("Felix was born in 1867.")
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
        # document directories only: the run files sit beside them
        assert sorted(
            p.name for p in (tmp_path / "state").iterdir() if p.is_dir()
        ) == ["naturalisation-1952"]
        assert [d.source.name for d in orch.documents()] == [
            "naturalisation-1952.txt"
        ]
