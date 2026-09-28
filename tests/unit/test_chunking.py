"""Offline tests for chunked extraction (ADR-0006/0007).

No instance, no LLM and no LLM cache: the extractor calls a scripted
client directly, and its state is read from its attributes and the side
outputs in its state directory. tests/unit/test_llmcache.py takes the
extractor through the cache (#225).
"""

import json
import threading

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
from yizkr.pipeline.llm import ConversationChanged
from yizkr.pipeline.transcribe import Transcription

# a two-person document: one claimable line per person, one logistics line
DOC = "\n".join(
    [
        "Family record",  # 1: boilerplate
        "Hildegard Löbig, b. 1876 in Fläckfeld.",  # 2
        "Sebastian Löbig, b. 1862 in Flörburg.",  # 3
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
            "transcription_excerpt": {"text": f"b. {year}", "language": "en"},
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
                    "label": "Hildegard Löbig",
                    "aliases": ["Hildegard"],
                    "kind": "human",
                    "summary": "b. 1876 Fläckfeld",
                    "mention_lines": [2],
                },
                {
                    "id": "e2",
                    "label": "Sebastian Löbig",
                    "aliases": [],
                    "kind": "human",
                    "summary": "b. 1862 Flörburg",
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
                    "label": "Hildegard Löbig entry",
                    "kind": "person entry",
                    "first_line": 2,
                    "last_line": 2,
                    "context": "generation 1",
                    "extract": True,
                },
                {
                    "id": "u3",
                    "label": "Sebastian Löbig entry",
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


def side_output(tmp_path, name: str):
    """A side output the extractor wrote to its state directory, parsed,
    or None when there is none."""
    path = tmp_path / "state" / name
    if not path.exists():
        return None
    if name.endswith(".ndjson"):
        return [json.loads(line) for line in path.read_text().splitlines()]
    return json.loads(path.read_text())


class Rejecting:
    """A client that takes rejections (as the LLM cache's does), around a
    scripted one: it records each rejected response with its reason, and
    each conversation closed."""

    def __init__(self, client) -> None:
        self.client = client
        self.rejected: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.model_identifier = client.model_identifier

    def complete(self, prompt, image_paths=()):
        return self.client.complete(prompt, image_paths)

    def open_conversation(self, prompt):
        return self.client.open_conversation(prompt)

    def fork(self, handle, prompt):
        return self.client.fork(handle, prompt)

    def reject(self, response, reason):
        self.rejected.append((response, reason))

    def close_conversation(self, handle):
        self.closed.append(handle)


class Attempting(Rejecting):
    """A client that takes rejections and tells attempts at a call apart
    (as the LLM cache's does): `attempt(n)` gives a sibling whose calls
    are logged in `asked` with n, beside the attempt-0 calls of the client
    itself."""

    def __init__(self, client, number: int = 0, parent=None) -> None:
        super().__init__(client)
        self.number = number
        if parent is not None:
            self.rejected, self.closed = parent.rejected, parent.closed
        self.asked: list[tuple[str, int]] = (
            parent.asked if parent is not None else []
        )

    def attempt(self, number):
        return Attempting(self.client, number, parent=self)

    def complete(self, prompt, image_paths=()):
        self.asked.append(("complete", self.number))
        return super().complete(prompt, image_paths)

    def open_conversation(self, prompt):
        self.asked.append(("open", self.number))
        return super().open_conversation(prompt)

    def fork(self, handle, prompt):
        self.asked.append(("fork", self.number))
        return super().fork(handle, prompt)


def placeholder_claims(*ids: str) -> str:
    """A complete chunk response with one claim per manifest id, labelled
    by the id itself: the merge takes each label from the manifest."""
    return ndjson([claim(local_id, local_id, 1917) for local_id in ids])


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
            parse_chunk_response(
                json.dumps(claim("e1", "Hildegard Löbig", 1876))
            )

    def test_count_mismatch_keeps_claims_and_warns(self):
        # a present sentinel proves completeness; a miscounted
        # claims_emitted keeps every parsed claim and only warns
        warnings: list[str] = []
        claims = parse_chunk_response(
            ndjson([claim("e1", "Hildegard Löbig", 1876)], emitted=5),
            warn=warnings.append,
        )
        assert len(claims) == 1
        assert warnings and "reported 5" in warnings[0]

    def test_valid_ndjson_parses(self):
        claims = parse_chunk_response(
            ndjson([claim("e1", "Hildegard Löbig", 1876)])
        )
        assert len(claims) == 1

    def test_gap_lines_and_extended_sentinel_do_not_break_claims(self):
        # ADR-0013: gap lines + gaps_emitted in the sentinel are ignored
        # by the claim parser (non-breaking), and the claim count still
        # matches the sentinel's claims_emitted
        response = "\n".join(
            [
                json.dumps(claim("e1", "Hildegard Löbig", 1876)),
                json.dumps(
                    {
                        "gap": {
                            "subject": "e1",
                            "predicate": "deported to",
                            "object": "Klurnau",
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
                json.dumps(claim("e1", "Hildegard Löbig", 1876)),
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
                ndjson([claim("e1", "Hildegard", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
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
            "subject": {
                "kind": "local",
                "local_id": "e1",
                "label": "Hildegard",
            },
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
            "e1": EntityRecord(id="e1", label="Hildegard Löbig", kind="human"),
            "e2": EntityRecord(id="e2", label="Sebastian Löbig", kind="human"),
        }
        fixed = normalize_to_manifest(self.raw_claim(), records)
        assert fixed["subject"]["label"] == "Hildegard Löbig"
        assert fixed["subject"]["instance_of"] == "human"
        assert fixed["value"]["value"]["label"] == "Sebastian Löbig"
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
            "e1": EntityRecord(id="e1", label="Hildegard Löbig", kind="human")
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
                ndjson(
                    [claim("e1", "Hildegard", 1876)]
                ),  # fork c001 (alias label)
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),  # fork c002
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")

        assert [call[0] for call in llm.calls] == ["open", "fork", "fork"]
        assert extractor.failed_chunks == []
        assert not extractor.used_fallback_manifest
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        # alias label normalized to the manifest's canonical form
        hildegard = next(c for c in batch.claims if c.subject.local_id == "e1")
        assert hildegard.subject.label == "Hildegard Löbig"
        # the manifest travels with the batch (reconcile needs it)
        assert [entity.id for entity in batch.entities] == ["e1", "e2"]
        # fork prompts carry the assignment, not an inline document
        assert "Full transcription" not in llm.calls[1][1]
        assert (
            "e1 = Hildegard Löbig" in llm.calls[1][1]
        )  # roster in every chunk
        assert extractor.context_mode == "fork"
        assert extractor.attention == []
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["strategy"] == "entity"
        assert coverage["entities_without_claims"] == []
        assert side_output(tmp_path, "rejects.json") is None

    def test_prompts_carry_manifest_descriptions(self, tmp_path):
        """The manifest already authors what each kind and property
        means — which way a relation points, what kind of entity a value
        must be. Rendering bare keys made the model guess both."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
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
                ndjson([claim("e1", "Hildegard", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
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
                        claim("e1", "Hildegard Löbig", 1876),
                        claim("e2", "Sebastian Löbig", 1862),
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
                        claim("e1", "Hildegard Löbig", 1876),
                        claim("e99", "Nobody", 1800),
                    ]
                ),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        rejects = side_output(tmp_path, "rejects.json")
        assert len(rejects) == 1
        assert "entity manifest" in rejects[0]["problems"][0]

    def test_truncation_retries_then_succeeds(self, tmp_path):
        # a missing sentinel is real truncation: retryable, unlike a
        # count mismatch (which keeps its claims — next test)
        truncated = json.dumps(
            claim("e1", "Hildegard Löbig", 1876)
        )  # no sentinel
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                truncated,  # c001 attempt 1 (fork) — cut off
                ndjson([claim("e1", "Hildegard Löbig", 1876)]),  # c001 retry
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []

    def test_a_truncated_response_is_rejected_before_the_retry(self, tmp_path):
        """A client that keeps responses (the LLM cache's) is told which
        one was cut off, so the retry, and a later run, ask the engine
        again instead of being answered with it."""
        cut = json.dumps(claim("e1", "e1", 1917))  # no sentinel
        llm = Rejecting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    cut,  # c001 attempt 1 (fork)
                    placeholder_claims("e1"),  # c001 attempt 2
                    placeholder_claims("e2"),  # c002
                ]
            )
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        ((response, reason),) = llm.rejected
        assert response == cut
        assert reason.startswith("chunk: no terminal sentinel")

    @pytest.mark.parametrize("kept", [1, 2])
    def test_a_kept_cut_off_answer_spends_no_retry(self, tmp_path, kept):
        """A cut-off answer the client kept from an earlier call is
        rejected and asked for once more without spending a retry: the
        engine then has the tries it would have without the cache (#230).
        A second kept answer is no longer free."""

        class Keeping(Rejecting):
            def reject(self, response, reason):
                super().reject(response, reason)
                return len(self.rejected) <= kept

        cut = json.dumps(claim("e1", "e1", 1917))  # no sentinel
        llm = Keeping(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    cut,  # c001: an answer kept from an earlier call
                    *([cut] if kept == 2 else []),  # a second kept answer
                    placeholder_claims("e1"),  # c001: the engine's answer
                    placeholder_claims("e2"),  # c002
                ]
            )
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=0
        )
        batch = extractor.extract(transcription(), "Q23")
        if kept == 1:
            assert len(batch.claims) == 2
            assert extractor.failed_chunks == []
        else:
            assert extractor.failed_chunks == ["c001"]
        assert [response for response, _ in llm.rejected] == [cut] * kept

    def test_count_mismatch_chunk_accepted_without_retry(self, tmp_path):
        # the c007/c019 case: a complete response whose sentinel
        # undercounts is kept as-is and does NOT burn a retry
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [claim("e1", "Hildegard Löbig", 1876)], emitted=99
                ),  # miscount
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == []
        assert {c.subject.label for c in batch.claims} == {
            "Hildegard Löbig",
            "Sebastian Löbig",
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
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), "Q23")
        assert extractor.failed_chunks == ["c001"]
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]
        assert "engine down" in extractor.failure_reasons["c001"]
        # a partial extraction writes no side outputs
        assert side_output(tmp_path, "coverage.json") is None

    def test_side_outputs_follow_the_latest_extraction(self, tmp_path):
        """A side output the latest complete extraction has nothing for is
        removed, so a reader never takes an older run's rejects for this
        one's."""
        bad = dict(claim("e1", "e1", 1917), property="shoe_size")
        self.extractor(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([claim("e1", "e1", 1917), bad]),
                    placeholder_claims("e2"),
                ]
            ),
            tmp_path,
            budget=LINE_LENGTH,
        ).extract(transcription(), "Q23")
        assert len(side_output(tmp_path, "rejects.json")) == 1
        self.extractor(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    placeholder_claims("e1"),
                    placeholder_claims("e2"),
                ]
            ),
            tmp_path,
            budget=LINE_LENGTH,
        ).extract(transcription(), "Q23")
        assert side_output(tmp_path, "rejects.json") is None
        assert side_output(tmp_path, "coverage.json")["entities_total"] == 2

    def test_without_a_state_directory_nothing_is_written(self, tmp_path):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                placeholder_claims("e1"),
                placeholder_claims("e2"),
            ]
        )
        ChunkedClaimExtractor(llm, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        assert list(tmp_path.iterdir()) == []

    def test_the_conversation_is_released_after_the_chunks(self, tmp_path):
        llm = Rejecting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    placeholder_claims("e1"),
                    placeholder_claims("e2"),
                ]
            )
        )
        self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), "Q23"
        )
        assert llm.closed == ["session-abc"]
        assert llm.rejected == []

    def test_engine_outage_fails_the_extraction(self, tmp_path):
        """A manifest the engine never answered is a failure to retry,
        not a fallback to extract by."""
        llm = ScriptedLlm([RuntimeError("down"), RuntimeError("down")])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        with pytest.raises(RuntimeError, match="no engine response"):
            extractor.extract(transcription(), "Q23")
        assert [call[0] for call in llm.calls] == ["open", "open"]
        assert side_output(tmp_path, "coverage.json") is None

    def test_duplicate_claims_collapse(self, tmp_path):
        """Focus drift: two chunks emitting the same claim (identical
        after label normalization) collapse to one at merge."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(
                    [
                        claim("e1", "Hildegard Löbig", 1876),
                        claim("e1", "Hildegard", 1876),
                    ]
                ),  # alias variant normalizes into an exact duplicate
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2

    def test_invalid_claims_land_in_rejects(self, tmp_path):
        bad = dict(claim("e1", "Hildegard Löbig", 1876), property="shoe_size")
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard Löbig", 1876), bad]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        rejects = side_output(tmp_path, "rejects.json")
        assert len(rejects) == 1
        assert "shoe_size" in rejects[0]["problems"][0]

    def test_nested_local_id_rejected_not_fatal(self, tmp_path):
        # regression: a double-nested local_id once crashed the whole
        # merge (unhashable dict, TypeError); it must degrade to a single
        # reject while the document's other claims survive
        bad = claim("e1", "Hildegard Löbig", 1876)
        bad["subject"]["local_id"] = {"local_id": "e1"}
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard Löbig", 1876), bad]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert len(batch.claims) == 2
        assert len(side_output(tmp_path, "rejects.json")) == 1


class KeyedLlm:
    """Thread-safe fake for concurrent chunks: responses selected by
    which assigned entity id appears in the prompt (pop-order fakes are
    racy under concurrency). With a `barrier`, every fork waits there
    until the barrier's other parties are in flight too."""

    model_identifier = "fake-model"

    def __init__(
        self,
        manifest_response: str,
        by_entity: dict[str, str],
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.manifest_response = manifest_response
        self.by_entity = by_entity
        self.barrier = barrier
        self._lock = threading.Lock()
        self.concurrent_peak = 0
        self._active = 0

    def open_conversation(self, prompt):
        return self.manifest_response, "session-abc"

    def fork(self, handle, prompt):
        with self._lock:
            self._active += 1
            self.concurrent_peak = max(self.concurrent_peak, self._active)
        if self.barrier is not None:
            self.barrier.wait()
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
        # both chunk calls must be in flight at once to pass the barrier:
        # run one at a time, the first waits out the timeout and fails
        llm = KeyedLlm(
            entity_manifest_json(),
            {
                "e1": ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                "e2": ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            },
            barrier=threading.Barrier(2, timeout=30),
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

    def test_a_changed_conversation_restarts_the_extraction(self, tmp_path):
        """A fork that finds its conversation changed (the LLM cache's
        client raises ConversationChanged when a replayed opening turn
        answers differently) voids the pass: the extraction opens the
        conversation again and forks from its new answer, and no chunk
        counts as failed."""
        opened = []

        class Changing(KeyedLlm):
            def open_conversation(self, prompt):
                opened.append(prompt)
                return super().open_conversation(prompt)

            def fork(self, handle, prompt):
                if len(opened) == 1:
                    raise ConversationChanged("answered differently")
                return super().fork(handle, prompt)

        llm = Changing(
            entity_manifest_json(),
            {
                "e1": ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                "e2": ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            },
        )
        extractor = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        )
        batch = extractor.extract(transcription(), "Q23")
        assert len(opened) == 2
        assert extractor.failed_chunks == []
        assert [c.subject.local_id for c in batch.claims] == ["e1", "e2"]

    def test_concurrent_failure_isolates_to_its_chunk(self, tmp_path):
        llm = KeyedLlm(
            entity_manifest_json(),
            {
                # truncated (no sentinel) → fails every retry
                "e1": json.dumps(claim("e1", "Hildegard Löbig", 1876)),
                "e2": ndjson([claim("e2", "Sebastian Löbig", 1862)]),
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
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]


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
                        claim("p1", "Hildegard Löbig", 1876),
                        claim("p2", "Sebastian Löbig", 1862),
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
        # the fallback is recorded, and flagged for attention
        (reason,) = extractor.attention
        assert reason.startswith(
            "entity manifest fell back to line-range segmentation"
        )
        coverage = side_output(tmp_path, "coverage.json")
        assert set(coverage["fallback"]) == {"manifest"}

    def test_refused_tiers_are_attempted_apart_and_kept(self, tmp_path):
        """The manifest and the segmentation are each asked in two
        attempts, the second through the client's `attempt(1)`, which a
        client that keeps answers keys apart (#225): a refused answer is
        kept, not rejected, so a later run replays the same attempts and
        takes the same fallback. Each refused conversation is released
        before the next try."""
        llm = Attempting(
            ScriptedLlm(
                [
                    "no manifest here",
                    "still no manifest",
                    "no segmentation here",
                    "still no segmentation",
                    placeholder_claims("w0001"),
                ]
            )
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        extractor.extract(transcription(), "Q23")
        assert extractor.used_fallback_segmentation
        assert llm.asked == [
            ("open", 0),
            ("open", 1),
            ("open", 0),
            ("open", 1),
            ("complete", 0),
        ]
        assert llm.rejected == []
        assert llm.closed == ["session-abc"] * 4

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
                ndjson([claim("p1", "Hildegard Löbig", 1876)]),
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
                        claim("p1", "Hildegard Löbig", 1876),
                        claim("p2", "Sebastian Löbig", 1862),
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
        assert any(
            "fell back to line windows" in reason
            for reason in extractor.attention
        )

    def test_a_batch_without_claims_needs_attention(self, tmp_path):
        llm = ScriptedLlm([entity_manifest_json(), ndjson([]), ndjson([])])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), "Q23")
        assert batch.claims == []
        assert extractor.attention == ["no claims extracted"]
