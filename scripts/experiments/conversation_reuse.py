#!/usr/bin/env python3
"""Measure conversation reuse offline with authored fictional answers.

The production extractor and cache execute a short postcard and a longer family
history. This measures dispatched calls, UTF-8 prompt bytes and local session
I/O; it does not measure model quality, billed tokens, provider prompt caching,
network latency or a representative distribution of private archive documents.
Only committed fictional fixtures are read. Temporary caches are discarded;
the aggregate report is written to a new directory outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.fixtures.corpus import FOLDER, LETTER, NARRATIVE  # noqa: E402
from tests.fixtures.corpus.answers import RECORDED  # noqa: E402
from tests.fixtures.corpus.engine import Calls, RecordedEngine  # noqa: E402

from yizkr.claims import ArtifactRef  # noqa: E402
from yizkr.pipeline.chunking import ChunkedClaimExtractor  # noqa: E402
from yizkr.pipeline.llm import cached_client, open_cache  # noqa: E402
from yizkr.pipeline.transcribe import Transcription  # noqa: E402

BUDGET = 512
DOCUMENTS = (LETTER, NARRATIVE)
MODES = ("restore", "replay", "inline")
STATES = ("cold", "full", "partial")


class SeedComplete(BaseException):
    """Stop seeding after the opening and one completed chunk."""


class MeasuredEngine(RecordedEngine):
    """Authored answers with counters at the provider's public boundary."""

    def __init__(self, folder: Path, *, limit: int | None = None):
        super().__init__(Calls(), model="fictional-measurement", effort="low")
        self.folder = folder
        self.folder.mkdir(mode=0o700, parents=True)
        self.limit = limit
        self.operations: Counter[str] = Counter()
        self.prompt_bytes = 0
        self.logical_context_bytes = 0
        self.snapshot_bytes = 0
        self.restored_bytes = 0
        self.copy_bytes = 0
        self.copy_ns = 0
        self.restores = 0
        self.releases = 0

    def dispatch(self, kind: str, prompt: str, inherited: str = "") -> None:
        if self.limit is not None and self.operations.total() >= self.limit:
            raise SeedComplete()
        self.operations[kind] += 1
        self.prompt_bytes += len(prompt.encode())
        self.logical_context_bytes += len((inherited + prompt).encode())

    def complete(self, prompt, image_paths=()):
        if image_paths:
            raise AssertionError(
                "the measurement uses supplied fictional text"
            )
        self.dispatch("complete", prompt)
        return super().complete(prompt)

    def open_conversation(self, prompt):
        self.dispatch("open", prompt)
        return super().open_conversation(prompt)

    def fork(self, handle, prompt):
        session = self._sessions[handle]
        self.dispatch("fork", prompt, session.prompt + session.answer)
        return super().fork(handle, prompt)

    def keep_conversation(self, handle):
        files = super().keep_conversation(handle)
        self.snapshot_bytes += sum(map(len, files.values()))
        return files

    def resume_conversation(self, prompt, response, files):
        handle = super().resume_conversation(prompt, response, files)
        self.restores += 1
        self.restored_bytes += sum(map(len, files.values()))
        # The authored provider materializes the supplied bundle locally.
        # Count and time actual writes, without claiming Claude's filesystem
        # layout, transcript size or restoration latency has been measured.
        started = time.perf_counter_ns()
        for name, payload in files.items():
            destination = self.folder / Path(name).name
            self.copy_bytes += destination.write_bytes(payload)
        self.copy_ns += time.perf_counter_ns() - started
        return handle

    def close_conversation(self, handle):
        self.releases += 1
        super().close_conversation(handle)


class ReplayEngine(MeasuredEngine):
    keep_conversation = None
    resume_conversation = None


class InlineEngine(ReplayEngine):
    open_conversation = None
    fork = None


ENGINES = {
    "restore": MeasuredEngine,
    "replay": ReplayEngine,
    "inline": InlineEngine,
}


def session_bytes(cache: Path) -> int:
    return sum(
        path.stat().st_size
        for folder in cache.glob("*/*.session")
        for path in folder.rglob("*")
        if path.is_file()
    )


def extract(cache_path: Path, engine: MeasuredEngine, document: str):
    recorded = RECORDED[document]
    artifact = ArtifactRef.from_bytes((FOLDER / document).read_bytes())
    reading = Transcription(
        artifact=artifact,
        text=recorded.text,
        language=recorded.language,
        engine="fictional-measurement",
    )
    events: Counter[str] = Counter()

    def event(name, **_fields):
        events[name] += 1

    resources = open_cache(cache_path)
    client = cached_client(engine, resources, role="extract", events=event)
    extractor = ChunkedClaimExtractor(client, budget=BUDGET, concurrency=1)
    try:
        batch = extractor.extract(reading, artifact)
    finally:
        primary = sys.exception()
        try:
            client.close()
            resources.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(
                "fictional execution cleanup also failed: "
                f"{type(cleanup).__name__}"
            )
    if extractor.failed_chunks or extractor.incomplete_reasons:
        raise AssertionError("the fictional extraction was incomplete")
    digest = hashlib.sha256(
        json.dumps(
            batch.model_dump(mode="json", exclude={"extraction_run"}),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return batch, events, digest


def scenario(root: Path, document: str, mode: str, state: str) -> dict:
    cache = root / "cache"
    engine_type = ENGINES[mode]
    if state != "cold":
        seed = engine_type(
            root / "seed", limit=2 if state == "partial" else None
        )
        try:
            extract(cache, seed, document)
        except SeedComplete:
            if state != "partial":
                raise
        else:
            if state == "partial":
                raise AssertionError(
                    "partial seeding did not leave a missing chunk"
                )
    before = session_bytes(cache)
    engine = engine_type(root / "measured")
    batch, events, digest = extract(cache, engine, document)
    return {
        "document": document,
        "reading_utf8_bytes": len(RECORDED[document].text.encode()),
        "mode": mode,
        "cache_state": state,
        "calls": dict(sorted(engine.operations.items())),
        "total_calls": engine.operations.total(),
        "submitted_prompt_utf8_bytes": engine.prompt_bytes,
        "logical_context_utf8_bytes": engine.logical_context_bytes,
        "cache_events": dict(sorted(events.items())),
        "session_restores": engine.restores,
        "session_snapshot_payload_bytes": engine.snapshot_bytes,
        "session_restore_payload_bytes": engine.restored_bytes,
        "synthetic_restore_copy_bytes": engine.copy_bytes,
        "synthetic_restore_copy_ns": engine.copy_ns,
        "persisted_session_bytes_before": before,
        "persisted_session_bytes_after": session_bytes(cache),
        "claims": len(batch.claims),
        "entities": len(batch.entities),
        "authored_batch_sha256": digest,
    }


def measure(root: Path) -> dict:
    rows = [
        scenario(root / str(index), document, mode, state)
        for index, (document, mode, state) in enumerate(
            (d, m, s) for d in DOCUMENTS for m in MODES for s in STATES
        )
    ]
    equivalent = all(
        len(
            {
                r["authored_batch_sha256"]
                for r in rows
                if r["document"] == document
            }
        )
        == 1
        for document in DOCUMENTS
    )
    if not equivalent:
        raise AssertionError("authored results differ between reuse scenarios")
    return {
        "schema_version": 1,
        "workload": {
            "documents": list(DOCUMENTS),
            "chunk_budget": BUDGET,
            "concurrency": 1,
            "partial_cache": (
                "opening and first chunk present; later chunks absent"
            ),
        },
        "authored_results_equal": equivalent,
        "limitations": [
            "Authored fixture answers test wiring, not model quality or "
            "equivalence of live outputs.",
            "Prompt bytes exclude system text, CLI/HTTP envelopes, tools "
            "and hidden provider context.",
            "Logical context includes an inherited opening prompt and "
            "answer for each dispatched fork; it is not a token or "
            "billing measure.",
            "Session bytes and copy timings describe this authored "
            "transcript and local filesystem only; timings include ordinary "
            "write overhead but no fsync or provider restore work.",
            "Two small fictional readings and an explicit chunk budget "
            "illustrate reuse mechanics, not private corpus frequencies "
            "or provider prompt-cache hit rates.",
            "All existing reuse optimizations remain; these measurements "
            "alone cannot justify removing them.",
        ],
        "scenarios": rows,
    }


def markdown(report: dict) -> str:
    rows = [
        "# Offline conversation reuse measurement",
        "",
        "All model answers are authored repository-fictional fixtures. "
        "Claims and entity manifests are equal across scenarios; "
        "extraction-run metadata is excluded from this comparison.",
        "",
        "| Reading | Mode | Cache | Calls | Submitted bytes | "
        "Logical context bytes | Restores | Copied bytes | Copy µs |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["scenarios"]:
        rows.append(
            f"| {row['document']} | {row['mode']} | {row['cache_state']} | "
            f"{row['total_calls']} | {row['submitted_prompt_utf8_bytes']} | "
            f"{row['logical_context_utf8_bytes']} | "
            f"{row['session_restores']} | "
            f"{row['synthetic_restore_copy_bytes']} | "
            f"{row['synthetic_restore_copy_ns'] / 1000:.1f} |"
        )
    rows.extend(["", *[f"- {limit}" for limit in report["limitations"]], ""])
    return "\n".join(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="new aggregate output directory outside the repository",
    )
    args = parser.parse_args(argv)
    output = args.out.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("--out must be outside the repository")
    if output.exists():
        parser.error("--out must name a new directory")
    with tempfile.TemporaryDirectory(
        prefix="yizkr-fictional-reuse-"
    ) as temporary:
        report = measure(Path(temporary))
    output.mkdir(mode=0o700, parents=True)
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "report.md").write_text(markdown(report))
    print(
        json.dumps(
            {
                "scenarios": len(report["scenarios"]),
                "authored_results_equal": report["authored_results_equal"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
