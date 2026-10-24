"""A single trailing comma before a closing bracket or brace in a manifest
or chunk answer is read as JSON syntax, and counted (#654). No value is
added, changed or guessed: any other JSON defect still leaves the object
unread, and a comma inside a string is never touched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_chunking import (
    DOC_REF,
    LINE_LENGTH,
    NO_MANIFEST,
    ScriptedLlm,
    claim,
    entity_manifest_json,
    ndjson,
    segmentation_json,
    side_output,
    transcription,
)
from tests.unit.test_run_report import (
    CERT,
    DIRECTORY,
    FILES,
    NOW,
    build,
    drilled,
    record,
    render_markdown,
)
from yizkr.pipeline.chunking import (
    ChunkedClaimExtractor,
    parse_chunk_gaps,
    parse_chunk_response,
    parse_chunk_unread,
)
from yizkr.pipeline.extract import (
    JsonScan,
    _trailing_comma,
    scan_json_answer,
    scan_json_objects,
)
from yizkr.pipeline.orchestrate import slugify

# Hildegard's manifest entry, as a model writes it: one trailing comma
# closing her aliases, another closing the entity list
MANIFEST_WITH_COMMAS = """{"entities": [
  {"id": "e1", "label": "Hildegard Löbig", "aliases": ["Hildegard",],
   "kind": "human", "summary": "b. 1876 Fläckfeld", "mention_lines": [2]},
  {"id": "e2", "label": "Sebastian Löbig", "aliases": [], "kind": "human",
   "summary": "b. 1862 Flörburg", "mention_lines": [3]},
]}"""


def trailing(line: str) -> str:
    """A JSON object line written with one trailing comma before its
    closing brace."""
    assert line.endswith("}")
    return line[:-1] + ",}"


class TestTheScan:
    @pytest.mark.parametrize(
        ("answer", "objects", "commas"),
        [
            ('{"aliases": ["Hilde",]}', [{"aliases": ["Hilde"]}], 1),
            ('{"label": "Flörburg",}', [{"label": "Flörburg"}], 1),
            # whitespace and line breaks between the comma and its closer
            ('{"aliases": ["Hilde" ,\n  ]\n,\n}', [{"aliases": ["Hilde"]}], 2),
            # one in each of two containers, nested
            ('{"e": [{"id": "e1",},]}', [{"e": [{"id": "e1"}]}], 2),
            # NDJSON lines, each read alone
            (
                '{"id": "e1",}\n{"done": true,}',
                [{"id": "e1"}, {"done": True}],
                2,
            ),
            ('{"aliases": []}', [{"aliases": []}], 0),
        ],
    )
    def test_a_trailing_comma_is_read_and_counted(
        self, answer, objects, commas
    ):
        assert scan_json_answer(answer) == JsonScan(objects, commas)

    @pytest.mark.parametrize(
        "answer",
        [
            '{"aliases": ["Hilde",,]}',  # two commas
            '{"aliases": ["Hilde",,],}',
            '{"aliases": [,"Hilde"]}',  # a comma before a value
            '{"aliases": [,]}',  # a comma with no value before it
            "{,}",
            '{"label":,}',  # a comma after a key's colon
            '{"label",}',  # a comma after a key with no value
            # a comma before a value, after a key's colon or the key
            '{"label":, "Flörburg"}',
            '{"label",: "Flörburg"}',
            '{"aliases": ["Hilde", ,]}',  # two commas, spaced apart
            '{"aliases": ["Hilde" "Hildegard"]}',  # a missing comma
            '{"aliases": ["Hilde",}}',  # the wrong closer
            # the wrong closer, with no comma before it: blanking the
            # string's quote would decode, as another value
            '{"aliases": ["Hilde"}"]}',
            '{"aliases": ["Hilde",',  # cut off
        ],
    )
    def test_any_other_defect_still_refuses(self, answer):
        assert scan_json_answer(answer) == JsonScan([], 0)

    @pytest.mark.parametrize(
        "answer",
        [
            '{"summary": "b. 1876 Fläckfeld,]"}',
            '{"summary": "b. 1876 Fläckfeld,}"}',
            '{"summary": "Hutmacherei zum Blauen Hut, ]"}',
            '{"summary": "Laudler ,\\n}"}',
        ],
    )
    def test_a_comma_in_a_string_is_never_touched(self, answer):
        """A string that holds `,]` or `,}` decodes as it stands, every
        character kept, and nothing is counted."""
        assert scan_json_answer(answer) == JsonScan([json.loads(answer)], 0)

    def test_a_string_holding_one_before_a_trailing_comma(self):
        """The string keeps its `,]` and `,}`; only the comma after it,
        outside the string, is read past."""
        answer = '{"summary": "Flörburg,] and ,}",}'
        assert scan_json_answer(answer) == JsonScan(
            [{"summary": "Flörburg,] and ,}"}], 1
        )

    def test_a_refused_object_counts_nothing_it_read(self):
        """An object that is read past one trailing comma but then fails
        for another defect counts nothing; scanning resumes inside it as
        for any undecodable span, and counts what it reads there."""
        answer = '{"e": [{"id": "e1",}], "x" 1}'
        assert scan_json_answer(answer) == JsonScan([{"id": "e1"}], 1)

    @pytest.mark.parametrize(
        ("text", "stop", "comma"),
        [
            # the decoder stops at the comma (3.13+) or at its closer
            ('["a" ,\n ]', 5, 5),
            ('["a" ,\n ]', 8, 5),
            ('{"a": 1 ,}', 8, 8),
            ('{"a": 1 ,}', 9, 8),
            # a comma before a value, after a colon, or after another
            ('["a" , "b"]', 5, None),
            ('{"a":, "b"}', 5, None),
            ('{"a":,}', 5, None),
            ('{"a":,}', 6, None),
            ('["a",,]', 5, None),
            ("[,]", 1, None),
            ("[,]", 2, None),
            # a stop at anything else
            ('["a" "b"]', 5, None),
            ('["a"}', 4, None),
            ('["a",', 5, None),
        ],
    )
    def test_the_comma_the_decoder_stopped_at(self, text, stop, comma):
        assert _trailing_comma(text, stop) == comma

    def test_other_readers_still_refuse_it(self):
        """scan_json_objects, the readers of the other stages' answers,
        reads no trailing comma."""
        assert scan_json_objects('{"aliases": ["Hilde",]}') == []
        assert scan_json_objects('{"aliases": ["Hilde"]}') == [
            {"aliases": ["Hilde"]}
        ]


class TestTheChunkReaders:
    def test_claims_gaps_and_sentinel_are_read_alike(self):
        """A claim line, a gap line and the sentinel, each written with a
        trailing comma: the claim and gap are read, the answer is
        finished, and nothing is unread."""
        hilde = claim("e1", "Hildegard Löbig", 1876)
        gap = {
            "gap": {
                "subject": "e1",
                "predicate": "apprenticed to",
                "object": "Hutmacherei zum Blauen Hut",
            }
        }
        answer = "\n".join(
            [
                trailing(json.dumps(hilde)),
                trailing(json.dumps(gap)),
                trailing(json.dumps({"done": True})),
            ]
        )
        assert parse_chunk_response(answer) == [hilde]
        assert [g["predicate"] for g in parse_chunk_gaps(answer)] == [
            "apprenticed to"
        ]
        assert parse_chunk_unread(answer) == []
        assert scan_json_answer(answer).trailing_commas == 3

    def test_a_claim_with_another_defect_is_still_lost(self):
        """Two commas leave the claim unread, as before: never guessed."""
        line = json.dumps(claim("e1", "Hildegard Löbig", 1876))
        answer = line[:-1] + ",,}\n" + json.dumps({"done": True})
        assert parse_chunk_response(answer) == []


class TestTheManifest:
    def test_the_manifest_reads_as_without_its_commas(self):
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            MANIFEST_WITH_COMMAS, 4
        )
        clean, _ = ChunkedClaimExtractor._parse_entity_manifest(
            entity_manifest_json(), 4
        )
        assert problems == []
        assert parsed.entities == clean.entities
        assert (parsed.trailing_commas, clean.trailing_commas) == (2, 0)

    def test_a_manifest_with_two_commas_is_still_refused(self):
        answer = MANIFEST_WITH_COMMAS.replace(
            '"Hildegard",]', '"Hildegard",,]'
        )
        parsed, problems = ChunkedClaimExtractor._parse_entity_manifest(
            answer, 4
        )
        assert (parsed, problems) == (None, [NO_MANIFEST])


class TestTheExtraction:
    def extract(self, tmp_path, responses):
        llm = ScriptedLlm(responses)
        extractor = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        )
        batch = extractor.extract(transcription(), DOC_REF)
        return llm, extractor, batch

    def test_no_second_attempt_and_the_commas_counted(self, tmp_path):
        """The manifest is used at its first attempt, and coverage counts
        the commas the manifest and each chunk's answer were read past."""
        llm, extractor, batch = self.extract(
            tmp_path,
            [
                MANIFEST_WITH_COMMAS,
                "\n".join(
                    [
                        trailing(json.dumps(claim("e1", "Hildegard", 1876))),
                        trailing(json.dumps({"done": True})),
                    ]
                ),
                trailing(ndjson([claim("e2", "Sebastian Löbig", 1862)])),
            ],
        )
        assert [call[0] for call in llm.calls] == ["open", "fork", "fork"]
        assert not extractor.used_fallback_manifest
        assert {c.subject.local_id for c in batch.claims} == {"e1", "e2"}
        coverage = side_output(tmp_path, "coverage.json")
        # the manifest's two, the first chunk's two, the second's one
        assert coverage["trailing_commas"] == 2 + 2 + 1
        assert coverage["fallback"] == {}

    def test_a_clean_extraction_counts_none(self, tmp_path):
        self.extract(
            tmp_path,
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ],
        )
        assert side_output(tmp_path, "coverage.json")["trailing_commas"] == 0

    def test_each_extraction_counts_its_own(self, tmp_path):
        """An extractor that falls back to line ranges after an extraction
        whose manifest it read past commas counts none of them again."""
        llm = ScriptedLlm(
            [
                MANIFEST_WITH_COMMAS,
                ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
                "no manifest here",
                "no manifest here either",
                segmentation_json(),
                ndjson([claim("p1", "Hildegard Löbig", 1876)]),
            ]
        )
        extractor = ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        )
        extractor.extract(transcription(), DOC_REF)
        assert side_output(tmp_path, "coverage.json")["trailing_commas"] == 2
        extractor.budget = 10_000
        extractor.extract(transcription(), DOC_REF)
        assert extractor.used_fallback_manifest
        assert side_output(tmp_path, "coverage.json")["trailing_commas"] == 0

    def test_a_refused_attempt_counts_nothing(self, tmp_path):
        """Only the manifest the extraction used is counted: a first
        attempt refused for two commas spends the second attempt, as
        before, and counts nothing."""
        refused = MANIFEST_WITH_COMMAS.replace("[2]},", "[2]},,")
        llm, extractor, _ = self.extract(
            tmp_path,
            [
                refused,
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ],
        )
        assert [call[0] for call in llm.calls] == [
            "open",
            "open",
            "fork",
            "fork",
        ]
        assert side_output(tmp_path, "coverage.json")["trailing_commas"] == 0


class TestTheRunReport:
    def test_the_commas_are_summed(self, tmp_path):
        """The run report sums each extraction's count, and counts apart,
        never as zero, the extractions whose coverage gives none."""
        path = record(tmp_path / "run")
        before = build(path, now=NOW)["aggregate"]["content"]
        extractions = before["rejects"]["extractions"]
        assert before["trailing_commas"] == {
            "read": 0,
            "extractions": 0,
            "absent": extractions,
        }
        for sha1, commas in ((CERT, 3), (DIRECTORY, 0)):
            covered = (
                path
                / slugify(Path(FILES[sha1][0]).stem)
                / "extraction"
                / "coverage.json"
            )
            written = json.loads(covered.read_text())
            covered.write_text(
                json.dumps(written | {"trailing_commas": commas})
            )
        made = build(path, now=NOW)
        assert made["aggregate"]["content"]["trailing_commas"] == {
            "read": 3,
            "extractions": 1,
            "absent": extractions - 2,
        }
        assert (
            "Trailing commas read as JSON syntax: 3, in 1 extraction "
            "(extractions whose coverage predates the count: "
            f"{extractions - 2})."
        ) in render_markdown(made["aggregate"])
        assert "Trailing commas read as JSON syntax: 3" in drilled(made, CERT)
        assert "Trailing commas read as JSON syntax: 0" in drilled(
            made, DIRECTORY
        )
