"""Folder/batch orchestration: `yizkr ingest run`.

One command walks every document in a folder through the pipeline —
transcribe -> extract (or the deterministic GEDCOM adapter) -> reconcile
-> resolve -> export review -> apply review -> register and write —
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
again in place. Each stage records its declared generation settings and
semantic code/resource dependencies; execution and estimation use the same
side-effect-free freshness plan. Reused readings still pass the
transcription check: a transcription made by a route whose transcriber
checks its output (CHECKED_ROUTES) is checked again whenever it is
reused, and one that now fails is moved aside and made again in the
run. The model calls a view is made with go through the LLM cache when
the run has one (ADR-0032): an accelerator, which the pipeline runs the
same without. The client factory (yizkr.pipeline.llm) wraps the run's
engine clients in it; the orchestrator only names the
cache folder, the refreshed roles and the epochs, and records the cache's
events in the run directory. The transcription is routed per document
(vision, paged, text file); a curator's
`<doc>/transcription.corrected.txt` routes it to the correction instead.
A paged container, a PDF or a deck, takes the paged route: it is read
page by page by its format's reader (yizkr.pipeline.containers), each
page from the text the file holds where it holds a usable one (a PDF's
text layer, yizkr.pipeline.textlayer; a deck's slide XML,
yizkr.pipeline.presentation), without a model call, any other page by
the vision engine, and each independent image source it places
(yizkr.pipeline.container_sources), on any page, read by the vision
engine on its own, as a witness of its own (ADR-0008). The images'
bytes and their transcriptions are kept in the document's directory,
recorded in its container inventory (`container-inventory.json`),
beside the container's own transcription: original parts under
`container-parts/`, prepared images under `representations/`, and each
reading at `derived-media/<sha1>.transcription.json`. A PDF none of
whose pages has a usable text layer, and none of whose images is read on
its own, is read whole by the vision engine. The PDF renderer the
vision engine and the transcription check need is checked once, before
any document.

Each image a container's transcription reads on its own, with a reading
that passes the check, is then a document of its own, an excerpt
(ExcerptRun): its identity is the SHA-1 of its bytes, its directory is in
the state directory's EXCERPTS_DIR, its transcription is the reading the
container kept, and its claims cite the image. An image several
containers place is one excerpt; one whose bytes a document of the
folder has, or that the never-ingest list names, is none. The run takes
each excerpt after the folder's documents, as its container's
transcription finds it.

One step uses the wiki: the write, which registers the document first
(yizkr.pipeline.registrar): its artifact item, a deck's slides as page
fragments, and the image sources of a deck or a PDF that its container
inventory binds (each part of the container on each slide or page that
paints it), each found or made by the SHA-1 of its bytes, just before
the claims that cite them are written; an excerpt's write registers each
container that places it, with its images. It finds or mints the item
of every entity of the batch through the identity map of the run's
documents (yizkr.pipeline.writer, step 2), which it keeps in the run
directory's corpus folder as a scratch run's clustering stage does
(`_identity_map`). Everything else,
the resolve step among it, which matches nothing by its label, reads
nothing from the wiki and writes nothing to it (ADR-0025 §2): the stages
before the gates; a deck's and a PDF's image sources, which are
recorded in its directory (`container-inventory.json`) for the write to
register; and the reconcile step, which has no roster to match a
document's entities against, so it proposes nothing and the first gate
has nothing to review.

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
no `state.json` has lost the state that names the bytes its views were
made for, which is saved before any view. It
is set aside whole, as `.<name>.superseded-<n>` in the state directory,
and the document starts afresh in a new one, as a document whose source
bytes changed does (ADR-0010: different bytes are a different artifact).
Nothing in the old directory is deleted, and it no longer counts for
content dedup. Modification times are read nowhere.

Each source file has a directory of its own, named after its stem, or
after its whole file name where another source in the folder shares the
stem; a source keeps the directory that records it (directory_names).
A document whose source or state.json cannot be read fails alone, and
the run goes on with the others; a state.json it cannot read is left as
it is. state.json is replaced in one step, as the views are, and only
when it changes; a document whose changed state cannot be saved fails
alone too.

A reviewed file's PRESENCE is the gate: the orchestrator never
interprets a half-filled CSV as consent. Documents whose reconcile
produced no proposals skip the first gate (there is nothing to review).

The state directory is also the run directory (yizkr.pipeline.rundir):
`run.json`, `events.jsonl` with an event for every stage start,
completion, failure and reused view, every answer the cache gives or
cannot give, every document that fails, and the end of every
invocation, however it ended, and `control.jsonl`, whose pause and abort
requests stop dispatch. It is runtime state — keep it under var/ or
outside the repo. Re-running a completed document changes nothing (the
writer is idempotent).

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

import contextlib
import functools
import hashlib
import json
import os
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.config import Config
from yizkr.core.corpus import (
    DECK_SUFFIXES,
    MISSING_LIST,
    ExclusionConfigError,
    ExclusionList,
    follow_decks,
    is_excluded,
)
from yizkr.core.wiki import WikiClient
from yizkr.pipeline import stage_planning
from yizkr.pipeline.cancellation import (
    Cancellation,
    RunCancelled,
    cancellation_scope,
    check_cancelled,
    current_cancellation,
)
from yizkr.pipeline.rundir import (
    ALERTS,
    CONTROL,
    DEVELOPMENT_STAGE,
    DOCUMENT_FAILED,
    EVENTS,
    MANIFEST,
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
from yizkr.pipeline.stage_planning import DOCUMENT_STAGES, MODEL_ROLES, VIEWS
from yizkr.pipeline.stages import (
    atomic_write_text,
    content_hash,
    sha1_file,
    sha256_file,
    sha256_text,
)
from yizkr.pipeline.workers import WorkerShutdownTimeout

GEDCOM_SUFFIXES = {".ged"}
TEXT_SUFFIXES = {".txt", ".md"}
MEDIA_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
PRESENTATION_SUFFIXES = DECK_SUFFIXES
SUPPORTED_SUFFIXES = (
    GEDCOM_SUFFIXES | TEXT_SUFFIXES | MEDIA_SUFFIXES | PRESENTATION_SUFFIXES
)

# document statuses, in pipeline order
PENDING = "pending"
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

# the kind of a PDF, a paged container of its own (ADR-0008)
PDF = "pdf"
# the kind of an image a paged container places on one of its pages and
# reads on its own (on any page of a PDF, on a deck's slide), taken as a
# document of its own (ExcerptRun), and the directory of the state
# directory that holds such documents' directories, each named for its
# image's SHA-1
EXCERPT = "excerpt"
EXCERPTS_DIR = "excerpts"
# the transcription route of a paged container, a PDF or a deck, read by
# its format's reader (yizkr.pipeline.containers)
PAGED = "paged"
# the transcription routes whose transcribers check their output: a view
# made by one of them is checked again when it is reused, by the driver
# and the estimate alike; route names, not the engine prefixes
# yizkr.pipeline.anomalies.CHECKED_ROUTES judges
CHECKED_ROUTES = frozenset(("vision", PAGED, "text-file", EXCERPT))
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
# the fields of a transcription that say what made it (the route and the
# engine build, and each page's provenance), and the transcription
# check's flag, which says what the check made of it: left out of its
# content hash. A page's place in the text, the images it names and its
# record's fields (its printed page number, status, languages and notes)
# are content, as its text and its document class are (ADR-0037)
TRANSCRIPTION_VOLATILE = (
    "engine",
    "pages.provenance",
    "flag",
    "input_representation",
    "reading_input",
)
# the event that says a stage's view was current and stands
REUSED = "reused"


def document_kind(source: Path) -> str:
    """The kind of source a document is: a run's slices are kinds. A
    deck (`presentation`) and a PDF (`pdf`) are the paged containers,
    each of its own kind, since each has a reader of its own; `media` is
    any other file, a photograph or a scan read whole."""
    suffix = source.suffix.lower()
    if suffix in GEDCOM_SUFFIXES:
        return "gedcom"
    if suffix in PRESENTATION_SUFFIXES:
        return "presentation"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix == ".pdf":
        return PDF
    return "media"


def route_transcription(source: Path, correction: Path | None = None) -> str:
    """The transcription method for a document, chosen deterministically.

    A curator's correction wins; otherwise the source decides: a text
    file is its own transcription; a paged container, a deck or a PDF,
    takes the paged route (PAGED), read page by page by its format's
    reader (yizkr.pipeline.containers), each page by its own method,
    chosen as deterministically, and each independent image source it
    places, on any page, read on its own; and anything else, an image,
    is read whole by the vision engine. The paged route reads a PDF none
    of whose pages has a usable text layer, and none of whose images is
    read on its own, whole by the vision engine too."""
    if correction is not None and correction.is_file():
        return "correction"
    suffix = source.suffix.lower()
    if suffix in PRESENTATION_SUFFIXES or suffix == ".pdf":
        return PAGED
    if suffix in TEXT_SUFFIXES:
        return "text-file"
    return "vision"


@dataclass
class DocumentState:
    file: str
    title: str
    status: str = PENDING
    # the artifact item the document's latest write registered it as
    artifact_qid: str | None = None
    error: str | None = None
    detail: dict | None = None
    # SHA-1 of the source bytes last processed (ADR-0010): the content
    # identity, so a renamed copy is recognised across runs and a changed
    # source forces a re-ingest.
    content_sha1: str | None = None

    @classmethod
    def load(cls, path: Path) -> DocumentState:
        """The state saved at `path`. OSError when it cannot be read;
        ValueError or TypeError when it holds no document's state: a file
        cut short, a foreign one, or fields of other types."""
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{path.name} holds no object")
        state = cls(**data)
        texts = (state.file, state.title, state.status)
        optional = (state.artifact_qid, state.error, state.content_sha1)
        if not (
            all(isinstance(value, str) for value in texts)
            and all(isinstance(value, str | None) for value in optional)
            and isinstance(state.detail, dict | None)
        ):
            raise ValueError(f"{path.name} holds a field of another type")
        return state

    def save(self, path: Path) -> None:
        """Replace the state saved at `path` in one step, as the views are
        (_write_view), and leave an identical one untouched. A save cut
        short, by a kill say, leaves the state it was replacing whole; and
        a state that has not changed is not written, so a document whose
        views all stand is advanced in a directory it cannot write to."""
        _write_view(path, json.dumps(self.__dict__, indent=2))


class Derivations:
    """What each derived file of a document was made from, by content hash.

    The dependency record behind safe re-runs, kept in `derivations.json`:
    `current` compares file inputs; the shared view planner additionally
    compares migrated stages' `processing` metadata. A reviewed CSV
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
        except (OSError, ValueError, RecursionError):
            data = {}
        for section in ("derived", "reviews", "content", "processing"):
            data.setdefault(section, {})
        if not isinstance(data["processing"], dict):
            data["processing"] = {}
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
            if path.name != "interpretation-context.json" or path.exists()
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

    def record(
        self,
        output: Path,
        *,
        inputs: Mapping[str, str | None] | None = None,
        processing: Mapping[str, Any] | None = None,
    ) -> None:
        data = self._load()
        data["derived"][output.name] = (
            self._inputs(output) if inputs is None else dict(inputs)
        )
        if processing is not None:
            data["processing"][output.name] = dict(processing)
        self._save(data)

    def forget(self, *outputs: Path) -> None:
        """Forget what `outputs` were made from, and their content."""
        data = self._load()
        for output in outputs:
            for section in ("derived", "reviews", "content", "processing"):
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


@dataclass(frozen=True)
class ViewInspection:
    """A read-only freshness decision and an admissible saved value."""

    plan: stage_planning.FreshnessPlan
    value: Any = None
    invalid_reason: str | None = None
    blocked_reason: str | None = None
    processing: Mapping[str, Any] | None = None
    inputs: Mapping[str, str | None] | None = None

    def require_value(self) -> Any:
        """A saved value or a recompute; an uninspectable plan must stop."""
        if self.plan.action == "blocked":
            raise RuntimeError(self.blocked_reason or "stage planning blocked")
        return self.value


class ProcessingUnavailable(RuntimeError):
    """A stage cannot establish its required generation dependencies."""


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "document"


# the files a run directory holds beside its documents' directories
# (yizkr.pipeline.rundir), the directory the run monitor writes its live
# status page into (yizkr.pipeline.monitor.STATUS_DIR), and the one a
# scratch run's corpus stages keep their outputs in
# (yizkr.pipeline.scratch.CORPUS_DIR), and the one that holds the
# excerpts' directories (EXCERPTS_DIR): no document directory may take
# their names
_RUN_FILES = frozenset(
    {MANIFEST, EVENTS, ALERTS, CONTROL, "status", "corpus", EXCERPTS_DIR}
)
# how much of a whole file name's slug a hashed directory name keeps, so
# that with its hash it stays well inside a filesystem's 255-byte limit
_HASHED_PREFIX = 200


def _directory_candidates(source: Path) -> tuple[str, str, str]:
    """The names a source's directory may have, in the order they are
    tried: its stem's slug (`letter`), its whole file name's (`letter.pdf`),
    and the start of that with a hash of the file name."""
    whole = slugify(source.name)
    hashed = f"{whole[:_HASHED_PREFIX]}-{sha256_text(source.name)[:8]}"
    return slugify(source.stem), whole, hashed


def _records(directory: Path, source: Path) -> bool:
    """Does `directory` hold the state of `source`: a state.json, readable,
    that names the source's file?"""
    try:
        state = DocumentState.load(directory / "state.json")
    except (OSError, TypeError, ValueError, RecursionError):
        return False
    return Path(state.file).name == source.name


def _damaged(directory: Path) -> bool:
    """Does `directory` hold a state.json that cannot be read: one cut
    short, a foreign one, or one without permission?"""
    try:
        DocumentState.load(directory / "state.json")
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, TypeError, ValueError, RecursionError):
        return True
    return False


def _named(directory: Path, sources: Sequence[Path]) -> list[Path]:
    """The sources whose file a damaged state.json in `directory` still
    names: a state is saved with its `file` first, a path that ends with
    the file's name, so a state cut short almost always keeps it. None
    when the bytes name none of them or cannot be read."""
    try:
        text = (directory / "state.json").read_bytes().decode(errors="replace")
    except OSError:
        return []
    return [
        source
        for source in sources
        if re.search(
            r'["/\\]' + re.escape(json.dumps(source.name)[1:-1]) + '"', text
        )
    ]


def directory_names(
    sources: Sequence[Path], state_dir: Path
) -> dict[Path, str]:
    """The name of each source's directory in `state_dir`, one directory
    per source file.

    A source keeps the directory that records it (its state.json names the
    source's file), under any of its candidate names. A source that keeps
    none takes the first of its candidate directories whose state.json
    cannot be read, unless another source took it first; where several
    sources that keep none could take it, the one whose file the damaged
    bytes still name takes it (_named), and failing that the first by
    name. The run fails that document with the reason (DocumentRun), so a
    damaged state.json is reported, against the document it belongs to,
    even where sources share the stem it is named after. Any other
    source, in name order, takes its stem's slug, unless another source in
    the folder has a stem with the same slug, ignoring case, or the name is
    taken or is a run file's; then its whole file name's slug, and failing
    that the start of that slug with a hash of the file name. So sources
    that differ only in extension, or in case, never share a directory; a
    source whose stem no other source shares keeps its stem's name; and a
    source that joins or leaves the folder moves no other source's
    recorded directory. Naming sets nothing aside: a directory whose source
    has left stays until a source is given its name, and that source, when
    its bytes differ, sets it aside as a changed source's when it is
    advanced."""
    names: dict[Path, str] = {}
    for source in sources:
        for name in _directory_candidates(source):
            if _records(state_dir / name, source):
                names[source] = name
                break
    for source in sources:
        if source in names:
            continue
        for name in _directory_candidates(source):
            claimed = {taken.casefold() for taken in names.values()}
            if name.casefold() in claimed or not _damaged(state_dir / name):
                continue
            # the sources that keep no directory and could take this one,
            # this source the first of them by name
            rivals = [
                other
                for other in sources
                if other not in names and name in _directory_candidates(other)
            ]
            named = _named(state_dir / name, rivals) if len(rivals) > 1 else []
            owner = named[0] if named else source
            names[owner] = name
            if owner == source:
                break
    stems = Counter(slugify(source.stem).casefold() for source in sources)
    taken = {name.casefold() for name in names.values()} | _RUN_FILES
    for source in sources:
        if source in names:
            continue
        stem, whole, hashed = _directory_candidates(source)
        if stems[stem.casefold()] == 1 and stem.casefold() not in taken:
            name = stem
        elif whole.casefold() not in taken:
            name = whole
        else:
            name, n = hashed, 2
            while name.casefold() in taken:
                name, n = f"{hashed}-{n}", n + 1
        names[source] = name
        taken.add(name.casefold())
    return names


def _why(exc: BaseException) -> str:
    """An exception as a failure's reason: its name and its message."""
    return f"{type(exc).__name__}: {exc}"


class DocumentRun:
    """Paths and state for one document within a folder run.

    Its directory in `state_dir` is `dirname` (directory_names), by
    default its stem's slug. A document whose state.json cannot be read is
    `unreadable`, and so is one whose source cannot be read once
    `read_source` has tried: the run fails it alone (Orchestrator.run)."""

    def __init__(
        self, source: Path, state_dir: Path, *, dirname: str | None = None
    ) -> None:
        self.source = source
        self.dir = state_dir / (dirname or slugify(source.stem))
        self.state_path = self.dir / "state.json"
        self.transcription = self.dir / VIEWS["transcription"]
        # a curator's corrected transcription (plain text); when present
        # it is the document's transcription
        self.correction = self.dir / "transcription.corrected.txt"
        # a curator's pointer to the document's statement about how its
        # text was prepared, by its words (yizkr.pipeline.preparation)
        self.pointer = self.dir / "preparation.pointer.txt"
        self.batch = self.dir / stage_planning.batch_stage(self.kind).artifact
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
        self.critique = self.dir / VIEWS["critique"]
        self.interpretation_context = self.dir / "interpretation-context.json"
        # each view the stages before the gates make -> (the refresh role
        # that makes it, the stage that makes it)
        descriptors = stage_planning.stages_for(self.kind)
        self.views = {
            self.dir / stage.artifact: (stage.role, stage.name)
            for stage in descriptors
        }
        self.derivations = Derivations(
            self.dir / "derivations.json",
            made_from={
                **{
                    self.dir / stage.artifact: tuple(
                        self.dir / name for name in stage.input_artifacts
                    )
                    for stage in descriptors
                },
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
        # why the run cannot take the document on, when it cannot
        self.unreadable: str | None = None
        # a state.json that cannot be read is kept as it is, never saved
        # over: it may still hold state a person can recover
        self._state_unread = False
        self.state = DocumentState(file=str(source), title=source.stem)
        try:
            if self.state_path.exists():
                self.state = DocumentState.load(self.state_path)
        except (OSError, TypeError, ValueError, RecursionError) as exc:
            self.unreadable = (
                f"its state.json cannot be read ({_why(exc)}); repair it, or "
                f"remove it and the document starts afresh"
            )
            self._state_unread = True

    @property
    def sha1(self) -> str:
        """The media SHA-1 of the source as it is now."""
        if self._sha1 is None:
            self._sha1 = sha1_file(self.source)
        return self._sha1

    @property
    def known_sha1(self) -> str | None:
        """The media SHA-1 once the source has been read, else None."""
        return self._sha1

    def read_source(self) -> str | None:
        """Read the source for its media SHA-1 now, before the run starts
        any document. None, and the document `unreadable`, when the source
        cannot be read."""
        try:
            return self.sha1
        except OSError as exc:
            self.unreadable = self.unreadable or (
                f"its source cannot be read ({_why(exc)})"
            )
            return None

    @property
    def kind(self) -> str:
        return document_kind(self.source)

    def pointed(self) -> str | None:
        """The words of the document's statement about its preparation
        that a curator pointed to (`pointer`); None where none did."""
        if not self.pointer.is_file():
            return None
        return self.pointer.read_text(encoding="utf-8")

    @property
    def unrecorded(self) -> bool:
        """Does the directory hold files nothing records the making of:
        anything without the derivations.json that says what its files
        were derived from, or files beside it without the state.json that
        names the artifact they were made for, by the SHA-1 of its bytes?

        Every directory the orchestrator makes has its record from its
        first run, and its state before any view is made; one without
        them was made before the records, or has lost them."""
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
        state in it. A state.json that could not be read is left as it
        is, and so is one that names another source's file, as a directory
        whose source has left does: the state the document is advanced with
        names its own file (Orchestrator._advance)."""
        if (
            not self._state_unread
            and Path(self.state.file).name == self.source.name
            and self.derivations.path.exists()
        ):
            self.state.save(self.state_path)


class ExcerptRun(DocumentRun):
    """An image a paged container places on one of its pages and reads on
    its own (on any page of a PDF, on a deck's slide), taken as a document
    of its own: a witness apart from the container, whose claims cite the
    image alone (ADR-0008).

    Its identity is the SHA-1 of its bytes, and so is its directory's
    name, in the state directory's EXCERPTS_DIR: an image that several
    containers place is one document, whichever of them places it first.
    `containers` are the PDFs and decks that place it, each keeping the
    image's bytes and its reading in its own directory (presentation.
    derived_transcription); the first one's copy of the bytes is its
    source. Its transcription is the reading the container's
    transcription made of it (media_stages.read_excerpt), so no
    model is asked again. Its title names one of its places, the first in
    sorted order of the names the container's source projection gives it
    (container_sources.BoundSource.names): "<the PDF's title> — page 6,
    image 1", or "<the deck's title> — image2.png"."""

    def __init__(
        self, sha1: str, container: DocumentRun, medium: Any, state_dir: Path
    ) -> None:
        super().__init__(
            container.dir / medium.file,
            state_dir,
            dirname=f"{EXCERPTS_DIR}/{sha1}",
        )
        self.containers = [container]
        if not self.state_path.exists():
            self.state.title = f"{container.state.title} — {medium.names[0]}"

    @property
    def kind(self) -> str:
        return EXCERPT

    def placed_by(self, container: DocumentRun) -> None:
        """Add a container that places the image too."""
        if all(known.dir != container.dir for known in self.containers):
            self.containers.append(container)


def _write_view(path: Path, text: str) -> bool:
    """Refresh a plain-file view, leaving an identical one untouched;
    returns whether the file changed. A file that cannot be read, or is
    not text, is replaced."""
    try:
        if path.read_text() == text:
            return False
    except (OSError, ValueError):
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
        cancellation: Cancellation | None = None,
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
        # extraction) run low; the judgment step, the critique, runs high
        # — where the thinking is cheap and the decision matters.
        self.extract_effort = extract_effort
        self.review_effort = review_effort
        # per-phase model: claim extraction is a high-volume, structured
        # task where source discipline (extract only what the source
        # asserts) matters more than raw capability, so it can run a
        # distinct model from the run's main one — which stays on the
        # critique. None inherits the main model. See docs/adr/0016.
        self.extract_model = extract_model
        # serial mode (ADR-0019): process artifacts one at a time, in a
        # greedy-overlap sequence, each fully written before the next
        # reconciles; every write finds its entities' items through the
        # run's identity map (_identity_map), never by label.
        self.serial = serial
        # how many documents to extract (transcribe + chunked extract) at
        # once during serial mode's up-front extract-all phase. Multiplies
        # with chunk_concurrency, so keep the product sane for the engine's
        # rate/usage limits. The phase asks nothing of the wiki: the write,
        # which registers each document, comes after it, one document at a
        # time.
        self.extract_concurrency = max(1, extract_concurrency)
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
        # Dependencies admitted before computation, recorded only after the
        # product is published. Later edits cannot relabel an old computation
        # as having used the newly changed resources or configuration.
        self._view_dependencies: dict[tuple[Path, str], ViewInspection] = {}
        # image SHA-1 -> the excerpt found for it in this run
        # (_excerpts_of): one document however many containers place it
        self._excerpts: dict[str, ExcerptRun] = {}
        # Frozen from every admitted current container before reading begins.
        # Discovery of excerpt readings must not choose their context scope.
        self._interpretation_parents: dict[str, tuple[DocumentRun, ...]] = {}
        self._interpretation_contexts: dict[str, Any] = {}
        self._interpretation_readings: dict[str, Any] = {}
        self._interpretation_pending: set[str] = set()
        self._interpretation_failed: set[str] = set()
        # the run's documents, their SHA-1s, and the identity map their
        # writes resolve their entities through, made once an invocation
        # (_identity_map)
        self._run_documents: list[DocumentRun] = []
        self._corpus_sha1s: set[str] = set()
        self._identity = None
        # the run's engine client per role, and its LLM cache, each made
        # on first use and closed when the run ends
        self._engines: dict[str, Any] = {}
        self._cache: Any = None
        self._engine_lock = threading.Lock()
        # the run-scope control request that held a stage in this run, if
        # one did: the run was stopped even when no document was left to
        # dispatch after it
        self._run_held: str | None = None
        self._cancellation_parent = cancellation or current_cancellation()
        self.cancellation = Cancellation(parent=self._cancellation_parent)
        self._cancellation_used = False
        self._control_active = False
        self._active_work: dict[Cancellation, tuple[str, str | None]] = {}
        self._cancellation_lock = threading.Lock()

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

    def _refuse_model_aliases(
        self, roles: Sequence[str] = MODEL_ROLES
    ) -> None:
        """Refuse a run whose configuration names a model by an alias
        (rundir.model_alias): the engine would choose the model, and
        neither the run manifest nor the LLM cache's keys could say
        which."""
        refused = {}
        for role in roles:
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
                check_cancelled()
                engine = make_client(
                    self.llm,
                    effort=effort,
                    model=model,
                    cancellation=self.cancellation,
                )
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
        receives what the cache does and, with or without the cache, an
        event for every call that reaches the engine
        (llm.observed_client).

        A tool that adopts answers for the run's calls (CachingClient.
        complete_key and seed) takes its client from an orchestrator
        configured as the run is, so that its keys are the ones the run's
        calls are served under. A CLI update changes the engine's config
        hash, and with it every key: adoptions made before it are no
        longer found, and those calls miss as absent."""
        from yizkr.pipeline.llm import observed_client

        check_cancelled()
        engine = observed_client(self._engine(role), role=role, events=events)
        cache = self._llm_cache()
        if cache is None:
            return engine
        from yizkr.pipeline.llm import cached_client

        return cached_client(engine, cache, role=role, events=events)

    def _llm(self, role: str, doc: DocumentRun):
        """The engine for one role, as one document's stages see it
        (`role_client`), with what the cache does and every call that
        reaches the engine reported to the run directory under the
        document's SHA-1."""
        return self.role_client(
            role,
            events=functools.partial(
                self.rundir.emit, subject=doc.sha1, kind=doc.kind
            ),
        )

    def _close_engines(self) -> None:
        """Release what the run's engines hold (the claude CLI's session
        transcripts), and forget them and the cache: the next run starts
        its own. A failed shutdown keeps its resources owned for retry;
        session files cannot be released before every engine has stopped.
        """
        primary = sys.exception()
        failures = []
        with self._engine_lock:
            engines = self._engines
            self._engines = {}
            cache = self._cache
        for role, engine in engines.items():
            try:
                close = getattr(engine, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                failures.append(exc)
                with self._engine_lock:
                    self._engines[role] = engine
        if not failures and cache is not None:
            try:
                cache.close()
            except BaseException as exc:
                failures.append(exc)
            else:
                with self._engine_lock:
                    self._cache = None
        if failures:
            terminal = primary if primary is not None else failures[0]
            for failure in failures:
                if failure is not terminal:
                    terminal.add_note(
                        "execution cleanup also failed: "
                        f"{type(failure).__name__}"
                    )
            if primary is None:
                raise terminal

    # -- document discovery --------------------------------------------------

    def documents(self) -> list[DocumentRun]:
        """The folder's documents, in name order, each with a directory of
        its own (directory_names) and its source read for its SHA-1. A
        document whose source or state.json cannot be read is `unreadable`
        (DocumentRun), and so is one whose bytes the never-ingest list's
        hashes cannot be checked against: it is not ingested. The list is
        first given the SHA-1s of the images its globs name in the
        folder's decks (`_gather_embedded`), so a file that is such an
        image's bytes is excluded too."""
        files = []
        unread: dict[Path, str] = {}
        paths = [
            path
            for path in sorted(self.folder.iterdir())
            if path.is_file() and not path.name.startswith(".")
        ]
        self._gather_embedded()
        for path in paths:
            # the never-ingest list is asked first, before any step
            # reads the document, and whatever the file's type: an excluded
            # file gets no state, no artifact and no transcription
            try:
                excluded = self.exclusions is not None and is_excluded(
                    self.exclusions, path
                )
            except OSError as exc:
                excluded = False
                unread[path] = f"its source cannot be read ({_why(exc)})"
            if excluded:
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
        names = directory_names(files, self.state_dir)
        docs = []
        for path in files:
            doc = DocumentRun(path, self.state_dir, dirname=names[path])
            if path in unread:
                doc.unreadable = doc.unreadable or unread[path]
            else:
                doc.read_source()
            docs.append(doc)
        self._check_container_owners(docs)
        return docs

    def _check_container_owners(self, docs: Sequence[DocumentRun]) -> None:
        """Refuse coincident decoded aliases before scheduling any sources.

        This compares original parts across every readable container and
        standalone source in the invocation. It does not infer visual
        equivalence or allocate a durable source registry.
        """
        from yizkr.pipeline.container_sources import check_owner_aliases

        inventories, originals = [], []
        for doc in docs:
            if doc.unreadable is not None:
                continue
            try:
                prepared = self._container_preparation(doc)
            except Exception:
                # The ordinary preparation stage reports malformed sources
                # as document failures; they supply no sources to this check.
                continue
            if prepared is None:
                originals.append(doc.sha1)
            else:
                inventories.append(prepared[0])
        check_owner_aliases(inventories, original_sources=originals)
        self._index_interpretation_parents(docs)

    def _index_interpretation_parents(
        self, docs: Sequence[DocumentRun]
    ) -> None:
        from yizkr.pipeline.container_sources import image_page_source

        parents: dict[str, dict[str, DocumentRun]] = {}
        seen = set()
        for doc in docs:
            if doc.unreadable is not None or doc.sha1 in seen:
                continue
            seen.add(doc.sha1)
            try:
                projection = self._container_sources(doc)
            except Exception:
                # A preparation failure supplies no verified correspondence.
                continue
            if projection is None:
                continue
            for body in projection.inventory.parts:
                if body.kind != "page_body":
                    continue
                located = image_page_source(projection, body)
                if located is not None:
                    source, _part, _placement = located
                    parents.setdefault(source.owner.sha1, {})[doc.sha1] = doc
        self._interpretation_parents = {
            owner: tuple(values[key] for key in sorted(values))
            for owner, values in parents.items()
        }
        self._interpretation_contexts = {}
        self._interpretation_readings = {}

    def _interpretation_context(self, doc: DocumentRun, transcription):
        """Build interpretation inputs from completed readings only."""
        parents = self._interpretation_parents.get(doc.sha1, ())
        if not parents:
            return None
        from yizkr.pipeline.interpretation_context import (
            ContainerReadings,
            ContextGap,
            build_interpretation_context,
        )

        containers, unavailable = [], []
        for parent in parents:
            try:
                sources = self._container_sources(parent)
            except Exception:
                sources = None
            if sources is None:
                unavailable.append(
                    ContextGap(
                        container=ArtifactRef(sha1=parent.sha1),
                        reason="container preparation unavailable",
                    )
                )
                continue
            reading = self._interpretation_readings.get(parent.sha1)
            images = (
                self._interpretation_images(parent, sources)
                if reading is not None
                else {}
            )
            containers.append(ContainerReadings(sources, reading, images))
        context = build_interpretation_context(transcription, containers)
        return context.model_copy(
            update={"gaps": (*context.gaps, *unavailable)}
        )

    def _interpretation_images(self, parent: DocumentRun, sources):
        from yizkr.pipeline.transcribe import Transcription

        images = {}
        for owner, source in sources.sources.items():
            if (
                source.readable
                and source.reading_path is not None
                and self._image_failure(parent, owner) is None
            ):
                # Unreadable products remain explicit gaps in the builder.
                with contextlib.suppress(OSError, ValueError):
                    images[owner] = Transcription.model_validate_json(
                        (parent.dir / source.reading_path).read_bytes()
                    )
        return images

    def _prepare_interpretation_context(self, doc: DocumentRun, transcription):
        """Publish the separate current input before any downstream reuse."""
        context = self._interpretation_context(doc, transcription)
        self._interpretation_contexts[doc.sha1] = context
        path = doc.interpretation_context
        if context is None:
            if path.exists():
                path.unlink()
                doc.derivations.forget(path)
        else:
            data = context.model_dump(mode="json")
            _write_view(path, json.dumps(data, indent=2, sort_keys=True))
            doc.derivations.set_content(path.name, content_hash(data))
        return context

    def _gather_embedded(self) -> None:
        """Give the never-ingest list the SHA-1s of the images its globs
        name, by their names in the archive, in the folder's decks, in
        place of any it was given before. The decks are selected and
        hashed by corpus.follow_decks, the one function the single-file
        steps and the transcription import ask too, so every path asks the
        list a run asks. This comes before any container's images are
        read, so the reader (`_excluded_image`) and the write's registrar,
        which both ask this list, keep those bytes out of every deck and
        PDF that holds them as they are, and out of the run as a file of
        their own (ADR-0008). A deck the list excludes is asked too: its
        members are hashed in memory and nothing of it is kept, so listing
        a deck never frees the bytes a glob names in it. A deck that
        cannot be read as an archive, or a folder that cannot be listed,
        refuses the run (ExclusionConfigError), since the bytes its globs
        would name cannot be known."""
        if self.exclusions is None:
            return
        self.exclusions = follow_decks(self.exclusions, self.folder)

    @contextlib.contextmanager
    def _cancellation_scope(self):
        """Own the invocation signal and its control-file watcher."""
        if self._cancellation_used:
            self.cancellation = Cancellation(parent=self._cancellation_parent)
        self._cancellation_used = True
        finished = threading.Event()
        invocation_signal = self.cancellation

        def watch():
            while not finished.wait(0.1):
                if not self._control_active:
                    continue
                try:
                    control = self.rundir.control()
                    if finished.is_set():
                        return
                    if control.scopes.get("run") == "aborted":
                        invocation_signal.cancel("run aborted")
                    with self._cancellation_lock:
                        active = list(self._active_work.items())
                    for token, (stage, kind) in active:
                        for scope in (f"stage:{stage}", f"kind:{kind}"):
                            if control.scopes.get(scope) == "aborted":
                                token.cancel(f"{scope} aborted")
                                break
                except Exception as exc:
                    if not finished.is_set():
                        invocation_signal.cancel(exception=exc)
                    return

        watcher = threading.Thread(
            target=watch, name="yizkr-run-control", daemon=True
        )
        watcher.start()
        try:
            with cancellation_scope(self.cancellation):
                self.cancellation.check()
                yield
        except KeyboardInterrupt as exc:
            self.cancellation.cancel(exception=exc)
            raise
        finally:
            self._control_active = False
            finished.set()
            watcher.join(timeout=1.0)
            self.cancellation.close()

    def _cancellation_reason(self, exc: RunCancelled) -> str:
        """Keep native control reasons; never copy arbitrary exception text."""
        reason = str(exc)
        control = self.rundir.control()
        if any(
            state == "aborted" and reason == f"{scope} aborted"
            for scope, state in control.scopes.items()
        ):
            return reason
        return type(exc).__name__

    @contextlib.contextmanager
    def _stage_cancellation(self, stage: str, kind: str | None = None):
        """Bind borrowed calls to the active stage's abort scope."""
        token = Cancellation(
            parent=current_cancellation() or self.cancellation
        )
        with self._cancellation_lock:
            self._active_work[token] = (stage, kind)
        try:
            with cancellation_scope(token):
                if self._control_active:
                    control = self.rundir.control()
                    if control.scopes.get("run") == "aborted":
                        self.cancellation.cancel("run aborted")
                    for scope in (f"stage:{stage}", f"kind:{kind}"):
                        if control.scopes.get(scope) == "aborted":
                            token.cancel(f"{scope} aborted")
                            break
                token.check()
                yield token
                token.check()
        finally:
            with self._cancellation_lock:
                self._active_work.pop(token, None)
            token.close()

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
        check_cancelled()
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
        typed_result = stage in {
            "transcription",
            "extraction",
            "gedcom",
            "critique",
        }
        if typed_result:
            detail = dict(doc.state.detail or {})
            outcomes = dict(detail.get("stage_results") or {})
            outcomes.pop(stage, None)
            doc.state.detail = detail | {"stage_results": outcomes}
        try:
            with self._stage_cancellation(stage, doc.kind):
                result = compute()
        except BaseException as exc:
            if typed_result:
                from yizkr.pipeline.llm import PassPaused

                outcomes = doc.state.detail["stage_results"]
                # Preserve an explicit incomplete result and its diagnostics;
                # otherwise an earlier successful attempt cannot describe this
                # failed, paused or cancelled computation.
                if (
                    stage not in outcomes
                    or outcomes[stage]["status"] == "complete"
                ):
                    outcomes[stage] = {
                        "status": "paused"
                        if isinstance(exc, PassPaused)
                        else "cancelled"
                        if isinstance(exc, (KeyboardInterrupt, RunCancelled))
                        else "failed"
                    }
            if isinstance(exc, RunCancelled):
                self.rundir.emit(
                    "cancelled", reason=self._cancellation_reason(exc), **where
                )
            if isinstance(exc, Exception):
                reason = f"{type(exc).__name__}: {exc}"
                self.rundir.emit("failed", reason=reason[:500], **where)
                from yizkr.pipeline import anomalies

                anomalies.failed(self.rundir, doc, stage)
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

    def _refresh_requested(
        self, doc: DocumentRun, role: str, stage: str
    ) -> bool:
        """Whether invocation policy requires this view to be recomputed."""
        return role in self.refresh

    def _processing_dependencies(
        self, doc: DocumentRun, stage: str, *, route: str | None = None
    ) -> dict[str, Any]:
        """Observe declared generation inputs without creating a client."""
        from yizkr.pipeline.textlayer import is_pdf_input

        descriptor = stage_planning.STAGES[stage]
        configuration = {}
        if descriptor.process != "deterministic":
            route = route or self._route(doc)
            if stage == "transcription":
                from yizkr.pipeline.media_stages import (
                    excerpt_dependencies,
                    preparation_dependencies,
                )

                configuration.update(
                    route=route,
                    language=self.language,
                    fallback_language=self.fallback_language,
                )
                prepared = (
                    self._media_preparation(doc)
                    if doc.kind != "text"
                    else None
                )
                if prepared is not None:
                    configuration["preparation"] = preparation_dependencies(
                        prepared
                    )
                if route == EXCERPT:
                    candidates = self._excerpt_candidates(doc)
                    if not candidates:
                        raise OSError(
                            "no current verified container image reading"
                        )
                    configuration["excerpt"] = excerpt_dependencies(
                        candidates[0]
                    )
            if stage != "transcription" or route in {"vision", PAGED}:
                role = descriptor.role
                effort, _ = self._engine_settings(role)
                model, _ = self._configured_model(role)
                configuration.update(
                    engine=self.llm,
                    model=model,
                    effort=effort if self.llm != "anthropic" else None,
                    epoch=self.epochs.get(role, 0),
                )
            if stage == "extraction":
                from yizkr.pipeline.text_stages import ExtractionRequest

                configuration.update(
                    strategy="chunked",
                    budget=ExtractionRequest.budget,
                    retries=ExtractionRequest.retries,
                    concurrency=self.chunk_concurrency,
                    read_preparation=True,
                )
        return stage_planning.processing_dependencies(
            stage,
            configuration=configuration,
            source_sha1=doc.sha1,
            document_kind=PDF
            if doc.kind not in {"text", "gedcom"} and is_pdf_input(doc.source)
            else doc.kind,
        )

    def _require_processing_dependencies(
        self, doc: DocumentRun, stage: str, *, route: str | None = None
    ) -> dict[str, Any]:
        """Observe required processing inputs before a stage may execute."""
        try:
            return self._processing_dependencies(doc, stage, route=route)
        except OSError as exc:
            raise ProcessingUnavailable(
                f"cannot inspect {stage} processing dependencies: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _inspect_view(
        self,
        doc: DocumentRun,
        view: Path,
        load: Callable[[str], Any],
        recheck: Callable[[Any], str | None] | None = None,
    ) -> ViewInspection:
        """Observe and plan reuse without writing or creating a client.

        Execution and estimation share this observation and pure decision.
        Review binding is deliberately outside this path: observing a view
        cannot authorize it. Downstream stages compare semantic products
        after a producer runs, not the producer's generation fingerprint.
        """
        role, stage = doc.views[view]
        recorded = doc.derivations._load()
        inputs = doc.derivations._inputs(view)
        if (
            stage in {"extraction", "critique"}
            and doc.sha1 in self._interpretation_contexts
        ):
            context = self._interpretation_contexts[doc.sha1]
            if context is None:
                inputs.pop(doc.interpretation_context.name, None)
            else:
                inputs[doc.interpretation_context.name] = content_hash(
                    context.model_dump(mode="json")
                )
        try:
            processing = self._require_processing_dependencies(doc, stage)
        except ProcessingUnavailable as exc:
            return ViewInspection(
                stage_planning.plan_view(
                    exists=view.is_file(),
                    recorded_inputs=None,
                    current_inputs={},
                    blocked_reason="processing-unavailable",
                ),
                blocked_reason=str(exc),
            )
        plan = stage_planning.plan_view(
            exists=view.is_file(),
            recorded_inputs=recorded["derived"].get(view.name),
            current_inputs=inputs,
            recorded_processing=recorded["processing"].get(view.name),
            processing=processing,
            refresh=self._refresh_requested(doc, role, stage),
        )
        if plan.action != "reuse":
            return ViewInspection(plan, processing=processing, inputs=inputs)
        try:
            value = load(view.read_text())
        except (OSError, ValueError, RecursionError):
            return ViewInspection(
                stage_planning.FreshnessPlan(
                    "recompute", ("unreadable-output",), ()
                ),
                processing=processing,
                inputs=inputs,
            )
        failure = recheck(value) if recheck is not None else None
        if failure is not None:
            return ViewInspection(
                stage_planning.FreshnessPlan(
                    "recompute", ("invalid-output",), ()
                ),
                invalid_reason=failure,
                processing=processing,
                inputs=inputs,
            )
        return ViewInspection(
            plan, value, processing=processing, inputs=inputs
        )

    def _record_view(self, doc: DocumentRun, view: Path) -> None:
        """Record a published product and its generation dependencies."""
        check_cancelled()
        observed = self._view_dependencies.pop((doc.dir, view.name))
        doc.derivations.record(
            view, inputs=observed.inputs, processing=observed.processing
        )

    def _reused_view(
        self,
        doc: DocumentRun,
        view: Path,
        load: Callable[[str], Any],
        recheck: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """The document's `view`, loaded, when the shared plan reuses it.

        Inputs and migrated processing dependencies must match, refresh
        must not request recomputation, and `recheck` must pass. A reuse is an
        event, with the attention reasons noted when the view was made.
        A view `recheck` fails is moved aside, never deleted, with a
        `set-aside` event that gives the reason, and its record is
        forgotten. None when the view is to be made."""
        check_cancelled()
        _role, stage = doc.views[view]
        inspected = self._inspect_view(doc, view, load, recheck)
        inspected.require_value()
        if inspected.plan.action == "recompute":
            self._view_dependencies[(doc.dir, view.name)] = inspected
        failure = inspected.invalid_reason
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
        if inspected.plan.action != "reuse":
            return None
        loaded = inspected.value
        noted = (doc.state.detail or {}).get("attention") or {}
        self.rundir.emit(
            REUSED,
            subject=doc.sha1,
            kind=doc.kind,
            stage=stage,
            attention=noted.get(stage) or None,
        )
        from yizkr.pipeline import anomalies

        anomalies.reused(self.rundir, doc, stage, loaded)
        return loaded

    # -- pipeline steps (override points for tests) ---------------------------

    def _step_register(self, doc: DocumentRun) -> str:
        """Register the document as its write begins (ADR-0025 §2): its
        artifact item, found or made by the SHA-1 of the source's bytes
        (ADR-0010), and for a deck its slides as page fragments, and for a
        deck or a PDF the image sources its common container inventory
        binds (yizkr.pipeline.container_sources), each by the SHA-1 of its
        own bytes and part of the container on each slide or page that
        paints it (ADR-0008). A source that no longer
        has the bytes the run read, or that is on the never-ingest list,
        is refused before anything is registered or uploaded, and an
        embedded image the list names is left out
        (yizkr.pipeline.registrar). An excerpt is registered as the
        derived medium of each container that places it, each container
        registered first. Returns the artifact's QID.

        An item the registrar flags as maybe an older artifact (another
        item names a file that holds its bytes, or its title is the label
        of an item with no hash; RegistrationFlag) is raised as an alert
        of the write on the document, its check the flag's kind, for a
        person to look at, the flags of a registration cut short by an
        error or an interrupt included. An item is flagged on each write
        while the condition holds, whether registration made it or found
        it, so a flag a killed process never raised is raised by the next
        write. The alert holds ids and hashes alone: `artifact`, the
        item; `others`, the items it may be; `medium`, the first 12 hex
        digits of the item's media SHA-1 (docs/ANOMALIES.md)."""
        from yizkr.pipeline.registrar import Registrar

        registrar = Registrar(self.client, exclusions=self.exclusions)
        try:
            return self._register_document(registrar, doc)
        finally:
            self._raise_flags(doc, registrar.flags)

    def _raise_flags(self, doc: DocumentRun, flags: Iterable[Any]) -> None:
        """Raise each RegistrationFlag in `flags` as an alert of the write on
        `doc`, its check the flag's kind, and log it (_step_register)."""
        for flag in flags:
            self.rundir.alert(
                stage="write",
                check=flag.kind,
                subject=doc.sha1[:12],
                value=True,
                carried=False,
                kind=doc.kind,
                artifact=flag.artifact,
                others=list(flag.others),
                medium=flag.sha1[:12],
            )
            self.log(
                f"  flagged {flag.kind}: item {flag.artifact}, "
                f"against {', '.join(flag.others)}"
            )

    def _register_document(self, registrar: Any, doc: DocumentRun) -> str:
        """The document's QID, as `_step_register` registers it."""
        from yizkr.pipeline.registrar import ExcludedMedia

        if not isinstance(doc, ExcerptRun):
            return self._register(registrar, doc)[0]
        found = None
        for container in doc.containers:
            derived = self._register(registrar, container)[1]
            found = found or derived.get(doc.sha1)
        if found is None:
            raise ExcludedMedia(
                f"image {doc.sha1[:12]} is on the never-ingest list, or no "
                "container that places it records it: nothing of it was "
                "registered"
            )
        return found

    def _register(self, registrar: Any, doc: DocumentRun) -> tuple[str, dict]:
        """(the document's QID, its derived media's QIDs by SHA-1), as
        `_step_register` registers a document that is not an excerpt. The
        derived media are kept by the document's SHA-1 too
        (registered_media), for the write of its claims."""
        registrar.check_media_source(doc.source, sha1=doc.sha1)
        self._record_container_inventory(doc)
        artifact_type = self.artifact_type
        pages = 0
        if doc.kind == "presentation":
            # a deck is a compound artifact: its slides ARE the page
            # fragments (ADR-0008), so claim locators point into slides
            artifact_type = "presentation"
            pages = len(self._presentation(doc).slides)
        elif doc.kind == "gedcom":
            artifact_type = "genealogical_report"
        result = registrar.register_artifact(
            doc.source,
            artifact_type=artifact_type,
            title=doc.state.title,
            description=(
                f"ingested by `{self.command}` from {doc.source.name}"
            ),
            pages=pages,
            sha1=doc.sha1,
        )
        sources = self._container_sources(doc)
        derived: dict[str, str] = {}
        if sources is not None:
            derived = registrar.register_container_sources(
                sources, doc.dir, title=doc.state.title
            )
            self.registered_media()[doc.sha1] = dict(derived)
            left_out = len(sources.sources) - len(derived)
            self.log(
                f"  derived media: {len(derived)} registered"
                + (
                    f", {left_out} left out (never-ingest list)"
                    if left_out
                    else ""
                )
            )
        return result.artifact_qid, derived

    def registered_media(self) -> dict[str, dict[str, str]]:
        """The derived media this orchestrator registered, by the SHA-1 of
        their container: each medium's QID by its SHA-1 (_register). They
        are what a container's `caption` claims may take as their subject
        (ADR-0039 §2.1, yizkr.pipeline.writer)."""
        if not hasattr(self, "_registered_media"):
            self._registered_media: dict[str, dict[str, str]] = {}
        return self._registered_media

    def _container_sources(self, doc: DocumentRun):
        """Current eligible sources, occurrences and readings; no publication.

        All consumers observe the same original-source preparation and current
        policy. A legacy media record is only a verified migration input.
        """
        from yizkr.pipeline.container_sources import project_sources

        prepared = self._container_preparation(doc)
        if prepared is None:
            return None
        inventory, data = prepared
        return project_sources(
            inventory, doc.dir, data=data, excluded=self._excluded_image
        )

    def _media_preparation(self, doc: DocumentRun):
        """Current deterministic preparation, shared by readers and reuse."""
        from yizkr.pipeline.media_stages import (
            PreparationRequest,
            prepare_container,
        )

        if not hasattr(self, "_media_preparations"):
            self._media_preparations = {}
        key = (doc.dir, doc.sha1)
        if key not in self._media_preparations:
            self._media_preparations[key] = prepare_container(
                PreparationRequest(
                    source=doc.source,
                    expected_artifact=ArtifactRef(sha1=doc.sha1),
                    existing_directory=doc.dir,
                    excluded=self._excluded_image,
                )
            )
        return self._media_preparations[key]

    def _presentation(self, doc: DocumentRun):
        prepared = self._media_preparation(doc)
        if prepared is None or prepared.presentation is None:
            raise ValueError("source has no prepared presentation")
        return prepared.presentation

    def _container_preparation(self, doc: DocumentRun):
        """Expose current preparation without publishing persisted views."""
        prepared = self._media_preparation(doc)
        if prepared is None:
            return None
        # Readers can add representations and outcomes to this preparation.
        return prepared.inventory, prepared.data

    def _record_container_inventory(self, doc: DocumentRun) -> None:
        """Publish source correspondence while retaining human products."""
        from yizkr.pipeline.container_inventory import keep_inventory

        prepared = self._container_preparation(doc)
        if prepared is not None:
            inventory, data = prepared
            keep_inventory(inventory, data, doc.dir)

    def _pdf_inventory(self, doc: DocumentRun):
        prepared = self._media_preparation(doc)
        if prepared is None or prepared.pdf is None:
            raise ValueError("source has no prepared PDF")
        return prepared.pdf

    def _record_pdf_inventory(self, doc: DocumentRun) -> None:
        """Publish PDF source parts before choosing or reusing a reading.

        Missing metadata and damaged representations are rebuilt without
        invalidating an unchanged transcription, proposal or review.
        """
        from yizkr.pipeline.container_inventory import (
            keep_inventory,
            read_inventory,
        )
        from yizkr.pipeline.textlayer import PdfExclusionError, is_pdf_input

        if is_pdf_input(doc.source):
            prepared = self._pdf_inventory(doc)
            previous = read_inventory(doc.dir)
            if (
                previous is not None
                and previous.source == prepared.inventory.source
            ):
                parts = {part.id: part for part in prepared.inventory.parts}
                for old in previous.parts:
                    part = parts.get(old.id)
                    if part is None or part.kind not in {
                        "container_source",
                        "page_body",
                    }:
                        continue
                    try:
                        prepared.guard(
                            {part.locator.position}
                            if part.kind == "page_body"
                            else None
                        )
                    except PdfExclusionError:
                        continue
                    for representation in old.representations:
                        if (
                            representation.file is not None
                            and representation.method
                            in {
                                "rendered-pdf-v1",
                                "pdfium-page-render",
                                "pdf-page-image",
                            }
                            and representation.eligibility.state == "allowed"
                            and not any(
                                current.method == representation.method
                                for current in part.representations
                            )
                        ):
                            part.representations.append(representation)
                            prepared.data[representation.byte_object.sha1] = (
                                doc.dir / representation.file
                            ).read_bytes()
            keep_inventory(prepared.inventory, prepared.data, doc.dir)

    def _restore_pdf_representations(self, doc: DocumentRun, transcription):
        """Recreate missing prepared bytes without making another model call.

        The caller has validated the saved reading. These are current source
        representations, not proof of an unrecorded historical model payload.
        """
        from tempfile import TemporaryDirectory

        from yizkr.pipeline.textlayer import prepare_pdf_vision, read_pages

        prepared = self._pdf_inventory(doc)
        if transcription.input_representation == "rendered-pdf-v1":
            if any(
                rep.method == "rendered-pdf-v1"
                for part in prepared.inventory.parts
                for rep in part.representations
            ):
                return
            with TemporaryDirectory(
                prefix="yizkr-pdf-inventory-"
            ) as directory:
                prepare_pdf_vision(
                    doc.source,
                    Path(directory),
                    self._excluded_image,
                    inventory=prepared,
                )
        else:
            needed = {
                page.page
                for page in transcription.pages or ()
                if page.provenance.method == "vision"
            }
            if not any(
                not prepared.page_parts[number].representations
                for number in needed
            ):
                return
            with TemporaryDirectory(
                prefix="yizkr-pdf-inventory-"
            ) as directory:
                read_pages(
                    doc.source,
                    Path(directory),
                    excluded=self._excluded_image,
                    inventory=prepared,
                )
        self._record_pdf_inventory(doc)

    def _keep_inventory_readings(
        self, doc: DocumentRun, reading=None, transcription=None
    ) -> None:
        """Record actual image outcomes beside the structural inventory.

        Native text belongs to the producer. Image success requires a
        validated reading of those bytes, including an explicitly empty
        reading. Reconstructing missing inventory can adopt an existing
        valid reading without making another model call.
        """
        from yizkr.pipeline.container_inventory import (
            ReadingOutcome,
            keep_inventory,
        )
        from yizkr.pipeline.presentation import derived_transcription
        from yizkr.pipeline.transcribe import Transcription

        if transcription is not None and doc.kind == PDF:
            self._restore_pdf_representations(doc, transcription)
        prepared = self._container_preparation(doc)
        if prepared is None:
            return
        inventory, data = prepared
        if inventory.source.sha1 != doc.sha1:
            raise ValueError("container inventory is missing or invalid")
        observed = (
            {image.sha1: image for image in reading.images}
            if reading is not None
            else {}
        )
        for part in inventory.parts:
            binding = part.owner_binding
            if (
                part.kind != "embedded_resource"
                or binding is None
                or part.ownership_issue is not None
                or part.eligibility.state != "allowed"
                or part.reading.state == "unsupported"
            ):
                continue
            sha1 = binding.artifact.sha1
            image = observed.get(sha1)
            if image is not None and image.failure is not None:
                part.reading = ReadingOutcome(
                    state="failed", reason=image.failure
                )
                continue
            path = derived_transcription(doc.dir, sha1)
            if not path.is_file() or path.is_symlink():
                if part.reading.state != "failed":
                    part.reading = ReadingOutcome(state="unattempted")
                continue
            failure = self._image_failure(
                doc, sha1, verify_product=image is None
            )
            if failure is not None:
                part.reading = ReadingOutcome(state="failed", reason=failure)
                continue
            reading_data = path.read_bytes()
            image_transcription = Transcription.model_validate_json(
                reading_data
            )
            record = image_transcription.record()
            empty = not any(page.text.strip() for page in record.pages)
            part.reading = ReadingOutcome(
                state="read",
                method=image_transcription.engine,
                empty=empty,
                product=path.relative_to(doc.dir).as_posix(),
                product_sha256=hashlib.sha256(reading_data).hexdigest(),
            )
        if inventory.container == "pdf" and transcription is not None:
            pages = {page.page: page for page in transcription.pages or ()}
            digest = hashlib.sha256(doc.transcription.read_bytes()).hexdigest()
            for part in inventory.parts:
                if (
                    part.kind == "container_source"
                    and transcription.input_representation == "rendered-pdf-v1"
                ):
                    part.reading = ReadingOutcome(
                        state="read",
                        method=transcription.engine,
                        empty=not any(
                            page.text.strip()
                            for page in transcription.record().pages
                        ),
                        product=doc.transcription.name,
                        product_sha256=digest,
                    )
                if part.kind != "page_body":
                    continue
                page = pages.get(part.locator.position)
                if page is not None:
                    part.reading = ReadingOutcome(
                        state="read",
                        method=page.provenance.engine,
                        empty=not transcription.text[
                            page.start : page.end
                        ].strip(),
                        product=doc.transcription.name,
                        product_sha256=digest,
                    )
        keep_inventory(inventory, data, doc.dir)

    def _step_batch(self, doc: DocumentRun) -> ClaimBatch:
        """Dispatch the batch producer declared by the document's plan."""
        producers = {
            "gedcom": self._step_gedcom,
            "extraction": lambda doc: self._step_extract(
                doc, self._transcription(doc)
            ),
        }
        return producers[stage_planning.batch_stage(doc.kind).name](doc)

    def _step_gedcom(self, doc: DocumentRun) -> ClaimBatch:
        from yizkr.pipeline.gedcom import format_unsupported_tags
        from yizkr.pipeline.gedcom_stages import GedcomRequest, derive_gedcom

        def compute():
            result = derive_gedcom(
                GedcomRequest(doc.source, ArtifactRef(sha1=doc.sha1))
            )
            self._record_stage_result(doc, "gedcom", result)
            return result

        result = self._run_stage(doc, "gedcom", compute)
        detail = doc.state.detail or {}
        # the adapter is deterministic: say what it could not adapt when
        # that is news, not on every run
        if (
            detail.get("gedcom_warnings") != result.warnings
            or detail.get("gedcom_unsupported") != result.unsupported_tags
            or detail.get("gedcom_outcomes") != result.outcomes
        ):
            for warning in result.warnings:
                self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
            if result.unsupported_tags:
                self.log(
                    f"  {doc.source.name}: unsupported GEDCOM tags: "
                    + format_unsupported_tags(result.unsupported_tags),
                    file=sys.stderr,
                )
            if result.outcomes:
                self.log(
                    f"  {doc.source.name}: GEDCOM outcomes: "
                    + format_unsupported_tags(result.outcomes),
                    file=sys.stderr,
                )
        doc.state.detail = detail | {
            "gedcom_warnings": result.warnings,
            "gedcom_unsupported": result.unsupported_tags,
            "gedcom_outcomes": result.outcomes,
        }
        return result.require_complete()

    def _transcription(self, doc: DocumentRun):
        """The document's transcription view, reused when it stands
        (`_reused_view`) and made otherwise, then recorded with its
        canonical content hash; once per run. A transcription made by a
        route whose transcriber checks its output (CHECKED_ROUTES) is
        checked again when it is reused: the check may have become
        stricter since the view was made, and one it now fails is made
        again (`_transcription_failure`)."""
        from yizkr.pipeline.transcribe import Transcription

        memo = (doc.sha1, doc.transcription.name)
        if memo in self._views:
            return self._views[memo]
        from yizkr.pipeline.textlayer import is_pdf_input

        if doc.transcription.is_file():
            self._record_pdf_inventory(doc)
        checked = self._route(doc) in CHECKED_ROUTES or is_pdf_input(
            doc.source
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
            self._record_view(doc, doc.transcription)
            from yizkr.pipeline import anomalies

            anomalies.stored(self.rundir, doc, "transcription", transcription)
        located = self._locate_transcription(doc, transcription)
        if located.source_context != transcription.source_context:
            transcription = located
            _write_view(
                doc.transcription, transcription.model_dump_json(indent=2)
            )
            doc.derivations.set_content(
                doc.transcription.name,
                content_hash(
                    transcription.model_dump(mode="json"),
                    volatile=TRANSCRIPTION_VOLATILE,
                ),
            )
        if self._container_preparation(doc) is not None:
            self._keep_inventory_readings(doc, transcription=transcription)
        self._views[memo] = transcription
        return transcription

    def _locate_transcription(self, doc: DocumentRun, transcription):
        """Attach source correspondence verified from current preparation."""
        from yizkr.pipeline.container_sources import source_context

        if isinstance(doc, ExcerptRun):
            return self._locate_excerpt(doc, transcription)
        sources = self._container_sources(doc)
        if sources is None:
            return transcription
        return transcription.model_copy(
            update={"source_context": source_context(transcription, sources)}
        )

    def _excerpt_candidates(self, doc: ExcerptRun):
        """Verified readings in the existing deterministic placement order."""
        return [
            check.reading
            for container in doc.containers
            if (check := self._image_reading(container, doc.sha1)).reading
            is not None
        ]

    def _locate_excerpt(self, doc: ExcerptRun, transcription):
        """Bind excerpt readings to verified original image parts."""
        from yizkr.pipeline.container_sources import image_source_context
        from yizkr.pipeline.source_context import SourceContext
        from yizkr.pipeline.transcribe import reading_digest

        context = SourceContext(
            container=ArtifactRef(sha1=doc.containers[0].sha1),
            limitations=["excerpt has no matching verified container reading"],
        )
        for candidate in self._excerpt_candidates(doc):
            # Language resolution can change without changing the image text.
            kept = candidate.transcription.model_copy(
                update={"language": transcription.language}
            )
            if reading_digest(kept) != reading_digest(transcription):
                continue
            context = image_source_context(transcription, candidate.supports)
            break
        return transcription.model_copy(update={"source_context": context})

    def _route(self, doc: DocumentRun) -> str:
        """The route the document's transcription takes: an excerpt's is
        the reading its container made of it (EXCERPT), unless a curator's
        correction routes it to the correction."""
        route = route_transcription(doc.source, doc.correction)
        if isinstance(doc, ExcerptRun) and route == "vision":
            return EXCERPT
        return route

    def _transcription_failure(
        self, doc: DocumentRun, transcription
    ) -> str | None:
        """Why the transcription check fails `transcription` of the
        document now, or None when it passes. A vision transcription made
        whole is checked with its record, and fails without one; a text
        file's, which has none, by its text. A container read page by page
        (a PDF's, a deck's) is checked where the vision engine read it:
        each page it read, with the page's one-page record
        (`Transcription.page_record`), as its reading was checked when the
        page was made, so that a page the record calls blank needs no
        text; and the transcription of each image placed within a page
        that the document's directory keeps (`_image_failure`). Its pages
        read from the file's own text (a PDF's text layer, a deck's slide
        XML) are the document's own text and are never checked. It is made
        again, too, when what it marks is not kept whole beside it
        (`_reading_failure`). A transcription that passes carries the flag
        of the check made now, or none (the first vision page's, for a
        container read page by page),
        since a check changed since it was made may flag it otherwise; the
        flag is no part of its content. It reads files only, and writes
        none."""
        from yizkr.pipeline.textlayer import VISION, is_pdf_input
        from yizkr.pipeline.transcribe import check_vision_page, is_paged
        from yizkr.pipeline.transcription_check import (
            TranscriptionCheckFailed,
            check_transcription,
            is_pdf,
            media_page_count,
        )

        if is_pdf_input(doc.source):
            from yizkr.pipeline.transcribe import pdf_reading_failure

            failure = pdf_reading_failure(
                doc.source,
                transcription,
                self._excluded_image,
                inventory=self._pdf_inventory(doc),
            )
            if failure is not None:
                return failure
            if self._route(doc) not in CHECKED_ROUTES:
                # A curator's correction remains exempt from model-answer
                # quality checks; it still needs source admission above.
                return None
        if not is_paged(transcription):
            sources = self._container_sources(doc)
            if sources is not None and any(
                source.readable for source in sources.sources.values()
            ):
                return "container has independent source readings to prepare"
            check = check_transcription(
                transcription.text,
                pages=media_page_count(doc.source),
                record=transcription.record(),
                expect_record=transcription.engine.startswith("llm-vision:"),
                whole_pdf=is_pdf(doc.source),
            )
            if not check.passed:
                return str(TranscriptionCheckFailed(check))
            transcription.flag = check.signature if check.flagged else None
            return None
        flag = None
        for page in transcription.pages or ():
            if page.provenance.method == VISION:
                check = check_vision_page(transcription, page)
                if not check.passed:
                    return (
                        f"{TranscriptionCheckFailed(check)} (page {page.page})"
                    )
                if check.flagged and flag is None:
                    flag = check.signature
            for sha1 in page.images:
                failure = self._image_failure(doc, sha1)
                if failure is not None:
                    return failure
        failure = self._reading_failure(doc, transcription)
        if failure is None:
            transcription.flag = flag
        return failure

    def _reading_failure(self, doc: DocumentRun, transcription) -> str | None:
        """Verify marked readings against eligible structural sources.

        Inventory reconstruction and an absent legacy media record do not
        invalidate a reading. Changed source selection or missing image
        readings do: no stored marker grants admission by itself.
        """
        from yizkr.pipeline.presentation import derived_transcription

        images = list(
            dict.fromkeys(
                sha1
                for page in transcription.pages or ()
                for sha1 in page.images
            )
        )
        if doc.kind == "presentation" and transcription.pages is None:
            return "it has no pages, one per slide"
        projection = self._container_sources(doc)
        if projection is None:
            return None if not images else "reading has no container inventory"
        if doc.kind == "presentation" and any(
            body.text is None
            or body.reading.state in {"failed", "unsupported"}
            for body in projection.bodies
        ):
            return "current slide content or background is unresolved"
        from yizkr.pipeline.container_sources import image_page_source
        from yizkr.pipeline.containers import IMAGE_SOURCE

        image_pages = {
            body.locator.position
            for body in projection.bodies
            if image_page_source(projection, body) is not None
        }
        for page in transcription.pages or ():
            if (page.page in image_pages) != (
                page.provenance.method == IMAGE_SOURCE
            ):
                return "the reading's image-source page routes have changed"
        for sha1 in images:
            source = projection.sources.get(sha1)
            if source is None or self._excluded_image(sha1, source.names):
                return (
                    f"image {sha1[:12]} has no eligible bound source in "
                    "the current inventory"
                )
        expected = {
            sha1
            for sha1, source in projection.sources.items()
            if source.readable
        }
        placed = {
            sha1
            for sha1 in expected
            if any(
                p.occurrence == "paint"
                for p in projection.sources[sha1].placements
            )
        }
        if set(images) != placed:
            return (
                "the reading's images differ from the independent sources "
                "in the current inventory"
            )
        for sha1 in expected:
            if not derived_transcription(doc.dir, sha1).exists():
                return (
                    f"image {sha1[:12]} has no transcription of its own, so "
                    "it is read again"
                )
            failure = self._image_failure(doc, sha1)
            if failure is not None:
                return failure
        return None

    def _image_reading(
        self, doc: DocumentRun, sha1: str, *, verify_product: bool = True
    ):
        """Verify a saved image product against current source preparation."""
        from yizkr.pipeline.media_stages import verify_image_reading

        return verify_image_reading(
            self._container_sources(doc),
            sha1,
            doc.dir,
            verify_product=verify_product,
        )

    def _image_failure(
        self, doc: DocumentRun, sha1: str, *, verify_product: bool = True
    ) -> str | None:
        return self._image_reading(
            doc, sha1, verify_product=verify_product
        ).failure

    def _step_transcribe(self, doc: DocumentRun):
        """Read through the shared operation, then publish its products."""
        from yizkr.pipeline.textlayer import is_pdf_input
        from yizkr.pipeline.transcribe import is_paged

        def transcribe():
            self._record_pdf_inventory(doc)
            result = self._transcribe(doc, self._route(doc))
            try:
                transcription = result.require_complete()
                check_cancelled()
                self._record_pdf_inventory(doc)
                if result.products is not None:
                    self._keep_reading(doc, result.products)
                resolution = result.resolution
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
                if is_pdf_input(doc.source):
                    self._keep_inventory_readings(
                        doc, transcription=transcription
                    )
                if is_paged(transcription) and changed:
                    self.log(
                        f"  {doc.source.name}: {_read_by_page(transcription)}"
                    )
                if self.language == "auto" and changed:
                    self.log(
                        f"  {doc.source.name}: language "
                        f"{resolution.describe()}"
                    )
            finally:
                result.close()
            self._record_stage_result(doc, "transcription", result)
            return result

        result = self._run_stage(
            doc,
            "transcription",
            transcribe,
            attention=lambda result: result.attention,
        )
        return result.transcription

    def _transcribe(self, doc: DocumentRun, method: str):
        """Construct a shared read request without publishing any products."""
        from yizkr.pipeline.media_stages import (
            ExcerptReadRequest,
            MediaReadRequest,
            MediaReadResult,
            read_excerpt,
            read_media,
        )
        from yizkr.pipeline.text_stages import TextReadRequest, read_text
        from yizkr.pipeline.textlayer import check_pdf_vision, is_pdf_input

        if method == EXCERPT:
            return read_excerpt(
                ExcerptReadRequest(
                    owner=ArtifactRef(sha1=doc.sha1),
                    candidates=self._excerpt_candidates(doc),
                    language=self.language,
                    fallback_language=self.fallback_language,
                )
            )
        if method in {PAGED, "vision"}:
            return read_media(
                MediaReadRequest(
                    source=doc.source,
                    route=method,
                    expected_artifact=ArtifactRef(sha1=doc.sha1),
                    language=self.language,
                    fallback_language=self.fallback_language,
                    prepared=self._media_preparation(doc),
                    excluded=self._excluded_image,
                ),
                client=functools.partial(self._llm, "transcribe", doc),
            )
        if method not in {"text-file", "correction"}:
            raise ValueError(f"unknown transcription route: {method}")
        if method == "correction" and is_pdf_input(doc.source):
            check_pdf_vision(
                doc.source,
                self._excluded_image,
                inventory=self._pdf_inventory(doc),
            )
        result = read_text(
            TextReadRequest(
                source=doc.source,
                text_file=doc.correction
                if method == "correction"
                else doc.source,
                language=self.language,
                fallback_language=self.fallback_language,
                correction=method == "correction",
            )
        )
        return MediaReadResult(
            transcription=result.transcription,
            resolution=result.resolution,
            dependencies=result.dependencies,
        )

    def _excluded_image(self, sha1: str, names: Sequence[str] = ()) -> bool:
        """Does the never-ingest list name an image a paged container
        places on a page, by the SHA-1 of its bytes or by any of its
        `names` (a deck's image's names in the archive, which its globs
        match, as registration matches them)? Its SHA-1 is named too when
        a glob names those bytes in any deck of the folder
        (`_gather_embedded`), so a PDF's image is asked of a deck's names
        for its bytes. Such an image is never read on its own, kept as the
        container's reading of it, or extracted."""
        return self.exclusions is not None and any(
            is_excluded(self.exclusions, name=name, sha1=sha1)
            for name in names or [None]
        )

    def _keep_reading(self, doc: DocumentRun, reading) -> None:
        """Publish image readings beside the source inventory.

        Reading paths retain their supported compatibility names. Source and
        placement authority is the common inventory; no image-only record is
        reconstructed from transcription markers.
        """
        from yizkr.pipeline.presentation import derived_transcription

        self._record_container_inventory(doc)
        for image in reading.images:
            path = derived_transcription(doc.dir, image.sha1)
            if path.exists() and self._image_failure(doc, image.sha1):
                _supersede(path)
            if image.transcription is not None:
                _write_view(
                    path, image.transcription.model_dump_json(indent=2)
                )
        self._keep_inventory_readings(doc, reading)

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline import text_stages

        # Run ingestion requires the entity manifest for identity/review,
        # so its explicit strategy is chunked even for a short document.
        results: list[text_stages.ExtractionResult] = []

        def extract() -> ClaimBatch:
            pointer = doc.pointed()
            result = text_stages.extract(
                text_stages.ExtractionRequest(
                    transcription=transcription,
                    artifact=ArtifactRef(sha1=doc.sha1),
                    strategy="chunked",
                    state_dir=doc.extraction,
                    diagnostic_dump=doc.extraction / "incomplete.json",
                    concurrency=self.chunk_concurrency,
                    read_preparation=True,
                    pointer=pointer,
                    context=self._interpretation_contexts.get(doc.sha1),
                ),
                None
                if pointer is None
                and text_stages.empty_container_reading(transcription)
                else self._llm("extract", doc),
                log=self.log,
            )
            results.append(result)
            self._record_stage_result(doc, "extraction", result)
            return result.require_complete()

        batch = self._run_stage(
            doc,
            "extraction",
            extract,
            attention=lambda _batch: results[0].attention,
        )
        doc.gaps_found = results[0].gaps
        return batch

    @staticmethod
    def _record_stage_result(doc: DocumentRun, stage: str, result) -> None:
        """Keep execution outcomes beside the existing view dependency record.

        This describes computed products; Derivations remains the authority
        for persisted-view reuse and the caller publishes the accepted view.
        """
        check_cancelled()
        detail = dict(doc.state.detail or {})
        outcomes = dict(detail.get("stage_results") or {})
        outcomes[stage] = {
            "status": result.status,
            "dependencies": result.dependencies,
            "artifacts": [str(path) for path in result.artifacts],
        }
        doc.state.detail = detail | {"stage_results": outcomes}

    def _step_reconcile(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        """Write the document's identity proposals, `equivalences.csv`,
        and return how many there are: none.

        A proposal matches one of the document's entities to an entity
        of a roster, and a stage before the write reads nothing from the
        wiki (ADR-0025 §2), so no roster of the graph, nor its place
        containment, reaches this step, and it has no other roster to
        match against. The CSV holds its header alone, and the identity
        gate has nothing to review."""
        from yizkr.pipeline.reconcile import export_equivalences_csv

        return export_equivalences_csv(
            [], batch.entities, [], doc.equivalences
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
        # every other local ref stays local: the write finds its item
        # through the run's identity map (_identity_map)
        return resolve_batch(batch)

    def _step_export_review(self, doc: DocumentRun, batch: ClaimBatch) -> int:
        from yizkr.pipeline.review import export_review_csv

        return export_review_csv(
            batch,
            doc.review,
            critique_notes=self._critique_notes(doc, batch),
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
        self._prepare_interpretation_context(doc, self._transcription(doc))
        if (
            self._reused_view(
                doc, doc.critique, _findings, recheck=_unreadable_critique
            )
            is not None
        ):
            return
        findings = self._step_critique(doc, batch)
        check_cancelled()
        changed = _write_view(
            doc.critique,
            json.dumps(
                [finding.model_dump(mode="json") for finding in findings],
                indent=2,
            ),
        )
        self._record_view(doc, doc.critique)
        from yizkr.pipeline import anomalies

        anomalies.stored(self.rundir, doc, "critique", findings)
        if findings and changed:
            from yizkr.pipeline.critique import Invalid, note

            invalid = sum(isinstance(f, Invalid) for f in findings)
            self.log(
                f"  critique: {len(findings) - invalid} concern(s) flagged "
                f"for review"
                + (f", {invalid} invalid kept as given" if invalid else "")
            )
            for finding in findings[:6]:
                self.log(
                    "    - "
                    + (
                        f"invalid: {'; '.join(finding.why)}"
                        if isinstance(finding, Invalid)
                        else note(finding)
                    ),
                    file=sys.stderr,
                )

    def _step_critique(self, doc: DocumentRun, batch: ClaimBatch):
        """The extraction critic's findings on the batch, each citation
        read where it stands in the document's transcription (ADR-0003).
        A critique the engine gave up on at its output limit has none, and
        needs attention, so that neither this run nor one that reuses the
        view takes it for a sound extraction's."""
        from yizkr.pipeline import text_stages

        results: list[text_stages.CritiqueResult] = []
        transcription = self._transcription(doc)

        def critique():
            result = text_stages.critique(
                text_stages.CritiqueRequest(
                    batch=batch,
                    transcription=transcription,
                    context=self._interpretation_contexts.get(doc.sha1),
                ),
                self._llm("critique", doc),
            )
            results.append(result)
            self._record_stage_result(doc, "critique", result)
            return result.findings

        return self._run_stage(
            doc,
            "critique",
            critique,
            attention=lambda _findings: results[0].attention,
        )

    def _critique_notes(
        self, doc: DocumentRun, rows: ClaimBatch
    ) -> dict[int, str]:
        """review row index -> the critic's concerns on that row, joined,
        for the review CSV `critique_notes` column (critique.sheet_notes):
        each valid finding on exactly one row, tagged with the
        statement-part it doubts (ADR-0023) and its category, and each
        finding that failed its checks on the first row, marked unchecked.
        `rows` is the batch the sheet lists, the extraction's batch
        resolved; a finding names an image the document places by its
        place, read from the transcription view (critique.artifact_places).
        """
        if not doc.critique.exists() or not doc.batch.exists():
            return {}
        from yizkr.pipeline.critique import (
            artifact_places,
            entries,
            sheet_notes,
        )

        extracted = ClaimBatch.model_validate_json(doc.batch.read_text())
        return sheet_notes(
            entries(json.loads(doc.critique.read_text())),
            extracted,
            rows,
            artifact_places(self._transcription_view(doc)),
        )

    def _transcription_view(self, doc: DocumentRun):
        """The document's transcription view as it stands, or None when it
        is missing or cannot be read: read only, never made."""
        from yizkr.pipeline.transcribe import Transcription

        try:
            return self._locate_transcription(
                doc,
                Transcription.model_validate_json(
                    doc.transcription.read_text()
                ),
            )
        except (OSError, ValueError):
            return None

    # the command an artifact's description says ingested it
    command = "yizkr ingest run"

    # whether a write refuses an instance that holds the writes of another
    # identity map (yizkr.pipeline.writer.OtherIdentityMap): an instance
    # holds one map's writes, but the one the live tests share, whose
    # labels their session marks keep apart
    one_map = True

    def _approved(self, doc: DocumentRun, batch: ClaimBatch) -> ClaimBatch:
        """The claims of `batch` the document's reviewed sheet approves
        (apply_review), kept as the document's approved batch, each warning
        of the review logged."""
        from yizkr.pipeline.review import apply_review

        approved, warnings = apply_review(batch, doc.review_reviewed)
        for warning in warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        doc.approved.write_text(approved.model_dump_json(indent=2))
        return approved

    def _mapped(
        self, doc: DocumentRun, approved: ClaimBatch, instance: bool = False
    ):
        """The run's identity map (_identity_map), once it holds every
        mention of the document's approved batch, read from the batch the
        map was made from, the document's extraction (`doc.batch`): raises
        UnmappedMention otherwise, before anything is registered or
        written (yizkr.pipeline.writer, step 2). With `instance`, and
        `one_map`, it raises OtherIdentityMap too, then, when the instance
        holds the writes of another map (_maps_on_instance), as the writer
        would only after the document's artifact was registered. Returns
        the map and that batch."""
        from yizkr.pipeline.writer import (
            OtherIdentityMap,
            mapped_clusters,
            other_identity_maps,
        )

        identity = self._identity_map()
        extracted = (
            ClaimBatch.model_validate_json(doc.batch.read_text())
            if doc.batch.exists()
            else None
        )
        clusters, _, _ = mapped_clusters(
            approved, identity, mapped_from=extracted
        )
        if instance and self.one_map and clusters:
            others = other_identity_maps(
                self._maps_on_instance(), identity.hash
            )
            if others:
                raise OtherIdentityMap(others)
        return identity, extracted

    def _maps_on_instance(self) -> Iterable[str]:
        """The item labels of the instance, among them its run items' with
        the tags of the identity maps they were written with
        (yizkr.pipeline.writer.other_identity_maps)."""
        _, labels = self.client.label_index()
        return labels

    def _step_write(
        self,
        doc: DocumentRun,
        batch: ClaimBatch,
        approved: ClaimBatch | None = None,
    ) -> dict:
        """Write the claims of `batch`, the document's resolved batch, that
        its reviewed sheet approves (`approved`, made here when not given;
        _approved), through the run's identity map (_mapped)."""
        from yizkr.pipeline.review import is_bulk_accepted
        from yizkr.pipeline.writer import BatchWriter

        if approved is None:
            approved = self._approved(doc, batch)
        identity, extracted = self._mapped(doc, approved)
        # the artifact the write registered (_step_register), which the
        # claims cite by the SHA-1 of its bytes, and the derived media it
        # registered for it, which its captions may take as their subject
        registered = doc.state.artifact_qid
        media = self.registered_media().get(doc.sha1, {}) if registered else {}
        result = BatchWriter(
            self.client,
            artifact_qids={doc.sha1: registered, **media}
            if registered
            else None,
        ).write_batch(
            approved,
            bulk_accepted=is_bulk_accepted(doc.review_reviewed),
            identity=identity,
            mapped_from=extracted,
            one_map=self.one_map,
            placed={doc.sha1: set(media)} if media else None,
        )
        # a registered artifact under the label of an artifact-kind entity
        # of the batch, such as a GEDCOM media object, flagged as
        # registration flags the two, against the entity's item or, for an
        # entity the writer refused, the item under its tagged label
        # (yizkr.pipeline.writer, step 2)
        self._raise_flags(doc, result.flags)
        for warning in result.warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        self.log(
            f"  written: {result.written} new, {result.referenced} "
            f"referenced, {result.skipped} skipped; type statements of "
            f"items found: {result.type_statements_written} new, "
            f"{result.type_statements_referenced} referenced"
        )
        return {
            "run_qid": result.run_qid,
            "written": result.written,
            "skipped": result.skipped,
            "referenced": result.referenced,
            "type_statements_written": result.type_statements_written,
            "type_statements_referenced": result.type_statements_referenced,
            "created_items": result.created_items,
            "refused": result.refused,
            # of `refused`, the claims whose item value is their subject's
            # item (yizkr.pipeline.writer.self_references)
            "self_references": result.self_references,
            # `refused` counted by reason (yizkr.pipeline.writer
            # .REFUSAL_REASONS), every reason in its order
            "refused_by_reason": result.refused_by_reason(),
            "refused_terms": result.refused_terms,
            "approved_claims": len(approved.claims),
        }

    def _identity_map(self):
        """The identity map every write of the invocation resolves its
        entities through (yizkr.pipeline.writer, step 2): the map of every
        document of the run whose batch is made, the excerpts its
        containers' transcriptions have found among them (_excerpts_of), as
        serial mode finds them first, kept in the run directory's corpus
        folder as a scratch run's clustering stage keeps it (clustering.
        ensure_identity_map), and reused while its inputs stand. It is made
        once an invocation, at its first write, and never again within it:
        a document whose batch is made or changed after that is not in it,
        and its write fails, with UnmappedMention, until a later
        invocation. An instance holds the writes of one map: a later
        invocation whose documents give another map is refused by the
        writer (OtherIdentityMap), and its instance is reset first."""
        if self._identity is not None:
            return self._identity
        from yizkr.pipeline.clustering import IdentityMap, ensure_identity_map

        found: dict[str, Any] = {}
        for run in self._run_documents:
            found.setdefault(run.sha1, run)
            for excerpt in self._excerpts_of(run, self._corpus_sha1s):
                found.setdefault(excerpt.sha1, excerpt)
        for excerpt in self._excerpts.values():
            found.setdefault(excerpt.sha1, excerpt)
        documents = [run for run in found.values() if run.batch.exists()]
        output = ensure_identity_map(
            self.state_dir / "corpus" / "clustering", documents
        )
        self._identity = IdentityMap(output.identity_map)
        return self._identity

    # -- excerpts -----------------------------------------------------------

    def _excerpts_of(
        self, doc: DocumentRun, corpus: Collection[str]
    ) -> list[ExcerptRun]:
        """The excerpts of a container read page by page (ExcerptRun), by
        their bound media owners, from the current source projection: each
        eligible independent source that has a reading of its own
        which passes the check (`_image_failure`), and which shows a page
        of a document (`_image_shows_a_page`): an image whose record calls
        every page blank (a photograph with no writing) or not a document
        page (a colour chart) holds nothing to extract. An illegible one
        is an excerpt, so that a reader sees it was there. An image the
        never-ingest list names is left out, asked again here, and so is
        one whose bytes a document of the corpus has (`corpus`, their
        SHA-1s): that document is the witness, and its bytes are not
        extracted again (ADR-0010). Each is found or made by its SHA-1
        among the run's excerpts, so an image that several containers place
        is one excerpt, with each of them among its containers. Reads files
        only, and writes none."""
        try:
            projection = self._container_sources(doc)
        except Exception:
            # A failed original-source preparation is reported by the
            # document's stage. It cannot authorize any excerpt discovery.
            return []
        if projection is None:
            return []
        found = []
        for sha1, medium in sorted(projection.sources.items()):
            if (
                sha1 in corpus
                or medium.reading_path is None
                or not medium.readable
                or not self._image_shows_a_page(doc, sha1)
            ):
                continue
            excerpt = self._excerpts.get(sha1)
            if excerpt is None:
                excerpt = self._excerpts[sha1] = ExcerptRun(
                    sha1, doc, medium, self.state_dir
                )
            else:
                excerpt.placed_by(doc)
            found.append(excerpt)
        return found

    def _image_shows_a_page(self, doc: DocumentRun, sha1: str) -> bool:
        """Whether the reading the document's directory keeps of the image
        with this SHA-1, one that passes the check, shows a page of a
        document: its record calls some page transcribed or illegible,
        not every page blank or not a document page (a reading with no
        record is judged by its text alone, and does)."""
        from yizkr.pipeline.transcription_record import (
            BLANK,
            NOT_A_DOCUMENT_PAGE,
        )

        checked = self._image_reading(doc, sha1)
        if checked.reading is None:
            return False
        record = checked.reading.transcription.record()
        nothing = {BLANK, NOT_A_DOCUMENT_PAGE}
        return record is None or not set(record.statuses()) <= nothing

    def _plan_excerpts(self, excerpts: Sequence[ExcerptRun]) -> None:
        """Add the excerpts the run plan does not hold yet to it, as the
        documents they are, each with its stages (docs/RUN_MANIFEST.md):
        they are found as their containers' transcriptions are made."""
        manifest = self.rundir.manifest()
        planned = list(manifest.get("documents") or [])
        known = {
            entry.get("sha1") for entry in planned if isinstance(entry, dict)
        }
        added = [
            {
                "sha1": excerpt.sha1,
                "file": excerpt.source.name,
                "kind": excerpt.kind,
                "stages": list(DOCUMENT_STAGES[excerpt.kind]),
            }
            for excerpt in excerpts
            if excerpt.sha1 not in known
        ]
        if added:
            self.rundir.update_manifest(documents=planned + added)

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
        """Bring the transcription and proposed batch up to date.

        A view that stands is reused; a changed batch invalidates its
        downstream views and review basis. The caller schedules critique
        separately, after a complete batch is available.
        """
        if any(
            parent.sha1 in self._interpretation_pending
            for parent in self._interpretation_parents.get(doc.sha1, ())
        ):
            raise DispatchStopped(
                "interpretation context transcription is held"
            )
        producer = stage_planning.batch_stage(doc.kind)
        if "transcription.json" in producer.input_artifacts:
            self._prepare_interpretation_context(doc, self._transcription(doc))
        batch = self._reused_view(
            doc, doc.batch, ClaimBatch.model_validate_json
        )
        changed = False
        if batch is None:
            batch = self._step_batch(doc)
            check_cancelled()
            changed = self._update_batch_view(doc, batch)
            self._record_view(doc, doc.batch)
            from yizkr.pipeline import anomalies

            anomalies.stored(self.rundir, doc, doc.views[doc.batch][1], batch)
        if changed:
            # A new batch invalidates everything derived from the old one.
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
        elif doc.state.status in (PENDING, FAILED, PAUSED):
            # the batch stands, so an earlier failure or pause is over; the
            # gates below re-derive the real status
            doc.state.status = BATCHED
            doc.save()
        return batch

    # -- the state machine ----------------------------------------------------

    def advance(
        self, doc: DocumentRun, *, stop_after_extract: bool = False
    ) -> str:
        """Advance one document as far as the gates allow; returns status.

        ``stop_after_extract`` returns once the batch (entity manifest)
        exists, before reconcile — the serial sequencer needs every
        document's manifest before it can order them. The document's state
        is saved on the way out, however it went; a state that must be
        saved and cannot be fails the document, and only it."""
        if doc.sha1 in self._interpretation_pending:
            doc.state.status = PAUSED
            return PAUSED
        if doc.sha1 in self._interpretation_failed:
            return FAILED
        try:
            status = self._advance(doc, stop_after_extract=stop_after_extract)
        except DispatchStopped as exc:
            # held by the control file: nothing failed, nothing is lost
            doc.state.status = PAUSED
            self.log(f"  {doc.source.name}: {exc}")
            status = PAUSED
        except RunCancelled as exc:
            self._run_held = self._cancellation_reason(exc)
            doc.state.status = PAUSED
            self._save(doc)
            if self.cancellation.is_set():
                raise
            return PAUSED
        except WorkerShutdownTimeout:
            raise
        except Exception as exc:  # per-document isolation: others continue
            self._fail(doc, _why(exc))
            return FAILED
        except BaseException:
            # a paused pass or Ctrl-C goes on through, with the state the
            # document reached kept where it can be
            unsaved = self._save(doc)
            if unsaved is not None:
                self.log(f"  {doc.source.name}: {unsaved}", file=sys.stderr)
            raise
        unsaved = self._save(doc)
        if unsaved is not None:
            self._fail(doc, unsaved, save=False)
            return FAILED
        return status

    def _save(self, doc: DocumentRun) -> str | None:
        """Save the document's state (DocumentRun.save): None when it was
        saved or needed no save, else why it could not be saved."""
        try:
            doc.save()
        except OSError as exc:
            return f"its state could not be saved ({_why(exc)})"
        return None

    def _fail(
        self, doc: DocumentRun, reason: str, *, save: bool = True
    ) -> None:
        """Fail the document, and only it, for `reason`: in its state
        (saved where its directory has its record, DocumentRun.save; a
        save that cannot be made adds why to the reason, and the run goes
        on), in the log, and in a `document-failed` event that names it by
        its file and, once its source has been read, its SHA-1. It is the
        event of every document that fails, whether a stage failed (which
        has its own `failed` event too) or something outside its stages
        did: its source, its state.json, its first record or the save of
        its state, say. `save` is False for a state that could not be saved
        just now."""
        doc.state.status = FAILED
        doc.state.error = reason
        unsaved = self._save(doc) if save else None
        if unsaved is not None:
            reason = f"{reason}; {unsaved}"
        self.rundir.emit(
            DOCUMENT_FAILED,
            file=doc.source.name,
            subject=doc.known_sha1,
            kind=doc.kind,
            reason=reason[:500],
        )
        self.log(f"  {doc.source.name}: FAILED — {reason}", file=sys.stderr)

    def _set_aside(self, doc: DocumentRun, why: str) -> None:
        """Move the document's directory aside whole and start it afresh,
        saying so in the log and in a `set-aside` event that names the
        document, by its file and its SHA-1."""
        kept = _set_aside(doc.dir)
        doc.state = DocumentState(file=str(doc.source), title=doc.source.stem)
        self.rundir.emit(
            SET_ASIDE,
            file=doc.source.name,
            subject=doc.sha1,
            kind=doc.kind,
            reason=why,
            kept=kept.name,
        )
        self.log(
            f"  {doc.source.name}: {why}; the old directory is kept as "
            f"{kept.name}; re-ingesting"
        )

    def _prepare_document(self, doc: DocumentRun) -> None:
        """Prepare a source directory before reading or advancing its views."""
        doc.state.error = None
        content_sha1 = doc.sha1

        # source bytes changed since this document was last processed: the
        # old artifact, claims, and reviews describe DIFFERENT content
        # (ADR-0010 — different bytes are a different artifact), so the
        # document starts over in a fresh directory rather than trusting
        # stale derived state. A directory without derivations.json holds
        # files nothing says were derived from what, and one whose files
        # have no state.json beside them was made for bytes nothing
        # records; either starts over the same way. Each is moved aside
        # whole, reviews included, and nothing in it is deleted
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
        # the state names the file it is kept for as the file is named now,
        # so a source renamed with its bytes unchanged keeps its directory
        # when another source takes its old name or its stem
        doc.state.file = str(doc.source)

        if not doc.state_path.exists():
            doc.save()

    def _prepare_context_containers(self) -> None:
        """Read required containers once before ordinary ingestion workers."""
        parents = {
            parent.sha1: parent
            for group in self._interpretation_parents.values()
            for parent in group
        }
        self._interpretation_pending = set(parents)
        self._interpretation_failed = set()
        for owner in sorted(parents):
            check_cancelled()
            parent = parents[owner]
            stop = self.rundir.control().stopped(
                stage="transcription", kind=parent.kind
            )
            if stop:
                self.rundir.emit(
                    "held",
                    subject=owner,
                    kind=parent.kind,
                    stage="transcription",
                    reason=stop,
                )
                continue
            try:
                self._prepare_document(parent)
                reading = self._transcription(parent)
                parent.state.status = "transcribed"
                error = self._save(parent)
                if error is not None:
                    raise OSError(error)
            except DispatchStopped:
                continue
            except RunCancelled:
                self._interpretation_readings.pop(owner, None)
                if self.cancellation.is_set():
                    raise
                continue
            except WorkerShutdownTimeout:
                raise
            except Exception as exc:
                self._fail(parent, _why(exc))
                self._interpretation_failed.add(owner)
            else:
                self._interpretation_readings[owner] = reading
            self._interpretation_pending.discard(owner)

    def _advance(
        self, doc: DocumentRun, *, stop_after_extract: bool = False
    ) -> str:
        check_cancelled()
        self._prepare_document(doc)
        content_sha1 = doc.sha1
        # content-identity dedup (ADR-0010): claim this file's bytes; if an
        # earlier document in this run — or one already ingested to
        # completion in a PRIOR run (see run()'s pre-seed) — already claimed
        # them, this is the same artifact and needs no transcribe/extract/
        # register/write. Runs every pass, so the canonical keeps its claim
        # on re-runs and the decision is stable.
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
        # a new directory has its state, which names the bytes its views
        # are made for, before any view is made (DocumentRun.unrecorded)
        if not doc.state_path.exists():
            doc.save()

        if doc.kind == "presentation":
            self._record_container_inventory(doc)
            # media other than images keep their unsupported outcome in the
            # inventory and need no attention; the run log counts them
            skipped = self._presentation(doc).skipped_media
            for suffix, count in sorted(skipped.items()):
                self.log(
                    f"  {doc.source.name}: {count} embedded {suffix} media "
                    "not read (images only)",
                    file=sys.stderr,
                )

        # the views before the gates are brought up to date once in each
        # run
        batch = self._derived.get(doc.sha1)
        if batch is None:
            batch = self._derive(doc)
            if doc.critique in doc.views:
                self._critique(doc, batch)
            self._derived[doc.sha1] = batch
        elif doc.state.status in (PENDING, FAILED, PAUSED):
            doc.state.status = BATCHED
            doc.save()

        # serial mode extracts every document first (so the sequencer has all
        # the manifests), then processes them in order — stop here on the
        # extract-only pass, before any reconcile.
        if stop_after_extract:
            return doc.state.status

        check_cancelled()
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
            stop = self.rundir.control().stopped(stage="write", kind=doc.kind)
            if stop:
                raise DispatchStopped(f"write not dispatched: {stop}")
            try:
                with self._stage_cancellation("write", doc.kind):
                    # Confirm this batch's identity map before registration
                    # or any other graph effect.
                    approved = self._approved(doc, resolved)
                    self._mapped(doc, approved, instance=True)
                    # Register the artifact and its bound inner sources just
                    # before writing the claims that cite them (ADR-0025).
                    doc.state.artifact_qid = self._step_register(doc)
                    self.log(f"  registered as {doc.state.artifact_qid}")
                    summary = self._step_write(doc, resolved, approved)
                    check_cancelled()
                    atomic_write_text(
                        doc.write_result, json.dumps(summary, indent=2)
                    )
                    derivations.record(doc.write_result)
                    doc.state.detail = (doc.state.detail or {}) | {
                        "write": summary
                    }
            except RunCancelled as exc:
                self.rundir.emit(
                    "cancelled",
                    subject=doc.sha1,
                    kind=doc.kind,
                    stage="write",
                    reason=self._cancellation_reason(exc),
                )
                raise
        doc.state.status = WRITTEN
        return doc.state.status

    def _prior_written_hashes(
        self, docs: Sequence[DocumentRun] = ()
    ) -> dict[str, str]:
        """content SHA-1 -> filename of a document already ingested to
        completion in a prior run, an excerpt among them (ADR-0010): the
        excerpts' directories, in EXCERPTS_DIR, are read too, so a file of
        the folder with an excerpt's bytes is that excerpt's duplicate.

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
        bytes, which is set aside when that document is advanced, nor one
        whose state.json cannot be read.
        """
        seen: dict[str, str] = {}
        if not self.state_dir.exists():
            return seen
        current: dict[Path, set[str]] = {}
        for doc in docs:
            current.setdefault(doc.dir, set()).add(doc.sha1)
        subs = sorted(self.state_dir.iterdir())
        with contextlib.suppress(OSError):
            subs += sorted((self.state_dir / EXCERPTS_DIR).iterdir())
        for sub in subs:
            if sub.name.startswith("."):  # set aside
                continue
            state_file = sub / "state.json"
            try:
                if not state_file.exists():
                    continue
                state = DocumentState.load(state_file)
            except (
                OSError,
                TypeError,
                ValueError,
                RecursionError,
            ):  # unreadable or foreign
                continue
            if (
                sub in current and state.content_sha1 not in current[sub]
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
        with self._cancellation_scope():
            return self._run()

    def _run(self) -> int:
        from yizkr.pipeline.llm import PassPaused
        from yizkr.pipeline.transcribe import preflight_vision_transcription

        # fail closed: extract-all work never starts without a
        # never-ingest list, so a missing list cannot pass as an empty one
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        found = self.documents()
        if not found:
            self.log(f"no supported documents in {self.folder}")
            return 0
        # a document whose source or state.json cannot be read is not
        # planned or dispatched: it fails alone once the run has started,
        # and the run goes on with the others
        unread = [doc for doc in found if doc.unreadable is not None]
        docs = [doc for doc in found if doc.unreadable is None]
        # Only stages that can call a model need an engine configuration.
        roles = {
            stage.role
            for doc in docs
            for stage in stage_planning.stages_for(doc.kind)
            if stage.process != "deterministic"
        }
        self._refuse_model_aliases(
            tuple(role for role in MODEL_ROLES if role in roles)
        )
        # once for the whole run, before any document: a PDF needs the
        # renderer the vision engine reads it through and the page count
        # the transcription check takes, whether its transcription comes
        # from the engine or from the cache, or is a view the run reuses
        # and checks again
        preflight_vision_transcription(
            doc.source for doc in docs if doc.kind == PDF
        )
        # seed with content already fully ingested in prior runs, so a
        # renamed copy skips the pipeline (cross-run dedup, ADR-0010)
        self._seen_hashes = self._prior_written_hashes(docs)
        self._derived = {}
        self._views = {}
        self._excerpts = {}
        self._run_documents = docs
        self._identity = None
        corpus = {doc.sha1 for doc in docs}
        self._corpus_sha1s = corpus
        # each run is an invocation of its own in the run directory
        self.rundir = RunDir(self.state_dir)
        self._start_run(docs)
        self._control_active = True

        # the control request that stopped the run before a document, if
        # one did, and the run-scope one that held a stage, if one did
        stopped = None
        self._run_held = None
        try:
            for doc in unread:
                self._fail(doc, doc.unreadable)
            self._prepare_context_containers()
            if self.serial:
                docs = self._serial_sequence(docs, corpus)
                found.extend(doc for doc in docs if doc not in found)
            # an excerpt joins the documents once its container's
            # transcription has read it, after the documents of the folder
            for doc in docs:
                check_cancelled()
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
                arrived = [
                    excerpt
                    for excerpt in self._excerpts_of(doc, corpus)
                    if excerpt not in docs
                ]
                self._plan_excerpts(arrived)
                docs.extend(arrived)
                found.extend(arrived)
                # serial (ADR-0019): the next artifact must not reconcile
                # until this one is written, so stop at the first that isn't
                # done. The human clears its gate and re-runs to advance.
                if self.serial and status not in (WRITTEN, DUPLICATE):
                    self.log(
                        f"  (serial) paused until {doc.source.name} is written"
                    )
                    break
        except RunCancelled as exc:
            self._end_invocation(
                found, "stopped", reason=self._cancellation_reason(exc)
            )
            return 2
        except KeyboardInterrupt as exc:
            self.cancellation.cancel(exception=exc)
            self._end_invocation(found, "interrupted")
            raise
        except BaseException as exc:
            if isinstance(exc, PassPaused):
                outcome = "paused"
            elif isinstance(exc, RunSettingsConflict):
                outcome = "refused"
            else:
                outcome = "failed"
            # only the exception's name: its message may quote a document
            self._end_invocation(found, outcome, reason=type(exc).__name__)
            raise
        finally:
            self._close_engines()

        # a run-scope request stopped the run even when it held a stage of
        # the last document, with no document left to dispatch after it
        stopped = stopped or self._run_held
        # the invocation ends before the statuses are printed, so that Ctrl-C
        # or a closed pipe while they print leaves it ended
        self._end_invocation(
            found, "stopped" if stopped else "finished", reason=stopped
        )
        self.log("")
        for doc in [*unread, *docs]:
            self.log(f"{doc.state.status:32} {doc.source.name}")
        if any(doc.state.status == FAILED for doc in found):
            return 1
        if any(
            doc.state.status.startswith("awaiting")
            or doc.state.status == PAUSED
            for doc in found
        ):
            return 2
        return 0

    def _end_invocation(
        self,
        docs: Sequence[DocumentRun],
        outcome: str,
        *,
        reason: str | None = None,
    ) -> None:
        """End the invocation, however it ended: its outcome (rundir.
        OUTCOMES) and reason in the run manifest, and then the same outcome
        and reason, with the count of the documents' statuses, in the event
        that closes its events: `run-interrupted` for Ctrl-C, `run-end`
        otherwise. So a reader of the events alone tells an invocation that
        ended, a paused or refused one included, from one still running;
        only a killed one ends with neither."""
        statuses = dict(Counter(doc.state.status for doc in docs))
        self.rundir.end(outcome, reason=reason)
        self.rundir.emit(
            RUN_INTERRUPTED if outcome == "interrupted" else RUN_END,
            outcome=outcome,
            reason=reason,
            statuses=statuses,
        )

    def _serial_sequence(
        self, docs: list[DocumentRun], corpus: Collection[str] = ()
    ) -> list[DocumentRun]:
        """Extract every document, then order them by greedy entity overlap.

        Extraction must run for all documents up front (the sequencer reads
        their manifests), the excerpts their containers' transcriptions find
        (`_excerpts_of`, `corpus` the folder's SHA-1s) after them; the
        actual reconcile/review/write then proceeds in the returned order,
        hubs first (ADR-0019)."""
        from yizkr.pipeline.sequencing import sequence_documents

        # Settle content-dedup (ADR-0010) BEFORE any parallel extraction, so
        # concurrent _advance calls only ever READ the seen-hashes map (the
        # first document in discovery order with a given hash is canonical) —
        # never two identical files both extracted.
        for doc in docs:
            try:
                sha1 = doc.sha1
            except OSError:
                continue
            self._seen_hashes.setdefault(sha1, doc.source.name)

        self._extract_all(docs)
        excerpts = list(
            {
                id(excerpt): excerpt
                for doc in docs
                for excerpt in self._excerpts_of(doc, corpus)
            }.values()
        )
        self._plan_excerpts(excerpts)
        self._extract_all(excerpts)
        docs = [*docs, *excerpts]
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
        Nothing here asks anything of the wiki: each document is registered
        and written after it, one at a time."""

        def extract_one(doc: DocumentRun) -> None:
            check_cancelled()
            self.log(f"{doc.source.name} [{doc.state.status}]")
            self.advance(doc, stop_after_extract=True)

        from yizkr.pipeline.workers import map_workers

        map_workers(extract_one, docs, concurrency=self.extract_concurrency)

    def _entity_keys(self, doc: DocumentRun) -> set[str]:
        """Name keys of a document's extractable entities, for the overlap
        sequencer — the same whole name forms reconcile blocks on given
        the batch: a person's appellations (ADR-0018) key nothing, while a
        place, an organisation or a family keys on every name."""
        from yizkr.pipeline.names import appellations_of
        from yizkr.pipeline.reconcile import entity_name_keys

        if not doc.batch.exists():
            return set()
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        appellations = appellations_of(batch.claims)
        keys: set[str] = set()
        for entity in batch.entities:
            if entity.extract:
                keys |= entity_name_keys(
                    entity, appellations.get(entity.id, ())
                )
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


def _read_by_page(transcription) -> str:
    """How a container read page by page was read, as counts: its pages
    by method, and the images read on their own."""
    methods = Counter(
        page.provenance.method for page in transcription.pages or ()
    )
    images = len(
        {sha1 for page in transcription.pages or () for sha1 in page.images}
    )
    return "read page by page: " + ", ".join(
        [f"{count} {method}" for method, count in sorted(methods.items())]
        + [f"{images} image(s) read on their own"]
    )


def _findings(text: str) -> list:
    """A critique view's findings; ValueError when it holds no list."""
    findings = json.loads(text)
    if not isinstance(findings, list):
        raise ValueError("a critique view holds a list of findings")
    return findings


def _unreadable_critique(findings: list) -> str | None:
    """Why a critique view's entries are not each a finding or one that
    failed its checks (critique.entries), such as findings in an earlier
    format; None when they are."""
    from yizkr.pipeline.critique import entries

    try:
        entries(findings)
    except ValueError as exc:
        return str(exc)
    return None


def _csv_has_rows(path: Path) -> bool:
    with open(path, newline="") as handle:
        next(handle, None)  # header
        return any(line.strip() for line in handle)
