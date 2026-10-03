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
A PDF on the vision route is read page by page when any of its pages has
a usable text layer (yizkr.pipeline.textlayer): those pages from their
text layer, without a model call, its other pages by the vision engine,
and each image placed within a text-layer page by the vision engine on
its own, as a witness of its own (ADR-0008). The images' bytes and their
transcriptions are kept in the document's directory, recorded in
`derived-media.json`, beside the PDF's own transcription. A PDF none of
whose pages has one is read whole by the vision engine. The PDF renderer
the vision engine and the transcription check need is checked once,
before any document.

Each image a PDF's transcription reads on its own, with a reading that
passes the check, is then a document of its own, an excerpt
(ExcerptRun): its identity is the SHA-1 of its bytes, its directory is in
the state directory's EXCERPTS_DIR, its transcription is the reading the
PDF kept, and its claims cite the image. An image several PDFs place is
one excerpt; one whose bytes a document of the folder has, or that the
never-ingest list names, is none. The run takes each excerpt after the
folder's documents, as its PDF's transcription finds it.

Two steps use the wiki: resolve, which matches labels to the graph's
items, and the write, which registers the document first
(yizkr.pipeline.registrar): its artifact item, a deck's slides as page
fragments, and a deck's derived media or a PDF's images, each found or
made by the SHA-1 of its bytes, just before the claims that cite them
are written; an excerpt's write registers each PDF that places it, with
its images. Everything else reads nothing from the wiki and writes
nothing to it (ADR-0025 §2): the stages before the gates; a deck's
derived media and a PDF's images, which are recorded in its directory
(`derived-media.json`) for the write to register; and the reconcile
step, which has no roster to match a document's entities against, so it
proposes nothing and the first gate has nothing to review.

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
import json
import os
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.config import Config
from yizkr.core.corpus import (
    MISSING_LIST,
    ExclusionConfigError,
    ExclusionList,
    is_excluded,
)
from yizkr.core.wiki import WikiClient
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
from yizkr.pipeline.stages import (
    atomic_write_text,
    content_hash,
    sha1_file,
    sha256_file,
    sha256_text,
)

GEDCOM_SUFFIXES = {".ged"}
TEXT_SUFFIXES = {".txt", ".md"}
MEDIA_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
PRESENTATION_SUFFIXES = {".pptx"}
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

# the kind of an image a PDF places within one of its text-layer pages,
# taken as a document of its own (ExcerptRun), and the directory of the
# state directory that holds such documents' directories, each named for
# its image's SHA-1
EXCERPT = "excerpt"
EXCERPTS_DIR = "excerpts"

# the stages each kind of document runs before the gates, in order
_EXTRACTED = ("transcription", "extraction", "critique")
DOCUMENT_STAGES = {
    "gedcom": ("gedcom",),
    "presentation": _EXTRACTED,
    "text": _EXTRACTED,
    "media": _EXTRACTED,
    EXCERPT: _EXTRACTED,
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
# the fields of a transcription that say what made it (the route and the
# engine build, and each page's provenance), and the transcription
# check's flag, which says what the check made of it: left out of its
# content hash. A page's place in the text, the images it names and its
# record's fields (its printed page number, status, languages and notes)
# are content, as its text and its document class are (ADR-0037)
TRANSCRIPTION_VOLATILE = ("engine", "pages.provenance", "flag")
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
    else is read by the vision engine. The vision route reads a PDF page
    by page when any of its pages has a usable text layer, each page by
    its own method, chosen as deterministically
    (yizkr.pipeline.textlayer); a PDF without one, and an image, it reads
    whole."""
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
        except (OSError, ValueError, RecursionError):
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
    """An image a PDF places within one of its text-layer pages, taken as
    a document of its own: a witness apart from the PDF, whose claims cite
    the image alone (ADR-0008).

    Its identity is the SHA-1 of its bytes, and so is its directory's
    name, in the state directory's EXCERPTS_DIR: an image that several
    PDFs place is one document, whichever of them places it first.
    `containers` are the PDFs that place it, each keeping the image's
    bytes and its reading in its own directory (presentation.
    derived_transcription); the first one's copy of the bytes is its
    source. Its transcription is the reading the PDF's transcription made
    of it (Orchestrator._transcribe_excerpt), so no model is asked again.
    Its title names its first place: "<the PDF's title> — page 6, image
    1"."""

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
        """Add a PDF that places the image too."""
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
        # reconciles — so a later artifact's resolve step finds, by exact
        # label, the items the earlier ones wrote.
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
        # image SHA-1 -> the excerpt found for it in this run
        # (_excerpts_of): one document however many PDFs place the image
        self._excerpts: dict[str, ExcerptRun] = {}
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
        """The folder's documents, in name order, each with a directory of
        its own (directory_names) and its source read for its SHA-1. A
        document whose source or state.json cannot be read is `unreadable`
        (DocumentRun), and so is one whose bytes the never-ingest list's
        hashes cannot be checked against: it is not ingested."""
        files = []
        unread: dict[Path, str] = {}
        for path in sorted(self.folder.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
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
        return docs

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
        except (OSError, ValueError, RecursionError):
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
        from yizkr.pipeline import anomalies

        anomalies.reused(self.rundir, doc, stage, loaded)
        return loaded

    # -- pipeline steps (override points for tests) ---------------------------

    def _step_register(self, doc: DocumentRun) -> str:
        """Register the document as its write begins (ADR-0025 §2): its
        artifact item, found or made by the SHA-1 of the source's bytes
        (ADR-0010), and for a deck its slides as page fragments and the
        images it embeds, and for a PDF read page by page the images placed
        within its pages, from the record of them in its directory, each
        by the SHA-1 of its own bytes (ADR-0008). A source that no longer
        has the bytes the run read, or that is on the never-ingest list,
        is refused before anything is registered or uploaded, and an
        embedded image the list names is left out
        (yizkr.pipeline.registrar). An excerpt is registered as the
        derived medium of each PDF that places it, each PDF registered
        first. Returns the artifact's QID.

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
                "PDF that places it records it: nothing of it was registered"
            )
        return found

    def _register(self, registrar: Any, doc: DocumentRun) -> tuple[str, dict]:
        """(the document's QID, its derived media's QIDs by SHA-1), as
        `_step_register` registers a document that is not an excerpt."""
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
                f"ingested by `yizkr ingest run` from {doc.source.name}"
            ),
            pages=pages,
            sha1=doc.sha1,
        )
        record = (
            self._derived_media(doc)
            if doc.kind == "presentation"
            else self._placed_images(doc)
        )
        derived: dict[str, str] = {}
        if record is not None:
            derived = registrar.register_derived_media(record, doc.dir)
            left_out = len(record.media) - len(derived)
            self.log(
                f"  derived media: {len(derived)} registered"
                + (
                    f", {left_out} left out (never-ingest list)"
                    if left_out
                    else ""
                )
            )
        return result.artifact_qid, derived

    def _placed_images(self, doc: DocumentRun):
        """The record of the images a PDF read page by page places within
        its pages, kept in its directory by its transcription
        (_keep_reading), when it is there whole and made for the PDF's
        bytes as they are now; None for any other document, and for a PDF
        whose record is not there. A reused transcription that marks
        images has its record whole, or is made again
        (_transcription_failure)."""
        from yizkr.pipeline.presentation import read_derived_media

        if doc.kind != "media":
            return None
        record = read_derived_media(doc.dir)
        if (
            record is None
            or record.container != "pdf"
            or record.source_sha1 != doc.sha1
        ):
            return None
        return record

    def _presentation(self, doc: DocumentRun):
        """The deck unpacked, kept by the SHA-1 of its bytes: a deck is
        unpacked once for each content it has had, so one whose bytes
        change between two runs of the orchestrator is unpacked again."""
        from yizkr.pipeline.presentation import unpack_pptx

        if not hasattr(self, "_presentation_cache"):
            self._presentation_cache: dict[str, object] = {}
        if doc.sha1 not in self._presentation_cache:
            self._presentation_cache[doc.sha1] = unpack_pptx(doc.source)
        return self._presentation_cache[doc.sha1]

    def _derived_media_current(self, doc: DocumentRun) -> bool:
        """Does the document's directory hold the record of its deck's
        derived media, whole and with the file of every medium it names
        (presentation.read_derived_media), made for the deck's bytes as
        they are now? A record made for other bytes, as in a directory
        two sources have shared, is made again, and so is a missing or
        damaged one."""
        from yizkr.pipeline.presentation import read_derived_media

        record = read_derived_media(doc.dir)
        return record is not None and record.source_sha1 == doc.sha1

    def _derived_media(self, doc: DocumentRun):
        """The record of the deck's derived media: the one in its
        directory while it is current (_derived_media_current), and
        otherwise one made again."""
        from yizkr.pipeline.presentation import read_derived_media

        record = read_derived_media(doc.dir)
        if record is None or record.source_sha1 != doc.sha1:
            record = self._step_derive_media(doc)
        return record

    def _step_derive_media(self, doc: DocumentRun):
        """Record the images the deck embeds in the document's directory
        (presentation.record_derived_media): each by the SHA-1 of its
        bytes, with the deck's own SHA-1 and title and the slides it is
        on, and its bytes kept beside the record. Nothing is read from the
        wiki or written to it (ADR-0025 §2): the write registers the
        images and uploads their bytes (_step_register). Returns the
        record."""
        from yizkr.pipeline.presentation import record_derived_media

        content = self._presentation(doc)
        for suffix, count in content.skipped_media.items():
            self.log(
                f"  {doc.source.name}: {count} embedded {suffix} media "
                f"skipped (image media only in W1)",
                file=sys.stderr,
            )
        return record_derived_media(
            content,
            source_sha1=doc.sha1,
            title=doc.state.title,
            directory=doc.dir,
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
                doc.source.read_text(), artifact=ArtifactRef(sha1=doc.sha1)
            ),
        )
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
        return result.batch

    def _transcription(self, doc: DocumentRun):
        """The document's transcription view, reused when it stands
        (`_reused_view`) and made otherwise, then recorded with its
        canonical content hash; once per run. A vision or text-file
        transcription, the routes whose transcribers check their output,
        is checked again when it is reused: the check may have become
        stricter since the view was made, and one it now fails is made
        again (`_transcription_failure`)."""
        from yizkr.pipeline.transcribe import Transcription

        memo = (doc.sha1, doc.transcription.name)
        if memo in self._views:
            return self._views[memo]
        checked = self._route(doc) in ("vision", "text-file", EXCERPT)
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
            from yizkr.pipeline import anomalies

            anomalies.stored(self.rundir, doc, "transcription", transcription)
        self._views[memo] = transcription
        return transcription

    def _route(self, doc: DocumentRun) -> str:
        """The route the document's transcription takes: an excerpt's is
        the reading its PDF made of it (EXCERPT), unless a curator's
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
        file's, which has none, by its text. A PDF read page by page is
        checked where the vision engine read it: each page it read, with
        the page's one-page record (`Transcription.page_record`), as its
        reading was checked when the page was made, so that a page the
        record calls blank needs no text; and the transcription of each
        image placed within a page that the document's directory keeps
        (`_image_failure`). Its text-layer pages are the document's own
        text and are never checked. It is made again, too, when what it
        marks is not kept whole beside it (`_reading_failure`). A
        transcription that passes carries the flag of the check made now,
        or none (the first vision page's, for a PDF read page by page),
        since a check changed since it was made may flag it otherwise; the
        flag is no part of its content. It reads files only, and writes
        none."""
        from yizkr.pipeline.textlayer import VISION
        from yizkr.pipeline.transcribe import PDF_PAGES, check_vision_page
        from yizkr.pipeline.transcription_check import (
            TranscriptionCheckFailed,
            check_transcription,
            is_pdf,
            media_page_count,
        )

        if not transcription.engine.startswith(f"{PDF_PAGES}:"):
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
        for page in transcription.pages:
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
        """Why a PDF's transcription read page by page is to be made again
        although it passes the check, or None: an image it marks that the
        never-ingest list names now, since such an image is never kept or
        marked; a record of the images it marks that is missing, damaged,
        made for other bytes, or names other images, or an image's bytes
        that are not kept whole (presentation.read_derived_media), since
        the write registers them from it; or an image it marks that has no
        transcription of its own, since its reading failed or was lost, so
        that the image is read again. Its pages the vision engine read are
        read again through the LLM cache, which answers the calls it
        answered before."""
        from yizkr.pipeline.presentation import (
            derived_transcription,
            read_derived_media,
        )

        images = list(
            dict.fromkeys(
                sha1 for page in transcription.pages for sha1 in page.images
            )
        )
        for sha1 in images:
            if self._excluded_image(sha1):
                return (
                    f"the never-ingest list names image {sha1[:12]}, which "
                    "it marks"
                )
        if not images:
            return None
        record = read_derived_media(doc.dir)
        if (
            record is None
            or record.container != "pdf"
            or record.source_sha1 != doc.sha1
            or set(record.media) != set(images)
        ):
            return (
                "the record of the images it marks is missing, damaged or "
                "names others, or an image's bytes are not kept whole"
            )
        for sha1 in images:
            if not derived_transcription(doc.dir, sha1).exists():
                return (
                    f"image {sha1[:12]} has no transcription of its own, so "
                    "it is read again"
                )
        return None

    def _image_failure(self, doc: DocumentRun, sha1: str) -> str | None:
        """Why the check fails the transcription the document's directory
        keeps of the image with this SHA-1, or None when it passes, or
        when the directory keeps none: an image's reading that failed has
        none, and nothing downstream reads one."""
        from yizkr.pipeline.presentation import derived_transcription
        from yizkr.pipeline.transcribe import Transcription
        from yizkr.pipeline.transcription_check import (
            TranscriptionCheckFailed,
            check_transcription,
        )

        path = derived_transcription(doc.dir, sha1)
        image = f"image {sha1[:12]}"
        try:
            kept = Transcription.model_validate_json(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as exc:
            return f"the transcription of {image} cannot be read ({_why(exc)})"
        if kept.artifact.sha1 != sha1:
            return f"the transcription of {image} names another image"
        check = check_transcription(
            kept.text, pages=1, record=kept.record(), expect_record=True
        )
        if check.passed:
            return None
        return f"{TranscriptionCheckFailed(check)} ({image})"

    def _step_transcribe(self, doc: DocumentRun):
        """Transcribe the document by its route, record the language it
        was found to be in, and write the transcription view. What a PDF
        read page by page made beside its transcription, its images and
        their transcriptions, is kept first (`_keep_reading`), and what
        of it needs attention is the stage's attention."""
        from yizkr.pipeline.transcribe import PDF_PAGES

        method = self._route(doc)
        transcription, resolution = self._run_stage(
            doc,
            "transcription",
            lambda: self._transcribe(doc, method),
            attention=_reading_attention,
        )
        reading = transcription.reading
        if reading is not None:
            try:
                self._keep_reading(doc, transcription)
            finally:
                reading.close()
                transcription.forget_reading()
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
        if transcription.engine.startswith(f"{PDF_PAGES}:") and changed:
            self.log(f"  {doc.source.name}: {_read_by_page(transcription)}")
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
            EXCERPT: self._transcribe_excerpt,
        }[method]
        return transcribe(doc)

    def _transcribe_excerpt(self, doc: DocumentRun):
        """(transcription, language resolution) of an excerpt: the reading
        of its image that a PDF placing it keeps (presentation.
        derived_transcription), the first of them whose reading names the
        image and passes the check (`_image_failure`). No model is asked:
        the PDF's transcription read the image, through the LLM cache. Its
        class and pages are the reading's; its flag is the one the check
        made now raises on the reading, as a reused view's is, not the one
        stored with it; and its language comes from the reading's record
        (language.record_language), as a vision transcription's does;
        from its text (language.resolve_language) only for a reading with
        no record. RuntimeError when none keeps one."""
        from yizkr.pipeline.language import record_language, resolve_language
        from yizkr.pipeline.presentation import derived_transcription
        from yizkr.pipeline.transcribe import Transcription
        from yizkr.pipeline.transcription_check import check_transcription

        for container in doc.containers:
            path = derived_transcription(container.dir, doc.sha1)
            if not path.is_file() or self._image_failure(container, doc.sha1):
                continue
            kept = Transcription.model_validate_json(path.read_text())
            record = kept.record()
            resolution = (
                record_language(
                    record.pages,
                    self.language,
                    fallback=self.fallback_language,
                )
                if record is not None
                else resolve_language(
                    kept.text, self.language, fallback=self.fallback_language
                )
            )
            check = check_transcription(
                kept.text, pages=1, record=record, expect_record=True
            )
            flag = check.signature if check.flagged else None
            return kept.model_copy(
                update={"language": resolution.code, "flag": flag}
            ), resolution
        raise RuntimeError(
            f"no PDF that places image {doc.sha1[:12]} keeps a reading of it "
            "that passes the transcription check"
        )

    def _transcribe_vision(self, doc: DocumentRun):
        """(transcription, language resolution) by the vision route: a PDF
        page by page when any of its pages has a usable text layer
        (transcribe.PdfPageTranscriber), and anything else whole. Nothing
        is written: what the page-by-page reading made beside the
        transcription rides on it (`Transcription.reading`) for
        `_step_transcribe` to keep."""
        from yizkr.pipeline.transcribe import (
            LlmVisionTranscriber,
            PdfPageTranscriber,
        )

        if doc.source.suffix.lower() == ".pdf":
            paged = PdfPageTranscriber(
                functools.partial(self._llm, "transcribe", doc),
                language=self.language,
                fallback_language=self.fallback_language,
                excluded=self._excluded_image,
            )
            transcription = paged.transcribe(doc.source)
            if transcription is not None:
                return transcription, paged.last_resolution
        # the transcriber checks what the engine or the cache answers, and
        # rejects an answer that fails the check (ADR-0032)
        transcriber = LlmVisionTranscriber(
            self._llm("transcribe", doc),
            language=self.language,
            fallback_language=self.fallback_language,
        )
        transcription = transcriber.transcribe(doc.source)
        return transcription, transcriber.last_resolution

    def _excluded_image(self, sha1: str) -> bool:
        """Does the never-ingest list name an image a PDF places within a
        page, by the SHA-1 of its bytes? Such an image is never read or
        kept."""
        return self.exclusions is not None and is_excluded(
            self.exclusions, sha1=sha1
        )

    def _keep_reading(self, doc: DocumentRun, transcription) -> None:
        """Keep what a PDF read page by page made beside its
        transcription, in the document's directory: the images placed
        within its pages, recorded as its derived media
        (presentation.keep_derived_media), and each image's own
        transcription beside its bytes (presentation.
        derived_transcription). A kept transcription of an image that the
        check fails now is moved aside first, never deleted, whether or
        not a new one takes its place. An image the never-ingest list names
        now is not in the record, so nothing registers or extracts what an
        earlier reading kept of it."""
        from yizkr.pipeline.presentation import (
            DERIVED_MEDIA_DIR,
            DerivedMediaRecord,
            DerivedMedium,
            derived_transcription,
            keep_derived_media,
        )

        reading = transcription.reading
        record = DerivedMediaRecord(
            source_sha1=transcription.artifact.sha1,
            container="pdf",
            title=doc.state.title,
            media={
                image.sha1: DerivedMedium(
                    names=image.names,
                    slides=image.pages,
                    file=f"{DERIVED_MEDIA_DIR}/{image.sha1}{image.suffix}",
                )
                for image in reading.images
            },
        )
        keep_derived_media(
            record,
            {image.sha1: image.path for image in reading.images},
            doc.dir,
        )
        for image in reading.images:
            path = derived_transcription(doc.dir, image.sha1)
            if path.exists() and self._image_failure(doc, image.sha1):
                _supersede(path)
            if image.transcription is not None:
                _write_view(
                    path, image.transcription.model_dump_json(indent=2)
                )

    def _transcribe_text_file(self, doc: DocumentRun):
        from yizkr.pipeline.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(
            doc.source,
            language=self.language,
            fallback_language=self.fallback_language,
        )
        transcription = transcriber.transcribe(doc.source)
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
            artifact=ArtifactRef(sha1=doc.sha1),
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
            artifact=ArtifactRef(sha1=doc.sha1),
            text=text,
            language=resolution.code,
            engine="correction",
        )
        return transcription, resolution

    def _step_extract(self, doc: DocumentRun, transcription) -> ClaimBatch:
        from yizkr.pipeline.chunking import ChunkedClaimExtractor

        # always entity-first (ADR-0007), even for documents that fit a
        # single call: the batch carries the entity manifest, which the
        # writer takes each entity's label, aliases and kind from, the
        # critic and serial mode's sequencer read, and identity matching
        # blocks on — every entity must be in it, however short the
        # document. Chunking a small document costs a few extra calls and
        # is never incorrect (ADR-0006 §7).
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
                transcription, ArtifactRef(sha1=doc.sha1)
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
        from yizkr.pipeline.resolve import resolvable_labels, resolve_batch

        if doc.equivalences_reviewed.exists():
            approved = read_approved_equivalences(doc.equivalences_reviewed)
            batch = apply_equivalences(
                batch,
                approved,
                read_reviewed_aliases(doc.equivalences_reviewed),
            )
            self.log(f"  applied {len(approved)} approved equivalences")
        return resolve_batch(batch, resolvable_labels(self.client, batch))

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
        if (
            self._reused_view(
                doc, doc.critique, _findings, recheck=_unreadable_critique
            )
            is not None
        ):
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
        """The extraction critic's findings on the batch. A critique the
        engine gave up on at its output limit has none, and needs
        attention, so that neither this run nor one that reuses the view
        takes it for a sound extraction's."""
        from yizkr.pipeline.critique import run_critique

        attention: list[str] = []
        return self._run_stage(
            doc,
            "critique",
            lambda: run_critique(
                self._llm("critique", doc), batch, attention=attention
            ),
            attention=lambda _findings: attention,
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
        resolved."""
        if not doc.critique.exists() or not doc.batch.exists():
            return {}
        from yizkr.pipeline.critique import entries, sheet_notes

        extracted = ClaimBatch.model_validate_json(doc.batch.read_text())
        return sheet_notes(
            entries(json.loads(doc.critique.read_text())), extracted, rows
        )

    def _step_write(self, doc: DocumentRun, batch: ClaimBatch) -> dict:
        from yizkr.pipeline.review import apply_review, is_bulk_accepted
        from yizkr.pipeline.writer import BatchWriter

        approved, warnings = apply_review(batch, doc.review_reviewed)
        for warning in warnings:
            self.log(f"  {doc.source.name}: {warning}", file=sys.stderr)
        doc.approved.write_text(approved.model_dump_json(indent=2))
        # the artifact the write registered (_step_register), which the
        # claims cite by the SHA-1 of its bytes
        registered = doc.state.artifact_qid
        result = BatchWriter(
            self.client,
            artifact_qids={doc.sha1: registered} if registered else None,
        ).write_batch(
            approved, bulk_accepted=is_bulk_accepted(doc.review_reviewed)
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
            "approved_claims": len(approved.claims),
        }

    # -- excerpts -----------------------------------------------------------

    def _excerpts_of(
        self, doc: DocumentRun, corpus: Collection[str]
    ) -> list[ExcerptRun]:
        """The excerpts of a PDF read page by page (ExcerptRun), by their
        SHA-1s, from what its directory keeps: each image the record of
        its images names (_placed_images) that has a reading of its own
        which passes the check (`_image_failure`), and which shows a page
        of a document (`_image_shows_a_page`): an image whose record calls
        every page blank (a photograph with no writing) or not a document
        page (a colour chart) holds nothing to extract. An illegible one
        is an excerpt, so that a reader sees it was there. An image the
        never-ingest list names is left out, asked again here, and so is
        one whose bytes a document of the corpus has (`corpus`, their
        SHA-1s): that document is the witness, and its bytes are not
        extracted again (ADR-0010). Each is found or made by its SHA-1
        among the run's excerpts, so an image that several PDFs place is
        one excerpt, with each of them among its containers. Reads files
        only, and writes none."""
        from yizkr.pipeline.presentation import derived_transcription

        record = self._placed_images(doc)
        if record is None:
            return []
        found = []
        for sha1, medium in sorted(record.media.items()):
            if (
                sha1 in corpus
                or self._excluded_image(sha1)
                or not derived_transcription(doc.dir, sha1).is_file()
                or self._image_failure(doc, sha1) is not None
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
        from yizkr.pipeline.presentation import derived_transcription
        from yizkr.pipeline.transcribe import Transcription
        from yizkr.pipeline.transcription_record import (
            BLANK,
            NOT_A_DOCUMENT_PAGE,
        )

        kept = Transcription.model_validate_json(
            derived_transcription(doc.dir, sha1).read_text()
        )
        record = kept.record()
        nothing = {BLANK, NOT_A_DOCUMENT_PAGE}
        return record is None or not set(record.statuses()) <= nothing

    def _plan_excerpts(self, excerpts: Sequence[ExcerptRun]) -> None:
        """Add the excerpts the run plan does not hold yet to it, as the
        documents they are, each with its stages (docs/RUN_MANIFEST.md):
        they are found as their PDFs' transcriptions are made."""
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
            from yizkr.pipeline import anomalies

            anomalies.stored(self.rundir, doc, doc.views[doc.batch][1], batch)
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
        elif doc.state.status in (PENDING, FAILED, PAUSED):
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
        document's manifest before it can order them. The document's state
        is saved on the way out, however it went; a state that must be
        saved and cannot be fails the document, and only it."""
        try:
            status = self._advance(doc, stop_after_extract=stop_after_extract)
        except DispatchStopped as exc:
            # held by the control file: nothing failed, nothing is lost
            doc.state.status = PAUSED
            self.log(f"  {doc.source.name}: {exc}")
            status = PAUSED
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

        # a deck's derived media are recorded in its directory, for the
        # write to register: no wiki access. The record is current while
        # it is there whole (_derived_media_current)
        if doc.kind == "presentation" and not self._derived_media_current(doc):
            record = self._step_derive_media(doc)
            self.log(f"  derived media: {len(record.media)} recorded")

        # the views before the gates are brought up to date once in each
        # run
        batch = self._derived.get(doc.sha1)
        if batch is None:
            batch = self._derive(doc)
            self._derived[doc.sha1] = batch
        elif doc.state.status in (PENDING, FAILED, PAUSED):
            doc.state.status = BATCHED
            doc.save()

        # serial mode extracts every document first (so the sequencer has all
        # the manifests), then processes them in order — stop here on the
        # extract-only pass, before any reconcile.
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
            # registration is part of the write: the document's artifact,
            # and a deck's fragments and derived media, are found or made
            # by the SHA-1 of their bytes just before the claims that cite
            # them are written (ADR-0025 §2)
            doc.state.artifact_qid = self._step_register(doc)
            self.log(f"  registered as {doc.state.artifact_qid}")
            summary = self._step_write(doc, resolved)
            atomic_write_text(doc.write_result, json.dumps(summary, indent=2))
            derivations.record(doc.write_result)
            doc.state.detail = (doc.state.detail or {}) | {"write": summary}
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
        from yizkr.pipeline.llm import PassPaused
        from yizkr.pipeline.transcribe import preflight_vision_transcription

        # fail closed: extract-all work never starts without a
        # never-ingest list, so a missing list cannot pass as an empty one
        if self.exclusions is None:
            raise ExclusionConfigError(MISSING_LIST)
        # only full model ids enter the run manifest and the cache's keys
        self._refuse_model_aliases()
        found = self.documents()
        if not found:
            self.log(f"no supported documents in {self.folder}")
            return 0
        # a document whose source or state.json cannot be read is not
        # planned or dispatched: it fails alone once the run has started,
        # and the run goes on with the others
        unread = [doc for doc in found if doc.unreadable is not None]
        docs = [doc for doc in found if doc.unreadable is None]
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
        self._excerpts = {}
        corpus = {doc.sha1 for doc in docs}
        # each run is an invocation of its own in the run directory
        self.rundir = RunDir(self.state_dir)
        self._start_run(docs)

        # the control request that stopped the run before a document, if
        # one did, and the run-scope one that held a stage, if one did
        stopped = None
        self._run_held = None
        try:
            for doc in unread:
                self._fail(doc, doc.unreadable)
            if self.serial:
                docs = self._serial_sequence(docs, corpus)
                found.extend(doc for doc in docs if doc not in found)
            # an excerpt joins the documents once its PDF's transcription
            # has read it, after the documents of the folder
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
        except KeyboardInterrupt:
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
        their manifests), the excerpts their PDFs' transcriptions find
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


def _read_by_page(transcription) -> str:
    """How a PDF read page by page was read, as counts: its pages by
    method, and the images read on their own."""
    methods = Counter(page.provenance.method for page in transcription.pages)
    images = len(
        {sha1 for page in transcription.pages for sha1 in page.images}
    )
    return "read page by page: " + ", ".join(
        [f"{count} {method}" for method, count in sorted(methods.items())]
        + [f"{images} image(s) read on their own"]
    )


def _reading_attention(result: tuple) -> list[str]:
    """What of a page-by-page reading of a PDF needs attention
    (transcribe.PdfReading.attention), for the transcription stage's
    `done` event: nothing for any other transcription."""
    transcription, _resolution = result
    reading = transcription.reading
    return reading.attention() if reading is not None else []


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
