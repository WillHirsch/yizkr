"""Archived transcriptions adopted for a scratch run's calls:
`yizkr scratch import-transcriptions` (ADR-0032 §9).

**The archive** is a state directory that `yizkr ingest run` left: one
folder per source file, holding the document's `state.json` (its source's
`file`, and the SHA-1 of its bytes, `content_sha1`) and its views, the
transcription (`transcription.json`: its `text` and `engine`, and the
artifact it names) among them. The import reads nothing of the archive
but those two files of each document folder and, where the archive is a
run directory, the run's `events.jsonl` in a folder of the archive that
holds document folders, which gives the time each transcription was made.
A folder whose name starts with a dot is one a run set aside, and is
passed over. A folder inside the archive that is a symbolic link is not
descended: what it holds is neither counted nor reported. A file among
those read that is a symbolic link, or whose path resolves outside the
archive, is not read: its transcription is skipped as unreadable, and
such an events file gives no time.

**What is adopted.** A scratch run transcribes each scan with one call,
the transcription prompt and the scan, and every call goes through the
LLM cache. A transcription the vision route made (its `engine` is
`llm-vision:` and the model identifier of the client that answered) is
the answer to that call. The import stores it in the cache as an
adoption (ADR-0032 §9): under the key of the call the run is to make,
which the transcribe role's client keys exactly as the run's does
(Orchestrator.role_client: the engine, the model, the effort, the pinned
claude CLI, the cache), with the key's components and with provenance
backfilled from the archive. The run's call is then a hit whose reason is
`adopted`, in its events, and no engine is asked; the transcription view
it makes names the client the archive names, not the one it asked. Each
media's transcriptions, in turn:

* **The never-ingest list** is asked first (yizkr.core.corpus.is_excluded),
  in two passes. The first reads each document's `state.json` alone and
  asks with its file name and media SHA-1; the second opens a
  `transcription.json` only for a document the list leaves in, and asks
  again with the SHA-1 the transcription names, before its text is used.
  A media any of whose states the list excludes is excluded whole: every
  transcription the archive holds of it. A transcription whose own SHA-1
  the list names is excluded alone. A document with no readable
  `state.json` is not opened at all: the list cannot be asked for it.
  Without a list the import refuses to start. As in a run, the list also
  names the bytes of the images its globs name in the corpus's decks
  (yizkr.core.corpus.follow_decks), so a transcription of such bytes is
  excluded by its SHA-1 before its text is read. The corpus is found as
  the run finds it, with the list asked of each file there too.
* **Left out.** A media whose SHA-1 starts with a prefix the operator
  gives is not adopted, so the next run transcribes it afresh, and any
  adoption the cache serves for its call, whichever archive it came from,
  is withdrawn: moved aside as rejected, so that no later import adopts
  that text for the call again. The engine's own answer is never
  withdrawn. A prefix that matches no media of the archive, or several,
  refuses the import before anything is written; one that matches an
  excluded media is named in its line.
* **Made without a call**: a text file's transcription, a deck's slide
  text or a curator's correction. A run makes them again without a call,
  so there is nothing to adopt.
* **One answer per call.** Transcriptions of one media that differ are
  none of them adopted: nothing says which the run should take.
* **Re-keyed by its media's SHA-1**, which its `state.json` records or its
  transcription names. The call's key needs the scan itself, so the
  corpus must hold a scan with those bytes that the run would transcribe.
  Its SHA-1 and SHA-256 come from one reading of the scan, and the call's
  key is taken only when the digest it names, from a second reading, is
  that SHA-256, so no adoption is keyed on bytes other than the archive's
  media. The QID a transcription in the QID format names its artifact by
  is dropped: what is adopted is the text alone, and the view the run
  makes of it names the artifact by the SHA-1.
* **Made with the prompt the run sends.** A vision transcription in the
  QID format (`artifact_qid`: its artifact named by an instance's item)
  was made with the prompt whose SHA-256 is QID_FORMAT_PROMPT, and is
  adopted only while the run sends that prompt. One in the current format
  does not say which prompt it was made with, and is not adopted.
* **Checked**: the transcription check judges it as the run judges an
  answer (yizkr.pipeline.transcription_check.check_answer), with its
  scan's page count, whatever the cache holds for the call: a text that
  is not the transcription record (ADR-0037) fails it. One it fails is not
  adopted: the entry the cache serves for the call with that text, an
  adoption or the engine's own answer, is moved aside as rejected with
  the check's reason; with none, the text is recorded as an answer
  rejected under the call's key (EngineCache.record_rejected). So no
  adoption takes it later.
* **Never over another answer.** An adoption never replaces what the
  cache holds for the call, the engine's answer or another adoption. One
  adopted already with the same text counts as imported, so running the
  import again changes nothing.

**Provenance.** Each adoption's provenance says what made it, from the
archive's own record: its `engine` field as it stands, and the engine,
CLI version, model and model identifier that field names; the time it was
made, where the archive's events give it, else None; the SHA-256 of the
prompt it was made with, where its format tells it, else None; the media
SHA-1, the archive and where in it the transcription lies; the check's
verdict; and `backfilled: true`. It claims only what the archive tells:
what the archive leaves out stays None, never filled in from the run.
Its origin is the adoption's reason, `adopted`, never the engine. No QID
is kept.

**Invalidation.** An adoption stands for one call: the one whose key it is
stored under. A change to what that call sends (the prompt, the model,
the effort, the engine's configuration, a claude CLI update among them,
the scan's bytes, or the transcribe role's epoch) gives the run's call
another key, under which nothing is adopted: the call misses as `absent`,
and the engine transcribes the document afresh. A stricter transcription
check reaches adoptions as it reaches every answer the cache gives: the
run's transcriber checks each one, and rejects one it fails, which moves
the adoption aside and asks the engine (ADR-0032 §4). Running the import
again after a key change adopts under the new key only what the prompt
and the check still pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yizkr.core.corpus import (
    MISSING_LIST,
    ExclusionConfigError,
    ExclusionList,
    follow_decks,
    is_excluded,
)
from yizkr.pipeline.stages import sha256_text

# the files of an archive the import reads: a document folder's state and
# transcription, and a run directory's events
STATE = "state.json"
TRANSCRIPTION = "transcription.json"
EVENTS = "events.jsonl"
# the role the adopted answers are for, and the route whose call they
# answer: a transcription's `engine` names its route before the first
# colon, then the model identifier of the client that answered
ROLE = "transcribe"
VISION = "llm-vision"
# the engines a vision transcription's model identifier names: the claude
# CLI's is `claude-code/<CLI version>`, with `:<model>` when a model was
# configured; the API engine's is the model it ran
CLAUDE_CODE = "claude-code"
ANTHROPIC = "anthropic"
# The SHA-256 of the prompt every vision transcription in the QID format
# was made with: that format names its artifact by an instance's item
# (`artifact_qid`), and the vision route sent this prompt for each one.
QID_FORMAT_PROMPT = (
    "e9b42f6645e49dc3840b8ed973cad18eeeae43c446ab805725094b2320793b54"
)
# the origin of the engine's own answers in the cache, which an entry
# without one has too, as the cache serves it: an adoption never replaces
# one, and the import never withdraws one
ENGINE_ORIGIN = "engine"
# the fewest hex digits a --leave-out prefix gives, and how many name a
# media in the import's lines
MIN_PREFIX = 7
PREFIX = 10

# what became of the transcriptions of a media
IMPORTED = "imported"
REJECTED = "rejected"
EXCLUDED = "excluded"
LEFT_OUT = "left out"
SKIPPED = "skipped"
OUTCOMES = (IMPORTED, REJECTED, EXCLUDED, LEFT_OUT, SKIPPED)
# why they were skipped
UNREADABLE = "unreadable"
NO_SHA1 = "no media SHA-1"
NO_CALL = "made without a call"
DIFFERING = "differing transcriptions"
NOT_IN_CORPUS = "not in the corpus"
CHANGED = "scan changed"
OTHER_PROMPT = "another prompt"
UNKNOWN_PROMPT = "prompt unknown"
ANSWERED = "answered already"
REJECTED_BEFORE = "rejected before"

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_HEX = re.compile(r"^[0-9a-f]+$")


class ImportRefused(RuntimeError):
    """The import will not start: what the message names has to change
    first. Nothing was written."""


@dataclass
class Archived:
    """One transcription in the archive: its file, the archive's folder
    (`root`, resolved) and its own folder relative to it (`where`), the
    file names and media SHA-1s the archive records for it, and what it
    holds, or why it cannot be read."""

    path: Path
    root: Path
    where: str
    names: set[str] = field(default_factory=set)
    sha1s: set[str] = field(default_factory=set)
    text: str | None = None
    engine: str | None = None
    qid_format: bool = False
    made_at: str | None = None
    problem: str | None = None

    @property
    def route(self) -> str | None:
        return self.engine.partition(":")[0] if self.engine else None


@dataclass(frozen=True)
class Outcome:
    """What became of the transcriptions of one media, or of one the
    archive names no media for or cannot read: the outcome (OUTCOMES), the
    media's SHA-1, the first transcription's folder in the archive, why
    it was skipped, and what the operator is told."""

    outcome: str
    sha1: str | None
    where: str
    skipped: str | None = None
    detail: str = ""


@dataclass
class ImportReport:
    """The outcomes of an import, and what the adoptions were keyed for:
    the client's model identifier, effort and configuration hash, and the
    cache (None when nothing needed a key)."""

    archive: Path
    transcriptions: int
    outcomes: list[Outcome]
    keyed: dict[str, Any] | None = None

    def counts(self) -> Counter:
        return Counter(outcome.outcome for outcome in self.outcomes)

    def skipped(self) -> Counter:
        return Counter(
            outcome.skipped
            for outcome in self.outcomes
            if outcome.outcome == SKIPPED
        )


# -- reading the archive -----------------------------------------------------


def _foreign(path: Path, root: Path) -> str | None:
    """Why the file at `path` is not the archive's own, which the import
    does not read: a symbolic link, or a path that resolves outside the
    archive's folder `root`; None when it is its own."""
    if path.is_symlink():
        return "is a symbolic link"
    try:
        inside = path.resolve().is_relative_to(root)
    except (OSError, RuntimeError) as exc:
        return f"cannot be resolved ({type(exc).__name__}: {exc})"
    return None if inside else "resolves outside the archive"


def _read_own(path: Path) -> bytes:
    """The bytes of a file of the archive's own, opened without following
    a symbolic link: OSError otherwise."""
    with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
        return file.read()


def _load(path: Path, root: Path) -> tuple[Any, str | None]:
    """(the JSON of the archive's file at `path`, None), or (None, why it
    is not read)."""
    foreign = _foreign(path, root)
    if foreign is not None:
        return None, f"it {foreign}"
    try:
        return json.loads(_read_own(path).decode("utf-8")), None
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _sha1(value: Any) -> str | None:
    if isinstance(value, str) and _SHA1.match(value.lower()):
        return value.lower()
    return None


def read_archive(archive: Path) -> list[Archived]:
    """Every transcription the archive holds, in path order, with the
    identity its `state.json` records: the list's first pass (the module
    docstring says what is read). No transcription is opened here
    (read_transcription). ImportRefused when the archive is not a
    folder."""
    if not archive.is_dir():
        raise ImportRefused(
            f"{archive} is not a folder: give the archived state directory, "
            f"which holds a folder per document with its {STATE} and "
            f"{TRANSCRIPTION}"
        )
    root = archive.resolve()
    found = []
    for path in sorted(root.rglob(TRANSCRIPTION)):
        folder = path.parent.relative_to(root)
        if any(part.startswith(".") for part in folder.parts):
            continue
        if not path.is_symlink() and not path.is_file():
            continue
        record = Archived(path=path, root=root, where=folder.as_posix())
        record.problem = _read_state(record)
        found.append(record)
    return found


def _read_state(record: Archived) -> str | None:
    """Read the identity the state beside a transcription records into
    it; why the document cannot be read, or None."""
    foreign = _foreign(record.path, record.root)
    if foreign is not None:
        return f"its {TRANSCRIPTION} {foreign}"
    state, problem = _load(record.path.parent / STATE, record.root)
    if problem is not None:
        return f"its {STATE} is not read ({problem})"
    if not isinstance(state, dict) or not isinstance(state.get("file"), str):
        return f"its {STATE} names no source file"
    record.names.add(Path(state["file"]).name)
    if state.get("content_sha1") is not None:
        sha1 = _sha1(state["content_sha1"])
        if sha1 is None:
            return f"its {STATE} records a malformed SHA-1"
        record.sha1s.add(sha1)
    return None


def read_transcription(record: Archived) -> None:
    """Open a transcription the list's first pass leaves in, and read what
    it holds into it: the list's second pass asks with the SHA-1 it names
    before its text is used. A problem found is its `problem`."""
    data, problem = _load(record.path, record.root)
    problems = []
    if problem is not None:
        problems.append(f"it is not read ({problem})")
    elif not isinstance(data, dict):
        problems.append("it holds no transcription")
    else:
        if "artifact" in data:
            artifact = data["artifact"]
            sha1 = _sha1(
                artifact.get("sha1") if isinstance(artifact, dict) else None
            )
            if sha1 is None:
                problems.append("it names its artifact by a malformed SHA-1")
            else:
                record.sha1s.add(sha1)
        elif "artifact_qid" in data:
            record.qid_format = True
        else:
            problems.append("it names no artifact")
        text, engine = data.get("text"), data.get("engine")
        if not isinstance(text, str) or not text:
            problems.append("it holds no text")
        elif not isinstance(engine, str) or not engine:
            problems.append("it names no engine")
        else:
            record.text, record.engine = text, engine
    if len(record.sha1s) > 1:
        problems.append(f"its {STATE} and the transcription name other media")
    record.problem = "; ".join(problems) or None


def _made_at(records: Iterable[Archived]) -> None:
    """Read into each record when a run last made its transcription: the
    time of the latest `done` event of the transcription stage for its
    media, in the events of the folder that holds its document folder,
    when those events are the archive's own file (_transcribed)."""
    times: dict[Path, dict[str, str]] = {}
    for record in records:
        if len(record.sha1s) != 1:
            continue
        folder = record.path.parent.parent
        if folder not in times:
            times[folder] = _transcribed(folder, record.root)
        (sha1,) = record.sha1s
        record.made_at = times[folder].get(sha1)


def _transcribed(folder: Path, root: Path) -> dict[str, str]:
    """media SHA-1 -> the time of its transcription stage's latest `done`
    event, in the events of `folder`; nothing where there are none, or
    they are not the archive's own file: a symbolic link, or one outside
    it, as the events of the folder above the archive are."""
    events = folder / EVENTS
    if not (events.is_symlink() or events.is_file()):
        return {}
    if _foreign(events, root) is not None:
        return {}
    try:
        lines = _read_own(events).split(b"\n")
    except OSError:
        return {}
    times: dict[str, str] = {}
    for line in lines:
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        subject, at = event.get("subject"), event.get("at")
        if (
            event.get("event") == "done"
            and event.get("stage") == "transcription"
            and isinstance(subject, str)
            and isinstance(at, str)
        ):
            times[subject] = at
    return times


def archived_client(engine: str) -> dict[str, str | None]:
    """What a vision transcription's `engine` says made it: the model
    identifier of the client that answered (what follows `llm-vision:`),
    and the engine, CLI version and model that identifier names, each
    None where it names none."""
    _route, _, identifier = engine.partition(":")
    found: dict[str, str | None] = {
        "engine": None,
        "cli_version": None,
        "model": None,
        "model_identifier": identifier or None,
    }
    if identifier.startswith(f"{CLAUDE_CODE}/"):
        version, _, model = identifier[len(CLAUDE_CODE) + 1 :].partition(":")
        found |= {
            "engine": CLAUDE_CODE,
            "cli_version": version or None,
            "model": model or None,
        }
    elif identifier and not re.search(r"[/:\s]", identifier):
        found |= {"engine": ANTHROPIC, "model": identifier}
    return found


# -- deciding ----------------------------------------------------------------


def _prefixes(leave_out: Iterable[str]) -> list[str]:
    """The --leave-out prefixes, lowercased; ImportRefused for one that is
    not the start of a SHA-1, in at least MIN_PREFIX hex digits."""
    prefixes = []
    for given in leave_out:
        prefix = given.strip().lower()
        if not (MIN_PREFIX <= len(prefix) <= 40 and _HEX.match(prefix)):
            raise ImportRefused(
                f"--leave-out {given!r} is not the start of a media SHA-1: "
                f"give {MIN_PREFIX} to 40 hex digits"
            )
        prefixes.append(prefix)
    return prefixes


def _match_prefixes(
    prefixes: Sequence[str], known: set[str], archive: Path
) -> dict[str, str]:
    """media SHA-1 -> the --leave-out prefix it matches. ImportRefused for
    a prefix that matches no media of the archive, or several."""
    matched: dict[str, str] = {}
    for prefix in prefixes:
        found = sorted(sha1 for sha1 in known if sha1.startswith(prefix))
        if not found:
            raise ImportRefused(
                f"--leave-out {prefix}: no transcription in {archive} is of "
                "a media whose SHA-1 starts so; nothing was imported"
            )
        if len(found) > 1:
            raise ImportRefused(
                f"--leave-out {prefix} is ambiguous: the SHA-1s of "
                f"{len(found)} media of the archive start so; give more of "
                "its digits. Nothing was imported"
            )
        matched[found[0]] = prefix
    return matched


def _run_model() -> str:
    """The model the scratch run transcribes with, which the adoptions'
    keys name: ImportRefused when none is configured, or an alias, both of
    which a scratch run refuses too."""
    from yizkr.pipeline.orchestrate import MODEL_VARIABLE
    from yizkr.pipeline.rundir import model_alias

    model = os.environ.get(MODEL_VARIABLE) or None
    if model is None:
        raise ImportRefused(
            f"no model is configured for the transcribe role: set "
            f"${MODEL_VARIABLE} to the full model id the scratch run is to "
            "transcribe with, such as claude-opus-5-5. The adoptions are "
            "keyed on it, and a scratch run refuses to run without one"
        )
    if model_alias(model):
        raise ImportRefused(
            f"${MODEL_VARIABLE} names the model {model!r}, an alias, which "
            "neither the cache's keys nor a scratch run accept: configure "
            "the full model id, such as claude-opus-5-5"
        )
    return model


def _excluded(exclusions: ExclusionList, record: Archived) -> bool:
    """Does the never-ingest list exclude a transcription, by any file name
    or media SHA-1 the archive records for it?"""
    return any(
        is_excluded(exclusions, name=name) for name in record.names
    ) or any(is_excluded(exclusions, sha1=sha1) for sha1 in record.sha1s)


def _prompt_skip(record: Archived) -> tuple[str, str] | None:
    """(why, what to say) when a vision transcription was not made with
    the prompt the run sends, or cannot be told to have been; None when it
    was."""
    from yizkr.pipeline import transcribe

    if not record.qid_format:
        return (
            UNKNOWN_PROMPT,
            "a transcription in the current format does not say which "
            "prompt it was made with",
        )
    if sha256_text(transcribe.TRANSCRIPTION_PROMPT) != QID_FORMAT_PROMPT:
        return (
            OTHER_PROMPT,
            "it was made with another transcription prompt than the one "
            "the run sends",
        )
    return None


@dataclass
class _Media:
    """The readable transcriptions of one media in the archive, the scan
    the corpus holds with its bytes (None when it holds none), and the
    --leave-out prefix it matches, if any."""

    sha1: str
    records: list[Archived]
    scan: Path | None = None
    prefix: str | None = None

    @property
    def where(self) -> str:
        return self.records[0].where

    @property
    def vision(self) -> list[Archived]:
        return [record for record in self.records if record.route == VISION]

    @property
    def chosen(self) -> Archived:
        """The vision transcription adopted, of those with its one text:
        the first in the QID format, whose prompt is known, else the
        first."""
        vision = self.vision
        return next((r for r in vision if r.qid_format), vision[0])


def _decide(media: _Media) -> Outcome | None:
    """The outcome of a media's transcriptions that no key is needed for;
    None when one is to be adopted: its vision transcriptions agree, the
    corpus holds its scan, and the chosen one was made with the prompt the
    run sends."""
    sha1, where = media.sha1, media.where
    vision = media.vision
    if not vision:
        routes = ", ".join(sorted({str(r.route) for r in media.records}))
        return Outcome(SKIPPED, sha1, where, NO_CALL, f"route {routes}")
    texts = {record.text for record in vision}
    if len(texts) > 1:
        return Outcome(
            SKIPPED,
            sha1,
            where,
            DIFFERING,
            f"the archive holds {len(texts)} differing transcriptions of "
            "its media, in " + ", ".join(record.where for record in vision),
        )
    if media.scan is None:
        return Outcome(
            SKIPPED,
            sha1,
            where,
            NOT_IN_CORPUS,
            "the corpus holds no scan with its bytes for the run to "
            "transcribe: none, one it cannot read, or one the never-ingest "
            "list excludes by its name there",
        )
    skip = _prompt_skip(media.chosen)
    if skip is not None:
        return Outcome(SKIPPED, sha1, where, *skip)
    return None


# -- the cache ---------------------------------------------------------------


def _digests(path: Path) -> tuple[str, str]:
    """(SHA-1, SHA-256) of the file at `path`, from one reading of it."""
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def _provenance(record: Archived, sha1: str, check: Any) -> dict[str, Any]:
    """What made an archived transcription, as its archive records it
    (the module docstring lists it)."""
    from yizkr.pipeline.llm import COMPLETE

    return {
        "backfilled": True,
        "role": ROLE,
        "call": COMPLETE,
        **archived_client(str(record.engine)),
        "engine_field": record.engine,
        "made_at": record.made_at,
        "prompt_sha256": QID_FORMAT_PROMPT if record.qid_format else None,
        "media_sha1": sha1,
        "archive": str(record.root),
        "archived_as": record.path.relative_to(record.root).as_posix(),
        "check": check.model_dump(
            include={
                "stage",
                "version",
                "passed",
                "failure",
                "signature",
                "pages",
                "text_chars",
            }
        ),
    }


def _engines(entry: dict[str, Any]) -> bool:
    """Is a cache entry the engine's own answer, as the cache serves it
    (an entry that names no origin included)?"""
    return entry.get("origin", ENGINE_ORIGIN) == ENGINE_ORIGIN


class _Adopter:
    """The transcribe role's client, as the run's (Orchestrator.
    role_client), and what it keys the run's calls for (`keyed`, once it
    has keyed one)."""

    def __init__(self, client: Any, cache_dir: Path) -> None:
        self.client = client
        self.cache = client.cache
        self.cache_dir = cache_dir
        self.keyed: dict[str, Any] | None = None

    def _call(self, media: _Media) -> tuple[str, dict[str, Any]] | None:
        """(key, components) of the call the run transcribes the media's
        scan with. The scan's SHA-1 and SHA-256 come from one reading of
        it, and the key is taken only when the digest it names, from a
        second reading, is that SHA-256. None when the SHA-1 is not the
        media's, or the scan changed between the two readings."""
        from yizkr.pipeline import transcribe

        scan = media.scan
        if scan is None:
            return None
        sha1, sha256 = _digests(scan)
        key, components = self.client.complete_call(
            transcribe.TRANSCRIPTION_PROMPT, [scan]
        )
        if sha1 != media.sha1 or components["files"] != [sha256]:
            return None
        if self.keyed is None:
            self.keyed = {
                "client": self.client.model_identifier,
                "effort": components["effort"],
                "config": components["config"],
                "cache": str(Path(self.cache_dir).absolute()),
            }
        return key, components

    def adopt(self, media: _Media) -> Outcome:
        """Check a media's transcription, and adopt it for the run's call,
        or record it as rejected under the call's key."""
        from yizkr.pipeline.transcription_check import (
            TranscriptionCheckFailed,
            check_answer,
            is_pdf,
            media_page_count,
        )

        sha1, where = media.sha1, media.where
        keyed = self._call(media)
        if keyed is None or media.scan is None:
            return Outcome(
                SKIPPED,
                sha1,
                where,
                CHANGED,
                "the scan's bytes changed while the import read them: run "
                "it again",
            )
        key, components = keyed
        record = media.chosen
        text = str(record.text)
        check, _record = check_answer(
            text,
            pages=media_page_count(media.scan),
            whole_pdf=is_pdf(media.scan),
        )
        provenance = _provenance(record, sha1, check)
        live = self.cache.get(key)
        same = live is not None and live.get("response") == text
        if not check.passed:
            reason = str(TranscriptionCheckFailed(check))
            # an answer the cache serves with this text, one an earlier
            # import adopted say, is moved aside with the reason
            if not (same and self.cache.reject(key, text, reason, role=ROLE)):
                self.cache.record_rejected(
                    key,
                    text,
                    provenance,
                    reason,
                    components=components,
                    role=ROLE,
                )
            return Outcome(REJECTED, sha1, where, detail=reason)
        if live is not None:
            engine = _engines(live)
            if same and not engine:
                return Outcome(IMPORTED, sha1, where, detail="adopted already")
            held = "the engine's answer" if engine else "another adoption"
            return Outcome(
                SKIPPED, sha1, where, ANSWERED, f"the cache holds {held}"
            )
        if self.client.seed(key, text, provenance, components=components):
            return Outcome(IMPORTED, sha1, where, detail="adopted")
        return Outcome(
            SKIPPED,
            sha1,
            where,
            REJECTED_BEFORE,
            "it was rejected under its call's key before, and the cache "
            "adopts no answer a caller rejected",
        )

    def withdraw(self, media: _Media) -> bool:
        """Withdraw the adoption the cache serves for the run's call for a
        left-out media's scan, whatever made it: move it aside as
        rejected. The engine's own answer stays. Returns whether one was
        withdrawn."""
        keyed = self._call(media)
        if keyed is None:
            return False
        key, _components = keyed
        live = self.cache.get(key)
        if live is None or _engines(live):
            return False
        return self.cache.reject(
            key,
            live["response"],
            "left out of the archived transcriptions adopted "
            f"(--leave-out {media.prefix})",
            role=ROLE,
        )

    def close(self) -> None:
        self.client.close()


def _preflight(scans: Iterable[Path]) -> None:
    """The PDF renderer, when a scan to check is a PDF: the check counts
    its pages with it, as the run's does."""
    from yizkr.pipeline.transcribe import (
        MissingPrerequisite,
        preflight_vision_transcription,
    )

    try:
        preflight_vision_transcription(scans)
    except MissingPrerequisite as exc:
        raise ImportRefused(str(exc)) from None


# -- the import ------------------------------------------------------------


def import_transcriptions(
    archive: Path,
    corpus: Path,
    *,
    cache_dir: Path,
    exclusions: ExclusionList | None,
    extract_effort: str = "low",
    leave_out: Sequence[str] = (),
    llm: str = "claude-code",
    log: Callable[..., Any] = print,
) -> ImportReport:
    """Adopt the archive's vision transcriptions for the calls a scratch
    run over `corpus` makes (the module docstring says which, and how), in
    the LLM cache under `cache_dir`, keyed for the transcribe role as the
    run keys its calls: the engine `llm`, `extract_effort` and
    $YIZKR_LLM_MODEL. `leave_out` gives prefixes of the SHA-1s of media
    whose transcriptions are not adopted. Refuses to start without a
    never-ingest list, or with a deck in `corpus` that cannot be read as an
    archive, or a `corpus` that cannot be listed, while the list holds a
    glob (ExclusionConfigError); for a
    malformed, unmatched or ambiguous prefix, no model or an alias, an
    archive or a corpus that is not a folder, a scan in PDF without the
    renderer the check counts its pages with, or an engine client that
    cannot start (ImportRefused); and when the cache refuses its folder,
    or another process holds it, or the engine cannot serve the pass
    (llm.PassPaused). `log` takes the
    corpus's discovery lines, which name its files. Returns what became of
    each media's transcriptions."""
    from yizkr.pipeline.orchestrate import Orchestrator

    if exclusions is None:
        raise ExclusionConfigError(MISSING_LIST)
    prefixes = _prefixes(leave_out)
    _run_model()
    if not corpus.is_dir():
        raise ImportRefused(f"the corpus {corpus} is not a folder")
    # the list names the bytes its globs name in the corpus's decks, as
    # the run's does, before any record is asked of
    exclusions = follow_decks(exclusions, corpus)

    # the never-ingest list, first pass: the states alone. A media any of
    # whose states it excludes is excluded whole
    records = read_archive(archive)
    excluded = {
        sha1
        for record in records
        if _excluded(exclusions, record)
        for sha1 in record.sha1s
    }

    def out(record: Archived) -> bool:
        """Does the list exclude the record, by its own identity or its
        media's? Asked again once its transcription is read, with the
        SHA-1 that names, before its text is used."""
        return bool(record.sha1s & excluded) or _excluded(exclusions, record)

    # second pass: the transcriptions the list leaves in
    for record in records:
        if record.problem is None and not out(record):
            read_transcription(record)
    matched = _match_prefixes(
        prefixes,
        {sha1 for record in records for sha1 in record.sha1s},
        archive,
    )
    _made_at(record for record in records if not out(record))

    outcomes: list[Outcome] = []
    media: dict[str, _Media] = {}
    reported: set[str] = set()
    for record in records:
        if out(record):
            sha1 = min(record.sha1s) if record.sha1s else None
            if sha1 is None or sha1 not in reported:
                note = (
                    f"--leave-out {matched[sha1]} matched it too"
                    if sha1 in matched
                    else ""
                )
                outcomes.append(
                    Outcome(EXCLUDED, sha1, record.where, detail=note)
                )
            if sha1 is not None:
                reported.add(sha1)
        elif len(record.sha1s) == 1 and next(iter(record.sha1s)) in matched:
            # left out whole, whether or not it can be read
            (sha1,) = record.sha1s
            media.setdefault(sha1, _Media(sha1, [])).records.append(record)
        elif record.problem is not None:
            outcomes.append(
                Outcome(
                    SKIPPED, None, record.where, UNREADABLE, record.problem
                )
            )
        elif not record.sha1s:
            outcomes.append(
                Outcome(
                    SKIPPED,
                    None,
                    record.where,
                    NO_SHA1,
                    f"neither its {STATE} nor the transcription records the "
                    "SHA-1 of its media",
                )
            )
        else:
            (sha1,) = record.sha1s
            media.setdefault(sha1, _Media(sha1, [])).records.append(record)

    with tempfile.TemporaryDirectory(prefix="yizkr-import-") as scratch:
        # the orchestrator is asked for the corpus's documents, as the run
        # finds them, and for the transcribe role's client; it writes
        # nothing to its state directory
        orchestrator = Orchestrator(
            corpus,
            Path(scratch) / "state",
            llm=llm,
            extract_effort=extract_effort,
            cache_dir=cache_dir,
            exclusions=exclusions,
            log=log,
        )
        scans = {
            doc.known_sha1: doc.source
            for doc in orchestrator.documents()
            if doc.unreadable is None and doc.kind in ("media", "pdf")
        }
        adopt: list[_Media] = []
        left: list[_Media] = []
        for sha1, found in sorted(media.items()):
            found.scan = scans.get(sha1)
            found.prefix = matched.get(sha1)
            if found.prefix is not None:
                left.append(found)
                continue
            decided = _decide(found)
            if decided is not None:
                outcomes.append(decided)
            else:
                adopt.append(found)
        withdraw = [found for found in left if found.scan is not None]
        withdrawn: set[str] = set()
        keyed = None
        if adopt or withdraw:
            _preflight(found.scan for found in adopt if found.scan)
            try:
                client = orchestrator.role_client(ROLE)
            except RuntimeError as exc:
                raise ImportRefused(
                    f"the transcribe role's engine client cannot start, and "
                    f"the keys need it: {exc}"
                ) from None
            adopter = _Adopter(client, cache_dir)
            try:
                outcomes += [adopter.adopt(found) for found in adopt]
                withdrawn = {
                    found.sha1 for found in withdraw if adopter.withdraw(found)
                }
            finally:
                adopter.close()
            keyed = adopter.keyed
        outcomes += [
            Outcome(
                LEFT_OUT,
                found.sha1,
                found.where,
                detail=f"--leave-out {found.prefix}"
                + (
                    "; the adoption the cache served withdrawn"
                    if found.sha1 in withdrawn
                    else ""
                ),
            )
            for found in left
        ]
    order = {outcome: rank for rank, outcome in enumerate(OUTCOMES)}
    outcomes.sort(key=lambda o: (order[o.outcome], o.sha1 or "", o.where))
    return ImportReport(archive, len(records), outcomes, keyed)


def describe(report: ImportReport) -> list[str]:
    """What an import did, for the operator: a line per media, then the
    counts. The lines are local output: a rejection's reason may quote
    the transcription it rejects."""
    lines = [f"{report.transcriptions} transcriptions in {report.archive}"]
    if report.keyed is not None:
        keyed = report.keyed
        lines.append(
            f"keyed for the transcribe role as a scratch run keys its calls: "
            f"{keyed['client']}, effort {keyed['effort']}, config "
            f"{str(keyed['config'])[:12]}, cache {keyed['cache']}"
        )
    for outcome in report.outcomes:
        why = f"{outcome.skipped}: " if outcome.skipped else ""
        lines.append(
            f"  {_short(outcome.sha1, outcome.where):<{PREFIX}}  "
            f"{outcome.outcome:<9} {why}{outcome.detail}".rstrip()
        )
    counts = report.counts()
    lines.append(
        ", ".join(f"{outcome} {counts[outcome]}" for outcome in OUTCOMES[:-1])
        + f", skipped {counts[SKIPPED]}"
        + (
            " ("
            + ", ".join(
                f"{count} {why}"
                for why, count in sorted(report.skipped().items())
            )
            + ")"
            if counts[SKIPPED]
            else ""
        )
    )
    return lines


def _short(sha1: str | None, where: str) -> str:
    return sha1[:PREFIX] if sha1 else where
