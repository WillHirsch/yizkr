"""The excerpt's language is asked for inside the excerpt, and a language
a claim gives on `source` instead is moved into it before validation,
and counted (#668).

Scratch run 3 lost claims whose excerpt gave no language: one claim
closed the excerpt a key early and gave the language on `source`, and
the claims after it kept the shape without the language. The chunk and
single-call prompts now show the excerpt's shape and name the
transcription's language; the moved language is a shape repair, like
the flattening of nested time values, never a default. Every example
comes from the fictional universe (docs/EXAMPLES.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_chunking import (
    DOC,
    DOC_REF,
    LINE_LENGTH,
    PlainLlm,
    ScriptedLlm,
    claim,
    entity_manifest_json,
    ndjson,
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
from yizkr.pipeline.chunking import CHUNK_PROMPT, ChunkedClaimExtractor
from yizkr.pipeline.extract import (
    EXTRACTION_PROMPT,
    LlmClaimExtractor,
    _normalize_claim_shape,
    language_on_source,
    salvage_valid_claims,
)
from yizkr.pipeline.orchestrate import slugify
from yizkr.pipeline.transcribe import Transcription

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-16T09:00:00Z",
}

SHAPE = (
    '"transcription_excerpt": {"text": "<verbatim>", "language": "<code>"} '
    "on every claim, the language inside the excerpt and never on source: "
    "the language of the excerpt's own words, de (the transcription's) "
    "unless it is written in another."
)


def flat(text: str) -> str:
    return " ".join(text.split())


def german() -> Transcription:
    return Transcription(
        artifact=DOC_REF, text=DOC, engine="test", language="de"
    )


def on_source(local_id: str, label: str, year: int) -> dict:
    """A claim that closed its excerpt one key early: the excerpt's
    language stands on `source`."""
    slipped = claim(local_id, label, year)
    slipped["source"] = {
        "artifact": DOC_REF.model_dump(),
        "transcription_excerpt": {"text": f"geb. {year}"},
        "language": "de",
    }
    return slipped


class TestThePrompts:
    @pytest.mark.parametrize("prompt", [CHUNK_PROMPT, EXTRACTION_PROMPT])
    def test_the_source_rule_shows_the_excerpt_and_its_language(self, prompt):
        """The JSON stays literal under str.format, and the language is
        the transcription's."""
        rendered = prompt.format_map(_Blank(language="de"))
        assert SHAPE in flat(rendered)

    def test_the_chunk_prompt_names_the_transcription_language(self, tmp_path):
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                ndjson([claim("e1", "Hildegard Löbig", 1876)]),
                ndjson([claim("e2", "Sebastian Löbig", 1862)]),
            ]
        )
        ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        ).extract(german(), DOC_REF)
        forks = [prompt for method, prompt in llm.calls if method == "fork"]
        assert len(forks) == 2
        assert all(SHAPE in flat(prompt) for prompt in forks)

    def test_the_single_call_prompt_names_it(self):
        llm = PlainLlm([ndjson([claim("e1", "Hildegard Löbig", 1876)])])
        LlmClaimExtractor(llm).extract(german(), DOC_REF)
        assert SHAPE in flat(llm.calls[0][1])


class _Blank(dict):
    """str.format_map's mapping for a prompt's other fields: each renders
    as its own name, so only the language is filled."""

    def __missing__(self, key: str) -> str:
        return f"<{key}>"


class TestTheRepair:
    def test_the_language_is_moved_into_the_excerpt(self):
        fixed = _normalize_claim_shape(on_source("e1", "Hildegard", 1876))
        assert fixed["source"] == {
            "artifact": DOC_REF.model_dump(),
            "transcription_excerpt": {"text": "geb. 1876", "language": "de"},
        }

    @pytest.mark.parametrize("given", [None, ""])
    def test_an_excerpt_with_an_empty_language_takes_it(self, given):
        slipped = on_source("e1", "Hildegard", 1876)
        slipped["source"]["transcription_excerpt"]["language"] = given
        assert language_on_source(slipped)
        fixed = _normalize_claim_shape(slipped)
        assert fixed["source"]["transcription_excerpt"]["language"] == "de"
        assert "language" not in fixed["source"]

    def test_the_claim_given_is_not_changed(self):
        slipped = on_source("e1", "Hildegard", 1876)
        before = json.loads(json.dumps(slipped))
        _normalize_claim_shape(slipped)
        assert slipped == before

    @pytest.mark.parametrize(
        "source",
        [
            # the excerpt gives its own: the one on source is not moved
            {
                "transcription_excerpt": {
                    "text": "geb. 1876",
                    "language": "en",
                },
                "language": "de",
            },
            # no excerpt object to move it into
            {"language": "de"},
            {"transcription_excerpt": "geb. 1876", "language": "de"},
            # a language that is no text
            {"transcription_excerpt": {"text": "geb. 1876"}, "language": 7},
            # nothing on source
            {"transcription_excerpt": {"text": "geb. 1876"}},
        ],
    )
    def test_anything_else_is_left_as_written(self, source):
        slipped = claim("e1", "Hildegard", 1876)
        slipped["source"] = {"artifact": DOC_REF.model_dump(), **source}
        assert not language_on_source(slipped)
        assert _normalize_claim_shape(slipped)["source"] == slipped["source"]

    @pytest.mark.parametrize("given", [None, [], "claim", {"source": "x"}])
    def test_no_claim_shape_is_none(self, given):
        assert not language_on_source(given)

    def test_validation_keeps_the_moved_claim(self):
        """The moved claim is valid; a claim with no language anywhere is
        still a schema reject, never given a default."""
        missing = claim("e2", "Sebastian", 1862)
        del missing["source"]["transcription_excerpt"]["language"]
        batch, rejects = salvage_valid_claims(
            [on_source("e1", "Hildegard", 1876), missing], RUN
        )
        assert [c.subject.local_id for c in batch.claims] == ["e1"]
        excerpt = batch.claims[0].source.transcription_excerpt
        assert (excerpt.text, excerpt.language) == ("geb. 1876", "de")
        assert [r["reasons"] for r in rejects] == [["schema"]]


class TestTheCoverage:
    def extract(self, tmp_path, chunks: list[list[dict]]) -> dict:
        llm = ScriptedLlm([entity_manifest_json(), *map(ndjson, chunks)])
        ChunkedClaimExtractor(
            llm, state_dir=tmp_path / "state", budget=LINE_LENGTH
        ).extract(transcription(), DOC_REF)
        return side_output(tmp_path, "coverage.json")

    def test_the_moved_languages_are_counted(self, tmp_path):
        coverage = self.extract(
            tmp_path,
            [
                [on_source("e1", "Hildegard Löbig", 1876)],
                [on_source("e2", "Sebastian Löbig", 1862)],
            ],
        )
        assert coverage["excerpt_language_moved"] == 2
        assert coverage["claims"] == 2
        assert coverage["rejected_claims"] == 0

    def test_a_clean_extraction_counts_none(self, tmp_path):
        coverage = self.extract(
            tmp_path,
            [
                [claim("e1", "Hildegard Löbig", 1876)],
                [claim("e2", "Sebastian Löbig", 1862)],
            ],
        )
        assert coverage["excerpt_language_moved"] == 0


class TestTheRunReport:
    def test_the_moved_languages_are_summed(self, tmp_path):
        """The run report sums each extraction's count, and counts apart,
        never as zero, the extractions whose coverage gives none."""
        path = record(tmp_path / "run")
        before = build(path, now=NOW)["aggregate"]["content"]
        extractions = before["rejects"]["extractions"]
        assert before["excerpt_language_moved"] == {
            "moved": 0,
            "extractions": 0,
            "absent": extractions,
        }
        for sha1, moved in ((CERT, 2), (DIRECTORY, 0)):
            covered = (
                path
                / slugify(Path(FILES[sha1][0]).stem)
                / "extraction"
                / "coverage.json"
            )
            written = json.loads(covered.read_text())
            covered.write_text(
                json.dumps(written | {"excerpt_language_moved": moved})
            )
        made = build(path, now=NOW)
        assert made["aggregate"]["content"]["excerpt_language_moved"] == {
            "moved": 2,
            "extractions": 1,
            "absent": extractions - 2,
        }
        assert (
            "Excerpt languages moved in from source: 2, in 1 extraction "
            "(extractions whose coverage predates the count: "
            f"{extractions - 2})."
        ) in render_markdown(made["aggregate"])
        assert "Excerpt languages moved in from source: 2" in drilled(
            made, CERT
        )
        assert "Excerpt languages moved in from source: 0" in drilled(
            made, DIRECTORY
        )

    def test_a_coverage_that_predates_the_count(self, tmp_path):
        made = build(record(tmp_path / "run"), now=NOW)
        assert "Excerpt languages moved in from source: not counted" in (
            drilled(made, CERT)
        )
