"""Offline tests for chunked extraction (ADR-0006). No instance, no LLM."""

import json

import pytest

from yizkr.pipeline.chunking import (
    ChunkedClaimExtractor,
    ChunkIncomplete,
    DocumentSegmentation,
    SegmentationUnit,
    pack_units,
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
        return DocumentSegmentation.model_validate_json(segmentation_json()).units

    def test_skips_non_extract_units(self):
        chunks = pack_units(self.units(), DOC.splitlines(), budget=10_000)
        assert [[u.id for u in chunk] for chunk in chunks] == [["u2", "u3"]]

    def test_budget_splits_chunks(self):
        line_length = len(unit_text(DOC.splitlines(), self.units()[1]))
        chunks = pack_units(self.units(), DOC.splitlines(), budget=line_length)
        assert [[u.id for u in chunk] for chunk in chunks] == [["u2"], ["u3"]]

    def test_oversized_unit_gets_own_chunk(self):
        chunks = pack_units(self.units(), DOC.splitlines(), budget=1)
        assert len(chunks) == 2


class TestChunkResponseContract:
    def test_sentinel_required(self):
        with pytest.raises(ChunkIncomplete, match="sentinel"):
            parse_chunk_response(json.dumps(claim("p1", "Anna Example", 1900)))

    def test_sentinel_count_must_match(self):
        with pytest.raises(ChunkIncomplete, match="says 5"):
            parse_chunk_response(ndjson([claim("p1", "Anna Example", 1900)], emitted=5))

    def test_valid_ndjson_parses(self):
        claims = parse_chunk_response(ndjson([claim("p1", "Anna Example", 1900)]))
        assert len(claims) == 1


class TestPrefixLocalIds:
    def test_prefixes_subject_value_and_qualifier_refs(self):
        raw = {
            "subject": {"kind": "local", "local_id": "p1", "label": "A"},
            "property": "spouse",
            "value": {
                "type": "item",
                "value": {"kind": "local", "local_id": "p2", "label": "B"},
            },
            "qualifiers": [
                {
                    "property": "location",
                    "value": {
                        "type": "item",
                        "value": {"kind": "local", "local_id": "pl1", "label": "X"},
                    },
                }
            ],
            "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
        }
        fixed = prefix_local_ids(raw, "c001")
        assert fixed["subject"]["local_id"] == "c001:p1"
        assert fixed["value"]["value"]["local_id"] == "c001:p2"
        assert fixed["qualifiers"][0]["value"]["value"]["local_id"] == "c001:pl1"
        # qid refs and the original dict untouched
        assert fixed["source"]["artifact"] == {"kind": "qid", "qid": "Q23"}
        assert raw["subject"]["local_id"] == "p1"


class TestChunkedExtractor:
    def extractor(self, llm, tmp_path, **kwargs):
        return ChunkedClaimExtractor(llm, state_dir=tmp_path / "state", **kwargs)

    def test_fork_flow_end_to_end(self, tmp_path):
        """Segmentation opens a conversation; each chunk is a fork that
        sees only unit instructions (document context arrives via the
        fork, not the prompt); ids come back chunk-prefixed."""
        llm = ScriptedLlm(
            [
                segmentation_json(),  # open_conversation
                ndjson([claim("p1", "Anna Example", 1900)]),  # fork c001
                ndjson([claim("p1", "Bern Example", 1902)]),  # fork c002
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=len(DOC.splitlines()[1]))
        batch = extractor.extract(transcription(), "Q23")

        assert [call[0] for call in llm.calls] == ["open", "fork", "fork"]
        assert extractor.failed_chunks == []
        assert len(batch.claims) == 2
        assert {c.subject.local_id for c in batch.claims} == {"c001:p1", "c002:p1"}
        # fork prompts rely on conversation context, not an inline document
        assert "Full transcription" not in llm.calls[1][1]
        assert "Anna Example" in llm.calls[1][1]
        # state persisted
        state = tmp_path / "state"
        manifest = json.loads((state / "manifest.json").read_text())
        assert manifest["context_mode"] == "fork"
        assert manifest["session_id"] == "session-abc"
        assert manifest["chunks"]["c001"]["status"] == "ok"
        coverage = json.loads((state / "coverage.json").read_text())
        assert coverage["units_without_matching_subject"] == []
        assert coverage["unsegmented_line_ranges"] == [[4, 4]]

    def test_plain_client_gets_inline_document(self, tmp_path):
        llm = PlainLlm(
            [
                segmentation_json(),
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
        assert len(batch.claims) == 2
        # chunk prompt carries the whole numbered transcription inline
        assert "Full transcription" in llm.calls[1][1]
        assert "1: Family record" in llm.calls[1][1]

    def test_sentinel_mismatch_retries_then_succeeds(self, tmp_path):
        good = ndjson([claim("p1", "Anna Example", 1900)])
        llm = ScriptedLlm(
            [
                segmentation_json(),
                ndjson([claim("p1", "Anna Example", 1900)], emitted=9),  # bad
                good,  # retry
                ndjson([claim("p1", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=len(DOC.splitlines()[1]), retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []

    def test_failed_chunk_yields_partial_batch(self, tmp_path):
        llm = ScriptedLlm(
            [
                segmentation_json(),
                RuntimeError("engine down"),  # c001 attempt 1 (fork)
                RuntimeError("engine down"),  # c001 attempt 1 (inline fallback)
                RuntimeError("engine down"),  # c001 attempt 2 (inline)
                ndjson([claim("p1", "Bern Example", 1902)]),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=len(DOC.splitlines()[1]), retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c001"]
        assert [c.subject.label for c in batch.claims] == ["Bern Example"]
        manifest = json.loads((tmp_path / "state" / "manifest.json").read_text())
        assert manifest["chunks"]["c001"]["status"].startswith("failed")

    def test_segmentation_fallback_windows_and_inline_mode(self, tmp_path):
        """Two invalid segmentations -> line windows, no forking (the
        rejected segmentation turn must not be planted in chunk
        context), document sent inline."""
        llm = ScriptedLlm(
            [
                "not json at all",  # segmentation attempt 1
                json.dumps({"units": [{"id": "x"}]}),  # attempt 2: invalid
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
        assert extractor.used_fallback_segmentation
        assert len(batch.claims) == 2
        # chunk ran as complete(), not fork(), with the document inline
        assert llm.calls[-1][0] == "complete"
        assert "Full transcription" in llm.calls[-1][1]
        assert (tmp_path / "state" / "segmentation-fallback.txt").exists()

    def test_resume_reuses_parsed_and_reparses_raw(self, tmp_path):
        """Resume must not re-invoke the model for chunks whose output
        (parsed or raw) already exists — a parser fix never costs a
        second model run."""
        first = ScriptedLlm(
            [
                segmentation_json(),
                ndjson([claim("p1", "Anna Example", 1900)]),  # c001 ok
                RuntimeError("timeout"),  # c002 fails (fork)
                RuntimeError("timeout"),  # c002 fails (inline)
                RuntimeError("timeout"),  # c002 retry
            ]
        )
        extractor = self.extractor(
            first, tmp_path, budget=len(DOC.splitlines()[1]), retries=1
        )
        extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c002"]

        # simulate: c002's raw actually arrived but parsing had failed;
        # a valid raw file now exists on disk
        (tmp_path / "state" / "c002.raw.txt").write_text(
            ndjson([claim("p1", "Bern Example", 1902)])
        )
        second = ScriptedLlm([])  # NO responses: no model call allowed
        extractor = self.extractor(
            second, tmp_path, budget=len(DOC.splitlines()[1]), resume=True
        )
        batch = extractor.extract(transcription(), "Q23")
        assert second.calls == []
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []

    def test_resume_with_changed_transcription_starts_fresh(self, tmp_path):
        llm = ScriptedLlm(
            [
                segmentation_json(),
                ndjson(
                    [
                        claim("p1", "Anna Example", 1900),
                        claim("p2", "Bern Example", 1902),
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
        assert llm.calls[0][0] == "open"  # segmentation re-ran

    def test_duplicate_claims_across_chunks_collapse(self, tmp_path):
        """Focus drift: both chunks emit Anna's birth. After id
        prefixing the dicts differ only by local id — dedupe happens on
        the exact post-prefix claim, so cross-chunk near-duplicates
        coexist (they carry different ids) and land in review, while
        WITHIN a chunk exact duplicates collapse."""
        duplicated = [
            claim("p1", "Anna Example", 1900),
            claim("p1", "Anna Example", 1900),
        ]
        llm = ScriptedLlm(
            [
                segmentation_json(),
                ndjson(duplicated),
                ndjson([claim("p1", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=len(DOC.splitlines()[1]))
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2  # within-chunk duplicate collapsed

    def test_invalid_claims_land_in_rejects(self, tmp_path):
        bad = dict(claim("p1", "Anna Example", 1900), property="shoe_size")
        llm = ScriptedLlm(
            [
                segmentation_json(),
                ndjson([claim("p1", "Anna Example", 1900), bad]),
                ndjson([claim("p1", "Bern Example", 1902)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=len(DOC.splitlines()[1]))
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        rejects = json.loads((tmp_path / "state" / "rejects.json").read_text())
        assert len(rejects) == 1
        assert "shoe_size" in rejects[0]["problems"][0]
