"""Offline tests for alias hygiene from mention records (ADR-0035 §8,
ADR-0022 §4 as amended; #409)."""

import unicodedata

import pytest

from yizkr.claims import (
    FamilyStructure,
    OrganizationStructure,
    PersonStructure,
    PlaceStructure,
    set_aside_structure,
)
from yizkr.pipeline.names import (
    _name_run,
    appellation_parts,
    appellations_of,
    normalize_aliases,
    normalize_entity_dict,
)


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

    def test_a_spelling_variant_in_capitals_stays(self):
        """'GUSTAV LOBIG' is the plain spelling in capitals: its parts are
        read in any case, so it keeps the spelling the label lacks."""
        record = person(
            given=[{"written": "Gustav"}],
            surnames=[{"written": "Löbig"}, {"written": "Lobig"}],
        )
        assert normalize_aliases("Gustav Löbig", ["GUSTAV LOBIG"], record) == [
            "GUSTAV LOBIG"
        ]

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

    def test_a_place_name_with_its_container_is_read_as_the_label(self):
        """A place's written name that is its label and one of its
        containers stands (ADR-0035 §5, #655), and its part is the label:
        an alias that holds the label is kept, as it was while the record
        was set aside, and one that holds only the container gives no
        part and is dropped."""
        entity = {
            "id": "e1",
            "kind": "place",
            "label": "Snylsjum",
            "aliases": ["it doarp Snylsjum", "Fryslân", "Snylsjum, Fryslân"],
            "structure": {
                "kind": "place",
                "names": [{"written": "Snylsjum, Fryslân"}],
                "within": [
                    {"name": "Fryslân", "evidence": "Snylsjum, Fryslân"}
                ],
            },
        }
        cleaned = normalize_entity_dict(entity)
        assert cleaned["aliases"] == ["it doarp Snylsjum", "Snylsjum, Fryslân"]
        assert cleaned["structure"] == entity["structure"]
        assert normalize_entity_dict(cleaned) == cleaned

    def test_only_a_name_with_its_container_is_read_as_the_label(self):
        """A second written name, no label and container, keeps its own
        part: the alias that alone spells it, a variant of the label, is
        kept."""
        entity = {
            "id": "e1",
            "kind": "place",
            "label": "Snylsjum",
            "aliases": ["Snylsum"],
            "structure": {
                "kind": "place",
                "names": [
                    {"written": "Snylsjum, Fryslân"},
                    {"written": "Snylsum"},
                ],
                "within": [
                    {"name": "Fryslân", "evidence": "Snylsjum, Fryslân"}
                ],
            },
        }
        assert normalize_entity_dict(entity)["aliases"] == ["Snylsum"]

    def test_missing_fields_do_not_raise(self):
        assert normalize_entity_dict({"kind": "human"}) == {
            "kind": "human",
            "aliases": [],
        }


# -- appellations (ADR-0018 as amended by #499) ------------------------------

RAHEL = person(given=[{"written": "Rahel"}], surnames=[{"written": "Löbig"}])
HILDEGARD = person(
    given=[{"written": "Hildegard"}], surnames=[{"written": "Löbig"}]
)


def rahel(label, aliases, structure=RAHEL) -> dict:
    return {
        "id": "e1",
        "kind": "human",
        "label": label,
        "aliases": aliases,
        "structure": structure.model_dump(),
    }


class TestAppellationAliases:
    def test_an_appellation_with_a_name_is_kept(self):
        """'Tante Rahel' gives no part the label lacks, so a plain alias
        would be derivable; as an appellation it stays."""
        assert normalize_aliases(
            "Rahel Löbig", ["Tante Rahel"], RAHEL, ["Tante Rahel"]
        ) == ["Tante Rahel"]

    def test_an_appellation_with_no_name_part_is_kept(self):
        """'Omama' gives no part at all, and 'Nana Maisie' none the label
        lacks: both stay as the appellations they are."""
        assert normalize_aliases(
            "Hildegard Löbig", ["Omama"], HILDEGARD, ["Omama"]
        ) == ["Omama"]
        maisie = person(
            given=[{"written": "Maisie"}], surnames=[{"written": "Cutmore"}]
        )
        assert normalize_aliases(
            "Maisie Cutmore", ["Nana Maisie"], maisie, ["Nana Maisie"]
        ) == ["Nana Maisie"]

    def test_an_unmarked_kinship_term_is_still_dropped(self):
        """The rule reads the claims, never the word: a kinship term no
        `referred_to_as` claim gives is dropped as before."""
        assert (
            normalize_aliases(
                "Hildegard Löbig", ["Omama", "Tante Hildegard"], HILDEGARD
            )
            == []
        )

    def test_a_role_label_and_a_reordering_are_still_dropped(self):
        """A role label nothing marks goes, and so does a derivable
        reordering even where it is marked: its words are all name
        parts, so the record's rules judge it."""
        assert normalize_aliases(
            "Rahel Löbig",
            ["INFORMANT", "Löbig, Rahel", "Tante Rahel"],
            RAHEL,
            ["Löbig, Rahel", "Tante Rahel"],
        ) == ["Tante Rahel"]

    def test_an_appellation_matches_as_the_instance_keeps_a_term(self):
        assert normalize_aliases(
            "Rahel Löbig", ["TANTE\nRahel"], RAHEL, ["Tante Rahel"]
        ) == ["TANTE\nRahel"]

    def test_an_unlisted_appellation_joins_a_persons_aliases(self):
        """A `referred_to_as` the manifest turn did not list is added,
        unless the label or the bag already holds it."""
        settled = normalize_entity_dict(
            rahel("Rahel Löbig", ["Tante Rahel"]),
            ["tante rahel", "Tantchen", "Tantchen"],
        )
        assert settled["aliases"] == ["Tante Rahel", "Tantchen"]

    @pytest.mark.parametrize(
        ("kind", "label", "alias", "structure"),
        [
            (
                "organization",
                "Hutmacherei zum Blauen Hut",
                "die Firma",
                OrganizationStructure(
                    names=["zum Blauen Hut"], kinds=["Hutmacherei"]
                ),
            ),
            (
                "place",
                "Flörburg",
                "dort",
                PlaceStructure(names=[{"written": "Flörburg"}]),
            ),
        ],
    )
    def test_marks_count_only_for_a_person(
        self, kind, label, alias, structure
    ):
        entity = {
            "id": "e1",
            "kind": kind,
            "label": label,
            "aliases": [alias],
            "structure": structure.model_dump(),
        }
        settled = normalize_entity_dict(entity, [alias])
        assert settled["aliases"] == []

    def test_a_recordless_entity_keeps_its_appellations(self):
        assert normalize_aliases("Rahel", ["Tante Rahel"], None, []) == [
            "Tante Rahel"
        ]


class TestAppellationLabel:
    def test_the_fullest_name_form_takes_the_label(self):
        """'Tante Rahel' as Rahel's label, with 'Rahel Löbig' and 'Rahel'
        beside it: the fullest form made only of name parts is the
        label, and the appellation stays an alias."""
        settled = normalize_entity_dict(
            rahel("Tante Rahel", ["Rahel", "Rahel Löbig"]), ["Tante Rahel"]
        )
        assert settled["label"] == "Rahel Löbig"
        assert settled["aliases"] == ["Tante Rahel"]

    def test_else_the_labels_own_name_words(self):
        """With no other form, the label's words that are name parts, in
        its order."""
        record = person(given=[{"written": "Rahel"}])
        settled = normalize_entity_dict(
            rahel("Tante Rahel", [], record), ["Tante Rahel"]
        )
        assert settled["label"] == "Rahel"
        assert settled["aliases"] == ["Tante Rahel"]

    def test_a_kinship_word_filed_as_a_title_changes_nothing(self):
        """A record that files 'Tante' as a title still stands, and the
        label is still taken from the name parts, never the title (as
        scratch run 2's two such records would be)."""
        record = person(given=[{"written": "Rahel"}], titles=["Tante"])
        settled = normalize_entity_dict(
            rahel("Tante Rahel", [], record), ["Tante Rahel"]
        )
        assert settled["label"] == "Rahel"
        assert settled["aliases"] == ["Tante Rahel"]

    def test_a_pet_name_label_gives_way_to_a_name(self):
        """'Pünktchen' is a record nickname, so made of name parts, but an
        appellation too: a name form that is none comes first."""
        klara = person(given=[{"written": "Klara"}], nicknames=["Pünktchen"])
        entity = rahel("Pünktchen", ["Klara"], klara)
        settled = normalize_entity_dict(entity, ["Pünktchen"])
        assert settled["label"] == "Klara"
        assert settled["aliases"] == ["Pünktchen"]
        # with no other form, the pet name is the record's only name
        alone = rahel("Pünktchen", [], person(nicknames=["Pünktchen"]))
        assert normalize_entity_dict(alone, ["Pünktchen"])["label"] == (
            "Pünktchen"
        )

    def test_a_label_no_claim_marks_is_left_alone(self):
        """A label holding a word the record leaves out (her birth name,
        which this record omits) is untouched: the rule fires on
        appellations only."""
        entity = rahel("Hildegard Laudler Löbig", ["Omama"], HILDEGARD)
        settled = normalize_entity_dict(entity, ["Omama"])
        assert settled["label"] == "Hildegard Laudler Löbig"
        assert settled["aliases"] == ["Omama"]
        unmarked = normalize_entity_dict(rahel("Tante Rahel", ["Rahel Löbig"]))
        assert unmarked["label"] == "Tante Rahel"

    @pytest.mark.parametrize("status", ["kinship", "courtesy", "role"])
    def test_only_a_name_record_renames(self, status):
        """A label whose record's status says it names no one keeps it,
        even beside a name form the record's parts make."""
        record = person(
            status=status,
            given=[{"written": "Hildegard"}],
            surnames=[{"written": "Löbig"}],
        )
        entity = rahel("Omama", ["Hildegard Löbig"], record)
        assert normalize_entity_dict(entity, ["Omama"])["label"] == "Omama"

    def test_a_record_that_does_not_stand_renames_nothing(self):
        """A record giving a part no form holds is set aside (ADR-0035
        §5): no record, no rename."""
        record = person(given=[{"written": "Klara"}])
        entity = rahel("Tante Rahel", [], record)
        settled = normalize_entity_dict(entity, ["Tante Rahel"])
        assert settled["label"] == "Tante Rahel"

    def test_a_name_inside_another_appellation_takes_the_label(self):
        """'Omama' with a record whose name only another appellation
        holds: the name-part run of 'Oma Hildegard' takes the label, and
        both appellations stay aliases."""
        record = person(given=[{"written": "Hildegard"}])
        entity = rahel("Omama", ["Oma Hildegard"], record)
        settled = normalize_entity_dict(entity, ["Omama", "Oma Hildegard"])
        assert settled["label"] == "Hildegard"
        assert settled["aliases"] == ["Omama", "Oma Hildegard"]

    def test_a_titled_form_gives_its_name_run(self):
        """'Frau Rahel Löbig' is no appellation and no pure name: its run
        'Rahel Löbig' beats the label's own 'Rahel'."""
        record = person(
            given=[{"written": "Rahel"}],
            surnames=[{"written": "Löbig"}],
            titles=["Frau"],
        )
        entity = rahel("Tante Rahel", ["Frau Rahel Löbig"], record)
        assert normalize_entity_dict(entity, ["Tante Rahel"])["label"] == (
            "Rahel Löbig"
        )

    def test_the_labels_own_run_beats_a_shorter_name(self):
        entity = rahel("Tante Rahel Löbig", ["Rahel"])
        assert normalize_entity_dict(entity, ["Tante Rahel Löbig"])[
            "label"
        ] == ("Rahel Löbig")

    def test_a_run_never_joins_across_a_marker(self):
        """'geb.' is no name part, so the run stops before it: never
        'Rahel Löbig geb. Tarle'."""
        record = person(
            given=[{"written": "Rahel"}],
            surnames=[{"written": "Löbig"}, {"written": "Tarle"}],
            titles=["Frau"],
        )
        entity = rahel("Tante Rahel", ["Frau Rahel Löbig geb. Tarle"], record)
        assert normalize_entity_dict(entity, ["Tante Rahel"])["label"] == (
            "Rahel Löbig"
        )

    @pytest.mark.parametrize(
        "aliases",
        [
            ["Rahel Löbig", "Löbig, Rahel"],
            ["Löbig, Rahel", "Rahel Löbig"],
            ["Rahel Löbig", "LÖBIG, Rahel"],
            # a decomposed umlaut is no punctuation: the inversion,
            # given first, still loses to its comma
            ["Löbig, Rahel", "Rahel Lo\u0308big"],
        ],
    )
    def test_a_register_inversion_never_takes_the_label(self, aliases):
        """A comma is no letter: the natural order wins the tie, and the
        inversion is dropped as derivable."""
        settled = normalize_entity_dict(
            rahel("Tante Rahel", aliases), ["Tante Rahel"]
        )
        assert unicodedata.normalize("NFC", settled["label"]) == "Rahel Löbig"
        assert settled["aliases"] == ["Tante Rahel"]

    def test_more_letters_win_a_tie_of_words(self):
        record = person(
            given=[{"written": "Hildegard"}],
            nicknames=["Hilde"],
            surnames=[{"written": "Löbig"}],
        )
        entity = rahel("Oma Hilde", ["Hilde Löbig", "Hildegard Löbig"], record)
        settled = normalize_entity_dict(entity, ["Oma Hilde"])
        assert settled["label"] == "Hildegard Löbig"

    def test_a_whole_form_beats_a_run_of_as_many_words(self):
        """'Hilde Löbig' is a whole name form, 'Hildegard Löbig' only a run
        inside 'Frau Hildegard Löbig': the whole form wins, letters
        aside."""
        record = person(
            given=[{"written": "Hildegard"}],
            nicknames=["Hilde"],
            surnames=[{"written": "Löbig"}],
            titles=["Frau"],
        )
        entity = rahel(
            "Oma Hilde", ["Frau Hildegard Löbig", "Hilde Löbig"], record
        )
        assert normalize_entity_dict(entity, ["Oma Hilde"])["label"] == (
            "Hilde Löbig"
        )

    def test_the_label_matches_as_the_instance_keeps_a_term(self):
        settled = normalize_entity_dict(
            rahel("Tante\nRahel", ["Rahel Löbig"]), ["Tante Rahel"]
        )
        assert settled["label"] == "Rahel Löbig"

    @pytest.mark.parametrize(
        ("label", "aliases", "structure", "marks"),
        [
            ("Tante Rahel", ["Rahel Löbig"], RAHEL, ["Tante Rahel"]),
            (
                "Omama",
                ["Oma Hildegard"],
                person(given=[{"written": "Hildegard"}]),
                ["Omama", "Oma Hildegard"],
            ),
            (
                "Tante Rahel",
                ["Frau Rahel Löbig geb. Tarle"],
                person(
                    given=[{"written": "Rahel"}],
                    surnames=[{"written": "Löbig"}, {"written": "Tarle"}],
                    titles=["Frau"],
                ),
                ["Tante Rahel"],
            ),
            ("Tante Rahel", ["Löbig, Rahel"], RAHEL, ["Tante Rahel"]),
        ],
        ids=["whole", "run", "geb", "inversion"],
    )
    def test_it_is_idempotent(self, label, aliases, structure, marks):
        """Settling a settled entity changes nothing, its record standing
        as the manifest parse left it."""
        once = normalize_entity_dict(rahel(label, aliases, structure), marks)
        assert once["label"] != label
        assert normalize_entity_dict(once, marks) == once

    @pytest.mark.parametrize(
        ("label", "aliases", "structure", "named"),
        [
            ("Tante Rahel [Löbig]", [], RAHEL, "Rahel"),
            ("Tante [Rahel] Löbig", [], RAHEL, "Rahel"),
            (
                "Omama",
                ["Oma Hilde (Hildegard Löbig)"],
                person(
                    given=[{"written": "Hildegard"}],
                    nicknames=["Hilde"],
                    surnames=[{"written": "Löbig"}],
                ),
                "Hildegard Löbig",
            ),
            (
                "Omama",
                ["Oma \u201eHildegard\u201c"],
                person(given=[{"written": "Hildegard"}]),
                "Hildegard",
            ),
            (
                "Tante Ida",
                ["Frau Ida Sörne (Löbig)"],
                person(
                    given=[{"written": "Ida"}],
                    surnames=[{"written": "Sörne"}, {"written": "Löbig"}],
                    titles=["Frau"],
                ),
                "Ida Sörne",
            ),
            # a whole form of name parts holding a bracket competes by
            # its runs
            ("Tante Rahel", ["Rahel (Löbig)"], RAHEL, "Rahel"),
        ],
        ids=["trailing", "inner", "gloss", "quotes", "register", "whole"],
    )
    def test_a_run_holds_no_unbalanced_bracket_or_quote(
        self, label, aliases, structure, named
    ):
        """A bracket or quote starts or ends a run, so the label never
        carries half of one; a gloss in brackets competes on its own."""
        entity = rahel(label, aliases, structure)
        settled = normalize_entity_dict(entity, [label])
        assert settled["label"] == named
        assert label in settled["aliases"]

    def test_the_records_own_hyphen_is_no_punctuation(self):
        """'Hans-Xxx' is how the record spells the given name: its hyphen
        weighs nothing, so the record's spelling, given first, keeps the
        tie."""
        record = person(
            given=[{"written": "Hans-Xxx"}], surnames=[{"written": "Löbig"}]
        )
        entity = rahel(
            "Onkel Hans", ["Hans-Xxx Löbig", "Hans Xxx Löbig"], record
        )
        assert normalize_entity_dict(entity, ["Onkel Hans"])["label"] == (
            "Hans-Xxx Löbig"
        )

    def test_a_name_fused_with_a_bracket_gives_no_candidate(self):
        """'Hilde(gard)' holds a bracket inside it, so it is no run word:
        with no candidate, the marked label stays."""
        record = person(given=[{"written": "Hilde(gard)"}])
        entity = rahel("Oma Hilde(gard)", [], record)
        settled = normalize_entity_dict(entity, ["Oma Hilde(gard)"])
        assert settled["label"] == "Oma Hilde(gard)"

    def test_a_form_with_no_word_gives_nothing(self):
        record = person(given=[{"written": "Rahel"}])
        settled = normalize_entity_dict(
            rahel("Tante Rahel", ["?"], record), ["Tante Rahel"]
        )
        assert settled["label"] == "Rahel"


class TestNameRun:
    # name words as the run compares them: folded, accents dropped
    WORDS = frozenset({"rahel", "lobig", "tarle"})

    def test_its_ends_are_trimmed(self):
        assert _name_run("Tante Rahel Löbig,", self.WORDS) == "Rahel Löbig"

    def test_the_first_longest_run_wins(self):
        assert (
            _name_run("Rahel, Tante Tarle Löbig geb. Rahel", self.WORDS)
            == "Tarle Löbig"
        )

    def test_a_right_single_quote_inside_a_word_stays(self):
        words = frozenset({"sipkje", "van", "t", "nijks"})
        assert _name_run("Mem Sipkje van \u2019t Nijks", words) == (
            "Sipkje van \u2019t Nijks"
        )


class TestPersonsOnly:
    def test_an_organisation_gains_no_unlisted_appellation(self):
        entity = {
            "id": "e1",
            "kind": "organization",
            "label": "Hutmacherei zum Blauen Hut",
            "aliases": [],
        }
        assert normalize_entity_dict(entity, ["die Firma"])["aliases"] == []

    def test_an_organisations_marked_name_is_no_appellation_part(self):
        entity = {
            "id": "e1",
            "kind": "organization",
            "label": "Hutmacherei zum Blauen Hut",
            "aliases": [],
            "structure": OrganizationStructure(
                names=["zum Blauen Hut"], kinds=["Hutmacherei"]
            ).model_dump(),
        }
        assert appellation_parts(entity, ["zum Blauen Hut"]) == 0

    def test_a_persons_marked_name_part_is_counted(self):
        record = RAHEL.model_copy(update={"nicknames": ["Pünktchen"]})
        entity = rahel("Rahel Löbig", ["Pünktchen"], record)
        assert appellation_parts(entity, ["Pünktchen"]) == 1


class TestAppellationsOf:
    def test_the_referred_to_as_values_by_local_id(self):
        def given(local_id, prop, text):
            return {
                "subject": {"kind": "local", "local_id": local_id},
                "property": prop,
                "value": {
                    "type": "monolingual",
                    "value": {"text": text, "language": "de"},
                },
            }

        claims = [
            given("e1", "referred_to_as", "Tante Rahel"),
            given("e1", "referred_to_as", " Tante Rahel "),
            given("e1", "nickname", "Pünktchen"),
            given("e2", "referred_to_as", "Omama"),
            {"property": "referred_to_as", "subject": "e3", "value": None},
        ]
        assert appellations_of(claims) == {
            "e1": ["Tante Rahel"],
            "e2": ["Omama"],
        }
