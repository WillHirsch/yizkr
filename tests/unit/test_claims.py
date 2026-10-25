"""Validation tests for the claim schema (pytest, offline)."""

import hashlib
import json
import math

import pytest
from pydantic import ValidationError

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    RegionPercent,
    TimeValue,
    load_vocabulary,
    validate_against_manifest,
)
from yizkr.core.dates import UnsupportedDate

RUN = {
    "model_identifier": "example-vision-model-2026-01",
    "pipeline_version": "0.1.0",
    "software_commit": "478f9ea",
    "started_at": "2026-07-12T09:00:00Z",
}


# the bytes of a fictional scan; claims cite it by their SHA-1 (#102)
MEDIA = b"fictional scan: an example certificate, page 1"
MEDIA_SHA1 = hashlib.sha1(MEDIA).hexdigest()


def source(**overrides) -> dict:
    base = {
        "artifact": {"kind": "artifact", "sha1": MEDIA_SHA1},
        "page_number": "1",
        "region": {"x": 10, "y": 42.5, "width": 60, "height": 8},
        "transcription_excerpt": {"text": "married 1905", "language": "en"},
    }
    base.update(overrides)
    return base


def sibling_claim(**overrides) -> dict:
    base = {
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
            },
        },
        "qualifiers": [
            {
                "property": "point_in_time",
                "value": {"type": "time", "year": 1905},
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
        parsed = batch(sibling_claim())
        again = ClaimBatch.model_validate_json(parsed.model_dump_json())
        assert again == parsed
        assert again.claims[0].qualifiers[0].value.precision == "year"

    def test_empty_claims_list_is_allowed(self):
        assert batch().claims == []

    def test_unknown_fields_rejected_everywhere(self):
        with pytest.raises(ValidationError):
            batch(sibling_claim(rnak="preferred"))  # typo must fail loudly

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
        claim = sibling_claim()
        del claim["source"]
        with pytest.raises(ValidationError, match="source"):
            batch(claim)

    def test_source_artifact_is_required(self):
        with pytest.raises(ValidationError, match="artifact"):
            batch(sibling_claim(source={"page_number": "1"}))

    def test_no_rank_field_exists(self):
        assert "rank" not in Claim.model_fields
        with pytest.raises(ValidationError):
            batch(sibling_claim(rank="preferred"))

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            batch(sibling_claim(confidence=1.5))


class TestEntityRefs:
    def test_qid_format_enforced(self):
        with pytest.raises(ValidationError):
            batch(sibling_claim(subject={"kind": "qid", "qid": "P31"}))
        with pytest.raises(ValidationError):
            batch(sibling_claim(subject={"kind": "qid", "qid": "Q01"}))

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            batch(sibling_claim(subject={"kind": "label", "label": "someone"}))

    def test_local_ref_requires_local_id(self):
        with pytest.raises(ValidationError):
            batch(sibling_claim(subject={"kind": "local", "label": "x"}))


class TestArtifactRef:
    """Claims cite their source artifact by the SHA-1 of its media, so a
    batch names the same artifact on any instance and after a reset
    (#102, ADR-0010)."""

    def test_source_artifact_is_content_addressed(self):
        cited = batch(sibling_claim()).claims[0].source.artifact
        assert cited == ArtifactRef(sha1=MEDIA_SHA1)

    def test_from_bytes_and_from_path_hash_the_media(self, tmp_path):
        scan = tmp_path / "certificate-scan.png"
        scan.write_bytes(MEDIA)
        renamed = tmp_path / "another-name.png"
        renamed.write_bytes(MEDIA)
        expected = ArtifactRef(sha1=MEDIA_SHA1)
        assert ArtifactRef.from_bytes(MEDIA) == expected
        # the key is the bytes, whatever the file is called
        assert ArtifactRef.from_path(scan) == expected
        assert ArtifactRef.from_path(renamed) == expected
        assert ArtifactRef.from_bytes(MEDIA + b"!") != expected

    def test_capitalised_digest_names_the_same_bytes(self):
        assert ArtifactRef(sha1=MEDIA_SHA1.upper()) == ArtifactRef(
            sha1=MEDIA_SHA1
        )

    @pytest.mark.parametrize(
        "sha1",
        [
            MEDIA_SHA1[:39],  # truncated
            MEDIA_SHA1 + "0",  # too long
            "g" * 40,  # not hex
            hashlib.sha256(MEDIA).hexdigest(),  # another digest
            "Q23",  # an item id is not a content key
            "",
        ],
    )
    def test_malformed_digest_rejected(self, sha1):
        with pytest.raises(ValidationError):
            batch(
                sibling_claim(
                    source=source(artifact={"kind": "artifact", "sha1": sha1})
                )
            )

    @pytest.mark.parametrize(
        "ref",
        [
            {"kind": "local", "local_id": "artifact-1", "label": "a letter"},
            {"kind": "vocab", "key": "letter"},
        ],
    )
    def test_entity_refs_cannot_name_the_source(self, ref):
        # an artifact is never proposed by extraction or named by a
        # vocabulary key: only its content is
        with pytest.raises(ValidationError):
            batch(sibling_claim(source=source(artifact=ref)))

    def test_an_item_qid_cannot_name_the_source(self):
        """No dual format (#102): a claim that cites its artifact item by
        QID, as batches made before content addressing did, is refused,
        whatever instance the item is on."""
        with pytest.raises(ValidationError):
            batch(
                sibling_claim(
                    source=source(artifact={"kind": "qid", "qid": "Q23"})
                )
            )

    def test_display_form_is_the_prefixed_digest(self):
        # the review CSV shows a source artifact through str()
        assert str(ArtifactRef(sha1=MEDIA_SHA1)) == f"sha1:{MEDIA_SHA1}"

    def test_json_round_trip_keeps_the_digest(self):
        parsed = batch(sibling_claim())
        payload = json.loads(parsed.model_dump_json())
        assert payload["claims"][0]["source"]["artifact"] == {
            "kind": "artifact",
            "sha1": MEDIA_SHA1,
        }
        assert ClaimBatch.model_validate(payload) == parsed


class TestTimeValues:
    # 19 March 1876 is Hildegard Löbig's birth in the example universe
    def test_precision_inferred_from_finest_field(self):
        assert TimeValue(year=1876).precision == "year"
        assert TimeValue(year=1876, month=3).precision == "month"
        assert TimeValue(year=1876, month=3, day=19).precision == "day"

    def test_day_without_month_rejected(self):
        with pytest.raises(ValidationError, match="requires a month"):
            TimeValue(year=1876, day=19)

    def test_fields_finer_than_precision_rejected(self):
        with pytest.raises(ValidationError, match="never narrow"):
            TimeValue(year=1876, month=3, day=19, precision="month")

    def test_precision_finer_than_fields_rejected(self):
        with pytest.raises(ValidationError, match="requires the day field"):
            TimeValue(year=1876, precision="day")

    def test_decade_and_century(self):
        assert (
            TimeValue(year=1920, precision="decade").to_wikibase()["precision"]
            == 8
        )
        with pytest.raises(ValidationError):
            TimeValue(year=1920, month=5, precision="decade")

    @pytest.mark.parametrize("year", [0, -1, -1836, -1800])
    def test_a_year_before_the_common_era_is_unsupported(self, year):
        # ADR-0034 (#338): refused with its reason, never converted
        with pytest.raises(
            ValidationError, match="unsupported: year .* before the common era"
        ):
            TimeValue(year=year)

    def test_the_julian_calendar_is_unsupported(self):
        # Eugen Löbig's birth given (here) as a Julian date: refused with
        # its reason, never converted to the Gregorian (ADR-0034, #338)
        with pytest.raises(
            ValidationError, match="unsupported: calendar 'julian'"
        ):
            TimeValue(year=1836, calendar="julian")

    @pytest.mark.parametrize("calendar", ["Gregorian", " gregorian", "hebrew"])
    def test_a_calendar_the_schema_does_not_name_is_a_schema_error(
        self, calendar
    ):
        # an unknown name is no ADR-0034 refusal: the literal refuses it
        with pytest.raises(ValidationError) as refused:
            TimeValue(year=1836, calendar=calendar)
        assert "unsupported:" not in str(refused.value)

    def test_the_schema_the_model_sees_names_the_julian_calendar(self):
        # so that a Julian source date is stated as one, and refused
        schema = TimeValue.model_json_schema()
        assert schema["properties"]["calendar"]["enum"] == [
            "gregorian",
            "julian",
        ]
        description = " ".join(schema["description"].split())
        assert 'with calendar "julian": it is a reported reject' in description

    def test_wikibase_serialization(self):
        assert TimeValue(year=1876).to_wikibase() == {
            "time": "+1876-00-00T00:00:00Z",
            "precision": 9,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }
        assert TimeValue(year=1, calendar="gregorian").to_wikibase() == {
            "time": "+0001-00-00T00:00:00Z",
            "precision": 9,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }

    @pytest.mark.parametrize(
        "fields",
        [
            {"year": -1836, "precision": "year", "calendar": "gregorian"},
            {"year": 0, "precision": "year", "calendar": "gregorian"},
            {"year": 1836, "precision": "year", "calendar": "julian"},
        ],
    )
    def test_an_unsupported_date_is_never_serialized(self, fields):
        # one built around validation is still never stored (#338)
        value = TimeValue.model_construct(**fields)
        assert value.problem().startswith("unsupported: ")
        with pytest.raises(UnsupportedDate):
            value.to_wikibase()


class TestCenturies:
    """A century is counted as Wikibase counts it, from year 1, and a claim
    gives it by its first year (#241; docs/MODELING.md, "Uncertainty and
    time")."""

    def test_a_century_is_given_by_the_first_year_wikibase_counts(self):
        # MwTimeIsoFormatter names the century of a stored year
        # ceil(year / 100): a century precision takes just the first year
        # of each, 1, 101, ..., 1801
        for year in range(1, 2501):
            first = math.ceil(year / 100) * 100 - 99 == year
            try:
                TimeValue(year=year, precision="century")
            except ValidationError:
                assert not first, year
            else:
                assert first, year

    def test_the_19th_century_is_written_as_its_first_year(self):
        # Eugen Löbig born (here) in the 19th century
        assert TimeValue(year=1801, precision="century").to_wikibase() == {
            "time": "+1801-00-00T00:00:00Z",
            "precision": 7,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }

    @pytest.mark.parametrize(
        "year",
        [
            1800,  # the 18th century's last year, never the 19th's first
            1900,  # the 19th century's last year
            1850,
            1802,
        ],
    )
    def test_a_century_takes_only_its_first_year(self, year):
        with pytest.raises(ValidationError, match="century's first year"):
            TimeValue(year=year, precision="century")

    def test_a_decade_is_not_held_to_a_centurys_first_year(self):
        # the 1800s as a decade, 1800 to 1809, stored as 1800
        written = TimeValue(year=1800, precision="decade").to_wikibase()
        assert (written["time"], written["precision"]) == (
            "+1800-00-00T00:00:00Z",
            8,
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
                sibling_claim(
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
                sibling_claim(
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
            batch(sibling_claim(value={"type": "boolean", "value": True}))

    def test_quantity_round_trip_and_serialization(self):
        from yizkr.claims import QuantityValue

        parsed = batch(
            sibling_claim(
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
        assert validate_against_manifest(batch(sibling_claim())) == []

    def test_referred_to_as_appellation_with_perspective_is_valid(self):
        # ADR-0018: a perspectival appellation is a monolingual statement on
        # the referent, qualified by the ego (a person/family item). In the
        # universe's interview Nicholas calls a neighbour who is no relation
        # "Nana Maisie": a courtesy title, from his perspective.
        appellation = {
            "subject": {
                "kind": "local",
                "local_id": "person-1",
                "label": "Maisie Cutmore",
                "instance_of": "human",
            },
            "property": "referred_to_as",
            "value": {
                "type": "monolingual",
                "value": {"text": "Nana Maisie", "language": "en"},
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
            batch(sibling_claim(property="shoe_size"))
        )
        assert problems == ["claims[0]: unknown property key 'shoe_size'"]

    def test_datatype_mismatch_reported(self):
        problems = validate_against_manifest(
            batch(
                sibling_claim(
                    property="date_of_birth",
                    value={"type": "string", "value": "1876"},
                )
            )
        )
        assert any("requires a 'time' value" in p for p in problems)

    def test_qualifier_property_checked(self):
        claim = sibling_claim()
        claim["qualifiers"][0]["property"] = "no_such_qualifier"
        problems = validate_against_manifest(batch(claim))
        assert any(
            "qualifiers[0]" in p and "no_such_qualifier" in p for p in problems
        )

    def test_unknown_vocab_key_reported(self):
        claim = sibling_claim()
        claim["qualifiers"][1]["value"]["value"]["key"] = "roughly"
        problems = validate_against_manifest(batch(claim))
        assert any(
            "unknown vocabulary item key 'roughly'" in p for p in problems
        )

    def test_unknown_instance_of_reported(self):
        claim = sibling_claim()
        claim["subject"]["instance_of"] = "wizard"
        problems = validate_against_manifest(batch(claim))
        assert any("wizard" in p for p in problems)

    def test_kinship_fallback_and_sex_validate(self):
        # ADR-0014: `relative` + a verbatim `kinship_type` string qualifier,
        # and sex_or_gender pointing at the male/female vocab items
        claim = sibling_claim(
            property="relative",
            qualifiers=[
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": "maternal aunt"},
                }  # fmt: skip
            ],
        )
        sex = sibling_claim(
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
                        "label": "RMS Flund",
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
                sibling_claim(property="shoe_size"),
                sibling_claim(
                    property="residence",
                    value={"type": "string", "value": "Stupfield"},
                ),
            )
        )
        assert len(problems) == 2
        assert problems[0].startswith("claims[0]")
        assert problems[1].startswith("claims[1]")

    def test_injected_vocabulary_supported(self):
        vocabulary = ({"sibling": "wikibase-item"}, set())
        claim = sibling_claim(qualifiers=[])
        claim["subject"].pop("instance_of")
        problems = validate_against_manifest(
            batch(claim), vocabulary=vocabulary
        )
        assert problems == []

    def test_unsupported_datatype_reported_not_crash(self):
        vocabulary = ({"sibling": "url"}, set())
        problems = validate_against_manifest(
            batch(sibling_claim()), vocabulary=vocabulary
        )
        assert any("does not support yet" in p for p in problems)

    def test_quantity_property_accepts_only_quantity_values(self):
        problems = validate_against_manifest(
            batch(
                sibling_claim(
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

    def test_source_artifact_offers_only_the_content_key(self):
        """The schema the extraction prompts carry offers one form of
        source: the artifact's SHA-1 (#102)."""
        schema = ClaimBatch.model_json_schema()
        artifact = schema["$defs"]["SourceReference"]["properties"]["artifact"]
        assert artifact == {"$ref": "#/$defs/ArtifactRef"}
        sha1 = schema["$defs"]["ArtifactRef"]["properties"]["sha1"]
        assert sha1["pattern"] == "^[0-9a-f]{40}$"


# -- a value is never its own subject; a kind key is no value (#461, #462) ---

# Gustav's birth register extract from Flörburg (docs/EXAMPLES.md): its
# people and places, by the ids an entity manifest gives them
ENTITIES = [
    {"id": "sebastian", "label": "Sebastian Löbig", "kind": "human"},
    {"id": "eugen", "label": "Eugen Löbig", "kind": "human"},
    {"id": "gustav", "label": "Gustav Löbig", "kind": "human"},
    {"id": "floerburg", "label": "Flörburg", "kind": "place"},
    {"id": "wedding", "label": "the 1905 wedding", "kind": "wedding"},
]


def local_item(local_id: str) -> dict:
    return {"type": "item", "value": {"kind": "local", "local_id": local_id}}


def qid_item(qid: str) -> dict:
    return {"type": "item", "value": {"kind": "qid", "qid": qid}}


def vocab_item(key: str) -> dict:
    return {"type": "item", "value": {"kind": "vocab", "key": key}}


def about(subject: str | dict, prop: str, value: dict, *qualifiers) -> dict:
    return {
        "subject": (
            {"kind": "local", "local_id": subject}
            if isinstance(subject, str)
            else subject
        ),
        "property": prop,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": source(),
    }


def problems_of(*claims: dict, entities=ENTITIES) -> list[str]:
    return validate_against_manifest(
        ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": list(claims),
                "entities": entities,
            }
        )
    )


def own_subject(found: list[str]) -> list[str]:
    return [p for p in found if "never its own subject" in p]


class TestAValueIsNeverItsOwnSubject:
    @pytest.mark.parametrize(
        "claim",
        [
            # Flörburg located in Flörburg; Flörburg its own residence
            about("floerburg", "location", local_item("floerburg")),
            about("floerburg", "residence", local_item("floerburg")),
            # Sebastian his own father, child, sibling or relative
            about("sebastian", "father", local_item("sebastian")),
            about("sebastian", "child", local_item("sebastian")),
            about("sebastian", "sibling", local_item("sebastian")),
            about("sebastian", "relative", local_item("sebastian")),
            # the same vocabulary item
            about(
                {"kind": "vocab", "key": "photograph"},
                "subclass_of",
                vocab_item("photograph"),
            ),
        ],
    )
    def test_a_claim_naming_its_subject_is_refused(self, claim):
        found = problems_of(claim)
        assert own_subject(found), found
        (problem,) = own_subject(found)
        assert problem.startswith("claims[0].value: ")

    def test_on_every_item_valued_property(self):
        """No property of the manifest takes its subject as its value, so
        the check holds on each one, whatever else refuses the claim."""
        properties, _ = load_vocabulary()
        item_valued = sorted(
            key
            for key, datatype in properties.items()
            if datatype == "wikibase-item"
        )
        assert len(item_valued) > 20
        for prop in item_valued:
            found = problems_of(
                about("sebastian", prop, local_item("sebastian"))
            )
            assert own_subject(found), prop

    def test_the_same_qid_is_the_same_entity(self):
        sebastian = {"kind": "qid", "qid": "Q71"}
        found = problems_of(about(sebastian, "father", qid_item("Q71")))
        assert len(own_subject(found)) == 1
        assert "qid:Q71" in own_subject(found)[0]

    def test_a_local_ref_resolved_to_the_subjects_qid_is_it(self):
        # a reviewer matched the document's Sebastian to the existing Q71
        entities = [
            dict(entity, qid="Q71") if entity["id"] == "sebastian" else entity
            for entity in ENTITIES
        ]
        sebastian = {"kind": "qid", "qid": "Q71"}
        found = problems_of(
            about(sebastian, "father", local_item("sebastian")),
            entities=entities,
        )
        assert len(own_subject(found)) == 1
        # and the other way round
        found = problems_of(
            about("sebastian", "father", qid_item("Q71")), entities=entities
        )
        assert len(own_subject(found)) == 1

    def test_a_qualifier_naming_the_subject_is_refused(self):
        # Sebastian as the ego of a name he is called by
        claim = about(
            "sebastian",
            "referred_to_as",
            {
                "type": "monolingual",
                "value": {"text": "Wastl", "language": "de"},
            },
            {"property": "perspective", "value": local_item("sebastian")},
        )
        (problem,) = problems_of(claim)
        assert problem.startswith("claims[0].qualifiers[0].value: ")
        assert "never its own subject" in problem

    def test_every_qualifier_is_checked(self):
        # the subject among two perspectives: the second is refused
        claim = about(
            "sebastian",
            "referred_to_as",
            {
                "type": "monolingual",
                "value": {"text": "Wastl", "language": "de"},
            },
            {"property": "perspective", "value": local_item("eugen")},
            {"property": "perspective", "value": local_item("sebastian")},
        )
        (problem,) = problems_of(claim)
        assert problem.startswith("claims[0].qualifiers[1].value: ")
        assert "never its own subject" in problem

    def test_one_reflexive_qualifier_rejects_the_whole_claim(self):
        from yizkr.pipeline.extract import salvage_valid_claims

        claim = about(
            "eugen",
            "father",
            local_item("sebastian"),
            {"property": "perspective", "value": local_item("eugen")},
        )
        valid, rejects = salvage_valid_claims([claim], RUN, entities=ENTITIES)
        assert valid.claims == []
        (reject,) = rejects
        assert own_subject(reject["problems"])

    @pytest.mark.parametrize(
        "claim",
        [
            about("eugen", "father", local_item("sebastian")),
            about("gustav", "residence", local_item("floerburg")),
            about("gustav", "place_of_birth", local_item("floerburg")),
            about("eugen", "father", qid_item("Q71")),
            about(
                "eugen",
                "referred_to_as",
                {
                    "type": "monolingual",
                    "value": {"text": "Wastl", "language": "de"},
                },
                {"property": "perspective", "value": local_item("sebastian")},
            ),
        ],
    )
    def test_another_entity_stands(self, claim):
        assert problems_of(claim) == []

    def test_it_is_a_counted_reject_that_sinks_no_other_claim(self):
        from yizkr.pipeline.extract import salvage_valid_claims

        valid, rejects = salvage_valid_claims(
            [
                about("eugen", "father", local_item("sebastian")),
                about("sebastian", "father", local_item("sebastian")),
                about("floerburg", "location", local_item("floerburg")),
            ],
            RUN,
            entities=ENTITIES,
        )
        assert len(valid.claims) == 1
        assert len(rejects) == 2
        for reject in rejects:
            assert own_subject(reject["problems"])

    def test_the_chunk_prompt_says_so(self):
        from yizkr.pipeline.chunking import CHUNK_PROMPT

        prompt = " ".join(CHUNK_PROMPT.split())
        assert "qualifier's, is never its own subject" in prompt
        assert (
            'a kind key ("place", "time_period_class") is a value of '
            "instance_of or subclass_of alone" in prompt
        )


class TestAKindKeyIsNoValue:
    @pytest.mark.parametrize(
        ("claim", "kind"),
        [
            # the manifest's kind key for periods, not a period
            (
                about(
                    "wedding", "time_period", vocab_item("time_period_class")
                ),
                "time_period_class",
            ),
            # the class of places, not a country
            (
                about("gustav", "country_of_citizenship", vocab_item("place")),
                "place",
            ),
            (about("gustav", "residence", vocab_item("place")), "place"),
            # the classes of documents, not a document
            (about("gustav", "image", vocab_item("artifact")), "artifact"),
            (about("gustav", "image", vocab_item("photograph")), "photograph"),
            # depicts takes an entity of any kind, never a class
            (about("gustav", "depicts", vocab_item("human")), "human"),
            # nor does the subject's own kind take one
            (
                about(
                    "sebastian", "said_to_be_the_same_as", vocab_item("human")
                ),
                "human",
            ),
        ],
    )
    def test_a_kind_key_as_a_value_is_refused(self, claim, kind):
        (problem,) = problems_of(claim)
        assert problem.startswith("claims[0].value: ")
        assert f"{kind!r} is a kind key" in problem

    @pytest.mark.parametrize(
        "claim",
        [
            about("gustav", "instance_of", vocab_item("human")),
            about("gustav", "instance_of", vocab_item("time_period_class")),
            about("gustav", "instance_of", vocab_item("photograph")),
            # an existing class, named by its QID
            about("gustav", "instance_of", qid_item("Q5")),
            # an existing period item, named by its QID
            about("wedding", "time_period", qid_item("Q88")),
            # a vocabulary value the property names
            about("gustav", "sex_or_gender", vocab_item("male")),
            about(
                "wedding",
                "point_in_time",
                {"type": "time", "year": 1919},
                {
                    "property": "sourcing_circumstances",
                    "value": vocab_item("circa"),
                },
            ),
        ],
    )
    def test_a_class_where_a_class_is_taken_and_a_value_where_named(
        self, claim
    ):
        assert problems_of(claim) == []

    def test_a_class_property_takes_no_vocabulary_value(self):
        (problem,) = problems_of(
            about("gustav", "instance_of", vocab_item("circa"))
        )
        assert "'circa' is a vocabulary value, not a class" in problem

    def test_an_unknown_key_has_one_reason(self):
        (problem,) = problems_of(
            about("gustav", "instance_of", vocab_item("hat_maker"))
        )
        assert "unknown vocabulary item key 'hat_maker'" in problem

    @pytest.mark.parametrize(
        "claim",
        [
            about("sebastian", "depicts", vocab_item("circa")),
            about(
                {"kind": "qid", "qid": "Q71"},
                "said_to_be_the_same_as",
                vocab_item("circa"),
            ),
        ],
    )
    def test_an_entity_property_takes_no_vocabulary_value(self, claim):
        (problem,) = problems_of(claim)
        assert problem.startswith("claims[0].value: ")
        assert "takes an entity, not the vocabulary value 'circa'" in problem

    def test_a_qualifier_is_checked_too(self):
        claim = about(
            "gustav",
            "residence",
            local_item("floerburg"),
            {"property": "perspective", "value": vocab_item("family")},
        )
        (problem,) = problems_of(claim)
        assert problem.startswith("claims[0].qualifiers[0].value: ")
        assert "'family' is a kind key" in problem

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            (vocab_item("time_period"), "unknown vocabulary item key"),
            (vocab_item("circa"), "does not accept a 'circa'"),
            (
                local_item("the_war_years"),
                "not in the batch's entity manifest",
            ),
        ],
    )
    def test_anything_else_is_refused(self, value, reason):
        found = problems_of(about("wedding", "time_period", value))
        assert any(reason in problem for problem in found), found

    def test_it_is_a_counted_reject_that_sinks_no_other_claim(self):
        from yizkr.pipeline.extract import salvage_valid_claims

        valid, rejects = salvage_valid_claims(
            [
                about("gustav", "residence", local_item("floerburg")),
                about(
                    "wedding", "time_period", vocab_item("time_period_class")
                ),
            ],
            RUN,
            entities=ENTITIES,
        )
        assert len(valid.claims) == 1
        (reject,) = rejects
        assert "is a kind key" in reject["problems"][0]


class TestCountryOfCitizenship:
    """The place model has no country class, so `country_of_citizenship`
    accepts a place, and the prompt guidance, its manifest description,
    says it is a country, never a town within one."""

    def test_a_citizenship_of_floerburg_is_a_place_validation_takes(self):
        # validation cannot tell a town from a country: the guidance does
        claim = about(
            "gustav", "country_of_citizenship", local_item("floerburg")
        )
        assert problems_of(claim) == []

    def test_it_accepts_places_and_the_escape_hatch(self):
        from yizkr.model.schema import load_acceptance

        assert load_acceptance().accepts["country_of_citizenship"] == {
            "place",
            "unclassified_entity",
        }

    def test_the_prompt_says_a_country_never_a_town(self):
        from yizkr.pipeline.extract import vocabulary_prompt_lines

        property_lines, _ = vocabulary_prompt_lines()
        (line,) = [
            line
            for line in property_lines.splitlines()
            if line.strip().startswith("country_of_citizenship:")
        ]
        assert "a country, never a town, city or region within one" in line
