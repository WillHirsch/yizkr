"""Offline tests for alias hygiene from mention records (ADR-0035 §8,
ADR-0022 §4 as amended; #409)."""

from yizkr.claims import (
    FamilyStructure,
    OrganizationStructure,
    PersonStructure,
    PlaceStructure,
    set_aside_structure,
)
from yizkr.pipeline.names import normalize_aliases, normalize_entity_dict


def person(**parts) -> PersonStructure:
    return PersonStructure.model_validate(parts)


IDA = person(
    given=[{"written": "Ida"}],
    surnames=[{"written": "Löbig"}, {"written": "SÖRNE", "type": "birth"}],
    nicknames=["Peppi"],
    titles=["Frau"],
    suffixes=["Wwe."],
)


class TestPeople:
    def test_an_alias_whose_parts_the_label_gives_is_dropped(self):
        """'Löbig, Ida' and 'Frau Löbig' give no part the label lacks,
        whatever their order or their title; 'Ida Sörne' and 'Peppi' add
        one."""
        assert normalize_aliases(
            "Ida Löbig",
            [
                "Löbig, Ida",
                "Frau Löbig",
                "LÖBIG Ida Wwe.",
                "Ida Sörne",
                "Peppi",
            ],
            IDA,
        ) == ["Ida Sörne", "Peppi"]

    def test_an_alias_that_gives_no_part_is_dropped(self):
        """A kinship term or a role that slipped into the aliases names no
        one: 'Mutter', 'INFORMANT'."""
        assert (
            normalize_aliases("Ida Löbig", ["Mutter", "INFORMANT"], IDA) == []
        )

    def test_a_surname_with_a_particle_stays_whole(self):
        """The Frisian letter's 'S. Faxridge-van 't Nijks' keeps its
        surname whole: an alias giving it is kept, a bare particle is not
        a part."""
        sipkje = person(
            given=[{"written": "Sipkje"}, {"written": "S.", "initial": True}],
            surnames=[
                {"written": "Faxridge"},
                {"written": "van 't Nijks", "type": "birth"},
            ],
        )
        assert normalize_aliases(
            "Sipkje Faxridge",
            ["S. Faxridge-van 't Nijks", "van", "Sipkje van 't Nijks"],
            sipkje,
        ) == ["S. Faxridge-van 't Nijks", "Sipkje van 't Nijks"]

    def test_blanks_the_label_and_duplicates_are_dropped(self):
        assert normalize_aliases(
            "Klara Löbig",
            ["Klara Löbig", "klara löbig", "", "Pünktchen", "pünktchen"],
            person(given=[{"written": "Klara"}], nicknames=["Pünktchen"]),
        ) == ["Pünktchen"]

    def test_is_idempotent(self):
        aliases = ["Löbig, Ida", "Peppi", "Ida Sörne", "Mutter"]
        once = normalize_aliases("Ida Löbig", aliases, IDA)
        assert normalize_aliases("Ida Löbig", once, IDA) == once


class TestOtherKinds:
    def test_a_places_short_form_is_kept_and_a_reference_dropped(self):
        """The camp's 'Klurnau' is its name's toponym, kept for lookup;
        'dort' and a bare 'Lager' give no part."""
        camp = PlaceStructure.model_validate(
            {
                "names": [
                    {
                        "written": "Lager Klurnau",
                        "toponym": "Klurnau",
                        "head": "Lager",
                    }
                ]
            }
        )
        assert normalize_aliases(
            "Lager Klurnau", ["Klurnau", "dort", "Lager"], camp
        ) == ["Klurnau"]

    def test_an_address_is_a_part(self):
        address = PlaceStructure.model_validate(
            {
                "address": {
                    "written": "8 Hawthorn Road",
                    "street": "Hawthorn Road",
                    "number": "8",
                },
                "within": ["Stupfield"],
            }
        )
        assert normalize_aliases(
            "8 Hawthorn Road, Stupfield",
            ["Hawthorn Road", "Stupfield", "No. 8"],
            address,
        ) == ["Hawthorn Road"]

    def test_an_organisations_name_or_place_is_a_part(self):
        """'Standesamt Flörburg' names its kind and place: an alias with
        the place is kept, the kind alone is not."""
        office = OrganizationStructure.model_validate(
            {"kinds": ["Standesamt"], "places": ["Flörburg"]}
        )
        assert normalize_aliases(
            "Standesamt Flörburg",
            ["Standesamt zu Flörburg", "das Standesamt"],
            office,
        ) == ["Standesamt zu Flörburg"]

    def test_a_familys_surname_may_end_inside_a_word(self):
        family = FamilyStructure.model_validate({"surnames": ["Löbig"]})
        assert normalize_aliases(
            "Familie Löbig", ["the Löbigs", "the family"], family
        ) == ["the Löbigs"]


class TestNormalizeEntityDict:
    def entity(self, **extra) -> dict:
        return {
            "id": "e1",
            "kind": "human",
            "label": "Ida Löbig",
            "aliases": ["Löbig, Ida", "Peppi", "Mutter"],
            **extra,
        }

    def test_a_record_that_stands_cleans_the_aliases(self):
        record = person(
            given=[{"written": "Ida"}],
            surnames=[{"written": "Löbig"}],
            nicknames=["Peppi"],
        )
        cleaned = normalize_entity_dict(
            self.entity(structure=record.model_dump())
        )
        assert cleaned["aliases"] == ["Peppi"]

    def test_without_a_record_every_alias_stays(self):
        """Nothing says which word of a recordless entity's alias is a
        part, so none is read from its shape."""
        entity = self.entity()
        assert normalize_entity_dict(entity) == entity

    def test_a_record_that_does_not_stand_cleans_nothing(self):
        """A record that adds a part (Sörne and the title occur in no
        form here), one of another kind, or one that does not parse is no
        record to clean by."""
        for structure in (
            IDA.model_dump(),
            {"kind": "place", "names": [{"written": "Ida Löbig"}]},
            {"kind": "human", "status": "nonsense"},
        ):
            entity = self.entity(structure=structure)
            assert (
                normalize_entity_dict(entity)["aliases"] == entity["aliases"]
            )

    def test_standing_is_judged_once_before_the_cleanup(self):
        """ADR-0035 §5 before §8: Ida's record, titled Frau, stands on
        her alias bag as it comes. The cleanup drops 'Frau Löbig', the
        only form holding the title, and the record is kept: standing is
        never judged again on the cleaned bag, and a second cleaning
        changes nothing."""
        record = person(
            given=[{"written": "Ida"}],
            surnames=[{"written": "Löbig"}],
            titles=["Frau"],
            nicknames=["Peppi"],
        ).model_dump()
        entity = self.entity(structure=record)
        entity["aliases"] = ["Frau Löbig", "Peppi"]
        assert set_aside_structure(entity) == (entity, [])
        cleaned = normalize_entity_dict(entity)
        assert cleaned["aliases"] == ["Peppi"]
        assert cleaned["structure"] == record
        assert normalize_entity_dict(cleaned) == cleaned

    def test_missing_fields_do_not_raise(self):
        assert normalize_entity_dict({"kind": "human"}) == {
            "kind": "human",
            "aliases": [],
        }
