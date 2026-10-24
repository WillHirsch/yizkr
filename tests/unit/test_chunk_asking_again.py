"""When the chunked extractor asks a call again, and when it does not.

#501: a chunk answer whose every claim lacks the same required field is
asked again once, as a second attempt keyed apart (llm.for_attempt), its
prompt naming the field, and coverage and the run report count it.

#621: a call the safety classifier refused (llm.ClassifierRefused), a
chunk's, its fork's, the manifest's or the segmentation's, is never asked
again and never drops to inline context; the failure names the refusal by
its class, and coverage counts the refusals. Other failures keep their
retries and fallback (tests/unit/test_chunking.py).

The engine is the scripted fake of tests/unit/test_chunking.py, its
documents and names from the fictional universe.
"""

import copy
import json
from pathlib import Path

import pytest

from tests.unit.test_chunking import (
    DOC_REF,
    LINE_LENGTH,
    Attempting,
    PlainLlm,
    Rejecting,
    ScriptedLlm,
    TestTheExtractionStage,
    claim,
    entity_manifest_json,
    ndjson,
    segmentation_json,
    side_output,
    transcription,
)
from yizkr.pipeline.chunking import (
    MISSING_FIELD_NOTE,
    ChunkedClaimExtractor,
    missing_field,
)
from yizkr.pipeline.llm import (
    REFUSAL_STOP,
    SAFETY_MONITOR_BLOCKED,
    ClassifierRefused,
    OutputLimitReached,
)
from yizkr.pipeline.orchestrate import FAILED

LANGUAGE = "source.transcription_excerpt.language"
HILDEGARD = ("e1", "Hildegard Löbig", 1876)
SEBASTIAN = ("e2", "Sebastian Löbig", 1862)


def without_language(local_id: str, label: str, year: int) -> dict:
    """A claim whose excerpt gives no language, which the schema
    requires."""
    found = claim(local_id, label, year)
    del found["source"]["transcription_excerpt"]["language"]
    return found


def without_year(local_id: str, label: str, year: int) -> dict:
    """A claim whose time value gives no year, which the schema
    requires."""
    found = claim(local_id, label, year)
    found["value"] = {"type": "time"}
    return found


def refused() -> ClassifierRefused:
    return ClassifierRefused(REFUSAL_STOP, "cyber")


def extractor(llm, tmp_path, **kwargs) -> ChunkedClaimExtractor:
    """An extractor over the two-person document, one chunk per person."""
    kwargs.setdefault("budget", LINE_LENGTH)
    return ChunkedClaimExtractor(llm, state_dir=tmp_path / "state", **kwargs)


class TestMissingField:
    def test_the_field_every_claim_lacks_is_named(self):
        assert (
            missing_field(
                [without_language(*HILDEGARD), without_language(*SEBASTIAN)]
            )
            == LANGUAGE
        )

    def test_one_claim_that_gives_it_names_none(self):
        assert (
            missing_field([without_language(*HILDEGARD), claim(*SEBASTIAN)])
            is None
        )

    def test_claims_that_lack_different_fields_name_none(self):
        assert (
            missing_field(
                [without_language(*HILDEGARD), without_year(*SEBASTIAN)]
            )
            is None
        )

    def test_a_field_lacked_in_common_is_named_beside_others(self):
        both = without_year(*HILDEGARD)
        del both["source"]["transcription_excerpt"]["language"]
        assert missing_field([both, without_language(*SEBASTIAN)]) == LANGUAGE

    def test_the_first_of_several_in_common_is_named(self):
        both = without_year(*HILDEGARD)
        del both["source"]["transcription_excerpt"]["language"]
        assert missing_field([both, copy.deepcopy(both)]) == LANGUAGE

    def test_no_claims_name_none(self):
        assert missing_field([]) is None

    def test_a_claim_that_fails_for_another_reason_lacks_none(self):
        """A value of the wrong type, or a property the vocabulary lacks,
        is no missing field."""
        wrong_type = claim(*HILDEGARD)
        wrong_type["source"]["transcription_excerpt"]["language"] = 7
        unknown = dict(claim(*SEBASTIAN), property="shoe_size")
        assert missing_field([wrong_type]) is None
        assert missing_field([unknown]) is None

    def test_a_list_index_is_left_out_of_the_path(self):
        found = claim(*HILDEGARD)
        found["qualifiers"] = [
            {"property": "point_in_time", "value": {"type": "time"}}
        ]
        assert missing_field([found]) == "qualifiers.value.time.year"

    def test_a_shape_validation_repairs_is_not_missing(self):
        """A time value nested under `value`, which validation flattens
        (salvage_valid_claims), lacks no year."""
        nested = claim(*HILDEGARD)
        nested["value"] = {"type": "time", "value": {"year": 1876}}
        assert missing_field([nested]) is None


class TestAChunkAskedAgain:
    def test_it_is_asked_again_once_naming_the_field(self, tmp_path):
        """The retry is the chunk's prompt with the note naming the field,
        asked in the same conversation as attempt 1, and its answer
        replaces the first, which is kept, not rejected."""
        first = ndjson([without_language(*HILDEGARD)])
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    first,  # c001
                    ndjson([claim(*HILDEGARD)]),  # c001 asked again
                    ndjson([claim(*SEBASTIAN)]),  # c002
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("fork", 1),
            ("fork", 0),
        ]
        prompts = [prompt for _, prompt in llm.client.calls]
        note = MISSING_FIELD_NOTE.format(count=1, field=LANGUAGE)
        assert prompts[2] == prompts[1] + note
        assert f"`{LANGUAGE}`" in note
        assert llm.rejected == []
        assert sorted(c.subject.label for c in batch.claims) == [
            "Hildegard Löbig",
            "Sebastian Löbig",
        ]
        assert run.retried_chunks == 1
        assert side_output(tmp_path, "rejects.json") is None
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["retried_chunks"] == 1
        assert coverage["refused"] == {}

    def test_inline_it_is_asked_again_inline(self, tmp_path):
        """A client without conversations is asked again with the inline
        prompt and the note."""
        llm = PlainLlm(
            [
                entity_manifest_json(),
                ndjson([without_language(*HILDEGARD)]),  # c001
                ndjson([claim(*HILDEGARD)]),  # c001 asked again
                ndjson([claim(*SEBASTIAN)]),  # c002
            ]
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert len(batch.claims) == 2
        prompts = [prompt for _, prompt in llm.calls]
        note = MISSING_FIELD_NOTE.format(count=1, field=LANGUAGE)
        assert prompts[2] == prompts[1] + note
        assert run.retried_chunks == 1

    def test_on_the_line_range_tier_too(self, tmp_path):
        llm = Attempting(
            ScriptedLlm(
                [
                    "no manifest here",
                    "still no manifest",
                    segmentation_json(),
                    ndjson([without_language("p1", "Hildegard Löbig", 1876)]),
                    ndjson([claim("p1", "Hildegard Löbig", 1876)]),
                    ndjson([claim("p1", "Sebastian Löbig", 1862)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert run.used_fallback_manifest
        assert len(batch.claims) == 2
        assert llm.asked[3:] == [("fork", 0), ("fork", 1), ("fork", 0)]
        assert run.retried_chunks == 1

    def test_a_second_answer_that_lacks_it_too_is_not_asked_again(
        self, tmp_path
    ):
        """Asked again once only: the second answer is validated as usual,
        and its claims are rejected."""
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    ndjson(
                        [
                            without_language(*HILDEGARD),
                            without_language("e1", "Hildegard Löbig", 1877),
                        ]
                    ),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("fork", 1),
            ("fork", 0),
        ]
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]
        # the second answer's two claims, not the first's one
        assert len(side_output(tmp_path, "rejects.json")) == 2
        assert side_output(tmp_path, "coverage.json")["retried_chunks"] == 1

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(
                [without_language(*HILDEGARD), claim(*HILDEGARD)],
                id="one-claim-gives-it",
            ),
            pytest.param(
                [without_language(*HILDEGARD), without_year(*HILDEGARD)],
                id="different-fields",
            ),
            pytest.param(
                [dict(claim(*HILDEGARD), property="shoe_size")],
                id="not-a-missing-field",
            ),
            pytest.param([], id="no-claims"),
        ],
    )
    def test_an_answer_that_does_not_qualify_is_asked_once(
        self, tmp_path, answer
    ):
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson(answer),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        run.extract(transcription(), DOC_REF)
        assert llm.asked == [("open", 0), ("fork", 0), ("fork", 0)]
        assert run.retried_chunks == 0
        assert side_output(tmp_path, "coverage.json")["retried_chunks"] == 0

    def test_fields_the_merge_fills_are_not_missing(self, tmp_path):
        """A claim is checked as the merge validates it, the artifact
        cited (cite_artifact), so a source without its artifact is no
        slip."""
        bare = claim(*HILDEGARD)
        del bare["source"]["artifact"]
        del bare["subject"]["label"]
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([bare]),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert run.retried_chunks == 0
        assert len(batch.claims) == 2

    def test_a_chunk_that_drops_to_inline_is_asked_again_inline(
        self, tmp_path
    ):
        """When the fork machinery failed and the chunk was answered
        inline, it is asked again inline, never forked."""
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    RuntimeError("session gone"),  # c001 fork
                    ndjson([without_language(*HILDEGARD)]),  # c001 inline
                    ndjson([claim(*HILDEGARD)]),  # c001 asked again
                    ndjson([claim(*SEBASTIAN)]),  # c002
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("complete", 0),
            ("complete", 1),
            ("fork", 0),
        ]
        assert len(batch.claims) == 2

    @pytest.mark.parametrize(
        ("second", "refusals"),
        [
            pytest.param(refused(), {"chunk": 1}, id="refused"),
            pytest.param(OutputLimitReached(), {}, id="gave-up"),
        ],
    )
    def test_when_the_second_call_gives_nothing_the_first_stands(
        self, tmp_path, second, refusals
    ):
        """The second call is made once: a refusal is counted and never
        asked again, and neither it nor any other failure is retried."""
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    second,
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("fork", 1),
            ("fork", 0),
        ]
        assert run.failed_chunks == []
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]
        assert len(side_output(tmp_path, "rejects.json")) == 1
        coverage = side_output(tmp_path, "coverage.json")
        assert coverage["retried_chunks"] == 1
        assert coverage["refused"] == refusals

    def test_a_second_call_that_fails_forked_and_inline_is_not_retried(
        self, tmp_path
    ):
        """A failed fork drops the second call to inline context, as it
        does a chunk's first, and a failure there ends it."""
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    RuntimeError("session gone"),
                    RuntimeError("engine down"),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("fork", 1),
            ("complete", 1),
            ("fork", 0),
        ]
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]
        assert run.failed_chunks == []

    def test_a_cut_off_second_answer_is_rejected_and_the_first_stands(
        self, tmp_path
    ):
        cut = json.dumps(claim(*HILDEGARD))  # no sentinel
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    cut,
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        ((response, reason),) = llm.rejected
        assert response == cut
        assert reason.startswith("chunk: no terminal sentinel")
        assert len(llm.asked) == 4
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]

    def test_a_kept_cut_off_second_answer_is_asked_for_once_more(
        self, tmp_path
    ):
        """A cut-off second answer the client kept from an earlier call is
        asked for once more, as every rejector does (llm.reject_response),
        and only once."""

        class Keeping(Attempting):
            def attempt(self, number):
                return Keeping(self.client, number, parent=self)

            def reject(self, response, reason):
                super().reject(response, reason)
                return True

        cut = json.dumps(claim(*HILDEGARD))  # no sentinel
        llm = Keeping(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    cut,  # the kept answer
                    ndjson([claim(*HILDEGARD)]),  # the engine's
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked[1:4] == [("fork", 0), ("fork", 1), ("fork", 1)]
        assert len(batch.claims) == 2

    def test_a_second_answer_kept_cut_off_twice_is_not_asked_a_third_time(
        self, tmp_path
    ):
        class Keeping(Attempting):
            def attempt(self, number):
                return Keeping(self.client, number, parent=self)

            def reject(self, response, reason):
                super().reject(response, reason)
                return True

        cut = json.dumps(claim(*HILDEGARD))  # no sentinel
        llm = Keeping(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    cut,
                    cut,
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked == [
            ("open", 0),
            ("fork", 0),
            ("fork", 1),
            ("fork", 1),
            ("fork", 0),
        ]
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]


class TestEachExtractionCountsItsOwn:
    def test_the_counts_start_again(self, tmp_path):
        """An extractor counts the retries and refusals of its latest
        extraction alone, as it keeps its other state."""
        llm = Attempting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ndjson([without_language(*HILDEGARD)]),
                    refused(),
                    ndjson([claim(*SEBASTIAN)]),
                    entity_manifest_json(),
                    ndjson([claim(*HILDEGARD)]),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        run.extract(transcription(), DOC_REF)
        assert (run.retried_chunks, run.refused) == (1, {"chunk": 1})
        run.extract(transcription(), DOC_REF)
        assert (run.retried_chunks, run.refused) == (0, {})
        coverage = side_output(tmp_path, "coverage.json")
        assert (coverage["retried_chunks"], coverage["refused"]) == (0, {})


class TestARefusedCall:
    @pytest.mark.parametrize(
        "blocked_by", [REFUSAL_STOP, SAFETY_MONITOR_BLOCKED]
    )
    def test_a_refused_fork_fails_its_chunk_at_once(
        self, tmp_path, blocked_by
    ):
        """No retry and no inline fallback: the other chunk is asked as
        usual."""
        llm = Rejecting(
            ScriptedLlm(
                [
                    entity_manifest_json(),
                    ClassifierRefused(blocked_by),  # c001 fork
                    ndjson([claim(*SEBASTIAN)]),  # c002
                ]
            )
        )
        run = extractor(llm, tmp_path, retries=2)
        batch = run.extract(transcription(), DOC_REF)
        assert [method for method, _ in llm.client.calls] == [
            "open",
            "fork",
            "fork",
        ]
        assert run.failed_chunks == ["c001"]
        assert run.failure_reasons["c001"].startswith("ClassifierRefused: ")
        assert run.refused == {"chunk": 1}
        assert [c.subject.label for c in batch.claims] == ["Sebastian Löbig"]

    def test_a_refused_inline_chunk_is_not_asked_again(self, tmp_path):
        llm = PlainLlm(
            [
                entity_manifest_json(),
                refused(),  # c001
                ndjson([claim(*SEBASTIAN)]),  # c002
            ]
        )
        run = extractor(llm, tmp_path, retries=2)
        run.extract(transcription(), DOC_REF)
        assert len(llm.calls) == 3
        assert run.failed_chunks == ["c001"]
        assert run.failure_reasons["c001"].startswith("ClassifierRefused: ")

    def test_a_refusal_after_a_failure_ends_the_retries(self, tmp_path):
        """Other failures keep their retries and their inline fallback; a
        refusal on a later try ends them."""
        llm = ScriptedLlm(
            [
                entity_manifest_json(),
                RuntimeError("session gone"),  # c001 fork
                RuntimeError("engine down"),  # c001 inline
                refused(),  # c001 retry, inline
                ndjson([claim(*SEBASTIAN)]),  # c002 fork
            ]
        )
        run = extractor(llm, tmp_path, retries=2)
        run.extract(transcription(), DOC_REF)
        assert [method for method, _ in llm.calls] == [
            "open",
            "fork",
            "complete",
            "complete",
            "fork",
        ]
        assert run.failure_reasons["c001"].startswith("ClassifierRefused: ")

    def test_a_refused_manifest_is_not_asked_again(self, tmp_path):
        """Nor does the line-range tier stand in for it: the extraction
        fails, naming the refusal."""
        llm = Attempting(ScriptedLlm([refused()]))
        run = extractor(llm, tmp_path)
        with pytest.raises(RuntimeError, match="ClassifierRefused") as raised:
            run.extract(transcription(), DOC_REF)
        assert str(raised.value).startswith("entity manifest: ")
        assert llm.asked == [("open", 0)]
        assert run.refused == {"manifest": 1}

    def test_a_manifest_refused_on_its_second_attempt_takes_no_fallback(
        self, tmp_path
    ):
        llm = Attempting(ScriptedLlm(["no manifest here", refused()]))
        run = extractor(llm, tmp_path)
        with pytest.raises(RuntimeError, match="ClassifierRefused"):
            run.extract(transcription(), DOC_REF)
        assert llm.asked == [("open", 0), ("open", 1)]
        assert not run.used_fallback_manifest

    def test_a_refused_segmentation_is_not_asked_again(self, tmp_path):
        llm = Attempting(
            ScriptedLlm(["no manifest here", "still no manifest", refused()])
        )
        run = extractor(llm, tmp_path)
        with pytest.raises(RuntimeError, match="ClassifierRefused") as raised:
            run.extract(transcription(), DOC_REF)
        assert str(raised.value).startswith("segmentation: ")
        assert llm.asked == [("open", 0), ("open", 1), ("open", 0)]
        assert run.refused == {"segmentation": 1}

    def test_other_manifest_failures_keep_their_second_attempt(self, tmp_path):
        llm = Attempting(
            ScriptedLlm(
                [
                    RuntimeError("engine down"),
                    entity_manifest_json(),
                    ndjson([claim(*HILDEGARD)]),
                    ndjson([claim(*SEBASTIAN)]),
                ]
            )
        )
        run = extractor(llm, tmp_path)
        batch = run.extract(transcription(), DOC_REF)
        assert llm.asked[:2] == [("open", 0), ("open", 1)]
        assert len(batch.claims) == 2
        assert side_output(tmp_path, "coverage.json")["refused"] == {}


class TestTheStageNamesTheRefusal:
    """Under `ingest run`: a refused call fails the document, whose reason
    names the refusal by its class."""

    @pytest.mark.parametrize(
        ("role", "reason"),
        [
            (
                "manifest",
                "RuntimeError: entity manifest: no engine response "
                "(ClassifierRefused: ",
            ),
            (
                "chunk",
                "ExtractionFailed: no claims extracted: 1 of 1 chunk failed "
                "(c001: ClassifierRefused: ",
            ),
        ],
    )
    def test_the_documents_reason_names_it(self, tmp_path, role, reason):
        answers = {"manifest": entity_manifest_json(), role: refused()}
        orchestrator = TestTheExtractionStage.orchestrator(
            tmp_path, "Rahel was born in 1871.", answers
        )
        assert orchestrator.run() == 1
        (doc,) = orchestrator.documents()
        assert doc.state.status == FAILED
        assert doc.state.error.startswith(reason)


class TestThroughTheCache:
    """Through the LLM cache (tests/unit/test_llmcache.py): the first
    answer is kept, the retry is keyed apart, and a later run replays
    both without asking the engine."""

    @staticmethod
    def answer(kind, prompt, opening):
        from tests.unit.test_llmcache import extractor_answers

        text = extractor_answers()(kind, prompt, opening)
        if kind == "complete" or "manifest of EVERY" in prompt:
            return text
        excerpt = {"text": "b. 1917"}
        if "- e2:" in prompt or "could not be used" in prompt:
            excerpt["language"] = "de"
        found, done = text.splitlines()
        found = json.loads(found)
        found["source"]["transcription_excerpt"] = excerpt
        return f"{json.dumps(found)}\n{done}"

    def test_the_retry_is_kept_apart_and_replays(self, tmp_path):
        from tests.unit.test_llmcache import Engine, Refusing, extract

        engine = Engine(self.answer)
        extractor, batch = extract(tmp_path, engine)
        assert [kind for kind, _ in engine.calls] == [
            "open",
            "fork",
            "fork",
            "fork",
        ]
        assert extractor.retried_chunks == 1
        assert len(batch.claims) == 2
        # nothing was rejected: the first answer stands in the cache
        assert not list((tmp_path / "cache").glob("*/*.rejected-*.json"))
        extractor, again = extract(tmp_path, Refusing(self.answer))
        assert extractor.retried_chunks == 1
        assert again.claims == batch.claims


class TestTheRunReportCountsRetries:
    """The run report sums each extraction's `retried_chunks` and counts
    apart the extractions whose coverage predates the count, never as
    zero; each document's own count is in report.json and its
    drill-down."""

    def test_retries_are_summed_and_absences_counted_apart(self, tmp_path):
        from tests.unit.test_run_report import (
            CARD,
            CERT,
            FILES,
            NOW,
            document,
            drilled,
            record,
        )
        from yizkr.pipeline.orchestrate import slugify
        from yizkr.pipeline.report import build, render_markdown

        path = record(tmp_path / "run")

        def count(sha1, retried) -> None:
            name = slugify(Path(FILES[sha1][0]).stem)
            target = path / name / "extraction" / "coverage.json"
            value = json.loads(target.read_text())
            value["retried_chunks"] = retried
            target.write_text(json.dumps(value))

        absent = build(path, now=NOW)["aggregate"]["content"]
        extractions = absent["retried_chunks"]["absent"]
        assert absent["retried_chunks"] == {
            "chunks": 0,
            "extractions": 0,
            "absent": extractions,
        }
        assert extractions >= 2
        count(CARD, 2)
        count(CERT, 0)
        made = build(path, now=NOW)
        assert made["aggregate"]["content"]["retried_chunks"] == {
            "chunks": 2,
            "extractions": 1,
            "absent": extractions - 2,
        }
        assert document(made, CARD)["coverage"]["retried_chunks"] == 2
        assert document(made, CERT)["coverage"]["retried_chunks"] == 0
        text = render_markdown(made["aggregate"])
        assert (
            "Chunks asked again for a missing required field: 2, in 1 "
            "extractions"
        ) in text
        assert "asked again for a missing required field: 2)" in drilled(
            made, CARD
        )
        assert "asked again for a missing required field: 0)" in drilled(
            made, CERT
        )
