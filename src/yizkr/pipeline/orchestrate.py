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
  (same convention for claim decisions and corrections — R3: human
  confirmation is part of the data flow).

A reviewed file's PRESENCE (and freshness) is the gate: the
orchestrator never interprets a half-filled CSV as consent, and a
reviewed file older than the CSV it reviews is a decision about
superseded rows, not consent for the current ones. Documents whose
reconcile produced no proposals skip the first gate (there is nothing
to review).

State lives beside the outputs in the state directory (runtime state —
keep it under var/ or outside the repo; the repo carries no personal
data). Every step's output is a plain file, so any step can be
inspected, corrected upstream, or re-run by deleting its output;
dependencies are make-style (a corrected transcription re-extracts, a
re-extracted batch invalidates every derived file, a re-reviewed CSV
re-writes). Re-running a completed document is a no-op (the writer is
idempotent).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient

GEDCOM_SUFFIXES = {".ged"}
TEXT_SUFFIXES = {".txt", ".md"}
MEDIA_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
PRESENTATION_SUFFIXES = {".pptx"}
SUPPORTED_SUFFIXES = (
    GEDCOM_SUFFIXES | TEXT_SUFFIXES | MEDIA_SUFFIXES | PRESENTATION_SUFFIXES
)

# document statuses, in pipeline order
REGISTERED = "registered"
BATCHED = "batched"
AWAITING_EQUIVALENCES = "awaiting equivalences review"
RESOLVED = "resolved"
AWAITING_CLAIMS = "awaiting claim review"
WRITTEN = "written"
FAILED = "failed"
# a file whose bytes were already claimed by an earlier document in the
# same run: identical content is one artifact, so it is not transcribed,
# extracted, or written again (ADR-0010)
DUPLICATE = "duplicate"


@dataclass
class DocumentState:
    file: str
    title: str
    status: str = "pending"
    artifact_qid: str | None = None
    error: str | None = None
    detail: dict | None = None
    # SHA-1 of the source bytes last processed (ADR-0010): the content
    # identity, so a renamed copy is recognised across runs and a changed
    # source forces a re-ingest.
    content_sha1: str | None = None

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
        # open-schema harness (ADR-0013)
        self.gaps = self.dir / "gaps.csv"
        self.critique = self.dir / "critique.json"
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
        language: str = "auto",
        fallback_language: str = "en",
        chunk_concurrency: int = 3,
        extract_effort: str = "low",
        review_effort: str = "high",
        extract_model: str | None = None,
        serial: bool = False,
        extract_concurrency: int = 1,
        env_file: Path | None = None,
        log=print,
    ) -> None:
        self.folder = folder
        self.state_dir = state_dir
        self.llm = llm
        self.artifact_type = artifact_type
        self.language = language
        self.fallback_language = fallback_language
        self.chunk_concurrency = chunk_concurrency
        # reasoning effort per phase: mechanical bulk steps (transcription,
        # extraction) run low; judgment steps (adjudication, critique) run
        # high — where the thinking is cheap and the decision matters.
        self.extract_effort = extract_effort
        self.review_effort = review_effort
        # per-phase model: claim extraction is a high-volume, structured
        # task where source discipline (extract only what the source
        # asserts) matters more than raw capability, so it can run a
        # distinct model from the run's main one — which stays on the
        # judgment steps (adjudication, critique). None inherits the main
        # model. See docs/adr/0016.
        self.extract_model = extract_model
        # serial mode (ADR-0019): process artifacts one at a time, in a
        # greedy-overlap sequence, each fully written before the next
        # reconciles — so cross-artifact identity resolves inline at the
        # per-artifact gates instead of in a separate reconcile-graph pass.
        self.serial = serial
        # how many documents to extract (transcribe + chunked extract) at
        # once during serial mode's up-front extract-all phase. Multiplies
        # with chunk_concurrency, so keep the product sane for the engine's
        # rate/usage limits. Wiki writes (register/derive) are serialised by
        # a lock, so only the LLM-heavy extraction actually overlaps.
        self.extract_concurrency = max(1, extract_concurrency)
        self._wiki_lock = threading.Lock()
        self.env_file = env_file
        self.log = log
        self._config: Config | None = None
        self._client: WikiClient | None = None
        # content SHA-1 -> the run's canonical filename for it, so a later
        # identical file is recognised before any expensive step (ADR-0010)
        self._seen_hashes: dict[str, str] = {}

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
            self.log(
                f"skipping {name}: unsupported file type", file=sys.stderr
            )
        return [DocumentRun(path, self.state_dir) for path in files]

    # -- pipeline steps (override points for tests) ---------------------------

    def _step_register(self, doc: DocumentRun) -> str:
        from yizkr.pipeline.registrar import register_artifact

        artifact_type = self.artifact_type
        pages = 0
        if doc.source.suffix.lower() in PRESENTATION_SUFFIXES:
            # a deck is a compound artifact: its slides ARE the page
            # fragments (ADR-0008), so claim locators point into slides
            artifact_type = "presentation"
            pages = len(self._presentation(doc).slides)
        elif doc.source.suffix.lower() in GEDCOM_SUFFIXES:
            artifact_type = "genealogical_report"
        result = register_artifact(
            self.client,
            media_path=doc.source,
            artifact_type=artifact_type,
            title=doc.state.title,
            description=(
                f"ingested by `yizkr ingest run` from {doc.source.name}"
            ),
            pages=pages,
        )
        return result.artifact_qid

    def _presentation(self, doc: DocumentRun):
        from yizkr.pipeline.presentation import unpack_pptx

        if not hasattr(self, "_presentation_cache"):
            self._presentation_cache: dict[str, object] = {}
        key = str(doc.source)
        if key not in self._presentation_cache:
            self._presentation_cache[key] = unpack_pptx(doc.source)
        return self._presentation_cache[key]

    def _step_derive_media(self, doc: DocumentRun) -> dict[str, str]:
        from yizkr.pipeline.presentation import register_derived_media

        content = self._presentation(doc)
        for suffix, count in content.skipped_media.items():
            self.log(
                f"  {doc.source.name}: {count} embedded {suffix} media "
                f"skipped (image media only in W1)",
                file=sys.stderr,
            )
        return register_derived_media(
            self.client,
            deck_qid=doc.state.artifact_qid,
            deck_title=doc.state.title,
            content=content,
            workdir=doc.dir / "media",
        )

    def _step_batch(self, doc: DocumentRun) -> ClaimBatch:
        if doc.source.suffix.lower() in GEDCOM_SUFFIXES:
            return self._step_gedcom(doc)
        transcription = self._step_transcribe(doc)
        return self._step_extract(doc, transcription)

    def _step_gedcom(self, doc: DocumentRun) -> ClaimBatch:
        from yizkr.pipeline.gedcom import (
            adapt_gedcom,
            format_unsupported_tags,
        )

        result = adapt_gedcom(
            doc.source.read_text(), artifact_qid=doc.state.artifact_qid
        )
        for warning in result.warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        if result.unsupported_tags:
            self.log(
                f"  {doc.source.name}: unsupported GEDCOM tags: "
                + format_unsupported_tags(result.unsupported_tags),
                file=sys.stderr,
            )
        doc.state.detail = (doc.state.detail or {}) | {
            "gedcom_warnings": result.warnings,
            "gedcom_unsupported": result.unsupported_tags,
        }
        return result.batch

    def _step_transcribe(self, doc: DocumentRun):
        from yizkr.pipeline.language import resolve_language
        from yizkr.pipeline.transcribe import Transcription

        if doc.transcription.exists():
            return Transcription.model_validate_json(
                doc.transcription.read_text()
            )
        if doc.source.suffix.lower() in PRESENTATION_SUFFIXES:
            # deterministic: slide text is data in the file, not an
            # image to read; [Slide N] markers scope the claim locators
            from yizkr.pipeline.presentation import slide_transcript

            text = slide_transcript(self._presentation(doc))
            resolution = resolve_language(
                text, self.language, fallback=self.fallback_language
            )
            transcription = Transcription(
                artifact_qid=doc.state.artifact_qid,
                text=text,
                language=resolution.code,
                engine="pptx-adapter",
            )
        else:
            if doc.source.suffix.lower() in TEXT_SUFFIXES:
                from yizkr.pipeline.transcribe import TextFileTranscriber

                transcriber = TextFileTranscriber(
                    doc.source,
                    language=self.language,
                    fallback_language=self.fallback_language,
                )
            else:
                from yizkr.pipeline.llm import make_client
                from yizkr.pipeline.transcribe import LlmVisionTranscriber

                transcriber = LlmVisionTranscriber(
                    make_client(self.llm, effort=self.extract_effort),
                    language=self.language,
                    fallback_language=self.fallback_language,
                )
            transcription = transcriber.transcribe(
                doc.source, artifact_qid=doc.state.artifact_qid
            )
            resolution = transcriber.last_resolution
        # detection is a per-document choice with real graph effect (the
        # excerpt language tag): record it, and surface it when it was
        # inferred rather than forced, so a wrong guess is reviewable.
        if resolution is not None:
            doc.state.detail = (doc.state.detail or {}) | {
                "language": {
                    "code": resolution.code,
                    "source": resolution.source,
                    "confidence": resolution.confidence,
                }
            }
            if self.language == "auto":
                self.log(
                    f"  {doc.source.name}: language {resolution.describe()}"
                )
        doc.transcription.write_text(transcription.model_dump_json(indent=2))
        return transcription

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline.chunking import ChunkedClaimExtractor
        from yizkr.pipeline.llm import make_client

        # always entity-first (ADR-0007), even for documents that fit a
        # single call: reconciliation NEEDS the entity manifest, and a
        # small document without one silently mints duplicates instead
        # of being adjudicated against the graph (found by the second
        # trial — a birth certificate's "Donald Bennett" must reach the
        # adjudicator, not become a second Donald). Chunking a small
        # document costs a few extra calls and is never incorrect
        # (ADR-0006 §7).
        extractor = ChunkedClaimExtractor(
            make_client(
                self.llm, effort=self.extract_effort, model=self.extract_model
            ),
            state_dir=doc.batch.with_name(doc.batch.name + ".chunks"),
            resume=True,
            concurrency=self.chunk_concurrency,
            log=self.log,
        )
        batch = extractor.extract(
            transcription, artifact_qid=doc.state.artifact_qid
        )
        failed = getattr(extractor, "failed_chunks", None)
        if failed:
            raise RuntimeError(
                f"{len(failed)} chunks failed after retries "
                f"({', '.join(failed)}); re-run to resume them"
            )
        return batch

    def _step_reconcile(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        from yizkr.pipeline.llm import make_client
        from yizkr.pipeline.reconcile import (
            adjudicate,
            block_candidates,
            export_equivalences_csv,
            fetch_place_containment,
            fetch_roster,
        )

        roster = fetch_roster(self.client, self.config)
        # the transitive place-containment closure lets blocking prune pairs
        # the graph proves impossible (a camp inside a country is not the
        # country) before adjudication (ADR-0017)
        containment = fetch_place_containment(self.client, self.config)
        candidates = block_candidates(
            batch.entities, roster, batch=batch, containment=containment
        )
        proposals = (
            adjudicate(
                make_client(self.llm, effort=self.review_effort),
                batch.entities,
                candidates,
                batch,
            )
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
            read_reviewed_aliases,
        )
        from yizkr.pipeline.resolve import resolve_batch

        if doc.equivalences_reviewed.exists():
            approved = read_approved_equivalences(doc.equivalences_reviewed)
            batch = apply_equivalences(
                batch,
                approved,
                read_reviewed_aliases(doc.equivalences_reviewed),
            )
            self.log(f"  applied {len(approved)} approved equivalences")
        _, item_labels = self.client.label_index()
        return resolve_batch(batch, item_labels)

    def _step_export_review(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        from yizkr.pipeline.review import export_review_csv

        return export_review_csv(
            batch,
            doc.review,
            critique_notes=self._critique_notes(doc),
        )

    # -- open-schema harness (ADR-0013) --------------------------------------

    def _surface_gaps(self, doc: DocumentRun) -> None:
        """Render the extractor's gap log to a per-document review CSV."""
        source = (
            doc.batch.with_name(doc.batch.name + ".chunks") / "gaps.ndjson"
        )
        if not source.exists():
            return
        gaps = [
            json.loads(line)
            for line in source.read_text().splitlines()
            if line.strip()
        ]
        if not gaps:
            return
        import csv as _csv

        with open(doc.gaps, "w", newline="") as handle:
            writer = _csv.DictWriter(
                handle,
                fieldnames=["subject", "predicate", "object", "evidence"],
            )
            writer.writeheader()
            writer.writerows(gaps)
        self.log(
            f"  gaps: {len(gaps)} relation(s) no property expresses "
            f"(review {doc.gaps.name})"
        )

    def _step_critique(self, doc: DocumentRun, batch: ClaimBatch):
        from yizkr.pipeline.critique import run_critique
        from yizkr.pipeline.llm import make_client

        findings = run_critique(
            make_client(self.llm, effort=self.review_effort), batch
        )
        doc.critique.write_text(
            json.dumps([f.model_dump() for f in findings], indent=2)
        )
        if findings:
            self.log(
                f"  critique: {len(findings)} concern(s) flagged for review"
            )
            for finding in findings[:6]:
                self.log(
                    f"    - {finding.target}: {finding.issue}", file=sys.stderr
                )
        return findings

    def _critique_notes(self, doc: DocumentRun) -> dict[str, str]:
        """entity local_id -> concatenated critic concerns, for the review
        CSV warning column."""
        if not doc.critique.exists():
            return {}
        notes: dict[str, str] = {}
        for finding in json.loads(doc.critique.read_text()):
            target = finding.get("target", "")
            # "e01", "e01 alias: X", or "claim: e01 prop" -> the entity id
            tokens = target.replace("claim:", "").split()
            entity_id = tokens[0] if tokens else ""
            if entity_id:
                note = f"critique: {finding.get('issue', '')}"
                notes[entity_id] = (
                    f"{notes[entity_id]}; {note}"
                    if entity_id in notes
                    else note
                )
        return notes

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

    def advance(
        self, doc: DocumentRun, *, stop_after_extract: bool = False
    ) -> str:
        """Advance one document as far as the gates allow; returns status.

        ``stop_after_extract`` returns once the batch (entity manifest)
        exists, before reconcile — the serial sequencer needs every
        document's manifest before it can order them."""
        try:
            return self._advance(doc, stop_after_extract=stop_after_extract)
        except Exception as exc:  # per-document isolation: others continue
            doc.state.status = FAILED
            doc.state.error = f"{type(exc).__name__}: {exc}"
            doc.save()
            self.log(
                f"  {doc.source.name}: FAILED — {doc.state.error}",
                file=sys.stderr,
            )
            return FAILED
        finally:
            doc.save()

    def _advance(
        self, doc: DocumentRun, *, stop_after_extract: bool = False
    ) -> str:
        doc.dir.mkdir(parents=True, exist_ok=True)
        doc.state.error = None

        content_sha1 = hashlib.sha1(doc.source.read_bytes()).hexdigest()

        # source bytes changed since this document was last processed: the
        # old artifact, claims, and reviews describe DIFFERENT content
        # (ADR-0010 — different bytes are a different artifact), so start
        # the document over rather than trusting stale derived state.
        if doc.state.content_sha1 and doc.state.content_sha1 != content_sha1:
            shutil.rmtree(doc.dir, ignore_errors=True)
            doc.dir.mkdir(parents=True, exist_ok=True)
            doc.state = DocumentState(
                file=str(doc.source), title=doc.source.stem
            )
            self.log(f"  {doc.source.name}: content changed; re-ingesting")
        doc.state.content_sha1 = content_sha1

        # content-identity dedup (ADR-0010): claim this file's bytes; if an
        # earlier document in this run — or one already ingested to
        # completion in a PRIOR run (see run()'s pre-seed) — already claimed
        # them, this is the same artifact and needs no register/transcribe/
        # extract/write. Runs every pass (before the artifact_qid guard) so
        # the canonical keeps its claim on re-runs and the decision is stable.
        canonical = self._seen_hashes.get(content_sha1)
        if canonical is not None and canonical != doc.source.name:
            doc.state.status = DUPLICATE
            doc.state.detail = (doc.state.detail or {}) | {
                "duplicate_of": canonical
            }
            self.log(
                f"  identical content to {canonical}; skipping (one artifact "
                f"per distinct file, ADR-0010)"
            )
            return DUPLICATE
        self._seen_hashes[content_sha1] = doc.source.name

        if doc.state.artifact_qid is None:
            # register is a wiki write; the lock keeps concurrent extract-all
            # documents (extract_concurrency > 1) from racing on the client.
            with self._wiki_lock:
                doc.state.artifact_qid = self._step_register(doc)
            doc.state.status = REGISTERED
            doc.save()
            self.log(f"  registered as {doc.state.artifact_qid}")

        if doc.source.suffix.lower() in PRESENTATION_SUFFIXES and not (
            doc.state.detail or {}
        ).get("derived_media"):
            with self._wiki_lock:  # register_derived_media also writes items
                derived = self._step_derive_media(doc)
            doc.state.detail = (doc.state.detail or {}) | {
                "derived_media": derived
            }
            doc.save()
            self.log(f"  derived media: {len(derived)} item(s)")

        if not doc.batch.exists():
            batch = self._step_batch(doc)
            # an upstream redo invalidates everything derived from the
            # old batch — stale machine outputs must never gate or write
            for stale in (doc.equivalences, doc.resolved, doc.review,
                          doc.approved, doc.write_result,
                          doc.gaps, doc.critique):  # fmt: skip
                stale.unlink(missing_ok=True)
            doc.batch.write_text(batch.model_dump_json(indent=2))
            doc.state.status = BATCHED
            doc.save()
            self.log(f"  batch: {len(batch.claims)} claims")
            # open-schema harness (ADR-0013): surface unmappable relations
            # and run the extraction critic once per batch generation
            self._surface_gaps(doc)
            if doc.source.suffix.lower() not in GEDCOM_SUFFIXES:
                self._step_critique(doc, batch)
        else:
            batch = ClaimBatch.model_validate_json(doc.batch.read_text())
            # a valid cached batch means extraction succeeded, so a
            # FAILED status is stale — left by a later step (e.g. a
            # write-time outage) on a prior run. Clear it to BATCHED so a
            # re-run reports the true state and flows on into reconcile,
            # instead of echoing the old failure forever (the downstream
            # gates re-derive the real status from here).
            if doc.state.status == FAILED:
                doc.state.status = BATCHED
                doc.save()

        # serial mode extracts every document first (so the sequencer has all
        # the manifests), then processes them in order — stop here on the
        # extract-only pass, before any reconcile touches the graph.
        if stop_after_extract:
            return doc.state.status

        if batch.entities and not _fresh(doc.equivalences, doc.batch):
            proposals = self._step_reconcile(doc, batch)
            self.log(f"  reconcile: {proposals} proposals")

        needs_equivalence_review = (
            doc.equivalences.exists()
            and _csv_has_rows(doc.equivalences)
            and not _fresh(doc.equivalences_reviewed, doc.equivalences)
        )
        if needs_equivalence_review:
            doc.state.status = AWAITING_EQUIVALENCES
            self.log(
                f"  waiting: review {doc.equivalences} and save decisions as "
                f"{doc.equivalences_reviewed.name}"
                + (
                    " (existing reviewed file predates the current CSV)"
                    if doc.equivalences_reviewed.exists()
                    else ""
                )
            )
            return doc.state.status

        if not _fresh(doc.resolved, doc.batch, doc.equivalences_reviewed):
            resolved = self._step_resolve(doc, batch)
            doc.resolved.write_text(resolved.model_dump_json(indent=2))
            doc.state.status = RESOLVED
            doc.save()
        else:
            resolved = ClaimBatch.model_validate_json(doc.resolved.read_text())

        if not _fresh(doc.review, doc.resolved):
            rows = self._step_export_review(doc, resolved)
            self.log(f"  review CSV: {rows} rows")

        if not _fresh(doc.review_reviewed, doc.review):
            doc.state.status = AWAITING_CLAIMS
            self.log(
                f"  waiting: review {doc.review} and save decisions as "
                f"{doc.review_reviewed.name}"
                + (
                    " (existing reviewed file predates the current CSV)"
                    if doc.review_reviewed.exists()
                    else ""
                )
            )
            return doc.state.status

        if not _fresh(doc.write_result, doc.review_reviewed, doc.resolved):
            summary = self._step_write(doc, resolved)
            doc.write_result.write_text(json.dumps(summary, indent=2))
            doc.state.detail = (doc.state.detail or {}) | {"write": summary}
            self.log(
                f"  written: {summary['written']} new, "
                f"{summary['referenced']} referenced, "
                f"{summary['skipped']} skipped"
            )
        doc.state.status = WRITTEN
        return doc.state.status

    def _prior_written_hashes(self) -> dict[str, str]:
        """content SHA-1 -> filename of a document already ingested to
        completion in a prior run (ADR-0010).

        Lets a renamed identical file be recognised and skipped BEFORE any
        expensive step, not merely deduped at the graph by the writer.
        Reads persisted per-document state (runtime state in var/, not a
        second store of assertions), so it degrades gracefully: if that
        state is cleared, the file is simply re-extracted and the writer
        still dedups. Only WRITTEN documents seed the map — an unfinished
        prior run must not block a fresh attempt from completing.
        """
        seen: dict[str, str] = {}
        if not self.state_dir.exists():
            return seen
        for sub in sorted(self.state_dir.iterdir()):
            state_file = sub / "state.json"
            if not state_file.exists():
                continue
            try:
                state = DocumentState.load(state_file)
            except (TypeError, ValueError):  # malformed/foreign state: skip
                continue
            if state.status == WRITTEN and state.content_sha1:
                seen.setdefault(state.content_sha1, Path(state.file).name)
        return seen

    def run(self) -> int:
        docs = self.documents()
        if not docs:
            self.log(f"no supported documents in {self.folder}")
            return 0
        # seed with content already fully ingested in prior runs, so a
        # renamed copy skips the pipeline (cross-run dedup, ADR-0010)
        self._seen_hashes = self._prior_written_hashes()

        if self.serial:
            docs = self._serial_sequence(docs)
        for doc in docs:
            self.log(f"{doc.source.name} [{doc.state.status}]")
            status = self.advance(doc)
            # serial (ADR-0019): the next artifact must not reconcile until
            # this one is written, so stop at the first that isn't done. The
            # human clears its gate and re-runs to advance the sequence.
            if self.serial and status not in (WRITTEN, DUPLICATE):
                self.log(
                    f"  (serial) paused until {doc.source.name} is written"
                )
                break

        self.log("")
        for doc in docs:
            self.log(f"{doc.state.status:32} {doc.source.name}")
        if any(doc.state.status == FAILED for doc in docs):
            return 1
        if any(doc.state.status.startswith("awaiting") for doc in docs):
            return 2
        return 0

    def _serial_sequence(self, docs: list[DocumentRun]) -> list[DocumentRun]:
        """Extract every document, then order them by greedy entity overlap.

        Extraction must run for all documents up front (the sequencer reads
        their manifests); the actual reconcile/review/write then proceeds in
        the returned order, hubs first (ADR-0019)."""
        from yizkr.pipeline.sequencing import sequence_documents

        # Settle content-dedup (ADR-0010) BEFORE any parallel extraction, so
        # concurrent _advance calls only ever READ the seen-hashes map (the
        # first document in discovery order with a given hash is canonical) —
        # never two identical files both registering.
        for doc in docs:
            try:
                sha1 = hashlib.sha1(doc.source.read_bytes()).hexdigest()
            except OSError:
                continue
            self._seen_hashes.setdefault(sha1, doc.source.name)

        self._extract_all(docs)
        keyed = {doc.source.name: self._entity_keys(doc) for doc in docs}
        by_name = {doc.source.name: doc for doc in docs}
        ordered = [
            by_name[name] for name in sequence_documents(list(keyed.items()))
        ]
        self.log(
            "\nserial order (most connective first): "
            + ", ".join(doc.source.name for doc in ordered)
            + "\n"
        )
        return ordered

    def _extract_all(self, docs: list[DocumentRun]) -> None:
        """Extract every document up front (transcribe + chunked extract),
        `extract_concurrency` documents at a time. Chunks within a document
        already run `chunk_concurrency`-wide, so total concurrent LLM calls
        is the product — the caller keeps it sane for the engine's limits.
        Wiki writes stay serialised (the `_wiki_lock` in `_advance`)."""

        def extract_one(doc: DocumentRun) -> None:
            self.log(f"{doc.source.name} [{doc.state.status}]")
            self.advance(doc, stop_after_extract=True)

        if self.extract_concurrency == 1:
            for doc in docs:
                extract_one(doc)
            return
        with ThreadPoolExecutor(max_workers=self.extract_concurrency) as pool:
            list(pool.map(extract_one, docs))

    def _entity_keys(self, doc: DocumentRun) -> set[str]:
        """Name-token keys of a document's extractable entities, for the
        overlap sequencer — the same signal reconcile blocks on."""
        from yizkr.pipeline.reconcile import _name_tokens

        if not doc.batch.exists():
            return set()
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        keys: set[str] = set()
        for entity in batch.entities:
            if entity.extract:
                keys |= _name_tokens(entity.label, *entity.aliases)
        return keys


def _fresh(output: Path, *inputs: Path) -> bool:
    """Is `output` present and at least as new as every existing input?

    The make-style dependency rule behind safe re-runs: a redone
    upstream step (a corrected transcription, a re-reviewed CSV) makes
    everything downstream stale, including the human gates — a reviewed
    file older than the CSV it reviews is a decision about superseded
    rows, not consent for the current ones.
    """
    if not output.exists():
        return False
    output_mtime = output.stat().st_mtime
    return all(
        not path.exists() or path.stat().st_mtime <= output_mtime
        for path in inputs
    )


def _csv_has_rows(path: Path) -> bool:
    with open(path, newline="") as handle:
        next(handle, None)  # header
        return any(line.strip() for line in handle)
