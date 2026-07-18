"""Validation tests for the claim schema (pytest, offline)."""

import json

import pytest
from pydantic import ValidationError

from yizkr.claims import (
    Claim,
    ClaimBatch,
    RegionPercent,
    TimeValue,
    load_vocabulary,
    validate_against_manifest,
)

RUN = {
    "model_identifier": "example-vision-model-2026-01",
    "pipeline_version": "0.1.0",
    "software_commit": "478f9ea",
    "started_at": "2026-07-12T09:00:00Z",
}


def source(**overrides) -> dict:
    base = {
        "artifact": {"kind": "qid", "qid": "Q23"},
        "page_number": "1",
        "region": {"x": 10, "y": 42.5, "width": 60, "height": 8},
        "transcription_excerpt": {"text": "married 1933", "language": "en"},
    }
    base.update(overrides)
    return base


def spouse_claim(**overrides) -> dict:
    base = {
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
            },
        },
        "qualifiers": [
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1933},
            },
            {
                "property": "sourcing_circumstances",
                "value": {
                    "type": "item",
                    "value": {"kind": "vocab", "key": "circa"},
                },
            },
        ],
        "source": source(),
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


def batch(*claims) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {"extraction_run": RUN, "claims": list(claims)}
    )


class TestBatchBasics:
    def test_full_batch_round_trips_through_json(self):
        parsed = batch(spouse_claim())
        again = ClaimBatch.model_validate_json(parsed.model_dump_json())
        assert again == parsed
        assert again.claims[0].qualifiers[0].value.precision == "year"

    def test_empty_claims_list_is_allowed(self):
        assert batch().claims == []

    def test_unknown_fields_rejected_everywhere(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(rnak="preferred"))  # typo must fail loudly

    def test_run_metadata_requires_timezone(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            ClaimBatch.model_validate(
                {
                    "extraction_run": {
                        **RUN,
                        "started_at": "2026-07-12T09:00:00",
                    },
                    "claims": [],
                }
            )


class TestConventionEnforcement:
    def test_source_is_required(self):
        claim = spouse_claim()
        del claim["source"]
        with pytest.raises(ValidationError, match="source"):
            batch(claim)

    def test_source_artifact_is_required(self):
        with pytest.raises(ValidationError, match="artifact"):
            batch(spouse_claim(source={"page_number": "1"}))

    def test_no_rank_field_exists(self):
        assert "rank" not in Claim.model_fields
        with pytest.raises(ValidationError):
            batch(spouse_claim(rank="preferred"))

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(confidence=1.5))


class TestEntityRefs:
    def test_qid_format_enforced(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(subject={"kind": "qid", "qid": "P31"}))
        with pytest.raises(ValidationError):
            batch(spouse_claim(subject={"kind": "qid", "qid": "Q01"}))

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(subject={"kind": "label", "label": "someone"}))

    def test_local_ref_requires_local_id(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(subject={"kind": "local", "label": "x"}))


class TestTimeValues:
    def test_precision_inferred_from_finest_field(self):
        assert TimeValue(year=1929).precision == "year"
        assert TimeValue(year=1929, month=3).precision == "month"
        assert TimeValue(year=1929, month=3, day=14).precision == "day"

    def test_day_without_month_rejected(self):
        with pytest.raises(ValidationError, match="requires a month"):
            TimeValue(year=1929, day=14)

    def test_fields_finer_than_precision_rejected(self):
        with pytest.raises(ValidationError, match="never narrow"):
            TimeValue(year=1929, month=3, day=14, precision="month")

    def test_precision_finer_than_fields_rejected(self):
        with pytest.raises(ValidationError, match="requires the day field"):
            TimeValue(year=1929, precision="day")

    def test_decade_and_century(self):
        assert (
            TimeValue(year=1920, precision="decade").to_wikibase()["precision"]
            == 8
        )
        with pytest.raises(ValidationError):
            TimeValue(year=1920, month=5, precision="decade")

    def test_year_zero_rejected(self):
        with pytest.raises(ValidationError, match="year zero"):
            TimeValue(year=0)

    def test_wikibase_serialization(self):
        assert TimeValue(year=1929).to_wikibase() == {
            "time": "+1929-00-00T00:00:00Z",
            "precision": 9,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }
        julian = TimeValue(
            year=1752, month=9, day=2, calendar="julian"
        ).to_wikibase()
        assert julian["time"] == "+1752-09-02T00:00:00Z"
        assert julian["precision"] == 11
        assert julian["calendarmodel"].endswith("Q1985786")
        assert (
            TimeValue(year=-44, month=3)
            .to_wikibase()["time"]
            .startswith("-0044-03")
        )


class TestRegions:
    def test_out_of_frame_rejected(self):
        with pytest.raises(ValidationError, match="right edge"):
            RegionPercent(x=60, y=10, width=50, height=10)
        with pytest.raises(ValidationError, match="bottom edge"):
            RegionPercent(x=10, y=95, width=10, height=10)
        with pytest.raises(ValidationError):
            RegionPercent(x=-1, y=0, width=10, height=10)
        with pytest.raises(ValidationError):
            RegionPercent(x=0, y=0, width=0, height=10)

    def test_wikibase_string_format(self):
        region = RegionPercent(x=10, y=42.5, width=60, height=8)
        assert region.as_wikibase_string() == "10%,42.5%,60%,8%"


class TestOtherValues:
    def test_monolingual_language_tag(self):
        with pytest.raises(ValidationError):
            batch(
                spouse_claim(
                    source=source(
                        transcription_excerpt={
                            "text": "x",
                            "language": "English",
                        }
                    )
                )
            )

    def test_coordinate_bounds(self):
        with pytest.raises(ValidationError):
            batch(
                spouse_claim(
                    property="coordinate_location",
                    value={
                        "type": "coordinate",
                        "latitude": 91,
                        "longitude": 0,
                    },
                )
            )

    def test_unknown_value_type_rejected(self):
        with pytest.raises(ValidationError):
            batch(spouse_claim(value={"type": "boolean", "value": True}))

    def test_quantity_round_trip_and_serialization(self):
        from yizkr.claims import QuantityValue

        parsed = batch(
            spouse_claim(
                property="number_of_children",
                value={"type": "quantity", "amount": 3},
            )
        )
        value = parsed.claims[0].value
        assert isinstance(value, QuantityValue)
        # unitless Wikibase quantity, sign always explicit
        assert value.to_wikibase() == {"amount": "+3", "unit": "1"}


class TestManifestValidation:
    """The real manifest is the vocabulary; these bind contract to model."""

    def test_realistic_batch_passes_against_real_manifest(self):
        assert validate_against_manifest(batch(spouse_claim())) == []

    def test_referred_to_as_appellation_with_perspective_is_valid(self):
        # ADR-0018: a perspectival appellation is a monolingual statement on
        # the referent, qualified by the ego (a person/family item).
        appellation = {
            "subject": {
                "kind": "local",
                "local_id": "person-1",
                "label": "Rosa",
                "instance_of": "human",
            },
            "property": "referred_to_as",
            "value": {
                "type": "monolingual",
                "value": {"text": "Oma", "language": "de"},
            },
            "qualifiers": [
                {
                    "property": "perspective",
                    "value": {
                        "type": "item",
                        "value": {"kind": "local", "local_id": "person-2"},
                    },
                }
            ],
            "source": source(),
        }
        assert validate_against_manifest(batch(appellation)) == []

    def test_unknown_property_reported(self):
        problems = validate_against_manifest(
            batch(spouse_claim(property="shoe_size"))
        )
        assert problems == ["claims[0]: unknown property key 'shoe_size'"]

    def test_datatype_mismatch_reported(self):
        problems = validate_against_manifest(
            batch(
                spouse_claim(
                    property="date_of_birth",
                    value={"type": "string", "value": "1929"},
                )
            )
        )
        assert any("requires a 'time' value" in p for p in problems)

    def test_qualifier_property_checked(self):
        claim = spouse_claim()
        claim["qualifiers"][0]["property"] = "no_such_qualifier"
        problems = validate_against_manifest(batch(claim))
        assert any(
            "qualifiers[0]" in p and "no_such_qualifier" in p for p in problems
        )

    def test_unknown_vocab_key_reported(self):
        claim = spouse_claim()
        claim["qualifiers"][1]["value"]["value"]["key"] = "roughly"
        problems = validate_against_manifest(batch(claim))
        assert any(
            "unknown vocabulary item key 'roughly'" in p for p in problems
        )

    def test_unknown_instance_of_reported(self):
        claim = spouse_claim()
        claim["subject"]["instance_of"] = "wizard"
        problems = validate_against_manifest(batch(claim))
        assert any("wizard" in p for p in problems)

    def test_kinship_fallback_and_sex_validate(self):
        # ADR-0014: `relative` + a verbatim `kinship_type` string qualifier,
        # and sex_or_gender pointing at the male/female vocab items
        claim = spouse_claim(
            property="relative",
            qualifiers=[
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": "maternal aunt"},
                }  # fmt: skip
            ],
        )
        sex = spouse_claim(
            property="sex_or_gender",
            value={
                "type": "item",
                "value": {"kind": "vocab", "key": "female"},
            },
            qualifiers=[],
        )
        assert validate_against_manifest(batch(claim, sex)) == []

    def test_open_schema_entity_validates(self):
        # ADR-0013: an unclassified entity with a type hint is valid
        parsed = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "entities": [
                    {
                        "id": "e01",
                        "label": "SS Rotterdam",
                        "kind": "unclassified_entity",
                        "type_hint": "ship",
                    }
                ],
            }
        )
        assert validate_against_manifest(parsed) == []
        assert parsed.entities[0].type_hint == "ship"

    def test_all_problems_reported_at_once(self):
        problems = validate_against_manifest(
            batch(
                spouse_claim(property="shoe_size"),
                spouse_claim(
                    property="residence",
                    value={"type": "string", "value": "Paris"},
                ),
            )
        )
        assert len(problems) == 2
        assert problems[0].startswith("claims[0]")
        assert problems[1].startswith("claims[1]")

    def test_injected_vocabulary_supported(self):
        vocabulary = ({"spouse": "wikibase-item"}, set())
        claim = spouse_claim(qualifiers=[])
        claim["subject"].pop("instance_of")
        problems = validate_against_manifest(
            batch(claim), vocabulary=vocabulary
        )
        assert problems == []

    def test_unsupported_datatype_reported_not_crash(self):
        vocabulary = ({"spouse": "url"}, set())
        problems = validate_against_manifest(
            batch(spouse_claim()), vocabulary=vocabulary
        )
        assert any("does not support yet" in p for p in problems)

    def test_quantity_property_accepts_only_quantity_values(self):
        problems = validate_against_manifest(
            batch(
                spouse_claim(
                    property="number_of_children",
                    value={"type": "string", "value": "3"},
                )
            )
        )
        assert any("requires a 'quantity' value" in p for p in problems)

    def test_real_manifest_datatypes_all_supported(self):
        """Every datatype the manifest uses must be expressible in claims."""
        from yizkr.claims import DATATYPE_TO_VALUE_TYPE

        properties, _ = load_vocabulary()
        assert set(properties.values()) <= set(DATATYPE_TO_VALUE_TYPE)


class TestJsonSchemaExport:
    def test_schema_generates_and_names_discriminators(self):
        schema = ClaimBatch.model_json_schema()
        text = json.dumps(schema)
        assert "discriminator" in text
        assert "extraction_run" in schema["properties"]
        # source must be a required property of a claim in the schema too
        claim_schema = schema["$defs"]["Claim"]
        assert "source" in claim_schema["required"]
