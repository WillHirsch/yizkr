"""Mention records, claim structure, accepted kinds and ADR-0036's
vocabulary in the schema and the manifest (ADR-0035, ADR-0036; #406).

Every example is from the fictional universe (docs/EXAMPLES.md), the
Russian and West Frisian sources of #400 among them.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tests.unit.test_manifest import minimal_raw
from yizkr.claims import (
    ClaimBatch,
    EntityRecord,
    PersonStructure,
    set_aside_structure,
    structure_problems,
    validate_against_manifest,
)
from yizkr.core.wiki import WikiClient
from yizkr.model.bootstrap import bootstrap_instance
from yizkr.model.schema import Manifest, load_acceptance, load_manifest
from yizkr.pipeline.extract import (
    claim_schema_prompt_text,
    entity_kind_prompt_lines,
    salvage_valid_claims,
    vocabulary_prompt_lines,
)

RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1",
    "started_at": "2026-09-29T10:00:00+00:00",
}
SHA1 = "0" * 40


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


# -- the manifest -------------------------------------------------------------


# ADR-0035 §3.5's table, with ADR-0036's properties and the new ones of
# §3.4 and §3.6: each item-valued property and the kinds it accepts,
# unclassified_entity aside.
ACCEPTS = {
    "place_of_birth": {"place"},
    "place_of_death": {"place"},
    "burial_place": {"place"},
    "residence": {"place"},
    "location": {"place"},
    "country_of_citizenship": {"place"},
    "part_of": {"place", "artifact"},
    "father": {"human"},
    "mother": {"human"},
    "child": {"human"},
    "sibling": {"human"},
    "relative": {"human"},
    "gave_refuge_to": {"human"},
    "participant": {"human", "family"},
    "perspective": {"human", "family"},
    "member_of": {"organization", "family"},
    "employer": {"organization"},
    "educated_at": {"organization"},
    "issued_by": {"organization"},
    "carried_out_by": {"organization"},
    "occupation": {"occupation_class"},
    "time_period": {"time_period_class"},
    "image": {"artifact"},
    "sex_or_gender": {"male", "female"},
    "sourcing_circumstances": {"circa"},
    "object_has_role": {
        "principal",
        "parent",
        "partner",
        "spouse",
        "informant",
        "witness",
        "officiant",
    },
    "kinship_class": {
        "kin_parent",
        "kin_child",
        "kin_sibling",
        "kin_grandparent",
        "kin_grandchild",
        "kin_aunt_or_uncle",
        "kin_niece_or_nephew",
        "kin_cousin",
        "kin_parent_in_law",
        "kin_child_in_law",
        "kin_sibling_in_law",
        "kin_relative",
    },
    "side": {"paternal", "maternal"},
    "civil_status": {"unmarried", "married", "widowed", "divorced"},
    "judgement": {
        "judgement_kind",
        "judgement_relation",
        "judgement_date_reading",
        "judgement_identity",
        "judgement_other",
    },
    "started_by": {"wedding", "civil_wedding", "religious_wedding"},
    "ended_by": {"divorce", "annulment", "death", "event"},
    "stated_in": {"artifact", "artifact_fragment"},
    "inferred_from_extraction_run": {"extraction_run"},
}


class TestAcceptedKinds:
    def test_every_item_valued_property_declares_its_kinds(self, manifest):
        for key, spec in manifest.properties.items():
            if spec.datatype != "wikibase-item":
                assert spec.accepts is None, key
            elif spec.retired:
                assert spec.accepts == [], key
            else:
                assert spec.accepts, key

    def test_the_kinds_are_adr_0035s(self, manifest):
        for key, kinds in ACCEPTS.items():
            accepts = set(manifest.properties[key].accepts)
            assert accepts - {"unclassified_entity"} == kinds, key

    def test_the_escape_hatch_is_accepted_wherever_a_real_kind_is(
        self, manifest
    ):
        kinds = {k for k, item in manifest.items.items() if item.entity_kind}
        for key, spec in manifest.properties.items():
            if isinstance(spec.accepts, list) and set(spec.accepts) & kinds:
                assert "unclassified_entity" in spec.accepts, key
        # a vocabulary list takes no entity, so no escape hatch either
        for key in ("sex_or_gender", "object_has_role", "civil_status"):
            accepts = manifest.properties[key].accepts
            assert "unclassified_entity" not in accepts, key

    def test_the_special_forms(self, manifest):
        properties = manifest.properties
        assert properties["depicts"].accepts == "any"
        assert properties["said_to_be_the_same_as"].accepts == "subject"
        assert properties["instance_of"].accepts == "class"
        assert properties["subclass_of"].accepts == "class"

    @staticmethod
    def raw_with(key: str, spec: dict) -> dict:
        raw = minimal_raw()
        raw["properties"][key] = {
            "label": key.replace("_", " "),
            "description": "d",
            "datatype": "wikibase-item",
            **spec,
        }
        return raw

    def test_the_loader_fails_closed_on_an_item_property_without_kinds(self):
        with pytest.raises(ValidationError, match="declares no accepts"):
            Manifest.model_validate(self.raw_with("location", {}))

    def test_a_retired_property_needs_none(self):
        manifest = Manifest.model_validate(
            self.raw_with("spouse", {"retired": True})
        )
        assert manifest.properties["spouse"].retired
        Manifest.model_validate(
            self.raw_with("spouse", {"retired": True, "accepts": []})
        )

    def test_a_retired_property_accepts_nothing(self):
        raw = self.raw_with(
            "spouse", {"retired": True, "accepts": ["artifact"]}
        )
        with pytest.raises(ValidationError, match="accepts nothing"):
            Manifest.model_validate(raw)

    def test_only_a_retired_property_accepts_nothing(self):
        with pytest.raises(ValidationError, match="only a retired"):
            Manifest.model_validate(self.raw_with("location", {"accepts": []}))

    def test_an_unknown_kind_is_refused(self):
        raw = self.raw_with("location", {"accepts": ["no_such_key"]})
        with pytest.raises(ValidationError, match="not an item key"):
            Manifest.model_validate(raw)

    def test_a_real_kind_without_the_escape_hatch_is_refused(self):
        raw = self.raw_with("location", {"accepts": ["place"]})
        raw["items"]["place"] = {
            "label": "place",
            "description": "d",
            "entity_kind": True,
        }
        with pytest.raises(ValidationError, match="unclassified_entity"):
            Manifest.model_validate(raw)
        raw["items"]["unclassified_entity"] = {
            "label": "unclassified entity",
            "description": "d",
            "entity_kind": True,
        }
        raw["properties"]["location"]["accepts"] = [
            "place",
            "unclassified_entity",
        ]
        Manifest.model_validate(raw)

    def test_only_an_item_property_declares_kinds(self):
        raw = minimal_raw()
        raw["properties"]["page_number"] = {
            "label": "page number",
            "description": "d",
            "datatype": "string",
            "accepts": ["artifact"],
        }
        with pytest.raises(ValidationError, match="only an item-valued"):
            Manifest.model_validate(raw)
        del raw["properties"]["page_number"]["accepts"]
        raw["properties"]["page_number"]["retired"] = True
        with pytest.raises(ValidationError, match="only an item-valued"):
            Manifest.model_validate(raw)


class TestVocabulary:
    def test_adr_0036s_classes(self, manifest):
        items = manifest.items
        assert items["partnership"].subclass_of is None
        assert items["marriage"].subclass_of == "partnership"
        assert items["marriage"].wikidata == "Q8445"
        assert items["wedding"].subclass_of == "event"
        for key in ("civil_wedding", "religious_wedding"):
            assert items[key].subclass_of == "wedding", key
        for key in ("divorce", "annulment"):
            assert items[key].subclass_of == "event", key
        for key in (
            "partnership",
            "marriage",
            "wedding",
            "civil_wedding",
            "religious_wedding",
            "divorce",
            "annulment",
        ):
            assert items[key].entity_kind, key

    def test_spouse_is_retired(self, manifest):
        spouse = manifest.properties["spouse"]
        assert spouse.retired and spouse.accepts == []
        assert spouse.wikidata == "P26"
        # the role of that name is a vocabulary item, not a kind
        assert not manifest.items["spouse"].entity_kind

    def test_the_claim_structure_properties(self, manifest):
        properties = manifest.properties
        assert properties["object_has_role"].wikidata == "P3831"
        assert properties["issued_by"].wikidata == "P2378"
        for key in ("degree", "removes"):
            assert properties[key].datatype == "quantity", key
        assert properties["judgement_note"].datatype == "string"
        for key in (
            "object_has_role",
            "kinship_class",
            "side",
            "issued_by",
            "carried_out_by",
            "civil_status",
            "judgement",
            "started_by",
            "ended_by",
        ):
            assert properties[key].datatype == "wikibase-item", key

    def test_vocabulary_values_are_no_entity_kinds(self, manifest):
        for key in (
            ACCEPTS["object_has_role"]
            | ACCEPTS["kinship_class"]
            | ACCEPTS["side"]
            | ACCEPTS["civil_status"]
            | ACCEPTS["judgement"]
        ):
            assert not manifest.items[key].entity_kind, key

    def test_no_prompt_offers_a_retired_property(self):
        properties, items = vocabulary_prompt_lines()
        keys = {line.split(":")[0].strip() for line in properties.splitlines()}
        assert "spouse" not in keys
        # offered now that a claim can name its own document as subject
        assert "issued_by" in keys
        assert {"object_has_role", "civil_status", "started_by"} <= keys
        kinds = entity_kind_prompt_lines()[0].split(", ")
        assert {"partnership", "wedding", "divorce"} <= set(kinds)

    def test_the_prompt_schema_leaves_records_out(self):
        assert "structure" not in claim_schema_prompt_text()
        assert "PersonStructure" not in claim_schema_prompt_text()


# -- validation ---------------------------------------------------------------


def entity(id_: str, label: str, kind: str, **fields) -> dict:
    return {"id": id_, "label": label, "kind": kind, **fields}


def local(id_: str) -> dict:
    return {"kind": "local", "local_id": id_}


def vocab(key: str) -> dict:
    return {"type": "item", "value": {"kind": "vocab", "key": key}}


def item(id_: str) -> dict:
    return {"type": "item", "value": local(id_)}


def qualifier(prop: str, value: dict) -> dict:
    return {"property": prop, "value": value}


def claim(subject: str, prop: str, value: dict, *qualifiers: dict) -> dict:
    return {
        "subject": local(subject),
        "property": prop,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": {"artifact": {"kind": "artifact", "sha1": SHA1}},
    }


# The divorce judgment (1957) and the documents around it: Leopold and
# Agnes, their marriage, its civil wedding, the divorce the Landgericht
# Rekau carried out, the court prison where Leopold was held, and Rudolf's
# 1958 letter's kin.
ENTITIES = [
    entity("leopold", "Leopold Löbig", "human"),
    entity("agnes", "Agnes Löbig", "human"),
    entity("rudolf", "Рудольф", "human"),
    entity("sebastian", "Sebastian Löbig", "human"),
    entity("klara", "Klara Löbig", "human"),
    entity("marriage", "Leopold and Agnes's marriage", "marriage"),
    entity("wedding", "the wedding before the registrar", "civil_wedding"),
    entity("divorce", "the divorce", "divorce"),
    entity("court", "Landgericht Rekau", "organization"),
    entity("prison", "Gerichtsgefängnis Peiltal", "place"),
    entity("imprisonment", "Leopold's imprisonment", "imprisonment"),
    entity("floerburg", "Flörburg", "place"),
    entity("standesamt", "Standesamt Flörburg", "organization"),
    entity("gustav", "Gustav Löbig", "human"),
    entity("birth", "Gustav's birth", "birth"),
    entity("cecilie", "Cäcilie Löbig", "human"),
    entity("death", "Cäcilie's death", "death"),
    entity("eugen", "Eugen Löbig", "human"),
    entity("hint", "RMS Flund", "unclassified_entity", type_hint="ship"),
]


def problems(*claims: dict, **kwargs) -> list[str]:
    batch = ClaimBatch.model_validate(
        {"extraction_run": RUN, "claims": list(claims), "entities": ENTITIES}
    )
    return validate_against_manifest(batch, **kwargs)


class TestTheRelationDecidesTheKind:
    @pytest.mark.parametrize(
        "claim_",
        [
            # ADR-0035 §2.3: the court acts, so it is an organisation
            claim("divorce", "carried_out_by", item("court")),
            # the court prison is where Leopold was held: a place
            claim("imprisonment", "location", item("prison")),
            # the register office issued the birth register extract, the
            # claim's own source document and so its subject
            claim("birth", "issued_by", item("standesamt"))
            | {"subject": {"kind": "artifact", "sha1": SHA1}},
            # the escape hatch goes wherever a real kind does
            claim("leopold", "residence", item("hint")),
            claim("marriage", "started_by", item("wedding")),
            claim("marriage", "ended_by", item("divorce")),
            claim("marriage", "ended_by", item("death")),
            claim(
                "marriage",
                "participant",
                item("leopold"),
                qualifier("object_has_role", vocab("spouse")),
            ),
            claim("leopold", "instance_of", vocab("human")),
            claim("leopold", "said_to_be_the_same_as", item("gustav")),
            claim("leopold", "depicts", item("prison")),
            claim("leopold", "sex_or_gender", vocab("male")),
        ],
    )
    def test_a_value_of_an_accepted_kind_stands(self, claim_):
        assert problems(claim_) == []

    @pytest.mark.parametrize(
        ("claim_", "prop", "kind"),
        [
            # a court that acts is never a place
            (
                claim("divorce", "carried_out_by", item("prison")),
                "carried_out_by",
                "place",
            ),
            # a place of imprisonment is never an organisation
            (
                claim("imprisonment", "location", item("court")),
                "location",
                "organization",
            ),
            (
                claim("leopold", "residence", item("gustav")),
                "residence",
                "human",
            ),
            # a participant claim points from the event to the person,
            # never from the person to the event
            (
                claim("gustav", "participant", item("birth")),
                "participant",
                "birth",
            ),
            (
                claim("marriage", "started_by", item("divorce")),
                "started_by",
                "divorce",
            ),
            (
                claim("marriage", "ended_by", item("birth")),
                "ended_by",
                "birth",
            ),
            (
                claim("leopold", "sex_or_gender", vocab("circa")),
                "sex_or_gender",
                "circa",
            ),
            (
                claim("leopold", "civil_status", vocab("female")),
                "civil_status",
                "female",
            ),
        ],
    )
    def test_a_value_of_another_kind_is_refused(self, claim_, prop, kind):
        (problem,) = problems(claim_)
        assert f"property {prop!r} does not accept a {kind!r}" in problem

    @pytest.mark.parametrize(
        ("claim_", "prop", "kind"),
        [
            (claim("gustav", "father", vocab("human")), "father", "human"),
            (
                claim("birth", "participant", vocab("human")),
                "participant",
                "human",
            ),
            (
                claim("imprisonment", "location", vocab("place")),
                "location",
                "place",
            ),
            (
                claim("divorce", "carried_out_by", vocab("organization")),
                "carried_out_by",
                "organization",
            ),
            (
                claim("marriage", "ended_by", vocab("death")),
                "ended_by",
                "death",
            ),
        ],
    )
    def test_a_kind_named_as_a_class_is_no_entity_of_it(
        self, claim_, prop, kind
    ):
        # a vocabulary ref to an entity kind names the class itself, never
        # a person, a place or an event of that kind
        (problem,) = problems(claim_)
        assert (
            f"property {prop!r} accepts an entity of kind {kind!r}, not the "
            f"class itself" in problem
        )

    def test_a_qualifier_is_checked_too(self):
        (problem,) = problems(
            claim(
                "birth",
                "participant",
                item("gustav"),
                qualifier("object_has_role", vocab("circa")),
            )
        )
        assert "qualifiers[0].value" in problem
        assert "'object_has_role' does not accept a 'circa'" in problem

    def test_a_class_is_no_entity(self):
        (problem,) = problems(claim("leopold", "instance_of", item("court")))
        assert "accepts a class" in problem

    def test_the_same_as_takes_the_subjects_own_kind(self):
        (problem,) = problems(
            claim("floerburg", "said_to_be_the_same_as", item("standesamt"))
        )
        assert "subject's own kind 'place'" in problem

    def test_an_existing_item_is_not_checked(self):
        existing = {"type": "item", "value": {"kind": "qid", "qid": "Q42"}}
        assert problems(claim("leopold", "residence", existing)) == []

    def test_a_ref_outside_a_manifest_keys_on_its_own_type(self):
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": [
                    {
                        **claim("leopold", "residence", item("x")),
                        "subject": {
                            "kind": "local",
                            "local_id": "leopold",
                            "instance_of": "human",
                        },
                        "value": {
                            "type": "item",
                            "value": {
                                "kind": "local",
                                "local_id": "gustav",
                                "instance_of": "human",
                            },
                        },
                    }
                ],
            }
        )
        (problem,) = validate_against_manifest(batch)
        assert "does not accept a 'human'" in problem

    def test_a_refusal_is_a_counted_reject(self):
        valid, rejects = salvage_valid_claims(
            [
                claim("imprisonment", "location", item("prison")),
                claim("imprisonment", "location", item("court")),
            ],
            RUN,
            entities=ENTITIES,
        )
        assert len(valid.claims) == 1
        (reject,) = rejects
        assert reject["claim"]["value"]["value"]["local_id"] == "court"
        assert "does not accept" in reject["problems"][0]


class TestRetiredProperty:
    def test_a_spouse_claim_is_refused(self):
        (problem,) = problems(claim("leopold", "spouse", item("agnes")))
        assert "'spouse' is retired" in problem

    def test_whatever_its_value(self):
        existing = {"type": "item", "value": {"kind": "qid", "qid": "Q42"}}
        (problem,) = problems(claim("leopold", "spouse", existing))
        assert "is retired" in problem

    def test_even_one_of_another_datatype(self):
        # refused as retired before its value is read, so the refusal is
        # the one problem, never a datatype mismatch
        text = {"type": "string", "value": "Agnes"}
        (problem,) = problems(claim("leopold", "spouse", text))
        assert "is retired" in problem

    def test_and_salvage_counts_it(self):
        valid, rejects = salvage_valid_claims(
            [claim("leopold", "spouse", item("agnes"))], RUN, entities=ENTITIES
        )
        assert valid.claims == []
        assert "is retired" in rejects[0]["problems"][0]

    def test_the_acceptance_names_it(self):
        acceptance = load_acceptance()
        assert acceptance.retired == frozenset({"spouse"})
        assert "retired" in acceptance.refusal("spouse", "human")


class TestReferenceProperties:
    """ADR-0035 §4: a judgement note goes only in the extraction-run
    reference, as `stated in` goes only in a reference (ADR-0003)."""

    REFERENCES = {
        "stated_in",
        "inferred_from_extraction_run",
        "judgement",
        "judgement_note",
        "printed_page_number",
    }

    def test_the_manifest_marks_them(self, manifest):
        marked = {k for k, s in manifest.properties.items() if s.reference}
        assert marked == self.REFERENCES
        assert load_acceptance().references == frozenset(self.REFERENCES)

    def test_no_prompt_offers_them(self):
        properties, _ = vocabulary_prompt_lines()
        keys = {line.split(":")[0].strip() for line in properties.splitlines()}
        assert not keys & self.REFERENCES

    def test_a_note_is_refused_as_a_claim(self):
        (problem,) = problems(
            claim("leopold", "judgement", vocab("judgement_date_reading"))
        )
        assert "'judgement' is a reference property" in problem

    def test_and_as_a_qualifier(self):
        note = {"type": "string", "value": "the postmark read as 1938"}
        (problem,) = problems(
            claim(
                "leopold",
                "residence",
                item("prison"),
                qualifier("judgement_note", note),
            )
        )
        assert "qualifiers[0]" in problem
        assert "'judgement_note' is a reference property" in problem

    def test_and_salvage_counts_it(self):
        valid, rejects = salvage_valid_claims(
            [claim("leopold", "stated_in", item("prison"))],
            RUN,
            entities=ENTITIES,
        )
        assert valid.claims == []
        assert "reference property" in rejects[0]["problems"][0]


class TestClaimStructure:
    def test_participant_roles(self):
        # Gustav's birth register extract: the boy is its principal, and
        # Sebastian a parent and the informant, on one claim
        assert (
            problems(
                claim(
                    "birth",
                    "participant",
                    item("gustav"),
                    qualifier("object_has_role", vocab("principal")),
                ),
                claim(
                    "birth",
                    "participant",
                    item("sebastian"),
                    qualifier("object_has_role", vocab("parent")),
                    qualifier("object_has_role", vocab("informant")),
                ),
            )
            == []
        )

    def test_a_deaths_principal_is_never_its_informant(self):
        (problem,) = problems(
            claim(
                "death",
                "participant",
                item("cecilie"),
                qualifier("object_has_role", vocab("principal")),
                qualifier("object_has_role", vocab("informant")),
            )
        )
        assert "never also its informant" in problem

    def test_a_births_principal_may_be_its_informant(self):
        # only a death forbids it: a birth's principal cannot report it,
        # but no rule reads that from the roles
        assert (
            problems(
                claim(
                    "birth",
                    "participant",
                    item("gustav"),
                    qualifier("object_has_role", vocab("principal")),
                    qualifier("object_has_role", vocab("informant")),
                )
            )
            == []
        )

    def test_nor_across_two_claims(self):
        principal = claim(
            "death",
            "participant",
            item("cecilie"),
            qualifier("object_has_role", vocab("principal")),
        )
        informant = claim(
            "death",
            "participant",
            item("cecilie"),
            qualifier("object_has_role", vocab("informant")),
        )
        (problem,) = problems(principal, informant)
        assert problem.startswith("claims[1]:")
        # another person may report the death
        eugen = dict(informant, value=item("eugen"))
        assert problems(principal, eugen) == []

    @pytest.mark.parametrize(
        ("term", "kin_class", "side", "degree"),
        [
            # the 1958 letter, with Rudolf as the ego: Sebastian is his
            # "uncle on the father's side", Klara his first cousin
            ("дядя по отцу", "kin_aunt_or_uncle", "paternal", None),
            ("двоюродная сестра", "kin_cousin", None, 1),
            # the 1952 letter: Sipkje's mother is Nicholas's great-grandmother
            ("oerbeppe", "kin_grandparent", None, 1),
        ],
    )
    def test_kinship_classes(self, term, kin_class, side, degree):
        qualifiers = [
            qualifier("kinship_type", {"type": "string", "value": term}),
            qualifier("kinship_class", vocab(kin_class)),
        ]
        if side:
            qualifiers.append(qualifier("side", vocab(side)))
        if degree is not None:
            qualifiers.append(
                qualifier("degree", {"type": "quantity", "amount": degree})
            )
        assert (
            problems(claim("klara", "relative", item("rudolf"), *qualifiers))
            == []
        )

    def test_a_side_is_no_class(self):
        (problem,) = problems(
            claim(
                "sebastian",
                "relative",
                item("rudolf"),
                qualifier("kinship_class", vocab("paternal")),
            )
        )
        assert "'kinship_class' does not accept a 'paternal'" in problem

    def test_civil_status(self):
        # the 1958 letter's "вдова": Nina's civil status, widowed
        assert problems(claim("agnes", "civil_status", vocab("widowed"))) == []

    def test_a_judgement_category(self):
        acceptance = load_acceptance()
        assert (
            acceptance.refusal("judgement", "judgement_date_reading") is None
        )
        assert acceptance.refusal("judgement", "circa") is not None


# -- mention records ----------------------------------------------------------


def record(label: str, kind: str, structure: dict, *aliases: str):
    return EntityRecord.model_validate(
        {
            "id": "e1",
            "label": label,
            "kind": kind,
            "aliases": list(aliases),
            "structure": structure,
        }
    )


class TestMentionRecords:
    def test_a_surname_first_name_with_a_patronymic(self):
        # the 1941 death certificate writes Rudolf surname first
        rudolf = record(
            "Лёбиг Рудольф Феликсович",
            "human",
            {
                "kind": "human",
                "given": [{"written": "Рудольф", "standard": "Рудольф"}],
                "surnames": [{"written": "Лёбиг"}],
                "patronymics": ["Феликсович"],
            },
        )
        assert structure_problems(rudolf) == []
        assert rudolf.structure.surnames[0].type == "other"

    def test_a_married_name_and_a_particle(self):
        # the 1952 letter's "S. Faxridge-van 't Nijks": an initial, her
        # husband's surname and her own, its particle kept whole
        sipkje = record(
            "S. Faxridge-van 't Nijks",
            "human",
            {
                "kind": "human",
                "given": [{"written": "S.", "initial": True}],
                "surnames": [
                    {"written": "Faxridge", "type": "married"},
                    {"written": "van 't Nijks", "type": "birth"},
                ],
            },
            "Sipkje",
        )
        assert structure_problems(sipkje) == []

    def test_an_initial_gives_no_standard_form(self):
        sipkje = record(
            "S. Faxridge-van 't Nijks",
            "human",
            {
                "kind": "human",
                "given": [
                    {"written": "S.", "initial": True, "standard": "Sipkje"}
                ],
            },
        )
        (problem,) = structure_problems(sipkje)
        assert problem.startswith("initial:")

    def test_a_standard_form_may_be_what_no_form_writes(self):
        # the 2008 family history: "Gus to everyone" is Gustave
        gus = record(
            "Gus",
            "human",
            {
                "kind": "human",
                "given": [{"written": "Gus", "standard": "Gustave"}],
            },
        )
        assert structure_problems(gus) == []

    def test_no_part_is_added(self):
        # a record that gives the naturalisation's Gustave the surname
        # Sörne, which no form of his holds, is set aside
        gustave = record(
            "Gustave LOBIG",
            "human",
            {
                "kind": "human",
                "given": [{"written": "Gustave"}],
                "surnames": [{"written": "LOBIG"}, {"written": "Sörne"}],
            },
            "Gustav LÖBIG",
        )
        (problem,) = structure_problems(gustave)
        assert problem.startswith("addition:") and "Sörne" in problem

    def test_parts_compare_folded_as_whole_words(self):
        folded = record(
            "LÖBIG, Ida, née SÖRNE",
            "human",
            {
                "kind": "human",
                "given": [{"written": "ida"}],
                "surnames": [
                    {"written": "Löbig"},
                    {"written": "Sorne", "type": "birth"},
                ],
            },
        )
        assert structure_problems(folded) == []
        # "Id" is inside "Ida", not a word of it
        partial = record(
            "LÖBIG, Ida",
            "human",
            {"kind": "human", "given": [{"written": "Id"}]},
        )
        assert structure_problems(partial)[0].startswith("addition:")

    def test_a_name_status_needs_a_name_part(self):
        titled = record(
            "Frau Löbig",
            "human",
            {"kind": "human", "titles": ["Frau"]},
        )
        (problem,) = structure_problems(titled)
        assert problem.startswith("no name part:")
        # a kinship reference names no one, and needs no part
        father = record(
            "Dein Vater", "human", {"kind": "human", "status": "kinship"}
        )
        assert structure_problems(father) == []

    def test_a_place_with_its_containers(self):
        # the 1958 letter: a village, its district and its oblast
        village = record(
            "д. Кошновка Калдовского района Чадской области",
            "place",
            {
                "kind": "place",
                "names": [
                    {
                        "written": "д. Кошновка",
                        "toponym": "Кошновка",
                        "head": "д.",
                    }
                ],
                "within": ["Калдовского района", "Чадской области"],
            },
        )
        assert structure_problems(village) == []
        # the 1952 letter: a village in a municipality, a province, a country
        snylsjum = record(
            "Snylsjum, gemeente Drakeradiel, Fryslân, Nederlân",
            "place",
            {
                "kind": "place",
                "names": [{"written": "Snylsjum"}],
                "within": ["gemeente Drakeradiel", "Fryslân", "Nederlân"],
            },
        )
        assert structure_problems(snylsjum) == []

    def test_an_address(self):
        house = record(
            "8 Hawthorn Road, Stupfield, Victoria",
            "place",
            {
                "kind": "place",
                "address": {
                    "written": "8 Hawthorn Road",
                    "street": "Hawthorn Road",
                    "number": "8",
                },
                "within": ["Stupfield", "Victoria"],
            },
        )
        assert structure_problems(house) == []

    def test_a_toponym_status_needs_a_name_or_an_address(self):
        (problem,) = structure_problems(
            record("Lager", "place", {"kind": "place"})
        )
        assert problem.startswith("no toponym:")
        generic = record(
            "Lager", "place", {"kind": "place", "status": "generic"}
        )
        assert structure_problems(generic) == []

    def test_an_organisation_by_kind_and_place(self):
        # the 1941 certificate's register office, known by kind and place;
        # its places may come from its location claims, so they are not
        # held to its forms
        zags = record(
            "Калдовское районное бюро ЗАГС",
            "organization",
            {
                "kind": "organization",
                "kinds": ["районное бюро ЗАГС"],
                "places": ["Калдовский район"],
            },
        )
        assert structure_problems(zags) == []
        brickworks = record(
            "tichelwurk fan Snylsjum",
            "organization",
            {"kind": "organization", "names": ["Snylsjum Tichelwurk"]},
        )
        assert structure_problems(brickworks)[0].startswith("addition:")

    def test_a_familys_surname_may_end_inside_a_word(self):
        loebigs = record(
            "the Löbigs", "family", {"kind": "family", "surnames": ["Löbig"]}
        )
        assert structure_problems(loebigs) == []
        # a person's surname may not
        person = record(
            "the Löbigs",
            "human",
            {"kind": "human", "surnames": [{"written": "Löbig"}]},
        )
        assert structure_problems(person)[0].startswith("addition:")

    def test_the_variant_matches_the_kind(self):
        (problem,) = structure_problems(
            record(
                "Flörburg",
                "human",
                {"kind": "place", "names": [{"written": "Flörburg"}]},
            )
        )
        assert problem.startswith("kind:")

    def test_the_record_is_strict(self):
        with pytest.raises(ValidationError):
            PersonStructure.model_validate(
                {"kind": "human", "status": "alias"}
            )
        with pytest.raises(ValidationError):
            PersonStructure.model_validate(
                {
                    "kind": "human",
                    "surnames": [{"written": "X", "type": "maiden"}],
                }
            )
        with pytest.raises(ValidationError):
            PersonStructure.model_validate(
                {"kind": "human", "forename": "Ida"}
            )

    def test_an_entity_without_one_serialises_as_before(self):
        plain = EntityRecord(id="e1", label="Ida Löbig", kind="human")
        assert "structure" not in json.loads(plain.model_dump_json())
        assert "structure" not in plain.model_dump()


class TestMoreParts:
    """Every part of a record is held to the forms, not only names."""

    @pytest.mark.parametrize(
        ("kind", "structure"),
        [
            (
                "place",
                {"kind": "place", "address": {"written": "9 Hawthorn Road"}},
            ),
            (
                "place",
                {
                    "kind": "place",
                    "address": {
                        "written": "8 Hawthorn Road",
                        "street": "Elm Road",
                    },
                },
            ),
            (
                "place",
                {
                    "kind": "place",
                    "address": {"written": "8 Hawthorn Road", "number": "9"},
                },
            ),
            (
                "place",
                {
                    "kind": "place",
                    "names": [{"written": "Stupfield"}],
                    "within": ["Tasmania"],
                },
            ),
            (
                "place",
                {
                    "kind": "place",
                    "names": [{"written": "Stupfield", "toponym": "Snylsjum"}],
                },
            ),
            (
                "place",
                {
                    "kind": "place",
                    "names": [{"written": "Stupfield", "head": "Lager"}],
                },
            ),
            (
                "organization",
                {"kind": "organization", "kinds": ["Standesamt"]},
            ),
            (
                "human",
                {
                    "kind": "human",
                    "given": [{"written": "Ida"}],
                    "titles": ["Dr."],
                },
            ),
            (
                "human",
                {
                    "kind": "human",
                    "given": [{"written": "Ida"}],
                    "suffixes": ["Wwe."],
                },
            ),
            ("human", {"kind": "human", "unsplit": ["Löbig Ursula"]}),
            ("human", {"kind": "human", "patronymics": ["Феликсович"]}),
            ("human", {"kind": "human", "nicknames": ["Peppi"]}),
        ],
    )
    def test_a_part_no_form_holds_is_an_addition(self, kind, structure):
        forms = {
            "place": "8 Hawthorn Road, Stupfield, Victoria",
            "organization": "Hutmacherei zum Blauen Hut",
            "human": "Ida Löbig",
        }
        (problem,) = structure_problems(record(forms[kind], kind, structure))
        assert problem.startswith("addition:")

    def test_a_multi_word_part_matches_whole(self):
        # its last word matches, its first does not
        (problem,) = structure_problems(
            record(
                "Ida Löbig",
                "human",
                {"kind": "human", "unsplit": ["Anna Löbig"]},
            )
        )
        assert problem.startswith("addition:")

    def test_a_part_may_come_from_an_alias_alone(self):
        ida = record(
            "Ida Löbig",
            "human",
            {
                "kind": "human",
                "given": [{"written": "Ida"}],
                "nicknames": ["Peppi"],
                "surnames": [{"written": "Sörne", "type": "birth"}],
            },
            "Peppi",
            "Ida Sörne",
        )
        assert structure_problems(ida) == []

    def test_a_nickname_outside_the_label_and_aliases_is_an_addition(self):
        """A record nickname must stand in the label or an alias. What one
        person calls another is `referred_to_as`, never an alias (ADR-0018,
        #434), so beside a label that names the person it cannot be a
        nickname: Nicholas's "Nana Maisie" is no name form of Maisie
        Cutmore."""
        maisie = record(
            "Maisie Cutmore",
            "human",
            {
                "kind": "human",
                "given": [{"written": "Maisie"}],
                "surnames": [{"written": "Cutmore", "type": "other"}],
                "nicknames": ["Nana Maisie"],
            },
        )
        assert structure_problems(maisie) == [
            "addition: 'Nana Maisie' occurs in neither the label nor an alias"
        ]

    def test_an_unsplit_form_is_a_name_part(self):
        # the 1912 directory's "Löbig Ursula" when its order is unsettled
        ursula = record(
            "Löbig Ursula",
            "human",
            {"kind": "human", "unsplit": ["Löbig Ursula"]},
        )
        assert structure_problems(ursula) == []

    def test_the_loader_refuses_a_kind_accepted_twice(self):
        raw = TestAcceptedKinds.raw_with(
            "image", {"accepts": ["artifact", "artifact"]}
        )
        with pytest.raises(ValidationError, match="repeats a kind"):
            Manifest.model_validate(raw)


class TestRecordsAreNeverWritten:
    """ADR-0035 §2.6: a record is extraction's reading, never an
    assertion, so nothing the writer sends depends on it."""

    # the modules that write to the wiki or resolve what it writes
    WRITE_PATH = ("writer.py", "resolve.py", "registrar.py")

    def test_no_write_path_module_names_a_record(self):
        import re
        from pathlib import Path

        import yizkr.pipeline

        folder = Path(yizkr.pipeline.__file__).parent
        for name in self.WRITE_PATH:
            text = (folder / name).read_text()
            assert not re.search(r"\bstructure\b", text), name
            assert "structure_problems" not in text, name

    def test_a_record_changes_nothing_the_writer_sends(self):
        from tests.unit.test_pipeline import TREE_SHA1, FakeWiki, _tree_batch
        from yizkr.claims import GivenName, Surname
        from yizkr.pipeline.writer import BatchWriter

        def sent(batch):
            """What the writer sends, but the run item's label, which ends
            in a fingerprint of the whole batch, the record included."""
            wiki = FakeWiki({TREE_SHA1: "Q29"}, detached=True)
            BatchWriter(wiki).write_batch(batch)
            items = []
            for item in wiki.wbi.item.sent:
                label = item["labels"]["en"]["value"]
                if label.startswith("extraction run "):
                    label = label.rsplit(" ", 1)[0]
                items.append(dict(item, labels={"en": label}))
            return items

        plain = _tree_batch()
        sebastian = PersonStructure(
            given=[GivenName(written="Sebastian", standard="Sebastian")],
            surnames=[Surname(written="Löbig")],
            nicknames=["Wastl"],
        )
        recorded = plain.model_copy(
            update={
                "entities": [
                    entity.model_copy(update={"structure": sebastian})
                    for entity in plain.entities
                ]
            }
        )
        assert recorded.entities[0].structure is not None
        assert sent(recorded) == sent(plain)


class TestSettingAside:
    def test_a_record_that_stands_is_kept(self):
        ida = entity(
            "e1",
            "Ida Löbig",
            "human",
            structure={"kind": "human", "given": [{"written": "Ida"}]},
        )
        assert set_aside_structure(ida) == (ida, [])

    def test_a_record_that_adds_is_set_aside_and_the_entity_kept(self):
        ida = entity(
            "e1",
            "Ida Löbig",
            "human",
            aliases=["Peppi"],
            structure={
                "kind": "human",
                "given": [{"written": "Ida"}],
                "surnames": [{"written": "Sörne", "type": "birth"}],
            },
        )
        kept, reasons = set_aside_structure(ida)
        assert "structure" not in kept
        assert kept["aliases"] == ["Peppi"] and kept["kind"] == "human"
        assert reasons[0].startswith("addition:")
        EntityRecord.model_validate(kept)

    def test_a_record_that_does_not_parse_is_set_aside(self):
        ida = entity(
            "e1",
            "Ida Löbig",
            "human",
            structure={"kind": "human", "given": "Ida"},
        )
        kept, reasons = set_aside_structure(ida)
        assert "structure" not in kept
        assert reasons[0].startswith("schema: structure.human.given")

    def test_an_entity_invalid_without_its_record_is_left_alone(self):
        broken = {"id": "e1", "kind": "human", "structure": {"kind": "human"}}
        assert set_aside_structure(broken) == (broken, [])

    def test_an_entity_without_a_record_is_untouched(self):
        plain = entity("e1", "Ida Löbig", "human")
        assert set_aside_structure(plain) == (plain, [])


# -- the bootstrap ------------------------------------------------------------


class FakeEntities:
    """Wikibase entities held in memory for the bootstrap: write() gives a
    new one its id and counts every write."""

    def __init__(self, wiki: FakeBootstrapWiki, prefix: str) -> None:
        self.wiki = wiki
        self.prefix = prefix
        self.store: dict[str, tuple[object, str | None]] = {}

    def new(self, datatype: str | None = None):
        from wikibaseintegrator import WikibaseIntegrator

        wbi = WikibaseIntegrator()
        entity = (
            wbi.property.new(datatype=datatype)
            if self.prefix == "P"
            else wbi.item.new()
        )

        def write(summary=None, **_):
            self.wiki.writes += 1
            if entity.id is None:
                self.wiki.next_id += 1
                entity.id = f"{self.prefix}{self.wiki.next_id}"
            self.store[entity.id] = (entity, datatype)
            return entity

        entity.write = write
        return entity

    def get(self, entity_id: str):
        return self.store[entity_id][0]


class FakeBootstrapWiki:
    """Just enough of WikiClient for bootstrap_instance."""

    def __init__(self) -> None:
        self.writes = 0
        self.next_id = 0
        self.pages: dict[str, str] = {}
        self.wbi = type(
            "Wbi",
            (),
            {
                "property": FakeEntities(self, "P"),
                "item": FakeEntities(self, "Q"),
            },
        )()

    def snapshot(self) -> dict[str, dict]:
        return {
            entity_id: {
                "label": entity.labels.get("en").value,
                "datatype": datatype,
            }
            for store in (self.wbi.property.store, self.wbi.item.store)
            for entity_id, (entity, datatype) in store.items()
        }

    def label_index(self, snapshot=None):
        return WikiClient.label_index(self, snapshot)

    def edit_page(self, title: str, text: str, summary: str) -> None:
        self.pages[title] = text

    def superclasses(self, key: str, manifest: Manifest) -> set[str]:
        """The labels of the classes the item `key` is a subclass of."""
        properties, items = self.label_index()
        subclass = properties[manifest.properties["subclass_of"].label]
        entity = self.wbi.item.get(items[manifest.items[key].label])
        labels = {qid: label for label, qid in items.items()}
        return {
            labels[claim.mainsnak.datavalue["value"]["id"]]
            for claim in entity.claims.get(subclass) or []
        }


class TestBootstrap:
    def test_it_creates_the_new_vocabulary_once(self, manifest):
        wiki = FakeBootstrapWiki()
        first = bootstrap_instance(wiki, manifest, log=lambda *_: None)
        assert first["created"] == len(manifest.properties) + len(
            manifest.items
        )
        properties, items = wiki.label_index()
        for key in ACCEPTS:
            assert manifest.properties[key].label in properties, key
        for key in (
            "partnership",
            "wedding",
            "civil_wedding",
            "religious_wedding",
            "divorce",
            "annulment",
            "principal",
            "kin_cousin",
            "paternal",
            "widowed",
            "judgement_other",
        ):
            assert manifest.items[key].label in items, key
        assert wiki.superclasses("marriage", manifest) == {"partnership"}
        assert wiki.superclasses("civil_wedding", manifest) == {"wedding"}
        assert wiki.superclasses("divorce", manifest) == {"event"}
        page = wiki.pages[manifest.mapping_page]

        writes = wiki.writes
        second = bootstrap_instance(wiki, manifest, log=lambda *_: None)
        assert second == {
            "created": 0,
            "updated": 0,
            "unchanged": first["created"],
            "statements_added": 0,
            "statements_present": first["statements_added"],
        }
        assert wiki.writes == writes
        assert wiki.pages[manifest.mapping_page] == page
