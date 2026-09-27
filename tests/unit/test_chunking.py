"""Offline tests for chunked extraction (ADR-0006/0007).

No instance, no LLM. The extractor's phases are stages in the stage store
(#87), so state is inspected through the store's records.
"""

import hashlib
import json

import pytest

from yizkr.claims import EntityRecord
from yizkr.pipeline import chunking
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
from yizkr.pipeline.stages import StageFailed, StageStore, doc_scope
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


class PlainLlm:
    """No conversation capability at all: no open_conversation or fork
    attribute, so the conversational protocol check fails (a property
    without a getter would still pass it on Python 3.11)."""

    model_identifier = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, image_paths=()):
        self.calls.append(("complete", prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def transcription() -> Transcription:
    return Transcription(text=DOC, engine="test", artifact_qid="Q23")


def store_scope(tmp_path, text: str = DOC):
    """The store a standalone extractor keeps in tmp_path/state, and the
    scope it names the document by (its transcription's SHA-1)."""
    scope = doc_scope(hashlib.sha1(text.encode()).hexdigest())
    return StageStore(tmp_path / "state"), scope


def stored(tmp_path, stage: str):
    store, scope = store_scope(tmp_path)
    return store.records(scope, stage)


def failures(tmp_path, stage: str):
    store, scope = store_scope(tmp_path)
    return store.failures(scope, stage)


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
                            "predicate": "godparent of",
                            "object": "e2",
                            "evidence": "stood godparent to",
                        }
                    }
                ),  # fmt: skip
                json.dumps(
                    {"done": True, "claims_emitted": 1, "gaps_emitted": 1}
                ),
            ]
        )
        (gap,) = parse_chunk_gaps(response)
        assert gap["predicate"] == "godparent of"
        assert gap["object"] == "e2"

    def test_prompt_routes_persecution_to_events_not_gaps(self, tmp_path):
        """The gap examples named `employed by` — a property added in the
        same commit as the gap channel — and `deported to`/`imprisoned
        at`, which ADR-0014 models as events. The prompt was telling the
        model to gap exactly what the vocabulary already holds."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        ).extract(transcription(), "Q23")
        chunk_prompt = llm.calls[1][1]

        assert "Persecution and displacement are EVENTS" in chunk_prompt
        assert "`deportation`" in chunk_prompt
        assert "`imprisonment`" in chunk_prompt
        assert "`release`" in chunk_prompt
        assert "`participant` (the person)" in chunk_prompt
        # no longer offered as things to gap
        assert "employed by" not in chunk_prompt
        assert "deported to" not in chunk_prompt
        # the gap channel itself survives (ADR-0013 mechanism B)
        assert '{"gap":' in chunk_prompt
        assert "godparent of" in chunk_prompt

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

    def test_nested_local_id_is_not_hashed(self):
        # a model once double-wrapped an id ({"local_id": "e1"} where a
        # bare string belongs); normalize must not use that dict as a
        # records key (TypeError: unhashable type: 'dict') — it leaves the
        # malformed ref intact for validation to reject downstream
        raw = self.raw_claim()
        raw["subject"]["local_id"] = {"local_id": "e1"}
        records = {
            "e1": EntityRecord(id="e1", label="Alma Example", kind="human")
        }
        fixed = normalize_to_manifest(raw, records)  # must not raise
        assert fixed["subject"]["local_id"] == {"local_id": "e1"}
        assert "instance_of" not in fixed["subject"]


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
        assert extractor.context_mode == "fork"
        (manifest,) = stored(tmp_path, "entity_manifest")
        assert manifest.output()["strategy"] == "entity"
        assert manifest.meta["session_id"] == "session-abc"
        chunks = stored(tmp_path, "chunk")
        assert {chunk.meta["context_mode"] for chunk in chunks} == {"fork"}
        (merged,) = stored(tmp_path, "batch")
        coverage = json.loads(merged.extra("coverage.json"))
        assert coverage["entities_without_claims"] == []
        # the raw output of every call is stored beside the parsed output
        assert manifest.raw() == [entity_manifest_json()]
        assert sorted(
            raw for chunk in chunks for raw in chunk.raw()
        ) == sorted(
            [
                ndjson([claim("e1", "Anna", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )

    def test_prompts_carry_manifest_descriptions(self, tmp_path):
        """The manifest already authors what each kind and property
        means — which way a relation points, what kind of entity a value
        must be. Rendering bare keys made the model guess both."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )

        manifest_prompt = llm.calls[0][1]
        assert (
            "organization: an organization acting as an agent"
            in manifest_prompt
        )
        assert (
            "deportation: event of a person being forcibly deported"
            in manifest_prompt
        )
        # artifact types and vocabulary values are real items but not
        # kinds the extractor may type a document entity as
        assert "photograph" not in manifest_prompt
        assert "circa" not in manifest_prompt

        chunk_prompt = llm.calls[1][1]
        assert (
            "employer: wikibase-item — organization the person works or "
            "worked for" in chunk_prompt
        )
        assert (
            "location: wikibase-item — place where the event or subject "
            "is located" in chunk_prompt
        )
        assert (
            "depicts: wikibase-item — person or thing depicted in the "
            "artifact" in chunk_prompt
        )

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
        (merged,) = stored(tmp_path, "batch")
        rejects = json.loads(merged.extra("rejects.json"))
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
        (failure,) = failures(tmp_path, "chunk")
        assert failure["item"] == "c001"
        assert "engine down" in failure["reason"]
        # the good chunk is stored; the partial batch is not
        assert len(stored(tmp_path, "chunk")) == 1
        assert stored(tmp_path, "batch") == []

    def test_failed_chunk_log_survives_recovery(self, tmp_path):
        """The reason a chunk first failed lives in an append-only log
        that a later successful re-run does not rewrite; the re-run
        recomputes only the failed chunk."""
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
        (record,) = failures(tmp_path, "chunk")
        assert record["item"] == "c001"
        assert record["attempts"] == 2
        assert "engine down" in record["reason"]
        assert "raw" not in record  # a call failure left no response

        # the re-run reuses the manifest and c002, and calls only for c001
        # (inline: a reused manifest has no live session to fork)
        second = ScriptedLlm([ndjson([claim("e1", "Anna Example", 1900)])])
        extractor = self.extractor(
            second, tmp_path, budget=LINE_LENGTH, resume=True
        )
        batch = extractor.extract(transcription(), "Q23")
        assert [call[0] for call in second.calls] == ["complete"]
        assert extractor.failed_chunks == []
        assert len(batch.claims) == 2
        assert len(stored(tmp_path, "batch")) == 1
        assert failures(tmp_path, "chunk") == [record]  # ...record survives

    def test_truncated_chunk_failure_keeps_raw(self, tmp_path):
        """A truncation (no terminal sentinel) logs the unparseable
        response with the failure, so a re-run cannot lose it."""
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
        (record,) = failures(tmp_path, "chunk")
        assert "truncated" in record["reason"]
        assert record["raw"] == cut

    def test_parser_fix_recovers_a_logged_failure(self, tmp_path, monkeypatch):
        """A response that failed to parse is kept with its failure, and a
        re-run re-parses it before calling the model — a parser fix never
        costs a second model run (ADR-0006)."""
        unfinished = json.dumps(claim("e2", "Bern Example", 1902))
        first = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900)]),  # c001 ok
                unfinished,  # c002 attempt 1: no sentinel
                unfinished,  # c002 attempt 2
            ]
        )
        extractor = self.extractor(
            first, tmp_path, budget=LINE_LENGTH, retries=1
        )
        extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c002"]

        # a parser fix: a response without the sentinel is now accepted
        strict = chunking.parse_chunk_response

        def lenient(response, warn=None):
            try:
                return strict(response, warn=warn)
            except ChunkIncomplete:
                return strict(
                    response + '\n{"done": true, "claims_emitted": 1}'
                )

        monkeypatch.setattr(chunking, "parse_chunk_response", lenient)
        second = ScriptedLlm([])  # NO responses: no model call allowed
        extractor = self.extractor(
            second, tmp_path, budget=LINE_LENGTH, resume=True
        )
        batch = extractor.extract(transcription(), "Q23")
        assert second.calls == []
        assert len(batch.claims) == 2
        assert [entity.id for entity in batch.entities] == ["e1", "e2"]
        assert extractor.failed_chunks == []
        recovered = [
            record
            for record in stored(tmp_path, "chunk")
            if record.meta.get("recovered")
        ]
        assert [record.raw() for record in recovered] == [[unfinished]]

    def test_parse_version_bump_reparses_without_a_call(
        self, tmp_path, monkeypatch
    ):
        """A stored chunk whose parser version is out of date is re-parsed
        from its raw response; the call is never repeated, and an
        unchanged parse leaves the batch's key, and the batch, alone."""
        import dataclasses

        from yizkr.pipeline import stageversions

        first = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        self.extractor(first, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        stages = dict(stageversions.STAGES)
        stages["chunk_parse"] = dataclasses.replace(
            stages["chunk_parse"], version=2
        )
        monkeypatch.setattr(stageversions, "STAGES", stages)
        second = ScriptedLlm([])
        extractor = self.extractor(
            second, tmp_path, budget=LINE_LENGTH, resume=True
        )
        batch = extractor.extract(transcription(), "Q23")
        assert second.calls == []
        assert len(batch.claims) == 2
        assert len(stored(tmp_path, "batch")) == 1

    def test_resume_with_changed_transcription_starts_fresh(self, tmp_path):
        both = ndjson(
            [
                claim("e1", "Anna Example", 1900),
                claim("e2", "Bern Example", 1902),
            ]
        )
        first = ScriptedLlm([entity_manifest_json(), both])
        self.extractor(first, tmp_path, budget=10_000).extract(
            transcription(), "Q23"
        )
        changed = Transcription(
            text=DOC.replace("Dinner is at 7pm.", "Dinner is at 8pm."),
            engine="test",
            artifact_qid="Q23",
        )
        second = ScriptedLlm([entity_manifest_json(), both])
        extractor = self.extractor(
            second, tmp_path, budget=10_000, resume=True
        )
        batch = extractor.extract(changed, "Q23")
        assert len(batch.claims) == 2
        assert second.calls[0][0] == "open"  # phase 1 re-ran

    def test_resume_reuses_everything_unchanged(self, tmp_path):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        first = self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        again = ScriptedLlm([])
        extractor = self.extractor(
            again, tmp_path, budget=LINE_LENGTH, resume=True
        )
        second = extractor.extract(transcription(), "Q23")
        assert again.calls == []
        assert second == first
        assert extractor.used_fallback_manifest is False

    def test_without_resume_every_stage_is_recomputed(self, tmp_path):
        def script():
            return ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([claim("e1", "Anna Example", 1900)]),
                    ndjson([claim("e2", "Bern Example", 1902)]),
                ]
            )

        self.extractor(script(), tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        llm = script()
        self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        assert [call[0] for call in llm.calls] == ["open", "fork", "fork"]
        # recomputed in place: one record per key
        assert len(stored(tmp_path, "chunk")) == 2

    def test_reuse_rebinds_the_artifact_qid(self, tmp_path):
        """The artifact QID is a binding, not a key component: after a
        reset gives the artifact a new QID, every output is reused and
        rebound to it."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Anna Example", 1900)]),
                ndjson([claim("e2", "Bern Example", 1902)]),
            ]
        )
        self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        again = ScriptedLlm([])
        batch = self.extractor(
            again, tmp_path, budget=LINE_LENGTH, resume=True
        ).extract(transcription(), "Q57")
        assert again.calls == []
        assert {claim.source.artifact.qid for claim in batch.claims} == {"Q57"}
        assert len(stored(tmp_path, "batch")) == 1

    def test_engine_outage_fails_the_manifest_stage(self, tmp_path):
        """A manifest the engine never answered is a failure to retry,
        not a fallback to store and reuse."""
        llm = ScriptedLlm([RuntimeError("down"), RuntimeError("down")])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        with pytest.raises(StageFailed, match="no engine response"):
            extractor.extract(transcription(), "Q23")
        assert stored(tmp_path, "entity_manifest") == []
        (failure,) = failures(tmp_path, "entity_manifest")
        assert "down" in failure["reason"]

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
        (merged,) = stored(tmp_path, "batch")
        rejects = json.loads(merged.extra("rejects.json"))
        assert len(rejects) == 1
        assert "shoe_size" in rejects[0]["problems"][0]

    def test_nested_local_id_rejected_not_fatal(self, tmp_path):
        # regression: a double-nested local_id once crashed the whole
        # merge (unhashable dict, TypeError); it must degrade to a single
        # reject while the document's other claims survive
        bad = claim("e1", "Alma Example", 1900)
        bad["subject"]["local_id"] = {"local_id": "e1"}
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
        (merged,) = stored(tmp_path, "batch")
        rejects = json.loads(merged.extra("rejects.json"))
        assert len(rejects) == 1


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
        assert len(stored(tmp_path, "chunk")) == 2
        assert failures(tmp_path, "chunk") == []

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
        (manifest,) = stored(tmp_path, "entity_manifest")
        assert manifest.output()["strategy"] == "line-range"
        # a fallback is stored, flagged for attention
        assert any(
            "fell back to line-range" in reason
            for reason in manifest.attention
        )
        assert len(manifest.raw()) == 3  # two manifest turns, segmentation

    def test_fallback_tier_offers_the_same_kinds_as_the_manifest(
        self, tmp_path
    ):
        """The line-range tier used to carry its own hand-written kind
        list, which had drifted behind the manifest by eight kinds."""
        llm = ScriptedLlm(
            [
                "no manifest here",
                "still no manifest",
                segmentation_json(),
                ndjson([claim("p1", "Anna Example", 1900)]),
            ]
        )
        self.extractor(llm, tmp_path, budget=10_000).extract(
            transcription(), "Q23"
        )

        chunk_prompt = llm.calls[-1][1]
        for kind in ("deportation", "family", "vessel", "unclassified_entity"):
            assert kind in chunk_prompt, kind

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
        (manifest,) = stored(tmp_path, "entity_manifest")
        assert any(
            "fell back to line windows" in reason
            for reason in manifest.attention
        )
