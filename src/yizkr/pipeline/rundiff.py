"""The run-to-run diff: what changed between two runs, and why
(`yizkr scratch diff`; ADR-0025 §4).

It compares a run with a baseline, by default the latest completed run on
main (`baseline_run`), and reads the two run directories and nothing
else of the run: never the wiki or its query service, the engine or the
corpus. From each it reads:

    .report/report.json  the run report (yizkr.pipeline.report), as the
                         operator made it; it is built afresh, and not
                         written, when the run has none, when it is older
                         than the run's latest invocation's end, or when
                         `rebuild` asks
    run.json             what shaped the run: its engine, each role's
                         model, effort, epoch and client, its code
    events.jsonl         the LLM cache's events, which key each call a
                         stage made
    alerts.jsonl         the anomaly alerts
    <document>/          each document's views: transcription.json,
                         batch.json and critique.json
    corpus/clustering/   the clustering stage's identity-map.json

and from the LLM cache the run names, read as it is through the client
factory (llm.read_cache), only the `components` of the entries those
events key: never a response. A recompute is attributed to
the components of its call's key that differ from the baseline's calls
for the same document and stage (ADR-0032 §11): the prompt, a file, the
model, the configuration, the attempt, the epoch.

**What it compares.**

- *Settings*: the run manifests' engine, roles, code and configuration.
- *Stages*: each stage's documents by how they came about (computed,
  reused by reason, needs attention, failed, ...), each recompute by the
  key components that changed, and each document's view, compared by its
  content hash with the volatile fields left out (the orchestrator's), so
  a recompute that made the same view again shows as such.
- *Figures*: every count, rate and share the two reports' aggregates
  hold (documents, stages, content, anomalies, calls), side by side. A
  figure one report lacks, as one made before that figure was counted
  does, is *absent* on that side, never 0, and has no delta.
- *Anomalies*: each finding (a document or the run, a stage and a
  check) new in the run, resolved since the baseline, or in both.
- *Failures*: the documents that fail in one run and not the other.
- *Flags* and *contradictions*: compared when both reports count them;
  otherwise not available, with why.
- *Place hierarchy*: the place stage's counts (questions, calls,
  answered, uncertain and unanswered, stated links left out, links
  written, referenced and already there, links a record alone states
  and of them those written, links unresolved, answers rejected by the
  schema, and cycles and conflicts refused), side by side, as figures.
- *Claims*, per document and property, matched by their claim
  fingerprints (yizkr.pipeline.fingerprints) as multisets: those only
  the baseline holds are removed and those only the run holds added,
  save where one of each shares its subject, property and artifact, a
  pair that is *changed* (its value, qualifiers or locator). The
  excerpt is no part of a fingerprint: a matched claim whose excerpt
  differs is counted apart, as a detail. A claim's subject is its
  mention's fingerprint, so a claim whose mention changed (another
  label, kind or span) is removed and added, never changed.
- *Mentions*, by their fingerprints: added and removed, by kind.
- *Clusters*, by membership over the mentions both identity maps hold: a
  cluster of the run whose mentions come from several of the
  baseline's is a merge, a cluster of the baseline whose mentions go to
  several of the run's a split; and the mentions whose method changed.
  Claim fingerprints never name a cluster, so a clustering that changes
  no claim changes no claim of the diff.

**Outputs.** `write_diff` writes `diff.json`, the whole diff, and
`diff.md`, the aggregates alone, into `<run>/.diff/<baseline>/`, a
folder private to its owner, as the report writes its own (`.diff`
starts with a dot, so no document's directory takes the name). diff.json
holds documents' SHA-1s and file names, labels, values and excerpts, so
it stays local, as the run directory does (docs/AGENT_PROTOCOL.md §6).
diff.md is rendered from the aggregates alone (`aggregate`): counts,
rates and names of the pipeline's own vocabulary, each checked as the
report checks its own (an unknown name is `other`), and never a
document, a file name, a path, a label, a value, an excerpt, a quote or
a language code.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yizkr.claims import ClaimBatch, load_vocabulary
from yizkr.pipeline import report
from yizkr.pipeline.clustering import IDENTITY_MAP, read_identity_map
from yizkr.pipeline.clustering import STAGE as CLUSTERING
from yizkr.pipeline.fingerprints import ClaimPrint, claim_prints, mentions_of
from yizkr.pipeline.llm import ROLE_STAGES, PassPaused, read_cache
from yizkr.pipeline.orchestrate import BATCH_VOLATILE, TRANSCRIPTION_VOLATILE
from yizkr.pipeline.rundir import (
    ALERTS,
    EVENTS,
    MANIFEST,
    PACKAGE_DIR,
    RUN_START,
    _git,
)
from yizkr.pipeline.scratch import CORPUS_DIR, is_complete, runs_in
from yizkr.pipeline.stages import canonical_json, content_hash, sha256_text

# the version of diff.json's layout
FORMAT = 1

DIFF_DIR = ".diff"
JSON_FILE = "diff.json"
MARKDOWN_FILE = "diff.md"

OTHER = report.OTHER
NOT_AVAILABLE = report.NOT_AVAILABLE
# a figure one report does not hold
ABSENT = "absent"

# the refs `baseline_run` asks git whether a run's commit is on, in turn
MAIN_REFS = ("origin/main", "main")

# the components of an LLM cache key (docs/LLM_CACHE.md, Keys)
COMPONENTS = frozenset(
    {
        "engine",
        "model",
        "effort",
        "config",
        "call",
        "system",
        "prompt",
        "files",
        "parent",
        "opening",
        "attempt",
        "epoch",
    }
)
# what a recompute is put down to besides the components that changed: a
# miss for another reason than an absent answer (`rejected`, `refresh`);
# the same key as the baseline's, whose answer the cache no longer
# serves; no call of the baseline's for the document and stage to
# compare with; a call or entry whose components cannot be read; a stage
# that made no call, or made its calls with no cache between
SAME_KEY = "same key"
NO_BASELINE = "no baseline call"
UNREAD = NOT_AVAILABLE

# a stage's view in a document's directory, and what its content hash
# leaves out
VIEWS = {
    "transcription": ("transcription.json", TRANSCRIPTION_VOLATILE),
    "extraction": ("batch.json", BATCH_VOLATILE),
    "gedcom": ("batch.json", BATCH_VOLATILE),
    "critique": ("critique.json", ()),
}
COMPLETED = frozenset({report.DONE, report.NEEDS_ATTENTION})
# the parts of a claim a changed pair can differ in
CLAIM_PARTS = ("value", "qualifiers", "locator")
# how a mention came to be where the identity map puts it
METHODS = frozenset(
    {
        "confident",
        "confident_component",
        "adjudicated",
        "ambiguous",
        "conflicting",
        "singleton",
        "unclustered",
    }
)
# the aggregates' sections whose figures the diff sets side by side;
# times are left out, as they differ from run to run whatever changed
FIGURE_SECTIONS = ("documents", "stages", "content", "anomalies", "calls")
# the figures of the place stage's counts, shown in a section of their own
PLACE_FIGURES = "content.place_hierarchy."
_TIMES = re.compile(r"(^|_)seconds$")
# figures never set side by side: the documents per language, since the
# corpus's languages are never posted
_UNSHOWN = frozenset({"documents.languages"})
# a tally of the report's aggregates, which holds only the names it
# counted: `by_kind`, `reused_by`, `hits_by_reason`, `forced`, ...
_COUNTER = re.compile(
    r"^by_|_by_|_by$|^(forced|attention_labels|failure_labels|fallbacks|"
    r"routes|language_resolved|pdf_pages|per_pdf|per_deck|set_aside|pauses|aborts|"
    r"give_ups_replayed|holds|errors|models|outcomes|output_limit_stops)$"
)
_SHA1 = re.compile(r"[0-9a-f]{40}")
_KEY = re.compile(r"[0-9a-f]{64}")
# the run manifest's sections a setting is read from
SETTING_SECTIONS = ("engine", "roles", "code", "config")


class DiffError(RuntimeError):
    """A run cannot be diffed: no run there, no baseline to diff it
    with, a report that cannot be made, or a folder that cannot be
    written."""


# -- reading a run ------------------------------------------------------------


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _manifest(path: Path) -> dict:
    try:
        found = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return found if isinstance(found, dict) else {}


def read_report(path: Path, *, rebuild: bool = False) -> tuple[dict, str]:
    """The run report of the run directory at `path`, and whether it is
    the one `yizkr scratch report` stored (`stored`) or one built now
    (`built`). The stored one is read unless `rebuild` asks, or it cannot
    be read, holds no aggregates, or was made before the run's latest
    invocation ended (or while it still ran); a built one is not
    written."""
    if not rebuild:
        stored = report._json(path / report.REPORT_DIR / report.JSON_FILE)
        ended = _time(_manifest(path).get("ended_at"))
        if (
            isinstance(stored, dict)
            and isinstance(stored.get("aggregate"), dict)
            and ended is not None
            and (made := _time(stored.get("generated_at"))) is not None
            and made >= ended
        ):
            return stored, "stored"
    try:
        return report.build(path), "built"
    except report.ReportError as exc:
        raise DiffError(str(exc)) from None


@dataclass
class _Call:
    """A call the cache's events key for a document's stage: the event
    (`cache-hit`, `cache-miss` or `cache-replay`), the key, the reason and
    whether a replay answered as the cache had."""

    event: str
    key: str
    reason: str | None
    matches: bool | None = None


def keyed_calls(path: Path) -> dict[tuple[str, str], list[_Call]]:
    """(document SHA-1, stage) -> the calls the cache's events key for
    the stage's latest attempt, in order. A cache event names the
    document, not the stage: it is the stage the document is in, as its
    stage events leave it, else the stage of the event's role."""
    calls: dict[tuple[str, str], list[_Call]] = {}
    current: dict[str, str] = {}
    try:
        events = list(report._lines(path / EVENTS))
    except OSError:
        return calls
    for event in events:
        name = event.get("event")
        subject = event.get("subject")
        if name == RUN_START:
            current.clear()
            continue
        if not isinstance(subject, str) or not subject:
            continue
        stage = event.get("stage")
        if name in report.STAGE_EVENTS and isinstance(stage, str):
            if name == "started":
                current[subject] = stage
                calls[(subject, stage)] = []
            elif current.get(subject) == stage:
                del current[subject]
            continue
        if name not in ("cache-hit", "cache-miss", "cache-replay"):
            continue
        key = event.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            continue
        stage = current.get(subject) or ROLE_STAGES.get(event.get("role"))
        if stage is None:
            continue
        reason = event.get("reason")
        matches = event.get("matches")
        calls.setdefault((subject, stage), []).append(
            _Call(
                event=name,
                key=key,
                reason=reason if isinstance(reason, str) else None,
                matches=matches if isinstance(matches, bool) else None,
            )
        )
    return calls


class Components:
    """The components of LLM cache keys, read from the cache folder as it
    is (llm.read_cache: no lock taken, nothing written): the entry
    served under the key, else one moved aside under it (rejected or
    replaced), whose components are the key's all the same. Only an
    entry's `components` is used, and only when they hash to its key;
    never its response. None for a key with no such entry, and for every
    key when there is no folder, or one the cache would refuse."""

    def __init__(self, folder: Path | None) -> None:
        self.cache: Any = None
        if folder is not None and Path(folder).is_dir():
            try:
                self.cache = read_cache(folder)
            except (PassPaused, OSError):
                self.cache = None
        self._read: dict[str, dict | None] = {}

    @property
    def available(self) -> bool:
        return self.cache is not None

    def __call__(self, key: str) -> dict | None:
        if key not in self._read:
            self._read[key] = self._find(key)
        return self._read[key]

    def _find(self, key: str) -> dict | None:
        if self.cache is None or not _KEY.fullmatch(key):
            return None
        entries = [self.cache.get(key)]
        with contextlib.suppress(OSError):
            entries += self.cache.asides(key)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("key") != key:
                continue
            components = entry.get("components")
            if isinstance(components, dict):
                try:
                    if sha256_text(canonical_json(components)) == key:
                        return components
                except (TypeError, ValueError):
                    continue
        return None


# -- the baseline -------------------------------------------------------------


def main_ref(refs: Iterable[str] = MAIN_REFS) -> str:
    """The first of `refs` that git knows, in the checkout the package
    runs from (as rundir.code_version asks it). DiffError when git knows
    none of them, or cannot be asked."""
    refs = tuple(refs)
    for ref in refs:
        known = _git(
            PACKAGE_DIR,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        )
        if known:
            return ref
    raise DiffError(
        f"git knows none of {', '.join(refs)} in {PACKAGE_DIR}, so no run "
        "can be told to be on main: name the baseline run, or the ref that "
        "is main (--main)"
    )


def _clean_on(code: Any, ref: str) -> bool:
    """Whether `code` (a run manifest's `code`) is clean code of a commit
    in the history of `ref`. A commit that is no SHA-1 is never given to
    git."""
    if not isinstance(code, dict) or code.get("dirty") is not False:
        return False
    commit = code.get("commit")
    if not isinstance(commit, str) or not _SHA1.fullmatch(commit):
        return False
    return (
        _git(PACKAGE_DIR, "merge-base", "--is-ancestor", commit, ref)
        is not None
    )


def on_main(run: Path, ref: str) -> bool:
    """Whether the run ran clean code (no difference from its commit) of
    a commit on main (`ref`), and so did an invocation that took it past
    its barrier on other code (its `scratch.code`), if one did."""
    manifest = _manifest(run)
    scratch = manifest.get("scratch")
    later = scratch.get("code") if isinstance(scratch, dict) else None
    return _clean_on(manifest.get("code"), ref) and (
        later is None or _clean_on(later, ref)
    )


def baseline_run(
    runs: Path, run: Path, refs: Iterable[str] = MAIN_REFS
) -> Path:
    """The latest completed run on main in `runs` that is older than
    `run` (by run id, the order runs start in): the latest whose last
    invocation went through every stage (scratch.is_complete), run on
    clean code of a commit on main (on_main). DiffError when there is
    none, or when git knows none of `refs`."""
    ref = main_ref(refs)
    # the run's own name, however its path was given (".", a link)
    name = Path(run).resolve().name
    for candidate in reversed(runs_in(runs)):
        if os.path.realpath(candidate) == os.path.realpath(run):
            continue
        if candidate.resolve().name >= name:
            continue
        if is_complete(candidate) and on_main(candidate, ref):
            return candidate
    raise DiffError(
        f"no completed run on {ref} older than {name} in {runs} to diff "
        "with: name the baseline run"
    )


# -- the vocabulary -----------------------------------------------------------


@dataclass(frozen=True)
class _Names:
    """What the aggregates may name: the report's stages, the model
    manifest's properties and item keys, and the anomaly checks."""

    stages: frozenset[str]
    properties: frozenset[str]
    items: frozenset[str]

    @classmethod
    def of(cls, *manifests: Mapping[str, Any]) -> _Names:
        stages: frozenset[str] = frozenset()
        for manifest in manifests:
            stages |= report._Vocabulary.of(manifest).stages
        properties, items = load_vocabulary()
        return cls(stages, frozenset(properties), frozenset(items))

    def stage(self, value: Any) -> str:
        return report._known(value, self.stages)

    def property(self, value: Any) -> str:
        return report._known(value, self.properties)

    def kind(self, value: Any) -> str:
        return report._known(value, self.items)


# -- settings -----------------------------------------------------------------


def _leaves(value: Any, prefix: str) -> Iterator[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for name, inner in value.items():
            if isinstance(name, str):
                yield from _leaves(inner, f"{prefix}.{name}")
    else:
        yield prefix, value


def _model(value: Any) -> Any:
    if isinstance(value, str) and report._MODEL.fullmatch(value):
        return value
    return OTHER


def _effort(value: Any) -> Any:
    if isinstance(value, str) and value in report.EFFORTS:
        return value
    return OTHER


def _epoch(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return OTHER


def _engine(value: Any) -> Any:
    if isinstance(value, str) and value in report.ENGINES:
        return value
    return OTHER


def _code_version(value: Any) -> Any:
    return report._version(value) or OTHER


def _prefix(value: Any) -> Any:
    """A commit or a configuration hash as report.md shows it: its first
    12 hex digits."""
    return report._hexed(value) or OTHER


def _flag(value: Any) -> Any:
    return value if isinstance(value, bool) else OTHER


# the settings diff.md may name, by path ("{role}" standing for each of
# the roles), with how each shows its value; any other setting that
# differs is only counted, its name and values never shown, so no hash,
# no language and no free text of a run's configuration reaches diff.md
SHOWN_SETTINGS = {
    "engine.name": _engine,
    "engine.cli_version": _code_version,
    "code.commit": _prefix,
    "code.dirty": _flag,
    "roles.{role}.model": _model,
    "roles.{role}.effort": _effort,
    "roles.{role}.epoch": _epoch,
    "roles.{role}.client.model": _model,
    "roles.{role}.client.effort": _effort,
    "roles.{role}.client.config_hash": _prefix,
    "config.extract_model": _model,
    "config.extract_effort": _effort,
    "config.review_effort": _effort,
    "config.epochs.{role}": _epoch,
}
_SHOWN = {
    path.format(role=role): shown
    for path, shown in SHOWN_SETTINGS.items()
    for role in sorted(report.ROLES)
}


def _differing(
    baseline: Mapping[str, Any], run: Mapping[str, Any]
) -> list[tuple[str, Any, Any]]:
    """(path, baseline's value, run's value) of each setting the two run
    manifests record differently, by path."""
    found: dict[str, list] = {}
    for side, manifest in enumerate((baseline, run)):
        for section in SETTING_SECTIONS:
            for name, value in _leaves(manifest.get(section), section):
                found.setdefault(name, [None, None])[side] = value
    return [
        (name, before, after)
        for name, (before, after) in sorted(found.items())
        if json.dumps(before, sort_keys=True, default=str)
        != json.dumps(after, sort_keys=True, default=str)
    ]


def settings(baseline: Mapping[str, Any], run: Mapping[str, Any]) -> dict:
    """The settings the two run manifests record differently: those
    SHOWN_SETTINGS names, each with its two values as diff.md may show
    them (None for a value a manifest does not record), and how many
    others differ."""
    rows = []
    others = 0
    for name, before, after in _differing(baseline, run):
        shown = _SHOWN.get(name)
        if shown is None:
            others += 1
            continue
        rows.append(
            {
                "setting": name,
                "baseline": None if before is None else shown(before),
                "run": None if after is None else shown(after),
            }
        )
    return {"shown": rows, "other": others}


# -- figures ------------------------------------------------------------------


@dataclass
class _Figures:
    """The numbers under a section of a report's aggregates, by their
    paths, and the paths of the mappings that hold them."""

    numbers: dict[tuple[str, ...], int | float] = field(default_factory=dict)
    tables: set[tuple[str, ...]] = field(default_factory=set)

    def read(self, value: Any, path: tuple[str, ...]) -> None:
        """Every number under `value`; a list of rows named by their
        `name` (a stage's) is read as a mapping by name."""
        if isinstance(value, list):
            value = {
                row["name"]: row
                for row in value
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            }
        if isinstance(value, Mapping):
            self.tables.add(path)
            for name, inner in value.items():
                if (
                    isinstance(name, str)
                    and not _TIMES.search(name)
                    and ".".join((*path, name)) not in _UNSHOWN
                ):
                    self.read(inner, (*path, name))
            return
        number = _number(value)
        if number is not None:
            self.numbers[path] = number

    def value(self, path: tuple[str, ...]) -> int | float | str:
        """The figure at `path`; 0 for a name a tally the report holds
        leaves out (a tally keeps only the names it counted); ABSENT for
        one under a section the report does not hold, or that is no
        tally's."""
        if path in self.numbers:
            return self.numbers[path]
        for end in range(len(path) - 1, 0, -1):
            if path[:end] in self.tables:
                return 0 if _COUNTER.search(path[end - 1]) else ABSENT
        return ABSENT


def _compare(sides: tuple[_Figures, _Figures]) -> list[dict]:
    """Each figure either side holds, with its value in each and the
    delta, which only two numbers have."""
    rows = []
    for path in sorted(set(sides[0].numbers) | set(sides[1].numbers)):
        before, after = sides[0].value(path), sides[1].value(path)
        rows.append(
            {
                "figure": ".".join(path),
                "baseline": before,
                "run": after,
                "delta": round(after - before, 4)
                if ABSENT not in (before, after)
                else None,
            }
        )
    return rows


def figures(baseline: Mapping[str, Any], run: Mapping[str, Any]) -> list:
    """Every figure of either report's aggregates (FIGURE_SECTIONS),
    with its value in each and the delta. A figure a report does not
    hold, as one made before the figure was counted does not, is ABSENT
    on its side, never 0, and has no delta; a name a tally leaves out is
    0."""
    sides = []
    for aggregate in (baseline, run):
        found = _Figures()
        for section in FIGURE_SECTIONS:
            found.read(aggregate.get(section), (section,))
        sides.append(found)
    return _compare((sides[0], sides[1]))


# -- documents ----------------------------------------------------------------


@dataclass
class _Document:
    """One document of one run: its kind, files, stages as the report
    leaves them, whether it failed and why, and where its views are."""

    sha1: str
    kind: str | None = None
    files: list[str] = field(default_factory=list)
    stages: dict[str, dict] = field(default_factory=dict)
    failed: bool = False
    reason: str | None = None
    directory: Path | None = None

    def completed(self, stage: str) -> bool:
        record = self.stages.get(stage)
        return isinstance(record, dict) and record.get("status") in COMPLETED


def _documents(path: Path, built: Mapping[str, Any]) -> dict[str, _Document]:
    """The documents the report holds, by SHA-1, each with the directory
    that holds its views."""
    directories = report._directories(path)
    documents: dict[str, _Document] = {}
    for record in built.get("documents") or []:
        if not isinstance(record, dict):
            continue
        sha1 = record.get("sha1")
        if not isinstance(sha1, str) or not _SHA1.fullmatch(sha1):
            continue
        stages = record.get("stages")
        stages = stages if isinstance(stages, dict) else {}
        reason = None
        for attempt in stages.values():
            if isinstance(attempt, dict) and attempt.get("status") == "failed":
                reason = attempt.get("reason")
        failures = record.get("failures")
        if reason is None and isinstance(failures, list) and failures:
            last = failures[-1]
            reason = last.get("reason") if isinstance(last, dict) else None
        documents[sha1] = _Document(
            sha1=sha1,
            kind=record.get("kind")
            if isinstance(record.get("kind"), str)
            else None,
            files=[f for f in record.get("files") or [] if isinstance(f, str)],
            stages=stages,
            failed=record.get("failed") is True,
            reason=reason if isinstance(reason, str) else None,
            directory=directories[sha1][0] if sha1 in directories else None,
        )
    return documents


def _view_hash(document: _Document, stage: str) -> str | None:
    """The content hash of the document's view of the stage, its volatile
    fields left out; None when the stage did not complete, or its view
    is missing or cannot be read."""
    if stage not in VIEWS or not document.completed(stage):
        return None
    if document.directory is None:
        return None
    name, volatile = VIEWS[stage]
    found = report._json(document.directory / name)
    if found is None or isinstance(found, report._Unreadable):
        return None
    return content_hash(found, volatile=volatile)


def _batch(document: _Document) -> ClaimBatch | None:
    """The document's batch, when its extraction or GEDCOM stage
    completed; None when none did. ValueError when its view is missing
    or cannot be read."""
    stage = next(
        (s for s in ("extraction", "gedcom") if document.completed(s)), None
    )
    if stage is None:
        return None
    if document.directory is None:
        raise ValueError("no directory holds the document's views")
    try:
        text = (document.directory / VIEWS[stage][0]).read_text(
            encoding="utf-8"
        )
    except OSError as exc:
        raise ValueError(f"its batch cannot be read: {exc}") from None
    return ClaimBatch.model_validate_json(text)


# -- claims and mentions ------------------------------------------------------


def _excerpt(batch: ClaimBatch, index: int) -> str | None:
    excerpt = batch.claims[index].source.transcription_excerpt
    if excerpt is None:
        return None
    return unicodedata.normalize("NFC", excerpt.text)


@dataclass
class _Read:
    """What one document's batch gives: its claims' prints with their
    excerpts, its mentions by fingerprint with their kinds and labels;
    `unreadable` when its stage completed but its view is missing, or
    cannot be read into prints. An unreadable batch counts on neither
    side, so its claims never read as added or removed."""

    prints: list[tuple[ClaimPrint, str | None]] = field(default_factory=list)
    mentions: dict[str, tuple[str, str]] = field(default_factory=dict)
    unreadable: bool = False


def _read_batch(document: _Document) -> _Read | None:
    read = _Read()
    try:
        batch = _batch(document)
        if batch is None:
            return None
        prints = claim_prints(document.sha1, batch)
        mentions = mentions_of(document.sha1, batch)
    except (TypeError, ValueError):
        read.unreadable = True
        return read
    read.prints = [
        (found, _excerpt(batch, index)) for index, found in enumerate(prints)
    ]
    read.mentions = {
        mention.fingerprint: (mention.kind, mention.label)
        for mention in mentions
    }
    return read


def _listed(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_listed(item) for item in value]
    return value


def _claim_record(found: ClaimPrint, labels: Mapping[str, str]) -> dict:
    """A claim as diff.json lists it: its fingerprint and components,
    and its subject's label where the subject is a mention."""
    subject = _listed(found.subject)
    record = {
        "fingerprint": found.fingerprint,
        "property": found.property,
        "subject": subject,
        "value": _listed(found.value),
        "qualifiers": _listed(found.qualifiers),
        "locator": _listed(found.locator),
    }
    if found.subject and found.subject[0] == "mention":
        record["subject_label"] = labels.get(found.subject[1])
    return record


def _changed_parts(a: ClaimPrint, b: ClaimPrint) -> tuple[str, ...]:
    return tuple(
        part for part in CLAIM_PARTS if getattr(a, part) != getattr(b, part)
    )


@dataclass
class ClaimChanges:
    """One document's claims, compared: those matched by fingerprint,
    added, removed and changed (a pair sharing subject, property and
    artifact), and the matched claims whose excerpt differs."""

    matched: int = 0
    added: list[ClaimPrint] = field(default_factory=list)
    removed: list[ClaimPrint] = field(default_factory=list)
    changed: list[tuple[ClaimPrint, ClaimPrint, tuple[str, ...]]] = field(
        default_factory=list
    )
    excerpts: int = 0


def compare_claims(
    baseline: list[tuple[ClaimPrint, str | None]],
    run: list[tuple[ClaimPrint, str | None]],
) -> ClaimChanges:
    """Two readings of a document's claims, compared as multisets of
    fingerprints. Of the claims left unmatched, a removed and an added
    one that share subject, property and artifact are paired as changed,
    each with the one of its group it differs from in the fewest parts
    (the first by fingerprint of those that tie), taking the removed
    ones by fingerprint, so the pairs are the same whatever order the
    batches list their claims in."""
    changes = ClaimChanges()
    before: dict[str, list[tuple[ClaimPrint, str | None]]] = {}
    after: dict[str, list[tuple[ClaimPrint, str | None]]] = {}
    for found, excerpt in baseline:
        before.setdefault(found.fingerprint, []).append((found, excerpt))
    for found, excerpt in run:
        after.setdefault(found.fingerprint, []).append((found, excerpt))
    removed: list[ClaimPrint] = []
    added: list[ClaimPrint] = []
    for fingerprint in [*before, *(f for f in after if f not in before)]:
        old = before.get(fingerprint, [])
        new = after.get(fingerprint, [])
        matched = min(len(old), len(new))
        changes.matched += matched
        if matched:
            same = Counter(e for _, e in old) & Counter(e for _, e in new)
            changes.excerpts += max(0, matched - same.total())
        removed.extend(found for found, _ in old[matched:])
        added.extend(found for found, _ in new[matched:])
    # sorted by fingerprint, so the pairs do not depend on the order the
    # batches list their claims in
    removed.sort(key=lambda found: found.fingerprint)
    groups: dict[tuple, list[ClaimPrint]] = {}
    for found in sorted(added, key=lambda found: found.fingerprint):
        groups.setdefault(
            (found.artifact, found.subject, found.property), []
        ).append(found)
    for found in removed:
        group = groups.get((found.artifact, found.subject, found.property))
        if not group:
            changes.removed.append(found)
            continue
        partner = min(
            group,
            key=lambda other: (
                len(_changed_parts(found, other)),
                other.fingerprint,
            ),
        )
        group.remove(partner)
        changes.changed.append(
            (found, partner, _changed_parts(found, partner))
        )
    changes.added = [found for group in groups.values() for found in group]
    order = {id(found): index for index, found in enumerate(added)}
    changes.added.sort(key=lambda found: order[id(found)])
    return changes


# -- clusters -----------------------------------------------------------------


def _identity_map(path: Path) -> dict | None:
    return read_identity_map(path / CORPUS_DIR / CLUSTERING / IDENTITY_MAP)


def compare_clusters(
    baseline: Mapping[str, Any] | None,
    run: Mapping[str, Any] | None,
    names: _Names,
) -> tuple[dict, dict]:
    """Two identity maps, compared by membership over the mentions both
    hold: (the aggregates, the detail). A cluster of the run whose
    mentions come from several clusters of the baseline is a merge; one
    of the baseline whose mentions go to several of the run's, a split.
    Not available when either run has no map to read."""
    if baseline is None or run is None:
        missing = [
            side
            for side, found in (("baseline", baseline), ("run", run))
            if found is None
        ]
        return (
            {
                "status": NOT_AVAILABLE,
                "why": "no identity map to read in the "
                + " or the ".join(missing),
            },
            {},
        )
    old: Mapping[str, Any] = baseline["mentions"]
    new: Mapping[str, Any] = run["mentions"]

    def cluster(mentions: Mapping[str, Any], fingerprint: str) -> str | None:
        record = mentions.get(fingerprint)
        found = record.get("cluster") if isinstance(record, dict) else None
        return found if isinstance(found, str) else None

    def field_of(record: Any, name: str) -> Any:
        return record.get(name) if isinstance(record, dict) else None

    common = sorted(set(old) & set(new))
    went: dict[str, set[str]] = {}
    came: dict[str, set[str]] = {}
    for fingerprint in common:
        a, b = cluster(old, fingerprint), cluster(new, fingerprint)
        if a is None or b is None:
            continue
        went.setdefault(a, set()).add(b)
        came.setdefault(b, set()).add(a)
    merges = sorted(b for b, sources in came.items() if len(sources) > 1)
    splits = sorted(a for a, targets in went.items() if len(targets) > 1)

    def kind_of(clusters: Mapping[str, Any], cid: str) -> str:
        return names.kind(field_of(clusters.get(cid), "kind"))

    def method_of(clusters: Mapping[str, Any], cid: str) -> str:
        return report._known(field_of(clusters.get(cid), "method"), METHODS)

    methods: Counter = Counter()
    for fingerprint in common:
        a = field_of(old[fingerprint], "method")
        b = field_of(new[fingerprint], "method")
        if a != b:
            methods[
                f"{report._known(a, METHODS)} → {report._known(b, METHODS)}"
            ] += 1
    kept = set(baseline["clusters"]) & set(run["clusters"])

    def members(clusters: Mapping[str, Any], cid: str) -> list:
        found = field_of(clusters.get(cid), "members")
        return found if isinstance(found, list) else []

    aggregate = {
        "status": "compared",
        "same_decisions": baseline.get("hash") == run.get("hash"),
        "mentions": {
            "baseline": len(old),
            "run": len(new),
            "both": len(common),
            "baseline_only": len(set(old) - set(new)),
            "run_only": len(set(new) - set(old)),
        },
        "clusters": {
            "baseline": len(baseline["clusters"]),
            "run": len(run["clusters"]),
            "unchanged": len(kept),
        },
        "merges": len(merges),
        "merges_by_kind": report._by_count(
            Counter(kind_of(run["clusters"], cid) for cid in merges)
        ),
        "mentions_merged": sum(
            len(members(run["clusters"], c)) for c in merges
        ),
        # how each merge's cluster came about in the run: confident links,
        # or an adjudicator's verdicts
        "merges_by_method": report._by_count(
            Counter(method_of(run["clusters"], cid) for cid in merges)
        ),
        "splits": len(splits),
        "splits_by_kind": report._by_count(
            Counter(kind_of(baseline["clusters"], cid) for cid in splits)
        ),
        "mentions_split": sum(
            len(members(baseline["clusters"], c)) for c in splits
        ),
        "methods_changed": report._by_count(methods),
    }

    def described(mentions: Mapping[str, Any], fingerprint: str) -> dict:
        record = mentions.get(fingerprint)
        return {
            "fingerprint": fingerprint,
            "document": field_of(record, "document"),
            "label": field_of(record, "label"),
            "kind": field_of(record, "kind"),
        }

    detail = {
        "merges": [
            {
                "cluster": cid,
                "method": method_of(run["clusters"], cid),
                "from": sorted(came[cid]),
                "members": [
                    described(new, m) for m in members(run["clusters"], cid)
                ],
            }
            for cid in merges
        ],
        "splits": [
            {
                "cluster": cid,
                "into": sorted(went[cid]),
                "members": [
                    described(old, m)
                    for m in members(baseline["clusters"], cid)
                ],
            }
            for cid in splits
        ],
    }
    return aggregate, detail


# -- anomalies ----------------------------------------------------------------


def findings(path: Path, documents: Iterable[str]) -> set[tuple] | None:
    """Each finding of the run's alerts once: (a document's SHA-1, or the
    subject as the alert gives it when it names no planned document, a
    stage, a check). None when alerts.jsonl cannot be read."""
    known = sorted(documents)
    found: set[tuple] = set()
    try:
        for alert in report._lines(path / ALERTS):
            subject = report._text(alert.get("subject")) or ""
            sha1 = report._match(subject, known)
            found.add(
                (
                    sha1 or subject,
                    report._text(alert.get("stage")) or "?",
                    report._text(alert.get("check")) or "?",
                )
            )
    except OSError:
        return None
    return found


def _stage_of_finding(stage: str, names: _Names) -> str:
    if stage in (report.RUN, report.WRITE):
        return stage
    return names.stage(stage)


def compare_findings(
    baseline: set[tuple] | None, run: set[tuple] | None, names: _Names
) -> tuple[dict, dict]:
    if baseline is None or run is None:
        return (
            {
                "status": NOT_AVAILABLE,
                "why": f"{ALERTS} cannot be read in one of the runs",
            },
            {},
        )
    new, resolved, both = run - baseline, baseline - run, run & baseline

    def by_check(found: set[tuple]) -> dict[str, int]:
        return report._folded(
            Counter(check for _, _, check in found),
            lambda value: report._known(value, report.CHECKS),
        )

    def by_stage(found: set[tuple]) -> dict[str, int]:
        return report._by_count(
            Counter(_stage_of_finding(stage, names) for _, stage, _ in found)
        )

    aggregate = {
        "status": "compared",
        "baseline": len(baseline),
        "run": len(run),
        "new": len(new),
        "resolved": len(resolved),
        "in_both": len(both),
        "new_by_check": by_check(new),
        "resolved_by_check": by_check(resolved),
        "new_by_stage": by_stage(new),
        "resolved_by_stage": by_stage(resolved),
    }
    detail = {
        "new": [list(finding) for finding in sorted(new)],
        "resolved": [list(finding) for finding in sorted(resolved)],
    }
    return aggregate, detail


# -- flags and contradictions -------------------------------------------------


def _counted_section(
    baseline: Mapping[str, Any], run: Mapping[str, Any], name: str
) -> dict:
    """A content section only some reports count (flags, contradictions):
    its figures side by side when both reports count it, else not
    available, with why."""
    sections = []
    for aggregate in (baseline, run):
        content = aggregate.get("content")
        section = content.get(name) if isinstance(content, dict) else None
        sections.append(section if isinstance(section, dict) else None)
    whys = []
    for side, section in zip(("baseline", "run"), sections, strict=True):
        if section is None:
            whys.append(f"the {side}'s report does not hold them")
        elif section.get("status") == NOT_AVAILABLE:
            whys.append(f"the {side}'s report does not count them")
    if whys:
        return {"status": NOT_AVAILABLE, "why": "; ".join(whys)}
    found = (_Figures(), _Figures())
    for figures_of, section in zip(found, sections, strict=True):
        figures_of.read(section, (name,))
    return {"status": "compared", "figures": _compare(found)}


# -- stages -------------------------------------------------------------------


def _how(record: Any) -> str | None:
    """How a document's stage came about, as the report gives it: its
    status, and for a completed one, `computed`, or `reused` with the
    reasons it was reused by (`reused: key match`)."""
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    if status in COMPLETED:
        how = record.get("how")
        if how in (None, report.COMPUTED):
            return report.COMPUTED
        return f"{report.REUSED}: {report._joined(how, report.REUSE_REASONS)}"
    return report._known(status, report.STATUSES)


def attribute(
    calls: list[_Call],
    baseline_calls: list[_Call],
    components: Components,
    forced: Any,
) -> tuple[str, Counter]:
    """What sent a computed stage to the engine: the components of each
    missed call's key that differ from the closest of the baseline's
    calls for the same document and stage (the one differing in the
    fewest), or the miss's reason when the answer was there but could
    not serve (`rejected`, `refresh`). Returns the stage's attributions
    joined, and the calls by attribution."""
    by_call: Counter = Counter()
    baseline = [
        c for c in (components(call.key) for call in baseline_calls) if c
    ]
    for call in calls:
        if call.event == "cache-replay":
            if call.matches is False:
                by_call[report.REPLAY_CHANGED] += 1
            continue
        if call.event != "cache-miss":
            continue
        if call.reason in ("rejected", "refresh"):
            by_call[call.reason] += 1
            continue
        found = components(call.key)
        if found is None:
            by_call[UNREAD] += 1
            continue
        if not baseline_calls:
            by_call[NO_BASELINE] += 1
            continue
        if not baseline:
            by_call[UNREAD] += 1
            continue
        closest = min(
            (
                sorted(
                    name
                    for name in set(found) | set(other)
                    if found.get(name) != other.get(name)
                )
                for other in baseline
            ),
            key=len,
        )
        if not closest:
            by_call[SAME_KEY] += 1
        for name in closest:
            by_call[report._known(name, COMPONENTS)] += 1
    if not by_call:
        shown = (
            forced if forced in (report.NO_CALL, report.NO_CACHE) else UNREAD
        )
        return shown, Counter()
    return " + ".join(sorted(by_call)), by_call


def compare_stages(
    documents: tuple[dict[str, _Document], dict[str, _Document]],
    calls: tuple[dict, dict],
    components: Components,
    names: _Names,
) -> tuple[dict, dict]:
    """Per stage: how each document came about in each run, what forced
    each recompute of the run, and whether each view changed."""
    old, new = documents
    rows: dict[str, dict[str, Any]] = {}
    detail: dict[str, dict[str, Any]] = {}
    for sha1 in [*old, *(s for s in new if s not in old)]:
        a, b = old.get(sha1), new.get(sha1)
        stages = dict.fromkeys(
            [*(a.stages if a else {}), *(b.stages if b else {})]
        )
        for stage in stages:
            name = names.stage(stage)
            row = rows.setdefault(
                name,
                {
                    "documents": 0,
                    "baseline": Counter(),
                    "run": Counter(),
                    "transitions": Counter(),
                    "recomputed_by": Counter(),
                    "calls_by": Counter(),
                    "views": Counter(),
                },
            )
            row["documents"] += 1
            before = _how(a.stages.get(stage)) if a else None
            after = _how(b.stages.get(stage)) if b else None
            row["baseline"][before or "not planned"] += 1
            row["run"][after or "not planned"] += 1
            if before != after:
                row["transitions"][
                    f"{before or 'not planned'} → {after or 'not planned'}"
                ] += 1
            entry: dict[str, Any] = {"baseline": before, "run": after}
            if after == report.COMPUTED:
                record = b.stages.get(stage) if b else None
                joined, by_call = attribute(
                    calls[1].get((sha1, stage), []),
                    calls[0].get((sha1, stage), []),
                    components,
                    record.get("forced") if isinstance(record, dict) else None,
                )
                row["recomputed_by"][joined] += 1
                row["calls_by"].update(by_call)
                entry["recomputed_by"] = joined
            if stage in VIEWS:
                hashes = (
                    _view_hash(a, stage) if a else None,
                    _view_hash(b, stage) if b else None,
                )
                view = (
                    "neither"
                    if hashes == (None, None)
                    else "baseline only"
                    if hashes[1] is None
                    else "run only"
                    if hashes[0] is None
                    else "same"
                    if hashes[0] == hashes[1]
                    else "changed"
                )
                row["views"][view] += 1
                entry["view"] = view
            detail.setdefault(sha1, {})[stage] = entry
    aggregate = {
        name: {
            "documents": row["documents"],
            "baseline": report._by_count(row["baseline"]),
            "run": report._by_count(row["run"]),
            "transitions": report._by_count(row["transitions"]),
            "recomputed_by": report._by_count(row["recomputed_by"]),
            "calls_by": report._by_count(row["calls_by"]),
            "views": report._by_count(row["views"]),
        }
        for name, row in rows.items()
    }
    return aggregate, detail


# -- the diff -----------------------------------------------------------------


def _run_record(path: Path, manifest: Mapping[str, Any], source: str) -> dict:
    """A run as the aggregates name it: its id, its commit and whether
    the code differed from it, the same of the code an invocation took it
    past its barrier on (`scratch.code`, none when no invocation did),
    whether it went to the end, and whether its report was stored or
    built."""
    code = manifest.get("code")
    code = code if isinstance(code, dict) else {}
    scratch = manifest.get("scratch")
    later = scratch.get("code") if isinstance(scratch, dict) else None
    later = later if isinstance(later, dict) else {}
    run_id = manifest.get("run_id")
    return {
        "run_id": run_id
        if isinstance(run_id, str) and report._RUN_ID.fullmatch(run_id)
        else OTHER,
        "commit": report._hexed(code.get("commit")),
        "dirty": code.get("dirty")
        if isinstance(code.get("dirty"), bool)
        else None,
        "past_barrier_commit": report._hexed(later.get("commit")),
        "past_barrier_dirty": later.get("dirty")
        if isinstance(later.get("dirty"), bool)
        else None,
        "complete": is_complete(path),
        "report": source,
    }


def _cache_folder(manifest: Mapping[str, Any]) -> Path | None:
    cache = manifest.get("cache")
    return Path(cache) if isinstance(cache, str) and cache else None


def build(
    baseline: Path | str,
    run: Path | str,
    *,
    cache: Path | None = None,
    rebuild: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The diff of the run directory `run` against `baseline`, as
    diff.json holds it: its `aggregate` (what diff.md shows) and its
    `detail`, per document. `cache` is the LLM cache whose entries'
    components attribute each recompute; by default the one the run's
    manifest names. DiffError when either directory holds no run."""
    paths = (Path(baseline), Path(run))
    for path in paths:
        if not (path / MANIFEST).is_file():
            raise DiffError(f"{path}: no {MANIFEST} there, so no run to diff")
    now = now or datetime.now(UTC)
    manifests = tuple(_manifest(path) for path in paths)
    reports = tuple(read_report(path, rebuild=rebuild) for path in paths)
    aggregates = tuple(found["aggregate"] for found, _ in reports)
    names = _Names.of(*manifests)
    documents = tuple(
        _documents(path, found)
        for path, (found, _) in zip(paths, reports, strict=True)
    )
    folder = cache if cache is not None else _cache_folder(manifests[1])
    components = Components(folder)
    stages, stage_detail = compare_stages(
        documents,
        tuple(keyed_calls(path) for path in paths),
        components,
        names,
    )
    claims, mentions, claim_detail = _compare_documents(documents, names)
    clusters, cluster_detail = compare_clusters(
        *(_identity_map(path) for path in paths), names
    )
    anomalies, anomaly_detail = compare_findings(
        *(
            findings(path, docs)
            for path, docs in zip(paths, documents, strict=True)
        ),
        names,
    )
    failures, failure_detail = _compare_failures(documents, aggregates, names)
    caches = tuple(_cache_folder(manifest) for manifest in manifests)
    aggregate = {
        "format": FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "runs": {
            "baseline": _run_record(paths[0], manifests[0], reports[0][1]),
            "run": _run_record(paths[1], manifests[1], reports[1][1]),
            "same_cache": None
            if None in caches
            else os.path.realpath(caches[0]) == os.path.realpath(caches[1]),
            "components": "read" if components.available else NOT_AVAILABLE,
        },
        "settings": settings(*manifests),
        "stages": stages,
        "claims": claims,
        "mentions": mentions,
        "clusters": clusters,
        "anomalies": anomalies,
        "failures": failures,
        "flags": _counted_section(*aggregates, "flags"),
        "contradictions": _counted_section(*aggregates, "contradictions"),
        "figures": figures(*aggregates),
    }
    detail = {
        "baseline": str(paths[0]),
        "run": str(paths[1]),
        "cache": str(folder) if folder is not None else None,
        # the settings that differ and diff.md only counts, by name: their
        # values are in the two run.json files
        "settings_not_shown": [
            name for name, _, _ in _differing(*manifests) if name not in _SHOWN
        ],
        "documents": {
            sha1: {
                "files": (
                    documents[1].get(sha1) or documents[0].get(sha1)
                ).files,
                "stages": stage_detail.get(sha1, {}),
                **claim_detail.get(sha1, {}),
                **failure_detail.get(sha1, {}),
            }
            for sha1 in sorted(
                set(stage_detail) | set(claim_detail) | set(failure_detail)
            )
            if sha1 in documents[0] or sha1 in documents[1]
        },
        "anomalies": anomaly_detail,
        "clusters": cluster_detail,
    }
    return {"format": FORMAT, "aggregate": aggregate, "detail": detail}


def _compare_documents(
    documents: tuple[dict[str, _Document], dict[str, _Document]],
    names: _Names,
) -> tuple[dict, dict, dict]:
    """The claims and mentions of every document either run extracted:
    (the claims' aggregates, the mentions' aggregates, the detail per
    document)."""
    old, new = documents
    totals: Counter = Counter()
    by_property: dict[str, Counter] = {}
    changed_parts: Counter = Counter()
    mention_counts: Counter = Counter()
    mention_kinds: dict[str, Counter] = {
        "added": Counter(),
        "removed": Counter(),
    }
    document_counts: Counter = Counter()
    detail: dict[str, dict] = {}
    for sha1 in [*old, *(s for s in new if s not in old)]:
        reads = (
            _read_batch(old[sha1]) if sha1 in old else None,
            _read_batch(new[sha1]) if sha1 in new else None,
        )
        if reads == (None, None):
            continue
        if any(read is not None and read.unreadable for read in reads):
            document_counts["unreadable"] += 1
            continue
        a, b = (read or _Read() for read in reads)
        if reads[0] is None:
            document_counts["run only"] += 1
        elif reads[1] is None:
            document_counts["baseline only"] += 1
        else:
            document_counts["both"] += 1
        changes = compare_claims(a.prints, b.prints)
        totals["baseline"] += len(a.prints)
        totals["run"] += len(b.prints)
        totals["matched"] += changes.matched
        totals["added"] += len(changes.added)
        totals["removed"] += len(changes.removed)
        totals["changed"] += len(changes.changed)
        totals["excerpt_changed"] += changes.excerpts
        for found in changes.added:
            by_property.setdefault(names.property(found.property), Counter())[
                "added"
            ] += 1
        for found in changes.removed:
            by_property.setdefault(names.property(found.property), Counter())[
                "removed"
            ] += 1
        for _, found, parts in changes.changed:
            by_property.setdefault(names.property(found.property), Counter())[
                "changed"
            ] += 1
            changed_parts[" + ".join(parts) or "none"] += 1
        if changes.added or changes.removed or changes.changed:
            document_counts["with claim changes"] += 1
        added = sorted(set(b.mentions) - set(a.mentions))
        removed = sorted(set(a.mentions) - set(b.mentions))
        mention_counts["baseline"] += len(a.mentions)
        mention_counts["run"] += len(b.mentions)
        mention_counts["added"] += len(added)
        mention_counts["removed"] += len(removed)
        mention_kinds["added"].update(
            names.kind(b.mentions[m][0]) for m in added
        )
        mention_kinds["removed"].update(
            names.kind(a.mentions[m][0]) for m in removed
        )
        labels = {
            fingerprint: label
            for fingerprint, (_, label) in {**a.mentions, **b.mentions}.items()
        }
        if not (
            changes.added
            or changes.removed
            or changes.changed
            or changes.excerpts
            or added
            or removed
        ):
            continue
        detail[sha1] = {
            "claims": {
                "matched": changes.matched,
                "excerpt_changed": changes.excerpts,
                "added": [_claim_record(f, labels) for f in changes.added],
                "removed": [_claim_record(f, labels) for f in changes.removed],
                "changed": [
                    {
                        "parts": list(parts),
                        "baseline": _claim_record(before, labels),
                        "run": _claim_record(after, labels),
                    }
                    for before, after, parts in changes.changed
                ],
            },
            "mentions": {
                "added": [
                    {
                        "fingerprint": m,
                        "kind": b.mentions[m][0],
                        "label": b.mentions[m][1],
                    }
                    for m in added
                ],
                "removed": [
                    {
                        "fingerprint": m,
                        "kind": a.mentions[m][0],
                        "label": a.mentions[m][1],
                    }
                    for m in removed
                ],
            },
        }
    claims = {
        **{
            name: totals[name]
            for name in (
                "baseline",
                "run",
                "matched",
                "added",
                "removed",
                "changed",
                "excerpt_changed",
            )
        },
        "changed_parts": report._by_count(changed_parts),
        "by_property": {
            name: {key: counts[key] for key in ("added", "removed", "changed")}
            for name, counts in sorted(by_property.items())
        },
        "documents": {
            key: document_counts[key]
            for key in (
                "both",
                "baseline only",
                "run only",
                "unreadable",
                "with claim changes",
            )
        },
    }
    mentions = {
        **{
            name: mention_counts[name]
            for name in ("baseline", "run", "added", "removed")
        },
        "added_by_kind": report._by_count(mention_kinds["added"]),
        "removed_by_kind": report._by_count(mention_kinds["removed"]),
    }
    return claims, mentions, detail


def _compare_failures(
    documents: tuple[dict[str, _Document], dict[str, _Document]],
    aggregates: tuple[Mapping[str, Any], Mapping[str, Any]],
    names: _Names,
) -> tuple[dict, dict]:
    """The documents that fail in one run and not the other, by kind and
    by failure label, and the reports' failure counts side by side."""
    old, new = documents
    failing = {sha1 for sha1, document in old.items() if document.failed}
    now_failing = {sha1 for sha1, document in new.items() if document.failed}
    started = now_failing - failing
    stopped = failing - now_failing

    def kinds(found: set[str], side: dict[str, _Document]) -> dict[str, int]:
        return report._folded(
            Counter(side[sha1].kind for sha1 in found), report._kind
        )

    def labels(found: set[str], side: dict[str, _Document]) -> dict[str, int]:
        return report._folded(
            Counter(side[sha1].reason for sha1 in found), report._failure_label
        )

    counts = []
    for aggregate in aggregates:
        section = aggregate.get("documents")
        counts.append(
            _number(section.get("failed"))
            if isinstance(section, dict)
            else None
        )
    aggregate = {
        "baseline": ABSENT if counts[0] is None else counts[0],
        "run": ABSENT if counts[1] is None else counts[1],
        "new": len(started),
        "resolved": len(stopped),
        "in_both": len(failing & now_failing),
        "new_by_kind": kinds(started, new),
        "new_by_label": labels(started, new),
        "resolved_by_kind": kinds(stopped, old),
        "resolved_by_label": labels(stopped, old),
    }
    detail = {
        sha1: {
            "failed": {
                "baseline": old[sha1].reason if sha1 in failing else None,
                "run": new[sha1].reason if sha1 in now_failing else None,
            }
        }
        for sha1 in sorted(started | stopped)
    }
    return aggregate, detail


# -- diff.md ------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(map(_cell, value)) or "none"
    return str(value).replace("|", "\\|")


def _delta(value: Any) -> str:
    if value is None:
        return "n/a"
    number = _number(value)
    if number is None:
        return _cell(value)
    if number > 0:
        return f"+{number:g}"
    return f"{number:g}"


def _counts(counts: Mapping[str, Any]) -> str:
    return (
        ", ".join(f"{name} {_cell(count)}" for name, count in counts.items())
        or "none"
    )


def _table(head: tuple[str, ...], rows: Iterable[tuple]) -> list[str]:
    rows = list(rows)
    if not rows:
        return ["none", ""]
    lines = [
        "| " + " | ".join(head) + " |",
        "|" + "|".join("---" for _ in head) + "|",
    ]
    lines += ["| " + " | ".join(map(_cell, row)) + " |" for row in rows]
    return [*lines, ""]


def render_markdown(aggregate: Mapping[str, Any]) -> str:
    """diff.md: the aggregates alone, for an issue or a PR."""
    runs = aggregate["runs"]
    lines = [
        "# Run diff",
        "",
        "Aggregates only: counts, rates and the pipeline's own names, with "
        "no document, file, name, label, value, excerpt, quote or language. "
        f"Made by `yizkr scratch diff` at {aggregate['generated_at']}; the "
        "per-document diff (diff.json) stays local.",
        "",
    ]
    lines += _table(
        (
            "",
            "run id",
            "commit",
            "differs from commit",
            "past the barrier on",
            "which differs from its commit",
            "complete",
            "report",
        ),
        [
            (
                side,
                runs[side]["run_id"],
                runs[side]["commit"],
                runs[side]["dirty"],
                runs[side]["past_barrier_commit"],
                runs[side]["past_barrier_dirty"],
                runs[side]["complete"],
                runs[side]["report"],
            )
            for side in ("baseline", "run")
        ],
    )
    lines += [
        f"Same LLM cache: {_cell(runs['same_cache'])}. Key components: "
        f"{runs['components']}.",
        "",
        "## Settings that differ",
        "",
    ]
    shown, others = (
        aggregate["settings"]["shown"],
        aggregate["settings"]["other"],
    )
    if shown or not others:
        lines += _table(
            ("setting", "baseline", "run"),
            [(r["setting"], r["baseline"], r["run"]) for r in shown],
        )
    if others:
        lines += [f"Other settings that differ, not shown: {others}.", ""]
    lines += ["## Stages", ""]
    lines += _table(
        ("stage", "documents", "baseline", "run", "recomputed by", "views"),
        [
            (
                name,
                row["documents"],
                _counts(row["baseline"]),
                _counts(row["run"]),
                _counts(row["recomputed_by"]),
                _counts(row["views"]),
            )
            for name, row in aggregate["stages"].items()
        ],
    )
    lines += ["Recomputed calls by the key component that changed:", ""]
    lines += _table(
        ("stage", "calls"),
        [
            (name, _counts(row["calls_by"]))
            for name, row in aggregate["stages"].items()
            if row["calls_by"]
        ],
    )
    claims = aggregate["claims"]
    lines += ["## Claims", ""]
    lines += _table(
        (
            "baseline",
            "run",
            "matched",
            "added",
            "removed",
            "changed",
            "excerpt changed",
        ),
        [
            tuple(
                claims[key]
                for key in (
                    "baseline",
                    "run",
                    "matched",
                    "added",
                    "removed",
                    "changed",
                    "excerpt_changed",
                )
            )
        ],
    )
    lines += [
        f"Documents: {_counts(claims['documents'])}.",
        "",
        f"Changed claims by what changed: {_counts(claims['changed_parts'])}.",
        "",
    ]
    if claims["by_property"]:
        lines += _table(
            ("property", "added", "removed", "changed"),
            [
                (name, row["added"], row["removed"], row["changed"])
                for name, row in claims["by_property"].items()
            ],
        )
    mentions = aggregate["mentions"]
    lines += [
        "## Mentions",
        "",
        f"Baseline {mentions['baseline']}, run {mentions['run']}; added "
        f"{mentions['added']} ({_counts(mentions['added_by_kind'])}), "
        f"removed {mentions['removed']} "
        f"({_counts(mentions['removed_by_kind'])}).",
        "",
        "## Clusters",
        "",
    ]
    clusters = aggregate["clusters"]
    if clusters["status"] == NOT_AVAILABLE:
        lines += [f"Not available: {clusters['why']}.", ""]
    else:
        lines += [
            f"Same decisions: {_cell(clusters['same_decisions'])}. Mentions: "
            f"{_counts(clusters['mentions'])}. Clusters: "
            f"{_counts(clusters['clusters'])}.",
            "",
            f"Merges {clusters['merges']} "
            f"({_counts(clusters['merges_by_kind'])}; "
            f"{clusters['mentions_merged']} mentions; by method: "
            f"{_counts(clusters.get('merges_by_method') or {})}); splits "
            f"{clusters['splits']} ({_counts(clusters['splits_by_kind'])}; "
            f"{clusters['mentions_split']} mentions).",
            "",
            f"Mentions whose method changed: "
            f"{_counts(clusters['methods_changed'])}.",
            "",
        ]
    lines += ["## Anomalies", ""]
    anomalies = aggregate["anomalies"]
    if anomalies["status"] == NOT_AVAILABLE:
        lines += [f"Not available: {anomalies['why']}.", ""]
    else:
        lines += [
            f"Findings: baseline {anomalies['baseline']}, run "
            f"{anomalies['run']}; new {anomalies['new']}, resolved "
            f"{anomalies['resolved']}, in both {anomalies['in_both']}.",
            "",
            f"New by check: {_counts(anomalies['new_by_check'])}. Resolved "
            f"by check: {_counts(anomalies['resolved_by_check'])}.",
            "",
            f"New by stage: {_counts(anomalies['new_by_stage'])}. Resolved "
            f"by stage: {_counts(anomalies['resolved_by_stage'])}.",
            "",
        ]
    failures = aggregate["failures"]
    lines += [
        "## Failures",
        "",
        f"Documents failed: baseline {_cell(failures['baseline'])}, run "
        f"{_cell(failures['run'])}; newly failing {failures['new']} "
        f"({_counts(failures['new_by_label'])}), no longer failing "
        f"{failures['resolved']} ({_counts(failures['resolved_by_label'])}), "
        f"in both {failures['in_both']}.",
        "",
        f"Newly failing by kind: {_counts(failures['new_by_kind'])}. No "
        f"longer failing by kind: {_counts(failures['resolved_by_kind'])}.",
        "",
    ]
    for title, key in (
        ("Flags", "flags"),
        ("Contradictions", "contradictions"),
    ):
        section = aggregate[key]
        lines += [f"## {title}", ""]
        if section["status"] == NOT_AVAILABLE:
            lines += [f"Not available: {section['why']}.", ""]
        else:
            lines += _table(
                ("figure", "baseline", "run", "delta"),
                [
                    (r["figure"], r["baseline"], r["run"], _delta(r["delta"]))
                    for r in section["figures"]
                ],
            )
    lines += ["## Place hierarchy", ""]
    places = [
        row
        for row in aggregate["figures"]
        if row["figure"].startswith(PLACE_FIGURES)
    ]
    if places:
        lines += _table(
            ("figure", "baseline", "run", "delta"),
            [
                (
                    row["figure"].removeprefix(PLACE_FIGURES),
                    row["baseline"],
                    row["run"],
                    _delta(row["delta"]),
                )
                for row in places
            ],
        )
    else:
        lines += [
            "Not available: neither run's place stage has counted its "
            "questions.",
            "",
        ]
    lines += [
        "## Figures that differ",
        "",
        f"Of the reports' figures, those that differ or that one report "
        f"does not hold ({ABSENT}: never counted as 0).",
        "",
    ]
    lines += _table(
        ("figure", "baseline", "run", "delta"),
        [
            (row["figure"], row["baseline"], row["run"], _delta(row["delta"]))
            for row in aggregate["figures"]
            if row["baseline"] != row["run"]
        ],
    )
    return "\n".join(lines).rstrip() + "\n"


# -- writing ------------------------------------------------------------------


def render_json(diff: Mapping[str, Any]) -> str:
    """diff.json: everything the diff holds."""
    return json.dumps(diff, indent=1, ensure_ascii=False, default=str) + "\n"


def write_diff(
    baseline: Path | str,
    run: Path | str,
    *,
    cache: Path | None = None,
    rebuild: bool = False,
    now: datetime | None = None,
) -> dict[str, Path]:
    """Build the diff of `run` against `baseline` and write diff.json and
    diff.md into `<run>/.diff/<baseline's directory name>/`, each folder
    private to its owner and each file replaced whole, as the report
    writes its own. DiffError for a folder that cannot be made or
    written. Returns each file's path by its name."""
    baseline, run = Path(baseline), Path(run)
    diff = build(baseline, run, cache=cache, rebuild=rebuild, now=now)
    texts = {
        JSON_FILE: render_json(diff),
        MARKDOWN_FILE: render_markdown(diff["aggregate"]),
    }
    folder = run / DIFF_DIR / baseline.resolve().name
    try:
        os.close(report._private_folder(run / DIFF_DIR))
        descriptor = report._private_folder(folder)
    except report.ReportError as exc:
        raise DiffError(str(exc)) from None
    try:
        for name, text in texts.items():
            try:
                report._write_in(descriptor, name, text)
            except OSError as exc:
                raise DiffError(
                    f"{folder / name} cannot be written: {exc}"
                ) from None
    finally:
        os.close(descriptor)
    return {name: folder / name for name in texts}
