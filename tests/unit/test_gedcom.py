"""Offline tests for the GEDCOM adapter (item 13). Fixture is fictional."""

from pathlib import Path

import pytest

from yizkr.claims import validate_against_manifest
from yizkr.pipeline.gedcom import (
    ADAPTER_IDENTIFIER,
    adapt_gedcom,
    parse_gedcom,
    parse_gedcom_date,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "example-family.ged"


def fixture_result():
    return adapt_gedcom(FIXTURE.read_text(), artifact_qid="Q23")


def claims_for(batch, subject_id, prop=None):
    return [
        claim
        for claim in batch.claims
        if getattr(claim.subject, "local_id", None) == subject_id
        and (prop is None or claim.property == prop)
    ]


class TestParser:
    def test_nesting_and_xrefs(self):
        roots = parse_gedcom(FIXTURE.read_text())
        indi = [r for r in roots if r.tag == "INDI"]
        assert [r.xref for r in indi] == ["@I1@", "@I2@", "@I3@", "@I4@", "@I5@"]
        birth = indi[0].first("BIRT")
        assert birth.text("DATE") == "12 AUG 1885"
        assert birth.text("PLAC") == "Springfield, Example County"

    def test_record_lines_round_trip(self):
        roots = parse_gedcom("0 @I1@ INDI\n1 NAME A /B/\n2 SOUR x\n")
        assert roots[0].lines() == "0 @I1@ INDI\n1 NAME A /B/\n2 SOUR x"

    def test_cont_concatenation(self):
        roots = parse_gedcom("0 @N1@ NOTE first\n1 CONC  part\n1 CONT second line\n")
        assert roots[0].value == "first part\nsecond line"

    def test_bad_line_raises_with_number(self):
        with pytest.raises(ValueError, match="line 2"):
            parse_gedcom("0 HEAD\nnot a gedcom line\n")


class TestDates:
    def test_exact_precisions(self):
        assert parse_gedcom_date("12 AUG 1885")[0] == {
            "type": "time", "year": 1885, "month": 8, "day": 12, "precision": "day",
        }  # fmt: skip
        assert parse_gedcom_date("MAR 1950")[0]["precision"] == "month"
        assert parse_gedcom_date("1908")[0]["precision"] == "year"

    def test_abt_maps_to_circa(self):
        value, quals = parse_gedcom_date("ABT 1880")
        assert value["year"] == 1880 and value["precision"] == "year"
        assert quals[0]["property"] == "sourcing_circumstances"
        assert quals[0]["value"]["value"]["key"] == "circa"

    def test_bef_maps_to_latest_date(self):
        value, quals = parse_gedcom_date("BEF 8 MAY 1945")
        assert value["day"] == 8
        assert quals == [{"property": "latest_date", "value": value}]

    def test_between_maps_to_range(self):
        value, quals = parse_gedcom_date("BET 1910 AND 1912")
        assert value["year"] == 1910
        assert {q["property"] for q in quals} == {"earliest_date", "latest_date"}

    def test_garbage_is_never_guessed(self):
        assert parse_gedcom_date("SOMEDAY MAYBE") == (None, [])
        assert parse_gedcom_date("ABT UNKNOWNVILLE") == (None, [])


class TestAdapter:
    def test_batch_is_manifest_valid_and_deterministic(self):
        first = fixture_result()
        assert validate_against_manifest(first.batch) == []
        second = fixture_result()
        assert first.batch.claims == second.batch.claims
        assert first.batch.entities == second.batch.entities
        assert first.batch.extraction_run.model_identifier == ADAPTER_IDENTIFIER

    def test_entities_carry_names_and_aliases(self):
        batch = fixture_result().batch
        anna = next(e for e in batch.entities if e.id == "i1")
        assert anna.label == "Anna Maria Beispiel"
        assert anna.aliases == ["Anna Muster"]
        assert anna.kind == "human"
        # places become entities too, deduplicated by name
        springfield = [
            e for e in batch.entities if e.label == "Springfield, Example County"
        ]
        assert len(springfield) == 1

    def test_uncertainty_flows_into_claims(self):
        batch = fixture_result().batch
        (death,) = claims_for(batch, "i1", "date_of_death")
        assert death.qualifiers[0].property == "latest_date"
        (birth,) = claims_for(batch, "i2", "date_of_birth")
        assert birth.qualifiers[0].property == "sourcing_circumstances"
        (carla_birth,) = claims_for(batch, "i3", "date_of_birth")
        assert {q.property for q in carla_birth.qualifiers} == {
            "earliest_date",
            "latest_date",
        }

    def test_parentage_and_marriage(self):
        batch = fixture_result().batch
        (father,) = claims_for(batch, "i3", "father")
        assert father.value.value.local_id == "i2"
        (mother,) = claims_for(batch, "i3", "mother")
        assert mother.value.value.local_id == "i1"
        (marriage,) = [
            c
            for c in claims_for(batch, "i1", "spouse")
            if c.value.value.local_id == "i2"
        ]
        quals = {q.property for q in marriage.qualifiers}
        assert {"start_time", "location"} <= quals

    def test_divorce_becomes_end_time(self):
        batch = fixture_result().batch
        (spouse,) = [
            c
            for c in claims_for(batch, "i3", "spouse")
            if c.value.value.local_id == "i4"
        ]
        quals = {q.property: q for q in spouse.qualifiers}
        assert quals["start_time"].value.year == 1935
        assert quals["end_time"].value.year == 1939

    def test_every_claim_carries_verbatim_gedcom_excerpt(self):
        batch = fixture_result().batch
        for claim in batch.claims:
            excerpt = claim.source.transcription_excerpt
            assert claim.source.artifact.qid == "Q23"
            assert excerpt is not None and len(excerpt.text) <= 400
            # monolingual text values forbid newlines
            assert "\n" not in excerpt.text
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        assert "2 DATE 12 AUG 1885" in birth.source.transcription_excerpt.text

    def test_unparseable_date_warns_instead_of_guessing(self):
        result = fixture_result()
        assert any("SOMEDAY MAYBE" in warning for warning in result.warnings)
        assert claims_for(result.batch, "i5", "date_of_birth") == []

    def test_unsupported_tags_are_counted_not_silent(self):
        result = fixture_result()
        assert result.unsupported_tags.get("OCCU") == 1
