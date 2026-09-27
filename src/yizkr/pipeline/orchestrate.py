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

The stages before the gates make a document's views, kept in its
directory: the transcription (`transcription.json`), the batch
(`batch.json`, from extraction or from the GEDCOM adapter) and the
critique (`critique.json`). A view is reused while the inputs recorded
for it are unchanged, and made again when they change, or when
`--refresh` names the role that makes it (MODEL_ROLES), which makes it
again in place. A changed prompt, model or piece of code does not by
itself make a view stale; a scratch run starts from fresh state
directories instead. The one check a view must still pass is the
transcription check: a vision or text-file transcription is checked
again whenever it is reused, and one that now fails is moved aside and
made again in the run. The model calls a view is made with go through the
LLM cache when the run has one (ADR-0032): an accelerator, which the
pipeline runs the same without. The client factory (yizkr.pipeline.llm)
wraps the run's engine clients in it; the orchestrator only names the
cache folder, the refreshed roles and the epochs, and records the cache's
events in the run directory. The transcription is routed per document
(vision, text file, slide text); a curator's
`<doc>/transcription.corrected.txt` routes it to the correction instead.
The PDF renderer the vision engine and the transcription check need is
checked once, before any document.

Dependencies are make-style by content hash (`derivations.json`): the
transcription is made from the correction, when there is one; the batch
from the transcription; the critique from the batch; and the later
steps' files from those. A transcription or a batch counts by its
canonical content (the transcription's text, language and artifact; the
batch without its run stamps), so an identical recompute, such as a
correction that says what the transcription said, invalidates nothing.
A changed batch invalidates every file derived from it and moves the
reviewed files aside as `*.reviewed.superseded-<n>.csv`; a re-exported
CSV whose content changed moves its reviewed file aside too; and a
reviewed file is consent only for the CSV it was first seen with, so
editing the reviewed CSV re-writes and editing the CSV it reviews
re-arms the gate.

Every directory made here has its `derivations.json` from its first run:
it is written before any other file, and no state is saved into a
directory without it. A directory with files but no `derivations.json`
was made before the records, or has lost its record, so nothing says
what its files were derived from; one with files beside its record but
no `state.json` has lost the registration its views were made for. It
is set aside whole, as `.<slug>.superseded-<n>` in the state directory,
and the document starts afresh in a new one, as a document whose source
bytes changed does (ADR-0010: different bytes are a different artifact).
Nothing in the old directory is deleted, and it no longer counts for
content dedup. Modification times are read nowhere.

A reviewed file's PRESENCE is the gate: the orchestrator never
interprets a half-filled CSV as consent. Documents whose reconcile
produced no proposals skip the first gate (there is nothing to review).

The state directory is also the run directory (yizkr.pipeline.rundir):
`run.json`, `events.jsonl` with an event for every stage start,
completion, failure and reused view and every answer the cache gives or
cannot give, and `control.jsonl`, whose pause and abort requests stop
dispatch. It is runtime state — keep it under var/ or outside the repo.
Re-running a completed document changes nothing (the writer is
idempotent).

`run.json` is the run manifest (docs/RUN_MANIFEST.md): what shapes the
run, as its latest invocation ran it. Each invocation records its mode
and project stage, the engine, each role's model, effort, epoch and
refresh, the LLM cache, the corpus folder and its never-ingest list's
hash, the code version, and its start, end and outcome; each role's
engine client adds its configuration hash, and the claude CLI its pinned
version, when it starts, which is inside the first stage that calls it.
A run refuses a model named by an alias, since neither the manifest nor
the cache's keys could say which model answered. Without
`fixed_settings` an invocation may change the settings, as `ingest run`
allows between runs over one state directory; with it, an invocation
that asks for other settings than the run recorded is refused.
"""

from __future__ import annotations

import functools
import json
import os
import re
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
from yizkr.pipeline.rundir import (
    DEVELOPMENT_STAGE,
    RUN_END,
    RUN_INTERRUPTED,
    SET_ASIDE,
    DispatchStopped,
    ModelAliasRefused,
    RunDir,
    RunSettingsConflict,
    code_version,
    exclusions_record,
    model_alias,
)
from yizkr.pipeline.stages import (
    atomic_write_text,
    content_hash,
    sha1_file,
    sha256_file,
)

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

# the stages each kind of document runs before the gates, in order
_EXTRACTED = ("transcription", "extraction", "critique")
DOCUMENT_STAGES = {
    "gedcom": ("gedcom",),
    "presentation": _EXTRACTED,
    "text": _EXTRACTED,
    "media": _EXTRACTED,
}
# the roles the engine is called in: each has its own effort and model,
# and its own epoch and refresh setting in the LLM cache
MODEL_ROLES = ("transcribe", "extract", "critique")
# the model a role runs when the run names none of its own; the engines
# read it themselves (yizkr.pipeline.llm)
MODEL_VARIABLE = "YIZKR_LLM_MODEL"

# fields of a claim batch that differ between two identical recomputes
# (when, which commit, which engine build), left out of its content hash
BATCH_VOLATILE = (
    "extraction_run.started_at",
    "extraction_run.software_commit",
    "extraction_run.model_identifier",
    "extraction_run.pipeline_version",
)
# the field of a transcription that names what made it (the route, and
# the engine build), left out of its content hash: extraction reads only
# the text, its language and the artifact
TRANSCRIPTION_VOLATILE = ("engine",)
# the event that says a stage's view was current and stands
REUSED = "reused"


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
    else is read by the vision engine."""
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
    `derivations.json`: a derived file, a view included, is current while
    the inputs it was made from still hash the same; a reviewed CSV
    consents while neither it nor the CSV it reviews has changed since it
    was first seen; and the canonical content hashes of the transcription
    and batch views say whether a view made again is really new.
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
        """The files `output` is made from, each by its canonical content
        hash where one is recorded (the transcription's and the batch's:
        what made them, and when, differ between identical recomputes),
        and by its bytes otherwise; a missing file is None."""
        content = self._load()["content"]
        return {
            path.name: content.get(path.name) or sha256_file(path)
            for path in self.made_from[output]
        }

    def _binding(self, reviewed: Path) -> dict[str, str | None]:
        source = self.reviews[reviewed]
        return {
            "reviewed": sha256_file(reviewed),
            "reviews": source.name,
            "source": sha256_file(source),
        }

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
        """Forget what `outputs` were made from, and their content."""
        data = self._load()
        for output in outputs:
            for section in ("derived", "reviews", "content"):
                data[section].pop(output.name, None)
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
        # the extractor's advisory side outputs: coverage, rejects, gaps
        self.extraction = self.dir / "extraction"
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
        gedcom = self.kind == "gedcom"
        # each view the stages before the gates make -> (the model role
        # that makes it, the stage that makes it)
        self.views = {
            self.transcription: ("transcribe", "transcription"),
            self.batch: ("extract", "gedcom" if gedcom else "extraction"),
            self.critique: ("critique", "critique"),
        }
        self.derivations = Derivations(
            self.dir / "derivations.json",
            made_from={
                self.transcription: (self.correction,),
                # the GEDCOM adapter reads the source alone, whose bytes
                # the directory is kept for (a changed source sets it
                # aside)
                self.batch: () if gedcom else (self.transcription,),
                self.critique: (self.batch,),
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
    def unrecorded(self) -> bool:
        """Does the directory hold files nothing records the making of:
        anything without the derivations.json that says what its files
        were derived from, or files beside it without the state.json that
        names the artifact they were made for?

        Every directory the orchestrator makes has its record from its
        first run, and its state from its registration, before any view
        is made; one without them was made before the records, or has lost
        them."""
        if not self.dir.is_dir():
            return False
        if not self.derivations.path.exists():
            return any(self.dir.iterdir())
        if self.state_path.exists():
            return False
        return any(
            path != self.derivations.path for path in self.dir.iterdir()
        )

    def save(self) -> None:
        """Save the document's state, only into a directory with its
        record: state without one would make the directory unrecorded, and
        a run stopped before a new directory's record is written leaves no
        state in it."""
        if self.derivations.path.exists():
            self.state.save(self.state_path)


def _write_view(path: Path, text: str) -> bool:
    """Refresh a plain-file view, leaving an identical one untouched;
    returns whether the file changed."""
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    atomic_write_text(path, text)
    return True


class Orchestrator:
    """Advances every document in a folder as far as review allows.

    Heavy operations live in small `_step_*` methods so the state
    machine is testable offline with scripted substitutes; engines come
    from `_llm`, which tests replace with a scripted model.

    `cache_dir` names the LLM cache (ADR-0032); None calls the engine
    directly. `refresh` names roles (MODEL_ROLES) whose views are made
    again in this run, current or not, with calls that go to the engine
    whatever the cache holds. `epochs` gives roles an epoch other than 0:
    it changes the keys of their calls, so a view made again after it is
    asked afresh, while a current view stands (combine it with `refresh`
    to re-sample the views themselves).

    `fixed_settings` makes the run keep the settings its manifest records
    (rundir.SETTINGS): a later invocation that asks for others, or whose
    engine applies others, a claude CLI other than the version the run
    recorded included, is refused (RunSettingsConflict). Without it, as
    for `ingest run`, each invocation records its own.
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
        cache_dir: Path | None = None,
        refresh: Collection[str] = (),
        epochs: Mapping[str, int] | None = None,
        fixed_settings: bool = False,
        log=print,
    ) -> None:
        self.folder = folder
        self.fixed_settings = fixed_settings
        # the never-ingest list. run() refuses to start without one:
        # an empty ExclusionList is an explicit configuration, None is none.
        self.exclusions = exclusions
        self.state_dir = state_dir
        self.rundir = RunDir(state_dir)
        self.cache_dir = cache_dir
        self.refresh = tuple(sorted(set(refresh)))
        self.epochs = dict(epochs or {})
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
        # media SHA-1 -> the batch derived in this run, so that serial
        # mode's second pass over a document derives nothing again
        self._derived: dict[str, ClaimBatch] = {}
        # (media SHA-1, view name) -> the view as made or reused in this
        # run: each view is decided once per run, a refreshed one made
        # again once
        self._views: dict[tuple[str, str], Any] = {}
        # the run's engine client per role, and its LLM cache, each made
        # on first use and closed when the run ends
        self._engines: dict[str, Any] = {}
        self._cache: Any = None
        self._engine_lock = threading.Lock()
        # the run-scope control request that held a stage in this run, if
        # one did: the run was stopped even when no document was left to
        # dispatch after it
        self._run_held: str | None = None

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

    # -- engines --------------------------------------------------------------

    def _engine_settings(self, role: str) -> tuple[str, str | None]:
        """(effort, model override) for a role: transcribe, extract,
        critique."""
        if role == "critique":
            return self.review_effort, None
        if role == "extract":
            return self.extract_effort, self.extract_model
        return self.extract_effort, None

    def _configured_model(self, role: str) -> tuple[str | None, str]:
        """(model, where it is configured) for a role: its own model (the
        extraction model), else $YIZKR_LLM_MODEL; None when neither names
        one and the engine runs a default of its own."""
        _effort, model = self._engine_settings(role)
        if model:
            return model, "the extraction model (--extract-model)"
        return os.environ.get(MODEL_VARIABLE) or None, f"${MODEL_VARIABLE}"

    def _refuse_model_aliases(self) -> None:
        """Refuse a run whose configuration names a model by an alias
        (rundir.model_alias): the engine would choose the model, and
        neither the run manifest nor the LLM cache's keys could say
        which."""
        refused = {}
        for role in MODEL_ROLES:
            model, where = self._configured_model(role)
            if model_alias(model):
                refused.setdefault((where, model), []).append(role)
        if refused:
            raise ModelAliasRefused(
                "; ".join(
                    f"{where} names the model {model!r} for "
                    f"{', '.join(roles)}, an alias"
                    for (where, model), roles in refused.items()
                )
                + ". The engine resolves an alias to a model of its own "
                "choosing, which neither the run manifest nor the LLM "
                "cache's keys can name. Configure the full model id "
                "instead, such as claude-sonnet-5."
            )

    def _engine(self, role: str) -> Any:
        """The run's engine client for a role, started on first use: one
        per role serves every document, calls in parallel included. What
        the client applies (its model, effort and configuration hash) and
        its CLI version go into the run manifest when it starts; a run
        that keeps its settings refuses a client that applies others, or
        runs another CLI version than the run recorded, before any call
        (RunSettingsConflict). The client is given no version to run: it
        pins the one installed, so a CLI updated since an earlier
        invocation is refused as another setting, while one updated
        during this process pauses the pass (llm.CliVersionMismatch)."""
        with self._engine_lock:
            if role not in self._engines:
                from yizkr.pipeline.llm import make_client

                effort, model = self._engine_settings(role)
                engine = make_client(self.llm, effort=effort, model=model)
                try:
                    self.rundir.record_engine(
                        role,
                        {
                            "model": _text(engine, "model"),
                            "effort": _text(engine, "effort"),
                            "config_hash": _text(engine, "config_hash"),
                            "model_identifier": _text(
                                engine, "model_identifier"
                            ),
                        },
                        cli_version=_text(engine, "cli_version"),
                        fixed_settings=self.fixed_settings,
                    )
                except BaseException:
                    close = getattr(engine, "close", None)
                    if callable(close):
                        close()
                    raise
                self._engines[role] = engine
            return self._engines[role]

    def _llm_cache(self) -> Any:
        """The run's LLM cache, opened on first use; None when the run
        calls the engine directly."""
        if self.cache_dir is None:
            return None
        with self._engine_lock:
            if self._cache is None:
                from yizkr.pipeline.llm import open_cache

                self._cache = open_cache(
                    self.cache_dir, refresh=self.refresh, epochs=self.epochs
                )
            return self._cache

    def role_client(
        self, role: str, *, events: Callable[..., Any] | None = None
    ) -> Any:
        """The client this run calls the engine through in `role`
        (MODEL_ROLES): the run's engine client for the role, with the
        role's effort and model and the pinned CLI, behind the run's LLM
        cache when it has one, keyed with the role's epoch. `events`
        receives what the cache does.

        A tool that adopts answers for the run's calls (CachingClient.
        complete_key and seed) takes its client from an orchestrator
        configured as the run is, so that its keys are the ones the run's
        calls are served under. A CLI update changes the engine's config
        hash, and with it every key: adoptions made before it are no
        longer found, and those calls miss as absent."""
        engine = self._engine(role)
        cache = self._llm_cache()
        if cache is None:
            return engine
        from yizkr.pipeline.llm import cached_client

        return cached_client(engine, cache, role=role, events=events)

    def _llm(self, role: str, doc: DocumentRun):
        """The engine for one role, as one document's stages see it
        (`role_client`), with what the cache does reported to the run
        directory under the document's SHA-1."""
        return self.role_client(
            role,
            events=functools.partial(
                self.rundir.emit, subject=doc.sha1, kind=doc.kind
            ),
        )

    def _close_engines(self) -> None:
        """Release what the run's engines hold (the claude CLI's session
        transcripts), and forget them and the cache: the next run starts
        its own."""
        with self._engine_lock:
            engines = list(self._engines.values())
            self._engines = {}
            self._cache = None
        for engine in engines:
            close = getattr(engine, "close", None)
            if callable(close):
                close()

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

    # -- stages ---------------------------------------------------------------

    def _run_stage(
        self,
        doc: DocumentRun,
        stage: str,
        compute: Callable[[], Any],
        attention: Callable[[Any], Sequence[str]] | None = None,
    ) -> Any:
        """Run one stage of a document as the run directory records it.

        The stage is dispatched only while the control file lets it and
        the document's kind through (DispatchStopped otherwise), and an
        event says when it starts, when it is done (with the reasons its
        output needs attention, if any) and when it fails, with the
        reason. The reasons are kept in the document's state, for the
        runs that reuse the stage's view."""
        where = {"subject": doc.sha1, "kind": doc.kind, "stage": stage}
        control = self.rundir.control()
        stopped = control.stopped(stage=stage, kind=doc.kind)
        if stopped:
            if control.stopped():
                # the whole run is held, not only this stage or kind
                self._run_held = stopped
            self.rundir.emit("held", reason=stopped, **where)
            raise DispatchStopped(f"{stage} not dispatched: {stopped}")
        self.rundir.emit("started", **where)
        try:
            result = compute()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.rundir.emit("failed", reason=reason[:500], **where)
            raise
        reasons = list(attention(result)) if attention is not None else []
        detail = dict(doc.state.detail or {})
        noted = dict(detail.pop("attention", None) or {})
        noted.pop(stage, None)
        if reasons:
            noted[stage] = reasons
        doc.state.detail = detail | ({"attention": noted} if noted else {})
        self.rundir.emit("done", attention=reasons or None, **where)
        return result

    def _reused_view(
        self,
        doc: DocumentRun,
        view: Path,
        load: Callable[[str], Any],
        recheck: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """The document's `view`, loaded, when it stands: it is current
        (Derivations.current), `--refresh` does not name its role, and
        `recheck`, when given, finds no reason it fails. A reuse is an
        event, with the attention reasons noted when the view was made.
        A view `recheck` fails is moved aside, never deleted, with a
        `set-aside` event that gives the reason, and its record is
        forgotten. None when the view is to be made."""
        role, stage = doc.views[view]
        if role in self.refresh or not doc.derivations.current(view):
            return None
        try:
            loaded = load(view.read_text())
        except (OSError, ValueError):
            return None
        failure = recheck(loaded) if recheck is not None else None
        if failure is not None:
            kept = _supersede(view)
            doc.derivations.forget(view)
            self.rundir.emit(
                SET_ASIDE,
                subject=doc.sha1,
                kind=doc.kind,
                stage=stage,
                reason=failure[:500],
                kept=kept.name if kept is not None else None,
            )
            self.log(
                f"  {doc.source.name}: {view.name} fails its check now "
                f"({failure}); kept as "
                f"{kept.name if kept is not None else 'nothing'}, making it "
                f"again"
            )
            return None
        noted = (doc.state.detail or {}).get("attention") or {}
        self.rundir.emit(
            REUSED,
            subject=doc.sha1,
            kind=doc.kind,
            stage=stage,
            attention=noted.get(stage) or None,
        )
        return loaded

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
        """Make the document's batch: the GEDCOM adapter's, or extraction
        from its transcription view."""
        if doc.kind == "gedcom":
            return self._step_gedcom(doc)
        return self._step_extract(doc, self._transcription(doc))

    def _step_gedcom(self, doc: DocumentRun) -> ClaimBatch:
        from yizkr.pipeline.gedcom import adapt_gedcom, format_unsupported_tags

        result = self._run_stage(
            doc,
            "gedcom",
            lambda: adapt_gedcom(
                doc.source.read_text(), artifact_qid=doc.state.artifact_qid
            ),
        )
        detail = doc.state.detail or {}
        # the adapter is deterministic: say what it could not adapt when
        # that is news, not on every run
        if (
            detail.get("gedcom_warnings") != result.warnings
            or detail.get("gedcom_unsupported") != result.unsupported_tags
        ):
            for warning in result.warnings:
                self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
            if result.unsupported_tags:
                self.log(
                    f"  {doc.source.name}: unsupported GEDCOM tags: "
                    + format_unsupported_tags(result.unsupported_tags),
                    file=sys.stderr,
                )
        doc.state.detail = detail | {
            "gedcom_warnings": result.warnings,
            "gedcom_unsupported": result.unsupported_tags,
        }
        return result.batch

    def _transcription(self, doc: DocumentRun):
        """The document's transcription view, reused when it stands
        (`_reused_view`) and made otherwise, then recorded with its
        canonical content hash; once per run. A vision or text-file
        transcription, the routes whose transcribers check their output,
        is checked again when it is reused: the check may have become
        stricter since the view was made, and one it now fails is made
        again."""
        from yizkr.pipeline.transcribe import Transcription

        memo = (doc.sha1, doc.transcription.name)
        if memo in self._views:
            return self._views[memo]
        checked = route_transcription(doc.source, doc.correction) in (
            "vision",
            "text-file",
        )
        transcription = self._reused_view(
            doc,
            doc.transcription,
            Transcription.model_validate_json,
            recheck=(
                functools.partial(self._transcription_failure, doc)
                if checked
                else None
            ),
        )
        if transcription is None:
            transcription = self._step_transcribe(doc)
            doc.derivations.set_content(
                doc.transcription.name,
                content_hash(
                    transcription.model_dump(mode="json"),
                    volatile=TRANSCRIPTION_VOLATILE,
                ),
            )
            doc.derivations.record(doc.transcription)
        self._views[memo] = transcription
        return transcription

    def _transcription_failure(
        self, doc: DocumentRun, transcription
    ) -> str | None:
        """Why the transcription check fails `transcription` of the
        document now, or None when it passes."""
        from yizkr.pipeline.transcription_check import (
            TranscriptionCheckFailed,
            check_transcription,
            media_page_count,
        )

        check = check_transcription(
            transcription.text, pages=media_page_count(doc.source)
        )
        return None if check.passed else str(TranscriptionCheckFailed(check))

    def _step_transcribe(self, doc: DocumentRun):
        """Transcribe the document by its route, record the language it
        was found to be in, and write the transcription view."""
        method = route_transcription(doc.source, doc.correction)
        transcription, resolution = self._run_stage(
            doc, "transcription", lambda: self._transcribe(doc, method)
        )
        # detection is a per-document choice with real graph effect (the
        # excerpt language tag): record it, and surface it when it was
        # inferred rather than forced, so a wrong guess is reviewable.
        doc.state.detail = (doc.state.detail or {}) | {
            "language": {
                "code": resolution.code,
                "source": resolution.source,
                "confidence": resolution.confidence,
            }
        }
        changed = _write_view(
            doc.transcription, transcription.model_dump_json(indent=2)
        )
        if self.language == "auto" and changed:
            self.log(f"  {doc.source.name}: language {resolution.describe()}")
        return transcription

    def _transcribe(self, doc: DocumentRun, method: str):
        """(transcription, language resolution) by the routed method."""
        transcribe = {
            "vision": self._transcribe_vision,
            "text-file": self._transcribe_text_file,
            "slide-text": self._transcribe_slides,
            "correction": self._transcribe_correction,
        }[method]
        return transcribe(doc)

    def _transcribe_vision(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import LlmVisionTranscriber

        # the transcriber checks what the engine or the cache answers, and
        # rejects an answer that fails the check (ADR-0032)
        transcriber = LlmVisionTranscriber(
            self._llm("transcribe", doc),
            language=self.language,
            fallback_language=self.fallback_language,
        )
        transcription = transcriber.transcribe(
            doc.source, artifact_qid=doc.state.artifact_qid
        )
        return transcription, transcriber.last_resolution

    def _transcribe_text_file(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(
            doc.source,
            language=self.language,
            fallback_language=self.fallback_language,
        )
        transcription = transcriber.transcribe(
            doc.source, artifact_qid=doc.state.artifact_qid
        )
        return transcription, transcriber.last_resolution

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
            artifact_qid=doc.state.artifact_qid,
            text=text,
            language=resolution.code,
            engine="pptx-adapter",
        )
        return transcription, resolution

    def _transcribe_correction(self, doc: DocumentRun):
        from yizkr.pipeline.language import resolve_language
        from yizkr.pipeline.transcribe import Transcription

        text = doc.correction.read_text()
        resolution = resolve_language(
            text, self.language, fallback=self.fallback_language
        )
        transcription = Transcription(
            artifact_qid=doc.state.artifact_qid,
            text=text,
            language=resolution.code,
            engine="correction",
        )
        return transcription, resolution

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline.chunking import ChunkedClaimExtractor

        # always entity-first (ADR-0007), even for documents that fit a
        # single call: reconciliation NEEDS the entity manifest, and a
        # small document without one silently mints duplicates instead
        # of being adjudicated against the graph — every entity must
        # reach the adjudicator, however short the document. Chunking a
        # small document costs a few extra calls and is never incorrect
        # (ADR-0006 §7).
        extractors: list[ChunkedClaimExtractor] = []

        def extract() -> ClaimBatch:
            # the engine starts inside the stage, so an engine that cannot
            # start fails the stage, with its event
            extractor = ChunkedClaimExtractor(
                self._llm("extract", doc),
                state_dir=doc.extraction,
                concurrency=self.chunk_concurrency,
                log=self.log,
            )
            extractors.append(extractor)
            batch = extractor.extract(
                transcription, artifact_qid=doc.state.artifact_qid
            )
            failed = extractor.failed_chunks
            if failed:
                first = extractor.failure_reasons.get(failed[0], "")
                raise RuntimeError(
                    f"{len(failed)} chunks failed after retries "
                    f"({', '.join(failed)}; {failed[0]}: {first}); re-run "
                    f"to retry them"
                )
            return batch

        batch = self._run_stage(
            doc,
            "extraction",
            extract,
            attention=lambda _batch: extractors[0].attention,
        )
        doc.gaps_found = extractors[0].gaps
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

    def _critique(self, doc: DocumentRun, batch: ClaimBatch) -> None:
        """The batch's critique view (ADR-0013), reused when it stands
        (`_reused_view`) and made otherwise, then recorded."""
        if self._reused_view(doc, doc.critique, _findings) is not None:
            return
        findings = self._step_critique(doc, batch)
        changed = _write_view(
            doc.critique,
            json.dumps(
                [finding.model_dump(mode="json") for finding in findings],
                indent=2,
            ),
        )
        doc.derivations.record(doc.critique)
        if findings and changed:
            self.log(
                f"  critique: {len(findings)} concern(s) flagged for review"
            )
            for finding in findings[:6]:
                self.log(
                    f"    - {finding.target}: {finding.issue}", file=sys.stderr
                )

    def _step_critique(self, doc: DocumentRun, batch: ClaimBatch):
        """The extraction critic's findings on the batch."""
        from yizkr.pipeline.critique import run_critique

        return self._run_stage(
            doc,
            "critique",
            lambda: run_critique(self._llm("critique", doc), batch),
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

    def _update_batch_view(self, doc: DocumentRun, batch: ClaimBatch) -> bool:
        """Write `batch.json` for the current batch; True when its content
        differs from the batch the document's derived files were made from.

        Content is compared by canonical hash (run timestamps left out),
        so an identical recompute invalidates nothing: a deleted view
        rebuilt with the same content, as the cache or the GEDCOM adapter
        gives it, included."""
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

    def _derive(self, doc: DocumentRun) -> ClaimBatch:
        """Bring the views before the gates up to date, each after the
        view it is made from: the transcription, the batch and the
        critique (a GEDCOM file has only its batch). A view that stands is
        reused (`_reused_view`); one that does not is made again, and a
        batch whose content changed invalidates everything derived from
        the old one."""
        extracted = doc.kind != "gedcom"
        if extracted:
            self._transcription(doc)
        batch = self._reused_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        )
        changed = False
        if batch is None:
            batch = self._step_batch(doc)
            changed = self._update_batch_view(doc, batch)
            doc.derivations.record(doc.batch)
        if changed:
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
        elif doc.state.status in (FAILED, PAUSED, REGISTERED):
            # the batch stands, so an earlier failure or pause is over; the
            # gates below re-derive the real status
            doc.state.status = BATCHED
            doc.save()
        # the extraction critic reviews each batch (ADR-0013)
        if extracted:
            self._critique(doc, batch)
        return batch

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

    def _set_aside(self, doc: DocumentRun, why: str) -> None:
        """Move the document's directory aside whole and start it afresh,
        saying so in the log and the run directory."""
        kept = _set_aside(doc.dir)
        doc.state = DocumentState(file=str(doc.source), title=doc.source.stem)
        self.rundir.emit(
            SET_ASIDE,
            subject=doc.sha1,
            kind=doc.kind,
            reason=why,
            kept=kept.name,
        )
        self.log(
            f"  {doc.source.name}: {why}; the old directory is kept as "
            f"{kept.name}; re-ingesting"
        )

    def _advance(
        self, doc: DocumentRun, *, stop_after_extract: bool = False
    ) -> str:
        doc.state.error = None
        content_sha1 = doc.sha1

        # source bytes changed since this document was last processed: the
        # old artifact, claims, and reviews describe DIFFERENT content
        # (ADR-0010 — different bytes are a different artifact), so the
        # document starts over in a fresh directory rather than trusting
        # stale derived state. A directory without derivations.json holds
        # files nothing says were derived from what, and one whose files
        # have no state.json beside them was made for a registration
        # nothing records; either starts over the same way. Each is moved
        # aside whole, reviews included, and nothing in it is deleted
        # (_set_aside).
        if doc.state.content_sha1 and doc.state.content_sha1 != content_sha1:
            self._set_aside(doc, "content changed")
        elif doc.unrecorded:
            self._set_aside(
                doc,
                "no derivations.json records what the files in its "
                "directory were derived from"
                if not doc.derivations.path.exists()
                else "no state.json records the artifact the files in its "
                "directory were made for",
            )
        # a directory is recorded from its first run, before any other file
        # is written to it
        doc.dir.mkdir(parents=True, exist_ok=True)
        doc.derivations.begin()
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

        # the views before the gates are brought up to date once in each
        # run
        batch = self._derived.get(doc.sha1)
        if batch is None:
            batch = self._derive(doc)
            self._derived[doc.sha1] = batch
        elif doc.state.status in (FAILED, PAUSED, REGISTERED):
            doc.state.status = BATCHED
            doc.save()

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

    def _prior_written_hashes(
        self, docs: Sequence[DocumentRun] = ()
    ) -> dict[str, str]:
        """content SHA-1 -> filename of a document already ingested to
        completion in a prior run (ADR-0010).

        Lets a renamed identical file be recognised and skipped BEFORE any
        expensive step, not merely deduped at the graph by the writer.
        Reads persisted per-document state (runtime state in var/, not a
        second store of assertions), so it degrades gracefully: if that
        state is cleared, the file is simply re-extracted and the writer
        still dedups. Only WRITTEN documents seed the map — an unfinished
        prior run must not block a fresh attempt from completing.

        A directory set aside (a dot-prefixed name, _set_aside) seeds
        nothing: the bytes it recorded are no longer its document's. Nor
        does a directory whose document is in this run (`docs`) with other
        bytes, which is set aside when that document is advanced.
        """
        seen: dict[str, str] = {}
        if not self.state_dir.exists():
            return seen
        current: dict[str, set[str]] = {}
        for doc in docs:
            current.setdefault(doc.dir.name, set()).add(doc.sha1)
        for sub in sorted(self.state_dir.iterdir()):
            if sub.name.startswith("."):  # set aside
                continue
            state_file = sub / "state.json"
            if not state_file.exists():
                continue
            try:
                state = DocumentState.load(state_file)
            except (TypeError, ValueError):  # malformed/foreign state: skip
                continue
            if (
                sub.name in current
                and state.content_sha1 not in current[sub.name]
            ):  # its source changed: it is set aside in this run
                continue
            if state.status == WRITTEN and state.content_sha1:
                seen.setdefault(state.content_sha1, Path(state.file).name)
        return seen

    def _roles(self) -> dict[str, dict[str, Any]]:
        """Each role as the run configures it, for the run manifest; the
        role's engine client adds what it applies when it starts."""
        roles = {}
        for role in MODEL_ROLES:
            effort, _model = self._engine_settings(role)
            roles[role] = {
                "model": self._configured_model(role)[0],
                "effort": effort,
                "epoch": self.epochs.get(role, 0),
                "refresh": role in self.refresh,
                "client": None,
            }
        return roles

    def _start_run(self, docs: list[DocumentRun]) -> None:
        """Record the run's plan and what shapes it in the run manifest
        (docs/RUN_MANIFEST.md): settings and hashes, never a prompt or
        any document content."""
        self.rundir.start(
            fixed_settings=self.fixed_settings,
            mode="ingest run",
            project_stage=DEVELOPMENT_STAGE,
            engine={"name": self.llm, "cli_version": None},
            roles=self._roles(),
            cache=(
                os.path.abspath(self.cache_dir)
                if self.cache_dir is not None
                else None
            ),
            corpus={
                "folder": os.path.abspath(self.folder),
                "exclusions": exclusions_record(self.exclusions),
            },
            code=code_version(),
            config={
                "engine": self.llm,
                "language": self.language,
                "fallback_language": self.fallback_language,
                "extract_effort": self.extract_effort,
                "review_effort": self.review_effort,
                "extract_model": self.extract_model,
                "refresh": list(self.refresh),
                "epochs": dict(sorted(self.epochs.items())),
                "artifact_type": self.artifact_type,
                "serial": self.serial,
                "chunk_concurrency": self.chunk_concurrency,
                "extract_concurrency": self.extract_concurrency,
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
        from yizkr.pipeline.llm import PassPaused
        from yizkr.pipeline.transcribe import preflight_vision_transcription

        # fail closed: extract-all work never starts without a
        # never-ingest list, so a missing list cannot pass as an empty one
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        # only full model ids enter the run manifest and the cache's keys
        self._refuse_model_aliases()
        docs = self.documents()
        if not docs:
            self.log(f"no supported documents in {self.folder}")
            return 0
        # once for the whole run, before any document: a PDF needs the
        # renderer the vision engine reads it through and the page count
        # the transcription check takes, whether its transcription comes
        # from the engine or from the cache, or is a view the run reuses
        # and checks again
        preflight_vision_transcription(
            doc.source for doc in docs if doc.kind == "media"
        )
        # seed with content already fully ingested in prior runs, so a
        # renamed copy skips the pipeline (cross-run dedup, ADR-0010)
        self._seen_hashes = self._prior_written_hashes(docs)
        self._derived = {}
        self._views = {}
        # each run is an invocation of its own in the run directory
        self.rundir = RunDir(self.state_dir)
        self._start_run(docs)

        # the control request that stopped the run before a document, if
        # one did, and the run-scope one that held a stage, if one did
        stopped = None
        self._run_held = None
        try:
            if self.serial:
                docs = self._serial_sequence(docs)
            for doc in docs:
                control = self.rundir.control()
                stopped = control.stopped()
                if stopped:
                    self.log(f"run stopped ({stopped})")
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
                        f"  (serial) paused until {doc.source.name} is written"
                    )
                    break
        except KeyboardInterrupt:
            self.rundir.emit(RUN_INTERRUPTED)
            self.rundir.end("interrupted")
            raise
        except BaseException as exc:
            if isinstance(exc, PassPaused):
                outcome = "paused"
            elif isinstance(exc, RunSettingsConflict):
                outcome = "refused"
            else:
                outcome = "failed"
            # only the exception's name: its message may quote a document
            self.rundir.end(outcome, reason=type(exc).__name__)
            raise
        finally:
            self._close_engines()

        statuses: dict[str, int] = {}
        self.log("")
        for doc in docs:
            self.log(f"{doc.state.status:32} {doc.source.name}")
            statuses[doc.state.status] = statuses.get(doc.state.status, 0) + 1
        # a run-scope request stopped the run even when it held a stage of
        # the last document, with no document left to dispatch after it
        stopped = stopped or self._run_held
        self.rundir.end("stopped" if stopped else "finished", reason=stopped)
        self.rundir.emit(RUN_END, statuses=statuses)
        if any(doc.state.status == FAILED for doc in docs):
            return 1
        if any(
            doc.state.status.startswith("awaiting")
            or doc.state.status == PAUSED
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


def _text(engine: Any, name: str) -> str | None:
    """An engine client's attribute as the run manifest records it: its
    text, or None when the client has none (or an empty one)."""
    value = getattr(engine, name, None)
    return value if isinstance(value, str) and value else None


def _set_aside(directory: Path) -> Path:
    """Move a document directory aside whole, never deleting anything in
    it: `letter` becomes `.letter.superseded-<n>` beside it (first free
    n). A document slug never starts with a dot, so no document directory
    can take that name, and the run's scans of its state skip it."""
    n = 1
    while (
        target := directory.with_name(f".{directory.name}.superseded-{n}")
    ).exists():
        n += 1
    directory.rename(target)
    return target


def _supersede(path: Path) -> Path | None:
    """Move a superseded file aside, never deleting it: `review.reviewed.csv`
    becomes `review.reviewed.superseded-<n>.csv` (first free n), so a
    review about superseded rows consents to nothing. Returns where the
    file went, or None when there was none."""
    if not path.exists():
        return None
    n = 1
    while (
        target := path.with_name(f"{path.stem}.superseded-{n}{path.suffix}")
    ).exists():
        n += 1
    path.rename(target)
    return target


def _findings(text: str) -> list:
    """A critique view's findings; ValueError when it holds no list."""
    findings = json.loads(text)
    if not isinstance(findings, list):
        raise ValueError("a critique view holds a list of findings")
    return findings


def _csv_has_rows(path: Path) -> bool:
    with open(path, newline="") as handle:
        next(handle, None)  # header
        return any(line.strip() for line in handle)
