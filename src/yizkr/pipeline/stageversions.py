"""Stage-version registry: the declared version of every stage and method.

A stage's version is a component of every key it produces
(yizkr.pipeline.stages), so bumping it recomputes that stage's outputs,
and downstream only what their changed inputs force. A routed stage has
one version per method, and a key holds only the version of the method
its document was routed to: a new or changed method recomputes only the
documents routed to it.

Each stage and method lists the code that defines its behaviour
(`sources`: ``module:qualified.name`` for a function, class or constant,
or a bare module name for a whole module). `stage_versions.json`, beside
this file, records a fingerprint of those sources for the declared
version. `check_versions` fails when a fingerprint drifts without a
version bump or a recorded "functionally neutral" entry, so a behaviour
change cannot slip into the store under an old key; a unit test runs it.

Fingerprints ignore comments, docstrings and blank lines. Prompt
templates, the model manifest and the claim schema are key components of
their own and are not listed as sources.

After editing a listed source, `yizkr stages check` names the drift:

* the change alters output: bump the version here, then run
  `yizkr stages lock <stage>`;
* it does not (a rename, a refactor): run
  `yizkr stages lock <stage> --neutral "<why>"`.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import io
import json
import textwrap
import tokenize
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOCK_FILE = Path(__file__).with_name("stage_versions.json")

# volatile fields of a claim batch: run provenance that differs between
# two identical recomputes (when, which commit, which engine build)
BATCH_VOLATILE = (
    "extraction_run.started_at",
    "extraction_run.software_commit",
    "extraction_run.model_identifier",
    "extraction_run.pipeline_version",
)


@dataclass(frozen=True)
class Method:
    """One way a routed stage can compute its output."""

    version: int
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stage:
    version: int
    sources: tuple[str, ...] = ()
    methods: Mapping[str, Method] = field(default_factory=dict)
    # dotted paths into the output that content hashes leave out
    volatile: tuple[str, ...] = ()
    # "document" (one output set per document) or "corpus"
    scope: str = "document"
    # whether computing it calls a model (for pacing and estimates)
    llm: bool = False
    # whether it has records of its own; a stage that only shapes
    # another stage's output (a parser) has none
    stored: bool = True


_ORCHESTRATE = "yizkr.pipeline.orchestrate:Orchestrator"
_EXTRACTOR = "yizkr.pipeline.chunking:ChunkedClaimExtractor"

STAGES: dict[str, Stage] = {
    "transcription": Stage(
        version=1,
        sources=(
            "yizkr.pipeline.transcribe:Transcription",
            "yizkr.pipeline.language:resolve_language",
            "yizkr.pipeline.language:detect_language",
            "yizkr.pipeline.language:LanguageResolution",
            "yizkr.pipeline.language:CONFIDENCE_FLOOR",
            "yizkr.pipeline.language:MIN_CHARS",
            "yizkr.pipeline.language:LANG_CODE_RE",
            f"{_ORCHESTRATE}._compute_transcription",
        ),
        methods={
            "vision": Method(
                1,
                (
                    "yizkr.pipeline.transcribe:LlmVisionTranscriber",
                    f"{_ORCHESTRATE}._transcribe_vision",
                ),
            ),
            "text-file": Method(
                1,
                (
                    "yizkr.pipeline.transcribe:TextFileTranscriber",
                    f"{_ORCHESTRATE}._transcribe_text_file",
                ),
            ),
            "slide-text": Method(
                1,
                (
                    "yizkr.pipeline.presentation:unpack_pptx",
                    "yizkr.pipeline.presentation:_text_runs",
                    "yizkr.pipeline.presentation:_relationships",
                    "yizkr.pipeline.presentation:slide_transcript",
                    f"{_ORCHESTRATE}._transcribe_slides",
                ),
            ),
            "correction": Method(
                1,
                (f"{_ORCHESTRATE}._transcribe_correction",),
            ),
        },
        volatile=("transcription.engine",),
        llm=True,
    ),
    "entity_manifest": Stage(
        version=1,
        sources=(
            f"{_EXTRACTOR}._phase_one",
            f"{_EXTRACTOR}._entity_manifest",
            f"{_EXTRACTOR}._parse_entity_manifest",
            f"{_EXTRACTOR}._segment",
            f"{_EXTRACTOR}._parse_segmentation",
            f"{_EXTRACTOR}._fallback_segmentation",
            "yizkr.pipeline.chunking:DocumentSegmentation",
            "yizkr.pipeline.chunking:SegmentationUnit",
            "yizkr.pipeline.chunking:number_lines",
            "yizkr.pipeline.extract:entity_kind_prompt_lines",
            "yizkr.pipeline.extract:scan_json_objects",
        ),
        llm=True,
    ),
    "chunk": Stage(
        version=1,
        sources=(
            "yizkr.pipeline.chunking:pack_entities",
            "yizkr.pipeline.chunking:pack_units",
            "yizkr.pipeline.chunking:unit_text",
            "yizkr.pipeline.chunking:number_lines",
            "yizkr.pipeline.chunking:INLINE_DOCUMENT_LIMIT",
            f"{_EXTRACTOR}._compute_chunk",
            f"{_EXTRACTOR}._run_chunk",
            f"{_EXTRACTOR}._render_chunk_prompt",
            f"{_EXTRACTOR}._chunk_prompt",
            f"{_EXTRACTOR}._entity_chunk_prompt",
            f"{_EXTRACTOR}._document_block",
            "yizkr.pipeline.extract:vocabulary_prompt_lines",
        ),
        llm=True,
    ),
    # the chunk parser: a stored chunk whose parse version differs is
    # re-parsed from its raw response, never re-called
    "chunk_parse": Stage(
        version=1,
        sources=(
            "yizkr.pipeline.chunking:parse_chunk_response",
            "yizkr.pipeline.chunking:parse_chunk_gaps",
            "yizkr.pipeline.chunking:ChunkIncomplete",
            "yizkr.pipeline.extract:scan_json_objects",
        ),
        stored=False,
    ),
    "batch": Stage(
        version=1,
        sources=(
            f"{_EXTRACTOR}._merge",
            f"{_EXTRACTOR}._refuse_empty",
            f"{_EXTRACTOR}._entity_coverage",
            f"{_EXTRACTOR}._coverage",
            "yizkr.pipeline.chunking:normalize_to_manifest",
            "yizkr.pipeline.chunking:prefix_local_ids",
            "yizkr.pipeline.chunking:_rewrite_local_refs",
            "yizkr.pipeline.extract:salvage_valid_claims",
            "yizkr.pipeline.extract:_normalize_claim_shape",
            "yizkr.pipeline.extract:run_metadata",
            "yizkr.pipeline.names:normalize_entity_dict",
            "yizkr.pipeline.names:normalize_aliases",
            "yizkr.pipeline.names:_is_derivable",
            "yizkr.pipeline.names:_tokens",
        ),
        volatile=BATCH_VOLATILE,
    ),
    "critique": Stage(
        version=1,
        sources=(
            "yizkr.pipeline.critique:run_critique",
            "yizkr.pipeline.critique:_entities_block",
            "yizkr.pipeline.critique:_claims_block",
            "yizkr.pipeline.critique:Finding",
            "yizkr.pipeline.critique:axis_of",
            f"{_ORCHESTRATE}._compute_critique",
        ),
        llm=True,
    ),
    "gedcom": Stage(
        version=1,
        sources=(
            "yizkr.pipeline.gedcom",
            f"{_ORCHESTRATE}._compute_gedcom",
        ),
        volatile=BATCH_VOLATILE,
    ),
}


# -- fingerprints -----------------------------------------------------------


def resolve_source(ref: str) -> Any:
    """The object a source reference names."""
    module_name, _, attribute = ref.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in attribute.split(".") if attribute else ():
        target = getattr(target, part)
    return target


def normalize_source(source: str) -> str:
    """Source text without comments, docstrings or blank lines, so an
    edit to any of those leaves the fingerprint alone."""
    source = textwrap.dedent(source)
    try:
        tree = ast.parse(source)
        offset = 0
    except SyntaxError:
        # a method whose body holds a string with lines at column 0 cannot
        # be dedented; parse it inside a class instead
        tree = ast.parse("class _:\n" + source)
        offset = 1
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_lines.update(
                range(first.lineno - offset, first.end_lineno + 1 - offset)
            )
    comments: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments[token.start[0]] = token.start[1]
    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if number in docstring_lines:
            continue
        if number in comments:
            line = line[: comments[number]]
        line = line.rstrip()
        if line.strip():
            kept.append(line)
    return "\n".join(kept)


def source_text(ref: str) -> str:
    target = resolve_source(ref)
    if isinstance(target, staticmethod | classmethod):
        target = target.__func__
    if callable(target) and not inspect.isclass(target):
        target = inspect.unwrap(target)  # through lru_cache and the like
    if (
        inspect.ismodule(target)
        or inspect.isclass(target)
        or inspect.isfunction(target)
        or inspect.ismethod(target)
    ):
        return normalize_source(inspect.getsource(target))
    text = repr(target)
    if " at 0x" in text:
        raise ValueError(f"{ref}: no stable source or value to fingerprint")
    return text


def fingerprint(sources: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for ref in sources:
        digest.update(ref.encode())
        digest.update(b"\0")
        digest.update(source_text(ref).encode())
        digest.update(b"\0")
    return digest.hexdigest()


# -- the lock -----------------------------------------------------------------


def entries(
    registry: Mapping[str, Stage] | None = None,
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """'stage' and 'stage:method' -> (version, sources)."""
    registry = STAGES if registry is None else registry
    found: dict[str, tuple[int, tuple[str, ...]]] = {}
    for name, stage in registry.items():
        found[name] = (stage.version, stage.sources)
        for method_name, method in stage.methods.items():
            found[f"{name}:{method_name}"] = (method.version, method.sources)
    return found


def load_lock(path: Path = LOCK_FILE) -> dict[str, dict]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_lock(lock: Mapping[str, dict], path: Path = LOCK_FILE) -> None:
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def check_versions(
    registry: Mapping[str, Stage] | None = None,
    lock: Mapping[str, dict] | None = None,
) -> list[str]:
    """Every stage or method whose sources, version and lock disagree."""
    lock = load_lock() if lock is None else lock
    declared = entries(registry)
    problems = []
    for name, (version, sources) in declared.items():
        recorded = lock.get(name)
        if recorded is None:
            problems.append(
                f"{name}: not in the lock; run `yizkr stages lock {name}`"
            )
            continue
        if recorded.get("version") != version:
            problems.append(
                f"{name}: version {version} is declared but the lock "
                f"records {recorded.get('version')}; run "
                f"`yizkr stages lock {name}`"
            )
            continue
        current = fingerprint(sources)
        neutral = {
            entry.get("fingerprint") for entry in recorded.get("neutral", [])
        }
        if current != recorded.get("fingerprint") and current not in neutral:
            problems.append(
                f"{name}: its sources changed since version {version} was "
                f"recorded. If the change alters output, bump the version "
                f"in yizkr.pipeline.stageversions and run `yizkr stages "
                f"lock {name}`; if it does not, run `yizkr stages lock "
                f"{name} --neutral '<why>'`"
            )
    for name in sorted(set(lock) - set(declared)):
        problems.append(
            f"{name}: in the lock but not declared; run "
            f"`yizkr stages lock {name}` to drop it"
        )
    return problems


def lock_versions(
    names: Iterable[str],
    *,
    neutral: str | None = None,
    registry: Mapping[str, Stage] | None = None,
    lock: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Record the current fingerprint of each named stage or method.

    With `neutral`, the fingerprint is recorded as functionally neutral
    for the version already locked. Without it, the version must be new
    (a bump, or a stage not yet locked); a changed fingerprint under an
    unchanged version is refused. Returns the updated lock."""
    lock = load_lock() if lock is None else lock
    declared = entries(registry)
    for name in names:
        if name not in declared:
            lock.pop(name, None)  # a stage or method no longer declared
            continue
        version, sources = declared[name]
        current = fingerprint(sources)
        recorded = lock.get(name)
        if neutral is not None:
            if recorded is None or recorded.get("version") != version:
                raise ValueError(
                    f"{name}: --neutral applies to a locked version; lock "
                    f"version {version} first"
                )
            if current != recorded.get("fingerprint") and current not in {
                entry.get("fingerprint")
                for entry in recorded.get("neutral", [])
            }:
                recorded.setdefault("neutral", []).append(
                    {"fingerprint": current, "note": neutral}
                )
            continue
        if recorded is not None and recorded.get("version") == version:
            known = {recorded.get("fingerprint")} | {
                entry.get("fingerprint")
                for entry in recorded.get("neutral", [])
            }
            if current in known:
                continue  # nothing to record
            raise ValueError(
                f"{name}: its sources changed but version {version} did "
                f"not; bump the version, or pass --neutral '<why>'"
            )
        lock[name] = {"version": version, "fingerprint": current}
    return lock
