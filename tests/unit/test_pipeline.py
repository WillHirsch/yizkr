"""Offline tests for pipeline steps (pytest; no instance, no LLM)."""

import copy
import csv
import errno
import hashlib
import itertools
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.fixtures.records import record_answer
from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    EntityRecord,
    ItemValue,
    LocalRef,
    MonolingualText,
    QidRef,
    RegionPercent,
)
from yizkr.pipeline import extract
from yizkr.pipeline.extract import (
    ClaimFileExtractor,
    LlmClaimExtractor,
    _parse_response,
)
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.review import (
    APPROVE,
    CSV_COLUMNS,
    accept_all,
    apply_review,
    claim_id,
    export_review_csv,
    is_bulk_accepted,
)
from yizkr.pipeline.transcribe import (
    HtrPlatformAdapter,
    LlmVisionTranscriber,
    TextFileTranscriber,
    Transcription,
)
from yizkr.pipeline.writer import BatchWriter, UnregisteredArtifactError

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-12T09:00:00Z",
}

# the bytes of a fictional scan; claims cite it by their SHA-1 (#102)
MEDIA = b"fictional scan: an example marriage certificate"
MEDIA_SHA1 = hashlib.sha1(MEDIA).hexdigest()
ARTIFACT = {"kind": "artifact", "sha1": MEDIA_SHA1}
MEDIA_REF = ArtifactRef(sha1=MEDIA_SHA1)

# Person One's dates are Leopold Löbig's in the example universe: born in
# May 1914 (29 May per the divorce judgment), and a claim dated 1947, the
# year of his wedding.
CLAIMS = [
    {
        "subject": {
            "kind": "local",
            "local_id": "person-1",
            "label": "Person One",
            "instance_of": "human",
        },
        "property": "sibling",
        "value": {
            "type": "item",
            "value": {
                "kind": "local",
                "local_id": "person-2",
                "label": "Person Two",
                "instance_of": "human",
            },
        },
        "qualifiers": [
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1947},
            }
        ],
        "source": {
            "artifact": ARTIFACT,
            "page_number": "1",
            "transcription_excerpt": {"text": "brother", "language": "en"},
        },
        "confidence": 0.9,
    },
    {
        "subject": {
            "kind": "local",
            "local_id": "person-1",
            "label": "Person One",
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": {"type": "time", "year": 1914, "month": 5},
        "source": {"artifact": ARTIFACT},
    },
]


def batch() -> ClaimBatch:
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": CLAIMS})


class FakeLlm:
    model_identifier = "fake-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt, image_paths=()):
        self.prompts.append(prompt)
        return self.response


class Stopping(FakeLlm):
    """A client that takes rejections, as the LLM cache's does, and
    reports why its model stopped (`last_report`, as the engines' clients
    do). No answer is one it kept from an earlier call."""

    def __init__(self, response: str, stop_reason: str) -> None:
        super().__init__(response)
        self.stop_reason = stop_reason
        self.rejected: list[tuple[str, str]] = []

    def last_report(self) -> dict:
        return {"stop_reason": self.stop_reason}

    def reject(self, response, reason) -> bool:
        self.rejected.append((response, reason))
        return False


class TestTranscribe:
    def test_text_file_engine(self, tmp_path):
        source = tmp_path / "scan.txt"
        source.write_text("Geboren 1914")
        scan = tmp_path / "scan.png"
        scan.write_bytes(MEDIA)
        result = TextFileTranscriber(source, language="de").transcribe(scan)
        assert result.text == "Geboren 1914"
        assert result.language == "de"
        # the transcription names the media it is of, never the text file
        # it was read from, by the SHA-1 of its bytes (#127)
        assert result.artifact == MEDIA_REF
        assert result.engine.startswith("text-file:")

    def test_llm_vision_engine_passes_image(self, tmp_path):
        llm = FakeLlm(record_answer("The transcribed text"))
        image = tmp_path / "scan.png"
        image.write_bytes(MEDIA)
        result = LlmVisionTranscriber(llm).transcribe(image)
        assert result.text == "[Page 1]\nThe transcribed text"
        assert result.document_class == "letter"
        assert result.engine == "llm-vision:fake-model"
        assert result.artifact == MEDIA_REF
        assert "exactly as written" in llm.prompts[0]

    def test_htr_stub_says_so(self, tmp_path):
        with pytest.raises(NotImplementedError, match="stub"):
            HtrPlatformAdapter().transcribe(tmp_path / "x.png")

    def test_a_transcription_names_its_artifact_by_key_alone(self):
        """A transcription file names its artifact by the SHA-1 of its
        media, and by nothing an instance gives it (#127): a file that
        names it by QID, as transcription files did before, is refused,
        whether or not it has the key besides."""
        text = {"text": "Geboren 1914", "engine": "test"}
        keyed = Transcription.model_validate(text | {"artifact": ARTIFACT})
        assert keyed.artifact == MEDIA_REF
        assert json.loads(keyed.model_dump_json())["artifact"] == ARTIFACT
        for named in (
            {"artifact_qid": "Q23"},
            {"artifact_qid": "Q23", "artifact": ARTIFACT},
            {"artifact": {"kind": "qid", "qid": "Q23"}},
        ):
            with pytest.raises(ValidationError):
                Transcription.model_validate(text | named)


class TestExtract:
    def transcription(self) -> Transcription:
        return Transcription(
            artifact=MEDIA_REF, text="married 1947", engine="test"
        )

    def test_llm_extractor_validates_response(self):
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        result = LlmClaimExtractor(llm).extract(
            self.transcription(), MEDIA_REF
        )
        assert len(result.claims) == 2
        assert result.extraction_run.model_identifier == "fake-model"
        assert result.extraction_run.started_at.tzinfo is not None
        # prompt carries the schema and the archive's uncertainty rules
        assert "never narrow" in llm.prompts[0].lower()
        assert '"Claim"' in llm.prompts[0]

    def test_llm_extractor_rejects_unknown_property(self):
        bad = [dict(CLAIMS[1], property="shoe_size")]
        llm = FakeLlm(json.dumps({"claims": bad}))
        with pytest.raises(ValueError, match="shoe_size"):
            LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)

    def test_llm_extractor_salvages_valid_claims_around_bad_ones(
        self, tmp_path
    ):
        """One invalid claim must not discard the whole (expensive)
        extraction: the valid subset proceeds, rejects and reasons land
        in the rejects dump for diagnosis."""
        mixed = [CLAIMS[0], dict(CLAIMS[1], property="shoe_size"), CLAIMS[1]]
        rejects_path = tmp_path / "batch.json.rejected.json"
        llm = FakeLlm(json.dumps({"claims": mixed}))
        batch = LlmClaimExtractor(llm, rejects_dump=rejects_path).extract(
            self.transcription(), MEDIA_REF
        )
        assert len(batch.claims) == 2
        rejects = json.loads(rejects_path.read_text())
        assert len(rejects) == 1
        assert "shoe_size" in rejects[0]["problems"][0]
        assert rejects[0]["claim"]["property"] == "shoe_size"

    def test_llm_extractor_prompt_enumerates_vocabulary(self):
        """The model can only comply with 'use only manifest keys' if the
        prompt actually lists them."""
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert "date_of_birth: time" in llm.prompts[0]
        assert "place_of_birth: wikibase-item" in llm.prompts[0]
        assert "circa" in llm.prompts[0]

    def test_llm_extractor_prompt_carries_property_descriptions(self):
        """Direction and value type live in the manifest description;
        a key and datatype alone leave the model to guess them."""
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert (
            "employer: wikibase-item — organization the person works or "
            "worked for" in llm.prompts[0]
        )
        assert (
            "child: wikibase-item — the subject has the object as a child"
            in llm.prompts[0]
        )

    def test_the_prompt_asks_for_a_done_line_and_no_count(self):
        """The answer ends with `{"done": true}`: a count of the claims
        gated nothing (#7)."""
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert 'final line:\n{"done": true}\n' in llm.prompts[0]
        assert "emitted" not in llm.prompts[0]

    @pytest.mark.parametrize(
        ("stop_reason", "rejected"),
        [
            ("max_tokens", ["extraction: cut off at max_tokens"]),
            ("end_turn", []),
        ],
    )
    def test_an_answer_stopped_at_the_output_limit_is_rejected(
        self, stop_reason, rejected
    ):
        """An answer whose engine reports it stopped at max_tokens is cut
        off, even with its sentinel: its claims are kept, and it is
        rejected, as one without its sentinel is (#7)."""
        whole = "\n".join([*map(json.dumps, CLAIMS), '{"done": true}'])
        llm = Stopping(whole, stop_reason)
        batch = LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert len(batch.claims) == 2
        assert llm.rejected == [(whole, reason) for reason in rejected]

    def test_a_cut_answer_stopped_at_the_output_limit_says_so(self):
        """Cut off twice over, at the output limit and before its
        sentinel: its claims are kept, and the stop reason names the
        rejection (#7)."""
        cut = "\n".join(map(json.dumps, CLAIMS))
        llm = Stopping(cut, "max_tokens")
        batch = LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert len(batch.claims) == 2
        assert llm.rejected == [(cut, "extraction: cut off at max_tokens")]

    def test_llm_extractor_rejects_prose_without_json(self):
        llm = FakeLlm("I could not find any claims, sorry!")
        with pytest.raises(ValueError, match="no claim JSON"):
            LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)

    def test_parse_response_tolerates_fences_and_prose(self):
        payload = {"claims": []}
        fenced = f"Here you go:\n```json\n{json.dumps(payload)}\n```\nDone!"
        assert _parse_response(fenced) == payload
        assert _parse_response(json.dumps(payload)) == payload

    def test_parse_response_merges_continuation_objects(self):
        """Very large extractions arrive as several concatenated claims
        objects (the model continues across turns); all of them count."""
        chunks = (
            json.dumps({"claims": [CLAIMS[0]]})
            + "\nContinuing with the remaining claims:\n"
            + json.dumps({"claims": [CLAIMS[1]]})
        )
        merged = _parse_response(chunks)
        assert merged == {"claims": [CLAIMS[0], CLAIMS[1]]}

    def test_parse_response_salvages_claims_from_corrupt_wrapper(self):
        """An inexact continuation seam corrupts the enclosing claims
        object; every individual claim that still decodes is salvaged,
        with resume-point duplicates dropped."""
        corrupt = (
            '{"schema_version": 1, "claims": [\n'
            + json.dumps(CLAIMS[0])
            + ',\n{"subject": {"kind": "loc'  # seam: truncated mid-claim
            + "\n"
            + json.dumps(CLAIMS[1])
            + ",\n"
            + json.dumps(CLAIMS[1])  # model overlapped on resume
            + "\n]}"
        )
        assert _parse_response(corrupt) == {"claims": [CLAIMS[0], CLAIMS[1]]}

    def test_parse_response_ignores_json_without_claims(self):
        text = json.dumps({"note": "irrelevant"}) + json.dumps({"claims": []})
        assert _parse_response(text) == {"claims": []}
        with pytest.raises(ValueError, match="claims"):
            _parse_response(json.dumps({"note": "no claims here"}))

    def test_nested_time_values_are_flattened_not_rejected(self):
        """Models sometimes wrap time values like item values
        ({"type": "time", "value": {"year": ...}}); the content is
        unambiguous, so it is repaired rather than rejected."""
        nested = dict(
            CLAIMS[1],
            value={"type": "time", "value": {"year": 1914, "month": 5}},
            qualifiers=[
                {
                    "property": "latest_date",
                    "value": {"type": "time", "value": {"year": 1915}},
                }
            ],
        )
        llm = FakeLlm(json.dumps({"claims": [nested]}))
        batch = LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        assert batch.claims[0].value.year == 1914
        assert batch.claims[0].qualifiers[0].value.year == 1915

    def test_over_length_excerpts_are_clipped_not_fatal(self):
        """Wikibase caps text values at 400 chars; an excerpt is a
        verification aid, so it is clipped visibly instead of losing
        the claim (or failing the write)."""
        long_claim = dict(
            CLAIMS[1],
            source={
                "artifact": ARTIFACT,
                "transcription_excerpt": {
                    "text": "word " * 100,
                    "language": "en",
                },
            },
        )
        llm = FakeLlm(json.dumps({"claims": [long_claim]}))
        batch = LlmClaimExtractor(llm).extract(self.transcription(), MEDIA_REF)
        text = batch.claims[0].source.transcription_excerpt.text
        assert len(text) <= 400
        assert text.endswith("…")

    def test_extractor_dumps_raw_response_before_parsing(self, tmp_path):
        """Engine output is expensive: it must be on disk even when
        parsing/validation fails, so nothing paid-for is unrecoverable."""
        raw = tmp_path / "deep" / "batch.json.raw.txt"
        llm = FakeLlm("total garbage, no JSON at all")
        with pytest.raises(ValueError):
            LlmClaimExtractor(llm, raw_dump=raw).extract(
                self.transcription(), MEDIA_REF
            )
        assert raw.read_text() == "total garbage, no JSON at all"

    def test_file_extractor_wraps_bare_claims(self, tmp_path):
        claims_file = tmp_path / "claims.json"
        claims_file.write_text(json.dumps({"claims": CLAIMS}))
        result = ClaimFileExtractor(claims_file).extract(
            self.transcription(), MEDIA_REF
        )
        assert len(result.claims) == 2
        assert result.extraction_run.model_identifier == "manual"

    def test_file_extractor_keeps_the_artifacts_its_claims_cite(
        self, tmp_path
    ):
        """A prepared file's claims cite their artifacts themselves: the
        file extractor validates them as they are, so a claim citing its
        item by QID is refused, never read as the document's (#102)."""
        by_qid = dict(
            CLAIMS[1], source={"artifact": {"kind": "qid", "qid": "Q23"}}
        )
        claims_file = tmp_path / "claims.json"
        claims_file.write_text(json.dumps({"claims": [by_qid]}))
        with pytest.raises(ValueError, match="artifact"):
            ClaimFileExtractor(claims_file).extract(
                self.transcription(), MEDIA_REF
            )
        other = ArtifactRef.from_bytes(b"fictional scan: another page")
        cites_other = dict(CLAIMS[1], source={"artifact": other.model_dump()})
        claims_file.write_text(json.dumps({"claims": [cites_other]}))
        result = ClaimFileExtractor(claims_file).extract(
            self.transcription(), MEDIA_REF
        )
        assert result.claims[0].source.artifact == other


# Person One's birth as one page of a document gives it, in May 1914, in a
# model's answer that cites the artifact by its key, as the prompt asks
BORN = dict(
    CLAIMS[1],
    source={
        "artifact": MEDIA_REF.model_dump(),
        "page_number": "1",
        "region": {"x": 10, "y": 20, "width": 30, "height": 5},
        "transcription_excerpt": {"text": "b. May 1914", "language": "en"},
    },
)


def born_restated(**source) -> dict:
    """BORN, with the fields `source` gives replaced in its source."""
    return dict(BORN, source={**BORN["source"], **source})


class TestExtractedClaimsCiteTheirDocument:
    """The extractor names each claim's source artifact itself: the
    document's content key, whatever the model wrote there (#102). The
    prompt names the artifact by that key (#127), and these answers cite
    it otherwise: by an item's QID, as a model might copy one from the
    document's text, by another key, or not at all."""

    OTHER = ArtifactRef.from_bytes(b"fictional scan: another page")

    @staticmethod
    def answered(*claims) -> ClaimBatch:
        sentinel = {"done": True, "claims_emitted": len(claims)}
        llm = FakeLlm(
            "\n".join(json.dumps(line) for line in (*claims, sentinel))
        )
        return LlmClaimExtractor(llm).extract(
            Transcription(
                artifact=MEDIA_REF, text="married 1947", engine="test"
            ),
            MEDIA_REF,
        )

    def test_a_copied_qid_becomes_the_documents_key(self):
        by_qid = {
            "artifact": {"kind": "qid", "qid": "Q23"},
            "page_number": "1",
        }
        batch = self.answered(dict(CLAIMS[1], source=by_qid))
        (claim,) = batch.claims
        assert claim.source.artifact == MEDIA_REF
        # the locators the model gave stay as they are
        assert claim.source.page_number == "1"

    @pytest.mark.parametrize(
        "cited",
        [
            {"kind": "artifact", "sha1": OTHER.sha1},  # another document's
            {"kind": "artifact", "sha1": "Q23"},  # a QID where a key goes
            {"kind": "local", "local_id": "doc-1", "label": "a letter"},
        ],
        ids=["another-key", "qid-as-key", "local-ref"],
    )
    def test_whatever_the_model_cites_the_claim_cites_the_document(
        self, cited
    ):
        batch = self.answered(dict(CLAIMS[1], source={"artifact": cited}))
        assert [claim.source.artifact for claim in batch.claims] == [MEDIA_REF]

    def test_a_source_without_an_artifact_cites_the_document(self):
        batch = self.answered(dict(CLAIMS[1], source={"page_number": "2"}))
        assert batch.claims[0].source.artifact == MEDIA_REF

    def test_claims_alike_once_they_cite_the_document_are_one(self):
        cited = [
            {"artifact": {"kind": "qid", "qid": "Q23"}},
            {"artifact": {"kind": "qid", "qid": "Q32"}},  # a slip of the pen
            {"artifact": ARTIFACT},
        ]
        batch = self.answered(
            *(dict(CLAIMS[1], source=source) for source in cited)
        )
        assert len(batch.claims) == 1

    @pytest.mark.parametrize(
        "second",
        [
            # 29 May per another line: two values, one property (ADR-0002)
            dict(
                BORN,
                value={"type": "time", "year": 1914, "month": 5, "day": 29},
            ),
            born_restated(page_number="2"),
            born_restated(region={"x": 10, "y": 60, "width": 30, "height": 5}),
            born_restated(
                transcription_excerpt={"text": "born 5/1914", "language": "en"}
            ),
        ],
        ids=[
            "another-value",
            "another-page",
            "another-region",
            "another-excerpt",
        ],
    )
    def test_claims_that_differ_beside_their_artifact_coexist(self, second):
        """Only claims that are the same once they cite the document are
        kept once: two values the document gives for one property, or one
        value it gives in two places, stay two claims (ADR-0002)."""
        batch = self.answered(BORN, second)
        assert len(batch.claims) == 2
        assert [claim.source.artifact for claim in batch.claims] == [
            MEDIA_REF,
            MEDIA_REF,
        ]

    @pytest.mark.parametrize(
        "source", ["Q23", None, ...], ids=["text", "null", "missing"]
    )
    def test_a_claim_without_a_source_object_is_still_refused(self, source):
        """Only a source the model gave is completed: a claim without one
        is refused, as any claim without its reference is (ADR-0003), and
        counted among the rejects, never dropped unseen."""
        claim = {key: value for key, value in CLAIMS[1].items()}
        if source is ...:
            del claim["source"]
        else:
            claim["source"] = source
        with pytest.raises(
            ValueError,
            match=r"every extracted claim failed validation \(1 rejects\)",
        ):
            self.answered(claim)

    def test_a_claim_the_model_marks_curated_is_a_machine_claim(self):
        """`curated` marks a claim a person stated, which the writer
        references to no extraction run, and the schema the prompt
        carries shows it, so a model can set it. The extractor drops it:
        every claim it gives is a machine claim, written with its run
        reference (CLAUDE.md rule 2, ADR-0003)."""
        batch = self.answered(dict(CLAIMS[1], curated=True))
        (claim,) = batch.claims
        assert claim.curated is False
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        result = BatchWriter(wiki).write_batch(batch)
        person = wiki.wbi.item.get(result.created_items["person-1"])
        (statement,) = person.claims.get(PROPERTIES["date_of_birth"])
        assert _cited(statement, "stated_in") == ["Q23"]
        assert _cited(statement, "inferred_from_extraction_run") == [
            result.run_qid
        ]

    def test_a_row_approval_the_model_writes_is_dropped(self):
        """A depicts claim is written only with the approval a curator
        gives it on its own review-sheet row (ADR-0005). One the model
        wrote is dropped, so the writer's gate holds the claim for a
        curator."""
        from yizkr.pipeline.review import unapproved_depicts

        depicts = {
            "subject": {"kind": "qid", "qid": "Q30"},
            "property": "depicts",
            "value": {"type": "item", "value": CLAIMS[1]["subject"]},
            "source": {"artifact": {"kind": "qid", "qid": "Q30"}},
            "row_approval": "0123456789ab",
        }
        batch = self.answered(depicts)
        (claim,) = batch.claims
        assert claim.row_approval is None
        assert unapproved_depicts(batch) == [claim_id(claim)]


class TestIngestExtract:
    """`yizkr ingest extract`: its claims cite the artifact its
    transcription names, by the SHA-1 of its media (#102, #127). A
    transcription of media on the never-ingest list is refused before the
    engine is asked anything: by the key the transcription names, and by
    the name and bytes of the media file `--media` gives. A transcription
    keeps no file name, so a list that names files by pattern refuses to
    run without `--media`."""

    # the transcribed media's file name, which a pattern can match
    NAME = "example-marriage-certificate.png"

    @pytest.fixture(autouse=True)
    def no_host_settings(self, tmp_path, monkeypatch):
        """No list from the host: an empty env file of the test's own,
        and the list's variable cleared from the environment."""
        env_file = tmp_path / "test.env"
        env_file.write_text("")
        monkeypatch.setenv("YIZKR_ENV_FILE", str(env_file))
        monkeypatch.delenv("YIZKR_EXCLUDE_FILE", raising=False)

    def run(
        self,
        tmp_path,
        monkeypatch,
        listed: str | None = "",
        *,
        media: bytes | None = None,
        engine: str = "llm",
        transcription: str | None = None,
    ):
        """The step on a transcription of MEDIA (or on `transcription`'s
        text), with the llm engine or a claims file. `listed` is the
        never-ingest list's text; with None, no list is configured here.
        `media`, when given, is written to a file named NAME, and passed
        as `--media`."""
        from yizkr import cli
        from yizkr.pipeline import llm

        if listed is not None:
            never = tmp_path / "never-ingest.txt"
            never.write_text(listed)
            monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(never))
        path = tmp_path / "transcription.json"
        path.write_text(
            transcription
            or Transcription(
                artifact=MEDIA_REF, text="married 1947", engine="test"
            ).model_dump_json()
        )
        # the answer cites an item by QID, which the claims do not keep
        by_qid = dict(
            CLAIMS[1], source={"artifact": {"kind": "qid", "qid": "Q23"}}
        )
        self.engine = FakeLlm(
            json.dumps(by_qid) + '\n{"done": true, "claims_emitted": 1}'
        )
        monkeypatch.setattr(llm, "make_client", lambda *a, **k: self.engine)
        out = tmp_path / "batch.json"
        argv = ["ingest", "extract", "--transcription", str(path)]
        if media is not None:
            (tmp_path / self.NAME).write_bytes(media)
            argv += ["--media", str(tmp_path / self.NAME)]
        if engine == "file":
            claims = tmp_path / "claims.json"
            claims.write_text(
                json.dumps(
                    {
                        "claims": [
                            dict(CLAIMS[1], source={"artifact": ARTIFACT})
                        ]
                    }
                )
            )
            argv += ["--engine", "file", "--claims-file", str(claims)]
        else:
            argv += ["--engine", "llm", "--strategy", "single"]
        args = cli.build_parser().parse_args([*argv, "--out", str(out)])
        return args.func(args), out, self.engine

    def refused(self, tmp_path, match: str, **run) -> None:
        """The step is refused, saying `match`, before the engine is asked
        anything: no answer is dumped, and no batch written."""
        with pytest.raises(SystemExit, match=match):
            self.run(tmp_path, **run)
        assert self.engine.prompts == []
        assert sorted(path.name for path in tmp_path.glob("batch.json*")) == []

    def test_the_claims_cite_the_transcriptions_artifact(
        self, tmp_path, monkeypatch
    ):
        status, out, engine = self.run(tmp_path, monkeypatch)
        assert status == 0
        batch = ClaimBatch.model_validate_json(out.read_text())
        assert [claim.source.artifact for claim in batch.claims] == [MEDIA_REF]
        assert len(engine.prompts) == 1

    @pytest.mark.parametrize(
        "listed",
        [None, "", "# nothing listed yet\n", f"{'1' * 40}\n"],
        ids=["no-list", "empty-list", "comments-only", "other-hash"],
    )
    def test_without_media_it_runs_where_hashes_can_decide(
        self, tmp_path, monkeypatch, listed
    ):
        """No list, or a list with no patterns in it: the key the
        transcription names is all the list can be asked about."""
        status, out, engine = self.run(tmp_path, monkeypatch, listed)
        assert status == 0
        assert out.exists() and len(engine.prompts) == 1

    @pytest.mark.parametrize("engine", ["llm", "file"])
    @pytest.mark.parametrize("media", [None, MEDIA], ids=["key", "media"])
    def test_media_listed_by_its_hash_is_refused(
        self, tmp_path, monkeypatch, engine, media
    ):
        self.refused(
            tmp_path,
            "on the never-ingest list",
            monkeypatch=monkeypatch,
            listed=f"{MEDIA_SHA1}\n",
            media=media,
            engine=engine,
        )

    @pytest.mark.parametrize("engine", ["llm", "file"])
    def test_a_list_with_patterns_needs_the_media(
        self, tmp_path, monkeypatch, engine
    ):
        """A transcription made before its media was listed by name: its
        key is on no list, and without the media file the pattern cannot
        be checked, so the step refuses to run."""
        self.refused(
            tmp_path,
            "names files by pattern",
            monkeypatch=monkeypatch,
            listed="*marriage-certificate*\n",
            engine=engine,
        )

    def test_media_listed_by_its_name_is_refused(self, tmp_path, monkeypatch):
        self.refused(
            tmp_path,
            f"{self.NAME} is on the never-ingest list",
            monkeypatch=monkeypatch,
            listed="*marriage-certificate*\n",
            media=MEDIA,
        )

    def test_media_a_pattern_does_not_match_runs(self, tmp_path, monkeypatch):
        status, out, engine = self.run(
            tmp_path, monkeypatch, "*divorce-judgment*\n", media=MEDIA
        )
        assert status == 0
        assert out.exists() and len(engine.prompts) == 1

    @pytest.mark.parametrize("listed", [None, "*divorce-judgment*\n"])
    def test_media_that_is_not_the_transcriptions_is_refused(
        self, tmp_path, monkeypatch, listed
    ):
        """`--media` must be the file the transcription names, or the list
        would be asked about another document."""
        self.refused(
            tmp_path,
            "is not the media transcription.json transcribes",
            monkeypatch=monkeypatch,
            listed=listed,
            media=b"fictional scan: another page",
        )

    def test_a_configured_list_that_cannot_be_read_is_refused(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(tmp_path / "gone.txt"))
        self.refused(
            tmp_path,
            "cannot read the never-ingest list",
            monkeypatch=monkeypatch,
            listed=None,
        )

    def test_a_transcription_that_names_a_qid_is_refused_with_a_reason(
        self, tmp_path, monkeypatch
    ):
        """A transcription made before #127 names its artifact by QID: the
        step says in one line that it cannot read it, and to transcribe the
        media again."""
        by_qid = {"artifact_qid": "Q23", "text": "married 1947", "engine": "t"}
        with pytest.raises(SystemExit) as refused:
            self.run(tmp_path, monkeypatch, transcription=json.dumps(by_qid))
        reason = str(refused.value.code)
        assert "\n" not in reason
        assert reason.startswith(
            "transcription.json is not a transcription this step reads"
        )
        assert "artifact: Field required" in reason
        assert "artifact_qid: Extra inputs are not permitted" in reason
        assert reason.endswith(
            "transcribe the media again with `yizkr ingest transcribe`"
        )
        assert self.engine.prompts == []

    @pytest.mark.parametrize(
        "argv",
        [
            ("transcribe", "--file", "scan.png", "--artifact", "Q23"),
            ("extract", "--transcription", "t.json", "--artifact", "Q23"),
        ],
        ids=["transcribe-artifact", "extract-artifact"],
    )
    def test_transcribe_and_extract_take_no_qid(self, argv, capsys):
        """No transcription and no extraction prompt names an artifact by
        its QID (#127): `ingest transcribe` names it by the SHA-1 of the
        file it reads, and `ingest extract` reads that key from the
        transcription."""
        from yizkr import cli

        with pytest.raises(SystemExit) as refused:
            cli.build_parser().parse_args(["ingest", *argv])
        assert refused.value.code == 2
        assert f"unrecognized arguments: {' '.join(argv[-2:])}" in (
            capsys.readouterr().err
        )


@pytest.fixture(scope="module")
def head() -> str:
    """The short commit of the checkout the package runs from, asked of
    git before any test's patches."""
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=Path(extract.__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestRunMetadata:
    """Every batch names the short commit of the checkout that made it.
    Each batch asks git until it answers, and the answer is kept for the
    process (#254)."""

    @pytest.fixture(autouse=True)
    def unanswered(self, monkeypatch):
        """git has not answered in this process yet; the answer the
        process kept comes back after the test."""
        monkeypatch.setattr(extract, "_commit", None)

    @pytest.fixture
    def asked(self, monkeypatch):
        """The commands started through subprocess.run in the test."""
        commands = []
        real_run = subprocess.run

        def run(args, *more, **options):
            commands.append(list(args))
            return real_run(args, *more, **options)

        monkeypatch.setattr(subprocess, "run", run)
        return commands

    def test_it_is_the_short_head_of_the_checkout(
        self, head, monkeypatch, tmp_path
    ):
        # git is asked in the package's checkout, whatever the working
        # directory
        monkeypatch.chdir(tmp_path)
        assert extract.run_metadata("fake-model").software_commit == head

    def test_git_is_asked_once_it_has_answered(self, head, asked):
        commits = [
            extract.run_metadata("fake-model").software_commit
            for _ in range(3)
        ]
        assert commits == [head] * 3
        assert len(asked) == 1

    @pytest.mark.parametrize("without", ["git", "a checkout"])
    def test_without_git_or_a_checkout_each_batch_asks_again(
        self, asked, monkeypatch, tmp_path, without
    ):
        if without == "git":
            monkeypatch.setenv("PATH", str(tmp_path))  # no git to be found
        else:
            monkeypatch.setenv("GIT_DIR", str(tmp_path / "no-repository"))
        for _ in range(2):
            assert extract.run_metadata("fake-model").software_commit is None
        assert len(asked) == 2

    @pytest.mark.parametrize(
        "failure",
        [
            "git cannot start",
            "git exits with an error",
            "an error exit prints a line",
        ],
    )
    def test_a_failed_ask_costs_only_its_own_batch(
        self, head, asked, monkeypatch, tmp_path, failure
    ):
        """The next batch asks again, and keeps the answer it gets."""
        with monkeypatch.context() as patch:
            if failure == "git exits with an error":
                patch.setenv("GIT_DIR", str(tmp_path / "no-repository"))
            else:

                def run(args, *more, **options):
                    if failure == "git cannot start":
                        raise OSError(errno.EAGAIN, "no process to be had")
                    # as `git rev-parse HEAD` does in a repository with no
                    # commit yet
                    return subprocess.CompletedProcess(
                        args, 128, stdout="HEAD\n", stderr="fatal: HEAD\n"
                    )

                patch.setattr(subprocess, "run", run)
            assert extract.run_metadata("fake-model").software_commit is None
        failed = len(asked)
        later = [
            extract.run_metadata("fake-model").software_commit
            for _ in range(2)
        ]
        assert later == [head, head]
        assert len(asked) == failed + 1


class TestResolve:
    def test_exact_label_matches_replace_local_refs(self):
        resolved = resolve_batch(batch(), {"Person One": "Q40"})
        for claim in resolved.claims:
            assert claim.subject == QidRef(qid="Q40")
        # Person Two unmatched: stays local
        assert isinstance(resolved.claims[0].value.value, LocalRef)

    def test_no_matches_is_identity(self):
        assert resolve_batch(batch(), {}) == batch()

    def test_original_batch_untouched(self):
        original = batch()
        resolve_batch(original, {"Person One": "Q40"})
        assert isinstance(original.claims[0].subject, LocalRef)

    def test_source_artifact_stays_content_addressed(self):
        # entity resolution maps entities, never the cited artifact:
        # that resolves to an item only at write time
        resolved = resolve_batch(batch(), {"Person One": "Q40"})
        for claim in resolved.claims:
            assert claim.source.artifact == ArtifactRef(sha1=MEDIA_SHA1)


class TestReviewRoundTrip:
    def test_export_columns_and_content(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        rows = export_review_csv(batch(), csv_path)
        assert rows == 2
        content = csv_path.read_text()
        assert "claim_id" in content and "decision" in content
        assert "NEW:person-1 (Person One)" in content
        assert "1914-05" in content
        assert "point_in_time=1947" in content
        with open(csv_path, newline="") as f:
            sources = {row["source_artifact"] for row in csv.DictReader(f)}
        assert sources == {f"sha1:{MEDIA_SHA1}"}

    @staticmethod
    def edit_csv(path, transform):
        """Edit the review CSV the way a reviewer tool would (by rows)."""
        import csv as csv_module

        with open(path, newline="") as f:
            reader = csv_module.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        rows = transform(rows)
        with open(path, "w", newline="") as f:
            writer = csv_module.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_only_explicit_approvals_pass(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)
        ids = [claim_id(c) for c in source.claims]

        def approve_first_only(rows):
            rows[0]["decision"] = "approve"  # second row left undecided
            return rows

        self.edit_csv(csv_path, approve_first_only)
        approved, warnings = apply_review(source, csv_path)
        assert [claim_id(c) for c in approved.claims] == [ids[0]]
        assert any("no decision" in w for w in warnings)

    def test_reordered_csv_still_applies_by_id(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def sort_and_approve_all(rows):
            for row in rows:
                row["decision"] = "approve"
            return list(reversed(rows))  # reviewer sorted the sheet

        self.edit_csv(csv_path, sort_and_approve_all)
        approved, warnings = apply_review(source, csv_path)
        assert len(approved.claims) == 2
        assert warnings == []

    def test_edited_claim_content_is_detected(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)
        edited = batch()
        edited.claims[0].source.page_number = "2"  # content drifted
        approved, warnings = apply_review(edited, csv_path)
        assert any("no row in the CSV" in w for w in warnings)
        assert any("matches no claim" in w for w in warnings)

    @staticmethod
    def booklet_batch() -> ClaimBatch:
        """`batch()`'s claims read from *Our Family from Germany*'s physical
        page 6, printed as page 4 (docs/EXAMPLES.md), then `batch()`'s
        own birth claim, which cites no page, and its sibling claim on a
        page 2 the record gives no printed number."""
        booklet = copy.deepcopy(CLAIMS)
        for claim in booklet:
            claim["source"]["page_number"] = "6"
            claim["source"]["printed_page_number"] = "4"
        unprinted = copy.deepcopy(CLAIMS[0])
        unprinted["source"]["page_number"] = "2"
        return ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [*booklet, CLAIMS[1], unprinted],
            }
        )

    @staticmethod
    def read_rows(path) -> tuple[list[str], list[dict]]:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            return list(reader.fieldnames or []), list(reader)

    def test_printed_page_is_shown_beside_the_page(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        export_review_csv(self.booklet_batch(), csv_path)
        fieldnames, rows = self.read_rows(csv_path)
        assert fieldnames.index("printed_page") == (
            fieldnames.index("page") + 1
        )
        assert [(r["page"], r["printed_page"]) for r in rows] == [
            ("6", "4"),  # the booklet's printed number for its page 6
            ("6", "4"),
            ("", ""),  # no page cited, so no printed number
            ("2", ""),  # a page the record gives no printed number
        ]

    def test_edited_printed_page_cells_are_never_written(self, tmp_path):
        source = self.booklet_batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def edit_locators_and_approve(rows):
            # a curator "fixes" the locators, then approves every row
            for row in rows:
                row["page"] = "5"
                row["printed_page"] = "17"
                row["decision"] = "approve"
            return rows

        self.edit_csv(csv_path, edit_locators_and_approve)
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        assert approved.claims == source.claims
        assert [
            (c.source.page_number, c.source.printed_page_number)
            for c in approved.claims
        ] == [("6", "4"), ("6", "4"), (None, None), ("2", None)]

    def test_a_dropped_printed_page_column_changes_nothing(self, tmp_path):
        source = self.booklet_batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        fieldnames = [n for n in CSV_COLUMNS if n != "printed_page"]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                row.pop("printed_page")
                writer.writerow({**row, "decision": "approve"})
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        assert approved.claims == source.claims

    def test_a_corrected_claim_keeps_its_printed_page(self, tmp_path):
        source = self.booklet_batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_the_booklet_row(rows):
            for row in rows:
                row["decision"] = "approve"
                row["printed_page"] = ""  # cleared by the curator
            rows[1]["correction"] = "1914-05-29"
            rows[1]["page"] = "5"  # and edits the corrected row's locators
            rows[1]["printed_page"] = "17"
            return rows

        self.edit_csv(csv_path, correct_the_booklet_row)
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        corrected = approved.claims[1]
        assert corrected.curated and corrected.value.day == 29
        assert corrected.source.page_number == "6"
        assert corrected.source.printed_page_number == "4"

    def test_claim_id_stable_across_serialization(self):
        first = batch().claims[0]
        again = ClaimBatch.model_validate_json(
            batch().model_dump_json()
        ).claims[0]
        assert claim_id(first) == claim_id(again)


def _write_equivalences_csv(path, rows):
    """Write a minimal equivalences.csv (entity_id/qid/verdict/decision)."""
    from yizkr.pipeline.reconcile import EQUIVALENCE_FIELDS

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EQUIVALENCE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: row.get(field, "") for field in EQUIVALENCE_FIELDS}
            )


class TestAcceptAll:
    """`accept_all` fills the decision column, ratifying the machine: a
    claims sheet round-trips through apply_review as a full approval; an
    equivalences sheet ratifies each verdict rather than merging all."""

    def test_fills_every_decision_and_apply_review_approves_all(
        self, tmp_path
    ):
        source = batch()
        review_csv = tmp_path / "review.csv"
        export_review_csv(source, review_csv)  # decisions start empty

        reviewed_csv = tmp_path / "review.reviewed.csv"
        result = accept_all(review_csv, reviewed_csv)
        assert result.mode == "claims"
        assert result.total == len(source.claims)
        assert result.approved == len(source.claims)

        with open(reviewed_csv, newline="") as f:
            reviewed_rows = list(csv.DictReader(f))
        assert reviewed_rows
        assert all(
            r["decision"].strip().lower() in APPROVE for r in reviewed_rows
        )

        approved, warnings = apply_review(source, reviewed_csv)
        assert len(approved.claims) == len(source.claims)
        assert warnings == []

    def test_keeps_every_cell_but_the_decision(self, tmp_path):
        source = TestReviewRoundTrip.booklet_batch()
        review_csv = tmp_path / "review.csv"
        export_review_csv(source, review_csv)
        reviewed_csv = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed_csv)

        def without_decisions(path):
            with open(path, newline="") as f:
                return [
                    {k: v for k, v in row.items() if k != "decision"}
                    for row in csv.DictReader(f)
                ]

        exported = without_decisions(review_csv)
        assert [row["printed_page"] for row in exported] == ["4", "4", "", ""]
        assert without_decisions(reviewed_csv) == exported

    def test_equivalences_ratifies_verdicts_not_blanket_approve(
        self, tmp_path
    ):
        # ratify the machine per row: same->approve, distinct->reject,
        # uncertain->blank; a `same` against two items is a contradiction
        # left blank (it must not merge). Verifies the reviewed file is
        # applicable: only the lone `same` survives read_approved_equivalences.
        from yizkr.pipeline.reconcile import read_approved_equivalences

        src = tmp_path / "equivalences.csv"
        _write_equivalences_csv(
            src,
            [
                {"entity_id": "e1", "qid": "Q10", "verdict": "same"},
                {"entity_id": "e2", "qid": "Q20", "verdict": "distinct"},
                {"entity_id": "e3", "qid": "Q30", "verdict": "uncertain"},
                # e4 judged "same" against two items -> contradiction
                {"entity_id": "e4", "qid": "Q40", "verdict": "same"},
                {"entity_id": "e4", "qid": "Q41", "verdict": "same"},
            ],
        )
        out = tmp_path / "equivalences.reviewed.csv"
        result = accept_all(src, out)
        assert result.mode == "equivalences"
        assert (result.approved, result.rejected, result.uncertain) == (
            1,
            1,
            1,
        )
        assert result.conflicted == 2

        with open(out, newline="") as f:
            by_pair = {
                (r["entity_id"], r["qid"]): r["decision"]
                for r in csv.DictReader(f)
            }
        assert by_pair[("e1", "Q10")] == "approve"
        assert by_pair[("e2", "Q20")] == "reject"
        assert by_pair[("e3", "Q30")] == ""
        assert by_pair[("e4", "Q40")] == "" and by_pair[("e4", "Q41")] == ""

        # applicable and conflict-free: only e1 is an approved merge
        assert read_approved_equivalences(out) == {"e1": "Q10"}

    def test_refuses_a_non_equivalences_verdict_csv(self, tmp_path):
        # a reconcile-places sheet has verdict+decision but no entity_id
        places = tmp_path / "places.csv"
        places.write_text("qid_a,qid_b,verdict,decision\nQ1,Q2,same,\n")
        with pytest.raises(ValueError, match="no 'entity_id'"):
            accept_all(places, tmp_path / "out.csv")

    def test_preserves_claim_ids_and_other_columns(self, tmp_path):
        # only `decision` changes — claim_id (the content hash apply_review
        # matches on) and every other column stay byte-identical
        source = batch()
        review_csv = tmp_path / "review.csv"
        export_review_csv(source, review_csv)
        with open(review_csv, newline="") as f:
            before = {r["claim_id"]: dict(r) for r in csv.DictReader(f)}

        reviewed_csv = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed_csv)
        with open(reviewed_csv, newline="") as f:
            after = {r["claim_id"]: dict(r) for r in csv.DictReader(f)}

        assert before.keys() == after.keys()
        for cid, row in after.items():
            assert row["decision"] == "bulk-accept"  # ADR-0023 disposition
            for col, value in before[cid].items():
                if col != "decision":
                    assert row[col] == value

    def test_rejects_a_non_review_csv(self, tmp_path):
        bad = tmp_path / "not-a-review.csv"
        bad.write_text("a,b\n1,2\n")
        with pytest.raises(ValueError, match="not a review CSV"):
            accept_all(bad, tmp_path / "out.csv")

    def test_bulk_accept_disposition_round_trips(self, tmp_path):
        # ADR-0023: a pristine accept-all sheet is detectable for the write
        # path (which marks its edits low-scrutiny), and still approves.
        source = batch()
        review_csv = tmp_path / "review.csv"
        export_review_csv(source, review_csv)
        reviewed = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed)

        assert is_bulk_accepted(reviewed)
        approved, warnings = apply_review(source, reviewed)
        assert len(approved.claims) == len(source.claims)
        assert warnings == []

    def test_human_touch_demotes_bulk_accept(self, tmp_path):
        source = batch()
        review_csv = tmp_path / "review.csv"
        export_review_csv(source, review_csv)
        reviewed = tmp_path / "review.reviewed.csv"
        accept_all(review_csv, reviewed)

        def flip_one(rows):
            rows[0]["decision"] = "approve"  # a per-row human decision
            return rows

        TestReviewRoundTrip.edit_csv(reviewed, flip_one)
        assert not is_bulk_accepted(reviewed)  # no longer a pristine sheet

    def test_is_bulk_accepted_false_for_missing_or_plain(self, tmp_path):
        assert not is_bulk_accepted(tmp_path / "nope.csv")
        source = batch()
        plain = tmp_path / "review.csv"
        export_review_csv(source, plain)  # decisions empty
        assert not is_bulk_accepted(plain)


class TestCritiqueNotes:
    """ADR-0013 / ADR-0020: the review CSV carries the critic's concerns
    in a single-role `critique_notes` column. There is no possibly-living
    warning and no `living` column — privacy is a runtime determination,
    never a review decision."""

    def test_critique_notes_render_in_their_column(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        # notes arrive already formatted, by row (critique.note, attach)
        source = batch()
        assert len(source.claims) > 1
        export_review_csv(
            source,
            csv_path,
            critique_notes={1: "critique: role label 'MOTHER' is not a name"},
        )
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            rows = list(reader)
        assert "critique_notes" in fields
        assert "warning" not in fields  # renamed
        assert "living" not in fields  # retired
        # the note is on its own row alone (#113)
        assert [bool(r["critique_notes"]) for r in rows] == [
            index == 1 for index in range(len(rows))
        ]
        assert rows[1]["critique_notes"].startswith("critique:")

    def test_no_critique_leaves_column_blank(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        export_review_csv(batch(), csv_path)
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows
        assert all(row["critique_notes"] == "" for row in rows)


class TestCorrections:
    """Review corrections: the correction column produces curator claims."""

    @pytest.mark.parametrize(
        ("correction", "expected"),
        [
            # a day above 12, which cannot be read as a month
            ("1914-05-29", (1914, 5, 29)),
            # day and month both 12 or less: in ISO order still one date,
            # 1 July 1919, where the slash form below leaves two open
            ("1919-07-01", (1919, 7, 1)),
        ],
        ids=["day-above-12", "day-and-month-both-12-or-less"],
    )
    def test_date_correction_replaces_value_and_marks_curated(
        self, tmp_path, correction, expected
    ):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_birth(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "date_of_birth":
                    row["correction"] = correction
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_birth)
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        (birth,) = [
            c for c in approved.claims if c.property == "date_of_birth"
        ]
        value = birth.value
        assert (value.year, value.month, value.day) == expected
        assert birth.value.precision == "day"
        assert birth.curated is True
        (sibling,) = [c for c in approved.claims if c.property == "sibling"]
        assert sibling.curated is False

    def test_ambiguous_date_correction_is_refused(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_badly(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "date_of_birth":
                    # 7 January or 1 July? The universe's two sources for
                    # Gustav's birth transpose exactly this day and month
                    row["correction"] = "07/01/1919"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_badly)
        approved, warnings = apply_review(source, csv_path)
        assert [c.property for c in approved.claims] == ["sibling"]
        assert any("not an unambiguous date" in w for w in warnings)

    def test_item_values_cannot_be_text_corrected(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_sibling(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "sibling":
                    row["correction"] = "Someone Else"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_sibling)
        approved, warnings = apply_review(source, csv_path)
        assert [c.property for c in approved.claims] == ["date_of_birth"]
        assert any("cannot be corrected as text" in w for w in warnings)

    def test_quantity_correction_takes_plain_numbers_only(self):
        from yizkr.pipeline.review import _parse_correction

        data = batch().claims[1].model_dump(mode="json")
        data["property"] = "number_of_children"
        data["value"] = {"type": "quantity", "amount": 3}
        count = ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": [data]}
        ).claims[0]
        corrected = _parse_correction(count, "4")
        assert corrected.value.amount == 4
        assert corrected.curated is True
        with pytest.raises(ValueError, match="not a number"):
            _parse_correction(count, "several")


# -- writer: cited artifacts resolve by media SHA-1 at write time (#102) ----

PROPERTIES = {
    "instance_of": "P1",
    "stated_in": "P2",
    "page_number": "P3",
    "transcription_excerpt": "P4",
    "inferred_from_extraction_run": "P5",
    "media_sha1": "P6",
    "spouse": "P7",
    "date_of_birth": "P8",
    "point_in_time": "P9",
    "relative_position_within_image": "P10",
    "type_hint": "P11",
    "referred_to_as": "P12",
    "start_time": "P13",
    "depicts": "P14",
    "residence": "P15",
    "location": "P16",
    "birth_name": "P17",
    "married_name": "P18",
    "nickname": "P19",
    "place_of_birth": "P20",
    "relative": "P21",
    "kinship_type": "P22",
    "whg_place_id": "P23",
    "cause_of_death": "P24",
    "sibling": "P25",
    "printed_page_number": "P26",
}
ITEMS = {
    "human": "Q1",
    "extraction_run": "Q2",
    "unclassified_entity": "Q3",
    "organization": "Q4",
    "place": "Q5",
}


def _snaks(statement: dict) -> list[dict]:
    """Every snak of a statement's JSON: its value's, then each
    qualifier's, then each of its references'."""
    return [
        statement["mainsnak"],
        *(
            snak
            for snaks in statement.get("qualifiers", {}).values()
            for snak in snaks
        ),
        *(
            snak
            for block in statement.get("references", [])
            for snaks in block["snaks"].values()
            for snak in snaks
        ),
    ]


# Wikibase's `illegal-string-chars` check, /^\s|[\v\t]|\s$/u, which it
# makes of a text value and of a trimmed term, as PHP matches it in the
# wikibase/wikibase:7 image, by Unicode property (#384): the characters of
# `\s`, which may not begin or end a text, and those of `[\v\t]`, which may
# not be in one. Written out here, apart from the writer's own sets, so that
# a fault in those is never the fake's too
WIKIBASE_SPACE = "".join(
    map(
        chr,
        (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680, 0x180E)
        + tuple(range(0x2000, 0x200B))
        + (0x2028, 0x2029, 0x202F, 0x205F, 0x3000),
    )
)
WIKIBASE_BREAK_OR_TAB = "".join(
    map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029))
)
# the separators (Unicode Z) Wikibase trims from either end of a term
WIKIBASE_SEPARATORS = "".join(
    map(
        chr,
        (0x20, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029)
        + (0x202F, 0x205F, 0x3000),
    )
)


def _illegal(text: str) -> bool:
    """Whether Wikibase's `illegal-string-chars` check refuses `text`."""
    return bool(text) and (
        text[0] in WIKIBASE_SPACE
        or text[-1] in WIKIBASE_SPACE
        or any(char in WIKIBASE_BREAK_OR_TAB for char in text)
    )


# the characters Wikibase makes U+FFFD, one for one, wherever it cleans a
# text up (UtfNormal's Validator::cleanUp, 4.0.0): every C0 control
# character but the tab, the line feed and the carriage return, and the
# noncharacters U+FFFE and U+FFFF. It cleans up a string value before it
# checks and stores it (StringNormalizer::cleanupToNFC), a term once it
# has trimmed it (trimToNFC), and every text its API gives back, a
# monolingual text among them (ApiResult, through Language::normalize;
# #403)
WIKIBASE_CLEANED = "".join(
    map(chr, (*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF))
)


def _cleaned_up(text: str) -> str:
    """A text as Wikibase cleans it up (cleanupToNFC): each character of
    WIKIBASE_CLEANED made U+FFFD, and the whole in NFC. It does so to a
    string value before it checks it, so it changes a vertical tab or a
    form feed in a string value, where it refuses one in a monolingual text
    (#401's review)."""
    return unicodedata.normalize(
        "NFC",
        "".join(
            "\N{REPLACEMENT CHARACTER}" if char in WIKIBASE_CLEANED else char
            for char in text
        ),
    )


def _kept_term(term: str) -> str:
    """A label, description or alias as Wikibase keeps it
    (StringNormalizer::trimToNFC): first trimmed (trimWhitespace), each
    run of control characters made one space and the separators at either
    end dropped, then cleaned up (_cleaned_up), which makes U+FFFE and
    U+FFFF U+FFFD, and in NFC (#403)."""
    term = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", term)
    return _cleaned_up(term.strip(WIKIBASE_SEPARATORS))


def _refusal(name: str, info: str, text: str):
    from wikibaseintegrator.wbi_exceptions import ModificationFailed

    return ModificationFailed(
        {
            "code": "modification-failed",
            "info": info,
            "messages": [{"name": name, "parameters": [text]}],
        }
    )


def _refuse_as_the_instance(item) -> None:
    """Refuse an edit of `item` as Wikibase does, whole, when it sends a
    text Wikibase does not hold, and keep nothing of it:

    * in a snak of one of its statements (a value, a qualifier or a
      reference), a text of more than 400 characters (its
      `string-limits`), a string value (of a string or external-id snak)
      counted as Wikibase cleans it up before it checks it (_cleaned_up),
      in NFC, and a monolingual text as it is sent (#366); or a text with
      a line break or a tab in it, or whitespace at either end (its
      `illegal-string-chars`, #384), a string value again as cleaned up;
    * a label, description or alias that, as Wikibase keeps it
      (_kept_term), has more than 250 characters or fails the same check,
      and an alias with nothing left (#384);
    * an item whose label and description, as Wikibase keeps them, are
      the same (its LabelDescriptionNotEqualValidator, #401's review)."""
    data = item.get_json()
    for statements in data.get("claims", {}).values():
        for statement in statements:
            for snak in _snaks(statement):
                datavalue = snak.get("datavalue") or {}
                if datavalue.get("type") == "string":
                    text = _cleaned_up(datavalue["value"])
                elif datavalue.get("type") == "monolingualtext":
                    text = datavalue["value"]["text"]
                else:
                    continue
                if len(text) > 400:
                    raise _refusal(
                        "wikibase-validator-too-long",
                        "Must be no more than 400 characters long",
                        text,
                    )
                if _illegal(text):
                    raise _refusal(
                        "wikibase-validator-illegal-string-chars",
                        "Illegal characters in a text value",
                        text,
                    )
    terms = [
        *(
            (term["value"], False)
            for kind in ("labels", "descriptions")
            for term in data.get(kind, {}).values()
        ),
        *(
            (alias["value"], True)
            for aliases in data.get("aliases", {}).values()
            for alias in aliases
        ),
    ]
    for term, alias in terms:
        kept = _kept_term(term)
        if len(kept) > 250:
            raise _refusal(
                "wikibase-validator-too-long",
                "Must be no more than 250 characters long",
                kept,
            )
        if _illegal(kept) or (alias and not kept):
            raise _refusal(
                "wikibase-validator-illegal-string-chars",
                "Illegal characters in a term",
                kept,
            )
    labels = data.get("labels", {})
    for language, description in data.get("descriptions", {}).items():
        kept = _kept_term(description["value"])
        label = labels.get(language)
        if kept and label and _kept_term(label["value"]) == kept:
            raise _refusal(
                "wikibase-validator-label-equals-description",
                "Label and description must not be the same",
                kept,
            )


class FakeItems:
    """WikibaseIntegrator items held in memory; write() assigns a QID.

    Every get() returns the one object the store holds, so a write through
    any copy is seen through every other (compare DetachedItems). As an
    instance does, write() refuses an edit that sends a text Wikibase does
    not hold, over its limits or with a line break, a tab or whitespace at
    either end (_refuse_as_the_instance). Unlike an instance, it holds each
    text as it was sent: DetachedItems keeps and serves text as an
    instance does, cleaned up, trimmed and in NFC."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.writes = 0
        self._next = 100

    def new(self):
        from wikibaseintegrator import WikibaseIntegrator

        item = WikibaseIntegrator().item.new()

        def write(summary=None, **_):
            _refuse_as_the_instance(item)
            self.writes += 1
            if item.id is None:
                item.id = f"Q{self._next}"
                self._next += 1
            self.store[item.id] = item
            return item

        item.write = write
        return item

    def get(self, qid):
        return self.store[qid]


def _in_form(value, form: str):
    """`value`, a JSON document, with every string in it in the Unicode
    normalization form `form` ("NFC", "NFD")."""
    if isinstance(value, str):
        return unicodedata.normalize(form, value)
    if isinstance(value, dict):
        return {key: _in_form(item, form) for key, item in value.items()}
    if isinstance(value, list):
        return [_in_form(item, form) for item in value]
    return value


class DetachedItems(FakeItems):
    """Items as an instance serves them: every get() returns a copy of the
    item as last written, and a write stores the writing copy whole. So a
    write through a copy read before another write undoes that write, as
    an edit sent from a stale read does on an instance: an edit sends the
    item's aliases whole, and its statements as that copy holds them.

    As an instance stores an edit, a write keeps a string or external-id
    value, in a statement, a qualifier or a reference, cleaned up
    (_cleaned_up, in NFC), a monolingual text as it was sent, and a label,
    a description or an alias trimmed and cleaned up (_kept_term, #384,
    #403): an empty one is gone, and so is a repeated alias (_as_stored).
    As an instance's API serves an item, every text in a copy is cleaned
    up, and so in NFC, monolingual text included (#335, #349, #403;
    _as_served). `sent` keeps each write's item as the writer sent it. As
    an instance does, a write that sends a text Wikibase does not hold is
    refused whole, and changes nothing (_refuse_as_the_instance, #366,
    #384).

    Unlike an instance, the store's round trip through `get_json` and
    `from_json` ignores WikibaseIntegrator's `remove` flags: a statement
    or alias removed through a copy stays. The writer removes nothing."""

    def __init__(self) -> None:
        super().__init__()
        self._statement_ids = itertools.count(1)
        self.sent: list[dict] = []

    def _bound(self, item):
        def write(summary=None, **_):
            _refuse_as_the_instance(item)
            self.writes += 1
            if item.id is None:
                item.id = f"Q{self._next}"
                self._next += 1
            for statement in item.claims:
                if statement.id is None:
                    statement.id = f"{item.id}${next(self._statement_ids)}"
            self.sent.append(item.get_json())
            self.store[item.id] = self._copy(item, _as_stored)
            return item

        item.write = write
        return item

    def _copy(self, item, form):
        """A copy of `item`, its JSON document in the form `form` gives it
        (_as_stored, _as_served)."""
        from wikibaseintegrator import WikibaseIntegrator

        data = form(item.get_json())
        data["lastrevid"] = self.writes
        for statements in data.get("claims", {}).values():
            for statement in statements:
                # an instance hashes every reference block it stores
                for block in statement.get("references", []):
                    block.setdefault(
                        "hash",
                        hashlib.sha1(
                            json.dumps(block["snaks"], sort_keys=True).encode()
                        ).hexdigest(),
                    )
        return self._bound(WikibaseIntegrator().item.new().from_json(data))

    def new(self):
        from wikibaseintegrator import WikibaseIntegrator

        return self._bound(WikibaseIntegrator().item.new())

    def get(self, qid):
        return self._copy(self.store[qid], _as_served)


def _as_stored(data: dict) -> dict:
    """An item's JSON document `data`, as a write sends it, as Wikibase
    stores it: each string or external-id value, in a statement, a
    qualifier or a reference, cleaned up (_cleaned_up), a monolingual text
    as it is sent, and each label, description and alias as Wikibase keeps
    it (_kept_term), an empty one dropped, and a repeated alias too."""
    data = copy.deepcopy(data)
    for kind in ("labels", "descriptions"):
        terms = data.get(kind, {})
        for language, term in list(terms.items()):
            term["value"] = _kept_term(term["value"])
            if not term["value"]:
                del terms[language]
    for language, aliases in data.get("aliases", {}).items():
        kept = {}
        for alias in aliases:
            value = _kept_term(alias["value"])
            if value:
                kept.setdefault(value, dict(alias, value=value))
        data["aliases"][language] = list(kept.values())
    for statements in data.get("claims", {}).values():
        for statement in statements:
            for snak in _snaks(statement):
                datavalue = snak.get("datavalue") or {}
                if datavalue.get("type") == "string":
                    datavalue["value"] = _cleaned_up(datavalue["value"])
    return data


def _as_served(value):
    """`value`, a JSON document as Wikibase stores it, as its API gives it
    back: every string in it cleaned up (_cleaned_up), and so in NFC."""
    if isinstance(value, str):
        return _cleaned_up(value)
    if isinstance(value, dict):
        return {key: _as_served(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_served(item) for item in value]
    return value


class FakeWiki:
    """Just enough of WikiClient for BatchWriter: the vocabulary, the
    label lookup, the media SHA-1 index and in-memory items, `detached`
    as an instance serves them (DetachedItems)."""

    def __init__(
        self, media: dict[str, str] | None = None, detached: bool = False
    ) -> None:
        self.media = dict(media or {})  # media SHA-1 -> artifact QID
        self.index_reads = 0
        self.wbi = SimpleNamespace(
            item=DetachedItems() if detached else FakeItems()
        )

    def vocabulary(self):
        return dict(PROPERTIES), dict(ITEMS)

    def find_item_by_label(self, label):
        for qid, item in self.wbi.item.store.items():
            if str(item.labels.get("en") or "") == label:
                return qid
        return None

    def label_index(self):
        """(property labels, item labels) as the wiki client's label index
        gives them: the first item under a label, as find_item_by_label
        finds it."""
        items: dict[str, str] = {}
        for qid, item in self.wbi.item.store.items():
            label = str(item.labels.get("en") or "")
            if label:
                items.setdefault(label, qid)
        return {}, items

    def string_statement_index(self, property_pid):
        assert property_pid == PROPERTIES["media_sha1"]
        self.index_reads += 1
        return dict(self.media)


def _cited(statement, key: str) -> list[str]:
    """The items a statement's references point at through `key`."""
    return [
        snak.datavalue["value"]["id"]
        for reference in statement.references.references
        for snak in reference.snaks.get(PROPERTIES[key]) or []
    ]


def _citing(*sha1s: str) -> ClaimBatch:
    """One date-of-birth claim per media SHA-1, each citing that media."""
    claims = [
        dict(
            CLAIMS[1],
            value={"type": "time", "year": 1914 + offset},
            source={"artifact": {"kind": "artifact", "sha1": sha1}},
        )
        for offset, sha1 in enumerate(sha1s)
    ]
    return ClaimBatch.model_validate({"extraction_run": RUN, "claims": claims})


OTHER_SHA1 = hashlib.sha1(b"fictional scan: a second example page").hexdigest()


class TestWriterArtifactResolution:
    def test_sha1_resolves_to_the_registered_artifact(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        result = BatchWriter(wiki).write_batch(batch())
        assert result.written == 2
        person = wiki.wbi.item.get(result.created_items["person-1"])
        for pid in (PROPERTIES["sibling"], PROPERTIES["date_of_birth"]):
            (statement,) = person.claims.get(pid)
            assert _cited(statement, "stated_in") == ["Q23"]
            assert _cited(statement, "inferred_from_extraction_run") == [
                result.run_qid
            ]
        # a proposed entity's kind is referenced to the same artifact
        (kind,) = person.claims.get(PROPERTIES["instance_of"])
        assert _cited(kind, "stated_in") == ["Q23"]

    def test_each_claim_cites_its_own_artifact(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23", OTHER_SHA1: "Q24"})
        result = BatchWriter(wiki).write_batch(_citing(MEDIA_SHA1, OTHER_SHA1))
        person = wiki.wbi.item.get(result.created_items["person-1"])
        cited = {
            statement.mainsnak.datavalue["value"]["time"][:5]: _cited(
                statement, "stated_in"
            )
            for statement in person.claims.get(PROPERTIES["date_of_birth"])
        }
        assert cited == {"+1914": ["Q23"], "+1915": ["Q24"]}

    def test_unregistered_media_writes_nothing(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        with pytest.raises(UnregisteredArtifactError) as raised:
            BatchWriter(wiki).write_batch(_citing(MEDIA_SHA1, OTHER_SHA1))
        assert raised.value.sha1s == [OTHER_SHA1]
        assert OTHER_SHA1[:12] in str(raised.value)
        # refused before the first write: no run item, no entities, so
        # no statement is ever written without its source
        assert wiki.wbi.item.writes == 0

    def test_dry_run_resolves_the_same_way(self):
        wiki = FakeWiki()
        with pytest.raises(UnregisteredArtifactError):
            BatchWriter(wiki, dry_run=True).write_batch(batch())
        wiki.media[MEDIA_SHA1] = "Q23"
        result = BatchWriter(wiki, dry_run=True).write_batch(batch())
        assert result.written == 2
        assert wiki.wbi.item.writes == 0

    def test_rewrite_is_skipped_against_the_resolved_artifact(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        BatchWriter(wiki).write_batch(batch())
        again = BatchWriter(wiki).write_batch(batch())
        assert (again.written, again.skipped, again.referenced) == (0, 2, 0)

    def test_caller_supplied_qids_skip_the_index(self):
        # write-time registration hands over what it just registered
        wiki = FakeWiki()
        writer = BatchWriter(wiki, artifact_qids={MEDIA_SHA1.upper(): "Q23"})
        result = writer.write_batch(batch())
        assert wiki.index_reads == 0
        person = wiki.wbi.item.get(result.created_items["person-1"])
        (statement,) = person.claims.get(PROPERTIES["date_of_birth"])
        assert _cited(statement, "stated_in") == ["Q23"]

    def test_index_is_read_once_and_again_for_unseen_media(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        writer = BatchWriter(wiki)
        writer.write_batch(batch())
        writer.write_batch(batch())
        assert wiki.index_reads == 1
        # media registered after the index was read still resolves
        wiki.media[OTHER_SHA1] = "Q24"
        resolved = writer.resolve_artifacts(_citing(MEDIA_SHA1, OTHER_SHA1))
        assert resolved == {MEDIA_SHA1: "Q23", OTHER_SHA1: "Q24"}
        assert wiki.index_reads == 2

    def test_a_batch_citing_its_artifact_by_qid_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        """No dual format (#102): a batch file whose claim cites its
        artifact item by QID, as batches made before content addressing
        did, does not load, so `ingest write` refuses it before it
        contacts the instance: it reads and writes nothing."""
        from yizkr import cli

        batch_json = tmp_path / "batch.json"
        batch_json.write_text(
            json.dumps(
                {
                    "extraction_run": RUN,
                    "claims": [
                        dict(
                            CLAIMS[1],
                            source={"artifact": {"kind": "qid", "qid": "Q23"}},
                        )
                    ],
                }
            )
        )

        def no_instance(args):
            raise AssertionError("ingest write contacted the instance")

        monkeypatch.setattr(cli, "_client", no_instance)
        args = cli.build_parser().parse_args(
            ["ingest", "write", "--batch", str(batch_json)]
        )
        with pytest.raises(ValidationError, match="artifact"):
            args.func(args)


# -- writer: a minted item's type statements carry both references (#88) ---

# The family firm as the 2002 interview gives it: "The hat shop, the Blue
# Hat". The extraction holds it as an unclassified entity whose type hint
# keeps the source's word (ADR-0013).
INTERVIEW_SHA1 = hashlib.sha1(
    b"fictional transcript: the 2002 oral-history interview"
).hexdigest()
FIRM_LABEL = "Hutmacherei zum Blauen Hut"


def _firm_batch(curated: bool = False, label: str = FIRM_LABEL) -> ClaimBatch:
    firm = {
        "kind": "local",
        "local_id": "firm",
        "label": label,
        "instance_of": "unclassified_entity",
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": firm,
                    "property": "referred_to_as",
                    "value": {
                        "type": "monolingual",
                        "value": {"text": "the Blue Hat", "language": "en"},
                    },
                    "source": {
                        "artifact": {
                            "kind": "artifact",
                            "sha1": INTERVIEW_SHA1,
                        },
                        "transcription_excerpt": {
                            "text": "The hat shop, the Blue Hat",
                            "language": "en",
                        },
                    },
                    "curated": curated,
                }
            ],
            "entities": [
                {
                    "id": "firm",
                    "label": label,
                    "kind": "unclassified_entity",
                    "type_hint": "hat shop",
                }
            ],
        }
    )


class TestWriterTypeStatements:
    """A minted item's `instance of` and `type hint` are machine writes:
    stated in the artifact that mentions the entity, and inferred from the
    batch's extraction run (rules 1 and 2, ADR-0003)."""

    def test_minted_subject_and_value_kinds_cite_artifact_and_run(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        result = BatchWriter(wiki).write_batch(batch())
        # person-1 is minted as a claim's subject, person-2 as its value
        for local_id in ("person-1", "person-2"):
            item = wiki.wbi.item.get(result.created_items[local_id])
            (kind,) = item.claims.get(PROPERTIES["instance_of"])
            assert kind.mainsnak.datavalue["value"]["id"] == ITEMS["human"]
            assert _cited(kind, "stated_in") == ["Q23"]
            assert _cited(kind, "inferred_from_extraction_run") == [
                result.run_qid
            ]

    def test_type_hint_cites_artifact_and_run(self):
        wiki = FakeWiki({INTERVIEW_SHA1: "Q25"})
        result = BatchWriter(wiki).write_batch(_firm_batch())
        firm = wiki.wbi.item.get(result.created_items["firm"])
        (kind,) = firm.claims.get(PROPERTIES["instance_of"])
        (hint,) = firm.claims.get(PROPERTIES["type_hint"])
        unclassified = ITEMS["unclassified_entity"]
        assert kind.mainsnak.datavalue["value"]["id"] == unclassified
        assert hint.mainsnak.datavalue["value"] == "hat shop"
        for statement in (kind, hint):
            assert _cited(statement, "stated_in") == ["Q25"]
            assert _cited(statement, "inferred_from_extraction_run") == [
                result.run_qid
            ]

    def test_kind_beside_a_curated_claim_still_cites_the_run(self):
        # the reviewer corrected the claim's value, not the entity's kind:
        # the kind and type hint remain the extraction's proposal
        wiki = FakeWiki({INTERVIEW_SHA1: "Q25"})
        result = BatchWriter(wiki).write_batch(_firm_batch(curated=True))
        firm = wiki.wbi.item.get(result.created_items["firm"])
        (name,) = firm.claims.get(PROPERTIES["referred_to_as"])
        assert _cited(name, "stated_in") == ["Q25"]
        assert _cited(name, "inferred_from_extraction_run") == []
        for pid in (PROPERTIES["instance_of"], PROPERTIES["type_hint"]):
            (statement,) = firm.claims.get(pid)
            assert _cited(statement, "stated_in") == ["Q25"]
            assert _cited(statement, "inferred_from_extraction_run") == [
                result.run_qid
            ]


# -- writer: a later source adds to an existing item's kind (#233) ---------

# The 1912 directory's entry for the firm: "Hutmacherei zum Blauen Hut,
# gegr. 1863". A later batch than the interview's, so the firm's item
# exists when it is written.
DIRECTORY_SHA1 = hashlib.sha1(
    "fictional scan: the 1912 Flörburg address book".encode()
).hexdigest()
DIRECTORY_PAGE_2_SHA1 = hashlib.sha1(
    "fictional scan: the 1912 Flörburg address book, page 2".encode()
).hexdigest()
DIRECTORY_RUN = dict(RUN, started_at="2026-07-13T09:00:00Z")
FIRM_ENTRY = "Hutmacherei zum Blauen Hut, gegr. 1863"


def _directory_batch(
    kind: str = "organization",
    type_hint: str | None = None,
    extra_claims: tuple = (),
) -> ClaimBatch:
    """The directory's batch: the firm's founding year, with the kind
    (and type hint) the directory's extraction gives the firm."""
    firm = {
        "kind": "local",
        "local_id": "firm",
        "label": FIRM_LABEL,
        "instance_of": kind,
    }
    entity = {"id": "firm", "label": FIRM_LABEL, "kind": kind}
    if type_hint:
        entity["type_hint"] = type_hint
    source = {
        "artifact": {"kind": "artifact", "sha1": DIRECTORY_SHA1},
        "transcription_excerpt": {"text": "gegr. 1863", "language": "de"},
    }
    founded = {
        "subject": firm,
        "property": "start_time",
        "value": {"type": "time", "year": 1863},
        "source": source,
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": DIRECTORY_RUN,
            "claims": [
                founded,
                *(dict(claim, subject=firm) for claim in extra_claims),
            ],
            "entities": [entity],
        }
    )


def _directory_lines_batch(kind: str = "organization") -> ClaimBatch:
    """The directory's batch as the line-range tier gives it (ADR-0006):
    no entity manifest, so the firm's kind is given only on its local
    refs' `instance_of`."""
    return _directory_batch(kind).model_copy(update={"entities": []})


# the directory's batch with and without an entity manifest
DIRECTORY_BATCHES = {
    "manifest": _directory_batch,
    "no manifest": _directory_lines_batch,
}


def _two_sources() -> FakeWiki:
    return FakeWiki({INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"})


SEBASTIAN = "Sebastian Löbig"


def _certificate_batch() -> ClaimBatch:
    """The 1905 marriage certificate's batch: Sebastian Löbig's birth."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e01",
                        "label": SEBASTIAN,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {
                        "type": "time",
                        "year": 1862,
                        "month": 2,
                        "day": 15,
                    },
                    "source": {"artifact": ARTIFACT},
                }
            ],
            "entities": [{"id": "e01", "label": SEBASTIAN, "kind": "human"}],
        }
    )


def _statements(wiki: FakeWiki, qid: str, key: str) -> list:
    return wiki.wbi.item.get(qid).claims.get(PROPERTIES[key])


def _by_value(statements) -> dict:
    """Type statements by what they say: an item QID or a hint's text."""
    by_value = {}
    for statement in statements:
        value = statement.mainsnak.datavalue["value"]
        by_value[value["id"] if isinstance(value, dict) else value] = statement
    return by_value


def _sources(statement) -> list[tuple[list[str], list[str]]]:
    """Each reference block's (stated-in items, extraction-run items)."""
    return [
        (
            [
                snak.datavalue["value"]["id"]
                for snak in block.snaks.get(PROPERTIES["stated_in"])
            ],
            [
                snak.datavalue["value"]["id"]
                for snak in block.snaks.get(
                    PROPERTIES["inferred_from_extraction_run"]
                )
            ],
        )
        for block in statement.references.references
    ]


def _snapshot(wiki: FakeWiki, qid: str) -> str:
    """The item's statements, with their references and ranks."""
    return json.dumps(
        wiki.wbi.item.get(qid).claims.get_json(), sort_keys=True, default=str
    )


class TestWriterLaterSourceTypes:
    """A later batch that names an existing item records the kind and type
    hint it gives the item: a matching statement gains the batch's source
    and run, a different value becomes a statement of its own, and a
    curated statement is never altered (rules 1-3, ADR-0002). The batch
    gives them in its entity manifest or, without one (#240), gives the
    kind on the local refs the writer matches to the item by label."""

    @pytest.mark.parametrize(
        "directory_batch",
        DIRECTORY_BATCHES.values(),
        ids=DIRECTORY_BATCHES.keys(),
    )
    def test_same_kind_from_a_second_source_gains_its_references(
        self, directory_batch
    ):
        wiki = _two_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]
        # a dry run plans; it writes nothing
        writes = wiki.wbi.item.writes
        BatchWriter(wiki, dry_run=True).write_batch(
            directory_batch("unclassified_entity")
        )
        assert wiki.wbi.item.writes == writes

        directory = BatchWriter(wiki).write_batch(
            directory_batch("unclassified_entity")
        )
        assert directory.created_items["firm"] == firm
        (kind,) = _statements(wiki, firm, "instance_of")
        assert _sources(kind) == [
            (["Q25"], []),
            ([], [interview.run_qid]),
            (["Q26"], []),
            ([], [directory.run_qid]),
        ]
        assert kind.rank.value == "normal"
        assert directory.type_statements_referenced == 1
        assert directory.type_statements_written == 0

    @pytest.mark.parametrize(
        "directory_batch",
        DIRECTORY_BATCHES.values(),
        ids=DIRECTORY_BATCHES.keys(),
    )
    def test_different_kind_is_a_statement_of_its_own(self, directory_batch):
        wiki = _two_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]
        directory = BatchWriter(wiki).write_batch(directory_batch())
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert set(kinds) == {
            ITEMS["unclassified_entity"],
            ITEMS["organization"],
        }
        # the minted kind is as it was; the directory's cites the
        # directory and its run, and both stand at normal rank
        assert _sources(kinds[ITEMS["unclassified_entity"]]) == [
            (["Q25"], []),
            ([], [interview.run_qid]),
        ]
        assert _sources(kinds[ITEMS["organization"]]) == [
            (["Q26"], []),
            ([], [directory.run_qid]),
        ]
        assert {k.rank.value for k in kinds.values()} == {"normal"}
        (hint,) = _statements(wiki, firm, "type_hint")
        assert _cited(hint, "stated_in") == ["Q25"]
        assert directory.type_statements_written == 1
        assert directory.type_statements_referenced == 0

    @pytest.mark.parametrize(
        ("hint", "same"),
        [
            # the interview's hint, in another case and spacing
            (" Hat  Shop", True),
            # the directory's own word for the firm
            ("Hutmacherei", False),
        ],
    )
    def test_type_hint_same_or_different(self, hint, same):
        wiki = _two_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]
        directory = BatchWriter(wiki).write_batch(
            _directory_batch("unclassified_entity", type_hint=hint)
        )
        hints = _by_value(_statements(wiki, firm, "type_hint"))
        interview_sources = [(["Q25"], []), ([], [interview.run_qid])]
        directory_sources = [(["Q26"], []), ([], [directory.run_qid])]
        if same:
            # one statement, in the words of the source that gave it
            # first, now cited by both
            assert list(hints) == ["hat shop"]
            assert _sources(hints["hat shop"]) == (
                interview_sources + directory_sources
            )
        else:
            assert set(hints) == {"hat shop", "Hutmacherei"}
            assert _sources(hints["hat shop"]) == interview_sources
            assert _sources(hints["Hutmacherei"]) == directory_sources
        # the kind is the same either way
        assert directory.type_statements_referenced == (2 if same else 1)
        assert directory.type_statements_written == (0 if same else 1)

    def test_type_hints_compare_as_free_text(self):
        from yizkr.pipeline.writer import normalized_type_hint

        assert normalized_type_hint(" Hat  SHOP\n") == "hat shop"
        # a compatibility form (a ligature) reads as its letters
        assert normalized_type_hint("ﬀ") == normalized_type_hint("ff")
        assert normalized_type_hint("Hutmacherei") != "hat shop"

    @pytest.mark.parametrize(
        "directory_batch",
        [
            lambda: _directory_batch(type_hint="Hutmacherei"),
            _directory_lines_batch,
        ],
        ids=DIRECTORY_BATCHES.keys(),
    )
    def test_rewriting_a_later_batch_adds_nothing(self, directory_batch):
        wiki = _two_sources()
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        batch = directory_batch()
        BatchWriter(wiki).write_batch(batch)
        assert set(_by_value(_statements(wiki, firm, "instance_of"))) == {
            ITEMS["unclassified_entity"],
            ITEMS["organization"],
        }
        before = _snapshot(wiki, firm)
        for again in (
            BatchWriter(wiki).write_batch(batch),
            BatchWriter(wiki).write_batch(_firm_batch()),
        ):
            assert again.created_items["firm"] == firm
            assert (again.written, again.referenced, again.skipped) == (
                0,
                0,
                1,
            )
            assert again.type_statements_written == 0
            assert again.type_statements_referenced == 0
        assert _snapshot(wiki, firm) == before

    @pytest.mark.parametrize(
        "directory_batch",
        DIRECTORY_BATCHES.values(),
        ids=DIRECTORY_BATCHES.keys(),
    )
    @pytest.mark.parametrize("curation", ["human write", "deprecated"])
    def test_curated_statement_is_left_untouched(
        self, curation, directory_batch
    ):
        from wikibaseintegrator.datatypes import Item
        from wikibaseintegrator.models import Reference, References
        from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

        wiki = _two_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm_qid = interview.created_items["firm"]
        firm = wiki.wbi.item.get(firm_qid)
        if curation == "human write":
            # a curator records, from the interview, that the firm is an
            # organization: a statement with no extraction-run reference
            kind = "organization"
            curated = Item(
                prop_nr=PROPERTIES["instance_of"], value=ITEMS[kind]
            )
            source = Reference()
            source.add(Item(prop_nr=PROPERTIES["stated_in"], value="Q25"))
            curated.references = References().add(source)
            firm.claims.add(
                curated, action_if_exists=ActionIfExists.FORCE_APPEND
            )
        else:
            # a curator deprecates the interview's kind
            kind = "unclassified_entity"
            (curated,) = firm.claims.get(PROPERTIES["instance_of"])
            curated.rank = WikibaseRank.DEPRECATED
        firm.write()
        curated_before = json.dumps(curated.get_json(), sort_keys=True)

        directory = BatchWriter(wiki).write_batch(directory_batch(kind))
        same_value = [
            statement
            for statement in _statements(wiki, firm_qid, "instance_of")
            if statement.mainsnak.datavalue["value"]["id"] == ITEMS[kind]
        ]
        assert len(same_value) == 2
        assert curated in same_value
        assert json.dumps(curated.get_json(), sort_keys=True) == (
            curated_before
        )
        (new,) = [s for s in same_value if s is not curated]
        assert new.rank == WikibaseRank.NORMAL
        assert _sources(new) == [(["Q26"], []), ([], [directory.run_qid])]
        assert directory.type_statements_written == 1
        assert directory.type_statements_referenced == 0

    def test_resolved_reference_is_matched_by_label(self):
        # the resolve step replaced the firm's local refs with its QID
        # (an exact label match), leaving its manifest record unmarked
        wiki = _two_sources()
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        resolved = resolve_batch(_directory_batch(), {FIRM_LABEL: firm})
        assert isinstance(resolved.claims[0].subject, QidRef)
        assert resolved.entities[0].qid is None
        directory = BatchWriter(wiki).write_batch(resolved)
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert _sources(kinds[ITEMS["organization"]]) == [
            (["Q26"], []),
            ([], [directory.run_qid]),
        ]

    def test_approved_equivalence_records_its_kind(self):
        from yizkr.pipeline.reconcile import apply_equivalences

        # the directory's batch mints the firm; the interview's name for
        # it, "the Blue Hat", matches no label, and a reviewer approves
        # the equivalence
        wiki = _two_sources()
        directory = BatchWriter(wiki).write_batch(_directory_batch())
        firm = directory.created_items["firm"]
        approved = apply_equivalences(
            _firm_batch(label="the Blue Hat"), {"firm": firm}
        )
        assert isinstance(approved.claims[0].subject, QidRef)
        interview = BatchWriter(wiki).write_batch(approved)
        interview_sources = [(["Q25"], []), ([], [interview.run_qid])]
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert set(kinds) == {
            ITEMS["organization"],
            ITEMS["unclassified_entity"],
        }
        assert _sources(kinds[ITEMS["unclassified_entity"]]) == (
            interview_sources
        )
        (hint,) = _statements(wiki, firm, "type_hint")
        assert hint.mainsnak.datavalue["value"] == "hat shop"
        assert _sources(hint) == interview_sources

    def test_gedcom_export_witnesses_an_existing_persons_kind(self):
        from yizkr.pipeline.gedcom import adapt_gedcom

        # Sebastian Löbig as the 1905 marriage certificate gives him, then
        # as the family's GEDCOM export does: its adapter's local refs
        # carry no label, so the writer finds him by his manifest label
        text = (
            "0 HEAD\n1 CHAR UTF-8\n0 @I3@ INDI\n1 NAME Sebastian /Löbig/\n"
            "1 BIRT\n2 DATE 15 FEB 1862\n0 TRLR\n"
        )
        # the export cites itself by the SHA-1 of its bytes, which the
        # instance holds as the item Q27's media SHA-1
        exported = ArtifactRef.from_bytes(text.encode())
        wiki = FakeWiki({MEDIA_SHA1: "Q23", exported.sha1: "Q27"})
        first = BatchWriter(wiki).write_batch(_certificate_batch())
        person = first.created_items["e01"]
        export = adapt_gedcom(text, artifact=exported).batch
        gedcom = BatchWriter(wiki).write_batch(export)
        assert gedcom.created_items["i3"] == person
        assert gedcom.referenced == 1  # the agreeing birth date
        (kind,) = _statements(wiki, person, "instance_of")
        assert _sources(kind) == [
            (["Q23"], []),
            ([], [first.run_qid]),
            (["Q27"], []),
            ([], [gedcom.run_qid]),
        ]

    def test_explicit_kind_claim_keeps_its_excerpt(self):
        # the directory's batch also claims the kind outright, citing its
        # entry: the claim is written first, with its excerpt, and the
        # manifest's same kind adds nothing beside it
        wiki = _two_sources()
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        claim = {
            "property": "instance_of",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "organization"},
            },
            "source": {
                "artifact": {"kind": "artifact", "sha1": DIRECTORY_SHA1},
                "transcription_excerpt": {
                    "text": FIRM_ENTRY,
                    "language": "de",
                },
            },
        }
        directory = BatchWriter(wiki).write_batch(
            _directory_batch(extra_claims=(claim,))
        )
        assert directory.written == 2
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        organization = kinds[ITEMS["organization"]]
        assert _sources(organization) == [
            (["Q26"], []),
            ([], [directory.run_qid]),
        ]
        (excerpt,) = organization.references.references[0].snaks.get(
            PROPERTIES["transcription_excerpt"]
        )
        assert excerpt.datavalue["value"]["text"] == FIRM_ENTRY
        assert directory.type_statements_written == 0
        assert directory.type_statements_referenced == 0

    def test_a_reference_block_already_there_is_not_repeated(self):
        # the batch cites two pages of the directory: the firm's first
        # mention is on one, its explicit kind claim on the other, so the
        # kind statement already cites this batch's run
        wiki = FakeWiki(
            {
                INTERVIEW_SHA1: "Q25",
                DIRECTORY_SHA1: "Q26",
                DIRECTORY_PAGE_2_SHA1: "Q28",
            }
        )
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        claim = {
            "property": "instance_of",
            "value": {
                "type": "item",
                "value": {"kind": "vocab", "key": "organization"},
            },
            "source": {
                "artifact": {"kind": "artifact", "sha1": DIRECTORY_PAGE_2_SHA1}
            },
        }
        directory = BatchWriter(wiki).write_batch(
            _directory_batch(extra_claims=(claim,))
        )
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert _sources(kinds[ITEMS["organization"]]) == [
            (["Q28"], []),
            ([], [directory.run_qid]),
            (["Q26"], []),
        ]
        assert directory.type_statements_referenced == 1

    def test_a_qualified_statement_is_no_match(self):
        # an earlier batch took "gegr. 1863" on the directory's second page
        # for the firm being an organization since 1863: a machine
        # statement at normal rank, but qualified, so a different assertion
        # from the bare kind the directory's manifest gives (ADR-0002)
        wiki = FakeWiki(
            {
                INTERVIEW_SHA1: "Q25",
                DIRECTORY_SHA1: "Q26",
                DIRECTORY_PAGE_2_SHA1: "Q28",
            }
        )
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        since_1863 = ClaimBatch.model_validate(
            {
                "extraction_run": dict(
                    DIRECTORY_RUN, started_at="2026-07-13T10:00:00Z"
                ),
                "claims": [
                    {
                        # a ref that gives no kind of its own
                        "subject": {
                            "kind": "local",
                            "local_id": "firm",
                            "label": FIRM_LABEL,
                        },
                        "property": "instance_of",
                        "value": {
                            "type": "item",
                            "value": {"kind": "vocab", "key": "organization"},
                        },
                        "qualifiers": [
                            {
                                "property": "start_time",
                                "value": {"type": "time", "year": 1863},
                            }
                        ],
                        "source": {
                            "artifact": {
                                "kind": "artifact",
                                "sha1": DIRECTORY_PAGE_2_SHA1,
                            },
                            "transcription_excerpt": {
                                "text": "gegr. 1863",
                                "language": "de",
                            },
                        },
                    }
                ],
            }
        )
        page_2 = BatchWriter(wiki).write_batch(since_1863)
        assert (page_2.written, page_2.type_statements_written) == (1, 0)
        (qualified,) = [
            statement
            for statement in _statements(wiki, firm, "instance_of")
            if statement.qualifiers.qualifiers
        ]
        qualified_before = json.dumps(qualified.get_json(), sort_keys=True)

        directory = BatchWriter(wiki).write_batch(_directory_batch())

        organization = [
            statement
            for statement in _statements(wiki, firm, "instance_of")
            if statement.mainsnak.datavalue["value"]["id"]
            == ITEMS["organization"]
        ]
        assert len(organization) == 2
        assert json.dumps(qualified.get_json(), sort_keys=True) == (
            qualified_before
        )
        (bare,) = [s for s in organization if not s.qualifiers.qualifiers]
        assert _sources(bare) == [(["Q26"], []), ([], [directory.run_qid])]
        assert directory.type_statements_written == 1
        assert directory.type_statements_referenced == 0

    def test_an_equivalence_no_claim_mentions_records_nothing(self):
        # the reviewer approved the directory's firm as the interview's
        # item, then rejected the firm's founding year: the claim left is
        # the widow's address, so no claim cites a source that the firm's
        # kind could be recorded from (rule 1)
        from yizkr.pipeline.reconcile import apply_equivalences

        wiki = _two_sources()
        firm = (
            BatchWriter(wiki).write_batch(_firm_batch()).created_items["firm"]
        )
        before = _snapshot(wiki, firm)
        widow = {"id": "widow", "label": "Ursula Löbig", "kind": "human"}
        address = {"id": "address", "label": "Angerring 57", "kind": "place"}

        def ref(entity: dict) -> dict:
            return {
                "kind": "local",
                "local_id": entity["id"],
                "label": entity["label"],
                "instance_of": entity["kind"],
            }

        lives_at = {
            "subject": ref(widow),
            "property": "residence",
            "value": {"type": "item", "value": ref(address)},
            "qualifiers": [
                {
                    "property": "point_in_time",
                    "value": {"type": "time", "year": 1912},
                }
            ],
            "source": {
                "artifact": {"kind": "artifact", "sha1": DIRECTORY_SHA1},
                "transcription_excerpt": {
                    "text": "Löbig, Ursula, Wwe., Angerring 57",
                    "language": "de",
                },
            },
        }
        reviewed = ClaimBatch.model_validate(
            {
                "extraction_run": DIRECTORY_RUN,
                "claims": [lives_at],
                "entities": [
                    {
                        "id": "firm",
                        "label": FIRM_LABEL,
                        "kind": "organization",
                    },
                    widow,
                    address,
                ],
            }
        )
        approved = apply_equivalences(reviewed, {"firm": firm})
        assert approved.entities[0].qid == firm

        directory = BatchWriter(wiki).write_batch(approved)

        assert directory.written == 1
        assert _snapshot(wiki, firm) == before
        assert directory.type_statements_written == 0
        assert directory.type_statements_referenced == 0


# the directory's pages, each scanned as an artifact of its own
DIRECTORY_PAGE_3_SHA1 = hashlib.sha1(
    "fictional scan: the 1912 Flörburg address book, page 3".encode()
).hexdigest()
DIRECTORY_PAGES = (
    DIRECTORY_SHA1,
    DIRECTORY_PAGE_2_SHA1,
    DIRECTORY_PAGE_3_SHA1,
)


def _directory_pages_batch(*page_kinds: str | None) -> ClaimBatch:
    """A claims file with no entity manifest that cites the directory's
    pages, one claim per page, each page an artifact of its own. Page 1
    gives the firm's founding year, each later page the firm's name as
    written. Each page's claim names the firm by a local id of its own,
    with the kind that page gives (None: none)."""

    def firm(local_id: str, kind: str | None) -> dict:
        ref = {"kind": "local", "local_id": local_id, "label": FIRM_LABEL}
        if kind:
            ref["instance_of"] = kind
        return ref

    def source(sha1: str, excerpt: str) -> dict:
        return {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        }

    founded = {
        "property": "start_time",
        "value": {"type": "time", "year": 1863},
        "source": source(DIRECTORY_SHA1, "gegr. 1863"),
    }
    named = [
        {
            "property": "referred_to_as",
            "value": {
                "type": "monolingual",
                "value": {"text": FIRM_LABEL, "language": "de"},
            },
            "source": source(sha1, FIRM_LABEL),
        }
        for sha1 in DIRECTORY_PAGES[1 : len(page_kinds)]
    ]
    return ClaimBatch.model_validate(
        {
            "extraction_run": DIRECTORY_RUN,
            "claims": [
                dict(claim, subject=firm(f"c{page:03d}:firm", kind))
                for page, (claim, kind) in enumerate(
                    zip([founded, *named], page_kinds, strict=True), 1
                )
            ],
        }
    )


def _three_sources() -> FakeWiki:
    return FakeWiki(
        {
            INTERVIEW_SHA1: "Q25",
            DIRECTORY_SHA1: "Q26",
            DIRECTORY_PAGE_2_SHA1: "Q28",
        }
    )


class TestWriterRefKindWitnesses:
    """A local ref without a manifest record gives its item's kind on the
    artifact of the claim the ref is in (#240 review): the kind cites that
    artifact, not the first one that mentions the item, and every artifact
    whose ref gives the kind is a witness of it (rule 1)."""

    def test_a_refs_kind_cites_its_own_claims_artifact(self):
        # page 1's claim names the firm without a kind; page 2's types it
        wiki = _three_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]

        directory = BatchWriter(wiki).write_batch(
            _directory_pages_batch(None, "organization")
        )

        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert _sources(kinds[ITEMS["organization"]]) == [
            (["Q28"], []),
            ([], [directory.run_qid]),
        ]
        assert _sources(kinds[ITEMS["unclassified_entity"]]) == [
            (["Q25"], []),
            ([], [interview.run_qid]),
        ]
        assert directory.type_statements_written == 1
        assert directory.type_statements_referenced == 0

    def test_every_page_that_gives_the_kind_witnesses_it(self):
        # both pages' claims give the kind the interview gave
        wiki = _three_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]
        batch = _directory_pages_batch(
            "unclassified_entity", "unclassified_entity"
        )

        directory = BatchWriter(wiki).write_batch(batch)

        (kind,) = _statements(wiki, firm, "instance_of")
        assert _sources(kind) == [
            (["Q25"], []),
            ([], [interview.run_qid]),
            (["Q26"], []),
            ([], [directory.run_qid]),
            (["Q28"], []),
        ]
        assert directory.type_statements_written == 0
        assert directory.type_statements_referenced == 2
        # and a re-run finds both witnesses there
        before = _snapshot(wiki, firm)
        again = BatchWriter(wiki).write_batch(batch)
        assert again.type_statements_referenced == 0
        assert _snapshot(wiki, firm) == before

    # the directory typing the firm by the interview's word for it, which
    # is no kind in the vocabulary: on a local ref of a claims file, or in
    # its entity manifest -> (the batch, the firm's local id, claims)
    UNKNOWN_KIND = {
        "ref": (
            lambda: _directory_pages_batch("hat_shop", None),
            "c001:firm",
            2,
        ),
        "record": (lambda: _directory_batch("hat_shop"), "firm", 1),
    }

    @pytest.mark.parametrize(
        ("given", "local_id", "claims"),
        UNKNOWN_KIND.values(),
        ids=UNKNOWN_KIND.keys(),
    )
    @pytest.mark.parametrize("found", [True, False], ids=["found", "minted"])
    def test_a_kind_the_vocabulary_lacks_is_skipped_with_a_warning(
        self, found, given, local_id, claims
    ):
        wiki = _three_sources()

        def kinds(qid: str) -> list[str]:
            return [
                json.dumps(statement.get_json(), sort_keys=True)
                for statement in _statements(wiki, qid, "instance_of") or []
            ]

        kinds_before = []
        if found:
            firm = (
                BatchWriter(wiki)
                .write_batch(_firm_batch())
                .created_items["firm"]
            )
            kinds_before = kinds(firm)
        batch = given()
        warning = (
            f"entity {local_id!r} has the kind 'hat_shop', which the "
            "vocabulary lacks: no kind is recorded for it"
        )
        # found before the first write, so a dry run reports it too
        writes = wiki.wbi.item.writes
        planned = BatchWriter(wiki, dry_run=True).write_batch(batch)
        assert planned.warnings == [warning]
        assert wiki.wbi.item.writes == writes

        directory = BatchWriter(wiki).write_batch(batch)

        assert directory.warnings == [warning]
        assert directory.written == claims
        assert kinds(directory.created_items[local_id]) == kinds_before
        assert directory.type_statements_written == 0

    # a claims file written on an empty instance: page 1's ref mints the
    # firm, and each later page's ref, under a local id of its own, finds
    # it by label -> (the pages' kinds, the artifacts each of the firm's
    # kinds cites after the first write, its type statements written and
    # referenced)
    MINTED = {
        "same kind": (
            ("organization", "organization"),
            {"organization": ["Q26", "Q28"]},
            (0, 1),
        ),
        "kinds differ": (
            ("organization", "unclassified_entity"),
            {"organization": ["Q26"], "unclassified_entity": ["Q28"]},
            (1, 0),
        ),
        "no kind on page 1": (
            (None, "organization"),
            {"organization": ["Q28"]},
            (1, 0),
        ),
        "three pages": (
            ("organization", "unclassified_entity", "organization"),
            {"organization": ["Q26", "Q30"], "unclassified_entity": ["Q28"]},
            (1, 1),
        ),
    }

    @pytest.mark.parametrize(
        ("page_kinds", "cited", "counts"),
        MINTED.values(),
        ids=MINTED.keys(),
    )
    def test_a_minted_items_later_refs_witness_it_on_the_first_write(
        self, page_kinds, cited, counts
    ):
        wiki = FakeWiki(
            dict(zip(DIRECTORY_PAGES, ("Q26", "Q28", "Q30"), strict=True))
        )
        batch = _directory_pages_batch(*page_kinds)

        first = BatchWriter(wiki).write_batch(batch)

        (firm,) = set(first.created_items.values())
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert {
            value: _cited(statement, "stated_in")
            for value, statement in kinds.items()
        } == {ITEMS[kind]: artifacts for kind, artifacts in cited.items()}
        for statement in kinds.values():
            assert _cited(statement, "inferred_from_extraction_run") == [
                first.run_qid
            ]
        assert (
            first.type_statements_written,
            first.type_statements_referenced,
        ) == counts
        # so a second write of the batch finds every witness there
        before = _snapshot(wiki, firm)
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced) == (0, 0)
        assert again.type_statements_written == 0
        assert again.type_statements_referenced == 0
        assert wiki.wbi.item.writes == writes
        assert _snapshot(wiki, firm) == before

    def test_a_refs_kind_gives_way_to_its_record(self):
        # a claims file with a manifest names the firm by its QID on page
        # 1 and by a local id on page 2, whose ref says human and whose
        # record says organization: the record's kind is recorded, citing
        # the page that first mentions the firm, and the ref gives none
        wiki = _three_sources()
        interview = BatchWriter(wiki).write_batch(_firm_batch())
        firm = interview.created_items["firm"]
        given = _directory_pages_batch(None, "human").model_dump(mode="json")
        given["claims"][0]["subject"] = {"kind": "qid", "qid": firm}
        given["entities"] = [
            {"id": "c002:firm", "label": FIRM_LABEL, "kind": "organization"}
        ]

        directory = BatchWriter(wiki).write_batch(
            ClaimBatch.model_validate(given)
        )

        assert directory.created_items == {"c002:firm": firm}
        kinds = _by_value(_statements(wiki, firm, "instance_of"))
        assert set(kinds) == {
            ITEMS["unclassified_entity"],
            ITEMS["organization"],
        }
        assert _sources(kinds[ITEMS["organization"]]) == [
            (["Q26"], []),
            ([], [directory.run_qid]),
        ]
        assert directory.type_statements_written == 1
        assert directory.type_statements_referenced == 0

    # a local id whose manifest record and ref give different kinds, one
    # of them a kind the vocabulary lacks -> the kind warned of, if any
    RECORD_AND_REF = {
        "record known": ("organization", "hat_shop", None),
        "record unknown": ("hat_shop", "organization", "hat_shop"),
    }

    @pytest.mark.parametrize(
        ("record_kind", "ref_kind", "warned"),
        RECORD_AND_REF.values(),
        ids=RECORD_AND_REF.keys(),
    )
    def test_a_records_kind_is_the_one_checked(
        self, record_kind, ref_kind, warned
    ):
        given = _directory_batch(record_kind).model_dump(mode="json")
        given["claims"][0]["subject"]["instance_of"] = ref_kind

        planned = BatchWriter(_two_sources(), dry_run=True).write_batch(
            ClaimBatch.model_validate(given)
        )

        assert planned.warnings == (
            [
                f"entity 'firm' has the kind {warned!r}, which the "
                "vocabulary lacks: no kind is recorded for it"
            ]
            if warned
            else []
        )

    @pytest.mark.parametrize("position", ["value", "qualifier"])
    def test_an_unknown_kind_is_found_in_every_ref_position(self, position):
        # the directory places the firm in Flörburg, typed by a kind the
        # vocabulary lacks: as the value of the firm's location, or as the
        # location qualifying its founding
        town = {
            "kind": "local",
            "local_id": "floerburg",
            "label": "Flörburg",
            "instance_of": "town",
        }
        given = _directory_batch().model_dump(mode="json")
        founded = given["claims"][0]
        located = {"type": "item", "value": town}
        if position == "value":
            given["claims"].append(
                dict(founded, property="location", value=located)
            )
        else:
            founded["qualifiers"] = [
                {"property": "location", "value": located}
            ]

        planned = BatchWriter(_two_sources(), dry_run=True).write_batch(
            ClaimBatch.model_validate(given)
        )

        assert planned.warnings == [
            "entity 'floerburg' has the kind 'town', which the vocabulary "
            "lacks: no kind is recorded for it"
        ]


# -- writer: a document's aliases reach the item however it was found (#240)

# The 1984 family tree names Sebastian Löbig by his diminutive too:
# "Sebastian 'Wastl' 1862–1945", "Children of Wastl and Hilde".
TREE_SHA1 = hashlib.sha1(b"fictional scan: the 1984 family tree").hexdigest()
TREE_RUN = dict(RUN, started_at="2026-07-15T09:00:00Z")


def _tree_batch() -> ClaimBatch:
    """The family tree's batch: Sebastian's birth year, and in its entity
    manifest the tree's other name for him."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": TREE_RUN,
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e03",
                        "label": SEBASTIAN,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1862},
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": TREE_SHA1},
                        "transcription_excerpt": {
                            "text": "Sebastian 'Wastl' 1862–1945",
                            "language": "en",
                        },
                    },
                }
            ],
            "entities": [
                {
                    "id": "e03",
                    "label": SEBASTIAN,
                    "kind": "human",
                    "aliases": ["Wastl"],
                }
            ],
        }
    )


def _aliases(wiki: FakeWiki, qid: str) -> list[str]:
    aliases = wiki.wbi.item.get(qid).aliases.get("en") or []
    return [str(alias) for alias in aliases]


def _approved(batch: ClaimBatch, approved: dict[str, str]) -> ClaimBatch:
    """`batch` after a reviewer approved each local id's equivalence to
    the item `approved` gives it (ADR-0007)."""
    from yizkr.pipeline.reconcile import apply_equivalences

    return apply_equivalences(batch, approved)


class TestWriterAliases:
    """Every document adds its names for an entity to the item's aliases,
    append-only (ADR-0007), whichever step found the item: the writer by
    exact label, the resolve step by exact label (#240), or a reviewer's
    approved equivalence. The instance here serves every read as a copy
    (DetachedItems), so an edit sent from a read taken before the aliases
    went on would take them off again."""

    # the tree's batch as the writer receives it, given Sebastian's QID
    FOUND_BY = {
        "writer": lambda batch, qid: batch,
        "resolve step": lambda batch, qid: resolve_batch(
            batch, {SEBASTIAN: qid}
        ),
        "equivalence": lambda batch, qid: _approved(batch, {"e03": qid}),
    }

    @pytest.mark.parametrize(
        "one_writer", [False, True], ids=["writer each", "one writer"]
    )
    @pytest.mark.parametrize(
        "found_by", FOUND_BY.values(), ids=FOUND_BY.keys()
    )
    def test_a_later_documents_aliases_reach_the_item(
        self, found_by, one_writer
    ):
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        writer = BatchWriter(wiki)
        certificate = writer.write_batch(_certificate_batch())
        sebastian = certificate.created_items["e01"]
        assert _aliases(wiki, sebastian) == []
        tree_batch = found_by(_tree_batch(), sebastian)

        tree = (writer if one_writer else BatchWriter(wiki)).write_batch(
            tree_batch
        )

        assert _aliases(wiki, sebastian) == ["Wastl"]
        # the tree's birth year and its witness of the kind, written after
        # the aliases went on, left them on
        assert tree.written == 1
        (kind,) = _statements(wiki, sebastian, "instance_of")
        assert _sources(kind) == [
            (["Q23"], []),
            ([], [certificate.run_qid]),
            (["Q29"], []),
            ([], [tree.run_qid]),
        ]
        # a re-run edits nothing, and the item's label is never an alias
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(tree_batch)
        assert wiki.wbi.item.writes == writes
        assert _aliases(wiki, sebastian) == ["Wastl"]

    def test_aliases_another_writer_put_on_survive_a_reused_writers_edit(
        self,
    ):
        # one writer wrote the certificate and holds its copy of
        # Sebastian's item; another wrote the tree's birth year and put
        # "Wastl" on it. The first writer's next batch, the tree's marriage
        # line, finds the alias already there, and edits from that read
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        writer = BatchWriter(wiki)
        sebastian = writer.write_batch(_certificate_batch()).created_items[
            "e01"
        ]
        BatchWriter(wiki).write_batch(_tree_batch())
        assert _aliases(wiki, sebastian) == ["Wastl"]
        tree = _tree_batch()
        hildegard = {
            "id": "e04",
            "label": "Hildegard Laudler",
            "kind": "human",
        }
        married = tree.model_copy(
            update={
                "claims": [
                    tree.claims[0].model_copy(
                        update={
                            "property": "spouse",
                            "value": ItemValue(
                                value=LocalRef(
                                    local_id="e04",
                                    label=hildegard["label"],
                                    instance_of="human",
                                )
                            ),
                            "source": tree.claims[0].source.model_copy(
                                update={
                                    "transcription_excerpt": MonolingualText(
                                        text="1862–1945 = Hildegard 'Hilde' "
                                        "Laudler",
                                        language="en",
                                    )
                                }
                            ),
                        }
                    )
                ],
                "entities": [
                    *tree.entities,
                    EntityRecord.model_validate(hildegard),
                ],
            }
        )

        writer.write_batch(married)

        assert _aliases(wiki, sebastian) == ["Wastl"]
        assert len(_statements(wiki, sebastian, "spouse")) == 1
        # the other writer's birth year is there too
        assert len(_statements(wiki, sebastian, "date_of_birth")) == 2


# -- writer: each place an artifact asserts a value keeps its block (#335) --

# (Here) the 1905 marriage certificate gives Sebastian Löbig's birth twice:
# in the groom's particulars, and again in the registrar's margin note.
GROOM = "geboren am 15. Februar 1862 zu Flörburg"
MARGIN = "geb. 15.2.1862"
LATER_RUN = dict(RUN, started_at="2026-07-20T09:00:00Z")


def _excerpt(text: str) -> dict:
    return {"text": text, "language": "de"}


# two places in the certificate, by what sets them apart -> the first
# claim's locators and the second's
PLACES = {
    "another page": (
        {"page_number": "1", "transcription_excerpt": _excerpt(GROOM)},
        {"page_number": "2", "transcription_excerpt": _excerpt(GROOM)},
    ),
    "another excerpt, no page": (
        {"transcription_excerpt": _excerpt(GROOM)},
        {"transcription_excerpt": _excerpt(MARGIN)},
    ),
    "another region": (
        {"region": {"x": 10, "y": 20, "width": 60, "height": 10}},
        {"region": {"x": 75, "y": 5, "width": 20, "height": 40}},
    ),
}


def _births(
    *places: dict, run: dict = RUN, curated: bool = False
) -> ClaimBatch:
    """The certificate's batch (_certificate_batch) with Sebastian's birth
    claimed once in each of `places`, each claim citing the certificate
    with that place's locators."""
    batch = _certificate_batch().model_dump(mode="json")
    (claim,) = batch["claims"]
    batch["claims"] = [
        dict(claim, source={"artifact": ARTIFACT, **place}, curated=curated)
        for place in places
    ]
    return ClaimBatch.model_validate(dict(batch, extraction_run=run))


def _places(statement) -> list[tuple]:
    """Each source block's (artifact, page number, region, excerpt text),
    None for what the block does not give. Run blocks are left out."""

    def one(block, key: str):
        values = [
            snak.datavalue["value"]
            for snak in block.snaks.get(PROPERTIES[key])
        ]
        return values[0] if values else None

    places = []
    for block in statement.references.references:
        artifact = one(block, "stated_in")
        if artifact is None:
            continue
        excerpt = one(block, "transcription_excerpt")
        places.append(
            (
                artifact["id"],
                one(block, "page_number"),
                one(block, "relative_position_within_image"),
                excerpt["text"] if excerpt else None,
            )
        )
    return places


def _place(place: dict, artifact: str = "Q23") -> tuple:
    """`_places`' view of the block a claim with `place`'s locators cites
    `artifact` with."""
    region = place.get("region")
    return (
        artifact,
        place.get("page_number"),
        RegionPercent(**region).as_wikibase_string() if region else None,
        (place.get("transcription_excerpt") or {}).get("text"),
    )


class TestWriterPlaces:
    """One artifact can assert one value in several places: on two pages,
    in two passages or in two regions of a scan. The claims agree, so they
    are one statement, and each place is a reference block of its own on
    it: the statement keeps every locator and excerpt, beside the run's
    block (rules 1 and 2, ADR-0003). A re-run adds nothing, and no block
    is changed or removed (rule 3). The instance serves every read as a
    copy of what it stores, its text in NFC (DetachedItems), so the
    blocks a claim's block is compared with are in the stored form."""

    @pytest.mark.parametrize(
        ("first", "second"), PLACES.values(), ids=PLACES.keys()
    )
    def test_each_place_is_a_block_of_its_own(self, first, second):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        result = BatchWriter(wiki).write_batch(_births(first, second))

        sebastian = result.created_items["e01"]
        (born,) = _statements(wiki, sebastian, "date_of_birth")
        assert _places(born) == [_place(first), _place(second)]
        # both claims are the run's, so its block is there once
        assert _sources(born) == [
            (["Q23"], []),
            ([], [result.run_qid]),
            (["Q23"], []),
        ]
        assert (result.written, result.referenced, result.skipped) == (
            1,
            1,
            0,
        )

    def test_the_same_place_twice_is_one_block(self):
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        groom, _ = PLACES["another page"]

        result = BatchWriter(wiki).write_batch(_births(groom, groom))

        (born,) = _statements(
            wiki, result.created_items["e01"], "date_of_birth"
        )
        assert _places(born) == [_place(groom)]
        assert _sources(born) == [(["Q23"], []), ([], [result.run_qid])]
        assert (result.written, result.referenced, result.skipped) == (
            1,
            0,
            1,
        )

    # a place its extraction gives in decomposed form (NFD), which the
    # instance stores in NFC: (here) the margin note on the certificate's
    # back, named by its page, or the groom's particulars, quoted
    DECOMPOSED = {
        "page number": {
            "page_number": unicodedata.normalize("NFD", "Rückseite"),
            "transcription_excerpt": _excerpt(MARGIN),
        },
        "excerpt": {
            "transcription_excerpt": _excerpt(
                unicodedata.normalize("NFD", GROOM)
            ),
        },
    }

    @pytest.mark.parametrize(
        "place", DECOMPOSED.values(), ids=DECOMPOSED.keys()
    )
    def test_a_place_given_in_nfd_is_found_again(self, place):
        # the writer sends the place's text in NFC, the form the instance
        # stores and gives back
        stored = tuple(
            unicodedata.normalize("NFC", part) if part else part
            for part in _place(place)
        )
        assert stored != _place(place)
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        batch = _births(place, place)

        first = BatchWriter(wiki).write_batch(batch)

        sebastian = first.created_items["e01"]
        (born,) = _statements(wiki, sebastian, "date_of_birth")
        assert _places(born) == [stored]
        assert _sources(born) == [(["Q23"], []), ([], [first.run_qid])]
        assert (first.written, first.referenced, first.skipped) == (1, 0, 1)
        # and a re-run finds the place there
        before = _snapshot(wiki, sebastian)
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
        assert _snapshot(wiki, sebastian) == before

    @pytest.mark.parametrize(
        "run", [RUN, LATER_RUN], ids=["same run", "later run"]
    )
    def test_a_rewrite_adds_nothing(self, run):
        # every place written, then written again: by the same batch, or
        # by a later run's batch that finds the same places
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        places = [place for pair in PLACES.values() for place in pair]
        first = BatchWriter(wiki).write_batch(_births(*places))
        sebastian = first.created_items["e01"]
        (born,) = _statements(wiki, sebastian, "date_of_birth")
        assert _places(born) == [_place(place) for place in places]
        before = _snapshot(wiki, sebastian)

        again = BatchWriter(wiki).write_batch(_births(*places, run=run))

        assert (again.written, again.referenced, again.skipped) == (
            0,
            0,
            len(places),
        )
        # not even a later run's block is added where its places are
        assert _snapshot(wiki, sebastian) == before

    @pytest.mark.parametrize(
        "curated", [False, True], ids=["machine claim", "curated claim"]
    )
    def test_a_later_claim_adds_its_place_with_its_provenance(self, curated):
        # a later batch finds the margin note: a later run's extraction,
        # or a reviewer's correction (a curated claim, which carries no
        # run block)
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        groom, margin = PLACES["another excerpt, no page"]
        first = BatchWriter(wiki).write_batch(_births(groom))
        sebastian = first.created_items["e01"]
        (born,) = _statements(wiki, sebastian, "date_of_birth")
        groom_block = json.dumps(
            born.references.references[0].get_json(), sort_keys=True
        )

        later = BatchWriter(wiki).write_batch(
            _births(margin, run=LATER_RUN, curated=curated)
        )

        (born,) = _statements(wiki, sebastian, "date_of_birth")
        assert _places(born) == [_place(groom), _place(margin)]
        assert _sources(born) == [
            (["Q23"], []),
            ([], [first.run_qid]),
            (["Q23"], []),
            *([] if curated else [([], [later.run_qid])]),
        ]
        # the first place's block is as it was written
        assert (
            json.dumps(
                born.references.references[0].get_json(), sort_keys=True
            )
            == groom_block
        )
        assert (later.written, later.referenced, later.skipped) == (0, 1, 0)

    def test_another_artifact_of_the_same_run_adds_its_source_block(self):
        # (here) the directory gives the firm's founding year on its second
        # page too, a scan of its own: an agreeing second source of the
        # same run, whose block is not repeated
        wiki = FakeWiki(
            {DIRECTORY_SHA1: "Q26", DIRECTORY_PAGE_2_SHA1: "Q28"},
            detached=True,
        )
        batch = _directory_batch().model_dump(mode="json")
        (founded,) = batch["claims"]
        page_2 = {"kind": "artifact", "sha1": DIRECTORY_PAGE_2_SHA1}
        batch["claims"].append(
            dict(founded, source=dict(founded["source"], artifact=page_2))
        )

        directory = BatchWriter(wiki).write_batch(
            ClaimBatch.model_validate(batch)
        )

        (founding,) = _statements(
            wiki, directory.created_items["firm"], "start_time"
        )
        assert _sources(founding) == [
            (["Q26"], []),
            ([], [directory.run_qid]),
            (["Q28"], []),
        ]
        assert (directory.written, directory.referenced) == (1, 1)

    @pytest.mark.parametrize("curation", ["human write", "deprecated"])
    def test_a_curated_statement_citing_the_artifact_is_left_as_it_is(
        self, curation
    ):
        # a curated statement is compared by artifact alone: a claim whose
        # artifact it cites, in whatever place, adds nothing to it
        from wikibaseintegrator.wbi_enums import WikibaseRank

        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        groom, margin = PLACES["another page"]
        first = BatchWriter(wiki).write_batch(
            _births(groom, curated=curation == "human write")
        )
        sebastian = first.created_items["e01"]
        if curation == "deprecated":
            item = wiki.wbi.item.get(sebastian)
            (born,) = item.claims.get(PROPERTIES["date_of_birth"])
            born.rank = WikibaseRank.DEPRECATED
            item.write()
        before = _snapshot(wiki, sebastian)

        later = BatchWriter(wiki).write_batch(_births(margin, run=LATER_RUN))

        assert (later.written, later.referenced, later.skipped) == (0, 0, 1)
        assert _snapshot(wiki, sebastian) == before

    def test_a_gedcom_export_keeps_both_citations_of_one_birth(self):
        from yizkr.pipeline.gedcom import adapt_gedcom

        # (here) the family's export gives Gustav's birth, 7 January 1919,
        # twice: citing his birth register extract, and citing the tree.
        # The adapter keeps each citation on a claim of its own (#322)
        text = (
            "0 HEAD\n1 CHAR UTF-8\n"
            "0 @S1@ SOUR\n1 TITL Birth register extract for Gustav Löbig\n"
            "0 @S2@ SOUR\n1 TITL Family tree drawn by Clara Lobig\n"
            "0 @I8@ INDI\n1 NAME Gustav /Löbig/\n"
            "1 BIRT\n2 DATE 7 JAN 1919\n2 SOUR @S1@\n"
            "1 BIRT\n2 DATE 7 JAN 1919\n2 SOUR @S2@\n"
            "0 TRLR\n"
        )
        exported = ArtifactRef.from_bytes(text.encode())
        wiki = FakeWiki({exported.sha1: "Q27"}, detached=True)

        gedcom = BatchWriter(wiki).write_batch(
            adapt_gedcom(text, artifact=exported).batch
        )

        (born,) = _statements(
            wiki, gedcom.created_items["i8"], "date_of_birth"
        )
        assert [excerpt for *_, excerpt in _places(born)] == [
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S1@"
            " | cited: Birth register extract for Gustav Löbig",
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S2@"
            " | cited: Family tree drawn by Clara Lobig",
        ]
        assert _sources(born) == [
            (["Q27"], []),
            ([], [gedcom.run_qid]),
            (["Q27"], []),
        ]

    def test_an_explicit_kind_keeps_its_place_beside_the_minted_kind(self):
        # (here) the certificate's batch also claims outright that Sebastian
        # is a human, quoting the groom's particulars. The kind minted for
        # him cites the certificate in no place, so the claim's excerpt is
        # a block of its own beside it
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        given = _certificate_batch().model_dump(mode="json")
        (born,) = given["claims"]
        human = {"kind": "vocab", "key": "human"}
        given["claims"].append(
            dict(
                born,
                property="instance_of",
                value={"type": "item", "value": human},
                source={
                    "artifact": ARTIFACT,
                    "transcription_excerpt": _excerpt(
                        "der Hutmacher Sebastian Löbig"
                    ),
                },
            )
        )
        batch = ClaimBatch.model_validate(given)

        first = BatchWriter(wiki).write_batch(batch)

        sebastian = first.created_items["e01"]
        (kind,) = _statements(wiki, sebastian, "instance_of")
        assert _places(kind) == [
            ("Q23", None, None, None),
            ("Q23", None, None, "der Hutmacher Sebastian Löbig"),
        ]
        assert _sources(kind) == [
            (["Q23"], []),
            ([], [first.run_qid]),
            (["Q23"], []),
        ]
        assert (first.written, first.referenced, first.skipped) == (1, 1, 0)
        # and a re-run finds both places there
        before = _snapshot(wiki, sebastian)
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
        assert again.type_statements_referenced == 0
        assert _snapshot(wiki, sebastian) == before


# -- writer: the batch's text goes in NFC, as the instance keeps text (#349) -

# A source's text can reach a batch in the decomposed form (NFD), a letter
# and then its combining mark where NFC has the one precomposed letter: a
# GEDCOM export can give its names so, and the adapter keeps them as given.
POSTCARD_SHA1 = hashlib.sha1(
    b"fictional scan: the 1938 postcard to Klara"
).hexdigest()
CARD_SHA1 = hashlib.sha1(
    b"fictional scan: the 1945 registration card for Ida"
).hexdigest()
COMPENSATION_SHA1 = hashlib.sha1(
    b"fictional scan: the 1953 compensation file for Ida"
).hexdigest()
GAZETTEER_SHA1 = hashlib.sha1(
    "fictional export: a gazetteer's record of Flörburg".encode()
).hexdigest()
REGISTER_SHA1 = hashlib.sha1(
    b"fictional scan: the 1909 burial register"
).hexdigest()
TEXT_MEDIA = {
    POSTCARD_SHA1: "Q30",
    CARD_SHA1: "Q31",
    COMPENSATION_SHA1: "Q32",
    GAZETTEER_SHA1: "Q33",
    REGISTER_SHA1: "Q34",
}


def _cites(sha1: str, excerpt: str | None = None, language: str = "de"):
    """A claim's source: the artifact with media `sha1`, quoted."""
    source = {"artifact": {"kind": "artifact", "sha1": sha1}}
    if excerpt:
        source["transcription_excerpt"] = {
            "text": excerpt,
            "language": language,
        }
    return source


def _local(local_id: str, label: str, kind: str = "human") -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


# a claim whose own text the batch may give in NFD, by where that text is
TEXT_CLAIMS = {
    # the postcard's "Liebes Pünktchen!"
    "monolingual value": {
        "subject": _local("e02", "Klara Löbig"),
        "property": "nickname",
        "value": {
            "type": "monolingual",
            "value": {"text": "Pünktchen", "language": "de"},
        },
        "source": _cites(POSTCARD_SHA1, "Liebes Pünktchen!"),
    },
    # (here) the compensation file names Hildegard, a witness, as the late
    # Ida's sister-in-law: a kinship term, as the source words it
    "string qualifier": {
        "subject": _local("e06", "Hildegard Löbig"),
        "property": "relative",
        "value": {"type": "item", "value": _local("e05", "Ida Löbig")},
        "qualifiers": [
            {
                "property": "kinship_type",
                "value": {"type": "string", "value": "Schwägerin"},
            }
        ],
        "source": _cites(
            COMPENSATION_SHA1, "Hildegard Löbig, Schwägerin der Verstorbenen"
        ),
    },
    # (here) a gazetteer that keys the town by its name
    "external id": {
        "subject": _local("pl1", "Flörburg", "place"),
        "property": "whg_place_id",
        "value": {"type": "external-id", "value": "flörburg"},
        "source": _cites(GAZETTEER_SHA1),
    },
}
# the text each claim gives, in NFC
TEXT_OF = {
    "monolingual value": "Pünktchen",
    "string qualifier": "Schwägerin",
    "external id": "flörburg",
}


def _batch_of(*claims: dict, entities: tuple = (), run: dict = RUN):
    return ClaimBatch.model_validate(
        {
            "extraction_run": run,
            "claims": list(claims),
            "entities": list(entities),
        }
    )


def _in_form_batch(batch: ClaimBatch, form: str) -> ClaimBatch:
    """`batch` with every text in it in the normalization form `form`."""
    return ClaimBatch.model_validate(
        _in_form(batch.model_dump(mode="json"), form)
    )


def _texts(value) -> list[str]:
    """Every string in `value`, a JSON document."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _not_in_nfc(wiki: FakeWiki) -> list[str]:
    """Each text that a write to `wiki` sent in another form than NFC."""
    return [
        text
        for text in _texts(wiki.wbi.item.sent)
        if not unicodedata.is_normalized("NFC", text)
    ]


def _values(statement) -> list[str]:
    """The text of a statement's value and of its qualifiers' values."""
    values = [statement.mainsnak.datavalue["value"]]
    values.extend(
        snak.datavalue["value"]
        for snaks in statement.qualifiers.qualifiers.values()
        for snak in snaks
    )
    return [
        value["text"] if isinstance(value, dict) else value
        for value in values
        if isinstance(value, str) or "text" in value
    ]


def _labelled(wiki: FakeWiki, label: str) -> list[str]:
    """The items whose English label is `label`."""
    return [
        qid
        for qid, item in wiki.wbi.item.store.items()
        if str(item.labels.get("en") or "") == label
    ]


# the registration card's batch and, (here) a later one, the compensation
# file's: each names Ida by her married name, with her birth name as an
# alias (ADR-0011); the file adds her name as it writes it
IDA = {
    "id": "e05",
    "label": "Ida Löbig",
    "aliases": ["Ida Sörne"],
    "kind": "human",
}


def _card_batch() -> ClaimBatch:
    return _batch_of(
        {
            "subject": _local("e05", "Ida Löbig"),
            "property": "birth_name",
            "value": {
                "type": "monolingual",
                "value": {"text": "Ida Sörne", "language": "de"},
            },
            "source": _cites(CARD_SHA1, "LÖBIG, Ida, née SÖRNE", "en"),
        },
        entities=(IDA,),
    )


def _compensation_batch() -> ClaimBatch:
    return _batch_of(
        {
            "subject": _local("e05", "Ida Löbig"),
            "property": "date_of_birth",
            "value": {"type": "time", "year": 1881, "month": 4, "day": 17},
            "source": _cites(
                COMPENSATION_SHA1,
                "Ida Löbig geb. Sörne, geb. 17.4.1881",
            ),
        },
        entities=(dict(IDA, aliases=["Ida Sörne", "Ida Löbig geb. Sörne"]),),
        run=LATER_RUN,
    )


class TestWriterTextInNfc:
    """Wikibase keeps text in Unicode NFC: it saves string and external-id
    values, labels, descriptions and aliases in NFC, and its API gives every
    text back in NFC, monolingual text included (DetachedItems). The writer
    sends the batch's text in NFC too, so what it compares with the
    instance, and the labels it finds items by, are in the form the
    instance gives back: a batch whose text is in NFD writes nothing on a
    re-run, and finds everything a batch in NFC wrote. NFC is the only
    change: a combining mark with no precomposed letter stays, and so does
    a compatibility character."""

    def test_a_gedcom_export_in_nfd_is_written_once(self):
        from yizkr.pipeline.gedcom import adapt_gedcom

        # (here) the family's export gives Ida's names decomposed
        text = unicodedata.normalize(
            "NFD",
            "0 HEAD\n1 CHAR UTF-8\n"
            "0 @S1@ SOUR\n1 TITL Post-war registration card for Ida Löbig\n"
            "0 @I5@ INDI\n1 NAME Ida /Löbig/\n2 TYPE married\n"
            "1 NAME Ida /Sörne/\n2 TYPE birth\n2 NICK Peppi\n"
            "1 BIRT\n2 DATE 17 APR 1881\n2 PLAC Moshausen\n2 SOUR @S1@\n"
            "0 TRLR\n",
        )
        exported = ArtifactRef.from_bytes(text.encode())
        batch = adapt_gedcom(text, artifact=exported).batch
        assert _in_form_batch(batch, "NFC") != batch
        wiki = FakeWiki({exported.sha1: "Q27"}, detached=True)

        first = BatchWriter(wiki).write_batch(batch)

        assert _not_in_nfc(wiki) == []
        ida = first.created_items["i5"]
        assert _labelled(wiki, "Ida Löbig") == [ida]
        assert _aliases(wiki, ida) == ["Ida Sörne", "Peppi"]
        (married,) = _statements(wiki, ida, "married_name")
        (born,) = _statements(wiki, ida, "birth_name")
        assert (_values(married), _values(born)) == (
            ["Ida Löbig"],
            ["Ida Sörne"],
        )
        # a re-run finds Ida by her label, her aliases and every statement
        # written for her, and sends nothing
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert again.created_items["i5"] == ida
        assert (again.written, again.referenced) == (0, 0)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("first", ["NFD", "NFC"])
    @pytest.mark.parametrize("case", TEXT_CLAIMS, ids=TEXT_CLAIMS)
    def test_a_claim_in_either_form_is_one_statement(self, case, first):
        # the claim twice in one batch, in each form, and then again
        claim = TEXT_CLAIMS[case]
        second = "NFC" if first == "NFD" else "NFD"
        batch = _batch_of(_in_form(claim, first), _in_form(claim, second))
        assert _in_form(claim, "NFD") != claim
        wiki = FakeWiki(TEXT_MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        subject = written.created_items[claim["subject"]["local_id"]]
        (statement,) = _statements(wiki, subject, claim["property"])
        assert _values(statement) == [TEXT_OF[case]]
        assert (written.written, written.referenced, written.skipped) == (
            1,
            0,
            1,
        )
        assert _not_in_nfc(wiki) == []
        before = _snapshot(wiki, subject)
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
        assert _snapshot(wiki, subject) == before

    @pytest.mark.parametrize(
        ("first", "then"), [("NFC", "NFD"), ("NFD", "NFC")]
    )
    def test_a_later_batch_finds_the_item_in_either_form(self, first, then):
        # the card's batch mints Ida's item; the compensation file's,
        # which gives her label and alias in the other form, finds it, and
        # adds the name the file gives her by, in NFC
        wiki = FakeWiki(TEXT_MEDIA, detached=True)
        card = BatchWriter(wiki).write_batch(
            _in_form_batch(_card_batch(), first)
        )
        ida = card.created_items["e05"]

        later = BatchWriter(wiki).write_batch(
            _in_form_batch(_compensation_batch(), then)
        )

        assert later.created_items["e05"] == ida
        assert _labelled(wiki, "Ida Löbig") == [ida]
        assert _aliases(wiki, ida) == ["Ida Sörne", "Ida Löbig geb. Sörne"]
        assert later.written == 1
        assert _not_in_nfc(wiki) == []

    def test_an_equivalence_in_nfd_adds_no_alias_of_the_label(self):
        # a reviewer approves the tree's Sebastian as the certificate's;
        # the tree's batch gives his label decomposed. It is his label in
        # another form, never an alias of his
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        certificate = BatchWriter(wiki).write_batch(_certificate_batch())
        sebastian = certificate.created_items["e01"]
        tree = _approved(
            _in_form_batch(_tree_batch(), "NFD"), {"e03": sebastian}
        )

        BatchWriter(wiki).write_batch(tree)

        assert _aliases(wiki, sebastian) == ["Wastl"]
        writes = wiki.wbi.item.writes
        BatchWriter(wiki).write_batch(tree)
        assert wiki.wbi.item.writes == writes

    def test_a_record_in_nfd_reaches_the_item_the_resolve_step_found(self):
        # the tree's refs to Sebastian give his label in NFC, and the
        # resolve step matched them to his item; its manifest record gives
        # the label decomposed. The record's kind and alias reach the item
        wiki = FakeWiki({MEDIA_SHA1: "Q23", TREE_SHA1: "Q29"}, detached=True)
        certificate = BatchWriter(wiki).write_batch(_certificate_batch())
        sebastian = certificate.created_items["e01"]
        tree = _tree_batch()
        (record,) = tree.entities
        tree = tree.model_copy(
            update={
                "entities": [
                    record.model_copy(
                        update={
                            "label": unicodedata.normalize("NFD", SEBASTIAN)
                        }
                    )
                ]
            }
        )

        written = BatchWriter(wiki).write_batch(
            resolve_batch(tree, {SEBASTIAN: sebastian})
        )

        assert _aliases(wiki, sebastian) == ["Wastl"]
        (kind,) = _statements(wiki, sebastian, "instance_of")
        assert _sources(kind)[2:] == [(["Q29"], []), ([], [written.run_qid])]

    def test_a_run_named_in_nfd_finds_its_run_item_again(self):
        # a model identifier is free text: (here) a local model's, given
        # decomposed
        run = dict(
            RUN,
            model_identifier=unicodedata.normalize(
                "NFD", "fake-model für Kurrent"
            ),
        )
        batch = ClaimBatch.model_validate(
            dict(
                _certificate_batch().model_dump(mode="json"),
                extraction_run=run,
            )
        )
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)
        first = BatchWriter(wiki).write_batch(batch)
        assert _not_in_nfc(wiki) == []
        writes = wiki.wbi.item.writes

        again = BatchWriter(wiki).write_batch(batch)

        assert again.run_qid == first.run_qid
        assert wiki.wbi.item.writes == writes

    def test_a_summary_is_capped_as_the_instance_counts_it(self):
        # Sebastian's summary: 248 characters in NFC, the form the instance
        # stores and caps at 250, and more in the decomposed form the batch
        # gives it in. It is his description whole
        summary = (
            "Hatter of Flörburg, born on 15 February 1862, son of the "
            "master hatter Eugen Löbig and Cäcilie Löbig née Tarle; married "
            "Hildegard Laudler of Fläckfeld at Flörburg on 15 December "
            "1905; owner of the Hutmacherei zum Blauen Hut after his "
            "father's death"
        )
        decomposed = unicodedata.normalize("NFD", summary)
        assert len(summary) <= 250 < len(decomposed)
        batch = _certificate_batch()
        (record,) = batch.entities
        batch = batch.model_copy(
            update={
                "entities": [record.model_copy(update={"summary": decomposed})]
            }
        )
        wiki = FakeWiki({MEDIA_SHA1: "Q23"}, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        item = wiki.wbi.item.get(written.created_items["e01"])
        assert str(item.descriptions.get("en")) == summary
        assert _not_in_nfc(wiki) == []

    def test_nfc_is_the_only_change(self):
        # (here) the burial register transcribed as written: the long s,
        # the bar that doubles the n of "Brunnenring", a combining mark
        # over a letter Unicode has no precomposed form of, and the two
        # spaces the register leaves after the address. The batch gives
        # the umlauts decomposed
        cause = "Waſſerſucht"
        excerpt = (
            "Wohnung: Brun\u0304enring 35.  Witwer der Cäcilie Löbig geb. "
            "Tarle. Alter: 73 Jahre. Geſtorben: 16. März 1909 in Flörburg. "
            "Todesurſache: Waſſerſucht."
        )
        claim = {
            "subject": _local("e07", "Eugen Löbig"),
            "property": "cause_of_death",
            "value": {
                "type": "monolingual",
                "value": {"text": cause, "language": "de"},
            },
            "source": _cites(REGISTER_SHA1, excerpt),
        }
        batch = _batch_of(_in_form(claim, "NFD"))
        # NFC leaves the long s, the bar and the spaces as they are, and
        # composes the umlauts
        assert unicodedata.normalize("NFC", excerpt) == excerpt
        assert unicodedata.normalize("NFKC", cause) != cause
        assert unicodedata.normalize("NFD", excerpt) != excerpt
        wiki = FakeWiki(TEXT_MEDIA, detached=True)

        written = BatchWriter(wiki).write_batch(batch)

        eugen = written.created_items["e07"]
        (died,) = _statements(wiki, eugen, "cause_of_death")
        assert _values(died) == [cause]
        assert [place[3] for place in _places(died)] == [excerpt]
        assert _not_in_nfc(wiki) == []
        writes = wiki.wbi.item.writes
        again = BatchWriter(wiki).write_batch(batch)
        assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
        assert wiki.wbi.item.writes == writes

    @pytest.mark.parametrize("found", [False, True], ids=["minted", "found"])
    def test_a_type_hint_goes_in_nfc(self, found):
        # (here) the directory lists the firm under the heading
        # "Hutgeschäfte": its extraction gives the firm no kind, and that
        # word as its type hint, decomposed. The firm is new, or the
        # interview's batch minted it first
        wiki = FakeWiki(
            {INTERVIEW_SHA1: "Q25", DIRECTORY_SHA1: "Q26"}, detached=True
        )
        if found:
            BatchWriter(wiki).write_batch(_firm_batch())
        directory = _directory_batch(
            "unclassified_entity",
            type_hint=unicodedata.normalize("NFD", "Hutgeschäft"),
        )

        written = BatchWriter(wiki).write_batch(directory)

        firm = written.created_items["firm"]
        hints = _by_value(_statements(wiki, firm, "type_hint"))
        assert "Hutgeschäft" in hints
        assert _not_in_nfc(wiki) == []


class TestPrivacyDefaults:
    def test_no_default_llm_client(self):
        from yizkr.pipeline.llm import make_client

        with pytest.raises(ValueError, match="unknown LLM engine"):
            make_client("default")

    def test_anthropic_requires_explicit_key(self, monkeypatch):
        anthropic = pytest.importorskip("anthropic")  # noqa: F841
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from yizkr.pipeline.llm import AnthropicClient

        with pytest.raises(RuntimeError, match="explicit opt-in"):
            AnthropicClient()

    def test_claude_code_requires_installed_cli(self, monkeypatch):
        from yizkr.pipeline import llm

        def missing(*args, **kwargs):
            raise FileNotFoundError("claude")

        monkeypatch.setattr(llm.subprocess, "run", missing)
        with pytest.raises(RuntimeError, match="explicit\\s+opt-in"):
            llm.ClaudeCodeClient()


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestClaudeCodeClient:
    """Subscription-billed engine: headless `claude -p` (offline, mocked)."""

    def make_client(self, monkeypatch, responses):
        """responses: list of FakeCompleted, popped per subprocess call."""
        from yizkr.pipeline import llm

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return responses.pop(0)

        monkeypatch.setattr(llm.subprocess, "run", fake_run)
        monkeypatch.delenv("YIZKR_LLM_MODEL", raising=False)
        monkeypatch.delenv("YIZKR_LLM_EFFORT", raising=False)
        return llm.ClaudeCodeClient(), calls

    def test_effort_flag(self, monkeypatch):
        monkeypatch.delenv("YIZKR_LLM_EFFORT", raising=False)
        from yizkr.pipeline import llm

        monkeypatch.setattr(
            llm.subprocess,
            "run",
            lambda *a, **k: FakeCompleted(stdout="2.1.207\n"),
        )
        # no effort configured -> no flag (unchanged default behaviour)
        assert "--effort" not in llm.ClaudeCodeClient()._command()
        # explicit effort -> passed through as `--effort <level>`
        cmd = llm.ClaudeCodeClient(effort="low")._command()
        assert cmd[cmd.index("--effort") + 1] == "low"
        # make_client threads it for the claude-code engine
        assert (
            "--effort"
            in llm.make_client("claude-code", effort="medium")._command()
        )

    def test_make_client_threads_model_override(self, monkeypatch):
        # per-phase model override (ADR-0016): make_client(model=...) sets
        # the client's model regardless of $YIZKR_LLM_MODEL, and None
        # inherits the env as before.
        monkeypatch.setenv("YIZKR_LLM_MODEL", "claude-opus-4-8")
        from yizkr.pipeline import llm

        monkeypatch.setattr(
            llm.subprocess,
            "run",
            lambda *a, **k: FakeCompleted(stdout="2.1.207\n"),
        )
        overridden = llm.make_client("claude-code", model="claude-sonnet-5")
        cmd = overridden._command()
        assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
        # None -> inherit the run's main model from the environment
        inherited = llm.make_client("claude-code", model=None)
        assert inherited._command()[
            inherited._command().index("--model") + 1
        ] == ("claude-opus-4-8")

    def test_version_probe_and_identifier(self, monkeypatch):
        client, calls = self.make_client(
            monkeypatch, [FakeCompleted(stdout="2.1.207 (Claude Code)\n")]
        )
        assert calls[0][0] == ["claude", "--version"]
        assert client.model_identifier == "claude-code/2.1.207"

    def test_model_flag_and_identifier(self, monkeypatch):
        monkeypatch.setenv("YIZKR_LLM_MODEL", "claude-sonnet-5")
        from yizkr.pipeline import llm

        monkeypatch.setattr(
            llm.subprocess,
            "run",
            lambda *a, **k: FakeCompleted(stdout="2.1.207\n"),
        )
        client = llm.ClaudeCodeClient()
        assert client.model_identifier == "claude-code/2.1.207:claude-sonnet-5"
        assert "--model" in client._command()

    def test_complete_locks_down_tools_and_strips_api_key(
        self, monkeypatch, tmp_path
    ):
        client, calls = self.make_client(
            monkeypatch,
            [
                FakeCompleted(stdout="2.1.207\n"),
                FakeCompleted(stdout="the transcription"),
            ],
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
        image = tmp_path / "scan.png"
        image.write_bytes(b"png")

        result = client.complete("Transcribe this.", [image])

        assert result == "the transcription"
        command, kwargs = calls[1]
        assert command[:2] == ["claude", "-p"]
        assert command[command.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in command
        assert command[command.index("--allowedTools") + 1] == "Read"
        # prompt goes via stdin and points Read at the image
        assert str(image.resolve()) in kwargs["input"]
        assert "Transcribe this." in kwargs["input"]
        # the engine exists to use subscription auth: keys must not leak in
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

    def test_reassembles_response_spanning_multiple_turns(self, monkeypatch):
        """A long answer split across assistant turns must survive intact.

        `--output-format text` prints only the final turn, silently
        dropping the head of a multi-turn response. stream-json exposes
        every assistant event; the client joins them all in order.
        """
        events = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": '{"claims": ['}]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "]}"}]},
            },
            # the terminal result event repeats only the FINAL turn's text
            {"type": "result", "subtype": "success", "result": "]}"},
        ]
        stream = "\n".join(json.dumps(event) for event in events)
        client, _ = self.make_client(
            monkeypatch,
            [FakeCompleted(stdout="2.1.207\n"), FakeCompleted(stdout=stream)],
        )
        # joined with NO separator: an exact mid-token resume must
        # reconstruct the original text byte-for-byte
        assert client.complete("extract") == '{"claims": []}'

    def test_nonzero_exit_raises_with_detail(self, monkeypatch):
        client, _ = self.make_client(
            monkeypatch,
            [
                FakeCompleted(stdout="2.1.207\n"),
                FakeCompleted(stderr="Invalid API key", returncode=1),
            ],
        )
        with pytest.raises(RuntimeError, match="Invalid API key"):
            client.complete("hello")

    def test_empty_output_raises(self, monkeypatch):
        client, _ = self.make_client(
            monkeypatch,
            [FakeCompleted(stdout="2.1.207\n"), FakeCompleted(stdout="  ")],
        )
        with pytest.raises(RuntimeError, match="no output"):
            client.complete("hello")

    def test_complete_isolates_session(self, monkeypatch):
        """Every one-shot call passes its own fresh --session-id: a
        nested CLI otherwise inherits the parent's session id from the
        environment, and concurrent pipeline calls would collide."""
        client, calls = self.make_client(
            monkeypatch,
            [FakeCompleted(stdout="2.1.207\n"), FakeCompleted(stdout="hi")],
        )
        client.complete("hello")
        command = calls[1][0]
        session_id = command[command.index("--session-id") + 1]
        assert len(session_id) == 36  # a fresh UUID, not inherited

    def test_conversation_open_and_fork(self, monkeypatch):
        """open_conversation pins a session id; fork resumes it with
        --fork-session so the original session is never mutated."""
        client, calls = self.make_client(
            monkeypatch,
            [
                FakeCompleted(stdout="2.1.207\n"),
                FakeCompleted(stdout="segmented"),
                FakeCompleted(stdout="chunk claims"),
            ],
        )
        response, handle = client.open_conversation("segment this")
        assert response == "segmented"
        open_command = calls[1][0]
        assert open_command[open_command.index("--session-id") + 1] == handle

        assert client.fork(handle, "extract unit u1") == "chunk claims"
        fork_command = calls[2][0]
        assert fork_command[fork_command.index("--resume") + 1] == handle
        assert "--fork-session" in fork_command
        assert "--session-id" not in fork_command
