"""Offline tests for deterministic name-alias hygiene (ADR-0022 §4)."""

from yizkr.pipeline.names import normalize_aliases, normalize_entity_dict


class TestNormalizeAliases:
    def test_drops_bare_given_and_initialism_keeps_nickname(self):
        # Case 2: "Sigmund" and "S. Gellerowski" are derivable from the label;
        # "Siggi" adds a token blocking can't recover from "sigmund".
        assert normalize_aliases(
            "Sigmund Gellerowski", ["Sigmund", "Siggi", "S. Gellerowski"]
        ) == ["Siggi"]

    def test_keeps_alternative_surnames(self):
        # Case 1: the label carries one surname; the OTHER attested surnames
        # add tokens the label lacks, so they survive (no given/family kind
        # asserted, no permutations composed).
        assert normalize_aliases(
            "Frida Hollfeld", ["Falk", "Gellerowski"]
        ) == ["Falk", "Gellerowski"]

    def test_keeps_compound_and_particle_surnames_whole(self):
        # a naive whitespace atomizer would shatter these into von/der/de/la;
        # the validator never splits, and none is a subset of the label.
        aliases = [
            "de la Cruz",
            "van der Berg",
            "Sadelman-Rauholz",
            "ben Yosef",
        ]
        assert normalize_aliases("Frida Hollfeld", aliases) == aliases

    def test_drops_label_equal_and_deduplicates(self):
        assert (
            normalize_aliases(
                "Rieke Gellerowski",
                ["Rieke Gellerowski", "rieke gellerowski", "Rieke", ""],
            )
            == []
        )

    def test_is_idempotent(self):
        label = "Sigmund Gellerowski"
        aliases = ["Sigmund", "Siggi", "S. Gellerowski", "van der Berg"]
        once = normalize_aliases(label, aliases)
        assert normalize_aliases(label, once) == once


class TestNormalizeEntityDict:
    def test_human_aliases_are_hygiened(self):
        cleaned = normalize_entity_dict(
            {
                "kind": "human",
                "label": "Sigmund Gellerowski",
                "aliases": ["Sigmund", "Siggi"],
            }
        )
        assert cleaned["aliases"] == ["Siggi"]

    def test_place_short_form_is_preserved(self):
        # "Berlin" is a genuine short place name, NOT a derivable personal
        # short-form — non-human entities pass through untouched.
        entity = {
            "kind": "place",
            "label": "Berlin, Germany",
            "aliases": ["Berlin"],
        }
        assert normalize_entity_dict(entity) == entity

    def test_missing_fields_do_not_raise(self):
        assert normalize_entity_dict({"kind": "human"}) == {
            "kind": "human",
            "aliases": [],
        }
