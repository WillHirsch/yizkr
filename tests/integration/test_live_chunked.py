"""Chunked extraction (ADR-0006) end to end against a real engine, through
the LLM cache (ADR-0032).

Needs the claude CLI installed and logged in; skips otherwise. One
small two-unit document exercises the full path: the entity manifest,
forked chunk calls, the sentinel contract and the merge. The calls go
through the LLM cache in $YIZKR_LIVE_CACHE (default var/live-cache):
the first run of the test asks the engine, at a few subscription calls,
and a later run is answered from the cache. Either way the test then
replays the extraction from the cache with the engine barred, and must
get the same batch. It prints the cache events of both passes (run
pytest with -s to see them).
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.llm import ClaudeCodeClient, cached_client, open_cache
from yizkr.pipeline.rundir import RunDir
from yizkr.pipeline.transcribe import Transcription

pytestmark = pytest.mark.live

DOC = """\
Record of the Example family, prepared 1990.

1. Anna Example  b: 4 Mar 1900 in Springfield. d: 11 Nov 1980 in Springfield; age: 80.
+ Bern Example  m: 2 Feb 1920 in Springfield.
    2. Carla Example  b: abt 1922 in Springfield.

Note to readers: coffee and biscuits are served in the reading room.
"""  # noqa: E501

CACHE = Path(
    os.environ.get("YIZKR_LIVE_CACHE")
    or Path(__file__).resolve().parents[2] / "var" / "live-cache"
)


class Barred:
    """The engine's configuration with the engine barred: every call
    fails, so whatever a caching client returns through it came from the
    cache."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def complete(self, prompt, image_paths=()):
        raise AssertionError("the replay asked the engine")

    def open_conversation(self, prompt):
        raise AssertionError("the replay asked the engine")

    def fork(self, handle, prompt):
        raise AssertionError("the replay asked the engine")


def extract(client, state_dir=None):
    extractor = ChunkedClaimExtractor(
        client,
        state_dir=state_dir,
        budget=120,  # force at least two chunks for this tiny document
    )
    transcription = Transcription(text=DOC, engine="test", artifact_qid="Q23")
    return extractor, extractor.extract(transcription, artifact_qid="Q23")


def cache_events(run: RunDir) -> dict[str, int]:
    return dict(
        Counter(
            f"{event['event']} ({event.get('reason', '-')})"
            for event in run.events()
        )
    )


def test_chunked_extraction_end_to_end(tmp_path):
    if not ClaudeCodeClient.available():
        pytest.skip("claude CLI not installed")
    engine = ClaudeCodeClient()
    run = RunDir(tmp_path / "run")
    client = cached_client(
        engine, open_cache(CACHE), role="extract", events=run.emit
    )
    extractor, batch = extract(client, tmp_path / "chunks")
    print(f"\ncache {CACHE}\nlive pass: {cache_events(run)}")

    assert extractor.failed_chunks == []
    assert not extractor.used_fallback_manifest  # entity-first path ran
    # three people, several facts each; exact counts are model-dependent
    labels = {
        claim.subject.label
        for claim in batch.claims
        if hasattr(claim.subject, "label")
    }
    assert any("Anna" in label for label in labels)
    assert any("Carla" in label for label in labels)
    # the entity manifest travels with the batch (ADR-0007)
    assert any("Anna" in entity.label for entity in batch.entities)
    # every claim carries its source (pydantic enforces; belt and braces)
    assert all(claim.source.artifact for claim in batch.claims)
    # the coverage report is written beside the batch
    assert (tmp_path / "chunks" / "coverage.json").exists()
    # every call was answered from the cache or asked of the engine
    assert {event["event"] for event in run.events()} <= {
        "cache-hit",
        "cache-miss",
        "cache-replay",
        "cache-reject",
    }

    # the same extraction again, answered from the cache alone
    replay = RunDir(tmp_path / "replay")
    _, again = extract(
        cached_client(
            Barred(engine),
            open_cache(CACHE),
            role="extract",
            events=replay.emit,
        )
    )
    print(f"replay: {cache_events(replay)}")
    assert {event["event"] for event in replay.events()} == {"cache-hit"}
    assert again.claims == batch.claims
    assert again.entities == batch.entities
