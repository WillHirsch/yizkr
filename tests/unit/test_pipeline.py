"""Offline tests for pipeline steps (pytest; no instance, no LLM)."""

import csv
import hashlib
import itertools
import json
from types import SimpleNamespace

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch, LocalRef, QidRef
from yizkr.pipeline.extract import (
    ClaimFileExtractor,
    LlmClaimExtractor,
    _parse_response,
)
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.review import (
    APPROVE,
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

# Person One's dates are Leopold Löbig's in the example universe: born in
# May 1914 (29 May per the divorce judgment) and married in 1947.
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
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1947},
            }
        ],
        "source": {
            "artifact": ARTIFACT,
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


class TestTranscribe:
    def test_text_file_engine(self, tmp_path):
        source = tmp_path / "scan.txt"
        source.write_text("Geboren 1914")
        result = TextFileTranscriber(source, language="de").transcribe(
            tmp_path / "scan.png", artifact_qid="Q23"
        )
        assert result.text == "Geboren 1914"
        assert result.language == "de"
        assert result.artifact_qid == "Q23"
        assert result.engine.startswith("text-file:")

    def test_llm_vision_engine_passes_image(self, tmp_path):
        llm = FakeLlm("The transcribed text")
        image = tmp_path / "scan.png"
        image.write_bytes(b"png")
        result = LlmVisionTranscriber(llm).transcribe(
            image, artifact_qid="Q23"
        )
        assert result.text == "The transcribed text"
        assert result.engine == "llm-vision:fake-model"
        assert "exactly as written" in llm.prompts[0]

    def test_htr_stub_says_so(self, tmp_path):
        with pytest.raises(NotImplementedError, match="stub"):
            HtrPlatformAdapter().transcribe(tmp_path / "x.png")


class TestExtract:
    def transcription(self) -> Transcription:
        return Transcription(text="married 1947", engine="test")

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

    def test_llm_extractor_prompt_carries_property_descriptions(self):
        """Direction and value type live in the manifest description;
        a key and datatype alone leave the model to guess them."""
        llm = FakeLlm(json.dumps({"claims": CLAIMS}))
        LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
        assert (
            "employer: wikibase-item — organization the person works or "
            "worked for" in llm.prompts[0]
        )
        assert (
            "child: wikibase-item — the subject has the object as a child"
            in llm.prompts[0]
        )

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
            value={"type": "time", "value": {"year": 1914, "month": 5}},
            qualifiers=[
                {
                    "property": "latest_date",
                    "value": {"type": "time", "value": {"year": 1915}},
                }
            ],
        )
        llm = FakeLlm(json.dumps({"claims": [nested]}))
        batch = LlmClaimExtractor(llm).extract(self.transcription(), "Q23")
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
            LlmClaimExtractor(llm, raw_dump=raw).extract(
                self.transcription(), "Q23"
            )
        assert raw.read_text() == "total garbage, no JSON at all"

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
        # notes arrive already formatted (the orchestrator adds "critique:")
        export_review_csv(
            batch(),
            csv_path,
            critique_notes={
                "person-1": "critique: role label 'MOTHER' is not a name"
            },
        )
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            rows = list(reader)
        assert "critique_notes" in fields
        assert "warning" not in fields  # renamed
        assert "living" not in fields  # retired
        person1_rows = [r for r in rows if "person-1" in r["subject"]]
        assert person1_rows
        assert all("critique:" in r["critique_notes"] for r in person1_rows)

    def test_no_critique_leaves_column_blank(self, tmp_path):
        csv_path = tmp_path / "review.csv"
        export_review_csv(batch(), csv_path)
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows
        assert all(row["critique_notes"] == "" for row in rows)


class TestCorrections:
    """ROADMAP item 11: the correction column produces curator claims."""

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
                    # 7 January or 1 July? The universe's two sources for
                    # Gustav's birth transpose exactly this day and month
                    row["correction"] = "07/01/1919"
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
}
ITEMS = {
    "human": "Q1",
    "extraction_run": "Q2",
    "unclassified_entity": "Q3",
    "organization": "Q4",
    "place": "Q5",
}


class FakeItems:
    """WikibaseIntegrator items held in memory; write() assigns a QID.

    Every get() returns the one object the store holds, so a write through
    any copy is seen through every other (compare DetachedItems)."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.writes = 0
        self._next = 100

    def new(self):
        from wikibaseintegrator import WikibaseIntegrator

        item = WikibaseIntegrator().item.new()

        def write(summary=None, **_):
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


class DetachedItems(FakeItems):
    """Items as an instance serves them: every get() returns a copy of the
    item as last written, and a write stores the writing copy whole. So a
    write through a copy read before another write undoes that write, as
    an edit sent from a stale read does on an instance: an edit sends the
    item's aliases whole, and its statements as that copy holds them."""

    def __init__(self) -> None:
        super().__init__()
        self._statement_ids = itertools.count(1)

    def _bound(self, item):
        def write(summary=None, **_):
            self.writes += 1
            if item.id is None:
                item.id = f"Q{self._next}"
                self._next += 1
            for statement in item.claims:
                if statement.id is None:
                    statement.id = f"{item.id}${next(self._statement_ids)}"
            self.store[item.id] = self._copy(item)
            return item

        item.write = write
        return item

    def _copy(self, item):
        from wikibaseintegrator import WikibaseIntegrator

        data = item.get_json()
        data["lastrevid"] = self.writes
        # an instance hashes every reference block it stores
        for statements in data.get("claims", {}).values():
            for statement in statements:
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
        return self._copy(self.store[qid])


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
        for pid in (PROPERTIES["spouse"], PROPERTIES["date_of_birth"]):
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

    def test_qid_source_is_written_as_given(self):
        qid_batch = ClaimBatch.model_validate(
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
        wiki = FakeWiki()
        result = BatchWriter(wiki).write_batch(qid_batch)
        assert wiki.index_reads == 0
        person = wiki.wbi.item.get(result.created_items["person-1"])
        (statement,) = person.claims.get(PROPERTIES["date_of_birth"])
        assert _cited(statement, "stated_in") == ["Q23"]


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
        wiki = FakeWiki({MEDIA_SHA1: "Q23"})
        first = BatchWriter(wiki).write_batch(_certificate_batch())
        person = first.created_items["e01"]
        export = adapt_gedcom(
            "0 HEAD\n1 CHAR UTF-8\n0 @I3@ INDI\n1 NAME Sebastian /Löbig/\n"
            "1 BIRT\n2 DATE 15 FEB 1862\n0 TRLR\n",
            artifact_qid="Q27",
        ).batch
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
