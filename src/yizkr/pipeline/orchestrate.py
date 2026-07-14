"""Folder/batch orchestration (ROADMAP item 15): `yizkr ingest run`.

One command walks every document in a folder through the pipeline —
register -> transcribe -> extract (or the deterministic GEDCOM adapter)
-> reconcile -> resolve -> export review -> apply review -> write —
with per-document state and safe re-runs. Each invocation advances
every document as far as it can and then STOPS at the human gates:

* after reconcile, until `<doc>/equivalences.reviewed.csv` exists
  (copy `equivalences.csv`, fill the `decision` column, save under the
  reviewed name — identity is never auto-confirmed, ADR-0007);
* after the review export, until `<doc>/review.reviewed.csv` exists
  (same convention for claim decisions, corrections, and the living
  flag — R3: human confirmation is part of the data flow).

A reviewed file's PRESENCE is the gate: the orchestrator never
interprets a half-filled CSV as consent. Documents whose reconcile
produced no proposals skip the first gate (there is nothing to review).

State lives beside the outputs in the state directory (runtime state —
keep it under var/ or outside the repo; the repo carries no personal
data). Every step's output is a plain file, so any step can be
inspected, corrected upstream, or re-run by deleting its output.
Re-running a completed document is a no-op (the writer is idempotent).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient

GEDCOM_SUFFIXES = {".ged"}
TEXT_SUFFIXES = {".txt", ".md"}
MEDIA_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
SUPPORTED_SUFFIXES = GEDCOM_SUFFIXES | TEXT_SUFFIXES | MEDIA_SUFFIXES

# document statuses, in pipeline order
REGISTERED = "registered"
BATCHED = "batched"
AWAITING_EQUIVALENCES = "awaiting equivalences review"
RESOLVED = "resolved"
AWAITING_CLAIMS = "awaiting claim review"
WRITTEN = "written"
FAILED = "failed"


@dataclass
class DocumentState:
    file: str
    title: str
    status: str = "pending"
    artifact_qid: str | None = None
    error: str | None = None
    detail: dict | None = None

    @classmethod
    def load(cls, path: Path) -> DocumentState:
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2))


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "document"


class DocumentRun:
    """Paths and state for one document within a folder run."""

    def __init__(self, source: Path, state_dir: Path) -> None:
        self.source = source
        self.dir = state_dir / slugify(source.stem)
        self.state_path = self.dir / "state.json"
        self.transcription = self.dir / "transcription.json"
        self.batch = self.dir / "batch.json"
        self.equivalences = self.dir / "equivalences.csv"
        self.equivalences_reviewed = self.dir / "equivalences.reviewed.csv"
        self.resolved = self.dir / "batch.resolved.json"
        self.review = self.dir / "review.csv"
        self.review_reviewed = self.dir / "review.reviewed.csv"
        self.approved = self.dir / "batch.approved.json"
        self.write_result = self.dir / "write.json"
        if self.state_path.exists():
            self.state = DocumentState.load(self.state_path)
        else:
            self.state = DocumentState(file=str(source), title=source.stem)

    def save(self) -> None:
        self.state.save(self.state_path)


class Orchestrator:
    """Advances every document in a folder as far as review allows.

    Heavy operations live in small `_step_*` methods so the state
    machine is testable offline with scripted substitutes.
    """

    def __init__(
        self,
        folder: Path,
        state_dir: Path,
        *,
        llm: str = "claude-code",
        artifact_type: str = "artifact",
        language: str = "en",
        env_file: Path | None = None,
        log=print,
    ) -> None:
        self.folder = folder
        self.state_dir = state_dir
        self.llm = llm
        self.artifact_type = artifact_type
        self.language = language
        self.env_file = env_file
        self.log = log
        self._config: Config | None = None
        self._client: WikiClient | None = None

    # -- lazy connections --------------------------------------------------

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config.load(self.env_file)
        return self._config

    @property
    def client(self) -> WikiClient:
        if self._client is None:
            self._client = WikiClient(self.config)
        return self._client

    # -- document discovery --------------------------------------------------

    def documents(self) -> list[DocumentRun]:
        files = sorted(
            path
            for path in self.folder.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
        skipped = [
            path.name
            for path in sorted(self.folder.iterdir())
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() not in SUPPORTED_SUFFIXES
        ]
        for name in skipped:
            self.log(f"skipping {name}: unsupported file type", file=sys.stderr)
        return [DocumentRun(path, self.state_dir) for path in files]

    # -- pipeline steps (override points for tests) ---------------------------

    def _step_register(self, doc: DocumentRun) -> str:
        from yizkr.pipeline.registrar import register_artifact

        result = register_artifact(
            self.client,
            media_path=doc.source,
            artifact_type=self.artifact_type,
            title=doc.state.title,
            description=f"ingested by `yizkr ingest run` from {doc.source.name}",
        )
        return result.artifact_qid

    def _step_batch(self, doc: DocumentRun) -> ClaimBatch:
        if doc.source.suffix.lower() in GEDCOM_SUFFIXES:
            return self._step_gedcom(doc)
        transcription = self._step_transcribe(doc)
        return self._step_extract(doc, transcription)

    def _step_gedcom(self, doc: DocumentRun) -> ClaimBatch:
        from yizkr.pipeline.gedcom import adapt_gedcom

        result = adapt_gedcom(
            doc.source.read_text(), artifact_qid=doc.state.artifact_qid
        )
        for warning in result.warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        if result.unsupported_tags:
            counted = ", ".join(
                f"{tag}x{count}"
                for tag, count in sorted(result.unsupported_tags.items())
            )
            self.log(
                f"  {doc.source.name}: unsupported GEDCOM tags: {counted}",
                file=sys.stderr,
            )
        doc.state.detail = (doc.state.detail or {}) | {
            "gedcom_warnings": result.warnings,
            "gedcom_unsupported": result.unsupported_tags,
        }
        return result.batch

    def _step_transcribe(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import Transcription

        if doc.transcription.exists():
            return Transcription.model_validate_json(doc.transcription.read_text())
        if doc.source.suffix.lower() in TEXT_SUFFIXES:
            from yizkr.pipeline.transcribe import TextFileTranscriber

            transcriber = TextFileTranscriber(doc.source, language=self.language)
        else:
            from yizkr.pipeline.llm import make_client
            from yizkr.pipeline.transcribe import LlmVisionTranscriber

            transcriber = LlmVisionTranscriber(
                make_client(self.llm), language=self.language
            )
        transcription = transcriber.transcribe(
            doc.source, artifact_qid=doc.state.artifact_qid
        )
        doc.transcription.write_text(transcription.model_dump_json(indent=2))
        return transcription

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline.chunking import AUTO_CHUNK_THRESHOLD, ChunkedClaimExtractor
        from yizkr.pipeline.extract import LlmClaimExtractor
        from yizkr.pipeline.llm import make_client

        if len(transcription.text) > AUTO_CHUNK_THRESHOLD:
            extractor = ChunkedClaimExtractor(
                make_client(self.llm),
                state_dir=doc.batch.with_name(doc.batch.name + ".chunks"),
                resume=True,
            )
        else:
            extractor = LlmClaimExtractor(
                make_client(self.llm),
                raw_dump=doc.batch.with_name(doc.batch.name + ".raw.txt"),
                rejects_dump=doc.batch.with_name(doc.batch.name + ".rejected.json"),
            )
        batch = extractor.extract(transcription, artifact_qid=doc.state.artifact_qid)
        failed = getattr(extractor, "failed_chunks", None)
        if failed:
            raise RuntimeError(
                f"{len(failed)} chunks failed after retries ({', '.join(failed)}); "
                f"re-run to resume them"
            )
        return batch

    def _step_reconcile(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        from yizkr.pipeline.llm import make_client
        from yizkr.pipeline.reconcile import (
            adjudicate,
            block_candidates,
            export_equivalences_csv,
            fetch_roster,
        )

        roster = fetch_roster(self.client, self.config)
        candidates = block_candidates(batch.entities, roster)
        proposals = (
            adjudicate(make_client(self.llm), batch.entities, candidates, batch)
            if candidates
            else []
        )
        return export_equivalences_csv(
            proposals, batch.entities, roster, doc.equivalences
        )

    def _step_resolve(self, doc: DocumentRun, batch: ClaimBatch) -> ClaimBatch:
        from yizkr.pipeline.reconcile import (
            apply_equivalences,
            read_approved_equivalences,
        )
        from yizkr.pipeline.resolve import resolve_batch

        if doc.equivalences_reviewed.exists():
            approved = read_approved_equivalences(doc.equivalences_reviewed)
            batch = apply_equivalences(batch, approved)
            self.log(f"  applied {len(approved)} approved equivalences")
        _, item_labels = self.client.label_index()
        return resolve_batch(batch, item_labels)

    def _step_export_review(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        from yizkr.pipeline.reconcile import fetch_roster
        from yizkr.pipeline.review import export_review_csv

        known_deaths = {
            entity.qid
            for entity in fetch_roster(self.client, self.config)
            if entity.death
        }
        return export_review_csv(batch, doc.review, known_deaths=known_deaths)

    def _step_write(self, doc: DocumentRun, batch: ClaimBatch) -> dict:
        from yizkr.pipeline.review import apply_review
        from yizkr.pipeline.writer import BatchWriter

        approved, warnings = apply_review(batch, doc.review_reviewed)
        for warning in warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        doc.approved.write_text(approved.model_dump_json(indent=2))
        result = BatchWriter(self.client).write_batch(approved)
        return {
            "run_qid": result.run_qid,
            "written": result.written,
            "skipped": result.skipped,
            "referenced": result.referenced,
            "created_items": result.created_items,
            "approved_claims": len(approved.claims),
        }

    # -- the state machine ----------------------------------------------------

    def advance(self, doc: DocumentRun) -> str:
        """Advance one document as far as the gates allow; returns status."""
        try:
            return self._advance(doc)
        except Exception as exc:  # per-document isolation: others continue
            doc.state.status = FAILED
            doc.state.error = f"{type(exc).__name__}: {exc}"
            doc.save()
            self.log(
                f"  {doc.source.name}: FAILED — {doc.state.error}", file=sys.stderr
            )
            return FAILED
        finally:
            doc.save()

    def _advance(self, doc: DocumentRun) -> str:
        doc.dir.mkdir(parents=True, exist_ok=True)
        doc.state.error = None

        if doc.state.artifact_qid is None:
            doc.state.artifact_qid = self._step_register(doc)
            doc.state.status = REGISTERED
            doc.save()
            self.log(f"  registered as {doc.state.artifact_qid}")

        if not doc.batch.exists():
            batch = self._step_batch(doc)
            doc.batch.write_text(batch.model_dump_json(indent=2))
            doc.state.status = BATCHED
            doc.save()
            self.log(f"  batch: {len(batch.claims)} claims")
        else:
            batch = ClaimBatch.model_validate_json(doc.batch.read_text())

        if batch.entities and not doc.equivalences.exists():
            proposals = self._step_reconcile(doc, batch)
            self.log(f"  reconcile: {proposals} proposals")

        needs_equivalence_review = (
            doc.equivalences.exists()
            and _csv_has_rows(doc.equivalences)
            and not doc.equivalences_reviewed.exists()
        )
        if needs_equivalence_review:
            doc.state.status = AWAITING_EQUIVALENCES
            self.log(
                f"  waiting: review {doc.equivalences} and save decisions as "
                f"{doc.equivalences_reviewed.name}"
            )
            return doc.state.status

        if not doc.resolved.exists():
            resolved = self._step_resolve(doc, batch)
            doc.resolved.write_text(resolved.model_dump_json(indent=2))
            doc.state.status = RESOLVED
            doc.save()
        else:
            resolved = ClaimBatch.model_validate_json(doc.resolved.read_text())

        if not doc.review.exists():
            rows = self._step_export_review(doc, resolved)
            self.log(f"  review CSV: {rows} rows")

        if not doc.review_reviewed.exists():
            doc.state.status = AWAITING_CLAIMS
            self.log(
                f"  waiting: review {doc.review} and save decisions as "
                f"{doc.review_reviewed.name}"
            )
            return doc.state.status

        if not doc.write_result.exists():
            summary = self._step_write(doc, resolved)
            doc.write_result.write_text(json.dumps(summary, indent=2))
            doc.state.detail = (doc.state.detail or {}) | {"write": summary}
            self.log(
                f"  written: {summary['written']} new, "
                f"{summary['referenced']} referenced, {summary['skipped']} skipped"
            )
        doc.state.status = WRITTEN
        return doc.state.status

    def run(self) -> int:
        docs = self.documents()
        if not docs:
            self.log(f"no supported documents in {self.folder}")
            return 0
        for doc in docs:
            self.log(f"{doc.source.name} [{doc.state.status}]")
            if doc.state.status == WRITTEN and doc.write_result.exists():
                self.log("  already written; nothing to do")
                continue
            self.advance(doc)

        self.log("")
        counts: dict[str, int] = {}
        for doc in docs:
            counts[doc.state.status] = counts.get(doc.state.status, 0) + 1
            self.log(f"{doc.state.status:32} {doc.source.name}")
        if any(doc.state.status == FAILED for doc in docs):
            return 1
        if any(doc.state.status.startswith("awaiting") for doc in docs):
            return 2
        return 0


def _csv_has_rows(path: Path) -> bool:
    with open(path, newline="") as handle:
        next(handle, None)  # header
        return any(line.strip() for line in handle)
