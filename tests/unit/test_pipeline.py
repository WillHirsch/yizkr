"""Offline tests for pipeline steps (pytest; no instance, no LLM)."""

import json

import pytest

from yizkr.claims import ClaimBatch, LocalRef, QidRef
from yizkr.pipeline.extract import (
    ClaimFileExtractor,
    LlmClaimExtractor,
    _parse_response,
)
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.review import apply_review, claim_id, export_review_csv
from yizkr.pipeline.transcribe import (
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
        "subject": {
            "kind": "local",
            "local_id": "person-1",
            "label": "Person One",
            "instance_of": "human",
        },
        "property": "spouse",
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
            {"property": "point_in_time", "value": {"type": "time", "year": 1933}}
        ],
        "source": {
            "artifact": {"kind": "qid", "qid": "Q23"},
            "page_number": "1",
            "transcription_excerpt": {"text": "married", "language": "en"},
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

    def test_llm_extractor_salvages_valid_claims_around_bad_ones(self, tmp_path):
        """One invalid claim must not discard the whole (expensive)
        extraction: the valid subset proceeds, rejects and reasons land
        in the rejects dump for diagnosis."""
        mixed = [CLAIMS[0], dict(CLAIMS[1], property="shoe_size"), CLAIMS[1]]
        rejects_path = tmp_path / "batch.json.rejected.json"
        llm = FakeLlm(json.dumps({"claims": mixed}))
        batch = LlmClaimExtractor(llm, rejects_dump=rejects_path).extract(
            self.transcription(), "Q23"
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
        LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
        assert "date_of_birth: time" in llm.prompts[0]
        assert "place_of_birth: wikibase-item" in llm.prompts[0]
        assert "circa" in llm.prompts[0]

    def test_llm_extractor_rejects_prose_without_json(self):
        llm = FakeLlm("I could not find any claims, sorry!")
        with pytest.raises(ValueError, match="no claim JSON"):
            LlmClaimExtractor(llm).extract(self.transcription(), "Q23")

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
            value={"type": "time", "value": {"year": 1910, "month": 5}},
            qualifiers=[
                {
                    "property": "latest_date",
                    "value": {"type": "time", "value": {"year": 1911}},
                }
            ],
        )
        llm = FakeLlm(json.dumps({"claims": [nested]}))
        batch = LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
        assert batch.claims[0].value.year == 1910
        assert batch.claims[0].qualifiers[0].value.year == 1911

    def test_over_length_excerpts_are_clipped_not_fatal(self):
        """Wikibase caps text values at 400 chars; an excerpt is a
        verification aid, so it is clipped visibly instead of losing
        the claim (or failing the write)."""
        long_claim = dict(
            CLAIMS[1],
            source={
                "artifact": {"kind": "qid", "qid": "Q23"},
                "transcription_excerpt": {"text": "word " * 100, "language": "en"},
            },
        )
        llm = FakeLlm(json.dumps({"claims": [long_claim]}))
        batch = LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
        text = batch.claims[0].source.transcription_excerpt.text
        assert len(text) <= 400
        assert text.endswith("…")

    def test_extractor_dumps_raw_response_before_parsing(self, tmp_path):
        """Engine output is expensive: it must be on disk even when
        parsing/validation fails, so nothing paid-for is unrecoverable."""
        raw = tmp_path / "deep" / "batch.json.raw.txt"
        llm = FakeLlm("total garbage, no JSON at all")
        with pytest.raises(ValueError):
            LlmClaimExtractor(llm, raw_dump=raw).extract(self.transcription(), "Q23")
        assert raw.read_text() == "total garbage, no JSON at all"

    def test_file_extractor_wraps_bare_claims(self, tmp_path):
        claims_file = tmp_path / "claims.json"
        claims_file.write_text(json.dumps({"claims": CLAIMS}))
        result = ClaimFileExtractor(claims_file).extract(self.transcription(), "Q23")
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
        again = ClaimBatch.model_validate_json(batch().model_dump_json()).claims[0]
        assert claim_id(first) == claim_id(again)


class TestLivingWarning:
    """ROADMAP item 10: the review CSV warns about possibly-living subjects."""

    def test_no_death_and_recent_activity_warns(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        export_review_csv(batch(), csv_path, cutoff_year=1920)
        with open(csv_path, newline="") as f:
            rows = list(__import__("csv").DictReader(f))
        # spouse claim carries point_in_time 1933 >= cutoff; no death data
        assert all("possibly living" in row["warning"] for row in rows)

    def test_death_data_suppresses_warning(self, tmp_path):
        source = batch()
        death = source.claims[1].model_copy(
            update={
                "property": "date_of_death",
                "value": source.claims[1].value.model_copy(update={"year": 1980}),
            }
        )
        source = source.model_copy(update={"claims": [*source.claims, death]})
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path, cutoff_year=1920)
        with open(csv_path, newline="") as f:
            rows = list(__import__("csv").DictReader(f))
        assert all(row["warning"] == "" for row in rows)

    def test_graph_death_knowledge_suppresses_warning(self, tmp_path):
        source = batch()
        qid_subject = source.claims[0].model_copy(update={"subject": QidRef(qid="Q99")})
        source = source.model_copy(update={"claims": [qid_subject]})
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path, known_deaths={"Q99"}, cutoff_year=1920)
        with open(csv_path, newline="") as f:
            rows = list(__import__("csv").DictReader(f))
        assert rows[0]["warning"] == ""

    def test_old_activity_does_not_warn(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        export_review_csv(batch(), csv_path, cutoff_year=1990)
        with open(csv_path, newline="") as f:
            rows = list(__import__("csv").DictReader(f))
        assert all(row["warning"] == "" for row in rows)


class TestCorrections:
    """ROADMAP item 11: the correction column produces curator claims."""

    def test_date_correction_replaces_value_and_marks_curated(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_birth(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "date_of_birth":
                    row["correction"] = "1910-05-12"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_birth)
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        (birth,) = [c for c in approved.claims if c.property == "date_of_birth"]
        assert (birth.value.year, birth.value.month, birth.value.day) == (1910, 5, 12)
        assert birth.value.precision == "day"
        assert birth.curated is True
        (spouse,) = [c for c in approved.claims if c.property == "spouse"]
        assert spouse.curated is False

    def test_ambiguous_date_correction_is_refused(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_badly(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "date_of_birth":
                    row["correction"] = "03/05/1950"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_badly)
        approved, warnings = apply_review(source, csv_path)
        assert [c.property for c in approved.claims] == ["spouse"]
        assert any("not an unambiguous date" in w for w in warnings)

    def test_item_values_cannot_be_text_corrected(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def correct_spouse(rows):
            for row in rows:
                row["decision"] = "approve"
                if row["property"] == "spouse":
                    row["correction"] = "Someone Else"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, correct_spouse)
        approved, warnings = apply_review(source, csv_path)
        assert [c.property for c in approved.claims] == ["date_of_birth"]
        assert any("cannot be corrected as text" in w for w in warnings)


class TestLivingFlag:
    """ROADMAP item 10: `living` column sets the privacy flag as curation."""

    def test_living_yes_emits_curated_flag_claim(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def flag_living(rows):
            for row in rows:
                row["decision"] = "approve"
            rows[0]["living"] = "yes"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, flag_living)
        approved, warnings = apply_review(source, csv_path)
        assert warnings == []
        (flag,) = [c for c in approved.claims if c.property == "living_person"]
        assert flag.curated is True
        assert flag.value.value.key == "yes"
        assert flag.subject == source.claims[0].subject
        # referenced to the artifact whose review prompted it, no locator
        assert flag.source.artifact.qid == "Q23"
        assert flag.source.transcription_excerpt is None

    def test_flag_rides_even_on_rejected_rows(self, tmp_path):
        """Rejecting a misread claim must not lose the living flag."""
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def reject_but_flag(rows):
            for row in rows:
                row["decision"] = "reject"
            rows[0]["living"] = "no"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, reject_but_flag)
        approved, warnings = apply_review(source, csv_path)
        (flag,) = approved.claims
        assert flag.property == "living_person"
        assert flag.value.value.key == "no"

    def test_conflicting_flags_warn_and_do_nothing(self, tmp_path):
        source = batch()
        csv_path = tmp_path / "review.csv"
        export_review_csv(source, csv_path)

        def conflict(rows):
            for row in rows:
                row["decision"] = "approve"
            rows[0]["living"] = "yes"
            rows[1]["living"] = "no"
            return rows

        TestReviewRoundTrip.edit_csv(csv_path, conflict)
        approved, warnings = apply_review(source, csv_path)
        assert not any(c.property == "living_person" for c in approved.claims)
        assert any("conflicting living flags" in w for w in warnings)


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
        return llm.ClaudeCodeClient(), calls

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

    def test_complete_locks_down_tools_and_strips_api_key(self, monkeypatch, tmp_path):
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
                "message": {"content": [{"type": "text", "text": '{"claims": ['}]},
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
