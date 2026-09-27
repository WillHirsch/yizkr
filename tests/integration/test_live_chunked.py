"""Chunked extraction (ADR-0006) end to end against a real engine.

Needs the claude CLI installed and logged in; skips otherwise. One
small two-unit document exercises the full path: segmentation call,
forked chunk calls, sentinel contract, merge, state dir. Costs a few
subscription calls per run.
"""

from __future__ import annotations

import pytest

from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.llm import ClaudeCodeClient
from yizkr.pipeline.transcribe import Transcription

pytestmark = pytest.mark.live

DOC = """\
Record of the Example family, prepared 1990.

1. Anna Example  b: 4 Mar 1900 in Springfield. d: 11 Nov 1980 in Springfield; age: 80.
+ Bern Example  m: 2 Feb 1920 in Springfield.
    2. Carla Example  b: abt 1922 in Springfield.

Note to readers: coffee and biscuits are served in the reading room.
"""  # noqa: E501


def test_chunked_extraction_end_to_end(tmp_path):
    if not ClaudeCodeClient.available():
        pytest.skip("claude CLI not installed")
    extractor = ChunkedClaimExtractor(
        ClaudeCodeClient(),
        state_dir=tmp_path / "chunks",
        budget=120,  # force at least two chunks for this tiny document
    )
    transcription = Transcription(text=DOC, engine="test", artifact_qid="Q23")
    batch = extractor.extract(transcription, artifact_qid="Q23")

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
    # every phase stored in the stage store for inspection and reuse (#87)
    assert extractor.batch_record is not None
    assert extractor.batch_record.extra("coverage.json")
