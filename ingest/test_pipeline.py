"""Offline tests for pipeline steps (pytest; no instance, no LLM)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.claims import ClaimBatch, LocalRef, QidRef
from ingest.extract import ClaimFileExtractor, LlmClaimExtractor, _parse_response
from ingest.resolve import resolve_batch
from ingest.review import apply_review, claim_id, export_review_csv
from ingest.transcribe import (
    HtrPlatformAdapter,
    LlmVisionTranscriber,
    TextFileTranscriber,
    Transcription,
)

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1.0",
    "started_at": "2026-07-12T09:00:00Z",
}

CLAIMS = [
    {
        "subject": {"kind": "local", "local_id": "person-1",
                    "label": "Person One", "instance_of": "human"},
        "property": "spouse",
        "value": {"type": "item",
                  "value": {"kind": "local", "local_id": "person-2",
                            "label": "Person Two", "instance_of": "human"}},
        "qualifiers": [{"property": "point_in_time",
                        "value": {"type": "time", "year": 1933}}],
        "source": {"artifact": {"kind": "qid", "qid": "Q23"},
                   "page_number": "1",
                   "transcription_excerpt": {"text": "married", "language": "en"}},
        "confidence": 0.9,
    },
    {
        "subject": {"kind": "local", "local_id": "person-1",
                    "label": "Person One", "instance_of": "human"},
        "property": "date_of_birth",
        "value": {"type": "time", "year": 1910, "month": 5},
        "source": {"artifact": {"kind": "qid", "qid": "Q23"}},
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


class TestTranscribe:
    def test_text_file_engine(self, tmp_path):
        source = tmp_path / "scan.txt"
        source.write_text("Geboren 1910")
        result = TextFileTranscriber(source, language="de").transcribe(
            tmp_path / "scan.png", artifact_qid="Q23"
        )
        assert result.text == "Geboren 1910"
        assert result.language == "de"
        assert result.artifact_qid == "Q23"
        assert result.engine.startswith("text-file:")

    def test_llm_vision_engine_passes_image(self, tmp_path):
        llm = FakeLlm("The transcribed text")
        image = tmp_path / "scan.png"
        image.write_bytes(b"png")
        result = LlmVisionTranscriber(llm).transcribe(image, artifact_qid="Q23")
        assert result.text == "The transcribed text"
        assert result.engine == "llm-vision:fake-model"
        assert "exactly as written" in llm.prompts[0]

    def test_htr_stub_says_so(self, tmp_path):
        with pytest.raises(NotImplementedError, match="stub"):
            HtrPlatformAdapter().transcribe(tmp_path / "x.png")


class TestExtract:
    def transcription(self) -> Transcription:
        return Transcription(text="married 1933", engine="test")

    def test_llm_extractor_validates_response(self):
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        result = LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
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
            LlmClaimExtractor(llm).extract(self.transcription(), "Q23")

    def test_llm_extractor_rejects_prose_without_json(self):
        llm = FakeLlm("I could not find any claims, sorry!")
        with pytest.raises(ValueError, match="no JSON object"):
            LlmClaimExtractor(llm).extract(self.transcription(), "Q23")

    def test_parse_response_tolerates_fences_and_prose(self):
        payload = {"claims": []}
        fenced = f"Here you go:\n```json\n{json.dumps(payload)}\n```\nDone!"
        assert _parse_response(fenced) == payload
        assert _parse_response(json.dumps(payload)) == payload

    def test_file_extractor_wraps_bare_claims(self, tmp_path):
        claims_file = tmp_path / "claims.json"
        claims_file.write_text(json.dumps({"claims": CLAIMS}))
        result = ClaimFileExtractor(claims_file).extract(
            self.transcription(), "Q23"
        )
        assert len(result.claims) == 2
        assert result.extraction_run.model_identifier == "manual"


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


class TestReviewRoundTrip:
    def test_export_columns_and_content(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        rows = export_review_csv(batch(), csv_path)
        assert rows == 2
        content = csv_path.read_text()
        assert "claim_id" in content and "decision" in content
        assert "NEW:person-1 (Person One)" in content
        assert "1910-05" in content
        assert "point_in_time=1933" in content

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

    def test_claim_id_stable_across_serialization(self):
        first = batch().claims[0]
        again = ClaimBatch.model_validate_json(
            batch().model_dump_json()
        ).claims[0]
        assert claim_id(first) == claim_id(again)


class TestPrivacyDefaults:
    def test_no_default_llm_client(self):
        from ingest.llm import make_client

        with pytest.raises(ValueError, match="unknown LLM engine"):
            make_client("default")

    def test_anthropic_requires_explicit_key(self, monkeypatch):
        anthropic = pytest.importorskip("anthropic")  # noqa: F841
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from ingest.llm import AnthropicClient

        with pytest.raises(RuntimeError, match="explicit opt-in"):
            AnthropicClient()
