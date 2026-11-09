"""Source paths, persisted document products and exclusion-aware discovery.

These objects describe current inputs and saved products. They schedule no
work, open no model clients and grant no graph-write authority.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yizkr.core.corpus import (
    DECK_SUFFIXES,
    ExclusionList,
    is_excluded,
)
from yizkr.pipeline import stage_planning
from yizkr.pipeline.rundir import (
    ALERTS,
    CONTROL,
    EVENTS,
    MANIFEST,
)
from yizkr.pipeline.stage_planning import VIEWS
from yizkr.pipeline.stages import (
    atomic_write_text,
    sha1_file,
    sha256_file,
    sha256_text,
)

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
# fields of a claim batch that differ between two identical recomputes
# (when, which commit, which engine build), left out of its content hash
BATCH_VOLATILE = (
    "extraction_run.started_at",
    "extraction_run.software_commit",
    "extraction_run.model_identifier",
    "extraction_run.pipeline_version",
    "extraction_run.provenance",
)
# the fields of a transcription that say what made it (the route and the
# engine build, and each page's provenance), and the transcription
# check's flag, which says what the check made of it: left out of its
# content hash. A page's place in the text, the images it names and its
# record's fields (its printed page number, status, languages and notes)
# are content, as its text and its document class are (ADR-0037)
TRANSCRIPTION_VOLATILE = (
    "engine",
    "provenance",
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
    compares migrated stages' `processing` metadata. The canonical content
    hashes of transcription and batch views distinguish changed products
    from identical recomputations.
    """

    def __init__(
        self,
        path: Path,
        *,
        made_from: Mapping[Path, Sequence[Path]],
    ) -> None:
        self.path = path
        # each derived file -> the files it is made from
        self.made_from = {out: tuple(ins) for out, ins in made_from.items()}

    def begin(self) -> None:
        """Start an empty record for a directory that has none."""
        if not self.path.exists():
            self._save(self._load())

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError, RecursionError):
            data = {}
        for section in ("derived", "content", "processing"):
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
            for section in ("derived", "content", "processing"):
                data[section].pop(output.name, None)
        self._save(data)

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


_RUN_FILES = frozenset(
    {MANIFEST, EVENTS, ALERTS, CONTROL, "status", "corpus", EXCERPTS_DIR}
)


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
    `read_source` has tried: the run fails it alone (the run scheduler)."""

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
        # open-schema harness (ADR-0013)
        self.gaps = self.dir / "gaps.csv"
        self.critique = self.dir / VIEWS["critique"]
        self.interpretation_context = self.dir / "interpretation-context.json"
        # each persisted document view -> (the refresh role
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

        Every document directory has its record from its
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
        names its own file (the run scheduler)."""
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
    """Preserve an invalidated product under the first free numbered name.

    Returns the destination, or None when there was no saved product.
    """
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


def discover_documents(
    folder: Path,
    state_dir: Path,
    *,
    exclusions: ExclusionList,
    log=print,
) -> list[DocumentRun]:
    """The folder's documents, in name order, each with a directory of
    its own (directory_names) and its source read for its SHA-1. A
    document whose source or state.json cannot be read is `unreadable`
    (DocumentRun), and so is one whose bytes the never-ingest list's
    hashes cannot be checked against: it is not ingested. The list is
    expanded by the caller through `core.corpus.follow_decks` so that
    image bytes selected by a container glob are excluded everywhere."""
    files = []
    unread: dict[Path, str] = {}
    paths = [
        path
        for path in sorted(folder.iterdir())
        if path.is_file() and not path.name.startswith(".")
    ]
    for path in paths:
        # the never-ingest list is asked first, before any step
        # reads the document, and whatever the file's type: an excluded
        # file gets no state, no artifact and no transcription
        try:
            excluded = exclusions is not None and is_excluded(exclusions, path)
        except OSError as exc:
            excluded = False
            unread[path] = f"its source cannot be read ({_why(exc)})"
        if excluded:
            log(
                f"excluding {path.name}: on the never-ingest list",
                file=sys.stderr,
            )
        elif path.suffix.lower() not in SUPPORTED_SUFFIXES:
            log(
                f"skipping {path.name}: unsupported file type",
                file=sys.stderr,
            )
        else:
            files.append(path)
    names = directory_names(files, state_dir)
    docs = []
    for path in files:
        doc = DocumentRun(path, state_dir, dirname=names[path])
        if path in unread:
            doc.unreadable = doc.unreadable or unread[path]
        else:
            doc.read_source()
        docs.append(doc)
    return docs


def validate_source_owners(
    docs: Sequence[DocumentRun],
    prepare: Callable[[DocumentRun], Any],
) -> None:
    """Refuse coincident decoded source aliases before admitting documents.

    The supplied deterministic preparation may reuse the execution's current
    containers. Malformed inputs retain their ordinary document failure path.
    """
    from yizkr.pipeline.container_sources import check_owner_aliases

    inventories, originals = [], []
    for doc in docs:
        if doc.unreadable is not None:
            continue
        try:
            prepared = prepare(doc)
        except Exception:
            continue
        if prepared is None:
            originals.append(doc.sha1)
        else:
            inventories.append(prepared[0])
    check_owner_aliases(inventories, original_sources=originals)
