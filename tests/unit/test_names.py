"""Offline tests for deterministic name-alias hygiene (ADR-0022 §4)."""

from yizkr.pipeline.names import normalize_aliases, normalize_entity_dict


class TestNormalizeAliases:
    def test_drops_bare_given_and_initialism_keeps_nickname(self):
        # Case 2: "Sebastian" and "S. Löbig" are derivable from the label;
        # "Wastl" adds a token blocking can't recover from "sebastian".
        assert normalize_aliases(
            "Sebastian Löbig", ["Sebastian", "Wastl", "S. Löbig"]
        ) == ["Wastl"]

    def test_keeps_alternative_surnames(self):
        # Case 1: the label carries one surname; the OTHER attested surname
        # adds a token the label lacks, so it survives (no given/family kind
        # asserted, no permutations composed).
        assert normalize_aliases("Hildegard Laudler", ["Löbig"]) == ["Löbig"]

    def test_keeps_compound_and_particle_surnames_whole(self):
        # a naive whitespace atomizer would shatter these into von/der/de/la;
        # the validator never splits, and none is a subset of the label.
        aliases = [
            "de la Cruz",
            "van der Berg",
            "A-B",
            "ben Yosef",
        ]
        assert normalize_aliases("Hildegard Laudler", aliases) == aliases

    def test_drops_label_equal_and_deduplicates(self):
        assert (
            normalize_aliases(
                "Klara Löbig",
                ["Klara Löbig", "klara löbig", "Klara", ""],
            )
            == []
        )

    def test_is_idempotent(self):
        label = "Sebastian Löbig"
        aliases = ["Sebastian", "Wastl", "S. Löbig", "van der Berg"]
        once = normalize_aliases(label, aliases)
        assert normalize_aliases(label, once) == once


class TestNormalizeEntityDict:
    def test_human_aliases_are_hygiened(self):
        cleaned = normalize_entity_dict(
            {
                "kind": "human",
                "label": "Sebastian Löbig",
                "aliases": ["Sebastian", "Wastl"],
            }
        )
        assert cleaned["aliases"] == ["Wastl"]

    def test_place_short_form_is_preserved(self):
        # "Flörburg" is a genuine short place name, NOT a derivable personal
        # short-form — non-human entities pass through untouched.
        entity = {
            "kind": "place",
            "label": "Flörburg, Germany",
            "aliases": ["Flörburg"],
        }
        assert normalize_entity_dict(entity) == entity

    def test_missing_fields_do_not_raise(self):
        assert normalize_entity_dict({"kind": "human"}) == {
            "kind": "human",
            "aliases": [],
        }
