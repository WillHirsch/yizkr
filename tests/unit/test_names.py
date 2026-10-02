"""Offline tests for alias hygiene from mention records (ADR-0035 §8,
ADR-0022 §4 as amended; #409)."""

import pytest

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
        untitled = IDA.model_copy(update={"titles": [], "suffixes": []})
        assert normalize_aliases(
            "Ida Löbig",
            [
                "Löbig, Ida",
                "Frau Löbig",
                "LÖBIG Ida Wwe.",
                "Ida Sörne",
                "Peppi",
            ],
            untitled,
        ) == ["Ida Sörne", "Peppi"]

    def test_the_only_form_holding_a_title_or_suffix_stays(self):
        """Her record titles her Frau and gives the suffix Wwe.: 'Frau
        Löbig' is the first form holding the title and 'LÖBIG Ida Wwe.'
        the only one holding the suffix, so both stay, and a second form
        holding the title goes; a label holding the title keeps none for
        it."""
        aliases = [
            "Löbig, Ida",
            "Frau Löbig",
            "Frau Ida Löbig",
            "LÖBIG Ida Wwe.",
            "Ida Sörne",
            "Peppi",
        ]
        assert normalize_aliases("Ida Löbig", aliases, IDA) == [
            "Frau Löbig",
            "LÖBIG Ida Wwe.",
            "Ida Sörne",
            "Peppi",
        ]
        assert normalize_aliases("Frau Ida Löbig", aliases, IDA) == [
            "LÖBIG Ida Wwe.",
            "Ida Sörne",
            "Peppi",
        ]
        cleaned = normalize_aliases("Ida Löbig", aliases, IDA)
        assert normalize_aliases("Ida Löbig", cleaned, IDA) == cleaned

    @pytest.mark.parametrize(
        ("label", "variant", "given", "surnames"),
        [
            ("Gustav Löbig", "Gustav Lobig", ["Gustav"], ["Löbig", "Lobig"]),
            (
                "Cäcilie Tarle",
                "Cacilie Tarle",
                ["Cäcilie", "Cacilie"],
                ["Tarle"],
            ),
            (
                "Eugen Fläckfeld",
                "Eugen Flackfeld",
                ["Eugen"],
                ["Fläckfeld", "Flackfeld"],
            ),
            ("Ida Sörne", "Ida Sorne", ["Ida"], ["Sörne", "Sorne"]),
            (
                "Hans-Xxx Löbig",
                "Hans Xxx Löbig",
                ["Hans-Xxx", "Hans", "Xxx"],
                ["Löbig"],
            ),
        ],
    )
    def test_a_spelling_variant_is_never_derivable(
        self, label, variant, given, surnames
    ):
        """An alias that spells a part otherwise than the label does (an
        umlaut written plain, a hyphen as a space) keeps that spelling;
        a reorder and a case-only variant of the label still go."""
        record = person(
            given=[{"written": name} for name in given],
            surnames=[{"written": name} for name in surnames],
        )
        reordered = ", ".join(reversed(label.split(" ", 1)))
        aliases = [variant, reordered, reordered.upper(), label.upper()]
        assert normalize_aliases(label, aliases, record) == [variant]

    def test_a_variant_in_case_alone_is_derivable(self):
        """'XXX YYYSS' writes 'Xxx Yyyß' in capitals, where a capital ß is
        written SS: it differs from the label in case alone, which the
        spelling test folds, so it goes."""
        record = person(
            given=[{"written": "Xxx"}], surnames=[{"written": "Yyyß"}]
        )
        assert normalize_aliases("Xxx Yyyß", ["XXX YYYSS"], record) == []

    def test_the_rescue_prefers_a_holder_that_gives_a_name(self):
        """Of the forms holding her title, the one that also gives a name
        part stays ('Frau Löbig'), before a role phrase or a bare title;
        with none such, the first holder stays."""
        titled = IDA.model_copy(update={"suffixes": []})
        for aliases in (["Frau Mutter", "Frau Löbig"], ["Frau", "Frau Löbig"]):
            assert normalize_aliases("Ida Löbig", aliases, titled) == [
                "Frau Löbig"
            ]
        assert normalize_aliases("Ida Löbig", ["Frau Mutter"], titled) == [
            "Frau Mutter"
        ]

    def test_the_rescue_holds_a_title_as_whole_words(self):
        """'Drxxx Löbig' only begins with the title 'Dr.', and holds it
        not: 'Dr. Löbig' stays."""
        doctor = IDA.model_copy(update={"titles": ["Dr."], "suffixes": []})
        assert normalize_aliases(
            "Ida Löbig", ["Drxxx Löbig", "Dr. Löbig"], doctor
        ) == ["Dr. Löbig"]

    def test_the_rescue_keeps_one_holder_per_title(self):
        """A form holding both her titles keeps neither another holder;
        a kept alias that holds a title already keeps no other; and the
        titles and suffixes are taken in the order a form first holds
        them, so 'Gustav Löbig jun.' stays beside 'Dr. Gustav Löbig
        jun.'."""
        two = IDA.model_copy(
            update={"titles": ["Frau", "Dr."], "suffixes": []}
        )
        assert normalize_aliases(
            "Ida Löbig", ["Frau Dr. Löbig", "Dr. Löbig", "Frau Löbig"], two
        ) == ["Frau Dr. Löbig"]
        titled = IDA.model_copy(update={"suffixes": []})
        assert normalize_aliases(
            "Ida Löbig", ["Frau Ida Sörne", "Frau Löbig"], titled
        ) == ["Frau Ida Sörne"]
        gustav = person(
            given=[{"written": "Gustav"}],
            surnames=[{"written": "Löbig"}],
            titles=["Dr."],
            suffixes=["jun."],
        )
        assert normalize_aliases(
            "Gustav Löbig",
            ["Gustav Löbig jun.", "Dr. Gustav Löbig jun."],
            gustav,
        ) == ["Gustav Löbig jun.", "Dr. Gustav Löbig jun."]

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
        """ADR-0035 §5 before §8: the shop's record, of kind Hutmacherei,
        stands on its alias bag as it comes. The cleanup drops 'die
        Hutmacherei', which gives no name part and is the only form
        holding the kind, and the record is kept: standing is never
        judged again on the cleaned bag, and a second cleaning changes
        nothing."""
        record = {
            "kind": "organization",
            "names": ["zum Blauen Hut"],
            "kinds": ["Hutmacherei"],
        }
        entity = {
            "id": "e1",
            "kind": "organization",
            "label": "zum Blauen Hut",
            "aliases": ["die Hutmacherei"],
            "structure": record,
        }
        assert set_aside_structure(entity) == (entity, [])
        cleaned = normalize_entity_dict(entity)
        assert cleaned["aliases"] == []
        assert cleaned["structure"] == record
        assert normalize_entity_dict(cleaned) == cleaned

    def test_missing_fields_do_not_raise(self):
        assert normalize_entity_dict({"kind": "human"}) == {
            "kind": "human",
            "aliases": [],
        }
