"""Offline tests for chunked extraction (ADR-0006/0007).

No instance, no LLM.
"""

import json

import pytest

from yizkr.claims import EntityRecord
from yizkr.pipeline.chunking import (
    ChunkedClaimExtractor,
    ChunkIncomplete,
    DocumentSegmentation,
    SegmentationUnit,
    normalize_to_manifest,
    pack_entities,
    pack_units,
    parse_chunk_gaps,
    parse_chunk_response,
    prefix_local_ids,
    unit_text,
)
from yizkr.pipeline.transcribe import Transcription

# a two-person document: one claimable line per person, one logistics line
DOC = "\n".join(
    [
        "Family record",  # 1: boilerplate
        "1. Anna Example  b: 1900 in Springfield.",  # 2
        "2. Bern Example  b: 1902 in Springfield.",  # 3
        "Dinner is at 7pm.",  # 4: logistics
    ]
)
LINE_LENGTH = len(DOC.splitlines()[1])  # per-entity evidence size


def claim(local_id: str, label: str, year: int) -> dict:
    return {
        "subject": {
            "kind": "local",
            "local_id": local_id,
            "label": label,
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": {"type": "time", "year": year},
        "source": {
            "artifact": {"kind": "qid", "qid": "Q23"},
            "transcription_excerpt": {"text": f"b: {year}", "language": "en"},
        },
        "confidence": 0.9,
    }


def ndjson(claims: list[dict], emitted: int | None = None) -> str:
    lines = [json.dumps(c) for c in claims]
    lines.append(
        json.dumps(
            {
                "done": True,
                "claims_emitted": len(claims) if emitted is None else emitted,
            }
        )
    )
    return "\n".join(lines)


def entity_manifest_json() -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": "Anna Example",
                    "aliases": ["Anna"],
                    "kind": "human",
                    "summary": "b. 1900 Springfield",
                    "mention_lines": [2],
                },
                {
                    "id": "e2",
                    "label": "Bern Example",
                    "aliases": [],
                    "kind": "human",
                    "summary": "b. 1902 Springfield",
                    "mention_lines": [3],
                },
            ]
        }
    )


def segmentation_json() -> str:
    return json.dumps(
        {
            "units": [
                {
                    "id": "u1",
                    "label": "header",
                    "kind": "boilerplate",
                    "first_line": 1,
                    "last_line": 1,
                    "context": "",
                    "extract": False,
                },
                {
                    "id": "u2",
                    "label": "Anna Example entry",
                    "kind": "person entry",
                    "first_line": 2,
                    "last_line": 2,
                    "context": "generation 1",
                    "extract": True,
                },
                {
                    "id": "u3",
                    "label": "Bern Example entry",
                    "kind": "person entry",
                    "first_line": 3,
                    "last_line": 3,
                    "context": "generation 1",
                    "extract": True,
                },
            ]
        }
    )


class ScriptedLlm:
    """Conversational fake: responses popped per call, calls recorded."""

    model_identifier = "fake-model"

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []  # (method, prompt)

    def _next(self, method: str, prompt: str) -> str:
        self.calls.append((method, prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def complete(self, prompt, image_paths=()):
        return self._next("complete", prompt)

    def open_conversation(self, prompt):
        return self._next("open", prompt), "session-abc"

    def fork(self, handle, prompt):
        assert handle == "session-abc"
        return self._next("fork", prompt)


class PlainLlm(ScriptedLlm):
    """No conversation capability at all (attributes absent)."""

    open_conversation = property()  # never satisfies the protocol

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fork(self, handle, prompt):  # pragma: no cover - must not be called
        raise AssertionError("fork on non-conversational client")


def transcription() -> Transcription:
    return Transcription(text=DOC, engine="test", artifact_qid="Q23")


class TestSegmentationModel:
    def test_valid_segmentation_has_no_problems(self):
        seg = DocumentSegmentation.model_validate_json(segmentation_json())
        assert seg.problems(line_count=4) == []
        assert seg.unsegmented_lines(line_count=4) == [(4, 4)]

    def test_overlap_and_bounds_are_problems(self):
        seg = DocumentSegmentation(
            units=[
                SegmentationUnit(id="a", label="a", first_line=1, last_line=3),
                SegmentationUnit(id="b", label="b", first_line=3, last_line=9),
            ]
        )
        problems = " ".join(seg.problems(line_count=4))
        assert "overlap" in problems
        assert "beyond" in problems


class TestPacking:
    def units(self):
        return DocumentSegmentation.model_validate_json(
            segmentation_json()
        ).units

    def entities(self):
        return [
            EntityRecord.model_validate(record)
            for record in json.loads(entity_manifest_json())["entities"]
        ]

    def test_skips_non_extract_units(self):
        chunks = pack_units(self.units(), DOC.splitlines(), budget=10_000)
        assert [[u.id for u in chunk] for chunk in chunks] == [["u2", "u3"]]

    def test_budget_splits_chunks(self):
        line_length = len(unit_text(DOC.splitlines(), self.units()[1]))
        chunks = pack_units(self.units(), DOC.splitlines(), budget=line_length)
        assert [[u.id for u in chunk] for chunk in chunks] == [["u2"], ["u3"]]

    def test_entities_pack_by_mention_evidence(self):
        chunks = pack_entities(
            self.entities(), DOC.splitlines(), budget=LINE_LENGTH
        )
        assert [[e.id for e in chunk] for chunk in chunks] == [["e1"], ["e2"]]
        chunks = pack_entities(
            self.entities(), DOC.splitlines(), budget=10_000
        )
        assert [[e.id for e in chunk] for chunk in chunks] == [["e1", "e2"]]

    def test_entities_without_mentions_use_flat_estimate(self):
        entities = self.entities()
        for entity in entities:
            entity.mention_lines = []
        chunks = pack_entities(entities, DOC.splitlines(), budget=10_000)
        assert len(chunks) == 1

    def test_non_extract_entities_are_skipped(self):
        """extract=false entities (logistics people, boilerplate) are
        recorded for audit but never assigned to a chunk."""
        entities = self.entities()
        entities[0].extract = False
        chunks = pack_entities(entities, DOC.splitlines(), budget=10_000)
        assert [[e.id for e in chunk] for chunk in chunks] == [["e2"]]


class TestChunkResponseContract:
    def test_sentinel_required(self):
        with pytest.raises(ChunkIncomplete, match="sentinel"):
            parse_chunk_response(json.dumps(claim("e1", "Anna Example", 1900)))

    def test_count_mismatch_keeps_claims_and_warns(self):
        # a present sentinel proves completeness; a miscounted
        # claims_emitted keeps every parsed claim and only warns
        warnings: list[str] = []
        claims = parse_chunk_response(
            ndjson([claim("e1", "Anna Example", 1900)], emitted=5),
            warn=warnings.append,
        )
        assert len(claims) == 1
        assert warnings and "reported 5" in warnings[0]

    def test_valid_ndjson_parses(self):
        claims = parse_chunk_response(
            ndjson([claim("e1", "Anna Example", 1900)])
        )
        assert len(claims) == 1

    def test_gap_lines_and_extended_sentinel_do_not_break_claims(self):
        # ADR-0013: gap lines + gaps_emitted in the sentinel are ignored
        # by the claim parser (non-breaking), and the claim count still
        # matches the sentinel's claims_emitted
        response = "\n".join(
            [
                json.dumps(claim("e1", "Anna Example", 1900)),
                json.dumps(
                    {
                        "gap": {
                            "subject": "e1",
                            "predicate": "deported to",
                            "object": "Riga",
                            "evidence": "...",
                        }
                    }
                ),  # fmt: skip
                json.dumps(
                    {"done": True, "claims_emitted": 1, "gaps_emitted": 1}
                ),
            ]
        )
        claims = parse_chunk_response(response)
        assert len(claims) == 1


class TestGapChannel:
    """ADR-0013: relations no property expresses, surfaced not written."""

    def test_gap_line_parsed(self):
        response = "\n".join(
            [
                json.dumps(claim("e1", "Anna Example", 1900)),
                json.dumps(
                    {
                        "gap": {
                            "subject": "e1",
                            "predicate": "imprisoned at",
                            "object": "Sachsenhausen",
                            "evidence": "held there",
                        }
                    }
                ),  # fmt: skip
                json.dumps(
                    {"done": True, "claims_emitted": 1, "gaps_emitted": 1}
                ),
            ]
        )
        (gap,) = parse_chunk_gaps(response)
        assert gap["predicate"] == "imprisoned at"
        assert gap["object"] == "Sachsenhausen"

    def test_malformed_gaps_dropped_never_raise(self):
        response = "\n".join(
            [
                json.dumps(
                    {"gap": {"predicate": "no subject"}}
                ),  # missing subject
                json.dumps({"gap": "not an object"}),
                json.dumps(
                    {"done": True, "claims_emitted": 0, "gaps_emitted": 0}
                ),
            ]
        )
        assert parse_chunk_gaps(response) == []


class TestLocalRefRewrites:
    def raw_claim(self) -> dict:
        return {
            "subject": {"kind": "local", "local_id": "e1", "label": "Anna"},
            "property": "spouse",
            "value": {
                "type": "item",
                "value": {"kind": "local", "local_id": "e2", "label": "B"},
            },
            "qualifiers": [
                {
                    "property": "location",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "e9",
                            "label": "X",
                        },
                    },
                }
            ],
            "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
        }

    def test_prefix_walks_subject_value_and_qualifiers(self):
        fixed = prefix_local_ids(self.raw_claim(), "c001")
        assert fixed["subject"]["local_id"] == "c001:e1"
        assert fixed["value"]["value"]["local_id"] == "c001:e2"
        assert (
            fixed["qualifiers"][0]["value"]["value"]["local_id"] == "c001:e9"
        )
        assert fixed["source"]["artifact"] == {"kind": "qid", "qid": "Q23"}

    def test_normalize_sets_canonical_label_and_kind(self):
        records = {
            "e1": EntityRecord(id="e1", label="Anna Example", kind="human"),
            "e2": EntityRecord(id="e2", label="Bern Example", kind="human"),
        }
        fixed = normalize_to_manifest(self.raw_claim(), records)
        assert fixed["subject"]["label"] == "Anna Example"
        assert fixed["subject"]["instance_of"] == "human"
        assert fixed["value"]["value"]["label"] == "Bern Example"
        # unknown id untouched: validation rejects it later
        assert fixed["qualifiers"][0]["value"]["value"]["label"] == "X"


class TestEntityFirstExtractor:
    def extractor(self, llm, tmp_path, **kwargs):
        return ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", **kwargs
        )

    def test_fork_flow_end_to_end(self, tmp_path):
        """The manifest opens a conversation; each chunk is a fork
        assigned entities; ids stay manifest-scoped; labels normalize
        to canonical; the manifest travels in the batch."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),  # open_conversation
                ndjson([claim("e1", "Anna", 1900)]),  # fork c001 (alias label)
                ndjson([claim("e2", "Bern Example", 1902)]),  # fork c002
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")

        assert [call[0] for call in llm.calls] == ["open", "fork", "fork"]
        assert extractor.failed_chunks == []
        assert not extractor.used_fallback_manifest
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        # alias label normalized to the manifest's canonical form
        anna = next(c for c in batch.claims if c.subject.local_id == "e1")
        assert anna.subject.label == "Anna Example"
        # the manifest travels with the batch (reconcile needs it)
        assert [entity.id for entity in batch.entities] == ["e1", "e2"]
        # fork prompts carry the assignment, not an inline document
        assert "Full transcription" not in llm.calls[1][1]
        assert "e1 = Anna Example" in llm.calls[1][1]  # roster in every chunk
        state = tmp_path / "state"
        manifest = json.loads((state / "manifest.json").read_text())
        assert manifest["strategy"] == "entity"
        assert manifest["context_mode"] == "fork"
        assert manifest["session_id"] == "session-abc"
        coverage = json.loads((state / "coverage.json").read_text())
        assert coverage["entities_without_claims"] == []
        assert (state / "entity-manifest.json").exists()

    def test_progress_logging_reports_each_chunk(self, tmp_path):
        """Chunked extraction emits per-chunk progress; otherwise a large
        document is silent for minutes (the `log` callback is a no-op when
        not supplied, so other callers are unaffected)."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        logs: list[str] = []
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, log=logs.append
        )
        extractor.extract(transcription(), "Q23")

        assert any("scanning document for entities" in line for line in logs)
        assert any("extract: 2 entities, 2 chunk(s)" in line for line in logs)
        chunk_lines = [
            line for line in logs if line.strip().startswith("chunk ")
        ]
        assert chunk_lines == [
            "  chunk 1/2 (1 claim)",
            "  chunk 2/2 (1 claim)",
        ]

    def test_plain_client_gets_inline_document(self, tmp_path):
        llm = PlainLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [
                        claim("e1", "Anna Example", 1900),
                        claim("e2", "Bern Example", 1902),
                    ]
                ),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert "Full transcription" in llm.calls[1][1]
        assert "1: Family record" in llm.calls[1][1]

    def test_invented_entity_id_is_rejected(self, tmp_path):
        """A claim naming an id outside the manifest is a detectable
        defect, never a silent duplicate."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [
                        claim("e1", "Anna Example", 1900),
                        claim("e99", "Nobody", 1800),
                    ]
                ),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        rejects = json.loads((tmp_path / "state" / "rejects.json").read_text())
        assert len(rejects) == 1
        assert "entity manifest" in rejects[0]["problems"][0]

    def test_truncation_retries_then_succeeds(self, tmp_path):
        # a missing sentinel is real truncation: retryable, unlike a
        # count mismatch (which keeps its claims — next test)
        truncated = json.dumps(
            claim("e1", "Anna Example", 1900)
        )  # no sentinel
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                truncated,  # c001 attempt 1 (fork) — cut off
                ndjson([claim("e1", "Anna Example", 1900)]),  # c001 retry
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []

    def test_count_mismatch_chunk_accepted_without_retry(self, tmp_path):
        # the c007/c019 case: a complete response whose sentinel
        # undercounts is kept as-is and does NOT burn a retry
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [claim("e1", "Anna Example", 1900)], emitted=99
                ),  # miscount
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == []
        assert {c.subject.label for c in batch.claims} == {
            "Anna Example",
            "Bern Example",
        }
        # one fork per chunk — the miscount triggered no retry
        assert sum(1 for method, _ in llm.calls if method == "fork") == 2

    def test_failed_chunk_yields_partial_batch(self, tmp_path):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                RuntimeError("engine down"),  # c001 attempt 1 (fork)
                RuntimeError(
                    "engine down"
                ),  # c001 attempt 1 (inline fallback)
                RuntimeError("engine down"),  # c001 attempt 2 (inline)
                ndjson([claim("e2", "Bern Example", 1902)]),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c001"]
        assert [c.subject.label for c in batch.claims] == ["Bern Example"]
        manifest = json.loads(
            (tmp_path / "state" / "manifest.json").read_text()
        )
        assert manifest["chunks"]["c001"]["status"].startswith("failed")

    def test_failed_chunk_sidecar_survives_recovery(self, tmp_path):
        """The reason a chunk first failed lives in an append-only sidecar
        that a later successful re-run does not overwrite."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                RuntimeError("engine down"),  # c001 attempt 1 (fork)
                RuntimeError(
                    "engine down"
                ),  # c001 attempt 1 (inline fallback)
                RuntimeError("engine down"),  # c001 attempt 2 (inline)
                ndjson([claim("e2", "Bern Example", 1902)]),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        extractor.extract(transcription(), "Q23")
        sidecar = tmp_path / "state" / "c001.fail.ndjson"
        record = json.loads(sidecar.read_text().splitlines()[0])
        assert record["chunk"] == "c001"
        assert record["attempts"] == 2
        assert "engine down" in record["error"]
        assert record["raw"] is None  # a call failure left no response

        # recover c001 on resume from its now-valid raw; no model call
        (tmp_path / "state" / "c001.raw.txt").write_text(
            ndjson([claim("e1", "Anna Example", 1900)])
        )
        second = ScriptedLlm([])
        extractor = self.extractor(
            second, tmp_path, budget=LINE_LENGTH, resume=True
        )
        extractor.extract(transcription(), "Q23")
        assert second.calls == []
        manifest = json.loads(
            (tmp_path / "state" / "manifest.json").read_text()
        )
        assert manifest["chunks"]["c001"]["status"] == "ok"  # recovered
        assert record["error"] in sidecar.read_text()  # ...record survives

    def test_truncated_chunk_sidecar_captures_raw(self, tmp_path):
        """A truncation (no terminal sentinel) records the unparseable
        response, which raw.txt would otherwise lose to a re-run."""
        cut = json.dumps(claim("e1", "Anna Example", 1900))  # no sentinel
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                cut,  # c001 attempt 1 (fork)
                cut,  # c001 attempt 2 (fork)
                ndjson([claim("e2", "Bern Example", 1902)]),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        extractor.extract(transcription(), "Q23")
        record = json.loads(
            (tmp_path / "state" / "c001.fail.ndjson")
            .read_text()
            .splitlines()[0]
        )
        assert "truncated" in record["error"]
        assert record["raw"] == cut

    def test_resume_reuses_parsed_and_reparses_raw(self, tmp_path):
        """Resume must not re-invoke the model for chunks whose output
        (parsed or raw) already exists — a parser fix never costs a
        second model run."""
        first = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900)]),  # c001 ok
                RuntimeError("timeout"),  # c002 fails (fork)
                RuntimeError("timeout"),  # c002 fails (inline)
                RuntimeError("timeout"),  # c002 retry
            ]
        )
        extractor = self.extractor(
            first, tmp_path, budget=LINE_LENGTH, retries=1
        )
        extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c002"]

        # simulate: c002's raw arrived but parsing had failed; a valid
        # raw file now exists on disk
        (tmp_path / "state" / "c002.raw.txt").write_text(
            ndjson([claim("e2", "Bern Example", 1902)])
        )
        second = ScriptedLlm([])  # NO responses: no model call allowed
        extractor = self.extractor(
            second, tmp_path, budget=LINE_LENGTH, resume=True
        )
        batch = extractor.extract(transcription(), "Q23")
        assert second.calls == []
        assert len(batch.claims) == 2
        assert [entity.id for entity in batch.entities] == ["e1", "e2"]
        assert extractor.failed_chunks == []

    def test_resume_with_changed_transcription_starts_fresh(self, tmp_path):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [
                        claim("e1", "Anna Example", 1900),
                        claim("e2", "Bern Example", 1902),
                    ]
                ),
            ]
        )
        state = tmp_path / "state"
        state.mkdir(parents=True)
        (state / "manifest.json").write_text(
            json.dumps({"transcription_sha256": "stale", "chunks": {}})
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000, resume=True)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert llm.calls[0][0] == "open"  # phase 1 re-ran

    def test_duplicate_claims_collapse(self, tmp_path):
        """Focus drift: two chunks emitting the same claim (identical
        after label normalization) collapse to one at merge."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [
                        claim("e1", "Anna Example", 1900),
                        claim("e1", "Anna", 1900),
                    ]
                ),  # alias variant normalizes into an exact duplicate
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2

    def test_invalid_claims_land_in_rejects(self, tmp_path):
        bad = dict(claim("e1", "Anna Example", 1900), property="shoe_size")
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900), bad]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        rejects = json.loads((tmp_path / "state" / "rejects.json").read_text())
        assert len(rejects) == 1
        assert "shoe_size" in rejects[0]["problems"][0]


class KeyedLlm:
    """Thread-safe fake for concurrent chunks: responses selected by
    which assigned entity id appears in the prompt (pop-order fakes are
    racy under concurrency)."""

    model_identifier = "fake-model"

    def __init__(
        self, manifest_response: str, by_entity: dict[str, str]
    ) -> None:
        self.manifest_response = manifest_response
        self.by_entity = by_entity
        import threading

        self._lock = threading.Lock()
        self.concurrent_peak = 0
        self._active = 0

    def open_conversation(self, prompt):
        return self.manifest_response, "session-abc"

    def fork(self, handle, prompt):
        import time

        with self._lock:
            self._active += 1
            self.concurrent_peak = max(self.concurrent_peak, self._active)
        time.sleep(0.05)  # widen the overlap window
        try:
            for entity_id, response in self.by_entity.items():
                if f"- {entity_id}:" in prompt:
                    return response
            raise AssertionError(
                f"no scripted response for prompt: {prompt[:80]}"
            )
        finally:
            with self._lock:
                self._active -= 1

    def complete(self, prompt, image_paths=()):  # pragma: no cover
        raise AssertionError("concurrent test expects fork calls")


class TestParallelChunks:
    def test_concurrent_chunks_merge_identically(self, tmp_path):
        llm = KeyedLlm(
            entity_manifest_json(),
            {
                "e1": ndjson([claim("e1", "Anna Example", 1900)]),
                "e2": ndjson([claim("e2", "Bern Example", 1902)]),
            },
        )
        extractor = ChunkedClaimExtractor(
            llm,
            state_dir=tmp_path / "state",
            budget=LINE_LENGTH,  # -> one chunk per entity
            concurrency=2,
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == []
        # claims arrive in chunk order regardless of completion order
        assert [c.subject.local_id for c in batch.claims] == ["e1", "e2"]
        assert llm.concurrent_peak == 2  # the calls genuinely overlapped
        manifest = json.loads(
            (tmp_path / "state" / "manifest.json").read_text()
        )
        assert all(c["status"] == "ok" for c in manifest["chunks"].values())

    def test_concurrent_failure_isolates_to_its_chunk(self, tmp_path):
        llm = KeyedLlm(
            entity_manifest_json(),
            {
                # truncated (no sentinel) → fails every retry
                "e1": json.dumps(claim("e1", "Anna Example", 1900)),
                "e2": ndjson([claim("e2", "Bern Example", 1902)]),
            },
        )
        # KeyedLlm has no complete(); disable the inline fallback path by
        # keeping fork alive — retries return the same truncated response
        extractor = ChunkedClaimExtractor(
            llm,
            state_dir=tmp_path / "state",
            budget=LINE_LENGTH,
            retries=1,
            concurrency=2,
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c001"]
        assert [c.subject.label for c in batch.claims] == ["Bern Example"]


class TestFallbackLadder:
    def extractor(self, llm, tmp_path, **kwargs):
        return ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", **kwargs
        )

    def test_bad_manifest_falls_back_to_line_segmentation(self, tmp_path):
        """Manifest unusable twice -> line-range segmentation runs, with
        chunk-prefixed ids and no manifest in the batch."""
        llm = ScriptedLlm(
            [
                "no manifest here",  # manifest attempt 1
                json.dumps({"entities": [{"id": "x"}]}),  # attempt 2: invalid
                segmentation_json(),  # line-range segmentation
                ndjson(
                    [
                        claim("p1", "Anna Example", 1900),
                        claim("p2", "Bern Example", 1902),
                    ]
                ),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.used_fallback_manifest
        assert not extractor.used_fallback_segmentation
        assert batch.entities == []
        assert {c.subject.local_id for c in batch.claims} == {
            "c001:p1",
            "c001:p2",
        }
        manifest = json.loads(
            (tmp_path / "state" / "manifest.json").read_text()
        )
        assert manifest["strategy"] == "line-range"
        assert (tmp_path / "state" / "manifest-fallback.txt").exists()

    def test_full_fallback_to_offset_windows(self, tmp_path):
        """Manifest AND segmentation unusable -> offset windows, no
        forking (rejected turns must not be planted in chunk context),
        document sent inline."""
        llm = ScriptedLlm(
            [
                "junk",  # manifest 1
                "junk",  # manifest 2
                "junk",  # segmentation 1
                "junk",  # segmentation 2
                ndjson(
                    [
                        claim("p1", "Anna Example", 1900),
                        claim("p2", "Bern Example", 1902),
                    ]
                ),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.used_fallback_manifest
        assert extractor.used_fallback_segmentation
        assert len(batch.claims) == 2
        assert llm.calls[-1][0] == "complete"
        assert "Full transcription" in llm.calls[-1][1]
        assert (tmp_path / "state" / "segmentation-fallback.txt").exists()
