"""Offline tests for chunked extraction (ADR-0006/0007).

No instance, no LLM and no LLM cache: the extractor calls a scripted
client directly, and its state is read from its attributes and the side
outputs in its state directory. tests/unit/test_llmcache.py takes the
extractor through the cache (#225).
"""

import inspect
import json
import re
import threading

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch, EntityRecord
from yizkr.pipeline.chunking import (
    ENTITY_MANIFEST_PROMPT,
    ChunkedClaimExtractor,
    ChunkIncomplete,
    DocumentSegmentation,
    ExtractionFailed,
    SegmentationUnit,
    normalize_to_manifest,
    pack_entities,
    pack_units,
    parse_chunk_gaps,
    parse_chunk_response,
    parse_chunk_unread,
    prefix_local_ids,
    unit_text,
)
from yizkr.pipeline.extract import LlmClaimExtractor
from yizkr.pipeline.llm import ConversationChanged, OutputLimitReached
from yizkr.pipeline.orchestrate import FAILED
from yizkr.pipeline.rundir import NEEDS_ATTENTION, StageState, run_state
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
# the document's content key, which the prompts name and every claim
# extracted from it cites (#102, #127); the scripted answers cite it, as
# the prompts ask
DOC_REF = ArtifactRef.from_bytes(DOC.encode())


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
            "artifact": DOC_REF.model_dump(),
            "transcription_excerpt": {"text": f"b. {year}", "language": "en"},
        },
        "confidence": 0.9,
    }


def ndjson(claims: list[dict]) -> str:
    """A complete chunk answer: its claims, one per line, then the terminal
    sentinel the prompt asks for."""
    lines = [json.dumps(c) for c in claims]
    lines.append(json.dumps({"done": True}))
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
    return Transcription(artifact=DOC_REF, text=DOC, engine="test")


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


class Reporting(Rejecting):
    """A client that takes rejections and reports why its model stopped on
    each call it answers (`last_report`, as the engines' clients do):
    `stops` holds each next answer's stop reason, in order."""

    def __init__(self, client, stops: list[str]) -> None:
        super().__init__(client)
        self.stops = list(stops)
        self.report: dict = {}

    def _stopped(self, response):
        self.report = {"stop_reason": self.stops.pop(0)}
        return response

    def complete(self, prompt, image_paths=()):
        return self._stopped(super().complete(prompt, image_paths))

    def open_conversation(self, prompt):
        response, handle = super().open_conversation(prompt)
        return self._stopped(response), handle

    def fork(self, handle, prompt):
        return self._stopped(super().fork(handle, prompt))

    def last_report(self):
        return dict(self.report)


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


# the back of a certificate, bearing only the stamp of the civil registry
# office that issued it: its one entity is boilerplate (extract=false)
STAMP = "Standesamt Flörburg"
# a page no one can read, which names nothing
ILLEGIBLE = "[illegible]"
# a page of logistics only: nothing on it is a claim
LOGISTICS = (
    "Note to readers: coffee and biscuits are served in the reading room."
)
# a manifest naming no entity at all
NO_ENTITIES = json.dumps({"entities": []})
# what an unusable manifest or segmentation is refused for
NO_MANIFEST = "no JSON object with an 'entities' array"
NO_SEGMENTATION = "no JSON object with a 'units' array"
UNKNOWN_KIND = "entity e1: unknown kind 'room'"


def logistics_manifest_json() -> str:
    """The logistics page's manifest, refused for a kind the manifest
    vocabulary lacks."""
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": "the reading room",
                    "kind": "room",
                    "summary": "where coffee and biscuits are served",
                    "mention_lines": [1],
                    "extract": False,
                }
            ]
        }
    )


def wrapped(claims: list[dict]) -> str:
    """A complete chunk answer whose claims are wrapped in one object, which
    the NDJSON parser does not read as claims."""
    return ndjson([{"claims": claims}])


def wrapped_in_sentinel(claims: list[dict]) -> str:
    """A complete chunk answer whose claims ride in its terminal sentinel,
    one object the parser reads as the sentinel alone."""
    return json.dumps({"claims": claims, "done": True})


def gap_in_sentinel(claims: list[dict]) -> str:
    """A complete chunk answer whose claims ride in its terminal sentinel
    under `gap`, the key of a gap line merged into it, which neither the
    claim parser nor the gap parser reads."""
    return json.dumps({"done": True, "gap": claims})


def with_predicate(claims: list[dict]) -> str:
    """A complete chunk answer whose claims name their property under
    `predicate`, which the parser does not read as a claim."""
    renamed = [
        {
            ("predicate" if key == "property" else key): value
            for key, value in one.items()
        }
        for one in claims
    ]
    return ndjson(renamed)


def stamp_manifest_json() -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": "Standesamt Flörburg",
                    "kind": "organization",
                    "summary": "the civil registry office that issued it",
                    "mention_lines": [1],
                    "extract": False,
                }
            ]
        }
    )


def whole_page_segmentation_json(last_line: int, extract: bool) -> str:
    """A segmentation into one unit that covers the whole page."""
    return json.dumps(
        {
            "units": [
                {
                    "id": "u1",
                    "label": "the whole page",
                    "kind": "page",
                    "first_line": 1,
                    "last_line": last_line,
                    "context": "",
                    "extract": extract,
                }
            ]
        }
    )


def page(text: str) -> Transcription:
    return Transcription(artifact=DOC_REF, text=text, engine="test")


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

    def test_a_done_line_is_the_sentinel(self):
        """The prompt asks for `{"done": true}` alone, with no count of
        what came before it (#7)."""
        response = json.dumps(claim("e1", "Hildegard Löbig", 1876))
        claims = parse_chunk_response(response + '\n{"done": true}')
        assert [c["subject"]["local_id"] for c in claims] == ["e1"]

    @pytest.mark.parametrize(
        "counts",
        [
            {"claims_emitted": 5},
            {"claims_emitted": 0, "gaps_emitted": 3},
            {"claims_emitted": None},
        ],
        ids=["too-many", "too-few", "null"],
    )
    def test_a_count_on_the_done_line_is_not_read(self, counts):
        """A count a model still writes, right or wrong, gates nothing:
        every parsed claim is kept (#7)."""
        response = "\n".join(
            [
                json.dumps(claim("e1", "Hildegard Löbig", 1876)),
                json.dumps({"done": True, **counts}),
            ]
        )
        assert len(parse_chunk_response(response)) == 1

    @pytest.mark.parametrize(
        "response",
        [
            ndjson([claim("e1", "Hildegard Löbig", 1876)]),
            json.dumps(claim("e1", "Hildegard Löbig", 1876)),
        ],
        ids=["with-sentinel", "without"],
    )
    def test_an_answer_stopped_at_the_output_limit_is_cut_off(self, response):
        """An answer its engine stopped at max_tokens is cut off, even with
        its sentinel (#7)."""
        with pytest.raises(ChunkIncomplete, match="^cut off at max_tokens$"):
            parse_chunk_response(response, cut_off=True)

    def test_valid_ndjson_parses(self):
        claims = parse_chunk_response(
            ndjson([claim("e1", "Hildegard Löbig", 1876)])
        )
        assert len(claims) == 1

    def test_gap_lines_do_not_break_claims(self):
        # ADR-0013: gap lines are ignored by the claim parser
        # (non-breaking)
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
                json.dumps({"done": True}),
            ]
        )
        claims = parse_chunk_response(response)
        assert len(claims) == 1

    def test_objects_neither_claim_gap_nor_sentinel_are_unread(self):
        """What the claim parser cannot read is counted, not guessed at, and
        what an object holds, at any depth, decides, not its shape (#45).
        Unread: a wrapper object, a claim that names its property
        `predicate` or is written flat under another key, any other object
        that holds something (a number, or a note beside the sentinel), and
        a sentinel that carries a list or object that holds something under
        any key, a gap line under another key than `gap` included, and
        under `gap` unless it holds a gap line that parse_chunk_gaps reads
        and that carries nothing else that holds something in a list or
        object (claims under `gap`, or in a gap line there, would otherwise
        be lost without a trace). Read: claims, gap lines, well formed or
        not, the sentinel, a sentinel with a note in a string, an empty
        list or object, or a gap line merged into it, and objects that hold
        nothing at any depth, which say there is nothing."""
        born = claim("e1", "Hildegard Löbig", 1876)
        renamed = json.loads(with_predicate([born]).splitlines()[0])
        godparent = {"subject": "e1", "predicate": "godparent of"}
        read = [
            born,
            {"gap": godparent},
            {"gap": "not an object"},
            {},
            {"claims": []},
            {"claims": [[]]},
            {"claims": [{}]},
            {"result": {"claims": []}},
            {"output": {"claims": [], "gaps": []}},
            {"done": True, "claims_emitted": 0, "note": "nothing else"},
            {"done": True, "claims_emitted": 0, "claims": []},
            {"done": True, "claims_emitted": 0, "result": {"claims": []}},
            {"done": True, "claims_emitted": 0, "gap": []},
            {
                "done": True,
                "claims_emitted": 0,
                "gaps_emitted": 1,
                "gap": godparent,
            },
            {
                "done": True,
                "claims_emitted": 0,
                "gap": {**godparent, "note": "p. 2"},
            },
            {
                "done": True,
                "claims_emitted": 0,
                "gap": {**godparent, "claims": []},
            },
            {
                "done": True,
                "claims_emitted": 0,
                "gap": {**godparent, "evidence": ["x"]},
            },
            {"done": True, "claims_emitted": 1, "gaps_emitted": 2},
        ]
        unread = [
            {"claims": [born]},
            renamed,
            {"claim": "Hildegard Löbig, born 1876"},
            {"count": 2},
            {"note": "nothing else"},
            {"claims": [born], "done": True, "claims_emitted": 1},
            {"done": True, "claims_emitted": 1, "people": [born]},
            {"done": True, "claims_emitted": 1, "output": {"claims": [born]}},
            {"done": True, "claims_emitted": [born]},
            {"done": True, "claims_emitted": 1, "gap": [born]},
            {"done": True, "claims_emitted": 1, "gap": {"claims": [born]}},
            {"done": True, "claims_emitted": 1, "gap": born},
            {
                "done": True,
                "claims_emitted": 1,
                "gaps_emitted": 1,
                "gap": {**godparent, "object": "e2", "claims": [born]},
            },
            {"done": True, "claims_emitted": 1, "result": renamed},
            # kept on purpose: a failure that shows, which --refresh extract
            # asks again, rather than claims lost in silence
            {"done": True, "claims_emitted": 0, "notes": ["nothing else"]},
            {"done": True, "claims_emitted": 0, "lines": [1, 4]},
            {"done": True, "claims_emitted": 0, "gaps": [godparent]},
            {"done": True, "claims_emitted": 0, "gaps": godparent},
            {"done": True, "claims_emitted": 0, "gap": [godparent] * 2},
        ]
        response = "\n".join(json.dumps(obj) for obj in (*read, *unread))
        assert parse_chunk_unread(response) == unread
        assert parse_chunk_gaps(response) == [
            {**godparent, "object": "", "evidence": ""},
            {**godparent, "object": "", "evidence": ""},
            {**godparent, "object": "", "evidence": ""},
            {**godparent, "object": "", "evidence": ""},
            {**godparent, "object": "", "evidence": "['x']"},
            {**godparent, "object": "e2", "evidence": ""},
        ]


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
        ).extract(transcription(), DOC_REF)
        chunk_prompt = llm.calls[1][1]

        assert "Persecution and displacement are EVENTS" in chunk_prompt
        assert "`deportation`" in chunk_prompt
        assert "`imprisonment`" in chunk_prompt
        assert "`release`" in chunk_prompt
        assert "`participant` (the person, role `principal`)" in chunk_prompt
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


def citing(source_artifact: dict, *args) -> dict:
    """`claim(*args)` as a chunk answer that cites `source_artifact`."""
    answered = claim(*args)
    answered["source"] = {**answered["source"], "artifact": source_artifact}
    return answered


class TestClaimsCiteTheirDocument:
    """Every claim the chunked extractor gives cites the document's
    content key, whatever the chunk answers wrote there, on either tier
    (#102): the extractor names the source itself."""

    OTHER = ArtifactRef.from_bytes(b"fictional scan: another page")

    def test_entity_tier(self):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard", 1876)]),  # cites Q23
                ndjson(
                    [
                        citing(
                            {"kind": "artifact", "sha1": self.OTHER.sha1},
                            "e2",
                            "Sebastian Löbig",
                            1862,
                        )
                    ]
                ),
            ]
        )
        batch = ChunkedClaimExtractor(llm, budget=LINE_LENGTH).extract(
            transcription(), DOC_REF
        )
        assert [claim.source.artifact for claim in batch.claims] == [
            DOC_REF,
            DOC_REF,
        ]
        # the rest of each source is the model's
        assert [
            claim.source.transcription_excerpt.text for claim in batch.claims
        ] == ["b. 1876", "b. 1862"]

    def test_line_range_tier(self):
        llm = ScriptedLlm(
            [
                "no manifest here",
                "no manifest here either",
                segmentation_json(),
                ndjson(
                    [
                        claim("p1", "Hildegard Löbig", 1876),
                        citing(
                            {"kind": "local", "local_id": "p9"},
                            "p2",
                            "Sebastian Löbig",
                            1862,
                        ),
                    ]
                ),
            ]
        )
        extractor = ChunkedClaimExtractor(llm, budget=10_000)
        batch = extractor.extract(transcription(), DOC_REF)
        assert extractor.used_fallback_manifest
        assert [claim.source.artifact for claim in batch.claims] == [
            DOC_REF,
            DOC_REF,
        ]

    def test_a_claim_answered_twice_under_two_keys_is_kept_once(self):
        """Chunk answers that give one claim, citing the document by two
        keys, give it once: it is one claim of one document."""
        twice = [
            claim("e1", "Hildegard", 1876),
            citing(
                {"kind": "artifact", "sha1": self.OTHER.sha1},
                "e1",
                "Hildegard",
                1876,
            ),
        ]
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson(twice),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        batch = ChunkedClaimExtractor(llm, budget=LINE_LENGTH).extract(
            transcription(), DOC_REF
        )
        assert [claim.subject.local_id for claim in batch.claims] == [
            "e1",
            "e2",
        ]

    def test_a_claim_a_chunk_marks_curated_is_a_machine_claim(self):
        """A chunk's answer that marks a claim `curated`, which the schema
        in the prompt shows, gives a machine claim all the same: the
        writer references it to its extraction run (CLAUDE.md rule 2)."""
        curated = claim("e1", "Hildegard", 1876)
        curated["curated"] = True
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([curated]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        batch = ChunkedClaimExtractor(llm, budget=LINE_LENGTH).extract(
            transcription(), DOC_REF
        )
        assert [claim.curated for claim in batch.claims] == [False, False]


class TestEntityFirstExtractor:
    def extractor(self, llm, tmp_path, **kwargs):
        return ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", **kwargs
        )

    def test_a_chunk_prompt_asks_for_a_done_line_and_no_count(self, tmp_path):
        """A chunk answer ends with `{"done": true}`: a count of its claim
        and gap lines gated nothing (#7)."""
        llm = ScriptedLlm(
            [entity_manifest_json(), placeholder_claims("e1", "e2")]
        )
        self.extractor(llm, tmp_path, budget=10_000).extract(
            transcription(), DOC_REF
        )
        ((method, prompt),) = llm.calls[1:]
        assert method == "fork"
        assert 'final line:\n{"done": true}\n' in prompt
        assert "emitted" not in prompt

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
        batch = extractor.extract(transcription(), DOC_REF)

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
            transcription(), DOC_REF
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
        extractor.extract(transcription(), DOC_REF)

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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        rejects = side_output(tmp_path, "rejects.json")
        assert len(rejects) == 1
        assert "entity manifest" in rejects[0]["problems"][0]

    def test_truncation_retries_then_succeeds(self, tmp_path):
        # a missing sentinel is real truncation: retryable
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
        if kept == 1:
            assert len(batch.claims) == 2
            assert extractor.failed_chunks == []
        else:
            assert extractor.failed_chunks == ["c001"]
        assert [response for response, _ in llm.rejected] == [cut] * kept

    def test_a_miscounting_done_line_is_accepted_without_retry(self, tmp_path):
        # a complete response whose done line still carries a wrong count
        # is kept as it is and does NOT burn a retry: the count gates
        # nothing (#7)
        miscounted = "\n".join(
            [
                json.dumps(claim("e1", "Hildegard Löbig", 1876)),
                json.dumps({"done": True, "claims_emitted": 99}),
            ]
        )
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                miscounted,
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), DOC_REF)
        assert extractor.failed_chunks == []
        assert {c.subject.label for c in batch.claims} == {
            "Hildegard Löbig",
            "Sebastian Löbig",
        }
        # one fork per chunk — the miscount triggered no retry
        assert sum(1 for method, _ in llm.calls if method == "fork") == 2

    def test_an_answer_stopped_at_the_output_limit_is_asked_again(
        self, tmp_path
    ):
        """An answer whose engine reports it stopped at max_tokens is cut
        off, however whole it looks: it is rejected, with a reason the
        truncations anomaly check counts, and the retry asks again (#7)."""
        whole = placeholder_claims("e1")
        llm = Reporting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    whole,  # c001 attempt 1: stopped at max_tokens
                    whole,  # c001 attempt 2: ended its turn
                    placeholder_claims("e2"),  # c002
                ]
            ),
            stops=["end_turn", "max_tokens", "end_turn", "end_turn"],
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=1
        )
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []
        assert llm.rejected == [(whole, "chunk: cut off at max_tokens")]

    def test_a_chunk_the_engine_gave_up_on_is_forked_again(self, tmp_path):
        """A call the engine gave up on at its output limit gave no answer:
        the chunk spends a try on it, as on an answer cut off, and forks
        again: the fork worked, and inline context would only lengthen the
        call (#7)."""
        llm = Rejecting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    OutputLimitReached(),  # c001 try 1: no answer
                    placeholder_claims("e1", "e2"),  # c001 try 2
                ]
            )
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000, retries=1)
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        assert extractor.failed_chunks == []
        assert [method for method, _ in llm.client.calls] == [
            "open",
            "fork",
            "fork",
        ]
        assert llm.rejected == []  # nothing came to reject

    def test_a_chunk_the_engine_gave_up_on_every_time_fails_as_cut_off(
        self, tmp_path
    ):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                OutputLimitReached(),
                OutputLimitReached(),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000, retries=1)
        with pytest.raises(ExtractionFailed, match="1 of 1 chunk failed"):
            extractor.extract(transcription(), DOC_REF)
        assert extractor.failure_reasons == {
            "c001": "ChunkIncomplete: cut off at max_tokens"
        }

    def test_a_chunk_stopped_at_the_output_limit_on_every_try_fails(
        self, tmp_path
    ):
        whole = placeholder_claims("e1")
        llm = Reporting(
            ScriptedLlm([entity_manifest_json(), whole, whole]),
            stops=["end_turn", "max_tokens", "max_tokens"],
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000, retries=1)
        with pytest.raises(ExtractionFailed, match="1 of 1 chunk failed"):
            extractor.extract(transcription(), DOC_REF)
        assert extractor.failure_reasons == {
            "c001": "ChunkIncomplete: cut off at max_tokens"
        }

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
        batch = extractor.extract(transcription(), DOC_REF)
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
        ).extract(transcription(), DOC_REF)
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
        ).extract(transcription(), DOC_REF)
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
            transcription(), DOC_REF
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
            transcription(), DOC_REF
        )
        assert llm.closed == ["session-abc"]
        assert llm.rejected == []

    def test_engine_outage_fails_the_extraction(self, tmp_path):
        """A manifest the engine never answered is a failure to retry,
        not a fallback to extract by."""
        llm = ScriptedLlm([RuntimeError("down"), RuntimeError("down")])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        with pytest.raises(RuntimeError, match="no engine response"):
            extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        batch = extractor.extract(transcription(), DOC_REF)
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
        # with the extraction's health, as on the entity-first tier (#45)
        assert (coverage["chunks"], coverage["claims"]) == (1, 2)
        assert coverage["rejected_claims"] == 0

    def test_a_manifest_the_engine_gave_up_on_falls_back(self, tmp_path):
        """A manifest the engine gave up on at its output limit was
        answered, past the limit: it is refused, and the line-range tier
        stands in for it, as for a manifest cut off, rather than the
        extraction failing as if the engine were down (#7)."""
        llm = ScriptedLlm(
            [
                OutputLimitReached(),  # manifest attempt 1
                OutputLimitReached(),  # attempt 2
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
        batch = extractor.extract(transcription(), DOC_REF)
        assert extractor.used_fallback_manifest
        assert extractor.fallback["manifest"] == "cut off at max_tokens"
        assert len(batch.claims) == 2

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
        extractor.extract(transcription(), DOC_REF)
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
            transcription(), DOC_REF
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
        batch = extractor.extract(transcription(), DOC_REF)
        assert extractor.used_fallback_manifest
        assert extractor.used_fallback_segmentation
        assert len(batch.claims) == 2
        assert llm.calls[-1][0] == "complete"
        assert "Full transcription" in llm.calls[-1][1]
        assert any(
            "fell back to line windows" in reason
            for reason in extractor.attention
        )

    @pytest.mark.parametrize(
        ("listed", "records", "problem"),
        [
            ([], [], "empty entity list"),
            (None, [], "empty entity list"),
            ({}, None, "'entities' is not a list (dict)"),
            ("", None, "'entities' is not a list (str)"),
            (0, None, "'entities' is not a list (int)"),
        ],
    )
    def test_only_a_list_or_null_can_name_no_entity(
        self, listed, records, problem
    ):
        """An empty list, or null, is the engine's word that the document
        names nothing, refused so that the line-range tier looks again;
        any other value where the entity list belongs is unusable (#45)."""
        manifest = json.dumps({"entities": listed})
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            manifest, 1
        )
        entities = None if parsed is None else parsed.entities
        assert (entities, problems) == (records, [problem])


class TestExtractionsWithoutClaims:
    """An extraction that ends with no claims either fails, raising
    ExtractionFailed with every cause, or is claim-free: its batch is
    returned, and its attention says why it is empty (#45). It fails when
    the model's answers could not be used: a chunk failed, the answers
    held objects the parser could not read, or validation rejected every
    claim. It is claim-free when the answers the extractor accepted say so,
    on either tier: a refused manifest falls back (ADR-0032 §5), and when
    the line-range tier finds nothing, the batch is claim-free and its
    attention names the refusal."""

    def extractor(self, llm, tmp_path, **kwargs):
        return ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", **kwargs
        )

    def failure(self, extractor, document: Transcription) -> str:
        """The reason the extraction fails with. A failed extraction
        writes no side output: they describe a batch, and there is none."""
        with pytest.raises(ExtractionFailed) as failed:
            extractor.extract(document, DOC_REF)
        assert list(extractor.state_dir.glob("*")) == []
        return str(failed.value)

    # -- claim-free --------------------------------------------------------

    def test_chunks_that_answer_with_none_leave_a_claim_free_batch(
        self, tmp_path
    ):
        llm = ScriptedLlm([entity_manifest_json(), ndjson([]), ndjson([])])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), DOC_REF)
        assert batch.claims == []
        assert extractor.attention == [
            "no claims extracted: 2 chunks answered with none"
        ]
        coverage = side_output(tmp_path, "coverage.json")
        assert (coverage["chunks"], coverage["claims"]) == (2, 0)
        assert coverage["unread_objects"] == 0

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"claims": []},
            {"claims": [], "claims_emitted": 0},
            {"result": {"claims": []}},
            {"output": {"claims": [], "gaps": []}},
            {"claims": [[]]},
            {"claims": [{}]},
        ],
        ids=[
            "empty-object",
            "empty-wrapper",
            "empty-wrapper-and-count",
            "nested-empty-wrapper",
            "nested-empty-lists",
            "empty-list-in-list",
            "empty-object-in-list",
        ],
    )
    @pytest.mark.parametrize(
        "sentinel",
        [
            {"done": True, "claims_emitted": 0},
            {"done": True, "claims_emitted": 0, "note": "nothing here"},
            {"done": True, "claims_emitted": 0, "claims": []},
            {"done": True, "claims_emitted": 0, "result": {"claims": []}},
            {
                "done": True,
                "claims_emitted": 0,
                "gaps_emitted": 1,
                "gap": {"subject": "e1", "predicate": "godparent of"},
            },
        ],
        ids=[
            "sentinel",
            "sentinel-with-note",
            "sentinel-with-no-claims",
            "sentinel-with-nested-nothing",
            "sentinel-with-gap",
        ],
    )
    def test_answers_that_hold_nothing_leave_a_claim_free_batch(
        self, tmp_path, extra, sentinel
    ):
        """An object that holds nothing at any depth beside the sentinel,
        and a note in a string, a list or object that holds nothing, or a
        gap line merged into it, say there is nothing: the chunks answered
        with none, and the page is claim-free rather than failing on every
        run from the cache (#45)."""
        answer = "\n".join(json.dumps(obj) for obj in (extra, sentinel))
        llm = ScriptedLlm([entity_manifest_json(), answer, answer])
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), DOC_REF)
        assert batch.claims == []
        assert extractor.attention == [
            "no claims extracted: 2 chunks answered with none"
        ]
        assert side_output(tmp_path, "coverage.json")["unread_objects"] == 0

    def test_a_manifest_marking_every_entity_extract_false_is_claim_free(
        self, tmp_path
    ):
        """No chunk is asked, and the batch keeps its manifest."""
        llm = ScriptedLlm([stamp_manifest_json()])
        extractor = self.extractor(llm, tmp_path)
        batch = extractor.extract(page(STAMP), DOC_REF)
        assert [method for method, _ in llm.calls] == ["open"]
        assert batch.claims == []
        assert [entity.id for entity in batch.entities] == ["e1"]
        assert extractor.attention == [
            "no claims extracted: every entity in the manifest is "
            "extract=false"
        ]
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["chunks"] == 0
        assert coverage["entities_not_extracted"] == ["e1"]

    @pytest.mark.parametrize(
        ("document", "answers", "refused", "found"),
        [
            pytest.param(
                LOGISTICS,
                [
                    logistics_manifest_json(),
                    logistics_manifest_json(),
                    whole_page_segmentation_json(1, extract=False),
                ],
                UNKNOWN_KIND,
                "every unit in the segmentation is extract=false",
                id="unknown-kind-then-no-unit",
            ),
            pytest.param(
                ILLEGIBLE,
                [
                    NO_ENTITIES,
                    "Nothing on this page can be read.",
                    whole_page_segmentation_json(1, extract=False),
                ],
                NO_MANIFEST,
                "every unit in the segmentation is extract=false",
                id="named-nothing-then-prose",
            ),
            pytest.param(
                ILLEGIBLE,
                [
                    NO_ENTITIES,
                    NO_ENTITIES,
                    whole_page_segmentation_json(1, extract=True),
                    ndjson([]),
                ],
                "empty entity list",
                "1 chunk answered with none",
                id="named-nothing-then-chunk-with-none",
            ),
            pytest.param(
                DOC,
                [
                    "no manifest here",
                    "still no manifest",
                    segmentation_json(),
                    ndjson([]),
                ],
                NO_MANIFEST,
                "1 chunk answered with none",
                id="unusable-then-chunk-with-none",
            ),
        ],
    )
    def test_a_refused_manifest_whose_fallback_finds_nothing_is_claim_free(
        self, tmp_path, document, answers, refused, found
    ):
        """The manifest was refused, so the line-range tier stood in for it
        (ADR-0032 §5), and that tier's answers say there is nothing to
        extract. The batch is claim-free and kept like any other, and its
        attention names the refusal. Failing it instead would replay the
        same failure from the cache in every run."""
        llm = ScriptedLlm(answers)
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        batch = extractor.extract(page(document), DOC_REF)
        assert batch.claims == []
        assert extractor.attention == [
            f"entity manifest fell back to line-range segmentation: {refused}",
            f"no claims extracted: the entity manifest was refused "
            f"({refused}), and on the line-range tier {found}",
        ]
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["fallback"] == {"manifest": refused}
        assert (coverage["claims"], coverage["unread_objects"]) == (0, 0)

    def test_line_windows_that_find_nothing_leave_a_claim_free_batch(
        self, tmp_path
    ):
        llm = ScriptedLlm(["junk", "junk", "junk", "junk", ndjson([])])
        extractor = self.extractor(llm, tmp_path, budget=10_000)
        batch = extractor.extract(transcription(), DOC_REF)
        assert batch.claims == []
        assert extractor.attention == [
            "entity manifest fell back to line-range segmentation: "
            f"{NO_MANIFEST}",
            f"segmentation fell back to line windows: {NO_SEGMENTATION}",
            f"no claims extracted: the entity manifest was refused "
            f"({NO_MANIFEST}), and on the line-range tier 1 chunk answered "
            "with none",
        ]

    # -- failed ------------------------------------------------------------

    def test_chunks_that_all_fail_fail_the_extraction(self, tmp_path):
        llm = PlainLlm(
            [
                entity_manifest_json(),
                RuntimeError("engine down"),  # c001
                RuntimeError("engine down"),  # c002
            ]
        )
        extractor = self.extractor(
            llm, tmp_path, budget=LINE_LENGTH, retries=0
        )
        assert self.failure(extractor, transcription()) == (
            "no claims extracted: 2 of 2 chunks failed (c001: RuntimeError: "
            "engine down)"
        )

    def test_claims_that_all_fail_validation_fail_the_extraction(
        self, tmp_path
    ):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([dict(claim("e1", "e1", 1917), property="shoe_size")]),
                ndjson([dict(claim("e2", "e2", 1917), property="shoe_size")]),
            ]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        assert self.failure(extractor, transcription()) == (
            "no claims extracted: validation rejected every claim (2)"
        )

    @pytest.mark.parametrize(
        "shape",
        [wrapped, with_predicate, wrapped_in_sentinel, gap_in_sentinel],
        ids=["wrapped", "predicate", "in-sentinel", "in-sentinel-gap"],
    )
    @pytest.mark.parametrize("tier", ["entity-first", "line-range"])
    def test_answers_the_parser_cannot_read_fail_the_extraction(
        self, tmp_path, tier, shape
    ):
        """Chunks that answered in a shape the claim parser cannot read
        (claims wrapped in one object, carried in the sentinel, under
        `gap` too, or naming their property `predicate`) hold no claim it
        reads, yet they did not answer with none. The extraction fails and
        says so, so that no view keeps the misreading and a parser fix
        reaches the next run."""
        people = [claim("e1", "e1", 1917), claim("e2", "e2", 1917)]
        if tier == "entity-first":
            answers = [entity_manifest_json(), shape(people[:1])]
            answers.append(shape(people[1:]))
            budget, count, after = LINE_LENGTH, 2, ""
        else:
            answers = ["no manifest here", "still no manifest"]
            answers += [segmentation_json(), shape(people)]
            budget = 10_000
            count = 2 if shape is with_predicate else 1
            after = (
                "; the entity manifest was refused (every attempt: "
                f"{NO_MANIFEST})"
            )
        keys = {
            wrapped: "claims",
            with_predicate: "subject, predicate, value, source, conf…",
            wrapped_in_sentinel: "claims, done",
            gap_in_sentinel: "done, gap",
        }[shape]
        plural = "" if count == 1 else "s"
        extractor = self.extractor(
            ScriptedLlm(answers), tmp_path, budget=budget
        )
        assert self.failure(extractor, transcription()) == (
            f"no claims extracted: {count} unreadable object{plural} in the "
            f"chunk answers (c001: keys {keys}){after}"
        )

    def test_unreadable_objects_beside_claims_are_counted_not_failed(
        self, tmp_path
    ):
        """An answer that holds claims besides something the parser cannot
        read: the claims are kept and the extraction does not fail; the
        other objects are counted for the checks to weigh."""
        mixed = "\n".join(
            json.dumps(obj)
            for obj in (
                claim("e1", "e1", 1917),
                {"claims": [claim("e1", "e1", 1876)]},
                {"done": True, "claims_emitted": 1},
            )
        )
        llm = ScriptedLlm(
            [entity_manifest_json(), mixed, placeholder_claims("e2")]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        assert extractor.attention == []
        assert side_output(tmp_path, "coverage.json")["unread_objects"] == 1

    def test_a_deep_nest_beside_claims_is_counted_not_raised(self, tmp_path):
        """An object nested 900 levels deep, about as deep as json decodes,
        beside a claim: what it holds is judged without recursion, so the
        extraction neither raises RecursionError nor loses the claim, and
        the nest is counted as unread (#45)."""
        nest = '{"notes": ' + "[" * 899 + '"see page 2"' + "]" * 899 + "}"
        answer = "\n".join(
            [
                json.dumps(claim("e1", "e1", 1917)),
                nest,
                json.dumps({"done": True, "claims_emitted": 1}),
            ]
        )
        llm = ScriptedLlm(
            [entity_manifest_json(), answer, placeholder_claims("e2")]
        )
        extractor = self.extractor(llm, tmp_path, budget=LINE_LENGTH)
        batch = extractor.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        assert side_output(tmp_path, "coverage.json")["unread_objects"] == 1

    @pytest.mark.parametrize(
        ("manifests", "segmentations", "refused"),
        [
            pytest.param(
                [NO_ENTITIES, NO_ENTITIES],
                [whole_page_segmentation_json(4, extract=True)],
                "the entity manifest was refused (every attempt: empty "
                "entity list)",
                id="manifest-named-nothing",
            ),
            pytest.param(
                ["no manifest here", NO_ENTITIES],
                ["junk", "junk"],
                f"the entity manifest was refused (1: {NO_MANIFEST}; 2: empty "
                "entity list), and the segmentation (every attempt: "
                f"{NO_SEGMENTATION})",
                id="both-tiers-refused",
            ),
        ],
    )
    def test_a_failure_after_a_fallback_names_each_attempt(
        self, tmp_path, manifests, segmentations, refused
    ):
        """A chunk failed on the line-range tier: the message leads with
        that outcome, then names the fallback with the reason each attempt
        at a refused turn gave, a manifest that only named nothing
        included."""
        llm = PlainLlm(
            [*manifests, *segmentations, RuntimeError("engine down")]
        )
        extractor = self.extractor(llm, tmp_path, budget=10_000, retries=0)
        assert self.failure(extractor, transcription()) == (
            "no claims extracted: 1 of 1 chunk failed (c001: RuntimeError: "
            f"engine down); {refused}"
        )

    @pytest.mark.parametrize(
        ("others", "limit"),
        [
            pytest.param([ndjson([]), ndjson([]), ndjson([])], 400, id="one"),
            pytest.param(
                [
                    ndjson(
                        [{f"an_unexpected_key_number_{n}": n for n in (1, 2)}]
                    ),
                    ndjson(
                        [dict(claim("p1", "p1", 1917), property="shoe_size")]
                    ),
                    ndjson([]),
                ],
                500,
                id="every",
            ),
        ],
    )
    def test_a_long_failure_leads_with_its_outcome_and_stays_short(
        self, tmp_path, others, limit
    ):
        """Both tiers refused, each attempt with a long reason of its own,
        and one cause or every cause at once: the message leads with the
        outcome and cuts each reason short, so that it stays within what a
        stage's failed event keeps (500 characters, after the exception's
        name)."""
        llm = PlainLlm(
            [
                json.dumps({"entities": [{"id": "e1"}]}),
                json.dumps({"entities": [{"id": "e2"}]}),
                json.dumps({"units": [{"id": "u1"}]}),
                json.dumps({"units": [{"id": "u2"}]}),
                RuntimeError("engine down: " + "and down " * 40),  # c001
                *others,  # c002 to c004
            ]
        )
        # one line window, and so one chunk, per line
        extractor = self.extractor(llm, tmp_path, budget=40, retries=0)
        message = self.failure(extractor, transcription())
        assert message.startswith(
            "no claims extracted: 1 of 4 chunks failed (c001: RuntimeError: "
            "engine down: and down"
        )
        assert "…); " in message  # the chunk's reason was cut short
        assert "; the entity manifest was refused (1: " in message
        assert "…), and the segmentation (1: " in message
        assert len(f"ExtractionFailed: {message}") < limit

    # -- the extraction's health ------------------------------------------

    def test_coverage_records_the_extractions_health(self, tmp_path):
        """What the checks that read a stored batch need, on either tier:
        its chunks, its claims, the claims validation rejected, the objects
        the parser could not read, and the fallback tiers it took (the
        line-range tier's are in TestFallbackLadder)."""
        bad = dict(claim("e1", "e1", 1917), property="shoe_size")
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "e1", 1917), bad]),
                placeholder_claims("e2"),
            ]
        )
        self.extractor(llm, tmp_path, budget=LINE_LENGTH).extract(
            transcription(), DOC_REF
        )
        coverage = side_output(tmp_path, "coverage.json")
        assert {
            key: coverage[key]
            for key in (
                "chunks",
                "failed_chunks",
                "claims",
                "rejected_claims",
                "unread_objects",
                "fallback",
            )
        } == {
            "chunks": 2,
            "failed_chunks": [],
            "claims": 2,
            "rejected_claims": 1,
            "unread_objects": 0,
            "fallback": {},
        }


class TestTheExtractionStage:
    """Under `ingest run`, with tests/unit/test_orchestrate.py's scripted
    orchestrator: an extraction that fails for want of a usable answer
    fails its stage, with a `failed` event that gives the reason, and the
    document fails; a claim-free one is done and needs attention, and a
    later run that reuses its view reports it the same (#45)."""

    @staticmethod
    def orchestrator(tmp_path, text: str, answers: dict[str, str]):
        from tests.unit.test_orchestrate import (
            ScriptedModel,
            ScriptedOrchestrator,
            quiet,
        )

        class Model(ScriptedModel):
            """Answers the manifest, the segmentation and the chunks from
            `answers` where it holds them, and as the scripted model does
            otherwise."""

            def complete(self, prompt, image_paths=()):
                if "manifest of EVERY distinct entity" in prompt:
                    role = "manifest"
                elif prompt.startswith("You segment a document"):
                    role = "segmentation"
                elif prompt.startswith("You extract source-attributed"):
                    role = "chunk"
                else:
                    role = None
                if role not in answers:
                    return super().complete(prompt, image_paths)
                self.calls.append(f"{role}:{self.name}")
                return answers[role]

        class Orchestrator(ScriptedOrchestrator):
            def _llm(self, role, doc):
                return Model(self.calls, doc.source.name)

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "page.txt").write_text(text)
        return Orchestrator(folder, tmp_path / "state", log=quiet)

    @pytest.mark.parametrize(
        ("shape", "keys"),
        [
            (wrapped, "claims"),
            (wrapped_in_sentinel, "claims, done"),
        ],
        ids=["wrapped", "in-sentinel"],
    )
    def test_an_unreadable_answer_fails_the_stage_in_every_run(
        self, tmp_path, shape, keys
    ):
        from tests.unit.test_orchestrate import this_run

        orchestrator = self.orchestrator(
            tmp_path,
            "Rahel was born in 1871.",
            {"chunk": shape([claim("e1", "Rahel", 1871)])},
        )
        # a failure keeps no view, so each run makes the extraction again,
        # and the same answers, as the LLM cache replays them, fail it again
        for _run in range(2):
            assert orchestrator.run() == 1
            (doc,) = orchestrator.documents()
            assert doc.state.status == FAILED
            assert doc.state.error == (
                "ExtractionFailed: no claims extracted: 1 unreadable object "
                f"in the chunk answers (c001: keys {keys})"
            )
            (failed,) = this_run(orchestrator, "failed")
            assert failed["stage"] == "extraction"
            assert failed["reason"] == doc.state.error
            extraction = run_state(orchestrator.rundir)[
                (doc.sha1, "extraction")
            ]
            assert extraction == StageState("failed", doc.state.error)
            assert not doc.batch.exists()

    @pytest.mark.parametrize(
        ("text", "answers", "why"),
        [
            pytest.param(
                STAMP,
                {"manifest": stamp_manifest_json()},
                [
                    "no claims extracted: every entity in the manifest is "
                    "extract=false"
                ],
                id="every-entity-extract-false",
            ),
            pytest.param(
                LOGISTICS,
                {
                    "manifest": logistics_manifest_json(),
                    "segmentation": whole_page_segmentation_json(
                        1, extract=False
                    ),
                },
                [
                    "entity manifest fell back to line-range segmentation: "
                    f"{UNKNOWN_KIND}",
                    "no claims extracted: the entity manifest was refused "
                    f"({UNKNOWN_KIND}), and on the line-range tier every "
                    "unit in the segmentation is extract=false",
                ],
                id="refused-manifest-then-no-unit",
            ),
        ],
    )
    def test_a_claim_free_extraction_needs_attention_in_every_run(
        self, tmp_path, text, answers, why
    ):
        from tests.unit.test_orchestrate import this_run

        orchestrator = self.orchestrator(tmp_path, text, answers)
        orchestrator.run()
        (doc,) = orchestrator.documents()
        assert doc.state.status != FAILED
        batch = ClaimBatch.model_validate_json(doc.batch.read_text())
        assert batch.claims == []
        extraction = run_state(orchestrator.rundir)[(doc.sha1, "extraction")]
        assert extraction == StageState(NEEDS_ATTENTION, "; ".join(why))
        # a later run reuses the view, and reports it as it was made
        orchestrator.run()
        (reused,) = [
            event
            for event in this_run(orchestrator, "reused")
            if event["stage"] == "extraction"
        ]
        assert reused["attention"] == why
        again = run_state(orchestrator.rundir)[(doc.sha1, "extraction")]
        assert again == extraction


class TestExtractFalseRule:
    def test_the_entity_record_states_the_manifest_prompts_rule(self):
        """One definition of extract=false (#259): the comment on
        EntityRecord.extract states the entity-manifest prompt's rule in
        the prompt's own words, so that neither drifts from the other. The
        rule is the prompt's two extract=false bullets, and every
        extract=true or extract=false the prompt says stands in them: a
        rule added elsewhere in the prompt would escape the comparison."""
        start = ENTITY_MANIFEST_PROMPT.index("- extract=false for entities")
        end = ENTITY_MANIFEST_PROMPT.index("- summary is evidence")
        said = [
            match.start()
            for match in re.finditer(
                r"\bextract\s*[=:]\s*(?:true|false)\b",
                ENTITY_MANIFEST_PROMPT,
                flags=re.I,
            )
        ]
        assert said and all(start <= at < end for at in said)
        bullets = ENTITY_MANIFEST_PROMPT[start:end]
        rule = " ".join(re.sub(r"^- ", "", bullets, flags=re.M).split())

        source = inspect.getsource(EntityRecord).splitlines()
        field = next(
            number
            for number, line in enumerate(source)
            if line.strip().startswith("extract: bool")
        )
        first = field
        while source[first - 1].strip().startswith("#"):
            first -= 1
        comment = " ".join(
            " ".join(line.strip()[1:] for line in source[first:field]).split()
        )
        stated, pointer = comment.split(" The entity-manifest prompt ", 1)
        assert stated == rule
        assert "ENTITY_MANIFEST_PROMPT" in pointer


class TestTheClaimSchemaInPrompts:
    """Each claim line must match the "Claim" definition of the claim JSON
    Schema, so every prompt that asks for claims carries the whole schema.
    Building it costs milliseconds and a document can have a hundred
    chunks, so every prompt carries one text, built once (#254)."""

    SCHEMA = json.dumps(ClaimBatch.model_json_schema(), indent=2)

    def chunk_prompts(self, tier: str, tmp_path) -> list[str]:
        """The chunk prompts of an extraction on the entity-first tier (two
        chunks) or on the line-range tier it falls back to (one chunk)."""
        if tier == "entity":
            answers = [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
            budget = LINE_LENGTH
        else:
            answers = [
                "no manifest here",
                json.dumps({"entities": [{"id": "x"}]}),
                segmentation_json(),
                ndjson([claim("p1", "Hildegard Löbig", 1876)]),
            ]
            budget = 10_000
        llm = ScriptedLlm(answers)
        ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=budget
        ).extract(transcription(), DOC_REF)
        return [prompt for method, prompt in llm.calls if method == "fork"]

    @pytest.mark.parametrize(
        "tier, chunks", [("entity", 2), ("line", 1)], ids=["entity", "line"]
    )
    def test_every_chunk_prompt_carries_the_schema(
        self, tmp_path, tier, chunks
    ):
        prompts = self.chunk_prompts(tier, tmp_path)
        assert len(prompts) == chunks
        assert all(self.SCHEMA in prompt for prompt in prompts)

    def test_the_single_call_prompt_carries_it_too(self):
        llm = ScriptedLlm([ndjson([claim("p1", "Hildegard Löbig", 1876)])])
        LlmClaimExtractor(llm).extract(transcription(), DOC_REF)
        ((method, prompt),) = llm.calls
        assert method == "complete" and self.SCHEMA in prompt

    def test_it_is_built_at_most_once(self, tmp_path, monkeypatch):
        """At most: an earlier test may have built it already. Each kind
        of prompt is asked twice, so a prompt that built its own schema
        would build it at least twice."""
        builds = []
        real_build = ClaimBatch.model_json_schema

        def build(cls, *args, **kwargs):
            builds.append(cls)
            return real_build(*args, **kwargs)

        monkeypatch.setattr(
            ClaimBatch, "model_json_schema", classmethod(build)
        )
        prompts = self.chunk_prompts("entity", tmp_path)
        for _ in range(2):
            llm = ScriptedLlm([ndjson([claim("p1", "Hildegard Löbig", 1876)])])
            LlmClaimExtractor(llm).extract(transcription(), DOC_REF)
            prompts.append(llm.calls[0][1])
        assert len(prompts) == 4
        assert all(self.SCHEMA in prompt for prompt in prompts)
        assert len(builds) <= 1


class TestThePromptsNameTheArtifactByItsKey:
    """Every prompt that shows the document names it by its content key,
    the SHA-1 of its media, and every prompt that asks for claims asks
    them to cite that key: nothing in a prompt depends on the instance the
    artifact is registered on, so a reset changes no prompt text and no
    cache key (#127)."""

    HEADER = f"Transcription of artifact sha1:{DOC_REF.sha1} (language: en):"
    INLINE = f"Full transcription of artifact sha1:{DOC_REF.sha1} for context"
    # the source the chunk and single-call prompts ask each claim to cite
    CITE = json.dumps(DOC_REF.model_dump())

    def prompts(self, tier: str, conversational: bool) -> dict:
        """The prompts of an extraction on the entity-first tier (two
        chunks) or on the line-range tier (one chunk), by the step that
        sends each: with a conversation, or with the document inline in
        every chunk."""
        if tier == "entity":
            answers = [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
            budget = LINE_LENGTH
        else:
            answers = [
                "no manifest here",
                "no manifest here either",
                segmentation_json(),
                ndjson([claim("p1", "Hildegard Löbig", 1876)]),
            ]
            budget = 10_000
        llm = (ScriptedLlm if conversational else PlainLlm)(answers)
        ChunkedClaimExtractor(llm, budget=budget).extract(
            transcription(), DOC_REF
        )
        steps: dict[str, list[str]] = {}
        for _method, prompt in llm.calls:
            if "manifest of EVERY distinct entity" in prompt:
                step = "manifest"
            elif prompt.startswith("You segment a document"):
                step = "segmentation"
            else:
                step = "chunk"
            steps.setdefault(step, []).append(prompt)
        return steps

    @pytest.mark.parametrize(
        "tier, conversational, steps",
        [
            ("entity", True, {"manifest": 1, "chunk": 2}),
            ("entity", False, {"manifest": 1, "chunk": 2}),
            ("line", True, {"manifest": 2, "segmentation": 1, "chunk": 1}),
        ],
        ids=["entity-conversation", "entity-inline", "line-range"],
    )
    def test_each_prompt_names_the_documents_key(
        self, tier, conversational, steps
    ):
        prompts = self.prompts(tier, conversational)
        assert {step: len(sent) for step, sent in prompts.items()} == steps
        for step in ("manifest", "segmentation"):
            assert all(self.HEADER in p for p in prompts.get(step, []))
        for prompt in prompts["chunk"]:
            assert self.CITE in prompt
            # a chunk that sees the document inline sees it named too
            assert (self.INLINE in prompt) is not conversational
        for prompt in (p for sent in prompts.values() for p in sent):
            assert "artifact QID" not in prompt
            assert "sha1:" in prompt or self.CITE in prompt

    def test_the_single_call_prompt_names_it_too(self):
        llm = ScriptedLlm([ndjson([claim("p1", "Hildegard Löbig", 1876)])])
        LlmClaimExtractor(llm).extract(transcription(), DOC_REF)
        ((_method, prompt),) = llm.calls
        assert self.HEADER in prompt
        assert self.CITE in prompt
        assert "artifact QID" not in prompt

    @pytest.mark.parametrize(
        "extractor", [ChunkedClaimExtractor, LlmClaimExtractor]
    )
    def test_an_extractor_is_given_the_key_alone(self, extractor):
        """What an extractor is given to name the document: its
        transcription and its key, and no QID."""
        parameters = inspect.signature(extractor.extract).parameters
        assert list(parameters) == ["self", "transcription", "artifact"]
