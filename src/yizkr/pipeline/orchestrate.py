"""Folder/batch orchestration: `yizkr ingest run`.

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

A run refuses to start without a never-ingest list
(``$YIZKR_EXCLUDE_FILE``; an empty list is allowed), and a document on
the list is skipped at discovery, before any step reads it
(yizkr.core.corpus).

The stages before the gates (transcription, entity manifest, chunks,
batch, critique, and the GEDCOM adapter) read and write through the
stage store (yizkr.pipeline.stages): each output is keyed by its input
content and everything that shapes it, so a re-run reuses what is
unchanged and recomputes only what a change reaches. The transcription
is routed per document (vision, text file, slide text); a curator's
`<doc>/transcription.corrected.txt` routes it to the correction instead,
and invalidates exactly what depends on the text.

The document directory keeps plain-file views for reading and review:
`transcription.json`, `batch.json`, `critique.json` and the files the
later steps derive from them. Dependencies are make-style by content
hash (`derivations.json`): a changed batch invalidates every file
derived from it and moves the reviewed files aside as
`*.reviewed.superseded-<n>.csv`; a re-exported CSV whose content changed
moves its reviewed file aside too; and a reviewed file is consent only
for the CSV it was first seen with, so editing the reviewed CSV
re-writes and editing the CSV it reviews re-arms the gate.

Every directory made here has its `derivations.json` from its first run.
A directory with files (its state, a stage output, a derived file or a
review) but no `derivations.json` holds files that no content hash
records, and the shared stage store cannot vouch for them: its records
may come from another directory's run of the same document. Deriving
the stages would compute every one the store does not hold under its
current key, calling the engine for those that use one, and a batch that
came out different would move the directory's reviews aside. So the
document is held before any stage runs, with every file left as it is,
and its status, the run's events and its alerts say why, until a run
opts in (`rederive_unrecorded`). That run keeps a copy of each stage
output as `<stem>.superseded-<n><suffix>`, records the directory once
from its files' modification times, the only dependency information it
holds (a file older than an input is derived again, and a reviewed file
older than its CSV is moved aside; Derivations.adopt), and then derives
the stages again. Modification times are read nowhere else.

A reviewed file's PRESENCE is the gate: the orchestrator never
interprets a half-filled CSV as consent. Documents whose reconcile
produced no proposals skip the first gate (there is nothing to review).

The state directory is also the run directory (yizkr.pipeline.rundir):
`run.json`, `events.jsonl` with an event for every stage reuse, store
and failure, and `control.jsonl`, whose pause and abort requests stop
dispatch. It is runtime state — keep it under var/ or outside the repo.
Re-running a completed document is a no-op (the writer is idempotent).
"""

from __future__ import annotations

import filecmp
import json
import re
import shutil
import sys
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.corpus import (
    MISSING_LIST,
    ExclusionConfigError,
    ExclusionList,
    is_excluded,
)
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.rundir import RUN_END, RUN_INTERRUPTED, RunDir
from yizkr.pipeline.stages import (
    UNPINNED_MODEL,
    DeferredClient,
    DispatchStopped,
    Recompute,
    RecordingClient,
    StageFailed,
    StageResult,
    StageScope,
    StageStore,
    atomic_write_text,
    content_hash,
    doc_scope,
    manifest_hash,
    model_spec,
    sha1_file,
    sha256_file,
    stage_key,
)
from yizkr.pipeline.stageversions import BATCH_VOLATILE, STAGES

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
# the run's control file held the document's next stage
PAUSED = "paused"
# the document's directory has files but no derivations.json, so nothing
# records what they were derived from; the run left it untouched
UNRECORDED = "held: unrecorded outputs"
# the alert a held document raises
UNRECORDED_CHECK = "unrecorded outputs"
# the characters of a document's SHA-1 an alert names it by
ALERT_SUBJECT_CHARS = 12

# the stages the store holds for each kind of document, in order
_EXTRACTED = ("transcription", "entity_manifest", "chunk", "batch", "critique")
DOCUMENT_STAGES = {
    "gedcom": ("gedcom",),
    "presentation": _EXTRACTED,
    "text": _EXTRACTED,
    "media": _EXTRACTED,
}
# the engine roles whose stage keys hold the role's model
MODEL_ROLES = ("transcribe", "extract", "critique")


def document_kind(source: Path) -> str:
    """The kind of source a document is: a run's slices are kinds."""
    suffix = source.suffix.lower()
    if suffix in GEDCOM_SUFFIXES:
        return "gedcom"
    if suffix in PRESENTATION_SUFFIXES:
        return "presentation"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "media"


def route_transcription(source: Path, correction: Path | None = None) -> str:
    """The transcription method for a document, chosen deterministically.

    A curator's correction wins; otherwise the source decides: slide text
    is data in a deck, a text file is its own transcription, and anything
    else is read by the vision engine. The method, and only its version,
    is part of the transcription's key, so a new or changed method
    recomputes only the documents routed to it."""
    if correction is not None and correction.is_file():
        return "correction"
    suffix = source.suffix.lower()
    if suffix in PRESENTATION_SUFFIXES:
        return "slide-text"
    if suffix in TEXT_SUFFIXES:
        return "text-file"
    return "vision"


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


class Derivations:
    """What each derived file of a document was made from, by content hash.

    The make-style dependency record behind safe re-runs, kept in
    `derivations.json`: a derived file is current while the inputs it was
    made from still hash the same; a reviewed CSV consents while neither
    it nor the CSV it reviews has changed since it was first seen; and the
    batch view's canonical content hash says whether a regenerated batch
    is really new. Modification times are read in one place only: `adopt`,
    once, for a directory that has files but no record, when a run opts in
    to deriving it again.
    """

    def __init__(
        self,
        path: Path,
        *,
        made_from: Mapping[Path, Sequence[Path]],
        reviews: Mapping[Path, Path],
    ) -> None:
        self.path = path
        # each derived file -> the files it is made from
        self.made_from = {out: tuple(ins) for out, ins in made_from.items()}
        # each reviewed CSV -> the CSV it decides on
        self.reviews = dict(reviews)

    def files(self) -> set[Path]:
        """Every file the record accounts for: each derived file, what it
        is made from, and each reviewed CSV and the CSV it reviews."""
        files = set(self.reviews) | set(self.reviews.values())
        for output, inputs in self.made_from.items():
            files.add(output)
            files.update(inputs)
        return files

    def begin(self) -> None:
        """Start an empty record for a directory that has none."""
        if not self.path.exists():
            self._save(self._load())

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        for section in ("derived", "reviews", "content"):
            data.setdefault(section, {})
        return data

    def _save(self, data: dict) -> None:
        atomic_write_text(
            self.path, json.dumps(data, indent=2, sort_keys=True)
        )

    def _inputs(self, output: Path) -> dict[str, str | None]:
        return {
            path.name: sha256_file(path) for path in self.made_from[output]
        }

    def _binding(self, reviewed: Path) -> dict[str, str | None]:
        source = self.reviews[reviewed]
        return {
            "reviewed": sha256_file(reviewed),
            "reviews": source.name,
            "source": sha256_file(source),
        }

    def adopt(self) -> list[Path]:
        """Record a directory that has files but no record, once; returns
        the reviewed CSVs it moved aside.

        Only the files' modification times say what such a directory's
        files were made from, so each file is judged by them once, on the
        directory as it stands, and by content hash from then on:

        * a derived file is recorded as current when no input it is made
          from is newer than it; any other stays unrecorded and is derived
          again;
        * a reviewed CSV is bound to the CSV it decides on when that CSV
          exists and is not newer than it. Otherwise its decisions are
          about rows that have since been replaced, so it is moved aside
          and consents to nothing.

        Every file is judged before any is moved, and the record is saved
        last: an adoption that stops part way leaves no record, so the
        next run adopts again rather than binding a review it had judged
        stale.

        Adoption covers only the files made from the batch and the
        reviews; it says nothing about the stages before the gates. Those
        are reused from the stage store only where it holds them under
        their current keys, and are computed again where it does not,
        calling the engine for those that use one. A batch that then
        differs from the directory's `batch.json` invalidates what
        adoption recorded and moves the reviews aside. So the orchestrator
        adopts a directory only when a run opts in to deriving its stages
        again, after keeping a copy of each stage output; otherwise it
        holds the document, and nothing is adopted (DocumentRun.unrecorded).
        """
        if self.path.exists():
            return []
        data = self._load()
        for output, inputs in self.made_from.items():
            if _no_input_newer(output, *inputs):
                data["derived"][output.name] = self._inputs(output)
        stale = []
        for reviewed, source in self.reviews.items():
            if not reviewed.exists():
                continue
            if source.exists() and _no_input_newer(reviewed, source):
                data["reviews"][reviewed.name] = self._binding(reviewed)
            else:
                stale.append(reviewed)
        for reviewed in stale:
            _supersede(reviewed)
        self._save(data)
        return stale

    def current(self, output: Path) -> bool:
        """Was `output` made from its inputs as they are now?"""
        if not output.exists():
            return False
        recorded = self._load()["derived"].get(output.name)
        return recorded == self._inputs(output)

    def record(self, output: Path) -> None:
        data = self._load()
        data["derived"][output.name] = self._inputs(output)
        self._save(data)

    def forget(self, *outputs: Path) -> None:
        data = self._load()
        for output in outputs:
            data["derived"].pop(output.name, None)
            data["reviews"].pop(output.name, None)
        self._save(data)

    def consents(self, reviewed: Path) -> bool:
        """Is `reviewed` a decision about the CSV it reviews, as it is now?

        A reviewed file seen for the first time, or saved again since, is
        bound to the CSV as it is now. While it stays unchanged it consents
        only as long as that CSV does too."""
        if not reviewed.exists():
            return False
        data = self._load()
        binding = data["reviews"].get(reviewed.name)
        if binding is None or binding.get("reviewed") != sha256_file(reviewed):
            data["reviews"][reviewed.name] = self._binding(reviewed)
            self._save(data)
            return True
        return binding.get("source") == sha256_file(self.reviews[reviewed])

    def bound_source(self, reviewed: Path) -> str | None:
        """The hash of the CSV `reviewed` was bound to, if it was seen."""
        binding = self._load()["reviews"].get(reviewed.name)
        return binding.get("source") if binding else None

    def content(self, name: str) -> str | None:
        return self._load()["content"].get(name)

    def set_content(self, name: str, digest: str) -> None:
        data = self._load()
        data["content"][name] = digest
        self._save(data)


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
        # a curator's corrected transcription (plain text); when present
        # it is the document's transcription
        self.correction = self.dir / "transcription.corrected.txt"
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
        self.derivations = Derivations(
            self.dir / "derivations.json",
            made_from={
                self.equivalences: (self.batch,),
                self.resolved: (self.batch, self.equivalences_reviewed),
                self.review: (self.resolved, self.critique),
                self.write_result: (self.review_reviewed, self.resolved),
            },
            reviews={
                self.equivalences_reviewed: self.equivalences,
                self.review_reviewed: self.review,
            },
        )
        # gaps the last extraction found, for the gap review CSV
        self.gaps_found: list[dict] = []
        self._sha1: str | None = None
        if self.state_path.exists():
            self.state = DocumentState.load(self.state_path)
        else:
            self.state = DocumentState(file=str(source), title=source.stem)

    @property
    def sha1(self) -> str:
        """The media SHA-1 of the source as it is now."""
        if self._sha1 is None:
            self._sha1 = sha1_file(self.source)
        return self._sha1

    @property
    def kind(self) -> str:
        return document_kind(self.source)

    @property
    def views(self) -> tuple[Path, ...]:
        """The plain-file views of the document's stage outputs."""
        if self.kind == "gedcom":
            return (self.batch,)
        return (self.transcription, self.batch, self.critique)

    @property
    def unrecorded(self) -> bool:
        """Does the directory hold files that no derivations.json records?

        Every directory the orchestrator makes has its record from its
        first run, so a directory without one but with a state.json, a
        stage view or any file the record accounts for was not made by it,
        or has lost its record. Nothing then says what its files were
        derived from. The stage store cannot say either: every run
        directory shares it, so a record there may come from another
        directory's run of the same document."""
        if self.derivations.path.exists():
            return False
        files = {self.state_path, *self.views, *self.derivations.files()}
        return any(path.exists() for path in files)

    def save(self) -> None:
        self.state.save(self.state_path)


def _write_view(path: Path, text: str) -> None:
    """Refresh a plain-file view, leaving an identical one untouched."""
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    atomic_write_text(path, text)


class Orchestrator:
    """Advances every document in a folder as far as review allows.

    Heavy operations live in small `_step_*` methods so the state
    machine is testable offline with scripted substitutes; engines come
    from `_llm`, which tests replace with a scripted model.
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
        exclusions: ExclusionList | None = None,
        store_dir: Path | None = None,
        recompute: Collection[str] = (),
        rederive_unrecorded: bool = False,
        log=print,
    ) -> None:
        self.folder = folder
        # the never-ingest list. run() refuses to start without one:
        # an empty ExclusionList is an explicit configuration, None is none.
        self.exclusions = exclusions
        self.state_dir = state_dir
        # stage outputs are shared across runs; a state directory without
        # a named store keeps its own. The name cannot be a document slug.
        self.store = StageStore(
            store_dir if store_dir is not None else state_dir / ".stages"
        )
        self.rundir = RunDir(state_dir)
        # stages to recompute once in this run even when a key matches
        self.recompute = Recompute(recompute)
        # adopt a document directory that has files but no derivations.json
        # and derive its stages again; without it such a document is held
        # (DocumentRun.unrecorded)
        self.rederive_unrecorded = rederive_unrecorded
        # documents held in this run, so each is reported once even when
        # serial mode advances it twice
        self._held: set[str] = set()
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

    # -- engines and stage scopes ------------------------------------------

    def _engine_settings(self, role: str) -> tuple[str, str | None]:
        """(effort, model override) for a role: transcribe, extract,
        critique."""
        if role == "critique":
            return self.review_effort, None
        if role == "extract":
            return self.extract_effort, self.extract_model
        return self.extract_effort, None

    def _llm(self, role: str, doc: DocumentRun):
        """The engine for one role on one document, started on first use."""
        from yizkr.pipeline.llm import make_client

        effort, model = self._engine_settings(role)
        return DeferredClient(
            lambda: make_client(self.llm, effort=effort, model=model)
        )

    def _model_spec(self, role: str) -> dict:
        """The model component of a role's stage keys. Only the
        claude-code engine takes an effort, so only it keys on one."""
        effort, model = self._engine_settings(role)
        return model_spec(
            self.llm,
            model=model,
            effort=effort if self.llm == "claude-code" else None,
        )

    def unpinned_roles(self) -> list[str]:
        """The roles whose stage keys name no model: neither the role's
        setting nor $YIZKR_LLM_MODEL sets one, so the engine runs its own
        default, and a change to that default leaves their keys as they
        were."""
        return [
            role
            for role in MODEL_ROLES
            if self._model_spec(role)["model"] == UNPINNED_MODEL
        ]

    def _warn_unpinned(self, docs: list[DocumentRun]) -> None:
        """Say which roles run an unpinned model, when any document's
        stages call the engine."""
        calls_engine = any(
            STAGES[stage].llm
            for doc in docs
            for stage in DOCUMENT_STAGES[doc.kind]
        )
        unpinned = self.unpinned_roles()
        if calls_engine and unpinned:
            self.log(
                f"warning: no model is pinned for {', '.join(unpinned)}, so "
                f"their stage keys hold {UNPINNED_MODEL!r}: if the "
                f"{self.llm} engine's own default model changes, outputs "
                f"the previous default made are still reused. Set "
                f"$YIZKR_LLM_MODEL to key them on a model id.",
                file=sys.stderr,
            )

    def _stages(self, doc: DocumentRun) -> StageScope:
        return StageScope(
            self.store,
            doc_scope(doc.sha1),
            events=self.rundir,
            recompute=self.recompute,
            kind=doc.kind,
        )

    # -- document discovery --------------------------------------------------

    def documents(self) -> list[DocumentRun]:
        files = []
        for path in sorted(self.folder.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            # the never-ingest list is asked first, before any step
            # reads the document, and whatever the file's type: an excluded
            # file gets no state, no artifact and no transcription
            if self.exclusions is not None and is_excluded(
                self.exclusions, path
            ):
                self.log(
                    f"excluding {path.name}: on the never-ingest list",
                    file=sys.stderr,
                )
            elif path.suffix.lower() not in SUPPORTED_SUFFIXES:
                self.log(
                    f"skipping {path.name}: unsupported file type",
                    file=sys.stderr,
                )
            else:
                files.append(path)
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
        # the adapter validates against the model manifest
        key = stage_key(
            "gedcom", inputs={"media": doc.sha1}, manifest=manifest_hash()
        )
        outcome = self._stages(doc).run(
            "gedcom",
            key,
            lambda: self._compute_gedcom(doc),
            artifact_qid=doc.state.artifact_qid,
        )
        meta = outcome.record.meta
        warnings = meta.get("warnings") or []
        unsupported = meta.get("unsupported") or {}
        if not outcome.reused:
            from yizkr.pipeline.gedcom import format_unsupported_tags

            for warning in warnings:
                self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
            if unsupported:
                self.log(
                    f"  {doc.source.name}: unsupported GEDCOM tags: "
                    + format_unsupported_tags(unsupported),
                    file=sys.stderr,
                )
        doc.state.detail = (doc.state.detail or {}) | {
            "gedcom_warnings": warnings,
            "gedcom_unsupported": unsupported,
        }
        return ClaimBatch.model_validate(
            outcome.record.bound_output(doc.state.artifact_qid)
        )

    def _compute_gedcom(self, doc: DocumentRun) -> StageResult:
        from yizkr.pipeline.gedcom import adapt_gedcom

        result = adapt_gedcom(
            doc.source.read_text(), artifact_qid=doc.state.artifact_qid
        )
        return StageResult(
            result.batch.model_dump(mode="json"),
            meta={
                "warnings": result.warnings,
                "unsupported": result.unsupported_tags,
            },
        )

    def _step_transcribe(self, doc: DocumentRun):
        from yizkr.pipeline.language import LanguageResolution
        from yizkr.pipeline.transcribe import (
            TRANSCRIPTION_PROMPT,
            Transcription,
        )

        method = route_transcription(doc.source, doc.correction)
        inputs = {"media": doc.sha1}
        if method == "correction":
            inputs["correction"] = sha256_file(doc.correction)
        vision = method == "vision"
        key = stage_key(
            "transcription",
            method=method,
            inputs=inputs,
            prompts={"vision": TRANSCRIPTION_PROMPT} if vision else None,
            model=self._model_spec("transcribe") if vision else None,
            params={
                "language": self.language,
                "fallback_language": self.fallback_language,
            },
        )
        outcome = self._stages(doc).run(
            "transcription",
            key,
            lambda: self._compute_transcription(doc, method),
        )
        output = outcome.record.output()
        transcription = Transcription.model_validate(
            output["transcription"] | {"artifact_qid": doc.state.artifact_qid}
        )
        # detection is a per-document choice with real graph effect (the
        # excerpt language tag): record it, and surface it when it was
        # inferred rather than forced, so a wrong guess is reviewable.
        language = output["language"]
        doc.state.detail = (doc.state.detail or {}) | {"language": language}
        if self.language == "auto" and not outcome.reused:
            self.log(
                f"  {doc.source.name}: language "
                f"{LanguageResolution(**language).describe()}"
            )
        _write_view(doc.transcription, transcription.model_dump_json(indent=2))
        return transcription

    def _compute_transcription(
        self, doc: DocumentRun, method: str
    ) -> StageResult:
        """The transcription stage: the routed method's text, its language
        and the raw engine responses. The stored transcription carries no
        artifact QID; it is bound when read."""
        transcribe = {
            "vision": self._transcribe_vision,
            "text-file": self._transcribe_text_file,
            "slide-text": self._transcribe_slides,
            "correction": self._transcribe_correction,
        }[method]
        transcription, resolution, raw, meta = transcribe(doc)
        return StageResult(
            {
                "transcription": transcription.model_dump(
                    mode="json", exclude={"artifact_qid"}
                ),
                "language": {
                    "code": resolution.code,
                    "source": resolution.source,
                    "confidence": resolution.confidence,
                },
            },
            raw=raw,
            meta=meta,
        )

    def _transcribe_vision(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import LlmVisionTranscriber

        client = RecordingClient(self._llm("transcribe", doc))
        transcriber = LlmVisionTranscriber(
            client,
            language=self.language,
            fallback_language=self.fallback_language,
        )
        try:
            transcription = transcriber.transcribe(doc.source)
        except Exception as exc:
            # the failure log keeps what the engine said, for diagnosis
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                raw=client.responses[-1] if client.responses else None,
            ) from exc
        return (
            transcription,
            transcriber.last_resolution,
            client.responses,
            {"model_identifier": client.model_identifier},
        )

    def _transcribe_text_file(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(
            doc.source,
            language=self.language,
            fallback_language=self.fallback_language,
        )
        transcription = transcriber.transcribe(doc.source)
        return transcription, transcriber.last_resolution, [], {}

    def _transcribe_slides(self, doc: DocumentRun):
        # deterministic: slide text is data in the file, not an image to
        # read; [Slide N] markers scope the claim locators
        from yizkr.pipeline.language import resolve_language
        from yizkr.pipeline.presentation import slide_transcript
        from yizkr.pipeline.transcribe import Transcription

        text = slide_transcript(self._presentation(doc))
        resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        transcription = Transcription(
            text=text, language=resolution.code, engine="pptx-adapter"
        )
        return transcription, resolution, [], {}

    def _transcribe_correction(self, doc: DocumentRun):
        from yizkr.pipeline.language import resolve_language
        from yizkr.pipeline.transcribe import Transcription

        text = doc.correction.read_text()
        resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        transcription = Transcription(
            text=text, language=resolution.code, engine="correction"
        )
        return transcription, resolution, [], {}

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline.chunking import ChunkedClaimExtractor

        # always entity-first (ADR-0007), even for documents that fit a
        # single call: reconciliation NEEDS the entity manifest, and a
        # small document without one silently mints duplicates instead
        # of being adjudicated against the graph — every entity must
        # reach the adjudicator, however short the document. Chunking a
        # small document costs a few extra calls and is never incorrect
        # (ADR-0006 §7).
        extractor = ChunkedClaimExtractor(
            self._llm("extract", doc),
            stages=self._stages(doc),
            model=self._model_spec("extract"),
            concurrency=self.chunk_concurrency,
            log=self.log,
        )
        batch = extractor.extract(
            transcription, artifact_qid=doc.state.artifact_qid
        )
        doc.gaps_found = extractor.gaps
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
        """Render the extraction's gap log to a per-document review CSV."""
        gaps = doc.gaps_found
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
        from yizkr.pipeline.critique import CRITIQUE_PROMPT, Finding

        key = stage_key(
            "critique",
            inputs={
                "batch": content_hash(
                    batch.model_dump(mode="json"),
                    volatile=BATCH_VOLATILE,
                    artifact_qid=doc.state.artifact_qid,
                )
            },
            prompts={"critique": CRITIQUE_PROMPT},
            model=self._model_spec("critique"),
        )
        outcome = self._stages(doc).run(
            "critique", key, lambda: self._compute_critique(doc, batch)
        )
        found = outcome.record.output()
        _write_view(doc.critique, json.dumps(found, indent=2))
        findings = [
            Finding.model_validate(
                {key: value for key, value in item.items() if key != "axis"}
            )
            for item in found
        ]
        if findings and not outcome.reused:
            self.log(
                f"  critique: {len(findings)} concern(s) flagged for review"
            )
            for finding in findings[:6]:
                self.log(
                    f"    - {finding.target}: {finding.issue}", file=sys.stderr
                )
        return findings

    def _compute_critique(
        self, doc: DocumentRun, batch: ClaimBatch
    ) -> StageResult:
        from yizkr.pipeline.critique import run_critique

        client = RecordingClient(self._llm("critique", doc))
        findings = run_critique(client, batch)
        return StageResult(
            [finding.model_dump(mode="json") for finding in findings],
            raw=client.responses,
            meta={"model_identifier": client.model_identifier}
            if client.responses
            else {},
        )

    def _critique_notes(self, doc: DocumentRun) -> dict[str, str]:
        """entity local_id -> concatenated critic concerns, for the review
        CSV `critique_notes` column. Each concern is tagged with the
        statement-part it doubts (ADR-0023): [subject] / [predicate] /
        [value], derived from the finding's target."""
        if not doc.critique.exists():
            return {}
        from yizkr.pipeline.critique import axis_of

        notes: dict[str, str] = {}
        for finding in json.loads(doc.critique.read_text()):
            target = finding.get("target", "")
            # "e01", "e01 alias: X", or "claim: e01 prop" -> the entity id
            tokens = target.replace("claim:", "").split()
            entity_id = tokens[0] if tokens else ""
            if entity_id:
                axis = axis_of(target)
                prefix = f"critique [{axis}]: " if axis else "critique: "
                note = prefix + finding.get("issue", "")
                notes[entity_id] = (
                    f"{notes[entity_id]}; {note}"
                    if entity_id in notes
                    else note
                )
        return notes

    def _step_write(self, doc: DocumentRun, batch: ClaimBatch) -> dict:
        from yizkr.pipeline.review import apply_review, is_bulk_accepted
        from yizkr.pipeline.writer import BatchWriter

        approved, warnings = apply_review(batch, doc.review_reviewed)
        for warning in warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        doc.approved.write_text(approved.model_dump_json(indent=2))
        result = BatchWriter(self.client).write_batch(
            approved, bulk_accepted=is_bulk_accepted(doc.review_reviewed)
        )
        return {
            "run_qid": result.run_qid,
            "written": result.written,
            "skipped": result.skipped,
            "referenced": result.referenced,
            "created_items": result.created_items,
            "approved_claims": len(approved.claims),
        }

    # -- views and derivations ----------------------------------------------

    def _hold_unrecorded(self, doc: DocumentRun) -> str:
        """Hold a document whose directory has files but no
        derivations.json (DocumentRun.unrecorded), with every file in it as
        it is, and say why: in state.json, in a `held` event for its first
        stage, in an alert and on stderr.

        An output is reused only on an exact key match or a recorded
        equivalence (ADR-0025), and the directory's own files have
        neither. Deriving the stages would compute every one the store does
        not hold under its current key, calling the engine for those that
        use one, and replace the views; a batch that came out different
        would then move the directory's reviews aside. Only a run that
        opts in does that (_adopt_unrecorded)."""
        names = [path.name for path in doc.views if path.exists()]
        calls_engine = any(
            STAGES[stage].llm for stage in DOCUMENT_STAGES[doc.kind]
        )
        outputs = f" (stage outputs: {', '.join(names)})" if names else ""
        engine = (
            ", calling the engine for each stage the store does not hold"
            if calls_engine
            else ""
        )
        reason = (
            f"no derivations.json records what the files in this directory "
            f"were derived from{outputs}. Nothing was changed. A run with "
            f"--rederive-unrecorded keeps a copy of each stage output (as "
            f"batch.superseded-<n>.json, and so on), adopts the directory "
            f"and derives every stage again{engine}; unless the new batch "
            f"has the same content as batch.json, the reviewed CSVs are then "
            f"moved aside."
        )
        held = (doc.state.detail or {}).get("held") or {}
        previous = (
            held.get("previous_status", doc.state.status)
            if doc.state.status == UNRECORDED
            else doc.state.status
        )
        doc.state.status = UNRECORDED
        doc.state.detail = (doc.state.detail or {}) | {
            "held": {
                "reason": reason,
                "outputs": names,
                "previous_status": previous,
            }
        }
        if doc.sha1 not in self._held:
            self._held.add(doc.sha1)
            stage = DOCUMENT_STAGES[doc.kind][0]
            self.rundir.emit(
                "held",
                subject=doc.sha1,
                kind=doc.kind,
                stage=stage,
                reason="unrecorded outputs: the directory has no "
                "derivations.json",
            )
            self.rundir.alert(
                stage=stage,
                check=UNRECORDED_CHECK,
                subject=doc.sha1[:ALERT_SUBJECT_CHARS],
                value=names,
                carried=False,
            )
            self.log(f"  {doc.source.name}: held: {reason}", file=sys.stderr)
        return UNRECORDED

    def _adopt_unrecorded(self, doc: DocumentRun) -> None:
        """Take a directory that has files but no derivations.json into
        the records, as a run that opts in asks.

        A copy of each stage output is kept first, beside it, as
        `<stem>.superseded-<n><suffix>`: those files are the only copy of
        what the earlier engine produced, and of what the reviews and the
        graph write were made from. The directory is then adopted by its
        files' modification times (Derivations.adopt), and the stages
        before the gates are derived again through the store."""
        kept = [
            f"{view.name} as {copy.name}"
            for view in doc.views
            if (copy := _supersede(view, keep=True)) is not None
        ]
        self.log(
            "  no derivations.json: "
            + (f"kept {', '.join(kept)}; " if kept else "")
            + "adopting the directory and deriving every stage again "
            "(--rederive-unrecorded)"
        )
        for reviewed in doc.derivations.adopt():
            self.log(
                f"  {reviewed.name} does not review the current "
                f"{doc.derivations.reviews[reviewed].name}: moved aside "
                f"(review the current rows)"
            )

    def _update_batch_view(self, doc: DocumentRun, batch: ClaimBatch) -> bool:
        """Write `batch.json` for the current batch; True when its content
        differs from the batch the document's derived files were made from.

        Content is compared by canonical hash (run timestamps left out),
        so an identical recompute, or a deleted view rebuilt from the
        store, invalidates nothing."""
        digest = content_hash(
            batch.model_dump(mode="json"), volatile=BATCH_VOLATILE
        )
        recorded = doc.derivations.content(doc.batch.name)
        if recorded is None and doc.batch.exists():
            try:
                existing = ClaimBatch.model_validate_json(
                    doc.batch.read_text()
                )
                recorded = content_hash(
                    existing.model_dump(mode="json"), volatile=BATCH_VOLATILE
                )
            except ValueError:
                recorded = None
        if recorded == digest:
            if not doc.batch.exists():
                atomic_write_text(doc.batch, batch.model_dump_json(indent=2))
            doc.derivations.set_content(doc.batch.name, digest)
            return False
        atomic_write_text(doc.batch, batch.model_dump_json(indent=2))
        doc.derivations.set_content(doc.batch.name, digest)
        return True

    def _export(
        self,
        doc: DocumentRun,
        target: Path,
        reviewed: Path,
        produce: Callable[[], Any],
    ) -> Any:
        """(Re)write a CSV a human reviews. When its content differs from
        the CSV the reviewed file decided on, move the reviewed file aside;
        an identical re-export keeps it."""
        before = doc.derivations.bound_source(reviewed) or sha256_file(target)
        result = produce()
        if sha256_file(target) != before and reviewed.exists():
            _supersede(reviewed)
            doc.derivations.forget(reviewed)
            self.log(
                f"  {target.name} changed: {reviewed.name} moved aside "
                f"(review the new rows)"
            )
        return result

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
        except DispatchStopped as exc:
            # held by the control file: nothing failed, nothing is lost
            doc.state.status = PAUSED
            self.log(f"  {doc.source.name}: {exc}")
            return PAUSED
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

        content_sha1 = doc.sha1

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

        # a directory made here is recorded from its first run, before any
        # file is written to it, so only a directory the orchestrator did
        # not make, or one that lost its record, is ever unrecorded
        if not doc.unrecorded:
            doc.derivations.begin()

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

        # files that no record accounts for hold the document here, whatever
        # the store holds, before any wiki or engine call and before any
        # file in its directory is moved or replaced; a run that opts in
        # keeps a copy of the stage outputs and adopts the directory
        if doc.unrecorded:
            if not self.rederive_unrecorded:
                return self._hold_unrecorded(doc)
            self._adopt_unrecorded(doc)
        if "held" in (doc.state.detail or {}):
            doc.state.detail = {
                key: value
                for key, value in doc.state.detail.items()
                if key != "held"
            }

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

        # every stage before the gates goes through the store: unchanged
        # outputs are reused, so this costs nothing when nothing changed
        batch = self._step_batch(doc)
        if self._update_batch_view(doc, batch):
            # a new batch invalidates everything derived from the old one
            # — stale machine outputs must never gate or write
            stale = (doc.equivalences, doc.resolved, doc.review,
                     doc.approved, doc.write_result,
                     doc.gaps, doc.critique)  # fmt: skip
            for path in stale:
                path.unlink(missing_ok=True)
            doc.derivations.forget(*stale)
            # the reviewed files decide rows of the OLD batch: move them
            # aside, never deleting a human's decisions
            reviews = (doc.equivalences_reviewed, doc.review_reviewed)
            moved = [reviewed for reviewed in reviews if reviewed.exists()]
            for reviewed in moved:
                _supersede(reviewed)
            doc.derivations.forget(*reviews)
            doc.state.status = BATCHED
            doc.save()
            self.log(f"  batch: {len(batch.claims)} claims")
            if moved:
                self.log(
                    f"  the batch changed: "
                    f"{', '.join(reviewed.name for reviewed in moved)} moved "
                    f"aside (review the new rows)"
                )
            # open-schema harness (ADR-0013): surface unmappable relations
            self._surface_gaps(doc)
        elif doc.state.status in (FAILED, PAUSED, REGISTERED, UNRECORDED):
            # the batch stands, so an earlier failure or hold is over; the
            # gates below re-derive the real status
            doc.state.status = BATCHED
            doc.save()
        # the extraction critic reviews each batch once (ADR-0013); the
        # store reuses its findings while the batch is unchanged
        if doc.source.suffix.lower() not in GEDCOM_SUFFIXES:
            self._step_critique(doc, batch)

        # serial mode extracts every document first (so the sequencer has all
        # the manifests), then processes them in order — stop here on the
        # extract-only pass, before any reconcile touches the graph.
        if stop_after_extract:
            return doc.state.status

        derivations = doc.derivations
        if batch.entities and not derivations.current(doc.equivalences):
            proposals = self._export(
                doc,
                doc.equivalences,
                doc.equivalences_reviewed,
                lambda: self._step_reconcile(doc, batch),
            )
            derivations.record(doc.equivalences)
            self.log(f"  reconcile: {proposals} proposals")

        needs_equivalence_review = (
            doc.equivalences.exists()
            and _csv_has_rows(doc.equivalences)
            and not derivations.consents(doc.equivalences_reviewed)
        )
        if needs_equivalence_review:
            doc.state.status = AWAITING_EQUIVALENCES
            self.log(
                f"  waiting: review {doc.equivalences} and save decisions as "
                f"{doc.equivalences_reviewed.name}"
                + (
                    " (the CSV changed after the existing reviewed file "
                    "was saved)"
                    if doc.equivalences_reviewed.exists()
                    else ""
                )
            )
            return doc.state.status

        if not derivations.current(doc.resolved):
            resolved = self._step_resolve(doc, batch)
            atomic_write_text(doc.resolved, resolved.model_dump_json(indent=2))
            derivations.record(doc.resolved)
            doc.state.status = RESOLVED
            doc.save()
        else:
            resolved = ClaimBatch.model_validate_json(doc.resolved.read_text())

        if not derivations.current(doc.review):
            rows = self._export(
                doc,
                doc.review,
                doc.review_reviewed,
                lambda: self._step_export_review(doc, resolved),
            )
            derivations.record(doc.review)
            self.log(f"  review CSV: {rows} rows")

        if not derivations.consents(doc.review_reviewed):
            doc.state.status = AWAITING_CLAIMS
            self.log(
                f"  waiting: review {doc.review} and save decisions as "
                f"{doc.review_reviewed.name}"
                + (
                    " (the CSV changed after the existing reviewed file "
                    "was saved)"
                    if doc.review_reviewed.exists()
                    else ""
                )
            )
            return doc.state.status

        if not derivations.current(doc.write_result):
            summary = self._step_write(doc, resolved)
            atomic_write_text(doc.write_result, json.dumps(summary, indent=2))
            derivations.record(doc.write_result)
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
        still dedups. Only documents written to the graph seed the map
        (WRITTEN ones, and ones held for unrecorded outputs after they were
        written) — an unfinished prior run must not block a fresh attempt
        from completing.
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
            if _was_written(state) and state.content_sha1:
                seen.setdefault(state.content_sha1, Path(state.file).name)
        return seen

    def _start_run(self, docs: list[DocumentRun]) -> None:
        """Record the run's plan and configuration in the run manifest."""
        self.rundir.start(
            mode="ingest run",
            store=str(self.store.root),
            config={
                "engine": self.llm,
                "language": self.language,
                "fallback_language": self.fallback_language,
                "extract_effort": self.extract_effort,
                "review_effort": self.review_effort,
                "extract_model": self.extract_model,
                "recompute": sorted(self.recompute.stages),
                "rederive_unrecorded": self.rederive_unrecorded,
            },
            documents=[
                {
                    "sha1": doc.sha1,
                    "file": doc.source.name,
                    "kind": doc.kind,
                    "stages": list(DOCUMENT_STAGES[doc.kind]),
                }
                for doc in docs
            ],
        )

    def run(self) -> int:
        # fail closed: extract-all work never starts without a
        # never-ingest list, so a missing list cannot pass as an empty one
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        docs = self.documents()
        if not docs:
            self.log(f"no supported documents in {self.folder}")
            return 0
        self._warn_unpinned(docs)
        self._held = set()
        # seed with content already fully ingested in prior runs, so a
        # renamed copy skips the pipeline (cross-run dedup, ADR-0010)
        self._seen_hashes = self._prior_written_hashes()
        self._start_run(docs)

        try:
            if self.serial:
                docs = self._serial_sequence(docs)
            for doc in docs:
                control = self.rundir.control()
                if control.stopped():
                    self.log(f"run stopped ({control.stopped()})")
                    break
                held = control.stopped(kind=doc.kind)
                if held:
                    self.log(f"{doc.source.name}: not dispatched ({held})")
                    continue
                self.log(f"{doc.source.name} [{doc.state.status}]")
                status = self.advance(doc)
                # serial (ADR-0019): the next artifact must not reconcile
                # until this one is written, so stop at the first that isn't
                # done. The human clears its gate and re-runs to advance.
                if self.serial and status not in (WRITTEN, DUPLICATE):
                    self.log(
                        f"  (serial) paused: {doc.source.name} is held until "
                        f"a run with --rederive-unrecorded derives it again"
                        if status == UNRECORDED
                        else f"  (serial) paused until {doc.source.name} is "
                        f"written"
                    )
                    break
        except KeyboardInterrupt:
            self.rundir.emit(RUN_INTERRUPTED)
            raise

        statuses: dict[str, int] = {}
        self.log("")
        for doc in docs:
            self.log(f"{doc.state.status:32} {doc.source.name}")
            statuses[doc.state.status] = statuses.get(doc.state.status, 0) + 1
        self.rundir.emit(RUN_END, statuses=statuses)
        if any(doc.state.status == FAILED for doc in docs):
            return 1
        if any(
            doc.state.status.startswith("awaiting")
            or doc.state.status in (PAUSED, UNRECORDED)
            for doc in docs
        ):
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
                sha1 = doc.sha1
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


def _was_written(state: DocumentState) -> bool:
    """Was the document written to the graph? It is WRITTEN, or it was
    WRITTEN when it was held for unrecorded outputs."""
    if state.status != UNRECORDED:
        return state.status == WRITTEN
    held = state.detail.get("held") if isinstance(state.detail, dict) else None
    return isinstance(held, dict) and held.get("previous_status") == WRITTEN


def _no_input_newer(output: Path, *inputs: Path) -> bool:
    """Is `output` present, with no existing input modified after it?

    The only dependency information a directory without derivation
    records holds, read by `Derivations.adopt` alone. A tie counts as not
    newer."""
    if not output.exists():
        return False
    made = output.stat().st_mtime_ns
    return all(
        not path.exists() or path.stat().st_mtime_ns <= made for path in inputs
    )


def _supersede(path: Path, *, keep: bool = False) -> Path | None:
    """Set a superseded file aside, never deleting it: `review.reviewed.csv`
    becomes `review.reviewed.superseded-<n>.csv` (first free n). Moves it,
    so a review about superseded rows consents to nothing; with `keep`,
    copies it there instead (times included) and leaves it in place, and a
    copy identical to the latest one is not made twice. Returns where the
    file went, or None when there was none."""
    if not path.exists():
        return None
    n = 1
    while (target := _superseded(path, n)).exists():
        n += 1
    if not keep:
        path.rename(target)
        return target
    latest = _superseded(path, n - 1)
    if n > 1 and filecmp.cmp(path, latest, shallow=False):
        return latest
    shutil.copy2(path, target)
    return target


def _superseded(path: Path, n: int) -> Path:
    return path.with_name(f"{path.stem}.superseded-{n}{path.suffix}")


def _csv_has_rows(path: Path) -> bool:
    with open(path, newline="") as handle:
        next(handle, None)  # header
        return any(line.strip() for line in handle)
