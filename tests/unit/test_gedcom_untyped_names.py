"""Offline tests for the GEDCOM adapter's name claims of no stated type
(ADR-0011 as amended by #435; ADR-0035 §6).

A name form the export gives without stating its type (a bracketed part,
a NAME with no TYPE or an unrecognised one) becomes a `name` claim cited
to its NAME, beside its alias. Every name is from the example universe
(docs/EXAMPLES.md).
"""

import unicodedata
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, validate_against_manifest
from yizkr.pipeline.gedcom import adapt_gedcom

FIXTURE = Path(__file__).parent.parent / "fixtures" / "example-family.ged"
EXPORT = ArtifactRef.from_path(FIXTURE)


def person(name: str, *lines: str, head: tuple[str, ...] = ()) -> str:
    """Ida's record, with its first NAME and then (here) `lines`."""
    return "\n".join(
        [
            *(["0 HEAD", *head] if head else []),
            "0 @I7@ INDI",
            f"1 NAME {name}",
            *lines,
            "0 TRLR",
            "",
        ]
    )


def adapted(text: str):
    return adapt_gedcom(text, artifact=EXPORT)


def names(text: str, prop: str = "name") -> list[tuple[str, str]]:
    """Ida's `prop` claims, each as its text and its excerpt: the lines of
    the record it cites."""
    batch = adapted(text).batch
    return [
        (claim.value.value.text, claim.source.transcription_excerpt.text)
        for claim in batch.claims
        if claim.subject.local_id == "i7" and claim.property == prop
    ]


def texts(text: str, prop: str = "name") -> list[str]:
    return [found for found, _line in names(text, prop)]


def test_the_fixture_gives_peppi_as_a_name_cited_to_her_name():
    result = adapt_gedcom(FIXTURE.read_text(), artifact=EXPORT)
    labels = {entity.id: entity.label for entity in result.batch.entities}
    found = [
        (labels[claim.subject.local_id], claim.value.value.text)
        for claim in result.batch.claims
        if claim.property == "name"
    ]
    assert found == [("Ida Sörne", "Peppi")]


class TestBrackets:
    def test_a_bracketed_part_is_a_name_cited_to_its_name(self):
        text = person("Ida (Peppi) /Sörne/")
        assert names(text) == [("Peppi", "1 NAME Ida (Peppi) /Sörne/")]
        # beside its alias, and no nickname: the type is not stated
        (ida,) = adapted(text).batch.entities
        assert "Peppi" in ida.aliases
        assert texts(text, "nickname") == []

    def test_a_givns_bracketed_part_is_a_name(self):
        text = person("Ida /Sörne/", "2 GIVN Ida (Peppi)")
        assert texts(text) == ["Peppi"]

    def test_a_bracket_in_a_typed_name_is_a_name(self):
        # the TYPE types the name, not what its bracket holds
        text = person("Ida (Peppi) /Sörne/", "2 TYPE birth")
        assert texts(text) == ["Peppi"]
        assert texts(text, "birth_name") == ["Ida Sörne"]

    def test_each_bracket_is_a_name_of_its_own(self):
        text = person("Hildegard (Hilde) (geb. Laudler) /Löbig/")
        assert texts(text) == ["Hilde", "geb. Laudler"]

    def test_a_bracket_the_name_gives_as_a_nickname_too_is_no_name(self):
        # the NICK types Peppi, so the bracket adds no untyped claim
        text = person("Ida (Peppi) /Sörne/", "2 NICK Peppi")
        assert texts(text) == []
        assert texts(text, "nickname") == ["Peppi"]

    def test_a_bracket_another_name_types_is_no_name(self):
        # a further NAME's NICK types Peppi; that NAME's own form, of no
        # stated type, is a name
        text = person(
            "Ida (Peppi) /Sörne/", "1 NAME Ida /Löbig/", "2 NICK Peppi"
        )
        assert texts(text) == ["Ida Löbig"]
        assert texts(text, "nickname") == ["Peppi"]
        # an aka types its whole form, so the bracket's word stays a name
        text = person(
            "Ida (Peppi) /Sörne/", "1 NAME Peppi /Sörne/", "2 TYPE aka"
        )
        assert texts(text) == ["Peppi"]
        assert texts(text, "nickname") == ["Peppi Sörne"]
        # a quote types Peppi; the further NAME's whole form, quotes kept,
        # is of no stated type
        text = person("Ida (Peppi) /Sörne/", "1 NAME Ida 'Peppi' /Sörne/")
        assert texts(text) == ["Ida 'Peppi' Sörne"]
        assert texts(text, "nickname") == ["Peppi"]

    def test_a_name_given_only_in_brackets_gives_its_words(self):
        # the label keeps the brackets ("(Peppi)"); the name is the words
        text = person("(Peppi)")
        (ida,) = adapted(text).batch.entities
        assert ida.label == "(Peppi)"
        assert texts(text) == ["Peppi"]


class TestUntypedNames:
    def test_an_untyped_further_name_is_a_name(self):
        text = person("Ida /Sörne/", "1 NAME Ida /Löbig/")
        assert names(text) == [("Ida Löbig", "1 NAME Ida /Löbig/")]

    def test_hilde_loebig_as_an_untyped_further_name(self):
        text = "\n".join(
            [
                "0 @I4@ INDI",
                "1 NAME Hildegard /Laudler/",
                "2 TYPE birth",
                "1 NAME Hilde /Löbig/",
                "0 TRLR",
                "",
            ]
        )
        batch = adapted(text).batch
        found = [
            (claim.value.value.text, claim.source.transcription_excerpt.text)
            for claim in batch.claims
            if claim.property == "name"
        ]
        assert found == [("Hilde Löbig", "1 NAME Hilde /Löbig/")]

    def test_an_unrecognised_type_is_a_name(self):
        text = person("Ida /Sörne/", "1 NAME Ida /Löbig/", "2 TYPE other")
        assert texts(text) == ["Ida Löbig"]

    def test_a_name_without_slashes_and_no_type_is_a_name(self):
        text = person("Ida /Sörne/", "1 NAME Hilde Löbig")
        assert texts(text) == ["Hilde Löbig"]

    def test_the_labels_own_form_is_no_name(self):
        # the first NAME is the label, typed or not
        assert texts(person("Ida /Sörne/")) == []
        assert texts(person("Ida /Sörne/", "2 TYPE other")) == []
        # nor is a further NAME that restates it
        assert texts(person("Ida /Sörne/", "1 NAME Ida /Sörne/")) == []

    @pytest.mark.parametrize(
        "kind", ["birth", "maiden", "married", "aka", "official"]
    )
    def test_a_typed_name_is_no_name(self, kind):
        text = person("Ida /Sörne/", "1 NAME Ida /Löbig/", f"2 TYPE {kind}")
        assert texts(text) == []

    def test_a_typed_name_without_slashes_is_no_name(self):
        # the export types it, though the adapter makes no typed claim
        text = person("Ida /Sörne/", "1 NAME Peppi", "2 TYPE aka")
        assert texts(text) == []
        assert texts(text, "nickname") == []

    def test_a_form_a_marnm_types_is_no_name(self):
        text = person(
            "Ida /Sörne/", "1 NAME Ida /Löbig/", "1 _MARNM Ida /Löbig/"
        )
        assert texts(text) == []
        assert texts(text, "married_name") == ["Ida Löbig"]

    @pytest.mark.parametrize(
        "typing, further",
        [
            # a typed NAME with no slashes: no typed claim is made of it
            (("1 NAME Peppi", "2 TYPE aka"), "Peppi"),
            (("1 NAME Hilde Löbig", "2 TYPE married"), "Hilde Löbig"),
            # a `_MARNM` with no slashes: no married-name claim either
            (("1 _MARNM Ida Löbig",), "Ida Löbig"),
        ],
        ids=["aka-unsplit", "married-unsplit", "marnm-unsplit"],
    )
    def test_a_form_the_export_types_without_a_claim_is_no_name(
        self, typing, further
    ):
        # the source types the form, though the adapter makes no typed
        # claim of it, so a further untyped NAME giving it gives no name
        text = person("Ida /Sörne/", *typing, f"1 NAME {further}")
        assert texts(text) == []

    def test_a_bracket_the_export_types_without_a_claim_is_no_name(self):
        text = person("Ida (Peppi) /Sörne/", "1 NAME Peppi", "2 TYPE aka")
        assert texts(text) == []

    def test_a_typed_names_form_is_no_name_elsewhere(self):
        text = person(
            "Ida /Sörne/",
            "1 NAME Ida /Löbig/",
            "2 TYPE married",
            "1 NAME Ida /Löbig/",
        )
        assert texts(text) == []
        assert texts(text, "married_name") == ["Ida Löbig"]

    def test_an_akas_whole_form_is_no_name_elsewhere(self):
        text = person(
            "Ida /Sörne/",
            "1 NAME Peppi /Sörne/",
            "2 TYPE aka",
            "1 NAME Peppi /Sörne/",
        )
        assert texts(text) == []
        assert texts(text, "nickname") == ["Peppi Sörne"]

    def test_a_givns_quoted_part_is_no_name_elsewhere(self):
        text = person(
            "Ida /Sörne/",
            "2 GIVN Ida 'Peppi'",
            "1 NAME Ida (Peppi) /Löbig/",
        )
        assert texts(text) == ["Ida Löbig"]
        assert texts(text, "nickname") == ["Peppi"]

    def test_the_label_is_compared_in_nfc_whitespace_and_case_aside(self):
        decomposed = unicodedata.normalize("NFD", "Ida /Sörne/")
        assert decomposed != "Ida /Sörne/"
        for further in (decomposed, "Ida  /Sörne/", "IDA /SÖRNE/"):
            assert texts(person("Ida /Sörne/", f"1 NAME {further}")) == []
        # accents are kept: a spelling variant is a name of its own
        assert texts(person("Ida /Sörne/", "1 NAME Ida /Sorne/")) == [
            "Ida Sorne"
        ]

    def test_a_typed_form_is_compared_in_nfc(self):
        bracket = unicodedata.normalize("NFD", "Hilde Löbig")
        text = person(f"Hildegard ({bracket}) /Laudler/", "2 NICK Hilde Löbig")
        assert texts(text) == []
        assert texts(text, "nickname") == ["Hilde Löbig"]

    def test_a_bracket_holding_a_cue_is_a_name_verbatim(self):
        # code never types a name from its wording (ADR-0035): "geb." is
        # no stated type here, and a curator may type it later
        text = person("Hildegard (geb. Laudler) /Löbig/")
        assert texts(text) == ["geb. Laudler"]
        assert texts(text, "birth_name") == []

    def test_a_name_two_names_give_is_a_claim_of_each(self):
        # each NAME cites a source of its own, so neither citation is lost
        text = person(
            "Ida /Sörne/",
            "1 NAME Ida /Löbig/",
            "2 NOTE register",
            "1 NAME Ida /Löbig/",
            "2 NOTE letter",
        )
        assert [found for found, _excerpt in names(text)] == [
            "Ida Löbig",
            "Ida Löbig",
        ]


class TestPlaceholders:
    @pytest.mark.parametrize(
        "name", ["Eugen /?/", "Eugen (?) /Löbig/", "Eugen ? /Löbig/"]
    )
    def test_a_name_with_a_placeholder_gives_no_name(self, name):
        text = person("Ida /Sörne/", f"1 NAME {name}")
        assert texts(text) == []
        assert adapted(text).outcomes["untyped name form with a placeholder"]

    def test_a_bracket_with_a_placeholder_gives_no_name(self):
        text = person("Ida (Peppi ?) /Sörne/")
        assert texts(text) == []
        assert adapted(text).outcomes["untyped name form with a placeholder"]
        text = person("Ida /Sörne/", "2 GIVN Ida (Peppi ?)")
        assert texts(text) == []
        assert adapted(text).outcomes["untyped name form with a placeholder"]
        # in a typed NAME, whose display form gives no `name`, only the
        # unsure bracket is counted
        text = person("Ida (Peppi ?) /Sörne/", "2 TYPE birth")
        assert texts(text) == []
        assert (
            adapted(text).outcomes["untyped name form with a placeholder"] == 1
        )

    def test_a_complete_bracket_of_an_unsure_name_is_a_name(self):
        # the unsure display form 'Ida' gives none; the bracket is whole
        text = person("Ida /Sörne/", "1 NAME Ida (Peppi) /?/")
        assert texts(text) == ["Peppi"]
        assert (
            adapted(text).outcomes["untyped name form with a placeholder"] == 1
        )

    def test_a_typed_name_with_a_placeholder_counts_no_untyped_form(self):
        text = person("Ida /Sörne/", "1 NAME Ida /?/", "2 TYPE married")
        assert texts(text) == []
        outcomes = adapted(text).outcomes
        assert outcomes["typed NAME with a placeholder"] == 1
        assert "untyped name form with a placeholder" not in outcomes

    def test_a_nicks_placeholder_blocks_nothing(self):
        text = person("Ida (Peppi) /Sörne/", "2 NICK ?")
        assert texts(text) == ["Peppi"]
        assert "untyped name form with a placeholder" not in (
            adapted(text).outcomes
        )


class TestTheClaim:
    def test_its_language_is_the_exports(self):
        text = person("Ida (Peppi) /Sörne/", head=("1 LANG German",))
        (claim,) = [
            c for c in adapted(text).batch.claims if c.property == "name"
        ]
        assert claim.value.value.language == "de"
        text = person("Ida (Peppi) /Sörne/")
        (claim,) = [
            c for c in adapted(text).batch.claims if c.property == "name"
        ]
        assert claim.value.value.language == "und"

    def test_it_cites_the_export_and_validates(self):
        text = person("Ida (Peppi) /Sörne/", "1 NAME Hilde Löbig")
        batch = adapted(text).batch
        assert validate_against_manifest(batch) == []
        found = [c for c in batch.claims if c.property == "name"]
        assert len(found) == 2
        assert all(c.source.artifact == EXPORT for c in found)
        assert all(c.value.type == "monolingual" for c in found)

    def test_a_sources_citation_reaches_the_name(self):
        text = "\n".join(
            [
                "0 @S1@ SOUR",
                "1 TITL Löbig family tree",
                "0 @I7@ INDI",
                "1 NAME Ida /Sörne/",
                "1 NAME Ida /Löbig/",
                "2 SOUR @S1@",
                "3 PAGE p. 4",
                "0 TRLR",
                "",
            ]
        )
        (claim,) = [
            c for c in adapted(text).batch.claims if c.property == "name"
        ]
        excerpt = claim.source.transcription_excerpt.text
        assert excerpt.startswith("1 NAME Ida /Löbig/")
        assert "p. 4" in excerpt
