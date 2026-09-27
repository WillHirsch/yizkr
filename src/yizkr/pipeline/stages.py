"""Stage store: every stage output, keyed by what it was computed from.

A run recomputes only what changed (ADR-0025). Each stage output is a
record stored under a key, the SHA-256 of the key's components:

* the input content: the media SHA-1 for a first stage; after that, the
  canonical content hash of each upstream output, with volatile fields
  (timestamps, engine version strings) left out, so an identical
  recompute changes no downstream key;
* the stage name and its declared version, and for a routed stage the
  chosen method and that method's version (yizkr.pipeline.stageversions);
* the hashes of the prompt templates and of the model manifest it uses;
* the model: engine, model id, effort, and any parameters. When
  configuration names no model, the key holds UNPINNED_MODEL in place of
  the engine's own default, which it cannot see (model_spec).

Nothing is keyed on QIDs, file modification times or timestamps. An
artifact QID that a stage's prompt or output carries is a binding, not an
input: the record notes the QID it was computed under, content hashes
abstract it, and a reused output is rebound to the document's current QID.

An output is reused only when its key matches exactly, or when a recorded
adoption maps the current key to it (an imported transcription, say); the
reuse reason says which. Records are written atomically: a record
directory appears only by renaming a fully written staging directory, so
an interrupted run never leaves a partial output that looks complete.
Failures go to an append-only log per stage that no later success
rewrites, and a failed key has no record, so the next run retries it.

Layout under the store root (runtime state, kept under var/):

    doc/<media sha1>/<stage>/<key>/record.json    components, output hash,
                                                  provenance, attention
                                  /output.json    the parsed output
                                  /raw/0001.txt   every LLM response, in order
                                  /extras/<name>  advisory side outputs
    doc/<media sha1>/<stage>/failures.jsonl       append-only failure log
    doc/<media sha1>/<stage>/adopted/<key>.json   recorded equivalences
    corpus/<stage>/...                            corpus-level stages

docs/STAGE_STORE.md describes the layout, the run directory beside it and
how run state is derived from both.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

STORE_FORMAT = 1
CORPUS_SCOPE = "corpus"

# the two reasons an output is reused
KEY_MATCH = "key match"
ADOPTED = "adopted"
# ...and why one was stored
COMPUTED = "computed"
RECOMPUTED = "recomputed"

# stands in for the artifact QID when an output is hashed, so a content
# hash never depends on which QID the artifact happens to have
ARTIFACT_PLACEHOLDER = "{artifact}"

# the model a key names when configuration names none: the engine then
# runs a default model of its own choosing, which the key cannot see
UNPINNED_MODEL = "engine default"

# the components a key can hold, in report order
KEY_COMPONENTS = (
    "inputs",
    "version",
    "method",
    "method_version",
    "prompts",
    "manifest",
    "model",
    "params",
    "depends",
)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_EXTRA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# -- hashing ------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """The one serialisation every hash is taken over."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha1_file(path: Path) -> str:
    """The media SHA-1: a document's content identity (ADR-0010)."""
    return _file_digest(path, "sha1")


def sha256_file(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or None when it does not exist."""
    if not path.is_file():
        return None
    return _file_digest(path, "sha256")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def strip_volatile(value: Any, volatile: Iterable[str]) -> Any:
    """A copy of `value` without the volatile fields.

    Each entry is a dotted path into nested objects ("extraction_run.
    started_at"); a list on the way fans the rest of the path out over
    its elements."""
    copy = _json_copy(value)
    for path in volatile:
        _drop(copy, path.split("."))
    return copy


def _drop(node: Any, parts: list[str]) -> None:
    if isinstance(node, list):
        for element in node:
            _drop(element, parts)
        return
    if not isinstance(node, dict) or not parts or parts[0] not in node:
        return
    if len(parts) == 1:
        del node[parts[0]]
    else:
        _drop(node[parts[0]], parts[1:])


def rebind_artifact(value: Any, old: str | None, new: str | None) -> Any:
    """A copy of `value` with every QID reference to `old` pointing at `new`.

    A reference is a ``{"kind": "qid", "qid": ...}`` object, the form the
    claims schema gives an item. Nothing changes when `old` is unset or
    equal to `new`."""
    if not old or not new or old == new:
        return value

    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            copied = {key: visit(item) for key, item in node.items()}
            if copied.get("kind") == "qid" and copied.get("qid") == old:
                copied["qid"] = new
            return copied
        if isinstance(node, list):
            return [visit(item) for item in node]
        return node

    return visit(value)


def content_hash(
    value: Any,
    *,
    volatile: Iterable[str] = (),
    artifact_qid: str | None = None,
) -> str:
    """Canonical content hash of an output, as downstream keys see it:
    volatile fields left out, the artifact QID abstracted."""
    stripped = strip_volatile(value, volatile)
    if artifact_qid:
        stripped = rebind_artifact(
            stripped, artifact_qid, ARTIFACT_PLACEHOLDER
        )
    return sha256_text(canonical_json(stripped))


@lru_cache(maxsize=1)
def manifest_hash() -> str:
    """SHA-256 of the model manifest the prompts and validation use."""
    from yizkr.model.schema import read_manifest_data

    return sha256_text(read_manifest_data())


def model_spec(
    engine: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    **params: Any,
) -> dict:
    """The model component of a key, from configuration alone.

    `model` falls back to $YIZKR_LLM_MODEL, as the engines do. The engine
    is never started to ask: a run that reuses every output needs none.
    With neither, the model is keyed as UNPINNED_MODEL. The engine then
    runs its own default model, which the key does not name, so a change
    to that default leaves the key unchanged, and outputs the previous
    default made are reused (`ingest run` warns when this applies).
    """
    resolved = model or os.environ.get("YIZKR_LLM_MODEL") or UNPINNED_MODEL
    spec: dict[str, Any] = {"engine": engine, "model": resolved}
    if effort is not None:
        spec["effort"] = effort
    if params:
        spec["params"] = params
    return spec


# -- keys -----------------------------------------------------------------


@dataclass(frozen=True)
class StageKey:
    """The identity of one stage output: its components and their digest."""

    stage: str
    components_json: str

    @property
    def components(self) -> dict:
        return json.loads(self.components_json)

    @property
    def digest(self) -> str:
        return sha256_text(self.components_json)

    def __str__(self) -> str:
        return self.digest


def stage_key(
    stage: str,
    *,
    inputs: Mapping[str, Any],
    method: str | None = None,
    prompts: Mapping[str, str] | None = None,
    manifest: str | None = None,
    model: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    depends: Iterable[str] = (),
    registry: Mapping[str, Any] | None = None,
) -> StageKey:
    """Build a stage key.

    `inputs` maps each input to its content identity (a media SHA-1 or
    an upstream content hash). `prompts` maps names to template TEXT; the
    key holds their hashes. `depends` names other registered stages whose
    versions shape this output without being inputs (a parser the stage
    reuses). A routed stage must name its `method`; only that method's
    version enters the key, so a new or changed method leaves documents
    routed elsewhere untouched.
    """
    if registry is None:
        from yizkr.pipeline.stageversions import STAGES

        registry = STAGES
    spec = registry[stage]
    components: dict[str, Any] = {
        "stage": stage,
        "version": spec.version,
        "inputs": dict(inputs),
    }
    if spec.methods or method is not None:
        if method not in spec.methods:
            raise KeyError(f"stage {stage!r} has no method {method!r}")
        components["method"] = method
        components["method_version"] = spec.methods[method].version
    if prompts:
        components["prompts"] = {
            name: sha256_text(text) for name, text in sorted(prompts.items())
        }
    if manifest is not None:
        components["manifest"] = manifest
    if model is not None:
        components["model"] = dict(model)
    if params:
        components["params"] = dict(params)
    depends = sorted(set(depends))
    if depends:
        components["depends"] = {
            name: registry[name].version for name in depends
        }
    return StageKey(stage, canonical_json(components))


def changed_components(old: Mapping[str, Any], new: Mapping[str, Any]):
    """Names of the key components that differ between two keys: what
    forced a recompute."""
    return [name for name in KEY_COMPONENTS if old.get(name) != new.get(name)]


def doc_scope(sha1: str) -> str:
    """The store scope of one document, named by its media SHA-1."""
    if not _SHA1.match(sha1 or ""):
        raise ValueError(f"not a SHA-1: {sha1!r}")
    return f"doc/{sha1}"


def scope_subject(scope: str) -> str:
    """The subject an event names: a document's SHA-1, or 'corpus'."""
    return scope.removeprefix("doc/")


def subject_scope(subject: str) -> str:
    return CORPUS_SCOPE if subject == CORPUS_SCOPE else doc_scope(subject)


# -- records ----------------------------------------------------------------


@dataclass
class StageResult:
    """What a stage computation hands the store."""

    output: Any
    raw: Sequence[str] = ()
    extras: Mapping[str, str] = field(default_factory=dict)
    attention: Sequence[str] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)


class StageFailed(Exception):
    """A stage failure that carries detail for the failure log (the raw
    response of a truncated call, the number of attempts)."""

    def __init__(self, reason: str, **detail: Any) -> None:
        super().__init__(reason)
        self.detail = detail


class DispatchStopped(Exception):
    """The run's control file paused or aborted this stage's slice."""


class StageRecord:
    """One stored output; its files are read on demand."""

    def __init__(self, path: Path, record: dict) -> None:
        self.path = path
        self.record = record

    @property
    def key(self) -> str:
        return self.record["key"]

    @property
    def stage(self) -> str:
        return self.record["stage"]

    @property
    def components(self) -> dict:
        return self.record["components"]

    @property
    def output_hash(self) -> str:
        return self.record["output_sha256"]

    @property
    def attention(self) -> list[str]:
        return list(self.record.get("attention") or [])

    @property
    def meta(self) -> dict:
        return dict(self.record.get("meta") or {})

    def output(self) -> Any:
        return json.loads((self.path / "output.json").read_text())

    def bound_output(self, artifact_qid: str | None) -> Any:
        """The output with its artifact binding moved to `artifact_qid`."""
        return rebind_artifact(
            self.output(), self.meta.get("artifact_qid"), artifact_qid
        )

    def raw(self) -> list[str]:
        return [
            (self.path / name).read_text()
            for name in self.record.get("raw") or []
        ]

    def extra(self, name: str) -> str | None:
        if name not in (self.record.get("extras") or []):
            return None
        return (self.path / "extras" / name).read_text()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_synced(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step: readers see the old file or
    the new one, never a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_synced(staging, text)
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


class StageStore:
    """Stage outputs on disk, keyed and written atomically."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def stage_dir(self, scope: str, stage: str) -> Path:
        if scope != CORPUS_SCOPE and not (
            scope.startswith("doc/") and _SHA1.match(scope[4:])
        ):
            raise ValueError(f"not a store scope: {scope!r}")
        if not _NAME.match(stage):
            raise ValueError(f"not a stage name: {stage!r}")
        return self.root / scope / stage

    # -- reading ------------------------------------------------------------

    def get(self, scope: str, stage: str, key: StageKey | str):
        """The record stored under exactly this key, or None."""
        digest = str(key)
        if not _DIGEST.match(digest):
            return None
        path = self.stage_dir(scope, stage) / digest
        try:
            record = json.loads((path / "record.json").read_text())
        except (OSError, ValueError):
            return None
        if record.get("key") != digest:
            return None
        return StageRecord(path, record)

    def lookup(self, scope: str, stage: str, key: StageKey | str):
        """(record, reason) for a reusable output, or None: an exact key
        match, else a record a recorded adoption maps this key to."""
        record = self.get(scope, stage, key)
        if record is not None:
            return record, KEY_MATCH
        adoption = self.adoption(scope, stage, key)
        if adoption is not None:
            record = self.get(scope, stage, adoption["record"])
            if record is not None:
                return record, ADOPTED
        return None

    def records(self, scope: str, stage: str) -> list[StageRecord]:
        """Every complete record of a stage, oldest first."""
        stage_dir = self.stage_dir(scope, stage)
        if not stage_dir.is_dir():
            return []
        found = [
            record
            for path in stage_dir.iterdir()
            if _DIGEST.match(path.name)
            and (record := self.get(scope, stage, path.name)) is not None
        ]
        return sorted(
            found, key=lambda r: (r.record.get("created_at", ""), r.key)
        )

    def latest(
        self, scope: str, stage: str, *, item: str | None = None
    ) -> StageRecord | None:
        """The newest record of a stage (for one item, when given)."""
        candidates = [
            record
            for record in self.records(scope, stage)
            if item is None or record.meta.get("item") == item
        ]
        return candidates[-1] if candidates else None

    # -- writing -------------------------------------------------------------

    def put(
        self,
        scope: str,
        stage: str,
        key: StageKey,
        result: StageResult,
        *,
        volatile: Iterable[str] = (),
        artifact_qid: str | None = None,
        replace: bool = False,
    ) -> StageRecord:
        """Store an output under `key`, atomically.

        Everything is written into a staging directory first; the record
        appears only when that directory is renamed to the key. An
        existing record is kept unless `replace` (a forced recompute), in
        which case the new one takes its place in one rename.
        """
        stage_dir = self.stage_dir(scope, stage)
        stage_dir.mkdir(parents=True, exist_ok=True)
        final = stage_dir / key.digest
        if final.exists() and self.get(scope, stage, key) is None:
            replace = True  # a directory under the key with no record
        staging = stage_dir / f".staging-{uuid.uuid4().hex}"
        staging.mkdir()
        meta = dict(result.meta)
        if artifact_qid:
            meta["artifact_qid"] = artifact_qid
        try:
            _write_synced(
                staging / "output.json",
                json.dumps(result.output, indent=2, ensure_ascii=False),
            )
            raw_names = []
            if result.raw:
                (staging / "raw").mkdir()
                for index, text in enumerate(result.raw, start=1):
                    name = f"raw/{index:04d}.txt"
                    _write_synced(staging / name, text)
                    raw_names.append(name)
            if result.extras:
                (staging / "extras").mkdir()
                for name, text in result.extras.items():
                    if not _EXTRA.match(name):
                        raise ValueError(f"not an extras name: {name!r}")
                    _write_synced(staging / "extras" / name, text)
            record = {
                "format": STORE_FORMAT,
                "scope": scope,
                "stage": stage,
                "key": key.digest,
                "components": key.components,
                "output_sha256": content_hash(
                    result.output, volatile=volatile, artifact_qid=artifact_qid
                ),
                "raw": raw_names,
                "extras": sorted(result.extras),
                "attention": list(result.attention),
                "created_at": _now(),
                "meta": meta,
            }
            # record.json last: a staging directory without it is debris
            _write_synced(
                staging / "record.json",
                json.dumps(record, indent=2, ensure_ascii=False),
            )
            _fsync_dir(staging)
            self._publish(staging, final, replace=replace)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        stored = self.get(scope, stage, key)
        if stored is None:  # pragma: no cover - rename succeeded above
            raise RuntimeError(f"stage record {final} vanished after write")
        return stored

    def _publish(self, staging: Path, final: Path, *, replace: bool) -> None:
        if final.exists() and replace:
            retired = final.with_name(f".retired-{uuid.uuid4().hex}")
            os.rename(final, retired)
            os.rename(staging, final)
            shutil.rmtree(retired, ignore_errors=True)
        elif final.exists():
            # computed concurrently elsewhere: the first record stands
            shutil.rmtree(staging, ignore_errors=True)
        else:
            try:
                os.rename(staging, final)
            except OSError:
                if not final.exists():
                    raise
                shutil.rmtree(staging, ignore_errors=True)
        _fsync_dir(final.parent)

    def _append(self, path: Path, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    # -- failures ------------------------------------------------------------

    def fail(
        self,
        scope: str,
        stage: str,
        key: StageKey | str,
        reason: str,
        **detail: Any,
    ) -> dict:
        """Append a failure to the stage's log. Nothing is stored under the
        key, so the next run retries it; the reason stays in the log."""
        entry = {"key": str(key), "at": _now(), "reason": reason}
        entry.update({k: v for k, v in detail.items() if v is not None})
        self._append(self.stage_dir(scope, stage) / "failures.jsonl", entry)
        return entry

    def failures(
        self, scope: str, stage: str, key: StageKey | str | None = None
    ) -> list[dict]:
        path = self.stage_dir(scope, stage) / "failures.jsonl"
        entries = read_jsonl(path)
        if key is None:
            return entries
        return [entry for entry in entries if entry.get("key") == str(key)]

    # -- adoption -------------------------------------------------------------

    def adopt(
        self,
        scope: str,
        stage: str,
        key: StageKey,
        record: StageRecord,
        reason: str,
    ) -> dict:
        """Record that `record` stands for `key`: a visible equivalence.

        Used for outputs made elsewhere, such as imported transcriptions
        stored under their own backfilled key. The equivalence names one
        exact current key, so a version bump (a new key) drops it."""
        entry = {
            "key": key.digest,
            "components": key.components,
            "record": record.key,
            "record_components": record.components,
            "reason": reason,
            "at": _now(),
        }
        path = self.stage_dir(scope, stage) / "adopted" / f"{key.digest}.json"
        atomic_write_text(path, json.dumps(entry, indent=2))
        return entry

    def adoption(self, scope: str, stage: str, key: StageKey | str):
        digest = str(key)
        if not _DIGEST.match(digest):
            return None
        path = self.stage_dir(scope, stage) / "adopted" / f"{digest}.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None


def read_jsonl(path: Path) -> list[dict]:
    """Entries of an append-only log; a torn last line is skipped."""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


# -- running a stage ----------------------------------------------------------


@dataclass
class Outcome:
    """How a stage output was obtained in this run."""

    record: StageRecord
    reused: bool
    reason: str  # key match | adopted | computed | recomputed


class Recompute:
    """Stages forced to recompute once per run, shared by every scope."""

    def __init__(self, stages: Collection[str] = ()) -> None:
        self.stages = frozenset(stages)
        self._done: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def forced(self, scope: str, stage: str, key: StageKey) -> bool:
        with self._lock:
            return (
                stage in self.stages
                and (scope, stage, key.digest) not in self._done
            )

    def done(self, scope: str, stage: str, key: StageKey) -> None:
        with self._lock:
            self._done.add((scope, stage, key.digest))


class StageScope:
    """Runs stages for one scope: reuse by key, else compute and store.

    `events` is the run directory (yizkr.pipeline.rundir.RunDir) that
    receives an event for every reuse, start, store and failure, and whose
    control file can hold a stage or a document kind; None runs silently.
    """

    def __init__(
        self,
        store: StageStore,
        scope: str,
        *,
        events: Any = None,
        recompute: Recompute | Collection[str] = (),
        kind: str | None = None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.events = events
        self.recompute = (
            recompute
            if isinstance(recompute, Recompute)
            else Recompute(recompute)
        )
        self.kind = kind

    def failures(self, stage: str, key: StageKey) -> list[dict]:
        return self.store.failures(self.scope, stage, key)

    def emit(self, event: str, stage: str, key: StageKey | None, **fields):
        if self.events is None:
            return
        self.events.emit(
            event,
            subject=scope_subject(self.scope),
            kind=self.kind,
            stage=stage,
            key=key.digest if key is not None else None,
            method=key.components.get("method") if key is not None else None,
            **fields,
        )

    def lookup(self, stage: str, key: StageKey):
        """(record, reason) when `key` would be reused in this run."""
        if self.recompute.forced(self.scope, stage, key):
            return None
        return self.store.lookup(self.scope, stage, key)

    def run(
        self,
        stage: str,
        key: StageKey,
        compute: Callable[[], StageResult],
        *,
        item: str | None = None,
        volatile: Iterable[str] | None = None,
        artifact_qid: str | None = None,
        force: bool = False,
    ) -> Outcome:
        """Reuse the output under `key`, or compute, store and return it.

        A computation that raises is logged as a failure (with the detail
        of a StageFailed) and re-raised; nothing is stored under the key.
        """
        if volatile is None:
            from yizkr.pipeline.stageversions import STAGES

            spec = STAGES.get(stage)
            volatile = spec.volatile if spec is not None else ()
        forced = force or self.recompute.forced(self.scope, stage, key)
        if not forced:
            found = self.store.lookup(self.scope, stage, key)
            if found is not None:
                record, reason = found
                self.emit(
                    "reused",
                    stage,
                    key,
                    item=item,
                    reason=reason,
                    record=record.key if reason == ADOPTED else None,
                    attention=record.attention or None,
                )
                return Outcome(record, True, reason)
        self._check_control(stage, key, item)
        previous = self.store.latest(self.scope, stage, item=item)
        self.emit(
            "started",
            stage,
            key,
            item=item,
            changed=changed_components(previous.components, key.components)
            if previous is not None
            else None,
        )
        try:
            result = compute()
        except DispatchStopped:
            raise
        except Exception as exc:
            detail = exc.detail if isinstance(exc, StageFailed) else {}
            reason = (
                str(exc)
                if isinstance(exc, StageFailed)
                else f"{type(exc).__name__}: {exc}"
            )
            self.store.fail(
                self.scope, stage, key, reason, item=item, **detail
            )
            self.emit("failed", stage, key, item=item, reason=reason[:500])
            raise
        meta = dict(result.meta)
        if item is not None:
            meta["item"] = item
        result = StageResult(
            result.output,
            raw=result.raw,
            extras=result.extras,
            attention=result.attention,
            meta=meta,
        )
        record = self.store.put(
            self.scope,
            stage,
            key,
            result,
            volatile=volatile,
            artifact_qid=artifact_qid,
            replace=forced,
        )
        if forced:
            self.recompute.done(self.scope, stage, key)
        reason = RECOMPUTED if forced else COMPUTED
        self.emit(
            "stored",
            stage,
            key,
            item=item,
            reason=reason,
            attention=list(result.attention) or None,
        )
        return Outcome(record, False, reason)

    def _check_control(
        self, stage: str, key: StageKey, item: str | None
    ) -> None:
        if self.events is None:
            return
        stopped = self.events.control().stopped(stage=stage, kind=self.kind)
        if stopped:
            self.emit("held", stage, key, item=item, reason=stopped)
            raise DispatchStopped(f"{stage} not dispatched: {stopped}")


# -- engine wrappers ----------------------------------------------------------


class DeferredClient:
    """An LLM client built on first use, so a run that reuses every
    output never starts an engine. It presents the conversational
    capability (ADR-0006) of the engines it wraps."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._client: Any = None
        self._lock = threading.Lock()

    def _get(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            return self._client

    @property
    def model_identifier(self) -> str:
        return self._get().model_identifier

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        return self._get().complete(prompt, image_paths)

    def open_conversation(self, prompt: str) -> tuple[str, Any]:
        return self._get().open_conversation(prompt)

    def fork(self, handle: Any, prompt: str) -> str:
        return self._get().fork(handle, prompt)


class RecordingClient:
    """Passes completions through and keeps every response, so a stage can
    store the raw output of each call beside its parsed output."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.responses: list[str] = []

    @property
    def model_identifier(self) -> str:
        return self.client.model_identifier

    def complete(self, prompt: str, image_paths: Sequence[Path] = ()) -> str:
        response = self.client.complete(prompt, image_paths)
        self.responses.append(response)
        return response
